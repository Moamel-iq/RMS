"""
The inventory screens, the master-data API, and the admin lockdown.

The screens matter here for one reason above appearance: they must show a
caller only their own organization's data, and they must not send a
storekeeper to Django admin to do their job.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.contrib import admin
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    ItemCategory,
    ItemType,
    PackageUnit,
    Warehouse,
)
from apps.inventory.services import create_item_conversion
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import TODAY

pytestmark = pytest.mark.django_db

SCREENS = [
    "inventory:item_list",
    "inventory:category_list",
    "inventory:package_unit_list",
    "inventory:conversion_list",
]


class TestScreensLiveInTheShell:
    @pytest.mark.parametrize("url_name", SCREENS)
    def test_a_manager_may_enter(self, manager: User, client_for: Any, url_name: str) -> None:
        response = client_for(manager).get(reverse(url_name))
        assert response.status_code == 200

    @pytest.mark.parametrize("url_name", SCREENS)
    def test_anonymous_is_redirected_to_login(self, client: Client, url_name: str) -> None:
        response = client.get(reverse(url_name))
        assert response.status_code == 302
        assert "/login" in response["Location"]

    def test_a_storekeeper_reaches_the_item_master_without_being_staff(
        self, storekeeper: User, client_for: Any
    ) -> None:
        """
        Access is by inventory permission, not by the staff flag. A
        storekeeper is not staff and must still see what they are moving —
        and must never be sent to Django admin to do it.
        """
        assert storekeeper.is_staff is False

        response = client_for(storekeeper).get(reverse("inventory:item_list"))

        assert response.status_code == 200
        assert "/admin/" not in response.content.decode()

    def test_a_storekeeper_cannot_reach_the_warehouse_screen(
        self, storekeeper: User, client_for: Any
    ) -> None:
        """Warehouse administration needs `manage_warehouses`."""
        response = client_for(storekeeper).get(reverse("inventory:warehouse_list"))
        assert response.status_code == 403

    def test_the_screen_renders_inside_the_shell_and_is_rtl(
        self, manager: User, client_for: Any, rice: InventoryItem
    ) -> None:
        # Test settings default to English for deterministic assertions, so
        # Arabic is selected explicitly here as a user would.
        client = client_for(manager)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"

        body = client.get(reverse("inventory:item_list")).content.decode()

        assert 'dir="rtl"' in body
        assert "الأصناف" in body
        assert rice.code in body

    def test_the_inventory_module_is_highlighted(self, manager: User, client_for: Any) -> None:
        body = client_for(manager).get(reverse("inventory:item_list")).content.decode()
        assert "المخزون" in body

    def test_item_conversions_has_its_own_screen(self, manager: User, client_for: Any) -> None:
        """The section Task 1.0's review found missing from the rail."""
        response = client_for(manager).get(reverse("inventory:conversion_list"))
        assert response.status_code == 200
        assert "تحويلات وحدات الصنف" in response.content.decode()


class TestScreensAreScoped:
    def test_a_rival_sees_none_of_our_items(
        self, rival_manager: User, client_for: Any, rice: InventoryItem
    ) -> None:
        body = client_for(rival_manager).get(reverse("inventory:item_list")).content.decode()
        assert rice.code not in body

    def test_a_rival_sees_none_of_our_warehouses(
        self,
        rival_manager: User,
        client_for: Any,
        main_store: Warehouse,
        other_warehouse: Warehouse,
    ) -> None:
        body = client_for(rival_manager).get(reverse("inventory:warehouse_list")).content.decode()
        assert other_warehouse.code in body
        assert "المخزن الرئيسي" not in body


