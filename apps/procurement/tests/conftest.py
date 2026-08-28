"""
Fixtures for the procurement tests.

Deliberately small and hand-built rather than seeded from the demo dataset.
Supplier master data touches no ledger, so these tests need an organization, a
branch and a few people — not eighty posted documents. The demo seed is ten
seconds; this is a few milliseconds, and Task 2.8 is where procurement first
needs real stock behind it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import time

import pytest
from django.test import Client

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
    return create_organization(code="KM", name="خان مندي")


@pytest.fixture
def other_organization() -> Organization:
    return create_organization(code="RIVAL", name="منافس")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK",
        name="البنوك",
        business_day_start_time=time(9, 0),
    )


def _user(username: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


@pytest.fixture
def manager(branch: Branch) -> User:
    """
    A branch manager, and the interesting case for supplier scope.

    They hold no organization membership, and the supplier master is
    organization property — so this fixture is what proves that *reaching* an
    organization through a branch is enough to maintain its suppliers, which is
    what `ORGANIZATION_MASTER_DATA` means.
    """
    user = _user("branch-manager")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def keeper(branch: Branch) -> User:
    """Receives goods, and must never see what they cost."""
    user = _user("storekeeper")
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def cashier(branch: Branch) -> User:
    user = _user("cashier")
    grant_branch_access(user=user, branch=branch, role=Role.CASHIER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def accounting_manager(organization: Organization) -> User:
    user = _user("accounting-manager")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
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
