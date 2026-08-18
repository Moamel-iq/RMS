"""
Draft versions, and everything hanging off one.

Two claims run through this file. The first is that **the boundary is held by
the database**: a version reaches a new status only along one of the five
permitted transitions, and a raw `UPDATE` that skips a step is refused whatever
the caller believed. Task 3.1 asserted the narrower form of the same claim — one
status, pinned by a check constraint — and Task 3.2A widened it rather than
loosening it.

The second is that **structure carries no arithmetic**. A step says *when* an
ingredient enters and never *how much exists*; a serving divides an output and
never prices a plate. Both are checked by asserting that quantities do not move
when the structure around them does.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client

from apps.inventory.models import InventoryItem, PackageUnit
from apps.kitchen.models import (
    MeasurementBasis,
    Recipe,
    RecipeLine,
    RecipeLineCostClass,
    RecipeServing,
    RecipeVersion,
    RecipeVersionStatus,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_line_substitute,
    add_recipe_serving,
    add_recipe_step,
    create_draft_recipe_version,
    delete_draft_recipe_version,
    link_step_ingredient,
    remove_recipe_step,
    update_draft_recipe_version,
)
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db


class TestTheLifecycleBoundary:
    def test_a_new_version_is_a_draft(self, draft: RecipeVersion) -> None:
        assert draft.status == RecipeVersionStatus.DRAFT
        assert draft.is_draft is True

    def test_the_status_enum_matches_the_approved_lifecycle(self) -> None:
        """
        Six states, and no seventh. Task 3.1 asserted one here for the same
        reason: an enum value with no service behind it is a state the system
        can be put into and cannot get out of.

        `EXPIRED` is absent because Task 3.0 §4 names one terminal state and
        expiry is a fact about a date, not a state a row sits in. `DISCARDED`
        is absent because discarding a draft deletes the row.
        """
        assert list(RecipeVersionStatus.values) == [
            "DRAFT",
            "SUBMITTED",
            "APPROVED",
            "ACTIVE",
            "REJECTED",
            "SUPERSEDED",
        ]

    def test_the_database_refuses_a_status_jump(self, draft: RecipeVersion) -> None:
        """
        A raw `UPDATE` cannot skip the lifecycle. `DRAFT -> APPROVED` is not one
        of the five permitted transitions, so the trigger refuses it whatever
        the caller believed.
        """
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersion.objects.filter(pk=draft.pk).update(status="APPROVED")

    def test_the_lifecycle_lives_outside_the_master_data_services(self) -> None:
        """
        `services.py` maintains a draft; `lifecycle.py` decides what may be
        done with it. Keeping the status transitions out of the module that
        edits rows is what makes "the only place a status moves" a checkable
        claim rather than a convention.

        `reactivate_recipe` is deliberately not caught here: un-archiving a
        recipe is master-data maintenance, not a version lifecycle.
        """
        from apps.kitchen import lifecycle, services

        in_services = {name for name in dir(services) if not name.startswith("_")}
        lifecycle_commands = {
            "submit_recipe_version",
            "approve_recipe_version",
            "reject_recipe_version",
            "activate_recipe_version",
            "supersede_recipe_version",
            "resolve_recipe_version",
        }
        assert not (in_services & lifecycle_commands)
        assert not {name for name in in_services if "approve" in name or "supersede" in name}
        assert lifecycle_commands <= set(dir(lifecycle))

    def test_the_routes_stop_where_production_begins(self) -> None:
        """
        Where the routes stop, asserted rather than promised — and the fence has
        now moved twice.

        Task 3.2A held the component routes out and **3.2B brought them in**;
        Task 3.2B held the cost routes out and **3.3 brought them in**; 3.3 held
        the production routes out and **3.4 brought the drafting half in**; 3.4
        held posting out and **3.5 brought it in**. Four rewrites, no removal,
        because a fence that moves is not a fence that came down.

        The fence now sits between production and the Kitchen report family: a
        batch may be drafted, scaled, allocated, posted and reversed through a
        screen, and there is no meal log, no theoretical or actual consumption
        read and no usage variance. Those are Tasks 3.6 to 3.9.
        `recipe_reactivate` is master data and stays.
        """
        from apps.kitchen import urls

        names = {pattern.name for pattern in urls.urlpatterns if pattern.name}
        assert "recipe_reactivate" in names
        assert "version_submit" in names
        assert "version_approve" in names
        assert "component_editor" in names, "Task 3.2B owns the component workspace"
        assert "cost_card" in names, "Task 3.3 owns the costing screens"
        assert "cost_snapshot_list" in names
        assert "production_list" in names, "Task 3.4 owns the drafting screens"
        assert "production_rescale" in names
        assert "production_post" in names, "Task 3.5 owns posting"
        assert "production_reverse" in names
        for forbidden in ("flatten", "meal", "variance", "consumption", "theoretical"):
            assert not {name for name in names if forbidden in name}, forbidden

    def test_version_numbers_are_sequential_and_never_reused(
        self, recipe: Recipe, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        first = create_draft_recipe_version(
            recipe=recipe,
            expected_output_quantity=Decimal("10"),
            output_unit=kilogram,
            created_by=manager,
        )
        assert first.version_number == 1
        delete_draft_recipe_version(version=first)
        second = create_draft_recipe_version(
            recipe=recipe,
            expected_output_quantity=Decimal("10"),
            output_unit=kilogram,
            created_by=manager,
        )
        assert second.version_number == 2

    def test_a_second_open_draft_is_refused(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure, manager: User
    ) -> None:
        with pytest.raises(ValidationError):
            create_draft_recipe_version(
                recipe=draft.recipe,
                expected_output_quantity=Decimal("5"),
                output_unit=kilogram,
                created_by=manager,
            )

    def test_a_stale_instance_cannot_modify_a_discarded_draft(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        """
        The database row is the authority; the Python object is a memory of one.
        """
        stale = RecipeVersion.objects.get(pk=draft.pk)
        delete_draft_recipe_version(version=draft)
        with pytest.raises(ValidationError):
            update_draft_recipe_version(
                version=stale,
                expected_output_quantity=Decimal("9"),
                output_unit=kilogram,
            )


class TestLines:
    def test_a_unit_quantity_converts_to_the_items_base_unit(
        self, draft: RecipeVersion, rice: InventoryItem, gram: UnitOfMeasure
    ) -> None:
        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("350"), entered_unit=gram
        )
        assert line.base_quantity == Decimal("0.350000")

    def test_a_dimension_mismatch_is_refused(
        self, draft: RecipeVersion, rice: InventoryItem, litre: UnitOfMeasure
    ) -> None:
        """
        KD-19 lands here: mass against volume needs a density nobody has
        agreed, so the unit layer refuses rather than guessing.
        """
        with pytest.raises(ValidationError):
            add_recipe_line(
                version=draft, item=rice, entered_quantity=Decimal("1"), entered_unit=litre
            )

    def test_a_fixed_package_snapshots_its_factor(
        self, draft: RecipeVersion, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("2"), package_unit=sack
        )
        assert line.base_quantity == Decimal("60.000000")
        assert line.conversion_factor == Decimal("30.000000000000")
        assert line.conversion_version == 1

    def test_a_variable_package_requires_a_measured_quantity(
        self, draft: RecipeVersion, oil: InventoryItem, drum: PackageUnit
    ) -> None:
        with pytest.raises(ValidationError):
            add_recipe_line(
                version=draft, item=oil, entered_quantity=Decimal("1"), package_unit=drum
            )

    def test_a_variable_package_accepts_the_measured_quantity(
        self, draft: RecipeVersion, oil: InventoryItem, drum: PackageUnit
    ) -> None:
        line = add_recipe_line(
            version=draft,
            item=oil,
            entered_quantity=Decimal("1"),
            package_unit=drum,
            measured_base_quantity=Decimal("17.4"),
        )
        assert line.base_quantity == Decimal("17.400000")

    def test_a_unit_and_a_package_together_are_refused(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure, sack: PackageUnit
    ) -> None:
        with pytest.raises(ValidationError):
            add_recipe_line(
                version=draft,
                item=rice,
                entered_quantity=Decimal("1"),
                entered_unit=kilogram,
                package_unit=sack,
            )

    def test_a_foreign_item_is_refused(
        self, draft: RecipeVersion, rival_item: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError):
            add_recipe_line(
                version=draft,
                item=rival_item,
                entered_quantity=Decimal("1"),
                entered_unit=kilogram,
            )

    def test_a_zero_quantity_is_refused(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError):
            add_recipe_line(
                version=draft, item=rice, entered_quantity=Decimal("0"), entered_unit=kilogram
            )

    def test_line_order_is_stable_and_unique(
        self,
        draft: RecipeVersion,
        rice: InventoryItem,
        oil: InventoryItem,
        kilogram: UnitOfMeasure,
        litre: UnitOfMeasure,
    ) -> None:
        first = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("1"), entered_unit=kilogram
        )
        second = add_recipe_line(
            version=draft, item=oil, entered_quantity=Decimal("1"), entered_unit=litre
        )
        assert (first.line_order, second.line_order) == (1, 2)
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeLine.objects.filter(pk=second.pk).update(line_order=1)

    def test_measured_and_approved_quantities_stay_apart(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        """
        `كمية القياس` is what the scale said; `الكمية المعتمدة` is what three
        people agreed to cost. Collapsing them would erase the evidence that an
        approval happened at all (RCP-062).
        """
        line = add_recipe_line(
            version=draft,
            item=rice,
            entered_quantity=Decimal("1.2"),
            entered_unit=kilogram,
            measured_quantity=Decimal("1.4"),
        )
        assert line.base_quantity == Decimal("1.200000")
        assert line.measured_quantity == Decimal("1.400000")

    def test_cost_class_separates_food_from_packaging(
        self,
        draft: RecipeVersion,
        rice: InventoryItem,
        box: InventoryItem,
        kilogram: UnitOfMeasure,
        piece: UnitOfMeasure,
    ) -> None:
        food = add_recipe_line(
            version=draft,
            item=rice,
            entered_quantity=Decimal("1"),
            entered_unit=kilogram,
            cost_class=RecipeLineCostClass.FOOD,
        )
        packaging = add_recipe_line(
            version=draft,
            item=box,
            entered_quantity=Decimal("1"),
            entered_unit=piece,
            cost_class=RecipeLineCostClass.PACKAGING,
        )
        assert food.cost_class != packaging.cost_class

    def test_measurement_basis_is_recorded_per_line(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        """
        The book's 350 g is cooked and the cards' 500 g is raw. Without this
        field the two look like a contradiction about the same meat.
        """
        raw = add_recipe_line(
            version=draft,
            item=rice,
            entered_quantity=Decimal("0.5"),
            entered_unit=kilogram,
            measurement_basis=MeasurementBasis.RAW,
        )
        assert raw.measurement_basis == MeasurementBasis.RAW

    def test_an_invalid_measurement_basis_is_refused(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError):
            add_recipe_line(
                version=draft,
                item=rice,
                entered_quantity=Decimal("1"),
                entered_unit=kilogram,
                measurement_basis="MAYBE",
            )

    def test_a_loss_rate_of_one_or_more_is_refused(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError):
            add_recipe_line(
                version=draft,
                item=rice,
                entered_quantity=Decimal("1"),
                entered_unit=kilogram,
                loss_rate=Decimal("1"),
            )

    def test_the_line_model_has_no_cost_field(self) -> None:
        """Task 3.3 owns costing. A unit cost here would be a copy that drifts."""
        forbidden = {"unit_cost", "line_cost", "cost", "price", "amount"}
        assert not ({field.name for field in RecipeLine._meta.get_fields()} & forbidden)


class TestSubstitutes:
    def test_a_substitute_cannot_be_the_primary_item(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("1"), entered_unit=kilogram
        )
        with pytest.raises(ValidationError):
            add_recipe_line_substitute(line=line, substitute_item=rice)

    def test_a_foreign_substitute_is_refused(
        self,
        draft: RecipeVersion,
        rice: InventoryItem,
        rival_item: InventoryItem,
        kilogram: UnitOfMeasure,
    ) -> None:
        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("1"), entered_unit=kilogram
        )
        with pytest.raises(ValidationError):
            add_recipe_line_substitute(line=line, substitute_item=rival_item)

    def test_a_duplicate_active_substitute_is_refused(
        self,
        draft: RecipeVersion,
        rice: InventoryItem,
        oil: InventoryItem,
        kilogram: UnitOfMeasure,
    ) -> None:
        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("1"), entered_unit=kilogram
        )
        add_recipe_line_substitute(line=line, substitute_item=oil)
        with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
            add_recipe_line_substitute(line=line, substitute_item=oil)

    def test_a_substitute_changes_no_quantity(
        self,
        draft: RecipeVersion,
        rice: InventoryItem,
        oil: InventoryItem,
        kilogram: UnitOfMeasure,
    ) -> None:
        """Guidance, never automation: nothing substitutes on its own."""
        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("2"), entered_unit=kilogram
        )
        before = line.base_quantity
        add_recipe_line_substitute(line=line, substitute_item=oil)
        line.refresh_from_db()
        assert line.base_quantity == before
        assert line.item == rice


class TestSteps:
    def test_an_arabic_instruction_is_required(self, draft: RecipeVersion) -> None:
        with pytest.raises(ValidationError):
            add_recipe_step(version=draft, instruction_ar="   ")

    def test_sequence_is_unique_per_version(self, draft: RecipeVersion) -> None:
        add_recipe_step(version=draft, instruction_ar="اغسل الرز", sequence=1)
        with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
            add_recipe_step(version=draft, instruction_ar="خطوة أخرى", sequence=1)

    def test_a_sourced_duration_is_accepted(self, draft: RecipeVersion) -> None:
        """The recipe book gives durations; they import with their page."""
        step = add_recipe_step(
            version=draft,
            instruction_ar="اطبخ في قدر الضغط",
            expected_duration=datetime.timedelta(minutes=90),
            source_document="كتاب وصفات المطبخ خان مندي",
            source_page=13,
        )
        assert step.expected_duration == datetime.timedelta(minutes=90)
        assert step.source_page == 13

    def test_a_qualitative_heat_leaves_the_temperature_null(self, draft: RecipeVersion) -> None:
        """
        The book says نار هادئة, جمر, قدر الضغط, تنور — never a number. A blank
        temperature asks a question; an invented one becomes food-safety
        guidance nobody approved (RCP-068).
        """
        step = add_recipe_step(
            version=draft,
            instruction_ar="اتركه على نار هادئة",
            heat_instruction_ar="نار هادئة",
        )
        assert step.temperature_c is None
        assert step.heat_instruction_ar == "نار هادئة"

    def test_duration_and_temperature_default_to_null(self, draft: RecipeVersion) -> None:
        step = add_recipe_step(version=draft, instruction_ar="خطوة بلا مصدر")
        assert step.expected_duration is None
        assert step.temperature_c is None

    def test_removing_a_step_changes_no_line_quantity(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        """
        A line's quantity is the whole quantity regardless of how many steps
        mention it. Removing the step that added the saffron does not remove
        the saffron (RCP-066).
        """
        line = add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("3"), entered_unit=kilogram
        )
        step = add_recipe_step(version=draft, instruction_ar="أضف الرز")
        link_step_ingredient(step=step, recipe_line=line, share=Decimal("1"))
        before = line.base_quantity

        remove_recipe_step(step=step)
        line.refresh_from_db()
        assert line.base_quantity == before
        assert RecipeLine.objects.filter(pk=line.pk).exists()


class TestStepIngredientLinks:
    def _line(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> RecipeLine:
        return add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("2"), entered_unit=kilogram
        )

    def test_shares_may_sum_to_less_than_one(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        line = self._line(draft, rice, kilogram)
        first = add_recipe_step(version=draft, instruction_ar="نصف البهار الآن")
        link_step_ingredient(step=first, recipe_line=line, share=Decimal("0.5"))
        assert line.step_links.count() == 1

    def test_shares_may_not_exceed_one_across_steps(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        line = self._line(draft, rice, kilogram)
        first = add_recipe_step(version=draft, instruction_ar="أضف ثلثي الكمية")
        second = add_recipe_step(version=draft, instruction_ar="أضف الباقي")
        link_step_ingredient(step=first, recipe_line=line, share=Decimal("0.7"))
        with pytest.raises(ValidationError):
            link_step_ingredient(step=second, recipe_line=line, share=Decimal("0.4"))

    def test_a_share_above_one_is_refused_outright(
        self, draft: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        line = self._line(draft, rice, kilogram)
        step = add_recipe_step(version=draft, instruction_ar="خطوة")
        with pytest.raises(ValidationError):
            link_step_ingredient(step=step, recipe_line=line, share=Decimal("1.5"))

    def test_a_cross_version_link_is_refused(
        self,
        draft: RecipeVersion,
        rice: InventoryItem,
        kilogram: UnitOfMeasure,
        organization: Organization,
        manager: User,
    ) -> None:
        from apps.kitchen.models import RecipeType
        from apps.kitchen.services import create_recipe

        line = self._line(draft, rice, kilogram)
        other_recipe = create_recipe(
            organization=organization,
            code="OTHER",
            name_ar="وصفة أخرى",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        other_draft = create_draft_recipe_version(
            recipe=other_recipe,
            expected_output_quantity=Decimal("1"),
            output_unit=kilogram,
            created_by=manager,
        )
        foreign_step = add_recipe_step(version=other_draft, instruction_ar="خطوة غريبة")
        with pytest.raises(ValidationError):
            link_step_ingredient(step=foreign_step, recipe_line=line)


class TestServings:
    def test_the_factor_is_derived_from_the_output_basis(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        """A 2 kg serving of a 10 kg batch is 0.2 of it. Nobody types that."""
        serving = add_recipe_serving(
            version=draft,
            code="FULL",
            name_ar="حصة",
            serving_quantity=Decimal("2"),
            serving_unit=kilogram,
            is_primary=True,
        )
        assert serving.base_quantity == Decimal("2.000000")
        assert serving.factor_of_batch == Decimal("0.200000000000")

    def test_a_half_is_exactly_half_and_is_data(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        """
        `0.500` is a row, not a branch in a service. The physical conversion of
        RCP-123 — one whole is exactly two halves — falls out of the arithmetic
        with nothing dish-specific anywhere.
        """
        whole = add_recipe_serving(
            version=draft,
            code="WHOLE",
            name_ar="كاملة",
            serving_quantity=Decimal("2"),
            serving_unit=kilogram,
            is_primary=True,
        )
        half = add_recipe_serving(
            version=draft,
            code="HALF",
            name_ar="نصف",
            serving_quantity=Decimal("1"),
            serving_unit=kilogram,
        )
        assert half.factor_of_batch == whole.factor_of_batch / 2

    def test_a_gram_serving_is_accepted_as_data(
        self, draft: RecipeVersion, gram: UnitOfMeasure
    ) -> None:
        """350 g and 500 g are source-backed data, never program logic."""
        portion = add_recipe_serving(
            version=draft,
            code="G350",
            name_ar="حصة ٣٥٠",
            serving_quantity=Decimal("350"),
            serving_unit=gram,
            source_document="كتاب وصفات المطبخ خان مندي",
            source_page=1,
        )
        assert portion.base_quantity == Decimal("0.350000")

    def test_an_incompatible_dimension_is_refused(
        self, draft: RecipeVersion, litre: UnitOfMeasure
    ) -> None:
        """
        KD-19 again: 80 ml does not become 125 g without a sourced density, and
        the operator gets a clear domain error rather than a fabricated number.
        """
        with pytest.raises(ValidationError):
            add_recipe_serving(
                version=draft,
                code="ML80",
                name_ar="كاسة",
                serving_quantity=Decimal("80"),
                serving_unit=litre,
            )

    def test_serving_codes_are_unique_per_version(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        add_recipe_serving(
            version=draft,
            code="DUP",
            name_ar="واحد",
            serving_quantity=Decimal("1"),
            serving_unit=kilogram,
        )
        with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
            add_recipe_serving(
                version=draft,
                code="dup",
                name_ar="اثنان",
                serving_quantity=Decimal("2"),
                serving_unit=kilogram,
            )

    def test_only_one_serving_is_primary(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        first = add_recipe_serving(
            version=draft,
            code="A",
            name_ar="أ",
            serving_quantity=Decimal("1"),
            serving_unit=kilogram,
            is_primary=True,
        )
        add_recipe_serving(
            version=draft,
            code="B",
            name_ar="ب",
            serving_quantity=Decimal("2"),
            serving_unit=kilogram,
            is_primary=True,
        )
        first.refresh_from_db()
        assert first.is_primary is False
        assert draft.servings.filter(is_primary=True).count() == 1

    def test_the_database_refuses_two_primaries(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        add_recipe_serving(
            version=draft,
            code="A",
            name_ar="أ",
            serving_quantity=Decimal("1"),
            serving_unit=kilogram,
            is_primary=True,
        )
        second = add_recipe_serving(
            version=draft,
            code="B",
            name_ar="ب",
            serving_quantity=Decimal("2"),
            serving_unit=kilogram,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeServing.objects.filter(pk=second.pk).update(is_primary=True)

    def test_changing_the_output_basis_re_derives_every_factor(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        """
        A serving is defined relative to the output basis, so a stale factor
        left behind after the basis moved would misprice every portion.
        """
        serving = add_recipe_serving(
            version=draft,
            code="FULL",
            name_ar="حصة",
            serving_quantity=Decimal("2"),
            serving_unit=kilogram,
            is_primary=True,
        )
        assert serving.factor_of_batch == Decimal("0.200000000000")

        update_draft_recipe_version(
            version=draft,
            expected_output_quantity=Decimal("20"),
            output_unit=kilogram,
        )
        serving.refresh_from_db()
        assert serving.factor_of_batch == Decimal("0.100000000000")

    def test_rounding_never_touches_money_because_there_is_none(self) -> None:
        forbidden = {"cost", "price", "unit_cost", "amount", "margin"}
        assert not ({field.name for field in RecipeServing._meta.get_fields()} & forbidden)

    def test_no_dish_or_gram_figure_is_hard_coded_in_the_app(self) -> None:
        """
        The convention test RCP-082 asks for: servings are **data**, and a
        service that named a dish would be the first of forty special cases.

        Docstrings and comments are excluded deliberately. Prose explaining
        *why* a half is 0.5 is the documentation this module needs; a string
        literal or a numeric constant carrying that value is the defect. The
        check therefore reads the AST and looks only at values the code
        actually computes with.
        """
        import ast
        import pathlib

        forbidden_text = ("دجاج", "chicken", "mandi", "لحم", "حنيذ")
        forbidden_numbers = {Decimal("0.35"), Decimal("0.5"), Decimal("1.4"), Decimal("1.3")}
        offenders: list[str] = []

        for path in pathlib.Path("apps/kitchen").rglob("*.py"):
            if {"tests", "migrations"} & set(path.parts) or path.name == "demo.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {
                ast.get_docstring(node, clean=False)
                for node in ast.walk(tree)
                if isinstance(
                    node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
                )
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue
                if isinstance(node.value, str):
                    if node.value in docstrings:
                        continue
                    offenders += [
                        f"{path}:{node.lineno}:{word}"
                        for word in forbidden_text
                        if word in node.value
                    ]
                elif isinstance(node.value, int | float) and not isinstance(node.value, bool):
                    if Decimal(str(node.value)) in forbidden_numbers:
                        offenders.append(f"{path}:{node.lineno}:{node.value}")
        assert offenders == []


class TestFactorIsLocaleIndependent:
    """
    A serving factor is a technical identity, not a human-facing figure.

    `CLAUDE.md` names this exact case: Django localises Decimals, so under
    Arabic a factor renders `0,033333333333`, and a comma there is ambiguous
    and invites a mis-typed re-entry. Caught by opening the screen, which is
    why the screen is worth opening.
    """

    def test_the_factor_renders_with_a_period_at_full_precision(
        self, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        serving = add_recipe_serving(
            version=draft,
            code="THIRD",
            name_ar="ثلث",
            serving_quantity=Decimal("1"),
            serving_unit=kilogram,
            is_primary=True,
        )
        assert serving.factor_display == "0.100000000000"
        assert "," not in serving.factor_display

    def test_the_screen_renders_the_factor_left_to_right_under_arabic(
        self, manager_client: Client, draft: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        from django.conf import settings
        from django.urls import reverse

        add_recipe_serving(
            version=draft,
            code="THIRD",
            name_ar="ثلث",
            serving_quantity=Decimal("1"),
            serving_unit=kilogram,
            is_primary=True,
        )
        manager_client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        body = manager_client.get(
            reverse("kitchen:recipe_detail", args=[draft.recipe_id])
        ).content.decode()
        assert '<code dir="ltr">0.100000000000</code>' in body
        assert "0,100000000000" not in body
