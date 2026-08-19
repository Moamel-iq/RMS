"""
Fixtures for the sales tests.

Hand-built rather than seeded from the demo dataset, for the reason the
procurement fixtures record: the menu, its prices and the channels touch no
ledger, so these tests need an organization, a branch, a cost centre and a few
people — not a day of posted trading. The checkpoints that *do* post bring
their own heavier fixtures.

`Role.CASHIER` appears here as a first-class fixture, which it has not been
anywhere else in this system: until Phase 4 the role existed and granted
nothing in any module, so there was never anything to test it against.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import time

import pytest
from django.test import Client

from apps.accounting.models import CostCenter
from apps.accounting.services import create_cost_center
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.users.models import User

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def other_organization() -> Organization:
    return create_organization(code="RIVAL", name_ar="منافس", name_en="Rival")


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
def second_branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="KARRADA",
        name_ar="الكرادة",
        name_en="Karrada",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def hall_cost_center(organization: Organization) -> CostCenter:
    return create_cost_center(
        organization=organization, code="HALL", name_ar="الصالة", name_en="Hall"
    )


@pytest.fixture
def delivery_cost_center(organization: Organization) -> CostCenter:
    return create_cost_center(
        organization=organization, code="DELIVERY", name_ar="التوصيل", name_en="Delivery"
    )


def _user(username: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


@pytest.fixture
def manager(branch: Branch) -> User:
    """
    A branch manager, and the interesting case for menu scope.

    They hold no organization membership, and the menu is organization
    property — so this fixture proves that *reaching* an organization through a
    branch is enough to maintain its menu, which is what
    `ORGANIZATION_MASTER_DATA` means.
    """
    user = _user("branch-manager")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def cashier(branch: Branch) -> User:
    """Enters the day's takings and counts a drawer. Nothing else."""
    user = _user("cashier")
    grant_branch_access(user=user, branch=branch, role=Role.CASHIER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def accounting_manager(organization: Organization) -> User:
    user = _user("accounting-manager")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def outsider(other_organization: Organization) -> User:
    """Holds real authority — somewhere else. The 404-not-403 case."""
    user = _user("outsider")
    grant_organization_access(user=user, organization=other_organization, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def superuser() -> User:
    return User.objects.create_superuser(username="root", password=PASSWORD)


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login
