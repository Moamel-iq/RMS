"""
The scale of a draft is revisable, and never independently mutable.

Migration 0011 deliberately leaves `multiplier` out of the frozen decision: how
much of a recipe to make is a thing an operator may change while the batch is a
draft. That permission is only safe because three figures must agree —

    ProductionBatch.multiplier
    ProductionBatch.expected_output_quantity
    ProductionBatchLine.planned_base_quantity   (every requirement)

— and migration 0015 checks them against each other at COMMIT. Without it, the
allowlist that makes a rescale possible would also make a batch possible that
claims to be double the recipe, expects a single output, and asks the kitchen for
one and a half times the rice.

## Why some of these force the deferred checks and one does not

A deferred constraint trigger fires at COMMIT. Inside an ordinary `django_db`
test the only COMMIT is teardown, long after the assertion, so most tests here
use `SET CONSTRAINTS ALL IMMEDIATE` — the idiom `accounting/tests/test_posting`
established for exactly this. One test does not: `transaction=True` and a real
commit, because "fires when forced" and "fires at a genuine commit boundary" are
two claims and the second is the one production depends on.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.core.quantity import quantize_calculation, quantize_factor
from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.models import ProductionBatch, Recipe, RecipeVersion
from apps.kitchen.production import (
    create_production_batch,
    preview_production_batch,
    rescale_production_batch,
    scaled_expected_output,
    scaled_line_quantity,
)
from apps.kitchen.services import create_recipe_component
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import (
    PRODUCTION_DATE,
    build_complete_draft,
    carry_to_active,
    codes_of,
    make_child_recipe,
)

pytestmark = pytest.mark.django_db


def force_deferred_checks() -> None:
    """Run the COMMIT-time triggers now, so an assertion can see them."""
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


# ---------------------------------------------------------------------------
# The invariant, from the outside
# ---------------------------------------------------------------------------


class TestAPartialRawRescaleCannotCommit:
    """
    Each of these changes one of the three figures and leaves the others. Every
    one is a coherent-looking single `UPDATE` and every one is refused.
    """

    def test_the_multiplier_alone_is_refused(self, production_draft: ProductionBatch) -> None:
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatch SET multiplier = 5 WHERE id = %s",
                [production_draft.pk],
            )
            force_deferred_checks()

    def test_the_expected_output_alone_is_refused(self, production_draft: ProductionBatch) -> None:
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatch SET expected_output_quantity = 99 WHERE id = %s",
                [production_draft.pk],
            )
            force_deferred_checks()

    def test_one_requirements_planned_quantity_alone_is_refused(
        self, production_draft: ProductionBatch
    ) -> None:
        line = production_draft.lines.first()
        assert line is not None
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatchline SET planned_base_quantity = "
                "planned_base_quantity + 1 WHERE id = %s",
                [line.pk],
            )
            force_deferred_checks()

    def test_a_header_rescale_that_misses_its_lines_is_refused(
        self, production_draft: ProductionBatch
    ) -> None:
        """
        The realistic mistake: a coherent header, untouched requirements.

        This is what a hand-written correction looks like, and it is the shape
        the trigger exists for — nothing about the header alone is wrong, and the
        batch it leaves behind is a plan no kitchen could execute.
        """
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatch b SET multiplier = 5, "
                "expected_output_quantity = round(v.expected_output_quantity * 5, 6) "
                "FROM kitchen_recipeversion v "
                "WHERE v.id = b.recipe_version_id AND b.id = %s",
                [production_draft.pk],
            )
            force_deferred_checks()

    def test_no_mixed_old_and_new_line_scale_may_commit(
        self,
        nested_draft: ProductionBatch,
    ) -> None:
        """
        Half the requirements rescaled is the failure a per-row check would miss.

        Deliberately on a batch with more than one requirement, and deliberately
        rescaling all but the last: the header agrees with most of the plan, which
        is precisely why "most" is not a state the database may hold.
        """
        assert nested_draft.lines.count() > 1
        last = nested_draft.lines.order_by("-line_order").first()
        assert last is not None
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatch b SET multiplier = 4, "
                "expected_output_quantity = round(v.expected_output_quantity * 4, 6) "
                "FROM kitchen_recipeversion v "
                "WHERE v.id = b.recipe_version_id AND b.id = %s",
                [nested_draft.pk],
            )
            cursor.execute(
                "UPDATE kitchen_productionbatchline SET planned_base_quantity = "
                "round(source_base_quantity * cumulative_multiplier * 4, 6) "
                "WHERE batch_id = %s AND id <> %s",
                [nested_draft.pk, last.pk],
            )
            force_deferred_checks()

    def test_a_complete_raw_rescale_commits(self, nested_draft: ProductionBatch) -> None:
        """
        The paired positive, so the tests above prove a rule rather than a wall.

        Every requirement, this time. If this failed, the trigger would be
        refusing the operation it exists to make safe.
        """
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatch b SET multiplier = 4, "
                "expected_output_quantity = round(v.expected_output_quantity * 4, 6) "
                "FROM kitchen_recipeversion v "
                "WHERE v.id = b.recipe_version_id AND b.id = %s",
                [nested_draft.pk],
            )
            cursor.execute(
                "UPDATE kitchen_productionbatchline SET planned_base_quantity = "
                "round(source_base_quantity * cumulative_multiplier * 4, 6) "
                "WHERE batch_id = %s",
                [nested_draft.pk],
            )
            force_deferred_checks()
        assert ProductionBatch.objects.get(pk=nested_draft.pk).multiplier == Decimal("4.000000")


class TestTheApprovedCommandSatisfiesTheInvariant:
    def test_an_ordinary_rescale_commits_and_agrees(self, nested_draft: ProductionBatch) -> None:
        rescaled = rescale_production_batch(batch=nested_draft, multiplier=Decimal("3.25"))
        force_deferred_checks()

        assert rescaled.expected_output_quantity == scaled_expected_output(
            version_output=rescaled.recipe_version.expected_output_quantity,
            multiplier=Decimal("3.250000"),
        )
        for line in rescaled.lines.all():
            assert line.planned_base_quantity == scaled_line_quantity(
                source_base_quantity=line.source_base_quantity,
                cumulative_multiplier=line.cumulative_multiplier,
                multiplier=Decimal("3.250000"),
            )

    def test_reset_and_rescale_follows_the_same_invariant(
        self, nested_draft: ProductionBatch, manager: User
    ) -> None:
        """Resetting the actuals changes what is discarded, never what must agree."""
        from apps.kitchen.production import update_production_batch_actuals

        row = nested_draft.lines.first()
        assert row is not None
        actual = row.actuals.get()
        update_production_batch_actuals(
            actual=actual, entered_quantity=Decimal("99"), actor=manager
        )

        rescaled = rescale_production_batch(
            batch=nested_draft,
            multiplier=Decimal("1.5"),
            actor=manager,
            reset_actuals=True,
            reason="أعيد الضبط بعد تغيير الحجم.",
        )
        force_deferred_checks()
        for line in rescaled.lines.all():
            assert line.planned_base_quantity == scaled_line_quantity(
                source_base_quantity=line.source_base_quantity,
                cumulative_multiplier=line.cumulative_multiplier,
                multiplier=Decimal("1.500000"),
            )

    def test_a_refused_rescale_leaves_every_figure_untouched(
        self, nested_draft: ProductionBatch, manager: User
    ) -> None:
        """
        A rescale refused mid-way must roll all of it back.

        `_touched` refuses after an actual edit, and it refuses *after* the header
        would have been written in a naive implementation — so this is the test
        that the whole command is one transaction rather than a header update
        followed by a change of mind.
        """
        from apps.kitchen.production import update_production_batch_actuals

        row = nested_draft.lines.first()
        assert row is not None
        update_production_batch_actuals(
            actual=row.actuals.get(), entered_quantity=Decimal("7"), actor=manager
        )
        before = {line.pk: line.planned_base_quantity for line in nested_draft.lines.all()}
        before_multiplier = nested_draft.multiplier

        with pytest.raises(ValidationError):
            rescale_production_batch(batch=nested_draft, multiplier=Decimal("9"))

        after = ProductionBatch.objects.get(pk=nested_draft.pk)
        assert after.multiplier == before_multiplier
        for line in after.lines.all():
            assert line.planned_base_quantity == before[line.pk]

    def test_a_discard_is_not_refused_by_the_deferred_check(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        The reason the trigger re-reads instead of trusting its captured tuple.

        An edit and then a discard in one transaction leaves a deferred event for a
        row that no longer exists. Validating the captured tuple would refuse the
        discard on account of a batch nobody is committing.
        """
        from apps.kitchen.production import (
            discard_production_batch,
            update_production_batch_actuals,
        )

        line = production_draft.lines.first()
        assert line is not None
        with transaction.atomic():
            update_production_batch_actuals(
                actual=line.actuals.get(), entered_quantity=Decimal("3"), actor=manager
            )
            discard_production_batch(batch=production_draft, actor=manager, reason="أُلغيت الدفعة.")
            force_deferred_checks()
        assert not ProductionBatch.objects.filter(pk=production_draft.pk).exists()


