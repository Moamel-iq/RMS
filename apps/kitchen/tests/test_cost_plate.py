"""
Plate cost, and the compact allocation that has no size limit.

Two claims, and both were wrong in the first pass of Task 3.3:

* **Plate cost is Task 3.3's, and it is derived.** No model carries a
  `portions_per_batch` column, so the divisor is the version's **primary**
  `RecipeServing` — the one RCP-084 guarantees with a partial unique index. The
  first pass deferred this to Task 3.4 and relabelled the navigation to avoid
  claiming it; that deferral was not approved, and the tests here are what make
  the claim real rather than restored wording.

* **Every serving count allocates exactly.** The first pass stopped above
  `MAX_ALLOCATED_SERVINGS` and returned a rate alone. Size is not a reason to
  stop answering a business question — it is a reason to stop *building lists*.
  The allocation is now analytic, and `TestTheCompactAllocationIsTheCertifiedOne`
  holds it against `apps/core/allocation.allocate` itself so the compact form is
  a derivation of the certified semantics rather than a second opinion.
"""

from __future__ import annotations

import datetime
import tracemalloc
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.core.allocation import AllocationItem, allocate
from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.costing import (
    MAX_ENUMERATED_SERVINGS,
    ServingAllocationState,
    _compact_allocation,
    cost_recipe_on_date,
    cost_recipe_version,
    preview_recipe_cost,
)
from apps.kitchen.models import (
    Recipe,
    RecipeCostSnapshot,
    RecipeServing,
    RecipeVersion,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    add_recipe_step,
    create_draft_recipe_version,
)
from apps.kitchen.snapshots import create_recipe_cost_snapshot
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import build_complete_draft, carry_to_active, carry_to_approved, make_child_recipe

pytestmark = pytest.mark.django_db


def codes_of(error: Any) -> set[str]:
    if hasattr(error, "message"):
        return {error.code or ""}
    if hasattr(error, "error_dict"):
        return {
            code for errs in error.error_dict.values() for item in errs for code in codes_of(item)
        }
    if hasattr(error, "error_list"):
        return {code for item in error.error_list for code in codes_of(item)}
    return set()


def _today() -> datetime.date:
    from django.utils import timezone

    return timezone.localdate()


def _version(
    *,
    recipe: Recipe,
    kilogram: UnitOfMeasure,
    rice: InventoryItem,
    people: dict[str, User],
    output: str,
    servings: list[tuple[str, str, bool]],
    quantity: str = "4",
) -> RecipeVersion:
    """One rice line and whatever servings the caller asked for."""
    draft = create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal(output),
        output_unit=kilogram,
        instructions="نظرة عامة.",
        created_by=people["author"],
    )
    add_recipe_line(
        version=draft, item=rice, entered_quantity=Decimal(quantity), entered_unit=kilogram
    )
    add_recipe_step(version=draft, instruction_ar="خطوة.")
    for code, serving_quantity, primary in servings:
        add_recipe_serving(
            version=draft,
            code=code,
            name=f"حصة {code}",
            serving_quantity=Decimal(serving_quantity),
            serving_unit=kilogram,
            is_primary=primary,
        )
    return carry_to_active(
        RecipeVersion.objects.get(pk=draft.pk),
        submitter=people["author"],
        cook=people["cook"],
        keeper=people["keeper"],
        accountant=people["accountant"],
        approver=people["approver"],
    )


@pytest.fixture
def people(
    manager: User, cook: User, keeper: User, accountant: User, approver: User
) -> dict[str, User]:
    return {
        "author": manager,
        "cook": cook,
        "keeper": keeper,
        "accountant": accountant,
        "approver": approver,
    }


# ---------------------------------------------------------------------------
# Plate cost
# ---------------------------------------------------------------------------


