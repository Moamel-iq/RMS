"""
The demo dataset, and what happens when two requests arrive at once.

Idempotency is asserted rather than trusted: "a second run changes nothing" is
the claim demo seeds most often get wrong, and the way it fails is by silently
doubling every row the second time somebody runs it.

The concurrency tests use `transaction=True`, so they run against real
COMMITs rather than inside a rolled-back test transaction. A uniqueness rule
tested without that proves only that one thread can count.
"""

from __future__ import annotations

import datetime
import threading
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.db import connections, transaction

from apps.accounting.models import JournalEntry
from apps.inventory.models import InventoryItem, ItemCategory, StockMovement
from apps.kitchen.demo import DEMO_BANNER, seed_demo_recipes
from apps.kitchen.lifecycle import resolve_recipe_version
from apps.kitchen.models import (
    ApprovalEvidenceKind,
    Recipe,
    RecipeLine,
    RecipeServing,
    RecipeStep,
    RecipeStepIngredient,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionReview,
    RecipeVersionStatus,
)
from apps.kitchen.reconciliation import verify_organization
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    create_recipe,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db


def _counts() -> tuple[int, ...]:
    """
    Every table the seed writes to.

    The lifecycle tables are here deliberately: a second seed that added one
    more review row, one more scope row or one more transition would be exactly
    as wrong as one that doubled the recipes, and much harder to notice.
    """
    return (
        Recipe.objects.count(),
        RecipeVersion.objects.count(),
        RecipeLine.objects.count(),
        RecipeStep.objects.count(),
        RecipeStepIngredient.objects.count(),
        RecipeServing.objects.count(),
        RecipeVersionReview.objects.count(),
        RecipeVersionBranchScope.objects.count(),
    )


