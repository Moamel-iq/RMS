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
    InventoryItem,
    InventoryLot,
    ItemCategory,
    ItemPackageConversion,
    PackageUnit,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
    ValuationLayer,
    Warehouse,
)

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[Any]
else:
    _ModelAdmin = admin.ModelAdmin


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
