"""
Contracts for the procurement overview screen.

Two separate protections meet on this one page, and confusing them is the way
it goes wrong:

* **Scope** — an invoice is a debt, so `visible_supplier_invoices` asks
  whether a post the caller actually *holds* carries the view permission, not
  merely whether they can reach the organization (PRC-060). A branch manager
  runs a branch end to end including its buying, so they legitimately see
  these; a cashier holds no such post and must come back empty.
* **Cost** — `view_supplier_cost` decides whether amounts render at all, and
  they are omitted rather than zeroed. No role today separates the two
  permissions, so this contract is only reachable through the function; that
  is exactly why it is tested there rather than only through a client.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounting.models import SUPPLIER_PAYABLE, Account, AccountRole, CostCenter
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.organizations.models import Branch, Organization
from apps.procurement.dashboard import procurement_overview
from apps.procurement.invoices import (
    add_account_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    post_supplier_invoice,
)
from apps.procurement.models import Supplier
from apps.procurement.services import create_supplier
from apps.users.models import User

pytestmark = pytest.mark.django_db

MARCH = datetime.date(2026, 3, 10)


@pytest.fixture
def accounting(organization: Organization) -> None:
    """A real chart and an open year — posting an invoice needs both."""
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=2026)
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)
    # An account line posts Dr expense / Cr payable, so the payable role has to
    # resolve before any invoice can reach the ledger.
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=SUPPLIER_PAYABLE),
        account=Account.objects.get(organization=organization, code="2-01-01-001"),
        effective_from=datetime.date(2026, 1, 1),
    )


@pytest.fixture
def supplier(organization: Organization) -> Supplier:
    return create_supplier(
        organization=organization,
        code="OVERVIEW-SUP",
        name="مورد النظرة العامة",
        payment_terms_days=0,
    )


def _posted_invoice(
    *,
    supplier: Supplier,
    branch: Branch,
    author: User,
    approver: User,
    poster: User,
    number: str,
    amount: str,
) -> None:
    """One posted invoice, built through the real services rather than ORM."""
    invoice = create_supplier_invoice(
        supplier=supplier,
        branch=branch,
        created_by=author,
        supplier_invoice_number=number,
        invoice_date=MARCH,
        business_date=MARCH,
    )
    add_account_line(
        invoice=invoice,
        account=Account.objects.get(organization=supplier.organization, code="5-01-02-003"),
        cost_center=CostCenter.objects.get(organization=supplier.organization, code="DELIVERY"),
        description="اختبار النظرة العامة",
        quantity=Decimal("1.000"),
        unit_price=Decimal(amount),
    )
    approve_supplier_invoice(invoice=invoice, actor=approver)
    post_supplier_invoice(invoice=invoice, actor=poster)


def test_a_post_without_the_permission_sees_no_debt(
    organization: Organization,
    branch: Branch,
    supplier: Supplier,
    cashier: User,
    accounting_manager: User,
    superuser: User,
    accounting: None,
) -> None:
    _posted_invoice(
        supplier=supplier,
        branch=branch,
        author=accounting_manager,
        approver=superuser,
        poster=accounting_manager,
        number="OV-1",
        amount="1000.000000",
    )

    # The cashier reaches this organization through their branch, and still
    # sees no invoice: reaching an organization is not authority over its
    # debts, and their post carries no invoice permission (PRC-060).
    theirs = procurement_overview(cashier, include_cost=True)
    assert theirs.invoice_count == 0
    assert theirs.posted_total == Decimal("0")
    assert theirs.rows == []

    mine = procurement_overview(accounting_manager, include_cost=True)
    assert mine.posted_count == 1
    assert mine.posted_total == Decimal("1000.000")


def test_without_cost_rights_the_amounts_are_absent_not_zero(
    organization: Organization,
    branch: Branch,
    supplier: Supplier,
    accounting_manager: User,
    superuser: User,
    accounting: None,
) -> None:
    _posted_invoice(
        supplier=supplier,
        branch=branch,
        author=accounting_manager,
        approver=superuser,
        poster=accounting_manager,
        number="OV-2",
        amount="2500.000000",
    )

    redacted = procurement_overview(accounting_manager, include_cost=False)

    # None, not Decimal("0") — the template tests `is not None` and drops the
    # card, and a zero would have read as a real figure.
    assert redacted.posted_total is None
    assert redacted.payable_total is None
    assert redacted.top_share is None
    assert [row.total for row in redacted.rows] == [None]
    # Counts survive: how many invoices are waiting is not money.
    assert redacted.posted_count == 1
    assert redacted.rows[0].invoice_count == 1


def test_the_supplier_share_is_the_concentration_risk(
    organization: Organization,
    branch: Branch,
    supplier: Supplier,
    accounting_manager: User,
    superuser: User,
    accounting: None,
) -> None:
    other = create_supplier(
        organization=organization,
        code="OVERVIEW-SUP-2",
        name="مورد ثانٍ",
        payment_terms_days=0,
    )
    for number, amount, who in (
        ("OV-3", "3000.000000", supplier),
        ("OV-4", "1000.000000", other),
    ):
        _posted_invoice(
            supplier=who,
            branch=branch,
            author=accounting_manager,
            approver=superuser,
            poster=accounting_manager,
            number=number,
            amount=amount,
        )

    overview = procurement_overview(accounting_manager, include_cost=True)

    assert overview.posted_total == Decimal("4000.000")
    assert [row.share for row in overview.rows] == [Decimal("75.0"), Decimal("25.0")]
    # The alert threshold reads this, so it has to be the largest share.
    assert overview.top_share == Decimal("75.0")


def test_the_screen_renders_and_hides_cost_from_the_storekeeper(
    organization: Organization,
    branch: Branch,
    supplier: Supplier,
    accounting_manager: User,
    keeper: User,
    superuser: User,
    client_for: Callable[[User], Client],
    accounting: None,
) -> None:
    _posted_invoice(
        supplier=supplier,
        branch=branch,
        author=accounting_manager,
        approver=superuser,
        poster=accounting_manager,
        number="OV-5",
        amount="7500.000000",
    )
    url = reverse("procurement:overview")

    entitled = client_for(accounting_manager).get(url)
    assert entitled.status_code == 200
    body = entitled.content.decode()
    assert "قيمة المشتريات المرحّلة" in body
    assert "7,500" in body

    # The storekeeper either lacks the view permission outright (403/404) or
    # sees the screen without a single figure. Both are correct; a screen with
    # the amount on it is not.
    keeper_response = client_for(keeper).get(url)
    if keeper_response.status_code == 200:
        keeper_body = keeper_response.content.decode()
        assert "قيمة المشتريات المرحّلة" not in keeper_body
        assert "7,500" not in keeper_body
