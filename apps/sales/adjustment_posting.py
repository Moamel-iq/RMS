"""
The authoritative sales-adjustment POST and REVERSE.

Same shape as `posting.py` and for the same reasons: one command rather than
three, one `transaction.atomic()`, no second ledger, and no stock movement. A
return moves no stock either — the ingredients left when the kitchen cooked the
plate, and where the returned food is physically thrown away that is a Waste
document in the kitchen's own ledger (ADR-026 §4).

## The journal — one shape, all three reason kinds

For an adjustment line whose original was a **cash or card** sale:

    Dr  SALES_RETURNS                                 adjusted gross
        Cr  SALES_DISCOUNT                            adjusted restaurant discount
        Cr  SALES_CASH_ON_HAND / SALES_CARD_CLEARING  adjusted net

For an adjustment line whose original was an **application** sale:

    Dr  SALES_RETURNS                                 adjusted gross
        Cr  SALES_DISCOUNT                            adjusted restaurant discount
        Cr  DELIVERY_COMMISSION_EXPENSE               adjusted commission
        Cr  DELIVERY_OTHER_FEE_EXPENSE                adjusted other fees
        Cr  DELIVERY_APP_RECEIVABLE                   adjusted net

Balanced by construction, because `net` is the residual of the others.

**`SALES_REVENUE` is never touched, and this is the single most likely
disagreement anybody will have with this module.** Debiting revenue would
restate a posted gross revenue figure and destroy ADR-027 §2's whole point:
revenue is gross, and every deduction sits beside it as an identifiable claim.
`SALES_RETURNS` exists for exactly this, and it is kept apart from
`SALES_DISCOUNT` because a discount is a pricing decision made *before* the sale
and a return is a sale that stopped being one *afterwards*. Netting them would
make a month of generous promotions indistinguishable from a month of rejected
food.

All three reason kinds post this identical journal. They differ in what they may
touch — `FINANCIAL_CORRECTION` may not move quantity — and in what the kitchen's
consumption adapter does with them, not in which account they reach.

The **application-funded** discount appears in neither journal, for the reason it
appears in neither sale journal (ADR-028 §3): the application reimburses it, so
it reduces neither revenue nor what the application owes.
`adjusted_application_discount` is stored, reported, and never posted.

## Lock order

Extends the documented global order rather than reinterpreting it:

    1. the adjustment row                   select_for_update
    2. the organization's mapping resolution
    3. the sales document-number counter    select_for_update
    4. the journal-number counter           inside post_entry

## The receivable ledger, and the suffix that looks like a hack

Posting against application lines writes one `AUTHORIZED_ADJUSTMENT` credit per
application. Reversing writes the mirror debit with the **same** `source` value
and a document id carrying `:REVERSED`.

That suffix is necessary rather than decorative. The ledger's uniqueness key is
`(organization, delivery_application, source, source_document_type,
source_document_id)`. A sale reversal and a settlement reversal each get a
distinct `source`, but ADR-027 §5 fixes that vocabulary at five values and there
is no `ADJUSTMENT_REVERSED` — adding a sixth would be this checkpoint editing an
accepted decision from underneath. The only free component left is the document
id, and it is the one field `canonical_source_identity` deliberately does *not*
case-fold, so a suffix there means exactly what it says. `verify_sales` matches
on the `str(public_id)` prefix.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    DELIVERY_COMMISSION_EXPENSE,
    DELIVERY_OTHER_FEE_EXPENSE,
    SALES_CARD_CLEARING,
    SALES_CASH_ON_HAND,
    SALES_DISCOUNT,
    SALES_RETURNS,
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
from apps.sales.models import (
    ApplicationReceivableEntry,
    ReceivableSource,
    SalesAdjustment,
    SalesAdjustmentLine,
    SalesAdjustmentStatus,
    SalesDayLine,
    SalesDayStatus,
    TenderDestination,
)
from apps.sales.posting import next_document_number

if TYPE_CHECKING:
    from apps.organizations.models import Organization
    from apps.users.models import User

ZERO = Decimal("0")

#: This document's own counter. Never the sales day's — sharing a sequence
#: between two document types is the one thing a gapless sequence must not do.
SALES_ADJUSTMENT_DOCUMENT_TYPE = "SALES_ADJUSTMENT"

#: Written in the **stored** form, upper-case with the dot retained.
#: `canonical_source_identity` case-folds the document type before persisting
#: it, so a constant spelled `sales.SalesAdjustment` would write
#: `SALES.SALESADJUSTMENT` and then fail to find it again — a reversal that
#: could not locate its own journal.
SOURCE_DOCUMENT_TYPE = "SALES.SALESADJUSTMENT"

#: Appended to the receivable entry's document id on reversal. See the module
#: docstring for why the id and not the source carries it.
REVERSAL_RECEIVABLE_ID_SUFFIX = ":REVERSED"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class _Plan:
    """
    Every account and figure resolved before a single effect exists.

    One missing mapping fails here — before a number, a journal line or a
    receivable entry exists — so there is nothing partial to clean up.
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


