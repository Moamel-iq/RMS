"""
The inventory demo scenario: a small, complete, *real* dataset.

Development tooling, not application code. Nothing here runs in production —
the only caller is `seed_inventory_demo`, which refuses to start unless
`settings.DEBUG` is true. See `docs/development/demo-data-policy.md` for the
convention this implements.

## The one rule that makes this worth having

**Every posted event goes through the same service the API and the UI call.**
Not one `StockLedgerEntry.objects.create`, not one hand-written `StockBalance`,
not one `JournalEntry` assembled here. The dataset's entire value is that it is
indistinguishable from data somebody typed: the balances came out of the
valuation kernel, the journals came out of the posting rules, the audit trail
records a real actor, and `verify_inventory_accounting` reconciles it because
there is genuinely nothing to reconcile away.

A directly-written balance would demonstrate the screens rendering and prove
nothing at all — and it would break the reconciliation screen, which is itself
one of the things being reviewed.

## Idempotency without owning the identity

Posted documents derive their own source identity and idempotency key from
`public_id`, which the services generate. This module therefore does **not**
mint those; it makes the *documents* findable instead. Every demo document
carries `{NAMESPACE}/{slug}` in its evidence reference, and each step looks for
that reference before creating anything. A second run finds all of them and
reports `reused`, so no second document, movement, or journal appears — and the
real source identities stay real, because the services still derive them.

## Why the scenario is shaped the way it is

The quantities are chosen so the whole sequence is valid in one pass and stays
valid on re-run: nothing goes negative, the reversible receipt is posted last
into stock nothing else touches, the partial transfer keeps a non-zero
remainder so the in-transit screen has rows, and the shortage transfer closes
to exactly zero so the dispatch/receipt/shortage identity is visible.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INTER_BRANCH_CLEARING,
    INVENTORY_ADJUSTMENT,
    INVENTORY_CONSUMPTION,
    INVENTORY_CONTROL,
    INVENTORY_COUNT_VARIANCE,
    INVENTORY_IN_TRANSIT,
    INVENTORY_OPENING_EQUITY,
    INVENTORY_SHORTAGE_LOSS,
    INVENTORY_WASTE_EXPENSE,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
    OrganizationAccountMapping,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.core.context import audit_context
from apps.inventory.accounts import create_inventory_mapping
from apps.inventory.adjustments import (
    AdjustmentLineInput,
    add_adjustment_line,
    create_adjustment,
    post_adjustment,
)
from apps.inventory.counts import (
    CountEntry,
    add_unexpected_line,
    approve_count,
    cancel_count,
    create_count,
    record_counts,
    start_count,
    submit_count,
)
from apps.inventory.imports import apply_batch, create_batch, validate_batch
from apps.inventory.locations import create_location, move_between_locations, put_away
from apps.inventory.models import (
    AdjustmentLineKind,
    BranchItemSetting,
    ConversionType,
    ImportBatch,
    ImportKind,
    InventoryAccountMapping,
    InventoryAdjustmentDocument,
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    InventoryMovementDocument,
    ItemCategory,
    ItemPackageConversion,
    ItemType,
    OpeningStockDocument,
    PackageUnit,
    StockCount,
    StockCountLine,
    StockLocation,
    StockLocationBalance,
    StockMovement,
    StockTransfer,
    StockTransferStatus,
    Warehouse,
    WarehouseType,
)
from apps.inventory.opening import (
    OpeningLineInput,
    add_opening_line,
    create_opening_document,
    ensure_opening_lot,
    post_opening_document,
    submit_opening_document,
)
from apps.inventory.operations import (
    DocumentLineInput,
    add_line,
    create_document,
    delete_document,
    post_document,
    reverse_document,
)
from apps.inventory.permissions import VIEW_STOCK, VIEW_VALUATION
from apps.inventory.services import (
    create_item,
    create_item_category,
    create_item_conversion,
    create_package_unit,
    create_warehouse,
    ensure_in_transit_warehouse,
    set_branch_item_setting,
)
from apps.inventory.transfers import (
    ReceiptLineInput,
    TransferLineInput,
    add_receipt_line,
    add_transfer_line,
    create_receipt,
    create_shortage,
    create_transfer,
    delete_transfer,
    dispatch_transfer,
    post_receipt,
    post_shortage,
)
from apps.organizations.models import (
    Branch,
    BranchMembership,
    Organization,
    OrganizationMembership,
    Role,
    RoleDefinition,
)
from apps.organizations.services import (
    create_branch,
    create_organization,
    create_role_definition,
    grant_branch_access,
    grant_organization_access,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

#: The namespace every record this module creates is findable by. Ends in a
#: version: a later scenario takes V2 rather than mutating what V1 posted,
#: because posted ledger history is append-only and a demo posting is history.
NAMESPACE = "DEMO-INVENTORY-V1"

DEMO_ORGANIZATION_CODE = "DEMO-KHAN-MANDI"
SOURCE_BRANCH_CODE = "DEMO-BUNOOK"
DESTINATION_BRANCH_CODE = "DEMO-SECOND"

#: The conductor of the physical count. Maker-checker needs two people, and
#: `approved_by != conducted_by` is a database constraint, not a UI courtesy.
#: Created with an unusable password: this is a data actor, not an account
#: anybody signs in with. `manage.py changepassword` if you want to look at
#: the blind count sheet through their eyes.
COUNT_CONDUCTOR_USERNAME = "demo-storekeeper"
#: The DEMO custom role and the data actor who holds it (ADR-034).
REPORTS_ROLE_CODE = "demo-reports-reader"
REPORTS_READER_USERNAME = "demo-reports-reader"

BAGHDAD = ZoneInfo("Asia/Baghdad")


class DemoSelectionError(Exception):
    """
    An argument named something that does not exist, or names too many things.

    Raised rather than guessed at. A seed that quietly picks the first of three
    organizations is a seed that writes into the wrong one exactly once, and
    posted stock and accounting effects are append-only.
    """


def reference(slug: str) -> str:
    """The evidence reference that makes a demo document findable."""
    return f"{NAMESPACE}/{slug}"


# ---------------------------------------------------------------------------
# What gets created
# ---------------------------------------------------------------------------

#: code, Arabic, parent code. Parents first: a child needs its parent to exist.
CATEGORIES: list[tuple[str, str, str | None]] = [
    ("DEMO-FOOD", "أغذية تجريبية", None),
    ("DEMO-GRAINS", "حبوب تجريبية", "DEMO-FOOD"),
    ("DEMO-MEAT-POULTRY", "لحوم ودواجن تجريبية", "DEMO-FOOD"),
    ("DEMO-OILS", "زيوت تجريبية", "DEMO-FOOD"),
    ("DEMO-PACKAGING", "مواد تغليف تجريبية", None),
]

#: Only the packages this scenario actually posts with. A `PackageUnit` still
#: carries no universal factor — one carton of chicken and one carton of oil
#: hold different quantities, and the factor lives on the conversion.
PACKAGE_UNITS: list[tuple[str, str]] = [
    ("SACK", "كيس"),
    ("CARTON", "كرتون"),
    ("CONTAINER", "حاوية"),
]

#: code, Arabic, category, item type, base unit, tracks_lots, tracks_expiry
ITEMS: list[tuple[str, str, str, str, str, bool, bool]] = [
    ("DEMO-RICE", "رز تجريبي", "DEMO-GRAINS", ItemType.RAW_MATERIAL, "KG", False, False),
    (
        "DEMO-CHICKEN",
        "دجاج تجريبي",
        "DEMO-MEAT-POULTRY",
        ItemType.RAW_MATERIAL,
        "PIECE",
        True,
        True,
    ),
    ("DEMO-OIL", "زيت طبخ تجريبي", "DEMO-OILS", ItemType.RAW_MATERIAL, "L", False, False),
    # Lots without expiry: a lot code is how the variable-weight containers stay
    # distinguishable, and §I of the brief asks for a meat lot. Meat is not
    # date-controlled here, so it carries no expiry — the model does not
    # require one, and inventing a date would be inventing a business rule.
    ("DEMO-MEAT", "لحم تجريبي", "DEMO-MEAT-POULTRY", ItemType.RAW_MATERIAL, "KG", True, False),
    (
        "DEMO-CONTAINER",
        "علبة تغليف تجريبية",
        "DEMO-PACKAGING",
        ItemType.PACKAGING,
        "PIECE",
        False,
        False,
    ),
]

#: item, package unit, factor to base, conversion type
CONVERSIONS: list[tuple[str, str, str, str]] = [
    ("DEMO-RICE", "SACK", "30.000000000000", ConversionType.FIXED),
    ("DEMO-CHICKEN", "CARTON", "10.000000000000", ConversionType.FIXED),
    ("DEMO-OIL", "CARTON", "20.000000000000", ConversionType.FIXED),
    # The planning factor only. A container is whatever it weighed, and posting
    # demands an explicit measured quantity — which is what stops one container
    # silently becoming 18.000 kg forever.
    ("DEMO-MEAT", "CONTAINER", "18.000000000000", ConversionType.VARIABLE),
    ("DEMO-CONTAINER", "CARTON", "500.000000000000", ConversionType.FIXED),
]

#: code, Arabic, type. The in-transit warehouses are not here: they are
#: system-controlled and come from `ensure_in_transit_warehouse`.
SOURCE_WAREHOUSES: list[tuple[str, str, str]] = [
    ("DEMO-MAIN", "المخزن الرئيسي — تجريبي", WarehouseType.PHYSICAL),
    ("DEMO-KITCHEN", "مخزن المطبخ — تجريبي", WarehouseType.PHYSICAL),
    ("DEMO-WIP", "الإنتاج تحت التشغيل — تجريبي", WarehouseType.PRODUCTION_WIP),
]

DESTINATION_WAREHOUSES: list[tuple[str, str, str]] = [
    ("DEMO-DEST-MAIN", "المخزن الرئيسي للفرع الثاني — تجريبي", WarehouseType.PHYSICAL),
]

#: role code, account code. Every role the implemented operations resolve.
ACCOUNT_MAPPINGS: list[tuple[str, str]] = [
    (INVENTORY_CONTROL, "1-03-01-001"),
    (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
    (INVENTORY_CONSUMPTION, "5-01-02-001"),
    (INVENTORY_OPENING_EQUITY, "3-02-01-001"),
    (INVENTORY_IN_TRANSIT, "1-03-02-001"),
    (INTER_BRANCH_CLEARING, "8-01-01-001"),
    (INVENTORY_SHORTAGE_LOSS, "6-02-01-001"),
    (INVENTORY_WASTE_EXPENSE, "6-02-01-002"),
    (INVENTORY_COUNT_VARIANCE, "7-09-02-001"),
    (INVENTORY_ADJUSTMENT, "7-09-03-001"),
]

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass
class DemoLog:
    """
    What the run did, so the command can report it exactly.

    `created` and `reused` are counted separately because that difference is
    the whole idempotency claim: a second run must report only reuse.
    """

    lines: list[tuple[str, str, str]] = field(default_factory=list)

    def record(self, kind: str, label: str, *, created: bool) -> None:
        self.lines.append((kind, label, "created" if created else "reused"))

    def of_kind(self, kind: str) -> list[tuple[str, str, str]]:
        return [line for line in self.lines if line[0] == kind]

    @property
    def created(self) -> int:
        return sum(1 for line in self.lines if line[2] == "created")

    @property
    def reused(self) -> int:
        return sum(1 for line in self.lines if line[2] == "reused")


@dataclass
class DemoResult:
    """The handles a caller needs to report on, and to verify against."""

    organization: Organization
    source_branch: Branch
    destination_branch: Branch
    user: User
    conductor: User
    business_date: datetime.date
    log: DemoLog


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


def _at(business_date: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    """A Baghdad wall-clock moment on the scenario's business date."""
    return datetime.datetime.combine(business_date, datetime.time(hour, minute), tzinfo=BAGHDAD)


