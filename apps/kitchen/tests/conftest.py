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

import datetime
from datetime import time
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    ItemCategory,
    ItemPackageConversion,
    ItemType,
    PackageUnit,
    Warehouse,
    WarehouseType,
)
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    approve_recipe_version,
    record_recipe_version_review,
    submit_recipe_version,
)
from apps.kitchen.models import (
    ApprovalEvidenceKind,
    Recipe,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
    RecipeVersion,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    add_recipe_step,
    create_draft_recipe_version,
    create_recipe,
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


# ---------------------------------------------------------------------------
# The lifecycle's own fixtures
# ---------------------------------------------------------------------------
#
# Four different people, because `KM-RCP-004`'s control is four signatures and
# a fixture that reused one user would pass every maker-checker test by
# accident. The names say which column of the form each one signs.


@pytest.fixture
def cook(branch: Branch) -> User:
    """Signs the kitchen review. Reaches the organization through the branch."""
    user = _user("kitchen-reviewer")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def accountant(branch: Branch) -> User:
    """Signs the costing-evidence review, and may not edit the recipe."""
    user = _user("accountant")
    grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTANT)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def approver(branch: Branch) -> User:
    """The final signatory. Never the author, never the submitter, never a reviewer."""
    user = _user("approving-manager")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def approver_client(approver: User) -> Client:
    return _client(approver)


@pytest.fixture
def keeper_reviewer(keeper: User) -> User:
    """The storekeeper, named for what they do to a version rather than to stock."""
    return keeper


@pytest.fixture
def second_branch(organization: Organization) -> Branch:
    """A second branch of the same organization, for per-branch resolution."""
    return create_branch(
        organization=organization,
        code="KARRADA",
        name_ar="الكرادة",
        name_en="Karrada",
        business_day_start_time=time(9, 0),
    )


def build_complete_draft(
    *,
    recipe: Recipe,
    unit: UnitOfMeasure,
    item: InventoryItem,
    author: User,
    output_unit: UnitOfMeasure | None = None,
) -> RecipeVersion:
    """
    A draft with everything submission demands: a line, a step, a serving and
    an overview.

    A helper rather than a fixture because several tests need two of them on
    one recipe, and a fixture cannot be asked for twice.
    """
    version = create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("10"),
        output_unit=output_unit or unit,
        instructions="نظرة عامة على الطريقة.",
        created_by=author,
    )
    add_recipe_line(
        version=version,
        item=item,
        entered_quantity=Decimal("4"),
        entered_unit=unit,
    )
    add_recipe_step(version=version, instruction_ar="خطوة أولى.")
    add_recipe_serving(
        version=version,
        code="ONE",
        name_ar="حصة واحدة",
        serving_quantity=Decimal("1"),
        serving_unit=output_unit or unit,
        is_primary=True,
    )
    return RecipeVersion.objects.get(pk=version.pk)


@pytest.fixture
def complete_draft(
    recipe: Recipe, kilogram: UnitOfMeasure, rice: InventoryItem, manager: User
) -> RecipeVersion:
    """A draft that would pass submission, ready for the lifecycle tests."""
    return build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)


def carry_to_approved(
    version: RecipeVersion,
    *,
    submitter: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
    reference: str = "KM-RCP-004/2026/07",
    evidence_kind: str = ApprovalEvidenceKind.SIGNED_FORM,
) -> RecipeVersion:
    """Submit, gather the three reviews, and take the final approval."""
    submit_recipe_version(version=version, actor=submitter)
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.KITCHEN,
        reviewer=cook,
        decision=RecipeReviewDecision.APPROVED,
    )
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.STOREKEEPER,
        reviewer=keeper,
        decision=RecipeReviewDecision.APPROVED,
    )
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.ACCOUNTING,
        reviewer=accountant,
        decision=RecipeReviewDecision.APPROVED,
        evidence_reference=reference,
        evidence_kind=evidence_kind,
    )
    return approve_recipe_version(
        version=version,
        actor=approver,
        approval_reference=reference,
        approval_evidence_kind=evidence_kind,
    )


