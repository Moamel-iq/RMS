"""Arabic shift-definition, assignment, and schedule workspaces."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.hr.forms import ShiftAssignmentForm, ShiftForm
from apps.hr.models import Shift
from apps.hr.permissions import ASSIGN_SHIFT, MANAGE_SHIFT, VIEW_SHIFT
from apps.hr.selectors import resolve_shift, visible_shift_assignments, visible_shifts
from apps.hr.services import assign_shift, create_shift, update_shift
from apps.hr.views import HumanResourcesMixin, _redirect
from apps.inventory.views import InventoryListView
from apps.organizations.authorization import (
    organizations_with_permission,
    require_organization_permission,
)


class ShiftListView(HumanResourcesMixin, InventoryListView):
    required_permission = VIEW_SHIFT
    template_name = "hr/shift_list.html"
    context_object_name = "shifts"
    page_title = _("الورديات")
    page_hint = _("إصدارات فعّالة تحفظ جدول كل موظف كما كان عند تسجيل الحضور.")
    search_fields = ("code", "name", "branch__code", "branch__name")
    manage_permission = MANAGE_SHIFT
    create_url_name = "hr:shift_create"
    create_label = _("وردية جديدة")
    result_label = _("وردية")

    def scoped_queryset(self) -> Any:
        queryset = visible_shifts(self.actor).select_related("organization", "branch")
        branch = self.request.GET.get("branch", "").strip()
        active = self.request.GET.get("active", "").strip()
        if branch.isdigit():
            queryset = queryset.filter(branch_id=int(branch))
        if active in {"1", "0"}:
            queryset = queryset.filter(is_active=active == "1")
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        visible = visible_shifts(self.actor)
        context.update(
            {
                "branches": {
                    row.branch_id: row.branch for row in visible.select_related("branch")
                }.values(),
                "selected_branch": self.request.GET.get("branch", ""),
                "selected_active": self.request.GET.get("active", ""),
            }
        )
        return context


class ShiftWriteView(HumanResourcesMixin, View):
    required_permission = MANAGE_SHIFT
    template_name = "hr/shift_form.html"
    instance: Shift | None = None

    def load(self) -> Shift | None:
        return None

    def build_form(self, data: Any = None) -> ShiftForm:
        kwargs: dict[str, Any] = {"actor": self.actor, "instance": self.instance}
        if data is not None:
            kwargs["data"] = data
        return ShiftForm(**kwargs)

    def context(self, form: ShiftForm) -> dict[str, Any]:
        return {
            "form": form,
            "shift": self.instance,
            "page_title": _("تعديل الوردية") if self.instance else _("إضافة وردية"),
            "page_hint": _(
                "إذا استُخدمت النسخة في جدول أو حضور فأنشئ إصداراً جديداً بدلاً من تغيير الماضي."
            ),
            "form_base_template": "settings/_form_fragment.html"
            if self.is_htmx()
            else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.instance = self.load()
        return render(request, self.template_name, self.context(self.build_form()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.instance = self.load()
        form = self.build_form(request.POST)
        if form.is_valid():
            values = form.cleaned_data.copy()
            branch = values.pop("branch")
            try:
                if self.instance is None:
                    shift = create_shift(branch=branch, actor=self.actor, **values)
                else:
                    values.pop("code", None)
                    shift = update_shift(shift=self.instance, actor=self.actor, **values)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, _("تم حفظ إصدار الوردية."))
                return _redirect(request, reverse("hr:shift_list") + f"?q={shift.code}")
        return render(request, self.template_name, self.context(form))


class ShiftCreateView(ShiftWriteView):
    pass


class ShiftUpdateView(ShiftWriteView):
    def load(self) -> Shift:
        shift = resolve_shift(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, MANAGE_SHIFT, shift.organization)
        return shift


class ShiftAssignmentWorkspace(HumanResourcesMixin, View):
    required_permission = VIEW_SHIFT
    template_name = "hr/shift_assignments.html"

    def context(self, form: ShiftAssignmentForm | None = None) -> dict[str, Any]:
        assignments = visible_shift_assignments(self.actor).select_related(
            "employee", "employee__branch", "shift", "shift__branch", "created_by"
        )
        employee = self.request.GET.get("employee", "").strip()
        rotation = self.request.GET.get("rotation", "").strip()
        if employee.isdigit():
            assignments = assignments.filter(employee_id=int(employee))
        if rotation:
            assignments = assignments.filter(rotation_code__icontains=rotation)
        return {
            "page_title": _("تعيين الورديات"),
            "page_hint": _("كل تعيين مؤرخ؛ تغيير الوردية الحالية لا يعيد كتابة حضور سابق."),
            "assignments": assignments,
            "form": form or ShiftAssignmentForm(actor=self.actor),
            "may_assign": organizations_with_permission(self.actor, ASSIGN_SHIFT).exists(),
            "selected_employee": employee,
            "selected_rotation": rotation,
            "base_template": ("settings/_form_fragment.html" if self.is_htmx() else "shell.html"),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, self.template_name, self.context())

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = ShiftAssignmentForm(actor=self.actor, data=request.POST)
        if form.is_valid():
            try:
                assignment = assign_shift(actor=self.actor, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, _("تم حفظ تعيين الوردية."))
                return _redirect(
                    request,
                    reverse("hr:shift_assignments") + f"?employee={assignment.employee_id}",
                )
        return render(request, self.template_name, self.context(form))


class ScheduleView(HumanResourcesMixin, View):
    required_permission = VIEW_SHIFT
    template_name = "hr/shift_schedule.html"
    mode = "employee"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        assignments = visible_shift_assignments(self.actor).select_related(
            "employee", "employee__branch", "shift", "shift__branch"
        )
        if self.mode == "rotation":
            assignments = assignments.exclude(rotation_code="").order_by(
                "rotation_code", "employee__code", "effective_from"
            )
            title = _("عرض التناوب")
        elif self.mode == "branch":
            assignments = assignments.order_by(
                "shift__branch__code", "shift__start_time", "employee__code"
            )
            title = _("جدول الفروع")
        else:
            assignments = assignments.order_by("employee__code", "effective_from")
            title = _("جداول الموظفين")
        return render(
            request,
            self.template_name,
            {
                "page_title": title,
                "page_hint": _("عرض زمني لتعيينات الورديات من دون تعديل السجل التاريخي."),
                "assignments": assignments,
                "mode": self.mode,
            },
        )
