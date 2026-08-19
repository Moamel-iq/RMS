"""Leave, absence, overtime, deduction, and advance workspaces."""

from __future__ import annotations

import datetime
from decimal import Decimal
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
from apps.hr.forms import (
    AdvanceForm,
    DeductionForm,
    LeaveRequestForm,
    LeaveTypeForm,
    OvertimeRequestForm,
)
from apps.hr.models import (
    AbsenceClassification,
    AbsenceRecord,
    LeaveType,
    RequestStatus,
)
from apps.hr.permissions import (
    APPROVE_ADVANCE,
    APPROVE_DEDUCTION,
    APPROVE_LEAVE,
    APPROVE_OVERTIME,
    CLASSIFY_ABSENCE,
    MANAGE_ADVANCE,
    MANAGE_DEDUCTION,
    MANAGE_OVERTIME,
    REQUEST_LEAVE,
    VIEW_ADVANCE,
    VIEW_DEDUCTION,
    VIEW_EMPLOYEE_SALARY,
    VIEW_LEAVE,
    VIEW_OVERTIME,
)
from apps.hr.selectors import (
    resolve_advance,
    resolve_deduction,
    resolve_leave_request,
    resolve_overtime,
    visible_advances,
    visible_deductions,
    visible_employees,
    visible_leave_requests,
    visible_overtime,
)
from apps.hr.services import (
    approve_advance,
    approve_deduction,
    approve_overtime_request,
    cancel_leave_request,
    classify_absence,
    create_advance,
    create_deduction,
    create_leave_request,
    create_leave_type,
    create_overtime_request,
    decide_leave_request,
    submit_advance,
    submit_deduction,
    submit_leave_request,
    submit_overtime_request,
)
from apps.hr.views import HumanResourcesMixin, _redirect
from apps.inventory.views import InventoryListView
from apps.organizations.authorization import (
    has_organization_permission,
    organizations_with_permission,
)


class OperationListView(HumanResourcesMixin, InventoryListView):
    template_name = "hr/operation_list.html"
    context_object_name = "objects"
    kind = ""

    def scoped_queryset(self) -> Any:
        queryset: Any
        if self.kind == "leave":
            queryset = visible_leave_requests(self.actor).select_related(
                "employee", "employee__branch", "leave_type", "requested_by", "approved_by"
            )
        elif self.kind == "overtime":
            queryset = visible_overtime(self.actor).select_related(
                "employee", "employee__branch", "shift", "created_by", "approved_by"
            )
        elif self.kind == "deduction":
            queryset = visible_deductions(self.actor).select_related(
                "employee", "employee__branch", "created_by", "approved_by"
            )
        elif self.kind == "advance":
            queryset = visible_advances(self.actor).select_related(
                "employee", "employee__branch", "created_by", "approved_by"
            )
        else:
            raise Http404
        status = self.request.GET.get("status", "").strip()
        if status:
            queryset = queryset.filter(status=status)
        if self.kwargs.get("approvals"):
            queryset = queryset.filter(status=RequestStatus.SUBMITTED)
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "kind": self.kind,
                "statuses": RequestStatus.choices,
                "selected_status": self.request.GET.get("status", ""),
                "approvals": bool(self.kwargs.get("approvals")),
                "salary_visible_ids": set(
                    organizations_with_permission(self.actor, VIEW_EMPLOYEE_SALARY).values_list(
                        "id", flat=True
                    )
                ),
            }
        )
        return context


class LeaveListView(OperationListView):
    required_permission = VIEW_LEAVE
    kind = "leave"
    page_title = _("طلبات الإجازة")
    page_hint = _("الإجازة المعتمدة تبقى منفصلة عن الغياب وتؤثر في تصنيف الحضور.")
    search_fields = ("employee__code", "employee__name_ar", "leave_type__name_ar", "reason")
    manage_permission = REQUEST_LEAVE
    create_url_name = "hr:leave_create"
    create_label = _("طلب إجازة")
    result_label = _("طلب")