def ensure_organization(log: DemoLog, *, code: str) -> Organization:
    """
    The demo organization, created on demand — but only under its own code.

    A dedicated organization is the preferred shape: it keeps every demo record
    in one place, so `--reset-demo` can prove ownership before deleting
    anything. Naming a *different* organization is supported (the scenario then
    adds only `DEMO`-prefixed master data inside it), but that organization has
    to already exist. This command does not invent real organizations.
    """
    existing = Organization.objects.filter(code=code).first()
    if existing is not None:
        log.record("organization", existing.code, created=False)
        return existing
    if code != DEMO_ORGANIZATION_CODE:
        raise DemoSelectionError(
            f"No organization with code {code}. Existing codes: "
            f"{', '.join(sorted(Organization.objects.values_list('code', flat=True))) or 'none'}. "
            f"Only {DEMO_ORGANIZATION_CODE} is created automatically."
        )
    organization = create_organization(
        code=DEMO_ORGANIZATION_CODE,
        name="خان مندي — بيانات تجريبية",
    )
    log.record("organization", organization.code, created=True)
    return organization


def ensure_branch(log: DemoLog, *, organization: Organization, code: str, name: str) -> Branch:
    existing = Branch.objects.filter(organization=organization, code=code).first()
    if existing is not None:
        log.record("branch", existing.code, created=False)
        return existing
    if code not in (SOURCE_BRANCH_CODE, DESTINATION_BRANCH_CODE):
        known = ", ".join(sorted(organization.branches.values_list("code", flat=True)))
        raise DemoSelectionError(
            f"{organization.code} has no branch {code}. Existing branches: {known or 'none'}. "
            f"Only {SOURCE_BRANCH_CODE} and {DESTINATION_BRANCH_CODE} are created automatically."
        )
    branch = create_branch(
        organization=organization,
        code=code,
        name=name,
        # A restaurant's day rolls over long after midnight; 06:00 is the
        # project's worked example and keeps the business date legible.
        business_day_start_time=datetime.time(6, 0),
        timezone="Asia/Baghdad",
    )
    log.record("branch", branch.code, created=True)
    return branch


def ensure_categories(log: DemoLog, *, organization: Organization) -> dict[str, ItemCategory]:
    categories: dict[str, ItemCategory] = {}
    for code, name, parent_code in CATEGORIES:
        existing = ItemCategory.objects.filter(organization=organization, code=code).first()
        if existing is not None:
            categories[code] = existing
            log.record("category", code, created=False)
            continue
        categories[code] = create_item_category(
            organization=organization,
            code=code,
            name=name,
            parent=categories[parent_code] if parent_code else None,
        )
        log.record("category", code, created=True)
    return categories


def ensure_package_units(log: DemoLog, *, organization: Organization) -> dict[str, PackageUnit]:
    units: dict[str, PackageUnit] = {}
    for code, name in PACKAGE_UNITS:
        existing = PackageUnit.objects.filter(organization=organization, code=code).first()
        if existing is not None:
            units[code] = existing
            log.record("package unit", code, created=False)
            continue
        units[code] = create_package_unit(organization=organization, code=code, name=name)
        log.record("package unit", code, created=True)
    return units


def ensure_items(
    log: DemoLog, *, organization: Organization, categories: dict[str, ItemCategory]
) -> dict[str, InventoryItem]:
    items: dict[str, InventoryItem] = {}
    for code, name, category_code, item_type, unit_code, lots, expiry in ITEMS:
        existing = InventoryItem.objects.filter(organization=organization, code=code).first()
        if existing is not None:
            items[code] = existing
            log.record("item", code, created=False)
            continue
        items[code] = create_item(
            organization=organization,
            code=code,
            name=name,
            category=categories[category_code],
            item_type=item_type,
            base_unit=UnitOfMeasure.objects.get(code=unit_code),
            tracks_lots=lots,
            tracks_expiry=expiry,
        )
        log.record("item", code, created=True)
    return items


def ensure_conversions(
    log: DemoLog,
    *,
    items: dict[str, InventoryItem],
    units: dict[str, PackageUnit],
    business_date: datetime.date,
) -> dict[str, ItemPackageConversion]:
    """
    One conversion per item, effective from the start of the scenario's year.

    Effective *before* the opening cutoff on purpose: a conversion that starts
    the same day the opening posts is a range boundary this dataset has no
    reason to sit on.
    """
    conversions: dict[str, ItemPackageConversion] = {}
    effective_from = datetime.date(business_date.year, 1, 1)
    for item_code, unit_code, factor, conversion_type in CONVERSIONS:
        item = items[item_code]
        unit = units[unit_code]
        existing = ItemPackageConversion.objects.filter(item=item, package_unit=unit).first()
        if existing is not None:
            conversions[item_code] = existing
            log.record("conversion", f"{item_code}/{unit_code}", created=False)
            continue
        conversions[item_code] = create_item_conversion(
            item=item,
            package_unit=unit,
            factor_to_base=Decimal(factor),
            effective_from=effective_from,
            conversion_type=conversion_type,
        )
        log.record("conversion", f"{item_code}/{unit_code}", created=True)
    return conversions


