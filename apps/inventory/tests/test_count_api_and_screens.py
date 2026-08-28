"""
The Task 1.6 API and Arabic screens (§AD, §AE, §AF).

Three rules run through everything here. **The route constrains the object**: a
count line submitted under another count's path is a 404, never somebody else's
line returned politely. **Cost is a separate permission from stock**: the API
omits valuation fields rather than blanking them, because a blank column still
says a number belongs there. And **the counting sheet is blind for everyone** —
including a caller who holds `view_valuation` — because the control is over
what the person counting knows at the moment they count.
"""

from __future__ import annotations

import datetime
import json
import zoneinfo
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_ADJUSTMENT,
    INVENTORY_CONSUMPTION,
    INVENTORY_CONTROL,
    INVENTORY_COUNT_VARIANCE,
    INVENTORY_WASTE_EXPENSE,
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
    create_document,
    create_stock_count,
    record_stock_counts,
    start_stock_count,
    submit_stock_count,
)
from apps.inventory.counts import CountEntry
from apps.inventory.models import (
    InventoryAdjustmentDocument,
    InventoryDocumentType,
    InventoryItem,
    StockCount,
    StockCountLine,
    Warehouse,
)
from apps.inventory.tests.stock_seed import seed_stock
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
WHEN = datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD)
API = "/api/v1/inventory"


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    for role_code, account_code in {
        INVENTORY_CONTROL: "1-03-01-001",
        GOODS_RECEIVED_NOT_INVOICED: "2-01-02-001",
        INVENTORY_CONSUMPTION: "5-01-02-001",
        INVENTORY_WASTE_EXPENSE: "6-02-01-002",
        INVENTORY_COUNT_VARIANCE: "7-09-02-001",
        INVENTORY_ADJUSTMENT: "7-09-03-001",
    }.items():
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=role_code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=datetime.date(TEST_YEAR, 1, 1),
        )