@pytest.fixture
def approved_version(
    complete_draft: RecipeVersion,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> RecipeVersion:
    """A version that has cleared the whole control and is not yet in effect."""
    return carry_to_approved(
        complete_draft,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )


@pytest.fixture
def active_version(
    approved_version: RecipeVersion, approver: User, branch: Branch
) -> RecipeVersion:
    """An approved version put into effect organization-wide from 1 July 2026."""
    return activate_recipe_version(
        version=approved_version,
        actor=approver,
        effective_from=datetime.date(2026, 7, 1),
    )


# ---------------------------------------------------------------------------
# The nested-recipe graph's fixtures
# ---------------------------------------------------------------------------
#
# Two shapes and the difference between them, because RCP-070's whole point is
# that they are mutually exclusive:
#
#   `blend`   — a PORTION recipe, so no `output_item`, so **non-stocked**. It may
#               be a component and may never be a line.
#   `stocked` — a BATCH recipe producing `RICE-COOKED`, so **stocked**. It may be
#               a line and may never be a component.


def make_child_recipe(
    *,
    organization: Organization,
    code: str,
    author: User,
    name_ar: str = "خلطة تجريبية",
) -> Recipe:
    """A non-stocked sub-recipe. A helper, because the cycle tests need several."""
    return create_recipe(
        organization=organization,
        code=code,
        name_ar=name_ar,
        recipe_type=RecipeType.PORTION,
        created_by=author,
    )


def carry_to_active(
    version: RecipeVersion,
    *,
    submitter: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
    effective_from: datetime.date = datetime.date(2026, 1, 1),
    effective_to: datetime.date | None = None,
    branches: list[Branch] | None = None,
    reference: str = "KM-RCP-004/2026/07",
    evidence_kind: str = ApprovalEvidenceKind.SIGNED_FORM,
) -> RecipeVersion:
    """Everything `carry_to_approved` does, and then put it into effect."""
    carry_to_approved(
        version,
        submitter=submitter,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
        reference=reference,
        evidence_kind=evidence_kind,
    )
    return activate_recipe_version(
        version=version,
        actor=approver,
        effective_from=effective_from,
        effective_to=effective_to,
        branches=branches,
    )


@pytest.fixture
def blend(organization: Organization, manager: User) -> Recipe:
    """A non-stocked sub-recipe: no output item, so only ever a component."""
    return make_child_recipe(organization=organization, code="BLEND-1", author=manager)


@pytest.fixture
def blend_draft(
    blend: Recipe, kilogram: UnitOfMeasure, rice: InventoryItem, manager: User
) -> RecipeVersion:
    return build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)


