"""
Inventory master data.

Task 1.1 delivers what the stock ledger will reference and nothing that moves
stock: categories, package units, items, item-specific package conversions,
branch item settings, and warehouses. `StockMovement`, `StockBalance`, and the
valuation engine arrive in Task 1.2.

See `docs/tasks/task-1-0-inventory-domain-spec.md` for the approved design and
`docs/invariants/inventory-invariants.md` for the rules these must satisfy.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel
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
