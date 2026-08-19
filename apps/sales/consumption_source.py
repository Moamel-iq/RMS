"""
The `SALES` theoretical-consumption source the kitchen reserved in Phase 3.

Task 3.8 declared `TheoreticalSourceType.SALES` and deliberately shipped **no
adapter** for it. That was not an oversight and not a stub: the coverage report
iterates the enum rather than the registry precisely so it can name a source it
does not have, and every theoretical figure in Phase 3 carried
`SALES_NOT_INCLUDED_PHASE_4` beside it as a result.

This module is that adapter.

## The dependency direction

Sales imports the kitchen's public interface. The kitchen imports **nothing**
from sales — it gains a `register_theoretical_source()` function and this
module calls it at app-ready. Registration inverts the dependency without
inverting the control: the kitchen still decides what a contribution means,
which expansion runs, and what coverage is reported (ADR-027 §9).

## What is contributed, and what is subtracted

Every line of a **posted** sales day contributes, expanded through the exact
`RecipeVersion` stored on that line. Nothing here re-resolves a version: a
recipe changed in September must not restate what August consumed, and this is
the module where that would be easiest to get wrong.

`CANCELLED_BEFORE_FULFILLMENT` adjustments reduce the contributed quantity.
`RETURNED_AFTER_FULFILLMENT` does **not**, and the asymmetry is the point: a
cancelled order was never cooked, so its ingredients never left; a returned one
*was* cooked, and subtracting it would manufacture an unexplained
actual-versus-theoretical variance of exactly the returned amount, every time.
Where returned food is physically discarded that is a Waste document in the
kitchen's own ledger, and counting it here as well would be the double count
ADR-026 §4 refuses.

`cancelled_quantities` is where that asymmetry lives, and it is one filter long:
`reason_kind=CANCELLED_BEFORE_FULFILLMENT`. The intuitive implementation —
subtract every posted adjustment — reads perfectly well, is one filter shorter,
and produces a usage-variance report that is wrong in a direction nobody
questions.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from apps.core.quantity import quantize_calculation
from apps.kitchen.consumption_sources import (
    CoverageStatus,
    TheoreticalConsumptionContribution,
    TheoreticalSourceType,
)
from apps.kitchen.expansion import expand_recipe_version
from apps.sales.models import (
    SalesAdjustmentLine,
    SalesAdjustmentReasonKind,
    SalesAdjustmentStatus,
    SalesDayLine,
    SalesDayStatus,
)

ZERO = Decimal("0")

#: The label a sales contribution carries in a report and a CSV, so the bucket
#: a number came from survives the export. Named beside the kitchen's own
#: `STAFF_MEAL_EQUIVALENT` and `COMPLIMENTARY_MEAL_EQUIVALENT`.
SALES_EQUIVALENT = "SALES_EQUIVALENT"


def cancelled_quantities(line_ids: Sequence[int]) -> dict[int, Decimal]:
    """
    Posted `CANCELLED_BEFORE_FULFILLMENT` quantity per original line.

    **Only cancellations, and the filter on `reason_kind` is the whole point.**
    A `RETURNED_AFTER_FULFILLMENT` adjustment is deliberately absent: the food
    was cooked and its ingredients left through the production batch that made
    them, so subtracting it would lower theoretical while actual stayed exactly
    where it was — manufacturing an unexplained variance of precisely the
    returned quantity, in every branch that ever takes a plate back. That
    variance is indistinguishable on the report from real over-portioning or
    real theft, which is the failure: the module would generate the exact signal
    the usage-variance report exists to detect (ADR-028 §8).

    A `FINANCIAL_CORRECTION` is absent for a simpler reason: it may not carry a
    quantity at all, and a database trigger enforces that.

    Answered as one aggregate over the whole result set rather than per line. A
    per-line query would be one round trip per sold item per day, which on a
    month of six branches is tens of thousands of them.
    """
    if not line_ids:
        return {}
    rows = (
        SalesAdjustmentLine.objects.filter(
            original_line_id__in=line_ids,
            adjustment__status=SalesAdjustmentStatus.POSTED,
            adjustment__reason_kind=SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT,
        )
        .values("original_line_id")
        .annotate(total=Sum("adjusted_quantity"))
    )
    return {row["original_line_id"]: row["total"] or ZERO for row in rows}


def fulfilled_quantity(line: SalesDayLine, cancelled: Decimal = ZERO) -> Decimal:
    """
    The quantity the kitchen actually had to produce for this line.

    Never negative: a cancellation cannot exceed what was sold — the containment
    trigger in `sales/0008` guarantees it — and if data ever said otherwise the
    honest answer is zero rather than a negative ingredient requirement.
    """
    remaining = line.quantity - cancelled
    return remaining if remaining > ZERO else ZERO


@dataclass(frozen=True)
class SalesQuantitySource:
    """
    A `TheoreticalConsumptionSource` over posted `SalesDayLine` rows.

    Frozen, and a peer of the two meal adapters rather than a special case: the
    kitchen's registry holds three sources that answer the same question about
    different records, and nothing in the arithmetic knows which is which.
    """

    source_type: TheoreticalSourceType = TheoreticalSourceType.SALES
    coverage_status: CoverageStatus = CoverageStatus.AVAILABLE

    def contributions(
        self,
        *,
        organization_id: int,
        branch_ids: Sequence[int] | None,
        date_from: datetime.date | None,
        date_to: datetime.date | None,
        recipe_id: int | None,
    ) -> list[TheoreticalConsumptionContribution]:
        rows = (
            SalesDayLine.objects.filter(
                sales_day__organization_id=organization_id,
                # A draft is a proposal and a reversed day did not happen.
                # Neither contributed an ingredient to anything.
                sales_day__status=SalesDayStatus.POSTED,
            )
            .select_related(
                "sales_day",
                "sales_day__branch",
                "menu_item",
                "recipe",
                "recipe_version",
                "recipe_version__output_unit",
                "serving",
            )
            .order_by("sales_day__business_date", "sales_day_id", "sequence")
        )
        if branch_ids is not None:
            rows = rows.filter(sales_day__branch_id__in=branch_ids)
        if date_from:
            rows = rows.filter(sales_day__business_date__gte=date_from)
        if date_to:
            rows = rows.filter(sales_day__business_date__lte=date_to)
        if recipe_id:
            rows = rows.filter(recipe_id=recipe_id)

        # Built **once**, for the whole result set, before the loop. Asking per
        # line would be one round trip per sold item per day.
        lines = list(rows)
        cancelled = cancelled_quantities([line.pk for line in lines])

        contributions: list[TheoreticalConsumptionContribution] = []
        for line in lines:
            contributions.extend(_contributions_for(line, cancelled.get(line.pk, ZERO)))
        return contributions


def _contributions_for(
    line: SalesDayLine, cancelled: Decimal
) -> list[TheoreticalConsumptionContribution]:
    """
    Expand one sold line into its leaf ingredients, at its own stored version.

    The fraction of a batch is `sold servings × the serving's factor_of_batch`,
    which is the same arithmetic `meal_batch_fraction` uses for a meal recorded
    against a serving — deliberately, because the two are the same question
    asked about different records.
    """
    quantity = fulfilled_quantity(line, cancelled)
    if quantity <= ZERO:
        return []

    version = line.recipe_version
    fraction = quantity * line.serving.factor_of_batch
    if fraction <= ZERO:  # pragma: no cover - a constraint forbids a zero factor
        return []

    day = line.sales_day
    rows: list[TheoreticalConsumptionContribution] = []
    # The **stored** version, never a re-resolved one.
    for leaf in expand_recipe_version(version):
        recipe_line = leaf.line
        item = recipe_line.item
        rows.append(
            TheoreticalConsumptionContribution(
                source_type=TheoreticalSourceType.SALES,
                source_public_id=line.public_id,
                source_reference=f"{line.menu_item.code} @ {day.business_date.isoformat()}",
                equivalent_label=SALES_EQUIVALENT,
                organization_id=day.organization_id,
                branch_id=day.branch_id,
                business_date=day.business_date,
                recipe_id=line.recipe_id,
                recipe_code=line.recipe.code,
                recipe_name=line.recipe.name_ar,
                recipe_version_id=version.pk,
                version_number=version.version_number,
                serving_code=line.serving.code,
                approved_quantity=quantize_calculation(quantity),
                component_path=leaf.path_display,
                leaf_item_id=recipe_line.item_id,
                leaf_item_code=item.code,
                leaf_item_name=item.name_ar,
                base_unit_code=item.base_unit.code,
                effective_base_quantity=quantize_calculation(
                    recipe_line.base_quantity * leaf.cumulative_multiplier * fraction
                ),
            )
        )
    return rows


def register_with_kitchen() -> None:
    """
    Hand the kitchen its missing source. Called once, from `SalesConfig.ready`.

    Idempotent, because `ready()` can run more than once in a test process and
    a registry that accumulated duplicates would double every sales
    contribution — which is exactly the double count this whole area exists to
    avoid.
    """
    from apps.kitchen.consumption_sources import register_theoretical_source

    register_theoretical_source(SalesQuantitySource())


__all__ = [
    "SALES_EQUIVALENT",
    "SalesQuantitySource",
    "cancelled_quantities",
    "fulfilled_quantity",
    "register_with_kitchen",
]
