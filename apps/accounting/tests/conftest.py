"""Shared fixtures for the accounting kernel tests."""

from __future__ import annotations

import datetime
from datetime import time

import pytest

from apps.accounting.models import Account, CostCenter
from apps.accounting.services import configure_accounting, open_fiscal_year
from apps.organizations.models import Branch, Organization
from apps.organizations.services import create_branch, create_organization
from apps.users.models import User

PASSWORD = "pw-not-real-1234"
TEST_YEAR = 2026
POSTING_DATE = datetime.date(TEST_YEAR, 3, 15)


@pytest.fixture
def actor() -> User:
    return User.objects.create_user(username="accountant", password=PASSWORD)


@pytest.fixture
def organization() -> Organization:
    org = create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")
    configure_accounting(organization=org, fiscal_year_start_month=1)
    open_fiscal_year(organization=org, year=TEST_YEAR)
    return org


@pytest.fixture
def other_organization() -> Organization:
    org = create_organization(code="RIVAL", name_ar="منافس", name_en="Rival")
    configure_accounting(organization=org, fiscal_year_start_month=1)
    open_fiscal_year(organization=org, year=TEST_YEAR)
    return org


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
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
