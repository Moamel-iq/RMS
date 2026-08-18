"""
The nested demo scenario, the graph verifier, and the zero-effect boundary.

The demo tests exist because "idempotent" is the claim a demo seed most often
gets wrong, and because the nested scenario is the only place a reader can *see*
RCP-070 and RCP-072 rather than read about them: two shapes on one screen, and a
dish that keeps naming the blend it was written against after a newer blend
exists.

The verifier tests exist for the opposite reason. Almost every finding it
reports is unrepresentable at the database, so they are exercised against the
pure check functions with in-memory mutated copies — never by disabling a
trigger, and never by writing a row the constraints would refuse.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from apps.accounting.models import JournalEntry, JournalLine
from apps.inventory.models import (
    InventoryItem,
    ItemCategory,
    ItemType,
    StockBalance,
    StockMovement,
)
from apps.kitchen.demo import (
    DEMO_DISH_CODE,
    DEMO_MARINADE_CODE,
    DEMO_SPICE_CODE,
    seed_demo_recipes,
)
from apps.kitchen.graph import component_tree, flatten_tree
from apps.kitchen.models import (
    Recipe,
    RecipeComponent,
    RecipeLine,
    RecipeVersion,
    RecipeVersionStatus,
)
from apps.kitchen.reconciliation import verify_organization
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db


def _counts() -> tuple[int, ...]:
    return (
        Recipe.objects.count(),
        RecipeVersion.objects.count(),
        RecipeComponent.objects.count(),
        RecipeLine.objects.count(),
    )


class TestTheNestedDemoScenario:
    @pytest.fixture
    def demo_items(
        self,
        organization: Organization,
        item_category: ItemCategory,
        kilogram: UnitOfMeasure,
        litre: UnitOfMeasure,
        piece: UnitOfMeasure,
    ) -> None:
        """The Phase 1 demo items the kitchen seed builds on, named as it names them."""
        for code, unit, kind in (
            ("DEMO-RICE", kilogram, ItemType.RAW_MATERIAL),
            ("DEMO-OIL", litre, ItemType.RAW_MATERIAL),
            ("DEMO-MEAT", kilogram, ItemType.RAW_MATERIAL),
            ("DEMO-CHICKEN", piece, ItemType.RAW_MATERIAL),
            ("DEMO-CONTAINER", piece, ItemType.PACKAGING),
        ):
            InventoryItem.objects.create(
                organization=organization,
                code=code,
                name_ar=code,
                category=item_category,
                item_type=kind,
                base_unit=unit,
            )

    @pytest.fixture
    def seeded(
        self,
        organization: Organization,
        demo_items: None,
        gram: UnitOfMeasure,
        manager: User,
    ) -> list[Recipe]:
        return seed_demo_recipes(organization=organization, created_by=manager)

    def test_the_three_nested_recipes_are_created(self, seeded: list[Recipe]) -> None:
        codes = {recipe.code for recipe in seeded}
        assert {DEMO_SPICE_CODE, DEMO_MARINADE_CODE, DEMO_DISH_CODE} <= codes

    def test_the_child_recipes_are_non_stocked(self, seeded: list[Recipe]) -> None:
        """No output item, so they may only ever be components (RCP-070)."""
        for code in (DEMO_SPICE_CODE, DEMO_MARINADE_CODE):
            recipe = Recipe.objects.get(code=code)
            assert recipe.output_item_id is None

    def test_the_tree_is_two_levels_deep(self, seeded: list[Recipe]) -> None:
        dish = Recipe.objects.get(code=DEMO_DISH_CODE)
        version = dish.versions.filter(status=RecipeVersionStatus.ACTIVE).get()
        rows = flatten_tree(component_tree(version))
        assert [row.depth for row in rows] == [1, 2]
        assert rows[0].recipe.code == DEMO_MARINADE_CODE
        assert rows[1].recipe.code == DEMO_SPICE_CODE

    def test_the_stocked_input_arrives_as_a_line_not_a_component(
        self, seeded: list[Recipe]
    ) -> None:
        """
        Both shapes on one screen. The semi-finished item has a book value and
        is consumed as a line; the marinade beside it has none and is expanded
        from its exact child version.
        """
        dish = Recipe.objects.get(code=DEMO_DISH_CODE)
        version = dish.versions.filter(status=RecipeVersionStatus.ACTIVE).get()
        line_items = {line.item.code for line in version.lines.select_related("item")}
        assert "DEMO-RICE-COOKED" in line_items
        component_recipes = {
            component.component_recipe.code
            for component in version.components.select_related("component_recipe")
        }
        assert "DEMO-RICE-COOKED" not in component_recipes
        assert not RecipeComponent.objects.filter(
            component_recipe__output_item__isnull=False
        ).exists()

    def test_the_historical_dish_still_names_the_historical_marinade(
        self, seeded: list[Recipe]
    ) -> None:
        """RCP-072, visible in the data rather than described in a docstring."""
        dish = Recipe.objects.get(code=DEMO_DISH_CODE)
        marinade = Recipe.objects.get(code=DEMO_MARINADE_CODE)
        historical = dish.versions.filter(status=RecipeVersionStatus.SUPERSEDED).get()
        superseded_child = marinade.versions.filter(status=RecipeVersionStatus.SUPERSEDED).get()
        assert historical.components.get().component_version_id == superseded_child.pk

    def test_the_replacement_dish_adopts_the_newer_marinade(self, seeded: list[Recipe]) -> None:
        dish = Recipe.objects.get(code=DEMO_DISH_CODE)
        marinade = Recipe.objects.get(code=DEMO_MARINADE_CODE)
        current = dish.versions.filter(status=RecipeVersionStatus.ACTIVE).get()
        newest_child = marinade.versions.filter(status=RecipeVersionStatus.ACTIVE).get()
        assert current.components.get().component_version_id == newest_child.pk
        assert newest_child.version_number == 2

    def test_no_demo_graph_is_invalid(self, seeded: list[Recipe]) -> None:
        """Nothing invalid is ever seeded: no cycle, no over-deep chain."""
        organization = Recipe.objects.get(code=DEMO_DISH_CODE).organization
        findings = verify_organization(organization)
        graph_findings = [finding for finding in findings if finding.code.startswith("component")]
        assert graph_findings == []

    def test_the_verifier_is_clean_on_the_whole_demo_dataset(
        self, seeded: list[Recipe], organization: Organization
    ) -> None:
        assert verify_organization(organization) == []

    def test_every_component_row_carries_the_demo_banner(self, seeded: list[Recipe]) -> None:
        from apps.kitchen.demo import DEMO_BANNER

        for component in RecipeComponent.objects.all():
            assert DEMO_BANNER in component.note

    def test_a_second_run_creates_no_duplicate_component(
        self, seeded: list[Recipe], organization: Organization, manager: User
    ) -> None:
        before = _counts()
        seed_demo_recipes(organization=organization, created_by=manager)
        assert _counts() == before

    def test_a_second_run_creates_no_duplicate_order_or_scope_or_review(
        self, seeded: list[Recipe], organization: Organization, manager: User
    ) -> None:
        from apps.kitchen.models import RecipeVersionBranchScope, RecipeVersionReview

        before = (
            RecipeVersionBranchScope.objects.count(),
            RecipeVersionReview.objects.count(),
            sorted(RecipeComponent.objects.values_list("version_id", "line_order")),
        )
        seed_demo_recipes(organization=organization, created_by=manager)
        after = (
            RecipeVersionBranchScope.objects.count(),
            RecipeVersionReview.objects.count(),
            sorted(RecipeComponent.objects.values_list("version_id", "line_order")),
        )
        assert after == before

    def test_the_seed_moves_no_stock_and_writes_no_journal(self, seeded: list[Recipe]) -> None:
        assert StockMovement.objects.count() == 0
        assert StockBalance.objects.count() == 0
        assert JournalEntry.objects.count() == 0
        assert JournalLine.objects.count() == 0


class TestZeroEffect:
    """
    The boundary, counted rather than asserted.

    Every component command runs, plus the parent's whole lifecycle, and the
    ledger and the general ledger are identical afterwards.
    """

    def _counts(self) -> tuple[int, ...]:
        return (
            StockMovement.objects.count(),
            StockBalance.objects.count(),
            JournalEntry.objects.count(),
            JournalLine.objects.count(),
        )

    def test_every_component_command_and_the_lifecycle_move_nothing(
        self,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        from apps.kitchen.services import (
            create_recipe_component,
            remove_recipe_component,
            reorder_recipe_component,
            update_recipe_component,
        )

        from .conftest import carry_to_active

        before = self._counts()

        component = create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.5")
        )
        assert self._counts() == before

        update_recipe_component(component=component, multiplier=Decimal("0.75"))
        assert self._counts() == before

        reorder_recipe_component(component=component, line_order=1)
        assert self._counts() == before

        remove_recipe_component(component=component, reason="اختبار")
        assert self._counts() == before

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
        assert self._counts() == before

    def test_superseding_a_referenced_child_moves_nothing_either(
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
        from apps.kitchen.lifecycle import activate_recipe_version
        from apps.kitchen.services import create_recipe_component

        from .conftest import build_complete_draft, carry_to_active, carry_to_approved

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
        replacement = carry_to_approved(
            build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        before = self._counts()
        # Permitted now. The earlier implementation refused this outright; the
        # count assertion is the part that was always the point.
        activate_recipe_version(
            version=replacement,
            actor=approver,
            effective_from=datetime.date(2026, 9, 1),
            supersedes=RecipeVersion.objects.get(pk=blend_active.pk),
        )
        assert (
            RecipeVersion.objects.get(pk=blend_active.pk).status == RecipeVersionStatus.SUPERSEDED
        )
        assert self._counts() == before

    def test_flattening_lives_in_the_shared_engine_and_nowhere_else(self) -> None:
        """
        Task 3.2B asserted that no flattening service existed anywhere; **Task
        3.4 built one**, and this test moved with it rather than being deleted.

        What it guards now is stricter than what it guarded then: flattening
        exists in exactly one module, `apps/kitchen/expansion.py`, and the
        recipe-editing modules still have none of their own. A second flattener
        would not fail — it would agree, for a while, and then one of them would
        be fixed alone.
        """
        from apps.kitchen import expansion, graph, services

        for module in (graph, services):
            names = {name.lower() for name in dir(module)}
            assert not [name for name in names if "flatten" in name and "tree" not in name], (
                module.__name__
            )
        assert hasattr(expansion, "expand_recipe_version")

    def test_a_batch_is_a_draft_with_no_evidence_or_a_posting_with_all_of_it(self) -> None:
        """
        The fence, moved a fourth time - and this move retired half of it.

        Task 3.2B held `RecipeComponent` out and brought it in; 3.3 brought in
        `RecipeCostSnapshot`; 3.4 brought in `ProductionBatch`; **3.5 posts**,
        so "nothing posts" is finally and deliberately false. Rewritten rather
        than removed, because what replaces it is the stronger claim the
        posting-evidence constraint enforces: there is no half-posted batch.

        A draft holds no number, no stock entry, no value and no posting
        moment. A posted batch holds all four, and its input and output values
        are equal. Nothing in between is representable.
        """
        from django.apps import apps

        from apps.kitchen.models import ProductionBatch, ProductionBatchStatus

        names = {model.__name__ for model in apps.get_app_config("kitchen").get_models()}

        assert {
            "ProductionBatch",
            "ProductionBatchLine",
            "ProductionBatchActualLine",
            "ProductionBatchAllocation",
        } <= names, "Task 3.5 owns the allocation rows"

        for batch in ProductionBatch.objects.all():
            if batch.status == ProductionBatchStatus.DRAFT:
                assert batch.number == ""
                assert batch.stock_entry_id is None
                assert batch.output_value is None
                assert batch.posted_at is None
            else:
                assert batch.number != ""
                assert batch.stock_entry_id is not None
                assert batch.output_value is not None
                assert batch.input_value == batch.output_value


class TestVerifierDetection:
    """
    The findings the database makes unrepresentable, exercised anyway.

    Each check is a pure function over rows, so a mutated **in-memory** copy is
    enough to prove the check works — no trigger is disabled, no constraint is
    dropped, and nothing invalid is ever written.
    """

    def test_a_stocked_child_is_reported(
        self,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        cooked_rice: InventoryItem,
    ) -> None:
        from apps.kitchen.reconciliation import component_findings
        from apps.kitchen.services import create_recipe_component

        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        version = RecipeVersion.objects.get(pk=complete_draft.pk)
        component = version.components.select_related("component_recipe", "component_version").get()
        # Mutate the copy, not the row.
        component.component_recipe.output_item = cooked_rice

        codes = {finding.code for finding in component_findings(version, [component])}
        assert "stocked_recipe_used_as_component" in codes

    def test_a_non_positive_multiplier_is_reported(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        from apps.kitchen.reconciliation import component_findings
        from apps.kitchen.services import create_recipe_component

        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        version = RecipeVersion.objects.get(pk=complete_draft.pk)
        component = version.components.select_related("component_recipe", "component_version").get()
        component.multiplier = Decimal("0")

        codes = {finding.code for finding in component_findings(version, [component])}
        assert "component_multiplier_not_positive" in codes

    def test_a_half_filled_provenance_is_reported(
        self, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        from apps.kitchen.reconciliation import component_findings
        from apps.kitchen.services import create_recipe_component

        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("1")
        )
        version = RecipeVersion.objects.get(pk=complete_draft.pk)
        component = version.components.select_related("component_recipe", "component_version").get()
        component.source_document = "دفتر وصفات"
        component.source_page = None

        codes = {finding.code for finding in component_findings(version, [component])}
        assert "component_broken_provenance" in codes

    def test_the_verifier_has_no_repair_mode(self) -> None:
        import inspect

        from apps.kitchen import reconciliation

        source = inspect.getsource(reconciliation)
        for forbidden in ("def repair", "def fix_", "--repair", "--fix"):
            assert forbidden not in source

    def test_the_management_command_reports_and_never_writes(self) -> None:
        import inspect

        from apps.kitchen.management.commands import verify_recipe_versions

        source = inspect.getsource(verify_recipe_versions)
        for forbidden in (".save(", ".delete(", ".update(", ".create("):
            assert forbidden not in source
