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

from apps.procurement.models import (
    PaymentAllocation,
    PurchaseMatch,
    PurchaseMatchAllocation,
    Supplier,
    SupplierCreditAllocation,
    SupplierCreditNote,
    SupplierCreditReturnAllocation,
    SupplierCreditTerm,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierItem,
    SupplierPayment,
    SupplierReturn,
    SupplierReturnLine,
)

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


@admin.register(SupplierCreditTerm)
class SupplierCreditTermAdmin(ReadOnlyAdmin):
    list_display = (
        "supplier",
        "version",
        "name_ar",
        "net_days",
        "effective_from",
        "effective_to",
        "status",
    )
    list_filter = ("organization", "status")
    search_fields = ("supplier__code", "supplier__name_ar", "name_ar", "name_en")
    ordering = ("supplier__code", "-version")


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


@admin.register(SupplierInvoice)
class SupplierInvoiceAdmin(ReadOnlyAdmin):
    """
    Read-only, and emphatically so (PRC-062).

    A posted invoice carries a payable and a journal entry. An admin form over
    it would be a write path that skips approval, the period check, the source
    identity and the audit event — every one of which is the reason the
    document exists.
    """

    list_display = (
        "number",
        "supplier",
        "supplier_invoice_number",
        "invoice_date",
        "due_date",
        "total_amount",
        "status",
    )
    list_filter = ("organization", "status")
    search_fields = ("number", "supplier_invoice_number", "supplier__code", "supplier__name_ar")
    ordering = ("-invoice_date", "-id")


@admin.register(SupplierInvoiceLine)
class SupplierInvoiceLineAdmin(ReadOnlyAdmin):
    list_display = ("invoice", "sequence", "line_type", "description", "net_amount")
    list_filter = ("line_type",)
    search_fields = ("invoice__number", "invoice__supplier_invoice_number", "description")
    ordering = ("invoice", "sequence")


@admin.register(PurchaseMatch)
class PurchaseMatchAdmin(ReadOnlyAdmin):
    """Read-only. A match decides what a later posting is built on."""

    list_display = ("number", "supplier", "supplier_invoice", "status", "created_by")
    list_filter = ("organization", "status")
    search_fields = ("number", "supplier__code", "supplier_invoice__supplier_invoice_number")
    ordering = ("-id",)


@admin.register(PurchaseMatchAllocation)
class PurchaseMatchAllocationAdmin(ReadOnlyAdmin):
    list_display = (
        "match",
        "sequence",
        "matched_base_quantity",
        "receipt_allocated_value",
        "invoice_allocated_value",
        "price_variance",
    )
    search_fields = ("match__number",)
    ordering = ("match", "sequence")


@admin.register(SupplierReturn)
class SupplierReturnAdmin(ReadOnlyAdmin):
    """
    Read-only, like every posted document here.

    A posted return carries a stock movement and a journal entry. An admin
    form over it would be a write path that skips the quantity bound, the
    period check, the source identity and the audit event.
    """

    list_display = (
        "number",
        "supplier",
        "receipt",
        "warehouse",
        "returned_at",
        "posted_value",
        "status",
    )
    list_filter = ("organization", "status")
    search_fields = ("number", "evidence_reference", "supplier__code", "supplier__name_ar")
    ordering = ("-id",)


@admin.register(SupplierReturnLine)
class SupplierReturnLineAdmin(ReadOnlyAdmin):
    list_display = (
        "supplier_return",
        "sequence",
        "item",
        "returned_base_quantity",
        "posted_value",
        "expected_credit_value",
    )
    search_fields = ("supplier_return__number", "item__code")
    ordering = ("supplier_return", "sequence")


@admin.register(SupplierCreditNote)
class SupplierCreditNoteAdmin(ReadOnlyAdmin):
    """Read-only. A posted note moved the payable and closed a claim."""

    list_display = (
        "number",
        "supplier",
        "supplier_document_number",
        "supplier_return",
        "credit_date",
        "amount",
        "status",
    )
    list_filter = ("organization", "status")
    search_fields = ("number", "supplier_document_number", "supplier__code", "supplier__name_ar")
    ordering = ("-id",)


@admin.register(SupplierCreditAllocation)
class SupplierCreditAllocationAdmin(ReadOnlyAdmin):
    list_display = ("credit_note", "sequence", "invoice", "allocated_amount")
    search_fields = ("credit_note__number", "invoice__number")
    ordering = ("credit_note", "sequence")


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(ReadOnlyAdmin):
    """Read-only. A posted payment moved money."""

    list_display = ("number", "supplier", "method", "paid_at", "amount", "status")
    list_filter = ("organization", "status", "method")
    search_fields = ("number", "reference", "supplier__code", "supplier__name_ar")
    ordering = ("-id",)


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(ReadOnlyAdmin):
    list_display = ("payment", "sequence", "invoice", "allocated_amount")
    search_fields = ("payment__number", "invoice__number")
    ordering = ("payment", "sequence")


@admin.register(SupplierCreditReturnAllocation)
class SupplierCreditReturnAllocationAdmin(ReadOnlyAdmin):
    list_display = (
        "credit_note",
        "sequence",
        "supplier_return_line",
        "credited_base_quantity",
        "allocated_credit_amount",
        "settled_book_value",
    )
    search_fields = ("credit_note__number", "supplier_return_line__supplier_return__number")
    ordering = ("credit_note", "sequence")
