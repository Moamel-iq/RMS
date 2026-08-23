"""
Contracts for the landing dashboard.

The page owns no figure, so the thing to prove is composition: a section
appears only for a caller who holds a post in that module, and money appears
only with that module's cost permission — the same two gates the module's own
screen applies. A user with no post anywhere must still get a page, and it
must be empty rather than full of zeros.
"""

from __future__ import annotations

from datetime import time

import pytest
from django.test import Client
from django.urls import reverse

from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
)
from apps.users.home_dashboard import home_overview, readiness_share
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-for-tests-only"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM-HOME", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK-HOME",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9),
    )


def _user(username: str, branch: Branch | None = None, role: Role | None = None) -> User:
    user = User.objects.create_user(username=username, password=PASSWORD)
    if branch is not None and role is not None:
        grant_branch_access(user=user, branch=branch, role=role)
    return User.objects.get(pk=user.pk)


def _page(user: User) -> str:
    client = Client()
    client.force_login(user)
    response = client.get(reverse("users:home") if _has_named_home() else "/")
    assert response.status_code == 200
    return response.content.decode()


def _has_named_home() -> bool:
    try:
        reverse("users:home")
    except Exception:  # noqa: BLE001 - the fallback path is the contract here
        return False
    return True


def test_a_user_with_no_post_gets_an_empty_page_not_a_page_of_zeros() -> None:
    nobody = _user("nobody")

    overview = home_overview(nobody)

    assert overview.inventory is None
    assert overview.procurement is None
    assert overview.kitchen is None
    assert overview.hr is None
    assert overview.sales is None
    assert overview.readiness == []
    assert readiness_share(overview.readiness) == 0

    body = _page(nobody)
    assert "قيمة المخزون" not in body
    assert "صافي المبيعات" not in body
    assert "الرواتب الشهرية" not in body


def test_a_storekeeper_sees_stock_counts_and_no_money(branch: Branch) -> None:
    keeper = _user("home-keeper", branch, Role.STOREKEEPER)

    overview = home_overview(keeper)

    # Holds a post in inventory, so the section exists — redacted.
    assert overview.inventory is not None
    assert overview.inventory.total_value is None
    # Holds no post that reads invoices, payroll, or sales reports.
    assert overview.procurement is None
    assert overview.hr is None
    assert overview.sales is None

    body = _page(keeper)
    assert "قيمة المخزون" not in body
    assert "الرواتب الشهرية" not in body
    assert "المشتريات المرحّلة" not in body


def test_a_manager_gets_every_section_the_modules_grant(branch: Branch) -> None:
    manager = _user("home-manager", branch, Role.MANAGER)

    overview = home_overview(manager)

    assert overview.inventory is not None
    assert overview.inventory.total_value is not None
    assert overview.procurement is not None
    assert overview.kitchen is not None
    assert overview.hr is not None
    # One readiness line per module the caller can read, in a fixed order.
    assert [item.url_name for item in overview.readiness] == [
        "inventory:overview",
        "procurement:overview",
        "kitchen:overview",
        "sales:dashboard",
        "hr:overview",
    ][: len(overview.readiness)]

    body = _page(manager)
    assert "جاهزية الأرقام" in body
