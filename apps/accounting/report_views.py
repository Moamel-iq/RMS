"""
ميزان المراجعة · دفتر الأستاذ · قائمة الدخل · الميزانية العمومية.

Read-only, every one of them. There is no repair control on any of these
screens: where a report disagrees with itself it says by how much and stops,
because a plug entry that made the two sides agree would make the books wrong
and would do it without anyone reading the difference that explained the cause.

**HTML and CSV call the same service.** The export is the rows the screen just
built, not a second query — two query paths drift, and the CSV is the one
nobody looks at until an auditor does.
"""

from __future__ import annotations

import csv
import datetime
from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.models import Account, AccountClass, CostCenter
from apps.accounting.permissions import VIEW_CHART_OF_ACCOUNTS, VIEW_JOURNAL
from apps.accounting.reports import (
    ReportFilters,
    balance_sheet,
    general_ledger,
    income_statement,
    trial_balance,
)
from apps.accounting.views import AccountingViewMixin
from apps.core.money import money_audit_with_currency
from apps.core.printing import PrintableReportMixin, PrintSheet, SheetFilter, sheet_from_table
from apps.core.templatetags.report_tags import render_value
from apps.inventory.report_views import neutralise, safe_filename
from apps.organizations.authorization import OutOfScope, organizations_with_permission
from apps.organizations.models import Organization
from apps.organizations.selectors import accessible_branches

ZERO = Decimal("0")


