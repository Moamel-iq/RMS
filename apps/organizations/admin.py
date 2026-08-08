"""Admin registration for the organization hierarchy."""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import Branch, BranchMembership, Organization


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
    list_display = ("user", "branch", "role", "is_active", "created_at")
    list_filter = ("is_active", "role", "branch")
    search_fields = ("user__username", "user__phone", "branch__code")
    ordering = ("branch__code", "user__username")
    list_select_related = ("user", "branch")
    autocomplete_fields = ("user", "branch")
