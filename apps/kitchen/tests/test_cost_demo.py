"""
The costing half of the kitchen demo seed.

A demo exists so somebody can *look* at the feature, and the ten things §U asks
this one to prove are the ten things a reader would otherwise have to take on
trust: a direct card, a nested roll-up, a stocked leaf that is not expanded, a
two-level cumulative multiplier, a parent still costed from its superseded
child, the food/packaging split, a serving cost, an immutable snapshot, a
preview marked non-authoritative, and a missing-valuation card that cannot be
snapshotted.

Two properties are tested rather than assumed, because both are what demo seeds
most often get wrong:

* **Idempotent.** The second run adds no recipe, no version, no component, no
  snapshot, no snapshot line and no snapshot serving.
* **Zero-effect.** The whole seed creates no stock movement, no balance change
  and no journal entry. Cost snapshots are the only new business records.

The seed also never builds anything invalid: no cycle, no over-deep graph, and
no snapshot from an incomplete card.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from apps.accounting.models import JournalEntry, JournalLine
from apps.inventory.models import (
    StockBalance,
    StockMovement,
    Warehouse,
)
from apps.kitchen.costing import cost_recipe_version, preview_recipe_cost
from apps.kitchen.demo import (
    DEMO_COST_CODE,
    DEMO_DISH_CODE,
    DEMO_MARINADE_CODE,
    DEMO_SPICE_CODE,
    seed_demo_recipes,
)
from apps.kitchen.models import (
    Recipe,
    RecipeComponent,
    RecipeCostSnapshot,
    RecipeCostSnapshotLine,
    RecipeCostSnapshotServing,
    RecipeLineCostClass,
    RecipeVersion,
    RecipeVersionStatus,
)
from apps.organizations.models import Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


def _today() -> datetime.date:
    from django.utils import timezone

    return timezone.localdate()


def _demo_counts() -> tuple[int, ...]:
    return (
        Recipe.objects.count(),
        RecipeVersion.objects.count(),
        RecipeComponent.objects.count(),
        RecipeCostSnapshot.objects.count(),
        RecipeCostSnapshotLine.objects.count(),
        RecipeCostSnapshotServing.objects.count(),
    )


def _ledger_counts() -> tuple[int, ...]:
    return (
        StockMovement.objects.count(),
        StockBalance.objects.count(),
        JournalEntry.objects.count(),
        JournalLine.objects.count(),
    )


# `demo_items` and `demo_store` moved to `conftest.py` when Task 3.4's demo
# tests needed the same two: one definition, so a change to the item list cannot
# leave the costing scenario and the production scenario building on different
# ground.


@pytest.fixture
def seeded(
    organization: Organization,
    demo_store: Warehouse,
    manager: User,
) -> list[Recipe]:
    return seed_demo_recipes(organization=organization, created_by=manager)


class TestTheCostingScenario:
    def test_the_costing_recipe_is_created_and_active(self, seeded: list[Recipe]) -> None:
        recipe = Recipe.objects.get(code=DEMO_COST_CODE)
        assert recipe.versions.filter(status=RecipeVersionStatus.ACTIVE).exists()

    def test_its_card_shows_a_direct_line_and_a_nested_roll_up(
        self, seeded: list[Recipe], demo_store: Warehouse
    ) -> None:
        """Proofs 1, 2 and 4: a direct leaf, a one-level roll-up and a two-level one."""
        version = Recipe.objects.get(code=DEMO_COST_CODE).versions.get(
            status=RecipeVersionStatus.ACTIVE
        )
        card = cost_recipe_version(version=version, warehouse=demo_store, as_of_date=_today())
        paths = [line.path_display for line in card.lines]
        assert "" in paths
        assert any(path.count(".") == 0 and path for path in paths)
        assert any(path.count(".") == 1 for path in paths)

    def test_the_two_level_cumulative_multiplier_is_the_product(
        self, seeded: list[Recipe], demo_store: Warehouse
    ) -> None:
        """
        Proof 4 exactly: the existing 0.5 x 0.30 path.

        The dish takes the marinade at 0.5 and the marinade takes its spice
        blend at 0.30, so the deepest leaf carries 0.15 — full precision, and
        not a rounded 0.2.
        """
        version = Recipe.objects.get(code=DEMO_COST_CODE).versions.get(
            status=RecipeVersionStatus.ACTIVE
        )
        card = cost_recipe_version(version=version, warehouse=demo_store, as_of_date=_today())
        deepest = max(card.lines, key=lambda line: len(line.path))
        assert deepest.cumulative_multiplier == Decimal("0.150")

    def test_the_food_and_packaging_totals_are_both_real(
        self, seeded: list[Recipe], demo_store: Warehouse
    ) -> None:
        """Proof 6: `KM-RCP-004`'s own split, with a number on both sides."""
        version = Recipe.objects.get(code=DEMO_COST_CODE).versions.get(
            status=RecipeVersionStatus.ACTIVE
        )
        card = cost_recipe_version(version=version, warehouse=demo_store, as_of_date=_today())
        assert card.food_total > Decimal("0")
        assert card.packaging_total > Decimal("0")
        assert (
            card.food_total + card.packaging_total + card.accompaniment_total
            == card.total_material_cost
        )
        assert {line.cost_class for line in card.lines} >= {
            RecipeLineCostClass.FOOD,
            RecipeLineCostClass.PACKAGING,
        }

    def test_the_serving_costs_are_present_and_each_allocates_the_total(
        self, seeded: list[Recipe], demo_store: Warehouse
    ) -> None:
        """
        Proof 7, four ways: four alternative portionings of one output.

        The fourth makes 10,000 servings, which is the case the first pass
        refused to allocate. It reconstructs the exact total like the others.
        """
        version = Recipe.objects.get(code=DEMO_COST_CODE).versions.get(
            status=RecipeVersionStatus.ACTIVE
        )
        card = cost_recipe_version(version=version, warehouse=demo_store, as_of_date=_today())
        assert len(card.servings) == 4
        for serving in card.servings:
            assert serving.cost_per_serving >= Decimal("0")
            assert serving.allocated_total == card.total_material_cost
            assert serving.reconstructs_to() == card.total_material_cost
        big = next(row for row in card.servings if row.serving.code == "TINY")
        assert big.whole_count == 10_000
        assert big.is_enumerable is False

    def test_the_demo_card_carries_a_plate_cost(
        self, seeded: list[Recipe], demo_store: Warehouse
    ) -> None:
        """The primary serving, its portions-per-batch, and the plate cost."""
        version = Recipe.objects.get(code=DEMO_COST_CODE).versions.get(
            status=RecipeVersionStatus.ACTIVE
        )
        card = cost_recipe_version(version=version, warehouse=demo_store, as_of_date=_today())
        assert card.plate is not None
        assert card.primary_serving is not None
        assert card.primary_serving.code == "FULL"
        assert card.portions_per_batch == Decimal("10.000000")
        primary_scenario = card.primary_serving_cost()
        assert primary_scenario is not None
        assert card.plate_cost == primary_scenario.cost_per_serving

    def test_a_draft_version_is_left_for_the_preview_banner(
        self, seeded: list[Recipe], demo_store: Warehouse
    ) -> None:
        """Proof 9: a preview that says in as many words that it is not approved."""
        recipe = Recipe.objects.get(code=DEMO_COST_CODE)
        draft = recipe.versions.get(status=RecipeVersionStatus.DRAFT)
        card = preview_recipe_cost(version=draft, warehouse=demo_store, as_of_date=_today())
        assert card.is_authoritative is False

    def test_one_authoritative_snapshot_is_written(self, seeded: list[Recipe]) -> None:
        """Proof 8, through the real service, with real evidence on it."""
        snapshot = RecipeCostSnapshot.objects.get()
        assert snapshot.recipe_code == DEMO_COST_CODE
        assert snapshot.is_authoritative is True
        assert snapshot.ledger_cutoff_sequence > 0
        assert snapshot.lines.exists()
        assert snapshot.servings.count() == 4
        assert (
            sum(line.allocated_extension for line in snapshot.lines.all())
            == snapshot.total_material_cost
        )
        # The plate-cost evidence, frozen with it.
        assert snapshot.primary_serving_code == "FULL"
        assert snapshot.plate_cost > Decimal("0")
        assert snapshot.portions_per_batch == Decimal("10.000000")
        # Ten thousand servings, one row, and it still reconstructs.
        big = snapshot.servings.get(code="TINY")
        assert big.whole_serving_count == 10_000
        assert big.reconstructs_to() == snapshot.total_material_cost

    def test_the_snapshot_says_it_is_a_demo_and_not_a_real_decision(
        self, seeded: list[Recipe]
    ) -> None:
        """
        A demo snapshot presented as a signed Khan Mandi costing card is exactly
        how unapproved figures acquire authority (RCP-126).
        """
        snapshot = RecipeCostSnapshot.objects.get()
        assert "تجريبي" in snapshot.reason
        assert snapshot.reference == "DEMO-NOT-A-REAL-DECISION"


