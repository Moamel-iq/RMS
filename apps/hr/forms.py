"""Validated employee, document, and contract forms."""

from __future__ import annotations

from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.hr.models import (
    AttendanceEvent,
    AttendanceEventSource,
    AttendanceEventType,
    Employee,
    EmployeeContract,
    EmployeeDocument,
    EmployeeStatus,
    PayrollPolicy,
    Shift,
    ShiftAssignment,
)
from apps.hr.permissions import (
    ASSIGN_SHIFT,
    CORRECT_ATTENDANCE,
    MANAGE_CONTRACT,
    MANAGE_EMPLOYEE,
    MANAGE_SHIFT,
    RECORD_ATTENDANCE,
)
from apps.hr.services import allowances_as_text, parse_fixed_allowances
from apps.organizations.authorization import organizations_with_permission
from apps.organizations.models import Branch, Organization
from apps.users.models import User

DATE_WIDGET = forms.DateInput(attrs={"type": "date"})


class EmployeeForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Employee
        fields = (
            "organization",
            "code",
            "name_ar",
            "name_en",
            "phone",
            "email",
            "identity_number",
            "date_of_birth",
            "gender",
            "marital_status",
            "address",
            "emergency_contact",
            "branch",
            "department",
            "job_title",
            "workplace",
            "hire_date",
            "payment_method",
            "payment_reference",
            "notes",
        )
        widgets = {
            "date_of_birth": DATE_WIDGET,
            "hire_date": DATE_WIDGET,
            "address": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "organization": _("المؤسسة"),
            "code": _("رمز الموظف"),
            "name_ar": _("الاسم بالعربية"),
            "name_en": _("الاسم بالإنجليزية"),
            "phone": _("رقم الهاتف"),
            "email": _("البريد الإلكتروني"),
            "identity_number": _("رقم الهوية"),
            "date_of_birth": _("تاريخ الميلاد"),
            "gender": _("الجنس"),
            "marital_status": _("الحالة الاجتماعية"),
            "address": _("العنوان"),
            "emergency_contact": _("اتصال الطوارئ"),
            "branch": _("الفرع"),
            "department": _("القسم"),
            "job_title": _("المسمى الوظيفي"),
            "workplace": _("مكان العمل"),
            "hire_date": _("تاريخ التعيين"),
            "payment_method": _("طريقة الدفع"),
            "payment_reference": _("مرجع الدفع"),
            "notes": _("ملاحظات"),
        }

    def __init__(self, *, actor: User, instance: Employee | None = None, **kwargs: Any) -> None:
        super().__init__(instance=instance, **kwargs)
        organizations = organizations_with_permission(actor, MANAGE_EMPLOYEE)
        cast(
            "forms.ModelChoiceField[Organization]", self.fields["organization"]
        ).queryset = organizations
        cast(
            "forms.ModelChoiceField[Branch]", self.fields["branch"]
        ).queryset = Branch.objects.filter(organization__in=organizations)
        if instance is not None:
            self.fields["organization"].disabled = True
            self.fields["code"].disabled = True

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        organization = cleaned.get("organization")
        branch = cleaned.get("branch")
        if organization is not None and branch is not None:
            if branch.organization_id != organization.pk:
                self.add_error("branch", _("The branch belongs to another organization."))
        return cleaned


class EmployeeDocumentForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = EmployeeDocument
        fields = (
            "document_type",
            "title",
            "reference",
            "file",
            "issue_date",
            "expiry_date",
            "notes",
        )
        widgets = {
            "issue_date": DATE_WIDGET,
            "expiry_date": DATE_WIDGET,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "document_type": _("نوع المستند"),
            "title": _("عنوان المستند"),
            "reference": _("المرجع"),
            "file": _("الملف"),
            "issue_date": _("تاريخ الإصدار"),
            "expiry_date": _("تاريخ الانتهاء"),
            "notes": _("ملاحظات"),
        }