def ensure_warehouses(
    log: DemoLog, *, branch: Branch, wanted: list[tuple[str, str, str]]
) -> dict[str, Warehouse]:
    warehouses: dict[str, Warehouse] = {}
    for code, name, warehouse_type in wanted:
        existing = Warehouse.objects.filter(branch=branch, code=code).first()
        if existing is not None:
            warehouses[code] = existing
            log.record("warehouse", f"{branch.code}/{code}", created=False)
            continue
        warehouses[code] = create_warehouse(
            branch=branch, code=code, name=name, warehouse_type=warehouse_type
        )
        log.record("warehouse", f"{branch.code}/{code}", created=True)

    # System-controlled and never user-created: the service owns it, and the
    # UI offers no way to make or pick one.
    transit_existed = Warehouse.objects.filter(
        branch=branch, warehouse_type=WarehouseType.IN_TRANSIT
    ).exists()
    transit = ensure_in_transit_warehouse(branch=branch)
    warehouses[transit.code] = transit
    log.record("warehouse", f"{branch.code}/{transit.code} (system)", created=not transit_existed)
    return warehouses


def ensure_branch_visibility(
    log: DemoLog,
    *,
    source_branch: Branch,
    destination_branch: Branch,
    items: dict[str, InventoryItem],
) -> None:
    """
    All five items stocked at the source; a subset at the destination.

    A subset rather than everything, because "which items does this branch
    carry" is a real question the screen exists to answer, and a branch that
    carries the entire item master answers it uninformatively.
    """

    def stock_it(branch: Branch, item: InventoryItem) -> None:
        existing = BranchItemSetting.objects.filter(branch=branch, item=item).first()
        # Carry the reorder values forward. `set_branch_item_setting` takes the
        # whole row, so passing the defaults on a re-run would silently erase
        # whatever the demo import applied — and the reorder report would be
        # empty on every second run for no visible reason.
        set_branch_item_setting(
            branch=branch,
            item=item,
            is_stocked=True,
            reorder_point=existing.reorder_point if existing else None,
            reorder_quantity=existing.reorder_quantity if existing else None,
        )
        log.record("branch item", f"{branch.code}/{item.code}", created=existing is None)

    for item in items.values():
        stock_it(source_branch, item)
    for code in ("DEMO-RICE", "DEMO-CONTAINER"):
        stock_it(destination_branch, items[code])


def ensure_accounting(
    log: DemoLog, *, organization: Organization, business_date: datetime.date
) -> None:
    """Chart, cost centres, fiscal year, periods, and the role mappings."""
    configure_accounting(organization=organization)
    had_chart = Account.objects.filter(organization=organization).exists()
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)
    log.record("accounting", "chart of accounts and cost centres", created=not had_chart)

    if not organization.fiscal_years.filter(year=business_date.year).exists():
        open_fiscal_year(organization=organization, year=business_date.year)
        log.record("accounting", f"fiscal year {business_date.year}", created=True)
    else:
        log.record("accounting", f"fiscal year {business_date.year}", created=False)

    effective_from = datetime.date(business_date.year, 1, 1)
    for role_code, account_code in ACCOUNT_MAPPINGS:
        role = AccountRole.objects.get(code=role_code)
        if OrganizationAccountMapping.objects.filter(
            organization=organization, account_role=role, is_active=True
        ).exists():
            # An explicit mapping already recorded is never overwritten.
            log.record("account mapping", role_code, created=False)
            continue
        create_account_mapping(
            organization=organization,
            account_role=role,
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=effective_from,
        )
        log.record("account mapping", role_code, created=True)


def ensure_packaging_override(
    log: DemoLog,
    *,
    organization: Organization,
    categories: dict[str, ItemCategory],
    business_date: datetime.date,
) -> None:
    """
    Packaging consumption lands in its own account.

    A category override rather than an item one, so the inventory-mapping
    screen shows the resolver doing something a default cannot: what a thing is
    consumed *as* is what makes the figure useful.
    """
    category = categories["DEMO-PACKAGING"]
    role = AccountRole.objects.get(code=INVENTORY_CONSUMPTION)
    if InventoryAccountMapping.objects.filter(
        organization=organization, account_role=role, category=category, is_active=True
    ).exists():
        log.record("inventory mapping", "DEMO-PACKAGING consumption", created=False)
        return
    create_inventory_mapping(
        organization=organization,
        role=role,
        account=Account.objects.get(organization=organization, code="5-01-02-002"),
        category=category,
        effective_from=datetime.date(business_date.year, 1, 1),
    )
    log.record("inventory mapping", "DEMO-PACKAGING consumption", created=True)


def ensure_access(
    log: DemoLog,
    *,
    user: User,
    organization: Organization,
    source_branch: Branch,
    destination_branch: Branch,
) -> User:
    """
    Give the demo user authority over the demo organization, and nowhere else.

    Organization authority already reaches every branch it owns, so the branch
    grants here are for the *conductor*, who holds custody at one branch only.
    """
    had_organization_access = OrganizationMembership.objects.filter(
        user=user, organization=organization
    ).exists()
    grant_organization_access(user=user, organization=organization, role=Role.OWNER)
    log.record(
        "access",
        f"{user.username} OWNER of {organization.code}",
        created=not had_organization_access,
    )

    conductor, created = User.objects.get_or_create(
        username=COUNT_CONDUCTOR_USERNAME,
        defaults={"first_name": "أمين مخزن", "last_name": "تجريبي", "is_active": True},
    )
    if created:
        # A data actor, not an account anybody signs in with.
        conductor.set_unusable_password()
        conductor.save(update_fields=["password"])
    log.record("user", COUNT_CONDUCTOR_USERNAME, created=created)

    had_branch_access = BranchMembership.objects.filter(
        user=conductor, branch__in=[source_branch, destination_branch]
    ).exists()
    grant_branch_access(user=conductor, branch=source_branch, role=Role.STOREKEEPER)
    grant_branch_access(user=conductor, branch=destination_branch, role=Role.STOREKEEPER)
    log.record(
        "access",
        f"{conductor.username} STOREKEEPER at both branches",
        created=not had_branch_access,
    )

    # A post the organization defined itself (ADR-034): reads stock and its
    # value, changes nothing. Held by a data actor so the roles screen has a
    # live example and the navigation can be seen cut down to one reader.
    definition = RoleDefinition.objects.filter(
        organization=organization, code=REPORTS_ROLE_CODE
    ).first()
    role_created = definition is None
    if definition is None:
        definition = create_role_definition(
            organization=organization,
            code=REPORTS_ROLE_CODE,
            name="قارئ تقارير المخزون (تجريبي)",
            description="يرى الأرصدة والقيمة والحركات ولا يغيّر شيئاً.",
            permissions=[VIEW_STOCK, VIEW_VALUATION],
        )
    log.record("role", definition.key, created=role_created)

    reader, created = User.objects.get_or_create(
        username=REPORTS_READER_USERNAME,
        defaults={"first_name": "قارئ تقارير", "last_name": "تجريبي", "is_active": True},
    )
    if created:
        reader.set_unusable_password()
        reader.save(update_fields=["password"])
    log.record("user", REPORTS_READER_USERNAME, created=created)
    had_reader_access = BranchMembership.objects.filter(user=reader, branch=source_branch).exists()
    grant_branch_access(user=reader, branch=source_branch, role=definition.key)
    log.record(
        "access",
        f"{reader.username} {REPORTS_ROLE_CODE} at {source_branch.code}",
        created=not had_reader_access,
    )
    return conductor


