"""
Inventory master data: categories, package units, items, conversions.

The rules these guard are not stylistic. A category that holds both items and
children makes every rollup wrong; a package unit with a universal factor
makes every carton of every item wrong; a conversion that can be edited in
place silently restates history.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import translation

from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    ItemCategory,
    ItemPackageConversion,
    ItemType,
    PackageUnit,
)
from apps.inventory.services import (
    canonical_code,
    correct_unused_item_base_unit,
    create_item,
    create_item_category,
    create_item_conversion,
    create_package_unit,
    supersede_item_conversion,
    update_item,
    update_item_category,
)
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure

from .conftest import TODAY

pytestmark = pytest.mark.django_db


class TestCategoryHierarchy:
    def test_code_is_unique_per_organization(self, organization: Organization) -> None:
        create_item_category(organization=organization, code="DRY", name="جافة")

        with pytest.raises(ValidationError):
            create_item_category(organization=organization, code="DRY", name="مكرر")

    def test_the_same_code_is_allowed_in_another_organization(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        create_item_category(organization=organization, code="DRY", name="جافة")
        rival = create_item_category(organization=other_organization, code="DRY", name="جافة")

        assert rival.pk is not None

    def test_depth_is_derived_from_the_parent(
        self, organization: Organization, category: ItemCategory
    ) -> None:
        child = create_item_category(
            organization=organization, code="MEAT", name="لحوم", parent=category
        )
        grandchild = create_item_category(
            organization=organization, code="BEEF", name="بقر", parent=child
        )

        assert (category.depth, child.depth, grandchild.depth) == (1, 2, 3)

    def test_a_fourth_level_is_refused(
        self, organization: Organization, category: ItemCategory
    ) -> None:
        child = create_item_category(
            organization=organization, code="MEAT", name="لحوم", parent=category
        )
        grandchild = create_item_category(
            organization=organization, code="BEEF", name="بقر", parent=child
        )

        with pytest.raises(ValidationError) as caught:
            create_item_category(
                organization=organization, code="RIBEYE", name="ريب آي", parent=grandchild
            )

        assert caught.value.code == "category_too_deep"

    def test_a_category_cannot_be_moved_beneath_itself(
        self, organization: Organization, category: ItemCategory
    ) -> None:
        child = create_item_category(
            organization=organization, code="MEAT", name="لحوم", parent=category
        )

        with pytest.raises(ValidationError) as caught:
            update_item_category(category=category, name="أغذية", parent=child)

        assert caught.value.code == "category_cycle"

    def test_a_category_cannot_be_moved_beneath_its_own_grandchild(
        self, organization: Organization, category: ItemCategory
    ) -> None:
        child = create_item_category(
            organization=organization, code="MEAT", name="لحوم", parent=category
        )
        grandchild = create_item_category(
            organization=organization, code="BEEF", name="بقر", parent=child
        )

        with pytest.raises(ValidationError) as caught:
            update_item_category(category=category, name="أغذية", parent=grandchild)

        assert caught.value.code == "category_cycle"

    def test_a_move_that_would_push_children_too_deep_is_refused(
        self, organization: Organization, category: ItemCategory
    ) -> None:
        """
        The subtree moves with the category. A two-level branch cannot be
        hung under a level-two parent, because its leaf would land at four.
        """
        child = create_item_category(
            organization=organization, code="MEAT", name="لحوم", parent=category
        )
        create_item_category(organization=organization, code="BEEF", name="بقر", parent=child)
        other_root = create_item_category(organization=organization, code="SUPPLIES", name="لوازم")
        other_child = create_item_category(
            organization=organization, code="CLEAN", name="تنظيف", parent=other_root
        )

        with pytest.raises(ValidationError) as caught:
            update_item_category(category=child, name="لحوم", parent=other_child)

        assert caught.value.code == "category_too_deep"

    def test_re_parenting_restamps_the_subtree_depth(
        self, organization: Organization, category: ItemCategory
    ) -> None:
        child = create_item_category(
            organization=organization, code="MEAT", name="لحوم", parent=category
        )
        grandchild = create_item_category(
            organization=organization, code="BEEF", name="بقر", parent=child
        )

        update_item_category(category=child, name="لحوم", parent=None)

        child.refresh_from_db()
        grandchild.refresh_from_db()
        assert (child.depth, grandchild.depth) == (1, 2)

    def test_a_parent_from_another_organization_is_refused(
        self, organization: Organization, other_category: ItemCategory
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_item_category(
                organization=organization,
                code="MEAT",
                name="لحوم",
                parent=other_category,
            )

        assert caught.value.code == "parent_organization_mismatch"


class TestCategoriesHoldItemsOrChildrenNeverBoth:
    """
    ADR-014's hierarchy exclusivity, applied to categories. An item hanging on
    a parent stops its children summing to it, and from that point no category
    report can be trusted.
    """

    def test_an_item_cannot_sit_on_a_category_with_children(
        self,
        organization: Organization,
        category: ItemCategory,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_item(
                organization=organization,
                code="RICE",
                name="رز",
                category=category,  # has `leaf_category` beneath it
                item_type=ItemType.RAW_MATERIAL,
                base_unit=kilogram,
            )

        assert caught.value.code == "category_has_children"

    def test_a_category_holding_items_cannot_acquire_children(
        self, organization: Organization, leaf_category: ItemCategory, rice: InventoryItem
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_item_category(
                organization=organization,
                code="BEEF",
                name="بقر",
                parent=leaf_category,  # already holds `rice`
            )

        assert caught.value.code == "category_has_items"

    def test_the_database_refuses_an_item_on_a_parent_too(
        self,
        organization: Organization,
        category: ItemCategory,
        leaf_category: ItemCategory,
        rice: InventoryItem,
    ) -> None:
        """The trigger, reached by a writer that skipped the service."""
        with pytest.raises(IntegrityError), transaction.atomic():
            InventoryItem.objects.filter(pk=rice.pk).update(category=category)

    def test_the_database_refuses_a_child_under_a_category_with_items(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        rice: InventoryItem,
    ) -> None:
        orphan = create_item_category(organization=organization, code="SPARE", name="احتياطي")

        with pytest.raises(IntegrityError), transaction.atomic():
            ItemCategory.objects.filter(pk=orphan.pk).update(parent=leaf_category)


class TestItemCode:
    def test_the_code_is_canonicalised(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        item = create_item(
            organization=organization,
            code="  rice-272  ",
            name="رز",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )

        assert item.code == "RICE-272"

    def test_case_and_padding_cannot_smuggle_a_duplicate(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
    ) -> None:
        """
        Only the canonical form is ever stored, so uniqueness is
        case-insensitive in effect without a functional index.
        """
        with pytest.raises(ValidationError):
            create_item(
                organization=organization,
                code=" rice-272 ",
                name="مكرر",
                category=leaf_category,
                item_type=ItemType.RAW_MATERIAL,
                base_unit=kilogram,
            )

    def test_a_whitespace_only_code_is_refused(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_item(
                organization=organization,
                code="   ",
                name="فارغ",
                category=leaf_category,
                item_type=ItemType.RAW_MATERIAL,
                base_unit=kilogram,
            )

        assert caught.value.code == "code_required"

    def test_the_same_code_is_allowed_in_another_organization(
        self,
        other_organization: Organization,
        other_category: ItemCategory,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
    ) -> None:
        twin = create_item(
            organization=other_organization,
            code="RICE-272",
            name="رز",
            category=other_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )

        assert twin.pk != rice.pk

    def test_an_archived_code_stays_reserved(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
    ) -> None:
        """
        Archiving never frees the code. A reissued code makes every historic
        movement, count sheet, and printed shelf label ambiguous.
        """
        update_item(
            item=rice,
            name=rice.name,
            category=leaf_category,
            item_type=rice.item_type,
            is_active=False,
        )

        with pytest.raises(ValidationError):
            create_item(
                organization=organization,
                code="RICE-272",
                name="رز جديد",
                category=leaf_category,
                item_type=ItemType.RAW_MATERIAL,
                base_unit=kilogram,
            )

    def test_canonical_code_helper(self) -> None:
        assert canonical_code("  rice-272 ") == "RICE-272"


class TestItemTypes:
    def test_there_are_exactly_six(self) -> None:
        assert len(ItemType.choices) == 6

    def test_finished_good_exists(self) -> None:
        assert ItemType.FINISHED_GOOD in ItemType.values

    def test_a_finished_good_is_an_inventory_item_not_a_menu_item(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        """
        `FINISHED_GOOD` means physically stored and countable output — a tray
        of bread baked for tomorrow. A plated dish is assembled on demand,
        never stored, and is not an `InventoryItem` at all; menu items live in
        Sales and Recipes and no model in this app represents one.
        """
        bread = create_item(
            organization=organization,
            code="BREAD-TRAY",
            name="صينية خبز",
            category=leaf_category,
            item_type=ItemType.FINISHED_GOOD,
            base_unit=kilogram,
        )

        assert bread.item_type == ItemType.FINISHED_GOOD
        # It is stock: it has a base unit and can be counted.
        assert bread.base_unit_id == kilogram.pk
        # And nothing in this app models a menu item.
        from django.apps import apps as django_apps

        inventory_models = {
            model.__name__ for model in django_apps.get_app_config("inventory").get_models()
        }
        assert "MenuItem" not in inventory_models


class TestItemFlags:
    def test_expiry_requires_lot_tracking(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        with pytest.raises(ValidationError):
            create_item(
                organization=organization,
                code="MILK",
                name="حليب",
                category=leaf_category,
                item_type=ItemType.RAW_MATERIAL,
                base_unit=kilogram,
                tracks_expiry=True,  # without tracks_lots
            )

    def test_lots_with_expiry_is_accepted(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        milk = create_item(
            organization=organization,
            code="MILK",
            name="حليب",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
            tracks_lots=True,
            tracks_expiry=True,
            shelf_life_days=7,
        )

        assert milk.tracks_expiry is True

    def test_the_database_refuses_expiry_without_lots(self, rice: InventoryItem) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            InventoryItem.objects.filter(pk=rice.pk).update(tracks_expiry=True)

    def test_there_is_no_negative_stock_flag(self) -> None:
        """
        A permanent field permitting negative stock is a standing bypass with
        no actor and no reason. An override is a per-posting decision.
        """
        fields = {field.name for field in InventoryItem._meta.get_fields()}
        assert "allows_negative_stock" not in fields

    def test_there_is_no_inventory_account_field(self) -> None:
        """Account resolution belongs exclusively to AccountRole/AccountMapping."""
        fields = {field.name for field in InventoryItem._meta.get_fields()}
        assert "inventory_account" not in fields

    def test_variable_weight_is_derived_not_stored(
        self, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        stored = {
            field.name
            for field in InventoryItem._meta.get_fields()
            if getattr(field, "concrete", False)
        }
        assert "is_variable_weight" not in stored

        assert rice.is_variable_weight is False
        create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=TODAY,
            conversion_type=ConversionType.VARIABLE,
        )
        assert rice.is_variable_weight is True


class TestBaseUnitCorrection:
    def test_an_unused_imported_item_can_be_corrected_and_stale_conversions_are_removed(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        piece: UnitOfMeasure,
        sack: PackageUnit,
    ) -> None:
        garlic = create_item(
            organization=organization,
            code="GARLIC",
            name="ثوم",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=piece,
        )
        conversion = create_item_conversion(
            item=garlic,
            package_unit=sack,
            factor_to_base=Decimal("1"),
            effective_from=TODAY,
        )

        corrected = correct_unused_item_base_unit(
            item=garlic,
            base_unit=kilogram,
            reason="تصحيح وحدة الاستيراد",
        )

        assert corrected.base_unit_id == kilogram.pk
        assert not ItemPackageConversion.objects.filter(pk=conversion.pk).exists()

    def test_a_reason_is_required(
        self,
        rice: InventoryItem,
        piece: UnitOfMeasure,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            correct_unused_item_base_unit(item=rice, base_unit=piece, reason="")

        assert caught.value.code == "base_unit_correction_reason_required"


class TestPackageUnitsCarryNoFactor:
    def test_the_model_has_no_factor_field(self) -> None:
        """
        The absence is the guarantee. A carton of chicken and a carton of oil
        share only the word, so there is nowhere to write a universal factor
        and therefore nobody can.
        """
        fields = {field.name for field in PackageUnit._meta.get_fields()}
        for forbidden in ("factor", "factor_to_base", "conversion_factor", "dimension"):
            assert forbidden not in fields

    def test_a_package_unit_is_not_a_unit_of_measure(self, carton: PackageUnit) -> None:
        """Different tables, different concepts — a package has no dimension."""
        assert PackageUnit._meta.db_table != UnitOfMeasure._meta.db_table
        assert not issubclass(PackageUnit, UnitOfMeasure)

    def test_code_unique_per_organization(self, organization: Organization) -> None:
        create_package_unit(organization=organization, code="TIN", name="علبة")

        with pytest.raises(ValidationError):
            create_package_unit(organization=organization, code="TIN", name="مكرر")

    def test_the_code_is_canonicalised(self, organization: Organization) -> None:
        unit = create_package_unit(organization=organization, code=" tin ", name="علبة")
        assert unit.code == "TIN"


class TestItemConversions:
    def test_a_fixed_conversion_resolves_directly_to_base(
        self, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        conversion = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=TODAY,
        )

        assert conversion.factor_to_base == Decimal("30.000000000000")
        assert conversion.conversion_type == ConversionType.FIXED
        # It points at the item's own base unit, not at another package.
        assert conversion.item.base_unit.code == "KG"

    def test_two_packages_of_one_item_both_resolve_to_base_without_chaining(
        self, rice: InventoryItem, sack: PackageUnit, carton: PackageUnit
    ) -> None:
        """
        A carton of 24 cans is recorded as a direct factor to kilograms, never
        as CARTON -> CAN -> KG. Every link in a chain is a place where a
        version or an effective date can disagree with the others.
        """
        can_factor = create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("0.8"), effective_from=TODAY
        )
        carton_factor = create_item_conversion(
            item=rice, package_unit=carton, factor_to_base=Decimal("24"), effective_from=TODAY
        )

        # Neither row references the other; there is no field that could.
        conversion_fields = {field.name for field in ItemPackageConversion._meta.get_fields()}
        assert "via_package" not in conversion_fields
        assert "parent_conversion" not in conversion_fields
        assert can_factor.package_unit_id != carton_factor.package_unit_id

    def test_a_variable_conversion_is_marked_as_an_estimate(
        self, rice: InventoryItem, carton: PackageUnit
    ) -> None:
        conversion = create_item_conversion(
            item=rice,
            package_unit=carton,
            factor_to_base=Decimal("17.5"),
            effective_from=TODAY,
            conversion_type=ConversionType.VARIABLE,
        )

        assert conversion.conversion_type == ConversionType.VARIABLE
        # The factor is a planning estimate; Task 1.2 requires a measured
        # quantity at posting and will not use this to derive base quantity.
        assert conversion.factor_to_base == Decimal("17.500000000000")

    def test_a_zero_or_negative_factor_is_refused(
        self, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_item_conversion(
                item=rice,
                package_unit=sack,
                factor_to_base=Decimal("0"),
                effective_from=TODAY,
            )
        assert caught.value.code == "factor_not_positive"

    def test_a_package_from_another_organization_is_refused(
        self, rice: InventoryItem, other_organization: Organization
    ) -> None:
        foreign = create_package_unit(organization=other_organization, code="CARTON", name="كرتون")

        with pytest.raises(ValidationError) as caught:
            create_item_conversion(
                item=rice,
                package_unit=foreign,
                factor_to_base=Decimal("10"),
                effective_from=TODAY,
            )
        assert caught.value.code == "package_organization_mismatch"

    def test_overlapping_effective_periods_are_impossible(
        self, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        """
        Two answers to "how many kilograms in a sack today" is a state to
        prevent, not a question to resolve at query time.
        """
        create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("30"), effective_from=TODAY
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            create_item_conversion(
                item=rice,
                package_unit=sack,
                factor_to_base=Decimal("25"),
                effective_from=TODAY + timedelta(days=1),
            )

    def test_superseding_closes_the_old_row_and_versions_the_new_one(
        self, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        """
        A supplier changing a 30 kg sack to 25 kg is a new packaging fact, not
        a correction. Both rows stay readable.
        """
        original = create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("30"), effective_from=TODAY
        )
        changeover = TODAY + timedelta(days=30)

        successor = supersede_item_conversion(
            conversion=original, factor_to_base=Decimal("25"), effective_from=changeover
        )

        original.refresh_from_db()
        assert original.effective_to == changeover - timedelta(days=1)
        assert original.factor_to_base == Decimal("30.000000000000")
        assert successor.factor_to_base == Decimal("25.000000000000")
        assert successor.version == original.version + 1

    def test_a_successor_must_start_later(self, rice: InventoryItem, sack: PackageUnit) -> None:
        original = create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("30"), effective_from=TODAY
        )

        with pytest.raises(ValidationError) as caught:
            supersede_item_conversion(
                conversion=original, factor_to_base=Decimal("25"), effective_from=TODAY
            )
        assert caught.value.code == "supersede_not_later"

    def test_only_one_default_purchase_package_per_item(
        self, rice: InventoryItem, sack: PackageUnit, carton: PackageUnit
    ) -> None:
        create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=TODAY,
            is_default_purchase_package=True,
        )
        create_item_conversion(
            item=rice,
            package_unit=carton,
            factor_to_base=Decimal("10"),
            effective_from=TODAY,
            is_default_purchase_package=True,
        )

        defaults = ItemPackageConversion.objects.filter(
            item=rice, is_default_purchase_package=True, is_active=True
        )
        assert defaults.count() == 1
        assert defaults.first().package_unit_id == carton.pk  # type: ignore[union-attr]

    def test_the_database_refuses_two_defaults(
        self, rice: InventoryItem, sack: PackageUnit, carton: PackageUnit
    ) -> None:
        create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=TODAY,
            is_default_purchase_package=True,
        )
        second = create_item_conversion(
            item=rice,
            package_unit=carton,
            factor_to_base=Decimal("10"),
            effective_from=TODAY,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            ItemPackageConversion.objects.filter(pk=second.pk).update(
                is_default_purchase_package=True
            )


class TestFactorsAreLocaleIndependent:
    def test_the_factor_renders_with_a_period_under_arabic(
        self, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        """
        A conversion factor is a technical identity. Django localises
        Decimals, so under Arabic this would otherwise render
        `0,800000000000` — and a comma there is ambiguous, inviting a
        mis-typed re-entry.
        """
        conversion = create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("0.8"), effective_from=TODAY
        )

        with translation.override("ar"):
            rendered = conversion.factor_display

        assert rendered == "0.800000000000"
        assert "," not in rendered

    def test_no_float_reaches_a_factor(self, rice: InventoryItem, sack: PackageUnit) -> None:
        conversion = create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("30"), effective_from=TODAY
        )
        conversion.refresh_from_db()

        assert isinstance(conversion.factor_to_base, Decimal)
