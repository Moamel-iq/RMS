"""Shared fixtures for the accounting kernel tests."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from datetime import time

import pytest
from django.test import Client

from apps.accounting.models import Account, CostCenter
from apps.accounting.services import configure_accounting, open_fiscal_year
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.users.models import User

PASSWORD = "pw-not-real-1234"
TEST_YEAR = 2026
POSTING_DATE = datetime.date(TEST_YEAR, 3, 15)


@pytest.fixture
def actor() -> User:
    return User.objects.create_user(username="accountant", password=PASSWORD)


@pytest.fixture
def organization() -> Organization:
    org = create_organization(code="KM", name="خان مندي")
    configure_accounting(organization=org, fiscal_year_start_month=1)
    open_fiscal_year(organization=org, year=TEST_YEAR)
    return org


@pytest.fixture
def other_organization() -> Organization:
    org = create_organization(code="RIVAL", name="منافس")
    configure_accounting(organization=org, fiscal_year_start_month=1)
    open_fiscal_year(organization=org, year=TEST_YEAR)
    return org


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK",
        name="البنوك",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def chart(organization: Organization) -> None:
    from django.core.management import call_command

    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def cash(organization: Organization, chart: None) -> Account:
    return Account.objects.get(organization=organization, code="1-01-01-001")


@pytest.fixture
def sales(organization: Organization, chart: None) -> Account:
    return Account.objects.get(organization=organization, code="4-01-01-001")


@pytest.fixture
def rent(organization: Organization, chart: None) -> Account:
    return Account.objects.get(organization=organization, code="6-01-02-001")


@pytest.fixture
def group_account(organization: Organization, chart: None) -> Account:
    """A non-leaf account. Nothing may post to it."""
    return Account.objects.get(organization=organization, code="1-01-01")


@pytest.fixture
def hall(organization: Organization, chart: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="HALL")


# ---------------------------------------------------------------------------
# Authorization fixtures (Task 0.7)
# ---------------------------------------------------------------------------
#
# Every user here is built through the grant services, never by writing a
# membership row directly. The services are what synchronise the role groups
# that carry the permissions, so a test that inserted the row itself would be
# testing a state the application cannot produce.


@pytest.fixture
def second_branch(organization: Organization) -> Branch:
    """A second branch in the same organization, for cross-branch tests."""
    return create_branch(
        organization=organization,
        code="KARRADA",
        name="الكرادة",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def other_branch(other_organization: Organization) -> Branch:
    """A branch belonging to the rival organization."""
    return create_branch(
        organization=other_organization,
        code="RIVALBR",
        name="فرع المنافس",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def other_chart(other_organization: Organization) -> None:
    from django.core.management import call_command

    call_command("seed_chart_of_accounts", organization="RIVAL", verbosity=0)


@pytest.fixture
def other_cash(other_organization: Organization, other_chart: None) -> Account:
    return Account.objects.get(organization=other_organization, code="1-01-01-001")


@pytest.fixture
def other_hall(other_organization: Organization, other_chart: None) -> CostCenter:
    return CostCenter.objects.get(organization=other_organization, code="HALL")


def _user(username: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


@pytest.fixture
def accountant(branch: Branch) -> User:
    """A branch accountant: may draft, post, and reverse — at this branch only."""
    user = _user("branch-accountant")
    grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTANT)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def accounting_manager(organization: Organization) -> User:
    """
    The Chief Accountant. Organization-wide authority, and the only role that
    carries `accounting.reopen_period` by default.
    """
    user = _user("accounting-manager")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def branch_manager(branch: Branch) -> User:
    """Reads the ledger, changes nothing in it."""
    user = _user("branch-manager")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def cashier(branch: Branch) -> User:
    """Holds no accounting authority at all."""
    user = _user("cashier")
    grant_branch_access(user=user, branch=branch, role=Role.CASHIER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def branch_accountant_elsewhere(second_branch: Branch) -> User:
    """
    An accountant at a different branch of the SAME organization.

    The user that proves branch scope actually narrows: identical permissions
    to `accountant`, no access to `branch`.
    """
    user = _user("karrada-accountant")
    grant_branch_access(user=user, branch=second_branch, role=Role.ACCOUNTANT)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def rival_accountant(other_branch: Branch) -> User:
    """An accountant in the rival organization."""
    user = _user("rival-accountant")
    grant_branch_access(user=user, branch=other_branch, role=Role.ACCOUNTANT)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def superuser() -> User:
    """Emergency authority. Reaches everything, bypasses nothing."""
    return User.objects.create_superuser(username="root", password=PASSWORD)


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    """A logged-in test client for a given user."""

    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login
