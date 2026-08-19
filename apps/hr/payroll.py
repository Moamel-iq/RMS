"""Deterministic payroll calculation and approval over frozen HR evidence."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.hr.attendance import assignment_for, calculate_attendance_day
from apps.hr.models import (
    AdvanceStatus,
    AttendanceApprovalStatus,
    AttendanceDayApproval,
    ContractStatus,
    DeductionType,
    Employee,
    EmployeeAdvance,
    EmployeeContract,
    EmployeeDeduction,
    OvertimeRequest,
    PayrollComponentKind,
    PayrollComponentLine,
    PayrollEmployeeLine,
    PayrollPolicy,
    PayrollRun,
    PayrollRunStatus,
    ProrationBasis,
    RecoveryMode,
    RequestStatus,
    WageBasis,
)
from apps.hr.permissions import APPROVE_PAYROLL, CALCULATE_PAYROLL, REVIEW_PAYROLL
from apps.organizations.authorization import require_organization_permission
from apps.organizations.models import Branch, Organization
from apps.users.models import User

ZERO = Decimal("0.000")
ONE_HUNDRED = Decimal("100.000")


def _policy_snapshot(policy: PayrollPolicy) -> dict[str, Any]:
    return {
        "public_id": str(policy.public_id),
        "code": policy.code,
        "version": policy.version,
        "proration_basis": policy.proration_basis,
        "money_rounding": str(policy.money_rounding),
        "hour_rounding": str(policy.hour_rounding),
        "overtime_multiplier": str(policy.overtime_multiplier),
        "max_overtime_minutes": policy.max_overtime_minutes,
        "deduction_cap_percentage": str(policy.deduction_cap_percentage),
        "deduct_lateness": policy.deduct_lateness,
        "deduct_early_departure": policy.deduct_early_departure,
        "absence_multiplier": str(policy.absence_multiplier),
        "unpaid_leave_multiplier": str(policy.unpaid_leave_multiplier),
    }


def _date_range(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    return [start + datetime.timedelta(days=offset) for offset in range((end - start).days + 1)]


def _contract_for(employee: Employee, period_end: datetime.date) -> EmployeeContract | None:
    return (
        EmployeeContract.objects.select_related("payroll_policy", "branch")
        .filter(
            employee=employee,
            status__in=[ContractStatus.APPROVED, ContractStatus.SUPERSEDED, ContractStatus.CLOSED],
            start_date__lte=period_end,
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=period_end))
        .order_by("-start_date", "-version")
        .first()
    )


def _warning(code: str, message: str, *, severity: str = "BLOCKER") -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def _component(
    *,
    kind: str,
    code: str,
    label: str,
    amount: Decimal,
    deduction: bool = False,
    quantity: Decimal = ZERO,
    rate: Decimal = ZERO,
    multiplier: Decimal = Decimal("1.000"),
    source_type: str = "",
    source_id: str = "",
    source_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "code": code,
        "label_ar": label,
        "amount": quantize_money(amount),
        "is_deduction": deduction,
        "quantity": quantity,
        "rate": rate,
        "multiplier": multiplier,
        "source_type": source_type,
        "source_id": source_id,
        "source_snapshot": source_snapshot or {},
    }


@transaction.atomic
def create_payroll_run(
    *,
    branch: Branch,
    period_start: datetime.date,
    period_end: datetime.date,
    accounting_date: datetime.date,
    policy: PayrollPolicy,
    notes: str,
    actor: User,
) -> PayrollRun:
    organization = Organization.objects.select_for_update().get(pk=branch.organization_id)
    require_organization_permission(actor, CALCULATE_PAYROLL, organization)
    if period_end < period_start:
        raise ValidationError(_("Payroll period end must follow its start."))
    if policy.organization_id != organization.pk:
        raise ValidationError(_("The payroll policy belongs to another organization."))
    if policy.effective_from > period_end or (
        policy.effective_to is not None and policy.effective_to < period_start
    ):
        raise ValidationError(_("The payroll policy does not cover this period."))
    if (
        PayrollRun.objects.filter(
            organization=organization,
            branch=branch,
            period_start=period_start,
            period_end=period_end,
        )
        .exclude(status=PayrollRunStatus.REVERSED)
        .exists()
    ):
        raise ValidationError(_("A live payroll run already exists for this branch and period."))
    sequence = (
        PayrollRun.objects.filter(
            organization=organization, period_start__year=period_start.year
        ).count()
        + 1
    )
    run = PayrollRun(
        organization=organization,
        branch=branch,
        run_number=f"PAY-{period_end:%Y%m}-{branch.code}-{sequence:03d}",
        period_start=period_start,
        period_end=period_end,
        accounting_date=accounting_date,
        policy=policy,
        policy_snapshot=_policy_snapshot(policy),
        created_by=actor,
        notes=notes.strip(),
    )
    run.full_clean()
    run.save()
    record_audit_event(
        action=AuditAction.CREATED, target=run, branch=branch, new_state=snapshot(run)
    )
    return run


def _employee_calculation(
    *, employee: Employee, contract: EmployeeContract, run: PayrollRun
) -> tuple[PayrollEmployeeLine, list[dict[str, Any]]]:
    policy = run.policy
    warnings: list[dict[str, str]] = []
    scheduled_days = ZERO
    scheduled_minutes = 0
    worked_minutes = 0
    paid_leave_minutes = 0
    unpaid_leave_minutes = 0
    absence_minutes = 0
    lateness_minutes = 0
    early_minutes = 0
    attendance_sources: list[dict[str, Any]] = []

    for business_date in _date_range(run.period_start, run.period_end):
        assignment = assignment_for(employee, business_date)
        if assignment is None:
            continue
        day_minutes = assignment.shift.scheduled_minutes
        scheduled_days += Decimal("1.000")
        scheduled_minutes += day_minutes
        result = calculate_attendance_day(employee=employee, business_date=business_date)
        approval = AttendanceDayApproval.objects.filter(
            employee=employee,
            business_date=business_date,
            status=AttendanceApprovalStatus.APPROVED,
        ).first()
        if approval is None:
            warnings.append(
                _warning(
                    "ATTENDANCE_UNAPPROVED",
                    str(_("Attendance is not approved for %(date)s.") % {"date": business_date}),
                )
            )
        if result.status == "APPROVED_PAID_LEAVE":
            paid_leave_minutes += day_minutes
        elif result.status == "APPROVED_UNPAID_LEAVE":
            unpaid_leave_minutes += day_minutes
        elif result.status == "ABSENCE_CANDIDATE":
            absence_minutes += day_minutes
            warnings.append(
                _warning(
                    "ABSENCE_UNCLASSIFIED",
                    str(_("Absence is not classified for %(date)s.") % {"date": business_date}),
                )
            )
        else:
            worked_minutes += result.worked_minutes
            lateness_minutes += result.lateness_minutes
            early_minutes += result.early_departure_minutes
            if result.missing_punch:
                warnings.append(
                    _warning(
                        "MISSING_PUNCH",
                        str(
                            _("Attendance has a missing punch for %(date)s.")
                            % {"date": business_date}
                        ),
                    )
                )
        attendance_sources.append(result.snapshot())

    if scheduled_minutes <= 0:
        warnings.append(_warning("NO_SCHEDULE", str(_("No scheduled work exists in this period."))))

    period_days = Decimal(str((run.period_end - run.period_start).days + 1))
    covered_start = max(run.period_start, contract.start_date)
    covered_end = min(run.period_end, contract.end_date or run.period_end)
    covered_days = Decimal(str(max(0, (covered_end - covered_start).days + 1)))
    if contract.start_date > run.period_start or (
        contract.end_date is not None and contract.end_date < run.period_end
    ):
        warnings.append(
            _warning(
                "PARTIAL_CONTRACT_PERIOD",
                str(_("The approved contract covers only part of this payroll period.")),
                severity="WARNING",
            )
        )

    if contract.wage_basis == WageBasis.MONTHLY:
        if policy.proration_basis == ProrationBasis.CALENDAR_DAY:
            basic_pay = contract.basic_salary * covered_days / period_days
        else:
            basic_pay = contract.basic_salary
    elif contract.wage_basis == WageBasis.DAILY:
        basic_pay = contract.basic_salary * scheduled_days
    else:
        basic_pay = contract.basic_salary * Decimal(scheduled_minutes) / Decimal("60")
    basic_pay = quantize_money(basic_pay)
    minute_rate = basic_pay / Decimal(scheduled_minutes) if scheduled_minutes else ZERO

    fixed_allowances = quantize_money(
        sum((Decimal(str(row.get("amount", "0"))) for row in contract.fixed_allowances), ZERO)
    )
    earnings = [
        _component(
            kind=PayrollComponentKind.BASIC,
            code="BASIC",
            label=str(_("الأجر الأساسي")),
            amount=basic_pay,
            quantity=scheduled_days,
            rate=contract.basic_salary,
            source_type="EmployeeContract",
            source_id=str(contract.public_id),
            source_snapshot={"version": contract.version, "wage_basis": contract.wage_basis},
        )
    ]
    for index, allowance in enumerate(contract.fixed_allowances, start=1):
        amount = quantize_money(Decimal(str(allowance.get("amount", "0"))))
        earnings.append(
            _component(
                kind=PayrollComponentKind.FIXED_ALLOWANCE,
                code=f"FIXED-{index}",
                label=str(allowance.get("name", _("بدل ثابت"))),
                amount=amount,
                source_type="EmployeeContract",
                source_id=str(contract.public_id),
            )
        )

    approved_overtime = OvertimeRequest.objects.filter(
        employee=employee,
        status=RequestStatus.APPROVED,
        business_date__range=(run.period_start, run.period_end),
        payroll_inclusion_reference="",
    ).order_by("business_date", "id")
    overtime_minutes = 0
    overtime_pay = ZERO
    for overtime in approved_overtime:
        amount = quantize_money(
            minute_rate * Decimal(overtime.approved_minutes) * overtime.multiplier
        )
        overtime_minutes += overtime.approved_minutes
        overtime_pay += amount
        earnings.append(
            _component(
                kind=PayrollComponentKind.OVERTIME,
                code=f"OT-{overtime.pk}",
                label=str(_("عمل إضافي")),
                amount=amount,
                quantity=Decimal(overtime.approved_minutes),
                rate=minute_rate,
                multiplier=overtime.multiplier,
                source_type="OvertimeRequest",
                source_id=str(overtime.public_id),
            )
        )
    if OvertimeRequest.objects.filter(
        employee=employee,
        business_date__range=(run.period_start, run.period_end),
        status__in=[RequestStatus.DRAFT, RequestStatus.SUBMITTED],
    ).exists():
        warnings.append(_warning("OVERTIME_UNAPPROVED", str(_("Unapproved overtime exists."))))

    deductions: list[dict[str, Any]] = []
    if absence_minutes:
        deductions.append(
            _component(
                kind=PayrollComponentKind.ABSENCE,
                code="ATT-ABSENCE",
                label=str(_("خصم الغياب")),
                amount=minute_rate * Decimal(absence_minutes) * policy.absence_multiplier,
                deduction=True,
                quantity=Decimal(absence_minutes),
                rate=minute_rate,
                multiplier=policy.absence_multiplier,
            )
        )
    if unpaid_leave_minutes:
        deductions.append(
            _component(
                kind=PayrollComponentKind.ABSENCE,
                code="UNPAID-LEAVE",
                label=str(_("خصم الإجازة غير المدفوعة")),
                amount=minute_rate * Decimal(unpaid_leave_minutes) * policy.unpaid_leave_multiplier,
                deduction=True,
                quantity=Decimal(unpaid_leave_minutes),
                rate=minute_rate,
                multiplier=policy.unpaid_leave_multiplier,
            )
        )
    if policy.deduct_lateness and lateness_minutes:
        deductions.append(
            _component(
                kind=PayrollComponentKind.LATENESS,
                code="ATT-LATE",
                label=str(_("خصم التأخر")),
                amount=minute_rate * Decimal(lateness_minutes),
                deduction=True,
                quantity=Decimal(lateness_minutes),
                rate=minute_rate,
            )
        )
    if policy.deduct_early_departure and early_minutes:
        deductions.append(
            _component(
                kind=PayrollComponentKind.EARLY_DEPARTURE,
                code="ATT-EARLY",
                label=str(_("خصم الانصراف المبكر")),
                amount=minute_rate * Decimal(early_minutes),
                deduction=True,
                quantity=Decimal(early_minutes),
                rate=minute_rate,
            )
        )

    for deduction in EmployeeDeduction.objects.filter(
        employee=employee,
        status=RequestStatus.APPROVED,
        effective_period__lte=run.period_end,
    ).order_by("effective_period", "id"):
        remaining = deduction.remaining_amount
        if remaining <= ZERO:
            continue
        amount = (
            remaining
            if deduction.recovery_mode == RecoveryMode.ONE_TIME
            else min(
                remaining,
                quantize_money(deduction.approved_amount / Decimal(deduction.instalment_count)),
            )
        )
        deductions.append(
            _component(
                kind=PayrollComponentKind.DEDUCTION,
                code=f"DED-{deduction.pk}",
                label=deduction.get_deduction_type_display(),
                amount=amount,
                deduction=True,
                source_type="EmployeeDeduction",
                source_id=str(deduction.public_id),
                source_snapshot={"type": deduction.deduction_type},
            )
        )
    if EmployeeDeduction.objects.filter(
        employee=employee,
        effective_period__lte=run.period_end,
        status__in=[RequestStatus.DRAFT, RequestStatus.SUBMITTED],
    ).exists():
        warnings.append(_warning("DEDUCTION_UNAPPROVED", str(_("Unapproved deductions exist."))))

    for advance in EmployeeAdvance.objects.filter(
        employee=employee,
        status__in=[
            AdvanceStatus.APPROVED,
            AdvanceStatus.PARTIALLY_DISBURSED,
            AdvanceStatus.DISBURSED,
        ],
        first_recovery_period__lte=run.period_end,
    ).order_by("first_recovery_period", "id"):
        outstanding = advance.outstanding_amount
        if outstanding <= ZERO:
            continue
        amount = (
            outstanding
            if advance.recovery_mode == RecoveryMode.ONE_TIME
            else min(outstanding, advance.instalment_amount)
        )
        deductions.append(
            _component(
                kind=PayrollComponentKind.ADVANCE_RECOVERY,
                code=f"ADV-{advance.pk}",
                label=str(_("استرداد سلفة")),
                amount=amount,
                deduction=True,
                source_type="EmployeeAdvance",
                source_id=str(advance.public_id),
            )
        )

    gross = quantize_money(sum((row["amount"] for row in earnings), ZERO))
    cap = quantize_money(gross * policy.deduction_cap_percentage / ONE_HUNDRED)
    remaining_cap = cap
    for row in deductions:
        original = row["amount"]
        row["amount"] = min(original, max(remaining_cap, ZERO))
        remaining_cap = quantize_money(max(remaining_cap - row["amount"], ZERO))
        if row["amount"] < original:
            warnings.append(
                _warning(
                    "DEDUCTION_CAP_APPLIED",
                    str(_("A deduction was deferred by the configured deduction cap.")),
                    severity="WARNING",
                )
            )
    deductions = [row for row in deductions if row["amount"] > ZERO]
    total_deductions = quantize_money(sum((row["amount"] for row in deductions), ZERO))
    net = quantize_money(gross - total_deductions)

    def deduction_sum(*kinds: str) -> Decimal:
        return quantize_money(
            sum((row["amount"] for row in deductions if row["kind"] in kinds), ZERO)
        )

    admin_types = {DeductionType.ADMINISTRATIVE, DeductionType.DAMAGE, DeductionType.CASH_SHORTAGE}
    administrative = quantize_money(
        sum(
            (
                row["amount"]
                for row in deductions
                if row["source_snapshot"].get("type") in admin_types
            ),
            ZERO,
        )
    )
    other_deductions = quantize_money(
        deduction_sum(PayrollComponentKind.DEDUCTION) - administrative
    )
    line = PayrollEmployeeLine(
        payroll_run=run,
        employee=employee,
        contract=contract,
        contract_version=contract.version,
        employee_code=employee.code,
        employee_name_ar=employee.name_ar,
        job_title=contract.job_title,
        wage_basis=contract.wage_basis,
        payment_method=contract.payment_method,
        payment_reference=employee.payment_reference,
        basic_salary_snapshot=contract.basic_salary,
        scheduled_days=scheduled_days,
        scheduled_minutes=scheduled_minutes,
        worked_minutes=worked_minutes,
        paid_leave_minutes=paid_leave_minutes,
        unpaid_leave_minutes=unpaid_leave_minutes,
        absence_minutes=absence_minutes,
        lateness_minutes=lateness_minutes,
        early_departure_minutes=early_minutes,
        overtime_minutes=overtime_minutes,
        basic_pay=basic_pay,
        fixed_allowances=fixed_allowances,
        variable_allowances=ZERO,
        overtime_pay=quantize_money(overtime_pay),
        rewards=ZERO,
        absence_deduction=deduction_sum(PayrollComponentKind.ABSENCE),
        lateness_deduction=deduction_sum(PayrollComponentKind.LATENESS),
        early_departure_deduction=deduction_sum(PayrollComponentKind.EARLY_DEPARTURE),
        administrative_deduction=administrative,
        advance_recovery=deduction_sum(PayrollComponentKind.ADVANCE_RECOVERY),
        other_deductions=other_deductions,
        gross_pay=gross,
        total_deductions=total_deductions,
        net_pay=net,
        warnings=warnings,
        source_snapshot={
            "contract_public_id": str(contract.public_id),
            "contract_version": contract.version,
            "policy": run.policy_snapshot,
            "attendance": attendance_sources,
        },
    )
    return line, earnings + deductions


@transaction.atomic
def calculate_payroll_run(*, payroll_run: PayrollRun, actor: User) -> PayrollRun:
    run = (
        PayrollRun.objects.select_for_update()
        .select_related("policy", "branch")
        .get(pk=payroll_run.pk)
    )
    require_organization_permission(actor, CALCULATE_PAYROLL, run.organization)
    if run.status not in {PayrollRunStatus.DRAFT, PayrollRunStatus.CALCULATED}:
        raise ValidationError(_("Only a draft or calculated payroll can be recalculated."))
    previous = snapshot(run)
    PayrollComponentLine.objects.filter(employee_line__payroll_run=run).delete()
    run.employee_lines.all().delete()
    employees = Employee.objects.filter(
        organization=run.organization,
        branch=run.branch,
        hire_date__lte=run.period_end,
    ).filter(Q(termination_date__isnull=True) | Q(termination_date__gte=run.period_start))
    warning_count = 0
    for employee in employees.order_by("code"):
        contract = _contract_for(employee, run.period_end)
        if contract is None:
            continue
        line, components = _employee_calculation(employee=employee, contract=contract, run=run)
        line.full_clean()
        line.save()
        PayrollComponentLine.objects.bulk_create(
            [
                PayrollComponentLine(
                    employee_line=line,
                    kind=row["kind"],
                    code=row["code"],
                    label_ar=row["label_ar"],
                    quantity=row["quantity"],
                    rate=row["rate"],
                    multiplier=row["multiplier"],
                    amount=row["amount"],
                    is_deduction=row["is_deduction"],
                    source_type=row["source_type"],
                    source_id=row["source_id"],
                    source_snapshot=row["source_snapshot"],
                )
                for row in components
            ]
        )
        warning_count += len(line.warnings)
    lines = list(run.employee_lines.all())
    if not lines:
        raise ValidationError(_("No employee with an approved contract is eligible."))
    run.employee_count = len(lines)
    run.basic_pay_total = quantize_money(sum((row.basic_pay for row in lines), ZERO))
    run.allowance_total = quantize_money(
        sum((row.fixed_allowances + row.variable_allowances for row in lines), ZERO)
    )
    run.overtime_total = quantize_money(sum((row.overtime_pay for row in lines), ZERO))
    run.reward_total = quantize_money(sum((row.rewards for row in lines), ZERO))
    run.gross_total = quantize_money(sum((row.gross_pay for row in lines), ZERO))
    run.deduction_total = quantize_money(sum((row.total_deductions for row in lines), ZERO))
    run.net_total = quantize_money(sum((row.net_pay for row in lines), ZERO))
    run.warning_count = warning_count
    run.status = PayrollRunStatus.CALCULATED
    run.calculated_by = actor
    run.calculated_at = timezone.now()
    run.reviewed_by = None
    run.reviewed_at = None
    run.approved_by = None
    run.approved_at = None
    run.rejection_reason = ""
    run.full_clean()
    run.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
        reason=str(_("Payroll calculated")),
    )
    return run


@transaction.atomic
def review_payroll_run(*, payroll_run: PayrollRun, actor: User) -> PayrollRun:
    run = PayrollRun.objects.select_for_update().get(pk=payroll_run.pk)
    require_organization_permission(actor, REVIEW_PAYROLL, run.organization)
    if run.status != PayrollRunStatus.CALCULATED:
        raise ValidationError(_("Only a calculated payroll can be reviewed."))
    previous = snapshot(run)
    run.status = PayrollRunStatus.REVIEWED
    run.reviewed_by = actor
    run.reviewed_at = timezone.now()
    run.rejection_reason = ""
    run.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
        reason=str(_("Payroll reviewed")),
    )
    return run


@transaction.atomic
def return_payroll_to_calculation(
    *, payroll_run: PayrollRun, reason: str, actor: User
) -> PayrollRun:
    if not reason.strip():
        raise ValidationError(_("Returning payroll requires a reason."))
    run = PayrollRun.objects.select_for_update().get(pk=payroll_run.pk)
    require_organization_permission(actor, REVIEW_PAYROLL, run.organization)
    if run.status != PayrollRunStatus.REVIEWED:
        raise ValidationError(_("Only a reviewed payroll can return to calculation."))
    previous = snapshot(run)
    run.status = PayrollRunStatus.CALCULATED
    run.rejection_reason = reason.strip()
    run.approved_by = None
    run.approved_at = None
    run.save()
    record_audit_event(
        action=AuditAction.REJECTED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
        reason=reason.strip(),
    )
    return run


@transaction.atomic
def approve_payroll_run(*, payroll_run: PayrollRun, actor: User) -> PayrollRun:
    run = (
        PayrollRun.objects.select_for_update()
        .prefetch_related("employee_lines")
        .get(pk=payroll_run.pk)
    )
    require_organization_permission(actor, APPROVE_PAYROLL, run.organization)
    if run.status != PayrollRunStatus.REVIEWED:
        raise ValidationError(_("Only a reviewed payroll can be approved."))
    if run.created_by_id == actor.pk or run.calculated_by_id == actor.pk:
        raise ValidationError(
            _("The payroll creator or calculator cannot approve it."), code="maker_checker"
        )
    lines = list(run.employee_lines.all())
    blockers = [
        warning
        for line in lines
        for warning in line.warnings
        if warning.get("severity") == "BLOCKER"
    ]
    if blockers:
        raise ValidationError(
            _("Resolve payroll blockers before approval."), code="payroll_blockers"
        )
    if sum((line.gross_pay for line in lines), ZERO) != run.gross_total:
        raise ValidationError(_("Payroll gross total does not match employee lines."))
    if sum((line.net_pay for line in lines), ZERO) != run.net_total:
        raise ValidationError(_("Payroll net total does not match employee lines."))
    if run.gross_total - run.deduction_total != run.net_total:
        raise ValidationError(_("Payroll gross minus deductions does not equal net."))
    previous = snapshot(run)
    run.status = PayrollRunStatus.APPROVED
    run.approved_by = actor
    run.approved_at = timezone.now()
    run.rejection_reason = ""
    run.save()
    record_audit_event(
        action=AuditAction.APPROVED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
    )
    return run
