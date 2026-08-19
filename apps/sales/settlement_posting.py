"""
The authoritative application-settlement POST and REVERSE.

Same shape as `posting.py` and `adjustment_posting.py`, and for the same
reasons: one command rather than three, one `transaction.atomic()`, no second
ledger, and no stock movement.

## The journal

    Dr  SALES_SETTLEMENT_BANK | SALES_CASH_ON_HAND    remitted amount
    Dr  DELIVERY_SETTLEMENT_VARIANCE                  total variance, when > 0
        Cr  DELIVERY_SETTLEMENT_VARIANCE              −total variance, when < 0
        Cr  DELIVERY_APP_RECEIVABLE                   expected amount

with `total_variance = expected_amount − remitted_amount`. The debit side is the
asset actually received plus the shortfall recognised; the credit side is the
receivable cleared. A `remitted_amount` of zero is legal — a fully offset
statement, where the application withheld the whole period against a penalty —
and simply omits that line.

Every explained difference lands in `DELIVERY_SETTLEMENT_VARIANCE`, whichever
leg and whichever reason claimed it. The leg and the reason are analytic
dimensions on the document, not different accounts: they answer *where* the
difference arose, and inventing a second variance role per reason would spread
one commercial fact across a chart nobody could add back up.

## What this journal must never contain

**A class-6 commission line.** Commission was accrued at the sale (ADR-028 §4).
`statement_commission_amount` is stored so it can be compared against
`accrued_commission_for`, is reported as `commission_gap`, and reaches
`DELIVERY_COMMISSION_EXPENSE` never. Expensing it again overstates selling
expense and understates gross margin by the same amount — both individually
defensible, which is exactly why nobody finds it. ADR-028 §6 names this the
single most likely error in the whole module and `verify_sales` checks for it by
name.

**`SALES_REVENUE`.** A settlement is the collection of a receivable, not a sale.

## Lock order

Extends the documented global order rather than reinterpreting it:

    1. the settlement row                   select_for_update
    2. the organization's mapping resolution
    3. the sales document-number counter    select_for_update
    4. the journal-number counter           inside post_entry

## The receivable ledger

Posting writes one `SETTLEMENT` credit for `expected_amount`. Reversal writes
`SETTLEMENT_REVERSED` with the same amount as a debit and the **same** document
id — the paired source value exists in ADR-027 §5's closed vocabulary here, so
no id suffix is used or needed, unlike the adjustment reversal which has no
paired value to use.

Reversal also returns the allocations to open. The allocation rows stay, because
they are evidence of what was claimed; `receivables.unallocated_debit` and
`0010`'s containment trigger both count only allocations belonging to **posted**
settlements, which is what makes the entries claimable again without deleting
the history of the claim that failed.
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
    DELIVERY_SETTLEMENT_VARIANCE,
    SALES_CASH_ON_HAND,
    SALES_SETTLEMENT_BANK,
    Account,
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
    DeliveryApplicationSettlement,
    ReceivableSource,
    SettlementRemittance,
    SettlementStatus,
)
from apps.sales.posting import next_document_number
from apps.sales.settlement_services import allocated_total, three_way_for

if TYPE_CHECKING:
    from apps.organizations.models import Organization
    from apps.users.models import User

ZERO = Decimal("0")

#: This document's own counter. Never the sales day's or the adjustment's —
#: sharing a sequence between two document types is the one thing a gapless
#: sequence must not do.
APPLICATION_SETTLEMENT_DOCUMENT_TYPE = "APPLICATION_SETTLEMENT"

#: Written in the **stored** form, upper-case with the dot retained.
#: `canonical_source_identity` case-folds the document type before persisting
#: it, so a constant spelled `sales.DeliveryApplicationSettlement` would write
#: `SALES.DELIVERYAPPLICATIONSETTLEMENT` and then fail to find it again.
SOURCE_DOCUMENT_TYPE = "SALES.DELIVERYAPPLICATIONSETTLEMENT"


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
    expected_amount: Decimal
    remitted_amount: Decimal
    total_variance: Decimal
    receivable_account: Account


def _account(organization: Organization, role: str, on_date: datetime.date) -> Account:
    """The organization's account for a role on a date, or a refusal."""
    return resolve_default_account(
        organization=organization, account_role=role, on_date=on_date
    ).account


def _receivable_account(
    settlement: DeliveryApplicationSettlement, on_date: datetime.date
) -> Account:
    """
    Where the receivable credit lands.

    The same rule `posting.py` applies, including the refusal when an override
    is inactive or non-postable: the application's own override wins over the
    organization default, and accounting never learns what a delivery
    application is (ADR-019). A settlement must clear the account the sale
    debited, or the receivable balance and the ledger balance part company.
    """
    override = settlement.delivery_application.receivable_account
    if override is not None:
        if not override.is_active or not override.is_postable:
            raise ValidationError(
                _("The receivable account for %(app)s is not usable.")
                % {"app": settlement.delivery_application.code},
                code="receivable_account_unusable",
            )
        return override
    return _account(settlement.organization, DELIVERY_APP_RECEIVABLE, on_date)


