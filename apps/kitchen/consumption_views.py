"""
The Task 3.8 screens. Seven of them, and no arithmetic in any.

Every number on every page comes from `consumption.py`,
`consumption_sources.py`, `consumption_reconciliation.py` or
`document_links.py`. A view here chooses a service, names its columns and
hands the result to a template. That is deliberate and it is checkable: a
figure that appeared only on a screen would be a figure `verify_kitchen`
cannot verify and CSV cannot export.

Five of the seven reuse Task 3.6's `KitchenReportView` unchanged — scope,
filters, pagination, HTMX, the export path and the structural cost redaction
all come from there. The two that do not are the ones whose subject is a single
document rather than a filtered list, exactly as `BatchVarianceView` already is.

## What the screens are careful to say

* **الاستهلاك الفعلي** shows custody in its own columns, beside consumption and
  never inside it.
* **الاستهلاك النظري** carries `SALES_NOT_INCLUDED_PHASE_4` and the approved
  Arabic notice on every response, and reports the missing `SALES` source by
  name rather than by omission.
* **انحراف الاستهلاك** shows two things and labels them apart: a **complete**
  production standard variance, and a **partial** diagnostic stamped
  `PARTIAL_COVERAGE` / `NOT_FINAL_USAGE_VARIANCE`.
* The link screens say in a sentence that an attribution moves no stock, writes
  no journal, and changes no batch consumption.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.kitchen.consumption import (
    BUCKET_LABELS,
    FlowFilters,
    MovementBucket,
    batch_actual_consumption,
    kitchen_warehouse_flow,
    period_actual_consumption,
    production_standard_requirements,
)
from apps.kitchen.consumption_reconciliation import usage_variance_analysis
from apps.kitchen.consumption_sources import (
    NOT_FINAL_USAGE_VARIANCE,
    PARTIAL_COVERAGE,
    SALES_COVERAGE_NOTICE,
    SALES_NOT_INCLUDED,
    MealUsageFilters,
    TheoreticalSourceType,
    complimentary_meal_equivalent_usage,
    staff_meal_equivalent_usage,
    theoretical_consumption_coverage,
    totals_by_item,
)
from apps.kitchen.document_links import (
    attribution_remaining,
    cancel_batch_document_link,
    create_batch_document_link,
    links_for_batch,
)
from apps.kitchen.models import BatchLinkType, MealType
from apps.kitchen.permissions import (
    LINK_BATCH_DOCUMENT,
    VIEW_KITCHEN_REPORT,
    VIEW_RECIPE_COST,
)
from apps.kitchen.productivity import ProductionFilters
from apps.kitchen.report_views import KitchenReportView, _money
from apps.kitchen.selectors import (
    cost_readable_organization_ids,
    resolve_production_batch,
)
from apps.kitchen.views import KitchenViewMixin

ZERO = Decimal("0")

#: The statement every attribution surface carries, defined once so the four
#: places that show it cannot drift into three different promises.
LINK_STATEMENT = _(
    "الربط تفسيري فقط: لا يحرّك مخزناً، ولا يكتب قيداً، ولا يغيّر استهلاك الدفعة "
    "المرحّلة ولا قيمتها. تصحيح دفعة مرحّلة يكون بعكسها ثم إعادة ترحيلها."
)


def _int(request: HttpRequest, name: str) -> int | None:
    raw = request.GET.get(name, "").strip()
    return int(raw) if raw.isdigit() else None


def _date(request: HttpRequest, name: str) -> datetime.date | None:
    raw = request.GET.get(name, "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        # A malformed date is not worth a 400: the filter does not apply, and
        # the applied-filter chip still shows what was typed.
        return None


def flow_filters_from(request: HttpRequest) -> FlowFilters:
    return FlowFilters(
        warehouse_id=_int(request, "warehouse_id"),
        item_id=_int(request, "item_id"),
        date_from=_date(request, "date_from"),
        date_to=_date(request, "date_to"),
        bucket=request.GET.get("bucket", "").strip(),
    )


def meal_filters_from(request: HttpRequest) -> MealUsageFilters:
    return MealUsageFilters(
        branch_id=_int(request, "branch_id"),
        recipe_id=_int(request, "recipe_id"),
        item_id=_int(request, "item_id"),
        date_from=_date(request, "date_from"),
        date_to=_date(request, "date_to"),
    )


def production_filters_from(request: HttpRequest) -> ProductionFilters:
    return ProductionFilters(
        warehouse_id=_int(request, "warehouse_id"),
        branch_id=_int(request, "branch_id"),
        recipe_id=_int(request, "recipe_id"),
        version_id=_int(request, "version_id"),
        batch_id=_int(request, "batch_id"),
        date_from=_date(request, "date_from"),
        date_to=_date(request, "date_to"),
        status=request.GET.get("status", "").strip(),
    )


def applied_filters_from(request: HttpRequest) -> list[tuple[Any, str]]:
    """
    The chips for a screen that does not extend the report shell.

    Reads `KitchenReportView.FILTER_LABELS` rather than a second copy, so the
    variance screen labels a filter exactly as the five reports do.
    """
    return [
        (label, request.GET.get(key, "").strip())
        for key, label in KitchenReportView.FILTER_LABELS.items()
        if request.GET.get(key, "").strip()
    ]


def bucket_choices() -> list[tuple[str, Any]]:
    """Every bucket, for the classification dropdown. Closed, so a full list."""
    return [(str(bucket), BUCKET_LABELS[bucket]) for bucket in MovementBucket]


class _ConsumptionReportView(KitchenReportView):
    """
    Shared plumbing for the five reports that use Task 3.6's shell.

    The filter parsing lives in module functions above rather than on this
    class, so the two single-document screens can call exactly the same
    functions instead of constructing a view they never dispatch.
    """

    required_permission = VIEW_KITCHEN_REPORT

    def flow_filters(self) -> FlowFilters:
        return flow_filters_from(self.request)

    def meal_filters(self) -> MealUsageFilters:
        return meal_filters_from(self.request)

    def bucket_choices(self) -> list[tuple[str, Any]]:
        return bucket_choices()


# ---------------------------------------------------------------------------
# تدفق مخزن المطبخ — the partition itself
# ---------------------------------------------------------------------------


class WarehouseFlowView(_ConsumptionReportView):
    """
    Every posted movement at a kitchen store, classified into exactly one bucket.

    The identity column is the report's own proof: `(closing − opening) − Σ
    buckets`, which is zero when the classification is exhaustive. It is shown
    rather than merely asserted, because a reader who can see the check pass is
    a reader who can notice it failing.
    """

    page_title = _("تدفق مخزن المطبخ")
    page_hint = _(
        "كل حركة مرحّلة في المخزن تُصنّف في خانة واحدة فقط. العهدة الواردة والصادرة "
        "خانتان مستقلتان وليستا استهلاكاً؛ والفرق بين الرصيد الافتتاحي والختامي "
        "يجب أن يساوي مجموع الخانات — وهذا هو برهان التصنيف."
    )
    export_stem = "kitchen-warehouse-flow"
    filter_extras_template = "kitchen/reports/_flow_filters.html"
    columns = (
        ("warehouse", _("المخزن")),
        ("item", _("الصنف")),
        ("unit", _("الوحدة")),
        ("opening", _("افتتاحي")),
        ("supply", _("توريد")),
        ("custody_in", _("عهدة واردة")),
        ("custody_out", _("عهدة صادرة")),
        ("production_consumption", _("استهلاك إنتاج")),
        ("production_output", _("ناتج إنتاج")),
        ("direct_issue", _("صرف مباشر")),
        ("raw_waste", _("هالك مواد")),
        ("output_waste", _("هالك ناتج")),
        ("corrections", _("تصحيحات")),
        ("closing", _("ختامي")),
        ("identity", _("فرق البرهان")),
        ("movements", _("عدد الحركات")),
    )
    money_columns = (("value_only", _("تسوية قيمة فقط")),)

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        flow = kitchen_warehouse_flow(self.actor, self.flow_filters())
        self._flow = flow
        rows: list[dict[str, Any]] = []
        for item in flow.items:
            row: dict[str, Any] = {
                "warehouse": item.warehouse_code,
                "item": f"{item.item_code} — {item.item_name}",
                "unit": item.base_unit_code,
                "opening": f"{item.opening:f}",
                "supply": f"{item.supply_receipt:f}",
                "custody_in": f"{item.custody_in:f}",
                "custody_out": f"{item.custody_out:f}",
                "production_consumption": f"{item.net_production_consumption:f}",
                "production_output": f"{item.production_output:f}",
                "direct_issue": f"{item.direct_economic_consumption:f}",
                "raw_waste": f"{item.raw_material_waste:f}",
                "output_waste": f"{item.produced_output_waste:f}",
                "corrections": f"{item.count_correction:f}",
                "closing": f"{item.closing:f}",
                "identity": f"{item.identity_difference:f}",
                "movements": str(item.movement_count),
            }
            value = _money(item.value_only_correction, include=include_cost)
            if value is not None:
                row["value_only"] = value
            rows.append(row)
        return rows

    def extra_context(self) -> dict[str, Any]:
        flow = getattr(self, "_flow", None)
        return {
            "bucket_choices": self.bucket_choices(),
            "identity_holds": flow.identity_holds if flow is not None else True,
            "unbalanced": flow.unbalanced if flow is not None else [],
            "classified_count": flow.classified_count if flow is not None else 0,
        }


# ---------------------------------------------------------------------------
# الاستهلاك الفعلي — the period read
# ---------------------------------------------------------------------------


class ActualConsumptionView(_ConsumptionReportView):
    """
    What one kitchen store actually consumed over a period, stream by stream.

    Net production consumption is `PRODUCTION_OUT` less the exact reversal of
    `PRODUCTION_OUT`; direct economic consumption is an ordinary issue less its
    genuine return. A custody transfer appears in neither, in either direction.
    """

    page_title = _("الاستهلاك الفعلي")
    page_hint = _(
        "الاستهلاك الفعلي = صرف الإنتاج المرحّل (ناقص عكسه الدقيق) + الصرف "
        "الاقتصادي المباشر (ناقص إرجاعه الحقيقي). تحويل العهدة ليس استهلاكاً في "
        "أيٍّ من الاتجاهين، والهالك يُعرض منفصلاً ولا يُدمج."
    )
    export_stem = "kitchen-actual-consumption"
    filter_extras_template = "kitchen/reports/_flow_filters.html"
    columns = (
        ("warehouse", _("المخزن")),
        ("item", _("الصنف")),
        ("unit", _("الوحدة")),
        ("production", _("استهلاك إنتاج صافي")),
        ("direct", _("استهلاك مباشر")),
        ("total", _("إجمالي الاستهلاك")),
        ("raw_waste", _("هالك مواد")),
        ("output_waste", _("هالك ناتج")),
        ("outflow", _("إجمالي الخروج الاقتصادي")),
        ("custody_in", _("عهدة واردة")),
        ("custody_out", _("عهدة صادرة")),
        ("production_output", _("ناتج إنتاج")),
        ("corrections", _("تصحيحات جرد")),
    )
    money_columns = (("value_only", _("تسوية قيمة فقط")),)

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        period = period_actual_consumption(self.actor, self.flow_filters())
        self._period = period
        rows: list[dict[str, Any]] = []
        for item in period.items:
            row: dict[str, Any] = {
                "warehouse": item.warehouse_code,
                "item": f"{item.item_code} — {item.item_name}",
                "unit": item.base_unit_code,
                "production": f"{item.net_production_consumption:f}",
                "direct": f"{item.direct_economic_consumption:f}",
                "total": f"{item.total_consumption:f}",
                "raw_waste": f"{item.raw_material_waste:f}",
                "output_waste": f"{item.produced_output_waste:f}",
                "outflow": f"{item.economic_outflow:f}",
                "custody_in": f"{item.custody_in:f}",
                "custody_out": f"{item.custody_out:f}",
                "production_output": f"{item.production_output:f}",
                "corrections": f"{item.count_correction:f}",
            }
            value = _money(item.value_only_correction, include=include_cost)
            if value is not None:
                row["value_only"] = value
            rows.append(row)
        return rows

    def extra_context(self) -> dict[str, Any]:
        period = getattr(self, "_period", None)
        return {
            "bucket_choices": self.bucket_choices(),
            "identity_holds": period.identity_holds if period is not None else True,
        }


# ---------------------------------------------------------------------------
# متطلبات الإنتاج القياسية — standard against actual
# ---------------------------------------------------------------------------


class StandardRequirementsView(_ConsumptionReportView):
    """
    What every posted batch's frozen recipe required, against what went in.

    A **complete** Phase 3 variance: both sides describe the same batch and both
    are posted facts. Where a substitution crossed dimensions the cell says
    `NOT_QUANTITATIVELY_COMPARABLE` rather than showing a zero, because zero
    means "no deviation" and this means "the question has no numeric answer".
    """

    page_title = _("متطلبات الإنتاج القياسية")
    page_hint = _(
        "انحراف قياسي حقيقي: المخطط المجمّد للدفعة مقابل ما دخل فعلاً. هذا ليس "
        "انحراف الاستهلاك النهائي المعتمد على المبيعات. حيث سُجّل بديل ببُعد قياس "
        "مختلف، يُحسب الانحراف على السطور المتوافقة فقط ويُعرض الباقي في عمود "
        "«مستهلك خارج الرقم» — لأن الكيلوغرامات واللترات لا تُجمع."
    )
    export_stem = "kitchen-production-standard"
    filter_extras_template = "kitchen/reports/_standard_filters.html"
    columns = (
        ("batch", _("الدفعة")),
        ("business_date", _("تاريخ العمل")),
        ("recipe", _("الوصفة")),
        ("version", _("النسخة")),
        ("path", _("مسار المكوّن")),
        ("item", _("الصنف")),
        ("unit", _("الوحدة")),
        ("planned", _("المخطط")),
        ("actual", _("الفعلي")),
        ("variance", _("الانحراف")),
        ("compatibility", _("قابلية المقارنة")),
        # The disclosure column. A variance computed over only the rows whose
        # dimension matches the plan is honest arithmetic and an incomplete
        # statement; this is where the rest of the statement goes.
        ("excluded", _("مستهلك خارج الرقم")),
        ("statement", _("بيان")),
    )
    money_columns = (("actual_value", _("قيمة المستهلك")),)

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in production_standard_requirements(
            self.actor, self.production_filters(), include_cost=include_cost
        ):
            batch = entry.batch
            row: dict[str, Any] = {
                "batch": batch.number,
                "business_date": batch.planned_business_date.isoformat(),
                "recipe": f"{batch.recipe.code} — {batch.recipe.name}",
                "version": entry.version_label,
                "path": entry.component_path,
                "item": f"{entry.item_code} — {entry.item_name}",
                "unit": entry.base_unit_code,
                "planned": f"{entry.planned_base_quantity:f}",
                "actual": (
                    f"{entry.actual_base_quantity:f}"
                    if entry.actual_base_quantity is not None
                    else ""
                ),
                "variance": f"{entry.variance:f}" if entry.variance is not None else "",
                "compatibility": entry.compatibility or "",
                "excluded": entry.excluded_display,
                "statement": entry.statement,
            }
            value = _money(entry.actual_posted_value, include=include_cost)
            if value is not None:
                row["actual_value"] = value
            rows.append(row)
        return rows


# ---------------------------------------------------------------------------
# وجبات الموظفين / الوجبات المجانية — meal-equivalent usage
# ---------------------------------------------------------------------------


class MealEquivalentUsageView(_ConsumptionReportView):
    """
    What recorded meals imply in raw ingredients, at each meal's own version.

    An **explanation of output disposition**, not a consumption of the store:
    the ingredients already left through the batch that cooked them. So this is
    reported as its own bucket and is never added to a production plan, because
    without a key linking a portion to the batch that produced it, adding them
    would count the same rice twice.

    Cancelled meals contribute nothing — no row rather than a zero row, because
    the correction said the meal never happened.
    """

    meal_type: str = MealType.STAFF
    export_stem = "kitchen-meal-equivalents"
    filter_extras_template = "kitchen/reports/_meal_filters.html"
    coverage_note = SALES_COVERAGE_NOTICE
    coverage_codes = (SALES_NOT_INCLUDED,)
    columns = (
        ("label", _("المصدر")),
        ("item", _("الصنف")),
        ("unit", _("الوحدة")),
        ("quantity", _("الكمية المكافئة")),
        ("contributions", _("عدد المساهمات")),
        ("coverage", _("كود التغطية")),
    )

    @property
    def page_title(self) -> Any:
        return (
            _("مكافئ وجبات الموظفين")
            if self.meal_type == MealType.STAFF
            else _("مكافئ الوجبات المجانية")
        )

    @property
    def page_hint(self) -> Any:
        return _(
            "توسيع الوجبات المسجّلة إلى مكوّناتها الأولية بنسخة الوصفة المحفوظة على "
            "كل سجل. هذا تفسير لمصير الناتج وليس استهلاكاً إضافياً للمخزن، ولا "
            "يُجمع مع متطلبات الإنتاج."
        )

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        del include_cost  # a meal equivalent is a quantity; it carries no money
        read = (
            staff_meal_equivalent_usage
            if self.meal_type == MealType.STAFF
            else complimentary_meal_equivalent_usage
        )
        contributions = read(self.actor, self.meal_filters())
        return [
            {
                "label": total.equivalent_label,
                "item": f"{total.leaf_item_code} — {total.leaf_item_name}",
                "unit": total.base_unit_code,
                "quantity": f"{total.effective_base_quantity:f}",
                "contributions": str(total.contribution_count),
                "coverage": total.coverage_code,
            }
            for total in totals_by_item(contributions)
        ]

    def extra_context(self) -> dict[str, Any]:
        return {"meal_type": self.meal_type}


# ---------------------------------------------------------------------------
# الاستهلاك النظري — coverage, and the source that is missing
# ---------------------------------------------------------------------------


class TheoreticalConsumptionView(_ConsumptionReportView):
    """
    The theoretical side, with the sales hole named rather than left blank.

    The table is per source and per item; the coverage panel above it lists
    **every declared source type**, so `SALES` appears as
    `DEFERRED_TO_PHASE_4` instead of being silently absent. A screen that
    listed only the sources it had would look complete.
    """

    page_title = _("الاستهلاك النظري")
    page_hint = _(
        "الجانب النظري = الكميات المسجّلة × نسخة الوصفة السارية عند كل تسجيل. "
        "المصادر المتاحة الآن هي وجبات الموظفين والوجبات المجانية فقط، وكلٌّ منها "
        "يُعرض في خانته دون جمعها معاً."
    )
    export_stem = "kitchen-theoretical-consumption"
    filter_extras_template = "kitchen/reports/_meal_filters.html"
    coverage_note = SALES_COVERAGE_NOTICE
    coverage_codes = (SALES_NOT_INCLUDED,)
    columns = (
        ("source", _("المصدر")),
        ("label", _("الخانة")),
        ("item", _("الصنف")),
        ("unit", _("الوحدة")),
        ("quantity", _("الكمية النظرية")),
        ("contributions", _("عدد المساهمات")),
        ("coverage", _("كود التغطية")),
    )

    def report_rows(self, *, include_cost: bool) -> list[dict[str, Any]]:
        del include_cost  # theoretical quantities carry no money column
        coverage = theoretical_consumption_coverage(self.actor, self.meal_filters())
        self._coverage = coverage
        return [
            {
                "source": str(total.source_type),
                "label": total.equivalent_label,
                "item": f"{total.leaf_item_code} — {total.leaf_item_name}",
                "unit": total.base_unit_code,
                "quantity": f"{total.effective_base_quantity:f}",
                "contributions": str(total.contribution_count),
                "coverage": total.coverage_code,
            }
            for total in coverage.totals
        ]

    def extra_context(self) -> dict[str, Any]:
        coverage = getattr(self, "_coverage", None)
        return {
            "coverage": coverage,
            "sources": coverage.sources if coverage is not None else (),
            "sales_source": TheoreticalSourceType.SALES.value,
        }


# ---------------------------------------------------------------------------
# انحراف الاستهلاك — two outputs, labelled apart
# ---------------------------------------------------------------------------


class UsageVarianceView(KitchenViewMixin, View):
    """
    The variance screen: one complete answer and one labelled partial one.

    Its own template rather than the report shell, because the page is two
    tables with two very different standings and a single paginated grid could
    not keep them apart. The partial half carries `PARTIAL_COVERAGE` and
    `NOT_FINAL_USAGE_VARIANCE` on the page and in its export, and there is no
    control anywhere that produces a "final" figure.
    """

    required_permission = VIEW_KITCHEN_REPORT
    template_name = "kitchen/reports/usage_variance.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        include_cost = bool(cost_readable_organization_ids(self.actor))
        analysis = usage_variance_analysis(
            self.actor,
            flow=flow_filters_from(request),
            production=production_filters_from(request),
            meals=meal_filters_from(request),
            include_cost=include_cost,
        )
        from apps.kitchen.kitchen_operations import readable_kitchen_warehouses

        return render(
            request,
            self.template_name,
            {
                "base_template": "kitchen/_bare.html" if self.is_htmx() else "shell.html",
                "page_title": _("انحراف الاستهلاك"),
                "page_hint": _(
                    "شاشتان: انحراف الإنتاج القياسي وهو متاح ومكتمل، وتشخيص جزئي "
                    "للاستهلاك لا يُعتبر انحرافاً نهائياً لأن كميات المبيعات "
                    "المعتمدة غير موجودة قبل المرحلة الرابعة."
                ),
                "analysis": analysis,
                "coverage_codes": [
                    SALES_NOT_INCLUDED,
                    PARTIAL_COVERAGE,
                    NOT_FINAL_USAGE_VARIANCE,
                ],
                "notices": analysis.notices,
                "warehouses": readable_kitchen_warehouses(self.actor),
                "filters": {
                    "date_from": request.GET.get("date_from", ""),
                    "date_to": request.GET.get("date_to", ""),
                    "warehouse_id": request.GET.get("warehouse_id", ""),
                    "branch_id": request.GET.get("branch_id", ""),
                },
                "applied_filters": applied_filters_from(request),
                "show_cost": include_cost,
            },
        )


# ---------------------------------------------------------------------------
# استهلاك دفعة الإنتاج — one batch
# ---------------------------------------------------------------------------


class BatchConsumptionView(KitchenViewMixin, View):
    """
    What one posted batch actually used, with the movement evidence beside it.

    The two agreement checks are shown, not hidden: recorded actuals against
    posted `PRODUCTION_OUT` per item, and — with cost permission — the value
    equation `Σ movement values = input value = output value`. A reader who can
    see the check is a reader who can notice it failing.

    Attributions appear in their own panel and are **not** in the arithmetic
    above them, and the panel says so.
    """

    required_permission = VIEW_KITCHEN_REPORT
    template_name = "kitchen/reports/batch_consumption.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, kwargs["pk"])
        include_cost = bool(cost_readable_organization_ids(self.actor))
        report = batch_actual_consumption(batch, include_cost=include_cost)
        return render(
            request,
            self.template_name,
            {
                "base_template": "kitchen/_bare.html" if self.is_htmx() else "shell.html",
                "page_title": _("استهلاك دفعة الإنتاج"),
                "page_hint": _(
                    "المستهلك الفعلي كما رحّلته الدفعة. الروابط التفسيرية أدناه لا "
                    "تدخل في هذه الأرقام: تصحيح دفعة مرحّلة يكون بعكسها وإعادة "
                    "ترحيلها، لا بمستند لاحق."
                ),
                "batch": batch,
                "report": report,
                "links": links_for_batch(batch),
                "link_statement": LINK_STATEMENT,
                "show_cost": include_cost,
                "may_link": self.actor.has_perm(LINK_BATCH_DOCUMENT),
                "cost_permission": VIEW_RECIPE_COST,
            },
        )


# ---------------------------------------------------------------------------
# ربط مستند بالدفعة — attribution
# ---------------------------------------------------------------------------


class BatchDocumentLinkForm(forms.Form):
    """
    One attribution. Ids are typed in rather than chosen from a global list.

    A dropdown of every transfer line in the organization would be both
    unusable and a disclosure: the operator reaches this screen from the
    document they are looking at, and the service refuses anything outside the
    batch's own warehouse, branch and organization.
    """

    source_line_id = forms.IntegerField(
        label=_("رقم سطر المستند"), min_value=1, widget=forms.NumberInput(attrs={"dir": "ltr"})
    )
    attributed_quantity = forms.DecimalField(
        label=_("الكمية المنسوبة"),
        min_value=Decimal("0.000001"),
        decimal_places=6,
        widget=forms.NumberInput(attrs={"dir": "ltr", "step": "any"}),
    )
    reason = forms.CharField(label=_("السبب"), max_length=200)
    note = forms.CharField(
        label=_("ملاحظة"), max_length=1000, required=False, widget=forms.Textarea(attrs={"rows": 2})
    )


class BatchDocumentLinkCreateView(KitchenViewMixin, View):
    """
    Attribute a custody transfer line, or a waste document line, to a batch.

    One view for both, parameterised by `link_type` in the route rather than
    duplicated: they are the same act with a different source family, and two
    copies would drift the first time one gained a field.
    """

    required_permission = LINK_BATCH_DOCUMENT
    template_name = "kitchen/reports/batch_link_form.html"
    link_type: str = BatchLinkType.ABNORMAL_WASTE_CONTEXT

    def _context(self, request: HttpRequest, batch: Any, form: forms.Form) -> dict[str, Any]:
        return {
            "base_template": "kitchen/_bare.html" if self.is_htmx() else "shell.html",
            "page_title": (
                _("ربط حركة إرجاع الحيازة بالدفعة")
                if self.link_type == BatchLinkType.CUSTODY_RETURN_CONTEXT
                else _("ربط الهالك بالدفعة")
            ),
            "batch": batch,
            "form": form,
            "link_type": self.link_type,
            "link_statement": LINK_STATEMENT,
            "is_custody": self.link_type == BatchLinkType.CUSTODY_RETURN_CONTEXT,
            "links": links_for_batch(batch),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, kwargs["pk"])
        return render(
            request, self.template_name, self._context(request, batch, BatchDocumentLinkForm())
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, kwargs["pk"])
        form = BatchDocumentLinkForm(request.POST)
        if form.is_valid():
            try:
                self._create(batch, form.cleaned_data)
            except ValidationError as refusal:
                # HTTP 200 with the form re-rendered: htmx does not swap an
                # error response, so a 400 here would silently do nothing.
                form.add_error(None, refusal)
            else:
                messages.success(request, _("تم إنشاء الربط التفسيري."))
                target = reverse("kitchen:report_batch_consumption", args=[batch.pk])
                response = HttpResponse(status=204)
                response["HX-Redirect"] = target
                return response if self.is_htmx() else redirect(target)
        return render(request, self.template_name, self._context(request, batch, form))

    def _create(self, batch: Any, data: dict[str, Any]) -> None:
        from apps.inventory.models import (
            InventoryMovementDocumentLine,
            StockTransferLine,
        )

        line_id = data["source_line_id"]
        if self.link_type == BatchLinkType.CUSTODY_RETURN_CONTEXT:
            source = StockTransferLine.objects.filter(pk=line_id).first()
            if source is None:
                raise ValidationError(
                    _("سطر التحويل غير موجود."), code="link_transfer_line_not_found"
                )
            create_batch_document_link(
                batch=batch,
                link_type=self.link_type,
                transfer_line=source,
                attributed_quantity=data["attributed_quantity"],
                reason=data["reason"],
                note=data.get("note", ""),
                actor=self.actor,
            )
            return
        source_waste = InventoryMovementDocumentLine.objects.filter(pk=line_id).first()
        if source_waste is None:
            raise ValidationError(
                _("سطر مستند الإتلاف غير موجود."), code="link_waste_line_not_found"
            )
        create_batch_document_link(
            batch=batch,
            link_type=self.link_type,
            waste_line=source_waste,
            attributed_quantity=data["attributed_quantity"],
            reason=data["reason"],
            note=data.get("note", ""),
            actor=self.actor,
        )


class BatchDocumentLinkCancelForm(forms.Form):
    reason = forms.CharField(label=_("سبب الإلغاء"), max_length=200)


class BatchDocumentLinkCancelView(KitchenViewMixin, View):
    """
    Withdraw an attribution. Cancellation with a reason, never a delete.

    The row stays visible afterwards, because a correction that hides what it
    corrected is not a correction — and the quantity it claimed returns to the
    source line's available attribution the moment this commits.
    """

    required_permission = LINK_BATCH_DOCUMENT
    template_name = "kitchen/reports/batch_link_cancel.html"

    def _link(self, link_id: int) -> Any:
        from apps.kitchen.selectors import resolve_batch_document_link

        return resolve_batch_document_link(self.actor, link_id)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        link = self._link(kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "base_template": "kitchen/_bare.html" if self.is_htmx() else "shell.html",
                "page_title": _("إلغاء الربط"),
                "link": link,
                "form": BatchDocumentLinkCancelForm(),
                "link_statement": LINK_STATEMENT,
                "remaining": attribution_remaining(
                    transfer_line=link.transfer_line, waste_line=link.waste_line
                ),
            },
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        link = self._link(kwargs["pk"])
        form = BatchDocumentLinkCancelForm(request.POST)
        if form.is_valid():
            try:
                cancel_batch_document_link(
                    link=link, reason=form.cleaned_data["reason"], actor=self.actor
                )
            except ValidationError as refusal:
                form.add_error(None, refusal)
            else:
                messages.success(request, _("تم إلغاء الربط."))
                target = reverse("kitchen:report_batch_consumption", args=[link.batch_id])
                if self.is_htmx():
                    response = HttpResponse(status=204)
                    response["HX-Redirect"] = target
                    return response
                return redirect(target)
        return render(
            request,
            self.template_name,
            {
                "base_template": "kitchen/_bare.html" if self.is_htmx() else "shell.html",
                "page_title": _("إلغاء الربط"),
                "link": link,
                "form": form,
                "link_statement": LINK_STATEMENT,
                "remaining": attribution_remaining(
                    transfer_line=link.transfer_line, waste_line=link.waste_line
                ),
            },
        )


__all__ = [
    "LINK_STATEMENT",
    "ActualConsumptionView",
    "BatchConsumptionView",
    "BatchDocumentLinkCancelView",
    "BatchDocumentLinkCreateView",
    "MealEquivalentUsageView",
    "StandardRequirementsView",
    "TheoreticalConsumptionView",
    "UsageVarianceView",
    "WarehouseFlowView",
]
