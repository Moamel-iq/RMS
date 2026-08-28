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
    AbsenceClassification,
    AbsenceRecord,
    AdvanceStatus,
    AttendanceApprovalStatus,
    AttendanceDayApproval,
    AttendanceEvent,
    AttendanceEventSource,
    ContractStatus,
    Employee,
    EmployeeAdvance,
    EmployeeContract,
    EmployeeDeduction,
    EmployeeDocument,
    EmployeeStatus,
    LeaveRequest,
    LeaveType,
    OvertimeRequest,
    PayrollPolicy,
    RequestStatus,
    Shift,
    ShiftAssignment,
)
from apps.hr.permissions import (
    APPROVE_ADVANCE,
    APPROVE_ATTENDANCE,
    APPROVE_DEDUCTION,
    APPROVE_LEAVE,
    APPROVE_OVERTIME,
    ASSIGN_SHIFT,
    CLASSIFY_ABSENCE,
    CORRECT_ATTENDANCE,
    MANAGE_ADVANCE,
    MANAGE_DEDUCTION,
    MANAGE_OVERTIME,
    MANAGE_SHIFT,
    RECORD_ATTENDANCE,
    REQUEST_LEAVE,
)
from apps.organizations.authorization import require_organization_permission
from apps.organizations.business_dates import business_date_for
from apps.organizations.models import Branch, Organization
from apps.users.models import User


def _validate(instance: Any) -> None:
    instance.full_clean()


@transaction.atomic
def create_employee(
    *,
    organization: Organization,
    code: str,
    name: str,
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
        name=name.strip(),
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
    name: str,
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
        "name": name.strip(),
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
            "name": "سياسة الرواتب القياسية",
            "effective_from": datetime.date(2020, 1, 1),
            "proration_basis": "SCHEDULED_WORKDAY",
            "created_by": actor,
        },
    )
    return policy


def _ranges_overlap_dates(
    left_start: datetime.date,
    left_end: datetime.date | None,
    right_start: datetime.date,
    right_end: datetime.date | None,
) -> bool:
    return left_start <= (right_end or datetime.date.max) and right_start <= (
        left_end or datetime.date.max
    )


@transaction.atomic
def create_shift(
    *,
    branch: Branch,
    code: str,
    actor: User,
    name: str,
    start_time: datetime.time,
    end_time: datetime.time,
    crosses_midnight: bool,
    scheduled_minutes: int,
    break_minutes: int,
    grace_minutes: int,
    late_threshold_minutes: int,
    early_departure_threshold_minutes: int,
    effective_from: datetime.date,
    effective_to: datetime.date | None,
    is_active: bool,
    notes: str,
) -> Shift:
    require_organization_permission(actor, MANAGE_SHIFT, branch.organization)
    normalized_code = code.strip().upper()
    existing = Shift.objects.select_for_update().filter(branch=branch, code=normalized_code)
    for current in existing:
        if _ranges_overlap_dates(
            effective_from, effective_to, current.effective_from, current.effective_to
        ):
            raise ValidationError(
                _("Shift versions with the same code may not overlap."),
                code="shift_period_overlap",
            )
    version = (existing.aggregate(value=Max("version"))["value"] or 0) + 1
    shift = Shift(
        organization=branch.organization,
        branch=branch,
        code=normalized_code,
        version=version,
        name=name.strip(),
        start_time=start_time,
        end_time=end_time,
        crosses_midnight=crosses_midnight,
        scheduled_minutes=scheduled_minutes,
        break_minutes=break_minutes,
        grace_minutes=grace_minutes,
        late_threshold_minutes=late_threshold_minutes,
        early_departure_threshold_minutes=early_departure_threshold_minutes,
        effective_from=effective_from,
        effective_to=effective_to,
        is_active=is_active,
        notes=notes.strip(),
        created_by=actor,
    )
    _validate(shift)
    shift.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=shift,
        branch=shift.branch,
        new_state=snapshot(shift),
    )
    return shift


