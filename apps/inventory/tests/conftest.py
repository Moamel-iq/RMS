"""Shared fixtures for the inventory master-data tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, time

import pytest
from django.core.management import call_command
from django.test import Client

from apps.inventory.models import ItemCategory, ItemType, PackageUnit, Warehouse
from apps.inventory.services import (
    create_item,
    create_item_category,
    create_package_unit,
    create_warehouse,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

PASSWORD = "pw-not-real-1234"
TODAY = date(2026, 3, 15)


@pytest.fixture
def units() -> None:
    """The standard units. Items need a base unit before they can exist."""
    call_command("seed_units", verbosity=0)


@pytest.fixture
def kilogram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="KG")


@pytest.fixture
def litre(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="L")


@pytest.fixture
def piece(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="PIECE")


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
def other_branch(other_organization: Organization) -> Branch:
    return create_branch(
        organization=other_organization,
        code="RIVALBR",
        name_ar="فرع المنافس",
        name_en="Rival Branch",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def category(organization: Organization) -> ItemCategory:
    return create_item_category(organization=organization, code="FOOD", name_ar="أغذية")


@pytest.fixture
def leaf_category(organization: Organization, category: ItemCategory) -> ItemCategory:
    return create_item_category(
        organization=organization, code="MEAT", name_ar="لحوم", parent=category
    )


@pytest.fixture
def other_category(other_organization: Organization) -> ItemCategory:
    return create_item_category(organization=other_organization, code="FOOD", name_ar="أغذية")


@pytest.fixture
def carton(organization: Organization) -> PackageUnit:
    return create_package_unit(organization=organization, code="CARTON", name_ar="كرتون")


@pytest.fixture
def sack(organization: Organization) -> PackageUnit:
    return create_package_unit(organization=organization, code="SACK", name_ar="كيس")


@pytest.fixture
def rice(
    organization: Organization, leaf_category: ItemCategory, kilogram: UnitOfMeasure
) -> object:
    return create_item(
        organization=organization,
        code="RICE-272",
        name_ar="رز ٢٧٢",
        category=leaf_category,
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )


@pytest.fixture
def main_store(branch: Branch) -> Warehouse:
    return create_warehouse(branch=branch, code="MAIN", name_ar="المخزن الرئيسي")


@pytest.fixture
def kitchen_store(branch: Branch) -> Warehouse:
    return create_warehouse(branch=branch, code="KITCHEN", name_ar="مخزن المطبخ")


@pytest.fixture
def other_warehouse(other_branch: Branch) -> Warehouse:
    return create_warehouse(branch=other_branch, code="MAIN", name_ar="مخزن المنافس")


def _user(username: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


@pytest.fixture
def manager(branch: Branch) -> User:
    user = _user("branch-manager")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def storekeeper(branch: Branch) -> User:
    """Moves goods, never sees cost."""
    user = _user("storekeeper")
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def accounting_manager(organization: Organization) -> User:
    user = _user("accounting-manager")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def rival_manager(other_branch: Branch) -> User:
    user = _user("rival-manager")
    grant_branch_access(user=user, branch=other_branch, role=Role.MANAGER)
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
