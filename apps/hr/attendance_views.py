"""Arabic attendance entry, review, correction, and approval workspaces."""

from __future__ import annotations

import datetime
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.hr.attendance import calculate_attendance_day
from apps.hr.forms import AttendanceCorrectionForm, AttendanceEventForm
from apps.hr.models import AttendanceDayApproval
from apps.hr.permissions import (
    APPROVE_ATTENDANCE,
    CORRECT_ATTENDANCE,
    RECORD_ATTENDANCE,
    VIEW_ATTENDANCE,
)
from apps.hr.selectors import (
    resolve_attendance_event,
    resolve_employee,
    visible_attendance_events,
    visible_employees,
)
from apps.hr.services import (
    approve_attendance_day,
    correct_attendance_event,
    record_attendance_event,
    reopen_attendance_day,
)
from apps.hr.views import HumanResourcesMixin, _redirect
from apps.organizations.authorization import has_organization_permission


def _date(raw: str, *, default: datetime.date | None = None) -> datetime.date:
    try:
        return datetime.date.fromisoformat(raw)
    except TypeError, ValueError:
        if default is not None:
            return default
        raise Http404 from None


def _day_rows(actor: Any, business_date: datetime.date, branch: str = "") -> list[dict[str, Any]]:
    employees = visible_employees(actor).select_related("organization", "branch")
    if branch.isdigit():
        employees = employees.filter(branch_id=int(branch))
    approvals = {
        row.employee_id: row
        for row in AttendanceDayApproval.objects.filter(
            employee__in=employees, business_date=business_date
        )
    }
    return [
        {
            "employee": employee,
            "result": calculate_attendance_day(employee=employee, business_date=business_date),
            "approval": approvals.get(employee.pk),
            "may_approve": has_organization_permission(
                actor, APPROVE_ATTENDANCE, employee.organization
            ),
        }
        for employee in employees
    ]


class AttendanceDayListView(HumanResourcesMixin, View):
    required_permission = VIEW_ATTENDANCE
    template_name = "hr/attendance_list.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        business_date = _date(request.GET.get("date", ""), default=timezone.localdate())
        branch = request.GET.get("branch", "").strip()
        status = request.GET.get("status", "").strip()
        rows = _day_rows(self.actor, business_date, branch)
        if status:
            rows = [row for row in rows if row["result"].status == status]
        events = visible_attendance_events(self.actor).filter(business_date=business_date)
        branches = {row["employee"].branch_id: row["employee"].branch for row in rows}.values()
        return render(
            request,
            self.template_name,
            {
                "page_title": _("الحضور والانصراف"),
                "page_hint": _("نتيجة يومية مشتقة من أحداث حضور ملحقة لا تُعدل أو تُحذف."),
                "rows": rows,
                "event_count": events.count(),
                "business_date": business_date,
                "selected_branch": branch,
                "selected_status": status,
                "branches": branches,
                "base_template": (
                    "settings/_form_fragment.html" if self.is_htmx() else "shell.html"
                ),
            },
        )


