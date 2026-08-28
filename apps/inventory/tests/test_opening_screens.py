"""
The Task 1.3 native screens: openings, mapping management, reconciliation.

Screens are presentation over the command layer, so these tests care about
three things: the page renders inside the shell for the people who work
there, the buttons follow the same authorization the commands enforce, and a
hand-made POST to a hidden action is refused on its merits.
"""

from __future__ import annotations

import datetime
import zoneinfo
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_OPENING_EQUITY,
    Account,
    AccountRole,
    OrganizationAccountMapping,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.commands import add_opening_line, create_opening, submit_opening
from apps.inventory.forms import OpeningLineForm
from apps.inventory.models import (
    InventoryItem,
    OpeningStockDocument,
    OpeningStockStatus,
    PackageUnit,
    Warehouse,
)
from apps.inventory.opening import OpeningLineInput
from apps.inventory.services import create_item_conversion
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
CUTOFF = datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD)


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


@pytest.fixture
def submitted(
    manager: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    rice: InventoryItem,
    accounting: None,
) -> OpeningStockDocument:
    document = create_opening(
        actor=manager,
        organization=organization,
        branch=branch,
        cutoff_at=CUTOFF,
        evidence_reference="SHEET-1",
    )
    add_opening_line(
        actor=manager,
        document=document,
        line=OpeningLineInput(
            warehouse=main_store,
            item=rice,
            base_quantity=Decimal("100"),
            unit_cost=Decimal("1500"),
        ),
    )
    return submit_opening(actor=manager, document=document)


