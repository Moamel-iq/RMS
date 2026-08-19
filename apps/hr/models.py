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
