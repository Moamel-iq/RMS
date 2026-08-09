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
    ItemCategory,
    ItemPackageConversion,
    PackageUnit,
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
