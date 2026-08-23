"""
The authoritative sales POST and REVERSE.

Checkpoints 1 and 2 built the masters and refused to post anything. This module
crosses that boundary, and the shape of the crossing is the whole design.

**There is one command, not three.** No `post_sales_journal` sits beside a
`write_receivable_entries` and a `stamp_the_number`, because a system that can
do the first without the second will eventually do exactly that, on the night
somebody's mapping is missing. `post_sales_day` writes the status, the gapless
number, the balanced journal, the application receivable entries and the audit
event inside one `transaction.atomic()`. A failure anywhere leaves the day
`SUBMITTED` and both ledgers untouched.

**It adds no second ledger.** Value reaches the accounts through `post_entry`,
the same function every other module's documents use, with the same period
validation, the same source-identity uniqueness and the same immutability
triggers. What is new here is a *document* that calls it.

**The fulfillment route decides stock.** A recipe serving consumes nothing at
the moment of sale: the ingredients left when the kitchen cooked them, through
a production batch that already posted its own movements. A direct-stock resale
item (a sealed bottle of water, for example) is different: posting the sales day
issues its frozen base quantity and merges that exact moving-average COGS into
this same journal. The two routes are mutually exclusive in the database, so
rice can never be counted twice as both recipe consumption and a sales issue.

## The journals

For an application line (ADR-027 §6):

    Dr  DELIVERY_APP_RECEIVABLE      gross − restaurant discount
                                     − commission − other fees
    Dr  SALES_DISCOUNT               restaurant-funded discount
    Dr  DELIVERY_COMMISSION_EXPENSE  commission
    Dr  DELIVERY_OTHER_FEE_EXPENSE   other fees, where any
        Cr  SALES_REVENUE                                    gross

For a cash or card line (§7):

    Dr  SALES_CASH_ON_HAND / SALES_CARD_CLEARING   gross − restaurant discount
    Dr  SALES_DISCOUNT                             restaurant-funded discount
        Cr  SALES_REVENUE                                             gross

The **application-funded** discount appears in neither, and that absence is the
single most consequential line in this module. The application reimburses it,
so it reduces neither revenue nor the amount owed — posting it as a restaurant
discount would understate both revenue and the receivable by the same amount,
and both figures would look internally consistent afterwards (ADR-028 §3).

## Lock order

Extends the documented global order rather than reinterpreting it:

    1. the sales day row                    select_for_update
    2. the organization's mapping resolution
    3. the sales document-number counter    select_for_update
    4. the journal-number counter           inside post_entry

Nothing takes a row lock between steps 2 and 4.

## What a reversal does and does not re-decide

A reversal mirrors what posted. It credits what the original debited, writes
the opposite receivable entry, and reads no mapping and no rate again: the
question "what did this day do" has one answer and it was settled the day it
posted. Only its *date* is current, because undoing something is an event that
happens now.

It also **refuses a day that has already been corrected.** A posted
`SalesAdjustment` has taken part of this day back once; reversing the whole day
would take the same sale back twice, in both ledgers, by the same amount — so
the two agree with each other and disagree with reality. See
`reverse_sales_day` and migration `0013`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    DELIVERY_COMMISSION_EXPENSE,
    DELIVERY_OTHER_FEE_EXPENSE,
    SALES_CARD_CLEARING,
    SALES_CASH_ON_HAND,
    SALES_DISCOUNT,
    SALES_REVENUE,
    Account,
    CostCenter,
    JournalEntry,
    SourceEvent,
)
from apps.accounting.services import post_entry, resolve_default_account, reverse_entry
from apps.accounting.validators import PostingLine
from apps.core.context import audit_context, get_correlation_id
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.inventory.sales import (
    DirectStockSaleLine,
    link_sales_stock_journal,
    plan_sales_stock,
    post_sales_stock,
    reverse_sales_stock,
)
from apps.sales.models import (
    ApplicationReceivableEntry,
    FulfillmentSource,
    ReceivableSource,
    SalesAdjustment,
    SalesAdjustmentStatus,
    SalesDay,
    SalesDayLine,
    SalesDayStatus,
    SalesDayStockPosting,
    SalesDirectStockFulfillment,
    SalesDocumentSequence,
    TenderDestination,
)

if TYPE_CHECKING:
    from apps.organizations.models import Organization
    from apps.users.models import User

ZERO = Decimal("0")

#: The document type this module counts. Its own value rather than a reused
#: inventory or procurement one: a sales day is not their document, and sharing
#: a counter is the one thing a gapless sequence must never do.
SALES_DAY_DOCUMENT_TYPE = "SALES_DAY"

#: What a sales journal calls itself in the ledger's source identity. Matched
#: by the verifier and by every reconciliation that asks "which document
#: produced this entry" (ADR-017).
#:
#: **Written in the stored form, upper-case.** `canonical_source_identity`
#: case-folds the document type before persisting it, so a constant spelled
#: `sales.SalesDay` would write `SALES.SALESDAY` and then fail to find it
#: again — a reversal that could not locate its own journal. Spelling it the
#: way it is stored makes the write and the lookup provably the same string.
SOURCE_DOCUMENT_TYPE = "SALES.SALESDAY"


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------


def next_document_number(
    *, organization: Organization, document_type: str, prefix: str, year: int
) -> str:
    """
    The next gapless number for this organization, document type and year.

    A row taken with `select_for_update` rather than `MAX(number) + 1`, because
    two concurrent postings reading the same maximum would produce two documents
    claiming the same number. Four lines, and deliberately identical to the
    three sequence helpers already in this system.

    Parameterised by `document_type` rather than copied per document, because
    Phase 4 has four numbered documents and four copies of this would be four
    chances to draw from the wrong counter — which is the one thing a gapless
    sequence must never do. What must stay separate is the *counter*, and the
    unique constraint on `(organization, document_type, year)` is what keeps it
    separate.
    """
    sequence, _created = SalesDocumentSequence.objects.select_for_update().get_or_create(
        organization=organization, document_type=document_type, year=year
    )
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return f"{prefix}-{year}-{sequence.last_number:05d}"


def _next_number(organization: Organization, year: int) -> str:
    """The next sales-day number."""
    return next_document_number(
        organization=organization,
        document_type=SALES_DAY_DOCUMENT_TYPE,
        prefix="SD",
        year=year,
    )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Bucket:
    """One account and the amount that lands in it."""

    account: Account
    debit: Decimal = ZERO
    credit: Decimal = ZERO


@dataclass
class _Plan:
    """
    Every account and figure resolved before a single effect exists.

    One missing mapping fails here — before a number, a journal line or a
    receivable entry has been written — so there is nothing partial to clean
    up. The same discipline `post_goods_receipt._plan` follows, and for the
    same reason.
    """

    posting_lines: list[PostingLine]
    receivable_by_application: dict[int, Decimal]
    gross_total: Decimal
    restaurant_discount_total: Decimal
    commission_total: Decimal
    other_fee_total: Decimal
    net_cash: Decimal
    net_card: Decimal


def _account(organization: Organization, role: str, on_date: datetime.date) -> Account:
    """The organization's account for a role on a date, or a refusal."""
    return resolve_default_account(
        organization=organization, account_role=role, on_date=on_date
    ).account