class TestFactorsRenderLocaleIndependently:
    def test_the_conversion_screen_shows_a_period_under_arabic(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        create_item_conversion(
            item=rice, package_unit=sack, factor_to_base=Decimal("0.8"), effective_from=TODAY
        )

        with translation.override("ar"):
            body = client_for(manager).get(reverse("inventory:conversion_list")).content.decode()

        assert "0.800000000000" in body
        assert "0,800000000000" not in body


class TestMasterDataApi:
    def test_authentication_is_required(self, client: Client) -> None:
        assert client.get("/api/v1/inventory/items/").status_code == 401

    def test_a_manager_can_create_and_read_an_item(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        client = client_for(manager)

        created = client.post(
            "/api/v1/inventory/items/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "code": "oil-1",
                    "name_ar": "زيت",
                    "category_id": leaf_category.pk,
                    "item_type": ItemType.RAW_MATERIAL,
                    "base_unit_id": kilogram.pk,
                }
            ),
            content_type="application/json",
        )

        assert created.status_code == 201
        body = created.json()
        assert body["code"] == "OIL-1"  # canonicalised on the way in
        assert body["is_variable_weight"] is False

    def test_a_foreign_organization_id_is_a_404(
        self,
        rival_manager: User,
        client_for: Any,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        """404, not 403 — another organization's records do not exist for them."""
        response = client_for(rival_manager).post(
            "/api/v1/inventory/items/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "code": "STEAL",
                    "name_ar": "سرقة",
                    "category_id": leaf_category.pk,
                    "item_type": ItemType.RAW_MATERIAL,
                    "base_unit_id": kilogram.pk,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_a_foreign_item_id_is_a_404(
        self, rival_manager: User, client_for: Any, rice: InventoryItem
    ) -> None:
        response = client_for(rival_manager).get(f"/api/v1/inventory/items/{rice.pk}/")
        assert response.status_code == 404

    def test_a_storekeeper_cannot_create_an_item(
        self,
        storekeeper: User,
        client_for: Any,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        """Reachable organization, missing permission — 403."""
        response = client_for(storekeeper).post(
            "/api/v1/inventory/items/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "code": "NOPE",
                    "name_ar": "لا",
                    "category_id": leaf_category.pk,
                    "item_type": ItemType.RAW_MATERIAL,
                    "base_unit_id": kilogram.pk,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 403

    def test_a_non_leaf_category_is_refused_with_422(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        category: ItemCategory,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        response = client_for(manager).post(
            "/api/v1/inventory/items/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "code": "BAD",
                    "name_ar": "خطأ",
                    "category_id": category.pk,
                    "item_type": ItemType.RAW_MATERIAL,
                    "base_unit_id": kilogram.pk,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 422
        assert response.json()["code"] == "category_has_children"

    def test_the_item_list_is_scoped(
        self, rival_manager: User, manager: User, client_for: Any, rice: InventoryItem
    ) -> None:
        assert len(client_for(manager).get("/api/v1/inventory/items/").json()) == 1
        assert client_for(rival_manager).get("/api/v1/inventory/items/").json() == []


class TestApiDecimalTransport:
    def test_a_factor_arrives_and_leaves_as_an_exact_string(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        """
        A conversion factor is a technical identity. JSON's only numeric type
        is binary floating point, so a bare 0.8 on the wire would already have
        been through a float before any Python code saw it.
        """
        client = client_for(manager)

        created = client.post(
            "/api/v1/inventory/conversions/",
            data=json.dumps(
                {
                    "item_id": rice.pk,
                    "package_unit_id": sack.pk,
                    "factor_to_base": "0.123456789012",
                    "effective_from": TODAY.isoformat(),
                }
            ),
            content_type="application/json",
        )

        assert created.status_code == 201
        body = created.json()
        assert body["factor_to_base"] == "0.123456789012"

        raw = created.content.decode("utf-8")
        assert '"factor_to_base": "0.123456789012"' in raw or (
            '"factor_to_base":"0.123456789012"' in raw
        )
        assert '"factor_to_base": 0.123456789012' not in raw

    def test_the_stored_factor_is_the_exact_decimal(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        client_for(manager).post(
            "/api/v1/inventory/conversions/",
            data=json.dumps(
                {
                    "item_id": rice.pk,
                    "package_unit_id": sack.pk,
                    "factor_to_base": "30",
                    "effective_from": TODAY.isoformat(),
                }
            ),
            content_type="application/json",
        )

        conversion = rice.package_conversions.get()
        assert isinstance(conversion.factor_to_base, Decimal)
        assert conversion.factor_to_base == Decimal("30.000000000000")

    def test_a_malformed_factor_is_a_422(
        self, manager: User, client_for: Any, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        response = client_for(manager).post(
            "/api/v1/inventory/conversions/",
            data=json.dumps(
                {
                    "item_id": rice.pk,
                    "package_unit_id": sack.pk,
                    "factor_to_base": "not a number",
                    "effective_from": TODAY.isoformat(),
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_decimal"

    def test_a_variable_conversion_marks_the_item_variable_weight(
        self, manager: User, client_for: Any, rice: InventoryItem, carton: PackageUnit
    ) -> None:
        client = client_for(manager)
        client.post(
            "/api/v1/inventory/conversions/",
            data=json.dumps(
                {
                    "item_id": rice.pk,
                    "package_unit_id": carton.pk,
                    "factor_to_base": "17.5",
                    "effective_from": TODAY.isoformat(),
                    "conversion_type": ConversionType.VARIABLE,
                }
            ),
            content_type="application/json",
        )

        body = client.get(f"/api/v1/inventory/items/{rice.pk}/").json()
        assert body["is_variable_weight"] is True


class TestAdminLockdown:
    MODELS = [
        ItemCategory,
        PackageUnit,
        InventoryItem,
        Warehouse,
    ]

    @pytest.mark.parametrize("model", MODELS)
    def test_registered_and_read_only(self, model: type, superuser: User, rf: Any) -> None:
        """
        Read-only for superusers too. An admin form would skip the code
        canonicalisation, the category checks, the conversion versioning, and
        the audit event.
        """
        assert model in admin.site._registry

        model_admin = admin.site._registry[model]
        request = rf.get("/admin/")
        request.user = superuser

        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)
        assert model_admin.actions is None

    def test_the_add_page_is_refused(self, superuser: User) -> None:
        client = Client()
        client.force_login(superuser)

        response = client.get(reverse("admin:inventory_inventoryitem_add"))

        assert response.status_code == 403

    def test_an_item_cannot_be_edited_through_the_admin(
        self, superuser: User, rice: InventoryItem
    ) -> None:
        client = Client()
        client.force_login(superuser)
        url = reverse("admin:inventory_inventoryitem_change", args=[rice.pk])

        assert client.get(url).status_code == 200
        client.post(url, data={"name_ar": "اسم مزيف"})

        rice.refresh_from_db()
        assert rice.name_ar != "اسم مزيف"
