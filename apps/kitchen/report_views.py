"""
The Kitchen report screens, and the one export path they share.

Modelled on `apps/inventory/report_views.py` deliberately, down to the block
names: a Kitchen report should behave exactly like an Inventory report the
operator already knows, and the way to guarantee that is to reuse the shape
rather than to describe it. Each subclass says which rows it produces and which
columns a CSV gets; scope, filters, pagination, HTMX and the export come from
here, once.

**The export is the same call as the screen.** Not a similar call — the same
one, so a caller who cannot see a money column on screen cannot obtain it by
exporting instead. Redaction is structural: without `view_recipe_cost` the
`columns` list has no money entries at all, and `cell` renders a missing key as
a dash rather than a blank, because a blank suggests zero.

## The three separations these screens exist to keep visible

* **Custody transfer is not consumption.** الصرف للمطبخ and المرتجع من المطبخ
  are Inventory transfers in and out of the kitchen store. Goods changed hands;
  nothing was used. The column headings say custody, and the page hint says it
  in a sentence.
* **Normal yield loss is not abnormal waste.** The gap between expected and
  actual output is absorbed into the produced item's unit cost and appears on
  الإنتاجية والفاقد. الهالك is an Inventory Waste document with a reason code, a
  value and a journal. The two are never added.
* **A variance is a number only where the dimensions agree.** Where a kitchen
  substituted across dimensions the cell says so in words rather than showing a
  zero.
"""

from __future__ import annotations

import csv
import datetime
from collections.abc import Sequence
from typing import Any

from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.core.money import money_export
from apps.kitchen.kitchen_operations import (
    OperationFilters,
    custody_in,
    custody_out,
    kitchen_waste,
    readable_kitchen_warehouses,
)
from apps.kitchen.permissions import VIEW_KITCHEN_REPORT, VIEW_RECIPE_COST
from apps.kitchen.productivity import (
    ProductionFilters,
    register_rows,
    variance_by_component,
    yield_rows,
)
from apps.kitchen.selectors import (
    cost_readable_organization_ids,
    resolve_production_batch,
)
from apps.kitchen.views import KitchenViewMixin

#: Anything Excel and Sheets would evaluate. Prefixed on export so a cell that
#: begins with one is read as text — inherited from the Inventory exports,
#: because a Kitchen CSV lands in the same spreadsheet.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in _FORMULA_LEAD else text


