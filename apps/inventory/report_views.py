"""
The report screens, and the one export path they share.

Nine reports, one view class. Each subclass says which permission it needs,
which report function produces its rows, and which columns a CSV gets; the
filtering, the scope, the valuation redaction, the htmx partial, the
pagination and the export all come from here, once.

That is the point rather than tidiness. §E requires an export to show exactly
what its screen shows — same queryset, same filters, same redaction — and the
only way to *guarantee* that is for both to be the same call. A separate export
view that rebuilt the query would be correct on the day it was written and
drift by the second filter somebody added.

## Redaction

`include_valuation` is computed once, from `inventory.view_valuation`, and
passed to the report function, which omits the cost keys entirely rather than
blanking them. The template renders whatever keys exist and the CSV writer
writes whatever columns exist, so neither can leak a figure the other hides.
A storekeeper who exports this gets a file with no cost column, not a file
with empty cost cells.
"""

from __future__ import annotations

import csv
import datetime
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from apps.inventory import reports
from apps.inventory.permissions import VIEW_STOCK, VIEW_VALUATION
from apps.inventory.reports import ReportFilters, ReportMode, resolve_mode
from apps.inventory.views import InventoryViewMixin
from apps.organizations.selectors import accessible_branches

#: Cells starting with one of these are read as a formula by Excel and by
#: Sheets. A file exported from here is opened by whoever asked for it, so an
#: item named `=cmd|...` would run on their machine, not ours. Prefixed with a
#: single quote, which both applications treat as "this is text".
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def neutralise(value: object) -> str:
    """
    One cell, rendered so a spreadsheet cannot execute it.

    Decimals go through `str` and never through `float`: the whole point of
    storing three decimal places exactly is lost if the export round-trips
    through binary.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # Exact, unlocalised, and never scientific notation.
        return format(value, "f")
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value)
    if text.startswith(FORMULA_TRIGGERS):
        return f"'{text}"
    return text


def safe_filename(stem: str, *, extension: str = "csv") -> str:
    """
    An ASCII filename nothing can traverse, quote-escape, or hide behind.

    Built from a constant stem and a timestamp rather than from anything the
    user typed, so there is no path, no separator, and no RTL override
    character to smuggle in.
    """
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    cleaned = "".join(character for character in stem if character.isalnum() or character in "-_")
    return f"{cleaned or 'report'}-{stamp}.{extension}"


class InventoryReportView(InventoryViewMixin, View):
    """
    A scoped, filtered, paginated report with a CSV export on the same query.

    Subclasses set `report`, `columns`, `template_name`, `page_title` and the
    export stem. Everything else is shared.
    """

    required_permission: str = VIEW_STOCK
    template_name: str = ""
    page_title: Any = ""
    page_hint: Any = ""
    export_stem: str = "inventory-report"
    paginate_by: int = 50
    #: Whether this report offers the posted-as-of / effective-date switch.
    supports_modes: bool = False
    #: (context key, Arabic header) pairs. Valuation columns are declared
    #: separately so they can be dropped as a unit.
    columns: Sequence[tuple[str, Any]] = ()
    valuation_columns: Sequence[tuple[str, Any]] = ()

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    # -- request plumbing --------------------------------------------------

    @property
    def include_valuation(self) -> bool:
        return bool(self.request.user.has_perm(VIEW_VALUATION))

    def _int(self, name: str) -> int | None:
        raw = self.request.GET.get(name, "").strip()
        if not raw.isdigit():
            return None
        return int(raw)

    def _date(self, name: str) -> datetime.date | None:
        raw = self.request.GET.get(name, "").strip()
        if not raw:
            return None
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            # A malformed date is not an error worth a 400: the filter simply
            # does not apply, and the field shows what was typed.
            return None

    def build_filters(self) -> ReportFilters:
        return ReportFilters(
            organization_id=self._int("organization_id"),
            branch_id=self._int("branch_id"),
            warehouse_id=self._int("warehouse_id"),
            category_id=self._int("category_id"),
            item_id=self._int("item_id"),
            lot_id=self._int("lot_id"),
            cost_center_id=self._int("cost_center_id"),
            reason_code_id=self._int("reason_code_id"),
            include_inactive=self.request.GET.get("include_inactive") == "1",
            date_from=self._date("date_from"),
            date_to=self._date("date_to"),
            within_days=self._int("within_days"),
            mode=resolve_mode(self.request.GET.get("mode"))
            if self.supports_modes
            else ReportMode.POSTED_AS_OF,
            search=self.request.GET.get("q", "").strip(),
        )

    def is_htmx(self) -> bool:
        return self.request.headers.get("HX-Request") == "true"

    def active_columns(self) -> list[tuple[str, Any]]:
        columns = list(self.columns)
        if self.include_valuation:
            columns += list(self.valuation_columns)
        return columns

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        filters = self.build_filters()
        rows = self.report_rows(filters, include_valuation=self.include_valuation)

        if request.GET.get("export") == "csv":
            return self.export_csv(rows, filters)

        paginator = Paginator(rows, self.paginate_by)
        page = paginator.get_page(request.GET.get("page"))

        return render(
            request,
            self.template_name,
            {
                "page_title": self.page_title,
                "page_hint": self.page_hint,
                "rows": page.object_list,
                "columns": self.active_columns(),
                "page_obj": page,
                "is_paginated": page.has_other_pages(),
                "paginator": paginator,
                "total_rows": paginator.count,
                "search": filters.search,
                "filters": filters,
                "show_cost": self.include_valuation,
                "supports_modes": self.supports_modes,
                "mode": filters.mode,
                "mode_label": filters.mode.label,
                "modes": [(mode.value, mode.label) for mode in ReportMode],
                "branches": accessible_branches(self.actor),
                "export_query": self._export_query(filters),
                "htmx_list": True,
                "list_base_template": (
                    "settings/_list_fragment.html" if self.is_htmx() else "shell.html"
                ),
            },
        )

    def _export_query(self, filters: ReportFilters) -> str:
        pairs = dict(filters.as_query())
        pairs["export"] = "csv"
        return urlencode(pairs)

    # -- export ------------------------------------------------------------

    def export_csv(self, rows: list[dict[str, Any]], filters: ReportFilters) -> HttpResponse:
        """
        The same rows the screen just built, as CSV.

        UTF-8 with a BOM: without it Excel on Windows opens Arabic as mojibake,
        and a report nobody can read is a report nobody uses.
        """
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="{safe_filename(self.export_stem)}"'
        )
        response.write("﻿")

        writer = csv.writer(response, lineterminator="\n")
        columns = self.active_columns()

        # Provenance, so a file found later can be trusted or discarded.
        writer.writerow([neutralise(_("تقرير")), neutralise(self.page_title)])
        writer.writerow([neutralise(_("وقت التصدير")), neutralise(timezone.localtime())])
        writer.writerow([neutralise(_("وضع التقرير")), neutralise(filters.mode.label)])
        writer.writerow(
            [
                neutralise(_("المرشحات")),
                neutralise("; ".join(f"{k}={v}" for k, v in filters.as_query().items())),
            ]
        )
        writer.writerow([])

        writer.writerow([neutralise(header) for _key, header in columns])
        for row in rows:
            writer.writerow([neutralise(row.get(key)) for key, _header in columns])
        return response


# ---------------------------------------------------------------------------
# The nine reports
# ---------------------------------------------------------------------------


class StockValuationReportView(InventoryReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("تقرير تقييم المخزون")
    page_hint = _(
        "الكمية والقيمة لكل مخزن وصنف ودفعة. بدون نافذة تاريخ يُقرأ الرصيد الحالي؛ "
        "مع نافذة تاريخ تُطوى الحركات حسب الوضع المختار."
    )
    export_stem = "stock-valuation"
    supports_modes = True
    columns = (
        ("branch_code", _("الفرع")),
        ("warehouse_code", _("المخزن")),
        ("category_code", _("المجموعة")),
        ("item_code", _("الصنف")),
        ("item_name", _("الاسم")),
        ("lot_code", _("الدفعة")),
        ("unit", _("الوحدة")),
        ("quantity", _("الكمية")),
        ("last_movement_at", _("آخر حركة")),
        ("last_posted_sequence", _("آخر تسلسل ترحيل")),
    )
    valuation_columns = (
        ("average_cost", _("متوسط الكلفة")),
        ("value", _("القيمة")),
        ("control_account", _("حساب المراقبة")),
    )

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        return reports.stock_valuation(self.actor, filters, include_valuation=include_valuation)


class StockCardReportView(InventoryReportView):
    """
    One position's ledger. Always ordered by posting sequence.

    The mode switch narrows *which* movements are shown; it never reorders
    them, because the running totals were computed in posting order and any
    other order would show them jumping backwards.
    """

    template_name = "inventory/reports/_base_report.html"
    page_title = _("بطاقة صنف")
    page_hint = _("حركة صنف واحد في مخزن ودفعة، بالرصيد الجاري كما احتسبه النظام.")
    export_stem = "stock-card"
    supports_modes = True
    columns = (
        ("posted_sequence", _("تسلسل الترحيل")),
        ("effective_at", _("تاريخ الاستحقاق")),
        ("posted_at", _("تاريخ الترحيل")),
        ("movement_type_label", _("نوع الحركة")),
        ("source_document_type", _("نوع المستند")),
        ("reference", _("المرجع")),
        ("warehouse_code", _("المخزن")),
        ("item_code", _("الصنف")),
        ("lot_code", _("الدفعة")),
        ("quantity_in", _("وارد")),
        ("quantity_out", _("صادر")),
        ("quantity_after", _("الرصيد")),
        ("actor", _("المستخدم")),
        ("is_reversal", _("عكس قيد")),
    )
    valuation_columns = (
        ("unit_cost", _("كلفة الوحدة")),
        ("value_in", _("قيمة واردة")),
        ("value_out", _("قيمة صادرة")),
        ("value_after", _("القيمة بعد")),
        ("average_after", _("المتوسط بعد")),
    )

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        opening, rows = reports.stock_card(self.actor, filters, include_valuation=include_valuation)
        self.opening = opening
        return rows


class InTransitReportView(InventoryReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("تقرير البضاعة بالطريق وأعمارها")
    page_hint = _("ما غادر مخزناً ولم يصل بعد، وعمره بالأيام من يوم عمل الإرسال.")
    export_stem = "in-transit"
    columns = (
        ("transfer_number", _("رقم التحويل")),
        ("status_label", _("الحالة")),
        ("source_branch", _("فرع المصدر")),
        ("source_warehouse", _("مخزن المصدر")),
        ("destination_branch", _("فرع الوجهة")),
        ("destination_warehouse", _("مخزن الوجهة")),
        ("dispatch_date", _("تاريخ الإرسال")),
        ("age_days", _("العمر (أيام)")),
        ("item_code", _("الصنف")),
        ("lot_code", _("الدفعة")),
        ("unit", _("الوحدة")),
        ("dispatched_quantity", _("كمية مرسلة")),
        ("received_quantity", _("كمية مستلمة")),
        ("remaining_quantity", _("كمية متبقية")),
    )
    valuation_columns = (
        ("dispatched_value", _("قيمة مرسلة")),
        ("received_value", _("قيمة مستلمة")),
        ("remaining_value", _("قيمة متبقية")),
    )

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        return reports.in_transit_aging(self.actor, filters, include_valuation=include_valuation)


class ExpiryReportView(InventoryReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("تقرير الصلاحية")
    page_hint = _("الدفعات المؤرخة التي تحمل رصيداً، الأقرب انتهاءً أولاً. المنتهية في الأعلى.")
    export_stem = "expiry"
    columns = (
        ("branch_code", _("الفرع")),
        ("warehouse_code", _("المخزن")),
        ("item_code", _("الصنف")),
        ("item_name", _("الاسم")),
        ("lot_code", _("الدفعة")),
        ("expiry_date", _("تاريخ الانتهاء")),
        ("days_to_expiry", _("الأيام المتبقية")),
        ("bucket", _("النافذة")),
        ("unit", _("الوحدة")),
        ("quantity", _("الكمية")),
    )
    valuation_columns = (("value", _("القيمة")),)

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        return reports.expiry(self.actor, filters, include_valuation=include_valuation)


class ReorderReportView(InventoryReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("تقرير حدود إعادة الطلب")
    page_hint = _(
        "الرصيد الحالي مقابل حد إعادة الطلب لكل فرع. لا يقترح أمر شراء ولا ينشئه — "
        "أوامر الشراء من المرحلة الثانية."
    )
    export_stem = "reorder"
    columns = (
        ("branch_code", _("الفرع")),
        ("item_code", _("الصنف")),
        ("item_name", _("الاسم")),
        ("unit", _("الوحدة")),
        ("on_hand", _("الرصيد")),
        ("reorder_point", _("حد إعادة الطلب")),
        ("reorder_quantity", _("كمية إعادة الطلب")),
        ("shortage", _("العجز عن الحد")),
    )
    valuation_columns = (("value", _("القيمة")),)

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        return reports.reorder(self.actor, filters, include_valuation=include_valuation)


class WasteReportView(InventoryReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("ملخص الإتلاف")
    page_hint = _("سطور الإتلاف المرحّلة، بالسبب ومركز الكلفة الذي تحمّلها.")
    export_stem = "waste-summary"
    columns = (
        ("document_number", _("رقم المستند")),
        ("business_date", _("يوم العمل")),
        ("branch_code", _("الفرع")),
        ("warehouse_code", _("المخزن")),
        ("item_code", _("الصنف")),
        ("lot_code", _("الدفعة")),
        ("unit", _("الوحدة")),
        ("quantity", _("الكمية")),
        ("reason_code", _("السبب")),
        ("reason_name", _("وصف السبب")),
        ("comment", _("ملاحظة")),
        ("cost_center", _("مركز الكلفة")),
    )
    valuation_columns = (("value", _("القيمة")),)

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        return reports.waste_summary(self.actor, filters, include_valuation=include_valuation)


class CountVarianceReportView(InventoryReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("تقرير فروقات الجرد")
    page_hint = _("الدفتري مقابل المعدود لكل سطر جرد، مع اسم العادّ والمعتمِد.")
    export_stem = "count-variance"
    columns = (
        ("count_number", _("رقم الجرد")),
        ("business_date", _("يوم العمل")),
        ("branch_code", _("الفرع")),
        ("warehouse_code", _("المخزن")),
        ("conductor", _("العادّ")),
        ("approver", _("المعتمِد")),
        ("item_code", _("الصنف")),
        ("lot_code", _("الدفعة")),
        ("unit", _("الوحدة")),
        ("book_quantity", _("الكمية الدفترية")),
        ("counted_quantity", _("الكمية المعدودة")),
        ("variance_quantity", _("الفرق")),
        ("is_unexpected", _("غير متوقع")),
        ("reason_code", _("السبب")),
    )
    valuation_columns = (
        ("book_value", _("القيمة الدفترية")),
        ("variance_value", _("أثر الفرق بالقيمة")),
        ("approved_unit_cost", _("كلفة معتمدة")),
    )

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        return reports.count_variance(self.actor, filters, include_valuation=include_valuation)


class AdjustmentReportView(InventoryReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("تقرير التسويات المخزنية")
    page_hint = _("زيادة كمية، نقص كمية، وإعادة تقييم بالقيمة فقط — بالسبب والفاعل وحالة العكس.")
    export_stem = "adjustments"
    columns = (
        ("document_number", _("رقم المستند")),
        ("business_date", _("يوم العمل")),
        ("branch_code", _("الفرع")),
        ("warehouse_code", _("المخزن")),
        ("kind_label", _("النوع")),
        ("item_code", _("الصنف")),
        ("lot_code", _("الدفعة")),
        ("unit", _("الوحدة")),
        ("quantity", _("الكمية")),
        ("reason_code", _("السبب")),
        ("comment", _("ملاحظة")),
        ("cost_center", _("مركز الكلفة")),
        ("actor", _("الفاعل")),
        ("is_reversed", _("معكوس")),
    )
    valuation_columns = (
        ("unit_cost", _("كلفة الوحدة")),
        ("value_adjustment", _("تعديل القيمة")),
        ("total_value", _("القيمة")),
    )

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        return reports.adjustments(self.actor, filters, include_valuation=include_valuation)
