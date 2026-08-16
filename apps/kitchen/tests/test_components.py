"""
The nested-recipe graph: shape, eligibility, cycles, depth and exact version
adoption.

The claim these tests defend is RCP-070's: **the two sub-recipe shapes are
mutually exclusive by construction, not by rule.** A stocked blend is consumed
as a line at its book value; a non-stocked blend is expanded from its exact
child version. Whichever shape a recipe has, the other reference is refused —
which is what makes double counting unrepresentable rather than merely
forbidden.

The second claim is RCP-072's: **an exact child-version link never moves.** No
service in this module re-points a component, and several tests below exist only
to prove that a newer, better, approved child version sitting right there does
not get adopted by anything that did not ask for it.

One fact shapes every graph test here and is worth stating once: **a draft can
never be a child.** A component may be written only on a `DRAFT` parent and may
point only at a frozen, approved child, so every multi-level graph is built
strictly bottom-up. That is the design working, not an inconvenience of the
harness — a dish cannot be built on a blend nobody has approved.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from apps.inventory.models import InventoryItem
from apps.kitchen.graph import (
    component_paths,
    component_tree,
    depth_above,
    depth_below,
    flatten_tree,
    read_graph,
)
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    reject_recipe_version,
    submit_recipe_version,
)
from apps.kitchen.models import (
    MAX_COMPONENT_DEPTH,
    Recipe,
    RecipeComponent,
    RecipeVersion,
    RecipeVersionStatus,
)
from apps.kitchen.selectors import component_candidates
from apps.kitchen.services import (
    archive_recipe,
    create_draft_recipe_version,
    create_recipe_component,
    remove_recipe_component,
    reorder_recipe_component,
    update_recipe_component,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import build_complete_draft, carry_to_active, carry_to_approved, make_child_recipe

pytestmark = pytest.mark.django_db


def codes_of(error: ValidationError) -> set[str]:
    """
    Every stable code in a refusal, however it was raised.

    `hasattr(error, "message")` is tested **first** and the recursion into
    `error_list` second, and the order is not cosmetic: a single-message
    `ValidationError` carries *both* attributes — its own `error_list` is
    `[self]` — so recursing first loops straight past the code this helper
    exists to read.
    """
    if hasattr(error, "message"):
        return {error.code or ""}
    if hasattr(error, "error_dict"):
        return {
            code
            for errors in error.error_dict.values()
            for item in errors
            for code in codes_of(item)
        }
    if hasattr(error, "error_list"):
        return {code for item in error.error_list for code in codes_of(item)}
    return set()


# ---------------------------------------------------------------------------
# A builder for graphs of non-stocked recipes
# ---------------------------------------------------------------------------


class World:
    """Builds bottom-up graphs of non-stocked recipes, addressed by code."""

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
        self.active: dict[str, RecipeVersion] = {}

    def recipe_for(self, code: str) -> Recipe:
        if code not in self.recipes:
            self.recipes[code] = make_child_recipe(
                organization=self.organization, code=code, author=self.manager
            )
        return self.recipes[code]

    def draft(self, code: str) -> RecipeVersion:
        """A new open draft of one recipe, complete enough to be submitted."""
        return build_complete_draft(
            recipe=self.recipe_for(code), unit=self.unit, item=self.item, author=self.manager
        )

    def activate(
        self,
        code: str,
        version: RecipeVersion,
        *,
        effective_from: datetime.date = datetime.date(2026, 1, 1),
        effective_to: datetime.date | None = None,
        branches: list[Branch] | None = None,
    ) -> RecipeVersion:
        """Carry a draft all the way to ACTIVE and remember it under its code."""
        self.active[code] = carry_to_active(
            version,
            submitter=self.manager,
            cook=self.cook,
            keeper=self.keeper,
            accountant=self.accountant,
            approver=self.approver,
            effective_from=effective_from,
            effective_to=effective_to,
            branches=branches,
        )
        return self.active[code]

    def leaf(self, code: str) -> RecipeVersion:
        """An active recipe with no components of its own."""
        return self.activate(code, self.draft(code))

    def above(
        self, code: str, *, child: RecipeVersion, multiplier: Decimal = Decimal("1")
    ) -> RecipeVersion:
        """A new active version of `code` containing `child`, one level up."""
        draft = self.draft(code)
        create_recipe_component(version=draft, component_version=child, multiplier=multiplier)
        return self.activate(code, draft)


@pytest.fixture
def world(
    organization: Organization,
    branch: Branch,
    kilogram: UnitOfMeasure,
    rice: InventoryItem,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> World:
    return World(
        organization=organization,
        unit=kilogram,
        item=rice,
        manager=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )


class TestTheTwoShapes:
    """RCP-070: a recipe is stock or it is a component, and never both."""

    def test_a_non_stocked_child_is_accepted(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.25")
        )
        assert component.component_version_id == blend_active.pk
        assert component.component_recipe_id == blend_active.recipe_id
        assert component.line_order == 1

    def test_a_stocked_child_is_refused(
        self, complete_draft: RecipeVersion, stocked_active: RecipeVersion
    ) -> None:
        """
        The blend has a book value, so expanding its ingredients as well would
        charge the parent for the ingredients *and* for the blend they became.
        """
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft, component_version=stocked_active, multiplier=Decimal("1")
            )
        assert "recipe_component_child_is_stocked" in codes_of(caught.value)

    def test_the_database_refuses_a_stocked_child_too(
        self, complete_draft: RecipeVersion, stocked_active: RecipeVersion
    ) -> None:
        """A raw insert that bypasses the service still cannot double count."""
        with (
            pytest.raises(Exception) as caught,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                INSERT INTO kitchen_recipecomponent
                    (version_id, recipe_id, line_order, component_version_id,
                     component_recipe_id, multiplier, note, public_id,
                     created_at, updated_at, source_document, source_sha256,
                     source_reference, source_note)
                VALUES (%s, %s, 1, %s, %s, 1, '', gen_random_uuid(), now(), now(),
                        '', '', '', '')
                """,
                [
                    complete_draft.pk,
                    complete_draft.recipe_id,
                    stocked_active.pk,
                    stocked_active.recipe_id,
                ],
            )
        assert "consumed as a line" in str(caught.value)

    def test_a_stocked_recipe_never_appears_as_a_candidate(
        self,
        manager: User,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        stocked_active: RecipeVersion,
    ) -> None:
        offered = list(component_candidates(manager, complete_draft))
        assert blend_active in offered
        assert stocked_active not in offered

    def test_the_parent_recipe_is_never_a_candidate(
        self,
        manager: User,
        recipe: Recipe,
        active_version: RecipeVersion,
        blend_active: RecipeVersion,
    ) -> None:
        second = build_complete_draft(
            recipe=recipe,
            unit=active_version.output_unit,
            item=blend_active.lines.first().item,  # type: ignore[union-attr]
            author=manager,
        )
        offered = list(component_candidates(manager, second))
        assert all(version.recipe_id != recipe.pk for version in offered)


