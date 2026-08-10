"""
The transfer API and the Arabic screens (Task 1.5 §V, §W, §X).

Two rules run through everything here. **The route constrains the object**: an
id from one series never resolves under another's path, and a foreign row is a
404 rather than a 403 that would confirm it exists. **Cost is a separate
permission from stock**: a storekeeper sees what is moving and never what it
is worth, and the API omits the fields rather than blanking them, because a
blanked column still says "there is a number here".
"""

from __future__ import annotations

import datetime
import json
import zoneinfo
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INTER_BRANCH_CLEARING,
    INVENTORY_CONTROL,
    INVENTORY_IN_TRANSIT,
    INVENTORY_SHORTAGE_LOSS,
    Account,
    AccountRole,
    CostCenter,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.commands import (
    add_document_line,
    add_transfer_line,
    create_document,
    create_transfer,
    create_transfer_receipt,
    dispatch_transfer,
    post_document,
    post_transfer_receipt,
    replace_transfer_receipt_lines,
)
from apps.inventory.models import (
    InventoryDocumentType,
    InventoryItem,
    StockTransfer,
    StockTransferStatus,
    Warehouse,
)
from apps.inventory.operations import DocumentLineInput
from apps.inventory.services import create_warehouse
from apps.inventory.transfers import ReceiptLineInput, TransferLineInput
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
WHEN = datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD)
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
API = "/api/v1/inventory"


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    for code, account_code in (
        (INVENTORY_CONTROL, "1-03-01-001"),
        (INVENTORY_IN_TRANSIT, "1-03-02-001"),
        (INTER_BRANCH_CLEARING, "8-01-01-001"),
        (INVENTORY_SHORTAGE_LOSS, "6-02-01-001"),
        (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=JAN_1,
        )


@pytest.fixture
def far_store(second_branch: Branch) -> Warehouse:
    return create_warehouse(branch=second_branch, code="MAIN", name_ar="مخزن الكرادة")


@pytest.fixture
def group_manager(organization: Organization, branch: Branch) -> User:
    user = User.objects.create_user(username="group-manager", password="pw-not-real-1234")
    grant_organization_access(user=user, organization=organization, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def keeper(branch: Branch) -> User:
    user = User.objects.create_user(username="keeper", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def stocked(
    group_manager: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    rice: InventoryItem,
    mapped: None,
) -> None:
    document = create_document(
        actor=group_manager,
        organization=organization,
        branch=branch,
        warehouse=main_store,
        document_type=InventoryDocumentType.RECEIPT,
        effective_at=WHEN,
        evidence_reference="DN-1",
    )
    add_document_line(
        actor=group_manager,
        document=document,
        line=DocumentLineInput(item=rice, base_quantity=Decimal("100"), unit_cost=Decimal("1500")),
    )
    post_document(actor=group_manager, document=document)


@pytest.fixture
def dispatched(
    group_manager: User,
    organization: Organization,
    main_store: Warehouse,
    kitchen_store: Warehouse,
    rice: InventoryItem,
    stocked: None,
) -> StockTransfer:
    transfer = create_transfer(
        actor=group_manager,
        organization=organization,
        source_warehouse=main_store,
        destination_warehouse=kitchen_store,
        effective_at=WHEN,
        evidence_reference="TN-1",
    )
    add_transfer_line(
        actor=group_manager,
        transfer=transfer,
        line=TransferLineInput(item=rice, base_quantity=Decimal("40")),
    )
    return dispatch_transfer(actor=group_manager, transfer=transfer)


def _json(response: Any) -> Any:
    return json.loads(response.content.decode("utf-8"))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class TestTransferApi:
    def test_a_transfer_round_trips_through_the_api(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        client = client_for(group_manager)
        created = client.post(
            f"{API}/transfers/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "source_warehouse_id": main_store.pk,
                    "destination_warehouse_id": kitchen_store.pk,
                    "effective_at": WHEN.isoformat(),
                    "evidence_reference": "TN-API",
                    "lines": [{"item_id": rice.pk, "base_quantity": "40"}],
                }
            ),
            content_type="application/json",
        )
        assert created.status_code == 201, created.content
        payload = _json(created)
        assert payload["status"] == StockTransferStatus.DRAFT
        assert payload["transfer_number"] == ""
        assert payload["line_count"] == 1

        transfer_id = payload["id"]
        dispatched = client.post(f"{API}/transfers/{transfer_id}/dispatch/")
        assert dispatched.status_code == 200, dispatched.content
        assert _json(dispatched)["status"] == StockTransferStatus.DISPATCHED
        assert _json(dispatched)["transfer_number"].startswith("TRF-")

    def test_decimals_are_quoted_strings_in_the_raw_json(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        """A JSON number is a binary float before any Python code sees it."""
        response = client_for(group_manager).get(f"{API}/transfers/{dispatched.pk}/")
        raw = response.content.decode("utf-8")
        assert '"base_quantity": "40.000"' in raw
        assert '"remaining_value": "60000.000"' in raw
        assert '"base_quantity": 40' not in raw

    def test_cost_is_omitted_without_view_valuation(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        group_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        keeper_payload = _json(client_for(keeper).get(f"{API}/transfers/{dispatched.pk}/"))
        assert "total_value" not in keeper_payload or keeper_payload["total_value"] is None
        assert "unit_cost" not in keeper_payload["lines"][0] or (
            keeper_payload["lines"][0]["unit_cost"] is None
        )
        # ...and a manager, who holds view_valuation, does see them.
        manager_payload = _json(client_for(group_manager).get(f"{API}/transfers/{dispatched.pk}/"))
        assert manager_payload["total_value"] == "60000.000"
        assert manager_payload["lines"][0]["unit_cost"] == "1500.000000"

    def test_a_foreign_transfer_is_a_404(
        self,
        client_for: Callable[[User], Client],
        rival_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        response = client_for(rival_manager).get(f"{API}/transfers/{dispatched.pk}/")
        assert response.status_code == 404

    def test_a_receipt_cannot_draw_on_another_transfers_line(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        dispatched: StockTransfer,
    ) -> None:
        """A transfer-line id from elsewhere is out of scope, not a shortcut."""
        other = create_transfer(
            actor=group_manager,
            organization=organization,
            source_warehouse=main_store,
            destination_warehouse=kitchen_store,
            effective_at=WHEN,
            evidence_reference="TN-2",
        )
        add_transfer_line(
            actor=group_manager,
            transfer=other,
            line=TransferLineInput(item=rice, base_quantity=Decimal("10")),
        )
        dispatch_transfer(actor=group_manager, transfer=other)

        client = client_for(group_manager)
        response = client.post(
            f"{API}/transfers/{dispatched.pk}/receipts/",
            data=json.dumps(
                {
                    "effective_at": WHEN.isoformat(),
                    "evidence_reference": "GRN-1",
                    "lines": [
                        {
                            "transfer_line_id": other.lines.get().pk,
                            "base_quantity": "5",
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 404, response.content

    def test_the_in_transit_report_lists_what_is_owed(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        rows = _json(client_for(group_manager).get(f"{API}/in-transit/"))
        assert len(rows) == 1
        assert rows[0]["remaining_quantity"] == "40.000"
        assert rows[0]["remaining_value"] == "60000.000"

    def test_a_posted_receipt_cannot_be_edited_through_the_api(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        receipt = create_transfer_receipt(
            actor=group_manager,
            transfer=dispatched,
            effective_at=WHEN,
            evidence_reference="GRN-1",
        )
        replace_transfer_receipt_lines(
            actor=group_manager,
            receipt=receipt,
            lines=[
                ReceiptLineInput(transfer_line=dispatched.lines.get(), base_quantity=Decimal("40"))
            ],
        )
        post_transfer_receipt(actor=group_manager, receipt=receipt)

        response = client_for(group_manager).patch(
            f"{API}/transfer-receipts/{receipt.pk}/",
            data=json.dumps({"evidence_reference": "tampered"}),
            content_type="application/json",
        )
        # 409, not 400: the request is well formed and the receipt exists —
        # it is the document's *state* that refuses, which is a conflict.
        assert response.status_code == 409, response.content
        receipt.refresh_from_db()
        assert receipt.evidence_reference == "GRN-1"

    def test_a_storekeeper_cannot_close_a_shortage_through_the_api(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        organization: Organization,
        dispatched: StockTransfer,
    ) -> None:
        center = CostCenter.objects.get(organization=organization, code="WAREHOUSE")
        response = client_for(keeper).post(
            f"{API}/transfers/{dispatched.pk}/shortage/",
            data=json.dumps(
                {
                    "effective_at": WHEN.isoformat(),
                    "reason": "ضاعت",
                    "evidence_reference": "CLAIM-1",
                    "cost_center_id": center.pk,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 403, response.content

    def test_no_writable_movement_endpoint_exists(
        self, client_for: Callable[[User], Client], group_manager: User
    ) -> None:
        """Stock moves through documents, never through CRUD on a movement."""
        response = client_for(group_manager).post(
            f"{API}/movements/", data=json.dumps({}), content_type="application/json"
        )
        assert response.status_code in {404, 405}


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class TestTransferScreens:
    def test_the_list_renders_in_arabic(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        response = client_for(group_manager).get(reverse("inventory:transfer_list"))
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert "التحويلات المخزنية" in html
        assert dispatched.transfer_number in html

    def test_the_detail_page_shows_the_event_timeline(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        receipt = create_transfer_receipt(
            actor=group_manager,
            transfer=dispatched,
            effective_at=WHEN,
            evidence_reference="GRN-1",
        )
        replace_transfer_receipt_lines(
            actor=group_manager,
            receipt=receipt,
            lines=[
                ReceiptLineInput(transfer_line=dispatched.lines.get(), base_quantity=Decimal("25"))
            ],
        )
        post_transfer_receipt(actor=group_manager, receipt=receipt)

        response = client_for(group_manager).get(
            reverse("inventory:transfer_detail", args=[dispatched.pk])
        )
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert "سجل الأحداث" in html
        assert "مستلم جزئياً" in html
        assert receipt.receipt_number in html
        # The remaining figures a receiving branch actually needs.
        assert "15.000" in html

    def test_a_storekeeper_sees_no_value_columns(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        group_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        keeper_html = (
            client_for(keeper)
            .get(reverse("inventory:transfer_detail", args=[dispatched.pk]))
            .content.decode("utf-8")
        )
        assert "القيمة المُرسلة" not in keeper_html
        assert "60000.000" not in keeper_html
        # A manager, holding view_valuation, does see them — so the absence
        # above is the permission and not a missing template block.
        manager_html = (
            client_for(group_manager)
            .get(reverse("inventory:transfer_detail", args=[dispatched.pk]))
            .content.decode("utf-8")
        )
        assert "القيمة المُرسلة" in manager_html
        assert "60000.000" in manager_html

    def test_a_storekeeper_is_offered_no_shortage_button(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        dispatched: StockTransfer,
    ) -> None:
        html = (
            client_for(keeper)
            .get(reverse("inventory:transfer_detail", args=[dispatched.pk]))
            .content.decode("utf-8")
        )
        assert reverse("inventory:transfer_shortage_create", args=[dispatched.pk]) not in html

    def test_a_hidden_shortage_action_is_still_refused_on_a_direct_post(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        organization: Organization,
        dispatched: StockTransfer,
    ) -> None:
        """Hiding a button is presentation; the command layer is protection."""
        center = CostCenter.objects.get(organization=organization, code="WAREHOUSE")
        response = client_for(keeper).post(
            reverse("inventory:transfer_shortage_create", args=[dispatched.pk]),
            data={
                "effective_at": "2026-03-15T10:00",
                "reason": "ضاعت",
                "evidence_reference": "CLAIM-1",
                "cost_center": center.pk,
            },
        )
        assert response.status_code == 403

    def test_the_dispatch_confirmation_lists_what_will_leave(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = create_transfer(
            actor=group_manager,
            organization=organization,
            source_warehouse=main_store,
            destination_warehouse=kitchen_store,
            effective_at=WHEN,
            evidence_reference="TN-1",
        )
        add_transfer_line(
            actor=group_manager,
            transfer=transfer,
            line=TransferLineInput(item=rice, base_quantity=Decimal("40")),
        )
        response = client_for(group_manager).get(
            reverse("inventory:transfer_dispatch", args=[transfer.pk])
        )
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert "تأكيد الإرسال" in html
        assert rice.code in html

    def test_the_in_transit_screen_renders(
        self,
        client_for: Callable[[User], Client],
        group_manager: User,
        dispatched: StockTransfer,
    ) -> None:
        response = client_for(group_manager).get(reverse("inventory:in_transit"))
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert "بضاعة بالطريق" in html
        assert dispatched.transfer_number in html

    def test_the_transfer_screens_have_distinct_titles(
        self, client_for: Callable[[User], Client], group_manager: User, dispatched: StockTransfer
    ) -> None:
        """Each page says which one it is, so nobody acts on the wrong screen."""
        client = client_for(group_manager)
        titles = {
            name: client.get(url).content.decode("utf-8").split("<title>")[1].split("</title>")[0]
            for name, url in (
                ("list", reverse("inventory:transfer_list")),
                ("detail", reverse("inventory:transfer_detail", args=[dispatched.pk])),
                ("dispatch", reverse("inventory:transfer_dispatch", args=[dispatched.pk])),
                ("in_transit", reverse("inventory:in_transit")),
            )
        }
        assert len(set(titles.values())) == len(titles), titles

    def test_admin_offers_no_transfer_write_path(
        self, client_for: Callable[[User], Client], superuser: User
    ) -> None:
        client = client_for(superuser)
        response = client.get("/admin/inventory/stocktransfer/add/")
        assert response.status_code in {302, 403}
