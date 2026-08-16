"""
The recipe master: identity, tenancy, and the output-item rule.

The centre of this file is that a recipe's **code is its identity**. It
canonicalises, it is unique per organization, and archiving never releases it —
because a code is what every later report groups by, and once Task 3.5 lands it
is what posted batches point at. Reusing `MANDI-01` for a different dish would
silently rewrite history somebody has already read.
"""

from __future__ import annotations

import inspect

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.inventory.models import InventoryItem, ItemType
from apps.kitchen.models import Recipe, RecipeCategory, RecipeType
from apps.kitchen.services import (
    archive_recipe,
    create_recipe,
    create_recipe_category,
    reactivate_recipe,
    set_recipe_branches,
    update_recipe,
    update_recipe_category,
)
from apps.organizations.models import Branch, Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


class TestCodeIsIdentity:
    def test_code_is_canonicalised_to_uppercase(
        self, organization: Organization, manager: User
    ) -> None:
        recipe = create_recipe(
            organization=organization,
            code="  mandi-01  ",
            name_ar="مندي",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        assert recipe.code == "MANDI-01"

    def test_code_is_unique_per_organization(
        self, organization: Organization, manager: User
    ) -> None:
        create_recipe(
            organization=organization,
            code="DUP",
            name_ar="واحد",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code="dup",
                name_ar="اثنان",
                recipe_type=RecipeType.PORTION,
                created_by=manager,
            )

    def test_the_same_code_is_free_in_another_organization(
        self, organization: Organization, other_organization: Organization, manager: User
    ) -> None:
        create_recipe(
            organization=organization,
            code="SHARED",
            name_ar="هنا",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        elsewhere = create_recipe(
            organization=other_organization,
            code="SHARED",
            name_ar="هناك",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        assert elsewhere.pk is not None

    def test_an_archived_code_stays_reserved(
        self, organization: Organization, recipe: Recipe, manager: User
    ) -> None:
        archive_recipe(recipe=recipe, reason="مؤرشفة")
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code=recipe.code,
                name_ar="محاولة إعادة استخدام",
                recipe_type=RecipeType.PORTION,
                created_by=manager,
            )

    def test_an_empty_code_is_refused(self, organization: Organization, manager: User) -> None:
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code="   ",
                name_ar="بلا رمز",
                recipe_type=RecipeType.PORTION,
                created_by=manager,
            )


class TestArchiveIsNeverADelete:
    def test_archive_keeps_the_row_and_flips_the_flag(self, recipe: Recipe) -> None:
        archived = archive_recipe(recipe=recipe, reason="خارج القائمة")
        assert archived.is_active is False
        assert Recipe.objects.filter(pk=recipe.pk).exists()

    def test_reactivate_brings_it_back(self, recipe: Recipe) -> None:
        archive_recipe(recipe=recipe)
        assert reactivate_recipe(recipe=recipe).is_active is True


class TestTheOutputItemRule:
    """
    A batch recipe produces stock; a portion recipe produces a plate.

    The plate is deliberately not an `InventoryItem` — the boundary Phase 1
    documented — so a portion recipe naming one is a category error rather than
    a convenience, and a batch recipe producing a `RAW_MATERIAL` would claim
    the kitchen manufactures flour.
    """

    def test_a_batch_recipe_requires_an_output_item(
        self, organization: Organization, manager: User
    ) -> None:
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code="BATCH-NO-OUT",
                name_ar="دفعة بلا ناتج",
                recipe_type=RecipeType.BATCH,
                output_item=None,
                created_by=manager,
            )

    def test_a_portion_recipe_refuses_an_output_item(
        self, organization: Organization, cooked_rice: InventoryItem, manager: User
    ) -> None:
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code="PORTION-WITH-OUT",
                name_ar="حصة بناتج",
                recipe_type=RecipeType.PORTION,
                output_item=cooked_rice,
                created_by=manager,
            )

    def test_a_raw_material_output_is_refused(
        self, organization: Organization, rice: InventoryItem, manager: User
    ) -> None:
        assert rice.item_type == ItemType.RAW_MATERIAL
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code="BATCH-RAW",
                name_ar="دفعة تنتج مادة أولية",
                recipe_type=RecipeType.BATCH,
                output_item=rice,
                created_by=manager,
            )

    def test_a_semi_finished_output_is_accepted(
        self, organization: Organization, cooked_rice: InventoryItem, manager: User
    ) -> None:
        recipe = create_recipe(
            organization=organization,
            code="BATCH-OK",
            name_ar="دفعة سليمة",
            recipe_type=RecipeType.BATCH,
            output_item=cooked_rice,
            created_by=manager,
        )
        assert recipe.output_item == cooked_rice

    def test_a_foreign_output_item_is_refused(
        self, organization: Organization, rival_item: InventoryItem, manager: User
    ) -> None:
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code="BATCH-FOREIGN",
                name_ar="ناتج من مؤسسة أخرى",
                recipe_type=RecipeType.BATCH,
                output_item=rival_item,
                created_by=manager,
            )