class TestChildEligibility:
    """Which child versions a parent may name at all."""

    def test_a_draft_child_is_refused(
        self, complete_draft: RecipeVersion, blend_draft: RecipeVersion
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft, component_version=blend_draft, multiplier=Decimal("1")
            )
        assert "recipe_component_child_not_eligible" in codes_of(caught.value)

    def test_a_submitted_child_is_refused(
        self, complete_draft: RecipeVersion, blend_draft: RecipeVersion, manager: User
    ) -> None:
        submit_recipe_version(version=blend_draft, actor=manager)
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft,
                component_version=RecipeVersion.objects.get(pk=blend_draft.pk),
                multiplier=Decimal("1"),
            )
        assert "recipe_component_child_not_eligible" in codes_of(caught.value)

    def test_a_rejected_child_is_refused(
        self,
        complete_draft: RecipeVersion,
        blend_draft: RecipeVersion,
        manager: User,
        approver: User,
    ) -> None:
        submit_recipe_version(version=blend_draft, actor=manager)
        reject_recipe_version(
            version=RecipeVersion.objects.get(pk=blend_draft.pk),
            actor=approver,
            reason="الخلطة غير مكتملة.",
        )
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft,
                component_version=RecipeVersion.objects.get(pk=blend_draft.pk),
                multiplier=Decimal("1"),
            )
        assert "recipe_component_child_not_eligible" in codes_of(caught.value)

    def test_an_approved_but_never_activated_child_is_accepted_at_draft_time(
        self, complete_draft: RecipeVersion, blend_approved: RecipeVersion
    ) -> None:
        """
        Drafting against an agreed child is permitted; taking effect on a date it
        does not cover is not. The two gates sit at different moments on purpose
        — requiring ACTIVE here would deadlock a blend and the dish that uses it
        being prepared in one sitting.
        """
        component = create_recipe_component(
            version=complete_draft, component_version=blend_approved, multiplier=Decimal("1")
        )
        assert component.component_version.status == RecipeVersionStatus.APPROVED

    def test_a_foreign_organization_child_is_refused(
        self,
        complete_draft: RecipeVersion,
        other_organization: Organization,
        rival_manager: User,
        kilogram: UnitOfMeasure,
        rival_item: InventoryItem,
    ) -> None:
        foreign_recipe = make_child_recipe(
            organization=other_organization, code="THEIR-BLEND", author=rival_manager
        )
        foreign = build_complete_draft(
            recipe=foreign_recipe, unit=kilogram, item=rival_item, author=rival_manager
        )
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft, component_version=foreign, multiplier=Decimal("1")
            )
        assert "recipe_component_foreign_organization" in codes_of(caught.value)

    def test_an_archived_child_recipe_is_refused(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion, blend: Recipe
    ) -> None:
        archive_recipe(recipe=blend, reason="لم تعد تُستعمل.")
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft,
                component_version=RecipeVersion.objects.get(pk=blend_active.pk),
                multiplier=Decimal("1"),
            )
        assert "recipe_component_child_recipe_archived" in codes_of(caught.value)

    def test_the_same_child_recipe_cannot_be_named_twice(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft, component_version=blend_active, multiplier=Decimal("2")
            )
        assert "recipe_component_duplicate_child" in codes_of(caught.value)