# ---------------------------------------------------------------------------
# The operation scenario
# ---------------------------------------------------------------------------
#
# Quantities are planned so the whole sequence is valid in one pass:
#
#   rice      150 +60 -20 +5 -30 -20              = 145.000 KG in DEMO-MAIN
#   oil        80  -5 +20 -20 (reversed) -10      =  65.000 L
#   container 1000 -100 -200 -10                  = 690.000 PIECE
#   meat       40 +35.650                         =  75.650 KG
#   chicken    50 (lot 01) · 6 -6 wasted (lot 02) =  50.000 PIECE
#
# Nothing goes negative at any point, and the reversible receipt is posted into
# oil stock that nothing between it and its reversal touches.


def _opening(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    submitter: User,
    poster: User,
    business_date: datetime.date,
) -> OpeningStockDocument:
    """
    One posted opening document carrying every item's starting balance.

    Two actors, because the service refuses one: whoever submitted an opening
    may not also post it. The storekeeper builds the sheet; the owner accepts
    it. Both are real audit actors, not a convenience.
    """
    slug = reference("OPENING-01")
    existing = OpeningStockDocument.objects.filter(
        organization=organization, evidence_reference=slug
    ).first()
    if existing is not None:
        log.record("opening", existing.document_number or slug, created=False)
        return existing

    with audit_context(actor=submitter):
        document = create_opening_document(
            organization=organization,
            branch=branch,
            cutoff_at=_at(business_date, 8),
            evidence_reference=slug,
            narration="أرصدة افتتاحية تجريبية",
        )
    chicken_lot = ensure_opening_lot(
        item=items["DEMO-CHICKEN"],
        code="DEMO-CHK-LOT-01",
        expiry_date=business_date + datetime.timedelta(days=90),
    )
    # Two more dated batches, so the expiry report has a row in every bucket:
    # one expiring soon, one already past. They used to arrive on un-invoiced
    # receipts; that document was withdrawn, and an opening sheet is the other
    # honest way for a batch to be standing there on day one.
    expiring_lot = ensure_opening_lot(
        item=items["DEMO-CHICKEN"],
        code="DEMO-CHK-LOT-03",
        expiry_date=business_date + datetime.timedelta(days=20),
    )
    expired_lot = ensure_opening_lot(
        item=items["DEMO-CHICKEN"],
        code="DEMO-CHK-LOT-04",
        expiry_date=business_date - datetime.timedelta(days=3),
    )
    meat_lot = ensure_opening_lot(item=items["DEMO-MEAT"], code="DEMO-MEAT-LOT-01")

    for line in (
        OpeningLineInput(
            warehouse=warehouse,
            item=items["DEMO-RICE"],
            unit_cost=Decimal("1500.000000"),
            base_quantity=Decimal("150.000"),
        ),
        OpeningLineInput(
            warehouse=warehouse,
            item=items["DEMO-CHICKEN"],
            lot=chicken_lot,
            unit_cost=Decimal("3250.000000"),
            base_quantity=Decimal("50.000"),
        ),
        OpeningLineInput(
            warehouse=warehouse,
            item=items["DEMO-CHICKEN"],
            lot=expiring_lot,
            unit_cost=Decimal("3300.000000"),
            base_quantity=Decimal("12.000"),
        ),
        OpeningLineInput(
            warehouse=warehouse,
            item=items["DEMO-CHICKEN"],
            lot=expired_lot,
            unit_cost=Decimal("3150.000000"),
            base_quantity=Decimal("5.000"),
        ),
        OpeningLineInput(
            warehouse=warehouse,
            item=items["DEMO-OIL"],
            unit_cost=Decimal("2750.000000"),
            base_quantity=Decimal("80.000"),
        ),
        OpeningLineInput(
            warehouse=warehouse,
            item=items["DEMO-MEAT"],
            lot=meat_lot,
            unit_cost=Decimal("12500.000000"),
            base_quantity=Decimal("40.000"),
        ),
        OpeningLineInput(
            warehouse=warehouse,
            item=items["DEMO-CONTAINER"],
            unit_cost=Decimal("250.000000"),
            base_quantity=Decimal("1000.000"),
        ),
    ):
        add_opening_line(document=document, line=line)

    with audit_context(actor=submitter):
        submit_opening_document(document=document)
    with audit_context(actor=poster):
        posted = post_opening_document(document=document)
    log.record("opening", posted.document_number, created=True)
    return posted


def _find_operational(
    *, organization: Organization, document_type: str, slug: str
) -> InventoryMovementDocument | None:
    return InventoryMovementDocument.objects.filter(
        organization=organization, document_type=document_type, evidence_reference=slug
    ).first()


def _issue(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    cost_center: CostCenter,
    business_date: datetime.date,
) -> InventoryMovementDocument:
    """Rice, oil and packaging consumed by the kitchen."""
    slug = reference("ISSUE-01")
    existing = _find_operational(
        organization=organization, document_type=InventoryDocumentType.ISSUE, slug=slug
    )
    if existing is not None:
        log.record("issue", existing.document_number or slug, created=False)
        return existing

    document = create_document(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=InventoryDocumentType.ISSUE,
        effective_at=_at(business_date, 10),
        evidence_reference=slug,
        narration="صرف للمطبخ",
        cost_center=cost_center,
    )
    for item_code, quantity in (
        ("DEMO-RICE", "20.000"),
        ("DEMO-OIL", "5.000"),
        ("DEMO-CONTAINER", "100.000"),
    ):
        add_line(
            document=document,
            line=DocumentLineInput(item=items[item_code], base_quantity=Decimal(quantity)),
        )
    posted = post_document(document=document)
    log.record("issue", posted.document_number, created=True)
    return posted


def _completed_transfer(
    log: DemoLog,
    *,
    organization: Organization,
    source: Warehouse,
    destination: Warehouse,
    items: dict[str, InventoryItem],
    business_date: datetime.date,
) -> StockTransfer:
    """DEMO-MAIN to DEMO-KITCHEN, dispatched and fully received: nothing left."""
    slug = reference("TRANSFER-01-COMPLETED")
    existing = StockTransfer.objects.filter(
        organization=organization, evidence_reference=slug
    ).first()
    if existing is not None:
        log.record("transfer", existing.transfer_number or slug, created=False)
        return existing

    transfer = create_transfer(
        organization=organization,
        source_warehouse=source,
        destination_warehouse=destination,
        effective_at=_at(business_date, 12),
        evidence_reference=slug,
        narration="تحويل داخل الفرع — مستلم بالكامل",
    )
    for item_code, quantity in (("DEMO-RICE", "30.000"), ("DEMO-OIL", "10.000")):
        add_transfer_line(
            transfer=transfer,
            line=TransferLineInput(item=items[item_code], base_quantity=Decimal(quantity)),
        )
    dispatched = dispatch_transfer(transfer=transfer)

    receipt = create_receipt(
        transfer=dispatched,
        effective_at=_at(business_date, 12, 30),
        evidence_reference=reference("TRANSFER-01-RECEIPT-01"),
        narration="استلام كامل",
    )
    for line in dispatched.lines.all():
        add_receipt_line(
            receipt=receipt,
            line=ReceiptLineInput(transfer_line=line, base_quantity=line.base_quantity),
        )
    post_receipt(receipt=receipt)
    log.record("transfer", dispatched.transfer_number, created=True)
    return dispatched


def _partial_transfer(
    log: DemoLog,
    *,
    organization: Organization,
    source: Warehouse,
    destination: Warehouse,
    items: dict[str, InventoryItem],
    business_date: datetime.date,
) -> StockTransfer:
    """
    Cross-branch, deliberately left open.

    200 packaging containers leave; 120 arrive. The remaining 80 stay in the
    source branch's in-transit warehouse and in the in-transit report, which is
    the only way that screen has anything to show.
    """
    slug = reference("TRANSFER-02-PARTIAL")
    existing = StockTransfer.objects.filter(
        organization=organization, evidence_reference=slug
    ).first()
    if existing is not None:
        log.record("transfer", existing.transfer_number or slug, created=False)
        return existing

    transfer = create_transfer(
        organization=organization,
        source_warehouse=source,
        destination_warehouse=destination,
        effective_at=_at(business_date, 13),
        evidence_reference=slug,
        narration="تحويل بين الفروع — استلام جزئي",
    )
    add_transfer_line(
        transfer=transfer,
        line=TransferLineInput(item=items["DEMO-CONTAINER"], base_quantity=Decimal("200.000")),
    )
    dispatched = dispatch_transfer(transfer=transfer)

    receipt = create_receipt(
        transfer=dispatched,
        effective_at=_at(business_date, 13, 30),
        evidence_reference=reference("TRANSFER-02-RECEIPT-01"),
        narration="استلام جزئي — الباقي بالطريق",
    )
    add_receipt_line(
        receipt=receipt,
        line=ReceiptLineInput(
            transfer_line=dispatched.lines.get(), base_quantity=Decimal("120.000")
        ),
    )
    post_receipt(receipt=receipt)
    log.record("transfer", dispatched.transfer_number, created=True)
    return dispatched


