"""
The authorized way into the accounting reads: statements and both subledgers.

Read-only throughout. Every function here resolves the organization and any
filter identifier **with** the caller, then hands off to the service that owns
the calculation — `apps/accounting/reports.py` for the four statements,
Procurement's and Sales' own report services for the two subledgers.

**Nothing in this file computes a figure.** The supplier and application
workspaces are reconciliation views over documents that belong to other
modules (ADR-029 §2), so they forward to `supplier_aging` and `positions_for`
rather than deriving a position from the ledger a second time. A second
derivation agrees with the first right up until the day it does not, and then
there are two numbers and no way to tell which one is wrong.

The screens in `report_views.py` and `subledger_views.py` call these same
services. This module exists so the API reaches them under the same scope
rules, not so it can reach them a different way.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.utils.translation import gettext_lazy as _

from apps.accounting.models import AccountClass, CostCenter
from apps.accounting.permissions import (
    VIEW_APPLICATION_RECEIVABLES,
    VIEW_JOURNAL,
    VIEW_SUPPLIER_LIABILITIES,
)
from apps.accounting.reports import (
    BalanceSheet,
    IncomeStatement,
    Ledger,
    ReportFilters,
    TrialBalance,
    balance_sheet,
    general_ledger,
    income_statement,
    trial_balance,
)
from apps.organizations.authorization import (
    OutOfScope,
    organizations_with_permission,
    require_reachable_organization_permission,
)
from apps.organizations.models import Organization
from apps.organizations.selectors import accessible_branches
from apps.users.models import User

ZERO = Decimal("0")


def resolve_report_organization(*, actor: User, organization_id: int | None) -> Organization:
    """
    The organization a report is being asked about.

    Omitted means the caller's first one by code, which is what the screens do
    when a reader with a single organization opens a report and never touches
    the selector. An id that is not in scope is 404, not 403: a 403 would
    confirm the organization exists, and ids are sequential (ADR-016).
    """
    organizations = organizations_with_permission(actor, VIEW_JOURNAL).order_by("code")
    if organization_id is not None:
        found = organizations.filter(pk=organization_id).first()
        if found is None:
            raise OutOfScope(_("Organization %(id)s does not exist.") % {"id": organization_id})
        return found
    first = organizations.first()
    if first is None:
        raise OutOfScope(_("No organization in your scope reports accounting."))
    return first


def build_report_filters(
    *,
    actor: User,
    organization_id: int | None = None,
    branch_id: int | None = None,
    cost_center_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    account_class: str = "",
    code_from: str = "",
    code_to: str = "",
    include_zero: bool = False,
) -> ReportFilters:
    """Turn a request's filter parameters into a `ReportFilters` the caller may use."""
    organization = resolve_report_organization(actor=actor, organization_id=organization_id)

    branch = None
    if branch_id is not None:
        branch = accessible_branches(actor).filter(pk=branch_id).first()
        if branch is None:
            raise OutOfScope(_("Branch %(id)s does not exist.") % {"id": branch_id})
        if branch.organization_id != organization.pk:
            raise OutOfScope(_("Branch %(id)s does not exist.") % {"id": branch_id})

    cost_center = None
    if cost_center_id is not None:
        cost_center = CostCenter.objects.filter(
            pk=cost_center_id, organization=organization
        ).first()
        if cost_center is None:
            raise OutOfScope(_("Cost center %(id)s does not exist.") % {"id": cost_center_id})

    if account_class and account_class not in AccountClass.values:
        raise OutOfScope(_("Unknown account class %(value)s.") % {"value": account_class})

    return ReportFilters(
        organization=organization,
        branch=branch,
        cost_center=cost_center,
        date_from=date_from,
        date_to=date_to,
        account_class=account_class,
        code_from=code_from.strip(),
        code_to=code_to.strip(),
        include_zero=include_zero,
    )


def read_trial_balance(*, actor: User, filters: ReportFilters) -> TrialBalance:
    require_reachable_organization_permission(actor, VIEW_JOURNAL, filters.organization)
    return trial_balance(filters)