class AccountingReportView(PrintableReportMixin, AccountingViewMixin, View):
    """
    Shared chrome for the four reports: scope, filters, fragment contract, CSV.

    Every subclass supplies `build`, which returns the context. Nothing here
    knows what a trial balance is; nothing in `reports.py` knows what a request
    is.
    """

    required_permission = VIEW_JOURNAL
    template_name = ""
    export_stem = "accounting-report"
    page_title: Any = ""
    page_hint: Any = ""

    def filters(self, request: HttpRequest, organization: Organization) -> ReportFilters:
        branch = None
        raw_branch = request.GET.get("branch", "").strip()
        if raw_branch.isdigit():
            branch = accessible_branches(self.actor).filter(pk=int(raw_branch)).first()
            if branch is None:
                raise OutOfScope(_("Branch does not exist."))

        cost_center = None
        raw_centre = request.GET.get("cost_center", "").strip()
        if raw_centre.isdigit():
            cost_center = CostCenter.objects.filter(
                pk=int(raw_centre), organization=organization
            ).first()

        return ReportFilters(
            organization=organization,
            branch=branch,
            cost_center=cost_center,
            date_from=_date(request.GET.get("from", "")),
            date_to=_date(request.GET.get("to", "")),
            account_class=request.GET.get("account_class", "").strip(),
            code_from=request.GET.get("code_from", "").strip(),
            code_to=request.GET.get("code_to", "").strip(),
            include_zero=request.GET.get("include_zero") == "1",
        )

    def organization(self, request: HttpRequest) -> Organization | None:
        organizations = organizations_with_permission(self.actor, VIEW_JOURNAL).order_by("code")
        raw = request.GET.get("organization", "").strip()
        if raw.isdigit():
            found = organizations.filter(pk=int(raw)).first()
            if found is None:
                raise OutOfScope(_("Organization does not exist."))
            return found
        return organizations.first()

    def build(self, request: HttpRequest, filters: ReportFilters) -> dict[str, Any]:
        raise NotImplementedError  # pragma: no cover - every subclass supplies it

    def csv_rows(self, context: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
        raise NotImplementedError  # pragma: no cover

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations = organizations_with_permission(self.actor, VIEW_JOURNAL).order_by("code")
        organization = self.organization(request)
        if organization is None:
            return render(
                request,
                self.template_name,
                {
                    "organizations": organizations,
                    "organization": None,
                    "page_title": self.page_title,
                    "page_hint": self.page_hint,
                    "list_base_template": self._base(request),
                    "inventory_ui": False,
                },
            )

        filters = self.filters(request, organization)
        context = self.build(request, filters)
        context.update(
            {
                "organizations": organizations,
                "organization": organization,
                "filters": filters,
                "filter_query": "&".join(f"{k}={v}" for k, v in filters.as_query().items()),
                "branches": accessible_branches(self.actor).order_by("code"),
                "cost_centers": CostCenter.objects.filter(
                    organization=organization, is_active=True
                ).order_by("code"),
                "account_classes": AccountClass.choices,
                "page_title": self.page_title,
                "page_hint": self.page_hint,
                "list_base_template": self._base(request),
                "inventory_ui": False,
            }
        )

        if request.GET.get("export") == "csv":
            return self.export_csv(context, filters)
        if self.wants_print(request):
            return self.render_print(request, context, filters)
        return render(request, self.template_name, context)

    def _base(self, request: HttpRequest) -> str:
        return (
            "settings/_form_fragment.html"
            if request.headers.get("HX-Request") == "true"
            else "shell.html"
        )

    #: Arabic names for the filters, so a sheet says "من 2026-08-01" and never
    #: "date_from=2026-08-01".
    FILTER_LABELS: dict[str, Any] = {
        "branch": _("الفرع"),
        "cost_center": _("مركز الكلفة"),
        "from": _("من"),
        "to": _("إلى"),
        "account_class": _("صنف الحساب"),
        "code_from": _("من رمز"),
        "code_to": _("إلى رمز"),
        "include_zero": _("يشمل الأرصدة الصفرية"),
    }

    def print_sheet(self, context: dict[str, Any], filters: ReportFilters) -> PrintSheet:
        """
        The sheet is the export, on paper.

        `csv_rows` is where each report already says what its columns and rows
        are; printing from anywhere else would be a second answer to a question
        that has one.
        """
        headers, rows = self.csv_rows(context)
        branch = filters.branch
        return sheet_from_table(
            title=str(self.page_title),
            headers=headers,
            table=rows,
            renderer=_ledger_value,
            organization_label=f"{filters.organization.code} — {filters.organization.name_ar}",
            branch_label=f"{branch.code} — {branch.name_ar}" if branch else "",
            period_label=self.period_label(filters),
            filters=self.sheet_filters(filters),
            note=str(self.page_hint),
        )

    def period_label(self, filters: ReportFilters) -> str:
        if filters.date_from and filters.date_to:
            return str(_("الفترة من %(from)s إلى %(to)s")) % {
                "from": filters.date_from.isoformat(),
                "to": filters.date_to.isoformat(),
            }
        if filters.date_to:
            return str(_("حتى %(to)s")) % {"to": filters.date_to.isoformat()}
        if filters.date_from:
            return str(_("من %(from)s")) % {"from": filters.date_from.isoformat()}
        return ""

    def sheet_filters(self, filters: ReportFilters) -> list[SheetFilter]:
        skip = {"organization", "from", "to", "branch"}
        out: list[SheetFilter] = []
        for key, value in filters.as_query().items():
            if key in skip:
                continue
            label = self.FILTER_LABELS.get(key, key)
            if key == "cost_center" and filters.cost_center is not None:
                value = f"{filters.cost_center.code} — {filters.cost_center.name_ar}"
            out.append(SheetFilter(label=str(label), value=str(value)))
        return out

    def export_csv(self, context: dict[str, Any], filters: ReportFilters) -> HttpResponse:
        """
        The same rows the screen just built.

        UTF-8 with a BOM, because without it Excel on Windows opens Arabic as
        mojibake and a report nobody can read is a report nobody uses. Every
        cell goes through `neutralise`, so a value beginning `=`, `+`, `-` or
        `@` cannot execute on the machine of whoever opens it.
        """
        headers, rows = self.csv_rows(context)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="{safe_filename(self.export_stem)}"'
        )
        response.write("﻿")
        writer = csv.writer(response, lineterminator="\n")
        writer.writerow([neutralise(_("تقرير")), neutralise(self.page_title)])
        writer.writerow([neutralise(_("المؤسسة")), neutralise(filters.organization.code)])
        writer.writerow([neutralise(_("وقت التصدير")), neutralise(timezone.localtime())])
        writer.writerow(
            [
                neutralise(_("المرشحات")),
                neutralise("; ".join(f"{k}={v}" for k, v in filters.as_query().items())),
            ]
        )
        writer.writerow([])
        writer.writerow([neutralise(header) for header in headers])
        for row in rows:
            writer.writerow([neutralise(cell) for cell in row])
        return response


