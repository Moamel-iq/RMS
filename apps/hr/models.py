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
