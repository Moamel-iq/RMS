"""
The operational API and native screens (Task 1.4 §S 50–60).

The endpoints are command-oriented: authenticate, resolve scope, parse exact
Decimal strings, call the application service, serialize. They write no ledger
row of their own, and there is still no writable movement endpoint anywhere.
"""

from __future__ import annotations

import datetime
import json
import zoneinfo
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONSUMPTION,
    INVENTORY_CONTROL,
    Account,
    AccountRole,
    CostCenter,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.commands import add_document_line, create_document, post_document
from apps.inventory.models import (
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryMovementDocument,
    Warehouse,
)
from apps.inventory.operations import DocumentLineInput
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
WHEN_ISO = "2026-03-15T10:00:00+03:00"
WHEN = datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD)
JAN_1 = datetime.date(TEST_YEAR, 1, 1)

RECEIPTS = "/api/v1/inventory/receipts/"
ISSUES = "/api/v1/inventory/issues/"
RETURNS = "/api/v1/inventory/returns-in/"


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    for code, account_code in (
        (INVENTORY_CONTROL, "1-03-01-001"),
        (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
        (INVENTORY_CONSUMPTION, "5-01-02-001"),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=JAN_1,
        )


@pytest.fixture
def kitchen(organization: Organization, accounting: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="KITCHEN")


@pytest.fixture
def viewer(branch: Branch) -> User:
    user = User.objects.create_user(username="viewer", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.VIEWER)
    return User.objects.get(pk=user.pk)


def _post(client: Client, url: str, payload: dict[str, Any] | None = None) -> Any:
    return client.post(url, data=json.dumps(payload or {}), content_type="application/json")


def _receipt_payload(
    organization: Organization, branch: Branch, warehouse: Warehouse, item: InventoryItem
) -> dict[str, Any]:
    return {
        "organization_id": organization.pk,
        "branch_id": branch.pk,
        "warehouse_id": warehouse.pk,
        "effective_at": WHEN_ISO,
        "evidence_reference": "DN-77",
        "lines": [{"item_id": item.pk, "base_quantity": "12.500", "unit_cost": "1234.567"}],
    }


class TestReceiptApi:
    def test_create_patch_post_and_reverse(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        client = client_for(manager)

        created = _post(client, RECEIPTS, _receipt_payload(organization, branch, main_store, rice))
        assert created.status_code == 201, created.content
        document_id = created.json()["id"]
        assert created.json()["status"] == InventoryDocumentStatus.DRAFT
        assert created.json()["document_number"] == ""

        patched = client.patch(
            f"{RECEIPTS}{document_id}/",
            data=json.dumps({"evidence_reference": "DN-78"}),
            content_type="application/json",
        )
        assert patched.status_code == 200, patched.content
        assert patched.json()["evidence_reference"] == "DN-78"

        posted = _post(client, f"{RECEIPTS}{document_id}/post/")
        assert posted.status_code == 200, posted.content
        body = posted.json()
        assert body["status"] == InventoryDocumentStatus.POSTED
        assert body["document_number"].startswith(f"RCV-{TEST_YEAR}-")
        assert body["journal_entry_number"]
        assert body["lines"][0]["inventory_account_code"] == "1-03-01-001"
        assert body["lines"][0]["contra_account_code"] == "2-01-02-001"

        # POSTED refuses PATCH and DELETE — the lifecycle is the contract.
        refused = client.patch(
            f"{RECEIPTS}{document_id}/",
            data=json.dumps({"narration": "quiet edit"}),
            content_type="application/json",
        )
        assert refused.status_code == 409, refused.content
        assert refused.json()["code"] == "not_a_draft"
        assert client.delete(f"{RECEIPTS}{document_id}/").status_code == 409

        reversed_response = _post(
            client, f"{RECEIPTS}{document_id}/reverse/", {"reason": "wrong note"}
        )
        assert reversed_response.status_code == 200, reversed_response.content
        assert reversed_response.json()["status"] == InventoryDocumentStatus.REVERSED

    def test_no_float_appears_in_the_raw_json(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """Raw bytes, not response.json(): the parser would hide the defect."""
        client = client_for(manager)
        created = _post(client, RECEIPTS, _receipt_payload(organization, branch, main_store, rice))
        body = client.get(f"{RECEIPTS}{created.json()['id']}/").content.decode()
        assert '"base_quantity": "12.500"' in body or '"base_quantity":"12.500"' in body
        assert '"unit_cost": "1234.567000"' in body or '"unit_cost":"1234.567000"' in body
        assert "12.5," not in body

    def test_a_viewer_sees_quantities_and_no_cost_keys(
        self,
        manager: User,
        viewer: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        created = _post(
            client_for(manager),
            RECEIPTS,
            _receipt_payload(organization, branch, main_store, rice),
        )
        response = client_for(viewer).get(f"{RECEIPTS}{created.json()['id']}/")
        assert response.status_code == 200
        body = response.json()
        line = body["lines"][0]
        assert line["base_quantity"] == "12.500"
        assert line.get("unit_cost") is None
        assert line.get("total_value") is None
        assert body.get("total_value") is None

    def test_a_viewer_cannot_create_or_post(
        self,
        viewer: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        response = _post(
            client_for(viewer),
            RECEIPTS,
            _receipt_payload(organization, branch, main_store, rice),
        )
        assert response.status_code == 403


class TestIssueAndReturnApi:
    @pytest.fixture
    def posted_receipt(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> InventoryMovementDocument:
        document = create_document(
            actor=manager,
            organization=organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.RECEIPT,
            effective_at=WHEN,
            evidence_reference="DN",
        )
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("100"), unit_cost=Decimal("1500")
            ),
        )
        return post_document(actor=manager, document=document)

    def test_an_issue_then_a_return_through_the_api(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        posted_receipt: InventoryMovementDocument,
    ) -> None:
        client = client_for(manager)

        issue = _post(
            client,
            ISSUES,
            {
                "organization_id": organization.pk,
                "branch_id": branch.pk,
                "warehouse_id": main_store.pk,
                "effective_at": WHEN_ISO,
                "evidence_reference": "REQ-1",
                "cost_center_id": kitchen.pk,
                "lines": [{"item_id": rice.pk, "base_quantity": "40"}],
            },
        )
        assert issue.status_code == 201, issue.content
        issue_id = issue.json()["id"]
        posted_issue = _post(client, f"{ISSUES}{issue_id}/post/")
        assert posted_issue.status_code == 200, posted_issue.content
        issue_line_id = posted_issue.json()["lines"][0]["id"]
        # Valued by the ledger, not by the caller.
        assert posted_issue.json()["lines"][0]["unit_cost"] == "1500.000000"

        returned = _post(
            client,
            RETURNS,
            {
                "organization_id": organization.pk,
                "branch_id": branch.pk,
                "warehouse_id": main_store.pk,
                "effective_at": WHEN_ISO,
                "evidence_reference": "RET-1",
                "lines": [
                    {
                        "item_id": rice.pk,
                        "base_quantity": "10",
                        "source_issue_line_id": issue_line_id,
                    }
                ],
            },
        )
        assert returned.status_code == 201, returned.content
        posted_return = _post(client, f"{RETURNS}{returned.json()['id']}/post/")
        assert posted_return.status_code == 200, posted_return.content
        assert posted_return.json()["document_number"].startswith("RTN-")
        assert posted_return.json()["lines"][0]["total_value"] == "15000.000"

    def test_an_entered_cost_on_an_issue_is_refused(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        posted_receipt: InventoryMovementDocument,
    ) -> None:
        response = _post(
            client_for(manager),
            ISSUES,
            {
                "organization_id": organization.pk,
                "branch_id": branch.pk,
                "warehouse_id": main_store.pk,
                "effective_at": WHEN_ISO,
                "evidence_reference": "REQ-2",
                "cost_center_id": kitchen.pk,
                "lines": [{"item_id": rice.pk, "base_quantity": "1", "unit_cost": "999"}],
            },
        )
        assert response.status_code == 422, response.content
        assert response.json()["code"] == "unit_cost_not_accepted"

    def test_a_document_id_cannot_cross_between_series(
        self,
        manager: User,
        client_for: Any,
        posted_receipt: InventoryMovementDocument,
    ) -> None:
        """
        The type comes from the route, so a receipt's id is not a valid issue
        id — it resolves to nothing rather than to the wrong document.
        """
        client = client_for(manager)
        assert client.get(f"{RECEIPTS}{posted_receipt.pk}/").status_code == 200
        assert client.get(f"{ISSUES}{posted_receipt.pk}/").status_code == 404
        assert _post(client, f"{ISSUES}{posted_receipt.pk}/post/").status_code == 404


class TestTenancyBoundary:
    @pytest.fixture
    def document(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        accounting: None,
    ) -> InventoryMovementDocument:
        return create_document(
            actor=manager,
            organization=organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.RECEIPT,
            effective_at=WHEN,
            evidence_reference="DN",
        )

    def test_a_foreign_organizations_document_is_a_404(
        self,
        rival_manager: User,
        client_for: Any,
        document: InventoryMovementDocument,
    ) -> None:
        rival = client_for(rival_manager)
        assert rival.get(f"{RECEIPTS}{document.pk}/").status_code == 404
        assert _post(rival, f"{RECEIPTS}{document.pk}/post/").status_code == 404
        assert rival.delete(f"{RECEIPTS}{document.pk}/").status_code == 404
        assert rival.get(RECEIPTS).json() == []

    def test_a_foreign_warehouse_cannot_be_named(
        self,
        rival_manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        response = _post(
            client_for(rival_manager),
            RECEIPTS,
            _receipt_payload(organization, branch, main_store, rice),
        )
        assert response.status_code == 404

    def test_a_cross_branch_warehouse_is_refused(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        second_branch: Branch,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        from apps.inventory.services import create_warehouse

        elsewhere = create_warehouse(branch=second_branch, code="KAR", name_ar="مخزن الكرادة")
        grant_branch_access(user=manager, branch=second_branch, role=Role.MANAGER)
        refreshed = User.objects.get(pk=manager.pk)
        response = _post(
            client_for(refreshed),
            RECEIPTS,
            _receipt_payload(organization, branch, elsewhere, rice),
        )
        assert response.status_code == 422, response.content
        assert response.json()["code"] == "warehouse_branch_mismatch"


class TestThereIsStillNoMovementWritePath:
    def test_the_api_exposes_no_movement_write_endpoint(self) -> None:
        """Checked against the routing table, not against a guess."""
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


class TestScreens:
    def test_the_full_lifecycle_through_the_screens(
        self,
        manager: User,
        client_for: Any,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        client = client_for(manager)

        created = client.post(
            reverse("inventory:inventory_receipt_create"),
            {
                "warehouse": main_store.pk,
                "effective_at": "2026-03-15T10:00",
                "evidence_reference": "DN-UI",
                "narration": "",
            },
        )
        assert created.status_code == 302, getattr(created, "content", b"")
        document = InventoryMovementDocument.objects.get()
        detail = reverse("inventory:inventory_receipt_detail", args=[document.pk])
        assert created["Location"] == detail

        added = client.post(
            detail,
            {
                "item": rice.pk,
                "lot_code": "",
                "package_conversion": "",
                "entered_package_quantity": "",
                "measured_base_quantity": "",
                "base_quantity": "20.000",
                "unit_cost": "1000",
            },
        )
        assert added.status_code == 302, added.content
        assert document.lines.count() == 1

        posted = client.post(reverse("inventory:inventory_receipt_post", args=[document.pk]))
        assert posted.status_code == 302
        document.refresh_from_db()
        assert document.status == InventoryDocumentStatus.POSTED

        page = client.get(detail).content.decode()
        assert document.document_number in page
        assert document.journal_entry is not None
        assert document.journal_entry.entry_number in page

        reversed_response = client.post(
            reverse("inventory:inventory_receipt_reverse", args=[document.pk]),
            {"reason": "wrong note"},
        )
        assert reversed_response.status_code == 302
        document.refresh_from_db()
        assert document.status == InventoryDocumentStatus.REVERSED

    def test_the_three_lists_render_with_their_own_arabic_headings(
        self, manager: User, client_for: Any, accounting: None
    ) -> None:
        """
        Each screen names its own document type and not the other two. The
        vocabulary is the point: a receipt screen that called itself a
        purchase, or an issue screen that called itself a transfer, would
        teach the operator the wrong thing about what they just did.
        """
        client = client_for(manager)
        headings = {
            "inventory:inventory_receipt_list": "استلام مخزني غير مفوتر",
            "inventory:inventory_issue_list": "صرف مخزني للاستهلاك",
            "inventory:inventory_return_in_list": "إرجاع من صرف سابق",
        }
        for name, heading in headings.items():
            response = client.get(reverse(name))
            assert response.status_code == 200
            html = response.content.decode()
            assert heading in html
            for other, other_heading in headings.items():
                if other != name:
                    # The rail links to all three, so look at the page title
                    # rather than the whole document.
                    assert f'<h1 class="pagehead__title">{other_heading}' not in html

    def test_a_storekeeper_sees_no_recorded_cost(
        self,
        manager: User,
        client_for: Any,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        A storekeeper never learns what stock is worth — the value columns are
        omitted, not blanked.

        They can still *enter* a receipt cost, and that is not a contradiction:
        the figure is on the delivery note in their hand, and the role map
        gives them `post_receipt` precisely so they can record what arrived.
        What `view_valuation` withholds is the ledger's answer — what the
        organization already paid, and what the shelf is now worth.
        """
        keeper = User.objects.create_user(username="keeper2", password="pw-not-real-1234")
        grant_branch_access(user=keeper, branch=branch, role=Role.STOREKEEPER)
        keeper = User.objects.get(pk=keeper.pk)

        document = create_document(
            actor=manager,
            organization=branch.organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.RECEIPT,
            effective_at=WHEN,
            evidence_reference="DN",
        )
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("5"), unit_cost=Decimal("1234")
            ),
        )

        page = client_for(keeper).get(
            reverse("inventory:inventory_receipt_detail", args=[document.pk])
        )
        assert page.status_code == 200
        html = page.content.decode()
        # The recorded figure is absent from the table…
        assert "1234.000000" not in html
        assert "6170.000" not in html
        assert '<th scope="col">القيمة</th>' not in html
        # …and so is the account it would post to.
        assert '<th scope="col">حساب المخزون</th>' not in html

        # The manager, who holds view_valuation, sees all of it.
        manager_html = (
            client_for(manager)
            .get(reverse("inventory:inventory_receipt_detail", args=[document.pk]))
            .content.decode()
        )
        assert "6170.000" in manager_html
        assert '<th scope="col">القيمة</th>' in manager_html

    def test_a_hidden_post_button_is_still_refused_on_a_direct_post(
        self,
        manager: User,
        client_for: Any,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """A PURCHASING post holds no posting authority; the POST is a 403."""
        buyer = User.objects.create_user(username="buyer", password="pw-not-real-1234")
        grant_branch_access(user=buyer, branch=branch, role=Role.PURCHASING)
        buyer = User.objects.get(pk=buyer.pk)

        document = create_document(
            actor=manager,
            organization=branch.organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.RECEIPT,
            effective_at=WHEN,
            evidence_reference="DN",
        )
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("5"), unit_cost=Decimal("1000")
            ),
        )
        response = client_for(buyer).post(
            reverse("inventory:inventory_receipt_post", args=[document.pk])
        )
        assert response.status_code == 403

    def test_navigation_points_only_at_live_screens(
        self, manager: User, client_for: Any, accounting: None
    ) -> None:
        """
        Every navigation entry marked available must actually resolve. A rail
        that offers a 404 is worse than one that shows the section as unbuilt.
        """
        from django.urls import NoReverseMatch
        from django.urls import reverse as django_reverse

        from apps.core.navigation import MODULES

        for module in MODULES:
            for section in module.sections:
                if not section.available or not section.url_name:
                    continue
                try:
                    django_reverse(section.url_name)
                except NoReverseMatch:  # pragma: no cover - the assertion is the point
                    pytest.fail(f"{section.url_name} is marked available but does not resolve")


class TestAdminStaysReadOnly:
    def test_no_admin_can_mutate_an_operational_document(self, superuser: User) -> None:
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        from apps.inventory.models import InventoryMovementDocument

        request = RequestFactory().get("/admin/")
        request.user = superuser
        registered = django_admin.site._registry[  # noqa: SLF001 - the registry is the fact
            InventoryMovementDocument
        ]
        assert registered.has_add_permission(request) is False
        assert registered.has_change_permission(request) is False
        assert registered.has_delete_permission(request) is False
