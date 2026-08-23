"""
Inventory master data and the stock ledger.

Task 1.1 delivered what the ledger references: categories, package units,
items, item-specific package conversions, branch item settings, and
warehouses. Task 1.2 adds the ledger itself — lots, postings, movements,
balances, and valuation layers.

The division that matters is at the bottom of this file. Master data is
mutable and historied. The ledger is **append-only**: posted movements are
immutable by database trigger, and a correction is a new movement that
reverses an old one, never an edit.

See `docs/tasks/task-1-0-inventory-domain-spec.md` for the approved design,
`docs/decisions/ADR-018-...` for the valuation contract, and
`docs/invariants/inventory-invariants.md` for the rules these must satisfy.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.accounting.models import SourceEvent
from apps.core.models import TimeStampedModel
from apps.core.money import MONEY_PLACES, UNIT_PRICE_PLACES
from apps.core.quantity import FACTOR_PLACES, QUANTITY_PLACES

#: Item and package codes. Wider than the organization/branch pattern by one
#: character — supplier catalogues use dots — and canonicalised to uppercase
#: before it is ever stored, so uniqueness is case-insensitive in effect
#: without a functional index (spec §3).
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"

#: Deepest category level. `Food > Meat > Beef` is the motivating case and
#: three is enough for it; a fourth level in practice means someone is
#: modelling the item itself as a category.
MAX_CATEGORY_DEPTH = 3

#: A conversion factor is a technical identity, at the same precision as
#: `UnitOfMeasure.factor_to_base` (ADR-006). 12 places because an ounce needs
#: them.
FACTOR_MAX_DIGITS = FACTOR_PLACES + 12

QUANTITY_MAX_DIGITS = QUANTITY_PLACES + 15

#: Room for an organization's whole inventory value in IQD, which is a
#: currency with large nominal amounts.
MONEY_MAX_DIGITS = MONEY_PLACES + 18

UNIT_PRICE_MAX_DIGITS = UNIT_PRICE_PLACES + 15


class ItemCategory(TimeStampedModel):
    """
    A reporting group, organization-owned, at most three levels deep.

    Items hang on leaves only. This is the invariant ADR-014 enforces for
    accounts, for the same reason: an item attached to a parent stops its
    children summing to it, and from that point no category report can be
    trusted.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="item_categories",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("parent category"),
    )
    #: Stored rather than walked so the limit is a database constraint and not
    #: only a service opinion. Maintained by the service across a re-parent,
    #: which is bounded because the tree is bounded.
    depth = models.PositiveSmallIntegerField(_("depth"), default=1)

    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("item category")
        verbose_name_plural = _("item categories")
        ordering = ["organization__code", "code"]
        permissions = [
            ("manage_categories", _("Can create and archive item categories")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="item_category_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN),
                name="item_category_code_format",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""),
                name="item_category_name_ar_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(depth__gte=1) & Q(depth__lte=MAX_CATEGORY_DEPTH),
                name="item_category_depth_within_limit",
            ),
            # A root has no parent and a child must have one, so `depth` can
            # never disagree with the shape of the tree at level 1.
            models.CheckConstraint(
                condition=(
                    (Q(depth=1) & Q(parent__isnull=True))
                    | (Q(depth__gt=1) & Q(parent__isnull=False))
                ),
                name="item_category_depth_matches_parentage",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"

    @property
    def is_leaf(self) -> bool:
        """Whether this category may hold items."""
        return not self.children.exists()


class PackageUnit(TimeStampedModel):
    """
    A container an item is bought or handled in — carton, sack, tin, tray.

    **It carries no conversion factor, and that absence is the point.** A
    carton of chicken and a carton of oil share only the word, so there is no
    universal "carton" factor to record. Every factor lives on
    `ItemPackageConversion`, per item, and there is nowhere here to write one
    — which means nobody can.

    Deliberately not a `UnitOfMeasure`: that model carries a global factor
    within a dimension (1 kg is 1000 g everywhere, forever) and a package has
    no such property.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="package_units",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=100)
    name_en = models.CharField(_("name (English)"), max_length=100, blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("package unit")
        verbose_name_plural = _("package units")
        ordering = ["organization__code", "code"]
        permissions = [
            ("manage_package_units", _("Can create and archive package units")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="package_unit_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="package_unit_code_format"
            ),
            models.CheckConstraint(condition=~Q(name_ar=""), name="package_unit_name_ar_not_empty"),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class ItemType(models.TextChoices):
    """
    What kind of thing an item is. Closed, six values (spec §3).

    `FINISHED_GOOD` means a produced output that is **physically stored and
    countable** — a tray of bread baked for tomorrow, bottled sauce made
    in-house. It does **not** mean menu item: a plated Chicken Mandi is
    assembled on demand, never stored, and is not an `InventoryItem` at all.
    Menu items belong to Sales and Recipes and are linked to inventory through
    a recipe in Phase 3, never by sharing a row.
    """

    RAW_MATERIAL = "RAW_MATERIAL", _("مادة أولية")
    SEMI_FINISHED = "SEMI_FINISHED", _("نصف مصنّع")
    FINISHED_GOOD = "FINISHED_GOOD", _("منتج تام مخزون")
    GOODS_FOR_RESALE = "GOODS_FOR_RESALE", _("بضاعة لإعادة البيع")
    PACKAGING = "PACKAGING", _("مواد تغليف")
    CONSUMABLE = "CONSUMABLE", _("مستهلكات")


class CostingMethod(models.TextChoices):
    """Release 1 offers one method (ADR-018); the field marks the boundary."""

    MOVING_WEIGHTED_AVERAGE = "MOVING_WEIGHTED_AVERAGE", _("المتوسط المرجح المتحرك")


class InventoryItem(TimeStampedModel):
    """
    A stocked thing, owned by the organization and shared across its branches.

    Carries no account foreign key: account resolution belongs exclusively to
    `AccountRole`/`AccountMapping` (Task 1.3), and a second path here would
    compete with it silently.

    Carries no negative-stock flag: an override is a per-posting decision with
    an actor and a reason, never a standing property of the item.

    Carries no `is_variable_weight` flag: it is derived from the item's active
    `VARIABLE` conversions, and a stored copy could only ever disagree.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="inventory_items",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    category = models.ForeignKey(
        ItemCategory,
        on_delete=models.PROTECT,
        related_name="items",
        verbose_name=_("category"),
    )
    item_type = models.CharField(_("type"), max_length=20, choices=ItemType.choices)

    #: Immutable once posted movements exist. Every stored quantity is in this
    #: unit, so changing it silently restates the item's whole history.
    base_unit = models.ForeignKey(
        "units.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="inventory_items",
        verbose_name=_("base stock unit"),
    )

    is_active = models.BooleanField(_("active"), default=True)

    tracks_lots = models.BooleanField(_("tracks lots"), default=False)
    tracks_expiry = models.BooleanField(_("tracks expiry"), default=False)
    shelf_life_days = models.PositiveSmallIntegerField(
        _("shelf life (days)"), null=True, blank=True
    )

    costing_method = models.CharField(
        _("costing method"),
        max_length=24,
        choices=CostingMethod.choices,
        default=CostingMethod.MOVING_WEIGHTED_AVERAGE,
    )

    notes = models.TextField(_("notes"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("inventory item")
        verbose_name_plural = _("inventory items")
        ordering = ["organization__code", "code"]
        permissions = [
            ("view_item", _("Can view the item master")),
            ("manage_items", _("Can create and archive inventory items")),
            ("create_item", _("Can register a new inventory item")),
            ("edit_item", _("Can change an existing inventory item")),
            ("manage_conversions", _("Can manage item package conversions")),
            ("view_stock", _("Can view stock on hand")),
            ("view_valuation", _("Can view inventory cost and valuation")),
            ("create_draft_movement", _("Can create a draft stock movement")),
            ("create_opening_stock", _("Can prepare and submit an opening stock document")),
            ("post_opening_stock", _("Can post opening stock")),
            ("post_receipt", _("Can post a stock receipt")),
            ("post_issue", _("Can post a stock issue")),
            ("post_return_in", _("Can return previously issued stock to inventory")),
            ("post_transfer", _("Can post a stock transfer")),
            (
                "close_transfer_shortage",
                _("Can close a transfer's missing quantity as a loss"),
            ),
            ("post_waste", _("Can post stock waste")),
            ("conduct_stock_count", _("Can conduct a stock count")),
            ("approve_stock_count", _("Can approve a stock count")),
            ("post_adjustment", _("Can post a stock adjustment")),
            ("reverse_movement", _("Can reverse a stock movement")),
            ("override_negative_stock", _("Can post stock below zero")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="inventory_item_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="inventory_item_code_format"
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""), name="inventory_item_name_ar_not_empty"
            ),
            # Expiry has nothing to attach to without a lot.
            models.CheckConstraint(
                condition=Q(tracks_expiry=False) | Q(tracks_lots=True),
                name="inventory_item_expiry_requires_lots",
            ),
            # A shelf life is meaningless unless expiry is tracked.
            models.CheckConstraint(
                condition=Q(shelf_life_days__isnull=True) | Q(tracks_expiry=True),
                name="inventory_item_shelf_life_requires_expiry",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"], name="item_org_active_idx"),
            models.Index(fields=["category"], name="item_category_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"

    @property
    def is_variable_weight(self) -> bool:
        """
        Derived, never stored.

        True when the item has an active `VARIABLE` package conversion. The
        same item legitimately has a fixed retail pack and a variable bulk
        container at once, so a single stored flag could not have been true.
        """
        return self.package_conversions.filter(
            is_active=True, conversion_type=ConversionType.VARIABLE
        ).exists()


class ConversionType(models.TextChoices):
    """
    Whether a package's content is known or measured.

    `FIXED` — the factor is exact and converts arithmetically. A 30 kg sack is
    30 kg.

    `VARIABLE` — the package is what was ordered and counted, but the base
    quantity is *measured* at receipt. One meat container is whatever it
    weighed. The stored factor is a planning estimate used for ordering and
    variance reporting, and posting requires an explicit measured quantity —
    which is what stops one container silently becoming 18.000 kg forever.
    """

    FIXED = "FIXED", _("ثابت")
    VARIABLE = "VARIABLE", _("متغير")


class ItemPackageConversion(TimeStampedModel):
    """
    How many base units are in one package of this item.

    **Resolves directly to the item's base unit. There are no chains.** If
    tomato paste is based in kilograms, both `1 CAN -> 0.8 KG` and
    `1 CARTON -> 24 KG` are recorded directly; `CARTON -> CAN -> KG` is not
    modelled. Every link in a chain is a place where a version, an effective
    date, or a rounding step can disagree with the others, and a carton
    changing from 30 cans to 24 does not change what a can is.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="item_package_conversions",
        verbose_name=_("organization"),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="package_conversions",
        verbose_name=_("item"),
    )
    package_unit = models.ForeignKey(
        PackageUnit,
        on_delete=models.PROTECT,
        related_name="item_conversions",
        verbose_name=_("package unit"),
    )

    conversion_type = models.CharField(
        _("conversion type"),
        max_length=8,
        choices=ConversionType.choices,
        default=ConversionType.FIXED,
    )
    factor_to_base = models.DecimalField(
        _("base units per package"),
        max_digits=FACTOR_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
        help_text=_(
            "Exact for a fixed package. For a variable package this is a planning "
            "estimate only — posting requires a measured quantity."
        ),
    )

    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)

    allows_fractional = models.BooleanField(_("allows fractional packages"), default=True)
    minimum_increment = models.DecimalField(
        _("minimum increment"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )

    is_default_purchase_package = models.BooleanField(_("default purchase package"), default=False)
    #: Incremented per (item, package_unit) whenever a factor is superseded.
    #: Snapshotted onto every posting from Task 1.2, so a later correction
    #: cannot change what an existing movement meant.
    version = models.PositiveIntegerField(_("version"), default=1)
    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("item package conversion")
        verbose_name_plural = _("item package conversions")
        ordering = ["item__code", "package_unit__code", "-effective_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(factor_to_base__gt=Decimal("0")),
                name="item_conversion_factor_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="item_conversion_period_is_ordered",
            ),
            models.CheckConstraint(
                condition=Q(minimum_increment__isnull=True) | Q(minimum_increment__gt=Decimal("0")),
                name="item_conversion_minimum_increment_is_positive",
            ),
            # One version of a factor per package per item at a time. The
            # overlap rule itself needs a range constraint and is added by the
            # trigger migration.
            models.UniqueConstraint(
                fields=["item", "package_unit", "version"],
                name="item_conversion_version_unique_per_package",
            ),
            # At most one default purchase package per item, among active rows.
            models.UniqueConstraint(
                fields=["item"],
                condition=Q(is_default_purchase_package=True, is_active=True),
                name="item_conversion_one_default_purchase_package",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.item.code}: 1 {self.package_unit.code} = {self.factor_display}"

    @property
    def factor_display(self) -> str:
        """
        The factor as a technical identity — always a period, never a comma,
        and always at full stored precision.

        Django localises Decimals, so under Arabic this would otherwise render
        `0,800000000000`. A comma there is ambiguous and invites a mis-typed
        re-entry (CLAUDE.md, locale-independence rule).

        Quantized rather than formatted as-is so the rendering does not depend
        on whether the value has been round-tripped through the database: a
        freshly assigned `Decimal("0.8")` and the same value re-read must not
        display differently.
        """
        quantum = Decimal(1).scaleb(-FACTOR_PLACES)
        return f"{self.factor_to_base.quantize(quantum):f}"

    def covers(self, on_date: datetime.date) -> bool:
        """
        Whether this version was the effective one on a given **business date**.

        Same shape as `InventoryAccountMapping.covers`, and asked the same way:
        against the document's business date rather than today's, so
        re-versioning a factor cannot restate what an already-entered line
        meant.
        """
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to


class BranchItemSetting(TimeStampedModel):
    """
    Whether a branch stocks an item, and at what levels it reorders.

    Absence of a row means "not stocked here" for pickers and reorder
    reports. It must **never** block a legitimate incoming transfer: stock
    arriving at a branch that has not configured the item is an operational
    fact, and refusing it would strand the goods in transit.

    Holds no quantity. Quantity lives on `StockBalance` from Task 1.2.
    """

    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="item_settings",
        verbose_name=_("branch"),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="branch_settings",
        verbose_name=_("item"),
    )
    is_stocked = models.BooleanField(_("stocked at this branch"), default=True)
    reorder_point = models.DecimalField(
        _("reorder point"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    reorder_quantity = models.DecimalField(
        _("reorder quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    notes = models.TextField(_("notes"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("branch item setting")
        verbose_name_plural = _("branch item settings")
        ordering = ["branch__code", "item__code"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "item"], name="branch_item_setting_unique"),
            models.CheckConstraint(
                condition=Q(reorder_point__isnull=True) | Q(reorder_point__gte=Decimal("0")),
                name="branch_item_reorder_point_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(reorder_quantity__isnull=True) | Q(reorder_quantity__gt=Decimal("0")),
                name="branch_item_reorder_quantity_is_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.item.code} @ {self.branch.code}"


class WarehouseType(models.TextChoices):
    """Closed. `IN_TRANSIT` is system-controlled and never user-created."""

    PHYSICAL = "PHYSICAL", _("مخزن فعلي")
    PRODUCTION_WIP = "PRODUCTION_WIP", _("إنتاج تحت التشغيل")
    IN_TRANSIT = "IN_TRANSIT", _("بضاعة بالطريق")


class Warehouse(TimeStampedModel):
    """
    A place stock is held, belonging to exactly one branch.

    **The warehouse owns inventory value** (ADR-018). Locations/bins arrive in
    Task 1.7 and will carry quantity only — moving a box between bins inside
    one store must not revalue anything.
    """

    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="warehouses",
        verbose_name=_("branch"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    warehouse_type = models.CharField(
        _("type"),
        max_length=20,
        choices=WarehouseType.choices,
        default=WarehouseType.PHYSICAL,
    )
    #: Set by migration or seeding only. A user-settable "system" flag would
    #: be a way to make an ordinary warehouse exempt from the rules that
    #: protect the ledger.
    is_system = models.BooleanField(_("system warehouse"), default=False)

    is_active = models.BooleanField(_("active"), default=True)

    #: The count that has this warehouse frozen, and **the only statement that
    #: it is frozen at all** (Task 1.6 §I).
    #:
    #: A boolean alongside an "active count somewhere" would be two mutable
    #: truths about one fact, and the interesting failures are exactly the ones
    #: where they disagree: a warehouse frozen with no count to unfreeze it, or
    #: a count that believes it holds a freeze somebody else has released.
    #: Here there is nothing to disagree with — frozen means this column is
    #: set, and the row it names is the only thing that may clear it.
    #:
    #: `StockBalance.is_frozen` is a different, finer concept that predates
    #: this and stays: a count freezes the **warehouse**, and pretending a
    #: warehouse-wide freeze is a per-position one is what §H forbids.
    frozen_by_count = models.ForeignKey(
        "inventory.StockCount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="frozen_warehouse_links",
        verbose_name=_("frozen by count"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("warehouse")
        verbose_name_plural = _("warehouses")
        ordering = ["branch__code", "code"]
        permissions = [
            ("manage_warehouses", _("Can create and archive warehouses")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "code"], name="warehouse_code_unique_per_branch"
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="warehouse_code_format"
            ),
            models.CheckConstraint(condition=~Q(name_ar=""), name="warehouse_name_ar_not_empty"),
            # In-transit is always a system warehouse, and a system warehouse
            # is always in-transit — the only system type Release 1 defines.
            models.CheckConstraint(
                condition=(
                    Q(warehouse_type=WarehouseType.IN_TRANSIT, is_system=True)
                    | (~Q(warehouse_type=WarehouseType.IN_TRANSIT) & Q(is_system=False))
                ),
                name="warehouse_in_transit_iff_system",
            ),
            # One in-transit warehouse per branch. Two would split stock that
            # has left one place and not arrived at another.
            models.UniqueConstraint(
                fields=["branch"],
                condition=Q(warehouse_type=WarehouseType.IN_TRANSIT),
                name="warehouse_one_in_transit_per_branch",
            ),
            # One count holds at most one warehouse. Two would mean a count
            # whose snapshot covered a place it never froze.
            models.UniqueConstraint(
                fields=["frozen_by_count"],
                condition=Q(frozen_by_count__isnull=False),
                name="warehouse_freeze_owner_unique",
            ),
            # Goods on the road are in nobody's building, so nobody can count
            # them. The in-transit balance is reconciled against the transfers
            # that put it there (ADR-020), which is a stronger check than a
            # physical one and does not need a freeze.
            models.CheckConstraint(
                condition=Q(frozen_by_count__isnull=True)
                | ~Q(warehouse_type=WarehouseType.IN_TRANSIT),
                name="warehouse_in_transit_is_never_counted",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


# ===========================================================================
# Task 1.2 — the stock ledger
# ===========================================================================
#
# Two append-only tables and one projection:
#
#   StockLedgerEntry   one posting. Carries the source identity, the
#                      idempotency key, and the reversal link ONCE.
#   StockMovement      the individual effects of that posting, each on one
#                      (warehouse, item, lot). Immutable.
#   StockBalance       a cache of where the ledger has got to, rebuildable by
#                      replaying the movements in posted order.
#
# The source identity lives on the entry and not on every movement, so a
# posting cannot half-change what it claims to record. Each movement reaches
# it through `entry`, which is enough to trace and impossible to contradict.


class MovementType(models.TextChoices):
    """
    Why stock moved. Closed, and each value has a fixed sign.

    The sign is a property of the movement type rather than of the caller's
    input: an issue is negative because it is an issue, and a caller passing a
    negative quantity to a receipt is making a mistake, not requesting a
    correction.
    """

    OPENING = "OPENING", _("رصيد افتتاحي")
    RECEIPT = "RECEIPT", _("إدخال")
    ISSUE = "ISSUE", _("صرف")
    #: Unused stock coming back from a consumption issue. Valued at the
    #: original issue's cost, never today's average, so the pair nets to zero
    #: (spec §8). Added by Task 1.4 with the document that produces it.
    RETURN_IN = "RETURN_IN", _("إرجاع من صرف")
    #: Goods going back **out** to the supplier they came from. Reserved by
    #: Task 1.0's movement table and deliberately left unbuilt until Phase 2
    #: had a document to produce it (Task 1.7's note: "a supplier return must
    #: reconcile against a supplier invoice, a payable, and a credit note,
    #: none of which exist yet"). Task 2.13 is that document.
    #:
    #: Its own value rather than `RETURN_IN`, and the distinction is the whole
    #: of PRC-047: `RETURN_IN` is stock coming back from a kitchen to a store,
    #: at the cost it was issued at, and this is stock leaving the business
    #: altogether at the standing average. Opposite directions, opposite signs,
    #: and two different reports. Sharing one value would make each report
    #: wrong about the other.
    #:
    #: The inventory module owns the movement and nothing else. There is no
    #: supplier-return document here and no generic screen that produces one:
    #: this type is reachable only through `apps.procurement`'s service, which
    #: is what keeps the causing document in the module that understands
    #: suppliers.
    RETURN_OUT = "RETURN_OUT", _("إرجاع إلى المورد")
    TRANSFER_OUT = "TRANSFER_OUT", _("تحويل صادر")
    TRANSFER_IN = "TRANSFER_IN", _("تحويل وارد")
    #: Dispatched goods that will never arrive, written off out of in-transit
    #: stock. Its own type rather than `WASTE`: waste is spoilage at a
    #: warehouse and belongs to the waste report, while a transfer shortage is
    #: a loss in transit and belongs to the transfer report. One value for both
    #: would make each report wrong about the other (Task 1.5 §N).
    TRANSFER_SHORTAGE = "TRANSFER_SHORTAGE", _("عجز تحويل")
    WASTE = "WASTE", _("هالك")
    COUNT_GAIN = "COUNT_GAIN", _("فائض جرد")
    COUNT_LOSS = "COUNT_LOSS", _("عجز جرد")
    PRODUCTION_IN = "PRODUCTION_IN", _("إنتاج وارد")
    PRODUCTION_OUT = "PRODUCTION_OUT", _("صرف للإنتاج")
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT", _("تسوية يدوية")
    REVERSAL = "REVERSAL", _("عكس قيد")


#: Which way each movement type pushes the balance. `REVERSAL` and
#: `MANUAL_ADJUSTMENT` are absent on purpose: both carry the sign of what they
#: are correcting, so a fixed direction would be a lie half the time.
INBOUND_MOVEMENT_TYPES = frozenset(
    {
        MovementType.OPENING,
        MovementType.RECEIPT,
        MovementType.RETURN_IN,
        MovementType.TRANSFER_IN,
        MovementType.COUNT_GAIN,
        MovementType.PRODUCTION_IN,
    }
)

OUTBOUND_MOVEMENT_TYPES = frozenset(
    {
        MovementType.ISSUE,
        MovementType.RETURN_OUT,
        MovementType.TRANSFER_OUT,
        MovementType.TRANSFER_SHORTAGE,
        MovementType.WASTE,
        MovementType.COUNT_LOSS,
        MovementType.PRODUCTION_OUT,
    }
)

#: Types whose direction the **caller** must state, because the type genuinely
#: does not carry one. A manual adjustment can add goods, remove goods, or
#: revalue goods that are not moving at all, and no fixed sign is right for
#: more than one of those.
#:
#: Task 1.2 left these out of both sets above with a comment saying why, and
#: `post_stock_entry` fell through to the outbound branch — correct for two of
#: the three cases and silently wrong for the third. Task 1.6 makes the caller
#: say which, and refuses an effect that does not (`MovementInput.direction`).
SIGNLESS_MOVEMENT_TYPES = frozenset({MovementType.MANUAL_ADJUSTMENT})


class InventoryLot(TimeStampedModel):
    """
    A batch of one item, tracked separately because its cost or its expiry
    differs from the next batch.

    Lot-tracked items hold **a separate moving average per lot** (ADR-018), so
    a lot is part of the valuation key and not merely a label.

    Never deleted. A lot is referenced by every movement that touched it, and
    an expired or exhausted lot is exactly what a recall traces.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="inventory_lots",
        verbose_name=_("organization"),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="lots",
        verbose_name=_("item"),
    )
    code = models.CharField(
        _("lot code"),
        max_length=64,
        help_text=_("Our reference for this batch. Unique per item."),
    )
    supplier_lot_code = models.CharField(
        _("supplier lot code"),
        max_length=64,
        blank=True,
        help_text=_("As printed by the supplier. Not case-folded — it is their vocabulary."),
    )
    expiry_date = models.DateField(_("expiry date"), null=True, blank=True)
    received_on = models.DateField(_("first received on"), null=True, blank=True)

    # --- Reserved for production (Phase 3) --------------------------------
    # A lot produced in-house is caused by a production order, and that order
    # is a source document like any other. The fields are declared now so the
    # vocabulary is one vocabulary; nothing writes them in Phase 1.
    produced_by_document_type = models.CharField(
        _("produced by document type"), max_length=100, blank=True
    )
    produced_by_document_id = models.CharField(
        _("produced by document id"), max_length=64, blank=True
    )

    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("inventory lot")
        verbose_name_plural = _("inventory lots")
        ordering = ["item__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["item", "code"], name="inventory_lot_code_unique"),
            models.CheckConstraint(condition=~Q(code=""), name="inventory_lot_code_not_empty"),
        ]
        indexes = [
            models.Index(fields=["organization", "expiry_date"], name="lot_org_expiry_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.item.code}/{self.code}"

    def is_expired_on(self, on_date: datetime.date) -> bool:
        """Whether this lot had passed its expiry by the given date."""
        return self.expiry_date is not None and self.expiry_date < on_date


class StockPostingSequence(models.Model):
    """
    The organization's posted-order counter.

    Valuation follows **posting order**, never effective-date order, so the
    ledger needs a total order that two concurrent postings cannot both claim.
    A counter row taken under `SELECT ... FOR UPDATE` gives one: gapless,
    deterministic, and scoped to the organization it orders.

    The cost is stated plainly rather than hidden: taking this lock serialises
    postings **within one organization** for the remainder of their
    transaction. That is a stronger bound than ADR-018 anticipated, where
    contention was per `(warehouse, item, lot)`. It is accepted at restaurant
    volumes — a branch posts tens of movements a day, not thousands a second —
    and it buys a sequence with no gaps and no dependence on commit order. If
    it ever binds, the replacement is a PostgreSQL sequence per organization,
    which trades gaplessness for concurrency; nothing above this model depends
    on the numbers being contiguous.

    It is acquired **after** the stock-key locks, never before. The reverse
    order would deadlock a transaction holding a key and waiting for the
    counter against one holding the counter and waiting for that key.
    """

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="stock_posting_sequence",
        verbose_name=_("organization"),
    )
    last_sequence = models.BigIntegerField(_("last sequence"), default=0)

    class Meta:
        verbose_name = _("stock posting sequence")
        verbose_name_plural = _("stock posting sequences")
        constraints = [
            models.CheckConstraint(
                condition=Q(last_sequence__gte=0), name="stock_sequence_not_negative"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code}: {self.last_sequence}"


class StockLedgerEntry(TimeStampedModel):
    """
    One posting into the stock ledger, and the only place its identity lives.

    Immutable once written, enforced by a database trigger and not by
    convention. The single permitted change is the back-link written when a
    later entry reverses this one; everything else is refused at the database,
    so bulk updates, raw SQL, and the admin cannot quietly rewrite history.

    Carries the source identity **once**. Repeating it on every movement would
    create a row that could disagree with its siblings about which document it
    came from.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="stock_ledger_entries",
        verbose_name=_("organization"),
    )

    # --- Source identity: all four, or a manual posting with none ---------
    source_document_type = models.CharField(_("source document type"), max_length=100, blank=True)
    source_document_id = models.CharField(_("source document id"), max_length=64, blank=True)
    source_event = models.CharField(
        _("source event"),
        max_length=16,
        blank=True,
        choices=SourceEvent.choices,
        help_text=_("Blank only for a posting with no upstream document."),
    )

    # --- Idempotency ------------------------------------------------------
    #: Unique **per organization**. A globally unique key would let one
    #: organization's choice block another's, and a lookup on the key alone
    #: would hand back somebody else's posting.
    idempotency_key = models.CharField(_("idempotency key"), max_length=128)
    #: A digest of what the request actually asked for. A key match with a
    #: different fingerprint is a conflict, not a retry.
    request_fingerprint = models.CharField(_("request fingerprint"), max_length=64)

    #: When it happened in the business. May be backdated within an OPEN
    #: period; it never re-prices anything already posted (ADR-018 §5).
    effective_at = models.DateTimeField(_("effective at"))
    #: The operational day this posting belongs to, derived through the
    #: branch's timezone and operating-day cutoff — **not** `effective_at`'s
    #: calendar date (ADR-008). This is the date the accounting period is
    #: validated against and the date daily reporting groups by. A posting at
    #: 01:30 on the 1st under an 03:00 cutoff belongs to the previous day and
    #: requires only that day's period to be open.
    business_date = models.DateField(_("business date"), db_index=True)
    #: When it entered the ledger.
    posted_at = models.DateTimeField(_("posted at"))
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_ledger_entries",
        verbose_name=_("posted by"),
    )

    reference = models.CharField(_("reference"), max_length=200, blank=True)
    reason = models.TextField(_("reason"), blank=True)

    #: The entry this one reverses, and the back-link to its reverser. Both
    #: nullable; a normal posting has neither.
    reverses = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_by",
        verbose_name=_("reverses"),
    )

    #: The journal this posting produced, written once immediately after it
    #: (Task 1.5 §S). Null means the posting never reached the general ledger
    #: — the bare kernel in a focused test, or a tool with no accounting in
    #: play. Non-null makes the conditional control-account invariant bite:
    #: every value-bearing movement under an accounted entry must name the
    #: account its value moved through.
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_ledger_entries",
        verbose_name=_("journal entry"),
    )

    class Meta:
        verbose_name = _("stock ledger entry")
        verbose_name_plural = _("stock ledger entries")
        ordering = ["-posted_at", "-id"]
        constraints = [
            # All of the source identity or none of it. A half-populated
            # identity sits OUTSIDE the partial unique index below while
            # looking, to a reader, exactly like an entry inside it.
            models.CheckConstraint(
                condition=(
                    Q(source_document_type="", source_document_id="", source_event="")
                    | (
                        ~Q(source_document_type="")
                        & ~Q(source_document_id="")
                        & ~Q(source_event="")
                    )
                ),
                name="stock_entry_source_identity_all_or_none",
            ),
            models.CheckConstraint(
                condition=Q(source_event__in=["", *SourceEvent.values]),
                name="stock_entry_source_event_is_known",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""),
                name="stock_entry_idempotency_key_not_empty",
            ),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                name="stock_entry_idempotency_key_unique_per_organization",
            ),
            # One economic event, one posting.
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "source_document_type",
                    "source_document_id",
                    "source_event",
                ],
                condition=~Q(source_event=""),
                name="stock_entry_source_event_unique_per_organization",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "effective_at"], name="stock_entry_org_effective_idx"
            ),
            models.Index(
                fields=["source_document_type", "source_document_id"],
                name="stock_entry_source_idx",
            ),
        ]

    def __str__(self) -> str:
        if self.source_event:
            return f"{self.source_document_type}/{self.source_document_id}/{self.source_event}"
        return f"stock entry {self.pk}"

    @property
    def is_reversed(self) -> bool:
        return hasattr(self, "reversed_by")


class StockMovement(models.Model):
    """
    One effect of one posting on one `(warehouse, item, lot)`.

    **Insert-only.** No update, no delete, enforced by a database trigger. A
    correction is a new movement that reverses this one; editing history would
    make every report that has already been read retrospectively untrue.

    Carries the whole arithmetic of the moment it was posted — the balance
    before, the balance after, and the average on both sides. That is
    redundant with a replay, and deliberately so: it is what lets a
    reconciliation *disagree* with the projection and say where.

    Not a `TimeStampedModel`: `updated_at` would be a field that can never
    change, and a column that lies about being mutable invites somebody to try.
    """

    entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        related_name="movements",
        verbose_name=_("ledger entry"),
    )

    # Denormalised from the warehouse for tenancy filtering and indexing. The
    # service asserts they agree; they are never an independent truth.
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="stock_movements",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="stock_movements",
        verbose_name=_("branch"),
    )

    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="stock_movements",
        verbose_name=_("warehouse"),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="stock_movements",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        InventoryLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
        verbose_name=_("lot"),
    )

    movement_type = models.CharField(
        _("movement type"), max_length=20, choices=MovementType.choices
    )

    #: Distinguishes the effects of ONE source event from each other:
    #: `line:1:warehouse:10`. Together with the entry's source identity it is
    #: what makes a retry idempotent at the effect level and not merely at the
    #: document level.
    effect_key = models.CharField(_("effect key"), max_length=120)

    #: Signed, in the item's base unit. Negative for anything leaving.
    base_quantity = models.DecimalField(
        _("base quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    #: Signed, in IQD. Mirrors the quantity's sign for ordinary movements.
    inventory_value = models.DecimalField(
        _("inventory value"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: What one base unit was worth in this movement. For an outbound this is
    #: the average at the time; for an inbound it is the purchase cost.
    unit_cost = models.DecimalField(
        _("unit cost"), max_digits=UNIT_PRICE_MAX_DIGITS, decimal_places=UNIT_PRICE_PLACES
    )

    quantity_before = models.DecimalField(
        _("quantity before"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    quantity_after = models.DecimalField(
        _("quantity after"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    value_before = models.DecimalField(
        _("value before"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    value_after = models.DecimalField(
        _("value after"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    average_before = models.DecimalField(
        _("average before"), max_digits=UNIT_PRICE_MAX_DIGITS, decimal_places=UNIT_PRICE_PLACES
    )
    average_after = models.DecimalField(
        _("average after"), max_digits=UNIT_PRICE_MAX_DIGITS, decimal_places=UNIT_PRICE_PLACES
    )

    #: The total order valuation was computed in. Technical, never a document
    #: number, and never allocated through a public write API.
    posted_sequence = models.BigIntegerField(_("posted sequence"))

    #: For a reversal: the movement being mirrored. A reversal takes its
    #: original's quantity and value exactly, not today's average.
    reverses = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_by",
        verbose_name=_("reverses movement"),
    )

    #: Which package conversion produced the base quantity, when one did. The
    #: link is what freezes that conversion: once a movement was valued
    #: through it, correcting it in place would restate this movement.
    source_conversion = models.ForeignKey(
        ItemPackageConversion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
        verbose_name=_("source conversion"),
    )

    #: The inventory-control account this movement's value entered or left —
    #: captured at posting and immutable with the rest of the row.
    #:
    #: This is what makes an ISSUE creditable without re-resolving anything:
    #: the value leaves the account it actually entered, so a mapping changed
    #: since cannot credit stock to an account it never sat in. Reconciliation
    #: reads it for the same reason.
    #:
    #: Nullable, and the null means something specific: a movement posted with
    #: no account mapping in play at all — the bare kernel, exercised by its
    #: own tests. Every movement a business document posts carries one, and a
    #: test holds that line. Inventing an account for a posting that resolved
    #: none would be worse than recording that it had none.
    control_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
        verbose_name=_("inventory control account"),
    )

    effective_at = models.DateTimeField(_("effective at"))
    posted_at = models.DateTimeField(_("posted at"), auto_now_add=True)

    class Meta:
        verbose_name = _("stock movement")
        verbose_name_plural = _("stock movements")
        ordering = ["-posted_sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "posted_sequence"],
                name="stock_movement_sequence_unique_per_organization",
            ),
            # One effect per effect key per posting. A retry that reached this
            # far would be refused by the database, not merely by the service.
            models.UniqueConstraint(
                fields=["entry", "effect_key"],
                name="stock_movement_effect_key_unique_per_entry",
            ),
            models.CheckConstraint(
                condition=~Q(effect_key=""), name="stock_movement_effect_key_not_empty"
            ),
            # Quantity zero means value zero — the invariant ADR-018 §4 makes
            # true by construction, checked here so no future code path can
            # quietly break it.
            models.CheckConstraint(
                condition=Q(quantity_after__gt=Decimal("0")) | Q(value_after=Decimal("0")),
                name="stock_movement_zero_quantity_has_zero_value",
            ),
            models.CheckConstraint(
                condition=Q(quantity_after__gte=Decimal("0")),
                name="stock_movement_quantity_after_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(value_after__gte=Decimal("0")),
                name="stock_movement_value_after_not_negative",
            ),
            models.CheckConstraint(
                condition=~Q(reverses=models.F("id")),
                name="stock_movement_does_not_reverse_itself",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "posted_sequence"], name="movement_org_sequence_idx"
            ),
            models.Index(fields=["warehouse", "item", "lot"], name="movement_stock_key_idx"),
            models.Index(fields=["item", "effective_at"], name="movement_item_effective_idx"),
        ]

    def __str__(self) -> str:
        return f"#{self.posted_sequence} {self.movement_type} {self.item_id} {self.base_quantity}"


class StockBalance(TimeStampedModel):
    """
    Where the ledger has got to for one `(warehouse, item, lot)`.

    **A projection, never the truth.** The movements are the truth; this is a
    cache so that "what do we hold" is one row read instead of a sum over
    history. `rebuild_balances` replays the ledger and compares; a divergence
    is a defect that fails a test, never something to repair by overwriting —
    a projection that can be quietly corrected proves nothing.

    Organization and branch are denormalised from the warehouse for tenancy
    filtering. They are not part of the identity: a warehouse belongs to one
    branch and one organization, and putting a derivable value inside an
    identity is an invitation for two rows to disagree.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="stock_balances",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="stock_balances",
        verbose_name=_("branch"),
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="stock_balances",
        verbose_name=_("warehouse"),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="stock_balances",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        InventoryLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_balances",
        verbose_name=_("lot"),
    )

    quantity = models.DecimalField(
        _("quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        default=Decimal("0"),
    )
    value = models.DecimalField(
        _("value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )
    average_cost = models.DecimalField(
        _("average cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        default=Decimal("0"),
    )

    #: The inventory-control account this position's **current** stock cycle
    #: belongs to, or NULL when the position is empty.
    #:
    #: A cycle runs from the first inbound into an empty position until the
    #: position empties again. Within one cycle the account cannot change: a
    #: receipt into standing stock must use the same account the stock already
    #: sits in, or the two would blend and no journal would ever have moved
    #: the difference. At zero there is nothing to strand, so the identity is
    #: cleared and the next inbound may establish a newly effective account.
    #:
    #: Derived state, and deliberately so — it is recoverable by replaying the
    #: movements' own `control_account`, which `rebuild`/`verify` checks.
    control_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_balances",
        verbose_name=_("inventory control account"),
    )

    last_movement = models.ForeignKey(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("last movement"),
    )
    last_posted_sequence = models.BigIntegerField(_("last posted sequence"), default=0)
    #: Bumped on every write. Lets a reader detect that the row moved under it
    #: without comparing every field.
    version = models.PositiveBigIntegerField(_("version"), default=0)

    #: A frozen warehouse rejects posting. Held here rather than on Warehouse
    #: so a freeze can be per stock position later without a second concept.
    is_frozen = models.BooleanField(_("frozen"), default=False)

    class Meta:
        verbose_name = _("stock balance")
        verbose_name_plural = _("stock balances")
        ordering = ["warehouse__code", "item__code"]
        constraints = [
            # One row per physical stock position. `nulls_distinct=False` is
            # the whole point: in standard SQL every NULL differs from every
            # other, so a plain unique constraint would permit unlimited rows
            # for a non-lot-tracked item — precisely the case that must have
            # exactly one (ADR-018 §1).
            models.UniqueConstraint(
                fields=["warehouse", "item", "lot"],
                nulls_distinct=False,
                name="stock_balance_key_unique",
            ),
            # Task 1.2 refuses negative stock outright, so the database says so
            # too. NOTE: activating `inventory.override_negative_stock` in a
            # later task must relax this constraint in the same migration that
            # activates it — otherwise the override is refused by the very
            # database it is meant to be an exception to.
            models.CheckConstraint(
                condition=Q(quantity__gte=Decimal("0")), name="stock_balance_quantity_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(value__gte=Decimal("0")), name="stock_balance_value_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(average_cost__gte=Decimal("0")),
                name="stock_balance_average_not_negative",
            ),
            # Q == 0 implies V == 0, by construction and then by constraint.
            models.CheckConstraint(
                condition=Q(quantity__gt=Decimal("0")) | Q(value=Decimal("0")),
                name="stock_balance_zero_quantity_has_zero_value",
            ),
            # An empty position holds no control-account identity. The
            # converse — positive quantity always naming an account — is a
            # service rule rather than a constraint, because the bare kernel
            # legitimately posts with no mapping resolved at all; see
            # `StockMovement.control_account`.
            models.CheckConstraint(
                condition=Q(quantity__gt=Decimal("0")) | Q(control_account__isnull=True),
                name="stock_balance_empty_position_has_no_control_account",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "item"], name="balance_org_item_idx"),
            models.Index(fields=["branch", "item"], name="balance_branch_item_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.warehouse.code}/{self.item.code}: {self.quantity}"


class ValuationLayer(TimeStampedModel):
    """
    One inbound receipt, with the cost it arrived at.

    Recorded from the first posting even though a moving average never needs
    it. That is the point (ADR-018 §3): with layers captured, introducing FIFO
    later is a new consumption strategy over data that already exists. Without
    them it is a migration of history that cannot be reconstructed, because
    the information was never written down.

    `remaining_quantity` is maintained only when a layered strategy is active.
    Under `MOVING_WEIGHTED_AVERAGE` it stays at the received quantity, and the
    field is **not** a claim about what is physically left — the balance row
    is the only authority on that.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="valuation_layers",
        verbose_name=_("organization"),
    )
    movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        related_name="valuation_layer",
        verbose_name=_("inbound movement"),
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="valuation_layers"
    )
    item = models.ForeignKey(
        InventoryItem, on_delete=models.PROTECT, related_name="valuation_layers"
    )
    lot = models.ForeignKey(
        InventoryLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="valuation_layers",
    )

    received_quantity = models.DecimalField(
        _("received quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    remaining_quantity = models.DecimalField(
        _("remaining quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    unit_cost = models.DecimalField(
        _("unit cost"), max_digits=UNIT_PRICE_MAX_DIGITS, decimal_places=UNIT_PRICE_PLACES
    )
    posted_sequence = models.BigIntegerField(_("posted sequence"))
    effective_at = models.DateTimeField(_("effective at"))

    class Meta:
        verbose_name = _("valuation layer")
        verbose_name_plural = _("valuation layers")
        ordering = ["posted_sequence"]
        constraints = [
            models.CheckConstraint(
                condition=Q(received_quantity__gt=Decimal("0")),
                name="valuation_layer_received_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(remaining_quantity__gte=Decimal("0")),
                name="valuation_layer_remaining_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__gte=Decimal("0")), name="valuation_layer_cost_not_negative"
            ),
        ]
        indexes = [
            models.Index(
                fields=["warehouse", "item", "lot", "posted_sequence"],
                name="layer_stock_key_seq_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"layer #{self.posted_sequence} {self.item_id} @ {self.unit_cost}"


class ValuationAllocation(models.Model):
    """
    Which layer an outbound movement consumed — **and nothing writes it yet.**

    Deliberately empty under `MOVING_WEIGHTED_AVERAGE`. A moving average does
    not consume a layer: it charges the blended cost of everything on hand, so
    recording that an issue "took 30 kg from the layer received on the 3rd"
    would be a fabrication that looks like evidence. The outbound cost
    authority is, and stays, the moving-average snapshot on `StockMovement`.

    The table exists because the allocation for a past period is **derivable**
    — layers and outbound movements both carry `posted_sequence`, so a FIFO
    migration can compute the consumption it needs from the ledger it already
    has. This gives that computation somewhere to land without inventing a
    second cost authority today.

    Anything written here must state which strategy produced it. Until one
    does, the honest content is no rows at all.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="valuation_allocations",
        verbose_name=_("organization"),
    )
    movement = models.ForeignKey(
        StockMovement,
        on_delete=models.PROTECT,
        related_name="valuation_allocations",
        verbose_name=_("outbound movement"),
    )
    layer = models.ForeignKey(
        ValuationLayer,
        on_delete=models.PROTECT,
        related_name="allocations",
        verbose_name=_("layer"),
    )
    quantity = models.DecimalField(
        _("quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    #: Which costing method computed this row. Never blank: an allocation
    #: whose provenance is unknown cannot be told apart from a fabricated one.
    strategy = models.CharField(_("strategy"), max_length=32)

    class Meta:
        verbose_name = _("valuation allocation")
        verbose_name_plural = _("valuation allocations")
        ordering = ["movement_id", "layer_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["movement", "layer"], name="valuation_allocation_unique"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=Decimal("0")),
                name="valuation_allocation_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=~Q(strategy=""), name="valuation_allocation_strategy_named"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.movement_id} <- {self.layer_id}: {self.quantity}"


# ===========================================================================
# Task 1.3 — account mapping overrides and the opening-stock document
# ===========================================================================
#
# The dependency rule these models exist to respect (ADR-019): inventory
# imports accounting, never the reverse. `AccountRole` and the organization
# default mapping live in `apps.accounting`; the item- and category-specific
# **overrides** live here, because they reference `InventoryItem` and
# `ItemCategory`, which accounting must not know exist.


class InventoryAccountMapping(TimeStampedModel):
    """
    An item- or category-specific override of the organization's account
    mapping, for roles that permit one.

    Exactly one target: an item, or a category — never both, never neither.
    An organization-wide default does not belong here; it belongs to
    `accounting.OrganizationAccountMapping`, and a second place to record one
    would eventually give two answers.

    Used mappings are immutable except for closing the effective range. The
    posting that resolved one snapshots it, and the snapshot must stay
    readable.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="inventory_account_mappings",
        verbose_name=_("organization"),
    )
    account_role = models.ForeignKey(
        "accounting.AccountRole",
        on_delete=models.PROTECT,
        related_name="inventory_mappings",
        verbose_name=_("account role"),
    )
    account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="inventory_mappings",
        verbose_name=_("account"),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="account_mappings",
        verbose_name=_("item"),
    )
    category = models.ForeignKey(
        ItemCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="account_mappings",
        verbose_name=_("category"),
    )
    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)
    version = models.PositiveIntegerField(_("version"), default=1)
    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("inventory account mapping")
        verbose_name_plural = _("inventory account mappings")
        ordering = ["organization__code", "account_role__code", "-effective_from"]
        constraints = [
            # One target, exactly. A row naming both would make precedence
            # ambiguous; a row naming neither would be an organization default
            # hiding in the wrong table.
            models.CheckConstraint(
                condition=(
                    (Q(item__isnull=False) & Q(category__isnull=True))
                    | (Q(item__isnull=True) & Q(category__isnull=False))
                ),
                name="inventory_mapping_one_target",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="inventory_mapping_period_is_ordered",
            ),
            models.UniqueConstraint(
                fields=["organization", "account_role", "item", "category", "version"],
                nulls_distinct=False,
                name="inventory_mapping_version_unique",
            ),
            # The overlap rule needs a range type and COALESCE over the
            # nullable target columns; the migration adds it as EXCLUDE.
        ]
        indexes = [
            models.Index(
                fields=["organization", "account_role", "is_active"],
                name="inv_mapping_role_idx",
            ),
        ]

    def __str__(self) -> str:
        target = self.item.code if self.item is not None else str(self.category)
        return f"{self.account_role.code}[{target}] -> {self.account.code} v{self.version}"

    def covers(self, on_date: datetime.date) -> bool:
        """Whether this mapping is in effect on the given date."""
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to


class InventoryDocumentType(models.TextChoices):
    """Business documents this module numbers. Grows one value per task."""

    OPENING = "INVENTORY_OPENING", _("رصيد افتتاحي")
    RECEIPT = "INVENTORY_RECEIPT", _("استلام مخزني غير مفوتر")
    ISSUE = "INVENTORY_ISSUE", _("صرف مخزني للاستهلاك")
    RETURN_IN = "INVENTORY_RETURN_IN", _("إرجاع من صرف سابق")
    #: Task 1.5. Three numbered documents, because a transfer is a multi-event
    #: aggregate: the transfer itself, each receipt against it, and the
    #: shortage that closes what never arrived.
    TRANSFER = "INVENTORY_TRANSFER", _("تحويل مخزني")
    TRANSFER_RECEIPT = "INVENTORY_TRANSFER_RECEIPT", _("استلام تحويل")
    TRANSFER_SHORTAGE = "INVENTORY_TRANSFER_SHORTAGE", _("إقفال عجز تحويل")
    #: Task 1.6. Waste is an *operational* document and shares
    #: `InventoryMovementDocument` with receipt, issue and return; the count
    #: and the adjustment are their own aggregates and are numbered here only.
    WASTE = "INVENTORY_WASTE", _("إتلاف مخزني")
    STOCK_COUNT = "INVENTORY_STOCK_COUNT", _("جرد فعلي")
    ADJUSTMENT = "INVENTORY_ADJUSTMENT", _("تسوية مخزنية يدوية")


#: The visible prefix each document type numbers with, per business year.
DOCUMENT_NUMBER_PREFIX: dict[str, str] = {
    InventoryDocumentType.OPENING: "OPN",
    InventoryDocumentType.RECEIPT: "RCV",
    InventoryDocumentType.ISSUE: "ISS",
    InventoryDocumentType.RETURN_IN: "RTN",
    InventoryDocumentType.TRANSFER: "TRF",
    InventoryDocumentType.TRANSFER_RECEIPT: "TRR",
    InventoryDocumentType.TRANSFER_SHORTAGE: "TRS",
    InventoryDocumentType.WASTE: "WST",
    InventoryDocumentType.STOCK_COUNT: "CNT",
    InventoryDocumentType.ADJUSTMENT: "ADJ",
}


# --- Transfer source identities (Task 1.5 §P) ------------------------------
#
# Ledger and journal source types, kept apart from `InventoryDocumentType`
# because they name **economic events**, not numbered documents. A transfer
# receipt is one document that releases stock from the source branch and lands
# it at the destination; when the two branches differ those are two postings on
# two business dates with two journals, and each needs its own identity or the
# second would collide with the first on
# `(organization, type, id, event)`.

TRANSFER_DISPATCH_SOURCE_TYPE = "INVENTORY_TRANSFER_DISPATCH"
TRANSFER_RECEIPT_SOURCE_TYPE = "INVENTORY_TRANSFER_RECEIPT_SOURCE"
TRANSFER_RECEIPT_DESTINATION_TYPE = "INVENTORY_TRANSFER_RECEIPT_DESTINATION"
TRANSFER_SHORTAGE_SOURCE_TYPE = "INVENTORY_TRANSFER_SHORTAGE"


# --- Task 1.6 source identities (§Z) ---------------------------------------
#
# Waste posts through `InventoryMovementDocument` and so carries that model's
# own document type as its source type, exactly as a receipt or an issue does.
# The count and the adjustment are separate aggregates and name themselves.
#
# A count produces **one** economic event however many lines vary, because the
# cutoff is one moment and the variance is one balanced journal. There is no
# per-line event to distinguish, so one identity is enough — unlike a
# cross-branch transfer receipt, which is genuinely two postings on two dates.

STOCK_COUNT_SOURCE_TYPE = "INVENTORY_STOCK_COUNT"
MANUAL_ADJUSTMENT_SOURCE_TYPE = "INVENTORY_MANUAL_ADJUSTMENT"


class InventoryDocumentSequence(models.Model):
    """
    Gapless per-organization, per-type, per-fiscal-year document numbering.

    Same shape and same reasoning as `accounting.JournalNumberSequence`: a
    counter row taken under `select_for_update`, because MAX+1 lets two
    concurrent postings claim one number. Drafts hold no number — the number
    is taken at the moment of posting and never before, so an abandoned draft
    cannot burn one.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="inventory_document_sequences",
    )
    document_type = models.CharField(
        _("document type"), max_length=32, choices=InventoryDocumentType.choices
    )
    year = models.PositiveSmallIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = _("inventory document sequence")
        verbose_name_plural = _("inventory document sequences")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type", "year"],
                name="inventory_document_sequence_unique_scope",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} {self.document_type} {self.year}: {self.last_number}"


class OpeningStockStatus(models.TextChoices):
    DRAFT = "DRAFT", _("مسودة")
    SUBMITTED = "SUBMITTED", _("مقدَّم")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class OpeningStockDocument(TimeStampedModel):
    """
    The business document that declares what the stock ledger starts from.

    **Not a receipt.** A receipt records goods arriving from somewhere; an
    opening declares a starting position that predates the ledger, and it is
    the only movement allowed to create quantity for a key with no history.

    Identity is `public_id`, immutable from birth — it is the source document
    id every ledger effect carries. The human `document_number` is display
    metadata, assigned gaplessly at posting and never before, so an abandoned
    draft cannot burn a number.

    Lifecycle: DRAFT → SUBMITTED → POSTED → REVERSED. Maker-checker is a rule
    about the *acts*, not the permissions: the user who submitted cannot be
    the user who posts, even holding both permissions.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="opening_stock_documents",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="opening_stock_documents",
        verbose_name=_("branch"),
    )

    #: The immutable internal identity. THIS is the ledger's
    #: `source_document_id`; the human number below is presentation.
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    document_number = models.CharField(_("document number"), max_length=32, blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=OpeningStockStatus.choices,
        default=OpeningStockStatus.DRAFT,
    )

    #: The declared moment the counted position was true. Timezone-aware and
    #: explicit — an opening without a stated cutoff is an opinion, not a
    #: declaration.
    cutoff_at = models.DateTimeField(_("cutoff at"))
    #: Derived from the cutoff through the branch's timezone and operating-day
    #: start (ADR-008). On a DRAFT this is a preview, recalculated whenever the
    #: cutoff changes. It becomes **authoritative at submission**, together
    #: with the two snapshot fields below, and posting uses what was stored
    #: rather than re-deriving: a branch whose cutoff is changed after a
    #: document was submitted must not silently move that document into a
    #: different accounting period.
    business_date = models.DateField(_("business date"))
    #: The branch settings the authoritative `business_date` was derived with.
    #: Empty while the document is a DRAFT; set at submission; cleared again by
    #: return-to-draft so a resubmission recalculates honestly.
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    #: The count sheet, signed stocktake, or file reference the figures came
    #: from. Required: an opening nobody can trace to evidence is a rumour.
    evidence_reference = models.CharField(_("evidence reference"), max_length=200)
    narration = models.TextField(_("narration"), blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_opening_documents",
        verbose_name=_("created by"),
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submitted_opening_documents",
        verbose_name=_("submitted by"),
    )
    submitted_at = models.DateTimeField(_("submitted at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_opening_documents",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_opening_documents",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    #: Written at posting, inside the same transaction as the effects they
    #: name. The document is the drill-down hub: document → stock entry →
    #: movements, and document → journal entry → lines.
    stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opening_documents",
        verbose_name=_("stock ledger entry"),
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opening_documents",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_opening_documents",
        verbose_name=_("reversal journal entry"),
    )

    class Meta:
        verbose_name = _("opening stock document")
        verbose_name_plural = _("opening stock documents")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(evidence_reference=""),
                name="opening_evidence_reference_not_empty",
            ),
            # A number exists exactly from the moment of posting. Numbering is
            # gapless, and a numbered draft that was abandoned would leave a
            # hole indistinguishable from a deleted document.
            models.CheckConstraint(
                condition=(
                    (
                        Q(status__in=[OpeningStockStatus.DRAFT, OpeningStockStatus.SUBMITTED])
                        & Q(document_number="")
                    )
                    | (
                        Q(status__in=[OpeningStockStatus.POSTED, OpeningStockStatus.REVERSED])
                        & ~Q(document_number="")
                    )
                ),
                name="opening_numbered_iff_posted",
            ),
            models.UniqueConstraint(
                fields=["organization", "document_number"],
                condition=~Q(document_number=""),
                name="opening_number_unique_per_organization",
            ),
            # Everything past DRAFT records who submitted and when.
            models.CheckConstraint(
                condition=Q(status=OpeningStockStatus.DRAFT)
                | (Q(submitted_by__isnull=False) & Q(submitted_at__isnull=False)),
                name="opening_submitted_fields_present",
            ),
            # ...and the business-date snapshot it was committed to, so a
            # submitted document can never be posted against a re-derived date.
            models.CheckConstraint(
                condition=Q(status=OpeningStockStatus.DRAFT)
                | (~Q(business_date_timezone="") & Q(business_day_start__isnull=False)),
                name="opening_business_date_snapshot_present",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=[OpeningStockStatus.POSTED, OpeningStockStatus.REVERSED])
                | (Q(posted_by__isnull=False) & Q(posted_at__isnull=False)),
                name="opening_posted_fields_present",
            ),
            models.CheckConstraint(
                condition=~Q(status=OpeningStockStatus.REVERSED)
                | (
                    Q(reversed_by__isnull=False)
                    & Q(reversed_at__isnull=False)
                    & ~Q(reversal_reason="")
                ),
                name="opening_reversed_fields_present",
            ),
            # Maker-checker, at the database as well as in the service. NULLs
            # pass — the rule binds at the moment both parties exist.
            models.CheckConstraint(
                condition=Q(posted_by__isnull=True)
                | Q(submitted_by__isnull=True)
                | ~Q(posted_by=models.F("submitted_by")),
                name="opening_submitter_is_not_poster",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="opening_org_status_idx"),
            models.Index(fields=["branch", "status"], name="opening_branch_status_idx"),
        ]

    def __str__(self) -> str:
        label = self.document_number or str(self.public_id)
        return f"{label} ({self.get_status_display()})"


class OpeningStockLine(TimeStampedModel):
    """
    One counted position: a quantity of one item, in one warehouse, at a cost.

    `line_uid` is the stable identity the movement's `effect_key` is built
    from — `opening-line:<uid>` — so re-ordering lines in a draft can never
    change what a posted effect claims to be.

    The resolved mapping and account are written at posting and never
    re-resolved: reconciliation reads what *was* resolved, not what today's
    mapping would say.
    """

    document = models.ForeignKey(
        OpeningStockDocument,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("document"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="opening_lines",
        verbose_name=_("warehouse"),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="opening_lines",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        InventoryLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opening_lines",
        verbose_name=_("lot"),
    )

    #: How the quantity was entered, when it came in packages. The conversion
    #: is snapshotted by FK, so a later factor version cannot restate what
    #: this line meant.
    package_conversion = models.ForeignKey(
        ItemPackageConversion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opening_lines",
        verbose_name=_("package conversion"),
    )
    entered_package_quantity = models.DecimalField(
        _("entered package quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    #: The scale reading, for VARIABLE packaging. Authoritative when present.
    measured_base_quantity = models.DecimalField(
        _("measured base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    #: The authoritative counted quantity, in the item's base unit.
    base_quantity = models.DecimalField(
        _("base quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    unit_cost = models.DecimalField(
        _("unit cost"), max_digits=UNIT_PRICE_MAX_DIGITS, decimal_places=UNIT_PRICE_PLACES
    )
    #: `quantize_money(base_quantity × unit_cost)` — the exact figure the
    #: movement and the journal both carry. Stored, never re-derived, so the
    #: three can be compared rather than assumed equal.
    total_value = models.DecimalField(
        _("total value"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )

    # --- Written at posting -----------------------------------------------
    resolved_mapping = models.ForeignKey(
        InventoryAccountMapping,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opening_lines",
        verbose_name=_("resolved inventory mapping"),
    )
    resolved_organization_mapping = models.ForeignKey(
        "accounting.OrganizationAccountMapping",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_opening_lines",
        verbose_name=_("resolved organization mapping"),
    )
    #: The inventory-control account this line's value entered. Immutable
    #: history: reconciliation groups by THIS, never by today's mapping.
    inventory_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opening_stock_lines",
        verbose_name=_("inventory account"),
    )
    movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opening_line",
        verbose_name=_("stock movement"),
    )
    #: The grouped debit line this value is inside. Several lines resolving to
    #: one account legitimately share one journal line.
    journal_line = models.ForeignKey(
        "accounting.JournalLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="opening_lines",
        verbose_name=_("journal line"),
    )

    class Meta:
        verbose_name = _("opening stock line")
        verbose_name_plural = _("opening stock lines")
        ordering = ["document_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "sequence"], name="opening_line_sequence_unique"
            ),
            # One valuation key per document. Two lines for one shelf is two
            # claims about one starting position, and `nulls_distinct=False`
            # keeps the lot-less case honest exactly as StockBalance does.
            models.UniqueConstraint(
                fields=["document", "warehouse", "item", "lot"],
                nulls_distinct=False,
                name="opening_line_valuation_key_unique",
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=Decimal("0")),
                name="opening_line_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__gt=Decimal("0")),
                name="opening_line_unit_cost_is_positive",
            ),
            # Positive quantity at zero value is refused: free stock on the
            # books understates cost of sales for as long as it lasts.
            models.CheckConstraint(
                condition=Q(total_value__gt=Decimal("0")),
                name="opening_line_value_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(entered_package_quantity__isnull=True)
                | Q(entered_package_quantity__gt=Decimal("0")),
                name="opening_line_package_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(measured_base_quantity__gt=Decimal("0")),
                name="opening_line_measured_positive",
            ),
            # A measured weight makes sense only against a package entry.
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(package_conversion__isnull=False),
                name="opening_line_measured_needs_package",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_id}#{self.sequence}: {self.item_id} {self.base_quantity}"


# ===========================================================================
# Task 1.4 — operational documents: receipt, issue, return-in
# ===========================================================================
#
# **One model with a type discriminator, not three.** The three documents
# share their whole lifecycle — draft, post, reverse — their numbering, their
# source-identity shape, their locking order, their scope resolution, their
# API surface, and their screens. What differs is per *line*: a receipt
# carries an entered cost, an issue carries none and takes its value from the
# moving average, and a return carries neither and takes its value from the
# issue it returns against.
#
# Three models would triple the lifecycle machinery to vary the smaller half,
# and a defect fixed in one copy would live on in the other two. The
# conditional invariants stay legible because each is a check constraint keyed
# on `document_type`, listed together where they can be read against each
# other rather than scattered across three files.
#
# The opening document stays separate: it is genuinely a different shape —
# maker-checker, no direct-from-draft posting, and the only movement type
# permitted to create quantity for a key with no history.


class InventoryDocumentStatus(models.TextChoices):
    """
    Draft, posted, reversed.

    No SUBMITTED step: a receipt and an issue are custody acts by the person
    holding the goods, and the approved role map already trusts a storekeeper
    with them. Maker-checker belongs to opening stock, which declares what the
    ledger starts from rather than moving what is already in it.
    """

    DRAFT = "DRAFT", _("مسودة")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class InventoryMovementDocument(TimeStampedModel):
    """
    A receipt, an issue, or a return of previously issued stock.

    One warehouse per document, one business date, one cost centre where the
    accounting needs one. `public_id` is the immutable identity every ledger
    effect carries; `document_number` is display metadata, gapless within the
    business year and assigned only at posting.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="inventory_documents",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="inventory_documents",
        verbose_name=_("branch"),
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="inventory_documents",
        verbose_name=_("warehouse"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    document_number = models.CharField(_("document number"), max_length=32, blank=True)
    document_type = models.CharField(
        _("document type"), max_length=32, choices=InventoryDocumentType.choices
    )
    status = models.CharField(
        _("status"),
        max_length=10,
        choices=InventoryDocumentStatus.choices,
        default=InventoryDocumentStatus.DRAFT,
    )

    #: The physical moment the goods moved.
    effective_at = models.DateTimeField(_("effective at"))
    #: The operational day it belongs to. Snapshotted at posting together with
    #: the branch settings that derived it, so a later cutoff change cannot
    #: move a posted document between periods (ADR-008).
    business_date = models.DateField(_("business date"))
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    evidence_reference = models.CharField(
        _("evidence reference"),
        max_length=200,
        help_text=_("Delivery note, requisition, or return slip. Required."),
    )
    narration = models.TextField(_("narration"), blank=True)

    #: Where consumption lands, for an issue. Snapshotted onto the journal
    #: lines at posting; a return reuses the original issue's rather than
    #: today's, so the pair nets to zero in the same managerial bucket.
    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_documents",
        verbose_name=_("cost center"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_inventory_documents",
        verbose_name=_("created by"),
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_inventory_documents",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_inventory_documents",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_documents",
        verbose_name=_("stock ledger entry"),
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_documents",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_inventory_documents",
        verbose_name=_("reversal journal entry"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("inventory movement document")
        verbose_name_plural = _("inventory movement documents")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(evidence_reference=""),
                name="inventory_document_evidence_reference_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(
                    document_type__in=[
                        InventoryDocumentType.RECEIPT,
                        InventoryDocumentType.ISSUE,
                        InventoryDocumentType.RETURN_IN,
                        # Task 1.6. Waste is an operational custody act like the
                        # other three — one warehouse, one business date, one
                        # posting, one reversal — and shares all of their
                        # machinery. What is different about it is per line.
                        InventoryDocumentType.WASTE,
                    ]
                ),
                name="inventory_document_type_is_operational",
            ),
            # A number exists exactly from the moment of posting. Numbering is
            # gapless, and a numbered draft that was abandoned would leave a
            # hole indistinguishable from a deleted document.
            models.CheckConstraint(
                condition=(
                    (Q(status=InventoryDocumentStatus.DRAFT) & Q(document_number=""))
                    | (~Q(status=InventoryDocumentStatus.DRAFT) & ~Q(document_number=""))
                ),
                name="inventory_document_numbered_iff_posted",
            ),
            models.UniqueConstraint(
                fields=["organization", "document_number"],
                condition=~Q(document_number=""),
                name="inventory_document_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(status=InventoryDocumentStatus.DRAFT)
                | (Q(posted_by__isnull=False) & Q(posted_at__isnull=False)),
                name="inventory_document_posted_fields_present",
            ),
            # ...and the business-date snapshot it was posted against.
            models.CheckConstraint(
                condition=Q(status=InventoryDocumentStatus.DRAFT)
                | (~Q(business_date_timezone="") & Q(business_day_start__isnull=False)),
                name="inventory_document_business_date_snapshot_present",
            ),
            models.CheckConstraint(
                condition=~Q(status=InventoryDocumentStatus.REVERSED)
                | (
                    Q(reversed_by__isnull=False)
                    & Q(reversed_at__isnull=False)
                    & ~Q(reversal_reason="")
                ),
                name="inventory_document_reversed_fields_present",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "document_type", "status"],
                name="inv_doc_org_type_status_idx",
            ),
            models.Index(fields=["warehouse", "business_date"], name="inv_doc_wh_date_idx"),
        ]

    def __str__(self) -> str:
        label = self.document_number or str(self.public_id)
        return f"{label} ({self.get_status_display()})"

    @property
    def source_document_type(self) -> str:
        """The ledger source type — the document type is already exactly it."""
        return str(self.document_type)


class InventoryMovementDocumentLine(TimeStampedModel):
    """
    One item on a receipt, issue, or return.

    `line_uid` is the stable identity the movement's `effect_key` is built
    from, so reordering a draft's lines can never change what a posted effect
    claims to be.

    The three cost stories, in one place:

    * **Receipt** — the operator enters `unit_cost`; `total_value` follows
      from it and the base quantity.
    * **Issue** — nothing is entered. Both are written at posting from the
      moving average the kernel computed, which is the only cost that exists.
    * **Return** — nothing is entered either. Both come from the issue being
      returned against, so the pair nets to zero however the average has moved
      since.
    """

    document = models.ForeignKey(
        InventoryMovementDocument,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("document"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="document_lines",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        InventoryLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="document_lines",
        verbose_name=_("lot"),
    )

    package_conversion = models.ForeignKey(
        ItemPackageConversion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="document_lines",
        verbose_name=_("package conversion"),
    )
    entered_package_quantity = models.DecimalField(
        _("entered package quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    measured_base_quantity = models.DecimalField(
        _("measured base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    base_quantity = models.DecimalField(
        _("base quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )

    #: Entered for a receipt; written at posting for an issue or a return.
    unit_cost = models.DecimalField(
        _("unit cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        null=True,
        blank=True,
    )
    total_value = models.DecimalField(
        _("total value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    #: Why this stock was destroyed. Required on a WASTE line and forbidden
    #: elsewhere: a receipt, an issue and a return each already say why they
    #: exist, and offering a reason code on them would invite one to be
    #: recorded where nothing reads it.
    reason_code = models.ForeignKey(
        "inventory.InventoryReasonCode",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="waste_lines",
        verbose_name=_("reason code"),
    )
    #: Mandatory when the chosen reason code says so — which is what makes an
    #: "other" reason usable without making it a hole in the record.
    line_comment = models.CharField(_("comment"), max_length=200, blank=True)

    #: The issue line this return goes back against. Required on a RETURN_IN
    #: and forbidden elsewhere — a return with no original has no cost to take
    #: and no quantity to be bounded by.
    source_issue_line = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="return_lines",
        verbose_name=_("source issue line"),
    )

    # --- Written at posting -----------------------------------------------
    #: The inventory-control account this line's value entered or left.
    inventory_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_document_lines",
        verbose_name=_("inventory account"),
    )
    #: The other side: GRNI for a receipt, the consumption account for an
    #: issue, and for a return the consumption account **the original issue
    #: used** — never a fresh resolution.
    contra_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_document_contra_lines",
        verbose_name=_("contra account"),
    )
    #: The cost centre the contra side was posted with, snapshotted so a
    #: return can reuse it exactly.
    contra_cost_center = models.ForeignKey(
        "accounting.CostCenter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_document_lines",
        verbose_name=_("contra cost center"),
    )
    resolved_mapping = models.ForeignKey(
        InventoryAccountMapping,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="document_lines",
        verbose_name=_("resolved inventory mapping"),
    )
    resolved_organization_mapping = models.ForeignKey(
        "accounting.OrganizationAccountMapping",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_document_lines",
        verbose_name=_("resolved organization mapping"),
    )
    movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="document_line",
        verbose_name=_("stock movement"),
    )
    journal_line = models.ForeignKey(
        "accounting.JournalLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_document_lines",
        verbose_name=_("journal line"),
    )

    class Meta:
        verbose_name = _("inventory document line")
        verbose_name_plural = _("inventory document lines")
        ordering = ["document_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "sequence"], name="inventory_document_line_sequence_unique"
            ),
            # One valuation key per document. Two lines for one shelf is two
            # claims about one position, and `nulls_distinct=False` keeps the
            # lot-less case honest exactly as StockBalance does.
            models.UniqueConstraint(
                fields=["document", "item", "lot"],
                nulls_distinct=False,
                name="inventory_document_line_valuation_key_unique",
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=Decimal("0")),
                name="inventory_document_line_quantity_is_positive",
            ),
            # Null until posting for an issue or a return; never zero or
            # negative once written. Positive quantity at zero value would put
            # free stock on the books.
            models.CheckConstraint(
                condition=Q(unit_cost__isnull=True) | Q(unit_cost__gt=Decimal("0")),
                name="inventory_document_line_unit_cost_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(total_value__isnull=True) | Q(total_value__gt=Decimal("0")),
                name="inventory_document_line_value_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(entered_package_quantity__isnull=True)
                | Q(entered_package_quantity__gt=Decimal("0")),
                name="inventory_document_line_package_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(measured_base_quantity__gt=Decimal("0")),
                name="inventory_document_line_measured_positive",
            ),
            # A measured weight makes sense only against a package entry.
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(package_conversion__isnull=False),
                name="inventory_document_line_measured_needs_package",
            ),
            # A reason code belongs to a waste line and to no other. The
            # document type lives on the parent, so this is expressed the way
            # the model's other cross-row rules are — by trigger, in migration
            # `0015`, where the parent's type can actually be read.
        ]

    def __str__(self) -> str:
        return f"{self.document_id}#{self.sequence}: {self.item_id} {self.base_quantity}"


# ===========================================================================
# Task 1.5 — transfers, in-transit stock, partial receipts and shortages
# ===========================================================================
#
# **Not an `InventoryMovementDocument`.** That model is one draft that becomes
# one posted or reversed event, and every part of it — the single status, the
# single stock entry, the single journal, the whole-document reversal — assumes
# exactly that. A transfer is a multi-event aggregate: it is dispatched once,
# received any number of times, possibly closed short, and each of those
# individual events can later be reversed on its own without undoing the
# others. Forcing it into the one-post shape would mean either a status that
# lies about how much has arrived or a second hidden document nobody can see.
#
# So: a parent aggregate whose status is *computed* from its posted children,
# and two child event models that each behave like a small posted document.
#
#     StockTransfer            the agreement: what leaves, from where, to where
#     StockTransferLine        one item on it, with its own remaining balance
#     StockTransferReceipt     one arrival event, whole or partial
#     StockTransferShortage    the closure of what will never arrive
#
# Ownership never moves until receipt. Goods sit in the **source branch's**
# in-transit warehouse from dispatch until each receipt takes its share out,
# which is both the accounting truth and the answer to "whose loss is it if
# the lorry never turns up" (ADR-020 §1).


class StockTransferStatus(models.TextChoices):
    """
    Where a transfer has got to, computed from its posted children.

    Never set by a caller and never edited directly: the value is derived from
    what has actually been posted against the transfer, so it cannot claim an
    arrival that no receipt records or hide one that does.
    """

    DRAFT = "DRAFT", _("مسودة")
    DISPATCHED = "DISPATCHED", _("مُرسل")
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", _("مستلم جزئياً")
    COMPLETED = "COMPLETED", _("مكتمل")
    CLOSED_WITH_SHORTAGE = "CLOSED_WITH_SHORTAGE", _("مقفل بعجز")
    REVERSED = "REVERSED", _("معكوس")


#: The statuses at which stock is standing in transit against the transfer.
OPEN_TRANSFER_STATUSES = frozenset(
    {StockTransferStatus.DISPATCHED, StockTransferStatus.PARTIALLY_RECEIVED}
)


class StockTransfer(TimeStampedModel):
    """
    Goods moving from one warehouse to another inside one organization.

    Cross-organization movement is prohibited outright and is not modelled
    here: two organizations are two sets of books, and goods crossing between
    them is a sale and a purchase, not an internal transfer (§F).

    `public_id` is the immutable identity every ledger effect carries;
    `transfer_number` is display metadata, gapless within the business year and
    assigned only at dispatch — a draft that is abandoned must not burn one.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="stock_transfers",
        verbose_name=_("organization"),
    )
    source_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="outgoing_transfers",
        verbose_name=_("source warehouse"),
    )
    destination_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="incoming_transfers",
        verbose_name=_("destination warehouse"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    transfer_number = models.CharField(_("transfer number"), max_length=32, blank=True)
    status = models.CharField(
        _("status"),
        max_length=24,
        choices=StockTransferStatus.choices,
        default=StockTransferStatus.DRAFT,
    )

    evidence_reference = models.CharField(
        _("evidence reference"),
        max_length=200,
        help_text=_("Transfer note or gate pass. Required."),
    )
    narration = models.TextField(_("narration"), blank=True)

    #: The physical moment the goods left, and the source branch's operating
    #: day it belongs to, with the settings that derived it. Snapshotted at
    #: dispatch: a cutoff changed afterwards cannot move a dispatched transfer
    #: into a different accounting period (ADR-008).
    effective_at = models.DateTimeField(_("effective at"))
    business_date = models.DateField(_("business date"))
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_stock_transfers",
        verbose_name=_("created by"),
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dispatched_stock_transfers",
        verbose_name=_("dispatched by"),
    )
    dispatched_at = models.DateTimeField(_("dispatched at"), null=True, blank=True)

    stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dispatched_transfers",
        verbose_name=_("dispatch stock entry"),
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dispatched_transfers",
        verbose_name=_("dispatch journal entry"),
    )

    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_stock_transfers",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_stock_transfers",
        verbose_name=_("dispatch reversal journal entry"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("stock transfer")
        verbose_name_plural = _("stock transfers")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(evidence_reference=""),
                name="stock_transfer_evidence_reference_not_empty",
            ),
            # Goods cannot be transferred to where they already are. The
            # organization and system-warehouse halves of this rule need the
            # warehouse rows, so they live in the trigger beside it.
            models.CheckConstraint(
                condition=~Q(source_warehouse=models.F("destination_warehouse")),
                name="stock_transfer_warehouses_differ",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(status=StockTransferStatus.DRAFT) & Q(transfer_number=""))
                    | (~Q(status=StockTransferStatus.DRAFT) & ~Q(transfer_number=""))
                ),
                name="stock_transfer_numbered_iff_dispatched",
            ),
            models.UniqueConstraint(
                fields=["organization", "transfer_number"],
                condition=~Q(transfer_number=""),
                name="stock_transfer_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(status=StockTransferStatus.DRAFT)
                | (Q(dispatched_by__isnull=False) & Q(dispatched_at__isnull=False)),
                name="stock_transfer_dispatched_fields_present",
            ),
            models.CheckConstraint(
                condition=Q(status=StockTransferStatus.DRAFT)
                | (~Q(business_date_timezone="") & Q(business_day_start__isnull=False)),
                name="stock_transfer_business_date_snapshot_present",
            ),
            models.CheckConstraint(
                condition=~Q(status=StockTransferStatus.REVERSED)
                | (
                    Q(reversed_by__isnull=False)
                    & Q(reversed_at__isnull=False)
                    & ~Q(reversal_reason="")
                ),
                name="stock_transfer_reversed_fields_present",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="transfer_org_status_idx"),
            models.Index(
                fields=["source_warehouse", "business_date"], name="transfer_source_date_idx"
            ),
            models.Index(fields=["destination_warehouse", "status"], name="transfer_dest_idx"),
        ]

    def __str__(self) -> str:
        label = self.transfer_number or str(self.public_id)
        return f"{label} ({self.get_status_display()})"

    @property
    def source_branch_id(self) -> int:
        return int(self.source_warehouse.branch_id)

    @property
    def destination_branch_id(self) -> int:
        return int(self.destination_warehouse.branch_id)

    @property
    def is_cross_branch(self) -> bool:
        """Whether the two ends sit in different branches, which decides the
        accounting shape: one branch-local journal, or two coordinated ones
        through inter-branch clearing (ADR-020 §8)."""
        return self.source_branch_id != self.destination_branch_id


class StockTransferLine(TimeStampedModel):
    """
    One item on a transfer, and its own running balance.

    `remaining_quantity` and `remaining_value` are **retained**, not derived on
    read. Derived-only would make the value allocation of §J a race — two
    concurrent receipts would each compute the same remaining basis — and would
    leave the database unable to state the invariant at all. They are
    maintained under the transfer's row lock, and reconciliation derives the
    same figures independently and compares, which is what makes retaining
    them safe rather than merely convenient.
    """

    transfer = models.ForeignKey(
        StockTransfer,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("transfer"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="transfer_lines",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        InventoryLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_lines",
        verbose_name=_("lot"),
    )

    package_conversion = models.ForeignKey(
        ItemPackageConversion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_lines",
        verbose_name=_("package conversion"),
    )
    entered_package_quantity = models.DecimalField(
        _("entered package quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    measured_base_quantity = models.DecimalField(
        _("measured base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    #: The dispatched quantity, in the item's base unit.
    base_quantity = models.DecimalField(
        _("base quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )

    #: Written at dispatch from the source position's moving average — the
    #: cost the goods actually left at, which every later receipt allocates
    #: from and never re-derives.
    unit_cost = models.DecimalField(
        _("dispatch unit cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        null=True,
        blank=True,
    )
    total_value = models.DecimalField(
        _("dispatched value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    #: What is still standing in transit against this line. Zero before
    #: dispatch and zero again once every unit is received or written off.
    remaining_quantity = models.DecimalField(
        _("remaining quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        default=Decimal("0"),
    )
    remaining_value = models.DecimalField(
        _("remaining value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )

    # --- Written at dispatch ----------------------------------------------
    source_movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_source_line",
        verbose_name=_("source warehouse movement"),
    )
    transit_movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_transit_line",
        verbose_name=_("in-transit movement"),
    )
    #: The account the goods left, and the one they are standing in. Both
    #: snapshotted so a mapping changed mid-transit cannot restate either.
    source_control_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_source_lines",
        verbose_name=_("source control account"),
    )
    transit_control_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_transit_lines",
        verbose_name=_("in-transit control account"),
    )
    journal_line = models.ForeignKey(
        "accounting.JournalLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_lines",
        verbose_name=_("journal line"),
    )

    class Meta:
        verbose_name = _("stock transfer line")
        verbose_name_plural = _("stock transfer lines")
        ordering = ["transfer_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "sequence"], name="transfer_line_sequence_unique"
            ),
            # One valuation key per transfer. The source warehouse is fixed by
            # the header, so `(item, lot)` is the whole key, and splitting one
            # physical position across two lines would give each its own
            # remaining balance for stock that has only one.
            models.UniqueConstraint(
                fields=["transfer", "item", "lot"],
                nulls_distinct=False,
                name="transfer_line_valuation_key_unique",
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=Decimal("0")),
                name="transfer_line_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__isnull=True) | Q(unit_cost__gt=Decimal("0")),
                name="transfer_line_unit_cost_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(total_value__isnull=True) | Q(total_value__gt=Decimal("0")),
                name="transfer_line_value_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(entered_package_quantity__isnull=True)
                | Q(entered_package_quantity__gt=Decimal("0")),
                name="transfer_line_package_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(measured_base_quantity__gt=Decimal("0")),
                name="transfer_line_measured_positive",
            ),
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(package_conversion__isnull=False),
                name="transfer_line_measured_needs_package",
            ),
            # Nothing may be received or written off that was never dispatched.
            models.CheckConstraint(
                condition=Q(remaining_quantity__gte=Decimal("0"))
                & Q(remaining_quantity__lte=models.F("base_quantity")),
                name="transfer_line_remaining_quantity_within_dispatch",
            ),
            models.CheckConstraint(
                condition=Q(remaining_value__gte=Decimal("0"))
                & (Q(total_value__isnull=True) | Q(remaining_value__lte=models.F("total_value"))),
                name="transfer_line_remaining_value_within_dispatch",
            ),
            # Quantity and value empty together or not at all. Value against
            # no quantity is stock nobody can ever receive; quantity at no
            # value is stock that would arrive free.
            models.CheckConstraint(
                condition=(
                    (Q(remaining_quantity=Decimal("0")) & Q(remaining_value=Decimal("0")))
                    | (Q(remaining_quantity__gt=Decimal("0")) & Q(remaining_value__gt=Decimal("0")))
                ),
                name="transfer_line_remaining_quantity_and_value_agree",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.transfer_id}#{self.sequence}: {self.item_id} {self.base_quantity}"


class StockTransferReceipt(TimeStampedModel):
    """
    One arrival against a transfer — the whole consignment or part of it.

    A transfer may have many. Each is its own posted event with its own
    business dates, its own stock postings and its own journals, and each can
    be reversed on its own without disturbing the others (§E).

    **Two business dates, deliberately.** The source branch releases the goods
    from its in-transit stock on *its* operating day and the destination takes
    them onto its books on *its* own; the two may differ, and forcing them
    together would date one branch's books by another branch's clock. Each side
    validates its own accounting period and both roll back together (§H).
    """

    transfer = models.ForeignKey(
        StockTransfer,
        on_delete=models.PROTECT,
        related_name="receipts",
        verbose_name=_("transfer"),
    )
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    receipt_number = models.CharField(_("receipt number"), max_length=32, blank=True)
    status = models.CharField(
        _("status"),
        max_length=10,
        choices=InventoryDocumentStatus.choices,
        default=InventoryDocumentStatus.DRAFT,
    )

    evidence_reference = models.CharField(
        _("evidence reference"),
        max_length=200,
        help_text=_("Goods-received note at the destination. Required."),
    )
    narration = models.TextField(_("narration"), blank=True)

    effective_at = models.DateTimeField(_("effective at"))
    #: The destination branch's operating day, and its settings.
    business_date = models.DateField(_("business date"))
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)
    #: The source branch's operating day for the in-transit release. Equal to
    #: `business_date` for a same-branch transfer and free to differ otherwise.
    source_business_date = models.DateField(_("source business date"), null=True, blank=True)
    source_business_date_timezone = models.CharField(
        _("source business date timezone"), max_length=64, blank=True
    )
    source_business_day_start = models.TimeField(
        _("source business day start"), null=True, blank=True
    )

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="received_transfer_receipts",
        verbose_name=_("received by"),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_transfer_receipts",
        verbose_name=_("created by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)

    #: The release from in-transit and the arrival at the destination. Two
    #: postings always, because they may fall on two business dates and a
    #: ledger entry carries exactly one.
    source_stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_receipt_releases",
        verbose_name=_("in-transit release entry"),
    )
    destination_stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_receipt_arrivals",
        verbose_name=_("destination arrival entry"),
    )
    #: One journal when both ends are in one branch — both fields then name the
    #: same row — and two coordinated ones when they are not, each balanced
    #: inside its own branch through inter-branch clearing (ADR-020 §9).
    source_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_receipt_source_journals",
        verbose_name=_("source journal entry"),
    )
    destination_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_receipt_destination_journals",
        verbose_name=_("destination journal entry"),
    )

    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_transfer_receipts",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)
    source_reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_transfer_receipt_source_journals",
        verbose_name=_("source reversal journal entry"),
    )
    destination_reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_transfer_receipt_destination_journals",
        verbose_name=_("destination reversal journal entry"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("stock transfer receipt")
        verbose_name_plural = _("stock transfer receipts")
        ordering = ["transfer_id", "-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(evidence_reference=""),
                name="transfer_receipt_evidence_reference_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(status=InventoryDocumentStatus.DRAFT) & Q(receipt_number=""))
                    | (~Q(status=InventoryDocumentStatus.DRAFT) & ~Q(receipt_number=""))
                ),
                name="transfer_receipt_numbered_iff_posted",
            ),
            models.UniqueConstraint(
                fields=["receipt_number"],
                condition=~Q(receipt_number=""),
                name="transfer_receipt_number_unique",
            ),
            models.CheckConstraint(
                condition=Q(status=InventoryDocumentStatus.DRAFT)
                | (Q(received_by__isnull=False) & Q(posted_at__isnull=False)),
                name="transfer_receipt_posted_fields_present",
            ),
            # Both snapshots, because both branches' periods were validated.
            models.CheckConstraint(
                condition=Q(status=InventoryDocumentStatus.DRAFT)
                | (
                    ~Q(business_date_timezone="")
                    & Q(business_day_start__isnull=False)
                    & Q(source_business_date__isnull=False)
                    & ~Q(source_business_date_timezone="")
                    & Q(source_business_day_start__isnull=False)
                ),
                name="transfer_receipt_business_date_snapshots_present",
            ),
            models.CheckConstraint(
                condition=~Q(status=InventoryDocumentStatus.REVERSED)
                | (
                    Q(reversed_by__isnull=False)
                    & Q(reversed_at__isnull=False)
                    & ~Q(reversal_reason="")
                ),
                name="transfer_receipt_reversed_fields_present",
            ),
        ]
        indexes = [
            models.Index(fields=["transfer", "status"], name="transfer_receipt_status_idx"),
        ]

    def __str__(self) -> str:
        label = self.receipt_number or str(self.public_id)
        return f"{label} ({self.get_status_display()})"


class StockTransferReceiptLine(TimeStampedModel):
    """
    How much of one transfer line this receipt took, and at what value.

    `allocated_value` is not `quantity x anything resolved now`. It is the
    share of the transfer line's own remaining value that this receipt
    consumes, computed by the rule in ADR-020 §5, and the last receipt takes
    the exact remainder so that the receipts plus any shortage sum to the
    dispatched value to the dinar.
    """

    receipt = models.ForeignKey(
        StockTransferReceipt,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("receipt"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))
    transfer_line = models.ForeignKey(
        StockTransferLine,
        on_delete=models.PROTECT,
        related_name="receipt_lines",
        verbose_name=_("transfer line"),
    )

    package_conversion = models.ForeignKey(
        ItemPackageConversion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_receipt_lines",
        verbose_name=_("package conversion"),
    )
    entered_package_quantity = models.DecimalField(
        _("entered package quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    measured_base_quantity = models.DecimalField(
        _("measured base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    base_quantity = models.DecimalField(
        _("received base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
    )
    allocated_value = models.DecimalField(
        _("allocated value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    unit_cost = models.DecimalField(
        _("unit cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        null=True,
        blank=True,
    )

    transit_movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_receipt_release_line",
        verbose_name=_("in-transit movement"),
    )
    destination_movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_receipt_arrival_line",
        verbose_name=_("destination movement"),
    )
    destination_control_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_receipt_lines",
        verbose_name=_("destination control account"),
    )

    class Meta:
        verbose_name = _("stock transfer receipt line")
        verbose_name_plural = _("stock transfer receipt lines")
        ordering = ["receipt_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["receipt", "sequence"], name="transfer_receipt_line_sequence_unique"
            ),
            # One line per transfer line per receipt: two would each allocate
            # against a remaining balance the other had already spent.
            models.UniqueConstraint(
                fields=["receipt", "transfer_line"],
                name="transfer_receipt_line_target_unique",
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=Decimal("0")),
                name="transfer_receipt_line_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(allocated_value__isnull=True) | Q(allocated_value__gt=Decimal("0")),
                name="transfer_receipt_line_value_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__isnull=True) | Q(unit_cost__gt=Decimal("0")),
                name="transfer_receipt_line_unit_cost_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(entered_package_quantity__isnull=True)
                | Q(entered_package_quantity__gt=Decimal("0")),
                name="transfer_receipt_line_package_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(measured_base_quantity__gt=Decimal("0")),
                name="transfer_receipt_line_measured_positive",
            ),
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(package_conversion__isnull=False),
                name="transfer_receipt_line_measured_needs_package",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.receipt_id}#{self.sequence}: {self.base_quantity}"


class StockTransferShortage(TimeStampedModel):
    """
    The closure of everything a transfer will never deliver.

    The loss belongs to the **source** branch, because that is where the goods
    still are on the books, and it needs a reason, a cost centre, an authorized
    actor and a sensitive permission of its own — turning missing stock into an
    expense is not a custody act (§F, §G).

    A closure resolves the *entire* remaining quantity. A partial write-off
    leaving an unexplained open residual is not modelled: a transfer that is
    neither fully received nor fully accounted for is exactly the state this
    document exists to end.
    """

    transfer = models.ForeignKey(
        StockTransfer,
        on_delete=models.PROTECT,
        related_name="shortages",
        verbose_name=_("transfer"),
    )
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    shortage_number = models.CharField(_("shortage number"), max_length=32, blank=True)
    status = models.CharField(
        _("status"),
        max_length=10,
        choices=InventoryDocumentStatus.choices,
        default=InventoryDocumentStatus.DRAFT,
    )

    #: Why the goods are missing. Never blank: an unexplained inventory loss
    #: posted to an expense account is indistinguishable from theft.
    reason = models.TextField(_("reason"))
    evidence_reference = models.CharField(
        _("evidence reference"),
        max_length=200,
        help_text=_("Investigation note, police report, or carrier claim. Required."),
    )
    #: Where the loss lands managerially. Explicitly chosen, never defaulted to
    #: Warehouse or Administration — which department carries a loss is a
    #: decision, and a hard-coded answer would make every branch's cost report
    #: agree by construction rather than by fact.
    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        on_delete=models.PROTECT,
        related_name="transfer_shortages",
        verbose_name=_("cost center"),
    )

    effective_at = models.DateTimeField(_("effective at"))
    business_date = models.DateField(_("business date"))
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_transfer_shortages",
        verbose_name=_("created by"),
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closed_transfer_shortages",
        verbose_name=_("closed by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)

    stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_shortages",
        verbose_name=_("stock entry"),
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_shortages",
        verbose_name=_("journal entry"),
    )

    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_transfer_shortages",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_transfer_shortages",
        verbose_name=_("reversal journal entry"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("stock transfer shortage")
        verbose_name_plural = _("stock transfer shortages")
        ordering = ["transfer_id", "-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(reason=""), name="transfer_shortage_reason_present"
            ),
            models.CheckConstraint(
                condition=~Q(evidence_reference=""),
                name="transfer_shortage_evidence_reference_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(status=InventoryDocumentStatus.DRAFT) & Q(shortage_number=""))
                    | (~Q(status=InventoryDocumentStatus.DRAFT) & ~Q(shortage_number=""))
                ),
                name="transfer_shortage_numbered_iff_posted",
            ),
            models.UniqueConstraint(
                fields=["shortage_number"],
                condition=~Q(shortage_number=""),
                name="transfer_shortage_number_unique",
            ),
            # At most one *active* closure per transfer. A reversed one leaves
            # the transfer open again and a fresh closure is then legitimate,
            # so the index covers POSTED alone — and it is what stops two
            # concurrent closures from both writing off the same goods.
            models.UniqueConstraint(
                fields=["transfer"],
                condition=Q(status=InventoryDocumentStatus.POSTED),
                name="transfer_shortage_one_active_per_transfer",
            ),
            models.CheckConstraint(
                condition=Q(status=InventoryDocumentStatus.DRAFT)
                | (Q(closed_by__isnull=False) & Q(posted_at__isnull=False)),
                name="transfer_shortage_posted_fields_present",
            ),
            models.CheckConstraint(
                condition=Q(status=InventoryDocumentStatus.DRAFT)
                | (~Q(business_date_timezone="") & Q(business_day_start__isnull=False)),
                name="transfer_shortage_business_date_snapshot_present",
            ),
            models.CheckConstraint(
                condition=~Q(status=InventoryDocumentStatus.REVERSED)
                | (
                    Q(reversed_by__isnull=False)
                    & Q(reversed_at__isnull=False)
                    & ~Q(reversal_reason="")
                ),
                name="transfer_shortage_reversed_fields_present",
            ),
        ]
        indexes = [
            models.Index(fields=["transfer", "status"], name="transfer_shortage_status_idx"),
        ]

    def __str__(self) -> str:
        label = self.shortage_number or str(self.public_id)
        return f"{label} ({self.get_status_display()})"


class StockTransferShortageLine(TimeStampedModel):
    """One transfer line's missing quantity, at its exact remaining value."""

    shortage = models.ForeignKey(
        StockTransferShortage,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("shortage"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))
    transfer_line = models.ForeignKey(
        StockTransferLine,
        on_delete=models.PROTECT,
        related_name="shortage_lines",
        verbose_name=_("transfer line"),
    )

    base_quantity = models.DecimalField(
        _("shortage quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
    )
    allocated_value = models.DecimalField(
        _("allocated value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    unit_cost = models.DecimalField(
        _("unit cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        null=True,
        blank=True,
    )

    transit_movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_shortage_line",
        verbose_name=_("in-transit movement"),
    )
    journal_line = models.ForeignKey(
        "accounting.JournalLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_shortage_lines",
        verbose_name=_("journal line"),
    )

    class Meta:
        verbose_name = _("stock transfer shortage line")
        verbose_name_plural = _("stock transfer shortage lines")
        ordering = ["shortage_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["shortage", "sequence"], name="transfer_shortage_line_sequence_unique"
            ),
            models.UniqueConstraint(
                fields=["shortage", "transfer_line"],
                name="transfer_shortage_line_target_unique",
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=Decimal("0")),
                name="transfer_shortage_line_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(allocated_value__isnull=True) | Q(allocated_value__gt=Decimal("0")),
                name="transfer_shortage_line_value_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__isnull=True) | Q(unit_cost__gt=Decimal("0")),
                name="transfer_shortage_line_unit_cost_is_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.shortage_id}#{self.sequence}: {self.base_quantity}"


# ===========================================================================
# Task 1.6 — reason codes, waste, physical counts, manual adjustments
# ===========================================================================
#
# Three shapes, chosen by what each thing actually is rather than by what is
# nearest to hand:
#
#   WASTE           extends `InventoryMovementDocument`. Its lifecycle, its
#                   numbering, its source-identity shape, its locking, its
#                   scope resolution and its screens are the issue's; what
#                   differs is one movement type, one journal side, and two
#                   per-line fields. That is exactly the variation the type
#                   discriminator was introduced to carry.
#
#   STOCK COUNT     its own aggregate. A count is not one posting: it freezes a
#                   warehouse, snapshots a book position, is entered blind by
#                   one person, submitted, approved by a different person, and
#                   only then posts. Most of those steps have no analogue in a
#                   one-post document, and three of them exist precisely to
#                   keep two people's authority apart.
#
#   ADJUSTMENT      its own aggregate, for one specific reason: a single
#                   adjustment carries lines that go in **different
#                   directions** — a gain, a loss, and a pure revaluation that
#                   moves no quantity at all. `InventoryMovementDocument` maps
#                   one document type to exactly one movement type, and making
#                   that per-line would push the discriminator down into the
#                   lines, which is the leak Task 1.4 avoided by keeping the
#                   type on the document.


class ReasonCodeApplication(models.TextChoices):
    """
    What a reason code may be selected for. Closed.

    Deliberately **not** a list of restaurant reasons. Spoilage, breakage,
    over-portioning and theft are one organization's vocabulary; another
    group's would differ, and a code baked into an enum could never be retired
    without a migration. What is closed is the set of *documents* a reason can
    attach to, because that is a property of this software.
    """

    WASTE = "WASTE", _("إتلاف")
    COUNT_VARIANCE = "COUNT_VARIANCE", _("فروقات الجرد")
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT", _("تسوية يدوية")


class InventoryReasonCode(TimeStampedModel):
    """
    Why stock was written off, found missing, or adjusted — organization master
    data, not a hard-coded list.

    **The code and what it applies to are immutable once created**, enforced by
    an allowlist trigger. Everything else — the names, the evidence and comment
    requirements, whether it is still offered — may change. That split is the
    whole design: repurposing `SPOIL` from waste to count variance would
    retroactively change what every document already posted against it says
    happened, and no amount of care at the call site can undo that afterwards.

    Archiving sets `is_active = False` and never deletes. The unique constraint
    therefore keeps a retired code **reserved**: reissuing `BREAK` to mean
    something new would make two different meanings share one identity in the
    history.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="inventory_reason_codes",
        verbose_name=_("organization"),
    )
    code = models.CharField(
        _("code"),
        max_length=32,
        help_text=_("Canonicalised to upper case. Reserved forever once used."),
    )
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    applies_to = models.CharField(
        _("applies to"),
        max_length=20,
        choices=ReasonCodeApplication.choices,
    )
    #: A free-text explanation is demanded on the line as well as the code.
    #: "Other" is the reason this exists.
    requires_comment = models.BooleanField(_("requires a comment"), default=False)
    #: A photograph, an incident report, a signed disposal note. The document
    #: already carries an evidence reference; this makes it mandatory for the
    #: reasons where the absence of proof is the risk.
    requires_evidence = models.BooleanField(_("requires evidence"), default=False)

    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("inventory reason code")
        verbose_name_plural = _("inventory reason codes")
        ordering = ["organization__code", "applies_to", "code"]
        permissions = [
            ("manage_reason_codes", _("Can create and archive inventory reason codes")),
        ]
        constraints = [
            # Organization-wide, not per application: one operator's mental
            # model is "our reason codes", and `SPOIL` meaning one thing on a
            # waste note and another on a count sheet is a trap.
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="inventory_reason_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN),
                name="inventory_reason_code_format",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""),
                name="inventory_reason_code_name_ar_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(applies_to__in=ReasonCodeApplication.values),
                name="inventory_reason_code_application_is_known",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "applies_to", "is_active"],
                name="reason_code_org_use_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


# --- The physical count ----------------------------------------------------


class StockCountStatus(models.TextChoices):
    """
    The count lifecycle. Never set by a caller — see `apps.inventory.counts`.

    `IN_PROGRESS` and `SUBMITTED` are the two **active** states, and they are
    exactly the states that own a warehouse freeze. Everything else has either
    not started or finished with it.
    """

    DRAFT = "DRAFT", _("مسودة")
    IN_PROGRESS = "IN_PROGRESS", _("جاري الجرد")
    SUBMITTED = "SUBMITTED", _("مقدّم للاعتماد")
    POSTED = "POSTED", _("مرحّل")
    CANCELLED = "CANCELLED", _("ملغى")
    REVERSED = "REVERSED", _("معكوس")


#: The states that hold a warehouse frozen. One list, referenced by the
#: service, the constraint and the trigger, so the three cannot drift.
ACTIVE_COUNT_STATUSES = frozenset({StockCountStatus.IN_PROGRESS, StockCountStatus.SUBMITTED})


class StockCountScope(models.TextChoices):
    """
    What a count covers.

    One value in Release 1, and the enum exists so the second one cannot be
    added by accident. A partial count freezes only part of a warehouse, and
    "part" has to mean something the ledger can enforce — a key-level freeze,
    not a warehouse-level freeze with a narrower sheet of paper. Until that
    exists, offering `SELECTED_ITEMS` would be offering a freeze that does not
    hold (Task 1.6 §H).
    """

    FULL_WAREHOUSE = "FULL_WAREHOUSE", _("جرد كامل للمخزن")


class StockCount(TimeStampedModel):
    """
    One physical count of one warehouse, from freeze to posted variance.

    **The cutoff is the count's spine.** `cutoff_at` is the single moment the
    book position was photographed, `business_date` is the day that moment
    belongs to, and both are fixed when the warehouse freezes — not when the
    counting finishes and not when somebody approves it. A count approved on
    the 3rd for a cutoff on the 1st posts into the 1st, because that is when
    the stock was what it was.

    The warehouse stays frozen from `start` until the count posts or is
    cancelled, and the freeze is owned through `Warehouse.frozen_by_count`
    rather than through a boolean that could disagree with it.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="stock_counts",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="stock_counts",
        verbose_name=_("branch"),
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="stock_counts",
        verbose_name=_("warehouse"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    #: Assigned when the count **starts**, not when it posts. A counter needs a
    #: number on the sheet in their hand before they count anything, and a
    #: cancelled count is a real event that stays in the record — so its number
    #: is not a hole in the sequence, it is a cancelled document.
    count_number = models.CharField(_("count number"), max_length=32, blank=True)
    status = models.CharField(
        _("status"),
        max_length=12,
        choices=StockCountStatus.choices,
        default=StockCountStatus.DRAFT,
    )
    scope_type = models.CharField(
        _("scope"),
        max_length=20,
        choices=StockCountScope.choices,
        default=StockCountScope.FULL_WAREHOUSE,
    )

    #: The moment the book position was photographed. Null while DRAFT.
    cutoff_at = models.DateTimeField(_("cutoff at"), null=True, blank=True)
    business_date = models.DateField(_("business date"), null=True, blank=True, db_index=True)
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_counts",
        verbose_name=_("cost center"),
    )
    reference = models.CharField(_("evidence reference"), max_length=200, blank=True)
    reason = models.TextField(_("reason"), blank=True)

    conducted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conducted_stock_counts",
        verbose_name=_("conducted by"),
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submitted_stock_counts",
        verbose_name=_("submitted by"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_stock_counts",
        verbose_name=_("approved by"),
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cancelled_stock_counts",
        verbose_name=_("cancelled by"),
    )
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_stock_counts",
        verbose_name=_("reversed by"),
    )

    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    submitted_at = models.DateTimeField(_("submitted at"), null=True, blank=True)
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    cancellation_reason = models.TextField(_("cancellation reason"), blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    #: Null on a posted count whose every line matched the book. Nothing moved,
    #: so there is nothing to point at, and inventing an empty posting would
    #: make "did this count find anything" unanswerable from the ledger.
    stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_counts",
        verbose_name=_("stock ledger entry"),
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_counts",
        verbose_name=_("journal entry"),
    )
    reversal_stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_stock_counts",
        verbose_name=_("reversal stock entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_stock_counts",
        verbose_name=_("reversal journal entry"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("stock count")
        verbose_name_plural = _("stock counts")
        ordering = ["-created_at", "-id"]
        constraints = [
            # One active count per warehouse. The freeze is warehouse-wide, so
            # a second active count would be counting a warehouse somebody else
            # has already frozen and snapshotted.
            models.UniqueConstraint(
                fields=["warehouse"],
                condition=Q(status__in=["IN_PROGRESS", "SUBMITTED"]),
                name="stock_count_one_active_per_warehouse",
            ),
            models.UniqueConstraint(
                fields=["organization", "count_number"],
                condition=~Q(count_number=""),
                name="stock_count_number_unique_per_organization",
            ),
            # Numbered from the moment it starts, and only from then.
            models.CheckConstraint(
                condition=(
                    (Q(status=StockCountStatus.DRAFT) & Q(count_number=""))
                    | (~Q(status=StockCountStatus.DRAFT) & ~Q(count_number=""))
                ),
                name="stock_count_numbered_iff_started",
            ),
            # The cutoff and its snapshot appear together with the number, and
            # never afterwards change.
            models.CheckConstraint(
                condition=(
                    Q(status=StockCountStatus.DRAFT)
                    | (
                        Q(cutoff_at__isnull=False)
                        & Q(business_date__isnull=False)
                        & ~Q(business_date_timezone="")
                        & Q(business_day_start__isnull=False)
                        & Q(conducted_by__isnull=False)
                        & Q(started_at__isnull=False)
                    )
                ),
                name="stock_count_cutoff_snapshot_present",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(
                        status__in=[
                            StockCountStatus.SUBMITTED,
                            StockCountStatus.POSTED,
                            StockCountStatus.REVERSED,
                        ]
                    )
                    | (Q(submitted_by__isnull=False) & Q(submitted_at__isnull=False))
                ),
                name="stock_count_submitted_fields_present",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=[StockCountStatus.POSTED, StockCountStatus.REVERSED])
                    | (Q(approved_by__isnull=False) & Q(approved_at__isnull=False))
                ),
                name="stock_count_approved_fields_present",
            ),
            # Maker-checker, at the database. Hiding the button is a courtesy;
            # this is the rule.
            models.CheckConstraint(
                condition=(
                    Q(approved_by__isnull=True)
                    | Q(conducted_by__isnull=True)
                    | ~Q(approved_by=models.F("conducted_by"))
                ),
                name="stock_count_approver_is_not_the_conductor",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=StockCountStatus.CANCELLED)
                    | (
                        Q(cancelled_by__isnull=False)
                        & Q(cancelled_at__isnull=False)
                        & ~Q(cancellation_reason="")
                    )
                ),
                name="stock_count_cancelled_fields_present",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=StockCountStatus.REVERSED)
                    | (
                        Q(reversed_by__isnull=False)
                        & Q(reversed_at__isnull=False)
                        & ~Q(reversal_reason="")
                    )
                ),
                name="stock_count_reversed_fields_present",
            ),
            # Nothing is posted before approval.
            models.CheckConstraint(
                condition=(
                    Q(status__in=[StockCountStatus.POSTED, StockCountStatus.REVERSED])
                    | (Q(stock_entry__isnull=True) & Q(journal_entry__isnull=True))
                ),
                name="stock_count_entries_only_when_posted",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=StockCountStatus.REVERSED)
                    | (
                        Q(reversal_stock_entry__isnull=True)
                        & Q(reversal_journal_entry__isnull=True)
                    )
                ),
                name="stock_count_reversal_entries_only_when_reversed",
            ),
            models.CheckConstraint(
                condition=Q(scope_type__in=StockCountScope.values),
                name="stock_count_scope_is_known",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="stock_count_org_status_idx"),
            models.Index(fields=["warehouse", "business_date"], name="stock_count_wh_date_idx"),
        ]

    def __str__(self) -> str:
        label = self.count_number or str(self.public_id)
        return f"{label} ({self.get_status_display()})"

    @property
    def is_active(self) -> bool:
        """Whether this count currently owns its warehouse's freeze."""
        return self.status in ACTIVE_COUNT_STATUSES


class StockCountLine(TimeStampedModel):
    """
    One `(item, lot)` on a count sheet: what the books said, and what was found.

    The book columns are a **photograph taken at the cutoff** and never
    recalculated. Reading the current balance at approval instead would value
    the variance against a position that may have moved since — which, if the
    freeze held, it cannot have; and if the freeze did not hold, silently
    posting against the changed figure is the worst of the available responses.
    `count_snapshot_mismatch` is the right one.

    `counted_quantity` is null until somebody counts, and that null is the
    difference between "nobody has been to that shelf yet" and "the shelf is
    empty" — which is why zero is permitted and null is not, at submission.
    """

    count = models.ForeignKey(
        StockCount,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("count"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="stock_count_lines",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        InventoryLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_count_lines",
        verbose_name=_("lot"),
    )
    #: Snapshotted with the rest: an item's base unit is master data and could
    #: in principle be corrected, and the sheet must keep meaning what it meant.
    base_unit = models.ForeignKey(
        "units.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="stock_count_lines",
        verbose_name=_("base unit"),
    )

    # --- The book, at the cutoff. Immutable after start. --------------------
    book_quantity = models.DecimalField(
        _("book quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    book_value = models.DecimalField(
        _("book value"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    book_average = models.DecimalField(
        _("book average cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
    )
    book_control_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_count_lines",
        verbose_name=_("book control account"),
    )
    book_last_movement = models.ForeignKey(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("last movement at cutoff"),
    )
    book_posted_sequence = models.BigIntegerField(_("posted sequence at cutoff"), default=0)

    #: Physically present, absent from the books. Added during counting, with
    #: every book column at zero — which is the honest snapshot, not a missing
    #: one.
    is_unexpected = models.BooleanField(_("unexpected stock"), default=False)

    # --- What was counted ---------------------------------------------------
    counted_quantity = models.DecimalField(
        _("counted quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    package_conversion = models.ForeignKey(
        ItemPackageConversion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_count_lines",
        verbose_name=_("package conversion"),
    )
    entered_package_quantity = models.DecimalField(
        _("entered package quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    measured_base_quantity = models.DecimalField(
        _("measured base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    line_note = models.CharField(_("note"), max_length=200, blank=True)
    reason_code = models.ForeignKey(
        "inventory.InventoryReasonCode",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_count_lines",
        verbose_name=_("reason code"),
    )

    # --- What the approver decided ------------------------------------------
    #: Required where a gain lands in a position with no standing average to
    #: value it at. Supplied by the approver, never by the conductor: it is a
    #: cost decision, and the blind sheet must not carry one.
    approved_unit_cost = models.DecimalField(
        _("approved unit cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        null=True,
        blank=True,
    )
    #: An omitted cost and a deliberate zero are different answers. Without
    #: this flag they would be the same NULL, and "we found stock worth
    #: nothing" would be indistinguishable from "nobody said what it was worth".
    zero_cost_confirmed = models.BooleanField(_("zero cost confirmed"), default=False)

    # --- The result ---------------------------------------------------------
    variance_quantity = models.DecimalField(
        _("variance quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    variance_value = models.DecimalField(
        _("variance value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_count_line",
        verbose_name=_("stock movement"),
    )

    class Meta:
        verbose_name = _("stock count line")
        verbose_name_plural = _("stock count lines")
        ordering = ["count_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["count", "sequence"], name="stock_count_line_sequence_unique"
            ),
            # NULL-safe: a non-lot item has one position, so it must have one
            # line, and standard SQL's "every NULL differs" would permit any
            # number of them.
            models.UniqueConstraint(
                fields=["count", "item", "lot"],
                nulls_distinct=False,
                name="stock_count_line_key_unique",
            ),
            models.CheckConstraint(
                condition=Q(counted_quantity__isnull=True) | Q(counted_quantity__gte=Decimal("0")),
                name="stock_count_line_counted_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(book_quantity__gte=Decimal("0")),
                name="stock_count_line_book_quantity_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(book_value__gte=Decimal("0")),
                name="stock_count_line_book_value_not_negative",
            ),
            # An unexpected line is unexpected precisely because the books held
            # nothing there.
            models.CheckConstraint(
                condition=(
                    Q(is_unexpected=False)
                    | (
                        Q(book_quantity=Decimal("0"))
                        & Q(book_value=Decimal("0"))
                        & Q(book_average=Decimal("0"))
                    )
                ),
                name="stock_count_line_unexpected_has_no_book",
            ),
            models.CheckConstraint(
                condition=Q(approved_unit_cost__isnull=True)
                | Q(approved_unit_cost__gte=Decimal("0")),
                name="stock_count_line_approved_cost_not_negative",
            ),
            # The flag means what it says, or it means nothing.
            models.CheckConstraint(
                condition=Q(zero_cost_confirmed=False) | Q(approved_unit_cost=Decimal("0")),
                name="stock_count_line_zero_cost_flag_matches",
            ),
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(package_conversion__isnull=False),
                name="stock_count_line_measured_needs_package",
            ),
        ]
        indexes = [
            models.Index(fields=["count", "is_unexpected"], name="count_line_unexpected_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.count_id}#{self.sequence}: {self.item_id}"


# --- The manual adjustment --------------------------------------------------


class AdjustmentLineKind(models.TextChoices):
    """
    What one adjustment line does. Closed, and each value has its own arithmetic.

    `VALUE_ONLY` is the reason this aggregate exists at all: it moves no
    quantity and cannot be expressed as a signed movement of goods.
    """

    QUANTITY_GAIN = "QUANTITY_GAIN", _("زيادة كمية")
    QUANTITY_LOSS = "QUANTITY_LOSS", _("نقص كمية")
    VALUE_ONLY = "VALUE_ONLY", _("إعادة تقييم بالقيمة فقط")


class InventoryAdjustmentDocument(TimeStampedModel):
    """
    An authorized exception: stock corrected outside the flows that normally
    move it.

    Not a substitute for a receipt, an issue, a transfer, a supplier return,
    production, or a count — each of those records *what happened*, and an
    adjustment records only that the books were wrong. The screens say so, and
    the reason code is mandatory so that "wrong how" is always on the record.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="inventory_adjustments",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="inventory_adjustments",
        verbose_name=_("branch"),
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="inventory_adjustments",
        verbose_name=_("warehouse"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    document_number = models.CharField(_("document number"), max_length=32, blank=True)
    status = models.CharField(
        _("status"),
        max_length=10,
        choices=InventoryDocumentStatus.choices,
        default=InventoryDocumentStatus.DRAFT,
    )

    effective_at = models.DateTimeField(_("effective at"))
    business_date = models.DateField(_("business date"))
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_adjustments",
        verbose_name=_("cost center"),
    )
    evidence_reference = models.CharField(_("evidence reference"), max_length=200)
    reason = models.TextField(_("reason"))

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_inventory_adjustments",
        verbose_name=_("created by"),
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_inventory_adjustments",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_inventory_adjustments",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    stock_entry = models.ForeignKey(
        StockLedgerEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_adjustments",
        verbose_name=_("stock ledger entry"),
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_adjustments",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_inventory_adjustments",
        verbose_name=_("reversal journal entry"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("inventory adjustment")
        verbose_name_plural = _("inventory adjustments")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(evidence_reference=""),
                name="inventory_adjustment_evidence_not_empty",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="inventory_adjustment_reason_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(status=InventoryDocumentStatus.DRAFT) & Q(document_number=""))
                    | (~Q(status=InventoryDocumentStatus.DRAFT) & ~Q(document_number=""))
                ),
                name="inventory_adjustment_numbered_iff_posted",
            ),
            models.UniqueConstraint(
                fields=["organization", "document_number"],
                condition=~Q(document_number=""),
                name="inventory_adjustment_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(status=InventoryDocumentStatus.DRAFT)
                | (Q(posted_by__isnull=False) & Q(posted_at__isnull=False)),
                name="inventory_adjustment_posted_fields_present",
            ),
            models.CheckConstraint(
                condition=Q(status=InventoryDocumentStatus.DRAFT)
                | (~Q(business_date_timezone="") & Q(business_day_start__isnull=False)),
                name="inventory_adjustment_business_date_snapshot_present",
            ),
            models.CheckConstraint(
                condition=~Q(status=InventoryDocumentStatus.REVERSED)
                | (
                    Q(reversed_by__isnull=False)
                    & Q(reversed_at__isnull=False)
                    & ~Q(reversal_reason="")
                ),
                name="inventory_adjustment_reversed_fields_present",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="inv_adjustment_org_status_idx"),
            models.Index(fields=["warehouse", "business_date"], name="inv_adjustment_wh_date_idx"),
        ]

    def __str__(self) -> str:
        label = self.document_number or str(self.public_id)
        return f"{label} ({self.get_status_display()})"


class InventoryAdjustmentLine(TimeStampedModel):
    """
    One corrected position, in one of three ways.

    The three kinds, and what each requires:

    * **QUANTITY_GAIN** — a positive base quantity and an explicit `unit_cost`.
      There is no average to borrow for goods the books do not have, and
      defaulting to zero would book free stock.
    * **QUANTITY_LOSS** — a positive base quantity, valued at the standing
      average, with the full-depletion rule at zero.
    * **VALUE_ONLY** — no quantity at all and a signed `value_adjustment`. The
      position must already hold quantity: value against nothing is exactly the
      corruption `_assert_position_is_coherent` refuses to compute against.
    """

    document = models.ForeignKey(
        InventoryAdjustmentDocument,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("document"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))
    kind = models.CharField(_("kind"), max_length=16, choices=AdjustmentLineKind.choices)

    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="adjustment_lines",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        InventoryLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustment_lines",
        verbose_name=_("lot"),
    )

    package_conversion = models.ForeignKey(
        ItemPackageConversion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustment_lines",
        verbose_name=_("package conversion"),
    )
    entered_package_quantity = models.DecimalField(
        _("entered package quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    measured_base_quantity = models.DecimalField(
        _("measured base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    #: The magnitude, always non-negative; `kind` carries the direction, for
    #: the same reason `MovementType` does in the kernel. Zero for VALUE_ONLY.
    base_quantity = models.DecimalField(
        _("base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        default=Decimal("0"),
    )
    unit_cost = models.DecimalField(
        _("unit cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        null=True,
        blank=True,
    )
    zero_cost_confirmed = models.BooleanField(_("zero cost confirmed"), default=False)
    #: Signed, VALUE_ONLY only. Positive writes the position up.
    value_adjustment = models.DecimalField(
        _("value adjustment"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    #: Signed, written at posting from the movement the kernel actually made.
    total_value = models.DecimalField(
        _("total value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    reason_code = models.ForeignKey(
        "inventory.InventoryReasonCode",
        on_delete=models.PROTECT,
        related_name="adjustment_lines",
        verbose_name=_("reason code"),
    )
    line_comment = models.CharField(_("comment"), max_length=200, blank=True)

    movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustment_line",
        verbose_name=_("stock movement"),
    )
    control_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustment_lines",
        verbose_name=_("control account"),
    )

    class Meta:
        verbose_name = _("inventory adjustment line")
        verbose_name_plural = _("inventory adjustment lines")
        ordering = ["document_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "sequence"], name="inventory_adjustment_line_sequence_unique"
            ),
            # One line per position per document. Two would each compute
            # against a balance the other had already changed, and the
            # canonical journal grouping would have to guess an order.
            models.UniqueConstraint(
                fields=["document", "item", "lot"],
                nulls_distinct=False,
                name="inventory_adjustment_line_key_unique",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=AdjustmentLineKind.values),
                name="inventory_adjustment_line_kind_is_known",
            ),
            # Quantity kinds move quantity; VALUE_ONLY does not.
            models.CheckConstraint(
                condition=(
                    (
                        Q(
                            kind__in=[
                                AdjustmentLineKind.QUANTITY_GAIN,
                                AdjustmentLineKind.QUANTITY_LOSS,
                            ]
                        )
                        & Q(base_quantity__gt=Decimal("0"))
                    )
                    | (Q(kind=AdjustmentLineKind.VALUE_ONLY) & Q(base_quantity=Decimal("0")))
                ),
                name="inventory_adjustment_line_quantity_matches_kind",
            ),
            # A gain names its cost; nothing else may.
            models.CheckConstraint(
                condition=(
                    (Q(kind=AdjustmentLineKind.QUANTITY_GAIN) & Q(unit_cost__isnull=False))
                    | (~Q(kind=AdjustmentLineKind.QUANTITY_GAIN) & Q(unit_cost__isnull=True))
                ),
                name="inventory_adjustment_line_cost_iff_gain",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__isnull=True) | Q(unit_cost__gte=Decimal("0")),
                name="inventory_adjustment_line_cost_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(zero_cost_confirmed=False) | Q(unit_cost=Decimal("0")),
                name="inventory_adjustment_line_zero_cost_flag_matches",
            ),
            # A revaluation names its amount; nothing else may. Zero is
            # excluded: a value adjustment of nothing is not a correction.
            models.CheckConstraint(
                condition=(
                    (
                        Q(kind=AdjustmentLineKind.VALUE_ONLY)
                        & Q(value_adjustment__isnull=False)
                        & ~Q(value_adjustment=Decimal("0"))
                    )
                    | (~Q(kind=AdjustmentLineKind.VALUE_ONLY) & Q(value_adjustment__isnull=True))
                ),
                name="inventory_adjustment_line_value_iff_value_only",
            ),
            models.CheckConstraint(
                condition=Q(measured_base_quantity__isnull=True)
                | Q(package_conversion__isnull=False),
                name="inventory_adjustment_line_measured_needs_package",
            ),
            models.CheckConstraint(
                condition=Q(entered_package_quantity__isnull=True)
                | Q(entered_package_quantity__gt=Decimal("0")),
                name="inventory_adjustment_line_package_quantity_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_id}#{self.sequence}: {self.kind}"


# ---------------------------------------------------------------------------
# Imports (Task 1.7)
# ---------------------------------------------------------------------------


class ImportKind(models.TextChoices):
    """
    What a batch imports. Closed, and deliberately short.

    Every value here is **master data or a draft**. There is no value for a
    posted receipt, issue, transfer, count, waste or adjustment, and that is
    the boundary rather than an omission: a spreadsheet is an assertion by
    whoever typed it, and the ledger only accepts assertions that a person
    posted through the service that values them. `OPENING_STOCK_DRAFT` reaches
    the ledger the same way everything else does — somebody opens the draft it
    produced and posts it.
    """

    ITEM_CATEGORY = "ITEM_CATEGORY", _("مجموعات الأصناف")
    PACKAGE_UNIT = "PACKAGE_UNIT", _("وحدات التعبئة")
    BRANCH_ITEM_SETTING = "BRANCH_ITEM_SETTING", _("إعدادات الصنف في الفرع")
    # Task 2.17. The framework is one; the kinds name which module's master
    # data a batch carries. Procurement's validators and writers live in
    # `apps/procurement/imports.py` and register themselves — this module
    # never imports procurement. The draft kind produces purchase-request
    # DRAFTS for review, never a submitted or posted document (§16.8).
    SUPPLIER = "SUPPLIER", _("الموردون")
    SUPPLIER_ITEM = "SUPPLIER_ITEM", _("كتالوج الموردين")
    PURCHASE_REQUEST_DRAFT = "PURCHASE_REQUEST_DRAFT", _("مسودات طلبات الشراء")


#: The kinds that write branch-scoped rows and therefore name a branch.
#: A purchase request is a branch document, so its draft import names one.
BRANCH_SCOPED_IMPORT_KINDS = frozenset(
    {ImportKind.BRANCH_ITEM_SETTING, ImportKind.PURCHASE_REQUEST_DRAFT}
)

#: Kinds needing `import_opening_draft` rather than `import_master_data`.
#:
#: **Empty in this release.** `OPENING_STOCK_DRAFT` was declared and then
#: removed: it parses nothing, validates nothing and writes nothing, and a
#: dropdown entry that accepts a file and then fails is worse than an absent
#: one. It is the only kind that would reach the ledger at all — even as a
#: draft it sets the ledger's starting position — so it earns its own review
#: rather than a corner of this task.
#:
#: The set stays, rather than the lookup being deleted, because the permission
#: split it drives is the durable decision; only the kind is deferred.
OPENING_IMPORT_KINDS: frozenset[str] = frozenset()


class ImportBatchStatus(models.TextChoices):
    """
    The batch lifecycle. Never set by a caller — see `apps.inventory.imports`.

    `UPLOADED` holds parsed rows and nothing else. `VALIDATED` means every row
    was judged and the batch may be applied; `FAILED_VALIDATION` means at
    least one row was rejected and it may not. `APPLIED` and `CANCELLED` are
    terminal.

    There is no `PARTIALLY_APPLIED`. A batch applies completely or not at all,
    because the alternative is a spreadsheet that half-changed the item master
    and a person who has to work out which half.
    """

    UPLOADED = "UPLOADED", _("مرفوع")
    VALIDATED = "VALIDATED", _("مدقّق")
    FAILED_VALIDATION = "FAILED_VALIDATION", _("فشل التدقيق")
    APPLIED = "APPLIED", _("مطبّق")
    CANCELLED = "CANCELLED", _("ملغى")


#: Statuses after which nothing more happens to a batch.
TERMINAL_IMPORT_STATUSES = frozenset({ImportBatchStatus.APPLIED, ImportBatchStatus.CANCELLED})


class ImportBatch(TimeStampedModel):
    """
    One uploaded file, its verdict, and what it did.

    **Nothing outside this table changes until apply.** Upload parses and
    stores rows; validation judges them and stores the verdict; only apply
    writes to the item master, and it does so inside one transaction. A
    reviewer can therefore look at exactly what a spreadsheet would do before
    any of it happens, which is the entire reason the batch exists rather than
    a direct upload-and-write.

    `content_hash` is the fingerprint of the normalised rows. Re-uploading the
    same file is legitimate — somebody lost the tab — but re-*applying* the
    same content is not, and the hash is how the second attempt is recognised
    rather than silently doubling every row it touches.

    The file itself is never stored. The rows are, as parsed JSON: keeping the
    upload would mean keeping a user-supplied binary on disk with a
    user-supplied name, and everything worth auditing is in the rows.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="inventory_import_batches",
        verbose_name=_("organization"),
    )
    #: Set for branch-scoped kinds only, and required for them by constraint.
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="inventory_import_batches",
        null=True,
        blank=True,
        verbose_name=_("branch"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    batch_number = models.CharField(_("batch number"), max_length=32, blank=True)

    kind = models.CharField(_("kind"), max_length=32, choices=ImportKind.choices)
    status = models.CharField(
        _("status"),
        max_length=24,
        choices=ImportBatchStatus.choices,
        default=ImportBatchStatus.UPLOADED,
        db_index=True,
    )

    #: What the uploader called it, after sanitising. Kept for the audit trail;
    #: never used to open, write, or serve anything.
    original_filename = models.CharField(_("original filename"), max_length=255)
    content_hash = models.CharField(_("content hash"), max_length=64, db_index=True)
    byte_size = models.PositiveIntegerField(_("byte size"))

    row_count = models.PositiveIntegerField(_("rows"), default=0)
    valid_row_count = models.PositiveIntegerField(_("valid rows"), default=0)
    error_row_count = models.PositiveIntegerField(_("rows in error"), default=0)
    #: Rows apply actually changed. Below `valid_row_count` when a row asked
    #: for a value the record already had — which is not an error and not a
    #: change, and reporting it as either would be a lie.
    applied_row_count = models.PositiveIntegerField(_("rows applied"), default=0)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_import_batches",
        null=True,
        blank=True,
        verbose_name=_("uploaded by"),
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="applied_import_batches",
        null=True,
        blank=True,
        verbose_name=_("applied by"),
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cancelled_import_batches",
        null=True,
        blank=True,
        verbose_name=_("cancelled by"),
    )

    validated_at = models.DateTimeField(_("validated at"), null=True, blank=True)
    applied_at = models.DateTimeField(_("applied at"), null=True, blank=True)
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)

    notes = models.TextField(_("notes"), blank=True)
    #: Why it was cancelled. Required when cancelling, by service.
    reason = models.TextField(_("reason"), blank=True)

    #: The draft the opening import produced, when it produced one.
    opening_document = models.ForeignKey(
        "inventory.OpeningStockDocument",
        on_delete=models.PROTECT,
        related_name="import_batches",
        null=True,
        blank=True,
        verbose_name=_("opening document"),
    )

    class Meta:
        verbose_name = _("import batch")
        verbose_name_plural = _("import batches")
        ordering = ("-created_at", "-id")
        permissions = [
            ("import_master_data", _("Can import inventory master data")),
            ("import_opening_draft", _("Can import an opening stock draft")),
            ("view_import_history", _("Can view inventory import history")),
        ]
        indexes = [
            models.Index(fields=["organization", "kind", "status"]),
            models.Index(fields=["organization", "-created_at"]),
        ]
        constraints = [
            # A branch-scoped kind names a branch; an organization-scoped one
            # must not, because a branch there would be a scope nobody applies.
            models.CheckConstraint(
                condition=(
                    (Q(kind__in=sorted(BRANCH_SCOPED_IMPORT_KINDS)) & Q(branch__isnull=False))
                    | (~Q(kind__in=sorted(BRANCH_SCOPED_IMPORT_KINDS)) & Q(branch__isnull=True))
                ),
                name="inventory_import_branch_matches_kind",
            ),
            # Applied means somebody applied it, at a time, and the count of
            # what changed is known. Any of those missing is a batch that
            # cannot be audited.
            models.CheckConstraint(
                condition=(
                    ~Q(status=ImportBatchStatus.APPLIED)
                    | (Q(applied_by__isnull=False) & Q(applied_at__isnull=False))
                ),
                name="inventory_import_applied_records_who_and_when",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=ImportBatchStatus.CANCELLED)
                    | (Q(cancelled_at__isnull=False) & ~Q(reason=""))
                ),
                name="inventory_import_cancelled_states_a_reason",
            ),
            # A failed batch never applied anything, whatever else is true.
            models.CheckConstraint(
                condition=(~Q(status=ImportBatchStatus.FAILED_VALIDATION) | Q(applied_row_count=0)),
                name="inventory_import_failed_applied_nothing",
            ),
            models.CheckConstraint(
                condition=Q(applied_row_count__lte=models.F("valid_row_count")),
                name="inventory_import_applied_within_valid",
            ),
            models.CheckConstraint(
                condition=Q(row_count=models.F("valid_row_count") + models.F("error_row_count")),
                name="inventory_import_rows_add_up",
            ),
            # Django choices are a form-layer courtesy, not a boundary: a raw
            # INSERT, a data migration or a `bulk_create` walks straight past
            # them. A batch whose kind has no validator could never be
            # previewed or applied, so it would sit in the history looking like
            # work somebody did.
            models.CheckConstraint(
                condition=Q(kind__in=sorted(ImportKind.values)),
                name="inventory_import_kind_is_supported",
            ),
            # One applied batch per content per kind: the second apply of the
            # same spreadsheet is a retry, not a second import.
            models.UniqueConstraint(
                fields=["organization", "kind", "content_hash"],
                condition=Q(status=ImportBatchStatus.APPLIED),
                name="inventory_import_one_applied_batch_per_content",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.batch_number or self.public_id} {self.kind} ({self.get_status_display()})"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_IMPORT_STATUSES


class ImportRowResult(TimeStampedModel):
    """
    One row of the uploaded file, as parsed, and what was decided about it.

    Kept for valid rows as well as rejected ones. A batch that stored only its
    errors could say what went wrong and never what went right, and "what did
    this import actually change" is the question an auditor asks.

    `payload` is the row exactly as parsed — strings, never coerced numbers.
    Decimal parsing happens in validation through the approved utilities, and
    storing a parsed float here would reintroduce binary rounding into the one
    record that exists to prove what the file said.
    """

    batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="rows",
        verbose_name=_("batch"),
    )
    #: 1-based, and the number the person sees in their spreadsheet — the
    #: header is row 1, so the first data row is 2.
    row_number = models.PositiveIntegerField(_("row number"))
    #: The row's own identity in the source: a code, usually. Blank when the
    #: file gave none, which is itself a validation error for most kinds.
    external_key = models.CharField(_("external key"), max_length=200, blank=True)

    is_valid = models.BooleanField(_("valid"), default=False)
    #: field name -> list of Arabic messages. `{}` for a valid row.
    errors = models.JSONField(_("errors"), default=dict, blank=True)
    payload = models.JSONField(_("payload"), default=dict)

    #: What apply did with it, once apply has run.
    applied_action = models.CharField(_("applied action"), max_length=16, blank=True)
    applied_object_id = models.CharField(_("applied object id"), max_length=64, blank=True)

    class Meta:
        verbose_name = _("import row")
        verbose_name_plural = _("import rows")
        ordering = ("batch", "row_number")
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"], name="inventory_import_row_number_unique"
            ),
            # A valid row carries no errors and an invalid one carries at
            # least the field that failed. Without this a row could be shown
            # green with a message attached, and the operator would believe
            # the colour.
            models.CheckConstraint(
                condition=(Q(is_valid=True) & Q(errors={})) | Q(is_valid=False),
                name="inventory_import_valid_row_has_no_errors",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.batch_id}#{self.row_number}"


# ---------------------------------------------------------------------------
# Stock locations (Task 1.7B)
# ---------------------------------------------------------------------------


class StockLocation(TimeStampedModel):
    """
    A bin, shelf or zone inside one warehouse. Refines *where*, never *what it
    cost*.

    ADR-018 §2 decided this and it is the whole design: the warehouse owns
    value, a location owns quantity. Moving a box between two bins in one store
    must revalue nothing, so `StockLocation` appears in no valuation key, takes
    no average cost and names no control account. Widening the stock key to
    include it would revalue stock on every put-away — which is precisely the
    outcome ADR-018 forbids.

    Locations are **optional**. A warehouse that has never used bins holds all
    its quantity unlocated, and that is a supported permanent state rather than
    a migration half-step: most stores in this business are one room.

    One level. No nesting — a tree needs the depth and cycle rules
    `ItemCategory` carries, and nothing in Release 1 asks for aisle→rack→bin.
    """

    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="locations",
        verbose_name=_("warehouse"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("stock location")
        verbose_name_plural = _("stock locations")
        ordering = ["warehouse__code", "code"]
        permissions = [
            ("manage_locations", _("Can create and archive stock locations")),
            ("move_location_stock", _("Can move stock between locations")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse", "code"], name="stock_location_code_unique_per_warehouse"
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="stock_location_code_format"
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""), name="stock_location_name_ar_not_empty"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.warehouse_id}/{self.code}"


class StockLocationBalance(TimeStampedModel):
    """
    How much of one `(item, lot)` sits in one location. Quantity only.

    **No value, no average cost, no control account.** A location holding five
    kilos of rice holds no money — the warehouse does, and asking this table
    what something cost is a question it is designed to be unable to answer.

    The unlocated remainder is not stored here. It is
    `StockBalance.quantity − sum(located)`, derived rather than retained,
    because a second retained number is a second thing that can drift from the
    warehouse total and invariant 22 exists to make drift impossible rather
    than merely detectable.
    """

    location = models.ForeignKey(
        StockLocation,
        on_delete=models.PROTECT,
        related_name="balances",
        verbose_name=_("location"),
    )
    #: Denormalised from `location.warehouse` so invariant 22 can be checked
    #: with one grouped query rather than a join per position.
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="location_balances",
        verbose_name=_("warehouse"),
    )
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="location_balances",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        "inventory.InventoryLot",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="location_balances",
        verbose_name=_("lot"),
    )

    quantity = models.DecimalField(
        _("quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        default=Decimal("0"),
    )

    class Meta:
        verbose_name = _("stock location balance")
        verbose_name_plural = _("stock location balances")
        ordering = ["warehouse__code", "location__code", "item__code"]
        indexes = [
            models.Index(fields=["warehouse", "item", "lot"]),
        ]
        constraints = [
            # NULL-safe: a lotless position is one position, not one per row.
            models.UniqueConstraint(
                fields=["location", "item", "lot"],
                name="stock_location_balance_key_unique",
                nulls_distinct=False,
            ),
            # Negative stock in a bin is refused for the same reason it is
            # refused in a warehouse: it describes goods nobody has.
            models.CheckConstraint(
                condition=Q(quantity__gte=Decimal("0")),
                name="stock_location_balance_quantity_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.location_id}:{self.item_id}:{self.lot_id or 0} = {self.quantity}"


class LocationMovementType(models.TextChoices):
    """
    Why quantity moved between or into a location. Closed.

    Deliberately *not* `MovementType`. These are put-away and picking events,
    they carry no value, and reusing the valued vocabulary would invite exactly
    the confusion this split exists to prevent.
    """

    PUT_AWAY = "PUT_AWAY", _("إدخال إلى موقع")
    PICK = "PICK", _("سحب من موقع")
    TRANSFER_IN = "TRANSFER_IN", _("نقل داخلي وارد")
    TRANSFER_OUT = "TRANSFER_OUT", _("نقل داخلي صادر")


class StockLocationMovement(TimeStampedModel):
    """
    One quantity-only effect on a location balance. Append-only.

    Links to the `StockMovement` that caused it where one exists — a receipt
    puts away what it received — and to nothing where one does not: a
    location-to-location move inside a warehouse creates a pair of these and
    **no** `StockMovement`, because nothing entered or left the warehouse and
    nothing was revalued. That case is the proof the split is real.
    """

    location = models.ForeignKey(
        StockLocation,
        on_delete=models.PROTECT,
        related_name="movements",
        verbose_name=_("location"),
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="location_movements",
        verbose_name=_("warehouse"),
    )
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="location_movements",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        "inventory.InventoryLot",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="location_movements",
        verbose_name=_("lot"),
    )

    movement_type = models.CharField(
        _("movement type"), max_length=20, choices=LocationMovementType.choices
    )
    #: Signed, in the item's base unit. Positive into the location.
    base_quantity = models.DecimalField(
        _("base quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    quantity_after = models.DecimalField(
        _("quantity after"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )

    #: The valued movement that caused this, when one did. Null for a move
    #: between two locations of one warehouse, which has no valued counterpart.
    stock_movement = models.ForeignKey(
        "inventory.StockMovement",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="location_movements",
        verbose_name=_("stock movement"),
    )

    effective_at = models.DateTimeField(_("effective at"))
    posted_at = models.DateTimeField(_("posted at"), auto_now_add=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="location_movements",
        verbose_name=_("posted by"),
    )
    reference = models.CharField(_("reference"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("stock location movement")
        verbose_name_plural = _("stock location movements")
        ordering = ["-posted_at", "-id"]
        indexes = [
            models.Index(fields=["location", "item", "lot"]),
            models.Index(fields=["warehouse", "-posted_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(base_quantity=Decimal("0")),
                name="stock_location_movement_quantity_not_zero",
            ),
            models.CheckConstraint(
                condition=Q(quantity_after__gte=Decimal("0")),
                name="stock_location_movement_after_not_negative",
            ),
            # An inbound type moves stock in and an outbound type moves it out.
            # Without this the sign is the caller's opinion, and a picking event
            # that increased a bin would look like a put-away in every report.
            models.CheckConstraint(
                condition=(
                    (
                        Q(movement_type__in=["PUT_AWAY", "TRANSFER_IN"])
                        & Q(base_quantity__gt=Decimal("0"))
                    )
                    | (
                        Q(movement_type__in=["PICK", "TRANSFER_OUT"])
                        & Q(base_quantity__lt=Decimal("0"))
                    )
                ),
                name="stock_location_movement_sign_matches_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.location_id} {self.movement_type} {self.base_quantity}"