class TestTheStockedLeafAndTheMissingValuation:
    def test_the_stocked_semi_finished_line_is_one_leaf_and_is_not_expanded(
        self, seeded: list[Recipe], demo_store: Warehouse
    ) -> None:
        """
        Proof 3, and proof 10 in the same card.

        `DEMO-RICE-COOKED` is the output item of `DEMO-RCP-RICE`, so it appears
        on the dish as **one** ordinary line and the recipe that produces it is
        never walked. That is visible precisely because the card shows one row
        for it rather than the rice, oil and container rows that recipe holds —
        and because the demo ledger holds none of that semi-finished item, the
        same row is also the missing-valuation case.
        """
        version = (
            Recipe.objects.get(code=DEMO_DISH_CODE)
            .versions.filter(status=RecipeVersionStatus.ACTIVE)
            .first()
        )
        assert version is not None
        card = cost_recipe_version(version=version, warehouse=demo_store, as_of_date=_today())
        cooked = [line for line in card.lines if line.item.code == "DEMO-RICE-COOKED"]
        assert len(cooked) == 1
        assert cooked[0].path_display == ""
        # The producing recipe's own lines are absent: no expansion happened.
        assert not any(line.source_recipe.code == "DEMO-RCP-RICE" for line in card.lines)

    def test_that_card_cannot_be_snapshotted(
        self, seeded: list[Recipe], demo_store: Warehouse, manager: User
    ) -> None:
        """Proof 10: a hole is reported, and no record may be built over it."""
        from django.core.exceptions import ValidationError

        from apps.kitchen.snapshots import create_recipe_cost_snapshot

        version = (
            Recipe.objects.get(code=DEMO_DISH_CODE)
            .versions.filter(status=RecipeVersionStatus.ACTIVE)
            .first()
        )
        assert version is not None
        card = cost_recipe_version(version=version, warehouse=demo_store, as_of_date=_today())
        assert not card.is_complete
        assert card.missing[0].item_code == "DEMO-RICE-COOKED"
        with pytest.raises(ValidationError):
            create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="DEMO-HOLE")

    def test_the_superseded_child_is_still_costed_from_the_parents_exact_link(
        self, seeded: list[Recipe], demo_store: Warehouse
    ) -> None:
        """
        Proof 5: the historical dish keeps naming the historical marinade.

        The demo supersedes marinade v1 with v2, and the first dish version was
        written against v1. Its card still expands v1, at v1's multipliers.
        """
        marinade_v1 = RecipeVersion.objects.get(recipe__code=DEMO_MARINADE_CODE, version_number=1)
        assert marinade_v1.status == RecipeVersionStatus.SUPERSEDED

        dish_v1 = RecipeVersion.objects.get(recipe__code=DEMO_DISH_CODE, version_number=1)
        card = cost_recipe_version(version=dish_v1, warehouse=demo_store, as_of_date=_today())
        component_sources = {line.source_version.pk for line in card.lines if line.path_display}
        assert marinade_v1.pk in component_sources
        spice_v1 = RecipeVersion.objects.get(recipe__code=DEMO_SPICE_CODE, version_number=1)
        assert spice_v1.pk in component_sources