def read_general_ledger(
    *,
    actor: User,
    filters: ReportFilters,
    account_id: int | None = None,
    source_type: str = "",
    origin: str = "",
) -> Ledger:
    require_reachable_organization_permission(actor, VIEW_JOURNAL, filters.organization)
    from apps.accounting.models import Account

    account = None
    if account_id is not None:
        account = Account.objects.filter(organization=filters.organization, pk=account_id).first()
        if account is None:
            raise OutOfScope(_("Account %(id)s does not exist.") % {"id": account_id})
    return general_ledger(
        filters,
        account=account,
        # Upper-cased on the way in, because `canonical_source_identity` stores
        # it that way. A caller passing the natural spelling would otherwise
        # filter for a string the ledger does not contain and get an empty
        # report that looks like an answer (ADR-017).
        source_type=source_type.strip().upper(),
        origin=origin.strip(),
    )


def read_income_statement(
    *,
    actor: User,
    filters: ReportFilters,
    date_from: datetime.date,
    date_to: datetime.date,
) -> IncomeStatement:
    require_reachable_organization_permission(actor, VIEW_JOURNAL, filters.organization)
    return income_statement(filters, date_from=date_from, date_to=date_to)


def read_balance_sheet(
    *,
    actor: User,
    filters: ReportFilters,
    as_of: datetime.date,
    year_start: datetime.date,
) -> BalanceSheet:
    require_reachable_organization_permission(actor, VIEW_JOURNAL, filters.organization)
    return balance_sheet(filters, as_of=as_of, year_start=year_start)


# ---------------------------------------------------------------------------
# ذمم الموردين · ذمم التطبيقات — the two reconciliation workspaces
# ---------------------------------------------------------------------------
#
# Read/reconciliation only, both of them. No balance is stored, no repair is
# offered, and a disagreement is reported rather than corrected (ADR-029 §3).


def read_supplier_liabilities(
    *, actor: User, organization_id: int | None = None
) -> tuple[Organization, list[dict[str, Any]]]:
    """Supplier positions, from Procurement's own aging service."""
    from apps.procurement.reports import ProcurementReportFilters, supplier_aging

    organizations = organizations_with_permission(actor, VIEW_SUPPLIER_LIABILITIES).order_by("code")
    organization = (
        organizations.filter(pk=organization_id).first()
        if organization_id is not None
        else organizations.first()
    )
    if organization is None:
        raise OutOfScope(_("No organization in your scope reports supplier liabilities."))
    require_reachable_organization_permission(actor, VIEW_SUPPLIER_LIABILITIES, organization)

    rows = supplier_aging(
        actor,
        ProcurementReportFilters(organization_id=organization.pk),
        include_cost=True,
    )
    return organization, list(rows)


def read_application_receivables(
    *, actor: User, organization_id: int | None = None, as_of: datetime.date | None = None
) -> tuple[Organization, list[Any]]:
    """Delivery-application positions, from Sales' own receivable ledger."""
    from django.utils import timezone

    from apps.sales.receivables import positions_for

    organizations = organizations_with_permission(actor, VIEW_APPLICATION_RECEIVABLES).order_by(
        "code"
    )
    organization = (
        organizations.filter(pk=organization_id).first()
        if organization_id is not None
        else organizations.first()
    )
    if organization is None:
        raise OutOfScope(_("No organization in your scope reports application receivables."))
    require_reachable_organization_permission(actor, VIEW_APPLICATION_RECEIVABLES, organization)

    positions = positions_for(
        actor,
        organization_id=organization.pk,
        as_of=as_of or timezone.localdate(),
    )
    return organization, list(positions)


__all__ = [
    "build_report_filters",
    "read_application_receivables",
    "read_balance_sheet",
    "read_general_ledger",
    "read_income_statement",
    "read_supplier_liabilities",
    "read_trial_balance",
    "resolve_report_organization",
]