def _receivable_account(line: SalesDayLine, on_date: datetime.date) -> Account:
    """
    Where this line's receivable lands.

    The application's own override wins over the organization default, which is
    the same arrangement inventory uses for item-level control accounts: the
    domain that owns the concept carries the override, and accounting never
    learns what a delivery application is (ADR-019).
    """
    application = line.delivery_application
    assert application is not None  # noqa: S101 - a check constraint guarantees it
    override = application.receivable_account
    if override is not None:
        if not override.is_active or not override.is_postable:
            raise ValidationError(
                _("The receivable account for %(app)s is not usable.") % {"app": application.code},
                code="receivable_account_unusable",
            )
        return override
    return _account(line.sales_day.organization, DELIVERY_APP_RECEIVABLE, on_date)


def _tender_role(line: SalesDayLine) -> str:
    """Which asset a non-application line's takings land in."""
    if line.channel.default_tender == TenderDestination.CARD:
        return SALES_CARD_CLEARING
    return SALES_CASH_ON_HAND


def build_plan(day: SalesDay, lines: list[SalesDayLine]) -> _Plan:
    """
    Resolve every account and figure for a day, without writing anything.

    Debits are accumulated per account rather than per line, so a day with two
    hundred lines produces a journal a human can read. The totals are sums of
    stored three-decimal line values and are never rounded on their own
    (ADR-012): a document total is the sum of its posted lines.
    """
    organization = day.organization
    on_date = day.business_date

    debits: dict[int, Decimal] = {}
    credits: dict[int, Decimal] = {}
    accounts: dict[int, Account] = {}

    def add(account: Account, *, debit: Decimal = ZERO, credit: Decimal = ZERO) -> None:
        accounts[account.pk] = account
        if debit:
            debits[account.pk] = debits.get(account.pk, ZERO) + debit
        if credit:
            credits[account.pk] = credits.get(account.pk, ZERO) + credit

    revenue_default = _account(organization, SALES_REVENUE, on_date)
    discount_account = _account(organization, SALES_DISCOUNT, on_date)

    receivable_by_application: dict[int, Decimal] = {}
    gross_total = ZERO
    restaurant_discount_total = ZERO
    commission_total = ZERO
    other_fee_total = ZERO
    net_cash = ZERO
    net_card = ZERO

    for line in lines:
        gross_total += line.gross_amount
        restaurant_discount_total += line.restaurant_discount

        # Revenue is **gross**, always. A deduction is a separate line beside
        # it, never a subtraction inside the credit: a revenue figure with
        # discounts already inside it cannot answer "what did we give away".
        revenue_account = line.channel.revenue_account or revenue_default
        add(revenue_account, credit=line.gross_amount)

        if line.restaurant_discount:
            add(discount_account, debit=line.restaurant_discount)

        if line.is_application_sale:
            commission_total += line.commission_amount
            other_fee_total += line.other_fee_amount
            if line.commission_amount:
                add(
                    _account(organization, DELIVERY_COMMISSION_EXPENSE, on_date),
                    debit=line.commission_amount,
                )
            if line.other_fee_amount:
                add(
                    _account(organization, DELIVERY_OTHER_FEE_EXPENSE, on_date),
                    debit=line.other_fee_amount,
                )
            receivable = _receivable_account(line, on_date)
            add(receivable, debit=line.net_amount)
            application_id = line.delivery_application_id
            assert application_id is not None  # noqa: S101 - guaranteed above
            receivable_by_application[application_id] = (
                receivable_by_application.get(application_id, ZERO) + line.net_amount
            )
            continue

        # Cash or card. The tender is the channel's, resolved through a role.
        add(_account(organization, _tender_role(line), on_date), debit=line.net_amount)
        if line.channel.default_tender == TenderDestination.CARD:
            net_card += line.net_amount
        else:
            net_cash += line.net_amount

    posting_lines: list[PostingLine] = []
    for account_id in sorted(accounts, key=lambda pk: accounts[pk].code):
        account = accounts[account_id]
        debit = debits.get(account_id, ZERO)
        credit = credits.get(account_id, ZERO)
        # An account that both gained and lost within one day nets, rather than
        # producing two lines that a reader has to add up.
        net = debit - credit
        if net == ZERO:
            continue
        posting_lines.append(
            PostingLine(
                account=account,
                branch=day.branch,
                cost_center=_cost_center_for(account, lines),
                debit=net if net > ZERO else ZERO,
                credit=-net if net < ZERO else ZERO,
            )
        )

    return _Plan(
        posting_lines=posting_lines,
        receivable_by_application=receivable_by_application,
        gross_total=quantize_money(gross_total),
        restaurant_discount_total=quantize_money(restaurant_discount_total),
        commission_total=quantize_money(commission_total),
        other_fee_total=quantize_money(other_fee_total),
        net_cash=quantize_money(net_cash),
        net_card=quantize_money(net_card),
    )


