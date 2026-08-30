"""Accounting workspaces added by the consolidated Arabic accounting UI."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.models import (
    Account,
    AccountClass,
    CostCenter,
    ImportedChartAccount,
    JournalEntryStatus,
)
from apps.accounting.permissions import VIEW_CHART_OF_ACCOUNTS, VIEW_JOURNAL
from apps.accounting.selectors import account_balances
from apps.accounting.views import AccountingViewMixin
from apps.organizations.authorization import organizations_with_permission
from apps.procurement.views import AdditionalCostListView

ZERO = Decimal("0")
MONEY_FIELD = DecimalField(max_digits=24, decimal_places=5)


def _base(request: HttpRequest) -> str:
    return (
        "settings/_form_fragment.html"
        if request.headers.get("HX-Request") == "true"
        else "shell.html"
    )


def _organization_for(request: HttpRequest, actor: Any, permission: str) -> tuple[Any, Any]:
    organizations = organizations_with_permission(actor, permission).order_by("code")
    selected = request.GET.get("organization", "").strip()
    organization = (
        organizations.filter(pk=int(selected)).first()
        if selected.isdigit()
        else organizations.first()
    )
    if selected.isdigit() and organization is None:
        raise Http404(_("Organization does not exist."))
    return organizations, organization


class ImportedChartTreeView(AccountingViewMixin, View):
    """The approved account hierarchy, stripped of workbook-period figures."""

    required_permission = VIEW_CHART_OF_ACCOUNTS
    template_name = "accounting/imported_chart_tree.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations, organization = _organization_for(request, self.actor, VIEW_CHART_OF_ACCOUNTS)
        sources = (
            list(
                ImportedChartAccount.objects.filter(organization=organization)
                .values_list("source_system", flat=True)
                .distinct()
                .order_by("source_system")
            )
            if organization is not None
            else []
        )
        selected_source = request.GET.get("source", "").strip()
        source = selected_source if selected_source in sources else (sources[0] if sources else "")
        rows = (
            ImportedChartAccount.objects.filter(organization=organization, source_system=source)
            if organization is not None and source
            else ImportedChartAccount.objects.none()
        )
        context = {
            "organizations": organizations,
            "organization": organization,
            "sources": sources,
            "source": source,
            "roots": rows.filter(parent__isnull=True)
            .annotate(child_count=Count("children"))
            .order_by("source_code"),
            "account_count": rows.count(),
            "page_title": _("الشجرة المحاسبية"),
            "page_hint": _(
                "الشجرة المعتمدة للحسابات. حُفظت الرموز والأسماء والهيكل فقط، "
                "وجميع معلومات وأرصدة ملف الاستيراد مصفّرة."
            ),
            "list_base_template": _base(request),
            "inventory_ui": False,
        }
        return render(request, self.template_name, context)


class ImportedChartChildrenView(AccountingViewMixin, View):
    required_permission = VIEW_CHART_OF_ACCOUNTS
    template_name = "accounting/_imported_chart_children.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations = organizations_with_permission(self.actor, VIEW_CHART_OF_ACCOUNTS)
        parent = ImportedChartAccount.objects.filter(
            pk=kwargs["pk"], organization__in=organizations
        ).first()
        if parent is None:
            raise Http404
        return render(
            request,
            self.template_name,
            {
                "parent": parent,
                "children": parent.children.annotate(child_count=Count("children")).order_by(
                    "source_code"
                ),
            },
        )


class AssetOverviewView(AccountingViewMixin, View):
    """A read-only asset workspace derived from posted ledger lines."""

    required_permission = VIEW_CHART_OF_ACCOUNTS
    template_name = "accounting/asset_overview.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations, organization = _organization_for(request, self.actor, VIEW_CHART_OF_ACCOUNTS)
        raw_as_of = request.GET.get("as_of", "").strip()
        try:
            as_of = datetime.date.fromisoformat(raw_as_of) if raw_as_of else timezone.localdate()
        except ValueError:
            as_of = timezone.localdate()
        accounts = (
            list(
                Account.objects.filter(
                    organization=organization,
                    account_class=AccountClass.ASSET,
                    is_active=True,
                    is_postable=True,
                ).order_by("code")
            )
            if organization is not None
            else []
        )
        balances: dict[int, Decimal] = (
            account_balances(organization=organization, up_to=as_of, accounts=accounts)
            if organization is not None
            else {}
        )
        rows = [
            {"account": account, "balance": balances.get(account.pk, ZERO)} for account in accounts
        ]
        asset_total = sum((balances.get(account.pk, ZERO) for account in accounts), ZERO)
        active_count = sum(1 for account in accounts if balances.get(account.pk, ZERO) != ZERO)
        return render(
            request,
            self.template_name,
            {
                "organizations": organizations,
                "organization": organization,
                "as_of": as_of,
                "rows": rows,
                "asset_total": asset_total,
                "active_count": active_count,
                "page_title": _("الأصول"),
                "page_hint": _(
                    "أرصدة الأصول التشغيلية مشتقة من القيود المرحّلة حتى التاريخ المختار."
                ),
                "list_base_template": _base(request),
                "inventory_ui": False,
            },
        )


class CostCenterListView(AccountingViewMixin, View):
    """Cost centres and their posted movement, without creating a second ledger."""

    required_permission = VIEW_JOURNAL
    template_name = "accounting/cost_center_list.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations, organization = _organization_for(request, self.actor, VIEW_JOURNAL)
        search = request.GET.get("q", "").strip()
        centers = (
            CostCenter.objects.filter(organization=organization)
            if organization
            else CostCenter.objects.none()
        )
        if search:
            centers = centers.filter(Q(code__icontains=search) | Q(name__icontains=search))
        centers = (
            centers.annotate(
                posted_debit=Coalesce(
                    Sum(
                        "journal_lines__debit",
                        filter=Q(
                            journal_lines__entry__status__in=[
                                JournalEntryStatus.POSTED,
                                JournalEntryStatus.REVERSED,
                            ]
                        ),
                    ),
                    ZERO,
                    output_field=MONEY_FIELD,
                ),
                posted_credit=Coalesce(
                    Sum(
                        "journal_lines__credit",
                        filter=Q(
                            journal_lines__entry__status__in=[
                                JournalEntryStatus.POSTED,
                                JournalEntryStatus.REVERSED,
                            ]
                        ),
                    ),
                    ZERO,
                    output_field=MONEY_FIELD,
                ),
                posted_lines=Count(
                    "journal_lines",
                    filter=Q(
                        journal_lines__entry__status__in=[
                            JournalEntryStatus.POSTED,
                            JournalEntryStatus.REVERSED,
                        ]
                    ),
                ),
            )
            .annotate(
                net_movement=ExpressionWrapper(
                    F("posted_debit") - F("posted_credit"), output_field=MONEY_FIELD
                )
            )
            .order_by("code")
        )
        return render(
            request,
            self.template_name,
            {
                "organizations": organizations,
                "organization": organization,
                "centers": centers,
                "search": search,
                "page_title": _("مراكز التكلفة"),
                "page_hint": _(
                    "البعد الإداري للربحية والمصروفات. القيم مشتقة من سطور القيود المرحّلة ولا تُخزَّن كأرصدة مستقلة."
                ),
                "list_base_template": _base(request),
                "inventory_ui": False,
            },
        )


class AccountingAdditionalCostListView(AdditionalCostListView):
    """The purchasing cost workspace, presented inside Accounting navigation."""

    module_key = "accounting"


__all__ = [
    "AccountingAdditionalCostListView",
    "AssetOverviewView",
    "CostCenterListView",
    "ImportedChartChildrenView",
    "ImportedChartTreeView",
]