class TestWhatCannotChange:
    def test_the_update_service_has_no_organization_or_code_or_type(self) -> None:
        """
        Identity and tenancy are absent from the signature, not merely ignored.

        A recipe re-homed to another organization would move its whole history
        across a tenancy boundary; a recipe whose type changed under its own
        lines would mean something different with the same rows.
        """
        parameters = set(inspect.signature(update_recipe).parameters)
        assert "organization" not in parameters
        assert "code" not in parameters
        assert "recipe_type" not in parameters

    def test_a_foreign_category_is_refused(
        self, recipe: Recipe, other_organization: Organization
    ) -> None:
        theirs = create_recipe_category(
            organization=other_organization, code="THEIRS", name_ar="لهم"
        )
        with pytest.raises(ValidationError):
            update_recipe(recipe=recipe, name_ar=recipe.name_ar, category=theirs)


class TestBranchApplicability:
    def test_no_rows_means_organization_wide(self, recipe: Recipe) -> None:
        assert recipe.branch_applicability.count() == 0

    def test_branches_are_rows_not_a_string(self, recipe: Recipe, branch: Branch) -> None:
        rows = set_recipe_branches(recipe=recipe, branches=[branch])
        assert len(rows) == 1
        assert recipe.branch_applicability.get().branch == branch

    def test_setting_branches_replaces_rather_than_appends(
        self, recipe: Recipe, branch: Branch
    ) -> None:
        set_recipe_branches(recipe=recipe, branches=[branch])
        set_recipe_branches(recipe=recipe, branches=[])
        assert recipe.branch_applicability.count() == 0

    def test_a_foreign_branch_is_refused(self, recipe: Recipe, other_branch: Branch) -> None:
        with pytest.raises(ValidationError):
            set_recipe_branches(recipe=recipe, branches=[other_branch])


class TestCategories:
    def test_a_category_code_is_unique_per_organization(self, organization: Organization) -> None:
        create_recipe_category(organization=organization, code="MAIN", name_ar="رئيسي")
        with pytest.raises(ValidationError):
            create_recipe_category(organization=organization, code="main", name_ar="مكرر")

    def test_a_category_is_archived_not_deleted(self, organization: Organization) -> None:
        category = create_recipe_category(organization=organization, code="OLD", name_ar="قديم")
        update_recipe_category(category=category, name_ar="قديم", is_active=False)
        assert RecipeCategory.objects.filter(pk=category.pk, is_active=False).exists()


class TestProvenance:
    """
    Both halves of a source, or neither (RCP-119).

    A half-filled provenance looks like an answer to "who says so" and is not
    one — precisely the failure a transcription from a PDF invites.
    """

    def test_a_document_without_a_page_is_refused(
        self, organization: Organization, manager: User
    ) -> None:
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code="HALF-SOURCE",
                name_ar="مصدر ناقص",
                recipe_type=RecipeType.PORTION,
                source_document="كتاب وصفات المطبخ خان مندي",
                source_page=None,
                created_by=manager,
            )

    def test_a_page_without_a_document_is_refused(
        self, organization: Organization, manager: User
    ) -> None:
        with pytest.raises(ValidationError):
            create_recipe(
                organization=organization,
                code="PAGE-ONLY",
                name_ar="صفحة بلا مستند",
                recipe_type=RecipeType.PORTION,
                source_page=12,
                created_by=manager,
            )

    def test_both_halves_are_accepted_and_stored(
        self, organization: Organization, manager: User
    ) -> None:
        recipe = create_recipe(
            organization=organization,
            code="SOURCED",
            name_ar="موثّق",
            recipe_type=RecipeType.PORTION,
            source_document="كتاب وصفات المطبخ خان مندي",
            source_page=1,
            source_reference="بطاقة 1",
            created_by=manager,
        )
        assert recipe.has_source is True
        assert recipe.source_page == 1

    def test_the_database_refuses_a_half_provenance_too(self, organization: Organization) -> None:
        """The service is the door; the constraint is the wall behind it."""
        with pytest.raises(IntegrityError), transaction.atomic():
            Recipe.objects.create(
                organization=organization,
                code="RAWSQL",
                name_ar="تجاوز الخدمة",
                recipe_type=RecipeType.PORTION,
                source_document="مستند",
                source_page=None,
            )

    def test_no_absolute_path_is_stored_as_business_data(
        self, organization: Organization, manager: User
    ) -> None:
        """
        The document is named, not pathed.

        A developer's Windows path is not a business fact, and the recipe book
        lives outside the repository on exactly one machine.
        """
        recipe = create_recipe(
            organization=organization,
            code="NAMED-SOURCE",
            name_ar="مصدر مسمّى",
            recipe_type=RecipeType.PORTION,
            source_document="كتاب وصفات المطبخ خان مندي",
            source_page=1,
            created_by=manager,
        )
        assert ":\\" not in recipe.source_document
        assert not recipe.source_document.startswith("/")


class TestNoMoneyAnywhere:
    def test_the_recipe_model_has_no_price_or_cost_field(self) -> None:
        """
        KD-13 puts prices and margins outside Phase 3, and RCP-009 keeps cost
        derived rather than stored. Both are checked by absence, because a
        field that exists will eventually be filled.
        """
        forbidden = {
            "price",
            "selling_price",
            "margin",
            "cost",
            "unit_cost",
            "commission",
            "fee",
        }
        names = {field.name for field in Recipe._meta.get_fields()}
        assert not (names & forbidden)