def _ledger_value(value: Any) -> Any:
    """
    Money the way the ledger screens write it — grouped, at stored precision.

    `money_audit_with_currency` is what `money_full` puts on the trial balance and the general
    ledger, so the paper and the screen carry the same figure in the same shape.
    Everything else falls through to the shared report renderer.
    """
    if isinstance(value, Decimal):
        return money_audit_with_currency(value)
    return render_value(value)


def _date(raw: str) -> datetime.date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        # A typo in a URL is a typo. Answering 500 to it turns a typo into an
        # outage; the screen shows the window it actually used.
        return None


class TrialBalanceView(AccountingReportView):
    template_name = "accounting/trial_balance.html"
    export_stem = "trial-balance"
    page_title = _("ميزان المراجعة")
    page_hint = _(
        "من القيود المُرحَّلة وحدها. مجموع المدين الختامي يساوي مجموع الدائن "
        "الختامي على أي تركيبة مرشحات — وإن لم يساوِه، يُعرض الفرق ولا يُصلَح."
    )

    def build(self, request: HttpRequest, filters: ReportFilters) -> dict[str, Any]:
        return {"report": trial_balance(filters)}

    def csv_rows(self, context: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
        report = context["report"]
        headers = [
            str(_("رمز الحساب")),
            str(_("اسم الحساب")),
            str(_("افتتاحي مدين")),
            str(_("افتتاحي دائن")),
            str(_("حركة مدين")),
            str(_("حركة دائن")),
            str(_("ختامي مدين")),
            str(_("ختامي دائن")),
        ]
        rows = [
            [
                row.account.code,
                row.account.name_ar,
                row.opening_debit,
                row.opening_credit,
                row.period_debit,
                row.period_credit,
                row.closing_debit,
                row.closing_credit,
            ]
            for row in report.rows
        ]
        rows.append(
            [
                "",
                str(_("المجموع")),
                report.opening_debit,
                report.opening_credit,
                report.period_debit,
                report.period_credit,
                report.closing_debit,
                report.closing_credit,
            ]
        )
        return headers, rows


class GeneralLedgerView(AccountingReportView):
    template_name = "accounting/general_ledger.html"
    export_stem = "general-ledger"
    required_permission = VIEW_CHART_OF_ACCOUNTS
    page_title = _("دفتر الأستاذ")
    page_hint = _(
        "الترتيب: تاريخ العملية ثم وقت الترحيل ثم رقم القيد ثم رقم السطر. "
        "الرصيد المتحرك يُحسب في الخدمة، لا في القالب."
    )

    def build(self, request: HttpRequest, filters: ReportFilters) -> dict[str, Any]:
        account = None
        raw = request.GET.get("account", "").strip()
        if raw.isdigit():
            account = Account.objects.filter(pk=int(raw), organization=filters.organization).first()
            if account is None:
                raise OutOfScope(_("Account does not exist."))
        return {
            "report": general_ledger(
                filters,
                account=account,
                source_type=request.GET.get("source_type", "").strip(),
                origin=request.GET.get("origin", "").strip(),
            ),
            "selected_account": account,
            "accounts": Account.objects.filter(
                organization=filters.organization, is_postable=True, is_active=True
            ).order_by("code"),
            "selected_source_type": request.GET.get("source_type", ""),
            "selected_origin": request.GET.get("origin", ""),
        }

    def csv_rows(self, context: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
        report = context["report"]
        headers = [
            str(_("التاريخ")),
            str(_("رقم القيد")),
            str(_("المصدر")),
            str(_("الحساب")),
            str(_("الفرع")),
            str(_("مركز الكلفة")),
            str(_("البيان")),
            str(_("مدين")),
            str(_("دائن")),
            str(_("الرصيد")),
        ]
        rows = [
            [
                row.line.entry.accounting_date,
                row.line.entry.entry_number,
                row.line.entry.source_document_type or str(_("يدوي")),
                row.line.account.code,
                row.line.branch.code,
                row.line.cost_center.code if row.line.cost_center else "",
                row.line.narration or row.line.entry.narration,
                row.line.debit,
                row.line.credit,
                row.running,
            ]
            for row in report.rows
        ]
        return headers, rows


class IncomeStatementView(AccountingReportView):
    template_name = "accounting/income_statement.html"
    export_stem = "income-statement"
    page_title = _("التقرير المالي الشامل")
    page_hint = _(
        "التصنيف من ربط القوائم المالية، لا من رمز الحساب. الحسابات غير المصنّفة "
        "ذات الرصيد تُعرض في قسم «غير مصنّف» وتمنع الاعتماد — ولا تُحذف."
    )

    def build(self, request: HttpRequest, filters: ReportFilters) -> dict[str, Any]:
        today = timezone.localdate()
        date_to = filters.date_to or today
        date_from = filters.date_from or date_to.replace(month=1, day=1)
        report = income_statement(filters, date_from=date_from, date_to=date_to)
        return {
            "report": report,
            # The template renders sections in order from a list the view
            # builds: a Django `for` tag cannot take a tuple literal, and
            # naming the order here keeps it in one place rather than repeated
            # across the page and the CSV.
            "trading_sections": [report.revenue, report.cost_of_sales],
            "other_sections": [report.other_income, report.other_expenses],
            "date_from": date_from,
            "date_to": date_to,
        }

    def csv_rows(self, context: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
        report = context["report"]
        headers = [str(_("القسم")), str(_("الحساب")), str(_("المبلغ"))]
        rows: list[list[Any]] = []
        for section in (
            report.revenue,
            report.cost_of_sales,
            report.operating_expenses,
            report.other_income,
            report.other_expenses,
        ):
            for account, balance in section.rows:
                rows.append([str(section.label), f"{account.code} {account.name_ar}", balance])
            rows.append([str(section.label), str(_("المجموع")), section.total])
        for account, balance in report.unmapped:
            rows.append([str(_("غير مصنّف")), f"{account.code} {account.name_ar}", balance])
        rows.append(["", str(_("مجمل الربح")), report.gross_profit])
        rows.append(["", str(_("الربح التشغيلي")), report.operating_profit])
        rows.append(["", str(_("صافي الربح")), report.net_profit])
        return headers, rows


class BalanceSheetView(AccountingReportView):
    template_name = "accounting/balance_sheet.html"
    export_stem = "balance-sheet"
    page_title = _("الميزانية العمومية")
    page_hint = _(
        "بتاريخ محدد. قبل إقفال السنة تتضمن حقوق الملكية سطر «أرباح السنة الحالية» "
        "محسوباً، فتتحقق المعادلة دون إقفال شهري لحسابات النتيجة."
    )

    def build(self, request: HttpRequest, filters: ReportFilters) -> dict[str, Any]:
        as_of = filters.date_to or timezone.localdate()
        year_start = as_of.replace(month=1, day=1)
        report = balance_sheet(filters, as_of=as_of, year_start=year_start)
        return {
            "report": report,
            "sections": [
                report.current_assets,
                report.non_current_assets,
                report.current_liabilities,
                report.non_current_liabilities,
                report.equity,
            ],
            "as_of": as_of,
            "year_start": year_start,
        }

    def csv_rows(self, context: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
        report = context["report"]
        headers = [str(_("القسم")), str(_("الحساب")), str(_("المبلغ"))]
        rows: list[list[Any]] = []
        for section in (
            report.current_assets,
            report.non_current_assets,
            report.current_liabilities,
            report.non_current_liabilities,
            report.equity,
        ):
            for account, balance in section.rows:
                rows.append([str(section.label), f"{account.code} {account.name_ar}", balance])
            rows.append([str(section.label), str(_("المجموع")), section.total])
        rows.append(["", str(_("أرباح السنة الحالية")), report.current_year_earnings])
        for account, balance in report.unmapped:
            rows.append([str(_("غير مصنّف")), f"{account.code} {account.name_ar}", balance])
        rows.append(["", str(_("مجموع الأصول")), report.assets])
        rows.append(
            ["", str(_("مجموع المطلوبات وحقوق الملكية")), report.liabilities + report.equity_total]
        )
        rows.append(["", str(_("الفرق")), report.difference])
        return headers, rows


__all__ = [
    "AccountingReportView",
    "BalanceSheetView",
    "GeneralLedgerView",
    "IncomeStatementView",
    "TrialBalanceView",
]
