"""
Building an application settlement: allocations, claims, and the reconciliation.

Kept apart from `settlement_posting.py` for the reason `day_services.py` is kept
apart from `posting.py`: nothing here writes a journal, a receivable entry or a
number. This module decides *what the settlement says*; the other one is the
only place value moves.

## The three figures, and the two gaps

A settlement carries three amounts that are never reduced to one:

    expected   Σ this settlement's allocations against posted receivable entries
    statement  what the delivery application's own statement says it owes
    remitted   what actually arrived in the bank or the till

and therefore two gaps:

    statement_gap  = expected  − statement
    remittance_gap = statement − remitted

ADR-028 §7 keeps all three because the *pattern* of which two agree is the
diagnosis (ADR-023). Expected and statement agreeing while the remittance is
short is a withholding or an offset. Statement and remittance agreeing while
expected is higher is a rate dispute — the restaurant accrued one commission and
the application charged another. A single net variance answers "how much" and
never "where", and "where" is the only part anybody can act on.

## Every dinar must be claimed

`reconcile_settlement` refuses `DRAFT → RECONCILED` unless

    Σ adjustments where leg = STATEMENT   ==  statement_gap
    Σ adjustments where leg = REMITTANCE  ==  remittance_gap

**exactly** — not within a tolerance. A tolerance is where a mis-configured
commission rate lives: it is small every day and it is the same sign every day,
and by the time it is large enough to notice it has been accruing for a year.
`post_settlement` re-checks both under the row lock, because the figures could
have moved between the two acts.

The escape hatch is `SettlementAdjustmentReason.UNEXPLAINED_APPROVED` and it is
not free: two check constraints in `0009` require a written explanation *and* a
named approver. An unexplained difference may reach the ledger, but only wearing
a name and a reason.

## Commission is compared, never re-recognised

`accrued_commission_for` adds up the commission this settlement's allocated days
already accrued at the sale (ADR-028 §4). `statement_commission_amount` is what
the counterparty says it charged. The two are compared and reported as
`commission_gap`, and any real difference is claimed on a leg like any other
variance. It reaches `DELIVERY_COMMISSION_EXPENSE` **never** — expensing it
again would overstate selling expense and understate gross margin by the same
amount, both individually defensible, which is exactly why ADR-028 §6 names it
the single most likely error in the module.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.sales.models import (
    ApplicationReceivableEntry,
    DeliveryApplication,
    DeliveryApplicationSettlement,
    DeliveryApplicationSettlementAdjustment,
    DeliveryApplicationSettlementAllocation,
    SalesAdjustment,
    SalesAdjustmentStatus,
    SalesDay,
    SalesDayLine,
    SalesDayStatus,
    SettlementAdjustmentReason,
    SettlementRemittance,
    SettlementStatus,
    SettlementVarianceLeg,
)
from apps.sales.receivables import unallocated_debit

if TYPE_CHECKING:
    from apps.organizations.models import Branch, Organization
    from apps.users.models import User

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# The three-way comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreeWay:
    """
    What a settlement says, and what is still unaccounted for.

    Derived every time from the allocations and the claims. Nothing here is
    stored except `expected`, which the document stamps at reconciliation as
    evidence of what was claimed *then* — and even that is recomputed here so a
    disagreement would be visible rather than inherited.
    """

    expected: Decimal
    statement: Decimal
    remitted: Decimal
    statement_gap: Decimal
    remittance_gap: Decimal
    explained_statement: Decimal
    explained_remittance: Decimal
    unexplained_statement: Decimal
    unexplained_remittance: Decimal
    total_variance: Decimal
    accrued_commission: Decimal
    statement_commission: Decimal
    commission_gap: Decimal

    @property
    def is_reconcilable(self) -> bool:
        """
        Whether both legs are fully claimed.

        Exactly zero on both, and the exactness is the decision. See the module
        docstring for what a tolerance would hide.
        """
        return self.unexplained_statement == ZERO and self.unexplained_remittance == ZERO


def allocated_total(settlement: DeliveryApplicationSettlement) -> Decimal:
    """Σ this settlement's allocations. The expected figure, derived."""
    total = settlement.allocations.aggregate(total=Sum("allocated_amount"))["total"]
    return quantize_money(total or ZERO)