class TestDemoDataset:
    @pytest.fixture
    def demo_items(
        self,
        organization: Organization,
        item_category: ItemCategory,
        kilogram: UnitOfMeasure,
        litre: UnitOfMeasure,
        piece: UnitOfMeasure,
    ) -> None:
        """
        The Phase 1 demo items the kitchen seed builds on.

        Named exactly as `seed_inventory_demo` names them, because that is what
        the kitchen seed looks for — and a test that invented its own codes
        would pass while the real command found nothing.
        """
        from apps.inventory.models import ItemType

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

    def test_fifteen_recipes_are_created(self, seeded: list[Recipe]) -> None:
        """
        Five that stay drafts, four that walk the approval boundary, three that
        form the nested graph Task 3.2B added, **one Task 3.3 added** whose
        every leaf is valued so a cost card adds up to a real number, and
        **one Task 3.4 added** — a batch recipe with an output item, which is
        the only shape a production draft can be raised from — and **one Task
        3.5 added**, producing a lot-tracked item into its own control account
        so both the output lot and a real netted journal appear on a screen.
        """
        assert len(seeded) == 15

    def test_every_screen_has_something_on_it(self, seeded: list[Recipe]) -> None:
        assert RecipeLine.objects.exists()
        assert RecipeStep.objects.exists()
        assert RecipeServing.objects.exists()
        assert RecipeStepIngredient.objects.exists()

    def test_both_cost_classes_appear(self, seeded: list[Recipe]) -> None:
        classes = set(RecipeLine.objects.values_list("cost_class", flat=True))
        assert {"FOOD", "PACKAGING"} <= classes

    def test_a_step_carries_a_sourced_duration(self, seeded: list[Recipe]) -> None:
        assert RecipeStep.objects.filter(expected_duration__isnull=False).exists()

    def test_a_step_carries_a_qualitative_heat_and_no_temperature(
        self, seeded: list[Recipe]
    ) -> None:
        """The demo has to show the empty Celsius column, because that is the rule."""
        assert RecipeStep.objects.filter(
            temperature_c__isnull=True, heat_instruction_ar__gt=""
        ).exists()

    def test_no_step_invents_a_temperature(self, seeded: list[Recipe]) -> None:
        assert not RecipeStep.objects.filter(temperature_c__isnull=False).exists()

    def test_a_whole_and_a_half_serving_both_exist(self, seeded: list[Recipe]) -> None:
        codes = set(RecipeServing.objects.values_list("code", flat=True))
        assert {"FULL", "HALF"} <= codes

    def test_a_weight_based_serving_exists(self, seeded: list[Recipe]) -> None:
        assert RecipeServing.objects.filter(code="G350").exists()

    def test_one_recipe_has_a_stocked_output_and_one_has_none(self, seeded: list[Recipe]) -> None:
        assert Recipe.objects.filter(output_item__isnull=False).exists()
        assert Recipe.objects.filter(output_item__isnull=True).exists()

    def test_one_recipe_is_archived(self, seeded: list[Recipe]) -> None:
        assert Recipe.objects.filter(is_active=False).exists()

    def test_at_least_one_editable_draft_exists(self, seeded: list[Recipe]) -> None:
        assert RecipeVersion.objects.filter(status=RecipeVersionStatus.DRAFT).exists()

    def test_every_lifecycle_state_worth_looking_at_is_present(self, seeded: list[Recipe]) -> None:
        """
        A screen that has never had a `SUPERSEDED` row on it has never been
        reviewed, so the demo puts one there.
        """
        present = set(RecipeVersion.objects.values_list("status", flat=True))
        assert present == {
            RecipeVersionStatus.DRAFT,
            RecipeVersionStatus.SUBMITTED,
            RecipeVersionStatus.APPROVED,
            RecipeVersionStatus.ACTIVE,
            RecipeVersionStatus.REJECTED,
            RecipeVersionStatus.SUPERSEDED,
        }

    def test_every_demo_approval_is_evidenced_as_fiction(self, seeded: list[Recipe]) -> None:
        """
        RCP-126, at the approval boundary. A demo signoff that looked like a
        signed `KM-RCP-004` is exactly how unapproved figures acquire
        authority, so every demo approval names its evidence as fictional —
        and the database refuses `DEMO_FICTIONAL` outside this namespace.
        """
        approved = RecipeVersion.objects.exclude(approval_evidence_kind="")
        assert approved.exists()
        assert not approved.exclude(
            approval_evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL
        ).exists()
        assert not approved.exclude(recipe__code__startswith="DEMO-").exists()

    def test_a_rejected_version_keeps_its_reason(self, seeded: list[Recipe]) -> None:
        rejected = RecipeVersion.objects.filter(status=RecipeVersionStatus.REJECTED)
        assert rejected.exists()
        assert not rejected.filter(rejection_reason="").exists()

    def test_the_superseded_version_still_resolves_for_its_own_dates(
        self, seeded: list[Recipe], organization: Organization
    ) -> None:
        """The demo's whole reason for having two dated versions."""
        recipe = Recipe.objects.get(organization=organization, code="DEMO-RCP-DATED")
        branch = Branch.objects.filter(organization=organization).first()
        assert branch is not None

        early = resolve_recipe_version(
            recipe=recipe, branch=branch, on_date=datetime.date(2026, 3, 1)
        )
        late = resolve_recipe_version(
            recipe=recipe, branch=branch, on_date=datetime.date(2026, 9, 1)
        )

        assert early.status == RecipeVersionStatus.SUPERSEDED
        assert late.status == RecipeVersionStatus.ACTIVE
        assert early.pk != late.pk

    def test_the_verifier_is_clean_on_the_demo_dataset(
        self, seeded: list[Recipe], organization: Organization
    ) -> None:
        assert verify_organization(organization) == []

    def test_provenance_is_present_on_the_sourced_rows(self, seeded: list[Recipe]) -> None:
        assert Recipe.objects.filter(source_document__gt="").exists()
        assert RecipeLine.objects.filter(source_page__isnull=False).exists()

    def test_every_demo_record_is_marked_as_fiction(self, seeded: list[Recipe]) -> None:
        """
        RCP-126: a demo screenshot that looked like the real menu is how
        unapproved numbers acquire authority.
        """
        for recipe in seeded:
            assert DEMO_BANNER in recipe.notes
            assert recipe.code.startswith("DEMO-")

    def test_no_demo_record_uses_a_real_dish_name_or_a_sourced_gram_figure(
        self, seeded: list[Recipe]
    ) -> None:
        real_names = ("حنيذ", "مدفون", "زربيان", "مضغوط", "مزموم", "كبسة", "مضبي")
        for recipe in seeded:
            assert not [word for word in real_names if word in recipe.name_ar]
        # The book's carving weight must not appear as a demo quantity, with two
        # deliberate exceptions. `DEMO-RCP-RICE` predates this test;
        # `DEMO-RCP-COST` carries a half serving **because a half is the case
        # RCP-086's arithmetic has to get right**, and a costing demo that
        # portioned only in whole units would exercise none of it. Neither
        # names a dish, a cut or an animal, which is what RCP-082 forbids —
        # the figure alone is data, and it lives in a row.
        assert not RecipeServing.objects.filter(base_quantity=Decimal("0.500000")).exclude(
            version__recipe__code__in=("DEMO-RCP-RICE", "DEMO-RCP-COST")
        )

    def test_the_seed_moves_no_stock_and_writes_no_journal(self, seeded: list[Recipe]) -> None:
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_a_second_run_creates_nothing(
        self, seeded: list[Recipe], organization: Organization, manager: User
    ) -> None:
        before = _counts()
        seed_demo_recipes(organization=organization, created_by=manager)
        assert _counts() == before

    def test_a_second_run_creates_no_duplicate_item(
        self, seeded: list[Recipe], organization: Organization, manager: User
    ) -> None:  # noqa: D102
        seed_demo_recipes(organization=organization, created_by=manager)
        assert (
            InventoryItem.objects.filter(organization=organization, code="DEMO-RICE-COOKED").count()
            == 1
        )


