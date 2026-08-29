from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from apps.inventory.views import InventoryViewMixin
from apps.organizations.authorization import (
    branches_with_permission,
    has_branch_permission,
    require_branch_permission,
)
from apps.sales.models import (
    MenuItem,
    PosMenuItemMapping,
    PosSalesImportBatch,
    PosSalesImportStatus,
)
from apps.sales.permissions import (
    CONFIRM_POS_SALES_IMPORT,
    POST_POS_SALES_IMPORT,
    RETURN_POS_SALES_IMPORT,
    REVIEW_POS_SALES_IMPORT,
    VIEW_POS_SALES_IMPORT,
)
from apps.sales.pos_closing import (
    application_summary,
    channel_summary,
    confirm_by_cashier,
    operational_expenses,
    post_and_close_import,
    posting_blockers,
    return_to_cashier,
    review_summary,
    save_review_step,
    start_accountant_review,
)
from apps.sales.pos_import_forms import (
    CashierSalesConfirmationForm,
    PosImportReturnForm,
    PosImportReviewStepForm,
    PosSalesImportForm,
)
from apps.sales.pos_imports import EXPECTED_REPORTS, import_pos_sales, normalize_name
from apps.sales.views import SalesListView


def _visible_imports(actor: Any) -> QuerySet[PosSalesImportBatch]:
    branches = branches_with_permission(actor, VIEW_POS_SALES_IMPORT)
    return PosSalesImportBatch.objects.filter(branch__in=branches).select_related(
        "branch", "created_by"
    )


class PosSalesImportListView(SalesListView):
    model = PosSalesImportBatch
    template_name = "sales/pos_import_list.html"
    context_object_name = "batches"
    page_title = "استيراد مبيعات POS"
    page_hint = "دفعة واحدة للتقارير الستة، مع مطابقة المجاميع ومنع تكرار اليوم نفسه."
    search_fields = ("branch__code", "created_by__username")
    manage_permission = CONFIRM_POS_SALES_IMPORT
    manage_scope = "branch"
    create_url_name = "sales:pos_import_create"
    create_label = "استيراد يوم مبيعات"
    result_label = "دفعة"

    def scoped_queryset(self) -> QuerySet[Any]:
        return _visible_imports(self.actor).order_by("-business_date", "-created_at")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        visible = _visible_imports(self.actor)
        context["workflow_metrics"] = {
            "pending": visible.exclude(
                status__in=[
                    PosSalesImportStatus.POSTED,
                    PosSalesImportStatus.CANCELLED,
                    PosSalesImportStatus.REVERSED,
                ]
            ).count(),
            "accountant": visible.filter(
                status__in=[
                    PosSalesImportStatus.AWAITING_ACCOUNTANT,
                    PosSalesImportStatus.ACCOUNTANT_REVIEW,
                ]
            ).count(),
            "differences": visible.filter(review_data__step_4__cash_variance__isnull=False)
            .exclude(review_data__step_4__cash_variance="0.000")
            .count(),
            "posted": visible.filter(status=PosSalesImportStatus.POSTED).count(),
        }
        return context