def _shortage_transfer(
    log: DemoLog,
    *,
    organization: Organization,
    source: Warehouse,
    destination: Warehouse,
    items: dict[str, InventoryItem],
    cost_center: CostCenter,
    business_date: datetime.date,
) -> StockTransfer:
    """
    Dispatch 20, receive 15, write off 5 — closed with the arithmetic visible.

    dispatch = receipt + shortage, exactly, with nothing left in transit.
    """
    slug = reference("TRANSFER-03-SHORTAGE")
    existing = StockTransfer.objects.filter(
        organization=organization, evidence_reference=slug
    ).first()
    if existing is not None:
        log.record("transfer", existing.transfer_number or slug, created=False)
        return existing

    transfer = create_transfer(
        organization=organization,
        source_warehouse=source,
        destination_warehouse=destination,
        effective_at=_at(business_date, 14),
        evidence_reference=slug,
        narration="تحويل بين الفروع — أُقفل بعجز",
    )
    add_transfer_line(
        transfer=transfer,
        line=TransferLineInput(item=items["DEMO-RICE"], base_quantity=Decimal("20.000")),
    )
    dispatched = dispatch_transfer(transfer=transfer)

    receipt = create_receipt(
        transfer=dispatched,
        effective_at=_at(business_date, 14, 30),
        evidence_reference=reference("TRANSFER-03-RECEIPT-01"),
        narration="استلام ناقص",
    )
    add_receipt_line(
        receipt=receipt,
        line=ReceiptLineInput(
            transfer_line=dispatched.lines.get(), base_quantity=Decimal("15.000")
        ),
    )
    post_receipt(receipt=receipt)

    shortage = create_shortage(
        transfer=dispatched,
        effective_at=_at(business_date, 15),
        reason="عجز مؤكد بعد الجرد على باب المخزن",
        evidence_reference=reference("TRANSFER-03-SHORTAGE-01"),
        cost_center=cost_center,
    )
    post_shortage(shortage=shortage)
    log.record("transfer", dispatched.transfer_number, created=True)
    return dispatched


def _waste(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    cost_center: CostCenter,
    business_date: datetime.date,
) -> InventoryMovementDocument:
    """
    Chicken destroyed in the store, written off against the kitchen.

    An operating expense and never a cost of sales: spoiled food was not sold,
    and burying it in food cost would flatter the gross margin by exactly the
    amount that was thrown away.
    """
    slug = reference("WASTE-01")
    existing = _find_operational(
        organization=organization, document_type=InventoryDocumentType.WASTE, slug=slug
    )
    if existing is not None:
        log.record("waste", existing.document_number or slug, created=False)
        return existing

    document = create_document(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=InventoryDocumentType.WASTE,
        effective_at=_at(business_date, 15, 30),
        evidence_reference=slug,
        narration="إتلاف دجاج",
        cost_center=cost_center,
    )
    add_line(
        document=document,
        line=DocumentLineInput(
            item=items["DEMO-CHICKEN"],
            lot=InventoryLot.objects.get(item=items["DEMO-CHICKEN"], code="DEMO-CHK-LOT-01"),
            base_quantity=Decimal("4.000"),
            line_comment="انقطاع التيار عن الثلاجة ليلاً",
        ),
    )
    posted = post_document(document=document)
    log.record("waste", posted.document_number, created=True)
    return posted


def _expired_waste(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    cost_center: CostCenter,
    business_date: datetime.date,
) -> InventoryMovementDocument:
    """
    The expired batch written off in full.

    A full depletion: the balance goes to zero quantity and surrenders its
    entire remaining book value, which is the rule the stock screen shows and
    the one an eyeballed demo should contain a case of.
    """
    slug = reference("WASTE-02-EXPIRED")
    existing = _find_operational(
        organization=organization, document_type=InventoryDocumentType.WASTE, slug=slug
    )
    if existing is not None:
        log.record("waste", existing.document_number or slug, created=False)
        return existing

    document = create_document(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=InventoryDocumentType.WASTE,
        effective_at=_at(business_date, 15, 45),
        evidence_reference=slug,
        narration="إتلاف دفعة منتهية الصلاحية",
        cost_center=cost_center,
    )
    add_line(
        document=document,
        line=DocumentLineInput(
            item=items["DEMO-CHICKEN"],
            lot=InventoryLot.objects.get(item=items["DEMO-CHICKEN"], code="DEMO-CHK-LOT-04"),
            base_quantity=Decimal("5.000"),
            line_comment="انتهت صلاحية الدفعة",
        ),
    )
    posted = post_document(document=document)
    log.record("waste", posted.document_number, created=True)
    return posted


def _reversed_issue(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    cost_center: CostCenter,
    business_date: datetime.date,
) -> InventoryMovementDocument:
    """
    One issue posted and then reversed, so a reversal is visible on a card.

    A reversal is a state every screen has to render and every reader has to
    recognise, and the demo lost its only one when the un-invoiced receipt was
    withdrawn. It reads better on the issue anyway: correcting a consumption
    keyed against the wrong store is the mistake that actually happens.
    """
    slug = reference("ISSUE-02-REVERSED")
    existing = _find_operational(
        organization=organization, document_type=InventoryDocumentType.ISSUE, slug=slug
    )
    if existing is not None:
        log.record("issue", existing.document_number or slug, created=False)
        return existing

    document = create_document(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=InventoryDocumentType.ISSUE,
        effective_at=_at(business_date, 11, 30),
        evidence_reference=slug,
        narration="صرف زيت — سُجّل على المخزن الخطأ",
        cost_center=cost_center,
    )
    add_line(
        document=document,
        line=DocumentLineInput(item=items["DEMO-OIL"], base_quantity=Decimal("5.000")),
    )
    posted = post_document(document=document)
    reversed_document = reverse_document(document=posted, reason="سُجّل على المخزن الخطأ")
    log.record("issue", reversed_document.document_number, created=True)
    return reversed_document


def _posted_count(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    conductor: User,
    approver: User,
    business_date: datetime.date,
) -> StockCount:
    """
    A full count of the kitchen store, conducted and approved by two people.

    `approved_by != conducted_by` is a database constraint, so the two actors
    are real: the conductor holds the ambient audit context while the sheet is
    entered and submitted, and the approver holds it while it posts.
    """
    slug = reference("COUNT-01-POSTED")
    existing = StockCount.objects.filter(organization=organization, reference=slug).first()
    if existing is not None:
        log.record("stock count", existing.count_number or slug, created=False)
        return existing

    with audit_context(actor=conductor):
        count = create_count(
            organization=organization,
            branch=branch,
            warehouse=warehouse,
            reference=slug,
            reason="جرد شهري تجريبي لمخزن المطبخ",
        )
        started = start_count(count=count, effective_at=_at(business_date, 16))

        # Counted quantities only. The sheet the conductor works from carries
        # no book quantity at all — that is what makes the count blind.
        counted = {"DEMO-RICE": Decimal("29.500"), "DEMO-OIL": Decimal("10.000")}
        entries = [
            CountEntry(
                line=line,
                base_quantity=counted[line.item.code],
            )
            for line in StockCountLine.objects.filter(count=started).select_related("item")
            if line.item.code in counted
        ]
        record_counts(count=started, entries=entries)
        submit_count(count=started)

    with audit_context(actor=approver):
        posted = approve_count(count=started)
    log.record("stock count", posted.count_number, created=True)
    return posted