def accrued_commission_for(settlement: DeliveryApplicationSettlement) -> Decimal:
    """
    The commission this settlement's allocated sales already accrued.

    Read from the **sales lines behind the allocated entries**, not from the
    agreement, and not re-rated. The agreement may have changed since; the lines
    carry what was actually charged and are the only thing a statement can
    honestly be compared against (ADR-024).

    Only `SALE_POSTED` entries contribute, because they are the only ones that
    accrued a commission: a settlement entry is a payment, an adjustment entry
    took commission back — and the adjustment's own journal has already credited
    `DELIVERY_COMMISSION_EXPENSE` for it, so counting it here as well would
    double the correction.
    """
    from apps.sales.posting import SOURCE_DOCUMENT_TYPE as SALE_SOURCE_DOCUMENT_TYPE

    day_ids = list(
        settlement.allocations.filter(
            receivable_entry__source_document_type=SALE_SOURCE_DOCUMENT_TYPE
        ).values_list("receivable_entry__source_document_id", flat=True)
    )
    if not day_ids:
        return ZERO
    total = SalesDayLine.objects.filter(
        sales_day__public_id__in=day_ids,
        sales_day__status=SalesDayStatus.POSTED,
        delivery_application=settlement.delivery_application,
    ).aggregate(total=Sum("commission_amount"))["total"]
    return quantize_money(total or ZERO)


def three_way_for(settlement: DeliveryApplicationSettlement) -> ThreeWay:
    """
    Expected against statement against remitted, with both residuals named.

    `expected` is Σ allocations rather than the stored `expected_amount`, so a
    draft's figure moves as allocations are added and a reconciled one is
    checked against the stamp rather than merely trusting it.
    """
    expected = allocated_total(settlement)
    statement = settlement.statement_amount
    remitted = settlement.remitted_amount

    statement_gap = expected - statement
    remittance_gap = statement - remitted

    claims = settlement.adjustments.values("leg").annotate(total=Sum("amount"))
    by_leg = {row["leg"]: quantize_money(row["total"] or ZERO) for row in claims}
    explained_statement = by_leg.get(SettlementVarianceLeg.STATEMENT, ZERO)
    explained_remittance = by_leg.get(SettlementVarianceLeg.REMITTANCE, ZERO)

    accrued = accrued_commission_for(settlement)
    return ThreeWay(
        expected=expected,
        statement=statement,
        remitted=remitted,
        statement_gap=statement_gap,
        remittance_gap=remittance_gap,
        explained_statement=explained_statement,
        explained_remittance=explained_remittance,
        unexplained_statement=statement_gap - explained_statement,
        unexplained_remittance=remittance_gap - explained_remittance,
        total_variance=expected - remitted,
        accrued_commission=accrued,
        statement_commission=settlement.statement_commission_amount,
        commission_gap=accrued - settlement.statement_commission_amount,
    )


# ---------------------------------------------------------------------------
# What may be allocated
# ---------------------------------------------------------------------------


def open_entries_for(
    *,
    delivery_application: DeliveryApplication,
    organization_id: int,
    up_to: datetime.date,
) -> QuerySet[ApplicationReceivableEntry]:
    """
    The debit entries this application still owes on, up to a date.

    Debits only, because a credit entry is a payment or a return and there is
    nothing in it to be paid. Bounded by `up_to` — the settlement's `period_end`
    — because a statement cannot pay for a sale that had not happened when it
    was issued, which `0010`'s trigger also refuses.

    Entries that posted settlements have already fully claimed are still in this
    queryset: filtering them out would need a per-row subquery, and the caller
    that offers a choice reads `unallocated_debit` for each anyway.
    """
    return ApplicationReceivableEntry.objects.filter(
        organization_id=organization_id,
        delivery_application=delivery_application,
        debit__gt=ZERO,
        business_date__lte=up_to,
    ).order_by("business_date", "pk")


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _require_draft(settlement: DeliveryApplicationSettlement) -> None:
    if settlement.status != SettlementStatus.DRAFT:
        raise ValidationError(
            _("Only a draft settlement can be changed."), code="settlement_not_draft"
        )


