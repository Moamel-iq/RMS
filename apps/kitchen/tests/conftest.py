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

from apps.accounting.models import Account
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


def codes_of(error: Any) -> set[str]:
    """
    Every stable refusal code inside a `ValidationError`, however it was raised.

    Needed because `str(error)` shows the *message* for a field-keyed error and
    the code for a bare one, so asserting on the string quietly stops checking
    anything the moment a refusal grows a field. The branch order matters:
    `message` first, because an error carrying one has neither dict nor list.
    """
    if hasattr(error, "message"):
        return {error.code or ""}
    if hasattr(error, "error_dict"):
        return {
            code for errs in error.error_dict.values() for item in errs for code in codes_of(item)
        }
    if hasattr(error, "error_list"):
        return {code for item in error.error_list for code in codes_of(item)}
    return set()


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


@pytest.fixture
def other_branch(other_organization: Organization) -> Branch:
    return create_branch(
        organization=other_organization,
        code="RIVAL-1",
        name="فرع المنافس",
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
        organization=organization, code="FOOD", name="أغذية", depth=1
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
        name=code,
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
def barley(
    organization: Organization, item_category: ItemCategory, kilogram: UnitOfMeasure
) -> InventoryItem:
    """
    A second MASS item, so a substitution can be **comparable** with its plan.

    Rice and oil are deliberately in different dimensions, which makes them the
    right pair for the cross-dimension tests and the wrong pair for every test
    that needs a variance to be a number.
    """
    return _item(organization=organization, category=item_category, code="BARLEY", unit=kilogram)


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
        organization=other_organization, code="THEIRS", name="لهم", depth=1
    )
    return _item(
        organization=other_organization, category=category, code="THEIR-RICE", unit=kilogram
    )


@pytest.fixture
def sack(organization: Organization, rice: InventoryItem) -> PackageUnit:
    """A fixed 30 kg sack, so package entry has something to convert through."""
    package = PackageUnit.objects.create(organization=organization, code="SACK", name="كيس")
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
    package = PackageUnit.objects.create(organization=organization, code="DRUM", name="برميل")
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
        name="طبق تجريبي",
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
        name="الكرادة",
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
        name="حصة واحدة",
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
    name: str = "خلطة تجريبية",
) -> Recipe:
    """A non-stocked sub-recipe. A helper, because the cycle tests need several."""
    return create_recipe(
        organization=organization,
        code=code,
        name=name,
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
        name="وصفة مخزنية تجريبية",
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
        name="المخزن الرئيسي",
        warehouse_type=WarehouseType.PHYSICAL,
    )


@pytest.fixture
def second_store(branch: Branch) -> Warehouse:
    """A second warehouse in the same branch, so "which one" is a real question."""
    return Warehouse.objects.create(
        branch=branch,
        code="STORE-2",
        name="مخزن ثانٍ",
        warehouse_type=WarehouseType.PHYSICAL,
    )