def _active_count(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    conductor: User,
    business_date: datetime.date,
) -> StockCount:
    """
    A count left in progress, so the blind sheet and the freeze are both visible.

    Deliberately on the production warehouse, which nothing else in this
    scenario posts to: an active count freezes its warehouse, and freezing the
    main store would make every re-run of this command fail.
    """
    slug = reference("COUNT-02-IN-PROGRESS")
    existing = StockCount.objects.filter(organization=organization, reference=slug).first()
    if existing is not None:
        log.record("stock count", existing.count_number or slug, created=False)
        return existing

    with audit_context(actor=conductor):
        count = create_count(
            organization=organization,
            branch=branch,
            warehouse=warehouse,
            reference=slug,
            reason="جرد قيد التنفيذ — لعرض ورقة الجرد المعمّاة",
        )
        started = start_count(count=count, effective_at=_at(business_date, 16, 30))
        # Stock the books do not know about is a legitimate count finding, and
        # the only way an empty warehouse's sheet has a row to look at.
        add_unexpected_line(
            count=started,
            item=items["DEMO-CONTAINER"],
            base_quantity=Decimal("12.000"),
            note="وُجدت علب في منطقة الإنتاج",
        )
    log.record("stock count", started.count_number or slug, created=True)
    return started


def _adjustment(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    cost_center: CostCenter,
    business_date: datetime.date,
) -> InventoryAdjustmentDocument:
    """
    An authorized correction: the books were wrong, and nothing else explains it.

    Not a substitute for a receipt, an issue, a transfer or a count — each of
    those records what happened, and an adjustment records only that the books
    were wrong.
    """
    slug = reference("ADJUSTMENT-01")
    existing = InventoryAdjustmentDocument.objects.filter(
        organization=organization, evidence_reference=slug
    ).first()
    if existing is not None:
        log.record("adjustment", existing.document_number or slug, created=False)
        return existing

    document = create_adjustment(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        effective_at=_at(business_date, 17),
        evidence_reference=slug,
        reason="تصحيح إدخال مزدوج لعلب التغليف",
        cost_center=cost_center,
    )
    add_adjustment_line(
        document=document,
        line=AdjustmentLineInput(
            kind=AdjustmentLineKind.QUANTITY_LOSS,
            item=items["DEMO-CONTAINER"],
            base_quantity=Decimal("10.000"),
            line_comment="أُدخلت الكمية مرتين في مستند الاستلام الورقي",
        ),
    )
    posted = post_adjustment(document=document)
    log.record("adjustment", posted.document_number, created=True)
    return posted


def _drafts(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    source: Warehouse,
    destination: Warehouse,
    items: dict[str, InventoryItem],
    business_date: datetime.date,
) -> None:
    """
    One undispatched transfer.

    Not everything is posted on purpose: a UI that only ever shows terminal
    states has not been shown its own lifecycle, and the draft actions are the
    ones a reviewer most needs to try.
    """
    transfer_slug = reference("TRANSFER-04-DRAFT")
    if StockTransfer.objects.filter(
        organization=organization, evidence_reference=transfer_slug
    ).exists():
        log.record("draft", transfer_slug, created=False)
    else:
        draft_transfer = create_transfer(
            organization=organization,
            source_warehouse=source,
            destination_warehouse=destination,
            effective_at=_at(business_date, 18, 30),
            evidence_reference=transfer_slug,
            narration="مسودة تحويل — لم تُرسل",
        )
        add_transfer_line(
            transfer=draft_transfer,
            line=TransferLineInput(item=items["DEMO-CONTAINER"], base_quantity=Decimal("50.000")),
        )
        log.record("draft", draft_transfer.transfer_number or transfer_slug, created=True)


# ---------------------------------------------------------------------------
# Expiry, the remaining adjustment kinds, and the rest of the count lifecycle
# ---------------------------------------------------------------------------


def _gain_adjustment(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    cost_center: CostCenter,
    business_date: datetime.date,
) -> InventoryAdjustmentDocument:
    """
    Stock the books did not know about, brought on at an explicit cost.

    A gain never guesses its cost. The unit cost is stated, because valuing
    found goods at the standing average would quietly move value that no
    purchase ever paid for.
    """
    slug = reference("ADJUSTMENT-02-GAIN")
    existing = InventoryAdjustmentDocument.objects.filter(
        organization=organization, evidence_reference=slug
    ).first()
    if existing is not None:
        log.record("adjustment", existing.document_number or slug, created=False)
        return existing

    document = create_adjustment(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        effective_at=_at(business_date, 17, 15),
        evidence_reference=slug,
        reason="علب وُجدت خارج الرف ولم تُسجَّل",
        cost_center=cost_center,
    )
    add_adjustment_line(
        document=document,
        line=AdjustmentLineInput(
            kind=AdjustmentLineKind.QUANTITY_GAIN,
            item=items["DEMO-CONTAINER"],
            base_quantity=Decimal("15.000"),
            unit_cost=Decimal("250.000000"),
            line_comment="كرتون لم يُدخل في مستند الاستلام",
        ),
    )
    posted = post_adjustment(document=document)
    log.record("adjustment", posted.document_number, created=True)
    return posted


def _revaluation_adjustment(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    cost_center: CostCenter,
    business_date: datetime.date,
) -> InventoryAdjustmentDocument:
    """
    Value corrected with no goods moving — the reason this aggregate exists.

    A write-down of the rice position: the quantity is untouched and the
    average cost falls. It cannot be expressed as a signed movement of goods,
    which is why `VALUE_ONLY` is its own line kind.
    """
    slug = reference("ADJUSTMENT-03-VALUE-ONLY")
    existing = InventoryAdjustmentDocument.objects.filter(
        organization=organization, evidence_reference=slug
    ).first()
    if existing is not None:
        log.record("adjustment", existing.document_number or slug, created=False)
        return existing

    document = create_adjustment(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        effective_at=_at(business_date, 17, 30),
        evidence_reference=slug,
        reason="تصحيح كلفة شحن حُمّلت على الرز مرتين",
        cost_center=cost_center,
    )
    add_adjustment_line(
        document=document,
        line=AdjustmentLineInput(
            kind=AdjustmentLineKind.VALUE_ONLY,
            item=items["DEMO-RICE"],
            value_adjustment=Decimal("-5000.000"),
            line_comment="كلفة نقل مكررة تُطرح من قيمة الرصيد",
        ),
    )
    posted = post_adjustment(document=document)
    log.record("adjustment", posted.document_number, created=True)
    return posted


def _cancelled_count(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    conductor: User,
    business_date: datetime.date,
) -> StockCount:
    """
    A count abandoned after it started, kept rather than deleted.

    It froze the main store for part of an afternoon and that is a fact worth
    keeping: the warehouse was unavailable, and a deleted count would leave no
    explanation. Cancelling releases the freeze — the store is usable again the
    moment the decision is recorded.
    """
    slug = reference("COUNT-03-CANCELLED")
    existing = StockCount.objects.filter(organization=organization, reference=slug).first()
    if existing is not None:
        log.record("stock count", existing.count_number or slug, created=False)
        return existing

    with audit_context(actor=conductor):
        count = create_count(
            organization=organization,
            branch=branch,
            warehouse=warehouse,
            reference=slug,
            reason="جرد بدأ ثم أُلغي لانشغال المخزن",
        )
        started = start_count(count=count, effective_at=_at(business_date, 19))
        cancelled = cancel_count(count=started, reason="طلب الفرع تأجيل الجرد إلى نهاية الشهر")
    log.record("stock count", cancelled.count_number or slug, created=True)
    return cancelled


def _submitted_count(
    log: DemoLog,
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    conductor: User,
    business_date: datetime.date,
) -> StockCount:
    """
    A count entered and submitted, waiting for a second person to approve it.

    The other half of maker-checker, and the state a reviewer most needs to
    see: submitted still holds the warehouse freeze, so the store stays closed
    until somebody who did not count it accepts the result.
    """
    slug = reference("COUNT-04-SUBMITTED")
    existing = StockCount.objects.filter(organization=organization, reference=slug).first()
    if existing is not None:
        log.record("stock count", existing.count_number or slug, created=False)
        return existing

    with audit_context(actor=conductor):
        count = create_count(
            organization=organization,
            branch=branch,
            warehouse=warehouse,
            reference=slug,
            reason="جرد الفرع الثاني — بانتظار الاعتماد",
        )
        started = start_count(count=count, effective_at=_at(business_date, 20))
        # Counted exactly as the books expect: this one is here for its
        # *status*, and a variance would post on approval and change balances
        # a reviewer is comparing against.
        record_counts(
            count=started,
            entries=[
                CountEntry(line=line, base_quantity=line.book_quantity)
                for line in StockCountLine.objects.filter(count=started)
            ],
        )
        submitted = submit_count(count=started)
    log.record("stock count", submitted.count_number or slug, created=True)
    return submitted


