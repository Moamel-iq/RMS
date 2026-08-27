"""Admin registration for the organization hierarchy."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import (
    AccessChangeRequest,
    Branch,
    BranchMembership,
    Organization,
    OrganizationMembership,
)


class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0
    fields = ("code", "name_ar", "name_en", "timezone", "business_day_start_time", "is_active")
    show_change_link = True


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ar", "name_en", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name_ar", "name_en")
    ordering = ("code",)
    inlines = [BranchInline]
    # Deletion is blocked by PROTECT on branches anyway; removing the action
    # keeps the admin from offering something it cannot deliver.
    actions = None


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name_ar",
        "organization",
        "timezone",
        "business_day_start_time",
        "is_active",
    )
    list_filter = ("is_active", "organization")
    search_fields = ("code", "name_ar", "name_en")
    ordering = ("organization__code", "code")
    list_select_related = ("organization",)
    fieldsets = (
        (None, {"fields": ("organization", "code", "name_ar", "name_en", "is_active")}),
        (
            _("Operating day"),
            {
                "fields": ("timezone", "business_day_start_time"),
                "description": _(
                    "Changing the cutoff after transactions exist would reassign their "
                    "business dates. See ADR-008."
                ),
            },
        ),
    )


@admin.register(BranchMembership)
class BranchMembershipAdmin(admin.ModelAdmin):
    """Read-only evidence; the maker-checker service owns all changes."""

    list_display = ("user", "branch", "role", "is_active", "created_at")
    list_filter = ("is_active", "role", "branch")
    search_fields = ("user__username", "user__phone", "branch__code")
    ordering = ("branch__code", "user__username")
    list_select_related = ("user", "branch")
    autocomplete_fields = ("user", "branch")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    """
    Visible here, granted through `grant_organization_access`.

    Read-only because the service also syncs the role groups that carry the
    permissions. A row created directly here would look like authority and
    grant none of it — a failure that presents as "the permission system is
    broken" rather than as "this row was made the wrong way".
    """

    list_display = ("user", "organization", "role", "is_active", "created_at")
    list_filter = ("is_active", "role", "organization")
    search_fields = ("user__username", "user__phone", "organization__code")
    ordering = ("organization__code", "user__username")
    list_select_related = ("user", "organization")
    actions = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(AccessChangeRequest)
class AccessChangeRequestAdmin(admin.ModelAdmin):
    """Emergency admins may inspect requests, never mutate their outcome."""

    list_display = (
        "id",
        "organization",
        "branch",
        "target_user",
        "action",
        "status",
        "requested_by",
        "reviewed_by",
    )
    list_filter = ("organization", "action", "status")
    search_fields = ("target_user__username", "requested_by__username", "reviewed_by__username")
    list_select_related = ("organization", "branch", "target_user", "requested_by", "reviewed_by")
    actions = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
