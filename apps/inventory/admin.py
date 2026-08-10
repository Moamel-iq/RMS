"""
Inventory in the admin: a developer inspection tool, never the write path.

Master data is not a posted ledger, so this is a softer lockdown than
`apps/accounting/admin.py` — the reasoning is different, not absent. An admin
form here would skip the code canonicalisation, the category depth and
exclusivity checks, the conversion versioning, and the audit event. A row
created that way would look like master data and behave like a landmine: an
item whose code is `rice-272` in lowercase, or a category holding both items
and children.

So everything is read-only, and the services are the only way in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import (
    BranchItemSetting,
    InventoryAccountMapping,
    InventoryItem,
    InventoryLot,
    InventoryMovementDocument,
    InventoryMovementDocumentLine,
    ItemCategory,
    ItemPackageConversion,
    OpeningStockDocument,
    OpeningStockLine,
    PackageUnit,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
    StockTransfer,
    StockTransferReceipt,
    StockTransferShortage,
    ValuationLayer,
    Warehouse,
)

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[Any]
    _LineInline = admin.TabularInline[OpeningStockLine, OpeningStockDocument]
    _MovementLineInline = admin.TabularInline[
        InventoryMovementDocumentLine, InventoryMovementDocument
    ]
else:
    _ModelAdmin = admin.ModelAdmin
    _LineInline = admin.TabularInline
    _MovementLineInline = admin.TabularInline


class ReadOnlyAdminMixin:
    """Look, do not touch. Must come first in the bases so it wins the MRO."""

    actions: Any = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ItemCategory)
class ItemCategoryAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = ("code", "name_ar", "depth", "parent", "organization", "is_active")
    list_filter = ("is_active", "depth", "organization")
    search_fields = ("code", "name_ar", "name_en")
    ordering = ("organization__code", "code")
    list_select_related = ("organization", "parent")


@admin.register(PackageUnit)
class PackageUnitAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """
    A package unit carries no factor, which is why this list has no factor
    column: there is no universal "how many kilograms in a carton" to show.
    """

    list_display = ("code", "name_ar", "organization", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("code", "name_ar", "name_en")
    ordering = ("organization__code", "code")
    list_select_related = ("organization",)


@admin.register(InventoryItem)
class InventoryItemAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "code",
        "name_ar",
        "category",
        "item_type",
        "base_unit",
        "tracks_lots",
        "is_active",
        "organization",
    )
    list_filter = ("item_type", "is_active", "tracks_lots", "tracks_expiry", "organization")
    search_fields = ("code", "name_ar", "name_en")
    ordering = ("organization__code", "code")
    list_select_related = ("organization", "category", "base_unit")


@admin.register(ItemPackageConversion)
class ItemPackageConversionAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "item",
        "package_unit",
        "factor_display",
        "conversion_type",
        "effective_from",
        "effective_to",
        "version",
        "is_active",
    )
    list_filter = ("conversion_type", "is_active", "organization")
    search_fields = ("item__code", "item__name_ar", "package_unit__code")
    ordering = ("item__code", "package_unit__code", "-effective_from")
    list_select_related = ("item", "package_unit")

    @admin.display(description=_("factor"))
    def factor_display(self, obj: ItemPackageConversion) -> str:
        """Locale-independent: a technical identity, never a localised Decimal."""
        return obj.factor_display


@admin.register(BranchItemSetting)
class BranchItemSettingAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = ("item", "branch", "is_stocked", "reorder_point", "reorder_quantity")
    list_filter = ("is_stocked", "branch")
    search_fields = ("item__code", "item__name_ar", "branch__code")
    ordering = ("branch__code", "item__code")
    list_select_related = ("branch", "item")


@admin.register(Warehouse)
class WarehouseAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = ("code", "name_ar", "branch", "warehouse_type", "is_system", "is_active")
    list_filter = ("warehouse_type", "is_system", "is_active", "branch")
    search_fields = ("code", "name_ar", "name_en")
    ordering = ("branch__code", "code")
    list_select_related = ("branch",)


# ---------------------------------------------------------------------------
# The ledger — read-only here, and refused by the database anyway
# ---------------------------------------------------------------------------
#
# The lockdown below is the *second* line. A stock movement is insert-only at
# the database (migration 0004), so an admin form that tried to save one would
# raise rather than corrupt anything. Registering these read-only means an
# administrator investigating an incident can read the ledger without being
# offered an edit button that leads to a stack trace.


@admin.register(InventoryLot)
class InventoryLotAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = ("code", "item", "expiry_date", "received_on", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("code", "supplier_lot_code", "item__code")
    ordering = ("item__code", "code")
    list_select_related = ("item",)


@admin.register(StockLedgerEntry)
class StockLedgerEntryAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "id",
        "organization",
        "source_document_type",
        "source_document_id",
        "source_event",
        "effective_at",
        "posted_at",
        "posted_by",
    )
    list_filter = ("source_event", "organization")
    search_fields = ("source_document_id", "idempotency_key", "reference")
    ordering = ("-posted_at",)
    list_select_related = ("organization", "posted_by")


@admin.register(StockMovement)
class StockMovementAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "posted_sequence",
        "movement_type",
        "warehouse",
        "item",
        "lot",
        "base_quantity",
        "quantity_after",
        "effective_at",
    )
    list_filter = ("movement_type", "organization")
    search_fields = ("item__code", "warehouse__code", "effect_key")
    ordering = ("-posted_sequence",)
    list_select_related = ("warehouse", "item", "lot", "entry")


@admin.register(StockBalance)
class StockBalanceAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "warehouse",
        "item",
        "lot",
        "quantity",
        "value",
        "average_cost",
        "last_posted_sequence",
    )
    list_filter = ("organization", "is_frozen")
    search_fields = ("item__code", "warehouse__code")
    ordering = ("warehouse__code", "item__code")
    list_select_related = ("warehouse", "item", "lot")


@admin.register(ValuationLayer)
class ValuationLayerAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "posted_sequence",
        "warehouse",
        "item",
        "lot",
        "received_quantity",
        "remaining_quantity",
        "unit_cost",
    )
    list_filter = ("organization",)
    search_fields = ("item__code", "warehouse__code")
    ordering = ("-posted_sequence",)
    list_select_related = ("warehouse", "item", "lot")


@admin.register(InventoryAccountMapping)
class InventoryAccountMappingAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "account_role",
        "target",
        "account",
        "effective_from",
        "effective_to",
        "version",
        "is_active",
        "organization",
    )
    list_filter = ("account_role", "is_active", "organization")
    search_fields = ("item__code", "category__code", "account__code")
    ordering = ("organization__code", "account_role__code", "-version")
    list_select_related = ("organization", "account_role", "account", "item", "category")

    @admin.display(description=_("target"))
    def target(self, obj: InventoryAccountMapping) -> str:
        return obj.item.code if obj.item is not None else str(obj.category)


class OpeningStockLineInline(_LineInline):
    model = OpeningStockLine
    extra = 0
    can_delete = False
    fields = (
        "sequence",
        "warehouse",
        "item",
        "lot",
        "base_quantity",
        "unit_cost",
        "total_value",
        "inventory_account",
        "movement",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(OpeningStockDocument)
class OpeningStockDocumentAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """
    Read-only, and the database agrees: a POSTED document refuses every
    change except its reversal transition, a REVERSED one refuses all of
    them, and the lines freeze with their document.
    """

    list_display = (
        "document_number",
        "public_id",
        "status",
        "branch",
        "business_date",
        "submitted_by",
        "posted_by",
        "organization",
    )
    list_filter = ("status", "branch", "organization")
    search_fields = ("document_number", "public_id", "evidence_reference")
    ordering = ("-created_at",)
    list_select_related = ("organization", "branch", "submitted_by", "posted_by")
    inlines = [OpeningStockLineInline]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return tuple(field.name for field in self.model._meta.fields)


class InventoryMovementLineInline(_MovementLineInline):
    model = InventoryMovementDocumentLine
    extra = 0
    can_delete = False
    fields = (
        "sequence",
        "item",
        "lot",
        "base_quantity",
        "unit_cost",
        "total_value",
        "inventory_account",
        "contra_account",
        "movement",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(InventoryMovementDocument)
class InventoryMovementDocumentAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """
    Read-only, and the database agrees: a POSTED document refuses every change
    except its reversal transition, a REVERSED one refuses all of them, and
    the lines freeze with their document.
    """

    list_display = (
        "document_number",
        "document_type",
        "status",
        "warehouse",
        "business_date",
        "cost_center",
        "posted_by",
        "organization",
    )
    list_filter = ("document_type", "status", "warehouse", "organization")
    search_fields = ("document_number", "public_id", "evidence_reference")
    ordering = ("-created_at",)
    list_select_related = ("organization", "branch", "warehouse", "cost_center", "posted_by")
    inlines = [InventoryMovementLineInline]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return tuple(field.name for field in self.model._meta.fields)


@admin.register(StockTransfer)
class StockTransferAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """
    Read-only, and the database agrees: a dispatched transfer refuses every
    change except its computed status and its reversal, and its lines freeze
    with it. The status is derived from the posted children, so an admin form
    that could set it would be a form that could lie about how much arrived.
    """

    list_display = (
        "transfer_number",
        "status",
        "source_warehouse",
        "destination_warehouse",
        "business_date",
        "dispatched_by",
        "organization",
    )
    list_filter = ("status", "source_warehouse", "destination_warehouse", "organization")
    search_fields = ("transfer_number", "public_id", "evidence_reference")
    ordering = ("-created_at",)
    list_select_related = ("organization", "source_warehouse", "destination_warehouse")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return tuple(field.name for field in self.model._meta.fields)


@admin.register(StockTransferReceipt)
class StockTransferReceiptAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """One arrival, with both branches' business dates side by side."""

    list_display = (
        "receipt_number",
        "transfer",
        "status",
        "business_date",
        "source_business_date",
        "received_by",
    )
    list_filter = ("status",)
    search_fields = ("receipt_number", "public_id", "evidence_reference")
    ordering = ("-created_at",)
    list_select_related = ("transfer", "received_by")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return tuple(field.name for field in self.model._meta.fields)


@admin.register(StockTransferShortage)
class StockTransferShortageAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """
    The closures. Listed with their cost centre and their actor, because the
    two questions anybody asks of a written-off loss are who authorised it and
    whose department carries it.
    """

    list_display = (
        "shortage_number",
        "transfer",
        "status",
        "business_date",
        "cost_center",
        "closed_by",
    )
    list_filter = ("status", "cost_center")
    search_fields = ("shortage_number", "public_id", "evidence_reference", "reason")
    ordering = ("-created_at",)
    list_select_related = ("transfer", "cost_center", "closed_by")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return tuple(field.name for field in self.model._meta.fields)
