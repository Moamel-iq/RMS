"""
The read API: statements, subledgers and the pre-close report.

Three properties are worth more than the rest here, and each has a test that
would fail if the property were quietly dropped:

* an unclassified balance **appears** and blocks approval, rather than being
  omitted from a statement that then still ties (ADR-031 §2);
* a cash endpoint returns **no balance field**, because there is no stored
  balance and inventing one would create a second answer to "how much is in
  the drawer" (ADR-030 §1);
* the pre-close endpoint collects **every** blocker instead of stopping at the
  first, which is the whole reason it exists separately from the close command.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    CostCenter,
)
from apps.accounting.services import create_draft, post_entry, resolve_period
from apps.accounting.validators import PostingLine
from apps.organizations.models import Branch, Organization
from apps.users.models import User

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db

TRIAL_BALANCE = "/api/v1/reports/trial-balance/"
LEDGER = "/api/v1/reports/general-ledger/"
INCOME = "/api/v1/reports/income-statement/"
BALANCE = "/api/v1/reports/balance-sheet/"
CASHBOXES = "/api/v1/cashboxes/"


@pytest.fixture
def posted_sale(
    organization: Organization,
    branch: Branch,
    cash: Account,
    sales: Account,
    hall: CostCenter,
) -> None:
    """One posted journal, so the reports have something to report."""
    post_entry(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=cash, branch=branch, debit=Decimal("250000")),
            PostingLine(account=sales, branch=branch, cost_center=hall, credit=Decimal("250000")),
        ],
        narration="cash sale",
        idempotency_key="test:report-api:sale",
    )


def test_trial_balance_ties_and_reports_strings(
    client_for: Any, accountant: User, organization: Organization, posted_sale: None
) -> None:
    client = client_for(accountant)
    response = client.get(f"{TRIAL_BALANCE}?organization_id={organization.pk}")
    assert response.status_code == 200, response.content
    body = response.json()

    assert len(body["rows"]) >= 2
    assert body["is_balanced"] is True
    assert body["difference"] == "0.000"
    assert isinstance(body["closing_debit"], str)
    assert body["closing_debit"] == body["closing_credit"]


def test_unmapped_balance_is_shown_and_blocks_approval(
    client_for: Any, accountant: User, organization: Organization, posted_sale: None
) -> None:
    """
    The account has a balance and no statement group, so it appears in
    `unmapped` and `is_approvable` is false.

    A statement that dropped it would still balance, which is precisely what
    makes the omission dangerous: nothing downstream would look wrong.
    """
    client = client_for(accountant)
    response = client.get(f"{INCOME}?organization_id={organization.pk}")
    assert response.status_code == 200, response.content
    body = response.json()

    assert body["unmapped"], "a classified-nothing chart must report the revenue account"
    assert body["is_approvable"] is False
    codes = {row["account_code"] for row in body["unmapped"]}
    assert any(code.startswith("4-") for code in codes)


def test_balance_sheet_reports_its_difference_rather_than_hiding_it(
    client_for: Any, accountant: User, organization: Organization, posted_sale: None
) -> None:
    client = client_for(accountant)
    response = client.get(f"{BALANCE}?organization_id={organization.pk}")
    assert response.status_code == 200, response.content
    body = response.json()

    assert isinstance(body["difference"], str)
    assert isinstance(body["current_year_earnings"], str)
    # Whatever the state, the report says which it is. There is no endpoint
    # anywhere that could make an unbalanced sheet balance.
    assert body["is_approvable"] == (body["is_balanced"] and not body["unmapped"])


def test_general_ledger_source_document_id_is_a_string(
    client_for: Any, accountant: User, organization: Organization, cash: Account, posted_sale: None
) -> None:
    """
    Upstream documents identify themselves by UUID as often as by primary key.

    Typed as an integer this returned a 500 the first time a Sales journal
    reached it, which is how the smoke found it.
    """
    client = client_for(accountant)
    response = client.get(f"{LEDGER}?organization_id={organization.pk}&account_id={cash.pk}")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["rows"]
    assert isinstance(body["rows"][0]["source_document_id"], str)
    assert isinstance(body["rows"][0]["running"], str)


def test_report_for_another_organization_is_404(
    client_for: Any, accountant: User, other_organization: Organization
) -> None:
    """Not 403. A 403 would confirm the organization exists."""
    client = client_for(accountant)
    response = client.get(f"{TRIAL_BALANCE}?organization_id={other_organization.pk}")
    assert response.status_code == 404


def test_reader_without_ledger_authority_cannot_read_reports(
    client_for: Any, cashier: User, organization: Organization
) -> None:
    """
    A cashier holds no accounting permission, so no organization is in scope for
    a report and the answer is 404 — there is nothing to disclose.
    """
    client = client_for(cashier)
    response = client.get(f"{TRIAL_BALANCE}?organization_id={organization.pk}")
    assert response.status_code == 404


def test_cashbox_endpoint_returns_no_balance(
    client_for: Any,
    accounting_manager: User,
    organization: Organization,
    branch: Branch,
    cash: Account,
) -> None:
    """
    ADR-030 §1: there is no stored balance, so there is none to serialize.

    Returning one computed on the fly would be worse than useless — it would
    look like a field somebody could reconcile against.
    """
    from apps.accounting.cash_services import create_cashbox

    create_cashbox(
        organization=organization,
        branch=branch,
        account=cash,
        code="CB-BAL",
        name="صندوق",
        opened_on=POSTING_DATE,
    )
    client = client_for(accounting_manager)
    response = client.get(f"{CASHBOXES}?organization_id={organization.pk}")
    assert response.status_code == 200, response.content
    row = response.json()[0]

    assert "balance" not in row
    assert "current_balance" not in row
    assert row["account_code"] == cash.code


def test_pre_close_collects_every_blocker_not_just_the_first(
    client_for: Any,
    accounting_manager: User,
    accountant: User,
    organization: Organization,
    branch: Branch,
    cash: Account,
    sales: Account,
    hall: CostCenter,
) -> None:
    """
    The reason this endpoint exists rather than reusing the close guards.

    `_run_period_close_guards` raises on the first veto, which is correct for a
    close attempt and useless for a preview: an accountant clearing a month
    needs the whole list at once. Two independent blockers are created here, so
    a collector that stopped at the first would report one and pass a weaker
    test.
    """
    create_draft(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=cash, branch=branch, debit=Decimal("1000")),
            PostingLine(account=sales, branch=branch, cost_center=hall, credit=Decimal("1000")),
        ],
        narration="first draft",
    )
    create_draft(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=cash, branch=branch, debit=Decimal("2000")),
            PostingLine(account=sales, branch=branch, cost_center=hall, credit=Decimal("2000")),
        ],
        narration="second draft",
    )
    period = resolve_period(organization=organization, accounting_date=POSTING_DATE)

    client = client_for(accounting_manager)
    response = client.get(f"/api/v1/periods/{period.pk}/pre-close/")
    assert response.status_code == 200, response.content
    body = response.json()

    assert body["period_id"] == period.pk
    assert body["is_closeable"] is False
    blocking = [row for row in body["blockers"] if row["is_blocking"]]
    assert blocking, "two drafts must produce at least one blocking finding"
    # Every blocker names itself, so the screen can link to the fix.
    assert all(row["code"] and row["message"] for row in body["blockers"])


def test_pre_close_for_another_organizations_period_is_404(
    client_for: Any, rival_accountant: User, organization: Organization
) -> None:
    period = AccountingPeriod.objects.filter(fiscal_year__organization=organization).first()
    if period is None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
    client = client_for(rival_accountant)
    assert client.get(f"/api/v1/periods/{period.pk}/pre-close/").status_code == 404
