"""
Usage variance, and the two very different things that phrase can mean.

## Output 1 — production standard variance. Available now.

For every posted batch: what its frozen recipe said it needed, against what the
kitchen actually put in. Both sides describe the same batch, both sides are
posted facts, and the difference is a real number a manager can act on.

This is a **complete** answer to a **narrow** question. Nothing about it is
provisional.

## Output 2 — final sales-based usage variance. Not available.

`actual consumption − theoretical sales consumption`, where theoretical sales
consumption is `approved sold quantity × the effective recipe`. Approved sold
quantities do not exist in Phase 3, so this number cannot be computed, and the
important design decision is that it is **not approximated**.

A variance report is read by people making staffing and purchasing decisions. A
number that silently omits sales is not a rough version of the real number — it
is a different number with the same name, and it will be wrong in the direction
that looks like theft. So the screen returns a labelled **partial diagnostic**
instead: every stream reported separately, the residual named for what it
actually is, and `PARTIAL_COVERAGE` / `NOT_FINAL_USAGE_VARIANCE` on every row
and in every export.

## The residual, and why meals are not subtracted from it

The diagnostic's residual is

```
unexplained_by_production_plan
    = actual economic consumption at the store
    − what the posted batch plans required
```

Meal equivalents sit **beside** that figure and are not subtracted from it, and
this is the single most important line of arithmetic in the module.

A staff meal does not consume store stock. Its ingredients already left through
the batch that cooked them, and that batch's `PRODUCTION_OUT` is already inside
`actual economic consumption`. The meal explains what happened to the *output* —
it was eaten by staff instead of sold — which is a statement about the other end
of the process. Subtracting it here would remove a quantity that was counted
once and credit it twice, and the residual would drift negative for any kitchen
that feeds its staff.

That is also why there is no combined theoretical total anywhere in Task 3.8: it
would need a key linking each meal portion to the batch that produced it, and a
`MealRecord` is recorded against a recipe and a date, not against a batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from apps.core.quantity import quantize_calculation
from apps.kitchen.consumption import (
    FlowFilters,
    ItemFlow,
    MovementBucket,
    StandardRequirementRow,
    flow_totals_by_item,
    kitchen_warehouse_flow,
    period_actual_consumption,
    production_standard_requirements,
    production_standard_variance,
)
from apps.kitchen.consumption_sources import (
    NOT_FINAL_USAGE_VARIANCE,
    PARTIAL_COVERAGE,
    PARTIAL_VARIANCE_NOTICE,
    SALES_COVERAGE_NOTICE,
    SALES_NOT_INCLUDED,
    MealUsageFilters,
    TheoreticalCoverage,
    TheoreticalSourceType,
    complimentary_meal_equivalent_usage,
    staff_meal_equivalent_usage,
    theoretical_consumption_coverage,
)
from apps.kitchen.productivity import ProductionFilters

if TYPE_CHECKING:
    from django.utils.functional import Promise

    from apps.users.models import User

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# انحراف الاستهلاك — the partial diagnostic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageDiagnosticRow:
    """
    One item, with every stream that touched it kept separately.

    Fourteen figures rather than one, because every one of them is a different
    conversation and merging any two of them destroys an answer somebody needs.
    """

    item_id: int
    item_code: str
    item_name: str
    base_unit_code: str

    #: Actual, from the movement partition.
    net_production_consumption: Decimal
    direct_economic_consumption: Decimal
    total_consumption: Decimal

    #: Standard, from the posted batch plans.
    production_standard_requirement: Decimal
    #: `total_consumption − production_standard_requirement`. Positive means the
    #: store consumed more than any posted plan called for.
    unexplained_by_production_plan: Decimal

    #: Explanatory, and never subtracted from the residual. See the module head.
    staff_meal_equivalent: Decimal
    complimentary_meal_equivalent: Decimal

    #: Reported beside consumption, never inside it.
    custody_in: Decimal
    custody_out: Decimal
    raw_material_waste: Decimal
    produced_output_waste: Decimal
    count_correction: Decimal

    coverage_code: str = SALES_NOT_INCLUDED
    coverage_label: str = PARTIAL_COVERAGE
    finality_label: str = NOT_FINAL_USAGE_VARIANCE


@dataclass(frozen=True)
class UsageVarianceAnalysis:
    """
    Both outputs, each labelled for exactly what it is.

    `production_standard_variance` is complete. `diagnostic` is partial and says
    so on every row. There is no third field holding "the" usage variance,
    because there is no such number until Phase 4 and a field for it would be
    filled in by somebody eventually.
    """

    #: Output 1. Complete, per posted batch requirement.
    production_variance: tuple[StandardRequirementRow, ...]
    #: Output 2. Partial, per item, labelled.
    diagnostic: tuple[UsageDiagnosticRow, ...]
    coverage: TheoreticalCoverage
    identity_holds: bool

    coverage_code: str = SALES_NOT_INCLUDED
    coverage_label: str = PARTIAL_COVERAGE
    finality_label: str = NOT_FINAL_USAGE_VARIANCE

    @property
    def final_sales_variance_available(self) -> bool:
        """
        Always `False` in Phase 3, and a constant rather than a computation.

        A derived boolean would eventually return `True` for a period with no
        sales in it, and an empty period is exactly where a false claim of
        finality does the most harm.
        """
        return False

    @property
    def notices(self) -> tuple[Promise, ...]:
        return (SALES_COVERAGE_NOTICE, PARTIAL_VARIANCE_NOTICE)

    @property
    def missing_sources(self) -> tuple[str, ...]:
        return tuple(str(row.source_type) for row in self.coverage.missing_sources)


def usage_variance_analysis(
    user: User,
    *,
    flow: FlowFilters,
    production: ProductionFilters,
    meals: MealUsageFilters,
    include_cost: bool = False,
) -> UsageVarianceAnalysis:
    """
    The variance screen's whole payload, from the three reads that feed it.

    Three separate filter objects rather than one, because the three subjects
    genuinely scope differently: the flow is a warehouse over a date range, the
    standard is a set of posted batches, and meals are a branch over a date
    range. Collapsing them into one filter would force one of the three to be
    scoped by something that does not apply to it.
    """
    consumption = period_actual_consumption(user, flow)

    # Planned requirement per item, summed across every posted batch in scope.
    planned: dict[str, Decimal] = {}
    for row in production_standard_requirements(user, production, include_cost=include_cost):
        planned[row.item_code] = planned.get(row.item_code, ZERO) + row.planned_base_quantity

    staff: dict[str, Decimal] = {}
    for contribution in staff_meal_equivalent_usage(user, meals):
        staff[contribution.leaf_item_code] = (
            staff.get(contribution.leaf_item_code, ZERO) + contribution.effective_base_quantity
        )

    complimentary: dict[str, Decimal] = {}
    for contribution in complimentary_meal_equivalent_usage(user, meals):
        complimentary[contribution.leaf_item_code] = (
            complimentary.get(contribution.leaf_item_code, ZERO)
            + contribution.effective_base_quantity
        )

    # Merged across warehouses: the diagnostic is per item, and the planned
    # requirement it is compared against is per item too. Leaving the flow rows
    # split per warehouse here would repeat one plan figure against each
    # warehouse's share of the consumption and overstate the residual.
    rows = [
        _diagnostic_row(
            item=item,
            planned=planned.get(item.item_code, ZERO),
            staff=staff.get(item.item_code, ZERO),
            complimentary=complimentary.get(item.item_code, ZERO),
        )
        for item in flow_totals_by_item(consumption.flow)
    ]

    return UsageVarianceAnalysis(
        production_variance=tuple(
            production_standard_variance(user, production, include_cost=include_cost)
        ),
        diagnostic=tuple(rows),
        coverage=theoretical_consumption_coverage(user, meals),
        identity_holds=consumption.identity_holds,
    )


def _diagnostic_row(
    *, item: ItemFlow, planned: Decimal, staff: Decimal, complimentary: Decimal
) -> UsageDiagnosticRow:
    consumed = item.total_consumption
    return UsageDiagnosticRow(
        item_id=item.item_id,
        item_code=item.item_code,
        item_name=item.item_name,
        base_unit_code=item.base_unit_code,
        net_production_consumption=item.net_production_consumption,
        direct_economic_consumption=item.direct_economic_consumption,
        total_consumption=consumed,
        production_standard_requirement=quantize_calculation(planned),
        # Meals are **not** in this subtraction. See the module docstring.
        unexplained_by_production_plan=quantize_calculation(consumed - planned),
        staff_meal_equivalent=quantize_calculation(staff),
        complimentary_meal_equivalent=quantize_calculation(complimentary),
        custody_in=item.custody_in,
        custody_out=item.custody_out,
        raw_material_waste=item.raw_material_waste,
        produced_output_waste=item.produced_output_waste,
        count_correction=item.count_correction,
    )


# ---------------------------------------------------------------------------
# What `verify_kitchen` asks of Task 3.8
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """
    One thing a verifier noticed, and how seriously to take it.

    Three severities and a stable code, so `verify_kitchen` can compose these
    with the four existing verifiers without any of them agreeing on a class.
    """

    severity: str
    code: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR


ERROR = "ERROR"
ADVISORY = "ADVISORY"
COVERAGE_LIMITATION = "COVERAGE_LIMITATION"


def verify_movement_partition(user: User, flow: FlowFilters) -> list[Finding]:
    """
    Every movement classified exactly once, and the stock identity balancing.

    The classifier raises on an unknown movement type rather than defaulting, so
    "classified exactly once" is structural. What this checks is the part that
    is *not* structural: that the per-bucket totals still reconstruct the
    ledger's own opening-to-closing movement (RCP-104).
    """
    findings: list[Finding] = []
    try:
        result = kitchen_warehouse_flow(user, flow)
    except ValueError as unclassified:
        return [
            Finding(
                severity=ERROR,
                code="kitchen_movement_unclassified",
                message=str(unclassified),
            )
        ]

    for row in result.unbalanced:
        findings.append(
            Finding(
                severity=ERROR,
                code="kitchen_stock_identity_broken",
                message=(
                    f"{row.item_code}: opening {row.opening:f} + movements "
                    f"{row.net_movement:f} != closing {row.closing:f} "
                    f"(difference {row.identity_difference:f})"
                ),
            )
        )

    custody = result.totals_by_bucket()
    if (
        custody.get(MovementBucket.CUSTODY_TRANSFER_IN, ZERO) != ZERO
        or custody.get(MovementBucket.CUSTODY_TRANSFER_OUT, ZERO) != ZERO
    ):
        findings.append(
            Finding(
                severity=ADVISORY,
                code="kitchen_custody_is_not_consumption",
                message=(
                    "custody transfers are present and are reported outside "
                    "consumption, as required"
                ),
            )
        )
    return findings


def verify_document_links() -> list[Finding]:
    """
    Attribution stays inside its source, and points at things that exist.

    Recomputes the cap from the rows rather than trusting the trigger. A guard
    and a verifier that read the same stored total would agree even when both
    were wrong; these two arrive at the number by different routes.
    """
    from apps.kitchen.models import (
        BatchDocumentLink,
        BatchDocumentLinkStatus,
        ProductionBatchStatus,
    )

    findings: list[Finding] = []
    live = BatchDocumentLink.objects.filter(status=BatchDocumentLinkStatus.ACTIVE).select_related(
        "batch", "transfer_line", "waste_line", "item"
    )

    attributed: dict[tuple[str, int], Decimal] = {}
    capacity: dict[tuple[str, int], Decimal] = {}

    for link in live:
        if link.batch.status not in {ProductionBatchStatus.POSTED, ProductionBatchStatus.REVERSED}:
            findings.append(
                Finding(
                    severity=ERROR,
                    code="kitchen_link_batch_is_not_posted",
                    message=f"link {link.public_id} points at a {link.batch.status} batch",
                )
            )
        transfer_line = link.transfer_line
        waste_line = link.waste_line
        if transfer_line is not None:
            key = ("transfer", transfer_line.pk)
            capacity[key] = transfer_line.base_quantity
            source_item_id = transfer_line.item_id
        elif waste_line is not None:
            key = ("waste", waste_line.pk)
            capacity[key] = waste_line.base_quantity
            source_item_id = waste_line.item_id
        else:  # pragma: no cover - a check constraint forbids it
            findings.append(
                Finding(
                    severity=ERROR,
                    code="kitchen_link_has_no_source",
                    message=f"link {link.public_id} names no inventory source line",
                )
            )
            continue

        attributed[key] = attributed.get(key, ZERO) + link.attributed_quantity

        if link.item_id != source_item_id:
            findings.append(
                Finding(
                    severity=ERROR,
                    code="kitchen_link_item_disagrees_with_source",
                    message=f"link {link.public_id} item does not match its source line",
                )
            )
        if link.warehouse_id != link.batch.warehouse_id:
            findings.append(
                Finding(
                    severity=ERROR,
                    code="kitchen_link_warehouse_disagrees_with_batch",
                    message=f"link {link.public_id} warehouse does not match its batch",
                )
            )

    for key, total in attributed.items():
        available = capacity.get(key, ZERO)
        if total > available:
            findings.append(
                Finding(
                    severity=ERROR,
                    code="kitchen_link_over_attributed",
                    message=(
                        f"{key[0]} line {key[1]}: attributed {total:f} exceeds "
                        f"available {available:f}"
                    ),
                )
            )
    return findings


def verify_batch_consumption(user: User, production: ProductionFilters) -> list[Finding]:
    """
    Each posted batch's recorded actuals agree with the movements it posted.

    Quantities only. The value equation needs cost permission and is checked by
    `verify_production` and by the posting verifier, which already hold it.
    """
    from apps.kitchen.consumption import batch_actual_consumption
    from apps.kitchen.productivity import posted_batches

    findings: list[Finding] = []
    for batch in posted_batches(user, production).select_related("recipe", "recipe_version"):
        report = batch_actual_consumption(batch)
        for item_id, difference in report.quantity_differences.items():
            findings.append(
                Finding(
                    severity=ERROR,
                    code="kitchen_batch_actual_disagrees_with_movements",
                    message=(
                        f"batch {batch.number} item {item_id}: recorded actuals differ "
                        f"from PRODUCTION_OUT by {difference:f}"
                    ),
                )
            )
    return findings


def verify_theoretical_coverage(user: User, meals: MealUsageFilters) -> list[Finding]:
    """
    The sales limitation is present and named, and no final claim is made.

    Reported as `COVERAGE_LIMITATION`, which is **not** an error: a missing
    module is not a defect in this one, and a verifier that exited non-zero for
    it would make the command useless as a gate for the whole of Phase 3.
    """
    coverage = theoretical_consumption_coverage(user, meals)
    findings = [
        Finding(
            severity=COVERAGE_LIMITATION,
            code=SALES_NOT_INCLUDED,
            message=str(SALES_COVERAGE_NOTICE),
        )
    ]
    for missing in coverage.missing_sources:
        findings.append(
            Finding(
                severity=COVERAGE_LIMITATION,
                code="kitchen_theoretical_source_absent",
                message=f"{missing.source_type} has no adapter: {missing.status}",
            )
        )
    if any(
        row.source_type == TheoreticalSourceType.SALES and row.is_available
        for row in coverage.sources
    ):
        findings.append(
            Finding(
                severity=ERROR,
                code="kitchen_sales_source_claims_availability",
                message="a SALES theoretical adapter reported itself available in Phase 3",
            )
        )
    if coverage.is_final:  # pragma: no cover - a constant False
        findings.append(
            Finding(
                severity=ERROR,
                code="kitchen_theoretical_claims_finality",
                message="theoretical coverage claimed to be final without sales quantities",
            )
        )
    return findings


CONSUMPTION_ADVISORIES: tuple[Promise, ...] = (
    _("تحويل العهدة ليس استهلاكاً، ولا يُطرح من صرف الإنتاج المرحّل."),
    _("الفاقد الطبيعي للإنتاج ليس هالكاً، ولا يُجمع معه."),
    _("سجل الوجبات لا يحرّك مخزناً ولا يكتب قيداً؛ وهو مصدر تفسيري منفصل."),
)


__all__ = [
    "ADVISORY",
    "COVERAGE_LIMITATION",
    "CONSUMPTION_ADVISORIES",
    "ERROR",
    "Finding",
    "UsageDiagnosticRow",
    "UsageVarianceAnalysis",
    "usage_variance_analysis",
    "verify_batch_consumption",
    "verify_document_links",
    "verify_movement_partition",
    "verify_theoretical_coverage",
]