class TestPlateCost:
    def test_an_authoritative_card_carries_a_plate_cost(
        self, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        assert card.plate is not None
        assert card.plate_problem is None
        assert card.plate_cost is not None
        assert card.plate_cost > Decimal("0")

    def test_the_basis_is_the_exact_primary_serving(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        Three servings, one primary, and the plate uses that one — not the
        first, not the largest, not the cheapest.
        """
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="10",
            servings=[("FULL", "1", False), ("HALF", "0.5", True), ("SMALL", "0.35", False)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert card.primary_serving is not None
        assert card.primary_serving.code == "HALF"
        assert card.primary_serving.is_primary is True

    def test_portions_per_batch_is_output_over_the_primary_serving(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """10 KG in 0.5 KG plates is 20 plates. Derived, never stored."""
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="10",
            servings=[("HALF", "0.5", True)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert card.portions_per_batch == Decimal("20.000000")

    def test_plate_cost_equals_the_primary_servings_own_rate(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        The equality §C asks for, asserted rather than trusted to the algebra.

        Both figures are the total times the same frozen twelve-place factor,
        computed by different code paths — the serving scenario and the plate
        basis — so this catches the day one of them starts dividing instead.
        """
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="7",
            servings=[("ONE", "1", True), ("HALF", "0.5", False)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        primary_scenario = card.primary_serving_cost()
        assert primary_scenario is not None
        assert card.plate_cost == primary_scenario.cost_per_serving

    def test_the_plate_cost_is_total_over_portions(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """4 KG of rice at 1,500 is 6,000; 20 plates of it are 300 each."""
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="10",
            servings=[("HALF", "0.5", True)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert card.total_material_cost == Decimal("6000.000")
        assert card.plate_cost == Decimal("300.000000")

    def test_the_plate_cost_reads_posted_as_of_valuation(
        self, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        """Yesterday's card sees no stock, so it has no plate cost to state."""
        yesterday = _today() - datetime.timedelta(days=1)
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=yesterday
        )
        assert str(card.cutoff.mode) == "POSTED_AS_OF"
        assert card.cutoff.posted_sequence == 0
        assert card.plate is not None
        assert card.plate_cost == Decimal("0.000000")
        assert not card.is_complete

    def test_a_historical_plate_cost_resolves_the_version_first(
        self, valued_store: Warehouse, costable_version: RecipeVersion, branch: Branch
    ) -> None:
        card = cost_recipe_on_date(
            recipe=costable_version.recipe,
            branch=branch,
            warehouse=valued_store,
            on_date=_today(),
        )
        assert card.version.pk == costable_version.pk
        assert card.plate is not None

    def test_a_superseded_exact_child_gives_the_same_plate_basis(
        self,
        valued_store: Warehouse,
        organization: Organization,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        The parent's plate cost is a fact about the parent's own primary
        serving and its frozen children. Superseding a child changes neither.
        """
        from apps.kitchen.lifecycle import activate_recipe_version
        from apps.kitchen.services import create_recipe_component

        child_recipe = make_child_recipe(
            organization=organization, code="PLATE-BLEND", author=people["author"]
        )
        child = carry_to_active(
            build_complete_draft(
                recipe=child_recipe, unit=kilogram, item=rice, author=people["author"]
            ),
            submitter=people["author"],
            cook=people["cook"],
            keeper=people["keeper"],
            accountant=people["accountant"],
            approver=people["approver"],
        )
        parent_recipe = make_child_recipe(
            organization=organization, code="PLATE-DISH", author=people["author"]
        )
        parent_draft = build_complete_draft(
            recipe=parent_recipe, unit=kilogram, item=rice, author=people["author"]
        )
        create_recipe_component(
            version=parent_draft, component_version=child, multiplier=Decimal("0.5")
        )
        parent = carry_to_active(
            parent_draft,
            submitter=people["author"],
            cook=people["cook"],
            keeper=people["keeper"],
            accountant=people["accountant"],
            approver=people["approver"],
        )
        before = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())

        replacement = build_complete_draft(
            recipe=child_recipe, unit=kilogram, item=rice, author=people["author"]
        )
        approved = carry_to_approved(
            replacement,
            submitter=people["author"],
            cook=people["cook"],
            keeper=people["keeper"],
            accountant=people["accountant"],
            approver=people["approver"],
        )
        activate_recipe_version(
            version=approved,
            actor=people["approver"],
            effective_from=datetime.date(2026, 6, 1),
            supersedes=RecipeVersion.objects.get(pk=child.pk),
        )
        after = cost_recipe_version(
            version=RecipeVersion.objects.get(pk=parent.pk),
            warehouse=valued_store,
            as_of_date=_today(),
        )
        assert after.plate_cost == before.plate_cost
        assert after.portions_per_batch == before.portions_per_batch

    def test_a_draft_preview_marks_its_plate_cost_non_authoritative(
        self, valued_store: Warehouse, complete_draft: RecipeVersion
    ) -> None:
        card = preview_recipe_cost(
            version=complete_draft, warehouse=valued_store, as_of_date=_today()
        )
        assert card.is_authoritative is False
        assert card.plate is not None
        assert card.plate_cost is not None

    def test_a_draft_without_a_primary_serving_says_why(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        """
        Structured, not blank, and not an invented divisor. The rest of the
        preview stays honest: the total is still there to look at.
        """
        draft = create_draft_recipe_version(
            recipe=recipe,
            batch_size=Decimal("1"),
            expected_output_quantity=Decimal("10"),
            output_unit=kilogram,
            instructions="نظرة عامة.",
            created_by=manager,
        )
        add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("4"), entered_unit=kilogram
        )
        card = preview_recipe_cost(
            version=RecipeVersion.objects.get(pk=draft.pk),
            warehouse=valued_store,
            as_of_date=_today(),
        )
        assert card.plate is None
        assert card.plate_problem is not None
        assert card.plate_problem.code == "recipe_cost_no_primary_serving"
        assert card.total_material_cost == Decimal("6000.000")
        assert card.is_complete is False

    def test_submission_already_refuses_a_version_with_no_primary_serving(
        self, complete_draft: RecipeVersion, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        """
        The invariant §C asks to *verify* rather than assume: an authoritative
        version cannot reach `APPROVED` without a primary serving, so
        `PlateCostUnavailable` is a preview-only state.
        """
        from apps.kitchen.lifecycle import submission_problems

        RecipeServing.objects.filter(version=complete_draft).update(is_primary=False)
        problems = submission_problems(RecipeVersion.objects.get(pk=complete_draft.pk))
        assert problems

    def test_no_snapshot_is_created_without_a_plate_basis(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        draft = create_draft_recipe_version(
            recipe=recipe,
            batch_size=Decimal("1"),
            expected_output_quantity=Decimal("10"),
            output_unit=kilogram,
            instructions="نظرة عامة.",
            created_by=manager,
        )
        add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("4"), entered_unit=kilogram
        )
        card = preview_recipe_cost(
            version=RecipeVersion.objects.get(pk=draft.pk),
            warehouse=valued_store,
            as_of_date=_today(),
        )
        with pytest.raises(ValidationError) as refusal:
            create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="NO-PLATE")
        assert codes_of(refusal.value) & {
            "recipe_cost_version_not_authoritative",
            "recipe_cost_snapshot_requires_plate_basis",
        }
        assert RecipeCostSnapshot.objects.count() == 0


class TestThePlateCostIsFrozenIntoTheSnapshot:
    @pytest.fixture
    def snapshot(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> RecipeCostSnapshot:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        return create_recipe_cost_snapshot(
            card=card, actor=manager, idempotency_key="PLATE-1", reference="R"
        )

    def test_the_snapshot_stores_the_whole_plate_basis(
        self, snapshot: RecipeCostSnapshot, costable_version: RecipeVersion
    ) -> None:
        primary = RecipeServing.objects.get(version=costable_version, is_primary=True)
        assert snapshot.primary_serving_code == primary.code
        assert snapshot.plate_cost > Decimal("0")
        assert snapshot.portions_per_batch > Decimal("0")

    def test_the_explanation_does_not_depend_on_a_mutable_name(
        self, snapshot: RecipeCostSnapshot, costable_version: RecipeVersion
    ) -> None:
        """
        Every figure needed to re-derive the plate cost is a stored column on
        the snapshot or its own serving row: code, names, quantity, unit and
        the frozen factor. Renaming the live serving changes none of them.
        """
        row = snapshot.servings.get(is_primary=True)
        assert row.code == snapshot.primary_serving_code
        assert row.name
        assert row.serving_quantity > Decimal("0")
        assert row.serving_unit_code
        assert row.factor_of_batch > Decimal("0")
        assert snapshot.plate_cost == snapshot.total_material_cost * row.factor_of_batch

    def test_a_later_inventory_movement_does_not_rewrite_the_plate_cost(
        self,
        snapshot: RecipeCostSnapshot,
        organization: Organization,
        valued_store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        from .conftest import post_receipt

        before = snapshot.plate_cost
        post_receipt(
            organization=organization,
            warehouse=valued_store,
            item=rice,
            quantity="100",
            unit_cost="9000",
            key="after-plate-snapshot",
        )
        assert RecipeCostSnapshot.objects.get(pk=snapshot.pk).plate_cost == before

    def test_a_retry_returns_the_same_plate_evidence(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        first = create_recipe_cost_snapshot(
            card=card, actor=manager, idempotency_key="PLATE-RETRY", reference="R"
        )
        again = create_recipe_cost_snapshot(
            card=card, actor=manager, idempotency_key="PLATE-RETRY", reference="R"
        )
        assert again.pk == first.pk
        assert again.plate_cost == first.plate_cost
        assert again.portions_per_batch == first.portions_per_batch
        assert RecipeCostSnapshot.objects.count() == 1


# ---------------------------------------------------------------------------
# The compact allocation
# ---------------------------------------------------------------------------


class TestTheCompactAllocationIsTheCertifiedOne:
    """
    The compact form is a **derivation** of `apps/core/allocation.allocate`,
    not a second opinion about it. These tests are what make that true.
    """

    @pytest.mark.parametrize(
        ("total", "whole", "serving", "leftover"),
        [
            ("6000.000", 7, "1", "0"),
            ("6000.000", 3, "1", "0"),
            ("6000.000", 28, "0.35", "0.2"),
            ("11793.670", 10, "1", "0"),
            ("11793.670", 28, "0.35", "0.2"),
            ("1.000", 3, "1", "0"),
            ("0.007", 3, "1", "0"),
            ("100.500", 6, "0.5", "0.25"),
            ("999.999", 13, "0.077", "0.001"),
        ],
    )
    def test_it_matches_the_allocator_exactly(
        self, total: str, whole: int, serving: str, leftover: str
    ) -> None:
        amount = Decimal(total)
        weight = Decimal(serving)
        spare = Decimal(leftover)

        items = [AllocationItem(sequence=index, weight=weight) for index in range(1, whole + 1)]
        if spare > Decimal("0"):
            items.append(AllocationItem(sequence=whole + 1, weight=spare))
        expected = [result.amount for result in allocate(amount, items)]
        expected_servings = expected[:whole]
        expected_leftover = expected[whole] if spare > Decimal("0") else Decimal("0.000")

        split = _compact_allocation(
            total=amount, whole=whole, serving_quantity=weight, leftover=spare
        )

        low = min(expected_servings)
        high = max(expected_servings)
        # When the residue reached nobody, every serving carries the same
        # amount and the "elevated" count is zero by definition.
        elevated = 0 if high == low else sum(1 for value in expected_servings if value == high)

        assert split.normal_count + split.elevated_count == whole
        assert split.normal_amount == low
        assert split.elevated_count == elevated
        assert split.leftover_amount == expected_leftover
        if elevated:
            assert split.elevated_amount == high
        assert (
            Decimal(split.normal_count) * split.normal_amount
            + Decimal(split.elevated_count) * split.elevated_amount
            + split.leftover_amount
            == amount
        )


class TestServingAllocationHasNoSizeLimit:
    def test_a_small_count_allocates_exactly(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="7",
            servings=[("ONE", "1", True)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        serving = card.servings[0]
        assert serving.whole_count == 7
        assert serving.allocated_total == card.total_material_cost
        assert serving.reconstructs_to() == card.total_material_cost

    def test_fifty_thousand_servings_still_allocate_exactly(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        The case the first pass refused. 50 KG in 1 g portions is 50,000
        servings, and the answer is exact.
        """
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="50",
            servings=[("TINY", "0.001", True)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        serving = card.servings[0]
        assert serving.whole_count == 50_000
        assert serving.state is ServingAllocationState.ALLOCATED
        assert serving.allocated_total == card.total_material_cost
        assert serving.reconstructs_to() == card.total_material_cost
        assert serving.normal_serving_count + serving.elevated_serving_count == 50_000
        assert serving.is_enumerable is False

    def test_the_large_case_builds_no_fifty_thousand_objects(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        Constant work, measured rather than asserted.

        A per-serving list of 50,000 `AllocationItem`s plus 50,000 `Decimal`
        results is megabytes; the analytic form allocates a handful of objects.
        The threshold is deliberately loose — this is testing an order of
        magnitude, not a byte count.
        """
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="50",
            servings=[("TINY", "0.001", True)],
        )
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert peak - before < 2_000_000

    def test_a_large_scenario_is_snapshotted_in_one_row(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
        manager: User,
    ) -> None:
        """Five columns, not fifty thousand rows — and it still reconstructs."""
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="50",
            servings=[("TINY", "0.001", True)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        snapshot = create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="BIG-1")
        assert snapshot.servings.count() == 1
        row = snapshot.servings.get()
        assert row.whole_serving_count == 50_000
        assert row.normal_serving_count + row.elevated_serving_count == 50_000
        assert row.reconstructs_to() == snapshot.total_material_cost

    def test_a_serving_bigger_than_the_batch_is_a_state_not_a_refusal(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """A 20 KG platter from a 10 KG batch. Real, and the total is intact."""
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="10",
            servings=[("ONE", "1", True), ("PLATTER", "20", False)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        platter = next(row for row in card.servings if row.serving.code == "PLATTER")
        assert platter.whole_count == 0
        assert platter.state is ServingAllocationState.NO_WHOLE_SERVING
        assert platter.remainder_cost == card.total_material_cost
        assert platter.allocated_total == card.total_material_cost

    def test_the_enumeration_limit_decides_no_calculation(self) -> None:
        """
        `MAX_ENUMERATED_SERVINGS` is a screen's limit and nothing else. The
        name is checked too: the old `MAX_ALLOCATED_SERVINGS` said the opposite.
        """
        from apps.kitchen import costing

        assert not hasattr(costing, "MAX_ALLOCATED_SERVINGS")
        assert MAX_ENUMERATED_SERVINGS > 0

    def test_the_distribution_is_stable_across_runs(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="7",
            servings=[("ONE", "1", True)],
        )
        first = cost_recipe_version(
            version=version, warehouse=valued_store, as_of_date=_today()
        ).servings[0]
        second = cost_recipe_version(
            version=version, warehouse=valued_store, as_of_date=_today()
        ).servings[0]
        assert (
            first.normal_cost_per_serving,
            first.normal_serving_count,
            first.elevated_cost_per_serving,
            first.elevated_serving_count,
            first.remainder_cost,
        ) == (
            second.normal_cost_per_serving,
            second.normal_serving_count,
            second.elevated_cost_per_serving,
            second.elevated_serving_count,
            second.remainder_cost,
        )

    def test_leftover_output_keeps_an_exact_cost(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="10",
            servings=[("SMALL", "0.35", True)],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        serving = card.servings[0]
        assert serving.whole_count == 28
        assert serving.remainder_quantity == Decimal("0.200000")
        assert serving.remainder_cost > Decimal("0")
        assert serving.reconstructs_to() == card.total_material_cost

    def test_every_scenario_allocates_the_whole_total(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """Whole, half, 0.350 KG and 0.500 KG — all rows, no service branches."""
        version = _version(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            people=people,
            output="10",
            servings=[
                ("FULL", "1", True),
                ("HALF", "0.5", False),
                ("SMALL", "0.35", False),
                ("PIECE", "0.5", False),
            ],
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert len(card.servings) == 4
        for serving in card.servings:
            assert serving.allocated_total == card.total_material_cost
            assert serving.reconstructs_to() == card.total_material_cost


class TestTheVerifierSeesAPlantedContradiction:
    @pytest.fixture
    def snapshot(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> RecipeCostSnapshot:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        return create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="VERIFY-PLATE")

    @pytest.mark.django_db(transaction=True)
    def test_a_broken_allocation_summary_is_reported_and_not_repaired(
        self, snapshot: RecipeCostSnapshot
    ) -> None:
        """
        Move one serving from the normal count to the elevated one. The counts
        still add up, so the constraint holds — and the arithmetic no longer
        does, which is exactly what the reconstruction check is for.
        """
        from apps.kitchen.cost_reconciliation import snapshot_findings

        from .test_cost_snapshots import _plant

        row = snapshot.servings.filter(elevated_serving_count=0).first()
        assert row is not None
        _plant(
            "UPDATE kitchen_recipecostsnapshotserving "
            "SET normal_serving_count = normal_serving_count - 1, "
            "    elevated_serving_count = elevated_serving_count + 1, "
            "    maximum_allocated = minimum_allocated + 0.001 "
            "WHERE id = %s",
            [row.pk],
            table="kitchen_recipecostsnapshotserving",
        )
        refreshed = RecipeCostSnapshot.objects.get(pk=snapshot.pk)
        codes = {finding.code for finding in snapshot_findings(refreshed)}
        assert "cost_snapshot_serving_summary_does_not_reconstruct" in codes
        # Reported, never repaired.
        again = RecipeCostSnapshot.objects.get(pk=snapshot.pk).servings.get(pk=row.pk)
        assert again.elevated_serving_count == 1

    @pytest.mark.django_db(transaction=True)
    def test_a_broken_plate_cost_is_reported(self, snapshot: RecipeCostSnapshot) -> None:
        from apps.kitchen.cost_reconciliation import snapshot_findings

        from .test_cost_snapshots import _plant

        _plant(
            "UPDATE kitchen_recipecostsnapshot SET plate_cost = plate_cost + 1 WHERE id = %s",
            [snapshot.pk],
            table="kitchen_recipecostsnapshot",
        )
        codes = {
            finding.code
            for finding in snapshot_findings(RecipeCostSnapshot.objects.get(pk=snapshot.pk))
        }
        assert "cost_snapshot_plate_cost_disagrees_with_its_basis" in codes