def _receivable_account(original: SalesDayLine, on_date: datetime.date) -> Account:
    """
    Where this line's receivable credit lands.

    The same rule `posting.py` applies, including the refusal when an override
    is inactive or non-postable: the application's own override wins over the
    organization default, and accounting never learns what a delivery
    application is (ADR-019). A reversal of a sale must reach the account the
    sale reached.
    """
    application = original.delivery_application
    assert application is not None  # noqa: S101 - a check constraint guarantees it
    override = application.receivable_account
    if override is not None:
        if not override.is_active or not override.is_postable:
            raise ValidationError(
                _("The receivable account for %(app)s is not usable.") % {"app": application.code},
                code="receivable_account_unusable",
            )
        return override
    return _account(original.sales_day.organization, DELIVERY_APP_RECEIVABLE, on_date)


def _tender_role(original: SalesDayLine) -> str:
    """Which asset a non-application line's refund comes out of."""
    if original.channel.default_tender == TenderDestination.CARD:
        return SALES_CARD_CLEARING
    return SALES_CASH_ON_HAND


def _cost_center_for(account: Account, lines: list[SalesAdjustmentLine]) -> CostCenter | None:
    """
    The cost centre a contra-revenue or expense line needs.

    Taken from the **channel of the original line**, because a sale never
    invents a cost centre and neither does its reversal: the value is coming
    back out of the department it went into. `SALES_RETURNS` (4-03),
    `SALES_DISCOUNT` (4-02) and the two class-6 fee accounts require one; the
    tender and receivable accounts do not and get none.

    Where an adjustment spans channels with different cost centres, the *first*
    line's is used for the shared accounts — the same bounded simplification
    `posting.build_plan` makes, and with the same honest fix available: a
    per-channel revenue account.
    """
    if not account.requires_cost_center:
        return None
    return lines[0].original_line.channel.cost_center if lines else None