class TestARealCommitBoundary:
    """
    `SET CONSTRAINTS ALL IMMEDIATE` proves the check runs. This proves it runs
    where production actually reaches it: at a genuine COMMIT, with nothing
    forcing it.
    """

    @pytest.mark.django_db(transaction=True)
    def test_an_inconsistent_raw_rescale_cannot_commit(
        self, production_draft: ProductionBatch
    ) -> None:
        with pytest.raises(IntegrityError):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch SET multiplier = 6 WHERE id = %s",
                    [production_draft.pk],
                )
            # No SET CONSTRAINTS: leaving the atomic block is the commit, and the
            # commit is what refuses.
        assert ProductionBatch.objects.get(pk=production_draft.pk).multiplier == Decimal("2.500000")


# ---------------------------------------------------------------------------
# The arithmetic the trigger mirrors
# ---------------------------------------------------------------------------


class TestThePythonAndSqlArithmeticAgree:
    """
    The trigger recomputes in SQL what the service computed in Python. Two
    implementations of one formula agree until one of them is written by somebody
    who did not know about the other, so this compares them directly.
    """

    @pytest.mark.parametrize(
        ("source", "cumulative", "multiplier"),
        [
            ("4.000000", "1.000000000000", "2.500000"),
            # A third: 12 places of stored cumulative against a 6-place scale.
            ("4.000000", "0.333333333333", "2.500000"),
            # A tie at the sixth decimal, which is where ROUND_HALF_UP and
            # PostgreSQL's round() would part company if either were half-even.
            ("0.000001", "0.500000000000", "1.000000"),
            ("1.000005", "1.000000000000", "0.500000"),
            # Deliberately awkward: nothing here terminates neatly.
            ("2.750000", "0.083333333333", "7.125000"),
            ("999.999999", "1.234567890123", "3.333333"),
        ],
    )
    def test_the_scaled_line_quantity_matches_postgresql(
        self, source: str, cumulative: str, multiplier: str
    ) -> None:
        python = scaled_line_quantity(
            source_base_quantity=Decimal(source),
            cumulative_multiplier=Decimal(cumulative),
            multiplier=Decimal(multiplier),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT round(%s::numeric * %s::numeric * %s::numeric, 6)",
                [source, cumulative, multiplier],
            )
            row = cursor.fetchone()
        assert row is not None
        assert python == row[0], f"{source} × {cumulative} × {multiplier}"

    def test_the_expected_output_matches_postgresql(self) -> None:
        python = scaled_expected_output(
            version_output=Decimal("10.000000"), multiplier=Decimal("2.500000")
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT round(10.000000::numeric * 2.500000::numeric, 6)")
            row = cursor.fetchone()
        assert row is not None
        assert python == row[0]

    def test_the_multiplier_is_quantized_before_it_is_used(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        manager: User,
    ) -> None:
        """
        A multiplier finer than the column scales the batch as **stored**.

        `2.5000005` stores as `2.500001`. Before this was fixed, the expected
        output was computed from the figure the caller sent and the row therefore
        recorded an output no multiplier on it could produce — invisible until the
        consistency trigger refused the next honest rescale.
        """
        batch = create_production_batch(
            recipe=batch_recipe[0],
            branch=branch,
            warehouse=store,
            planned_business_date=PRODUCTION_DATE,
            multiplier=Decimal("2.5000005"),
            actor=manager,
            idempotency_key="FINE-SCALE",
        )
        force_deferred_checks()
        assert batch.multiplier == Decimal("2.500001")
        assert batch.expected_output_quantity == scaled_expected_output(
            version_output=batch.recipe_version.expected_output_quantity,
            multiplier=Decimal("2.500001"),
        )

    def test_a_multiplier_below_the_stored_precision_is_a_named_refusal(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        manager: User,
    ) -> None:
        """Not a 500 from a check constraint it would silently round into."""
        with pytest.raises(ValidationError) as exc:
            create_production_batch(
                recipe=batch_recipe[0],
                branch=branch,
                warehouse=store,
                planned_business_date=PRODUCTION_DATE,
                multiplier=Decimal("0.0000001"),
                actor=manager,
                idempotency_key="TOO-FINE",
            )
        assert "production_batch_multiplier_below_precision" in codes_of(exc.value)


# ---------------------------------------------------------------------------
# Creation, rescale and preview agree with each other
# ---------------------------------------------------------------------------


@pytest.fixture
def nested_draft(
    organization: Organization,
    branch: Branch,
    store: Warehouse,
    cooked_rice: InventoryItem,
    kilogram: UnitOfMeasure,
    litre: UnitOfMeasure,
    rice: InventoryItem,
    oil: InventoryItem,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> ProductionBatch:
    """
    A draft of a **two-level** recipe with awkward component multipliers.

    `0.333333333333` on the way down is the case that matters: the walk composes
    the cumulative multiplier at full precision and its column holds twelve
    places, so this is the shape where scaling by the unrounded product and
    storing the rounded one produced two different plans for one batch.
    """
    from apps.kitchen.services import create_recipe, set_recipe_branches

    leaf = make_child_recipe(organization=organization, code="SCALE-LEAF", author=manager)
    carry_to_active(
        # Litres, because the leaf's item is oil and a line is entered in a unit
        # of its item's own dimension.
        build_complete_draft(recipe=leaf, unit=litre, item=oil, author=manager),
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
        effective_from=datetime.date(2025, 1, 1),
    )

    middle = make_child_recipe(organization=organization, code="SCALE-MID", author=manager)
    middle_draft = build_complete_draft(recipe=middle, unit=kilogram, item=rice, author=manager)
    create_recipe_component(
        version=middle_draft,
        component_version=RecipeVersion.objects.filter(recipe=leaf).get(),
        multiplier=Decimal("0.333333333333"),
        actor=manager,
    )
    carry_to_active(
        middle_draft,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
        effective_from=datetime.date(2025, 1, 1),
    )

    root = create_recipe(
        organization=organization,
        code="SCALE-ROOT",
        name="جذر القياس",
        recipe_type="BATCH",
        output_item=cooked_rice,
        created_by=manager,
    )
    set_recipe_branches(recipe=root, branches=[branch])
    root_draft = build_complete_draft(recipe=root, unit=kilogram, item=rice, author=manager)
    create_recipe_component(
        version=root_draft,
        component_version=RecipeVersion.objects.filter(recipe=middle).get(),
        multiplier=Decimal("0.777777777777"),
        actor=manager,
    )
    carry_to_active(
        root_draft,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
        effective_from=datetime.date(2026, 1, 1),
    )

    return create_production_batch(
        recipe=root,
        branch=branch,
        warehouse=store,
        planned_business_date=PRODUCTION_DATE,
        multiplier=Decimal("2.5"),
        actor=manager,
        idempotency_key="NESTED-SCALE-1",
    )


class TestCreationRescaleAndPreviewAgree:
    def test_a_rescale_to_the_multiplier_it_already_has_changes_nothing(
        self, nested_draft: ProductionBatch
    ) -> None:
        """
        The defect the consistency trigger surfaced, as its own test.

        Creation scaled by the cumulative multiplier the walk computed — up to
        thirty-six places for a two-level recipe — and stored twelve. A rescale
        reads the stored twelve. So rescaling to the multiplier the batch already
        had used to *move* the planned quantities, silently, on exactly the recipes
        where the arithmetic is hardest to check by eye.
        """
        before = {
            line.component_path: line.planned_base_quantity for line in nested_draft.lines.all()
        }
        rescaled = rescale_production_batch(batch=nested_draft, multiplier=Decimal("2.5"))
        force_deferred_checks()
        after = {line.component_path: line.planned_base_quantity for line in rescaled.lines.all()}
        assert after == before

    def test_the_stored_cumulative_multiplier_is_what_the_plan_was_scaled_by(
        self, nested_draft: ProductionBatch
    ) -> None:
        for line in nested_draft.lines.all():
            assert line.cumulative_multiplier == quantize_factor(line.cumulative_multiplier)
            assert line.planned_base_quantity == quantize_calculation(
                line.source_base_quantity * line.cumulative_multiplier * nested_draft.multiplier
            )

    def test_the_preview_promises_what_creation_writes(
        self,
        nested_draft: ProductionBatch,
        branch: Branch,
    ) -> None:
        """
        A preview computed a second way is a preview that can disagree with the
        batch it previews. Compared per path, because the whole point of two
        levels is that the paths are what distinguish the requirements.
        """
        preview = preview_production_batch(
            recipe=nested_draft.recipe,
            branch=branch,
            planned_business_date=nested_draft.planned_business_date,
            multiplier=nested_draft.multiplier,
        )
        promised = {leaf.path_display: quantity for leaf, quantity in preview.planned}
        written = {
            line.component_path: line.planned_base_quantity for line in nested_draft.lines.all()
        }
        assert promised == written
        assert preview.expected_output_quantity == nested_draft.expected_output_quantity