def _remittance_role(settlement: DeliveryApplicationSettlement) -> str:
    """Which asset received the money. The role resolves the account, never an id."""
    if settlement.remittance_destination == SettlementRemittance.CASH:
        return SALES_CASH_ON_HAND
    return SALES_SETTLEMENT_BANK


def _refuse_a_required_cost_center(account: Account) -> None:
    """
    None of this journal's accounts should require a cost centre.

    1-01, 1-02 and 7-09 are all balance-sheet or variance accounts and a
    settlement has no principled source for a dimension: the statement covers a
    period across every channel the application took orders through, and picking
    one channel's cost centre would attribute a whole month's variance to
    whichever line happened to be first.

    So the service **refuses** rather than inventing one. A refusal is a
    mapping to fix; a fabricated dimension is a report that is quietly wrong.
    """
    if account.requires_cost_center:
        raise ValidationError(
            _("%(account)s requires a cost centre, and a settlement has none to give.")
            % {"account": account.code},
            code="cost_center_required",
        )


def build_settlement_plan(settlement: DeliveryApplicationSettlement) -> _Plan:
    """
    Resolve every account and figure for a settlement, without writing.

    `expected_amount` is read from the stamp made at reconciliation rather than
    recomputed, and then checked against Σ allocations by the caller — the stamp
    is what was declared, the sum is what is there, and posting is the moment
    the two must still agree.
    """
    organization = settlement.organization
    on_date = settlement.business_date

    expected = settlement.expected_amount
    remitted = settlement.remitted_amount
    variance = expected - remitted

    receivable_account = _receivable_account(settlement, on_date)
    remittance_account = _account(organization, _remittance_role(settlement), on_date)
    _refuse_a_required_cost_center(receivable_account)
    _refuse_a_required_cost_center(remittance_account)

    lines: list[PostingLine] = []
    if remitted > ZERO:
        lines.append(
            PostingLine(
                account=remittance_account,
                branch=settlement.branch,
                cost_center=None,
                debit=remitted,
                credit=ZERO,
            )
        )
    if variance != ZERO:
        variance_account = _account(organization, DELIVERY_SETTLEMENT_VARIANCE, on_date)
        _refuse_a_required_cost_center(variance_account)
        lines.append(
            PostingLine(
                account=variance_account,
                branch=settlement.branch,
                cost_center=None,
                # Bidirectional by design: a debit when the application
                # short-paid, a credit when it over-paid. One account, because
                # the leg and the reason on the document already say where the
                # difference arose.
                debit=variance if variance > ZERO else ZERO,
                credit=-variance if variance < ZERO else ZERO,
            )
        )
    lines.append(
        PostingLine(
            account=receivable_account,
            branch=settlement.branch,
            cost_center=None,
            debit=ZERO,
            credit=expected,
        )
    )

    return _Plan(
        posting_lines=lines,
        expected_amount=quantize_money(expected),
        remitted_amount=quantize_money(remitted),
        total_variance=quantize_money(variance),
        receivable_account=receivable_account,
    )


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@transaction.atomic
def post_settlement(
    *, settlement: DeliveryApplicationSettlement, actor: User
) -> DeliveryApplicationSettlement:
    """
    Post a reconciled settlement to the ledger and the receivable, atomically.

    Both leg equations are re-checked **here**, under the row lock, and not only
    at reconciliation. The figures could have moved between the two acts — a
    claim withdrawn, an allocation added — and a settlement that reconciled last
    Tuesday is not evidence about what it says today.

    `Σ allocations == expected_amount` is checked in the same place, which is
    ADR-028 §6's required equality: the journal credits the stamped figure, so
    the stamp and the claims have to still be the same number at the moment the
    credit is written.

    The idempotency key is derived from the settlement's own `public_id` rather
    than accepted from the caller: posting *this settlement* is the command, and
    a posted settlement is frozen by a database trigger, so a retry cannot
    present the same key with a different payload.
    """
    locked = DeliveryApplicationSettlement.objects.select_for_update().get(pk=settlement.pk)
    if locked.status == SettlementStatus.POSTED:
        raise ValidationError(_("This settlement is already posted."), code="already_posted")
    if locked.status == SettlementStatus.REVERSED:
        raise ValidationError(
            _("A reversed settlement cannot be posted again. Record a new one."),
            code="settlement_reversed",
        )
    if locked.status != SettlementStatus.RECONCILED:
        raise ValidationError(
            _("Only a reconciled settlement can be posted."), code="settlement_not_reconciled"
        )

    # The allocations are checked **before** the legs, and the order is not
    # arbitrary. `expected` is Σ allocations, so if the allocations have moved
    # since reconciliation then both gaps have moved with them and the leg
    # failure would be a symptom reported in place of its cause — an operator
    # would go looking for a missing claim when what actually changed was what
    # the settlement is claiming to pay for.
    claimed = allocated_total(locked)
    if claimed != locked.expected_amount:
        # The equality ADR-028 §6 asks for by name. The stamp is what was
        # declared and the sum is what is there; posting is where they must
        # still agree, because the journal credits the stamp.
        raise ValidationError(
            _("The allocations no longer add up to the reconciled figure."),
            code="allocations_do_not_match",
        )

    comparison = three_way_for(locked)
    if not comparison.is_reconcilable:
        raise ValidationError(
            _(
                "The difference is no longer fully explained: %(statement)s on the "
                "statement and %(remittance)s on the remittance are unclaimed."
            )
            % {
                "statement": f"{comparison.unexplained_statement:f}",
                "remittance": f"{comparison.unexplained_remittance:f}",
            },
            code="unexplained_variance",
        )

    if locked.expected_amount <= ZERO:
        raise ValidationError(
            _("A settlement that clears nothing has nothing to post."), code="nothing_to_post"
        )

    plan = build_settlement_plan(locked)

    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        number = next_document_number(
            organization=locked.organization,
            document_type=APPLICATION_SETTLEMENT_DOCUMENT_TYPE,
            prefix="AS",
            year=locked.business_date.year,
        )
        entry = post_entry(
            organization=locked.organization,
            accounting_date=locked.business_date,
            document_date=locked.business_date,
            lines=plan.posting_lines,
            idempotency_key=f"application-settlement:{locked.public_id}",
            narration=(
                f"{number} · {locked.delivery_application.code} · {locked.statement_reference}"
            ),
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
            source_event=SourceEvent.POSTED,
        )

        ApplicationReceivableEntry.objects.create(
            organization=locked.organization,
            branch=locked.branch,
            delivery_application=locked.delivery_application,
            business_date=locked.business_date,
            source=ReceivableSource.SETTLEMENT,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
            credit=plan.expected_amount,
            narration=str(_("تسوية تطبيق %(number)s")) % {"number": number},
        )

        previous = snapshot(locked)
        locked.status = SettlementStatus.POSTED
        locked.number = number
        locked.posted_by = actor
        locked.posted_at = timezone.now()
        locked.idempotency_key = f"application-settlement:{locked.public_id}"
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
                "expected": str(plan.expected_amount),
                "statement": str(locked.statement_amount),
                "remitted": str(plan.remitted_amount),
                "variance": str(plan.total_variance),
                # Reported, never posted. See the module docstring.
                "commission_gap": str(comparison.commission_gap),
            },
        )
    return locked


