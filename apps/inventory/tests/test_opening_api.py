"""
The opening-stock API and its security boundary (Task 1.3 §S, §V 57–62).

The endpoints are command-oriented: they authenticate, resolve scope, parse
exact Decimal strings, and drive the document lifecycle. They write no ledger
row of their own — and there is still no writable movement endpoint anywhere.
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

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_OPENING_EQUITY,
    Account,
    AccountRole,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.commands import add_opening_line
from apps.inventory.models import (
    InventoryItem,
    OpeningStockDocument,
    OpeningStockStatus,
    Warehouse,
)
from apps.inventory.opening import OpeningLineInput
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
CUTOFF = "2026-03-15T10:00:00+03:00"
OPENINGS_URL = "/api/v1/inventory/openings/"


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_CONTROL),
        account=Account.objects.get(organization=organization, code="1-03-01-001"),
        effective_from=datetime.date(TEST_YEAR, 1, 1),
    )
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_OPENING_EQUITY),
        account=Account.objects.get(organization=organization, code="3-02-01-001"),
        effective_from=datetime.date(TEST_YEAR, 1, 1),
    )


@pytest.fixture
def viewer(branch: Branch) -> User:
    user = User.objects.create_user(username="viewer", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.VIEWER)
    return User.objects.get(pk=user.pk)


def _payload(
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    item: InventoryItem,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "organization_id": organization.pk,
        "branch_id": branch.pk,
        "cutoff_at": CUTOFF,
        "evidence_reference": "COUNT-SHEET-9",
        "narration": "جرد افتتاحي",
        "lines": [
            {
                "warehouse_id": warehouse.pk,
                "item_id": item.pk,
                "base_quantity": "100.000",
                "unit_cost": "1500",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _post(client: Client, url: str, payload: dict[str, Any] | None = None) -> Any:
    return client.post(url, data=json.dumps(payload or {}), content_type="application/json")


class TestLifecycleOverHttp:
    def test_create_read_patch_submit_post_and_reverse(
        self,
        manager: User,
        accounting_manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        preparer = client_for(manager)
        approver = client_for(accounting_manager)

        created = _post(preparer, OPENINGS_URL, _payload(organization, branch, main_store, rice))
        assert created.status_code == 201, created.content
        document_id = created.json()["id"]
        assert created.json()["status"] == OpeningStockStatus.DRAFT
        assert created.json()["lines"][0]["base_quantity"] == "100.000"

        patched = preparer.patch(
            f"{OPENINGS_URL}{document_id}/",
            data=json.dumps({"evidence_reference": "COUNT-SHEET-10"}),
            content_type="application/json",
        )
        assert patched.status_code == 200, patched.content
        assert patched.json()["evidence_reference"] == "COUNT-SHEET-10"

        submitted = _post(preparer, f"{OPENINGS_URL}{document_id}/submit/")
        assert submitted.status_code == 200, submitted.content
        assert submitted.json()["status"] == OpeningStockStatus.SUBMITTED

        # SUBMITTED refuses PATCH and DELETE — the lifecycle is the contract.
        refused = preparer.patch(
            f"{OPENINGS_URL}{document_id}/",
            data=json.dumps({"narration": "quiet edit"}),
            content_type="application/json",
        )
        assert refused.status_code == 409, refused.content
        assert refused.json()["code"] == "not_a_draft"
        assert preparer.delete(f"{OPENINGS_URL}{document_id}/").status_code == 409

        returned = _post(
            preparer,
            f"{OPENINGS_URL}{document_id}/return-to-draft/",
            {"reason": "one more warehouse"},
        )
        assert returned.status_code == 200
        assert returned.json()["status"] == OpeningStockStatus.DRAFT

        _post(preparer, f"{OPENINGS_URL}{document_id}/submit/")
        posted = _post(approver, f"{OPENINGS_URL}{document_id}/post/")
        assert posted.status_code == 200, posted.content
        body = posted.json()
        assert body["status"] == OpeningStockStatus.POSTED
        assert body["document_number"].startswith("OPN-")
        assert body["journal_entry_number"]
        assert body["lines"][0]["inventory_account_code"] == "1-03-01-001"

        reversed_response = _post(
            approver, f"{OPENINGS_URL}{document_id}/reverse/", {"reason": "restated"}
        )
        assert reversed_response.status_code == 200, reversed_response.content
        assert reversed_response.json()["status"] == OpeningStockStatus.REVERSED

    def test_the_submitter_is_refused_posting_over_http(
        self,
        accounting_manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        client = client_for(accounting_manager)
        created = _post(client, OPENINGS_URL, _payload(organization, branch, main_store, rice))
        document_id = created.json()["id"]
        _post(client, f"{OPENINGS_URL}{document_id}/submit/")
        refused = _post(client, f"{OPENINGS_URL}{document_id}/post/")
        assert refused.status_code == 422, refused.content
        assert refused.json()["code"] == "submitter_cannot_post"


class TestDecimalsAndCostVisibility:
    @pytest.fixture
    def draft_document(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> OpeningStockDocument:
        from apps.inventory.commands import create_opening

        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD),
            evidence_reference="SHEET",
        )
        add_opening_line(
            actor=manager,
            document=document,
            line=OpeningLineInput(
                warehouse=main_store,
                item=rice,
                base_quantity=Decimal("12.500"),
                unit_cost=Decimal("1234.567"),
            ),
        )
        return document

    def test_no_float_appears_in_the_raw_json(
        self, manager: User, client_for: Any, draft_document: OpeningStockDocument
    ) -> None:
        """Raw bytes, not response.json(): the parser would hide the defect."""
        body = client_for(manager).get(f"{OPENINGS_URL}{draft_document.pk}/").content.decode()
        assert '"base_quantity": "12.500"' in body or '"base_quantity":"12.500"' in body
        assert '"unit_cost": "1234.567000"' in body or '"unit_cost":"1234.567000"' in body
        assert "12.5," not in body

    def test_a_viewer_sees_quantities_and_no_cost_keys_at_all(
        self, viewer: User, client_for: Any, draft_document: OpeningStockDocument
    ) -> None:
        response = client_for(viewer).get(f"{OPENINGS_URL}{draft_document.pk}/")
        assert response.status_code == 200
        body = response.json()
        line = body["lines"][0]
        assert line["base_quantity"] == "12.500"
        assert line.get("unit_cost") is None
        assert line.get("total_value") is None
        assert body.get("total_value") is None

    def test_a_viewer_cannot_create_an_opening(
        self,
        viewer: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        response = _post(
            client_for(viewer), OPENINGS_URL, _payload(organization, branch, main_store, rice)
        )
        assert response.status_code == 403


class TestTenancyBoundary:
    def test_a_foreign_organizations_opening_is_a_404_not_a_403(
        self,
        rival_manager: User,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        from apps.inventory.commands import create_opening

        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD),
            evidence_reference="SHEET",
        )
        rival = client_for(rival_manager)
        assert rival.get(f"{OPENINGS_URL}{document.pk}/").status_code == 404
        assert _post(rival, f"{OPENINGS_URL}{document.pk}/submit/").status_code == 404
        assert rival.delete(f"{OPENINGS_URL}{document.pk}/").status_code == 404
        # The list shows nothing rather than an error.
        assert rival.get(OPENINGS_URL).json() == []

    def test_a_foreign_branch_cannot_be_named_at_creation(
        self,
        rival_manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        """The rival manager submits OUR branch id: 404, it does not exist
        as far as they are concerned."""
        response = _post(
            client_for(rival_manager),
            OPENINGS_URL,
            _payload(organization, branch, main_store, rice),
        )
        assert response.status_code == 404

    def test_a_foreign_warehouse_in_a_line_is_a_404(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        other_warehouse: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        payload = _payload(organization, branch, other_warehouse, rice)
        response = _post(client_for(manager), OPENINGS_URL, payload)
        assert response.status_code == 404

    def test_a_cross_branch_warehouse_is_refused_in_a_line(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        second_branch: Branch,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        """A same-organization, different-branch warehouse resolves but does
        not belong to the document's branch: a domain refusal, not a leak."""
        from apps.inventory.services import create_warehouse
        from apps.organizations.services import grant_branch_access

        elsewhere = create_warehouse(branch=second_branch, code="KAR", name_ar="مخزن الكرادة")
        grant_branch_access(user=manager, branch=second_branch, role=Role.MANAGER)
        manager = User.objects.get(pk=manager.pk)
        response = _post(
            client_for(manager),
            OPENINGS_URL,
            _payload(organization, branch, elsewhere, rice),
        )
        assert response.status_code == 422, response.content
        assert response.json()["code"] == "warehouse_branch_mismatch"


