"""
The native master-data workflows: create, edit, archive, reactivate.

These screens are the only ones a storekeeper or branch manager ever sees, so
what matters here is not that a page renders but that:

* the write goes through a service, with a real audit trail behind it;
* a hidden button is never the thing stopping an unauthorized write;
* an identifier from another organization is a 404, not a 403;
* a conversion factor survives an Arabic page with its decimal **point**.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.core.models import AuditAction, AuditEvent
from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    ItemCategory,
    ItemPackageConversion,
    ItemType,
    PackageUnit,
    Warehouse,
    WarehouseType,
)
from apps.inventory.services import (
    create_item_conversion,
    ensure_in_transit_warehouse,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import TODAY

pytestmark = pytest.mark.django_db


def _events_for(instance: Any) -> Any:
    label = f"{instance._meta.app_label}.{instance._meta.object_name}"
    return AuditEvent.objects.filter(target_type=label, target_id=str(instance.pk)).order_by("id")


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class TestCategoryWorkflow:
    def test_create(self, manager: User, client_for: Any, organization: Organization) -> None:
        response = client_for(manager).post(
            reverse("inventory:category_create"),
            {
                "organization": organization.pk,
                # Deliberately lower case with padding: the service
                # canonicalises before it validates or stores.
                "code": " drinks ",
                "name": "مشروبات",
                "parent": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        created = ItemCategory.objects.get(organization=organization, code="DRINKS")
        assert created.depth == 1
        assert _events_for(created).filter(action=AuditAction.CREATED).exists()

    def test_edit_records_a_before_that_differs_from_the_after(
        self, manager: User, client_for: Any, category: ItemCategory
    ) -> None:
        """
        The ModelForm trap, checked directly.

        A bound form mutates its instance during validation, so a snapshot
        taken from it would record before == after and the audit trail would
        say nothing happened. The services re-read from the database.
        """
        response = client_for(manager).post(
            reverse("inventory:category_update", args=[category.pk]),
            {
                "name": "أغذية ومشروبات",
                "parent": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        event = _events_for(category).filter(action=AuditAction.UPDATED).last()
        assert event is not None
        assert event.previous_state["name"] == "أغذية"
        assert event.new_state["name"] == "أغذية ومشروبات"
        assert event.previous_state != event.new_state

    def test_archive_then_reactivate(
        self, manager: User, client_for: Any, category: ItemCategory
    ) -> None:
        client = client_for(manager)

        client.post(reverse("inventory:category_archive", args=[category.pk]))
        category.refresh_from_db()
        assert category.is_active is False
        assert _events_for(category).filter(action=AuditAction.DEACTIVATED).exists()

        client.post(reverse("inventory:category_reactivate", args=[category.pk]))
        category.refresh_from_db()
        assert category.is_active is True

    def test_a_cycle_is_refused_in_arabic_rather_than_crashing(
        self, manager: User, client_for: Any, category: ItemCategory, leaf_category: ItemCategory
    ) -> None:
        """Moving a parent beneath its own child. The form says so; it does not 500."""
        client = client_for(manager)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"

        response = client.post(
            reverse("inventory:category_update", args=[category.pk]),
            {"name": "أغذية", "parent": leaf_category.pk, "is_active": "on"},
        )

        assert response.status_code == 200
        category.refresh_from_db()
        assert category.parent_id is None

    def test_a_validation_error_reaches_the_page_in_arabic(
        self, manager: User, client_for: Any, organization: Organization
    ) -> None:
        """
        The message an operator actually sees. Arabic is the source language,
        so a rule they break must be stated in it and not in a stack trace.
        """
        client = client_for(manager)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"

        response = client.post(
            reverse("inventory:category_create"),
            {
                "organization": organization.pk,
                "code": "",  # required
                "name": "",  # required
                "parent": "",
            },
        )

        page = response.content.decode()
        assert response.status_code == 200
        assert "formrow__error" in page
        assert "هذا الحقل مطلوب" in page

    def test_a_category_holding_items_cannot_acquire_children(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        leaf_category: ItemCategory,
        rice: InventoryItem,
    ) -> None:
        """
        The other half of the leaf rule, from the screen. `leaf_category` now
        holds `rice`; giving it a child would stop its children summing to it.
        """
        orphan = ItemCategory.objects.create(
            organization=organization, code="SPARE", name="احتياطي", depth=1
        )

        response = client_for(manager).post(
            reverse("inventory:category_update", args=[orphan.pk]),
            {"name": "احتياطي", "parent": leaf_category.pk, "is_active": "on"},
        )

        assert response.status_code == 200
        orphan.refresh_from_db()
        assert orphan.parent_id is None

    def test_the_parent_selector_offers_only_this_organization(
        self,
        manager: User,
        client_for: Any,
        category: ItemCategory,
        other_category: ItemCategory,
    ) -> None:
        response = client_for(manager).get(reverse("inventory:category_update", args=[category.pk]))
        offered = response.context["form"].fields["parent"].queryset
        assert other_category not in offered

    def test_a_foreign_category_is_a_404(
        self, manager: User, client_for: Any, other_category: ItemCategory
    ) -> None:
        response = client_for(manager).get(
            reverse("inventory:category_update", args=[other_category.pk])
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Package units
# ---------------------------------------------------------------------------


class TestPackageUnitWorkflow:
    def test_the_form_has_no_factor_field(self, manager: User, client_for: Any) -> None:
        """
        The absence is the guarantee. A field here would invite a universal
        carton factor, and there is no such thing.
        """
        response = client_for(manager).get(reverse("inventory:package_unit_create"))
        assert "factor" not in response.context["form"].fields

    def test_create_edit_and_archive(
        self, manager: User, client_for: Any, organization: Organization
    ) -> None:
        client = client_for(manager)

        client.post(
            reverse("inventory:package_unit_create"),
            {"organization": organization.pk, "code": "tray", "name": "صينية"},
        )
        unit = PackageUnit.objects.get(organization=organization, code="TRAY")

        client.post(
            reverse("inventory:package_unit_update", args=[unit.pk]),
            {"name": "صينية كبيرة", "is_active": "on"},
        )
        unit.refresh_from_db()
        assert unit.name == "صينية كبيرة"

        client.post(reverse("inventory:package_unit_archive", args=[unit.pk]))
        unit.refresh_from_db()
        assert unit.is_active is False
        # Archived, never deleted: the code stays reserved.
        assert PackageUnit.objects.filter(pk=unit.pk).exists()


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


class TestItemWorkflow:
    def test_create_including_a_finished_good(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        leaf_category: ItemCategory,
        piece: UnitOfMeasure,
    ) -> None:
        response = client_for(manager).post(
            reverse("inventory:item_create"),
            {
                "organization": organization.pk,
                "code": "bread-01",
                "name": "صمون",
                "category": leaf_category.pk,
                "item_type": ItemType.FINISHED_GOOD,
                "base_unit": piece.pk,
                "shelf_life_days": "",
                "notes": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        item = InventoryItem.objects.get(organization=organization, code="BREAD-01")
        assert item.item_type == ItemType.FINISHED_GOOD
        assert item.name == ""  # optional, by decision

    def test_expiry_without_lots_is_refused_in_the_form(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        response = client_for(manager).post(
            reverse("inventory:item_create"),
            {
                "organization": organization.pk,
                "code": "MILK-01",
                "name": "حليب",
                "category": leaf_category.pk,
                "item_type": ItemType.RAW_MATERIAL,
                "base_unit": kilogram.pk,
                "tracks_expiry": "on",
                "shelf_life_days": "",
                "notes": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 200
        assert "tracks_expiry" in response.context["form"].errors
        assert not InventoryItem.objects.filter(code="MILK-01").exists()

    def test_the_category_selector_offers_leaves_only(
        self,
        manager: User,
        client_for: Any,
        category: ItemCategory,
        leaf_category: ItemCategory,
    ) -> None:
        response = client_for(manager).get(reverse("inventory:item_create"))
        offered = response.context["form"].fields["category"].queryset
        assert leaf_category in offered
        assert category not in offered  # it has a child

    def test_a_non_leaf_category_is_refused_even_when_posted_directly(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        category: ItemCategory,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        """`category` has a child, so it may hold no items. The selector hides
        it; the service refuses it regardless."""
        response = client_for(manager).post(
            reverse("inventory:item_create"),
            {
                "organization": organization.pk,
                "code": "MISFILED",
                "name": "مصنّف خطأ",
                "category": category.pk,
                "item_type": ItemType.RAW_MATERIAL,
                "base_unit": kilogram.pk,
                "shelf_life_days": "",
                "notes": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 200
        assert not InventoryItem.objects.filter(code="MISFILED").exists()

    def test_the_base_unit_is_frozen_on_the_edit_screen(
        self, manager: User, client_for: Any, rice: InventoryItem
    ) -> None:
        response = client_for(manager).get(reverse("inventory:item_update", args=[rice.pk]))
        form = response.context["form"]
        assert form.fields["base_unit"].disabled
        assert form.fields["code"].disabled

    def test_a_posted_base_unit_cannot_be_swapped_by_posting_one(
        self,
        manager: User,
        client_for: Any,
        rice: InventoryItem,
        leaf_category: ItemCategory,
        litre: UnitOfMeasure,
    ) -> None:
        """A disabled field takes its value from the server, never from the POST."""
        client_for(manager).post(
            reverse("inventory:item_update", args=[rice.pk]),
            {
                "name": "رز ٢٧٢",
                "category": leaf_category.pk,
                "item_type": ItemType.RAW_MATERIAL,
                "base_unit": litre.pk,
                "code": "HIJACKED",
                "shelf_life_days": "",
                "notes": "",
                "is_active": "on",
            },
        )

        rice.refresh_from_db()
        assert rice.base_unit.code == "KG"
        assert rice.code == "RICE-272"

    def test_archive_and_reactivate(
        self, manager: User, client_for: Any, rice: InventoryItem
    ) -> None:
        client = client_for(manager)
        client.post(reverse("inventory:item_archive", args=[rice.pk]))
        rice.refresh_from_db()
        assert rice.is_active is False

        client.post(reverse("inventory:item_reactivate", args=[rice.pk]))
        rice.refresh_from_db()
        assert rice.is_active is True

    def test_the_form_carries_no_account_and_no_negative_stock_flag(
        self, manager: User, client_for: Any
    ) -> None:
        fields = client_for(manager).get(reverse("inventory:item_create")).context["form"].fields
        assert "inventory_account" not in fields
        assert "allows_negative_stock" not in fields


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


class TestConversionWorkflow:
    def test_create_stores_the_exact_decimal(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        response = client_for(manager).post(
            reverse("inventory:conversion_create"),
            {
                "item": rice.pk,
                "package_unit": sack.pk,
                "conversion_type": ConversionType.FIXED,
                "factor_to_base": "30.5",
                "effective_from": TODAY.isoformat(),
                "effective_to": "",
                "allows_fractional": "on",
                "minimum_increment": "",
                "is_default_purchase_package": "on",
            },
        )

        assert response.status_code == 302
        conversion = ItemPackageConversion.objects.get(item=rice, package_unit=sack)
        assert conversion.factor_to_base == Decimal("30.5")
        assert conversion.version == 1

    def test_a_comma_is_refused_rather_than_silently_reinterpreted(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        """
        `30,5` is thirty-point-five in Arabic typing and thirty thousand five
        hundred to a parser that strips separators. Refusing is the only safe
        answer.
        """
        response = client_for(manager).post(
            reverse("inventory:conversion_create"),
            {
                "item": rice.pk,
                "package_unit": sack.pk,
                "conversion_type": ConversionType.FIXED,
                "factor_to_base": "30,5",
                "effective_from": TODAY.isoformat(),
                "effective_to": "",
                "minimum_increment": "",
            },
        )

        assert response.status_code == 200
        assert "factor_to_base" in response.context["form"].errors
        assert not ItemPackageConversion.objects.filter(item=rice).exists()

    def test_editing_corrects_an_unused_factor(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        conversion = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=TODAY,
        )

        response = client_for(manager).post(
            reverse("inventory:conversion_update", args=[conversion.pk]),
            {
                "conversion_type": ConversionType.FIXED,
                "factor_to_base": "25",
                "effective_from": TODAY.isoformat(),
                "effective_to": "",
                "allows_fractional": "on",
                "minimum_increment": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        conversion.refresh_from_db()
        assert conversion.factor_to_base == Decimal("25")
        assert conversion.version == 1  # a correction, not a new packaging fact

    def test_superseding_opens_a_new_version_and_closes_the_old_one(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        first = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=TODAY,
            is_default_purchase_package=True,
        )
        starts = date(2026, 6, 1)

        response = client_for(manager).post(
            reverse("inventory:conversion_supersede", args=[first.pk]),
            {
                "factor_to_base": "25",
                "effective_from": starts.isoformat(),
                "reason": "المورد غيّر الكيس",
            },
        )

        assert response.status_code == 302
        first.refresh_from_db()
        assert first.effective_to == date(2026, 5, 31)
        assert first.is_default_purchase_package is False

        second = ItemPackageConversion.objects.get(item=rice, package_unit=sack, version=2)
        assert second.factor_to_base == Decimal("25")
        assert second.is_default_purchase_package is True

    def test_an_overlapping_period_is_refused_with_a_message(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=TODAY,
            effective_to=date(2026, 5, 31),
        )
        archived = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("25"),
            effective_from=date(2026, 6, 1),
        )
        ItemPackageConversion.objects.filter(pk=archived.pk).update(is_active=False)
        archived.refresh_from_db()

        # Bringing it back with a period that collides with the live row.
        response = client_for(manager).post(
            reverse("inventory:conversion_update", args=[archived.pk]),
            {
                "conversion_type": ConversionType.FIXED,
                "factor_to_base": "25",
                "effective_from": TODAY.isoformat(),
                "effective_to": "",
                "minimum_increment": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 200
        assert response.context["form"].non_field_errors()
        archived.refresh_from_db()
        assert archived.is_active is False

    def test_the_edit_form_prefills_the_factor_with_a_period(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        """
        Under Arabic, Django would localise a Decimal to `0,800000000000`.
        What is shown must be re-enterable as the same number.
        """
        conversion = create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("0.8"), effective_from=TODAY
        )
        client = client_for(manager)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"

        response = client.get(reverse("inventory:conversion_update", args=[conversion.pk]))

        assert response.context["form"].initial["factor_to_base"] == "0.800000000000"
        assert "0.800000000000" in response.content.decode()

    def test_archive_and_reactivate(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        conversion = create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("30"), effective_from=TODAY
        )
        client = client_for(manager)

        client.post(reverse("inventory:conversion_archive", args=[conversion.pk]))
        conversion.refresh_from_db()
        assert conversion.is_active is False

        client.post(reverse("inventory:conversion_reactivate", args=[conversion.pk]))
        conversion.refresh_from_db()
        assert conversion.is_active is True


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------


class TestWarehouseWorkflow:
    def test_create_edit_archive_and_reactivate(
        self, manager: User, client_for: Any, branch: Branch
    ) -> None:
        client = client_for(manager)

        client.post(
            reverse("inventory:warehouse_create"),
            {
                "branch": branch.pk,
                "code": "cold",
                "name": "المبرد",
                "warehouse_type": WarehouseType.PHYSICAL,
                "is_active": "on",
            },
        )
        warehouse = Warehouse.objects.get(branch=branch, code="COLD")

        client.post(
            reverse("inventory:warehouse_update", args=[warehouse.pk]),
            {"name": "المبرد الرئيسي", "is_active": "on"},
        )
        warehouse.refresh_from_db()
        assert warehouse.name == "المبرد الرئيسي"

        client.post(reverse("inventory:warehouse_archive", args=[warehouse.pk]))
        warehouse.refresh_from_db()
        assert warehouse.is_active is False

        # And it is still readable — the code stays reserved and the history
        # is still referenced.
        listing = client.get(reverse("inventory:warehouse_list"))
        assert warehouse in listing.context["warehouses"]

        client.post(reverse("inventory:warehouse_reactivate", args=[warehouse.pk]))
        warehouse.refresh_from_db()
        assert warehouse.is_active is True

    def test_in_transit_is_not_offered_as_a_type(self, manager: User, client_for: Any) -> None:
        response = client_for(manager).get(reverse("inventory:warehouse_create"))
        offered = [
            value for value, _label in response.context["form"].fields["warehouse_type"].choices
        ]
        assert WarehouseType.IN_TRANSIT not in offered

    def test_posting_in_transit_directly_is_still_refused(
        self, manager: User, client_for: Any, branch: Branch
    ) -> None:
        """The choice list is presentation; the refusal is the service."""
        response = client_for(manager).post(
            reverse("inventory:warehouse_create"),
            {
                "branch": branch.pk,
                "code": "SNEAKY",
                "name": "تهريب",
                "warehouse_type": WarehouseType.IN_TRANSIT,
                "is_active": "on",
            },
        )

        assert response.status_code == 200
        assert not Warehouse.objects.filter(code="SNEAKY").exists()

    def test_the_system_warehouse_cannot_be_renamed_or_archived(
        self, manager: User, client_for: Any, branch: Branch
    ) -> None:
        system = ensure_in_transit_warehouse(branch=branch)
        client = client_for(manager)

        renamed = client.post(
            reverse("inventory:warehouse_update", args=[system.pk]),
            {"name": "مخزن عادي", "is_active": "on"},
        )
        assert renamed.status_code == 200
        system.refresh_from_db()
        assert system.name == "بضاعة بالطريق"

        client.post(reverse("inventory:warehouse_archive", args=[system.pk]))
        system.refresh_from_db()
        assert system.is_active is True

    def test_the_system_warehouse_shows_no_actions(
        self, manager: User, client_for: Any, branch: Branch
    ) -> None:
        ensure_in_transit_warehouse(branch=branch)
        page = client_for(manager).get(reverse("inventory:warehouse_list")).content.decode()
        assert "warehouses/" in page  # ordinary rows still offer their actions
        assert "محمي" in page

    def test_the_branch_selector_offers_only_reachable_branches(
        self, manager: User, client_for: Any, branch: Branch, other_branch: Branch
    ) -> None:
        response = client_for(manager).get(reverse("inventory:warehouse_create"))
        offered = response.context["form"].fields["branch"].queryset
        assert branch in offered
        assert other_branch not in offered


# ---------------------------------------------------------------------------
# Authorization of the workflows themselves
# ---------------------------------------------------------------------------


class TestButtonsAreNotTheProtection:
    def test_a_storekeeper_sees_no_actions_on_the_item_list(
        self, storekeeper: User, client_for: Any, rice: InventoryItem
    ) -> None:
        response = client_for(storekeeper).get(reverse("inventory:item_list"))

        assert response.status_code == 200
        assert response.context["manageable_ids"] == []
        assert response.context["create_url"] is None
        assert reverse("inventory:item_update", args=[rice.pk]) not in response.content.decode()

    def test_and_posting_the_hidden_edit_anyway_is_refused(
        self,
        storekeeper: User,
        client_for: Any,
        rice: InventoryItem,
        leaf_category: ItemCategory,
    ) -> None:
        response = client_for(storekeeper).post(
            reverse("inventory:item_update", args=[rice.pk]),
            {
                "name": "مسروق",
                "category": leaf_category.pk,
                "item_type": ItemType.RAW_MATERIAL,
                "shelf_life_days": "",
                "notes": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 403
        rice.refresh_from_db()
        assert rice.name == "رز ٢٧٢"

    def test_the_hidden_archive_action_is_refused_too(
        self, storekeeper: User, client_for: Any, rice: InventoryItem
    ) -> None:
        response = client_for(storekeeper).post(reverse("inventory:item_archive", args=[rice.pk]))
        assert response.status_code == 403
        rice.refresh_from_db()
        assert rice.is_active is True

    def test_an_archive_action_refuses_get(
        self, manager: User, client_for: Any, rice: InventoryItem
    ) -> None:
        """A state change behind a GET would fire on a link prefetch."""
        response = client_for(manager).get(reverse("inventory:item_archive", args=[rice.pk]))
        assert response.status_code == 405
        rice.refresh_from_db()
        assert rice.is_active is True

    def test_csrf_is_enforced(self, manager: User, organization: Organization) -> None:
        client = Client(enforce_csrf_checks=True)
        client.force_login(manager)

        response = client.post(
            reverse("inventory:category_create"),
            {"organization": organization.pk, "code": "NOCSRF", "name": "بلا", "parent": ""},
        )

        assert response.status_code == 403
        assert not ItemCategory.objects.filter(code="NOCSRF").exists()


class TestCrossOrganizationInjection:
    def test_a_foreign_organization_id_in_the_create_form_is_refused(
        self,
        manager: User,
        client_for: Any,
        other_organization: Organization,
        other_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        """
        The selector never offered it, so the form rejects it as an invalid
        choice — and even if it had been offered, `authorize` runs the same
        check the service would.
        """
        response = client_for(manager).post(
            reverse("inventory:item_create"),
            {
                "organization": other_organization.pk,
                "code": "STOLEN",
                "name": "مسروق",
                "category": other_category.pk,
                "item_type": ItemType.RAW_MATERIAL,
                "base_unit": kilogram.pk,
                "shelf_life_days": "",
                "notes": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 200
        assert not InventoryItem.objects.filter(code="STOLEN").exists()

    def test_a_foreign_object_id_in_the_url_is_a_404(
        self, manager: User, client_for: Any, other_warehouse: Warehouse
    ) -> None:
        response = client_for(manager).post(
            reverse("inventory:warehouse_archive", args=[other_warehouse.pk])
        )
        assert response.status_code == 404
        other_warehouse.refresh_from_db()
        assert other_warehouse.is_active is True

    def test_a_viewer_elsewhere_cannot_manage_our_master_data(
        self,
        client_for: Any,
        organization: Organization,
        other_branch: Branch,
        branch: Branch,
        rice: InventoryItem,
        leaf_category: ItemCategory,
    ) -> None:
        """
        The provenance rule, exercised through the screen a real operator uses.

        A manager at the rival, holding a viewer post at ours, carries
        `manage_items` globally and reaches our organization. Neither fact
        gives them authority here.
        """
        from apps.organizations.models import Role
        from apps.organizations.services import grant_branch_access

        from .conftest import PASSWORD

        intruder = User.objects.create_user(username="intruder", password=PASSWORD)
        grant_branch_access(user=intruder, branch=other_branch, role=Role.MANAGER)
        grant_branch_access(user=intruder, branch=branch, role=Role.VIEWER)
        intruder = User.objects.get(pk=intruder.pk)

        assert intruder.has_perm("inventory.manage_items")

        response = client_for(intruder).post(
            reverse("inventory:item_update", args=[rice.pk]),
            {
                "name": "مسروق",
                "category": leaf_category.pk,
                "item_type": ItemType.RAW_MATERIAL,
                "shelf_life_days": "",
                "notes": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 403
        rice.refresh_from_db()
        assert rice.name == "رز ٢٧٢"

    def test_and_the_buttons_are_absent_for_them_too(
        self, client_for: Any, other_branch: Branch, branch: Branch, rice: InventoryItem
    ) -> None:
        from apps.organizations.models import Role
        from apps.organizations.services import grant_branch_access

        from .conftest import PASSWORD

        intruder = User.objects.create_user(username="intruder2", password=PASSWORD)
        grant_branch_access(user=intruder, branch=other_branch, role=Role.MANAGER)
        grant_branch_access(user=intruder, branch=branch, role=Role.VIEWER)
        intruder = User.objects.get(pk=intruder.pk)

        response = client_for(intruder).get(reverse("inventory:item_list"))

        assert rice.organization_id not in response.context["manageable_ids"]


class TestTheWritePathIsStructurallySafe:
    """
    The `previous_state` trap closed at the source rather than by discipline.

    A `ModelForm` binds to an instance and mutates it during validation, so
    anything that later snapshots that instance records before == after. None
    of these forms is a `ModelForm`, so there is no mutated instance for a
    view to reach for, and no `form.save()` for one to call by habit.
    """

    def test_no_inventory_form_is_a_modelform(self) -> None:
        from django import forms as django_forms

        from apps.inventory import forms as inventory_forms

        declared = [
            value
            for value in vars(inventory_forms).values()
            if isinstance(value, type)
            and issubclass(value, django_forms.BaseForm)
            and value.__module__ == inventory_forms.__name__
        ]
        assert declared
        for form_class in declared:
            assert not issubclass(form_class, django_forms.BaseModelForm), form_class.__name__

    def test_no_view_calls_form_save_or_writes_a_model_directly(self) -> None:
        """
        Read from the parsed module, not from its text — a docstring that
        merely *mentions* `form.save()` must not make this pass or fail.
        """
        import ast
        from pathlib import Path

        from apps.inventory import views

        tree = ast.parse(Path(views.__file__).read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "save" not in called
        assert "create" not in called
        assert "delete" not in called


class TestSuccessIsVisible:
    def test_a_message_survives_the_redirect(
        self, manager: User, client_for: Any, organization: Organization
    ) -> None:
        response = client_for(manager).post(
            reverse("inventory:category_create"),
            {
                "organization": organization.pk,
                "code": "SWEETS",
                "name": "حلويات",
                "parent": "",
            },
            follow=True,
        )

        assert response.status_code == 200
        assert "تمت إضافة المجموعة." in response.content.decode()
