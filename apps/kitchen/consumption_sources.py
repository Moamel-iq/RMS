"""
Where theoretical consumption comes from, and the one source that does not exist yet.

Theoretical consumption is *recorded quantities × the recipe version in force
when each was recorded*. The arithmetic never changes; only the list of things
that count as a recorded quantity does. So this module defines a **source
interface** and registers the adapters that exist, and Phase 4 adds sales by
writing one more adapter rather than by touching any arithmetic.

## What is registered, and what is conspicuously absent

Two adapters ship: `STAFF_MEAL` and `COMPLIMENTARY_MEAL`. The `SALES` adapter
is **absent** — not stubbed, not returning an empty list, not present with a
flag. Absent, so that nothing can accidentally read it as "sales contributed
nothing".

`SALES` is nevertheless a declared member of `TheoreticalSourceType`, and the
distinction is the point: the vocabulary reserves the value so the coverage
report can name a source it does not have, which is a very different statement
from silence.

## Why meals are not added to production plans

A `ProductionBatch`'s planned lines and a `MealRecord`'s expansion **overlap
physically**. The batch already contains the ingredients that produced the
output; the meal record explains where some of those produced portions went.
Expanding both to raw materials and adding them counts the same rice twice.

No combined total is offered here, and that is deliberate: a combined figure
would need a deduplication key linking each meal portion to the batch that
produced it, and no such key exists — a meal is recorded against a recipe and a
date, not against a batch. Until one exists the honest presentation is separate
explanatory buckets, which is what `theoretical_consumption_coverage` returns.

## The exact version, always

Every expansion walks `record.recipe_version` — the version stored on the meal
when it was recorded — and nothing here re-resolves. A recipe changed in June
must not restate what March consumed, and that is the charter's absolute rule
rather than a preference.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from django.utils.translation import gettext_lazy as _

from apps.core.quantity import quantize_calculation
from apps.kitchen.expansion import expand_recipe_version
from apps.kitchen.models import MealRecord, MealRecordStatus, MealType

if TYPE_CHECKING:
    from django.utils.functional import Promise

    from apps.users.models import User

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Coverage vocabulary
# ---------------------------------------------------------------------------


class TheoreticalSourceType(StrEnum):
    """
    The closed vocabulary of things that can contribute a recorded quantity.

    `SALES` is declared and **not implemented**. Reserving the value is what
    lets the coverage report say "the sales source is missing" instead of
    returning a total that silently excludes it.
    """

    SALES = "SALES"
    STAFF_MEAL = "STAFF_MEAL"
    COMPLIMENTARY_MEAL = "COMPLIMENTARY_MEAL"


class CoverageStatus(StrEnum):
    """Whether a declared source can actually answer."""

    AVAILABLE = "AVAILABLE"
    #: The adapter is not registered in this deployment because its data does
    #: not exist here. Not an error, and — since Phase 4 — not permanent: the
    #: value is reported for a *declared* source type with no adapter, which is
    #: a statement about what is installed rather than about what is built.
    DEFERRED_TO_PHASE_4 = "DEFERRED_TO_PHASE_4"


#: Stamped on every theoretical response, without exception and regardless of
#: filters. A reader who sees a number must see this beside it.
SALES_NOT_INCLUDED = "SALES_NOT_INCLUDED_PHASE_4"

#: The counterpart, stamped once Phase 4's adapter is registered. Its own
#: value rather than the absence of the first, because a reader of a CSV has
#: to see which of the two claims a figure carries — silence is not a claim.
SALES_INCLUDED = "SALES_INCLUDED"

#: What a partial diagnostic is, so nobody can mistake it for the real thing.
PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
NOT_FINAL_USAGE_VARIANCE = "NOT_FINAL_USAGE_VARIANCE"
COMPLETE_COVERAGE = "COMPLETE_COVERAGE"
FINAL_USAGE_VARIANCE = "FINAL_USAGE_VARIANCE"
FINAL_SALES_USAGE_VARIANCE_NOT_AVAILABLE = "FINAL_SALES_USAGE_VARIANCE_NOT_AVAILABLE"

#: Task 3.7's standing limitation, restated here because this is the module
#: that turns a meal into a consumption figure and somebody reading that figure
#: will ask where the expense went (RCP-044).
MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED = "MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED"

#: The notice every theoretical and variance surface renders, in the exact
#: approved wording.
SALES_COVERAGE_NOTICE: Promise = _(
    "الاستهلاك النظري المعتمد على المبيعات غير مكتمل حالياً؛ "
    "سيتم ربط كميات المبيعات المعتمدة في المرحلة الرابعة."
)

#: Rendered once approved sales quantities are part of the figure.
SALES_INCLUDED_NOTICE: Promise = _(
    "الاستهلاك النظري يشمل كميات المبيعات المعتمدة ووجبات الموظفين والوجبات المجانية."
)

#: Rendered beside any partial usage-variance figure.
PARTIAL_VARIANCE_NOTICE: Promise = _(
    "هذا تشخيص جزئي وليس انحراف الاستهلاك النهائي: الجانب النظري لا يشمل "
    "كميات المبيعات المعتمدة، وهي شرط لأي رقم نهائي."
)

#: Arabic for each source type, for a screen.
SOURCE_LABELS: dict[TheoreticalSourceType, Promise] = {
    TheoreticalSourceType.SALES: _("المبيعات المعتمدة"),
    TheoreticalSourceType.STAFF_MEAL: _("وجبات الموظفين"),
    TheoreticalSourceType.COMPLIMENTARY_MEAL: _("الوجبات المجانية"),
}

#: The label a meal-equivalent row carries in a report and in a CSV, so the
#: bucket a number came from survives the export.
STAFF_MEAL_EQUIVALENT = "STAFF_MEAL_EQUIVALENT"
COMPLIMENTARY_MEAL_EQUIVALENT = "COMPLIMENTARY_MEAL_EQUIVALENT"

_EQUIVALENT_LABELS: dict[TheoreticalSourceType, str] = {
    TheoreticalSourceType.STAFF_MEAL: STAFF_MEAL_EQUIVALENT,
    TheoreticalSourceType.COMPLIMENTARY_MEAL: COMPLIMENTARY_MEAL_EQUIVALENT,
}

_MEAL_TYPE_FOR_SOURCE: dict[TheoreticalSourceType, str] = {
    TheoreticalSourceType.STAFF_MEAL: MealType.STAFF,
    TheoreticalSourceType.COMPLIMENTARY_MEAL: MealType.COMPLIMENTARY,
}


# ---------------------------------------------------------------------------
# The contribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TheoreticalConsumptionContribution:
    """
    One leaf ingredient, from one recorded quantity, at one exact version.

    Frozen and self-describing. Everything a caller needs to audit the figure is
    on the row — which record produced it, which version and serving were used,
    what the approved quantity was, which component path the leaf sits on — so
    a variance report can be argued with rather than merely believed.
    """

    source_type: TheoreticalSourceType
    #: The immutable public identity of the record this came from. A UUID rather
    #: than a primary key, because this crosses an API boundary and a sequential
    #: id is a census of somebody else's records.
    source_public_id: uuid.UUID
    source_reference: str
    equivalent_label: str

    organization_id: int
    branch_id: int
    business_date: datetime.date

    recipe_id: int
    recipe_code: str
    recipe_name: str
    recipe_version_id: int
    version_number: int
    serving_code: str

    #: The recorded quantity as approved — portions of a meal, and in Phase 4
    #: sold units of a menu item.
    approved_quantity: Decimal
    #: Component path from the root version. Empty for the root's own lines.
    component_path: str

    leaf_item_id: int
    leaf_item_code: str
    leaf_item_name: str
    base_unit_code: str
    #: The leaf's own base quantity, scaled through every multiplier from the
    #: root and through the recorded quantity. This is the theoretical figure.
    effective_base_quantity: Decimal

    coverage_code: str = SALES_NOT_INCLUDED


class TheoreticalConsumptionSource(Protocol):
    """
    What Phase 4 has to implement, and nothing more.

    A source turns records into contributions. It does not know about variance,
    about actual consumption, about reports or about permissions — the caller
    has already scoped the read before it gets here.
    """

    # Read-only members rather than mutable attributes, so a **frozen**
    # dataclass can satisfy this. An adapter whose source type could be
    # reassigned after construction is an adapter that can be pointed at the
    # wrong records halfway through a report.
    @property
    def source_type(self) -> TheoreticalSourceType:  # pragma: no cover - a protocol
        ...

    @property
    def coverage_status(self) -> CoverageStatus:  # pragma: no cover - a protocol
        ...

    def contributions(
        self,
        *,
        organization_id: int,
        branch_ids: Sequence[int] | None,
        date_from: datetime.date | None,
        date_to: datetime.date | None,
        recipe_id: int | None,
    ) -> list[TheoreticalConsumptionContribution]:  # pragma: no cover - a protocol
        ...


# ---------------------------------------------------------------------------
# The meal adapters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MealUsageFilters:
    """What narrows a meal-equivalent read."""

    branch_id: int | None = None
    recipe_id: int | None = None
    item_id: int | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None


def meal_batch_fraction(record: MealRecord) -> Decimal:
    """
    How much of one recipe batch this meal record represents.

    Two paths, and both read approved data rather than converting anything:

    * **With a serving**, `RecipeServing.factor_of_batch` is the fraction of a
      batch one portion is — approved on the version, and the same figure the
      cost card allocates through. Portions × that factor is the answer.
    * **Without servings**, the meal was recorded in the version's own output
      unit, so the fraction is `output quantity ÷ the version's expected output`.

    Full precision throughout; the caller quantizes once at the end (ADR-006).
    """
    serving = record.serving
    if serving is not None:
        return record.quantity * serving.factor_of_batch
    expected = record.recipe_version.expected_output_quantity
    if expected <= ZERO:  # pragma: no cover - a version constraint forbids it
        return ZERO
    return record.output_base_quantity / expected


def _contributions_for(
    record: MealRecord, source_type: TheoreticalSourceType
) -> list[TheoreticalConsumptionContribution]:
    """Expand one recorded meal into its leaf ingredients, at its own version."""
    fraction = meal_batch_fraction(record)
    if fraction == ZERO:
        return []

    version = record.recipe_version
    serving = record.serving
    rows: list[TheoreticalConsumptionContribution] = []
    # The **stored** version, never a re-resolved one. See the module docstring.
    for leaf in expand_recipe_version(version):
        line = leaf.line
        item = line.item
        rows.append(
            TheoreticalConsumptionContribution(
                source_type=source_type,
                source_public_id=record.public_id,
                source_reference=f"{record.recipe.code} @ {record.consumed_on.isoformat()}",
                equivalent_label=_EQUIVALENT_LABELS[source_type],
                organization_id=record.organization_id,
                branch_id=record.branch_id,
                business_date=record.consumed_on,
                recipe_id=record.recipe_id,
                recipe_code=record.recipe.code,
                recipe_name=record.recipe.name_ar,
                recipe_version_id=version.pk,
                version_number=version.version_number,
                serving_code=serving.code if serving is not None else "",
                approved_quantity=quantize_calculation(record.quantity),
                component_path=leaf.path_display,
                leaf_item_id=line.item_id,
                leaf_item_code=item.code,
                leaf_item_name=item.name_ar,
                base_unit_code=item.base_unit.code,
                effective_base_quantity=quantize_calculation(
                    line.base_quantity * leaf.cumulative_multiplier * fraction
                ),
            )
        )
    return rows


@dataclass(frozen=True)
class MealEquivalentSource:
    """
    A `TheoreticalConsumptionSource` over `MealRecord`, one meal type each.

    Two instances rather than one with a filter, because a coverage report has
    to be able to say that staff meals are available and complimentary meals
    are available *separately* — and because Phase 4's sales adapter will be a
    third peer, not a third branch inside this one.
    """

    source_type: TheoreticalSourceType
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
            MealRecord.objects.filter(
                organization_id=organization_id,
                meal_type=_MEAL_TYPE_FOR_SOURCE[self.source_type],
                # A cancelled meal contributes zero. Not a zero row — no row,
                # because the correction said the meal never happened.
                status=MealRecordStatus.RECORDED,
            )
            .select_related("recipe", "recipe_version", "recipe_version__output_unit", "serving")
            .order_by("consumed_on", "pk")
        )
        if branch_ids is not None:
            rows = rows.filter(branch_id__in=branch_ids)
        if date_from:
            rows = rows.filter(consumed_on__gte=date_from)
        if date_to:
            rows = rows.filter(consumed_on__lte=date_to)
        if recipe_id:
            rows = rows.filter(recipe_id=recipe_id)

        contributions: list[TheoreticalConsumptionContribution] = []
        for record in rows:
            contributions.extend(_contributions_for(record, self.source_type))
        return contributions


#: Every source that can actually answer, in this deployment, right now.
#:
#: The two meal adapters are built in. `SALES` is **registered from outside**
#: by `apps.sales` at app-ready, and that inversion is the whole reason this is
#: a mutable registry rather than a literal: the kitchen must not import a
#: sales model, and sales must not reach inside the kitchen's arithmetic
#: (ADR-027 section 9).
#:
#: `theoretical_consumption_coverage` reports every declared source *type*
#: against this registry, so a deployment where sales is not installed still
#: names the gap honestly, and one where it is reports a final figure.
REGISTERED_SOURCES: tuple[TheoreticalConsumptionSource, ...] = (
    MealEquivalentSource(source_type=TheoreticalSourceType.STAFF_MEAL),
    MealEquivalentSource(source_type=TheoreticalSourceType.COMPLIMENTARY_MEAL),
)

#: The kitchen's own adapters. A module registering from outside may not
#: replace one: the meal sources are this module's, and something that could
#: shadow them could silently restate what the staff ate.
_BUILT_IN = frozenset({TheoreticalSourceType.STAFF_MEAL, TheoreticalSourceType.COMPLIMENTARY_MEAL})


def register_theoretical_source(source: TheoreticalConsumptionSource) -> None:
    """
    Add a source the kitchen does not own. Called by the module that owns it.

    **Idempotent by source type.** `AppConfig.ready()` can run more than once
    in a test process, and a registry that accumulated duplicates would count
    every sales contribution twice — precisely the double count this whole area
    exists to avoid. Re-registering a type replaces it rather than appending.
    """
    global REGISTERED_SOURCES  # noqa: PLW0603 - a registry is what this is

    if source.source_type in _BUILT_IN:
        raise ValueError(
            f"{source.source_type} is the kitchen's own adapter and may not be replaced"
        )
    REGISTERED_SOURCES = (
        *(row for row in REGISTERED_SOURCES if row.source_type != source.source_type),
        source,
    )


def sales_source_is_registered() -> bool:
    """
    Whether approved sales quantities can be read at all.

    The one question every coverage code is computed from. A deployment without
    `apps.sales` answers `False` and keeps Phase 3's honest limitation; one with
    it answers `True` and the limitation disappears — which is what Task 3.8
    promised when it reserved the value and shipped no adapter.
    """
    return any(row.source_type == TheoreticalSourceType.SALES for row in REGISTERED_SOURCES)


def coverage_code() -> str:
    """
    The code stamped on every theoretical figure, computed rather than fixed.

    Task 3.8 wrote `SALES_NOT_INCLUDED_PHASE_4` as a constant default, which was
    correct while no adapter could exist. It is a *fact about the deployment*
    now, so it is answered rather than asserted.
    """
    return SALES_INCLUDED if sales_source_is_registered() else SALES_NOT_INCLUDED


def coverage_labels() -> tuple[str, str]:
    """
    The pair a usage-variance surface stamps: coverage, then finality.

    Returned together because they move together. A variance whose theoretical
    side excludes sales is partial *and* non-final; one that includes it is
    neither, and no combination in between is meaningful.
    """
    if sales_source_is_registered():
        return (COMPLETE_COVERAGE, FINAL_USAGE_VARIANCE)
    return (PARTIAL_COVERAGE, NOT_FINAL_USAGE_VARIANCE)


def _scoped(user: User, filters: MealUsageFilters) -> tuple[int, list[int]] | None:
    """
    The organization and branches this caller may read meals in.

    Returns `None` when the caller reaches nothing, which every caller treats
    as an empty report rather than as an error: a user with no membership has
    an empty kitchen, not a broken one.
    """
    from apps.kitchen.selectors import visible_meal_records

    visible = visible_meal_records(user)
    if filters.branch_id:
        visible = visible.filter(branch_id=filters.branch_id)
    identifiers = list(visible.values_list("organization_id", "branch_id").distinct())
    if not identifiers:
        return None
    organization_id = identifiers[0][0]
    return organization_id, sorted({branch_id for _org, branch_id in identifiers})


def _usage(
    user: User, filters: MealUsageFilters, source_type: TheoreticalSourceType
) -> list[TheoreticalConsumptionContribution]:
    scope = _scoped(user, filters)
    if scope is None:
        return []
    organization_id, branch_ids = scope
    source = next(row for row in REGISTERED_SOURCES if row.source_type == source_type)
    rows = source.contributions(
        organization_id=organization_id,
        branch_ids=branch_ids,
        date_from=filters.date_from,
        date_to=filters.date_to,
        recipe_id=filters.recipe_id,
    )
    if filters.item_id:
        rows = [row for row in rows if row.leaf_item_id == filters.item_id]
    return rows


def staff_meal_equivalent_usage(
    user: User, filters: MealUsageFilters
) -> list[TheoreticalConsumptionContribution]:
    """
    `STAFF_MEAL_EQUIVALENT` — what staff meals imply in raw ingredients.

    An **explanation**, not a consumption: the ingredients already left stock
    through the batch that cooked them or the issue that took them out. This
    figure exists so fed-but-not-sold portions stop surfacing as unexplained
    variance (RCP-043), and it is shown as its own bucket rather than added to
    anything.
    """
    return _usage(user, filters, TheoreticalSourceType.STAFF_MEAL)


def complimentary_meal_equivalent_usage(
    user: User, filters: MealUsageFilters
) -> list[TheoreticalConsumptionContribution]:
    """`COMPLIMENTARY_MEAL_EQUIVALENT` — the same, for hospitality portions."""
    return _usage(user, filters, TheoreticalSourceType.COMPLIMENTARY_MEAL)


# ---------------------------------------------------------------------------
# Aggregation and coverage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquivalentTotal:
    """One leaf item, summed across the contributions of one source."""

    source_type: TheoreticalSourceType
    equivalent_label: str
    leaf_item_id: int
    leaf_item_code: str
    leaf_item_name: str
    base_unit_code: str
    effective_base_quantity: Decimal
    contribution_count: int
    coverage_code: str = SALES_NOT_INCLUDED


def totals_by_item(
    contributions: Iterable[TheoreticalConsumptionContribution],
) -> list[EquivalentTotal]:
    """
    Contributions summed per leaf item, per source, in item-code order.

    Summed **within** a source only. Adding two sources together is exactly the
    double count this module refuses to make, so there is no `grand_total` here
    and adding one would need a deduplication key that does not exist.
    """
    grouped: dict[tuple[TheoreticalSourceType, int], EquivalentTotal] = {}
    for row in contributions:
        key = (row.source_type, row.leaf_item_id)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = EquivalentTotal(
                source_type=row.source_type,
                equivalent_label=row.equivalent_label,
                leaf_item_id=row.leaf_item_id,
                leaf_item_code=row.leaf_item_code,
                leaf_item_name=row.leaf_item_name,
                base_unit_code=row.base_unit_code,
                effective_base_quantity=row.effective_base_quantity,
                contribution_count=1,
            )
            continue
        grouped[key] = EquivalentTotal(
            source_type=existing.source_type,
            equivalent_label=existing.equivalent_label,
            leaf_item_id=existing.leaf_item_id,
            leaf_item_code=existing.leaf_item_code,
            leaf_item_name=existing.leaf_item_name,
            base_unit_code=existing.base_unit_code,
            effective_base_quantity=quantize_calculation(
                existing.effective_base_quantity + row.effective_base_quantity
            ),
            contribution_count=existing.contribution_count + 1,
        )
    return sorted(grouped.values(), key=lambda row: (row.source_type, row.leaf_item_code))


@dataclass(frozen=True)
class SourceCoverage:
    """One declared source type, and whether it could answer."""

    source_type: TheoreticalSourceType
    status: CoverageStatus
    contribution_count: int
    total_quantity: Decimal

    @property
    def label(self) -> Promise:
        return SOURCE_LABELS[self.source_type]

    @property
    def is_available(self) -> bool:
        return self.status is CoverageStatus.AVAILABLE


@dataclass(frozen=True)
class TheoreticalCoverage:
    """
    The theoretical side of consumption, and an honest account of its holes.

    `is_final` is a **constant `False`** in Phase 3 and reads as one on
    purpose. There is no filter, no permission and no date range that makes it
    true, because the missing input is a whole module. A boolean computed from
    the data would eventually compute `True` for an empty period, and an empty
    period is the case where a false "final" claim does the most damage.
    """

    sources: tuple[SourceCoverage, ...]
    totals: tuple[EquivalentTotal, ...]
    coverage_code: str = SALES_NOT_INCLUDED
    is_final: bool = False

    @property
    def notice(self) -> Promise:
        """
        The sentence beside the figure — or the other one, once sales count.

        Phase 3 returned the limitation unconditionally, which was right when
        no adapter could exist. Continuing to show it after Phase 4 registered
        one would be the opposite failure: a complete figure carrying a
        warning that it is incomplete.
        """
        if self.is_final:
            return SALES_INCLUDED_NOTICE
        return SALES_COVERAGE_NOTICE

    @property
    def missing_sources(self) -> tuple[SourceCoverage, ...]:
        return tuple(row for row in self.sources if not row.is_available)


def theoretical_consumption_coverage(user: User, filters: MealUsageFilters) -> TheoreticalCoverage:
    """
    Every declared source, reported against the registry, with the gap named.

    Iterates `TheoreticalSourceType` rather than `REGISTERED_SOURCES`, which is
    the whole mechanism: a declared type with no adapter appears as
    `DEFERRED_TO_PHASE_4` with a zero count, so the report says *sales are
    missing* instead of quietly summing the two sources it happens to have.
    """
    registry = {source.source_type: source for source in REGISTERED_SOURCES}
    rows: list[SourceCoverage] = []
    contributions: list[TheoreticalConsumptionContribution] = []

    for source_type in TheoreticalSourceType:
        source = registry.get(source_type)
        if source is None:
            rows.append(
                SourceCoverage(
                    source_type=source_type,
                    status=CoverageStatus.DEFERRED_TO_PHASE_4,
                    contribution_count=0,
                    total_quantity=ZERO,
                )
            )
            continue
        found = _usage(user, filters, source_type)
        contributions.extend(found)
        rows.append(
            SourceCoverage(
                source_type=source_type,
                status=CoverageStatus.AVAILABLE,
                contribution_count=len(found),
                total_quantity=quantize_calculation(
                    sum((row.effective_base_quantity for row in found), ZERO)
                ),
            )
        )

    return TheoreticalCoverage(
        sources=tuple(rows),
        totals=tuple(totals_by_item(contributions)),
        coverage_code=coverage_code(),
        # Computed from the registry rather than a hard-coded `False`. Phase
        # 3's constant was honest then and would be a lie now.
        is_final=sales_source_is_registered(),
    )


__all__ = [
    "COMPLIMENTARY_MEAL_EQUIVALENT",
    "COMPLETE_COVERAGE",
    "FINAL_SALES_USAGE_VARIANCE_NOT_AVAILABLE",
    "FINAL_USAGE_VARIANCE",
    "MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED",
    "NOT_FINAL_USAGE_VARIANCE",
    "PARTIAL_COVERAGE",
    "PARTIAL_VARIANCE_NOTICE",
    "REGISTERED_SOURCES",
    "SALES_COVERAGE_NOTICE",
    "SALES_INCLUDED",
    "SALES_INCLUDED_NOTICE",
    "SALES_NOT_INCLUDED",
    "SOURCE_LABELS",
    "STAFF_MEAL_EQUIVALENT",
    "CoverageStatus",
    "EquivalentTotal",
    "MealEquivalentSource",
    "MealUsageFilters",
    "SourceCoverage",
    "TheoreticalConsumptionContribution",
    "TheoreticalConsumptionSource",
    "TheoreticalCoverage",
    "TheoreticalSourceType",
    "complimentary_meal_equivalent_usage",
    "coverage_code",
    "coverage_labels",
    "meal_batch_fraction",
    "register_theoretical_source",
    "sales_source_is_registered",
    "staff_meal_equivalent_usage",
    "theoretical_consumption_coverage",
    "totals_by_item",
]
