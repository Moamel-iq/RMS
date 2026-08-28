"""Direct-stock menu maintenance screens and their scoped selectors."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.forms import ModelChoiceField
from django.urls import reverse

from apps.inventory.models import InventoryItem, ItemType
from apps.inventory.services import create_item, create_item_category, create_warehouse
from apps.organizations.models import Branch, Organization
from apps.sales.forms import BranchAvailabilityForm, MenuItemForm
from apps.sales.models import FulfillmentSource, MenuItem, MenuItemBranchSetting
from apps.sales.services import create_menu_item
from apps.units.models import Dimension, UnitOfMeasure
from apps.units.services import create_unit
from apps.users.models import User


@pytest.fixture
def count_unit() -> UnitOfMeasure:
    existing = UnitOfMeasure.objects.filter(dimension=Dimension.COUNT, is_active=True).first()
    if existing is not None:
        return existing
    return create_unit(
        code="PIECE",
        name="حبة",
        dimension=Dimension.COUNT,
        factor_to_base=Decimal("1"),
        is_base=True,
    )


@pytest.fixture
def direct_stock_candidates(
    organization: Organization,
    count_unit: UnitOfMeasure,
) -> dict[str, InventoryItem]:
    category = create_item_category(
        organization=organization,
        code="RESALE",
        name="إعادة البيع",
    )
    rows = {}
    for key, code, item_type, tracks_lots in (
        ("resale", "WATER", ItemType.GOODS_FOR_RESALE, False),
        ("raw", "RAW-WATER", ItemType.RAW_MATERIAL, False),
        ("lot", "LOT-DRINK", ItemType.GOODS_FOR_RESALE, True),
    ):
        rows[key] = create_item(
            organization=organization,
            code=code,
            name=code,
            category=category,
            item_type=item_type,
            base_unit=count_unit,
            tracks_lots=tracks_lots,
        )
    return rows


@pytest.fixture
def direct_menu_item(
    organization: Organization,
    direct_stock_candidates: dict[str, InventoryItem],
) -> MenuItem:
    return create_menu_item(
        organization=organization,
        code="MENU-WATER",
        name="ماء",
        fulfillment_source=FulfillmentSource.DIRECT_STOCK,
        inventory_item=direct_stock_candidates["resale"],
        direct_stock_base_quantity=Decimal("1"),
    )


@pytest.mark.django_db
class TestDirectStockMenuForm:
    def test_only_certified_resale_items_are_offered(
        self,
        manager: User,
        organization: Organization,
        direct_stock_candidates: dict[str, InventoryItem],
    ) -> None:
        form = MenuItemForm(actor=manager)
        field = form.fields["inventory_item"]
        assert isinstance(field, ModelChoiceField)
        assert field.queryset is not None

        assert list(field.queryset) == [direct_stock_candidates["resale"]]

    def test_direct_route_requires_item_and_quantity_and_clears_recipe_fields(
        self,
        manager: User,
        organization: Organization,
        direct_stock_candidates: dict[str, InventoryItem],
    ) -> None:
        form = MenuItemForm(
            actor=manager,
            data={
                "organization": organization.pk,
                "code": "menu-water",
                "name": "ماء",
                "fulfillment_source": FulfillmentSource.DIRECT_STOCK,
                "inventory_item": direct_stock_candidates["resale"].pk,
                "direct_stock_base_quantity": "1",
                "display_order": "1",
            },
        )

        assert form.is_valid(), form.errors
        assert form.cleaned_data["recipe"] is None
        assert form.cleaned_data["serving_code"] == ""
        assert form.cleaned_data["direct_stock_base_quantity"] == Decimal("1")


@pytest.mark.django_db
class TestDirectStockBranchForm:
    def test_direct_item_requires_a_source_warehouse(
        self,
        manager: User,
        branch: Branch,
        direct_menu_item: MenuItem,
    ) -> None:
        create_warehouse(branch=branch, code="MAIN", name="المخزن الرئيسي")
        form = BranchAvailabilityForm(
            actor=manager,
            menu_item=direct_menu_item,
            data={"branch": branch.pk, "is_available": "on"},
        )

        assert not form.is_valid()
        assert "source_warehouse" in form.errors

    def test_warehouse_must_belong_to_the_selected_branch(
        self,
        superuser: User,
        branch: Branch,
        second_branch: Branch,
        direct_menu_item: MenuItem,
    ) -> None:
        create_warehouse(branch=branch, code="MAIN", name="المخزن الرئيسي")
        other = create_warehouse(branch=second_branch, code="MAIN", name="مخزن الفرع الآخر")
        form = BranchAvailabilityForm(
            actor=superuser,
            menu_item=direct_menu_item,
            data={
                "branch": branch.pk,
                "source_warehouse": other.pk,
                "is_available": "on",
            },
        )

        assert not form.is_valid()
        assert "source_warehouse" in form.errors


@pytest.mark.django_db
class TestDirectStockMenuScreens:
    def test_create_and_branch_assignment_are_visible_and_htmx_safe(
        self,
        manager: User,
        branch: Branch,
        organization: Organization,
        direct_stock_candidates: dict[str, InventoryItem],
        client_for: Any,
    ) -> None:
        warehouse = create_warehouse(branch=branch, code="MAIN", name="المخزن الرئيسي")
        client = client_for(manager)
        create_url = reverse("sales:menu_item_create")

        fragment = client.get(create_url, headers={"HX-Request": "true"})
        assert fragment.status_code == 200
        assert b"<html" not in fragment.content.lower()
        assert b'data-fulfillment-panel="DIRECT_STOCK"' in fragment.content

        response = client.post(
            create_url,
            {
                "organization": organization.pk,
                "code": "MENU-WATER",
                "name": "ماء",
                "fulfillment_source": FulfillmentSource.DIRECT_STOCK,
                "inventory_item": direct_stock_candidates["resale"].pk,
                "direct_stock_base_quantity": "1",
                "display_order": "1",
            },
        )
        assert response.status_code == 302

        item = MenuItem.objects.get(code="MENU-WATER")
        detail_url = reverse("sales:menu_item_detail", args=[item.pk])
        response = client.post(
            detail_url,
            {
                "branch": branch.pk,
                "source_warehouse": warehouse.pk,
                "is_available": "on",
            },
        )
        assert response.status_code == 302
        setting = MenuItemBranchSetting.objects.get(menu_item=item, branch=branch)
        assert setting.source_warehouse == warehouse

        detail = client.get(detail_url)
        page = detail.content.decode()
        assert "WATER" in page
        assert "MAIN" in page
        assert "1" in page