def build_adjustment_plan(adjustment: SalesAdjustment, lines: list[SalesAdjustmentLine]) -> _Plan:
    """
    Resolve every account and figure for an adjustment, without writing.

    Amounts are accumulated **per account** across lines and then netted, so an
    adjustment with forty lines produces a journal a human can read — exactly
    what `posting.build_plan` does with a day.
    """
    organization = adjustment.organization
    on_date = adjustment.business_date

    debits: dict[int, Decimal] = {}
    credits: dict[int, Decimal] = {}
    accounts: dict[int, Account] = {}

    def add(account: Account, *, debit: Decimal = ZERO, credit: Decimal = ZERO) -> None:
        accounts[account.pk] = account
        if debit:
            debits[account.pk] = debits.get(account.pk, ZERO) + debit
        if credit:
            credits[account.pk] = credits.get(account.pk, ZERO) + credit

    returns_account = _account(organization, SALES_RETURNS, on_date)
    discount_account = _account(organization, SALES_DISCOUNT, on_date)

    receivable_by_application: dict[int, Decimal] = {}
    gross_total = ZERO
    restaurant_discount_total = ZERO
    commission_total = ZERO
    other_fee_total = ZERO
    net_cash = ZERO
    net_card = ZERO

    for line in lines:
        original = line.original_line
        gross_total += line.adjusted_gross
        restaurant_discount_total += line.adjusted_restaurant_discount

        # **Not `SALES_REVENUE`.** See the module docstring: revenue is gross
        # and stays gross, and what came back is its own identifiable claim.
        add(returns_account, debit=line.adjusted_gross)

        if line.adjusted_restaurant_discount:
            add(discount_account, credit=line.adjusted_restaurant_discount)

        if original.is_application_sale:
            commission_total += line.adjusted_commission
            other_fee_total += line.adjusted_other_fees
            if line.adjusted_commission:
                add(
                    _account(organization, DELIVERY_COMMISSION_EXPENSE, on_date),
                    credit=line.adjusted_commission,
                )
            if line.adjusted_other_fees:
                add(
                    _account(organization, DELIVERY_OTHER_FEE_EXPENSE, on_date),
                    credit=line.adjusted_other_fees,
                )
            add(_receivable_account(original, on_date), credit=line.adjusted_net_amount)
            application_id = original.delivery_application_id
            assert application_id is not None  # noqa: S101 - guaranteed above
            receivable_by_application[application_id] = (
                receivable_by_application.get(application_id, ZERO) + line.adjusted_net_amount
            )
            continue

        add(
            _account(organization, _tender_role(original), on_date), credit=line.adjusted_net_amount
        )
        if original.channel.default_tender == TenderDestination.CARD:
            net_card += line.adjusted_net_amount
        else:
            net_cash += line.adjusted_net_amount

    posting_lines: list[PostingLine] = []
    for account_id in sorted(accounts, key=lambda pk: accounts[pk].code):
        account = accounts[account_id]
        net = debits.get(account_id, ZERO) - credits.get(account_id, ZERO)
        if net == ZERO:
            continue
        posting_lines.append(
            PostingLine(
                account=account,
                branch=adjustment.branch,
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


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def _refuse_over_adjustment_at_posting(lines: list[SalesAdjustmentLine]) -> None:
    """
    Re-check containment under the row lock, immediately before posting.

    The drafting service already checked, and `0008`'s trigger checks on every
    write of a line — but neither covers this moment. Two drafts may each
    legitimately propose the whole of a line; the trigger counts only *posted*
    adjustments, so both drafts pass, and the second one becomes wrong only when
    the first posts. This is the check that catches it, and it has to be here
    rather than at drafting because that is where the fact changes.
    """
    for line in lines:
        original = line.original_line
        claimed = SalesAdjustmentLine.objects.filter(
            original_line=original, adjustment__status=SalesAdjustmentStatus.POSTED
        ).aggregate(quantity=Sum("adjusted_quantity"), gross=Sum("adjusted_gross"))
        claimed_quantity = claimed["quantity"] or ZERO
        claimed_gross = claimed["gross"] or ZERO
        if claimed_quantity + line.adjusted_quantity > original.quantity:
            raise ValidationError(
                _("Another adjustment has already taken back part of %(item)s.")
                % {"item": original.menu_item.code},
                code="quantity_over_adjusted",
            )
        if claimed_gross + line.adjusted_gross > original.gross_amount:
            raise ValidationError(
                _("Another adjustment has already taken back part of %(item)s.")
                % {"item": original.menu_item.code},
                code="gross_over_adjusted",
            )


@transaction.atomic
def post_sales_adjustment(*, adjustment: SalesAdjustment, actor: User) -> SalesAdjustment:
    """
    Post a draft adjustment to the ledger and the receivable, atomically.

    The idempotency key is derived from the adjustment's own `public_id` rather
    than accepted from the caller: posting *this adjustment* is the command, the
    adjustment is the payload, and a posted adjustment is frozen by a database
    trigger — so a retry cannot present the same key with a different payload.
    The kernel still refuses a duplicate on the source identity independently.
    """
    locked = SalesAdjustment.objects.select_for_update().get(pk=adjustment.pk)
    if locked.status == SalesAdjustmentStatus.POSTED:
        raise ValidationError(_("This adjustment is already posted."), code="already_posted")
    if locked.status == SalesAdjustmentStatus.REVERSED:
        raise ValidationError(
            _("A reversed adjustment cannot be posted again. Record a new one."),
            code="adjustment_reversed",
        )
    # Re-read at posting, not only at drafting. `0008`'s containment trigger
    # checks the day's status when a *line* is written, and a draft written
    # while the day was posted survives the day being reversed afterwards.
    # Posting it then would credit `SALES_RETURNS` against a sale the day's own
    # reversal has already taken back — the mirror of the rule
    # `reverse_sales_day` enforces from the other side.
    if locked.sales_day.status != SalesDayStatus.POSTED:
        raise ValidationError(
            _("The sales day this corrects is no longer posted. There is nothing to take back."),
            code="day_not_posted",
        )

    lines = list(
        locked.lines.select_related(
            "original_line",
            "original_line__menu_item",
            "original_line__channel",
            "original_line__channel__cost_center",
            "original_line__delivery_application",
            "original_line__delivery_application__receivable_account",
            "original_line__sales_day",
            "original_line__sales_day__organization",
        ).order_by("sequence")
    )
    if not lines:
        raise ValidationError(_("An empty adjustment cannot be posted."), code="no_lines")

    _refuse_over_adjustment_at_posting(lines)

    plan = build_adjustment_plan(locked, lines)
    if not plan.posting_lines:  # pragma: no cover - an empty adjustment is refused above
        raise ValidationError(_("This adjustment posts nothing."), code="nothing_to_post")

    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        number = next_document_number(
            organization=locked.organization,
            document_type=SALES_ADJUSTMENT_DOCUMENT_TYPE,
            prefix="SA",
            year=locked.business_date.year,
        )
        entry = post_entry(
            organization=locked.organization,
            accounting_date=locked.business_date,
            document_date=locked.business_date,
            lines=plan.posting_lines,
            idempotency_key=f"sales-adjustment:{locked.public_id}",
            narration=f"{number} · {locked.branch.code} {locked.get_reason_kind_display()}",
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
            source_event=SourceEvent.POSTED,
        )

        for application_id, amount in sorted(plan.receivable_by_application.items()):
            ApplicationReceivableEntry.objects.create(
                organization=locked.organization,
                branch=locked.branch,
                delivery_application_id=application_id,
                business_date=locked.business_date,
                source=ReceivableSource.AUTHORIZED_ADJUSTMENT,
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=str(locked.public_id),
                credit=quantize_money(amount),
                narration=str(_("تسوية مبيعات %(number)s")) % {"number": number},
            )

        previous = snapshot(locked)
        locked.status = SalesAdjustmentStatus.POSTED
        locked.number = number
        locked.posted_by = actor
        locked.posted_at = timezone.now()
        locked.idempotency_key = f"sales-adjustment:{locked.public_id}"
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
                "reason_kind": locked.reason_kind,
                "gross": str(plan.gross_total),
                "restaurant_discount": str(plan.restaurant_discount_total),
                "commission": str(plan.commission_total),
            },
        )
    return locked


