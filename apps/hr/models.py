"""Employee master, effective-dated contracts, and payroll policy evidence."""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel


class EmployeeStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("نشط")
    ON_LEAVE = "ON_LEAVE", _("في إجازة")
    SUSPENDED = "SUSPENDED", _("موقوف")
    TERMINATED = "TERMINATED", _("منتهية خدمته")
    ARCHIVED = "ARCHIVED", _("مؤرشف")


class EmployeePaymentMethod(models.TextChoices):
    CASH = "CASH", _("نقداً")
    BANK = "BANK", _("تحويل مصرفي")


class Employee(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="employees"
    )
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=200)
    name_en = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    identity_number = models.CharField(max_length=80, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=30, blank=True)
    marital_status = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    emergency_contact = models.CharField(max_length=200, blank=True)
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="employees"
    )
    department = models.CharField(max_length=120, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    workplace = models.CharField(max_length=160, blank=True)
    hire_date = models.DateField()
    termination_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=EmployeeStatus.choices, default=EmployeeStatus.ACTIVE
    )
    payment_method = models.CharField(
        max_length=12,
        choices=EmployeePaymentMethod.choices,
        default=EmployeePaymentMethod.CASH,
    )
    payment_reference = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_employees",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["organization__code", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"], name="hr_employee_code_unique"
            ),
            models.CheckConstraint(
                condition=Q(termination_date__isnull=True)
                | Q(termination_date__gte=models.F("hire_date")),
                name="hr_employee_termination_after_hire",
            ),
            models.CheckConstraint(
                condition=~Q(status=EmployeeStatus.TERMINATED) | Q(termination_date__isnull=False),
                name="hr_terminated_employee_has_date",
            ),
        ]
        permissions = [
            ("view_employee_workspace", "Can view employee workspace"),
            ("manage_employee", "Can manage employees"),
            ("terminate_employee", "Can terminate employees"),
            ("view_employee_personal", "Can view personal employee information"),
            ("view_employee_salary", "Can view employee salary information"),
        ]

    def clean(self) -> None:
        super().clean()
        if self.branch_id and self.organization_id:
            if self.branch.organization_id != self.organization_id:
                raise ValidationError({"branch": _("The branch belongs to another organization.")})

    @property
    def display_name(self) -> str:
        return self.name_ar or self.name_en

    @property
    def is_editable(self) -> bool:
        return self.status != EmployeeStatus.ARCHIVED

    def __str__(self) -> str:
        return f"{self.code} — {self.display_name}"


class EmployeeDocument(TimeStampedModel):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="documents")
    document_type = models.CharField(max_length=80)
    title = models.CharField(max_length=200)
    reference = models.CharField(max_length=120, blank=True)
    file = models.FileField(upload_to="hr/employee-documents/%Y/%m/", blank=True)
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_employee_documents",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(expiry_date__isnull=True)
                | Q(issue_date__isnull=True)
                | Q(expiry_date__gte=models.F("issue_date")),
                name="hr_employee_document_dates_valid",
            )
        ]

    def __str__(self) -> str:
        return f"{self.employee} · {self.title}"


class ProrationBasis(models.TextChoices):
    CALENDAR_DAY = "CALENDAR_DAY", _("أيام التقويم")
    SCHEDULED_WORKDAY = "SCHEDULED_WORKDAY", _("أيام العمل المجدولة")
    HOURLY = "HOURLY", _("بالساعة")


