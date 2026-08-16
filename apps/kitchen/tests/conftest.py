"""
Fixtures for the kitchen tests.

Deliberately small and hand-built rather than seeded from the demo dataset.
Task 3.1 touches no ledger, so these tests need an organization, a branch, a
few people and a handful of items — not eighty posted documents.

The `manager` fixture is the interesting one for scope. They hold no
organization membership, and the recipe master is organization property, so
that fixture is what proves *reaching* an organization through a branch is
enough to maintain its recipes — which is what `ORGANIZATION_MASTER_DATA`
means.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import Client

from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    ItemCategory,
    ItemPackageConversion,
    ItemType,
    PackageUnit,
)
from apps.kitchen.models import Recipe, RecipeType, RecipeVersion
from apps.kitchen.services import create_draft_recipe_version, create_recipe
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


@pytest.fixture
def units() -> None:
    """The standard units. Items need a base unit before they can exist."""
    call_command("seed_units", verbosity=0)


@pytest.fixture
def kilogram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="KG")


@pytest.fixture
def gram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="G")


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
def other_branch(other_organization: Organization) -> Branch:
    return create_branch(
        organization=other_organization,
        code="RIVAL-1",
        name_ar="فرع المنافس",
        name_en="Rival Branch",
        business_day_start_time=time(9, 0),
    )


def _user(username: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


@pytest.fixture
def manager(branch: Branch) -> User:
    """A branch manager: reaches the organization, and may maintain its recipes."""
    user = _user("branch-manager")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def keeper(branch: Branch) -> User:
    """Reads the recipe card and its quantities; never what they cost."""
    user = _user("storekeeper")
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def cashier(branch: Branch) -> User:
    """Handles takings, not recipes. Reaches the organization and holds nothing here."""
    user = _user("cashier")
    grant_branch_access(user=user, branch=branch, role=Role.CASHIER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def owner(organization: Organization) -> User:
    user = _user("owner")
    grant_organization_access(user=user, organization=organization, role=Role.OWNER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def rival_manager(other_branch: Branch) -> User:
    """A manager somewhere else entirely. Must reach nothing here."""
    user = _user("rival-manager")
    grant_branch_access(user=user, branch=other_branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def manager_client(manager: User) -> Client:
    return _client(manager)


@pytest.fixture
def keeper_client(keeper: User) -> Client:
    return _client(keeper)


@pytest.fixture
def cashier_client(cashier: User) -> Client:
    return _client(cashier)


@pytest.fixture
def rival_client(rival_manager: User) -> Client:
    return _client(rival_manager)


@pytest.fixture
def item_category(organization: Organization) -> ItemCategory:
    return ItemCategory.objects.create(
        organization=organization, code="FOOD", name_ar="أغذية", depth=1
    )


def _item(
    *,
    organization: Organization,
    category: ItemCategory,
    code: str,
    unit: UnitOfMeasure,
    item_type: str = ItemType.RAW_MATERIAL,
) -> InventoryItem:
    return InventoryItem.objects.create(
        organization=organization,
        code=code,
        name_ar=code,
        category=category,
        item_type=item_type,
        base_unit=unit,
    )


@pytest.fixture
def rice(
    organization: Organization, item_category: ItemCategory, kilogram: UnitOfMeasure
) -> InventoryItem:
    return _item(organization=organization, category=item_category, code="RICE", unit=kilogram)


@pytest.fixture
def oil(
    organization: Organization, item_category: ItemCategory, litre: UnitOfMeasure
) -> InventoryItem:
    return _item(organization=organization, category=item_category, code="OIL", unit=litre)


@pytest.fixture
def box(
    organization: Organization, item_category: ItemCategory, piece: UnitOfMeasure
) -> InventoryItem:
    return _item(
        organization=organization,
        category=item_category,
        code="BOX",
        unit=piece,
        item_type=ItemType.PACKAGING,
    )


@pytest.fixture
def cooked_rice(
    organization: Organization, item_category: ItemCategory, kilogram: UnitOfMeasure
) -> InventoryItem:
    """A producible item — the only kind a batch recipe may name as its output."""
    return _item(
        organization=organization,
        category=item_category,
        code="RICE-COOKED",
        unit=kilogram,
        item_type=ItemType.SEMI_FINISHED,
    )


@pytest.fixture
def rival_item(other_organization: Organization, kilogram: UnitOfMeasure) -> InventoryItem:
    category = ItemCategory.objects.create(
        organization=other_organization, code="THEIRS", name_ar="لهم", depth=1
    )
    return _item(
        organization=other_organization, category=category, code="THEIR-RICE", unit=kilogram
    )


@pytest.fixture
def sack(organization: Organization, rice: InventoryItem) -> PackageUnit:
    """A fixed 30 kg sack, so package entry has something to convert through."""
    package = PackageUnit.objects.create(organization=organization, code="SACK", name_ar="كيس")
    ItemPackageConversion.objects.create(
        organization=organization,
        item=rice,
        package_unit=package,
        conversion_type=ConversionType.FIXED,
        factor_to_base=Decimal("30"),
        effective_from="2020-01-01",
    )
    return package


@pytest.fixture
def drum(organization: Organization, oil: InventoryItem) -> PackageUnit:
    """A variable-weight container: posting one needs a measured quantity."""
    package = PackageUnit.objects.create(organization=organization, code="DRUM", name_ar="برميل")
    ItemPackageConversion.objects.create(
        organization=organization,
        item=oil,
        package_unit=package,
        conversion_type=ConversionType.VARIABLE,
        factor_to_base=Decimal("18"),
        effective_from="2020-01-01",
    )
    return package


@pytest.fixture
def recipe(organization: Organization, manager: User) -> Recipe:
    return create_recipe(
        organization=organization,
        code="MANDI-1",
        name_ar="طبق تجريبي",
        recipe_type=RecipeType.PORTION,
        created_by=manager,
    )


@pytest.fixture
def draft(recipe: Recipe, kilogram: UnitOfMeasure, manager: User) -> RecipeVersion:
    return create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("10"),
        output_unit=kilogram,
        created_by=manager,
    )