def _cost_center_for(account: Account, lines: list[SalesDayLine]) -> CostCenter | None:
    """
    The cost centre a revenue, cost-of-sales or expense line needs.

    Taken from the **channel**, which is the only master with the right meaning
    and the right granularity: dine-in earns in the hall, application orders
    earn in delivery. Balance-sheet accounts need none and get none — offering
    one would be a dimension the account never receives.

    Where a day spans channels with different cost centres, the *first* line's
    is used for the shared discount and revenue accounts. That is a real
    simplification and it is bounded: it only bites when one organization maps
    every channel to one revenue account **and** splits its cost centres, and
    the honest fix is a per-channel revenue account, which the channel already
    supports.
    """
    if not account.requires_cost_center:
        return None
    return lines[0].channel.cost_center if lines else None


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@transaction.atomic
def post_sales_day(*, day: SalesDay, actor: User) -> SalesDay:
    """
    Post a submitted day to the ledger and the receivable, atomically.

    The idempotency key is derived from the day's own `public_id` rather than
    accepted from the caller. Posting *this day* is the command, the day is the
    payload, and a posted day is frozen by a database trigger — so a retry
    cannot present the same key with a different payload, and there is no
    second key vocabulary for anybody to get wrong. The kernel still refuses a
    duplicate on the source identity independently.
    """
    locked = SalesDay.objects.select_for_update().get(pk=day.pk)
    if locked.status == SalesDayStatus.POSTED:
        raise ValidationError(_("This sales day is already posted."), code="already_posted")
    if locked.status == SalesDayStatus.REVERSED:
        raise ValidationError(
            _("A reversed sales day cannot be posted again. Record a new day."),
            code="day_reversed",
        )
    if locked.status != SalesDayStatus.SUBMITTED:
        raise ValidationError(
            _("Only a submitted sales day can be posted."), code="day_not_submitted"
        )

    lines = list(
        locked.lines.select_related(
            "menu_item",
            "channel",
            "channel__cost_center",
            "channel__revenue_account",
            "delivery_application",
            "delivery_application__receivable_account",
            "recipe",
            "recipe_version",
            "serving",
            "inventory_item",
            "source_warehouse",
            "agreement",
        ).order_by("sequence")
    )
    if not lines:
        raise ValidationError(_("An empty sales day cannot be posted."), code="no_lines")

    plan = build_plan(locked, lines)
    if not plan.posting_lines:  # pragma: no cover - an empty day is refused above
        raise ValidationError(_("This sales day posts nothing."), code="nothing_to_post")

    direct_lines = [
        DirectStockSaleLine(
            line_key=str(line.public_id),
            warehouse=line.source_warehouse,
            item=line.inventory_item,
            quantity=line.direct_stock_total_base_qty,
            cost_center=line.channel.cost_center,
        )
        for line in lines
        if line.fulfillment_source == FulfillmentSource.DIRECT_STOCK
    ]
    # Planning resolves every inventory-consumption account before a document
    # number or stock effect exists.  A missing mapping therefore leaves this
    # submitted day completely untouched.
    stock_plan = (
        plan_sales_stock(
            organization=locked.organization,
            branch=locked.branch,
            business_date=locked.business_date,
            lines=direct_lines,
        )
        if direct_lines
        else None
    )

    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        number = _next_number(locked.organization, locked.business_date.year)
        stock_posting = None
        if stock_plan is not None:
            stock_posting = post_sales_stock(
                plan=stock_plan,
                effective_at=timezone.now(),
                idempotency_key=f"sales-stock:{locked.public_id}",
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=str(locked.public_id),
                reference=number,
                reason=f"Direct-stock fulfillment for {number}",
            )
        entry = post_entry(
            organization=locked.organization,
            accounting_date=locked.business_date,
            document_date=locked.business_date,
            lines=[
                *plan.posting_lines,
                *(stock_posting.posting_lines if stock_posting is not None else ()),
            ],
            idempotency_key=f"sales-day:{locked.public_id}",
            narration=f"{number} · {locked.branch.code} {locked.business_date.isoformat()}",
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
            source_event=SourceEvent.POSTED,
        )

        if stock_posting is not None:
            link_sales_stock_journal(entry=stock_posting.entry, journal=entry)
            SalesDayStockPosting.objects.create(
                sales_day=locked,
                stock_entry=stock_posting.entry,
            )
            sales_lines_by_key = {str(line.public_id): line for line in lines}
            for evidence in stock_posting.evidence:
                source_line = sales_lines_by_key[evidence.line_key]
                SalesDirectStockFulfillment.objects.create(
                    sales_line=source_line,
                    stock_movement=evidence.movement,
                    consumption_account=evidence.consumption.account,
                    cost_center=evidence.cost_center,
                    base_quantity=-evidence.movement.base_quantity,
                    cogs_value=evidence.cogs_value,
                )

        for application_id, amount in sorted(plan.receivable_by_application.items()):
            ApplicationReceivableEntry.objects.create(
                organization=locked.organization,
                branch=locked.branch,
                delivery_application_id=application_id,
                business_date=locked.business_date,
                source=ReceivableSource.SALE_POSTED,
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=str(locked.public_id),
                debit=quantize_money(amount),
                narration=str(_("مبيعات مرحّلة %(number)s")) % {"number": number},
            )

        previous = snapshot(locked)
        locked.status = SalesDayStatus.POSTED
        locked.number = number
        locked.posted_by = actor
        locked.posted_at = timezone.now()
        locked.idempotency_key = f"sales-day:{locked.public_id}"
        locked.save(
            update_fields=[
                "status",
                "number",
                "posted_by",
                "posted_at",
                "idempotency_key",
                "updated_at",
            ]
        )
        record_audit_event(
            action=AuditAction.POSTED,
            target=locked,
            previous_state=previous,
            new_state=snapshot(locked),
            branch=locked.branch,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
            metadata={
                "journal_entry": entry.entry_number,
                "gross": str(plan.gross_total),
                "restaurant_discount": str(plan.restaurant_discount_total),
                "commission": str(plan.commission_total),
                "direct_stock_cogs": str(
                    stock_posting.total_cost if stock_posting is not None else ZERO
                ),
            },
        )
    return locked


