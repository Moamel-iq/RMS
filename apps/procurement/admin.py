"""
Procurement in the Django admin: registered, and read-only.

The admin exists here so an operator can *look* at a supplier — which
organization owns it, when it was archived, what its public id is — without
being able to change anything. Every write goes through a service that
validates, canonicalises and audits, and an admin form would be a second write
path with none of that.

Same arrangement as `apps.inventory.admin` and for the same reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.http import HttpRequest

from apps.procurement.models import Supplier, SupplierItem

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[Any]
else:  # pragma: no cover - `ModelAdmin` is not subscriptable at runtime
    _ModelAdmin = admin.ModelAdmin


class ReadOnlyAdmin(_ModelAdmin):
    """Look, do not touch. No add, no change, no delete, every field frozen."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        return [field.name for field in self.model._meta.fields]


@admin.register(Supplier)
class SupplierAdmin(ReadOnlyAdmin):
    list_display = ("code", "name_ar", "organization", "payment_terms_days", "is_active")
    list_filter = ("organization", "is_active")
    search_fields = ("code", "name_ar", "name_en", "contact_name", "phone")
    ordering = ("organization__code", "code")


@admin.register(SupplierItem)
class SupplierItemAdmin(ReadOnlyAdmin):
    list_display = (
        "supplier",
        "item",
        "package_unit",
        "effective_from",
        "is_preferred",
        "is_active",
    )
    list_filter = ("organization", "is_active", "is_preferred")
    search_fields = ("supplier__code", "item__code", "supplier_sku")
    ordering = ("supplier__code", "item__code", "-effective_from")
