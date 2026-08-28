"""Permission-gated Arabic employee statements and browser-rendered payslips."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.hr.models import EmployeeStatus
from apps.hr.permissions import VIEW_EMPLOYEE_STATEMENT
from apps.hr.statements import (
    build_employee_statement,
    resolve_statement_employee,
    resolve_statement_line,
    visible_statement_employees,
)
from apps.hr.views import HumanResourcesMixin
from apps.inventory.views import InventoryListView


class EmployeeStatementListView(HumanResourcesMixin, InventoryListView):
    required_permission = VIEW_EMPLOYEE_STATEMENT
    template_name = "hr/statement_list.html"
    context_object_name = "employees"
    page_title = _("كشوف الموظفين")
    page_hint = _("كشوف الرواتب والسلف والاستقطاعات والمدفوعات والحضور لكل موظف.")
    search_fields = ("code", "name", "branch__name", "job_title")
    create_url_name = ""
    result_label = _("موظف")

    def scoped_queryset(self) -> Any:
        queryset = visible_statement_employees(self.actor).select_related("branch", "organization")
        status = self.request.GET.get("status", "").strip().upper()
        branch = self.request.GET.get("branch", "").strip()
        if status:
            queryset = queryset.filter(status=status)
        if branch.isdigit():
            queryset = queryset.filter(branch_id=int(branch))
        return queryset.order_by("code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        visible = visible_statement_employees(self.actor)
        context.update(
            {
                "statuses": EmployeeStatus.choices,
                "selected_status": self.request.GET.get("status", ""),
                "selected_branch": self.request.GET.get("branch", ""),
                "branches": {
                    employee.branch_id: employee.branch
                    for employee in visible.select_related("branch")
                }.values(),
            }
        )
        return context


class EmployeeStatementDetailView(HumanResourcesMixin, View):
    required_permission = VIEW_EMPLOYEE_STATEMENT
    template_name = "hr/employee_statement.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        employee = resolve_statement_employee(self.actor, self.kwargs["pk"])
        statement = build_employee_statement(employee)
        return render(
            request,
            self.template_name,
            {
                "page_title": _("كشف الموظف %(employee)s") % {"employee": employee.name},
                "statement": statement,
                "employee": employee,
            },
        )


class PayslipView(HumanResourcesMixin, View):
    required_permission = VIEW_EMPLOYEE_STATEMENT
    template_name = "hr/payslip.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        line = resolve_statement_line(self.actor, self.kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "page_title": _("قسيمة راتب %(employee)s") % {"employee": line.employee_name_ar},
                "line": line,
                "run": line.payroll_run,
                "payments": line.payment_allocations.select_related("payment"),
                "print_mode": request.GET.get("print") == "1",
            },
        )