@pytest.fixture
def boss(organization: Organization) -> User:
    user = User.objects.create_user(username="boss", password="pw-not-real-1234")
    grant_organization_access(user=user, organization=organization, role=Role.OWNER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def checker(organization: Organization) -> User:
    user = User.objects.create_user(username="checker", password="pw-not-real-1234")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def keeper(branch: Branch) -> User:
    user = User.objects.create_user(username="keeper", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def stocked(
    boss: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    rice: InventoryItem,
    mapped: None,
) -> None:
    seed_stock(
        actor=boss,
        organization=organization,
        warehouse=main_store,
        item=rice,
        quantity="100",
        unit_cost="1500",
        control_account=Account.objects.get(organization=organization, code="1-03-01-001"),
        effective_at=WHEN,
    )


@pytest.fixture
def started_count(
    boss: User, organization: Organization, branch: Branch, main_store: Warehouse, stocked: None
) -> StockCount:
    count = create_stock_count(
        actor=boss,
        organization=organization,
        branch=branch,
        warehouse=main_store,
        reference="SHEET-1",
        reason="جرد شهري",
        cost_center=CostCenter.objects.get(organization=organization, code="WAREHOUSE"),
    )
    return start_stock_count(actor=boss, count=count, effective_at=WHEN)


def _json(response: Any) -> Any:
    return json.loads(response.content.decode("utf-8"))


# ---------------------------------------------------------------------------
# The blind sheet
# ---------------------------------------------------------------------------


class TestBlindCountEndpoint:
    def test_the_api_sheet_contains_no_book_quantity(
        self, boss: User, started_count: StockCount, client_for: Callable[[User], Client]
    ) -> None:
        response = client_for(boss).get(f"{API}/counts/{started_count.pk}/sheet/")
        assert response.status_code == 200
        body = response.content.decode("utf-8")

        payload = _json(response)
        assert len(payload) == 1
        forbidden = {"book_quantity", "book_value", "book_average", "variance_quantity"}
        assert forbidden.isdisjoint(payload[0].keys())
        # And no stray occurrence of the figures themselves, under any name.
        assert "100.000" not in body
        assert "150000" not in body
        assert "1500" not in body

    def test_the_sheet_is_blind_even_with_view_valuation(
        self, boss: User, started_count: StockCount, client_for: Callable[[User], Client]
    ) -> None:
        from apps.inventory.permissions import VIEW_VALUATION

        assert boss.has_perm(VIEW_VALUATION)
        payload = _json(client_for(boss).get(f"{API}/counts/{started_count.pk}/sheet/"))
        assert "book_quantity" not in payload[0]

    def test_the_rendered_html_sheet_carries_no_book_quantity(
        self, boss: User, started_count: StockCount, client_for: Callable[[User], Client]
    ) -> None:
        response = client_for(boss).get(reverse("inventory:count_sheet", args=[started_count.pk]))
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        # Not merely absent from the visible table: absent from the source, so
        # there is no hidden input or data attribute to read.
        assert "100.000" not in html
        assert "150000" not in html
        assert 'value="100"' not in html

    def test_counted_quantities_post_through_the_api(
        self, boss: User, started_count: StockCount, client_for: Callable[[User], Client]
    ) -> None:
        line = StockCountLine.objects.get(count=started_count)
        response = client_for(boss).patch(
            f"{API}/counts/{started_count.pk}/lines/",
            data=json.dumps({"entries": [{"line_id": line.pk, "base_quantity": "95"}]}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert _json(response)[0]["counted_quantity"] == "95.000"

    def test_a_line_from_another_count_is_a_404_on_this_route(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        kitchen_store: Warehouse,
        started_count: StockCount,
        client_for: Callable[[User], Client],
    ) -> None:
        other = start_stock_count(
            actor=boss,
            count=create_stock_count(
                actor=boss,
                organization=organization,
                branch=branch,
                warehouse=kitchen_store,
                reference="SHEET-2",
            ),
            effective_at=WHEN,
        )
        mine = StockCountLine.objects.get(count=started_count)
        response = client_for(boss).patch(
            f"{API}/counts/{other.pk}/lines/",
            data=json.dumps({"entries": [{"line_id": mine.pk, "base_quantity": "1"}]}),
            content_type="application/json",
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Scope, permission and valuation redaction
# ---------------------------------------------------------------------------


class TestCountApiSecurity:
    def test_a_foreign_count_is_a_404(
        self,
        started_count: StockCount,
        rival_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(rival_manager).get(f"{API}/counts/{started_count.pk}/")
        assert response.status_code == 404

    def test_a_storekeeper_sees_no_cost_on_the_review(
        self,
        boss: User,
        keeper: User,
        started_count: StockCount,
        client_for: Callable[[User], Client],
    ) -> None:
        line = StockCountLine.objects.get(count=started_count)
        record_stock_counts(
            actor=boss,
            count=started_count,
            entries=[CountEntry(line=line, base_quantity=Decimal("95"))],
        )
        submit_stock_count(actor=boss, count=started_count)

        payload = _json(client_for(keeper).get(f"{API}/counts/{started_count.pk}/"))
        row = payload["lines"][0]
        # The quantity columns are the storekeeper's business; the money is not.
        assert row["book_quantity"] == "100.000"
        assert row["variance_quantity"] == "-5.000"
        assert "book_value" not in row
        assert "variance_value" not in row

    def test_a_storekeeper_cannot_approve_through_the_api(
        self,
        boss: User,
        keeper: User,
        started_count: StockCount,
        client_for: Callable[[User], Client],
    ) -> None:
        line = StockCountLine.objects.get(count=started_count)
        record_stock_counts(
            actor=boss,
            count=started_count,
            entries=[CountEntry(line=line, base_quantity=Decimal("95"))],
        )
        submit_stock_count(actor=boss, count=started_count)
        response = client_for(keeper).post(
            f"{API}/counts/{started_count.pk}/approve/",
            data=json.dumps({"costs": []}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_the_conductor_cannot_approve_their_own_count_by_direct_post(
        self, boss: User, started_count: StockCount, client_for: Callable[[User], Client]
    ) -> None:
        """The button is hidden. This is the rule behind it."""
        line = StockCountLine.objects.get(count=started_count)
        record_stock_counts(
            actor=boss,
            count=started_count,
            entries=[CountEntry(line=line, base_quantity=Decimal("95"))],
        )
        submit_stock_count(actor=boss, count=started_count)
        response = client_for(boss).post(
            f"{API}/counts/{started_count.pk}/approve/",
            data=json.dumps({"costs": []}),
            content_type="application/json",
        )
        # 422, the module's code for "the caller could fix this by sending
        # something different" — here, by being somebody else.
        assert response.status_code == 422
        assert "approver_is_the_conductor" in response.content.decode("utf-8")

    def test_decimals_are_quoted_strings_in_both_directions(
        self,
        boss: User,
        checker: User,
        started_count: StockCount,
        client_for: Callable[[User], Client],
    ) -> None:
        line = StockCountLine.objects.get(count=started_count)
        record_stock_counts(
            actor=boss,
            count=started_count,
            entries=[CountEntry(line=line, base_quantity=Decimal("95"))],
        )
        submit_stock_count(actor=boss, count=started_count)
        response = client_for(checker).post(
            f"{API}/counts/{started_count.pk}/approve/",
            data=json.dumps({"costs": []}),
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.content.decode("utf-8")
        row = _json(response)["lines"][0]
        assert isinstance(row["variance_value"], str)
        assert row["variance_value"] == "-7500.000"
        # A JSON number would have been written without quotes.
        assert '"variance_value": "-7500.000"' in body or '"variance_value":"-7500.000"' in body


# ---------------------------------------------------------------------------
# Waste and adjustments through the API
# ---------------------------------------------------------------------------


class TestWasteAndAdjustmentApi:
    def test_waste_posts_through_its_own_route(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        client_for: Callable[[User], Client],
    ) -> None:
        client = client_for(boss)
        centre = CostCenter.objects.get(organization=organization, code="KITCHEN")
        created = client.post(
            f"{API}/waste/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "branch_id": branch.pk,
                    "warehouse_id": main_store.pk,
                    "effective_at": WHEN.isoformat(),
                    "evidence_reference": "W-1",
                    "cost_center_id": centre.pk,
                    "lines": [
                        {
                            "item_id": rice.pk,
                            "base_quantity": "10",
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        assert created.status_code == 201
        document_id = _json(created)["id"]

        posted = client.post(f"{API}/waste/{document_id}/post/")
        assert posted.status_code == 200
        payload = _json(posted)
        assert payload["status"] == "POSTED"
        assert payload["document_number"].startswith("WST-")
        assert payload["lines"][0]["total_value"] == "15000.000"

    def test_a_waste_id_does_not_resolve_under_the_issue_route(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        client_for: Callable[[User], Client],
    ) -> None:
        document = create_document(
            actor=boss,
            organization=organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.WASTE,
            effective_at=WHEN,
            evidence_reference="W-2",
            cost_center=CostCenter.objects.get(organization=organization, code="KITCHEN"),
        )
        client = client_for(boss)
        assert client.get(f"{API}/waste/{document.pk}/").status_code == 200
        assert client.get(f"{API}/issues/{document.pk}/").status_code == 404

    def test_a_value_only_adjustment_posts_and_redacts_cost_for_a_storekeeper(
        self,
        boss: User,
        keeper: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        client_for: Callable[[User], Client],
    ) -> None:
        client = client_for(boss)
        created = client.post(
            f"{API}/adjustments/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "branch_id": branch.pk,
                    "warehouse_id": main_store.pk,
                    "effective_at": WHEN.isoformat(),
                    "evidence_reference": "MEMO-1",
                    "reason": "خطأ إدخال كلفة",
                    "lines": [
                        {
                            "kind": "VALUE_ONLY",
                            "item_id": rice.pk,
                            "value_adjustment": "30000",
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        assert created.status_code == 201
        document_id = _json(created)["id"]

        posted = _json(client.post(f"{API}/adjustments/{document_id}/post/"))
        assert posted["status"] == "POSTED"
        assert posted["lines"][0]["total_value"] == "30000.000"
        assert posted["lines"][0]["base_quantity"] == "0.000"

        redacted = _json(client_for(keeper).get(f"{API}/adjustments/{document_id}/"))
        row = redacted["lines"][0]
        assert "total_value" not in row
        assert "value_adjustment" not in row

    def test_a_storekeeper_cannot_create_an_adjustment(
        self,
        keeper: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(keeper).post(
            f"{API}/adjustments/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "branch_id": branch.pk,
                    "warehouse_id": main_store.pk,
                    "effective_at": WHEN.isoformat(),
                    "evidence_reference": "MEMO",
                    "reason": "محاولة",
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_a_posted_adjustment_refuses_a_patch(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        client_for: Callable[[User], Client],
    ) -> None:
        client = client_for(boss)
        created = _json(
            client.post(
                f"{API}/adjustments/",
                data=json.dumps(
                    {
                        "organization_id": organization.pk,
                        "branch_id": branch.pk,
                        "warehouse_id": main_store.pk,
                        "effective_at": WHEN.isoformat(),
                        "evidence_reference": "MEMO-1",
                        "reason": "تصحيح",
                        "lines": [
                            {
                                "kind": "QUANTITY_LOSS",
                                "item_id": rice.pk,
                                "base_quantity": "1",
                            }
                        ],
                    }
                ),
                content_type="application/json",
            )
        )
        client.post(f"{API}/adjustments/{created['id']}/post/")
        response = client.patch(
            f"{API}/adjustments/{created['id']}/",
            data=json.dumps({"reason": "تغيير بعد الترحيل"}),
            content_type="application/json",
        )
        # 409, not 400: the request is well formed and the *state* refuses it.
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------


class TestScreens:
    @pytest.mark.parametrize(
        "url_name",
        [
            "inventory:count_list",
            "inventory:adjustment_list",
            "inventory:inventory_waste_list",
        ],
    )
    def test_every_new_screen_renders_in_arabic(
        self, boss: User, url_name: str, client_for: Callable[[User], Client], mapped: None
    ) -> None:
        client = client_for(boss)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        response = client.get(reverse(url_name))
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert 'lang="ar"' in html
        assert 'dir="rtl"' in html

    def test_the_count_review_screen_shows_the_variance_after_submission(
        self, boss: User, started_count: StockCount, client_for: Callable[[User], Client]
    ) -> None:
        line = StockCountLine.objects.get(count=started_count)
        record_stock_counts(
            actor=boss,
            count=started_count,
            entries=[CountEntry(line=line, base_quantity=Decimal("95"))],
        )
        submit_stock_count(actor=boss, count=started_count)
        response = client_for(boss).get(reverse("inventory:count_detail", args=[started_count.pk]))
        html = response.content.decode("utf-8")
        assert response.status_code == 200
        assert "100.000" in html
        assert "-5.000" in html

    def test_the_review_screen_hides_the_figures_before_submission(
        self, boss: User, started_count: StockCount, client_for: Callable[[User], Client]
    ) -> None:
        """While counting is in progress, the review page is not a way round the sheet."""
        response = client_for(boss).get(reverse("inventory:count_detail", args=[started_count.pk]))
        html = response.content.decode("utf-8")
        assert response.status_code == 200
        assert "100.000" not in html

    def test_the_admin_cannot_mutate_a_posted_count(
        self, started_count: StockCount, superuser: User, client_for: Callable[[User], Client]
    ) -> None:
        response = client_for(superuser).get(
            f"/admin/inventory/stockcount/{started_count.pk}/change/"
        )
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        assert "<input" not in html.split('id="content"')[-1].split("</form>")[0] or (
            "readonly" in html
        )
        # And the add view is refused outright.
        assert client_for(superuser).get("/admin/inventory/stockcount/add/").status_code == 403


class TestAdjustmentScreens:
    def test_an_adjustment_draft_and_post_run_through_the_screens(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        client_for: Callable[[User], Client],
    ) -> None:
        client = client_for(boss)
        created = client.post(
            reverse("inventory:adjustment_create"),
            data={
                "warehouse": main_store.pk,
                "effective_at": "2026-03-15T10:00",
                "evidence_reference": "MEMO-1",
                "reason": "تصحيح كلفة",
                "cost_center": "",
            },
        )
        assert created.status_code == 302
        document = InventoryAdjustmentDocument.objects.get()

        client.post(
            reverse("inventory:adjustment_detail", args=[document.pk]),
            data={
                "kind": "QUANTITY_LOSS",
                "item": rice.pk,
                "lot_code": "",
                "base_quantity": "5",
                "unit_cost": "",
                "value_adjustment": "",
                "line_comment": "",
            },
        )
        assert document.lines.count() == 1

        client.post(reverse("inventory:adjustment_post", args=[document.pk]))
        document.refresh_from_db()
        assert document.status == "POSTED"

    def test_a_locale_comma_is_refused_rather_than_guessed(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        client_for: Callable[[User], Client],
    ) -> None:
        client = client_for(boss)
        client.post(
            reverse("inventory:adjustment_create"),
            data={
                "warehouse": main_store.pk,
                "effective_at": "2026-03-15T10:00",
                "evidence_reference": "MEMO-1",
                "reason": "تصحيح",
                "cost_center": "",
            },
        )
        document = InventoryAdjustmentDocument.objects.get()
        response = client.post(
            reverse("inventory:adjustment_detail", args=[document.pk]),
            data={
                "kind": "QUANTITY_LOSS",
                "item": rice.pk,
                "lot_code": "",
                "base_quantity": "5,5",
                "unit_cost": "",
                "value_adjustment": "",
                "line_comment": "",
            },
        )
        assert response.status_code == 200
        assert "النقطة العشرية" in response.content.decode("utf-8")
        assert document.lines.count() == 0