@transaction.atomic
def update_shift(*, shift: Shift, actor: User, **values: Any) -> Shift:
    locked = Shift.objects.select_for_update().get(pk=shift.pk)
    require_organization_permission(actor, MANAGE_SHIFT, locked.organization)
    if locked.assignments.exists() or locked.attendance_events.exists():
        raise ValidationError(
            _("A used shift version is immutable; create a new effective version."),
            code="shift_version_immutable",
        )
    previous = snapshot(locked)
    for field, value in values.items():
        setattr(locked, field, value.strip() if isinstance(value, str) else value)
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
def assign_shift(
    *,
    employee: Employee,
    shift: Shift,
    effective_from: datetime.date,
    effective_to: datetime.date | None,
    rotation_code: str,
    notes: str,
    actor: User,
) -> ShiftAssignment:
    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    locked_shift = Shift.objects.select_for_update().get(pk=shift.pk)
    require_organization_permission(actor, ASSIGN_SHIFT, locked_employee.organization)
    if locked_employee.organization_id != locked_shift.organization_id:
        raise ValidationError(_("The shift belongs to another organization."))
    if locked_employee.branch_id != locked_shift.branch_id:
        raise ValidationError(_("The shift belongs to another branch."))
    if effective_from < locked_shift.effective_from or (
        locked_shift.effective_to is not None
        and (effective_to is None or effective_to > locked_shift.effective_to)
    ):
        raise ValidationError(_("Assignment dates must remain inside the shift version dates."))
    current_assignments = ShiftAssignment.objects.select_for_update().filter(
        employee=locked_employee
    )
    for current in current_assignments:
        if _ranges_overlap_dates(
            effective_from, effective_to, current.effective_from, current.effective_to
        ):
            raise ValidationError(
                _("Employee shift assignments may not overlap."),
                code="shift_assignment_overlap",
            )
    assignment = ShiftAssignment(
        organization=locked_employee.organization,
        employee=locked_employee,
        shift=locked_shift,
        effective_from=effective_from,
        effective_to=effective_to,
        rotation_code=rotation_code.strip(),
        notes=notes.strip(),
        created_by=actor,
    )
    _validate(assignment)
    assignment.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=assignment,
        branch=locked_shift.branch,
        new_state=snapshot(assignment),
    )
    return assignment


@transaction.atomic
def record_attendance_event(
    *,
    employee: Employee,
    branch: Branch,
    business_date: datetime.date,
    occurred_at: datetime.datetime,
    event_type: str,
    source: str,
    device_reference: str,
    notes: str,
    actor: User,
) -> AttendanceEvent:
    from apps.hr.attendance import assignment_for

    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    require_organization_permission(actor, RECORD_ATTENDANCE, locked_employee.organization)
    if locked_employee.branch_id != branch.pk:
        raise ValidationError(_("The employee belongs to another branch."))
    if timezone.is_naive(occurred_at):
        raise ValidationError(_("Attendance timestamps must include a timezone."))
    if abs((business_date - business_date_for(branch, occurred_at)).days) > 1:
        raise ValidationError(_("Attendance business date is too far from the event timestamp."))
    assignment = assignment_for(locked_employee, business_date)
    if device_reference.strip():
        existing = (
            AttendanceEvent.objects.select_for_update()
            .filter(
                organization=locked_employee.organization,
                source=source,
                device_reference=device_reference.strip(),
            )
            .first()
        )
        if existing is not None:
            if (
                existing.employee_id == locked_employee.pk
                and existing.occurred_at == occurred_at
                and existing.event_type == event_type
            ):
                return existing
            raise ValidationError(
                _("This attendance reference was already used with different values."),
                code="attendance_reference_conflict",
            )
    event = AttendanceEvent(
        organization=locked_employee.organization,
        employee=locked_employee,
        branch=branch,
        shift_assignment=assignment,
        scheduled_shift=assignment.shift if assignment is not None else None,
        business_date=business_date,
        occurred_at=occurred_at,
        event_type=event_type,
        source=source,
        device_reference=device_reference.strip(),
        notes=notes.strip(),
        created_by=actor,
    )
    _validate(event)
    event.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=event,
        branch=event.branch,
        new_state=snapshot(event),
    )
    return event


