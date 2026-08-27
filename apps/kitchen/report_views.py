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
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.core.money import money_export, quantize_money
from apps.core.printing import (
    PrintableReportMixin,
    PrintSheet,
    SheetFilter,
    sheet_from_columns,
)
from apps.kitchen.cost_reconciliation import snapshot_findings
from apps.kitchen.costing import cost_recipe_version, preview_recipe_cost
from apps.kitchen.kitchen_operations import (
    OperationFilters,
    custody_in,
    custody_out,
    kitchen_waste,
    readable_kitchen_warehouses,
)
from apps.kitchen.models import (
    Recipe,
    RecipeCostSnapshot,
    RecipeCostSnapshotLine,
    RecipeVersionStatus,
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
    resolve_cost_snapshot,
    resolve_production_batch,
    visible_cost_snapshots,
)
from apps.kitchen.views import KitchenViewMixin
from apps.sales.models import MenuItem

#: Anything Excel and Sheets would evaluate. Prefixed on export so a cell that
#: begins with one is read as text — inherited from the Inventory exports,
#: because a Kitchen CSV lands in the same spreadsheet.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in _FORMULA_LEAD else text


class KitchenReportView(PrintableReportMixin, KitchenViewMixin, View):
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
        "q": _("بحث"),
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

    def _print_query(self) -> str:
        params = self.request.GET.copy()
        params["print"] = "1"
        params.pop("page", None)
        params.pop("export", None)
        return params.urlencode()

    def print_sheet(self, context: dict[str, Any], filters: Any = None) -> PrintSheet:
        """
        Every row, and the coverage statement above them.

        A kitchen report that covers only some recipes says so on the screen
        and in the export; paper is where that sentence matters most, because
        paper is what gets filed and read a month later.
        """
        columns = self.active_columns()
        numeric = {
            key
            for key, _label in columns
            if any(
                word in key
                for word in (
                    "quantity",
                    "cost",
                    "amount",
                    "total",
                    "value",
                    "price",
                    "share",
                    "count",
                )
            )
        }
        note = str(self.coverage_note) if self.coverage_note else str(self.page_hint or "")
        if self.coverage_codes:
            covered = "، ".join(str(code) for code in self.coverage_codes)
            covered_label = str(_("تغطية التقرير"))
            line = f"{covered_label}: {covered}"
            note = f"{note} — {line}" if note else line
        return sheet_from_columns(
            title=str(self.page_title),
            columns=columns,
            rows=context["rows"],
            numeric_keys=numeric,
            filters=[
                SheetFilter(label=str(label), value=value)
                for label, value in self.applied_filters()
            ],
            note=note,
        )

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
        if self.wants_print(request):
            return self.render_print(request, {"rows": rows}, None)

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
                    "q": request.GET.get("q", ""),
                },
                "warehouses": readable_kitchen_warehouses(self.actor),
                "show_cost": self.include_cost,
                "export_query": self._export_query(),
                "print_query": self._print_query(),
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
# كلفة الوصفات — historical, immutable recipe-cost evidence
# ---------------------------------------------------------------------------

ZERO = Decimal("0.000")


def _approved_loss_cost(lines: Iterable[RecipeCostSnapshotLine]) -> Decimal:
    """The informational loss share already carried by approved gross quantities.

    A recipe line's ``loss_rate`` never inflates costing: its approved quantity
    is already gross of cleaning/cooking loss.  This report exposes the share
    of that stored line extension attributable to the approved rate, without
    adding it to the batch total a second time.
    """

    return quantize_money(
        sum(
            (line.allocated_extension * (line.recipe_line.loss_rate or ZERO) for line in lines),
            ZERO,
        )
    )


def _missing_valuation_lines(
    lines: Iterable[RecipeCostSnapshotLine],
) -> tuple[RecipeCostSnapshotLine, ...]:
    return tuple(
        line for line in lines if line.valuation_lot_count == 0 or line.valuation_quantity <= ZERO
    )