class KitchenReportView(KitchenViewMixin, View):
    """A scoped, filtered, paginated Kitchen report with a CSV on the same query."""

    required_permission: str = VIEW_KITCHEN_REPORT
    template_name = "kitchen/reports/report.html"
    page_title: Any = ""
    page_hint: Any = ""
    export_stem: str = "kitchen-report"
    paginate_by: int = 50
    #: (context key, Arabic header) pairs. Money columns are declared apart so
    #: they can be dropped as a unit rather than blanked one at a time.
    columns: Sequence[tuple[str, Any]] = ()
    money_columns: Sequence[tuple[str, Any]] = ()
    #: Rendered above the table when the report's coverage is narrower than its
    #: title suggests. Task 3.8 uses it for the Phase 4 sales exclusion.
    coverage_note: Any = ""
    #: Machine-readable coverage labels, rendered as chips beside the note and
    #: written into the CSV. Task 3.8 stamps `SALES_NOT_INCLUDED_PHASE_4` and,
    #: on the variance screen, `PARTIAL_COVERAGE` / `NOT_FINAL_USAGE_VARIANCE`.
    #: They are codes rather than sentences on purpose: a downstream reader
    #: greps a code and cannot grep a translated paragraph.
    coverage_codes: Sequence[str] = ()
    #: An optional partial rendered inside the shared toolbar, so a report can
    #: add its own filters without a second report template. The shell keeps
    #: owning scope, dates, pagination, HTMX and the export.
    filter_extras_template: str = ""

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        raise NotImplementedError

    def extra_context(self) -> dict[str, Any]:
        """
        Anything this report needs beside its rows — coverage, totals, choices.

        A hook rather than an overridden `get`, so a subclass cannot
        accidentally drop the scope, the pagination or the export while adding
        a filter dropdown.
        """
        return {}

    # -- request plumbing --------------------------------------------------

    @property
    def include_cost(self) -> bool:
        """
        Whether this caller reads money **in this organization**.

        Asked through the same selector every cost read uses, rather than with
        a bare `has_perm`: a permission without a reach authorizes nothing
        (ADR-016), and a report that checked only the codename would show one
        organization's costs to somebody who holds the grant in another.
        """
        return bool(cost_readable_organization_ids(self.actor))

    def _int(self, name: str) -> int | None:
        raw = self.request.GET.get(name, "").strip()
        return int(raw) if raw.isdigit() else None

    def _date(self, name: str) -> datetime.date | None:
        raw = self.request.GET.get(name, "").strip()
        if not raw:
            return None
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            # A malformed date is not worth a 400: the filter does not apply
            # and the field shows what was typed.
            return None

    def production_filters(self) -> ProductionFilters:
        return ProductionFilters(
            warehouse_id=self._int("warehouse_id"),
            branch_id=self._int("branch_id"),
            recipe_id=self._int("recipe_id"),
            version_id=self._int("version_id"),
            batch_id=self._int("batch_id"),
            date_from=self._date("date_from"),
            date_to=self._date("date_to"),
            status=self.request.GET.get("status", "").strip(),
        )

    def operation_filters(self) -> OperationFilters:
        return OperationFilters(
            warehouse_id=self._int("warehouse_id"),
            item_id=self._int("item_id"),
            date_from=self._date("date_from"),
            date_to=self._date("date_to"),
            status=self.request.GET.get("status", "").strip(),
            reason_code_id=self._int("reason_code_id"),
        )

    def is_htmx(self) -> bool:
        return self.request.headers.get("HX-Request") == "true"

    #: Arabic for each filter key, for the applied-filter chips.
    FILTER_LABELS: dict[str, Any] = {
        "date_from": _("من تاريخ"),
        "date_to": _("إلى تاريخ"),
        "warehouse_id": _("المخزن"),
        "branch_id": _("الفرع"),
        "item_id": _("الصنف"),
        "recipe_id": _("الوصفة"),
        "version_id": _("النسخة"),
        "batch_id": _("الدفعة"),
        "bucket": _("التصنيف"),
        "meal_type": _("نوع الوجبة"),
        "status": _("الحالة"),
    }

    def applied_filters(self) -> list[tuple[Any, str]]:
        """
        The filters actually in force, for a chip row above the table.

        Read from `request.GET` rather than from the parsed filter objects, so
        a value the report could not parse still shows as applied — a silently
        ignored malformed date is the filter bug hardest to notice.
        """
        return [
            (label, self.request.GET.get(key, "").strip())
            for key, label in self.FILTER_LABELS.items()
            if self.request.GET.get(key, "").strip()
        ]

    def active_columns(self) -> list[tuple[str, Any]]:
        columns = list(self.columns)
        if self.include_cost:
            columns += list(self.money_columns)
        return columns

    def _export_query(self) -> str:
        params = self.request.GET.copy()
        params["export"] = "csv"
        params.pop("page", None)
        return params.urlencode()

    def export_csv(self, rows: list[dict[str, Any]]) -> HttpResponse:
        columns = self.active_columns()
        response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
        stamp = datetime.date.today().isoformat()
        response["Content-Disposition"] = f'attachment; filename="{self.export_stem}-{stamp}.csv"'
        response.write("﻿")
        writer = csv.writer(response)
        # Coverage first, above the header, so a partial diagnostic cannot be
        # separated from the fact that it is partial by somebody deleting a
        # column. `_safe` runs on it too: it is still a spreadsheet cell.
        if self.coverage_codes:
            writer.writerow(
                [_safe(str(_("تغطية التقرير"))), *(_safe(code) for code in self.coverage_codes)]
            )
            if self.coverage_note:
                writer.writerow([_safe(str(_("بيان التغطية"))), _safe(str(self.coverage_note))])
        writer.writerow([str(header) for _key, header in columns])
        for row in rows:
            writer.writerow([_safe(row.get(key)) for key, _header in columns])
        return response

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        rows = self.report_rows(include_cost=self.include_cost)
        if request.GET.get("export") == "csv":
            return self.export_csv(rows)

        paginator = Paginator(rows, self.paginate_by)
        page = paginator.get_page(request.GET.get("page"))
        return render(
            request,
            self.template_name,
            {
                "base_template": "kitchen/_bare.html" if self.is_htmx() else "shell.html",
                "page_title": self.page_title,
                "page_hint": self.page_hint,
                "coverage_note": self.coverage_note,
                "rows": page.object_list,
                "columns": self.active_columns(),
                "page_obj": page,
                "is_paginated": page.has_other_pages(),
                "paginator": paginator,
                "total_rows": paginator.count,
                # Every filter this family understands, echoed back whether or
                # not this particular report uses it. That is what makes a
                # filter survive pagination: the links are built from the same
                # query string, and a key the template forgot would be dropped
                # on the second page only.
                "filters": {
                    "date_from": request.GET.get("date_from", ""),
                    "date_to": request.GET.get("date_to", ""),
                    "warehouse_id": request.GET.get("warehouse_id", ""),
                    "branch_id": request.GET.get("branch_id", ""),
                    "item_id": request.GET.get("item_id", ""),
                    "recipe_id": request.GET.get("recipe_id", ""),
                    "version_id": request.GET.get("version_id", ""),
                    "batch_id": request.GET.get("batch_id", ""),
                    "bucket": request.GET.get("bucket", ""),
                    "meal_type": request.GET.get("meal_type", ""),
                    "status": request.GET.get("status", ""),
                },
                "warehouses": readable_kitchen_warehouses(self.actor),
                "show_cost": self.include_cost,
                "export_query": self._export_query(),
                "coverage_codes": list(self.coverage_codes),
                "filter_extras_template": self.filter_extras_template,
                "applied_filters": self.applied_filters(),
                **self.extra_context(),
            },
        )