class TestTheMultiplier:
    """A scaling identity, and nothing else."""

    def test_a_zero_multiplier_is_refused(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        with pytest.raises(ValidationError):
            create_recipe_component(
                version=complete_draft, component_version=blend_active, multiplier=Decimal("0")
            )

    def test_a_negative_multiplier_is_refused(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        with pytest.raises(ValidationError):
            create_recipe_component(
                version=complete_draft, component_version=blend_active, multiplier=Decimal("-1")
            )

    def test_the_database_refuses_a_non_positive_multiplier(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeComponent.objects.filter(pk=component.pk).update(multiplier=Decimal("0"))

    def test_the_multiplier_renders_with_a_period(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        """
        A conversion factor is a technical identity: a comma there is ambiguous
        and invites a mis-typed re-entry (`CLAUDE.md`).
        """
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.25")
        )
        assert component.multiplier_display == "0.250000000000"
        assert "," not in component.multiplier_display

    def test_no_component_field_names_a_cost(self) -> None:
        """Task 3.3 owns costing. Nothing here may quietly acquire a price."""
        forbidden = ("cost", "price", "amount", "margin", "value", "total")
        names = {field.name for field in RecipeComponent._meta.get_fields()}
        assert not [name for name in names if any(word in name for word in forbidden)]


class TestCycles:
    """RCP-076: the transitive closure may not contain the parent's own recipe."""

    def test_a_version_cannot_contain_itself(self, complete_draft: RecipeVersion) -> None:
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft, component_version=complete_draft, multiplier=Decimal("1")
            )
        assert "recipe_component_cycle" in codes_of(caught.value)

    def test_a_recipe_cannot_contain_another_version_of_itself(
        self,
        recipe: Recipe,
        active_version: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        """
        `A v2 → A v1`. No version repeats, and it is still recursion: the dish
        would contain an older edition of itself. A version-identity check alone
        accepts this, which is why the walk compares recipes — and why the
        database carries a matching `CheckConstraint`.
        """
        second = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=second, component_version=active_version, multiplier=Decimal("1")
            )
        assert "recipe_component_cycle" in codes_of(caught.value)

    def test_the_database_refuses_a_self_reference(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeComponent.objects.filter(pk=component.pk).update(
                component_version_id=complete_draft.pk,
                component_recipe_id=complete_draft.recipe_id,
            )

    def test_a_two_node_cycle_is_refused(self, world: World) -> None:
        """`B v1 → A v1` exists; a new `A v2 → B v1` would close the loop."""
        a_v1 = world.leaf("CY-A")
        world.above("CY-B", child=a_v1)

        a_v2 = world.draft("CY-A")
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=a_v2, component_version=world.active["CY-B"], multiplier=Decimal("1")
            )
        assert "recipe_component_cycle" in codes_of(caught.value)
        assert "CY-A" in str(caught.value)

    def test_a_three_node_cycle_is_refused_and_names_the_path(self, world: World) -> None:
        """`C → B → A` exists; `A v2 → C` would close a three-node loop."""
        a_v1 = world.leaf("CY3-A")
        b_v1 = world.above("CY3-B", child=a_v1)
        world.above("CY3-C", child=b_v1)

        a_v2 = world.draft("CY3-A")
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=a_v2, component_version=world.active["CY3-C"], multiplier=Decimal("1")
            )
        assert "recipe_component_cycle" in codes_of(caught.value)
        message = str(caught.value)
        assert "CY3-B" in message and "CY3-A" in message

    def test_the_persisted_graph_is_what_is_walked(self, world: World) -> None:
        """
        Not the submitted payload. The refusal above comes from rows the caller
        never mentioned — `A v2` names only `C`, and `C → B → A` is discovered.
        """
        a_v1 = world.leaf("PG-A")
        b_v1 = world.above("PG-B", child=a_v1)
        graph = read_graph(world.organization.pk)
        assert graph.edges_below(b_v1.pk)[0].child_version_id == a_v1.pk


class TestDepth:
    """RCP-077 / KD-08: at most three component edges on any path."""

    def test_the_limit_is_the_approved_constant(self) -> None:
        assert MAX_COMPONENT_DEPTH == 3

    def test_depth_one_is_permitted(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        graph = read_graph(complete_draft.recipe.organization_id)
        assert depth_below(graph, complete_draft.pk)[0] == 1

    def test_depth_two_is_permitted(self, world: World) -> None:
        leaf = world.leaf("D2-C")
        middle = world.above("D2-B", child=leaf)
        top = world.draft("D2-A")
        create_recipe_component(version=top, component_version=middle, multiplier=Decimal("1"))
        graph = read_graph(world.organization.pk)
        assert depth_below(graph, top.pk)[0] == 2

    def test_the_maximum_depth_is_permitted_and_one_more_is_refused(self, world: World) -> None:
        """
        Built bottom-up: `E`, then `D → E`, `C → D`, `B → C` gives three edges
        below `B` and is accepted. `A → B` would make four, and is refused with
        the path that proves it.
        """
        e_v1 = world.leaf("DP-E")
        d_v1 = world.above("DP-D", child=e_v1)
        c_v1 = world.above("DP-C", child=d_v1)
        b_v1 = world.above("DP-B", child=c_v1)

        graph = read_graph(world.organization.pk)
        assert depth_below(graph, b_v1.pk)[0] == MAX_COMPONENT_DEPTH

        a_draft = world.draft("DP-A")
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=a_draft, component_version=b_v1, multiplier=Decimal("1")
            )
        assert "recipe_component_depth_exceeded" in codes_of(caught.value)
        assert "DP-E" in str(caught.value)

    def test_the_refusal_reports_the_whole_path(self, world: World) -> None:
        e_v1 = world.leaf("PT-E")
        d_v1 = world.above("PT-D", child=e_v1)
        c_v1 = world.above("PT-C", child=d_v1)
        b_v1 = world.above("PT-B", child=c_v1)
        a_draft = world.draft("PT-A")
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=a_draft, component_version=b_v1, multiplier=Decimal("1")
            )
        message = str(caught.value)
        for code in ("PT-B", "PT-C", "PT-D", "PT-E"):
            assert code in message

    def test_depth_is_measured_upward_as_well_as_downward(self, world: World) -> None:
        """
        The depth formula is `above(parent) + 1 + below(child)`, and the upward
        half is not decoration.

        Today it is always zero at the moment an edge is written, because the
        parent of a new edge is a `DRAFT` and a draft is never a child. It stops
        being zero at **activation**, where the version being certified may
        already be somebody's component — which is why the whole-chain check
        runs there too. This asserts the measurement itself rather than a
        refusal that the draft-only rule makes unreachable.
        """
        leaf = world.leaf("UP-C")
        middle = world.above("UP-B", child=leaf)
        top = world.draft("UP-A")
        create_recipe_component(version=top, component_version=middle, multiplier=Decimal("1"))
        world.activate("UP-A", top)

        graph = read_graph(world.organization.pk)
        assert depth_above(graph, leaf.pk)[0] == 2
        assert depth_above(graph, middle.pk)[0] == 1
        assert depth_above(graph, world.active["UP-A"].pk)[0] == 0


