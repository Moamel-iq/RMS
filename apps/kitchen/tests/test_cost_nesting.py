"""
Costing a nested recipe: exact children, cumulative multipliers, and the two
ways of double-counting that are refused by construction.

The claims here:

* **Multipliers compose, and nothing rounds on the way down** (RCP-073). A
  quarter of a half is an eighth, not "0.13".
* **The child is followed through its exact frozen foreign key** (RCP-072).
  Superseding it changes nothing, and approving a newer one changes nothing
  either. Both are tested, because the first is what everybody expects and the
  second is what actually goes wrong.
* **A stocked sub-recipe stays one leaf** (RCP-071). Its book value already
  contains its ingredients; expanding them too would charge the parent twice.
  The paired test is the point: the same shape non-stocked *does* expand.
* **The same item on two paths is two rows**, priced identically and summed
  exactly (§J).
* **Order is the component path, then the leaf line order, then the item
  code** — never primary-key order, so two databases restored differently
  produce the same card.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.costing import CostLineKind, cost_recipe_version
from apps.kitchen.lifecycle import activate_recipe_version
from apps.kitchen.models import Recipe, RecipeVersion, RecipeVersionStatus
from apps.kitchen.services import add_recipe_line, create_recipe_component
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import (
    build_complete_draft,
    carry_to_active,
    carry_to_approved,
    make_child_recipe,
)

pytestmark = pytest.mark.django_db

FIRST = datetime.date(2026, 1, 1)
SECOND = datetime.date(2026, 6, 1)


def _today() -> datetime.date:
    from django.utils import timezone

    return timezone.localdate()


class Chain:
    """
    Builds bottom-up chains of non-stocked recipes, each with one rice line.

    Bottom-up because a draft can never be anybody's child: a component must
    name a frozen, approved version, so the leaf has to reach `ACTIVE` before
    the level above it can even be drafted.
    """

    def __init__(
        self,
        *,
        organization: Organization,
        unit: UnitOfMeasure,
        item: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        self.organization = organization
        self.unit = unit
        self.item = item
        self.manager = manager
        self.cook = cook
        self.keeper = keeper
        self.accountant = accountant
        self.approver = approver
        self.recipes: dict[str, Recipe] = {}

    def recipe_for(self, code: str) -> Recipe:
        if code not in self.recipes:
            self.recipes[code] = make_child_recipe(
                organization=self.organization, code=code, author=self.manager
            )
        return self.recipes[code]

    def draft(self, code: str) -> RecipeVersion:
        return build_complete_draft(
            recipe=self.recipe_for(code), unit=self.unit, item=self.item, author=self.manager
        )

    def activate(
        self,
        version: RecipeVersion,
        *,
        effective_from: datetime.date = FIRST,
        effective_to: datetime.date | None = None,
    ) -> RecipeVersion:
        return carry_to_active(
            version,
            submitter=self.manager,
            cook=self.cook,
            keeper=self.keeper,
            accountant=self.accountant,
            approver=self.approver,
            effective_from=effective_from,
            effective_to=effective_to,
        )

    def leaf(self, code: str) -> RecipeVersion:
        return self.activate(self.draft(code))

    def above(self, code: str, *, child: RecipeVersion, multiplier: Decimal) -> RecipeVersion:
        draft = self.draft(code)
        create_recipe_component(version=draft, component_version=child, multiplier=multiplier)
        return self.activate(draft)


@pytest.fixture
def chain(
    organization: Organization,
    kilogram: UnitOfMeasure,
    rice: InventoryItem,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> Chain:
    return Chain(
        organization=organization,
        unit=kilogram,
        item=rice,
        manager=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )


class TestNestedRollUp:
    """`build_complete_draft` puts 4 KG of rice on every level, at 1,500 each."""

    def test_one_level_multiplies_the_child_quantity(
        self, valued_store: Warehouse, chain: Chain
    ) -> None:
        """Parent 4 KG + (child 4 KG x 0.5) = 6 KG at 1,500 = 9,000."""
        child = chain.leaf("BLEND-A")
        parent = chain.above("DISH-A", child=child, multiplier=Decimal("0.5"))
        card = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())
        assert len(card.lines) == 2
        assert [line.effective_quantity for line in card.lines] == [
            Decimal("4.000000"),
            Decimal("2.000000"),
        ]
        assert card.total_material_cost == Decimal("9000.000")

    def test_two_levels_multiply_through(self, valued_store: Warehouse, chain: Chain) -> None:
        """
        4 + (4 x 0.5) + (4 x 0.5 x 0.25) = 4 + 2 + 0.5 = 6.5 KG -> 9,750.

        The cumulative factor on the deepest leaf is 0.125 exactly, not a
        rounded 0.13: nothing quantizes on the way down.
        """
        leaf = chain.leaf("SPICE-A")
        middle = chain.above("BLEND-B", child=leaf, multiplier=Decimal("0.25"))
        parent = chain.above("DISH-B", child=middle, multiplier=Decimal("0.5"))
        card = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())
        assert len(card.lines) == 3
        deepest = card.lines[-1]
        assert deepest.cumulative_multiplier == Decimal("0.125")
        assert deepest.effective_quantity == Decimal("0.500000")
        assert card.total_material_cost == Decimal("9750.000")

    def test_the_same_item_on_two_paths_stays_two_rows(
        self, valued_store: Warehouse, chain: Chain
    ) -> None:
        """
        Every level here uses rice, so one card holds the same item three times.

        Collapsing them would hide which level to fix, and the unit cost is the
        same on all three because it was fetched once for the whole card.
        """
        leaf = chain.leaf("SPICE-C")
        middle = chain.above("BLEND-C", child=leaf, multiplier=Decimal("0.25"))
        parent = chain.above("DISH-C", child=middle, multiplier=Decimal("0.5"))
        card = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())
        assert [line.item.code for line in card.lines] == ["RICE", "RICE", "RICE"]
        assert len({line.unit_cost for line in card.lines}) == 1
        assert len({line.path_display for line in card.lines}) == 3
        assert sum(line.allocated_extension for line in card.lines) == (card.total_material_cost)

    def test_the_order_is_path_then_line_then_item(
        self, valued_store: Warehouse, chain: Chain
    ) -> None:
        """The version's own lines lead; each component's subtree follows in order."""
        leaf = chain.leaf("SPICE-D")
        middle = chain.above("BLEND-D", child=leaf, multiplier=Decimal("0.25"))
        parent = chain.above("DISH-D", child=middle, multiplier=Decimal("0.5"))
        card = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())
        assert [line.path_display for line in card.lines] == ["", "1", "1.1"]
        assert [line.kind for line in card.lines] == [
            CostLineKind.DIRECT,
            CostLineKind.COMPONENT,
            CostLineKind.COMPONENT,
        ]
        assert [line.line_number for line in card.lines] == [1, 2, 3]

    def test_the_line_carries_its_source_version_identity(
        self, valued_store: Warehouse, chain: Chain
    ) -> None:
        child = chain.leaf("BLEND-E")
        parent = chain.above("DISH-E", child=child, multiplier=Decimal("0.5"))
        card = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())
        component_line = card.lines[1]
        assert component_line.source_version.pk == child.pk
        assert component_line.source_recipe.code == "BLEND-E"


class TestTheExactChildSurvives:
    def test_superseding_the_child_does_not_change_the_parents_cost(
        self,
        valued_store: Warehouse,
        chain: Chain,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        RCP-072 and spec §26.4: `component_version` is a frozen foreign key.

        A blend replaced in September does not restate what the July dish
        claimed to contain, and it does not restate what the July dish cost.
        """
        child = chain.leaf("BLEND-F")
        parent = chain.above("DISH-F", child=child, multiplier=Decimal("0.5"))
        before = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())

        replacement = chain.draft("BLEND-F")
        approved = carry_to_approved(
            replacement,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=approved,
            actor=approver,
            effective_from=SECOND,
            supersedes=RecipeVersion.objects.get(pk=child.pk),
        )
        assert RecipeVersion.objects.get(pk=child.pk).status == RecipeVersionStatus.SUPERSEDED

        after = cost_recipe_version(
            version=RecipeVersion.objects.get(pk=parent.pk),
            warehouse=valued_store,
            as_of_date=_today(),
        )
        assert after.total_material_cost == before.total_material_cost
        assert [line.source_version.pk for line in after.lines] == [
            line.source_version.pk for line in before.lines
        ]
        assert after.lines[1].source_version.pk == child.pk

    def test_a_newer_child_version_is_never_selected_automatically(
        self,
        valued_store: Warehouse,
        chain: Chain,
        rice: InventoryItem,
        kilogram: UnitOfMeasure,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        The replacement carries **twice** the rice, so a card that had silently
        re-pointed would be visibly larger. It is not.
        """
        child = chain.leaf("BLEND-G")
        parent = chain.above("DISH-G", child=child, multiplier=Decimal("0.5"))
        before = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())

        replacement = chain.draft("BLEND-G")
        add_recipe_line(
            version=replacement,
            item=rice,
            entered_quantity=Decimal("4"),
            entered_unit=kilogram,
        )
        approved = carry_to_approved(
            replacement,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=approved,
            actor=approver,
            effective_from=SECOND,
            supersedes=RecipeVersion.objects.get(pk=child.pk),
        )

        after = cost_recipe_version(
            version=RecipeVersion.objects.get(pk=parent.pk),
            warehouse=valued_store,
            as_of_date=_today(),
        )
        assert after.total_material_cost == before.total_material_cost
        assert len(after.lines) == len(before.lines)


class TestStockedSubRecipesAreNotExpanded:
    def test_a_stocked_sub_recipe_is_one_leaf_and_a_non_stocked_one_expands(
        self,
        valued_store: Warehouse,
        organization: Organization,
        chain: Chain,
        cooked_rice: InventoryItem,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        The paired case, in one test because the contrast *is* the claim.

        A stocked recipe's output item arrives as an ordinary `RecipeLine` and
        the card holds exactly one row for it — the ingredient tree that
        produced it is never walked, because its book value already contains
        those ingredients (RCP-071). The non-stocked blend beside it, with the
        very same internal structure, does expand.
        """
        from apps.kitchen.services import create_recipe

        # A stocked recipe: it has an output item, so it may only ever be
        # referenced as a line, and a database constraint holds that.
        stocked = create_recipe(
            organization=organization,
            code="STOCKED-1",
            name_ar="نصف مصنّع",
            recipe_type="BATCH",
            output_item=cooked_rice,
            created_by=manager,
        )
        stocked_draft = build_complete_draft(
            recipe=stocked, unit=kilogram, item=rice, author=manager
        )
        carry_to_active(
            stocked_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

        blend = chain.leaf("BLEND-H")
        parent_draft = chain.draft("DISH-H")
        create_recipe_component(
            version=parent_draft, component_version=blend, multiplier=Decimal("0.5")
        )
        add_recipe_line(
            version=parent_draft,
            item=cooked_rice,
            entered_quantity=Decimal("1"),
            entered_unit=kilogram,
        )
        parent = chain.activate(parent_draft)

        card = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())
        by_item = [line.item.code for line in card.lines]
        # One row for the stocked item, and no row from the recipe that makes it.
        assert by_item.count(cooked_rice.code) == 1
        # The non-stocked blend did expand: its own rice line is on the card.
        assert [line.path_display for line in card.lines if line.path_display] == ["1"]

    def test_the_stocked_leaf_carries_no_component_path(
        self,
        valued_store: Warehouse,
        organization: Organization,
        chain: Chain,
        cooked_rice: InventoryItem,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """It is the parent's own line, so its path is empty and its kind DIRECT."""
        parent_draft = chain.draft("DISH-I")
        add_recipe_line(
            version=parent_draft,
            item=cooked_rice,
            entered_quantity=Decimal("1"),
            entered_unit=kilogram,
        )
        parent = chain.activate(parent_draft)
        card = cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())
        stocked_line = next(line for line in card.lines if line.item.code == cooked_rice.code)
        assert stocked_line.kind is CostLineKind.DIRECT
        assert stocked_line.path_display == ""
        assert stocked_line.cumulative_multiplier == Decimal("1")


class TestACorruptGraphFailsLoudly:
    """
    §H: the walk must **refuse** an impossible graph, not truncate it.

    The graph is already acyclic and depth-bounded by three earlier guards — the
    service, the activation walk and a row trigger — so both tests here step
    past those on purpose. A costing walk that silently stopped at the limit
    would return a total that is too small and still look like an answer, and
    one that did not stop would hang the request thread. Both are worse than a
    named refusal, and neither is visible until somebody writes this test.

    The **codes** read `recipe_expansion_*` since Task 3.4 moved the walk into
    `apps/kitchen/expansion.py` for production drafting to share. A production
    draft refused for a cycle is not a costing failure, so the code says what
    happened rather than which caller asked. The behaviour these two guard —
    refuse loudly, never truncate — is unchanged, which is why they were
    rewritten rather than deleted.
    """

    def test_a_graph_deeper_than_the_limit_is_refused_not_truncated(
        self, valued_store: Warehouse, chain: Chain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Lower the limit rather than build an over-deep graph, because an
        over-deep graph cannot be built: activation refuses it.

        With the bound at one edge, a legitimate two-edge chain is over-deep as
        far as the costing walk is concerned — and the walk says so instead of
        quietly costing the top two levels and dropping the third.
        """
        from apps.kitchen import expansion

        leaf = chain.leaf("SPICE-K")
        middle = chain.above("BLEND-K", child=leaf, multiplier=Decimal("0.25"))
        parent = chain.above("DISH-K", child=middle, multiplier=Decimal("0.5"))

        # Patch the engine, not the caller: the bound lives with the walk now.
        monkeypatch.setattr(expansion, "MAX_COMPONENT_DEPTH", 1)
        with pytest.raises(ValidationError) as refusal:
            cost_recipe_version(version=parent, warehouse=valued_store, as_of_date=_today())
        assert refusal.value.code == "recipe_expansion_graph_too_deep"

    @pytest.mark.django_db(transaction=True)
    def test_a_planted_cycle_is_refused_rather_than_recursed_forever(
        self,
        valued_store: Warehouse,
        chain: Chain,
    ) -> None:
        """
        Plants a cycle by disabling the trigger that forbids one, then costs.

        `transaction=True` because `ALTER TABLE` cannot run inside a
        transaction that still has trigger events queued, which is every
        ordinary test here. The table is truncated afterwards, so the invalid
        row exists for the length of one test and is never seeded — the demo
        seed builds nothing invalid, and a separate test holds that line.
        """
        from django.db import connection

        leaf = chain.leaf("SPICE-L")
        parent = chain.above("DISH-L", child=leaf, multiplier=Decimal("0.5"))

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE kitchen_recipecomponent "
                "DISABLE TRIGGER kitchen_recipe_component_follows_its_version"
            )
            try:
                cursor.execute(
                    """
                    INSERT INTO kitchen_recipecomponent
                        (version_id, recipe_id, line_order, component_version_id,
                         component_recipe_id, multiplier, note, created_at,
                         updated_at, public_id, source_document, source_page,
                         source_sha256, source_reference, source_note)
                    VALUES (%s, %s, 1, %s, %s, 1, '', NOW(), NOW(),
                            gen_random_uuid(), '', NULL, '', '', '')
                    """,
                    [leaf.pk, leaf.recipe_id, parent.pk, parent.recipe_id],
                )
            finally:
                cursor.execute(
                    "ALTER TABLE kitchen_recipecomponent "
                    "ENABLE TRIGGER kitchen_recipe_component_follows_its_version"
                )

        with pytest.raises(ValidationError) as refusal:
            cost_recipe_version(
                version=RecipeVersion.objects.get(pk=parent.pk),
                warehouse=valued_store,
                as_of_date=_today(),
            )
        assert refusal.value.code == "recipe_expansion_graph_cycle"