def _money(value: Any, *, include: bool) -> str | None:
    """A money cell, or **no key at all** when the caller may not read money."""
    if not include or value is None:
        return None
    return money_export(value)


# ---------------------------------------------------------------------------
# سجل الإنتاج — the production register
# ---------------------------------------------------------------------------


class ProductionRegisterView(KitchenReportView):
    """What was produced, when, at what scale, and by whom."""

    page_title = _("سجل الإنتاج")
    page_hint = _("كل دفعة إنتاج مرحّلة، بنسختها الدقيقة ورقم مستندها.")
    export_stem = "kitchen-production-register"
    columns = (
        ("number", _("الرقم")),
        ("business_date", _("تاريخ العمل")),
        ("recipe", _("الوصفة")),
        ("version", _("النسخة")),
        ("warehouse", _("المخزن")),
        ("multiplier", _("المعامل")),
        ("expected_output", _("الناتج المتوقع")),
        ("actual_output", _("الناتج الفعلي")),
        ("output_item", _("صنف الناتج")),
        ("lot", _("اللوط")),
        ("status", _("الحالة")),
        ("posted_by", _("رحّلها")),
    )
    money_columns = (("output_value", _("قيمة الناتج")),)

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for batch in register_rows(self.actor, self.production_filters()):
            # Narrowed into locals: both are nullable because a draft has
            # neither, and a posted batch that reaches this report always does.
            output_item = batch.output_item
            output_lot = batch.output_lot
            row: dict[str, Any] = {
                "number": batch.number,
                "business_date": batch.planned_business_date.isoformat(),
                "recipe": f"{batch.recipe.code} — {batch.recipe.name_ar}",
                "version": f"v{batch.recipe_version.version_number}",
                "warehouse": batch.warehouse.code,
                "multiplier": batch.multiplier_display,
                "expected_output": batch.expected_output_display,
                "actual_output": batch.actual_output_display,
                "output_item": output_item.code if output_item is not None else "",
                "lot": output_lot.code if output_lot is not None else "",
                "status": str(batch.get_status_display()),
                "posted_by": str(batch.posted_by) if batch.posted_by_id else "",
            }
            value = _money(batch.output_value, include=include_cost)
            if value is not None:
                row["output_value"] = value
            rows.append(row)
        return rows