@transaction.atomic
def correct_attendance_event(
    *,
    event: AttendanceEvent,
    business_date: datetime.date,
    occurred_at: datetime.datetime,
    event_type: str,
    reason: str,
    notes: str,
    actor: User,
) -> AttendanceEvent:
    if not reason.strip():
        raise ValidationError(_("Attendance correction requires a reason."))
    locked = AttendanceEvent.objects.select_for_update().get(pk=event.pk)
    require_organization_permission(actor, CORRECT_ATTENDANCE, locked.organization)
    if locked.corrections.exists():
        raise ValidationError(
            _("This event has already been superseded."), code="attendance_event_stale"
        )
    if AttendanceDayApproval.objects.filter(
        employee=locked.employee,
        business_date=locked.business_date,
        status=AttendanceApprovalStatus.APPROVED,
    ).exists():
        raise ValidationError(_("Reopen the approved attendance day before correction."))
    if abs((business_date - business_date_for(locked.branch, occurred_at)).days) > 1:
        raise ValidationError(
            _("Attendance business date is too far from the corrected timestamp.")
        )
    replacement = AttendanceEvent(
        organization=locked.organization,
        employee=locked.employee,
        branch=locked.branch,
        shift_assignment=locked.shift_assignment,
        scheduled_shift=locked.scheduled_shift,
        business_date=business_date,
        occurred_at=occurred_at,
        event_type=event_type,
        source=AttendanceEventSource.MANUAL,
        device_reference="",
        notes=notes.strip(),
        created_by=actor,
        supersedes=locked,
        correction_reason=reason.strip(),
    )
    _validate(replacement)
    replacement.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=replacement,
        branch=replacement.branch,
        previous_state=snapshot(locked),
        new_state=snapshot(replacement),
        reason=reason.strip(),
    )
    return replacement


@transaction.atomic
def approve_attendance_day(
    *, employee: Employee, business_date: datetime.date, actor: User
) -> AttendanceDayApproval:
    from apps.hr.attendance import calculate_attendance_day

    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    require_organization_permission(actor, APPROVE_ATTENDANCE, locked_employee.organization)
    approval, _ = AttendanceDayApproval.objects.select_for_update().get_or_create(
        employee=locked_employee,
        business_date=business_date,
        defaults={
            "organization": locked_employee.organization,
            "branch": locked_employee.branch,
            "created_by": actor,
        },
    )
    if approval.status == AttendanceApprovalStatus.APPROVED:
        return approval
    previous = snapshot(approval)
    result = calculate_attendance_day(employee=locked_employee, business_date=business_date)
    approval.status = AttendanceApprovalStatus.APPROVED
    approval.result_snapshot = result.snapshot()
    approval.approved_by = actor
    approval.approved_at = timezone.now()
    approval.reason = ""
    _validate(approval)
    approval.save()
    record_audit_event(
        action=AuditAction.APPROVED,
        target=approval,
        branch=approval.branch,
        previous_state=previous,
        new_state=snapshot(approval),
    )
    return approval


@transaction.atomic
def reopen_attendance_day(
    *, employee: Employee, business_date: datetime.date, reason: str, actor: User
) -> AttendanceDayApproval:
    if not reason.strip():
        raise ValidationError(_("Reopening attendance requires a reason."))
    approval = AttendanceDayApproval.objects.select_for_update().get(
        employee=employee, business_date=business_date
    )
    require_organization_permission(actor, APPROVE_ATTENDANCE, approval.organization)
    if approval.status != AttendanceApprovalStatus.APPROVED:
        raise ValidationError(_("Only an approved attendance day can be reopened."))
    previous = snapshot(approval)
    approval.status = AttendanceApprovalStatus.REOPENED
    approval.reason = reason.strip()
    approval.approved_by = None
    approval.approved_at = None
    approval.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=approval,
        branch=approval.branch,
        previous_state=previous,
        new_state=snapshot(approval),
        reason=reason.strip(),
    )
    return approval


@transaction.atomic
def create_leave_request(
    *,
    employee: Employee,
    leave_type: LeaveType,
    start_at: datetime.datetime,
    end_at: datetime.datetime,
    reason: str,
    evidence_reference: str,
    evidence_file: Any,
    actor: User,
) -> LeaveRequest:
    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    require_organization_permission(actor, REQUEST_LEAVE, locked_employee.organization)
    if end_at <= start_at:
        raise ValidationError(_("Leave end must follow its start."))
    requested_minutes = int((end_at - start_at).total_seconds() // 60)
    request = LeaveRequest(
        organization=locked_employee.organization,
        employee=locked_employee,
        leave_type=leave_type,
        start_at=start_at,
        end_at=end_at,
        requested_minutes=requested_minutes,
        paid_treatment=leave_type.paid_treatment,
        reason=reason.strip(),
        evidence_reference=evidence_reference.strip(),
        evidence_file=evidence_file,
        requested_by=actor,
    )
    _validate(request)
    request.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=request,
        branch=locked_employee.branch,
        new_state=snapshot(request),
    )
    return request