class AttendanceEventCreateView(HumanResourcesMixin, View):
    required_permission = RECORD_ATTENDANCE
    template_name = "hr/attendance_form.html"

    def context(self, form: AttendanceEventForm) -> dict[str, Any]:
        return {
            "page_title": _("تسجيل حدث حضور"),
            "page_hint": _("التوقيت يُحفظ مع منطق يوم العمل للفرع بتوقيت بغداد."),
            "form": form,
            "form_base_template": "settings/_form_fragment.html"
            if self.is_htmx()
            else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(
            request, self.template_name, self.context(AttendanceEventForm(actor=self.actor))
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = AttendanceEventForm(actor=self.actor, data=request.POST)
        if form.is_valid():
            try:
                event = record_attendance_event(actor=self.actor, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, _("تم إلحاق حدث الحضور بالسجل."))
                return _redirect(
                    request,
                    reverse("hr:attendance_employee", args=[event.employee_id])
                    + f"?date={event.business_date.isoformat()}",
                )
        return render(request, self.template_name, self.context(form))


class EmployeeAttendanceView(HumanResourcesMixin, View):
    required_permission = VIEW_ATTENDANCE
    template_name = "hr/attendance_employee.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        employee = resolve_employee(self.actor, self.kwargs["pk"])
        selected_date = _date(request.GET.get("date", ""), default=timezone.localdate())
        dates = [selected_date - datetime.timedelta(days=offset) for offset in range(14)]
        rows = [
            {
                "result": calculate_attendance_day(employee=employee, business_date=day),
                "approval": AttendanceDayApproval.objects.filter(
                    employee=employee, business_date=day
                ).first(),
            }
            for day in dates
        ]
        events = visible_attendance_events(self.actor).filter(
            employee=employee,
            business_date__gte=dates[-1],
            business_date__lte=selected_date,
        )
        return render(
            request,
            self.template_name,
            {
                "page_title": _("حضور الموظف"),
                "page_hint": _("أحداث خام ونتائج يومية محسوبة مع بقاء كل تصحيح مرتبطاً بأصله."),
                "employee": employee,
                "rows": rows,
                "events": events.select_related("created_by", "supersedes"),
                "selected_date": selected_date,
                "may_correct": has_organization_permission(
                    self.actor, CORRECT_ATTENDANCE, employee.organization
                ),
            },
        )


class AttendanceCorrectionView(HumanResourcesMixin, View):
    required_permission = CORRECT_ATTENDANCE
    template_name = "hr/attendance_correction.html"

    def context(self, event: Any, form: AttendanceCorrectionForm) -> dict[str, Any]:
        return {
            "page_title": _("تصحيح حدث حضور"),
            "page_hint": _("التصحيح يضيف حدثاً بديلاً ويربطه بالأصل؛ الحدث الأصلي لا يتغير."),
            "event": event,
            "form": form,
            "form_base_template": "settings/_form_fragment.html"
            if self.is_htmx()
            else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        event = resolve_attendance_event(self.actor, self.kwargs["pk"])
        form = AttendanceCorrectionForm(actor=self.actor, event=event)
        return render(request, self.template_name, self.context(event, form))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        event = resolve_attendance_event(self.actor, self.kwargs["pk"])
        form = AttendanceCorrectionForm(actor=self.actor, event=event, data=request.POST)
        if form.is_valid():
            try:
                replacement = correct_attendance_event(
                    event=event, actor=self.actor, **form.cleaned_data
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, _("تم إلحاق التصحيح مع الاحتفاظ بالحدث الأصلي."))
                return _redirect(
                    request,
                    reverse("hr:attendance_employee", args=[replacement.employee_id])
                    + f"?date={replacement.business_date.isoformat()}",
                )
        return render(request, self.template_name, self.context(event, form))


class MissingPunchView(HumanResourcesMixin, View):
    required_permission = VIEW_ATTENDANCE
    template_name = "hr/attendance_missing.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        business_date = _date(request.GET.get("date", ""), default=timezone.localdate())
        rows = [row for row in _day_rows(self.actor, business_date) if row["result"].missing_punch]
        return render(
            request,
            self.template_name,
            {
                "page_title": _("معالجة البصمات الناقصة"),
                "page_hint": _("كل معالجة تُنفذ كتصحيح ملحق بسبب إلزامي."),
                "business_date": business_date,
                "rows": rows,
            },
        )


class AttendanceApproveView(HumanResourcesMixin, View):
    required_permission = APPROVE_ATTENDANCE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        employee = resolve_employee(self.actor, self.kwargs["pk"])
        business_date = _date(self.kwargs["business_date"])
        try:
            approve_attendance_day(employee=employee, business_date=business_date, actor=self.actor)
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, _("تم اعتماد نتيجة الحضور اليومية."))
        return _redirect(
            request, reverse("hr:attendance_list") + f"?date={business_date.isoformat()}"
        )


class AttendanceReopenView(HumanResourcesMixin, View):
    required_permission = APPROVE_ATTENDANCE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        employee = resolve_employee(self.actor, self.kwargs["pk"])
        business_date = _date(self.kwargs["business_date"])
        try:
            reopen_attendance_day(
                employee=employee,
                business_date=business_date,
                reason=request.POST.get("reason", ""),
                actor=self.actor,
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, _("أعيد فتح يوم الحضور للتصحيح."))
        return _redirect(
            request, reverse("hr:attendance_list") + f"?date={business_date.isoformat()}"
        )
