"""
Productivity, yield and batch variance: reads over what production already did.

Nothing here posts, and nothing here stores. Every figure is derived from rows
the posting service already wrote, because a stored yield goes stale the moment
a batch is reversed and the only way to trust it would be to recompute it —
which is this module.

## The four numbers, and the one that is not a number

    expected output  = version expected output x batch multiplier
    output variance  = actual output - expected output
    yield ratio      = actual output / expected output      (expected > 0)
    input variance   = comparable actual - planned          (same dimension)

The last one is the careful one. A kitchen may substitute across dimensions —
RCP-022 approves *items*, never conversions — so a requirement for 4 KG met
with 3 L of something else has no quantity variance at all. The honest answer
is the words "not quantitatively comparable", and this module returns `None`
rather than a zero, because a zero would read as "no variance" and be wrong in
the direction nobody checks.

## Normal loss is not waste

The gap between expected and actual output is **normal production loss**. It is
absorbed into the produced item's unit cost — 50 kg of inputs worth 70,000
becoming 42 kg makes those 42 kg worth 70,000 — and it writes no Waste
document, no journal and no variance account. That is RCP-035, and it is why
this report is where a kitchen manager sees a yield problem: there is nowhere
else for it to show up.

Abnormal waste is a different thing with its own Inventory document, its own
reason code and its own accounting, and `kitchen_operations.py` reads it
separately. The two are never added together.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import QuerySet

from apps.core.money import quantize_unit_price
from apps.core.quantity import quantize_calculation
from apps.kitchen.models import (
    ProductionBatch,
    ProductionBatchLine,
    ProductionBatchStatus,
)
from apps.kitchen.production import ConsumptionComparison, consumption_comparisons

if TYPE_CHECKING:
    from apps.users.models import User

ZERO = Decimal("0")


@dataclass(frozen=True)
class ProductionFilters:
    """What narrows a production read. Every field optional, all combinable."""

    warehouse_id: int | None = None
    branch_id: int | None = None
    recipe_id: int | None = None
    version_id: int | None = None
    batch_id: int | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    status: str = ""


def posted_batches(user: User, filters: ProductionFilters) -> QuerySet[ProductionBatch]:
    """
    Every batch this caller may read that has actually moved stock.

    Starts from `visible_production_batches`, so the warehouse scope is the
    same one the workspace uses. A report that widened its own scope would be
    the one place somebody could read another branch's production.
    """
    from apps.kitchen.selectors import visible_production_batches

    rows = visible_production_batches(user).exclude(status=ProductionBatchStatus.DRAFT)
    return _narrow(rows, filters)


def register_rows(user: User, filters: ProductionFilters) -> QuerySet[ProductionBatch]:
    """The production register: what was made, when, by whom, at what scale."""
    return posted_batches(user, filters).select_related(
        "recipe", "recipe_version", "branch", "warehouse", "output_item", "output_lot", "posted_by"
    )


def _narrow(
    rows: QuerySet[ProductionBatch], filters: ProductionFilters
) -> QuerySet[ProductionBatch]:
    if filters.warehouse_id:
        rows = rows.filter(warehouse_id=filters.warehouse_id)
    if filters.branch_id:
        rows = rows.filter(branch_id=filters.branch_id)
    if filters.recipe_id:
        rows = rows.filter(recipe_id=filters.recipe_id)
    if filters.version_id:
        rows = rows.filter(recipe_version_id=filters.version_id)
    if filters.batch_id:
        rows = rows.filter(pk=filters.batch_id)
    if filters.date_from:
        rows = rows.filter(planned_business_date__gte=filters.date_from)
    if filters.date_to:
        rows = rows.filter(planned_business_date__lte=filters.date_to)
    if filters.status:
        rows = rows.filter(status=filters.status)
    return rows


# ---------------------------------------------------------------------------
# Productivity and yield
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class YieldRow:
    """One posted batch, measured against what its recipe expected."""

    batch: ProductionBatch
    expected_output: Decimal
    actual_output: Decimal
    output_variance: Decimal
    #: `None` when the expected output is zero — a ratio against nothing is not
    #: infinity, it is a question that was never asked.
    yield_ratio: Decimal | None
    #: Consumed value divided by what actually came out. The whole of RCP-035:
    #: a poor yield makes the produced kilo dearer and moves nothing else.
    actual_unit_cost: Decimal | None
    normal_loss: Decimal
    is_reversed: bool

    @property
    def yield_percent(self) -> Decimal | None:
        if self.yield_ratio is None:
            return None
        return quantize_calculation(self.yield_ratio * Decimal("100"))

    @property
    def has_shortfall(self) -> bool:
        """Less came out than the recipe expected. Not an error — a subject."""
        return self.output_variance < ZERO


def yield_rows(user: User, filters: ProductionFilters) -> list[YieldRow]:
    """Expected against actual, per posted batch, newest first."""
    return [_yield_row(batch) for batch in register_rows(user, filters)]


def _yield_row(batch: ProductionBatch) -> YieldRow:
    expected = quantize_calculation(batch.expected_output_quantity)
    actual = quantize_calculation(batch.actual_output_base_quantity or ZERO)
    ratio = quantize_calculation(actual / expected) if expected > ZERO else None
    unit_cost = (
        quantize_unit_price(batch.output_value / actual)
        if actual > ZERO and batch.output_value is not None
        else None
    )
    return YieldRow(
        batch=batch,
        expected_output=expected,
        actual_output=actual,
        output_variance=quantize_calculation(actual - expected),
        yield_ratio=ratio,
        actual_unit_cost=unit_cost,
        # Named rather than derived at the template, because "normal loss" is a
        # classification and not merely a subtraction: it is the shortfall that
        # is absorbed into unit cost, and it is never abnormal waste.
        normal_loss=quantize_calculation(expected - actual) if actual < expected else ZERO,
        is_reversed=batch.status == ProductionBatchStatus.REVERSED,
    )


# ---------------------------------------------------------------------------
# Batch variance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VarianceRow:
    """One requirement, planned against consumed, with its component path."""

    line: ProductionBatchLine
    comparison: ConsumptionComparison | None
    planned: Decimal
    #: `None` where the dimensions disagree. Never zero — see the module header.
    comparable_actual: Decimal | None
    variance: Decimal | None

    @property
    def component_path(self) -> str:
        return self.line.component_path or "-"

    @property
    def is_comparable(self) -> bool:
        return self.variance is not None

    @property
    def statement(self) -> str:
        return self.comparison.statement if self.comparison is not None else ""


def variance_rows(batch: ProductionBatch) -> list[VarianceRow]:
    """
    Planned against consumed for one batch, in the expansion's own order.

    Grouping happens in `variance_by_component`, derived from this list rather
    than queried separately, so the grouped totals cannot disagree with the
    ungrouped ones.
    """
    comparisons = {row.line.pk: row for row in consumption_comparisons(batch)}
    rows: list[VarianceRow] = []
    for line in batch.lines.select_related("item", "item__base_unit").order_by("line_order"):
        comparison = comparisons.get(line.pk)
        comparable = comparison.comparable_quantity if comparison is not None else None
        rows.append(
            VarianceRow(
                line=line,
                comparison=comparison,
                planned=quantize_calculation(line.planned_base_quantity),
                comparable_actual=comparable,
                variance=(
                    quantize_calculation(comparable - line.planned_base_quantity)
                    if comparable is not None
                    else None
                ),
            )
        )
    return rows


def variance_by_component(batch: ProductionBatch) -> list[tuple[str, list[VarianceRow]]]:
    """
    The same rows, grouped by the path they came from (RCP-080).

    "Was the overspend in the dish or in the blend?" is the batch variance
    report's whole subject, and a flattened list answers it with a shrug.

    A list of pairs rather than a dict, because a template iterates it and the
    order has to stay the expansion's own.
    """
    grouped: dict[str, list[VarianceRow]] = {}
    for row in variance_rows(batch):
        grouped.setdefault(row.component_path, []).append(row)
    return list(grouped.items())


__all__ = [
    "ProductionFilters",
    "VarianceRow",
    "YieldRow",
    "posted_batches",
    "register_rows",
    "variance_by_component",
    "variance_rows",
    "yield_rows",
]