@pytest.fixture
def blend_approved(
    blend_draft: RecipeVersion,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> RecipeVersion:
    """A child version that has cleared the control but is effective nowhere."""
    return carry_to_approved(
        blend_draft,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )


@pytest.fixture
def blend_active(blend_approved: RecipeVersion, approver: User, branch: Branch) -> RecipeVersion:
    """
    A child in effect organization-wide from 1 January 2026, open-ended.

    Six months earlier than `active_version` and with no end, so a parent
    activated on 1 July 2026 is covered at both ends and the coverage tests have
    to *arrange* a gap rather than start with one.
    """
    return activate_recipe_version(
        version=blend_approved,
        actor=approver,
        effective_from=datetime.date(2026, 1, 1),
    )


@pytest.fixture
def stocked_recipe(organization: Organization, manager: User, cooked_rice: InventoryItem) -> Recipe:
    """A batch recipe that produces stock — the shape a component may never take."""
    return create_recipe(
        organization=organization,
        code="STOCKED-1",
        name_ar="وصفة مخزنية تجريبية",
        recipe_type=RecipeType.BATCH,
        output_item=cooked_rice,
        created_by=manager,
    )


@pytest.fixture
def stocked_active(
    stocked_recipe: Recipe,
    kilogram: UnitOfMeasure,
    rice: InventoryItem,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> RecipeVersion:
    """An ACTIVE version of the stocked recipe, eligible in every way but shape."""
    draft = build_complete_draft(recipe=stocked_recipe, unit=kilogram, item=rice, author=manager)
    return carry_to_active(
        draft,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )


# ---------------------------------------------------------------------------
# Task 3.3 - costing fixtures
# ---------------------------------------------------------------------------
#
# Costing is the first kitchen concern that needs a **valued** ledger, so these
# fixtures post real stock through `post_stock_entry`. Not a shortcut through
# `StockBalance.objects.create`: the whole point of the costing contract is that
# the number comes from the movements the kernel replayed, and a fixture that
# wrote a balance directly would test a projection nobody produced.


@pytest.fixture
def open_period(organization: Organization) -> Any:
    """
    Stock postings need an OPEN accounting period for their business date.

    `open_fiscal_year` writes all twelve months, so this uses it rather than
    hand-building one period: a fixture that constructed periods its own way
    could pass against a year no deployment would ever have.
    """
    from apps.accounting.models import AccountingPeriod
    from apps.accounting.services import open_fiscal_year

    today = timezone.localdate()
    open_fiscal_year(organization=organization, year=today.year)
    return AccountingPeriod.objects.get(
        fiscal_year__organization=organization,
        start_date__lte=today,
        end_date__gte=today,
    )


@pytest.fixture
def other_open_period(other_organization: Organization) -> Any:
    from apps.accounting.models import AccountingPeriod
    from apps.accounting.services import open_fiscal_year

    today = timezone.localdate()
    open_fiscal_year(organization=other_organization, year=today.year)
    return AccountingPeriod.objects.get(
        fiscal_year__organization=other_organization,
        start_date__lte=today,
        end_date__gte=today,
    )


@pytest.fixture
def store(branch: Branch) -> Warehouse:
    """The warehouse recipes are costed against. Never a default - an input."""
    return Warehouse.objects.create(
        branch=branch,
        code="STORE-1",
        name_ar="المخزن الرئيسي",
        warehouse_type=WarehouseType.PHYSICAL,
    )


@pytest.fixture
def second_store(branch: Branch) -> Warehouse:
    """A second warehouse in the same branch, so "which one" is a real question."""
    return Warehouse.objects.create(
        branch=branch,
        code="STORE-2",
        name_ar="مخزن ثانٍ",
        warehouse_type=WarehouseType.PHYSICAL,
    )


@pytest.fixture
def rival_store(other_branch: Branch) -> Warehouse:
    """A warehouse in another organization entirely."""
    return Warehouse.objects.create(
        branch=other_branch,
        code="RIVAL-1",
        name_ar="مخزن مؤسسة أخرى",
        warehouse_type=WarehouseType.PHYSICAL,
    )


def post_receipt(
    *,
    organization: Organization,
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    unit_cost: str,
    key: str,
    lot: Any = None,
) -> Any:
    """
    Put valued stock on a shelf, through the real ledger.

    A helper rather than a fixture because a costing test usually needs two or
    three postings at different unit costs, and a fixture cannot be asked for
    twice.
    """
    from apps.inventory.ledger import MovementInput, post_stock_entry
    from apps.inventory.models import MovementType

    return post_stock_entry(
        organization=organization,
        effects=[
            MovementInput(
                warehouse=warehouse,
                item=item,
                movement_type=MovementType.RECEIPT,
                quantity=Decimal(quantity),
                unit_cost=Decimal(unit_cost),
                effect_key="line:1",
                lot=lot,
            )
        ],
        idempotency_key=key,
    )


def post_issue(
    *,
    organization: Organization,
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    key: str,
    lot: Any = None,
) -> Any:
    """Take stock off the shelf, so a position can legitimately reach zero."""
    from apps.inventory.ledger import MovementInput, post_stock_entry
    from apps.inventory.models import MovementType

    return post_stock_entry(
        organization=organization,
        effects=[
            MovementInput(
                warehouse=warehouse,
                item=item,
                movement_type=MovementType.ISSUE,
                quantity=Decimal(quantity),
                effect_key="line:1",
                lot=lot,
            )
        ],
        idempotency_key=key,
    )


@pytest.fixture
def valued_store(
    open_period: Any,
    organization: Organization,
    store: Warehouse,
    rice: InventoryItem,
    oil: InventoryItem,
    box: InventoryItem,
) -> Warehouse:
    """
    A warehouse holding rice, oil and boxes at known averages.

    Rice arrives in **two receipts at different unit costs**, so every test
    that reads its average is reading a figure that only comes out right if the
    lots are summed and divided rather than averaged pairwise:

        100 KG @ 1,000 + 100 KG @ 2,000  ->  200 KG / 300,000  ->  1,500
    """
    post_receipt(
        organization=organization,
        warehouse=store,
        item=rice,
        quantity="100",
        unit_cost="1000",
        key="fixture-rice-1",
    )
    post_receipt(
        organization=organization,
        warehouse=store,
        item=rice,
        quantity="100",
        unit_cost="2000",
        key="fixture-rice-2",
    )
    post_receipt(
        organization=organization,
        warehouse=store,
        item=oil,
        quantity="50",
        unit_cost="4000",
        key="fixture-oil-1",
    )
    post_receipt(
        organization=organization,
        warehouse=store,
        item=box,
        quantity="500",
        unit_cost="250",
        key="fixture-box-1",
    )
    return store


@pytest.fixture
def costable_version(
    complete_draft: RecipeVersion,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> RecipeVersion:
    """An `ACTIVE` version with one rice line — the smallest authoritative card."""
    approved = carry_to_approved(
        complete_draft,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )
    activate_recipe_version(
        version=approved, actor=approver, effective_from=datetime.date(2026, 1, 1)
    )
    return RecipeVersion.objects.get(pk=approved.pk)


@pytest.fixture
def cost_reader(branch: Branch) -> User:
    """
    Holds `view_recipe_cost` through the approved role map, and nothing extra.

    `ACCOUNTANT` rather than `MANAGER`: the accountant reads cost and cannot
    edit a recipe, which is the pair of facts every security test here needs.
    """
    user = _user("cost-reader")
    grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTANT)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def cost_reader_client(cost_reader: User) -> Client:
    return _client(cost_reader)
