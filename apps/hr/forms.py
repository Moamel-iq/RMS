"""Validated employee, document, and contract forms."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.hr.models import (
    AttendanceEvent,
    AttendanceEventSource,
    AttendanceEventType,
    Employee,
    EmployeeAdvance,
    EmployeeContract,
    EmployeeDeduction,
    EmployeeDocument,
    EmployeePaymentMethod,
    EmployeeStatus,
    LeaveRequest,
    LeaveType,
    OvertimeRequest,
    PayrollEmployeeLine,
    PayrollPolicy,
    PayrollRun,
    Shift,
    ShiftAssignment,
)
from apps.hr.permissions import (
    ASSIGN_SHIFT,
    CALCULATE_PAYROLL,
    CORRECT_ATTENDANCE,
    MANAGE_ADVANCE,
    MANAGE_CONTRACT,
    MANAGE_DEDUCTION,
    MANAGE_EMPLOYEE,
    MANAGE_OVERTIME,
    MANAGE_SHIFT,
    RECORD_ATTENDANCE,
    REQUEST_LEAVE,
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
            "name",
            "name",
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
            "name": _("الاسم بالعربية"),
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
            "name",
            "name",
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
            "name": _("الاسم بالعربية"),
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


class LeaveTypeForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = LeaveType
        fields = (
            "organization",
            "code",
            "name",
            "name",
            "paid_treatment",
            "requires_evidence",
            "is_active",
            "notes",
        )
        labels = {
            "organization": _("المؤسسة"),
            "code": _("رمز نوع الإجازة"),
            "name": _("الاسم بالعربية"),
            "paid_treatment": _("معالجة الأجر"),
            "requires_evidence": _("يتطلب مستنداً"),
            "is_active": _("نشط"),
            "notes": _("ملاحظات"),
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *, actor: User, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        cast(
            "forms.ModelChoiceField[Organization]", self.fields["organization"]
        ).queryset = organizations_with_permission(actor, REQUEST_LEAVE)


class LeaveRequestForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = LeaveRequest
        fields = (
            "employee",
            "leave_type",
            "start_at",
            "end_at",
            "reason",
            "evidence_reference",
            "evidence_file",
        )
        widgets = {
            "start_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "end_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "reason": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "employee": _("الموظف"),
            "leave_type": _("نوع الإجازة"),
            "start_at": _("البداية"),
            "end_at": _("النهاية"),
            "reason": _("السبب"),
            "evidence_reference": _("مرجع المستند"),
            "evidence_file": _("ملف الإثبات"),
        }

    def __init__(self, *, actor: User, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        organizations = organizations_with_permission(actor, REQUEST_LEAVE)
        cast(
            "forms.ModelChoiceField[Employee]", self.fields["employee"]
        ).queryset = Employee.objects.filter(organization__in=organizations).select_related(
            "branch"
        )
        cast(
            "forms.ModelChoiceField[LeaveType]", self.fields["leave_type"]
        ).queryset = LeaveType.objects.filter(organization__in=organizations, is_active=True)


class OvertimeRequestForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = OvertimeRequest
        fields = (
            "employee",
            "business_date",
            "requested_minutes",
            "source",
            "classification",
            "reason",
            "evidence_reference",
            "evidence_file",
        )
        widgets = {
            "business_date": DATE_WIDGET,
            "reason": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "employee": _("الموظف"),
            "business_date": _("يوم العمل"),
            "requested_minutes": _("الدقائق المطلوبة"),
            "source": _("المصدر"),
            "classification": _("تصنيف اليوم / العطلة"),
            "reason": _("السبب"),
            "evidence_reference": _("مرجع الإثبات"),
            "evidence_file": _("ملف الإثبات"),
        }

    def __init__(self, *, actor: User, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        organizations = organizations_with_permission(actor, MANAGE_OVERTIME)
        cast(
            "forms.ModelChoiceField[Employee]", self.fields["employee"]
        ).queryset = Employee.objects.filter(organization__in=organizations)


class DeductionForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = EmployeeDeduction
        fields = (
            "employee",
            "deduction_type",
            "original_amount",
            "effective_period",
            "recovery_mode",
            "instalment_count",
            "evidence_reference",
            "evidence_file",
            "reason",
        )
        widgets = {
            "effective_period": DATE_WIDGET,
            "reason": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "employee": _("الموظف"),
            "deduction_type": _("نوع الاستقطاع"),
            "original_amount": _("المبلغ الأصلي"),
            "effective_period": _("فترة الرواتب"),
            "recovery_mode": _("طريقة الاستقطاع"),
            "instalment_count": _("عدد الأقساط"),
            "evidence_reference": _("مرجع الإثبات"),
            "evidence_file": _("ملف الإثبات"),
            "reason": _("السبب"),
        }

    def __init__(self, *, actor: User, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        organizations = organizations_with_permission(actor, MANAGE_DEDUCTION)
        cast(
            "forms.ModelChoiceField[Employee]", self.fields["employee"]
        ).queryset = Employee.objects.filter(organization__in=organizations)


class AdvanceForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = EmployeeAdvance
        fields = (
            "employee",
            "advance_type",
            "principal_amount",
            "request_date",
            "recovery_mode",
            "instalment_amount",
            "instalment_count",
            "first_recovery_period",
            "payment_method",
            "evidence_reference",
            "reason",
        )
        widgets = {
            "request_date": DATE_WIDGET,
            "first_recovery_period": DATE_WIDGET,
            "reason": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "employee": _("الموظف"),
            "advance_type": _("نوع السلفة / الذمة"),
            "principal_amount": _("المبلغ الأصلي"),
            "request_date": _("تاريخ الطلب"),
            "recovery_mode": _("طريقة الاسترداد"),
            "instalment_amount": _("قيمة القسط"),
            "instalment_count": _("عدد الأقساط"),
            "first_recovery_period": _("أول فترة استرداد"),
            "payment_method": _("طريقة الصرف"),
            "evidence_reference": _("مرجع الإثبات"),
            "reason": _("السبب"),
        }

    def __init__(self, *, actor: User, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        organizations = organizations_with_permission(actor, MANAGE_ADVANCE)
        cast(
            "forms.ModelChoiceField[Employee]", self.fields["employee"]
        ).queryset = Employee.objects.filter(organization__in=organizations)


class PayrollRunForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = PayrollRun
        fields = (
            "branch",
            "period_start",
            "period_end",
            "accounting_date",
            "policy",
            "notes",
        )
        widgets = {
            "period_start": DATE_WIDGET,
            "period_end": DATE_WIDGET,
            "accounting_date": DATE_WIDGET,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "branch": _("الفرع"),
            "period_start": _("بداية الفترة"),
            "period_end": _("نهاية الفترة"),
            "accounting_date": _("التاريخ المحاسبي"),
            "policy": _("سياسة الرواتب"),
            "notes": _("ملاحظات"),
        }

    def __init__(self, *, actor: User, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        organizations = organizations_with_permission(actor, CALCULATE_PAYROLL)
        cast(
            "forms.ModelChoiceField[Branch]", self.fields["branch"]
        ).queryset = Branch.objects.filter(organization__in=organizations, is_active=True)
        cast(
            "forms.ModelChoiceField[PayrollPolicy]", self.fields["policy"]
        ).queryset = PayrollPolicy.objects.filter(
            organization__in=organizations, is_active=True
        ).order_by("organization__code", "code", "-version")


class PayrollPaymentForm(forms.Form):
    MODE_FULL = "FULL"
    MODE_CUSTOM = "CUSTOM"
    mode = forms.ChoiceField(
        label=_("نطاق الصرف"),
        choices=(
            (MODE_FULL, _("صرف كامل الرصيد المستحق")),
            (MODE_CUSTOM, _("صرف جزئي أو لموظف محدد")),
        ),
    )
    payment_date = forms.DateField(label=_("تاريخ الصرف"), widget=DATE_WIDGET)
    method = forms.ChoiceField(label=_("طريقة الصرف"), choices=EmployeePaymentMethod.choices)
    reference = forms.CharField(label=_("مرجع الصرف"), max_length=200)
    reason = forms.CharField(
        label=_("البيان"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, *, payroll_run: PayrollRun, **kwargs: Any) -> None:
        self.payroll_run = payroll_run
        super().__init__(**kwargs)
        if not self.is_bound:
            self.initial.update(
                {
                    "payment_date": payroll_run.released_at.date()
                    if payroll_run.released_at
                    else payroll_run.period_end,
                    "mode": self.MODE_FULL,
                    "idempotency_key": str(uuid.uuid4()),
                }
            )
        self.payment_lines = [
            line
            for line in payroll_run.employee_lines.select_related("employee").order_by(
                "employee_code"
            )
            if line.outstanding_amount > Decimal("0.000")
        ]
        for line in self.payment_lines:
            self.fields[self._field_name(line)] = forms.DecimalField(
                label=f"{line.employee_code} — {line.employee_name_ar}",
                min_value=Decimal("0.000"),
                max_digits=18,
                decimal_places=3,
                required=False,
                initial=line.outstanding_amount,
                help_text=_("المستحق: %(amount)s IQD") % {"amount": line.outstanding_amount},
            )

    @staticmethod
    def _field_name(line: PayrollEmployeeLine) -> str:
        return f"employee_{line.pk}"

    def allocation_fields(self) -> list[tuple[PayrollEmployeeLine, forms.BoundField]]:
        return [(line, self[self._field_name(line)]) for line in self.payment_lines]

    def payment_allocations(self) -> list[tuple[PayrollEmployeeLine, Decimal]]:
        if not self.is_valid():
            raise ValueError("payment_allocations requires a valid form")
        if self.cleaned_data["mode"] == self.MODE_FULL:
            return [(line, line.outstanding_amount) for line in self.payment_lines]
        return [
            (line, amount)
            for line in self.payment_lines
            if (amount := self.cleaned_data.get(self._field_name(line)))
            and amount > Decimal("0.000")
        ]

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if not self.payment_lines:
            raise ValidationError(_("لا يوجد رصيد رواتب مستحق للصرف."))
        if cleaned.get("mode") == self.MODE_CUSTOM:
            selected = [
                cleaned.get(self._field_name(line), Decimal("0.000")) for line in self.payment_lines
            ]
            if not any(amount and amount > Decimal("0.000") for amount in selected):
                raise ValidationError(_("أدخل مبلغاً لموظف واحد على الأقل."))
            for line, amount in zip(self.payment_lines, selected, strict=True):
                if amount and amount > line.outstanding_amount:
                    self.add_error(
                        self._field_name(line),
                        _("المبلغ يتجاوز صافي الراتب المستحق."),
                    )
        return cleaned


class PayrollReversalForm(forms.Form):
    reversal_date = forms.DateField(label=_("تاريخ العكس"), widget=DATE_WIDGET)
    reason = forms.CharField(label=_("سبب العكس"), widget=forms.Textarea(attrs={"rows": 2}))
