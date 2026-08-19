"""Locked employee and effective-dated contract workflows."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.hr.models import (
    ContractStatus,
    Employee,
    EmployeeContract,
    EmployeeDocument,
    EmployeeStatus,
    PayrollPolicy,
)
from apps.organizations.models import Branch, Organization
from apps.users.models import User


def _validate(instance: Any) -> None:
    instance.full_clean()


@transaction.atomic
def create_employee(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    name_en: str,
    phone: str,
    email: str,
    identity_number: str,
    date_of_birth: datetime.date | None,
    gender: str,
    marital_status: str,
    address: str,
    emergency_contact: str,
    branch: Branch,
    department: str,
    job_title: str,
    workplace: str,
    hire_date: datetime.date,
    payment_method: str,
    payment_reference: str,
    notes: str,
    actor: User,
) -> Employee:
    employee = Employee(
        organization=organization,
        code=code.strip().upper(),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        phone=phone.strip(),
        email=email.strip(),
        identity_number=identity_number.strip(),
        date_of_birth=date_of_birth,
        gender=gender.strip(),
        marital_status=marital_status.strip(),
        address=address.strip(),
        emergency_contact=emergency_contact.strip(),
        branch=branch,
        department=department.strip(),
        job_title=job_title.strip(),
        workplace=workplace.strip(),
        hire_date=hire_date,
        payment_method=payment_method,
        payment_reference=payment_reference.strip(),
        notes=notes.strip(),
        created_by=actor,
    )
    _validate(employee)
    employee.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=employee,
        branch=employee.branch,
        new_state=snapshot(employee),
    )
    return employee


@transaction.atomic
def update_employee(
    *,
    employee: Employee,
    name_ar: str,
    name_en: str,
    phone: str,
    email: str,
    identity_number: str,
    date_of_birth: datetime.date | None,
    gender: str,
    marital_status: str,
    address: str,
    emergency_contact: str,
    branch: Branch,
    department: str,
    job_title: str,
    workplace: str,
    hire_date: datetime.date,
    payment_method: str,
    payment_reference: str,
    notes: str,
) -> Employee:
    locked = Employee.objects.select_for_update().get(pk=employee.pk)
    if locked.status == EmployeeStatus.ARCHIVED:
        raise ValidationError(_("An archived employee cannot be edited."), code="employee_archived")
    previous = snapshot(locked)
    for field, value in {
        "name_ar": name_ar.strip(),
        "name_en": name_en.strip(),
        "phone": phone.strip(),
        "email": email.strip(),
        "identity_number": identity_number.strip(),
        "date_of_birth": date_of_birth,
        "gender": gender.strip(),
        "marital_status": marital_status.strip(),
        "address": address.strip(),
        "emergency_contact": emergency_contact.strip(),
        "branch": branch,
        "department": department.strip(),
        "job_title": job_title.strip(),
        "workplace": workplace.strip(),
        "hire_date": hire_date,
        "payment_method": payment_method,
        "payment_reference": payment_reference.strip(),
        "notes": notes.strip(),
    }.items():
        setattr(locked, field, value)
    _validate(locked)
    locked.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def archive_employee(*, employee: Employee, reason: str) -> Employee:
    locked = Employee.objects.select_for_update().get(pk=employee.pk)
    if locked.status == EmployeeStatus.ARCHIVED:
        return locked
    previous = snapshot(locked)
    locked.status = EmployeeStatus.ARCHIVED
    locked.save(update_fields=["status", "updated_at"])
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason,
    )
    return locked


@transaction.atomic
def reactivate_employee(*, employee: Employee, reason: str) -> Employee:
    locked = Employee.objects.select_for_update().get(pk=employee.pk)
    if locked.status != EmployeeStatus.ARCHIVED:
        raise ValidationError(_("Only an archived employee can be reactivated."))
    previous = snapshot(locked)
    locked.status = EmployeeStatus.TERMINATED if locked.termination_date else EmployeeStatus.ACTIVE
    locked.save(update_fields=["status", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason,
    )
    return locked


@transaction.atomic
def terminate_employee(
    *, employee: Employee, termination_date: datetime.date, reason: str
) -> Employee:
    if not reason.strip():
        raise ValidationError(
            _("Termination requires a reason."), code="termination_reason_required"
        )
    locked = Employee.objects.select_for_update().get(pk=employee.pk)
    if termination_date < locked.hire_date:
        raise ValidationError(_("Termination cannot precede hire date."))
    previous = snapshot(locked)
    locked.status = EmployeeStatus.TERMINATED
    locked.termination_date = termination_date
    _validate(locked)
    locked.save(update_fields=["status", "termination_date", "updated_at"])

    contracts = (
        EmployeeContract.objects.select_for_update()
        .filter(employee=locked)
        .filter(Q(status=ContractStatus.APPROVED) | Q(status=ContractStatus.DRAFT))
    )
    for contract in contracts:
        contract_previous = snapshot(contract)
        if contract.status == ContractStatus.DRAFT:
            contract.status = ContractStatus.CANCELLED
            fields = ["status", "updated_at"]
        elif contract.start_date > termination_date:
            contract.status = ContractStatus.CANCELLED
            fields = ["status", "updated_at"]
        else:
            contract.status = ContractStatus.CLOSED
            if contract.end_date is None or contract.end_date > termination_date:
                contract.end_date = termination_date
            fields = ["status", "end_date", "updated_at"]
        contract.save(update_fields=fields)
        record_audit_event(
            action=AuditAction.CANCELLED,
            target=contract,
            branch=contract.branch,
            previous_state=contract_previous,
            new_state=snapshot(contract),
            reason=reason,
        )
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason,
    )
    return locked


@transaction.atomic
def add_employee_document(*, employee: Employee, actor: User, **values: Any) -> EmployeeDocument:
    locked = Employee.objects.select_for_update().get(pk=employee.pk)
    document = EmployeeDocument(employee=locked, created_by=actor, **values)
    _validate(document)
    document.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=document,
        branch=locked.branch,
        new_state=snapshot(document),
    )
    return document


def _ranges_overlap(
    left_start: datetime.date,
    left_end: datetime.date | None,
    right_start: datetime.date,
    right_end: datetime.date | None,
) -> bool:
    infinity = datetime.date.max
    return left_start <= (right_end or infinity) and right_start <= (left_end or infinity)


@transaction.atomic
def create_contract(
    *, employee: Employee, actor: User, fixed_allowances: list[dict[str, str]], **values: Any
) -> EmployeeContract:
    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if locked_employee.status in {EmployeeStatus.TERMINATED, EmployeeStatus.ARCHIVED}:
        raise ValidationError(_("A terminated or archived employee cannot receive a contract."))
    version = (
        EmployeeContract.objects.filter(employee=locked_employee).aggregate(value=Max("version"))[
            "value"
        ]
        or 0
    ) + 1
    contract = EmployeeContract(
        organization=locked_employee.organization,
        employee=locked_employee,
        version=version,
        fixed_allowances=fixed_allowances,
        created_by=actor,
        **values,
    )
    _validate(contract)
    contract.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=contract,
        branch=contract.branch,
        new_state=snapshot(contract),
    )
    return contract


@transaction.atomic
def update_contract(
    *, contract: EmployeeContract, fixed_allowances: list[dict[str, str]], **values: Any
) -> EmployeeContract:
    locked = EmployeeContract.objects.select_for_update().get(pk=contract.pk)
    if locked.status != ContractStatus.DRAFT:
        raise ValidationError(_("An approved contract is immutable."), code="contract_immutable")
    previous = snapshot(locked)
    for field, value in values.items():
        setattr(locked, field, value)
    locked.fixed_allowances = fixed_allowances
    _validate(locked)
    locked.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def approve_contract(*, contract: EmployeeContract, actor: User) -> EmployeeContract:
    locked = (
        EmployeeContract.objects.select_for_update()
        .select_related("employee", "payroll_policy", "branch")
        .get(pk=contract.pk)
    )
    if locked.status != ContractStatus.DRAFT:
        raise ValidationError(_("Only a draft contract can be approved."))
    if locked.created_by_id == actor.pk:
        raise ValidationError(_("The contract creator cannot approve it."), code="maker_checker")
    if not locked.payroll_policy.is_active:
        raise ValidationError(_("The payroll policy is inactive."))
    candidates = EmployeeContract.objects.select_for_update().filter(
        employee=locked.employee, status=ContractStatus.APPROVED
    )
    for current in candidates:
        if not _ranges_overlap(
            locked.start_date, locked.end_date, current.start_date, current.end_date
        ):
            continue
        if current.start_date < locked.start_date:
            current_previous = snapshot(current)
            current.status = ContractStatus.SUPERSEDED
            current.end_date = locked.start_date - datetime.timedelta(days=1)
            current.save(update_fields=["status", "end_date", "updated_at"])
            record_audit_event(
                action=AuditAction.DEACTIVATED,
                target=current,
                branch=current.branch,
                previous_state=current_previous,
                new_state=snapshot(current),
                reason=str(_("Superseded by a later approved contract version.")),
            )
        else:
            raise ValidationError(
                _("Approved contract periods may not overlap."), code="contract_period_overlap"
            )
    previous = snapshot(locked)
    locked.status = ContractStatus.APPROVED
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    _validate(locked)
    locked.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


def parse_fixed_allowances(raw: str) -> list[dict[str, str]]:
    """Parse one `name:amount` per line without ever introducing float."""
    rows: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            name, amount_raw = line.split(":", 1)
            amount = Decimal(amount_raw.strip())
        except (ValueError, ArithmeticError) as error:
            raise ValidationError(_("Allowances must use name:amount, one per line.")) from error
        if not name.strip() or amount <= 0:
            raise ValidationError(_("Allowance names and amounts must be positive."))
        rows.append({"name": name.strip(), "amount": format(amount, "f")})
    return rows


def allowances_as_text(contract: EmployeeContract) -> str:
    return "\n".join(
        f"{row.get('name', '')}:{row.get('amount', '')}" for row in contract.fixed_allowances
    )


def default_policy_values(*, organization: Organization, actor: User) -> PayrollPolicy:
    """Idempotent configurable baseline used by demo/bootstrap, never by payroll calculation."""
    policy, _ = PayrollPolicy.objects.get_or_create(
        organization=organization,
        code="STANDARD",
        version=1,
        defaults={
            "name_ar": "سياسة الرواتب القياسية",
            "name_en": "Standard payroll policy",
            "effective_from": datetime.date(2020, 1, 1),
            "proration_basis": "SCHEDULED_WORKDAY",
            "created_by": actor,
        },
    )
    return policy
