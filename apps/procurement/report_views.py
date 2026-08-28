"""
The twelve procurement report screens — Task 2.16, on the Phase 1 machinery.

One base class, `ProcurementReportView`, adapts `InventoryReportView` to this
module: the entry permission becomes `procurement.view_procurement_report`,
the cost redaction switches from `inventory.view_valuation` to
`procurement.view_supplier_cost` (money owed to suppliers is exactly what
that permission was created to hide), and the filters gain a supplier. The
template, the CSV export, the formula neutralisation, the pagination and the
scope-then-filter discipline are inherited unchanged — a procurement export
is trustworthy for the same reasons an inventory export is, because it is
the same code.
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext_lazy as _

from apps.inventory.report_views import InventoryReportView
from apps.inventory.reports import ReportFilters
from apps.procurement import reports
from apps.procurement.permissions import (
    VIEW_PROCUREMENT_REPORT,
    VIEW_SUPPLIER_COST,
)
from apps.procurement.reports import ProcurementReportFilters


class ProcurementReportView(InventoryReportView):
    """A procurement report: same contract, this module's permissions."""

    module_key = "procurement"
    required_permission = VIEW_PROCUREMENT_REPORT
    export_stem = "procurement-report"

    @property
    def include_valuation(self) -> bool:
        return bool(self.request.user.has_perm(VIEW_SUPPLIER_COST))

    def build_filters(self) -> ProcurementReportFilters:
        return ProcurementReportFilters(
            organization_id=self._int("organization_id"),
            branch_id=self._int("branch_id"),
            supplier_id=self._int("supplier_id"),
            item_id=self._int("item_id"),
            date_from=self._date("date_from"),
            date_to=self._date("date_to"),
            search=self.request.GET.get("q", "").strip(),
        )

    def report_rows(
        self, filters: ReportFilters, *, include_valuation: bool
    ) -> list[dict[str, Any]]:
        if not isinstance(filters, ProcurementReportFilters):
            # `build_filters` above is the only constructor on this path, so
            # this is unreachable in the running system — but the base class
            # signature admits the wider type, and a silent attribute error
            # deep in a query service would be a worse way to find out.
            raise TypeError("procurement report received non-procurement filters")
        return self.procurement_rows(filters, include_cost=include_valuation)

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# The twelve reports
# ---------------------------------------------------------------------------


class SupplierAgingReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("أعمار ذمم الموردين")
    page_hint = _(
        "المستحق لكل مورد حسب تاريخ الاستحقاق، مع الرصيد الدائن القائم والدفعات "
        "المقدّمة. يُحتسب من المستندات المرحّلة ولا يُخزَّن."
    )
    export_stem = "supplier-aging"
    columns = (
        ("supplier_code", _("رمز المورد")),
        ("supplier_name", _("المورد")),
    )
    valuation_columns = (
        ("current", _("غير مستحق")),
        ("d30", _("1–30 يوماً")),
        ("d60", _("31–60 يوماً")),
        ("d90", _("61–90 يوماً")),
        ("older", _("أكثر من 90")),
        ("open_total", _("إجمالي المفتوح")),
        ("standing_credit", _("رصيد دائن قائم")),
        ("advances", _("دفعات مقدّمة")),
        ("net_position", _("صافي المركز")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.supplier_aging(self.actor, filters, include_cost=include_cost)


class SettlementBreachReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("تجاوز الحد الأدنى للسداد")
    page_hint = _(
        "فواتير مرحّلة مرّ تاريخ استحقاقها ولم يبلغ المسدَّد منها الحد الأدنى "
        "المتفق عليه مع المورد. لا تظهر هنا فواتير مورد لم يُتفق معه على حد."
    )
    export_stem = "settlement-breaches"
    columns = (
        ("supplier_code", _("رمز المورد")),
        ("supplier_name", _("المورد")),
        ("number", _("الفاتورة")),
        ("invoice_date", _("تاريخ الفاتورة")),
        ("due_date", _("تاريخ الاستحقاق")),
        ("days_overdue", _("أيام التأخير")),
    )
    valuation_columns = (
        ("charged", _("قيمة الفاتورة")),
        ("settled", _("المسدَّد")),
        ("settled_percent", _("نسبة السداد %")),
        ("required_percent", _("الحد الأدنى %")),
        ("shortfall", _("العجز عن الحد")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.settlement_breaches(self.actor, filters, include_cost=include_cost)


class PaymentCycleReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("دورات السداد")
    page_hint = _(
        "نوافذ السداد التي لم تُسدَّد بعد، الأقرب استحقاقاً أولاً. الأيام المتبقية "
        "بالسالب تعني أن الاستحقاق قد مرّ والمبلغ ما زال قائماً. المبلغ المطلوب "
        "هو الرصيد غير المسدَّد مضروباً بالحد الأدنى المتفق عليه عند فتح الدورة."
    )
    export_stem = "payment-cycles"
    columns = (
        ("supplier_code", _("رمز المورد")),
        ("supplier_name", _("المورد")),
        ("sequence", _("الدورة")),
        ("status", _("الحالة")),
        ("opened_on", _("بداية الدورة")),
        ("due_date", _("تاريخ الاستحقاق")),
        ("days_remaining", _("الأيام المتبقية")),
        ("invoice_count", _("عدد الفواتير")),
    )
    valuation_columns = (
        ("charged", _("إجمالي الدورة")),
        ("settled", _("المسدَّد")),
        ("outstanding", _("غير المسدَّد")),
        ("required_percent", _("الحد الأدنى %")),
        ("required_amount", _("المبلغ المطلوب")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.payment_cycles(self.actor, filters, include_cost=include_cost)


class SupplierStatementReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("كشف حساب مورد")
    page_hint = _(
        "كل مستند مالي مرحّل بالتسلسل الزمني برصيد جارٍ. الفاتورة ترفع الرصيد،  "
        "والإشعار الدائن يخفضه بكامل مبلغه، والدفعة بمقدار ما خُصص منها؛ "
        "المتبقي غير المخصص يظهر دفعةً مقدّمة لا ديناً أصغر."
    )
    export_stem = "supplier-statement"
    columns = (
        ("supplier_code", _("رمز المورد")),
        ("date", _("التاريخ")),
        ("document_kind", _("المستند")),
        ("number", _("الرقم")),
    )
    valuation_columns = (
        ("charged", _("مدين")),
        ("settled", _("تسوية")),
        ("advance", _("دفعة مقدّمة")),
        ("balance", _("الرصيد")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.supplier_statement(self.actor, filters, include_cost=include_cost)


class OpenPurchaseOrdersReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("أوامر الشراء المفتوحة")
    page_hint = _("سطور الأوامر المُرسلة التي لم يكتمل استلامها بعد.")
    export_stem = "open-purchase-orders"
    columns = (
        ("order_number", _("رقم الأمر")),
        ("supplier_code", _("المورد")),
        ("item_code", _("الصنف")),
        ("item_name", _("الاسم")),
        ("ordered", _("المطلوب")),
        ("received", _("المستلم")),
        ("outstanding", _("المتبقي")),
    )
    valuation_columns = (("unit_price", _("سعر الوحدة")),)

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.open_purchase_orders(self.actor, filters, include_cost=include_cost)


class OutstandingReceiptQuantityReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("الكميات المطلوبة غير المستلمة")
    page_hint = _("ما طُلب ولم يصل، مجموعاً لكل صنف عبر كل الأوامر المفتوحة.")
    export_stem = "outstanding-receipts"
    columns = (
        ("item_code", _("الصنف")),
        ("item_name", _("الاسم")),
        ("ordered", _("المطلوب")),
        ("received", _("المستلم")),
        ("outstanding", _("المتبقي")),
        ("order_count", _("عدد الأوامر")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.outstanding_receipt_quantity(self.actor, filters, include_cost=include_cost)


class GrniExceptionsReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("مستلم غير مفوتر")
    page_hint = _(
        "سطور الاستلام المرحّلة التي لم تغطها فاتورة مرحّلة بعد، بأعمارها. "
        "هذه هي القيمة التي يجب أن يطابقها حساب GRNI."
    )
    export_stem = "grni-exceptions"
    columns = (
        ("receipt_number", _("رقم الاستلام")),
        ("supplier_code", _("المورد")),
        ("item_code", _("الصنف")),
        ("received_at", _("تاريخ الاستلام")),
        ("age_days", _("العمر بالأيام")),
        ("accepted_quantity", _("الكمية المقبولة")),
    )
    valuation_columns = (
        ("accepted_value", _("القيمة المقبولة")),
        ("uninvoiced_value", _("قيمة غير مفوترة")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.grni_exceptions(self.actor, filters, include_cost=include_cost)


class InvoiceWithoutReceiptReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("مفوتر غير مستلم")
    page_hint = _("سطور بضاعة معتمدة أو مرحّلة لم تُطابق أي استلام إطلاقاً.")
    export_stem = "invoice-without-receipt"
    columns = (
        ("invoice_number", _("رقم الفاتورة")),
        ("supplier_code", _("المورد")),
        ("item_code", _("الصنف")),
        ("status", _("الحالة")),
        ("quantity", _("الكمية")),
    )
    valuation_columns = (
        ("unit_price", _("سعر الوحدة")),
        ("line_amount", _("مبلغ السطر")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.invoice_without_receipt(self.actor, filters, include_cost=include_cost)


class MatchingExceptionsReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("فروقات المطابقة القائمة")
    page_hint = _(
        "تخصيصات المطابقة القائمة التي يختلف فيها سعر الفاتورة عن قيمة "
        "الاستلام — القرارات التي تنتظر من يبتّ فيها قبل الترحيل."
    )
    export_stem = "matching-exceptions"
    columns = (
        ("match_number", _("رقم المطابقة")),
        ("supplier_code", _("المورد")),
        ("item_code", _("الصنف")),
        ("matched_quantity", _("الكمية المطابقة")),
    )
    valuation_columns = (
        ("receipt_value", _("قيمة الاستلام")),
        ("invoice_value", _("قيمة الفاتورة")),
        ("price_variance", _("فرق السعر")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.matching_exceptions(self.actor, filters, include_cost=include_cost)


class PurchaseSpendReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("الإنفاق الشرائي")
    page_hint = _("قيمة الفواتير المرحّلة لكل مورد وشهر.")
    export_stem = "purchase-spend"
    columns = (
        ("month", _("الشهر")),
        ("supplier_code", _("رمز المورد")),
        ("supplier_name", _("المورد")),
        ("invoice_count", _("عدد الفواتير")),
    )
    valuation_columns = (("spend", _("الإنفاق")),)

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.purchase_spend(self.actor, filters, include_cost=include_cost)


class PriceVarianceReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("فروقات أسعار الشراء المرحّلة")
    page_hint = _(
        "أين اختلفت الفاتورة عن الاستلام، لكل تخصيص مطابقة خلف ترحيل سارٍ — "
        "تفصيل رصيد حساب فروقات الأسعار المُودَع."
    )
    export_stem = "price-variance"
    columns = (
        ("match_number", _("رقم المطابقة")),
        ("supplier_code", _("المورد")),
        ("item_code", _("الصنف")),
        ("matched_quantity", _("الكمية المطابقة")),
    )
    valuation_columns = (
        ("receipt_value", _("قيمة الاستلام")),
        ("invoice_value", _("قيمة الفاتورة")),
        ("price_variance", _("فرق السعر")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.price_variance(self.actor, filters, include_cost=include_cost)


class ReturnCreditStatusReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("حالة المرتجعات والإشعارات")
    page_hint = _(
        "لكل سطر مرتجع مرحّل: قيمته الدفترية، وما سُوّي منها بإشعارات دائنة "
        "مرحّلة، وما بقي مطالبةً قائمة على المورد."
    )
    export_stem = "return-credit-status"
    columns = (
        ("return_number", _("رقم المرتجع")),
        ("supplier_code", _("المورد")),
        ("item_code", _("الصنف")),
        ("returned_quantity", _("الكمية المرتجعة")),
        ("state", _("الحالة")),
    )
    valuation_columns = (
        ("book_value", _("القيمة الدفترية")),
        ("settled_value", _("المُسوّى")),
        ("open_claim", _("المطالبة القائمة")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.return_credit_status(self.actor, filters, include_cost=include_cost)


class PaymentAllocationsReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("تخصيصات الدفعات")
    page_hint = _("ما غطّته كل دفعة مرحّلة من فواتير، وما بقي منها دفعةً مقدّمة.")
    export_stem = "payment-allocations"
    columns = (
        ("payment_number", _("رقم الدفعة")),
        ("supplier_code", _("المورد")),
        ("method", _("طريقة الدفع")),
        ("paid_at", _("تاريخ الدفع")),
        ("covered_invoices", _("الفواتير المغطاة")),
    )
    valuation_columns = (
        ("amount", _("المبلغ")),
        ("allocated", _("المخصص")),
        ("advance", _("دفعة مقدّمة")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.payment_allocations(self.actor, filters, include_cost=include_cost)


class ProcurementToGlReportView(ProcurementReportView):
    template_name = "inventory/reports/_base_report.html"
    page_title = _("المشتريات إلى الأستاذ العام")
    page_hint = _(
        "مطابقات PRC-058 الثلاث كما يثبتها المدقق الآلي نفسه: الأرصدة المفتوحة "
        "مقابل حساب الذمم، والاستلام غير المفوتر مقابل GRNI، واقتفاء كل قيد "
        "شراء إلى مستنده."
    )
    export_stem = "procurement-to-gl"
    columns = (
        ("organization", _("المؤسسة")),
        ("check", _("المطابقة")),
        ("state", _("النتيجة")),
    )

    def procurement_rows(
        self, filters: ProcurementReportFilters, *, include_cost: bool
    ) -> list[dict[str, Any]]:
        return reports.procurement_to_gl(self.actor, filters, include_cost=include_cost)
