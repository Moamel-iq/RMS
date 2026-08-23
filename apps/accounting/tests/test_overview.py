"""
Contracts for the accounting landing page's own figures.

Two rules carry the page. A role with no account in force has **no** balance
— None, not zero — because a zero there claims the stock is worth nothing.
And the trial balance panel may hold rows back, but never its totals: the two
columns at the foot run over every moving account, so they still prove the
ledger when only the largest rows are shown.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.urls import reverse

from apps.accounting.dashboard import accounting_overview, trial_balance_table
from apps.accounting.models import INVENTORY_CONTROL, SUPPLIER_PAYABLE, Account, AccountRole
from apps.accounting.services import create_account_mapping, post_entry
from apps.accounting.validators import PostingLine
from apps.organizations.models import Branch, Organization
from apps.users.models import User

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db

YEAR_START = POSTING_DATE.replace(month=1, day=1)


def _post(
    organization: Organization,
    branch: Branch,
    *,
    debit: Account,
    credit: Account,
    amount: str,
    key: str,
    cost_center: Any = None,
) -> None:
    """One balanced entry; the cost centre goes on the P&L side only."""
    post_entry(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=debit, branch=branch, debit=Decimal(amount)),
            PostingLine(
                account=credit, branch=branch, cost_center=cost_center, credit=Decimal(amount)
            ),
        ],
        narration=key,
        idempotency_key=f"test:overview:{key}",
    )


def _map(organization: Organization, role_code: str, account: Account) -> None:
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=role_code),
        account=account,
        effective_from=YEAR_START,
    )


def test_a_role_without_an_account_has_no_balance_not_zero(
    organization: Organization, branch: Branch, cash: Account, sales: Account, hall: Any
) -> None:
    _post(
        organization, branch, debit=cash, credit=sales, amount="75000", key="sale", cost_center=hall
    )

    before = accounting_overview(organization)

    assert before.posted_entry_count == 1
    assert before.posted_line_count == 2
    assert before.is_sound
    # Nothing is mapped yet: absent, and not a zero that reads as "worthless".
    assert before.inventory is None
    assert before.payable is None
    # And the absence is itself a gap the page names.
    assert "roles" in {gap.key for gap in before.gaps}

    # The read follows the mapping, whatever the account's own nature: here
    # the inventory role points at the debited account and the payable role
    # at the credited one, so one side reads debit and the other credit.
    _map(organization, INVENTORY_CONTROL, cash)
    _map(organization, SUPPLIER_PAYABLE, sales)

    after = accounting_overview(organization)

    assert after.inventory is not None and after.payable is not None
    assert (after.inventory.code, after.inventory.balance, after.inventory.is_credit) == (
        cash.code,
        Decimal("75000.000"),
        False,
    )
    assert (after.payable.code, after.payable.magnitude, after.payable.is_credit) == (
        sales.code,
        Decimal("75000.000"),
        True,
    )


def test_the_trial_balance_panel_holds_rows_back_but_never_its_totals(
    organization: Organization,
    branch: Branch,
    cash: Account,
    sales: Account,
    rent: Account,
    hall: Any,
) -> None:
    _post(
        organization, branch, debit=cash, credit=sales, amount="75000", key="sale", cost_center=hall
    )
    # Rent is the P&L side here, so the cost centre moves to the debit line.
    post_entry(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=rent, branch=branch, cost_center=hall, debit=Decimal("20000")),
            PostingLine(account=cash, branch=branch, credit=Decimal("20000")),
        ],
        narration="rent",
        idempotency_key="test:overview:rent",
    )

    table = trial_balance_table(organization, limit=2)

    # Three accounts moved; the two largest by movement are shown, in code
    # order — cash (95,000 of movement) and sales (75,000) — rent is held back.
    assert [row.code for row in table.rows] == [cash.code, sales.code]
    assert (table.account_count, table.hidden_count) == (3, 1)
    # The totals are over all three, and they tie.
    assert table.total_debits == table.total_credits == Decimal("95000.000")
    assert table.is_balanced
    # A credit balance is a credit balance, shown as a magnitude in parentheses.
    sales_row = table.rows[1]
    assert sales_row.is_credit and sales_row.magnitude == Decimal("75000.000")
    assert not table.rows[0].is_credit and table.rows[0].balance == Decimal("55000.000")


def test_the_page_renders_the_headline_and_the_panel_answers_as_a_fragment(
    client_for: Any,
    accountant: User,
    organization: Organization,
    branch: Branch,
    cash: Account,
    sales: Account,
    hall: Any,
) -> None:
    _post(
        organization, branch, debit=cash, credit=sales, amount="75000", key="sale", cost_center=hall
    )
    client = client_for(accountant)

    page = client.get(reverse("accounting:dashboard"))
    body = page.content.decode("utf-8")

    assert page.status_code == 200
    for label in (
        "قيود مرحّلة",
        "دليل الحسابات",
        "قيود غير متزنة",
        "ما لم يُسجَّل بعد",
        "الفترات المحاسبية",
    ):
        assert label in body, label
    # Unmapped roles: a dash with the reason, not a zero.
    assert "دور المخزون بلا حساب سارٍ" in body
    # The ledger is sound and the fiscal year covers today: no alert.
    assert "قيود مرحّلة غير متزنة" not in body
    assert "لا فترة محاسبية تغطي اليوم" not in body

    panel = client.get(
        reverse("accounting:dashboard_card", args=["trial_balance_rows"]),
        {"organization": organization.pk},
        headers={"HX-Request": "true"},
    )
    fragment = panel.content.decode("utf-8")

    assert panel.status_code == 200
    assert "<html" not in fragment.lower()
    assert cash.code in fragment and sales.code in fragment
    assert "متوازن" in fragment