class TestTheSeedIsIdempotentAndInert:
    def test_a_second_run_adds_nothing(
        self, seeded: list[Recipe], organization: Organization, manager: User
    ) -> None:
        before = _demo_counts()
        seed_demo_recipes(organization=organization, created_by=manager)
        assert _demo_counts() == before

    def test_the_seed_creates_no_stock_movement_and_no_journal(
        self,
        demo_store: Warehouse,
        organization: Organization,
        manager: User,
    ) -> None:
        """
        The fixture's own receipts are posted *before* this count is taken, so
        what is measured is the kitchen seed alone.
        """
        before = _ledger_counts()
        seed_demo_recipes(organization=organization, created_by=manager)
        assert _ledger_counts() == before

    def test_a_second_run_creates_no_second_snapshot(
        self, seeded: list[Recipe], organization: Organization, manager: User
    ) -> None:
        """
        The key carries the as-of date, so a same-day re-run replays through
        idempotency rather than colliding. A key with no date in it would raise
        `idempotency_key_conflict` the next morning — a real bug dressed as a
        safety feature.
        """
        seed_demo_recipes(organization=organization, created_by=manager)
        assert RecipeCostSnapshot.objects.count() == 1

    def test_nothing_invalid_is_ever_seeded(self, seeded: list[Recipe]) -> None:
        """No cycle, no over-deep graph, and no snapshot over a hole."""
        from apps.kitchen.graph import component_paths
        from apps.kitchen.models import MAX_COMPONENT_DEPTH

        for version in RecipeVersion.objects.all():
            for path in component_paths(version):
                assert path.count("←") <= MAX_COMPONENT_DEPTH
        for snapshot in RecipeCostSnapshot.objects.all():
            assert snapshot.lines.exists()
            assert snapshot.total_material_cost > Decimal("0")

    def test_the_verifier_is_clean_on_the_seeded_data(
        self, seeded: list[Recipe], organization: Organization
    ) -> None:
        from apps.kitchen.cost_reconciliation import verify_cost_snapshots

        assert verify_cost_snapshots(organization) == []