class PayrollPolicy(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="payroll_policies"
    )
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=160)
    name_en = models.CharField(max_length=160, blank=True)
    version = models.PositiveIntegerField(default=1)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    proration_basis = models.CharField(max_length=24, choices=ProrationBasis.choices)
    money_rounding = models.DecimalField(max_digits=8, decimal_places=3, default=Decimal("0.001"))
    hour_rounding = models.DecimalField(max_digits=8, decimal_places=3, default=Decimal("0.001"))
    salary_cutoff_day = models.PositiveSmallIntegerField(default=25)
    release_day = models.PositiveSmallIntegerField(default=1)
    overtime_multiplier = models.DecimalField(
        max_digits=8, decimal_places=3, default=Decimal("1.000")
    )
    max_overtime_minutes = models.PositiveIntegerField(default=0)
    unpaid_leave_affects_payroll = models.BooleanField(default=True)
    deduction_cap_percentage = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal("100.000")
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_payroll_policies",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["organization__code", "code", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code", "version"], name="hr_payroll_policy_version_unique"
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="hr_payroll_policy_dates_valid",
            ),
            models.CheckConstraint(
                condition=Q(salary_cutoff_day__gte=1) & Q(salary_cutoff_day__lte=31),
                name="hr_payroll_policy_cutoff_day_valid",
            ),
            models.CheckConstraint(
                condition=Q(release_day__gte=1) & Q(release_day__lte=31),
                name="hr_payroll_policy_release_day_valid",
            ),
            models.CheckConstraint(
                condition=Q(money_rounding__gt=0)
                & Q(hour_rounding__gt=0)
                & Q(overtime_multiplier__gte=0)
                & Q(deduction_cap_percentage__gte=0),
                name="hr_payroll_policy_values_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} v{self.version} — {self.name_ar}"


class ContractType(models.TextChoices):
    PERMANENT = "PERMANENT", _("دائم")
    FIXED_TERM = "FIXED_TERM", _("محدد المدة")
    TEMPORARY = "TEMPORARY", _("مؤقت")


class WageBasis(models.TextChoices):
    MONTHLY = "MONTHLY", _("شهري")
    DAILY = "DAILY", _("يومي")
    HOURLY = "HOURLY", _("بالساعة")


class ContractStatus(models.TextChoices):
    DRAFT = "DRAFT", _("مسودة")
    APPROVED = "APPROVED", _("معتمد")
    SUPERSEDED = "SUPERSEDED", _("مستبدل")
    CLOSED = "CLOSED", _("مغلق")
    CANCELLED = "CANCELLED", _("ملغى")


class EmployeeContract(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="employee_contracts"
    )
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="contracts")
    version = models.PositiveIntegerField()
    contract_type = models.CharField(max_length=16, choices=ContractType.choices)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=ContractStatus.choices, default=ContractStatus.DRAFT
    )
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="employee_contracts"
    )
    job_title = models.CharField(max_length=120)
    department = models.CharField(max_length=120, blank=True)
    wage_basis = models.CharField(max_length=12, choices=WageBasis.choices)
    basic_salary = models.DecimalField(max_digits=18, decimal_places=3)
    scheduled_work_days = models.DecimalField(max_digits=6, decimal_places=3)
    scheduled_hours = models.DecimalField(max_digits=8, decimal_places=3)
    probation_days = models.PositiveIntegerField(default=0)
    default_shift_code = models.CharField(max_length=40, blank=True)
    fixed_allowances = models.JSONField(default=list, blank=True)
    payment_method = models.CharField(max_length=12, choices=EmployeePaymentMethod.choices)
    payroll_policy = models.ForeignKey(
        PayrollPolicy, on_delete=models.PROTECT, related_name="contracts"
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_employee_contracts",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_employee_contracts",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["employee__code", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "version"], name="hr_employee_contract_version_unique"
            ),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
                name="hr_employee_contract_dates_valid",
            ),
            models.CheckConstraint(
                condition=Q(basic_salary__gt=0)
                & Q(scheduled_work_days__gt=0)
                & Q(scheduled_hours__gt=0),
                name="hr_employee_contract_work_values_positive",
            ),
            models.CheckConstraint(
                condition=~Q(status=ContractStatus.APPROVED)
                | (Q(approved_by__isnull=False) & Q(approved_at__isnull=False)),
                name="hr_approved_contract_has_evidence",
            ),
        ]
        permissions = [
            ("view_contract_workspace", "Can view employee contract workspace"),
            ("manage_contract", "Can manage employee contracts"),
            ("approve_contract", "Can approve employee contracts"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.employee_id and self.organization_id:
            if self.employee.organization_id != self.organization_id:
                errors["employee"] = str(_("The employee belongs to another organization."))
        if self.branch_id and self.organization_id:
            if self.branch.organization_id != self.organization_id:
                errors["branch"] = str(_("The branch belongs to another organization."))
        if self.payroll_policy_id and self.organization_id:
            if self.payroll_policy.organization_id != self.organization_id:
                errors["payroll_policy"] = str(_("The policy belongs to another organization."))
        if self.created_by_id and self.approved_by_id == self.created_by_id:
            errors["approved_by"] = str(_("The contract creator cannot approve it."))
        if errors:
            raise ValidationError(errors)

    @property
    def is_editable(self) -> bool:
        return self.status == ContractStatus.DRAFT

    def __str__(self) -> str:
        return f"{self.employee.code} · v{self.version}"


class Shift(TimeStampedModel):
    """Effective-dated shift version; assignments and punches retain this exact version."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="hr_shifts"
    )
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="hr_shifts"
    )
    code = models.CharField(max_length=40)
    version = models.PositiveIntegerField()
    name_ar = models.CharField(max_length=160)
    name_en = models.CharField(max_length=160, blank=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    crosses_midnight = models.BooleanField(default=False)
    scheduled_minutes = models.PositiveIntegerField()
    break_minutes = models.PositiveIntegerField(default=0)
    grace_minutes = models.PositiveIntegerField(default=0)
    late_threshold_minutes = models.PositiveIntegerField(default=1)
    early_departure_threshold_minutes = models.PositiveIntegerField(default=1)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_hr_shifts",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["branch__code", "code", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "code", "version"], name="hr_shift_version_unique"
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="hr_shift_dates_valid",
            ),
            models.CheckConstraint(
                condition=Q(scheduled_minutes__gt=0)
                & Q(break_minutes__lt=models.F("scheduled_minutes")),
                name="hr_shift_duration_valid",
            ),
        ]
        permissions = [
            ("view_shift_workspace", "Can view shift workspace"),
            ("manage_shift", "Can manage shift definitions"),
            ("assign_shift", "Can assign employee shifts"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.branch_id and self.organization_id:
            if self.branch.organization_id != self.organization_id:
                errors["branch"] = str(_("The branch belongs to another organization."))
        inferred_crossing = self.end_time <= self.start_time
        if self.crosses_midnight != inferred_crossing:
            errors["crosses_midnight"] = str(
                _("Cross-midnight must match the relationship between start and end time.")
            )
        if errors:
            raise ValidationError(errors)

    @property
    def display_name(self) -> str:
        return self.name_ar or self.name_en

    def __str__(self) -> str:
        return f"{self.branch.code} · {self.code} v{self.version} — {self.display_name}"


class ShiftAssignment(TimeStampedModel):
    """One effective-dated employee schedule assignment."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="shift_assignments"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="shift_assignments"
    )
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="assignments")
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    rotation_code = models.CharField(max_length=60, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_shift_assignments",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["employee__code", "-effective_from", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="hr_shift_assignment_dates_valid",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.employee_id and self.organization_id:
            if self.employee.organization_id != self.organization_id:
                errors["employee"] = str(_("The employee belongs to another organization."))
        if self.shift_id and self.organization_id:
            if self.shift.organization_id != self.organization_id:
                errors["shift"] = str(_("The shift belongs to another organization."))
            if self.employee_id and self.employee.branch_id != self.shift.branch_id:
                errors["shift"] = str(_("The shift belongs to another branch."))
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.employee.code} · {self.shift.code} · {self.effective_from}"


