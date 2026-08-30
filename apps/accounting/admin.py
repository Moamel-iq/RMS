"""
Accounting in the admin: visible, never writable.

The admin is a developer and support inspection tool here, not the application
UI. Every accounting write goes through `apps.accounting.commands` and then the
kernel, and an editable admin form would be a second way in — one that skips
the validators, takes no idempotency key, resolves no scope, and writes no
audit event. A ledger with two write paths has, in practice, one write path
and one unexplained discrepancy.

So this is read-only for everyone, superusers included. That is deliberate and
it is not the same as saying a superuser has no emergency authority: they do,
through the same commands as everyone else, where the reason, the ordering,
and the audit trail still apply. What they do not have is a form that writes
around them.

The database backs this up regardless of what any admin class claims —
`accounting_journalentry_no_change` and `accounting_journalline_no_change`
refuse the UPDATE at the source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    AccountingSettings,
    AccountReportMapping,
    AccountRole,
    CostCenter,
    FiscalYear,
    ImportedChartAccount,
    JournalEntry,
    JournalLine,
    OrganizationAccountMapping,
)

if TYPE_CHECKING:
    # django-stubs types both generically. The runtime classes are not
    # subscriptable, and the plugin that normally infers the parameter from
    # `@admin.register` does not fire when a mixin precedes the base — so the
    # parameters are supplied here, for the type checker only.
    _ModelAdmin = admin.ModelAdmin[Any]
    _TabularInline = admin.TabularInline[JournalLine, JournalEntry]
else:
    _ModelAdmin = admin.ModelAdmin
    _TabularInline = admin.TabularInline


class ReadOnlyAdminMixin:
    """
    Look, do not touch.

    A mixin rather than a `ModelAdmin` subclass so each concrete admin below
    still declares its own model in the usual way. It must come *first* in the
    bases: the whole point is that these three methods win over Django's.

    `has_view_permission` is left to Django, so who may *see* accounting data
    is still governed by permissions. Only mutation is removed.
    """

    #: Annotated because `ModelAdmin.actions` is a sequence-or-None and a bare
    #: `= None` here would narrow it to `None` and clash with the base.
    actions: Any = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class JournalLineInline(_TabularInline):
    model = JournalLine
    extra = 0
    can_delete = False
    fields = ("line_number", "account", "branch", "cost_center", "debit", "credit", "narration")
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "entry_number",
        "status",
        "accounting_date",
        "organization",
        "period",
        "source_document_type",
        "source_document_id",
        "source_event",
    )
    list_filter = ("status", "source_event", "is_adjustment", "organization")
    search_fields = (
        "entry_number",
        "narration",
        "idempotency_key",
        "source_document_id",
    )
    date_hierarchy = "accounting_date"
    list_select_related = ("organization", "period")
    ordering = ("-accounting_date", "-id")
    inlines = [JournalLineInline]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "organization",
                    "period",
                    "entry_number",
                    "status",
                    "accounting_date",
                    "document_date",
                    "is_adjustment",
                    "narration",
                )
            },
        ),
        (
            _("Source identity"),
            {
                "fields": (
                    "source_document_type",
                    "source_document_id",
                    "source_event",
                    "idempotency_key",
                ),
                "description": _(
                    "Organization, type, id, and event together identify one upstream "
                    "economic event. Immutable once posted — the database refuses the "
                    "UPDATE."
                ),
            },
        ),
        (_("Posting"), {"fields": ("posted_at", "posted_by", "reverses")}),
    )

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return tuple(field.name for field in self.model._meta.fields)


@admin.register(JournalLine)
class JournalLineAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = ("entry", "line_number", "account", "branch", "cost_center", "debit", "credit")
    list_filter = ("branch", "cost_center")
    search_fields = ("entry__entry_number", "account__code", "narration")
    list_select_related = ("entry", "account", "branch", "cost_center")
    ordering = ("-entry_id", "line_number")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return tuple(field.name for field in self.model._meta.fields)


@admin.register(Account)
class AccountAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """
    Read-only although accounts are mutable master data.

    `create_account` derives the class and the parent from the code and checks
    the parentage rules; a form that let someone type a class would let the
    code, the class, and the tree disagree.

    **There is no account command endpoint to use instead — yet.** This
    docstring used to send the reader to one, and Task 0.7 §4's table implied
    it existed, but §3 never listed one and `api.py` has never had one. Today
    the chart is created by `seed_chart_of_accounts`, which calls
    `services.create_account` directly. A managed surface for the chart is
    Phase 5 work, and `manage_accounts` is the permission waiting for it.
    """

    list_display = (
        "code",
        "name",
        "account_class",
        "is_postable",
        "requires_cost_center",
        "is_active",
        "organization",
    )
    list_filter = ("account_class", "is_postable", "requires_cost_center", "is_active")
    search_fields = ("code", "name", "external_account_code")
    ordering = ("organization__code", "code")
    list_select_related = ("organization", "parent")


@admin.register(ImportedChartAccount)
class ImportedChartAccountAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "source_code",
        "name",
        "parent",
        "is_leaf",
        "source_system",
        "organization",
    )
    list_filter = ("source_system", "organization", "is_leaf")
    search_fields = ("source_code", "name")
    ordering = ("organization__code", "source_system", "source_code")
    list_select_related = ("organization", "parent")


@admin.register(CostCenter)
class CostCenterAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = ("code", "name", "organization", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("code", "name")
    ordering = ("organization__code", "code")
    list_select_related = ("organization",)


@admin.register(AccountingPeriod)
class AccountingPeriodAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """
    Read-only: a period's state is changed by the close, soft-close, and reopen
    commands, each of which enforces ordering, a reason, and an audit event. A
    dropdown here would offer none of that.
    """

    list_display = ("__str__", "fiscal_year", "period_number", "start_date", "end_date", "state")
    list_filter = ("state", "fiscal_year")
    ordering = ("fiscal_year__year", "period_number")
    list_select_related = ("fiscal_year",)


@admin.register(FiscalYear)
class FiscalYearAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = ("year", "organization", "start_date", "end_date", "is_closed")
    list_filter = ("organization",)
    ordering = ("organization__code", "-year")
    list_select_related = ("organization",)

    @admin.display(boolean=True, description=_("closed"))
    def is_closed(self, obj: FiscalYear) -> bool:
        """Derived from the periods, never stored (ADR-013)."""
        return obj.is_closed


@admin.register(AccountingSettings)
class AccountingSettingsAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = ("organization", "fiscal_year_start_month")
    list_select_related = ("organization",)


@admin.register(AccountRole)
class AccountRoleAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """System vocabulary. Renaming or deleting a system role is refused by
    the database trigger regardless of what any admin form attempted."""

    list_display = ("code", "name", "domain", "mapping_scope", "is_system", "is_active")
    list_filter = ("domain", "mapping_scope", "is_system")
    search_fields = ("code", "name")
    ordering = ("domain", "code")


@admin.register(OrganizationAccountMapping)
class OrganizationAccountMappingAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    list_display = (
        "organization",
        "account_role",
        "account",
        "effective_from",
        "effective_to",
        "version",
        "is_active",
    )
    list_filter = ("organization", "account_role", "is_active")
    search_fields = ("account_role__code", "account__code")
    ordering = ("organization__code", "account_role__code", "-version")
    list_select_related = ("organization", "account_role", "account")


@admin.register(AccountReportMapping)
class AccountReportMappingAdmin(ReadOnlyAdminMixin, _ModelAdmin):
    """
    Which statement group each account lands in (ADR-031).

    Read-only here for the same reason the chart is. `set_report_mapping`
    refuses a rollup — a fact about other rows that no form field could
    express — and clears rather than deletes, so the classification a
    statement was produced under stays readable. A dropdown here would offer
    neither.
    """

    list_display = (
        "account",
        "statement_group",
        "presentation_section",
        "display_order",
        "is_active",
        "organization",
    )
    list_filter = ("statement_group", "presentation_section", "is_active", "organization")
    search_fields = ("account__code", "account__name")
    ordering = ("organization__code", "statement_group", "display_order")
    list_select_related = ("organization", "account")