class EmployeeContractForm(forms.ModelForm):  # type: ignore[type-arg]
    fixed_allowances_text = forms.CharField(
        label=_("البدلات الثابتة"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": _("مثال: بدل نقل:50000")}),
        help_text=_("سطر واحد لكل بدل بصيغة الاسم:المبلغ."),
    )

    class Meta:
        model = EmployeeContract
        fields = (
            "employee",
            "contract_type",
            "start_date",
            "end_date",
            "branch",
            "job_title",
            "department",
            "wage_basis",
            "basic_salary",
            "scheduled_work_days",
            "scheduled_hours",
            "probation_days",
            "default_shift_code",
            "fixed_allowances_text",
            "payment_method",
            "payroll_policy",
            "notes",
        )
        widgets = {
            "start_date": DATE_WIDGET,
            "end_date": DATE_WIDGET,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "employee": _("الموظف"),
            "contract_type": _("نوع العقد"),
            "start_date": _("تاريخ البداية"),
            "end_date": _("تاريخ النهاية"),
            "branch": _("الفرع"),
            "job_title": _("المسمى الوظيفي"),
            "department": _("القسم"),
            "wage_basis": _("أساس الأجر"),
            "basic_salary": _("الأجر الأساسي"),
            "scheduled_work_days": _("أيام العمل المجدولة"),
            "scheduled_hours": _("ساعات العمل المجدولة"),
            "probation_days": _("أيام التجربة"),
            "default_shift_code": _("رمز الوردية الافتراضية"),
            "payment_method": _("طريقة الدفع"),
            "payroll_policy": _("سياسة الرواتب"),
            "notes": _("ملاحظات"),
        }

    def __init__(
        self,
        *,
        actor: User,
        instance: EmployeeContract | None = None,
        employee: Employee | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(instance=instance, **kwargs)
        organizations = organizations_with_permission(actor, MANAGE_CONTRACT)
        cast(
            "forms.ModelChoiceField[Employee]", self.fields["employee"]
        ).queryset = Employee.objects.filter(organization__in=organizations)
        cast(
            "forms.ModelChoiceField[Branch]", self.fields["branch"]
        ).queryset = Branch.objects.filter(organization__in=organizations)
        cast(
            "forms.ModelChoiceField[PayrollPolicy]", self.fields["payroll_policy"]
        ).queryset = PayrollPolicy.objects.filter(organization__in=organizations, is_active=True)
        if employee is not None:
            self.fields["employee"].initial = employee
        if instance is not None:
            self.fields["employee"].disabled = True
            self.fields["fixed_allowances_text"].initial = allowances_as_text(instance)

    def clean_fixed_allowances_text(self) -> str:
        raw = str(self.cleaned_data.get("fixed_allowances_text", ""))
        try:
            parse_fixed_allowances(raw)
        except ValidationError as error:
            raise forms.ValidationError(error.messages) from error
        return raw

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        employee = cleaned.get("employee")
        branch = cleaned.get("branch")
        policy = cleaned.get("payroll_policy")
        if employee is not None and branch is not None:
            if employee.organization_id != branch.organization_id:
                self.add_error("branch", _("The branch belongs to another organization."))
        if employee is not None and policy is not None:
            if employee.organization_id != policy.organization_id:
                self.add_error("payroll_policy", _("The policy belongs to another organization."))
        return cleaned


class ShiftForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Shift
        fields = (
            "branch",
            "code",
            "name_ar",
            "name_en",
            "start_time",
            "end_time",
            "crosses_midnight",
            "scheduled_minutes",
            "break_minutes",
            "grace_minutes",
            "late_threshold_minutes",
            "early_departure_threshold_minutes",
            "effective_from",
            "effective_to",
            "is_active",
            "notes",
        )
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "effective_from": DATE_WIDGET,
            "effective_to": DATE_WIDGET,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "branch": _("الفرع"),
            "code": _("رمز الوردية"),
            "name_ar": _("الاسم بالعربية"),
            "name_en": _("الاسم بالإنجليزية"),
            "start_time": _("وقت البداية"),
            "end_time": _("وقت النهاية"),
            "crosses_midnight": _("تمتد بعد منتصف الليل"),
            "scheduled_minutes": _("دقائق العمل المجدولة"),
            "break_minutes": _("دقائق الاستراحة"),
            "grace_minutes": _("دقائق السماح"),
            "late_threshold_minutes": _("حد التأخر بالدقائق"),
            "early_departure_threshold_minutes": _("حد الانصراف المبكر بالدقائق"),
            "effective_from": _("سارية من"),
            "effective_to": _("سارية إلى"),
            "is_active": _("نشطة"),
            "notes": _("ملاحظات"),
        }

    def __init__(self, *, actor: User, instance: Shift | None = None, **kwargs: Any) -> None:
        super().__init__(instance=instance, **kwargs)
        organizations = organizations_with_permission(actor, MANAGE_SHIFT)
        cast(
            "forms.ModelChoiceField[Branch]", self.fields["branch"]
        ).queryset = Branch.objects.filter(organization__in=organizations, is_active=True)
        if instance is not None:
            self.fields["branch"].disabled = True
            self.fields["code"].disabled = True

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        start = cleaned.get("start_time")
        end = cleaned.get("end_time")
        crosses = cleaned.get("crosses_midnight")
        if start is not None and end is not None and crosses != (end <= start):
            self.add_error(
                "crosses_midnight",
                _("حدّد الامتداد بعد منتصف الليل عندما يكون وقت النهاية قبل البداية."),
            )
        return cleaned


class ShiftAssignmentForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = ShiftAssignment
        fields = (
            "employee",
            "shift",
            "effective_from",
            "effective_to",
            "rotation_code",
            "notes",
        )
        widgets = {
            "effective_from": DATE_WIDGET,
            "effective_to": DATE_WIDGET,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "employee": _("الموظف"),
            "shift": _("الوردية"),
            "effective_from": _("من تاريخ"),
            "effective_to": _("إلى تاريخ"),
            "rotation_code": _("رمز التناوب"),
            "notes": _("ملاحظات"),
        }

    def __init__(self, *, actor: User, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        organizations = organizations_with_permission(actor, ASSIGN_SHIFT)
        cast(
            "forms.ModelChoiceField[Employee]", self.fields["employee"]
        ).queryset = Employee.objects.filter(
            organization__in=organizations, status=EmployeeStatus.ACTIVE
        ).select_related("branch")
        cast("forms.ModelChoiceField[Shift]", self.fields["shift"]).queryset = Shift.objects.filter(
            organization__in=organizations, is_active=True
        ).select_related("branch")


class AttendanceEventForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = AttendanceEvent
        fields = (
            "employee",
            "branch",
            "business_date",
            "occurred_at",
            "event_type",
            "source",
            "device_reference",
            "notes",
        )
        widgets = {
            "occurred_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "business_date": DATE_WIDGET,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "employee": _("الموظف"),
            "branch": _("الفرع / مكان العمل"),
            "business_date": _("يوم العمل"),
            "occurred_at": _("وقت الحدث"),
            "event_type": _("نوع الحدث"),
            "source": _("المصدر"),
            "device_reference": _("مرجع الجهاز أو الاستيراد"),
            "notes": _("ملاحظات"),
        }

    def __init__(self, *, actor: User, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        organizations = organizations_with_permission(actor, RECORD_ATTENDANCE)
        cast(
            "forms.ModelChoiceField[Employee]", self.fields["employee"]
        ).queryset = Employee.objects.filter(organization__in=organizations).select_related(
            "branch"
        )
        cast(
            "forms.ModelChoiceField[Branch]", self.fields["branch"]
        ).queryset = Branch.objects.filter(organization__in=organizations, is_active=True)
        self.fields["source"].initial = AttendanceEventSource.MANUAL
        self.fields["source"].widget = forms.HiddenInput()


class AttendanceCorrectionForm(forms.Form):
    business_date = forms.DateField(label=_("يوم العمل المصحح"), widget=DATE_WIDGET)
    occurred_at = forms.DateTimeField(
        label=_("الوقت المصحح"), widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    event_type = forms.ChoiceField(label=_("نوع الحدث المصحح"), choices=AttendanceEventType.choices)
    reason = forms.CharField(label=_("سبب التصحيح"), widget=forms.Textarea(attrs={"rows": 2}))
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *, actor: User, event: AttendanceEvent, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if (
            not organizations_with_permission(actor, CORRECT_ATTENDANCE)
            .filter(pk=event.organization_id)
            .exists()
        ):
            self.fields["reason"].disabled = True
        self.fields["occurred_at"].initial = event.occurred_at
        self.fields["business_date"].initial = event.business_date
        self.fields["event_type"].initial = event.event_type