@transaction.atomic
def create_leave_type(*, organization: Organization, actor: User, **values: Any) -> LeaveType:
    require_organization_permission(actor, REQUEST_LEAVE, organization)
    leave_type = LeaveType(organization=organization, created_by=actor, **values)
    leave_type.code = leave_type.code.strip().upper()
    _validate(leave_type)
    leave_type.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=leave_type,
        new_state=snapshot(leave_type),
    )
    return leave_type


@transaction.atomic
def submit_leave_request(*, request: LeaveRequest, actor: User) -> LeaveRequest:
    locked = LeaveRequest.objects.select_for_update().get(pk=request.pk)
    require_organization_permission(actor, REQUEST_LEAVE, locked.organization)
    if locked.status != RequestStatus.DRAFT:
        raise ValidationError(_("Only a draft leave request can be submitted."))
    previous = snapshot(locked)
    locked.status = RequestStatus.SUBMITTED
    locked.save(update_fields=["status", "updated_at"])
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def decide_leave_request(
    *, request: LeaveRequest, approve: bool, reason: str, actor: User
) -> LeaveRequest:
    locked = LeaveRequest.objects.select_for_update().get(pk=request.pk)
    require_organization_permission(actor, APPROVE_LEAVE, locked.organization)
    if locked.status != RequestStatus.SUBMITTED:
        raise ValidationError(_("Only a submitted leave request can be decided."))
    if locked.requested_by_id == actor.pk:
        raise ValidationError(_("The request creator cannot approve it."), code="maker_checker")
    previous = snapshot(locked)
    if approve:
        overlaps = LeaveRequest.objects.select_for_update().filter(
            employee=locked.employee,
            status=RequestStatus.APPROVED,
            start_at__lt=locked.end_at,
            end_at__gt=locked.start_at,
        )
        if overlaps.exists():
            raise ValidationError(_("Approved leave periods may not overlap."))
        locked.status = RequestStatus.APPROVED
        locked.approved_by = actor
        locked.approved_at = timezone.now()
        locked.rejection_reason = ""
        action = AuditAction.APPROVED
    else:
        if not reason.strip():
            raise ValidationError(_("Leave rejection requires a reason."))
        locked.status = RequestStatus.REJECTED
        locked.rejection_reason = reason.strip()
        action = AuditAction.REJECTED
    _validate(locked)
    locked.save()
    record_audit_event(
        action=action,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


@transaction.atomic
def cancel_leave_request(*, request: LeaveRequest, reason: str, actor: User) -> LeaveRequest:
    if not reason.strip():
        raise ValidationError(_("Leave cancellation requires a reason."))
    locked = LeaveRequest.objects.select_for_update().get(pk=request.pk)
    require_organization_permission(actor, REQUEST_LEAVE, locked.organization)
    if locked.status not in {
        RequestStatus.DRAFT,
        RequestStatus.SUBMITTED,
        RequestStatus.APPROVED,
    }:
        raise ValidationError(_("This leave request cannot be cancelled."))
    previous = snapshot(locked)
    locked.status = RequestStatus.CANCELLED
    locked.save(update_fields=["status", "updated_at"])
    record_audit_event(
        action=AuditAction.CANCELLED,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


@transaction.atomic
def classify_absence(
    *,
    employee: Employee,
    business_date: datetime.date,
    classification: str,
    reason: str,
    actor: User,
) -> AbsenceRecord:
    from apps.hr.attendance import calculate_attendance_day

    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    require_organization_permission(actor, CLASSIFY_ABSENCE, locked_employee.organization)
    result = calculate_attendance_day(employee=locked_employee, business_date=business_date)
    if not result.absence_candidate:
        raise ValidationError(_("Only an attendance absence candidate can be classified."))
    if classification not in AbsenceClassification.values:
        raise ValidationError(_("Choose a valid absence classification."))
    if not reason.strip():
        raise ValidationError(_("Absence classification requires a reason."))
    day_start = timezone.make_aware(datetime.datetime.combine(business_date, datetime.time.min))
    day_end = day_start + datetime.timedelta(days=1)
    leave = LeaveRequest.objects.filter(
        employee=locked_employee,
        status=RequestStatus.APPROVED,
        start_at__lt=day_end,
        end_at__gt=day_start,
    ).first()
    if leave is not None:
        classification = (
            AbsenceClassification.APPROVED_UNPAID_LEAVE
            if leave.paid_treatment == "UNPAID"
            else AbsenceClassification.APPROVED_PAID_LEAVE
        )
    record = (
        AbsenceRecord.objects.select_for_update()
        .filter(
            employee=locked_employee,
            business_date=business_date,
        )
        .first()
    )
    created = record is None
    if record is None:
        record = AbsenceRecord(
            organization=locked_employee.organization,
            employee=locked_employee,
            branch=locked_employee.branch,
            business_date=business_date,
            classification=classification,
            leave_request=leave,
            reason=reason.strip(),
            created_by=actor,
        )
        previous = None
    else:
        previous = snapshot(record)
        record.classification = classification
        record.leave_request = leave
        record.reason = reason.strip()
        record.created_by = actor
    _validate(record)
    record.save()
    record_audit_event(
        action=AuditAction.CREATED if created else AuditAction.UPDATED,
        target=record,
        branch=record.branch,
        previous_state=previous,
        new_state=snapshot(record),
        reason=reason.strip(),
    )
    return record


def _policy_for(employee: Employee, business_date: datetime.date) -> PayrollPolicy:
    contract = (
        EmployeeContract.objects.select_related("payroll_policy")
        .filter(
            employee=employee,
            status__in=[ContractStatus.APPROVED, ContractStatus.SUPERSEDED, ContractStatus.CLOSED],
            start_date__lte=business_date,
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=business_date))
        .order_by("-start_date", "-version")
        .first()
    )
    if contract is None:
        raise ValidationError(_("No approved contract covers this business date."))
    return contract.payroll_policy


@transaction.atomic
def create_overtime_request(
    *,
    employee: Employee,
    business_date: datetime.date,
    requested_minutes: int,
    source: str,
    classification: str,
    reason: str,
    evidence_reference: str,
    evidence_file: Any,
    actor: User,
) -> OvertimeRequest:
    from apps.hr.attendance import assignment_for

    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    require_organization_permission(actor, MANAGE_OVERTIME, locked_employee.organization)
    assignment = assignment_for(locked_employee, business_date)
    if assignment is None:
        raise ValidationError(_("No shift assignment covers this business date."))
    policy = _policy_for(locked_employee, business_date)
    if policy.max_overtime_minutes and requested_minutes > policy.max_overtime_minutes:
        raise ValidationError(_("Requested overtime exceeds the configured policy maximum."))
    overtime = OvertimeRequest(
        organization=locked_employee.organization,
        employee=locked_employee,
        business_date=business_date,
        shift=assignment.shift,
        requested_minutes=requested_minutes,
        source=source,
        multiplier=policy.overtime_multiplier,
        classification=classification.strip(),
        reason=reason.strip(),
        evidence_reference=evidence_reference.strip(),
        evidence_file=evidence_file,
        created_by=actor,
    )
    _validate(overtime)
    overtime.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=overtime,
        branch=locked_employee.branch,
        new_state=snapshot(overtime),
    )
    return overtime