# ---------------------------------------------------------------------------
# الإنتاجية والفاقد — productivity and normal loss
# ---------------------------------------------------------------------------


class ProductivityReportView(KitchenReportView):
    """
    Expected against actual, and the normal loss between them.

    The loss column is **normal production loss** and nothing else: it is
    absorbed into the produced item's unit cost, it writes no Waste document
    and no journal, and it is never added to الهالك. That separation is why
    this report is where a yield problem becomes visible at all.
    """

    page_title = _("الإنتاجية والفاقد")
    page_hint = _(
        "الفاقد هنا فاقد إنتاج طبيعي يُستوعب في كلفة وحدة الناتج — وليس هالكاً، "
        "وليس له مستند إتلاف ولا قيد محاسبي."
    )
    export_stem = "kitchen-productivity"
    columns = (
        ("number", _("الرقم")),
        ("business_date", _("تاريخ العمل")),
        ("recipe", _("الوصفة")),
        ("expected_output", _("الناتج المتوقع")),
        ("actual_output", _("الناتج الفعلي")),
        ("output_variance", _("انحراف الناتج")),
        ("yield_percent", _("نسبة المردود %")),
        ("normal_loss", _("الفاقد الطبيعي")),
        ("status", _("الحالة")),
    )
    money_columns = (
        ("input_value", _("قيمة المستهلك")),
        ("actual_unit_cost", _("كلفة وحدة الناتج")),
    )

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in yield_rows(self.actor, self.production_filters()):
            batch = entry.batch
            percent = entry.yield_percent
            row: dict[str, Any] = {
                "number": batch.number,
                "business_date": batch.planned_business_date.isoformat(),
                "recipe": f"{batch.recipe.code} — {batch.recipe.name_ar}",
                "expected_output": f"{entry.expected_output:f}",
                "actual_output": f"{entry.actual_output:f}",
                "output_variance": f"{entry.output_variance:f}",
                "yield_percent": f"{percent:f}" if percent is not None else "",
                "normal_loss": f"{entry.normal_loss:f}",
                "status": str(batch.get_status_display()),
            }
            consumed = _money(batch.input_value, include=include_cost)
            if consumed is not None:
                row["input_value"] = consumed
            unit_cost = _money(entry.actual_unit_cost, include=include_cost)
            if unit_cost is not None:
                row["actual_unit_cost"] = unit_cost
            rows.append(row)
        return rows


# ---------------------------------------------------------------------------
# انحراف دفعة الإنتاج — batch variance
# ---------------------------------------------------------------------------


class BatchVarianceView(KitchenViewMixin, View):
    """
    Planned against consumed for one batch, grouped by component path.

    Its own view rather than another `KitchenReportView` because the subject is
    one document rather than a filtered list, and because the grouping is the
    report — "was the overspend in the dish or in the blend?" (RCP-080).
    """

    required_permission = VIEW_KITCHEN_REPORT
    template_name = "kitchen/reports/batch_variance.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "base_template": "kitchen/_bare.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html",
                "page_title": _("انحراف دفعة الإنتاج"),
                "page_hint": _(
                    "الانحراف رقم فقط حين تتفق الأبعاد؛ وحيث لا تتفق تُعرض السطور "
                    "منفصلة مع بيان أنها غير قابلة للمقارنة كمياً."
                ),
                "batch": batch,
                "groups": variance_by_component(batch),
                "show_cost": self.actor.has_perm(VIEW_RECIPE_COST),
            },
        )