@transaction.atomic
def reverse_settlement(
    *, settlement: DeliveryApplicationSettlement, actor: User, reason: str
) -> DeliveryApplicationSettlement:
    """
    Undo a posted settlement: reverse its journal and mirror its receivable.

    Nothing is re-decided. The mirroring receivable entry carries the amount the
    settlement wrote, read from the ledger rather than recomputed from the
    allocations, because recomputing would silently pick up any change since —
    exactly the drift a reversal must not introduce. Only its *date* is current,
    because undoing something is an event that happens now.

    The allocations stay. They are the evidence of what this settlement claimed,
    and the entries become open again on their own because every count of a
    claimed amount is restricted to **posted** settlements.
    """
    if not reason.strip():
        raise ValidationError(_("Reversing a settlement needs a reason."), code="reason_required")

    locked = DeliveryApplicationSettlement.objects.select_for_update().get(pk=settlement.pk)
    if locked.status == SettlementStatus.REVERSED:
        raise ValidationError(_("This settlement is already reversed."), code="already_reversed")
    if locked.status != SettlementStatus.POSTED:
        raise ValidationError(
            _("Only a posted settlement can be reversed."), code="settlement_not_posted"
        )

    entry = JournalEntry.objects.filter(
        organization=locked.organization,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
    ).first()
    if entry is None:  # pragma: no cover - a posted settlement always has one
        raise ValidationError(
            _("The journal for this settlement is missing."), code="journal_missing"
        )

    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        reverse_entry(
            entry=entry,
            idempotency_key=f"application-settlement-reversal:{locked.public_id}",
            reason=reason.strip(),
            accounting_date=timezone.localdate(),
        )

        original = ApplicationReceivableEntry.objects.filter(
            organization=locked.organization,
            delivery_application=locked.delivery_application,
            source=ReceivableSource.SETTLEMENT,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
        ).first()
        if original is not None:
            ApplicationReceivableEntry.objects.create(
                organization=original.organization,
                branch=original.branch,
                delivery_application=original.delivery_application,
                business_date=locked.business_date,
                # The paired source value exists here, so the mirror needs no
                # suffix on the document id — unlike an adjustment reversal,
                # which has no paired value in the closed vocabulary to use.
                source=ReceivableSource.SETTLEMENT_REVERSED,
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=str(locked.public_id),
                debit=original.credit,
                narration=str(_("عكس تسوية تطبيق %(number)s")) % {"number": locked.number},
            )

        previous = snapshot(locked)
        locked.status = SettlementStatus.REVERSED
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
    "APPLICATION_SETTLEMENT_DOCUMENT_TYPE",
    "SOURCE_DOCUMENT_TYPE",
    "build_settlement_plan",
    "post_settlement",
    "reverse_settlement",
]
