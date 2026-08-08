"""Read-only admin for the audit trail."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.core.models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """
    Visible, never editable.

    The database trigger already refuses UPDATE and DELETE, so an editable
    admin would only offer an action that fails. Removing the permissions
    keeps the admin honest about what it can do.
    """

    list_display = (
        "occurred_at",
        "action",
        "actor_label",
        "target_type",
        "target_id",
        "branch",
    )
    list_filter = ("action", "branch", "occurred_at")
    search_fields = ("actor_label", "target_type", "target_id", "reason", "correlation_id")
    date_hierarchy = "occurred_at"
    list_select_related = ("actor", "branch")
    ordering = ("-occurred_at",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
