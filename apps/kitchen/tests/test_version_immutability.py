"""
Whole-row immutability, at every layer that could rewrite an approved version.

Four layers, and a test for each on every owned table: the service, the form
path, the API, and — the only one that actually matters when the others are
bypassed — a raw `UPDATE` or `DELETE` at the database.

The raw statements go through `queryset.update()` and `queryset.delete()`,
which emit SQL without loading a model or running a `save()`. That is exactly
how an immutability rule expressed only in Python gets bypassed, so it is how
it has to be tested.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.test import Client

from apps.inventory.models import InventoryItem
from apps.kitchen.lifecycle import activate_recipe_version, submit_recipe_version
from apps.kitchen.models import (
    Recipe,
    RecipeLine,
    RecipeLineSubstitute,
    RecipeServing,
    RecipeStep,
    RecipeStepIngredient,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionReview,
    RecipeVersionStatus,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_line_substitute,
    add_recipe_serving,
    add_recipe_step,
    delete_draft_recipe_version,
    link_step_ingredient,
    remove_recipe_line,
    remove_recipe_serving,
    remove_recipe_step,
    update_draft_recipe_version,
    update_recipe_line,
    update_recipe_serving,
    update_recipe_step,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def furnished_draft(
    complete_draft: RecipeVersion,
    rice: InventoryItem,
    oil: InventoryItem,
    kilogram: UnitOfMeasure,
) -> RecipeVersion:
    """A draft carrying one of every owned child row, before it is frozen."""
    line = complete_draft.lines.get(item=rice)
    add_recipe_line_substitute(line=line, substitute_item=oil, reason="بديل")
    step = complete_draft.steps.first()
    assert step is not None
    link_step_ingredient(step=step, recipe_line=line, share=Decimal("1"))
    return RecipeVersion.objects.get(pk=complete_draft.pk)


@pytest.fixture
def frozen(furnished_draft: RecipeVersion, manager: User) -> RecipeVersion:
    """The same version, submitted — and therefore frozen in every table."""
    return submit_recipe_version(version=furnished_draft, actor=manager)


class TestTheServiceRefuses:
    def test_the_header_cannot_be_edited(
        self, frozen: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError):
            update_draft_recipe_version(
                version=frozen,
                expected_output_quantity=Decimal("99"),
                output_unit=kilogram,
            )

    def test_a_line_cannot_be_edited(
        self, frozen: RecipeVersion, rice: InventoryItem, kilogram: UnitOfMeasure
    ) -> None:
        line = frozen.lines.get(item=rice)
        with pytest.raises(ValidationError):
            update_recipe_line(
                line=line,
                entered_quantity=Decimal("99"),
                entered_unit=kilogram,
            )

    def test_a_line_cannot_be_added(
        self, frozen: RecipeVersion, oil: InventoryItem, litre: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError):
            add_recipe_line(
                version=frozen,
                item=oil,
                entered_quantity=Decimal("1"),
                entered_unit=litre,
            )

    def test_a_line_cannot_be_removed(self, frozen: RecipeVersion, rice: InventoryItem) -> None:
        with pytest.raises(ValidationError):
            remove_recipe_line(line=frozen.lines.get(item=rice))

    def test_a_step_cannot_be_edited_added_or_removed(self, frozen: RecipeVersion) -> None:
        step = frozen.steps.first()
        assert step is not None
        with pytest.raises(ValidationError):
            update_recipe_step(step=step, instruction_ar="مختلفة")
        with pytest.raises(ValidationError):
            add_recipe_step(version=frozen, instruction_ar="جديدة")
        with pytest.raises(ValidationError):
            remove_recipe_step(step=step)

    def test_a_serving_cannot_be_edited_added_or_removed(
        self, frozen: RecipeVersion, kilogram: UnitOfMeasure
    ) -> None:
        serving = frozen.servings.first()
        assert serving is not None
        with pytest.raises(ValidationError):
            update_recipe_serving(
                serving=serving,
                name="أخرى",
                serving_quantity=Decimal("2"),
                serving_unit=kilogram,
            )
        with pytest.raises(ValidationError):
            add_recipe_serving(
                version=frozen,
                code="TWO",
                name="ثانية",
                serving_quantity=Decimal("2"),
                serving_unit=kilogram,
            )
        with pytest.raises(ValidationError):
            remove_recipe_serving(serving=serving)

    def test_the_version_cannot_be_discarded(self, frozen: RecipeVersion) -> None:
        with pytest.raises(ValidationError):
            delete_draft_recipe_version(version=frozen)


class TestTheDatabaseRefusesRawWrites:
    """
    The layer that matters. Every statement here bypasses the services, the
    forms and the API entirely.
    """

    def test_a_raw_update_cannot_rewrite_a_frozen_header(self, frozen: RecipeVersion) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersion.objects.filter(pk=frozen.pk).update(
                expected_output_quantity=Decimal("999")
            )

    def test_a_raw_update_cannot_skip_a_lifecycle_step(self, frozen: RecipeVersion) -> None:
        """`SUBMITTED -> ACTIVE` is not one of the five permitted transitions."""
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersion.objects.filter(pk=frozen.pk).update(status=RecipeVersionStatus.ACTIVE)

    def test_a_raw_update_cannot_move_the_version_identity(self, frozen: RecipeVersion) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersion.objects.filter(pk=frozen.pk).update(version_number=99)

    def test_a_raw_delete_cannot_erase_a_frozen_version(self, frozen: RecipeVersion) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersion.objects.filter(pk=frozen.pk).delete()

    @pytest.mark.parametrize(
        "model, field, value",
        [
            (RecipeLine, "base_quantity", Decimal("999")),
            (RecipeStep, "instruction_ar", "مكتوبة من الخارج"),
            (RecipeServing, "name", "مكتوبة من الخارج"),
            (RecipeLineSubstitute, "priority", 9),
            (RecipeStepIngredient, "share", Decimal("0.5")),
        ],
    )
    def test_a_raw_update_cannot_rewrite_an_owned_child_row(
        self,
        frozen: RecipeVersion,
        model: type[models.Model],
        field: str,
        value: object,
    ) -> None:
        row = _child_of(model, frozen)
        with pytest.raises(IntegrityError), transaction.atomic():
            model._default_manager.filter(pk=row.pk).update(**{field: value})

    @pytest.mark.parametrize(
        "model",
        [RecipeLine, RecipeStep, RecipeServing, RecipeLineSubstitute, RecipeStepIngredient],
    )
    def test_a_raw_delete_cannot_erase_an_owned_child_row(
        self, frozen: RecipeVersion, model: type[models.Model]
    ) -> None:
        row = _child_of(model, frozen)
        with pytest.raises(IntegrityError), transaction.atomic():
            model._default_manager.filter(pk=row.pk).delete()

    def test_a_review_cannot_be_edited(self, active_version: RecipeVersion) -> None:
        review = active_version.reviews.first()
        assert review is not None
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersionReview.objects.filter(pk=review.pk).update(decision="REJECTED")

    def test_a_review_cannot_be_deleted(self, active_version: RecipeVersion) -> None:
        review = active_version.reviews.first()
        assert review is not None
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersionReview.objects.filter(pk=review.pk).delete()

    def test_an_effective_scope_row_cannot_be_deleted(self, active_version: RecipeVersion) -> None:
        """
        Closing a scope is how a superseded version keeps answering for its own
        dates. Deleting one would erase the period rather than end it.
        """
        scope = active_version.branch_scopes.first()
        assert scope is not None
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersionBranchScope.objects.filter(pk=scope.pk).delete()

    def test_a_live_scope_cannot_be_closed_without_a_supersession(
        self, active_version: RecipeVersion
    ) -> None:
        """
        The hole a naive "only `effective_to` may move" rule would leave: a raw
        update ending a live recipe at one branch, with nothing recording why.
        """
        scope = active_version.branch_scopes.first()
        assert scope is not None
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersionBranchScope.objects.filter(pk=scope.pk).update(
                effective_to=datetime.date(2026, 7, 10)
            )

    def test_a_scope_row_cannot_be_moved_to_another_branch(
        self, active_version: RecipeVersion, second_branch: Branch
    ) -> None:
        scope = active_version.branch_scopes.first()
        assert scope is not None
        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeVersionBranchScope.objects.filter(pk=scope.pk).update(branch=second_branch)


class TestChildRowsCannotChangeParent:
    def test_a_line_cannot_be_moved_to_another_version(
        self,
        frozen: RecipeVersion,
        organization: Organization,
        rice: InventoryItem,
        kilogram: UnitOfMeasure,
        manager: User,
    ) -> None:
        """
        The other version is on a *different* recipe: one version in flight per
        recipe is its own rule, and borrowing this test to violate it would
        prove the wrong thing.
        """
        from apps.kitchen.models import RecipeType
        from apps.kitchen.services import create_draft_recipe_version, create_recipe

        elsewhere = create_recipe(
            organization=organization,
            code="ELSEWHERE",
            name="وصفة أخرى",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        other = create_draft_recipe_version(
            recipe=elsewhere,
            expected_output_quantity=Decimal("5"),
            output_unit=kilogram,
            created_by=manager,
        )
        line = frozen.lines.get(item=rice)

        with pytest.raises(IntegrityError), transaction.atomic():
            RecipeLine.objects.filter(pk=line.pk).update(version=other)


class TestTheApiRefuses:
    def test_the_api_cannot_patch_a_frozen_version(
        self, frozen: RecipeVersion, manager_client: Client
    ) -> None:
        response = manager_client.patch(
            f"/api/v1/kitchen/versions/{frozen.pk}",
            data={"expected_output_quantity": "50", "output_unit_code": "KG"},
            content_type="application/json",
        )

        assert response.status_code == 422
        frozen.refresh_from_db()
        assert frozen.expected_output_quantity == Decimal("10")

    def test_the_api_cannot_delete_a_frozen_version(
        self, frozen: RecipeVersion, manager_client: Client
    ) -> None:
        response = manager_client.delete(f"/api/v1/kitchen/versions/{frozen.pk}")

        assert response.status_code == 422
        assert RecipeVersion.objects.filter(pk=frozen.pk).exists()

    def test_the_api_cannot_add_a_line_to_a_frozen_version(
        self, frozen: RecipeVersion, oil: InventoryItem, manager_client: Client
    ) -> None:
        response = manager_client.post(
            f"/api/v1/kitchen/versions/{frozen.pk}/lines",
            data={"item_id": oil.pk, "entered_quantity": "1", "entered_unit_code": "L"},
            content_type="application/json",
        )

        assert response.status_code == 422


class TestApprovedHistoryIsRetained:
    def test_the_review_rows_survive_activation_and_supersession(
        self,
        active_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        from .conftest import build_complete_draft, carry_to_approved

        before = list(active_version.reviews.values_list("pk", flat=True))
        second = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=second,
            actor=approver,
            effective_from=datetime.date(2026, 8, 1),
            supersedes=active_version,
        )

        active_version.refresh_from_db()
        assert active_version.status == RecipeVersionStatus.SUPERSEDED
        assert list(active_version.reviews.values_list("pk", flat=True)) == before


def _child_of(model: type[models.Model], version: RecipeVersion) -> models.Model:
    """One row of `model` belonging to this version, however it hangs off it."""
    row: models.Model | None
    if model is RecipeLine:
        row = version.lines.first()
    elif model is RecipeStep:
        row = version.steps.first()
    elif model is RecipeServing:
        row = version.servings.first()
    elif model is RecipeLineSubstitute:
        row = RecipeLineSubstitute.objects.filter(line__version=version).first()
    elif model is RecipeStepIngredient:
        row = RecipeStepIngredient.objects.filter(step__version=version).first()
    else:  # pragma: no cover - a typo in the parametrisation, not a state
        raise AssertionError(f"no accessor for {model.__name__}")
    assert row is not None
    return row