class PosSalesImportCreateView(InventoryViewMixin, View):
    module_key = "sales"
    required_permission = CONFIRM_POS_SALES_IMPORT

    def _render(self, request: HttpRequest, form: PosSalesImportForm) -> HttpResponse:
        return render(
            request,
            "sales/pos_import_form.html",
            {"form": form, "page_title": "استيراد مبيعات يوم كامل", "reports": EXPECTED_REPORTS},
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(request, PosSalesImportForm(actor=self.actor))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = PosSalesImportForm(request.POST, request.FILES, actor=self.actor)
        if not form.is_valid():
            return self._render(request, form)
        branch = form.cleaned_data["branch"]
        require_branch_permission(self.actor, CONFIRM_POS_SALES_IMPORT, branch)
        try:
            batch, created = import_pos_sales(
                branch=branch, actor=self.actor, uploads=form.uploads()
            )
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
            return self._render(request, form)
        if created:
            messages.success(request, "تم استيراد التقارير الستة ومطابقة أرقامها في دفعة واحدة.")
        else:
            messages.info(
                request, "هذه الملفات مستوردة سابقاً؛ فُتحت الدفعة الموجودة من دون تكرار البيانات."
            )
        return HttpResponseRedirect(reverse("sales:pos_import_detail", args=[batch.pk]))


class PosSalesImportDetailView(InventoryViewMixin, View):
    module_key = "sales"
    required_permission = VIEW_POS_SALES_IMPORT

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = (
            _visible_imports(self.actor).filter(pk=kwargs["pk"]).prefetch_related("files").first()
        )
        if batch is None:
            from apps.organizations.authorization import OutOfScope

            raise OutOfScope("POS sales import does not exist.")
        data = batch.report_data
        pos_names = sorted({line["name"] for line in data.get("sales_items", {}).get("items", [])})
        menu_by_name = {
            normalize_name(item.name): item
            for item in MenuItem.objects.filter(organization=batch.organization, is_active=True)
        }
        aliases = {
            mapping.normalized_source_name: mapping
            for mapping in PosMenuItemMapping.objects.filter(
                organization=batch.organization
            ).select_related("menu_item")
        }
        mapping_rows = []
        for source_name in pos_names:
            normalized = normalize_name(source_name)
            if normalized in menu_by_name:
                mapping_rows.append(
                    {
                        "source_name": source_name,
                        "menu_item": menu_by_name[normalized],
                        "kind": "مطابقة مباشرة",
                    }
                )
            elif normalized in aliases:
                mapping_rows.append(
                    {
                        "source_name": source_name,
                        "menu_item": aliases[normalized].menu_item,
                        "kind": "رابط معتمد",
                    }
                )
        return render(
            request,
            "sales/pos_import_detail.html",
            {
                "batch": batch,
                "page_title": f"استيراد مبيعات {batch.business_date.isoformat()}",
                "sale_types": data.get("sales_by_type", {}).get("lines", []),
                "categories": data.get("sales_by_category", {}).get("lines", []),
                "applications": [
                    line
                    for line in data.get("expenses", {}).get("lines", [])
                    if line.get("application")
                ],
                "expenses": [
                    line
                    for line in data.get("expenses", {}).get("lines", [])
                    if not line.get("application")
                ],
                "item_count": len(data.get("sales_items", {}).get("items", [])),
                "mapping_rows": mapping_rows,
                "mapped_item_count": len(mapping_rows),
                "channel_summary": channel_summary(batch),
                "application_summary": application_summary(batch),
                "review_summary": review_summary(batch),
                "posting_blockers": posting_blockers(batch),
                "may_confirm": has_branch_permission(
                    self.actor, CONFIRM_POS_SALES_IMPORT, batch.branch
                ),
                "may_review": has_branch_permission(
                    self.actor, REVIEW_POS_SALES_IMPORT, batch.branch
                )
                and (batch.cashier_confirmed_by_id != self.actor.pk or self.actor.is_superuser),
                "may_post": has_branch_permission(self.actor, POST_POS_SALES_IMPORT, batch.branch),
                "may_return": has_branch_permission(
                    self.actor, RETURN_POS_SALES_IMPORT, batch.branch
                ),
            },
        )


def _batch_for(actor: Any, pk: int) -> PosSalesImportBatch:
    batch = _visible_imports(actor).filter(pk=pk).prefetch_related("files").first()
    if batch is None:
        from apps.organizations.authorization import OutOfScope

        raise OutOfScope("POS sales import does not exist.")
    return batch


def _form_error_messages(form: Any) -> list[str]:
    return [str(message) for errors in form.errors.values() for message in errors]


class PosSalesImportWorkflowView(InventoryViewMixin, View):
    module_key = "sales"
    required_permission = VIEW_POS_SALES_IMPORT

    def _render(
        self,
        request: HttpRequest,
        batch: PosSalesImportBatch,
        *,
        step: int,
        form: Any = None,
        return_form: Any = None,
    ) -> HttpResponse:
        if form is None:
            form = (
                CashierSalesConfirmationForm()
                if step == 0
                else PosImportReviewStepForm(batch=batch, step=step)
            )
        context = {
            "batch": batch,
            "step": step,
            "steps": range(1, 6),
            "form": form,
            "return_form": return_form or PosImportReturnForm(),
            "channel_rows": channel_summary(batch),
            "application_rows": application_summary(batch),
            "expense_rows": operational_expenses(batch),
            "cash_summary": review_summary(batch),
            "blockers": posting_blockers(batch),
        }
        template = (
            "sales/partials/pos_import_workflow.html"
            if request.headers.get("HX-Request") == "true"
            else "sales/pos_import_workflow_page.html"
        )
        response = render(request, template, context)
        response["Vary"] = "HX-Request"
        return response

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = _batch_for(self.actor, kwargs["pk"])
        requested = int(request.GET.get("step", "0") or 0)
        if batch.status in {
            PosSalesImportStatus.AWAITING_CASHIER,
            PosSalesImportStatus.RETURNED_TO_CASHIER,
        }:
            require_branch_permission(self.actor, CONFIRM_POS_SALES_IMPORT, batch.branch)
            return self._render(request, batch, step=0)
        require_branch_permission(self.actor, REVIEW_POS_SALES_IMPORT, batch.branch)
        if batch.status == PosSalesImportStatus.AWAITING_ACCOUNTANT:
            try:
                batch = start_accountant_review(batch=batch, actor=self.actor)
            except ValidationError as error:
                if request.headers.get("HX-Request") == "true":
                    return HttpResponse(" ".join(error.messages), status=409)
                messages.error(request, " ".join(error.messages))
                return HttpResponseRedirect(reverse("sales:pos_import_detail", args=[batch.pk]))
        allowed = min(batch.review_step + 1, 5)
        step = requested if requested and requested <= allowed else allowed
        return self._render(request, batch, step=step)


class PosSalesImportCashierConfirmView(InventoryViewMixin, View):
    module_key = "sales"
    required_permission = CONFIRM_POS_SALES_IMPORT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = _batch_for(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, CONFIRM_POS_SALES_IMPORT, batch.branch)
        form = CashierSalesConfirmationForm(request.POST)
        if not form.is_valid():
            return PosSalesImportWorkflowView()._render(request, batch, step=0, form=form)
        try:
            confirm_by_cashier(batch=batch, actor=self.actor)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
            return PosSalesImportWorkflowView()._render(request, batch, step=0, form=form)
        messages.success(request, "تم تأكيد المبيعات وقفل التقارير بانتظار مراجعة المحاسب.")
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("sales:pos_import_detail", args=[batch.pk])
        return response


class PosSalesImportReviewStepView(InventoryViewMixin, View):
    module_key = "sales"
    required_permission = REVIEW_POS_SALES_IMPORT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = _batch_for(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, REVIEW_POS_SALES_IMPORT, batch.branch)
        batch = start_accountant_review(batch=batch, actor=self.actor)
        step = int(kwargs["step"])
        form = PosImportReviewStepForm(request.POST, batch=batch, step=step)
        if not form.is_valid():
            return PosSalesImportWorkflowView()._render(request, batch, step=step, form=form)
        try:
            batch = save_review_step(
                batch=batch, actor=self.actor, step=step, evidence=form.evidence()
            )
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
            return PosSalesImportWorkflowView()._render(request, batch, step=step, form=form)
        messages.success(request, f"تم اعتماد الخطوة {step}.")
        if batch.status == PosSalesImportStatus.READY_TO_POST:
            return PosSalesImportWorkflowView()._render(request, batch, step=5)
        return PosSalesImportWorkflowView()._render(request, batch, step=step + 1)


class PosSalesImportReturnView(InventoryViewMixin, View):
    module_key = "sales"
    required_permission = RETURN_POS_SALES_IMPORT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = _batch_for(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, RETURN_POS_SALES_IMPORT, batch.branch)
        form = PosImportReturnForm(request.POST)
        if not form.is_valid():
            step = min(batch.review_step + 1, 5)
            return PosSalesImportWorkflowView()._render(request, batch, step=step, return_form=form)
        return_to_cashier(batch=batch, actor=self.actor, reason=form.cleaned_data["reason"])
        messages.warning(request, "أُعيدت الدفعة إلى الكاشير مع حفظ سبب الإعادة.")
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("sales:pos_import_detail", args=[batch.pk])
        return response


class PosSalesImportPostView(InventoryViewMixin, View):
    module_key = "sales"
    required_permission = POST_POS_SALES_IMPORT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = _batch_for(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, POST_POS_SALES_IMPORT, batch.branch)
        try:
            post_and_close_import(batch=batch, actor=self.actor)
        except ValidationError as error:
            form = PosImportReviewStepForm(batch=batch, step=5)
            for message in error.messages:
                form.add_error(None, message)
            return PosSalesImportWorkflowView()._render(request, batch, step=5, form=form)
        messages.success(request, "تم الترحيل المحاسبي وإقفال المبيعات.")
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("sales:pos_import_detail", args=[batch.pk])
        return response


__all__ = [
    "PosSalesImportCashierConfirmView",
    "PosSalesImportCreateView",
    "PosSalesImportDetailView",
    "PosSalesImportListView",
    "PosSalesImportPostView",
    "PosSalesImportReturnView",
    "PosSalesImportReviewStepView",
    "PosSalesImportWorkflowView",
]