@transaction.atomic
def create_settlement(
    *,
    organization: Organization,
    branch: Branch,
    delivery_application: DeliveryApplication,
    period_start: datetime.date,
    period_end: datetime.date,
    business_date: datetime.date,
    statement_reference: str,
    statement_date: datetime.date,
    statement_amount: Decimal,
    remitted_amount: Decimal,
    statement_commission_amount: Decimal,
    remittance_destination: str,
    evidence_reference: str,
    actor: User,
    notes: str = "",
) -> DeliveryApplicationSettlement:
    """
    Open a settlement against one statement.

    `statement_reference` is mandatory and unique per application, and the
    uniqueness is the point rather than tidiness: paying one statement twice is
    a failure that only surfaces once the counterparty stops answering, and by
    then the evidence trail is a year old.
    """
    if not statement_reference.strip():
        raise ValidationError(
            _("A settlement needs the counterparty's statement reference."),
            code="statement_reference_required",
        )
    if not evidence_reference.strip():
        raise ValidationError(
            _("A settlement needs an evidence reference."), code="evidence_required"
        )
    if period_end < period_start:
        raise ValidationError(
            _("The statement period ends before it starts."), code="period_is_not_ordered"
        )
    if remittance_destination not in SettlementRemittance.values:
        raise ValidationError(_("Unknown remittance destination."), code="unknown_destination")
    if delivery_application.organization_id != organization.pk:
        raise ValidationError(
            _("That delivery application belongs to another organization."),
            code="application_out_of_organization",
        )
    if branch.organization_id != organization.pk:
        raise ValidationError(
            _("That branch belongs to another organization."), code="branch_out_of_organization"
        )

    settlement = DeliveryApplicationSettlement(
        organization=organization,
        branch=branch,
        delivery_application=delivery_application,
        period_start=period_start,
        period_end=period_end,
        business_date=business_date,
        statement_reference=statement_reference.strip(),
        statement_date=statement_date,
        statement_amount=quantize_money(statement_amount),
        remitted_amount=quantize_money(remitted_amount),
        statement_commission_amount=quantize_money(statement_commission_amount),
        remittance_destination=remittance_destination,
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
        created_by=actor,
    )
    settlement.full_clean()
    settlement.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=settlement,
        new_state=snapshot(settlement),
        branch=branch,
    )
    return settlement


@transaction.atomic
def allocate_entry(
    *,
    settlement: DeliveryApplicationSettlement,
    receivable_entry: ApplicationReceivableEntry,
    allocated_amount: Decimal,
    actor: User,
) -> DeliveryApplicationSettlementAllocation:
    """
    Claim part of one posted receivable entry for this settlement.

    Every refusal here is also a database guarantee — `0010`'s containment
    trigger checks the same four things — and both are wanted. The service is
    the sentence an operator can act on; the trigger is what survives a data fix
    applied at two in the morning through a shell.
    """
    _require_draft(settlement)

    amount = quantize_money(allocated_amount)
    if amount <= ZERO:
        raise ValidationError(
            _("An allocation must claim something."), code="allocation_is_not_positive"
        )
    if receivable_entry.debit <= ZERO:
        raise ValidationError(
            _("A credit entry is a payment, not something to be paid."),
            code="entry_is_not_a_debit",
        )
    if (
        receivable_entry.organization_id != settlement.organization_id
        or receivable_entry.delivery_application_id != settlement.delivery_application_id
    ):
        raise ValidationError(
            _("That receivable entry belongs to another application."),
            code="entry_out_of_scope",
        )
    if receivable_entry.business_date > settlement.period_end:
        raise ValidationError(
            _("A statement cannot pay for a sale dated after the period it covers."),
            code="entry_after_the_period",
        )

    open_amount = unallocated_debit(receivable_entry)
    if amount > open_amount:
        raise ValidationError(
            _("Only %(open)s of that entry is still open.") % {"open": f"{open_amount:f}"},
            code="allocation_exceeds_the_entry",
        )

    allocation = DeliveryApplicationSettlementAllocation(
        settlement=settlement,
        receivable_entry=receivable_entry,
        allocated_amount=amount,
    )
    allocation.full_clean()
    allocation.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=allocation,
        new_state=snapshot(allocation),
        branch=settlement.branch,
    )
    return allocation


@transaction.atomic
def remove_allocation(*, allocation: DeliveryApplicationSettlementAllocation, actor: User) -> None:
    """Take a claim off a draft settlement. The database refuses it otherwise."""
    settlement = allocation.settlement
    _require_draft(settlement)
    previous = snapshot(allocation)
    allocation.delete()
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="sales.DeliveryApplicationSettlementAllocation",
        target_id=str(previous.get("id", "")),
        previous_state=previous,
        branch=settlement.branch,
    )


@transaction.atomic
def add_settlement_adjustment(
    *,
    settlement: DeliveryApplicationSettlement,
    leg: str,
    reason: str,
    amount: Decimal,
    explanation: str = "",
    actor: User,
    approver: User | None = None,
) -> DeliveryApplicationSettlementAdjustment:
    """
    Claim part of one gap against a named reason.

    `amount` is **signed**: positive means the restaurant received less than the
    previous leg promised. A magnitude plus a direction flag would be two fields
    that can disagree, and the sign is what makes the two leg equations plain
    addition rather than a case analysis.

    `UNEXPLAINED_APPROVED` demands both a written explanation and an approver
    here and in two check constraints. That is ADR-028 §7's actual requirement:
    an unexplained difference may reach the ledger, but only wearing a name and
    a reason, and ADR-022's rule that a difference is recognised where it is
    decided, by somebody who decided it.
    """
    _require_draft(settlement)

    if leg not in SettlementVarianceLeg.values:
        raise ValidationError(_("Unknown variance leg."), code="unknown_leg")
    if reason not in SettlementAdjustmentReason.values:
        raise ValidationError(_("Unknown variance reason."), code="unknown_reason")

    claimed = quantize_money(amount)
    if claimed == ZERO:
        raise ValidationError(_("A claim of nothing explains nothing."), code="adjustment_is_zero")

    is_unexplained = reason == SettlementAdjustmentReason.UNEXPLAINED_APPROVED
    if is_unexplained:
        if not explanation.strip():
            raise ValidationError(
                _("An unexplained difference still needs a written explanation."),
                code="explanation_required",
            )
        if approver is None:
            raise ValidationError(
                _("An unexplained difference needs a named approver."),
                code="approver_required",
            )

    adjustment = DeliveryApplicationSettlementAdjustment(
        settlement=settlement,
        leg=leg,
        reason=reason,
        amount=claimed,
        explanation=explanation.strip(),
        approved_by=approver,
        approved_at=timezone.now() if approver is not None else None,
    )
    adjustment.full_clean()
    adjustment.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=adjustment,
        new_state=snapshot(adjustment),
        branch=settlement.branch,
    )
    return adjustment


@transaction.atomic
def remove_settlement_adjustment(
    *, adjustment: DeliveryApplicationSettlementAdjustment, actor: User
) -> None:
    """Withdraw a claim from a draft settlement."""
    settlement = adjustment.settlement
    _require_draft(settlement)
    previous = snapshot(adjustment)
    adjustment.delete()
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="sales.DeliveryApplicationSettlementAdjustment",
        target_id=str(previous.get("id", "")),
        previous_state=previous,
        branch=settlement.branch,
    )


@transaction.atomic
def reconcile_settlement(
    *, settlement: DeliveryApplicationSettlement, actor: User
) -> DeliveryApplicationSettlement:
    """
    Declare that both gaps are fully claimed, and stamp the expected figure.

    Refuses with `unexplained_variance` unless both leg equations hold exactly.
    That refusal is ADR-028 §7 and it is the reason this state exists at all:
    an account that silently absorbs differences is an account nobody reads, and
    a mis-configured commission rate sitting inside it is invisible for a year.
    """
    locked = DeliveryApplicationSettlement.objects.select_for_update().get(pk=settlement.pk)
    if locked.status != SettlementStatus.DRAFT:
        raise ValidationError(
            _("Only a draft settlement can be reconciled."), code="settlement_not_draft"
        )
    if not locked.allocations.exists():
        raise ValidationError(
            _("A settlement with no allocations pays for nothing."), code="no_allocations"
        )

    comparison = three_way_for(locked)
    if not comparison.is_reconcilable:
        raise ValidationError(
            _(
                "The difference is not fully explained: %(statement)s on the statement "
                "and %(remittance)s on the remittance are still unclaimed."
            )
            % {
                "statement": f"{comparison.unexplained_statement:f}",
                "remittance": f"{comparison.unexplained_remittance:f}",
            },
            code="unexplained_variance",
        )

    previous = snapshot(locked)
    locked.status = SettlementStatus.RECONCILED
    locked.expected_amount = comparison.expected
    locked.reconciled_by = actor
    locked.reconciled_at = timezone.now()
    locked.save(
        update_fields=[
            "status",
            "expected_amount",
            "reconciled_by",
            "reconciled_at",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        branch=locked.branch,
        metadata={
            "expected": str(comparison.expected),
            "statement": str(comparison.statement),
            "remitted": str(comparison.remitted),
            "commission_gap": str(comparison.commission_gap),
        },
    )
    return locked


@transaction.atomic
def return_settlement_to_draft(
    *, settlement: DeliveryApplicationSettlement, actor: User, reason: str
) -> DeliveryApplicationSettlement:
    """
    Take a reconciled settlement back to draft, on the record.

    The way back from a declaration, and the reason `0010`'s allowlist permits
    `status` and the reconciliation stamps to move at `RECONCILED` and nothing
    else. A reconciliation that could be quietly edited would not be a
    declaration; one that could never be undone would make the first typo
    permanent.
    """
    if not reason.strip():
        raise ValidationError(
            _("Returning a settlement to draft needs a reason."), code="reason_required"
        )

    locked = DeliveryApplicationSettlement.objects.select_for_update().get(pk=settlement.pk)
    if locked.status != SettlementStatus.RECONCILED:
        raise ValidationError(
            _("Only a reconciled settlement can be returned to draft."),
            code="settlement_not_reconciled",
        )

    previous = snapshot(locked)
    locked.status = SettlementStatus.DRAFT
    locked.reconciled_by = None
    locked.reconciled_at = None
    locked.save(update_fields=["status", "reconciled_by", "reconciled_at", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
        branch=locked.branch,
    )
    return locked


# ---------------------------------------------------------------------------
# Reads a screen needs
# ---------------------------------------------------------------------------


def settled_days_for(settlement: DeliveryApplicationSettlement) -> list[SalesDay]:
    """
    Which trading days this settlement pays for.

    The question ADR-028 §6 says a period total could never answer, answered by
    following the allocations to their entries and the entries to the documents
    that wrote them. Adjustment-sourced entries have no day of their own and are
    left out; they are visible on the allocation list itself.
    """
    from apps.sales.posting import SOURCE_DOCUMENT_TYPE as SALE_SOURCE_DOCUMENT_TYPE

    public_ids = [
        row.receivable_entry.source_document_id
        for row in settlement.allocations.select_related("receivable_entry")
        if row.receivable_entry.source_document_type == SALE_SOURCE_DOCUMENT_TYPE
    ]
    if not public_ids:
        return []
    return list(
        SalesDay.objects.filter(public_id__in=public_ids)
        .select_related("branch")
        .order_by("business_date")
    )


def settled_adjustments_for(
    settlement: DeliveryApplicationSettlement,
) -> list[SalesAdjustment]:
    """The posted corrections this settlement's allocated entries came from."""
    from apps.sales.adjustment_posting import SOURCE_DOCUMENT_TYPE as ADJUSTMENT_SOURCE

    public_ids = [
        row.receivable_entry.source_document_id
        for row in settlement.allocations.select_related("receivable_entry")
        if row.receivable_entry.source_document_type == ADJUSTMENT_SOURCE
    ]
    if not public_ids:
        return []
    return list(
        SalesAdjustment.objects.filter(
            public_id__in=public_ids, status=SalesAdjustmentStatus.POSTED
        ).order_by("business_date")
    )


__all__ = [
    "ThreeWay",
    "accrued_commission_for",
    "add_settlement_adjustment",
    "allocate_entry",
    "allocated_total",
    "create_settlement",
    "open_entries_for",
    "reconcile_settlement",
    "remove_allocation",
    "remove_settlement_adjustment",
    "return_settlement_to_draft",
    "settled_adjustments_for",
    "settled_days_for",
    "three_way_for",
]