class TestReconciliationEndpoint:
    def test_the_report_is_scoped_and_permission_gated(
        self,
        manager: User,
        storekeeper: User,
        client_for: Any,
        organization: Organization,
        accounting: None,
    ) -> None:
        url = f"/api/v1/inventory/reconciliation/?organization_id={organization.pk}"
        clean = client_for(manager).get(url)
        assert clean.status_code == 200, clean.content
        assert clean.json() == {
            "organization_code": organization.code,
            "is_clean": True,
            "mismatches": [],
        }
        # A storekeeper holds view_stock and not view_valuation: 403.
        assert client_for(storekeeper).get(url).status_code == 403

    def test_the_report_is_get_only(self, manager: User, client_for: Any) -> None:
        response = _post(client_for(manager), "/api/v1/inventory/reconciliation/")
        assert response.status_code in (404, 405)


class TestAdminStaysReadOnly:
    def test_no_admin_can_mutate_opening_movement_or_journal_state(self, superuser: User) -> None:
        """§V 62 — checked against the registered admin classes themselves,
        for every model the posting touches."""
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        from apps.accounting.models import JournalEntry, JournalLine
        from apps.inventory.models import (
            InventoryAccountMapping,
            StockBalance,
            StockLedgerEntry,
            StockMovement,
        )

        request = RequestFactory().get("/admin/")
        request.user = superuser
        for model in (
            OpeningStockDocument,
            InventoryAccountMapping,
            StockMovement,
            StockLedgerEntry,
            StockBalance,
            JournalEntry,
            JournalLine,
        ):
            registered = django_admin.site._registry[model]  # noqa: SLF001 - the registry is the fact
            assert registered.has_add_permission(request) is False, model
            assert registered.has_change_permission(request) is False, model
            assert registered.has_delete_permission(request) is False, model