@pytest.fixture
def rival_store(other_branch: Branch) -> Warehouse:
    """A warehouse in another organization entirely."""
    return Warehouse.objects.create(
        branch=other_branch,
        code="RIVAL-1",
        name="مخزن مؤسسة أخرى",
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
    control_account: Account | None = None,
) -> Any:
    """
    Put valued stock on a shelf, through the real ledger.

    A helper rather than a fixture because a costing test usually needs two or
    three postings at different unit costs, and a fixture cannot be asked for
    twice.

    `control_account` is optional because Task 3.3's costing tests genuinely do
    not need one — a cost card reads quantity and value and never asks which
    account holds them. Task 3.5 does need it: an outbound leaves through the
    account its position carries, and a position with no account contributes
    nothing to a journal that the produced goods would then unbalance.
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
                control_account=control_account,
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


# ---------------------------------------------------------------------------
# Task 3.4 - production drafting fixtures
# ---------------------------------------------------------------------------
#
# Shared here rather than per-module because the production suite is several
# files — contracts, scale consistency, services, races, security, surface — and
# each of them needs the same starting point: a producible recipe in effect at a
# branch, a warehouse in that branch, and a draft.
#
# `PRODUCTION_EFFECTIVE_FROM` is well before `PRODUCTION_DATE` so a test that
# wants a *gap* or a *replacement* has to arrange one, rather than beginning with
# an edge case it did not ask for.

PRODUCTION_EFFECTIVE_FROM = datetime.date(2026, 1, 1)
PRODUCTION_DATE = datetime.date(2026, 3, 1)


@pytest.fixture
def batch_recipe(
    organization: Organization,
    branch: Branch,
    cooked_rice: InventoryItem,
    kilogram: UnitOfMeasure,
    rice: InventoryItem,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> tuple[Recipe, RecipeVersion]:
    """
    A producible recipe — one with an `output_item` — active from January.

    `BATCH` with an output item because RCP-032 refuses to produce a portion
    recipe: producing one would create stock of an item that deliberately does
    not exist.
    """
    from apps.kitchen.services import create_recipe, set_recipe_branches

    recipe = create_recipe(
        organization=organization,
        code="PROD-DISH",
        name="طبخة للإنتاج",
        recipe_type=RecipeType.BATCH,
        output_item=cooked_rice,
        created_by=manager,
    )
    set_recipe_branches(recipe=recipe, branches=[branch])
    version = carry_to_active(
        build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
        effective_from=PRODUCTION_EFFECTIVE_FROM,
    )
    return recipe, version


@pytest.fixture
def substituted_recipe(
    organization: Organization,
    branch: Branch,
    cooked_rice: InventoryItem,
    kilogram: UnitOfMeasure,
    rice: InventoryItem,
    barley: InventoryItem,
    oil: InventoryItem,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> tuple[Recipe, RecipeVersion]:
    """
    A producible recipe whose rice line carries **two ranked** approved stand-ins.

    `barley` first, in the same dimension as rice, so a partial or complete
    substitution has a comparable quantity. `oil` second, in another dimension
    entirely, because RCP-022 approves items rather than conversions and a kitchen
    may legitimately approve a stand-in nothing converts to — which is the case
    the "not quantitatively comparable" statement exists for.

    The substitutes are added while the version is a **draft**, because
    `add_recipe_line_substitute` refuses anything else: an approval added after
    activation would be a change to a version somebody signed.
    """
    from apps.kitchen.services import (
        add_recipe_line_substitute,
        create_recipe,
        set_recipe_branches,
    )

    recipe = create_recipe(
        organization=organization,
        code="SUB-DISH",
        name="طبخة ببدائل",
        recipe_type=RecipeType.BATCH,
        output_item=cooked_rice,
        created_by=manager,
    )
    set_recipe_branches(recipe=recipe, branches=[branch])
    version = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
    line = version.lines.get()
    add_recipe_line_substitute(line=line, substitute_item=barley, reason="نقص في السوق")
    add_recipe_line_substitute(line=line, substitute_item=oil, reason="بديل بوحدة أخرى")
    carry_to_active(
        version,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
        effective_from=PRODUCTION_EFFECTIVE_FROM,
    )
    return recipe, RecipeVersion.objects.get(pk=version.pk)


@pytest.fixture
def substituted_draft(
    substituted_recipe: tuple[Recipe, RecipeVersion],
    branch: Branch,
    store: Warehouse,
    manager: User,
) -> Any:
    """A DRAFT of the substituted recipe — one requirement, two approvals to use."""
    from apps.kitchen.production import create_production_batch

    return create_production_batch(
        recipe=substituted_recipe[0],
        branch=branch,
        warehouse=store,
        planned_business_date=PRODUCTION_DATE,
        multiplier=Decimal("2"),
        actor=manager,
        idempotency_key="SUB-DRAFT-1",
    )


@pytest.fixture
def optional_recipe(
    organization: Organization,
    branch: Branch,
    cooked_rice: InventoryItem,
    kilogram: UnitOfMeasure,
    piece: UnitOfMeasure,
    rice: InventoryItem,
    box: InventoryItem,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> tuple[Recipe, RecipeVersion]:
    """
    A producible recipe with one required line and one **optional** one.

    Optionality is a fact about the *recipe*, and it reaches the requirement row
    frozen. A test that wanted to compare the two by flipping `is_optional` on a
    drafted requirement would be asking for a state migration 0011 refuses, so
    the difference is arranged here where it belongs.
    """
    from apps.kitchen.services import add_recipe_line, create_recipe, set_recipe_branches

    recipe = create_recipe(
        organization=organization,
        code="OPT-DISH",
        name="طبخة بسطر اختياري",
        recipe_type=RecipeType.BATCH,
        output_item=cooked_rice,
        created_by=manager,
    )
    set_recipe_branches(recipe=recipe, branches=[branch])
    version = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
    add_recipe_line(
        version=version,
        item=box,
        entered_quantity=Decimal("4"),
        entered_unit=piece,
        is_optional=True,
    )
    carry_to_active(
        version,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
        effective_from=PRODUCTION_EFFECTIVE_FROM,
    )
    return recipe, RecipeVersion.objects.get(pk=version.pk)


@pytest.fixture
def optional_draft(
    optional_recipe: tuple[Recipe, RecipeVersion],
    branch: Branch,
    store: Warehouse,
    manager: User,
) -> Any:
    from apps.kitchen.production import create_production_batch

    return create_production_batch(
        recipe=optional_recipe[0],
        branch=branch,
        warehouse=store,
        planned_business_date=PRODUCTION_DATE,
        multiplier=Decimal("1"),
        actor=manager,
        idempotency_key="OPT-DRAFT-1",
    )


@pytest.fixture
def demo_items(
    organization: Organization,
    item_category: ItemCategory,
    kilogram: UnitOfMeasure,
    litre: UnitOfMeasure,
    piece: UnitOfMeasure,
) -> dict[str, InventoryItem]:
    """The Phase 1 demo items the kitchen seed builds on, named as it names them."""
    items: dict[str, InventoryItem] = {}
    for code, unit, kind in (
        ("DEMO-RICE", kilogram, ItemType.RAW_MATERIAL),
        ("DEMO-OIL", litre, ItemType.RAW_MATERIAL),
        ("DEMO-MEAT", kilogram, ItemType.RAW_MATERIAL),
        ("DEMO-CHICKEN", piece, ItemType.RAW_MATERIAL),
        ("DEMO-CONTAINER", piece, ItemType.PACKAGING),
    ):
        items[code] = InventoryItem.objects.create(
            organization=organization,
            code=code,
            name=code,
            category=item_category,
            item_type=kind,
            base_unit=unit,
        )
    return items


@pytest.fixture
def demo_store(
    open_period: object,
    organization: Organization,
    branch: Branch,
    demo_items: dict[str, InventoryItem],
) -> Warehouse:
    """
    `DEMO-MAIN`, holding the three items the demo scenarios read.

    Named exactly as the inventory demo names it, because the kitchen seed looks
    that warehouse up by code — a fixture that invented its own name would leave
    the snapshot and production halves of the seed silently unexercised.
    """
    warehouse = Warehouse.objects.create(
        branch=branch,
        code="DEMO-MAIN",
        name="المخزن الرئيسي — تجريبي",
        warehouse_type=WarehouseType.PHYSICAL,
    )
    for code, quantity, unit_cost in (
        ("DEMO-RICE", "200", "1500"),
        ("DEMO-OIL", "50", "4000"),
        ("DEMO-CONTAINER", "500", "250"),
    ):
        post_receipt(
            organization=organization,
            warehouse=warehouse,
            item=demo_items[code],
            quantity=quantity,
            unit_cost=unit_cost,
            key=f"demo-{code}",
        )
    return warehouse


@pytest.fixture
def production_draft(
    batch_recipe: tuple[Recipe, RecipeVersion],
    branch: Branch,
    store: Warehouse,
    manager: User,
) -> Any:
    """
    One DRAFT batch at 2.5×, through the real service.

    Non-integral deliberately: a multiplier of 2 hides every rounding question a
    scaled requirement can raise, and half a pit is a real thing to cook.
    """
    from apps.kitchen.production import create_production_batch

    return create_production_batch(
        recipe=batch_recipe[0],
        branch=branch,
        warehouse=store,
        planned_business_date=PRODUCTION_DATE,
        multiplier=Decimal("2.5"),
        actor=manager,
        idempotency_key="PROD-DRAFT-1",
    )


# ---------------------------------------------------------------------------
# Task 3.5 — what a posting needs that a draft did not
# ---------------------------------------------------------------------------


@pytest.fixture
def kitchen_accounts(organization: Organization, open_period: Any) -> Account:
    """
    A chart, and one `INVENTORY_CONTROL` account mapped from the year's start.

    Task 3.4 never needed this: a draft resolves no account, and the costing
    fixtures posted their receipts with no control account at all — which is
    why every batch in those tests nets to zero by having no accounts rather
    than by having matching ones. Posting resolves the **output** item's
    account, so the mapping has to exist and has to cover the batch's own
    business date rather than today's.
    """
    from django.core.management import call_command

    from apps.accounting.models import INVENTORY_CONTROL, AccountRole
    from apps.accounting.services import create_account_mapping

    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)
    account = Account.objects.get(organization=organization, code="1-03-01-001")
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_CONTROL),
        account=account,
        effective_from=datetime.date(2026, 1, 1),
    )
    return account


@pytest.fixture
def separate_output_account(
    organization: Organization, kitchen_accounts: Account, cooked_rice: InventoryItem
) -> Account:
    """
    An **item-scoped** control account for the produced item only.

    This is the whole of the non-zero journal case: the ingredients leave the
    organization default and the output enters somewhere else, so the per-
    account nets stop cancelling and a journal has something to say. Without an
    override the two sides are the same account and the correct answer is
    silence.
    """
    from apps.accounting.models import INVENTORY_CONTROL
    from apps.accounting.services import create_account
    from apps.inventory.accounts import create_inventory_mapping

    account = create_account(
        organization=organization,
        code="1-03-01-009",
        name="مخزون الإنتاج التام",
    )
    create_inventory_mapping(
        organization=organization,
        role=INVENTORY_CONTROL,
        account=account,
        item=cooked_rice,
        effective_from=datetime.date(2026, 1, 1),
    )
    return account


@pytest.fixture
def posting_store(
    kitchen_accounts: Account,
    organization: Organization,
    store: Warehouse,
    rice: InventoryItem,
    oil: InventoryItem,
    box: InventoryItem,
) -> Warehouse:
    """
    `valued_store`, but with every position homed to a real control account.

    A separate fixture rather than a change to `valued_store`, because the two
    answer different questions. Costing asks what stock is worth and does not
    care where the value sits; posting asks the value to *move between*
    accounts, and a position with no account has nowhere for it to move from.

    Same figures as `valued_store` so a reader comparing the two sees only the
    account:

        100 KG @ 1,000 + 100 KG @ 2,000  ->  200 KG / 300,000  ->  1,500
    """
    for item, quantity, unit_cost, key in (
        (rice, "100", "1000", "posting-rice-1"),
        (rice, "100", "2000", "posting-rice-2"),
        (oil, "50", "4000", "posting-oil-1"),
        (box, "500", "250", "posting-box-1"),
    ):
        post_receipt(
            organization=organization,
            warehouse=store,
            item=item,
            quantity=quantity,
            unit_cost=unit_cost,
            key=key,
            control_account=kitchen_accounts,
        )
    return store
