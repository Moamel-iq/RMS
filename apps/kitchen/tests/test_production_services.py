"""
The production drafting commands, the readiness report, and the boundary.

Three groups, and the third is the one Task 3.4 is defined by:

* **The commands.** Every act an operator performs on a draft — record what was
  consumed, add an approved stand-in, substitute completely, rescale, reset,
  enter the output, discard.
* **Readiness.** Derived, never stored; every problem at once; no stock query.
* **Zero effect.** A census before and after the whole scenario, on every table
  a posting would touch. The point of Task 3.4 is that this census does not
  move, and a boundary asserted only in prose is a boundary somebody crosses.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.accounting.models import JournalEntry, JournalLine
from apps.core.models import AuditAction, AuditEvent
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    PackageUnit,
    StockBalance,
    StockLedgerEntry,
    StockLocationBalance,
    StockMovement,
    Warehouse,
)
from apps.kitchen.models import (
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchStatus,
    Recipe,
    RecipeCostSnapshot,
    RecipeLineSubstitute,
    RecipeVersion,
)
from apps.kitchen.production import (
    add_production_batch_substitute,
    comparable_consumption,
    consumption_comparisons,
    create_production_batch,
    discard_production_batch,
    has_recorded_consumption,
    production_batch_is_ready,
    production_batch_readiness,
    record_production_output,
    remove_production_batch_substitute,
    rescale_production_batch,
    update_production_batch_actuals,
    update_production_batch_notes,
    validate_production_batch_ready,
)
from apps.kitchen.production_reconciliation import batch_findings
from apps.organizations.models import Branch
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import PRODUCTION_DATE, codes_of

pytestmark = pytest.mark.django_db


def codes(problems: Any) -> set[str]:
    return {item.code for item in problems}


def ready(batch: ProductionBatch, manager: User, output_unit: UnitOfMeasure) -> ProductionBatch:
    """Bring a draft to the point where readiness has nothing left to say."""
    record_production_output(
        batch=batch, entered_quantity=Decimal("9"), entered_unit=output_unit, actor=manager
    )
    return ProductionBatch.objects.get(pk=batch.pk)


# ---------------------------------------------------------------------------
# Recording what was actually consumed
# ---------------------------------------------------------------------------


class TestActualConsumption:
    def test_a_variance_is_recorded_and_never_refused(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        RCP-030. Refusing a variance teaches kitchens to falsify quantities to
        match the recipe, which is the one outcome that makes the module useless.
        """
        actual = production_draft.lines.get().actuals.get()
        more = update_production_batch_actuals(
            actual=actual, entered_quantity=Decimal("99"), actor=manager
        )
        assert more.base_quantity == Decimal("99.000000")

        less = update_production_batch_actuals(
            actual=more, entered_quantity=Decimal("0.5"), actor=manager
        )
        assert less.base_quantity == Decimal("0.500000")

    def test_a_negative_quantity_is_refused(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """Zero is a fact; consuming minus two kilos is not."""
        actual = production_draft.lines.get().actuals.get()
        with pytest.raises(ValidationError) as exc:
            update_production_batch_actuals(
                actual=actual, entered_quantity=Decimal("-1"), actor=manager
            )
        assert "value_is_negative" in codes_of(exc.value)

    def test_a_float_is_refused_outright(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """`0.1` is not 0.1 in binary, and a quantity path must never see one."""
        actual = production_draft.lines.get().actuals.get()
        with pytest.raises(ValidationError) as exc:
            update_production_batch_actuals(actual=actual, entered_quantity=0.1, actor=manager)
        assert "float_not_permitted" in codes_of(exc.value)

    def test_an_entry_in_grams_converts_once_and_snapshots_its_factor(
        self, production_draft: ProductionBatch, gram: UnitOfMeasure, manager: User
    ) -> None:
        actual = production_draft.lines.get().actuals.get()
        updated = update_production_batch_actuals(
            actual=actual,
            entered_quantity=Decimal("350"),
            entered_unit=gram,
            actor=manager,
        )

        assert updated.base_quantity == Decimal("0.350000")
        assert updated.conversion_factor == Decimal("0.001000000000")

    def test_a_package_entry_freezes_the_sack_size(
        self,
        production_draft: ProductionBatch,
        sack: PackageUnit,
        manager: User,
    ) -> None:
        actual = production_draft.lines.get().actuals.get()
        updated = update_production_batch_actuals(
            actual=actual,
            entered_quantity=Decimal("2"),
            package_unit=sack,
            actor=manager,
        )

        assert updated.base_quantity == Decimal("60.000000")
        assert updated.conversion_factor == Decimal("30.000000000000")
        assert updated.conversion_version is not None

    def test_a_variable_package_needs_a_measurement_and_never_invents_one(
        self,
        substituted_draft: ProductionBatch,
        drum: PackageUnit,
        oil: InventoryItem,
        manager: User,
    ) -> None:
        """
        One meat container is whatever it weighed. There is no arithmetic answer,
        and inventing one would put a weight in the database no scale produced.
        """
        line = substituted_draft.lines.get()
        with pytest.raises(ValidationError) as exc:
            add_production_batch_substitute(
                line=line,
                item=oil,
                entered_quantity=Decimal("1"),
                package_unit=drum,
                actor=manager,
            )
        assert "production_actual_variable_package_needs_a_measurement" in codes_of(exc.value)

    def test_both_entry_modes_at_once_is_refused(
        self,
        production_draft: ProductionBatch,
        sack: PackageUnit,
        kilogram: UnitOfMeasure,
        manager: User,
    ) -> None:
        actual = production_draft.lines.get().actuals.get()
        with pytest.raises(ValidationError) as exc:
            update_production_batch_actuals(
                actual=actual,
                entered_quantity=Decimal("1"),
                entered_unit=kilogram,
                package_unit=sack,
                actor=manager,
            )
        assert "production_actual_one_entry_mode" in codes_of(exc.value)


class TestOnlyBatchRecipesAreProducible:
    """
    RCP-032. Producing a portion recipe would create stock of an item that
    deliberately does not exist.
    """

    def test_a_recipe_without_an_output_item_cannot_be_drafted(
        self,
        active_version: RecipeVersion,
        branch: Branch,
        store: Warehouse,
        manager: User,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            create_production_batch(
                recipe=active_version.recipe,
                branch=branch,
                warehouse=store,
                planned_business_date=PRODUCTION_DATE,
                multiplier=Decimal("1"),
                actor=manager,
                idempotency_key="NO-OUTPUT",
            )
        assert "production_batch_recipe_has_no_output_item" in codes_of(exc.value)

    def test_the_create_screen_offers_only_producible_recipes(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        active_version: RecipeVersion,
        manager: User,
    ) -> None:
        """
        A courtesy, not the control — the service refuses the same shape by name.
        But offering a recipe that will be refused on submit teaches an operator
        that the system is arbitrary.
        """
        from apps.kitchen.selectors import draftable_recipes

        offered = set(
            draftable_recipes(manager, batch_recipe[0].organization).values_list("code", flat=True)
        )

        assert batch_recipe[0].code in offered
        assert active_version.recipe.code not in offered


class TestSubstitution:
    def test_a_partial_substitution_is_two_rows_about_one_requirement(
        self, substituted_draft: ProductionBatch, barley: InventoryItem, manager: User
    ) -> None:
        """
        3 kg of the primary plus 1 kg of a stand-in is two facts, and the caller
        decides what the primary becomes. Nothing reduces it automatically,
        because "the rest was substituted" is an assumption.
        """
        line = substituted_draft.lines.get()
        primary = line.actuals.get()
        update_production_batch_actuals(
            actual=primary, entered_quantity=Decimal("3"), actor=manager
        )
        add_production_batch_substitute(
            line=line,
            item=barley,
            entered_quantity=Decimal("1"),
            actor=manager,
            reason="نقص في السوق",
        )

        rows = list(line.actuals.order_by("entry_order"))
        assert len(rows) == 2
        assert [row.kind for row in rows] == ["PRIMARY", "SUBSTITUTE"]
        # Both are MASS, so this requirement *does* have a comparable figure.
        assert comparable_consumption(line) == Decimal("4.000000")

    def test_a_complete_substitution_may_remove_the_primary_row(
        self, substituted_draft: ProductionBatch, barley: InventoryItem, manager: User
    ) -> None:
        """
        The kitchen used none of the planned item. Forcing a zero row to remain
        would force a statement about an item that never entered the pot.
        """
        line = substituted_draft.lines.get()
        primary = line.actuals.get()
        add_production_batch_substitute(
            line=line, item=barley, entered_quantity=Decimal("12"), actor=manager, reason="كامل"
        )
        remove_production_batch_substitute(actual=primary, actor=manager, reason="استبدال كامل")

        rows = list(line.actuals.all())
        assert len(rows) == 1
        assert rows[0].item_id == barley.pk
        assert has_recorded_consumption(line)

    def test_two_ranked_substitutes_may_both_be_used(
        self,
        substituted_draft: ProductionBatch,
        barley: InventoryItem,
        oil: InventoryItem,
        manager: User,
    ) -> None:
        line = substituted_draft.lines.get()
        add_production_batch_substitute(
            line=line, item=barley, entered_quantity=Decimal("1"), actor=manager
        )
        add_production_batch_substitute(
            line=line, item=oil, entered_quantity=Decimal("2"), actor=manager
        )

        assert line.actuals.count() == 3
        approvals = list(
            RecipeLineSubstitute.objects.filter(line=line.source_line).order_by("priority")
        )
        assert [row.substitute_item.code for row in approvals] == ["BARLEY", "OIL"]

    def test_an_unapproved_item_is_refused(
        self, substituted_draft: ProductionBatch, box: InventoryItem, manager: User
    ) -> None:
        """A variance is a fact. A different item is a different recipe."""
        line = substituted_draft.lines.get()
        with pytest.raises(ValidationError) as exc:
            add_production_batch_substitute(
                line=line, item=box, entered_quantity=Decimal("1"), actor=manager
            )
        assert "production_actual_item_not_approved" in codes_of(exc.value)

    def test_a_foreign_item_is_refused_before_it_is_looked_up(
        self, substituted_draft: ProductionBatch, rival_item: InventoryItem, manager: User
    ) -> None:
        line = substituted_draft.lines.get()
        with pytest.raises(ValidationError) as exc:
            add_production_batch_substitute(
                line=line, item=rival_item, entered_quantity=Decimal("1"), actor=manager
            )
        assert "production_actual_foreign_item" in codes_of(exc.value)

    def test_a_cross_dimension_substitution_is_never_summed(
        self, substituted_draft: ProductionBatch, oil: InventoryItem, manager: User
    ) -> None:
        """
        4 KG of rice met with 2 litres has not been met with "6" of anything.
        """
        line = substituted_draft.lines.get()
        primary = line.actuals.get()
        add_production_batch_substitute(
            line=line, item=oil, entered_quantity=Decimal("2"), actor=manager
        )

        # The rice row alone. Never rice plus oil.
        assert comparable_consumption(line) == primary.base_quantity

        remove_production_batch_substitute(actual=primary, actor=manager, reason="استبدال كامل")
        refreshed = substituted_draft.lines.get()
        assert comparable_consumption(refreshed) is None
        assert has_recorded_consumption(refreshed) is True

        comparison = consumption_comparisons(substituted_draft)[0]
        assert comparison.is_comparable is False
        assert comparison.variance is None
        assert "غير قابل للمقارنة" in comparison.statement


class TestOutputAndNotes:
    def test_the_output_is_entered_and_never_derived(
        self, production_draft: ProductionBatch, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        """
        RCP-031. Deriving it from the inputs would assume a yield nobody
        measured, and the gap between expected and actual is the yield report.
        """
        updated = record_production_output(
            batch=production_draft,
            entered_quantity=Decimal("9"),
            entered_unit=kilogram,
            actor=manager,
        )

        assert updated.actual_output_base_quantity == Decimal("9.000000")
        assert updated.expected_output_quantity == Decimal("25.000000")

    def test_a_note_may_be_cleared_and_the_edit_is_audited(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        update_production_batch_notes(batch=production_draft, notes="سبب الانحراف", actor=manager)
        cleared = update_production_batch_notes(
            batch=ProductionBatch.objects.get(pk=production_draft.pk), notes="", actor=manager
        )

        assert cleared.notes == ""
        events = AuditEvent.objects.filter(
            target_type="kitchen.ProductionBatch",
            target_id=str(production_draft.pk),
            action=AuditAction.UPDATED,
        )
        assert events.count() >= 2, "both the write and the clearing left a trail"


class TestRescaleAndDiscard:
    def test_an_ordinary_rescale_is_refused_once_anything_was_entered(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        Silently recomputing over somebody's measurements would replace a fact
        with an assumption, and nothing afterwards would say it happened.
        """
        update_production_batch_actuals(
            actual=production_draft.lines.get().actuals.get(),
            entered_quantity=Decimal("7"),
            actor=manager,
        )
        with pytest.raises(ValidationError) as exc:
            rescale_production_batch(batch=production_draft, multiplier=Decimal("4"))
        assert "production_batch_has_operator_edits" in codes_of(exc.value)

    def test_reset_and_rescale_requires_a_reason(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        update_production_batch_actuals(
            actual=production_draft.lines.get().actuals.get(),
            entered_quantity=Decimal("7"),
            actor=manager,
        )
        with pytest.raises(ValidationError) as exc:
            rescale_production_batch(
                batch=production_draft,
                multiplier=Decimal("4"),
                reset_actuals=True,
                reason="   ",
            )
        assert "production_batch_reset_requires_reason" in codes_of(exc.value)

    def test_reset_and_rescale_replaces_the_actuals_and_says_why(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        update_production_batch_actuals(
            actual=production_draft.lines.get().actuals.get(),
            entered_quantity=Decimal("7"),
            actor=manager,
        )
        rescaled = rescale_production_batch(
            batch=production_draft,
            multiplier=Decimal("4"),
            actor=manager,
            reset_actuals=True,
            reason="تغيّر حجم الطلب.",
        )

        line = rescaled.lines.get()
        assert rescaled.multiplier == Decimal("4.000000")
        assert line.actuals.get().base_quantity == line.planned_base_quantity
        event = (
            AuditEvent.objects.filter(
                target_type="kitchen.ProductionBatch", target_id=str(rescaled.pk)
            )
            .order_by("-occurred_at")
            .first()
        )
        assert event is not None
        assert event.reason == "تغيّر حجم الطلب."

    def test_an_untouched_batch_rescales_freely(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        rescaled = rescale_production_batch(
            batch=production_draft, multiplier=Decimal("1"), actor=manager
        )
        line = rescaled.lines.get()

        assert rescaled.multiplier == Decimal("1.000000")
        assert line.actuals.get().base_quantity == line.planned_base_quantity

    def test_a_discard_with_operator_figures_requires_a_reason(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        update_production_batch_actuals(
            actual=production_draft.lines.get().actuals.get(),
            entered_quantity=Decimal("7"),
            actor=manager,
        )
        with pytest.raises(ValidationError) as exc:
            discard_production_batch(batch=production_draft, actor=manager)
        assert "production_batch_discard_requires_reason" in codes_of(exc.value)

    def test_a_discard_cascades_to_its_own_rows_and_to_nothing_else(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        version_id = production_draft.recipe_version_id
        discard_production_batch(batch=production_draft, actor=manager, reason="ألغي الطلب")

        assert not ProductionBatch.objects.filter(pk=production_draft.pk).exists()
        assert not ProductionBatchActualLine.objects.filter(
            line__batch_id=production_draft.pk
        ).exists()
        assert RecipeVersion.objects.filter(pk=version_id).exists(), "the recipe is untouched"
        assert AuditEvent.objects.filter(
            target_type="kitchen.ProductionBatch",
            target_id=str(production_draft.pk),
            action=AuditAction.DELETED,
        ).exists()


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_every_problem_is_reported_at_once(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        An operator fixing one thing at a time and resubmitting is an operator
        the system is wasting.
        """
        update_production_batch_actuals(
            actual=production_draft.lines.get().actuals.get(),
            entered_quantity=Decimal("0"),
            actor=manager,
        )
        problems = validate_production_batch_ready(
            ProductionBatch.objects.get(pk=production_draft.pk)
        )

        assert {
            "production_ready_no_actual_output",
            "production_ready_required_line_is_zero",
        } <= codes(problems)

    def test_a_required_line_at_zero_blocks_and_an_optional_one_does_not(
        self, optional_draft: ProductionBatch, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        """
        The same zero, on two requirements that differ only in being optional.

        The optionality comes from the **recipe**, not from an edit to the
        requirement: those columns are frozen, and a test that flipped one would
        be testing a state the database refuses to hold.
        """
        record_production_output(
            batch=optional_draft,
            entered_quantity=Decimal("9"),
            entered_unit=kilogram,
            actor=manager,
        )
        for line in optional_draft.lines.all():
            update_production_batch_actuals(
                actual=line.actuals.get(), entered_quantity=Decimal("0"), actor=manager
            )

        problems = validate_production_batch_ready(
            ProductionBatch.objects.get(pk=optional_draft.pk)
        )
        zeros = [
            problem
            for problem in problems
            if problem.code == "production_ready_required_line_is_zero"
        ]
        required = optional_draft.lines.filter(is_optional=False)
        optional = optional_draft.lines.filter(is_optional=True)

        assert required.exists() and optional.exists(), "the fixture has one of each"
        assert {problem.line_order for problem in zeros} == set(
            required.values_list("line_order", flat=True)
        ), "every required line at zero blocks, and no optional one does"

    def test_a_missing_output_blocks_and_a_zero_output_blocks_differently(
        self, production_draft: ProductionBatch, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        missing = validate_production_batch_ready(production_draft)
        assert "production_ready_no_actual_output" in codes(missing)

        record_production_output(
            batch=production_draft,
            entered_quantity=Decimal("0"),
            entered_unit=kilogram,
            actor=manager,
        )
        zero = validate_production_batch_ready(ProductionBatch.objects.get(pk=production_draft.pk))
        assert "production_ready_actual_output_not_positive" in codes(zero)

    def test_a_complete_draft_is_ready(
        self, production_draft: ProductionBatch, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        batch = ready(production_draft, manager, kilogram)
        problems = validate_production_batch_ready(batch)

        assert problems == [], f"unexpected: {codes(problems)}"
        assert production_batch_is_ready(batch) is True

    def test_a_cross_dimension_substitution_is_an_observation_not_a_blocker(
        self,
        substituted_draft: ProductionBatch,
        oil: InventoryItem,
        kilogram: UnitOfMeasure,
        manager: User,
    ) -> None:
        """
        The kitchen used an approved stand-in. Reporting that as a problem would
        refuse a batch for being correct; saying nothing would leave a blank
        variance indistinguishable from "nobody looked".
        """
        line = substituted_draft.lines.get()
        add_production_batch_substitute(
            line=line, item=oil, entered_quantity=Decimal("2"), actor=manager
        )
        remove_production_batch_substitute(
            actual=line.actuals.get(substitute__isnull=True), actor=manager, reason="كامل"
        )
        record_production_output(
            batch=substituted_draft,
            entered_quantity=Decimal("9"),
            entered_unit=kilogram,
            actor=manager,
        )
        readiness = production_batch_readiness(ProductionBatch.objects.get(pk=substituted_draft.pk))

        assert readiness.is_ready is True, f"blocked by {codes(readiness.problems)}"
        assert "production_ready_not_quantitatively_comparable" in codes(readiness.observations)

    def test_readiness_queries_no_stock_and_writes_nothing(
        self, production_draft: ProductionBatch, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        """
        Availability, lots and expiry are Task 3.5's, at posting. A draft that
        reserved stock would make drafting a thing that can fail for reasons
        nothing about the draft can fix.
        """
        batch = ready(production_draft, manager, kilogram)
        before = _census()

        with connection.execute_wrapper(_recorder := _QueryRecorder()):
            production_batch_readiness(batch)

        for table in (
            "inventory_stockbalance",
            "inventory_stockmovement",
            "inventory_inventorylot",
        ):
            assert not any(table in sql for sql in _recorder.statements), (
                f"readiness queried {table}"
            )
        assert not any(
            sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for sql in _recorder.statements
        ), "readiness wrote something"
        assert _census() == before


class _QueryRecorder:
    """Every SQL statement a block of code ran, for the read-only assertions."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        self.statements.append(sql)
        return execute(sql, params, many, context)


# ---------------------------------------------------------------------------
# Cardinality and integrity, at the database
# ---------------------------------------------------------------------------


class TestTheDatabaseRefusesWhatTheServiceRefuses:
    def test_a_second_primary_row_is_refused(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        line = production_draft.lines.get()
        existing = line.actuals.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            ProductionBatchActualLine.objects.create(
                line=line,
                entry_order=9,
                kind="PRIMARY",
                item=line.item,
                entered_quantity=Decimal("1"),
                entered_unit=line.item.base_unit,
                conversion_factor=Decimal("1"),
                base_quantity=Decimal("1"),
            )
        assert line.actuals.count() == 1
        assert existing.pk == line.actuals.get().pk

    def test_a_zero_entry_order_is_refused(self, production_draft: ProductionBatch) -> None:
        """A zero-ordered row would sort ahead of the generated primary row."""
        actual = production_draft.lines.get().actuals.get()
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatchactualline SET entry_order = 0 WHERE id = %s",
                [actual.pk],
            )

    def test_a_substitute_approved_for_another_line_is_refused_by_the_trigger(
        self,
        substituted_draft: ProductionBatch,
        barley: InventoryItem,
        rice: InventoryItem,
        manager: User,
        organization: Any,
        branch: Branch,
        kilogram: UnitOfMeasure,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        A substitute approved for the rice line is not approved for the oil line,
        even when both name rice. Raw SQL, because the service check proves
        nothing about a psql prompt.
        """
        from apps.kitchen.services import add_recipe_line_substitute, create_recipe

        from .conftest import build_complete_draft

        other_recipe = create_recipe(
            organization=organization,
            code="OTHER-LINE",
            name="وصفة أخرى",
            recipe_type="PORTION",
            created_by=manager,
        )
        other_version = build_complete_draft(
            recipe=other_recipe, unit=kilogram, item=rice, author=manager
        )
        foreign_approval = add_recipe_line_substitute(
            line=other_version.lines.get(), substitute_item=barley, reason="اعتماد لسطر آخر"
        )

        line = substituted_draft.lines.get()
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO kitchen_productionbatchactualline "
                "(line_id, entry_order, kind, item_id, substitute_id, entered_quantity, "
                " entered_unit_id, conversion_factor, base_quantity, reason, note, "
                " public_id, created_at, updated_at) "
                "VALUES (%s, 7, 'SUBSTITUTE', %s, %s, 1, %s, 1, 1, '', '', "
                " gen_random_uuid(), now(), now())",
                [line.pk, barley.pk, foreign_approval.pk, barley.base_unit_id],
            )

    def test_a_substitute_row_naming_a_different_item_is_refused(
        self,
        substituted_draft: ProductionBatch,
        barley: InventoryItem,
        oil: InventoryItem,
    ) -> None:
        """An approval pointing at barley does not authorize consuming oil."""
        line = substituted_draft.lines.get()
        approval = RecipeLineSubstitute.objects.get(line=line.source_line, substitute_item=barley)
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO kitchen_productionbatchactualline "
                "(line_id, entry_order, kind, item_id, substitute_id, entered_quantity, "
                " entered_unit_id, conversion_factor, base_quantity, reason, note, "
                " public_id, created_at, updated_at) "
                "VALUES (%s, 8, 'SUBSTITUTE', %s, %s, 1, %s, 1, 1, '', '', "
                " gen_random_uuid(), now(), now())",
                [line.pk, oil.pk, approval.pk, oil.base_unit_id],
            )

    def test_the_draft_only_boundary_holds_against_raw_sql(
        self, production_draft: ProductionBatch
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatch SET status = 'POSTED', number = 'X-1' "
                "WHERE id = %s",
                [production_draft.pk],
            )
        assert ProductionBatch.objects.get(pk=production_draft.pk).status == (
            ProductionBatchStatus.DRAFT
        )


# ---------------------------------------------------------------------------
# Zero effect — the boundary Task 3.4 is defined by
# ---------------------------------------------------------------------------


def _census() -> dict[str, Any]:
    """Every table a posting would touch, plus the snapshot rows costing owns."""
    return {
        "movements": StockMovement.objects.count(),
        "ledger_entries": StockLedgerEntry.objects.count(),
        "balances": list(
            StockBalance.objects.order_by("pk").values_list("pk", "quantity", "value")
        ),
        "location_balances": list(
            StockLocationBalance.objects.order_by("pk").values_list("pk", "quantity")
        ),
        "journals": JournalEntry.objects.count(),
        "journal_lines": JournalLine.objects.count(),
        "lots": InventoryLot.objects.count(),
        "cost_snapshots": list(
            RecipeCostSnapshot.objects.order_by("pk").values_list("pk", "total_material_cost")
        ),
        "posted_batches": ProductionBatch.objects.exclude(
            status=ProductionBatchStatus.DRAFT
        ).count(),
    }


class TestZeroInventoryAndZeroGeneralLedger:
    def test_the_whole_drafting_scenario_moves_nothing(
        self,
        substituted_draft: ProductionBatch,
        barley: InventoryItem,
        oil: InventoryItem,
        kilogram: UnitOfMeasure,
        gram: UnitOfMeasure,
        manager: User,
    ) -> None:
        """
        Create, edit, substitute twice, rescale, reset, record an output, verify,
        and discard — against a census taken before any of it.

        The census includes the *values* of every stock balance and cost
        snapshot, not merely their counts: a posting that replaced one row with
        another would keep the count identical.
        """
        before = _census()
        line = substituted_draft.lines.get()

        update_production_batch_actuals(
            actual=line.actuals.get(),
            entered_quantity=Decimal("2500"),
            entered_unit=gram,
            actor=manager,
        )
        add_production_batch_substitute(
            line=line, item=barley, entered_quantity=Decimal("1"), actor=manager, reason="نقص"
        )
        add_production_batch_substitute(
            line=line, item=oil, entered_quantity=Decimal("0.5"), actor=manager, reason="بديل"
        )
        rescale_production_batch(
            batch=substituted_draft,
            multiplier=Decimal("3.5"),
            actor=manager,
            reset_actuals=True,
            reason="إعادة ضبط",
        )
        refreshed = ProductionBatch.objects.get(pk=substituted_draft.pk)
        record_production_output(
            batch=refreshed,
            entered_quantity=Decimal("18"),
            entered_unit=kilogram,
            actor=manager,
        )
        production_batch_readiness(ProductionBatch.objects.get(pk=refreshed.pk))
        batch_findings(ProductionBatch.objects.get(pk=refreshed.pk))
        discard_production_batch(
            batch=ProductionBatch.objects.get(pk=refreshed.pk),
            actor=manager,
            reason="انتهاء العرض",
        )

        assert _census() == before

    def test_no_production_code_imports_the_posting_layer(self) -> None:
        """
        Read from the source. An import is how a boundary stops being one, and
        it happens long before anything actually posts.
        """
        import pathlib

        forbidden = (
            "post_stock_entry",
            "reverse_stock_entry",
            "post_entry",
            "StockMovement",
            "StockBalance",
            "InventoryLot",
            "JournalEntry",
            "allocate_lots",
        )
        for name in ("production.py", "production_views.py", "expansion.py"):
            source = (pathlib.Path("apps/kitchen") / name).read_text(encoding="utf-8")
            for word in forbidden:
                assert word not in source, f"{name} names {word}"


# ---------------------------------------------------------------------------
# The demo, as the task requires it to be
# ---------------------------------------------------------------------------


class TestTheDemoProductionDraft:
    """
    The demo is not decoration: it is the one place every shape appears at once,
    and a screenshot of it is what a reviewer looks at instead of reading code.
    """

    @pytest.fixture
    def seeded(self, demo_store: Warehouse, organization: Any, manager: User) -> Any:
        """
        The kitchen demo, on the same lightweight `DEMO-` items the costing demo
        test builds. Named exactly as the inventory demo names them, because the
        kitchen seed looks the warehouse up by code — a fixture that invented its
        own name would leave half the seed silently unexercised.
        """
        from apps.kitchen.demo import seed_demo_recipes

        seed_demo_recipes(organization=organization, created_by=manager)
        return organization

    def test_the_draft_shows_every_shape_the_task_asks_for(self, seeded: Any) -> None:
        from apps.kitchen.demo import (
            COOKED_RICE_CODE,
            DEMO_BANNER,
            DEMO_PRODUCTION_DATE,
            DEMO_PRODUCTION_MULTIPLIER,
        )

        batch = ProductionBatch.objects.get(organization=seeded)
        lines = list(batch.lines.order_by("line_order"))
        paths = {line.component_path for line in lines}

        assert batch.status == ProductionBatchStatus.DRAFT
        assert batch.number == ""
        assert batch.planned_business_date == DEMO_PRODUCTION_DATE
        assert batch.multiplier == DEMO_PRODUCTION_MULTIPLIER
        assert batch.multiplier > Decimal("1")
        assert batch.multiplier != batch.multiplier.to_integral_value()
        assert batch.expected_output_quantity == Decimal("50.000000")
        assert batch.actual_output_base_quantity is not None
        assert batch.actual_output_base_quantity != batch.expected_output_quantity
        assert DEMO_BANNER in batch.notes

        assert "" in paths, "a direct source path"
        assert any(path for path in paths), "a nested component path"
        stocked = [line for line in lines if line.item_code == COOKED_RICE_CODE]
        assert len(stocked) == 1, "a stocked semi-finished leaf, one row, unexpanded"
        rice_paths = {line.component_path for line in lines if line.item_code == "DEMO-RICE"}
        assert len(rice_paths) > 1, "the same item reached by more than one path"

        optional = [line for line in lines if line.is_optional]
        assert optional and optional[0].actuals.get().base_quantity == Decimal("0")

        rice_direct = next(
            line for line in lines if line.item_code == "DEMO-RICE" and line.component_path == ""
        )
        rows = list(rice_direct.actuals.all())
        assert len(rows) == 3, "primary, a same-dimension stand-in, a cross-dimension one"
        assert any(row.item.base_unit.dimension == "VOLUME" for row in rows)
        assert comparable_consumption(rice_direct) is not None, (
            "the same-dimension rows still compare; the litres are simply excluded"
        )
        assert all(row.conversion_factor is not None for row in rows)

        findings = batch_findings(batch)
        assert any(row.code == "production_actual_rows_span_dimensions" for row in findings)
        assert all(not row.is_blocking for row in findings), (
            "the demo database has no defects, only the cross-dimension observation"
        )

    def test_a_second_run_adds_nothing(self, seeded: Any) -> None:
        """Run the seed twice and prove no duplicates, including audit events."""
        from apps.kitchen.demo import seed_demo_recipes

        def census() -> dict[str, int]:
            return {
                "batches": ProductionBatch.objects.count(),
                "requirements": sum(b.lines.count() for b in ProductionBatch.objects.all()),
                "actuals": ProductionBatchActualLine.objects.count(),
                "events": AuditEvent.objects.filter(
                    target_type__startswith="kitchen.Production"
                ).count(),
                "recipes": Recipe.objects.count(),
                "versions": RecipeVersion.objects.count(),
                "items": InventoryItem.objects.count(),
            }

        before = census()
        seed_demo_recipes(
            organization=seeded, created_by=User.objects.get(username="branch-manager")
        )

        assert census() == before

    def test_the_demo_posts_nothing(self, seeded: Any) -> None:
        assert ProductionBatch.objects.exclude(status=ProductionBatchStatus.DRAFT).count() == 0
        assert not ProductionBatch.objects.exclude(number="").exists()
        assert not JournalEntry.objects.filter(
            source_document_type="kitchen.ProductionBatch"
        ).exists()

    def test_every_demo_row_carries_the_banner(self, seeded: Any) -> None:
        from apps.kitchen.demo import DEMO_BANNER

        batch = ProductionBatch.objects.get(organization=seeded)
        assert DEMO_BANNER in batch.notes
        for line in batch.lines.all():
            for row in line.actuals.all():
                assert DEMO_BANNER in f"{row.note}{row.reason}", (
                    f"line {line.line_order} row {row.entry_order} is unmarked"
                )


def test_no_real_khan_mandi_quantity_is_used_as_demo_production_data() -> None:
    """
    RCP-126. A demo screenshot that looked like the real menu is how unapproved
    figures acquire authority, and a production draft is the most convincing
    screenshot the module produces.
    """
    import pathlib

    source = pathlib.Path("apps/kitchen/demo.py").read_text(encoding="utf-8")
    start = source.index("Task 3.4 - one visible production draft")
    scenario = source[start:]

    assert "خان مندي" not in scenario.replace("خان مندي — بيانات تجريبية", "")
    assert "DEMO-" in scenario
    for code in ("DEMO-RCP-PROD", "DEMO-MEAL-READY"):
        assert code in scenario


def test_the_seed_command_lists_the_production_screen() -> None:
    from apps.kitchen.management.commands.seed_kitchen_demo import INSPECTION_ROUTES

    assert ("kitchen:production_list", "أوامر الإنتاج") in INSPECTION_ROUTES


def test_exactly_one_navigation_entry_was_promoted() -> None:
    """
    Exactly one. Promoting two would advertise a screen that does not exist, and
    promoting none would leave the finished module unreachable.
    """
    from apps.core.navigation import MODULES

    kitchen = next(module for module in MODULES if module.key == "kitchen")
    available = [section.label for section in kitchen.sections if section.available]
    inert = [section.label for section in kitchen.sections if not section.available]

    assert "أوامر الإنتاج" in [str(label) for label in available]
    assert len(available) == 5, f"expected five reachable entries, found {available}"
    assert len(inert) == 9, f"expected nine still inert, found {inert}"


def test_no_navigation_entry_promises_a_posting_screen() -> None:
    from apps.core.navigation import MODULES

    kitchen = next(module for module in MODULES if module.key == "kitchen")
    for section in kitchen.sections:
        if not section.available:
            continue
        assert "ترحيل" not in str(section.label)
        assert section.url_name is None or "post" not in section.url_name