class RecipeCostReportView(KitchenReportView):
    """Exact historical recipe versions costed from frozen inventory evidence."""

    module_key = "reports"
    required_permission = VIEW_RECIPE_COST
    template_name = "kitchen/reports/recipe_cost.html"
    page_title = _("كلفة الوصفات")
    page_hint = _(
        "كل سطر لقطة كلفة ثابتة للوصفة والنسخة الفعالة والمخزن ونقطة قطع الدفتر في تاريخها؛ "
        "لا يستبدل التقرير النسخة التاريخية بالنسخة الحالية."
    )
    export_stem = "recipe-cost"
    filter_extras_template = "kitchen/reports/_recipe_cost_filters.html"
    columns = (
        ("recipe", _("الوصفة")),
        ("version", _("النسخة")),
        ("effective_period", _("فترة النفاذ")),
        ("branch", _("انطباق الفرع")),
        ("output", _("كمية الناتج")),
        ("ingredients", _("كميات المكونات")),
        ("nested_expansion", _("توسيع الوصفات الفرعية")),
        ("portions", _("الحصص")),
        ("valuation_warnings", _("تحذيرات التقييم")),
        ("snapshot_date", _("تاريخ اللقطة")),
        ("cost_basis", _("أساس كلفة المخزون")),
    )
    money_columns = (
        ("food_cost", _("كلفة الغذاء")),
        ("packaging_cost", _("كلفة التغليف")),
        ("approved_loss_cost", _("كلفة الفاقد المعتمد")),
        ("batch_cost", _("كلفة الدفعة")),
        ("portion_cost", _("كلفة الحصة")),
    )

    def cost_snapshots(self) -> QuerySet[RecipeCostSnapshot]:
        filters = self.production_filters()
        rows = (
            visible_cost_snapshots(self.actor)
            .select_related("version__output_unit")
            .prefetch_related("lines__recipe_line")
        )
        if filters.warehouse_id is not None:
            rows = rows.filter(warehouse_id=filters.warehouse_id)
        if filters.branch_id is not None:
            rows = rows.filter(branch_id=filters.branch_id)
        if filters.recipe_id is not None:
            rows = rows.filter(recipe_id=filters.recipe_id)
        if filters.version_id is not None:
            rows = rows.filter(version_id=filters.version_id)
        if filters.date_from is not None:
            rows = rows.filter(as_of_date__gte=filters.date_from)
        if filters.date_to is not None:
            rows = rows.filter(as_of_date__lte=filters.date_to)
        search = self.request.GET.get("q", "").strip()
        if search:
            rows = rows.filter(
                Q(recipe_code__icontains=search)
                | Q(recipe_name__icontains=search)
                | Q(reference__icontains=search)
            )
        return rows.order_by("-as_of_date", "recipe_code", "-version_number", "-id")

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        branches: dict[int, Any] = {}
        warehouses: set[int] = set()
        warning_total = 0
        latest_date: datetime.date | None = None
        for snapshot in self.cost_snapshots():
            lines = tuple(snapshot.lines.all())
            missing = _missing_valuation_lines(lines)
            warning_total += len(missing)
            branches[snapshot.branch_id] = snapshot.branch
            warehouses.add(snapshot.warehouse_id)
            latest_date = (
                max(latest_date, snapshot.as_of_date) if latest_date else snapshot.as_of_date
            )

            ingredients = "; ".join(
                f"{line.item_code} {line.quantity_display} {line.item_unit_code}" for line in lines
            )
            nested = tuple(
                dict.fromkeys(
                    f"{line.component_path}: {line.source_recipe_code} v{line.source_version_number}"
                    for line in lines
                    if line.component_path
                )
            )
            effective_from = (
                snapshot.version.effective_from.isoformat()
                if snapshot.version.effective_from is not None
                else str(_("غير محدد"))
            )
            effective_to = (
                snapshot.version.effective_to.isoformat()
                if snapshot.version.effective_to is not None
                else str(_("مفتوح"))
            )
            row: dict[str, Any] = {
                "snapshot_id": snapshot.pk,
                "detail_url": reverse("kitchen:report_recipe_cost_detail", args=[snapshot.pk]),
                "recipe": f"{snapshot.recipe_code} — {snapshot.recipe_name}",
                "version": f"v{snapshot.version_number} · {snapshot.version_status}",
                "effective_period": f"{effective_from} — {effective_to}",
                "branch": f"{snapshot.branch.code} — {snapshot.branch.name_ar}",
                "output": f"{snapshot.output_quantity_display} {snapshot.output_unit_code}",
                "ingredients": ingredients,
                "ingredient_count": len(lines),
                "nested_expansion": "; ".join(nested) if nested else str(_("لا توجد")),
                "nested_count": len(nested),
                "portions": snapshot.portions_per_batch_display,
                "valuation_warnings": (
                    str(_("سليم"))
                    if not missing
                    else str(_("%(count)s مكوّن بلا تقييم")) % {"count": len(missing)}
                ),
                "warning_count": len(missing),
                "snapshot_date": snapshot.as_of_date.isoformat(),
                "created_at": snapshot.created_at.isoformat(timespec="minutes"),
                "cost_basis": (
                    f"{snapshot.warehouse_code} · {snapshot.get_valuation_mode_display()} · "
                    f"#{snapshot.ledger_cutoff_sequence}"
                ),
            }
            if include_cost:
                row.update(
                    {
                        "food_cost": money_export(snapshot.food_total),
                        "packaging_cost": money_export(snapshot.packaging_total),
                        "approved_loss_cost": money_export(_approved_loss_cost(lines)),
                        "batch_cost": money_export(snapshot.total_material_cost),
                        "portion_cost": snapshot.plate_cost_display,
                    }
                )
            report.append(row)

        self._report_summary = {
            "snapshot_count": len(report),
            "warning_total": warning_total,
            "warehouse_count": len(warehouses),
            "latest_snapshot_date": latest_date,
            "branches": tuple(branches.values()),
        }
        return report

    def extra_context(self) -> dict[str, Any]:
        summary = getattr(
            self,
            "_report_summary",
            {
                "snapshot_count": 0,
                "warning_total": 0,
                "warehouse_count": 0,
                "latest_snapshot_date": None,
                "branches": (),
            },
        )
        return {**summary, **self._menu_cost_context()}

    def _menu_cost_context(self) -> dict[str, Any]:
        """Live cost readiness for every food item defined in Sales.

        The immutable table below this panel remains the authoritative record.
        This panel answers the operational question that comes first: which
        menu items can be costed now, and exactly which ingredient valuations
        still prevent a complete plate cost.  A partial number is labelled as
        such and is never written to ``RecipeCostSnapshot``.
        """
        filters = self.production_filters()
        as_of_date = filters.date_to or datetime.date.today()
        warehouses = list(readable_kitchen_warehouses(self.actor).select_related("branch"))
        if filters.warehouse_id is not None:
            warehouses = [row for row in warehouses if row.pk == filters.warehouse_id]
        if filters.branch_id is not None:
            warehouses = [row for row in warehouses if row.branch_id == filters.branch_id]

        # One valuation answer per branch.  A branch with several readable
        # stores must be selected explicitly; silently choosing one would make
        # the same dish show a different cost depending on queryset order.
        by_branch: dict[int, list[Any]] = {}
        for warehouse in warehouses:
            by_branch.setdefault(warehouse.branch_id, []).append(warehouse)

        rows: list[dict[str, Any]] = []
        ambiguous_branches: list[Any] = []
        for _branch_id, branch_warehouses in by_branch.items():
            branch = branch_warehouses[0].branch
            if len(branch_warehouses) != 1:
                ambiguous_branches.append(branch)
                continue
            warehouse = branch_warehouses[0]
            items = (
                MenuItem.objects.filter(
                    organization=branch.organization,
                    is_active=True,
                )
                .exclude(code__startswith="DEMO-")
                .select_related("recipe", "category")
                .order_by("category__display_order", "display_order", "code")
            )
            search = self.request.GET.get("q", "").strip()
            if search:
                items = items.filter(
                    Q(code__icontains=search)
                    | Q(name_ar__icontains=search)
                    | Q(recipe__code__icontains=search)
                    | Q(recipe__name_ar__icontains=search)
                )

            for item in items:
                rows.append(
                    self._menu_item_cost_row(
                        item=item,
                        branch=branch,
                        warehouse=warehouse,
                        as_of_date=as_of_date,
                    )
                )

        complete_count = sum(1 for row in rows if row["is_complete"])
        return {
            "menu_cost_rows": rows,
            "menu_cost_date": as_of_date,
            "menu_cost_complete_count": complete_count,
            "menu_cost_partial_count": len(rows) - complete_count,
            "menu_cost_ambiguous_branches": ambiguous_branches,
        }

    @staticmethod
    def _menu_item_cost_row(
        *, item: MenuItem, branch: Any, warehouse: Any, as_of_date: datetime.date
    ) -> dict[str, Any]:
        recipe = item.recipe
        if recipe is None:
            return {
                "item": item,
                "branch": branch,
                "warehouse": warehouse,
                "recipe": None,
                "version": None,
                "known_batch_cost": None,
                "known_plate_cost": None,
                "missing_count": 0,
                "missing_names": "",
                "is_complete": False,
                "is_authoritative": False,
                "problem": str(_("الصنف غير مربوط بوصفة.")),
            }

        # Test data keeps the sourced recipe drafts untouched and activates an
        # identical DEMO copy after fictional review.  Prefer that frozen copy
        # where it exists; otherwise show a draft preview, still clearly marked
        # non-authoritative and impossible to snapshot.
        costing_recipe = (
            Recipe.objects.filter(
                organization=item.organization,
                code=f"DEMO-{recipe.code}",
                versions__status=RecipeVersionStatus.ACTIVE,
            )
            .distinct()
            .first()
            or recipe
        )
        version = (
            costing_recipe.versions.filter(status=RecipeVersionStatus.ACTIVE)
            .order_by("-version_number")
            .first()
            or costing_recipe.versions.order_by("-version_number").first()
        )
        if version is None:
            return {
                "item": item,
                "branch": branch,
                "warehouse": warehouse,
                "recipe": recipe,
                "costing_recipe": costing_recipe,
                "version": None,
                "known_batch_cost": None,
                "known_plate_cost": None,
                "missing_count": 0,
                "missing_names": "",
                "is_complete": False,
                "is_authoritative": False,
                "problem": str(_("الوصفة بلا نسخة يمكن تقييمها.")),
            }
        try:
            if version.status in {
                RecipeVersionStatus.APPROVED,
                RecipeVersionStatus.ACTIVE,
                RecipeVersionStatus.SUPERSEDED,
            }:
                card = cost_recipe_version(
                    version=version, warehouse=warehouse, as_of_date=as_of_date
                )
            else:
                card = preview_recipe_cost(
                    version=version, warehouse=warehouse, as_of_date=as_of_date
                )
        except ValidationError as error:
            return {
                "item": item,
                "branch": branch,
                "warehouse": warehouse,
                "recipe": recipe,
                "costing_recipe": costing_recipe,
                "version": version,
                "known_batch_cost": None,
                "known_plate_cost": None,
                "missing_count": 0,
                "missing_names": "",
                "is_complete": False,
                "is_authoritative": False,
                "problem": "؛ ".join(str(message) for message in error.messages),
            }

        missing_names = tuple(dict.fromkeys(row.item_name for row in card.missing))
        valued_line_count = sum(1 for line in card.lines if line.is_valued)
        line_count = len(card.lines)
        has_known_cost = card.is_complete or valued_line_count > 0
        return {
            "item": item,
            "branch": branch,
            "warehouse": warehouse,
            "recipe": recipe,
            "costing_recipe": costing_recipe,
            "version": version,
            "serving": card.primary_serving,
            # An incomplete card with no valued leaves has no cost answer at
            # all.  Showing the costing kernel's internal zero in that case is
            # operationally misleading: zero means free, while this state
            # means "no purchase valuation exists".  Keep genuine zero-cost
            # complete cards representable, but render an unavailable partial
            # card as unavailable.
            "known_batch_cost": card.total_material_cost if has_known_cost else None,
            "known_plate_cost": card.plate_cost if has_known_cost else None,
            "valued_line_count": valued_line_count,
            "line_count": line_count,
            "coverage_percent": (
                round((valued_line_count / line_count) * 100) if line_count else 0
            ),
            "missing_count": len(card.missing),
            "missing_names": "، ".join(missing_names[:4]),
            "missing_more": max(len(missing_names) - 4, 0),
            "is_complete": card.is_complete,
            "is_authoritative": card.is_authoritative,
            "problem": "",
        }


class RecipeCostReportDetailView(KitchenViewMixin, View):
    """One immutable snapshot with ingredients, nested paths, servings, and warnings."""

    module_key = "reports"
    required_permission = VIEW_RECIPE_COST
    template_name = "kitchen/cost_snapshot_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        snapshot = resolve_cost_snapshot(self.actor, kwargs["pk"])
        lines = tuple(snapshot.lines.select_related("recipe_line", "source_version"))
        missing = _missing_valuation_lines(lines)
        return render(
            request,
            self.template_name,
            {
                "snapshot": snapshot,
                "lines": lines,
                "servings": snapshot.servings.all(),
                "findings": snapshot_findings(snapshot),
                "class_totals": [
                    (_("كلفة الغذاء"), snapshot.food_total),
                    (_("كلفة التغليف"), snapshot.packaging_total),
                    (_("كلفة المرافقات"), snapshot.accompaniment_total),
                ],
                "approved_loss_cost": _approved_loss_cost(lines),
                "show_approved_loss": True,
                "missing_valuation_lines": missing,
                "report_back": True,
                "page_title": _("تفاصيل كلفة الوصفة"),
                "fragment_base_template": (
                    "kitchen/_bare.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
            },
        )


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