class LeaveApprovalListView(LeaveListView):
    required_permission = APPROVE_LEAVE
    page_title = _("اعتماد الإجازات")
    page_hint = _("طلبات مقدمة تنتظر قرار مستخدم مختلف عن منشئ الطلب.")

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.kwargs["approvals"] = True
        return super().dispatch(request, *args, **kwargs)  # type: ignore[no-any-return]


class OvertimeListView(OperationListView):
    required_permission = VIEW_OVERTIME
    kind = "overtime"
    page_title = _("العمل الإضافي")
    page_hint = _("الدقائق والمضاعف يثبتان من سياسة الرواتب عند الاعتماد.")
    search_fields = ("employee__code", "employee__name_ar", "reason", "evidence_reference")
    manage_permission = MANAGE_OVERTIME
    create_url_name = "hr:overtime_create"
    create_label = _("طلب عمل إضافي")
    result_label = _("طلب")


class DeductionListView(OperationListView):
    required_permission = VIEW_DEDUCTION
    kind = "deduction"
    page_title = _("الاستقطاعات")
    page_hint = _("كل استقطاع يحتاج سبباً وإثباتاً ولا يدخل الرواتب قبل الاعتماد.")
    search_fields = ("employee__code", "employee__name_ar", "reason", "evidence_reference")
    manage_permission = MANAGE_DEDUCTION
    create_url_name = "hr:deduction_create"
    create_label = _("استقطاع جديد")
    result_label = _("استقطاع")


class AdvanceListView(OperationListView):
    required_permission = VIEW_ADVANCE
    kind = "advance"
    page_title = _("السلف وذمم الموظفين")
    page_hint = _("الاعتماد لا يعني الصرف؛ الصرف والاسترداد حدثان ماليان منفصلان.")
    search_fields = ("employee__code", "employee__name_ar", "reason", "evidence_reference")
    manage_permission = MANAGE_ADVANCE
    create_url_name = "hr:advance_create"
    create_label = _("طلب سلفة")
    result_label = _("سلفة")