@transaction.atomic
def reverse_sales_day(*, day: SalesDay, actor: User, reason: str) -> SalesDay:
    """
    Undo a posted day: reverse its journal and write the opposite receivable.

    Nothing is re-decided. The reversing receivable entry carries the same
    amount the sale wrote, read from the ledger rather than recomputed from the
    lines, because recomputing would silently pick up any master-data change
    since — which is exactly the drift a reversal must not introduce.

    **A day that has already been corrected may not be reversed.** A posted
    adjustment has un-recognised part of this day once, in the general ledger
    and in the receivable subledger both; reversing the whole day afterwards
    un-recognises the same sale a second time. No verifier catches it, and the
    reason is worth stating: the two ledgers are wrong by the *identical*
    amount, so `verify_receivable_ledger` finds them in perfect agreement,
    while `verify_adjustments_are_within_their_originals` reads the original
    line's quantity and gross, which a reversal never touches. What is left is
    a receivable with a credit balance the application never owed and a class-6
    expense account holding a credit.

    The remedy is to reverse the adjustment first, which puts the day back into
    the state its own reversal describes. Enforced here and by
    `0013`'s trigger, because the status change is an ordinary `UPDATE`.
    """
    if not reason.strip():
        raise ValidationError(_("Reversing a sales day needs a reason."), code="reason_required")

    locked = SalesDay.objects.select_for_update().get(pk=day.pk)
    if locked.status == SalesDayStatus.REVERSED:
        raise ValidationError(_("This sales day is already reversed."), code="already_reversed")
    if locked.status != SalesDayStatus.POSTED:
        raise ValidationError(_("Only a posted sales day can be reversed."), code="day_not_posted")

    corrections = list(
        SalesAdjustment.objects.filter(sales_day_id=locked.pk, status=SalesAdjustmentStatus.POSTED)
        .order_by("number")
        .values_list("number", flat=True)
    )
    if corrections:
        raise ValidationError(
            _(
                "This sales day has posted corrections against it (%(numbers)s). "
                "Reverse them first: reversing the day as well would take the same "
                "sale back twice."
            )
            % {"numbers": "، ".join(corrections)},
            code="day_has_posted_adjustments",
        )

    entry = JournalEntry.objects.filter(
        organization=locked.organization,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
    ).first()
    if entry is None:  # pragma: no cover - a posted day always has one
        raise ValidationError(
            _("The journal for this sales day is missing."), code="journal_missing"
        )

    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        # The reversal's own key, derived from the day rather than accepted:
        # reversing *this day* is the command, and a posted day is frozen, so a
        # retry cannot present the same key with a different payload.
        stock_reversal = None
        try:
            stock_link = locked.direct_stock_posting
        except SalesDayStockPosting.DoesNotExist:
            stock_link = None
        if stock_link is not None:
            stock_reversal = reverse_sales_stock(
                entry=stock_link.stock_entry,
                idempotency_key=f"sales-stock-reversal:{locked.public_id}",
                reason=reason.strip(),
                effective_at=timezone.now(),
                business_date=timezone.localdate(),
            )

        journal_reversal = reverse_entry(
            entry=entry,
            idempotency_key=f"sales-day-reversal:{locked.public_id}",
            reason=reason.strip(),
            accounting_date=timezone.localdate(),
        )
        if stock_reversal is not None:
            link_sales_stock_journal(entry=stock_reversal, journal=journal_reversal)

        original_entries = ApplicationReceivableEntry.objects.filter(
            organization=locked.organization,
            source=ReceivableSource.SALE_POSTED,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
        )
        for row in original_entries:
            ApplicationReceivableEntry.objects.create(
                organization=row.organization,
                branch=row.branch,
                delivery_application=row.delivery_application,
                business_date=locked.business_date,
                source=ReceivableSource.SALE_REVERSED,
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=str(locked.public_id),
                credit=row.debit,
                narration=str(_("عكس مبيعات %(number)s")) % {"number": locked.number},
            )

        previous = snapshot(locked)
        locked.status = SalesDayStatus.REVERSED
        locked.reversed_by = actor
        locked.reversed_at = timezone.now()
        locked.reversal_reason = reason.strip()
        locked.save(
            update_fields=[
                "status",
                "reversed_by",
                "reversed_at",
                "reversal_reason",
                "updated_at",
            ]
        )
        record_audit_event(
            action=AuditAction.REVERSED,
            target=locked,
            previous_state=previous,
            new_state=snapshot(locked),
            reason=reason.strip(),
            branch=locked.branch,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
        )
    return locked


__all__ = [
    "SALES_DAY_DOCUMENT_TYPE",
    "SOURCE_DOCUMENT_TYPE",
    "build_plan",
    "next_document_number",
    "post_sales_day",
    "reverse_sales_day",
]
