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

from apps.hr.forms import PayrollRunForm
from apps.hr.models import PayrollEmployeeLine, PayrollRunStatus
from apps.hr.payroll import (
    approve_payroll_run,
    calculate_payroll_run,
    create_payroll_run,
    return_payroll_to_calculation,
    review_payroll_run,
)
from apps.hr.permissions import (
    APPROVE_PAYROLL,
    CALCULATE_PAYROLL,
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
            },
        )


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
            else:
                raise Http404
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, _("تم تنفيذ إجراء الرواتب."))
        return _redirect(request, reverse("hr:payroll_detail", args=[run.pk]))
