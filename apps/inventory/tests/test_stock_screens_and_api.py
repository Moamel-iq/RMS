"""
The read-only stock API and screens.

Two claims are worth more than the rest here:

* **There is no way to write a movement through the API.** Not a blocked one,
  not a permission-gated one — none exists.
* **Quantity and cost are different permissions.** A storekeeper must know
  what they are moving and has no business knowing what it cost, so the cost
  columns are *absent* for them rather than blank.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounting.services import open_fiscal_year
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import InventoryItem, MovementType, StockMovement, Warehouse
from apps.organizations.models import Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db

STOCK_URL = "/api/v1/inventory/stock/"
MOVEMENTS_URL = "/api/v1/inventory/movements/"


@pytest.fixture
def open_year(organization: Organization) -> None:
    open_fiscal_year(organization=organization, year=timezone.localdate().year)


@pytest.fixture
def posted(
    organization: Organization, main_store: Warehouse, rice: InventoryItem, open_year: None
) -> StockMovement:
    """One receipt, so the screens and the API have something to show."""
    post_stock_entry(
        organization=organization,
        effects=[
            MovementInput(
                warehouse=main_store,
                item=rice,
                movement_type=MovementType.RECEIPT,
                quantity=Decimal("12.5"),
                unit_cost=Decimal("1234.567"),
                effect_key="line:1",
            )
        ],
        idempotency_key="seed",
        source_document_type="GOODS_RECEIPT",
        source_document_id="145",
    )
    return StockMovement.objects.get()


class TestThereIsNoWritePath:
    def test_the_api_exposes_no_movement_write_endpoint(self) -> None:
        """
        Checked against the routing table, not against a guess. A generic
        write over `StockMovement` would put a row in the ledger without the
        lock, the availability check, or the average that make it meaningful.
        """
        from config.api import api

        writes = [
            (operation.methods, path)
            for _prefix, router in api._routers  # noqa: SLF001 - the routing table is the fact
            for path, view in router.path_operations.items()
            for operation in view.operations
            if set(operation.methods) & {"POST", "PUT", "PATCH", "DELETE"}
            and ("movement" in path or "/stock" in path)
        ]
        assert writes == []

    def test_the_stock_endpoints_are_get_only(self, manager: User, client_for: Any) -> None:
        client = client_for(manager)
        for url in (STOCK_URL, MOVEMENTS_URL):
            assert client.post(url, {}, content_type="application/json").status_code in (
                404,
                405,
            )


class TestStockApi:
    def test_a_manager_sees_quantity_and_cost(
        self, manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        response = client_for(manager).get(STOCK_URL)
        assert response.status_code == 200
        row = response.json()[0]

        assert row["quantity"] == "12.500"
        assert row["value"] == "15432.088"
        assert row["average_cost"] == "1234.567040"

    def test_decimals_cross_the_wire_as_strings(
        self, manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        """
        Read from the raw bytes. `response.json()` would have already turned a
        JSON number into a float, hiding exactly the defect this guards.
        """
        body = client_for(manager).get(STOCK_URL).content.decode()
        assert '"quantity": "12.500"' in body or '"quantity":"12.500"' in body
        assert "12.5," not in body

    def test_a_storekeeper_sees_quantity_and_no_cost_at_all(
        self, storekeeper: User, client_for: Any, posted: StockMovement
    ) -> None:
        response = client_for(storekeeper).get(STOCK_URL)
        assert response.status_code == 200
        row = response.json()[0]

        assert row["quantity"] == "12.500"
        # Absent, not blank. A null where a number belongs still says there is
        # a number to be had.
        assert row["value"] is None
        assert row["average_cost"] is None
        assert "1234" not in response.content.decode()

    def test_a_rival_sees_nothing_of_ours(
        self, rival_manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        assert client_for(rival_manager).get(STOCK_URL).json() == []

    def test_a_cashier_is_refused(
        self, branch: Any, client_for: Any, posted: StockMovement
    ) -> None:
        from apps.organizations.models import Role
        from apps.organizations.services import grant_branch_access

        from .conftest import PASSWORD

        cashier = User.objects.create_user(username="cashier", password=PASSWORD)
        grant_branch_access(user=cashier, branch=branch, role=Role.CASHIER)
        cashier = User.objects.get(pk=cashier.pk)

        assert client_for(cashier).get(STOCK_URL).status_code == 403


class TestMovementApi:
    def test_the_history_carries_the_source_identity(
        self, manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        row = client_for(manager).get(MOVEMENTS_URL).json()[0]
        assert row["source_document_type"] == "GOODS_RECEIPT"
        assert row["source_document_id"] == "145"
        assert row["source_event"] == "POSTED"
        assert row["posted_sequence"] == 1
        assert row["base_quantity"] == "12.500"

    def test_one_movement_can_be_read(
        self, manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        response = client_for(manager).get(f"{MOVEMENTS_URL}{posted.pk}/")
        assert response.status_code == 200
        assert response.json()["id"] == posted.pk

    def test_a_foreign_movement_is_a_404_not_a_403(
        self, rival_manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        """
        A 403 would confirm the id names a real movement, and ids are
        sequential. Absent and foreign must be indistinguishable.
        """
        response = client_for(rival_manager).get(f"{MOVEMENTS_URL}{posted.pk}/")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_a_storekeeper_sees_the_movement_without_its_value(
        self, storekeeper: User, client_for: Any, posted: StockMovement
    ) -> None:
        row = client_for(storekeeper).get(MOVEMENTS_URL).json()[0]
        assert row["base_quantity"] == "12.500"
        assert row["inventory_value"] is None
        assert row["unit_cost"] is None


class TestStockScreens:
    def test_the_stock_screen_renders(
        self, manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        response = client_for(manager).get(reverse("inventory:stock_list"))
        assert response.status_code == 200
        page = response.content.decode()
        assert "RICE-272" in page
        assert "12.500" in page

    def test_the_stock_screen_hides_cost_from_a_storekeeper(
        self, storekeeper: User, client_for: Any, posted: StockMovement
    ) -> None:
        response = client_for(storekeeper).get(reverse("inventory:stock_list"))
        assert response.status_code == 200
        assert response.context["show_cost"] is False
        assert "1234.567" not in response.content.decode()

    def test_the_movement_screen_renders_in_posted_order(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        posted: StockMovement,
    ) -> None:
        post_stock_entry(
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=main_store,
                    item=rice,
                    movement_type=MovementType.ISSUE,
                    quantity=Decimal("2"),
                    effect_key="line:1",
                )
            ],
            idempotency_key="second",
        )
        response = client_for(manager).get(reverse("inventory:movement_list"))
        sequences = [m.posted_sequence for m in response.context["movements"]]
        assert sequences == [2, 1]

    def test_the_detail_screen_shows_before_and_after(
        self, manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        response = client_for(manager).get(reverse("inventory:movement_detail", args=[posted.pk]))
        assert response.status_code == 200
        page = response.content.decode()
        assert "GOODS_RECEIPT/145/POSTED" in page
        assert "12.500" in page

    def test_a_foreign_movement_detail_is_a_404(
        self, rival_manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        response = client_for(rival_manager).get(
            reverse("inventory:movement_detail", args=[posted.pk])
        )
        assert response.status_code == 404

    def test_a_technical_decimal_keeps_its_point_under_arabic(
        self, manager: User, client_for: Any, posted: StockMovement
    ) -> None:
        """
        Django localises Decimals, so under Arabic a quantity would otherwise
        render `12,500`. A comma there is ambiguous and invites a mis-read.
        """
        client = client_for(manager)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        page = client.get(reverse("inventory:stock_list")).content.decode()

        assert 'dir="rtl"' in page
        assert "12.500" in page
        assert "12,500" not in page

    def test_the_screens_are_reachable_from_the_rail(self, manager: User, client_for: Any) -> None:
        page = client_for(manager).get(reverse("inventory:item_list")).content.decode()
        assert reverse("inventory:stock_list") in page
        assert reverse("inventory:movement_list") in page


class TestAdminCannotMutateTheLedger:
    def test_the_ledger_models_are_registered_read_only(self, superuser: User) -> None:
        from django.contrib import admin

        from apps.inventory.models import (
            InventoryLot,
            StockBalance,
            StockLedgerEntry,
            ValuationLayer,
        )
        from apps.inventory.models import (
            StockMovement as Movement,
        )

        for model in (InventoryLot, StockLedgerEntry, Movement, StockBalance, ValuationLayer):
            registered = admin.site._registry[model]  # noqa: SLF001 - the registry is the fact
            assert registered.has_add_permission(None) is False  # type: ignore[arg-type]
            assert registered.has_change_permission(None) is False  # type: ignore[arg-type]
            assert registered.has_delete_permission(None) is False  # type: ignore[arg-type]

    def test_the_movement_add_page_is_refused(self, superuser: User, posted: StockMovement) -> None:
        client = Client()
        client.force_login(superuser)
        assert client.get("/admin/inventory/stockmovement/add/").status_code == 403