class TestOpeningScreens:
    def test_opening_line_form_keeps_the_common_entry_short_and_clear(
        self, manager: User, branch: Branch, main_store: Warehouse, rice: InventoryItem
    ) -> None:
        form = OpeningLineForm(
            data={
                "warehouse": main_store.pk,
                "item": rice.pk,
                "base_quantity": "",
                "unit_cost": "1500",
            },
            actor=manager,
            branch=branch,
        )

        assert form.is_valid() is False
        assert form.errors["base_quantity"] == ["أدخل الكمية."]

    def test_an_opening_line_is_added_with_htmx_without_a_page_redirect(
        self,
        manager: User,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=CUTOFF,
            evidence_reference="HTMX-OPENING",
        )

        response = client_for(manager).post(
            reverse("inventory:opening_detail", args=[document.pk]),
            {
                "warehouse": main_store.pk,
                "item": rice.pk,
                "lot_code": "",
                "lot_expiry": "",
                "package_conversion": "",
                "entered_package_quantity": "",
                "measured_base_quantity": "",
                "base_quantity": "100.000",
                "unit_cost": "1500",
            },
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert document.lines.count() == 1
        body = response.content.decode()
        assert 'id="opening-lines-workspace"' in body
        assert "أُضيف السطر." in body
        assert "hx-post" in body

    def test_the_full_lifecycle_through_the_screens(
        self,
        manager: User,
        accounting_manager: User,
        client_for: Any,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        preparer = client_for(manager)

        created = preparer.post(
            reverse("inventory:opening_create"),
            {
                "branch": branch.pk,
                "cutoff_at": "2026-03-15T10:00",
                "evidence_reference": "SHEET-UI",
                "narration": "",
            },
        )
        assert created.status_code == 302, getattr(created, "content", b"")
        document = OpeningStockDocument.objects.get()
        detail_url = reverse("inventory:opening_detail", args=[document.pk])
        assert created["Location"] == detail_url

        added = preparer.post(
            detail_url,
            {
                "warehouse": main_store.pk,
                "item": rice.pk,
                "lot_code": "",
                "package_conversion": "",
                "entered_package_quantity": "",
                "measured_base_quantity": "",
                "base_quantity": "100.000",
                "unit_cost": "1500",
            },
        )
        assert added.status_code == 302, added.content
        assert document.lines.count() == 1

        submitted = preparer.post(reverse("inventory:opening_submit", args=[document.pk]))
        assert submitted.status_code == 302
        document.refresh_from_db()
        assert document.status == OpeningStockStatus.SUBMITTED

        approver = client_for(accounting_manager)
        posted = approver.post(reverse("inventory:opening_post", args=[document.pk]))
        assert posted.status_code == 302
        document.refresh_from_db()
        assert document.status == OpeningStockStatus.POSTED
        assert document.document_number.startswith("OPN-")

        page = approver.get(detail_url).content.decode()
        assert document.document_number in page
        assert document.journal_entry is not None
        assert document.journal_entry.entry_number in page

        reversed_response = approver.post(
            reverse("inventory:opening_reverse", args=[document.pk]),
            {"reason": "restated"},
        )
        assert reversed_response.status_code == 302
        document.refresh_from_db()
        assert document.status == OpeningStockStatus.REVERSED

    def test_a_hidden_post_button_is_still_refused_on_a_direct_post(
        self, manager: User, client_for: Any, submitted: OpeningStockDocument
    ) -> None:
        """The manager prepared the screen shows no posting button — and a
        hand-made POST is refused with 403, not honoured."""
        response = client_for(manager).post(reverse("inventory:opening_post", args=[submitted.pk]))
        assert response.status_code == 403

    def test_a_viewer_sees_the_document_without_cost_columns(
        self, viewer: User, client_for: Any, submitted: OpeningStockDocument
    ) -> None:
        page = client_for(viewer).get(reverse("inventory:opening_detail", args=[submitted.pk]))
        assert page.status_code == 200
        html = page.content.decode()
        assert "كلفة الوحدة" not in html
        assert "1500" not in html

    def test_a_rival_manager_gets_a_404_not_a_403(
        self, rival_manager: User, client_for: Any, submitted: OpeningStockDocument
    ) -> None:
        response = client_for(rival_manager).get(
            reverse("inventory:opening_detail", args=[submitted.pk])
        )
        assert response.status_code == 404


class TestMappingScreens:
    def test_inventory_override_create_screen_renders(
        self, accounting_manager: User, client_for: Any, accounting: None
    ) -> None:
        response = client_for(accounting_manager).get(reverse("inventory:mapping_create"))

        assert response.status_code == 200
        assert "تخصيص حساب لصنف أو مجموعة" in response.content.decode()

    def test_the_role_list_is_read_only_vocabulary(
        self, accounting_manager: User, client_for: Any
    ) -> None:
        page = client_for(accounting_manager).get(reverse("accounting:role_list"))
        assert page.status_code == 200
        html = page.content.decode()
        assert "INVENTORY_CONTROL" in html
        assert "INVENTORY_OPENING_EQUITY" in html

    def test_a_mapping_is_created_through_the_screen(
        self,
        accounting_manager: User,
        client_for: Any,
        organization: Organization,
        accounting: None,
    ) -> None:
        client = client_for(accounting_manager)
        role = AccountRole.objects.get(code=INVENTORY_CONTROL)
        account = Account.objects.get(organization=organization, code="1-03-01-001")
        response = client.post(
            reverse("accounting:mapping_create"),
            {
                "organization": organization.pk,
                "account_role": role.pk,
                "account": account.pk,
                "effective_from": "2026-01-01",
                "effective_to": "",
            },
        )
        assert response.status_code == 302, response.content
        mapping = OrganizationAccountMapping.objects.get()
        assert mapping.account == account

        page = client.get(reverse("accounting:mapping_list")).content.decode()
        assert "INVENTORY_CONTROL" in page
        assert "1-03-01-001" in page

    def test_the_mapping_screens_refuse_a_branch_accountant(
        self, client_for: Any, branch: Branch
    ) -> None:
        """`manage_account_mappings` is organization authority; an ACCOUNTANT
        post at a branch does not carry it."""
        accountant = User.objects.create_user(username="acct", password="pw-not-real-1234")
        grant_branch_access(user=accountant, branch=branch, role=Role.ACCOUNTANT)
        accountant = User.objects.get(pk=accountant.pk)
        assert client_for(accountant).get(reverse("accounting:mapping_list")).status_code == 403

    def test_the_override_screen_lists_and_gates(
        self,
        accounting_manager: User,
        manager: User,
        client_for: Any,
    ) -> None:
        assert (
            client_for(accounting_manager).get(reverse("inventory:mapping_list")).status_code == 200
        )
        # A branch MANAGER holds no mapping authority: the screen refuses.
        assert client_for(manager).get(reverse("inventory:mapping_list")).status_code == 403


class TestProvenanceRegression:
    """§U: the two halves of authorization must come from the same place."""

    def test_rival_authority_plus_local_reach_grants_nothing(
        self,
        client_for: Any,
        submitted: OpeningStockDocument,
        other_organization: Organization,
        branch: Branch,
    ) -> None:
        """An ACCOUNTING_MANAGER of the rival who also holds a VIEWER post
        here satisfies `has_perm` globally and reaches this organization —
        and still may not post here."""
        hybrid = User.objects.create_user(username="hybrid", password="pw-not-real-1234")
        grant_organization_access(
            user=hybrid, organization=other_organization, role=Role.ACCOUNTING_MANAGER
        )
        grant_branch_access(user=hybrid, branch=branch, role=Role.VIEWER)
        hybrid = User.objects.get(pk=hybrid.pk)
        assert hybrid.has_perm("inventory.post_opening_stock")

        response = client_for(hybrid).post(reverse("inventory:opening_post", args=[submitted.pk]))
        assert response.status_code == 403
        submitted.refresh_from_db()
        assert submitted.status == OpeningStockStatus.SUBMITTED

    def test_a_direct_permission_grant_authorizes_no_organization(
        self,
        client_for: Any,
        submitted: OpeningStockDocument,
        branch: Branch,
    ) -> None:
        """A Django `user_permissions` grant names no post in any
        organization, so it carries no authority anywhere (ADR-016)."""
        from django.contrib.auth.models import Permission

        loner = User.objects.create_user(username="loner", password="pw-not-real-1234")
        grant_branch_access(user=loner, branch=branch, role=Role.VIEWER)
        loner.user_permissions.add(
            Permission.objects.get(
                codename="post_opening_stock", content_type__app_label="inventory"
            )
        )
        loner = User.objects.get(pk=loner.pk)
        assert loner.has_perm("inventory.post_opening_stock")

        response = client_for(loner).post(reverse("inventory:opening_post", args=[submitted.pk]))
        assert response.status_code == 403
        submitted.refresh_from_db()
        assert submitted.status == OpeningStockStatus.SUBMITTED


class TestALineFormRefusesRatherThanCrashes:
    """
    An unparsable package count reached `None % 1` and raised TypeError.

    `_decimal` records its own error and returns None — a comma decimal, which
    an Arabic keyboard produces by default, is the everyday case. The count is
    then unknown rather than fractional, but the fractional check asked anyway
    and the reader got a server error instead of the sentence telling them to
    use a decimal point.
    """

    def test_a_comma_decimal_package_count_is_a_field_error_not_a_crash(
        self,
        manager: User,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        carton: PackageUnit,
    ) -> None:
        conversion = create_item_conversion(
            item=rice,
            package_unit=carton,
            factor_to_base=Decimal("25"),
            effective_from=datetime.date(2026, 1, 1),
            allows_fractional=False,
        )

        form = OpeningLineForm(
            data={
                "warehouse": main_store.pk,
                "item": rice.pk,
                "package_conversion": conversion.pk,
                "entered_package_quantity": "1,5",
                "base_quantity": "",
                "unit_cost": "1500",
            },
            actor=manager,
            branch=branch,
        )

        # The call itself is the assertion: this raised TypeError before.
        assert form.is_valid() is False
        assert form.errors["entered_package_quantity"] == ["استخدم النقطة العشرية لا الفاصلة."]