class OperationCreateView(HumanResourcesMixin, View):
    template_name = "hr/operation_form.html"
    form_class: Any = None
    kind = ""
    page_title: Any = ""

    def build_form(self, data: Any = None, files: Any = None) -> Any:
        kwargs: dict[str, Any] = {"actor": self.actor}
        if data is not None:
            kwargs["data"] = data
            kwargs["files"] = files
        return self.form_class(**kwargs)

    def context(self, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "kind": self.kind,
            "page_title": self.page_title,
            "page_hint": _("احفظ المسودة ثم أرسلها إلى مسار الاعتماد."),
            "form_base_template": "settings/_form_fragment.html"
            if self.is_htmx()
            else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, self.template_name, self.context(self.build_form()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = self.build_form(request.POST, request.FILES)
        if form.is_valid():
            try:
                obj: Any
                if self.kind == "leave":
                    obj = create_leave_request(actor=self.actor, **form.cleaned_data)
                elif self.kind == "overtime":
                    obj = create_overtime_request(actor=self.actor, **form.cleaned_data)
                elif self.kind == "deduction":
                    obj = create_deduction(actor=self.actor, **form.cleaned_data)
                elif self.kind == "advance":
                    obj = create_advance(actor=self.actor, **form.cleaned_data)
                else:
                    raise Http404
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, _("تم حفظ المسودة."))
                return _redirect(request, reverse(f"hr:{self.kind}_detail", args=[obj.pk]))
        return render(request, self.template_name, self.context(form))


class LeaveCreateView(OperationCreateView):
    required_permission = REQUEST_LEAVE
    form_class = LeaveRequestForm
    kind = "leave"
    page_title = _("طلب إجازة جديد")


class OvertimeCreateView(OperationCreateView):
    required_permission = MANAGE_OVERTIME
    form_class = OvertimeRequestForm
    kind = "overtime"
    page_title = _("طلب عمل إضافي")


class DeductionCreateView(OperationCreateView):
    required_permission = MANAGE_DEDUCTION
    form_class = DeductionForm
    kind = "deduction"
    page_title = _("استقطاع جديد")


class AdvanceCreateView(OperationCreateView):
    required_permission = MANAGE_ADVANCE
    form_class = AdvanceForm
    kind = "advance"
    page_title = _("طلب سلفة أو ذمة")


class OperationDetailView(HumanResourcesMixin, View):
    template_name = "hr/operation_detail.html"
    kind = ""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        obj: Any
        if self.kind == "leave":
            obj = resolve_leave_request(self.actor, self.kwargs["pk"])
            approve_permission = APPROVE_LEAVE
        elif self.kind == "overtime":
            obj = resolve_overtime(self.actor, self.kwargs["pk"])
            approve_permission = APPROVE_OVERTIME
        elif self.kind == "deduction":
            obj = resolve_deduction(self.actor, self.kwargs["pk"])
            approve_permission = APPROVE_DEDUCTION
        elif self.kind == "advance":
            obj = resolve_advance(self.actor, self.kwargs["pk"])
            approve_permission = APPROVE_ADVANCE
        else:
            raise Http404
        creator_id = obj.requested_by_id if self.kind == "leave" else obj.created_by_id
        return render(
            request,
            self.template_name,
            {
                "page_title": obj,
                "kind": self.kind,
                "object": obj,
                "may_approve": creator_id != self.actor.pk
                and has_organization_permission(self.actor, approve_permission, obj.organization),
                "may_salary": has_organization_permission(
                    self.actor, VIEW_EMPLOYEE_SALARY, obj.organization
                ),
            },
        )


class LeaveDetailView(OperationDetailView):
    required_permission = VIEW_LEAVE
    kind = "leave"


class OvertimeDetailView(OperationDetailView):
    required_permission = VIEW_OVERTIME
    kind = "overtime"


class DeductionDetailView(OperationDetailView):
    required_permission = VIEW_DEDUCTION
    kind = "deduction"


class AdvanceDetailView(OperationDetailView):
    required_permission = VIEW_ADVANCE
    kind = "advance"


class OperationCommandView(HumanResourcesMixin, View):
    required_permission = ""
    kind = ""
    action = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        allowed_actions = {
            "leave": {"submit", "approve", "reject", "cancel"},
            "overtime": {"submit", "approve"},
            "deduction": {"submit", "approve"},
            "advance": {"submit", "approve"},
        }
        if self.action not in allowed_actions.get(self.kind, set()):
            raise Http404
        try:
            obj: Any
            if self.kind == "leave":
                obj = resolve_leave_request(self.actor, self.kwargs["pk"])
                if self.action == "submit":
                    submit_leave_request(request=obj, actor=self.actor)
                elif self.action == "approve":
                    decide_leave_request(request=obj, approve=True, reason="", actor=self.actor)
                elif self.action == "reject":
                    decide_leave_request(
                        request=obj,
                        approve=False,
                        reason=request.POST.get("reason", ""),
                        actor=self.actor,
                    )
                elif self.action == "cancel":
                    cancel_leave_request(
                        request=obj, reason=request.POST.get("reason", ""), actor=self.actor
                    )
            elif self.kind == "overtime":
                obj = resolve_overtime(self.actor, self.kwargs["pk"])
                if self.action == "submit":
                    submit_overtime_request(overtime=obj, actor=self.actor)
                elif self.action == "approve":
                    approve_overtime_request(
                        overtime=obj,
                        approved_minutes=int(request.POST.get("approved_minutes", "0")),
                        actor=self.actor,
                    )
            elif self.kind == "deduction":
                obj = resolve_deduction(self.actor, self.kwargs["pk"])
                if self.action == "submit":
                    submit_deduction(deduction=obj, actor=self.actor)
                elif self.action == "approve":
                    approve_deduction(
                        deduction=obj,
                        approved_amount=Decimal(request.POST.get("approved_amount", "0")),
                        actor=self.actor,
                    )
            elif self.kind == "advance":
                obj = resolve_advance(self.actor, self.kwargs["pk"])
                if self.action == "submit":
                    submit_advance(advance=obj, actor=self.actor)
                elif self.action == "approve":
                    approve_advance(advance=obj, actor=self.actor)
            else:
                raise Http404
        except (ValidationError, ValueError) as error:
            messages.error(request, str(error))
        else:
            messages.success(request, _("تم تنفيذ الإجراء."))
        return _redirect(request, reverse(f"hr:{self.kind}_detail", args=[self.kwargs["pk"]]))


class LeaveTypeWorkspace(HumanResourcesMixin, View):
    required_permission = REQUEST_LEAVE
    template_name = "hr/leave_types.html"

    def context(self, form: LeaveTypeForm | None = None) -> dict[str, Any]:
        organizations = organizations_with_permission(self.actor, REQUEST_LEAVE)
        return {
            "page_title": _("أنواع الإجازات"),
            "page_hint": _(
                "تعريف قابل للتهيئة للمعالجة المدفوعة أو غير المدفوعة ومتطلبات الإثبات."
            ),
            "types": LeaveType.objects.filter(organization__in=organizations),
            "form": form or LeaveTypeForm(actor=self.actor),
            "base_template": "settings/_form_fragment.html" if self.is_htmx() else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, self.template_name, self.context())

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = LeaveTypeForm(actor=self.actor, data=request.POST)
        if form.is_valid():
            values = form.cleaned_data.copy()
            organization = values.pop("organization")
            try:
                create_leave_type(organization=organization, actor=self.actor, **values)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, _("تم حفظ نوع الإجازة."))
                return _redirect(request, reverse("hr:leave_types"))
        return render(request, self.template_name, self.context(form))


