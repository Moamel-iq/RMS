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
            ("manage_conversions", _("Can manage item package conversions")),
            ("view_stock", _("Can view stock on hand")),
            ("view_valuation", _("Can view inventory cost and valuation")),
            ("create_draft_movement", _("Can create a draft stock movement")),
            ("post_opening_stock", _("Can post opening stock")),
            ("post_receipt", _("Can post a stock receipt")),
            ("post_issue", _("Can post a stock issue")),
            ("post_transfer", _("Can post a stock transfer")),
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
    TRANSFER_OUT = "TRANSFER_OUT", _("تحويل صادر")
    TRANSFER_IN = "TRANSFER_IN", _("تحويل وارد")
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
        MovementType.TRANSFER_IN,
        MovementType.COUNT_GAIN,
        MovementType.PRODUCTION_IN,
    }
)

OUTBOUND_MOVEMENT_TYPES = frozenset(
    {
        MovementType.ISSUE,
        MovementType.TRANSFER_OUT,
        MovementType.WASTE,
        MovementType.COUNT_LOSS,
        MovementType.PRODUCTION_OUT,
    }
)


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
