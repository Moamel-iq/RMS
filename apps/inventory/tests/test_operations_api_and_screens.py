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
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client

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
from apps.inventory.api import DocumentLineIn
from apps.inventory.commands import create_document
from apps.inventory.models import (
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryMovementDocument,
    Warehouse,
)
from apps.inventory.tests.stock_seed import seed_stock
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
WHEN_ISO = "2026-03-15T10:00:00+03:00"
WHEN = datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD)
JAN_1 = datetime.date(TEST_YEAR, 1, 1)

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


def _issue_payload(
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    item: InventoryItem,
    cost_center: Any,
) -> dict[str, Any]:
    """
    One issue, ready to post.

    No `unit_cost`: an issue is valued by the ledger at the standing average.
    The document that stated its own cost — the un-invoiced receipt — was
    withdrawn from the product, and with it the only line that ever carried one.
    """
    return {
        "organization_id": organization.pk,
        "branch_id": branch.pk,
        "warehouse_id": warehouse.pk,
        "cost_center_id": cost_center.pk,
        "effective_at": WHEN_ISO,
        "evidence_reference": "DN-77",
        "lines": [{"item_id": item.pk, "base_quantity": "12.500"}],
    }


class TestDocumentApi:
    """
    The contracts every operational document owes, demonstrated on the issue.

    They were written against the receipt because it was the first document
    this module had. Nothing in them was ever about a receipt.
    """

    def test_create_patch_post_and_reverse(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen: Any,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        client = client_for(manager)

        created = _post(
            client, ISSUES, _issue_payload(organization, branch, main_store, rice, kitchen)
        )
        assert created.status_code == 201, created.content
        document_id = created.json()["id"]
        assert created.json()["status"] == InventoryDocumentStatus.DRAFT
        assert created.json()["document_number"] == ""

        patched = client.patch(
            f"{ISSUES}{document_id}/",
            data=json.dumps({"evidence_reference": "DN-78"}),
            content_type="application/json",
        )
        assert patched.status_code == 200, patched.content
        assert patched.json()["evidence_reference"] == "DN-78"

        posted = _post(client, f"{ISSUES}{document_id}/post/")
        assert posted.status_code == 200, posted.content
        body = posted.json()
        assert body["status"] == InventoryDocumentStatus.POSTED
        assert body["document_number"].startswith(f"ISS-{TEST_YEAR}-")
        assert body["journal_entry_number"]

        # POSTED refuses PATCH and DELETE — the lifecycle is the contract.
        refused = client.patch(
            f"{ISSUES}{document_id}/",
            data=json.dumps({"narration": "quiet edit"}),
            content_type="application/json",
        )
        assert refused.status_code == 409, refused.content
        assert refused.json()["code"] == "not_a_draft"
        assert client.delete(f"{ISSUES}{document_id}/").status_code == 409

        reversed_response = _post(
            client, f"{ISSUES}{document_id}/reverse/", {"reason": "wrong note"}
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
        kitchen: Any,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """Raw bytes, not response.json(): the parser would hide the defect."""
        client = client_for(manager)
        created = _post(
            client, ISSUES, _issue_payload(organization, branch, main_store, rice, kitchen)
        )
        body = client.get(f"{ISSUES}{created.json()['id']}/").content.decode()
        assert '"base_quantity": "12.500"' in body or '"base_quantity":"12.500"' in body
        assert "12.5," not in body

    def test_a_viewer_sees_quantities_and_no_cost_keys(
        self,
        manager: User,
        viewer: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen: Any,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        created = _post(
            client_for(manager),
            ISSUES,
            _issue_payload(organization, branch, main_store, rice, kitchen),
        )
        response = client_for(viewer).get(f"{ISSUES}{created.json()['id']}/")
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
        kitchen: Any,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        response = _post(
            client_for(viewer),
            ISSUES,
            _issue_payload(organization, branch, main_store, rice, kitchen),
        )
        assert response.status_code == 403


@pytest.fixture
def stocked(
    manager: User,
    organization: Organization,
    main_store: Warehouse,
    rice: InventoryItem,
    mapped: None,
) -> None:
    """100 kg of rice at 1500, so an issue has something to take."""
    seed_stock(
        actor=manager,
        organization=organization,
        warehouse=main_store,
        item=rice,
        quantity="100",
        unit_cost="1500",
        control_account=Account.objects.get(organization=organization, code="1-03-01-001"),
        effective_at=WHEN,
    )


class TestIssueApi:
    def test_the_api_has_no_field_for_an_entered_cost(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        stocked: None,
    ) -> None:
        """
        A cost is decided by the ledger, and the contract no longer offers a
        place to state one.

        The service used to refuse `unit_cost` with `unit_cost_not_accepted`.
        The un-invoiced receipt was the only document that ever sent one, and
        when it was withdrawn the field left the schema with it — so a caller
        that sends one anyway is ignored rather than argued with, and the
        posted line still takes the standing average.
        """
        payload = _issue_payload(organization, branch, main_store, rice, kitchen)
        payload["lines"][0]["unit_cost"] = "999"

        created = _post(client_for(manager), ISSUES, payload)
        assert created.status_code == 201, created.content
        assert "unit_cost" not in DocumentLineIn.model_fields

        posted = _post(client_for(manager), f"{ISSUES}{created.json()['id']}/post/")
        assert posted.status_code == 200, posted.content
        assert posted.json()["lines"][0]["unit_cost"] == "1500.000000"


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
            document_type=InventoryDocumentType.ISSUE,
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
        assert rival.get(f"{ISSUES}{document.pk}/").status_code == 404
        assert _post(rival, f"{ISSUES}{document.pk}/post/").status_code == 404
        assert rival.delete(f"{ISSUES}{document.pk}/").status_code == 404
        assert rival.get(ISSUES).json() == []

    def test_a_foreign_warehouse_cannot_be_named(
        self,
        rival_manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        accounting: None,
    ) -> None:
        response = _post(
            client_for(rival_manager),
            ISSUES,
            _issue_payload(organization, branch, main_store, rice, kitchen),
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
        kitchen: CostCenter,
        accounting: None,
    ) -> None:
        from apps.inventory.services import create_warehouse

        elsewhere = create_warehouse(branch=second_branch, code="KAR", name="مخزن الكرادة")
        grant_branch_access(user=manager, branch=second_branch, role=Role.MANAGER)
        refreshed = User.objects.get(pk=manager.pk)
        response = _post(
            client_for(refreshed),
            ISSUES,
            _issue_payload(organization, branch, elsewhere, rice, kitchen),
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