@transaction.atomic
def reverse_sales_adjustment(
    *, adjustment: SalesAdjustment, actor: User, reason: str
) -> SalesAdjustment:
    """
    Undo a posted adjustment: reverse its journal and mirror its receivable.

    Nothing is re-decided. The mirroring receivable entry carries the amount the
    adjustment wrote, read from the ledger rather than recomputed from the
    lines, because recomputing would silently pick up any master-data change
    since — exactly the drift a reversal must not introduce. Only its *date* is
    current, because undoing something is an event that happens now.
    """
    if not reason.strip():
        raise ValidationError(_("Reversing an adjustment needs a reason."), code="reason_required")

    locked = SalesAdjustment.objects.select_for_update().get(pk=adjustment.pk)
    if locked.status == SalesAdjustmentStatus.REVERSED:
        raise ValidationError(_("This adjustment is already reversed."), code="already_reversed")
    if locked.status != SalesAdjustmentStatus.POSTED:
        raise ValidationError(
            _("Only a posted adjustment can be reversed."), code="adjustment_not_posted"
        )

    entry = JournalEntry.objects.filter(
        organization=locked.organization,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
    ).first()
    if entry is None:  # pragma: no cover - a posted adjustment always has one
        raise ValidationError(
            _("The journal for this adjustment is missing."), code="journal_missing"
        )

    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        reverse_entry(
            entry=entry,
            idempotency_key=f"sales-adjustment-reversal:{locked.public_id}",
            reason=reason.strip(),
            accounting_date=timezone.localdate(),
        )

        original_entries = ApplicationReceivableEntry.objects.filter(
            organization=locked.organization,
            source=ReceivableSource.AUTHORIZED_ADJUSTMENT,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
        )
        for row in original_entries:
            ApplicationReceivableEntry.objects.create(
                organization=row.organization,
                branch=row.branch,
                delivery_application=row.delivery_application,
                business_date=locked.business_date,
                source=ReceivableSource.AUTHORIZED_ADJUSTMENT,
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=f"{locked.public_id}{REVERSAL_RECEIVABLE_ID_SUFFIX}",
                debit=row.credit,
                narration=str(_("عكس تسوية مبيعات %(number)s")) % {"number": locked.number},
            )

        previous = snapshot(locked)
        locked.status = SalesAdjustmentStatus.REVERSED
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
    "REVERSAL_RECEIVABLE_ID_SUFFIX",
    "SALES_ADJUSTMENT_DOCUMENT_TYPE",
    "SOURCE_DOCUMENT_TYPE",
    "build_adjustment_plan",
    "post_sales_adjustment",
    "reverse_sales_adjustment",
]