class AttendanceEventType(models.TextChoices):
    CHECK_IN = "CHECK_IN", _("دخول")
    CHECK_OUT = "CHECK_OUT", _("خروج")
    BREAK_OUT = "BREAK_OUT", _("بدء استراحة")
    BREAK_IN = "BREAK_IN", _("نهاية استراحة")


class AttendanceEventSource(models.TextChoices):
    MANUAL = "MANUAL", _("إدخال يدوي")
    DEVICE_IMPORT = "DEVICE_IMPORT", _("استيراد جهاز")
    FUTURE_FINGERPRINT_IMPORT = "FUTURE_FINGERPRINT_IMPORT", _("استيراد بصمة مستقبلي")


class AttendanceEvent(TimeStampedModel):
    """Append-only punch. Corrections append another event linked through ``supersedes``."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="attendance_events"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="attendance_events"
    )
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="attendance_events"
    )
    shift_assignment = models.ForeignKey(
        ShiftAssignment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attendance_events",
    )
    scheduled_shift = models.ForeignKey(
        Shift,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attendance_events",
    )
    business_date = models.DateField(db_index=True)
    occurred_at = models.DateTimeField(db_index=True)
    event_type = models.CharField(max_length=16, choices=AttendanceEventType.choices)
    source = models.CharField(max_length=32, choices=AttendanceEventSource.choices)
    device_reference = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_attendance_events",
    )
    supersedes = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="corrections",
    )
    correction_reason = models.TextField(blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["business_date", "employee__code", "occurred_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(supersedes__isnull=True) & Q(correction_reason=""))
                | (Q(supersedes__isnull=False) & ~Q(correction_reason="")),
                name="hr_attendance_correction_has_reason",
            ),
        ]
        permissions = [
            ("view_attendance_workspace", "Can view attendance workspace"),
            ("record_attendance", "Can record attendance events"),
            ("correct_attendance", "Can correct attendance events"),
            ("approve_attendance", "Can approve attendance days"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.employee_id and self.organization_id:
            if self.employee.organization_id != self.organization_id:
                errors["employee"] = str(_("The employee belongs to another organization."))
        if self.branch_id and self.organization_id:
            if self.branch.organization_id != self.organization_id:
                errors["branch"] = str(_("The branch belongs to another organization."))
        if self.employee_id and self.branch_id and self.employee.branch_id != self.branch_id:
            errors["branch"] = str(_("The employee belongs to another branch."))
        if self.shift_assignment_id and self.employee_id:
            assignment = self.shift_assignment
            if assignment is not None and assignment.employee_id != self.employee_id:
                errors["shift_assignment"] = str(_("The assignment belongs to another employee."))
        if self.scheduled_shift_id and self.branch_id:
            scheduled_shift = self.scheduled_shift
            if scheduled_shift is not None and scheduled_shift.branch_id != self.branch_id:
                errors["scheduled_shift"] = str(_("The shift belongs to another branch."))
        if self.supersedes_id and self.supersedes_id == self.pk:
            errors["supersedes"] = str(_("An event cannot correct itself."))
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.employee.code} · {self.business_date} · {self.event_type}"


class AttendanceApprovalStatus(models.TextChoices):
    DRAFT = "DRAFT", _("قيد المراجعة")
    APPROVED = "APPROVED", _("معتمد")
    REOPENED = "REOPENED", _("أعيد فتحه")


class AttendanceDayApproval(TimeStampedModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="attendance_approvals"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="attendance_approvals"
    )
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="attendance_approvals"
    )
    business_date = models.DateField()
    status = models.CharField(
        max_length=12,
        choices=AttendanceApprovalStatus.choices,
        default=AttendanceApprovalStatus.DRAFT,
    )
    result_snapshot = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_attendance_approvals",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_attendance_days",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-business_date", "employee__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "business_date"], name="hr_attendance_day_unique"
            ),
            models.CheckConstraint(
                condition=~Q(status=AttendanceApprovalStatus.APPROVED)
                | (Q(approved_by__isnull=False) & Q(approved_at__isnull=False)),
                name="hr_attendance_approval_has_evidence",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee.code} · {self.business_date} · {self.status}"


class PaidTreatment(models.TextChoices):
    PAID = "PAID", _("مدفوعة")
    UNPAID = "UNPAID", _("غير مدفوعة")
    POLICY = "POLICY", _("وفق السياسة")


class LeaveType(TimeStampedModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="leave_types"
    )
    code = models.CharField(max_length=40)
    name_ar = models.CharField(max_length=160)
    name_en = models.CharField(max_length=160, blank=True)
    paid_treatment = models.CharField(max_length=12, choices=PaidTreatment.choices)
    requires_evidence = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_leave_types",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["organization__code", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"], name="hr_leave_type_code_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class RequestStatus(models.TextChoices):
    DRAFT = "DRAFT", _("مسودة")
    SUBMITTED = "SUBMITTED", _("مقدم")
    APPROVED = "APPROVED", _("معتمد")
    REJECTED = "REJECTED", _("مرفوض")
    CANCELLED = "CANCELLED", _("ملغى")


class LeaveRequest(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="leave_requests"
    )
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="leave_requests")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="requests")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    requested_minutes = models.PositiveIntegerField()
    paid_treatment = models.CharField(max_length=12, choices=PaidTreatment.choices)
    reason = models.TextField()
    evidence_reference = models.CharField(max_length=200, blank=True)
    evidence_file = models.FileField(upload_to="hr/leave-evidence/%Y/%m/", blank=True)
    status = models.CharField(
        max_length=12, choices=RequestStatus.choices, default=RequestStatus.DRAFT
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_leave",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_leave",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-start_at", "employee__code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_at__gt=models.F("start_at")), name="hr_leave_dates_valid"
            ),
            models.CheckConstraint(
                condition=Q(requested_minutes__gt=0), name="hr_leave_minutes_positive"
            ),
            models.CheckConstraint(
                condition=~Q(status=RequestStatus.APPROVED)
                | (Q(approved_by__isnull=False) & Q(approved_at__isnull=False)),
                name="hr_approved_leave_has_evidence",
            ),
        ]
        permissions = [
            ("view_leave_workspace", "Can view leave workspace"),
            ("request_leave", "Can create leave requests"),
            ("approve_leave", "Can approve leave requests"),
            ("classify_absence", "Can classify employee absences"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.employee_id and self.organization_id:
            if self.employee.organization_id != self.organization_id:
                errors["employee"] = str(_("The employee belongs to another organization."))
        if self.leave_type_id and self.organization_id:
            if self.leave_type.organization_id != self.organization_id:
                errors["leave_type"] = str(_("The leave type belongs to another organization."))
            if (
                self.leave_type.requires_evidence
                and not self.evidence_reference
                and not self.evidence_file
            ):
                errors["evidence_reference"] = str(_("This leave type requires evidence."))
        if self.requested_by_id and self.approved_by_id == self.requested_by_id:
            errors["approved_by"] = str(_("The request creator cannot approve it."))
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.employee.code} · {self.leave_type.code} · {self.start_at.date()}"


class AbsenceClassification(models.TextChoices):
    ABSENT = "ABSENT", _("غياب غير مبرر")
    APPROVED_PAID_LEAVE = "APPROVED_PAID_LEAVE", _("إجازة مدفوعة معتمدة")
    APPROVED_UNPAID_LEAVE = "APPROVED_UNPAID_LEAVE", _("إجازة غير مدفوعة معتمدة")
    EXCUSED = "EXCUSED", _("غياب بعذر")
    NOT_SCHEDULED = "NOT_SCHEDULED", _("غير مجدول")


class AbsenceRecord(TimeStampedModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="absence_records"
    )
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="absence_records")
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="absence_records"
    )
    business_date = models.DateField()
    classification = models.CharField(max_length=32, choices=AbsenceClassification.choices)
    leave_request = models.ForeignKey(
        LeaveRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="absence_records",
    )
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="classified_absences"
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["-business_date", "employee__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "business_date"], name="hr_absence_employee_day_unique"
            )
        ]


class OvertimeSource(models.TextChoices):
    REQUESTED = "REQUESTED", _("مطلوب مسبقاً")
    ATTENDANCE_DERIVED = "ATTENDANCE_DERIVED", _("مشتق من الحضور")
    MANUAL_CORRECTION = "MANUAL_CORRECTION", _("تصحيح يدوي")


class OvertimeRequest(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="overtime_requests"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="overtime_requests"
    )
    business_date = models.DateField()
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="overtime_requests")
    requested_minutes = models.PositiveIntegerField()
    approved_minutes = models.PositiveIntegerField(default=0)
    source = models.CharField(max_length=24, choices=OvertimeSource.choices)
    multiplier = models.DecimalField(max_digits=8, decimal_places=3)
    classification = models.CharField(max_length=40, blank=True)
    reason = models.TextField()
    evidence_reference = models.CharField(max_length=200, blank=True)
    evidence_file = models.FileField(upload_to="hr/overtime-evidence/%Y/%m/", blank=True)
    status = models.CharField(
        max_length=12, choices=RequestStatus.choices, default=RequestStatus.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_overtime"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_overtime",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    payroll_inclusion_reference = models.CharField(max_length=120, blank=True)
    included_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-business_date", "employee__code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(requested_minutes__gt=0), name="hr_overtime_minutes_positive"
            ),
            models.CheckConstraint(
                condition=Q(approved_minutes__lte=models.F("requested_minutes")),
                name="hr_overtime_approved_within_request",
            ),
            models.CheckConstraint(
                condition=Q(multiplier__gte=0), name="hr_overtime_multiplier_nonnegative"
            ),
            models.CheckConstraint(
                condition=~Q(status=RequestStatus.APPROVED)
                | (
                    Q(approved_by__isnull=False)
                    & Q(approved_at__isnull=False)
                    & Q(approved_minutes__gt=0)
                ),
                name="hr_approved_overtime_has_evidence",
            ),
        ]
        permissions = [
            ("view_overtime_workspace", "Can view overtime workspace"),
            ("manage_overtime", "Can create overtime requests"),
            ("approve_overtime", "Can approve overtime requests"),
        ]

    @property
    def is_included(self) -> bool:
        return bool(self.payroll_inclusion_reference)


class DeductionType(models.TextChoices):
    ABSENCE = "ABSENCE", _("غياب")
    LATENESS = "LATENESS", _("تأخر")
    EARLY_DEPARTURE = "EARLY_DEPARTURE", _("انصراف مبكر")
    ADMINISTRATIVE = "ADMINISTRATIVE", _("استقطاع إداري")
    DAMAGE = "DAMAGE", _("ضرر مثبت")
    CASH_SHORTAGE = "CASH_SHORTAGE", _("عجز صندوق")
    EQUIPMENT = "EQUIPMENT", _("زي أو معدات")
    OTHER = "OTHER", _("أخرى")


class RecoveryMode(models.TextChoices):
    ONE_TIME = "ONE_TIME", _("دفعة واحدة")
    INSTALMENTS = "INSTALMENTS", _("أقساط")


class EmployeeDeduction(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="employee_deductions"
    )
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="deductions")
    deduction_type = models.CharField(max_length=24, choices=DeductionType.choices)
    original_amount = models.DecimalField(max_digits=18, decimal_places=3)
    approved_amount = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0.000"))
    effective_period = models.DateField()
    recovery_mode = models.CharField(max_length=16, choices=RecoveryMode.choices)
    instalment_count = models.PositiveIntegerField(default=1)
    evidence_reference = models.CharField(max_length=200)
    evidence_file = models.FileField(upload_to="hr/deduction-evidence/%Y/%m/", blank=True)
    reason = models.TextField()
    status = models.CharField(
        max_length=12, choices=RequestStatus.choices, default=RequestStatus.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_deductions"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_deductions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    replaces = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="replacements"
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["-effective_period", "employee__code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(original_amount__gt=0)
                & Q(approved_amount__gte=0)
                & Q(approved_amount__lte=models.F("original_amount")),
                name="hr_deduction_amounts_valid",
            ),
            models.CheckConstraint(
                condition=Q(instalment_count__gt=0), name="hr_deduction_instalments_positive"
            ),
            models.CheckConstraint(
                condition=~Q(status=RequestStatus.APPROVED)
                | (
                    Q(approved_by__isnull=False)
                    & Q(approved_at__isnull=False)
                    & Q(approved_amount__gt=0)
                ),
                name="hr_approved_deduction_has_evidence",
            ),
        ]
        permissions = [
            ("view_deduction_workspace", "Can view deduction workspace"),
            ("manage_deduction", "Can manage employee deductions"),
            ("approve_deduction", "Can approve employee deductions"),
        ]

    @property
    def allocated_amount(self) -> Decimal:
        return sum((row.amount for row in self.allocations.all()), Decimal("0.000"))

    @property
    def remaining_amount(self) -> Decimal:
        return self.approved_amount - self.allocated_amount


class DeductionAllocation(TimeStampedModel):
    deduction = models.ForeignKey(
        EmployeeDeduction, on_delete=models.PROTECT, related_name="allocations"
    )
    payroll_reference = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=18, decimal_places=3)
    allocated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["deduction", "payroll_reference"], name="hr_deduction_payroll_unique"
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="hr_deduction_allocation_positive"
            ),
        ]


class AdvanceType(models.TextChoices):
    SALARY_ADVANCE = "SALARY_ADVANCE", _("سلفة راتب")
    EMPLOYEE_LOAN = "EMPLOYEE_LOAN", _("قرض موظف")
    CASH_SHORTAGE = "CASH_SHORTAGE", _("عجز صندوق مستحق")
    DAMAGE = "DAMAGE", _("ضرر مثبت مستحق")
    OTHER = "OTHER", _("ذمة موظف أخرى")


class AdvanceStatus(models.TextChoices):
    DRAFT = "DRAFT", _("مسودة")
    SUBMITTED = "SUBMITTED", _("مقدم")
    APPROVED = "APPROVED", _("معتمد")
    PARTIALLY_DISBURSED = "PARTIALLY_DISBURSED", _("مصروف جزئياً")
    DISBURSED = "DISBURSED", _("مصروف")
    CLOSED = "CLOSED", _("مسدد")
    CANCELLED = "CANCELLED", _("ملغى")


class EmployeeAdvance(TimeStampedModel):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="employee_advances"
    )
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="advances")
    advance_type = models.CharField(max_length=24, choices=AdvanceType.choices)
    principal_amount = models.DecimalField(max_digits=18, decimal_places=3)
    request_date = models.DateField()
    status = models.CharField(
        max_length=24, choices=AdvanceStatus.choices, default=AdvanceStatus.DRAFT
    )
    recovery_mode = models.CharField(max_length=16, choices=RecoveryMode.choices)
    instalment_amount = models.DecimalField(
        max_digits=18, decimal_places=3, default=Decimal("0.000")
    )
    instalment_count = models.PositiveIntegerField(default=1)
    first_recovery_period = models.DateField()
    payment_method = models.CharField(max_length=12, choices=EmployeePaymentMethod.choices)
    evidence_reference = models.CharField(max_length=200)
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_advances"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_advances",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-request_date", "employee__code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(principal_amount__gt=0)
                & Q(instalment_amount__gte=0)
                & Q(instalment_amount__lte=models.F("principal_amount"))
                & Q(instalment_count__gt=0),
                name="hr_advance_values_valid",
            ),
            models.CheckConstraint(
                condition=~Q(status=AdvanceStatus.APPROVED)
                | (Q(approved_by__isnull=False) & Q(approved_at__isnull=False)),
                name="hr_approved_advance_has_evidence",
            ),
        ]
        permissions = [
            ("view_advance_workspace", "Can view employee advances"),
            ("manage_advance", "Can manage employee advances"),
            ("approve_advance", "Can approve employee advances"),
            ("disburse_advance", "Can disburse employee advances"),
        ]

    @property
    def disbursed_amount(self) -> Decimal:
        return sum((row.net_amount for row in self.disbursements.all()), Decimal("0.000"))

    @property
    def recovered_amount(self) -> Decimal:
        return sum((row.amount for row in self.recoveries.all()), Decimal("0.000"))

    @property
    def outstanding_amount(self) -> Decimal:
        return self.disbursed_amount - self.recovered_amount


class AdvanceDisbursement(TimeStampedModel):
    advance = models.ForeignKey(
        EmployeeAdvance, on_delete=models.PROTECT, related_name="disbursements"
    )
    amount = models.DecimalField(max_digits=18, decimal_places=3)
    disbursement_date = models.DateField()
    payment_method = models.CharField(max_length=12, choices=EmployeePaymentMethod.choices)
    evidence_reference = models.CharField(max_length=200)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="advance_disbursements",
    )
    reversal_of = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversal"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_advance_disbursements",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="hr_advance_disbursement_positive"
            )
        ]

    @property
    def net_amount(self) -> Decimal:
        return -self.amount if self.reversal_of_id else self.amount


class AdvanceRecoveryAllocation(TimeStampedModel):
    advance = models.ForeignKey(
        EmployeeAdvance, on_delete=models.PROTECT, related_name="recoveries"
    )
    payroll_reference = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=18, decimal_places=3)
    recovered_at = models.DateTimeField()
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="advance_recoveries",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["advance", "payroll_reference"], name="hr_advance_payroll_unique"
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="hr_advance_recovery_positive"),
        ]
