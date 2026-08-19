"""Arabic HTMX payroll calculation, review, and approval workspaces."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.hr.forms import PayrollPaymentForm, PayrollReversalForm, PayrollRunForm
from apps.hr.models import (
    PayrollEmployeeLine,
    PayrollPayment,
    PayrollRunStatus,
)
from apps.hr.payroll import (
    approve_payroll_run,
    calculate_payroll_run,
    create_payroll_payment,
    create_payroll_run,
    post_payroll_run,
    release_payroll_run,
    return_payroll_to_calculation,
    reverse_payroll_payment,
    reverse_payroll_run,
    review_payroll_run,
)
from apps.hr.permissions import (
    APPROVE_PAYROLL,
    CALCULATE_PAYROLL,
    PAY_PAYROLL,
    POST_PAYROLL,
    REVIEW_PAYROLL,
    VIEW_PAYROLL,
    VIEW_PAYROLL_AMOUNTS,
)
from apps.hr.selectors import resolve_payroll_run, visible_payroll_runs
from apps.hr.views import HumanResourcesMixin, _redirect
from apps.inventory.views import InventoryListView
from apps.organizations.authorization import (
    has_organization_permission,
    organizations_with_permission,
)
from apps.organizations.models import Branch


class PayrollRunListView(HumanResourcesMixin, InventoryListView):
    required_permission = VIEW_PAYROLL
    template_name = "hr/payroll_list.html"
    context_object_name = "runs"
    page_title = _("احتساب الرواتب")
    page_hint = _("تشغيل رواتب مجمّد من العقود والحضور والمدخلات المعتمدة فقط.")
    search_fields = ("run_number", "branch__code", "branch__name_ar")
    manage_permission = CALCULATE_PAYROLL
    create_url_name = "hr:payroll_create"
    create_label = _("تشغيل رواتب جديد")
    result_label = _("تشغيل")

    def scoped_queryset(self) -> Any:
        queryset = visible_payroll_runs(self.actor).select_related(
            "branch", "policy", "created_by", "approved_by"
        )
        status = self.request.GET.get("status", "").strip()
        branch = self.request.GET.get("branch", "").strip()
        if status in PayrollRunStatus.values:
            queryset = queryset.filter(status=status)
        if branch.isdigit():
            queryset = queryset.filter(branch_id=int(branch))
        if self.kwargs.get("approvals"):
            queryset = queryset.filter(status=PayrollRunStatus.REVIEWED)
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        organizations = organizations_with_permission(self.actor, VIEW_PAYROLL)
        context.update(
            {
                "statuses": PayrollRunStatus.choices,
                "selected_status": self.request.GET.get("status", ""),
                "selected_branch": self.request.GET.get("branch", ""),
                "branches": Branch.objects.filter(
                    organization__in=organizations, is_active=True
                ).order_by("organization__code", "code"),
                "may_amounts": bool(
                    organizations_with_permission(self.actor, VIEW_PAYROLL_AMOUNTS).exists()
                ),
                "approvals": bool(self.kwargs.get("approvals")),
            }
        )
        return context


class PayrollApprovalListView(PayrollRunListView):
    required_permission = APPROVE_PAYROLL
    page_title = _("اعتماد الرواتب")
    page_hint = _("تشغيلات تمت مراجعتها وتنتظر اعتماد مستخدم مختلف عن المنشئ أو الحاسب.")
    create_url_name = ""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.kwargs["approvals"] = True
        return super().dispatch(request, *args, **kwargs)  # type: ignore[no-any-return]


class PayrollPaymentListView(PayrollRunListView):
    required_permission = PAY_PAYROLL
    page_title = _("صرف الرواتب")
    page_hint = _("تشغيلات مرحّلة ومطلقة للصرف مع الرصيد المدفوع والمتبقي.")
    create_url_name = ""

    def scoped_queryset(self) -> Any:
        return (
            super()
            .scoped_queryset()
            .filter(
                status__in=[
                    PayrollRunStatus.RELEASED,
                    PayrollRunStatus.PARTIALLY_PAID,
                    PayrollRunStatus.PAID,
                ]
            )
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["payment_workspace"] = True
        return context


class PayrollRunCreateView(HumanResourcesMixin, View):
    required_permission = CALCULATE_PAYROLL
    template_name = "hr/payroll_form.html"

    def form(self, data: Any = None) -> PayrollRunForm:
        kwargs: dict[str, Any] = {"actor": self.actor}
        if data is not None:
            kwargs["data"] = data
        return PayrollRunForm(**kwargs)

    def context(self, form: PayrollRunForm) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("تشغيل رواتب جديد"),
            "page_hint": _("حدد الفرع والفترة والسياسة ثم احسب المدخلات في خطوة منفصلة."),
            "form_base_template": "settings/_form_fragment.html"
            if self.is_htmx()
            else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, self.template_name, self.context(self.form()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = self.form(request.POST)
        if form.is_valid():
            try:
                run = create_payroll_run(actor=self.actor, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, _("تم إنشاء مسودة تشغيل الرواتب."))
                return _redirect(request, reverse("hr:payroll_detail", args=[run.pk]))
        return render(request, self.template_name, self.context(form))


class PayrollRunDetailView(HumanResourcesMixin, View):
    required_permission = VIEW_PAYROLL
    template_name = "hr/payroll_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        run = resolve_payroll_run(self.actor, self.kwargs["pk"])
        may_amounts = has_organization_permission(
            self.actor, VIEW_PAYROLL_AMOUNTS, run.organization
        )
        return render(
            request,
            self.template_name,
            {
                "page_title": run.run_number,
                "run": run,
                "lines": run.employee_lines.select_related("employee", "contract"),
                "may_amounts": may_amounts,
                "may_calculate": has_organization_permission(
                    self.actor, CALCULATE_PAYROLL, run.organization
                ),
                "may_review": has_organization_permission(
                    self.actor, REVIEW_PAYROLL, run.organization
                ),
                "may_approve": self.actor.pk not in {run.created_by_id, run.calculated_by_id}
                and has_organization_permission(self.actor, APPROVE_PAYROLL, run.organization),
                "may_post": has_organization_permission(self.actor, POST_PAYROLL, run.organization),
                "may_pay": has_organization_permission(self.actor, PAY_PAYROLL, run.organization),
                "payments": run.payments.select_related(
                    "journal_entry", "created_by", "reversal_of"
                ).prefetch_related("allocations"),
            },
        )


class PayrollPaymentCreateView(HumanResourcesMixin, View):
    required_permission = PAY_PAYROLL
    template_name = "hr/payroll_payment_form.html"

    def load(self) -> Any:
        return resolve_payroll_run(self.actor, self.kwargs["pk"])

    def form(self, run: Any, data: Any = None) -> PayrollPaymentForm:
        kwargs: dict[str, Any] = {"payroll_run": run}
        if data is not None:
            kwargs["data"] = data
        return PayrollPaymentForm(**kwargs)

    def context(self, run: Any, form: PayrollPaymentForm) -> dict[str, Any]:
        return {
            "run": run,
            "form": form,
            "allocation_fields": form.allocation_fields(),
            "page_title": _("صرف رواتب %(run)s") % {"run": run.run_number},
            "page_hint": _("اختر صرفاً كاملاً أو أدخل مبالغ جزئية لكل موظف."),
            "form_base_template": "settings/_form_fragment.html"
            if self.is_htmx()
            else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        run = self.load()
        if run.status not in {PayrollRunStatus.RELEASED, PayrollRunStatus.PARTIALLY_PAID}:
            messages.error(request, _("تشغيل الرواتب غير جاهز للصرف."))
            return _redirect(request, reverse("hr:payroll_detail", args=[run.pk]))
        return render(request, self.template_name, self.context(run, self.form(run)))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        run = self.load()
        form = self.form(run, request.POST)
        if form.is_valid():
            try:
                payment = create_payroll_payment(
                    payroll_run=run,
                    payment_date=form.cleaned_data["payment_date"],
                    method=form.cleaned_data["method"],
                    reference=form.cleaned_data["reference"],
                    reason=form.cleaned_data["reason"],
                    allocations=form.payment_allocations(),
                    idempotency_key=form.cleaned_data["idempotency_key"],
                    actor=self.actor,
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(
                    request,
                    _("تم ترحيل دفعة الرواتب %(number)s.") % {"number": payment.payment_number},
                )
                return _redirect(request, reverse("hr:payroll_detail", args=[run.pk]))
        return render(request, self.template_name, self.context(run, form))


class PayrollEmployeeLineView(HumanResourcesMixin, View):
    required_permission = VIEW_PAYROLL
    template_name = "hr/payroll_line.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        line = (
            PayrollEmployeeLine.objects.filter(
                pk=self.kwargs["pk"], payroll_run__in=visible_payroll_runs(self.actor)
            )
            .select_related("payroll_run", "employee", "contract", "payroll_run__organization")
            .prefetch_related("components", "payment_allocations", "payment_allocations__payment")
            .first()
        )
        if line is None:
            raise Http404
        if not has_organization_permission(
            self.actor, VIEW_PAYROLL_AMOUNTS, line.payroll_run.organization
        ):
            return self.handle_no_permission()
        return render(
            request,
            self.template_name,
            {"page_title": line.employee_name_ar, "line": line},
        )


class PayrollRunCommandView(HumanResourcesMixin, View):
    required_permission = VIEW_PAYROLL

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        run = resolve_payroll_run(self.actor, self.kwargs["pk"])
        action = self.kwargs["action"]
        try:
            if action == "calculate":
                calculate_payroll_run(payroll_run=run, actor=self.actor)
            elif action == "review":
                review_payroll_run(payroll_run=run, actor=self.actor)
            elif action == "return":
                return_payroll_to_calculation(
                    payroll_run=run,
                    reason=request.POST.get("reason", ""),
                    actor=self.actor,
                )
            elif action == "approve":
                approve_payroll_run(payroll_run=run, actor=self.actor)
            elif action == "post":
                post_payroll_run(payroll_run=run, actor=self.actor)
            elif action == "release":
                release_payroll_run(payroll_run=run, actor=self.actor)
            elif action == "reverse":
                reversal_form = PayrollReversalForm(request.POST)
                if not reversal_form.is_valid():
                    raise ValidationError(_("أدخل تاريخاً وسبباً صالحين للعكس."))
                reverse_payroll_run(
                    payroll_run=run,
                    reversal_date=reversal_form.cleaned_data["reversal_date"],
                    reason=reversal_form.cleaned_data["reason"],
                    actor=self.actor,
                )
            else:
                raise Http404
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, _("تم تنفيذ إجراء الرواتب."))
        return _redirect(request, reverse("hr:payroll_detail", args=[run.pk]))


class PayrollPaymentReverseView(HumanResourcesMixin, View):
    required_permission = PAY_PAYROLL

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payment = (
            PayrollPayment.objects.filter(
                pk=self.kwargs["pk"], payroll_run__in=visible_payroll_runs(self.actor)
            )
            .select_related("payroll_run", "organization", "journal_entry")
            .first()
        )
        if payment is None:
            raise Http404
        form = PayrollReversalForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("أدخل تاريخاً وسبباً صالحين للعكس."))
            return _redirect(request, reverse("hr:payroll_detail", args=[payment.payroll_run_id]))
        try:
            reverse_payroll_payment(
                payment=payment,
                reversal_date=form.cleaned_data["reversal_date"],
                reason=form.cleaned_data["reason"],
                actor=self.actor,
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, _("تم عكس دفعة الرواتب بقيد مقابل."))
        return _redirect(request, reverse("hr:payroll_detail", args=[payment.payroll_run_id]))