@transaction.atomic
def submit_overtime_request(*, overtime: OvertimeRequest, actor: User) -> OvertimeRequest:
    locked = OvertimeRequest.objects.select_for_update().get(pk=overtime.pk)
    require_organization_permission(actor, MANAGE_OVERTIME, locked.organization)
    if locked.status != RequestStatus.DRAFT:
        raise ValidationError(_("Only draft overtime can be submitted."))
    previous = snapshot(locked)
    locked.status = RequestStatus.SUBMITTED
    locked.save(update_fields=["status", "updated_at"])
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def approve_overtime_request(
    *, overtime: OvertimeRequest, approved_minutes: int, actor: User
) -> OvertimeRequest:
    locked = OvertimeRequest.objects.select_for_update().get(pk=overtime.pk)
    require_organization_permission(actor, APPROVE_OVERTIME, locked.organization)
    if locked.status != RequestStatus.SUBMITTED:
        raise ValidationError(_("Only submitted overtime can be approved."))
    if locked.created_by_id == actor.pk:
        raise ValidationError(_("The request creator cannot approve it."), code="maker_checker")
    policy = _policy_for(locked.employee, locked.business_date)
    if approved_minutes <= 0 or approved_minutes > locked.requested_minutes:
        raise ValidationError(_("Approved overtime must be positive and within the request."))
    if policy.max_overtime_minutes and approved_minutes > policy.max_overtime_minutes:
        raise ValidationError(_("Approved overtime exceeds the configured policy maximum."))
    previous = snapshot(locked)
    locked.status = RequestStatus.APPROVED
    locked.approved_minutes = approved_minutes
    locked.multiplier = policy.overtime_multiplier
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    _validate(locked)
    locked.save()
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def create_deduction(
    *,
    employee: Employee,
    deduction_type: str,
    original_amount: Decimal,
    effective_period: datetime.date,
    recovery_mode: str,
    instalment_count: int,
    evidence_reference: str,
    evidence_file: Any,
    reason: str,
    actor: User,
) -> EmployeeDeduction:
    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    require_organization_permission(actor, MANAGE_DEDUCTION, locked_employee.organization)
    if not reason.strip() or not evidence_reference.strip():
        raise ValidationError(_("A deduction requires both evidence and a reason."))
    deduction = EmployeeDeduction(
        organization=locked_employee.organization,
        employee=locked_employee,
        deduction_type=deduction_type,
        original_amount=original_amount,
        effective_period=effective_period,
        recovery_mode=recovery_mode,
        instalment_count=instalment_count,
        evidence_reference=evidence_reference.strip(),
        evidence_file=evidence_file,
        reason=reason.strip(),
        created_by=actor,
    )
    _validate(deduction)
    deduction.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=deduction,
        branch=locked_employee.branch,
        new_state=snapshot(deduction),
    )
    return deduction


