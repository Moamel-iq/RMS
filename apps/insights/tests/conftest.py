"""
Fixtures for the insights tests.

Deliberately builds its world through the real services rather than through
factories that write rows directly: the detector reads the authoritative
consumption engines, and a fixture that bypassed posting would prove the
arithmetic against data the engines would never actually produce.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command

from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_organization_access,
)
from apps.users.models import User

PASSWORD = "pw-not-real-1234"
TEST_YEAR = 2026
WINDOW_START = datetime.date(TEST_YEAR, 3, 1)
WINDOW_END = datetime.date(TEST_YEAR, 4, 1)


@pytest.fixture
def units() -> None:
    call_command("seed_units", verbosity=0)


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
        code="MAIN",
        name="الفرع الرئيسي",
        business_day_start_time=datetime.time(9, 0),
    )


def _user(username: str, organization: Organization, role: str) -> User:
    user = User.objects.create_user(username=username, password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=role)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def owner(organization: Organization) -> User:
    """Holds every insights permission, including the threshold."""
    return _user("owner", organization, Role.OWNER)


@pytest.fixture
def manager(organization: Organization) -> User:
    """Reads and decides, but may not move a threshold."""
    return _user("manager", organization, Role.MANAGER)


@pytest.fixture
def accountant(organization: Organization) -> User:
    """Reads only — the case for testing that management is a separate gate."""
    return _user("accountant", organization, Role.ACCOUNTANT)


@pytest.fixture
def storekeeper(organization: Organization) -> User:
    """Holds no insights permission at all."""
    return _user("storekeeper", organization, Role.STOREKEEPER)


@pytest.fixture
def rival_owner(other_organization: Organization) -> User:
    """An owner of a different organization: the isolation case."""
    return _user("rival-owner", other_organization, Role.OWNER)


@pytest.fixture
def client_for() -> Any:
    from django.test import Client

    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login


@pytest.fixture
def zero_ratio_evidence() -> dict[str, Any]:
    """A minimal, float-free evidence payload for kernel-level tests."""
    return {
        "schema": "test/1",
        "measures": {
            "actual_consumption": "0.000",
            "theoretical_consumption": "100.000",
            "item_issue_ratio": "0",
            "threshold": "0.05",
        },
        "counts": {"movements_at_warehouse": 3},
    }


@pytest.fixture
def decimal_threshold() -> Decimal:
    return Decimal("0.05")