class TestExactVersionAdoption:
    """RCP-072: the link is to one exact version and nothing re-points it."""

    def test_a_parent_keeps_its_child_after_a_newer_child_exists(
        self,
        complete_draft: RecipeVersion,
        blend: Recipe,
        blend_active: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.5")
        )
        second = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        carry_to_approved(
            second,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        component.refresh_from_db()
        assert component.component_version_id == blend_active.pk
        assert component.component_version.version_number == 1

    def test_superseding_the_child_does_not_repoint_the_parent(
        self,
        complete_draft: RecipeVersion,
        blend: Recipe,
        blend_active: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        A blend that changed in September must not restate what the July dish
        claimed to contain — RCP-011's rule one level down.
        """
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.5")
        )
        second = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        carry_to_approved(
            second,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=second,
            actor=approver,
            effective_from=datetime.date(2027, 1, 1),
            supersedes=RecipeVersion.objects.get(pk=blend_active.pk),
        )
        component.refresh_from_db()
        assert component.component_version_id == blend_active.pk
        assert (
            RecipeVersion.objects.get(pk=blend_active.pk).status == RecipeVersionStatus.SUPERSEDED
        )

    def test_a_replacement_parent_adopts_the_new_child_explicitly(
        self,
        recipe: Recipe,
        blend: Recipe,
        blend_active: RecipeVersion,
        complete_draft: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """Adopting a newer child is a new **parent** version, never an edit."""
        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.5")
        )
        carry_to_active(
            complete_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=datetime.date(2026, 7, 1),
        )
        blend_v2 = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        carry_to_approved(
            blend_v2,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

        parent_v2 = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        adopted = create_recipe_component(
            version=parent_v2,
            component_version=RecipeVersion.objects.get(pk=blend_v2.pk),
            multiplier=Decimal("0.5"),
        )
        assert adopted.component_version_id == blend_v2.pk
        assert adopted.component_version_id != blend_active.pk

    def test_the_historical_parent_tree_is_unchanged_by_a_later_child(
        self,
        complete_draft: RecipeVersion,
        blend: Recipe,
        blend_active: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.5")
        )
        before = [row.label for row in flatten_tree(component_tree(complete_draft))]
        second = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        carry_to_approved(
            second,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        after = [
            row.label
            for row in flatten_tree(component_tree(RecipeVersion.objects.get(pk=complete_draft.pk)))
        ]
        assert before == after


class TestDraftOnlyMutation:
    """A component may be written only while its parent is a draft."""

    def test_edit_reorder_and_remove_all_work_on_a_draft(
        self, world: World, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        other = world.leaf("ED-X")
        first = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        second = create_recipe_component(
            version=complete_draft, component_version=other, multiplier=Decimal("2")
        )
        assert [first.line_order, second.line_order] == [1, 2]

        updated = update_recipe_component(component=first, multiplier=Decimal("3"), note="أكثر")
        assert updated.multiplier == Decimal("3.000000000000")
        assert updated.note == "أكثر"

        ordered = reorder_recipe_component(component=second, line_order=1)
        assert [row.line_order for row in ordered] == [1, 2]
        assert ordered[0].pk == second.pk

        remove_recipe_component(component=first, reason="لم تعد مطلوبة.")
        assert RecipeComponent.objects.filter(version=complete_draft).count() == 1

    def test_a_draft_may_swap_the_child_version(
        self, world: World, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        other = world.leaf("SW-A")
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        swapped = update_recipe_component(
            component=component, multiplier=Decimal("1"), component_version=other
        )
        assert swapped.component_version_id == other.pk
        assert swapped.component_recipe_id == other.recipe_id

    def test_a_duplicate_line_order_is_refused_by_the_database(
        self, world: World, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        other = world.leaf("DU-A")
        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        second = create_recipe_component(
            version=complete_draft, component_version=other, multiplier=Decimal("1")
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeComponent.objects.filter(pk=second.pk).update(line_order=1)

    def test_a_zero_line_order_is_refused(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeComponent.objects.filter(pk=component.pk).update(line_order=0)

    @pytest.mark.parametrize("target", ["SUBMITTED", "APPROVED", "ACTIVE"])
    def test_a_frozen_parent_refuses_every_component_mutation(
        self,
        target: str,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        if target == "SUBMITTED":
            submit_recipe_version(version=complete_draft, actor=manager)
        elif target == "APPROVED":
            carry_to_approved(
                complete_draft,
                submitter=manager,
                cook=cook,
                keeper=keeper,
                accountant=accountant,
                approver=approver,
            )
        else:
            carry_to_active(
                complete_draft,
                submitter=manager,
                cook=cook,
                keeper=keeper,
                accountant=accountant,
                approver=approver,
                effective_from=datetime.date(2026, 7, 1),
            )

        with pytest.raises(ValidationError):
            update_recipe_component(component=component, multiplier=Decimal("9"))
        with pytest.raises(ValidationError):
            reorder_recipe_component(component=component, line_order=1)
        with pytest.raises(ValidationError):
            remove_recipe_component(component=component)
        with pytest.raises(ValidationError):
            create_recipe_component(
                version=RecipeVersion.objects.get(pk=complete_draft.pk),
                component_version=blend_active,
                multiplier=Decimal("1"),
            )

    def test_a_stale_draft_object_cannot_edit_a_submitted_parent(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion, manager: User
    ) -> None:
        """The caller's object still says DRAFT; the row does not, and the row wins."""
        stale = RecipeVersion.objects.get(pk=complete_draft.pk)
        submit_recipe_version(version=complete_draft, actor=manager)
        assert stale.status == RecipeVersionStatus.DRAFT
        with pytest.raises(ValidationError):
            create_recipe_component(
                version=stale, component_version=blend_active, multiplier=Decimal("1")
            )


class TestTheDatabaseRefusesRawWrites:
    """Whole-row protection, proven by bypassing every Python guard."""

    def _frozen(
        self,
        parent: RecipeVersion,
        child: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> RecipeComponent:
        component = create_recipe_component(
            version=parent, component_version=child, multiplier=Decimal("1")
        )
        carry_to_approved(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        return component

    def test_a_raw_update_on_a_frozen_parent_is_refused(
        self,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        component = self._frozen(
            complete_draft, blend_active, manager, cook, keeper, accountant, approver
        )
        with pytest.raises(Exception) as caught, transaction.atomic():
            RecipeComponent.objects.filter(pk=component.pk).update(multiplier=Decimal("99"))
        assert "frozen" in str(caught.value)

    def test_a_raw_delete_on_a_frozen_parent_is_refused(
        self,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        component = self._frozen(
            complete_draft, blend_active, manager, cook, keeper, accountant, approver
        )
        with pytest.raises(Exception) as caught, transaction.atomic():
            RecipeComponent.objects.filter(pk=component.pk).delete()
        assert "frozen" in str(caught.value)

    def test_a_raw_insert_under_a_frozen_parent_is_refused(
        self,
        world: World,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        other = world.leaf("RI-A")
        self._frozen(complete_draft, blend_active, manager, cook, keeper, accountant, approver)
        with (
            pytest.raises(Exception) as caught,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                INSERT INTO kitchen_recipecomponent
                    (version_id, recipe_id, line_order, component_version_id,
                     component_recipe_id, multiplier, note, public_id,
                     created_at, updated_at, source_document, source_sha256,
                     source_reference, source_note)
                VALUES (%s, %s, 9, %s, %s, 1, '', gen_random_uuid(), now(), now(),
                        '', '', '', '')
                """,
                [complete_draft.pk, complete_draft.recipe_id, other.pk, other.recipe_id],
            )
        assert "frozen" in str(caught.value)

    def test_a_component_cannot_be_moved_to_another_version(
        self,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        manager: User,
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        # A draft on *another* recipe: `recipe_version_one_open_per_recipe`
        # already forbids a sibling draft on the same recipe, so there would be
        # nothing to move it to.
        other_recipe = make_child_recipe(
            organization=recipe.organization, code="MOVE-TARGET", author=manager
        )
        other = create_draft_recipe_version(
            recipe=other_recipe,
            batch_size=Decimal("1"),
            expected_output_quantity=Decimal("1"),
            output_unit=kilogram,
            created_by=manager,
        )
        with pytest.raises(Exception) as caught, transaction.atomic():
            RecipeComponent.objects.filter(pk=component.pk).update(version_id=other.pk)
        assert "another recipe version" in str(caught.value)

    def test_the_public_id_is_immutable_even_on_a_draft(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        with pytest.raises(Exception) as caught, transaction.atomic():
            RecipeComponent.objects.filter(pk=component.pk).update(public_id=uuid.uuid4())
        assert "immutable" in str(caught.value)

    def test_the_denormalised_recipe_cannot_drift(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion, blend: Recipe
    ) -> None:
        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        with pytest.raises(Exception) as caught, transaction.atomic():
            RecipeComponent.objects.filter(pk=component.pk).update(recipe_id=blend.pk)
        assert "own recipe" in str(caught.value)

    def test_a_child_version_cannot_be_deleted_while_a_parent_names_it(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        """`PROTECT`: a component whose child vanished is a recipe that lies."""
        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        with pytest.raises((ProtectedError, Exception)), transaction.atomic():
            RecipeVersion.objects.filter(pk=blend_active.pk).delete()


class TestTheTree:
    """Reading the graph: order, cumulative scaling, and no cost anywhere."""

    def test_the_tree_is_ordered_by_line_order_not_primary_key(
        self, world: World, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        other = world.leaf("TR-X")
        first = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        second = create_recipe_component(
            version=complete_draft, component_version=other, multiplier=Decimal("1")
        )
        # Put the higher primary key first by line order.
        reorder_recipe_component(component=second, line_order=1)
        rows = flatten_tree(component_tree(RecipeVersion.objects.get(pk=complete_draft.pk)))
        assert [row.version.pk for row in rows] == [
            second.component_version_id,
            first.component_version_id,
        ]

    def test_the_cumulative_multiplier_is_a_full_precision_product(self, world: World) -> None:
        """
        RCP-073: multiplied down the path at full precision and quantized
        exactly once, at the batch line — which is Task 3.4's, not this one's.
        """
        leaf = world.leaf("CM-C")
        middle = world.above("CM-B", child=leaf, multiplier=Decimal("0.5"))
        top = world.draft("CM-A")
        create_recipe_component(version=top, component_version=middle, multiplier=Decimal("0.25"))

        rows = flatten_tree(component_tree(top))
        assert [row.depth for row in rows] == [1, 2]
        assert rows[0].cumulative_multiplier == Decimal("0.250000000000")
        assert rows[1].cumulative_multiplier == Decimal("0.250000000000") * Decimal(
            "0.500000000000"
        )
        assert "," not in rows[1].cumulative_display

    def test_paths_are_reported_for_the_verifier_and_the_refusal_alike(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        paths = component_paths(RecipeVersion.objects.get(pk=complete_draft.pk))
        assert len(paths) == 1
        assert "BLEND-1" in paths[0]