# ---------------------------------------------------------------------------
# الصرف للمطبخ / المرتجع من المطبخ — custody, not consumption
# ---------------------------------------------------------------------------


class _CustodyReportView(KitchenReportView):
    """Shared shape for the two custody directions."""

    export_stem = "kitchen-custody"
    columns = (
        ("number", _("رقم التحويل")),
        ("business_date", _("تاريخ العمل")),
        ("source", _("من مخزن")),
        ("destination", _("إلى مخزن")),
        ("status", _("الحالة")),
        ("lines", _("عدد السطور")),
    )

    def transfers(self) -> Any:
        raise NotImplementedError

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        del include_cost  # a custody movement carries no Kitchen money column
        return [
            {
                "number": transfer.transfer_number,
                "business_date": transfer.business_date.isoformat(),
                "source": transfer.source_warehouse.code,
                "destination": transfer.destination_warehouse.code,
                "status": str(transfer.get_status_display()),
                "lines": str(transfer.lines.count()),
            }
            for transfer in self.transfers()
        ]


class KitchenIssueReportView(_CustodyReportView):
    """الصرف للمطبخ — goods carried **into** the kitchen store."""

    page_title = _("الصرف للمطبخ")
    page_hint = _(
        "تحويل عهدة إلى مخزن المطبخ. تغيّرت الحيازة ولم يُستهلك شيء بعد — "
        "الاستهلاك يحدث بالإنتاج أو بالصرف المباشر."
    )
    export_stem = "kitchen-custody-in"

    def transfers(self) -> Any:
        return custody_in(self.actor, self.operation_filters())


class KitchenReturnReportView(_CustodyReportView):
    """المرتجع من المطبخ — goods carried back **out** to the store."""

    page_title = _("المرتجع من المطبخ")
    page_hint = _(
        "تحويل عهدة من مخزن المطبخ إلى المخزن. ليس عكساً لصرف الإنتاج ولا يُطرح "
        "مرة ثانية من الاستهلاك."
    )
    export_stem = "kitchen-custody-out"

    def transfers(self) -> Any:
        return custody_out(self.actor, self.operation_filters())


# ---------------------------------------------------------------------------
# الهالك — abnormal waste
# ---------------------------------------------------------------------------


class KitchenWasteReportView(KitchenReportView):
    """
    الهالك — Inventory Waste documents raised at a kitchen warehouse.

    Abnormal loss: a deliberate act with a reason code, a quantity, a value and
    a journal. Kept apart from the normal yield loss on الإنتاجية والفاقد,
    because adding them would let spoilage hide inside a yield figure.
    """

    page_title = _("الهالك")
    page_hint = _(
        "هالك غير طبيعي بمستند إتلاف مخزني وسبب وقيمة وقيد محاسبي — منفصل تماماً "
        "عن الفاقد الطبيعي للإنتاج."
    )
    export_stem = "kitchen-waste"
    columns = (
        ("number", _("رقم المستند")),
        ("business_date", _("تاريخ العمل")),
        ("warehouse", _("المخزن")),
        ("status", _("الحالة")),
        ("narration", _("البيان")),
        ("posted_by", _("رحّلها")),
    )

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        del include_cost  # the document total lives on the Inventory screen
        return [
            {
                "number": document.document_number,
                "business_date": document.business_date.isoformat(),
                "warehouse": document.warehouse.code,
                "status": str(document.get_status_display()),
                "narration": document.narration,
                "posted_by": str(document.posted_by) if document.posted_by_id else "",
            }
            for document in kitchen_waste(self.actor, self.operation_filters())
        ]


__all__ = [
    "BatchVarianceView",
    "KitchenIssueReportView",
    "KitchenReportView",
    "KitchenReturnReportView",
    "KitchenWasteReportView",
    "ProductionRegisterView",
    "ProductivityReportView",
]
