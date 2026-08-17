"""
Serving costs: a rate and an allocation, and why they are not the same number.

RCP-086 asks "what does one cost" and answers with a **rate** — the total times
the serving's share of the output basis, quantized once to six places because a
unit cost is not a posted amount. RCP-087 asks "divide this exact total among
the servings it makes" and answers with an **allocation**, whose parts sum to
the total to the fils because the residue is distributed rather than lost.

Conflating them is how allocation bugs are born, so both are computed and both
are on the card.

Every quantity below is a **row**, never a constant in a service (RCP-082).
`test_no_dish_or_gram_figure_is_hard_coded_in_the_app` in `test_draft_structure`
is the convention test that holds that line across the whole app; this file
exercises the data side of it — a factor of 1.000, a factor of 0.500, and two
gram-weight portions — and nothing in `apps/kitchen/costing.py` knows any of
them.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.costing import ServingAllocationState, cost_recipe_version
from apps.kitchen.models import Recipe, RecipeVersion
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    add_recipe_step,
    create_draft_recipe_version,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import carry_to_active

pytestmark = pytest.mark.django_db


def _today() -> datetime.date:
    from django.utils import timezone

    return timezone.localdate()


def _version_with_servings(
    *,
    recipe: Recipe,
    kilogram: UnitOfMeasure,
    rice: InventoryItem,
    author: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
    output: str,
    servings: list[tuple[str, str, bool]],
) -> RecipeVersion:
    """
    One rice line, one step, and whatever servings the caller asked for.

    4 KG of rice at 1,500 is 6,000 — a round total on purpose, so a test that
    wants an *awkward* division can choose it deliberately rather than inherit
    one.
    """
    draft = create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal(output),
        output_unit=kilogram,
        instructions="نظرة عامة.",
        created_by=author,
    )
    add_recipe_line(version=draft, item=rice, entered_quantity=Decimal("4"), entered_unit=kilogram)
    add_recipe_step(version=draft, instruction_ar="خطوة.")
    for code, quantity, primary in servings:
        add_recipe_serving(
            version=draft,
            code=code,
            name_ar=f"حصة {code}",
            serving_quantity=Decimal(quantity),
            serving_unit=kilogram,
            is_primary=primary,
        )
    return carry_to_active(
        RecipeVersion.objects.get(pk=draft.pk),
        submitter=author,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
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


class TestTheServingRate:
    def test_a_whole_serving_costs_the_whole_batch(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        Factor 1.000 through data: the serving *is* the output.

        Nothing in the service knows that a factor of one is special; it falls
        out of `base_quantity / expected_output` like every other factor.
        """
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="1",
            servings=[("FULL", "1", True)],
            **people,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        serving = card.servings[0]
        assert serving.factor_of_batch == Decimal("1.000000000000")
        assert serving.cost_per_serving == card.total_material_cost
        assert serving.whole_count == 1

    def test_a_half_serving_costs_half(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """Factor 0.500 through data. 6,000 over two halves is 3,000 each."""
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="1",
            servings=[("FULL", "1", True), ("HALF", "0.5", False)],
            **people,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        half = next(row for row in card.servings if row.serving.code == "HALF")
        assert half.factor_of_batch == Decimal("0.500000000000")
        assert half.cost_per_serving == Decimal("3000.000000")
        assert half.whole_count == 2

    def test_a_gram_portion_costs_its_share(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        0.350 KG of a 10 KG output. 6,000 x 0.035 = 210 exactly.

        The 0.350 lives in the `add_recipe_serving` call — a row — and the
        engine multiplies whatever it finds there.
        """
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="10",
            servings=[("SMALL", "0.35", True)],
            **people,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        serving = card.servings[0]
        assert serving.factor_of_batch == Decimal("0.035000000000")
        assert serving.cost_per_serving == Decimal("210.000000")

    def test_a_half_kilo_portion_costs_its_share(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """0.500 KG of a 10 KG output: 6,000 x 0.05 = 300."""
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="10",
            servings=[("PIECE", "0.5", True)],
            **people,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert card.servings[0].cost_per_serving == Decimal("300.000000")

    def test_the_output_unit_cost_is_a_rate_at_six_places(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """6,000 over 7 KG does not divide, and the rate keeps six places."""
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="7",
            servings=[("ONE", "1", True)],
            **people,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert card.cost_per_output_unit == Decimal("857.142857")


class TestTheServingAllocation:
    def test_each_scenario_allocates_the_whole_total_exactly(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        Two ways of portioning one output are **alternatives**, not additions.

        Each scenario divides the *whole* total, so adding two of them together
        would double the recipe. Both sum to 6,000 and neither is half of it.
        """
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="10",
            servings=[("FULL", "1", True), ("HALF", "0.5", False)],
            **people,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert len(card.servings) == 2
        for serving in card.servings:
            assert serving.state is ServingAllocationState.ALLOCATED
            assert serving.allocated_total == card.total_material_cost

    def test_an_awkward_division_distributes_the_remainder_by_a_fils(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        6,000 across three servings is 2,000 each — so make it awkward instead.

        A 7 KG output in 1 KG servings is seven whole servings and nothing left
        over, and 6,000 / 7 does not divide. The allocator gives some servings
        one extra fils rather than losing the residue, and the difference
        between the largest and smallest share is exactly that fils.
        """
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="7",
            servings=[("ONE", "1", True)],
            **people,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        serving = card.servings[0]
        assert serving.whole_count == 7
        assert serving.remainder_quantity == Decimal("0.000000")
        assert serving.allocated_total == card.total_material_cost
        # The residue reached some servings and not others, and the difference
        # between the two amounts is exactly the fils it hands out.
        assert serving.elevated_cost_per_serving - serving.normal_cost_per_serving == Decimal(
            "0.001"
        )
        assert serving.elevated_serving_count > 0
        assert serving.reconstructs_to() == card.total_material_cost

    def test_leftover_output_carries_cost_rather_than_vanishing(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        10 KG in 0.35 KG portions is 28 portions and 0.2 KG left over.

        That leftover is output the batch paid for. Dropping it would make the
        scenario sum to less than the recipe; inflating the 28 portions to
        absorb it would overstate what one portion cost. It gets a weight of
        its own, and the scenario still sums to the total.
        """
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="10",
            servings=[("SMALL", "0.35", True)],
            **people,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        serving = card.servings[0]
        assert serving.whole_count == 28
        assert serving.remainder_quantity == Decimal("0.200000")
        assert serving.remainder_cost > Decimal("0")
        assert serving.allocated_total == card.total_material_cost

    def test_the_remainder_distribution_is_stable_across_runs(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        Remainder DESC then sequence ASC, so two runs over the same economic
        input can never differ. A card whose figures moved between two loads is
        one nobody can reconcile.
        """
        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="7",
            servings=[("ONE", "1", True)],
            **people,
        )
        first = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        second = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert (
            first.servings[0].normal_cost_per_serving == second.servings[0].normal_cost_per_serving
        )
        assert first.servings[0].elevated_serving_count == second.servings[0].elevated_serving_count
        assert first.servings[0].remainder_cost == second.servings[0].remainder_cost

    def test_the_rounding_policy_never_moves_money(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        RCP-085: `rounding_increment` and `rounding_policy` govern planning
        counts and **never** touch cost.

        Rounding 28.57 portions down to 28 for planning is sensible; letting
        that rounding move cost would make the sum of the serving costs
        disagree with the batch, which RCP-087 forbids outright.
        """
        from apps.kitchen.models import ServingRoundingPolicy
        from apps.kitchen.services import update_recipe_serving

        version = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="10",
            servings=[("SMALL", "0.35", True)],
            **people,
        )
        before = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())

        # A second recipe version cannot be edited, so the policy is exercised
        # on a fresh draft of the same shape.
        draft = create_draft_recipe_version(
            recipe=recipe,
            batch_size=Decimal("1"),
            expected_output_quantity=Decimal("10"),
            output_unit=kilogram,
            instructions="نظرة عامة.",
            created_by=people["author"],
        )
        add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("4"), entered_unit=kilogram
        )
        add_recipe_step(version=draft, instruction_ar="خطوة.")
        serving = add_recipe_serving(
            version=draft,
            code="SMALL",
            name_ar="حصة صغيرة",
            serving_quantity=Decimal("0.35"),
            serving_unit=kilogram,
            is_primary=True,
        )
        update_recipe_serving(
            serving=serving,
            name_ar="حصة صغيرة",
            serving_quantity=Decimal("0.35"),
            serving_unit=kilogram,
            rounding_increment=Decimal("5"),
            rounding_policy=ServingRoundingPolicy.DOWN,
            is_primary=True,
        )
        from apps.kitchen.costing import preview_recipe_cost

        after = preview_recipe_cost(
            version=RecipeVersion.objects.get(pk=draft.pk),
            warehouse=valued_store,
            as_of_date=_today(),
        )
        assert after.servings[0].cost_per_serving == before.servings[0].cost_per_serving
        assert after.servings[0].allocated_total == after.total_material_cost

    def test_two_recipes_stay_independent(
        self,
        valued_store: Warehouse,
        organization: object,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        people: dict[str, User],
    ) -> None:
        """
        RCP-124: a half-chicken card is its own recipe with its own rice.

        A physical factor of 0.500 divides one output; it does not derive a
        second commercial recipe. Two recipes are costed separately and neither
        one's serving factor reaches the other.
        """
        from .conftest import make_child_recipe

        first = _version_with_servings(
            recipe=recipe,
            kilogram=kilogram,
            rice=rice,
            output="10",
            servings=[("FULL", "1", True)],
            **people,
        )
        other_recipe = make_child_recipe(
            organization=organization,  # type: ignore[arg-type]
            code="OTHER-1",
            author=people["author"],
        )
        second = _version_with_servings(
            recipe=other_recipe,
            kilogram=kilogram,
            rice=rice,
            output="5",
            servings=[("FULL", "1", True)],
            **people,
        )
        first_card = cost_recipe_version(version=first, warehouse=valued_store, as_of_date=_today())
        second_card = cost_recipe_version(
            version=second, warehouse=valued_store, as_of_date=_today()
        )
        # Same lines, same total; different output, so different unit costs.
        assert first_card.total_material_cost == second_card.total_material_cost
        assert first_card.cost_per_output_unit != second_card.cost_per_output_unit