class LeaveCalendarView(HumanResourcesMixin, View):
    required_permission = VIEW_LEAVE
    template_name = "hr/leave_calendar.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        month = request.GET.get("month", timezone.localdate().strftime("%Y-%m"))
        try:
            month_start = datetime.date.fromisoformat(month + "-01")
        except ValueError:
            month_start = timezone.localdate().replace(day=1)
        next_month = (month_start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        rows = visible_leave_requests(self.actor).filter(
            status=RequestStatus.APPROVED,
            start_at__date__lt=next_month,
            end_at__date__gte=month_start,
        )
        return render(
            request,
            self.template_name,
            {
                "page_title": _("تقويم الإجازات"),
                "month": month_start,
                "rows": rows.select_related("employee", "leave_type"),
            },
        )


class AbsenceWorkspace(HumanResourcesMixin, View):
    required_permission = VIEW_LEAVE
    template_name = "hr/absence_list.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            business_date = datetime.date.fromisoformat(
                request.GET.get("date", timezone.localdate().isoformat())
            )
        except ValueError:
            business_date = timezone.localdate()
        existing = {
            row.employee_id: row
            for row in AbsenceRecord.objects.filter(
                employee__in=visible_employees(self.actor), business_date=business_date
            )
        }
        rows = []
        for employee in visible_employees(self.actor).select_related("organization", "branch"):
            result = calculate_attendance_day(employee=employee, business_date=business_date)
            if result.status.startswith("APPROVED_") or result.absence_candidate:
                rows.append(
                    {
                        "employee": employee,
                        "result": result,
                        "record": existing.get(employee.pk),
                        "may_classify": has_organization_permission(
                            self.actor, CLASSIFY_ABSENCE, employee.organization
                        ),
                    }
                )
        return render(
            request,
            self.template_name,
            {
                "page_title": _("الغياب والتصنيف"),
                "business_date": business_date,
                "rows": rows,
                "classifications": AbsenceClassification.choices,
            },
        )


class AbsenceClassifyView(HumanResourcesMixin, View):
    required_permission = CLASSIFY_ABSENCE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        employee = visible_employees(self.actor).filter(pk=self.kwargs["pk"]).first()
        if employee is None:
            raise Http404
        business_date = datetime.date.fromisoformat(self.kwargs["business_date"])
        try:
            classify_absence(
                employee=employee,
                business_date=business_date,
                classification=request.POST.get("classification", ""),
                reason=request.POST.get("reason", ""),
                actor=self.actor,
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, _("تم حفظ تصنيف الغياب."))
        return _redirect(request, reverse("hr:absence_list") + f"?date={business_date.isoformat()}")