@transaction.atomic
def submit_deduction(*, deduction: EmployeeDeduction, actor: User) -> EmployeeDeduction:
    locked = EmployeeDeduction.objects.select_for_update().get(pk=deduction.pk)
    require_organization_permission(actor, MANAGE_DEDUCTION, locked.organization)
    if locked.status != RequestStatus.DRAFT:
        raise ValidationError(_("Only a draft deduction can be submitted."))
    previous = snapshot(locked)
    locked.status = RequestStatus.SUBMITTED
    locked.save(update_fields=["status", "updated_at"])
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def approve_deduction(
    *, deduction: EmployeeDeduction, approved_amount: Decimal, actor: User
) -> EmployeeDeduction:
    locked = EmployeeDeduction.objects.select_for_update().get(pk=deduction.pk)
    require_organization_permission(actor, APPROVE_DEDUCTION, locked.organization)
    if locked.status != RequestStatus.SUBMITTED:
        raise ValidationError(_("Only a submitted deduction can be approved."))
    if locked.created_by_id == actor.pk:
        raise ValidationError(_("The deduction creator cannot approve it."), code="maker_checker")
    if approved_amount <= 0 or approved_amount > locked.original_amount:
        raise ValidationError(_("Approved amount must be positive and within the original amount."))
    previous = snapshot(locked)
    locked.status = RequestStatus.APPROVED
    locked.approved_amount = approved_amount
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    _validate(locked)
    locked.save()
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def create_advance(*, employee: Employee, actor: User, **values: Any) -> EmployeeAdvance:
    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
    require_organization_permission(actor, MANAGE_ADVANCE, locked_employee.organization)
    if (
        not str(values.get("reason", "")).strip()
        or not str(values.get("evidence_reference", "")).strip()
    ):
        raise ValidationError(_("An advance requires both evidence and a reason."))
    advance = EmployeeAdvance(
        organization=locked_employee.organization,
        employee=locked_employee,
        created_by=actor,
        **values,
    )
    _validate(advance)
    advance.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=advance,
        branch=locked_employee.branch,
        new_state=snapshot(advance),
    )
    return advance


@transaction.atomic
def submit_advance(*, advance: EmployeeAdvance, actor: User) -> EmployeeAdvance:
    locked = EmployeeAdvance.objects.select_for_update().get(pk=advance.pk)
    require_organization_permission(actor, MANAGE_ADVANCE, locked.organization)
    if locked.status != AdvanceStatus.DRAFT:
        raise ValidationError(_("Only a draft advance can be submitted."))
    previous = snapshot(locked)
    locked.status = AdvanceStatus.SUBMITTED
    locked.save(update_fields=["status", "updated_at"])
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def approve_advance(*, advance: EmployeeAdvance, actor: User) -> EmployeeAdvance:
    locked = EmployeeAdvance.objects.select_for_update().get(pk=advance.pk)
    require_organization_permission(actor, APPROVE_ADVANCE, locked.organization)
    if locked.status != AdvanceStatus.SUBMITTED:
        raise ValidationError(_("Only a submitted advance can be approved."))
    if locked.created_by_id == actor.pk:
        raise ValidationError(_("The advance creator cannot approve it."), code="maker_checker")
    previous = snapshot(locked)
    locked.status = AdvanceStatus.APPROVED
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    _validate(locked)
    locked.save()
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.employee.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked
