"""
المطابقة اليومية — one branch, one business date, every figure compared.

**Report only.** No model, no persisted status, no repair action, and that is
the spec's decision rather than an omission (task 4.0 §2 and assumption §8.5).
A `SalesDailyReconciliation` row would record no fact the documents do not
already carry: the day carries what was declared and what its lines say, the
shift carries what was counted, the adjustments carry what came back, and the
ledger carries what posted. Storing the comparison would create a second place
for the same truth to live, and the stored one is always the one that goes
stale — the reconciliation for the 3rd would still say "clean" a week after
somebody reversed the day it reconciled.

There is also no "resolve" or "acknowledge" action, for the same reason there is
no `--fix` in any verifier in this system (RCP-050): a finding here is a
statement that two documents disagree, and the only honest response is to change
a document, which is a service call with its own permission and its own audit
trail. A button that marked a difference as seen would let a real shortage be
closed by clicking.

## The three legs, and what each side of them is

For `CASH`, `CARD` and `APPLICATION_RECEIVABLE`:

    declared  SalesTenderSummary.declared_amount — what the operator typed
    derived   Σ the day's posted line net amounts — what the document posted

They are two different claims made by the same document and a difference between
them is a real finding: the lines reached the ledger, the declaration did not,
and an operator who typed a till-report total that disagrees with the items they
entered has one of the two wrong. The cash leg additionally carries the shift's
**counted** figure, which is the only one of the three that came from outside
the document at all.

Three numbers rather than two on the cash leg is the same refusal ADR-028 §7
makes for a settlement: which two of them agree is the diagnosis. Declared and
derived agreeing while the count is short is a till difference. Derived and
counted agreeing while the declaration is higher is a typing error in the
summary. One "cash variance" answers neither.

## Severities

`ERROR`, `ADVISORY` and `COVERAGE_LIMITATION` — the same three
`apps/kitchen/consumption_reconciliation.py` uses, and the same `Finding` shape,
so `verify_sales` can compose these with the kitchen's own verifiers in
checkpoint 7 without either side agreeing on a class.

An `ERROR` is something that should be impossible: a posted day with no journal,
a journal whose figures disagree with the document. An `ADVISORY` is something
real that a human decides about: a till that came up short, a declaration that
disagrees with the lines. A missing document is a `COVERAGE_LIMITATION` rather
than an error — a branch that has not closed its drawer yet has not done
anything wrong, and reporting it as a failure would make the screen red on every
day before the shift is approved.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db.models import Sum

from apps.accounting.models import JournalEntry, SourceEvent
from apps.core.money import quantize_money
from apps.kitchen.consumption_reconciliation import (
    ADVISORY,
    COVERAGE_LIMITATION,
    ERROR,
    Finding,
)
from apps.sales.adjustment_posting import (
    SOURCE_DOCUMENT_TYPE as ADJUSTMENT_SOURCE_DOCUMENT_TYPE,
)
from apps.sales.consumption_source import cancelled_quantities
from apps.sales.models import (
    ApplicationReceivableEntry,
    CashierShift,
    CashierShiftStatus,
    SalesAdjustment,
    SalesAdjustmentLine,
    SalesAdjustmentStatus,
    SalesDay,
    SalesDayStatus,
    SalesTenderSummary,
    TenderDestination,
)
from apps.sales.posting import SOURCE_DOCUMENT_TYPE as DAY_SOURCE_DOCUMENT_TYPE
from apps.sales.selectors import visible_sales_days

if TYPE_CHECKING:
    from apps.organizations.models import Branch
    from apps.users.models import User

ZERO = Decimal("0")

#: The order the legs are reported in: what is countable first, what is owed
#: last. Stable, because a report whose rows move between refreshes is a report
#: nobody can compare against yesterday's printout.
LEG_ORDER: tuple[str, ...] = (
    TenderDestination.CASH,
    TenderDestination.CARD,
    TenderDestination.APPLICATION_RECEIVABLE,
)


@dataclass(frozen=True)
class ReconciliationLeg:
    """
    One tender, as declared and as derived.

    Both figures and their difference, never just the difference: a leg that
    reported only the gap would answer "how much" and never "which of the two
    is wrong", and which one is wrong decides who fixes it.
    """

    label: Any
    tender: str
    declared: Decimal
    derived: Decimal
    difference: Decimal

    @property
    def agrees(self) -> bool:
        return self.difference == ZERO


@dataclass(frozen=True)
class DailyReconciliation:
    """
    One branch's day, with every stream compared and nothing merged.

    `is_clean` is a convenience for the screen's filter, not a verdict: it is
    false whenever any finding is an `ERROR` or an `ADVISORY`, and a
    `COVERAGE_LIMITATION` alone leaves it true. A day whose shift has simply not
    been approved yet is not a day with a problem.
    """

    branch: Branch
    business_date: datetime.date
    sales_day: SalesDay | None
    shift: CashierShift | None
    legs: tuple[ReconciliationLeg, ...]
    counted_cash: Decimal | None
    cash_variance: Decimal | None
    adjustments_total: Decimal
    receivable_movement: Decimal
    cancelled_quantity: Decimal
    findings: tuple[Finding, ...]
    is_clean: bool

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(row for row in self.findings if row.severity == ERROR)

    @property
    def advisories(self) -> tuple[Finding, ...]:
        return tuple(row for row in self.findings if row.severity == ADVISORY)

    @property
    def limitations(self) -> tuple[Finding, ...]:
        return tuple(row for row in self.findings if row.severity == COVERAGE_LIMITATION)


def _derived_by_tender(day: SalesDay) -> dict[str, Decimal]:
    """
    What the day's own lines say each tender took.

    From the lines rather than from the ledger, on purpose. The ledger is
    checked separately against `build_plan`; comparing the declaration against
    the ledger instead would fold two different findings into one and lose which
    of them happened.
    """
    totals: dict[str, Decimal] = dict.fromkeys(LEG_ORDER, ZERO)
    for line in day.lines.select_related("channel").all():
        if line.is_application_sale:
            tender = TenderDestination.APPLICATION_RECEIVABLE
        elif line.channel.default_tender == TenderDestination.CARD:
            tender = TenderDestination.CARD
        else:
            tender = TenderDestination.CASH
        totals[tender] = totals[tender] + line.net_amount
    return {tender: quantize_money(amount) for tender, amount in totals.items()}


def _declared_by_tender(day: SalesDay) -> dict[str, Decimal]:
    """What the operator said, per tender. A tender nobody declared reads zero."""
    declared: dict[str, Decimal] = dict.fromkeys(LEG_ORDER, ZERO)
    for summary in SalesTenderSummary.objects.filter(sales_day=day):
        if summary.tender in declared:
            declared[summary.tender] = quantize_money(summary.declared_amount)
    return declared


def _adjustments_for(day: SalesDay) -> Decimal:
    """Posted adjustment gross against this day — what came back."""
    total = (
        SalesAdjustmentLine.objects.filter(
            adjustment__sales_day=day,
            adjustment__status=SalesAdjustmentStatus.POSTED,
        ).aggregate(total=Sum("adjusted_gross"))["total"]
        or ZERO
    )
    return quantize_money(total)


def _cancelled_for(day: SalesDay) -> Decimal:
    """
    The quantity a posted cancellation took back out of theoretical consumption.

    Only `CANCELLED_BEFORE_FULFILLMENT`, because that is the only kind that
    reduces it — the asymmetry lives in `consumption_source.cancelled_quantities`
    and this report reads it from there rather than reimplementing the filter. A
    second copy of that one filter is exactly how the two would eventually
    disagree, and the report would then contradict the kitchen it exists to
    reconcile against.
    """
    line_ids = list(day.lines.values_list("pk", flat=True))
    return quantize_money(sum(cancelled_quantities(line_ids).values(), ZERO))


def _receivable_movement(day: SalesDay) -> Decimal:
    """Net receivable movement dated on this day, at this branch."""
    rows = ApplicationReceivableEntry.objects.filter(
        branch_id=day.branch_id, business_date=day.business_date
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return quantize_money((rows["debit"] or ZERO) - (rows["credit"] or ZERO))


def _journal_findings(day: SalesDay) -> list[Finding]:
    """
    The posted day has exactly one journal, and it says what the day says.

    Recomputed from `build_plan` rather than trusted, because a verifier that
    read the same stored total the poster wrote would agree even when both were
    wrong. These two arrive at the number by different routes.
    """
    from apps.sales.posting import build_plan

    findings: list[Finding] = []
    entries = list(
        JournalEntry.objects.filter(
            organization_id=day.organization_id,
            source_document_type=DAY_SOURCE_DOCUMENT_TYPE,
            source_document_id=str(day.public_id),
            source_event=SourceEvent.POSTED,
        )
    )
    if not entries:
        findings.append(
            Finding(
                severity=ERROR,
                code="sales_day_has_no_journal",
                message=f"{day.number or day.public_id}: posted with no journal entry",
            )
        )
        return findings
    if len(entries) > 1:
        findings.append(
            Finding(
                severity=ERROR,
                code="sales_day_has_many_journals",
                message=f"{day.number or day.public_id}: {len(entries)} journals at one identity",
            )
        )

    entry = entries[0]
    lines = list(day.lines.select_related("channel", "delivery_application").all())
    if not lines:
        return findings
    try:
        plan = build_plan(day, lines)
    except Exception as refusal:  # pragma: no cover - a mapping withdrawn later
        findings.append(
            Finding(
                severity=ERROR,
                code="sales_day_plan_cannot_be_rebuilt",
                message=f"{day.number or day.public_id}: {refusal}",
            )
        )
        return findings

    planned = quantize_money(sum((row.debit for row in plan.posting_lines), ZERO))
    posted = quantize_money(entry.lines.aggregate(total=Sum("debit"))["total"] or ZERO)
    if planned != posted:
        findings.append(
            Finding(
                severity=ERROR,
                code="sales_day_journal_disagrees",
                message=(
                    f"{day.number or day.public_id}: the journal debits {posted:f} "
                    f"where the document says {planned:f}"
                ),
            )
        )
    return findings


def reconcile_day(*, sales_day: SalesDay) -> DailyReconciliation:
    """
    Compare everything one branch's day touched, and say nothing about fixing it.

    Every figure here is derived at read time from documents that already exist.
    Nothing is written, and there is deliberately nowhere to write it — see the
    module docstring.
    """
    branch = sales_day.branch
    declared = _declared_by_tender(sales_day)
    derived = _derived_by_tender(sales_day)

    legs = tuple(
        ReconciliationLeg(
            label=TenderDestination(tender).label,
            tender=tender,
            declared=declared[tender],
            derived=derived[tender],
            difference=quantize_money(declared[tender] - derived[tender]),
        )
        for tender in LEG_ORDER
    )

    shift = (
        CashierShift.objects.filter(branch_id=branch.pk, business_date=sales_day.business_date)
        .select_related("cashier", "closed_by", "approved_by")
        .first()
    )

    findings: list[Finding] = []
    label = sales_day.number or str(sales_day.public_id)

    for leg in legs:
        if not leg.agrees:
            findings.append(
                Finding(
                    severity=ADVISORY,
                    code="sales_declaration_disagrees_with_lines",
                    message=(
                        f"{label} {leg.tender}: declared {leg.declared:f} against "
                        f"lines of {leg.derived:f} (difference {leg.difference:f})"
                    ),
                )
            )

    if sales_day.status == SalesDayStatus.POSTED:
        findings.extend(_journal_findings(sales_day))
    else:
        findings.append(
            Finding(
                severity=COVERAGE_LIMITATION,
                code="sales_day_is_not_posted",
                message=f"{label}: the day is {sales_day.status} and has reached no ledger",
            )
        )

    counted_cash: Decimal | None = None
    cash_variance: Decimal | None = None
    if shift is None:
        findings.append(
            Finding(
                severity=COVERAGE_LIMITATION,
                code="cashier_shift_is_missing",
                message=(
                    f"{branch.code} {sales_day.business_date.isoformat()}: no cashier "
                    "shift, so the drawer has not been compared to the day"
                ),
            )
        )
    else:
        if shift.status in {CashierShiftStatus.CLOSED, CashierShiftStatus.APPROVED}:
            counted_cash = shift.counted_cash
            cash_variance = shift.variance_amount
            if shift.variance_amount != ZERO:
                findings.append(
                    Finding(
                        severity=ADVISORY,
                        code="cashier_shift_variance",
                        message=(
                            f"{shift.number or branch.code}: the drawer counted "
                            f"{shift.counted_cash:f} against an expected "
                            f"{shift.expected_cash:f} (variance {shift.variance_amount:f})"
                        ),
                    )
                )
            # The stamped expectation against what the day says now. They agree
            # unless the day was reversed and replaced after the count, which is
            # the case this comparison exists to surface — the shift would
            # otherwise sit there approved against arithmetic nobody can
            # reproduce.
            expected_now = quantize_money(shift.opening_float + derived[TenderDestination.CASH])
            if shift.expected_cash != expected_now:
                findings.append(
                    Finding(
                        severity=ERROR,
                        code="cashier_shift_expectation_is_stale",
                        message=(
                            f"{shift.number or branch.code}: closed against an expected "
                            f"{shift.expected_cash:f}, but the day now says {expected_now:f}"
                        ),
                    )
                )
        if shift.status == CashierShiftStatus.OPEN:
            findings.append(
                Finding(
                    severity=COVERAGE_LIMITATION,
                    code="cashier_shift_is_open",
                    message=f"{branch.code}: the drawer is still open and has not been counted",
                )
            )
        if shift.status == CashierShiftStatus.CLOSED:
            findings.append(
                Finding(
                    severity=COVERAGE_LIMITATION,
                    code="cashier_shift_is_not_approved",
                    message=f"{branch.code}: the count is declared but nobody has approved it",
                )
            )
        if shift.status == CashierShiftStatus.REVERSED:
            findings.append(
                Finding(
                    severity=ADVISORY,
                    code="cashier_shift_is_reversed",
                    message=(
                        f"{shift.number or branch.code}: the closing was reversed — "
                        f"{shift.reversal_reason}"
                    ),
                )
            )

    # A posted adjustment that never reached a journal. The one check here that
    # is about the correction rather than the day: an adjustment whose journal
    # is missing would leave `SALES_RETURNS` understated with a document sitting
    # beside it claiming otherwise.
    for adjustment in SalesAdjustment.objects.filter(
        sales_day=sales_day, status=SalesAdjustmentStatus.POSTED
    ):
        if not JournalEntry.objects.filter(
            organization_id=adjustment.organization_id,
            source_document_type=ADJUSTMENT_SOURCE_DOCUMENT_TYPE,
            source_document_id=str(adjustment.public_id),
            source_event=SourceEvent.POSTED,
        ).exists():
            findings.append(
                Finding(
                    severity=ERROR,
                    code="sales_adjustment_has_no_journal",
                    message=f"{adjustment.number or adjustment.public_id}: posted with no journal",
                )
            )

    is_clean = not any(row.severity in {ERROR, ADVISORY} for row in findings)
    return DailyReconciliation(
        branch=branch,
        business_date=sales_day.business_date,
        sales_day=sales_day,
        shift=shift,
        legs=legs,
        counted_cash=counted_cash,
        cash_variance=cash_variance,
        adjustments_total=_adjustments_for(sales_day),
        receivable_movement=_receivable_movement(sales_day),
        cancelled_quantity=_cancelled_for(sales_day),
        findings=tuple(findings),
        is_clean=is_clean,
    )


def reconcile_range(
    user: User,
    *,
    branch_ids: Sequence[int] | None,
    date_from: datetime.date,
    date_to: datetime.date,
) -> list[DailyReconciliation]:
    """
    Every day in a window, for the branches the caller can reach.

    Scoped through `visible_sales_days`, which is branch-scoped — the permission
    that reaches this screen is `view_sales_reports` at the organization, and
    the selector narrows the rows to the caller's own branches. Scope and
    selector answer two different questions and both are asked (ADR-016).
    """
    days = visible_sales_days(user).filter(business_date__gte=date_from, business_date__lte=date_to)
    if branch_ids is not None:
        days = days.filter(branch_id__in=branch_ids)
    return [reconcile_day(sales_day=day) for day in days.order_by("-business_date", "branch__code")]


__all__ = [
    "ADVISORY",
    "COVERAGE_LIMITATION",
    "ERROR",
    "LEG_ORDER",
    "DailyReconciliation",
    "Finding",
    "ReconciliationLeg",
    "reconcile_day",
    "reconcile_range",
]