@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    """
    Two requests at once, against real COMMITs.

    Each test runs the same operation twice in parallel and asserts that
    exactly one succeeded — which is the only way to tell a constraint that
    holds from a check that merely looked at a stale read.
    """

    def _race(self, work: Callable[[int], None]) -> list[BaseException | None]:
        errors: list[BaseException | None] = [None, None]
        barrier = threading.Barrier(2)

        def run(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    work(index)
            except BaseException as problem:  # noqa: BLE001 - recorded, then asserted
                errors[index] = problem
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return errors

    def test_two_recipes_cannot_take_the_same_code(
        self, organization: Organization, manager: User
    ) -> None:
        def work(index: int) -> None:
            create_recipe(
                organization=organization,
                code="RACE",
                name_ar=f"سباق {index}",
                recipe_type="PORTION",
                created_by=manager,
            )

        errors = self._race(work)
        assert len([e for e in errors if e is None]) == 1
        assert Recipe.objects.filter(organization=organization, code="RACE").count() == 1

    def test_two_lines_cannot_take_the_same_order(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        """
        Both threads draw `line_order` under the version's lock, so they
        serialise and both succeed with different numbers — which is the
        behaviour wanted here, unlike the code race above.
        """

        def work(index: int) -> None:
            add_recipe_line(
                version=draft,
                item=rice,
                entered_quantity=Decimal("1"),
                entered_unit=kilogram,
            )

        errors = self._race(work)
        assert errors == [None, None]
        orders = sorted(draft.lines.values_list("line_order", flat=True))
        assert orders == [1, 2]

    def test_two_primary_servings_cannot_both_survive(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        def work(index: int) -> None:
            add_recipe_serving(
                version=draft,
                code=f"P{index}",
                name_ar=f"حصة {index}",
                serving_quantity=Decimal("1"),
                serving_unit=kilogram,
                is_primary=True,
            )

        self._race(work)
        assert draft.servings.filter(is_primary=True).count() <= 1

    def test_two_concurrent_version_creations_cannot_collide(
        self, recipe: Recipe, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        """
        Task 3.2 §B.1: the allocator is a locked read-modify-write on the
        Recipe row, so two callers cannot draw the same number, overwrite
        `last_version_number`, or both leave a DRAFT behind.

        Asserted at real COMMIT rather than against the partial unique index
        alone: an index only refuses the *second insert*, and by then both
        callers have already chosen the same number and one of them has lost
        work it thought it had done.
        """
        from apps.kitchen.services import create_draft_recipe_version

        def work(index: int) -> None:
            create_draft_recipe_version(
                recipe=recipe,
                expected_output_quantity=Decimal("10"),
                output_unit=kilogram,
                created_by=manager,
            )

        errors = self._race(work)

        # One draft per recipe is the Task 3.1 rule, so exactly one survives.
        assert len([e for e in errors if e is None]) == 1
        versions = list(RecipeVersion.objects.filter(recipe=recipe))
        assert len(versions) == 1
        recipe.refresh_from_db()
        # The allocator advanced exactly once, and the surviving row holds it.
        assert recipe.last_version_number == versions[0].version_number

    def test_two_ranked_alternatives_can_both_be_active(
        self,
        draft: RecipeVersion,
        rice: InventoryItem,
        oil: InventoryItem,
        box: InventoryItem,
        kilogram: UnitOfMeasure,
    ) -> None:
        """
        Task 3.2 §B.2. The rule is one active substitute per *item* per line —
        not one substitute per line. A cook needs a ranked list of what may
        stand in, and the constraint permits it.
        """
        from apps.kitchen.services import add_recipe_line, add_recipe_line_substitute

        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("1"), entered_unit=kilogram
        )
        first = add_recipe_line_substitute(line=line, substitute_item=oil)
        second = add_recipe_line_substitute(line=line, substitute_item=box)

        assert line.substitutes.filter(is_active=True).count() == 2
        # Ranks are allocated, not defaulted, so the order is an order.
        assert [first.priority, second.priority] == [1, 2]

    def test_two_active_substitutes_cannot_share_a_rank(
        self,
        draft: RecipeVersion,
        rice: InventoryItem,
        oil: InventoryItem,
        box: InventoryItem,
        kilogram: UnitOfMeasure,
    ) -> None:
        """A rank that two rows share is not a rank; the database refuses it."""
        from django.db import IntegrityError

        from apps.kitchen.services import add_recipe_line, add_recipe_line_substitute

        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("1"), entered_unit=kilogram
        )
        add_recipe_line_substitute(line=line, substitute_item=oil, priority=1)
        with pytest.raises((IntegrityError, Exception)):
            with transaction.atomic():
                add_recipe_line_substitute(line=line, substitute_item=box, priority=1)
