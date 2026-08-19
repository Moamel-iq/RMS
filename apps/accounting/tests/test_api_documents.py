"""
The document and report API, over HTTP.

Exercised through the real URL conf and the real auth backend, because the
properties worth checking here are the ones a direct call to the command layer
cannot demonstrate: that a submitted id reaches a scoped resolver instead of a
bare queryset, that the two failure shapes stay distinguishable (404 for out of
scope, 403 for in scope without authority), that money survives the round trip
as an exact string, and that maker-checker is refused on the API path with no
help from the API itself.

The last one is the reason several of these tests use two different users. A
test where one user does everything would pass against a build with no
maker-checker at all.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from apps.accounting.models import (
    Account,
    BankAccount,
    Cashbox,
    CostCenter,
    ExpenseVoucher,
    FinancialDocumentStatus,
    Prepayment,
)
from apps.organizations.models import Branch, Organization
from apps.users.models import User

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db

VOUCHERS = "/api/v1/expense-vouchers/"
ACCRUALS = "/api/v1/accruals/"
PREPAYMENTS = "/api/v1/prepayments/"
CASHBOXES = "/api/v1/cashboxes/"
BANKS = "/api/v1/bank-accounts/"


def _post(client: Client, url: str, payload: dict[str, Any] | None = None) -> Any:
    return client.post(url, data=json.dumps(payload or {}), content_type="application/json")


def _patch(client: Client, url: str, payload: dict[str, Any]) -> Any:
    return client.patch(url, data=json.dumps(payload), content_type="application/json")


@pytest.fixture
def demo_cashbox(organization: Organization, branch: Branch, cash: Account) -> Cashbox:
    from apps.accounting.cash_services import create_cashbox

    return create_cashbox(
        organization=organization,
        branch=branch,
        account=cash,
        code="CB-API",
        name_ar="صندوق الاختبار",
        name_en="Test cashbox",
        opened_on=POSTING_DATE,
    )


def _voucher_payload(branch: Branch, cashbox: Cashbox) -> dict[str, Any]:
    return {
        "branch_id": branch.pk,
        "business_date": POSTING_DATE.isoformat(),
        "expense_date": POSTING_DATE.isoformat(),
        "beneficiary": "مكتب الكهرباء",
        "reason": "فاتورة كهرباء",
        "cashbox_id": cashbox.pk,
    }


# ---------------------------------------------------------------------------
# Authentication and the two failure shapes
# ---------------------------------------------------------------------------


def test_anonymous_is_refused() -> None:
    """Every new endpoint is private, because the API authenticates by default."""
    client = Client()
    for url in (VOUCHERS, ACCRUALS, PREPAYMENTS, CASHBOXES, "/api/v1/reports/trial-balance/"):
        assert client.get(url).status_code == 401, url


def test_out_of_scope_voucher_is_404_not_403(
    client_for: Any,
    rival_accountant: User,
    accountant: User,
    branch: Branch,
    demo_cashbox: Cashbox,
) -> None:
    """
    A rival organization's voucher does not exist, as far as this caller knows.

    404 rather than 403 on purpose: a 403 would confirm the row exists, and
    voucher ids are sequential (ADR-016).
    """
    author = client_for(accountant)
    created = _post(author, VOUCHERS, _voucher_payload(branch, demo_cashbox))
    assert created.status_code == 201, created.content
    voucher_id = created.json()["id"]

    rival = client_for(rival_accountant)
    assert rival.get(f"{VOUCHERS}{voucher_id}/").status_code == 404


def test_in_scope_without_authority_is_403(
    client_for: Any, cashier: User, branch: Branch, demo_cashbox: Cashbox
) -> None:
    """A cashier is inside the organization and holds no expense authority."""
    response = _post(client_for(cashier), VOUCHERS, _voucher_payload(branch, demo_cashbox))
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# المصروفات — the expense voucher lifecycle
# ---------------------------------------------------------------------------


def test_expense_voucher_full_lifecycle(
    client_for: Any,
    accountant: User,
    accounting_manager: User,
    branch: Branch,
    rent: Account,
    hall: CostCenter,
    demo_cashbox: Cashbox,
) -> None:
    """
    Open → add a line → approve → post → reverse, through HTTP only.

    Two actors throughout: the accountant writes, the manager releases. The
    services refuse the single-actor version, so a test that used one client
    would pass against a build with the control removed.
    """
    author = client_for(accountant)
    created = _post(author, VOUCHERS, _voucher_payload(branch, demo_cashbox))
    assert created.status_code == 201, created.content
    voucher_id = created.json()["id"]
    assert created.json()["status"] == FinancialDocumentStatus.DRAFT

    lined = _post(
        author,
        f"{VOUCHERS}{voucher_id}/lines/",
        {
            "account_id": rent.pk,
            "amount": "125000.125",
            "cost_center_id": hall.pk,
            "description": "إيجار",
        },
    )
    assert lined.status_code == 201, lined.content
    body = lined.json()
    # Exact, as a string. A float round trip would show up here as
    # 125000.12499999999 and no later care could recover the thousandth.
    assert body["lines"][0]["amount"] == "125000.125"
    assert body["total_amount"] == "125000.125"

    approver = client_for(accounting_manager)
    approved = _post(approver, f"{VOUCHERS}{voucher_id}/approve/", {"reason": "checked"})
    assert approved.status_code == 200, approved.content
    assert approved.json()["status"] == FinancialDocumentStatus.APPROVED
    assert approved.json()["number"]  # numbered on leaving draft

    posted = _post(approver, f"{VOUCHERS}{voucher_id}/post/", {"reason": "post"})
    assert posted.status_code == 200, posted.content
    assert posted.json()["status"] == FinancialDocumentStatus.POSTED
    assert posted.json()["journal_entry_id"] is not None

    reversed_ = _post(
        approver, f"{VOUCHERS}{voucher_id}/reverse/", {"reason": "paid the wrong office"}
    )
    assert reversed_.status_code == 200, reversed_.content
    assert reversed_.json()["status"] == FinancialDocumentStatus.REVERSED
    # The original journal stays. A correction appends; it never erases.
    assert reversed_.json()["journal_entry_id"] is not None
    assert reversed_.json()["reversal_entry_id"] is not None


def test_author_cannot_approve_own_voucher(
    client_for: Any,
    accounting_manager: User,
    branch: Branch,
    rent: Account,
    hall: CostCenter,
    demo_cashbox: Cashbox,
) -> None:
    """
    Holding both permissions is not enough. The service refuses the same person.

    The manager here holds every accounting permission there is, which is
    exactly the case that would slip through if maker-checker lived in the view.
    """
    client = client_for(accounting_manager)
    created = _post(client, VOUCHERS, _voucher_payload(branch, demo_cashbox))
    assert created.status_code == 201, created.content
    voucher_id = created.json()["id"]
    _post(
        client,
        f"{VOUCHERS}{voucher_id}/lines/",
        {"account_id": rent.pk, "amount": "1000", "cost_center_id": hall.pk},
    )

    refused = _post(client, f"{VOUCHERS}{voucher_id}/approve/", {"reason": "self"})
    assert refused.status_code == 422
    assert refused.json()["code"] == "voucher_self_approved"


def test_voucher_needs_exactly_one_payment_source(
    client_for: Any,
    accountant: User,
    branch: Branch,
    demo_cashbox: Cashbox,
    organization: Organization,
    cash: Account,
) -> None:
    """Neither zero sources nor two. The credit side would be absent or ambiguous."""
    client = client_for(accountant)

    payload = _voucher_payload(branch, demo_cashbox)
    del payload["cashbox_id"]
    none_given = _post(client, VOUCHERS, payload)
    assert none_given.status_code == 422
    assert none_given.json()["code"] == "payment_source_not_exactly_one"

    bank = BankAccount.objects.create(
        organization=organization,
        account=cash,
        code="BANK-API",
        bank_name="مصرف الاختبار",
        name_ar="حساب",
        name_en="Account",
        masked_account_number="****1234",
    )
    both = _voucher_payload(branch, demo_cashbox) | {"bank_account_id": bank.pk}
    two_given = _post(client, VOUCHERS, both)
    assert two_given.status_code == 422
    assert two_given.json()["code"] == "payment_source_not_exactly_one"


def test_posted_voucher_cannot_be_discarded(
    client_for: Any,
    accountant: User,
    accounting_manager: User,
    branch: Branch,
    rent: Account,
    hall: CostCenter,
    demo_cashbox: Cashbox,
) -> None:
    """Nothing that reached the ledger takes the discard path."""
    author = client_for(accountant)
    voucher_id = _post(author, VOUCHERS, _voucher_payload(branch, demo_cashbox)).json()["id"]
    _post(
        author,
        f"{VOUCHERS}{voucher_id}/lines/",
        {"account_id": rent.pk, "amount": "500", "cost_center_id": hall.pk},
    )
    approver = client_for(accounting_manager)
    _post(approver, f"{VOUCHERS}{voucher_id}/approve/", {"reason": "ok"})
    _post(approver, f"{VOUCHERS}{voucher_id}/post/", {"reason": "ok"})

    refused = author.delete(f"{VOUCHERS}{voucher_id}/")
    assert refused.status_code == 422
    assert ExpenseVoucher.objects.filter(pk=voucher_id).exists()


# ---------------------------------------------------------------------------
# المستحقات — accruals
# ---------------------------------------------------------------------------


def test_accrual_lifecycle_and_line_totals(
    client_for: Any,
    accounting_manager: User,
    accountant: User,
    branch: Branch,
    rent: Account,
    hall: CostCenter,
) -> None:
    """The header total is the sum of the lines, at every step."""
    manager = client_for(accounting_manager)
    created = _post(
        manager,
        ACCRUALS,
        {
            "branch_id": branch.pk,
            "business_date": POSTING_DATE.isoformat(),
            "description": "كهرباء لم تصل فاتورتها",
        },
    )
    assert created.status_code == 201, created.content
    accrual_id = created.json()["id"]

    for amount in ("100.001", "200.002"):
        added = _post(
            manager,
            f"{ACCRUALS}{accrual_id}/lines/",
            {"account_id": rent.pk, "amount": amount, "cost_center_id": hall.pk},
        )
        assert added.status_code == 201, added.content

    body = added.json()
    assert [line["amount"] for line in body["lines"]] == ["100.001", "200.002"]
    assert body["total_amount"] == "300.003"

    # The author may not approve their own, even holding every permission.
    refused = _post(manager, f"{ACCRUALS}{accrual_id}/approve/", {"reason": "self"})
    assert refused.status_code == 422
    assert refused.json()["code"] == "self_approved"


def test_accrual_line_refuses_a_non_expense_account(
    client_for: Any, accounting_manager: User, branch: Branch, cash: Account
) -> None:
    """An accrual crediting cash would be a payment nobody made."""
    manager = client_for(accounting_manager)
    accrual_id = _post(
        manager,
        ACCRUALS,
        {
            "branch_id": branch.pk,
            "business_date": POSTING_DATE.isoformat(),
            "description": "خطأ",
        },
    ).json()["id"]

    refused = _post(
        manager, f"{ACCRUALS}{accrual_id}/lines/", {"account_id": cash.pk, "amount": "100"}
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "account_not_an_expense"


# ---------------------------------------------------------------------------
# المقدمات — prepayments
# ---------------------------------------------------------------------------


def test_prepayment_schedule_is_exact(
    client_for: Any,
    accounting_manager: User,
    branch: Branch,
    rent: Account,
    hall: CostCenter,
    cash: Account,
    demo_cashbox: Cashbox,
) -> None:
    """
    The ADR-006 counterexample, over HTTP.

    1,000,000 across three periods at three decimals is 333,333.333 each, which
    sums to 999,999.999. The allocator gives the residual to one period, so the
    schedule sums to the header exactly and the prepaid account can reach zero.
    """
    manager = client_for(accounting_manager)
    created = _post(
        manager,
        PREPAYMENTS,
        {
            "branch_id": branch.pk,
            "business_date": POSTING_DATE.isoformat(),
            "description": "إيجار سنوي",
            "total_amount": "1000000",
            "start_date": POSTING_DATE.replace(day=1).isoformat(),
            "frequency": "MONTHLY",
            "period_count": 3,
            "expense_account_id": rent.pk,
            "prepaid_account_id": rent.pk,
            "cost_center_id": hall.pk,
            "cashbox_id": demo_cashbox.pk,
        },
    )
    assert created.status_code == 201, created.content
    body = created.json()

    amounts = [Decimal(line["amount"]) for line in body["schedule_lines"]]
    assert len(amounts) == 3
    assert sum(amounts) == Decimal("1000000.000")
    assert body["schedule_total"] == body["total_amount"]
    # Not three equal thirds: one period carries the residual thousandth.
    assert len(set(amounts)) == 2

    # end_date is derived from the schedule, never submitted.
    prepayment = Prepayment.objects.get(pk=body["id"])
    last = prepayment.schedule_lines.order_by("-sequence").first()
    assert last is not None
    assert prepayment.end_date == last.period_end


def test_prepayment_end_date_cannot_be_submitted(
    client_for: Any,
    accounting_manager: User,
    branch: Branch,
    rent: Account,
    hall: CostCenter,
    demo_cashbox: Cashbox,
) -> None:
    """
    A submitted `end_date` is ignored, not honoured.

    Django Ninja drops unknown fields, and that is the behaviour wanted here:
    an end date that disagreed with the final schedule line by a day would put
    two different assets on the balance sheet.
    """
    manager = client_for(accounting_manager)
    created = _post(
        manager,
        PREPAYMENTS,
        {
            "branch_id": branch.pk,
            "business_date": POSTING_DATE.isoformat(),
            "description": "تأمين",
            "total_amount": "300",
            "start_date": POSTING_DATE.replace(day=1).isoformat(),
            "frequency": "MONTHLY",
            "period_count": 3,
            "expense_account_id": rent.pk,
            "prepaid_account_id": rent.pk,
            "cost_center_id": hall.pk,
            "cashbox_id": demo_cashbox.pk,
            "end_date": "2099-12-31",
        },
    )
    assert created.status_code == 201, created.content
    assert created.json()["end_date"] != "2099-12-31"


# ---------------------------------------------------------------------------
# Money crosses as a string, in both directions
# ---------------------------------------------------------------------------


def test_amounts_leave_as_strings(
    client_for: Any,
    accountant: User,
    branch: Branch,
    rent: Account,
    hall: CostCenter,
    demo_cashbox: Cashbox,
) -> None:
    """
    Not floats. `json.loads` would turn a bare number into a binary float here,
    so the assertion is on the raw type as parsed.
    """
    client = client_for(accountant)
    voucher_id = _post(client, VOUCHERS, _voucher_payload(branch, demo_cashbox)).json()["id"]
    body = _post(
        client,
        f"{VOUCHERS}{voucher_id}/lines/",
        {"account_id": rent.pk, "amount": "0.001", "cost_center_id": hall.pk},
    ).json()

    assert isinstance(body["total_amount"], str)
    assert isinstance(body["lines"][0]["amount"], str)
    assert body["total_amount"] == "0.001"