# ---------------------------------------------------------------------------
# Task 1.7A — what the reports and the import history need to have rows
# ---------------------------------------------------------------------------

#: filename slug, item, reorder point, reorder quantity. Chosen against the
#: scenario's known branch holdings so the reorder report shows all three
#: cases: rice sits below its point, oil sits exactly on it, packaging is well
#: above. Fixed numbers rather than arithmetic on the current balance, because
#: a demo whose expectations move with the data teaches nothing and cannot be
#: asserted.
REORDER_ROWS: list[tuple[str, str, str]] = [
    ("DEMO-RICE", "200.000", "120.000"),  # on hand 174.500 -> below by 25.500
    ("DEMO-OIL", "75.000", "60.000"),  # on hand  75.000 -> exactly at
    ("DEMO-CONTAINER", "500.000", "250.000"),  # on hand 785.000 -> above
]


def _find_batch(*, organization: Organization, filename: str) -> ImportBatch | None:
    return ImportBatch.objects.filter(organization=organization, original_filename=filename).first()


def _applied_import(
    log: DemoLog, *, organization: Organization, branch: Branch, actor: User
) -> ImportBatch:
    """
    One import that went all the way through, setting the reorder points.

    Built through the real import service, not by writing `ImportBatch` rows:
    the point of having it in the demo is that the history screen shows a batch
    somebody could have uploaded, with real row verdicts and a real applied
    count.
    """
    filename = f"{NAMESPACE}-reorder-applied.csv"
    existing = _find_batch(organization=organization, filename=filename)
    if existing is not None:
        log.record("import batch", f"{filename} ({existing.status})", created=False)
        return existing

    lines = ["item_code,is_stocked,reorder_point,reorder_quantity"]
    lines += [f"{code},yes,{point},{quantity}" for code, point, quantity in REORDER_ROWS]
    raw = ("\n".join(lines) + "\n").encode("utf-8")

    with audit_context(actor=actor):
        batch = create_batch(
            organization=organization,
            branch=branch,
            kind=ImportKind.BRANCH_ITEM_SETTING,
            raw=raw,
            filename=filename,
        )
        batch = validate_batch(batch=batch)
        batch = apply_batch(batch=batch)
    log.record(
        "import batch",
        f"{filename} ({batch.status}, {batch.applied_row_count} changed)",
        created=True,
    )
    return batch


def _failed_import(
    log: DemoLog, *, organization: Organization, branch: Branch, actor: User
) -> ImportBatch:
    """
    One import that was refused, with a good row in it.

    The good row matters more than the bad ones. A batch of nothing but errors
    demonstrates that validation rejects rubbish; a batch that is *mostly*
    right demonstrates the harder rule — that one bad row stops all of it, and
    the valid row was not quietly applied anyway.
    """
    filename = f"{NAMESPACE}-reorder-rejected.csv"
    existing = _find_batch(organization=organization, filename=filename)
    if existing is not None:
        log.record("import batch", f"{filename} ({existing.status})", created=False)
        return existing

    raw = (
        "item_code,is_stocked,reorder_point,reorder_quantity\n"
        # Valid, and deliberately never applied.
        "DEMO-MEAT,yes,30.000,20.000\n"
        # No such item in this organization.
        "NOT-A-DEMO-ITEM,yes,10.000,10.000\n"
        # Neither field parses.
        "DEMO-CHICKEN,ربما,كثير,5.000\n"
    ).encode()

    with audit_context(actor=actor):
        batch = create_batch(
            organization=organization,
            branch=branch,
            kind=ImportKind.BRANCH_ITEM_SETTING,
            raw=raw,
            filename=filename,
        )
        batch = validate_batch(batch=batch)
    log.record(
        "import batch",
        f"{filename} ({batch.status}, {batch.error_row_count} rejected)",
        created=True,
    )
    return batch


# ---------------------------------------------------------------------------
# Task 1.7B — locations
# ---------------------------------------------------------------------------

#: code, Arabic name. Three bins in the main store, which is enough to show a
#: put-away, a move between two of them, and a bin that stays empty.
DEMO_LOCATIONS: list[tuple[str, str]] = [
    ("BIN-A", "رف أ — الحبوب"),
    ("BIN-B", "رف ب — الزيوت"),
    ("BIN-C", "رف ج — فارغ"),
]


def _locations(
    log: DemoLog,
    *,
    warehouse: Warehouse,
    items: dict[str, InventoryItem],
    actor: User,
) -> None:
    """
    Bins in the main store, with stock deliberately left partly unlocated.

    The unlocated remainder is the point rather than an oversight. Locations are
    optional and most rooms in this business are one room; a demo where every
    kilo sat in a bin would hide the state every real warehouse starts in and
    the one the reconciliation screen has to show.

    Rice is put away and then partly moved between two bins — a move that posts
    no `StockMovement` at all, because nothing entered or left the warehouse.
    Oil is put away whole. Meat, chicken and packaging stay unlocated.

    The rice figures are three quarters of what they were: the scenario used to
    receive 60 kg through an un-invoiced receipt and take 5 back through a
    return, and both documents were withdrawn from the product. The shape is
    what matters — some located, some not, and an internal move across bins —
    and the shape is unchanged.
    """
    created: dict[str, StockLocation] = {}
    for code, name in DEMO_LOCATIONS:
        existing = StockLocation.objects.filter(warehouse=warehouse, code=code).first()
        if existing is not None:
            created[code] = existing
            log.record("location", f"{warehouse.code}/{code}", created=False)
            continue
        with audit_context(actor=actor):
            created[code] = create_location(warehouse=warehouse, code=code, name=name)
        log.record("location", f"{warehouse.code}/{code}", created=True)

    # Idempotent by state: if anything is already put away, this ran before.
    if StockLocationBalance.objects.filter(warehouse=warehouse).exists():
        log.record("location stock", "put-away and internal move", created=False)
        return

    with audit_context(actor=actor):
        put_away(
            location=created["BIN-A"],
            item=items["DEMO-RICE"],
            quantity=Decimal("60.000"),
            reference=reference("PUT-AWAY-RICE"),
        )
        put_away(
            location=created["BIN-B"],
            item=items["DEMO-OIL"],
            quantity=Decimal("40.000"),
            reference=reference("PUT-AWAY-OIL"),
        )
        # No StockMovement, no journal, no re-average — the warehouse position
        # is identical before and after.
        move_between_locations(
            source=created["BIN-A"],
            destination=created["BIN-C"],
            item=items["DEMO-RICE"],
            quantity=Decimal("15.000"),
            reference=reference("MOVE-RICE-A-TO-C"),
        )
    log.record("location stock", "put-away and internal move", created=True)


# ---------------------------------------------------------------------------
# The whole scenario
# ---------------------------------------------------------------------------


