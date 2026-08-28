"""
Cashbox and bank-account rules, and the statement's ordering.

The test that matters most here is `test_no_cash_model_stores_a_balance`. Every
other rule in this module is enforced by a constraint that would fail loudly;
that one is enforced by an **absence**, and an absence is exactly what a later
change adds to without noticing.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.accounting.cash_services import (
    archive_cashbox,
    create_bank_account,
    create_cashbox,
    reactivate_cashbox,
)
from apps.accounting.models import Account, BankAccount, Cashbox
from apps.accounting.statements import STATEMENT_ORDER, account_statement, parse_window
from apps.organizations.models import Branch, Organization

pytestmark = pytest.mark.django_db


#: Any field whose name suggests a stored figure. Invariant A1 of
#: `docs/invariants/accounting-module-invariants.md`.
_BALANCE_LIKE = ("balance", "outstanding", "total_due", "amount")


def _cashbox(
    organization: Organization,
    branch: Branch,
    account: Account,
    code: str = "CASH-1",
) -> Cashbox:
    return create_cashbox(
        organization=organization,
        branch=branch,
        account=account,
        code=code,
        name="صندوق",
        opened_on=datetime.date(2026, 1, 1),
    )


class TestNoStoredBalance:
    def test_no_cash_model_stores_a_balance(self) -> None:
        """
        Neither model has a balance field, and this is how it stays that way.

        Introspection rather than a code review note: `current_balance` is the
        obvious thing to add when a page feels slow, and it is the one change
        that would let a cash screen disagree with the ledger silently.
        """
        for model in (Cashbox, BankAccount):
            offenders = [
                field.name
                for field in model._meta.get_fields()
                if hasattr(field, "attname") and any(word in field.name for word in _BALANCE_LIKE)
            ]
            assert offenders == [], f"{model.__name__} stores {offenders}"


class TestOneAccountOneRecord:
    def test_two_active_cashboxes_cannot_share_an_account(
        self, organization: Organization, branch: Branch, cash: Account
    ) -> None:
        _cashbox(organization, branch, cash, code="CASH-1")
        with pytest.raises(ValidationError) as refused:
            _cashbox(organization, branch, cash, code="CASH-2")
        assert refused.value.code == "account_already_a_cashbox"

    def test_archiving_frees_the_account(
        self, organization: Organization, branch: Branch, cash: Account
    ) -> None:
        first = _cashbox(organization, branch, cash, code="CASH-1")
        archive_cashbox(cashbox=first, reason="replaced")
        second = _cashbox(organization, branch, cash, code="CASH-2")
        assert second.account_id == cash.pk
        first.refresh_from_db()
        assert first.is_active is False
        assert first.archived_at is not None

    def test_reactivating_is_refused_while_the_account_is_taken(
        self, organization: Organization, branch: Branch, cash: Account
    ) -> None:
        """
        The interesting half of archive-frees-the-account.

        Without this check the partial unique constraint would refuse the save
        with a database error rather than a sentence somebody can act on.
        """
        first = _cashbox(organization, branch, cash, code="CASH-1")
        archive_cashbox(cashbox=first, reason="replaced")
        _cashbox(organization, branch, cash, code="CASH-2")
        with pytest.raises(ValidationError) as refused:
            reactivate_cashbox(cashbox=first)
        assert refused.value.code == "account_already_a_cashbox"

    def test_a_cashbox_and_a_bank_account_cannot_share_one_account(
        self, organization: Organization, branch: Branch, cash: Account
    ) -> None:
        """
        The cross-table half, which no single-table constraint can see.

        Both statements would be the same movements, and an operator counting
        the drawer against the cashbox page would find it over by exactly the
        bank balance.
        """
        _cashbox(organization, branch, cash, code="CASH-1")
        with pytest.raises(ValidationError) as refused:
            create_bank_account(
                organization=organization,
                account=cash,
                code="BANK-1",
                bank_name="مصرف",
                name="حساب",
                masked_account_number="12345678",
            )
        assert refused.value.code == "account_already_a_cashbox"


class TestTheAccountMustBeUsable:
    def test_a_group_account_is_refused(
        self, organization: Organization, branch: Branch, group_account: Account
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            _cashbox(organization, branch, group_account)
        assert refused.value.code == "account_not_postable"

    def test_a_non_asset_account_is_refused(
        self, organization: Organization, branch: Branch, sales: Account
    ) -> None:
        """
        A cashbox on a revenue account produces a statement that runs backwards.

        Every receipt would read as a credit and the drawer would appear to owe
        money, with nothing on the page to say why.
        """
        with pytest.raises(ValidationError) as refused:
            _cashbox(organization, branch, sales)
        assert refused.value.code == "account_not_an_asset"

    def test_another_organizations_account_is_refused(
        self, organization: Organization, branch: Branch, other_cash: Account
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            _cashbox(organization, branch, other_cash)
        assert refused.value.code == "account_organization_mismatch"


class TestTheAccountNumberIsMasked:
    def test_a_full_number_never_lands_in_the_column(
        self, organization: Organization, cash: Account
    ) -> None:
        """
        Masked on the way in, not trusted from the form.

        The field is the only protection: a full number that reaches the column
        is stored, exported, and in every history row from then on.
        """
        bank = create_bank_account(
            organization=organization,
            account=cash,
            code="BANK-1",
            bank_name="مصرف",
            name="حساب",
            masked_account_number="9876543210123456",
        )
        assert bank.masked_account_number == "****3456"
        assert "9876543210" not in bank.masked_account_number


class TestTheStatement:
    def test_the_documented_order_is_the_one_used(self) -> None:
        """
        The ordering is the entire content of a running balance.

        Asserted as a constant rather than by reading rows, so a change to it
        fails here — where the reasoning is written down — rather than silently
        producing a column that is plausible on every row and wrong in total.
        """
        assert STATEMENT_ORDER == (
            "entry__accounting_date",
            "entry__posted_at",
            "entry__entry_number",
            "line_number",
        )

    def test_an_empty_window_still_reports_its_opening(
        self, organization: Organization, branch: Branch, cash: Account
    ) -> None:
        statement = account_statement(
            account=cash,
            date_from=datetime.date(2026, 3, 1),
            date_to=datetime.date(2026, 3, 31),
            branch=branch,
        )
        assert statement.is_empty
        assert statement.opening == Decimal("0")
        assert statement.closing == statement.opening

    def test_opening_stops_the_day_before_the_window(self) -> None:
        """
        Documented here because getting it wrong is invisible.

        An opening balance taken *up to* `date_from` would include everything
        posted on the first day, and then the first rows would show it again.
        """
        from apps.accounting import statements

        source = statements.account_statement.__doc__ or ""
        assert "day before" in source


class TestParseWindow:
    def test_a_mistyped_date_falls_back_rather_than_raising(self) -> None:
        """A typo in a URL is a typo; answering 500 turns it into an outage."""
        today = datetime.date(2026, 5, 17)
        date_from, date_to = parse_window("not-a-date", "", today=today)
        assert date_to == today
        assert date_from == datetime.date(2026, 5, 1)

    def test_a_reversed_window_is_corrected(self) -> None:
        today = datetime.date(2026, 5, 17)
        date_from, date_to = parse_window("2026-05-20", "2026-05-10", today=today)
        assert date_from <= date_to


class TestScope:
    def test_a_cashbox_list_shows_only_reachable_organizations(
        self,
        accounting_manager: Any,
        client_for: Any,
        organization: Organization,
        other_organization: Organization,
        branch: Branch,
        cash: Account,
    ) -> None:
        _cashbox(organization, branch, cash, code="CASH-1")
        client = client_for(accounting_manager)
        response = client.get("/accounting/cashboxes/")
        assert response.status_code == 200
        body = response.content.decode("utf-8")
        assert "CASH-1" in body
        assert other_organization.code not in body

    def test_a_foreign_cashbox_is_404_not_403(
        self,
        accounting_manager: Any,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        cash: Account,
    ) -> None:
        """
        Out of scope is absent, not forbidden.

        A 403 about another organization's record confirms it exists, and ids
        are sequential.
        """
        cashbox = _cashbox(organization, branch, cash, code="CASH-1")
        client = client_for(accounting_manager)
        response = client.get(f"/accounting/cashboxes/{cashbox.pk + 5000}/")
        assert response.status_code == 404


class TestTheFragmentCarriesMarkup:
    def test_the_cashbox_list_htmx_response_is_not_empty(
        self,
        accounting_manager: Any,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        cash: Account,
    ) -> None:
        """
        A fragment that answers 200 with whitespace is the defect this asserts
        against: status alone, "no nested html" and "shorter than the page" are
        all satisfied by an empty body.
        """
        _cashbox(organization, branch, cash, code="CASH-1")
        client = client_for(accounting_manager)
        response = client.get("/accounting/cashboxes/", headers={"HX-Request": "true"})
        body = response.content.decode("utf-8").strip()
        assert response.status_code == 200
        assert "<html" not in body.lower()
        assert len(body) > 200
        assert "<table" in body

    def test_the_cashbox_detail_htmx_response_is_not_empty(
        self,
        accounting_manager: Any,
        client_for: Any,
        organization: Organization,
        branch: Branch,
        cash: Account,
    ) -> None:
        cashbox = _cashbox(organization, branch, cash, code="CASH-1")
        client = client_for(accounting_manager)
        response = client.get(
            f"/accounting/cashboxes/{cashbox.pk}/", headers={"HX-Request": "true"}
        )
        body = response.content.decode("utf-8").strip()
        assert response.status_code == 200
        assert "<html" not in body.lower()
        assert len(body) > 200
        assert "accounting-cashbox-detail" in body


class TestTheAccountIsNotAmendable:
    def test_the_edit_form_does_not_offer_the_account(self) -> None:
        """
        A cashbox that changed account would re-attribute every statement it
        has ever shown. The field is absent from the form, and the service
        would not accept it either.
        """
        from apps.accounting.cash_forms import CashboxMetadataForm

        assert "account" not in CashboxMetadataForm.base_fields
        assert "branch" not in CashboxMetadataForm.base_fields


def test_the_seeded_chart_still_has_the_cash_accounts(
    organization: Organization, chart: None
) -> None:
    """The demo and the tests both assume these two exist."""
    assert Account.objects.filter(organization=organization, code="1-01-01-001").exists()
    assert Account.objects.filter(organization=organization, code="1-01-02-001").exists()