@transaction.atomic
def seed_inventory_demo(
    *,
    user: User,
    organization_code: str = DEMO_ORGANIZATION_CODE,
    source_branch_code: str = SOURCE_BRANCH_CODE,
    destination_branch_code: str = DESTINATION_BRANCH_CODE,
    business_date: datetime.date | None = None,
    with_operations: bool = True,
) -> DemoResult:
    """
    Build the demo dataset, or find that it is already there.

    One transaction: a scenario that fails half way through is worse than one
    that did not run, because the half that posted is real.
    """
    log = DemoLog()
    business_date = business_date or timezone.localdate()

    organization = ensure_organization(log, code=organization_code)
    source_branch = ensure_branch(
        log,
        organization=organization,
        code=source_branch_code,
        name="فرع البنوك — تجريبي",
    )
    destination_branch = ensure_branch(
        log,
        organization=organization,
        code=destination_branch_code,
        name="فرع ثانٍ — تجريبي",
    )
    if source_branch.pk == destination_branch.pk:
        raise DemoSelectionError(
            "The source and destination branches are the same. The cross-branch "
            "transfer scenario needs two."
        )

    ensure_accounting(log, organization=organization, business_date=business_date)
    categories = ensure_categories(log, organization=organization)
    ensure_packaging_override(
        log, organization=organization, categories=categories, business_date=business_date
    )
    units = ensure_package_units(log, organization=organization)
    items = ensure_items(log, organization=organization, categories=categories)
    ensure_conversions(log, items=items, units=units, business_date=business_date)
    source_warehouses = ensure_warehouses(log, branch=source_branch, wanted=SOURCE_WAREHOUSES)
    destination_warehouses = ensure_warehouses(
        log, branch=destination_branch, wanted=DESTINATION_WAREHOUSES
    )
    ensure_branch_visibility(
        log,
        source_branch=source_branch,
        destination_branch=destination_branch,
        items=items,
    )
    conductor = ensure_access(
        log,
        user=user,
        organization=organization,
        source_branch=source_branch,
        destination_branch=destination_branch,
    )

    if with_operations:
        kitchen_cost_center = CostCenter.objects.get(organization=organization, code="KITCHEN")
        warehouse_cost_center = CostCenter.objects.get(organization=organization, code="WAREHOUSE")
        main = source_warehouses["DEMO-MAIN"]
        kitchen = source_warehouses["DEMO-KITCHEN"]
        wip = source_warehouses["DEMO-WIP"]
        destination_main = destination_warehouses["DEMO-DEST-MAIN"]

        _opening(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            items=items,
            submitter=conductor,
            poster=user,
            business_date=business_date,
        )
        _issue(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            items=items,
            cost_center=kitchen_cost_center,
            business_date=business_date,
        )
        _completed_transfer(
            log,
            organization=organization,
            source=main,
            destination=kitchen,
            items=items,
            business_date=business_date,
        )
        _partial_transfer(
            log,
            organization=organization,
            source=main,
            destination=destination_main,
            items=items,
            business_date=business_date,
        )
        _shortage_transfer(
            log,
            organization=organization,
            source=main,
            destination=destination_main,
            items=items,
            cost_center=warehouse_cost_center,
            business_date=business_date,
        )
        _waste(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            items=items,
            cost_center=kitchen_cost_center,
            business_date=business_date,
        )
        _expired_waste(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            items=items,
            cost_center=kitchen_cost_center,
            business_date=business_date,
        )
        _reversed_issue(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            items=items,
            cost_center=kitchen_cost_center,
            business_date=business_date,
        )
        _posted_count(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=kitchen,
            items=items,
            conductor=conductor,
            approver=user,
            business_date=business_date,
        )
        _adjustment(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            items=items,
            cost_center=warehouse_cost_center,
            business_date=business_date,
        )
        _gain_adjustment(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            items=items,
            cost_center=warehouse_cost_center,
            business_date=business_date,
        )
        _revaluation_adjustment(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            items=items,
            cost_center=warehouse_cost_center,
            business_date=business_date,
        )
        _drafts(
            log,
            organization=organization,
            branch=source_branch,
            source=main,
            destination=kitchen,
            items=items,
            business_date=business_date,
        )
        # The imports need the branch item settings to exist, which
        # `ensure_branch_visibility` guaranteed above.
        _applied_import(log, organization=organization, branch=source_branch, actor=user)
        _failed_import(log, organization=organization, branch=source_branch, actor=user)
        _locations(log, warehouse=main, items=items, actor=user)
        # The counts go last, and in this order. A cancelled count releases its
        # freeze, so the main store is usable again; the other two keep theirs,
        # and anything posted into a frozen warehouse afterwards is refused.
        _cancelled_count(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=main,
            conductor=conductor,
            business_date=business_date,
        )
        _active_count(
            log,
            organization=organization,
            branch=source_branch,
            warehouse=wip,
            items=items,
            conductor=conductor,
            business_date=business_date,
        )
        _submitted_count(
            log,
            organization=organization,
            branch=destination_branch,
            warehouse=destination_main,
            conductor=conductor,
            business_date=business_date,
        )

    return DemoResult(
        organization=organization,
        source_branch=source_branch,
        destination_branch=destination_branch,
        user=user,
        conductor=conductor,
        business_date=business_date,
        log=log,
    )


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


@dataclass
class ResetReport:
    """What a reset removed, and what it honestly could not."""

    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    refused: str = ""


@transaction.atomic
def reset_demo(*, organization_code: str = DEMO_ORGANIZATION_CODE) -> ResetReport:
    """
    Remove what can legitimately be removed, and say what cannot.

    Posted stock and accounting effects are append-only by design. A reset that
    deleted them would not be a reset — it would be the one operation this
    system exists to make impossible, performed for the convenience of a
    development command. So this deletes drafts, and for everything posted it
    reports the count and stops.

    The honest way to start over from nothing is a fresh scenario version
    (`DEMO-INVENTORY-V2`) or a fresh development database. Both leave the
    ledger's guarantee intact.
    """
    report = ResetReport()
    organization = Organization.objects.filter(code=organization_code).first()
    if organization is None:
        report.refused = f"No organization with code {organization_code}. Nothing to reset."
        return report

    if organization_code != DEMO_ORGANIZATION_CODE:
        # Ownership cannot be proved: a shared organization holds records this
        # command did not create, and a reset there could not tell them apart.
        report.refused = (
            f"{organization_code} is not the dedicated demo organization "
            f"({DEMO_ORGANIZATION_CODE}). Reset only runs where every record is "
            "demo-owned, because it cannot otherwise prove what it is deleting."
        )
        return report

    for document in InventoryMovementDocument.objects.filter(
        organization=organization, evidence_reference__startswith=f"{NAMESPACE}/"
    ).order_by("pk"):
        if document.status == InventoryDocumentStatus.DRAFT:
            number = document.document_number or document.evidence_reference
            delete_document(document=document, reason="إعادة تهيئة البيانات التجريبية")
            report.removed.append(f"draft document {number}")

    for transfer in StockTransfer.objects.filter(
        organization=organization, evidence_reference__startswith=f"{NAMESPACE}/"
    ).order_by("pk"):
        if transfer.status == StockTransferStatus.DRAFT:
            number = transfer.transfer_number or transfer.evidence_reference
            delete_transfer(transfer=transfer, reason="إعادة تهيئة البيانات التجريبية")
            report.removed.append(f"draft transfer {number}")

    posted_movements = StockMovement.objects.filter(organization=organization).count()
    posted_journals = JournalEntry.objects.filter(organization=organization).count()
    if posted_movements or posted_journals:
        report.kept.append(
            f"{posted_movements} posted stock movements and {posted_journals} journal "
            "entries — append-only, and never deleted to make a reseed convenient"
        )
        report.kept.append(f"all {NAMESPACE} master data, because posted movements reference it")
        return report

    # Nothing posted: the rest of the master data is genuinely unused and can
    # go. Children first — a conversion, a lot and a branch stocking decision
    # all PROTECT the item they hang off.
    def note(deleted: int, label: str) -> None:
        if deleted:
            report.removed.append(f"{deleted} {label}")

    note(
        ItemPackageConversion.objects.filter(item__code__startswith="DEMO-").delete()[0],
        "conversions",
    )
    note(InventoryLot.objects.filter(item__code__startswith="DEMO-").delete()[0], "lots")
    note(
        BranchItemSetting.objects.filter(item__code__startswith="DEMO-").delete()[0],
        "branch item settings",
    )
    note(
        InventoryItem.objects.filter(organization=organization, code__startswith="DEMO-").delete()[
            0
        ],
        "items",
    )
    note(
        PackageUnit.objects.filter(
            organization=organization, code__in=[code for code, _ in PACKAGE_UNITS]
        ).delete()[0],
        "package units",
    )
    note(
        InventoryAccountMapping.objects.filter(
            organization=organization, category__code__startswith="DEMO-"
        ).delete()[0],
        "inventory account overrides",
    )

    # Leaves first, and one at a time: a category PROTECTs its parent, and a
    # queryset `delete()` collects the whole set before deleting any of it, so
    # ordering the queryset would not help.
    removed_categories = 0
    for category in ItemCategory.objects.filter(
        organization=organization, code__startswith="DEMO-"
    ).order_by("-depth"):
        category.delete()
        removed_categories += 1
    if removed_categories:
        report.removed.append(f"{removed_categories} categories")
    return report
