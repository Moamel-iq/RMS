"""Deterministic payroll calculation and approval over frozen HR evidence."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    EMPLOYEE_RECEIVABLE,
    PAYROLL_ALLOWANCE_EXPENSE,
    PAYROLL_BANK,
    PAYROLL_CASH,
    PAYROLL_OTHER_LIABILITY,
    PAYROLL_OVERTIME_EXPENSE,
    PAYROLL_PAYABLE,
    PAYROLL_SALARY_EXPENSE,
    CostCenter,
    SourceEvent,
)
from apps.accounting.services import (
    post_entry,
    resolve_default_account,
    resolve_period,
    reverse_entry,
)
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.locks import lock_account_mappings_shared
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.hr.attendance import assignment_for, calculate_attendance_day
from apps.hr.models import (
    AdvanceRecoveryAllocation,
    AdvanceStatus,
    AttendanceApprovalStatus,
    AttendanceDayApproval,
    ContractStatus,
    DeductionAllocation,
    DeductionType,
    Employee,
    EmployeeAdvance,
    EmployeeContract,
    EmployeeDeduction,
    EmployeePaymentMethod,
    OvertimeRequest,
    PayrollComponentKind,
    PayrollComponentLine,
    PayrollEmployeeLine,
    PayrollPayment,
    PayrollPaymentAllocation,
    PayrollPaymentStatus,
    PayrollPolicy,
    PayrollRun,
    PayrollRunStatus,
    ProrationBasis,
    RecoveryMode,
    RequestStatus,
    WageBasis,
)
from apps.hr.permissions import (
    APPROVE_PAYROLL,
    CALCULATE_PAYROLL,
    PAY_PAYROLL,
    POST_PAYROLL,
    REVIEW_PAYROLL,
)
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
    period = resolve_period(organization=run.organization, accounting_date=run.accounting_date)
    validate_period_accepts_postings(period)
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


# ---------------------------------------------------------------------------
# Payroll accounting, release, payment, and reversal
# ---------------------------------------------------------------------------


PAYROLL_RUN_SOURCE = "HR_PAYROLL_RUN"
PAYROLL_PAYMENT_SOURCE = "HR_PAYROLL_PAYMENT"
PAYROLL_ACCRUAL_RULE = "hr-payroll-accrual-v1"
PAYROLL_PAYMENT_RULE = "hr-payroll-payment-v1"

PAYMENT_METHOD_ROLES: dict[str, str] = {
    EmployeePaymentMethod.CASH: PAYROLL_CASH,
    EmployeePaymentMethod.BANK: PAYROLL_BANK,
}


def _mapped_account(run: PayrollRun, role: str, on_date: datetime.date) -> Any:
    return resolve_default_account(
        organization=run.organization, account_role=role, on_date=on_date
    ).account


def _line_total(run: PayrollRun, *fields: str) -> Decimal:
    total = ZERO
    for line in run.employee_lines.all():
        total += sum((getattr(line, field) for field in fields), ZERO)
    return quantize_money(total)


def _payroll_posting_lines(run: PayrollRun) -> list[PostingLine]:
    attendance_reductions = _line_total(
        run, "absence_deduction", "lateness_deduction", "early_departure_deduction"
    )
    advance_recovery = _line_total(run, "advance_recovery")
    other_liability = _line_total(run, "administrative_deduction", "other_deductions")
    salary_expense = quantize_money(run.basic_pay_total - attendance_reductions)
    allowance_expense = quantize_money(run.allowance_total + run.reward_total)

    roles: dict[str, Any] = {}
    amounts_by_role = {
        PAYROLL_SALARY_EXPENSE: salary_expense,
        PAYROLL_ALLOWANCE_EXPENSE: allowance_expense,
        PAYROLL_OVERTIME_EXPENSE: run.overtime_total,
        PAYROLL_PAYABLE: run.net_total,
        EMPLOYEE_RECEIVABLE: advance_recovery,
        PAYROLL_OTHER_LIABILITY: other_liability,
    }
    for role, amount in amounts_by_role.items():
        if amount > ZERO:
            roles[role] = _mapped_account(run, role, run.accounting_date)
    needs_cost_center = any(
        account.requires_cost_center
        for role, account in roles.items()
        if role
        in {
            PAYROLL_SALARY_EXPENSE,
            PAYROLL_ALLOWANCE_EXPENSE,
            PAYROLL_OVERTIME_EXPENSE,
        }
    )
    cost_center = None
    if needs_cost_center:
        cost_center = CostCenter.objects.filter(
            organization=run.organization, code="HR", is_active=True
        ).first()
        if cost_center is None:
            raise ValidationError(
                _("The organization needs an active HR cost center before payroll posting."),
                code="payroll_cost_center_missing",
            )

    lines: list[PostingLine] = []
    for role, amount in (
        (PAYROLL_SALARY_EXPENSE, salary_expense),
        (PAYROLL_ALLOWANCE_EXPENSE, allowance_expense),
        (PAYROLL_OVERTIME_EXPENSE, run.overtime_total),
    ):
        if amount > ZERO:
            lines.append(
                PostingLine(
                    account=roles[role],
                    branch=run.branch,
                    cost_center=cost_center if roles[role].requires_cost_center else None,
                    debit=amount,
                    narration=str(_("استحقاق رواتب %(run)s") % {"run": run.run_number}),
                )
            )
    for role, amount in (
        (PAYROLL_PAYABLE, run.net_total),
        (EMPLOYEE_RECEIVABLE, advance_recovery),
        (PAYROLL_OTHER_LIABILITY, other_liability),
    ):
        if amount > ZERO:
            lines.append(
                PostingLine(
                    account=roles[role],
                    branch=run.branch,
                    credit=amount,
                    narration=str(_("استحقاق رواتب %(run)s") % {"run": run.run_number}),
                )
            )
    debit = quantize_money(sum((line.debit for line in lines), ZERO))
    credit = quantize_money(sum((line.credit for line in lines), ZERO))
    if debit != credit:
        raise ValidationError(
            _("Payroll accounting lines do not reconcile."), code="payroll_posting_unbalanced"
        )
    return lines


def _allocate_posted_inputs(*, run: PayrollRun, journal: Any) -> None:
    reference_prefix = str(run.public_id)
    components = run.employee_lines.all().prefetch_related("components")
    now = timezone.now()
    for employee_line in components:
        for component in employee_line.components.all():
            if component.amount <= ZERO or not component.source_id:
                continue
            reference = f"{reference_prefix}:{component.pk}"
            if component.source_type == "EmployeeDeduction":
                deduction = (
                    EmployeeDeduction.objects.select_for_update()
                    .filter(
                        public_id=component.source_id,
                        employee_id=employee_line.employee_id,
                        organization=run.organization,
                        status=RequestStatus.APPROVED,
                    )
                    .first()
                )
                if deduction is None:
                    raise ValidationError(
                        _("A frozen payroll deduction source is no longer valid."),
                        code="payroll_deduction_source_invalid",
                    )
                deduction_allocation = DeductionAllocation.objects.create(
                    deduction=deduction,
                    payroll_reference=reference,
                    amount=component.amount,
                    allocated_at=now,
                    payroll_run=run,
                    journal_entry=journal,
                )
                record_audit_event(
                    action=AuditAction.POSTED,
                    target=deduction_allocation,
                    branch=run.branch,
                    new_state=snapshot(deduction_allocation),
                )
            elif component.source_type == "EmployeeAdvance":
                advance = (
                    EmployeeAdvance.objects.select_for_update()
                    .filter(
                        public_id=component.source_id,
                        employee_id=employee_line.employee_id,
                        organization=run.organization,
                    )
                    .first()
                )
                if advance is None:
                    raise ValidationError(
                        _("A frozen payroll advance source is no longer valid."),
                        code="payroll_advance_source_invalid",
                    )
                advance_allocation = AdvanceRecoveryAllocation.objects.create(
                    advance=advance,
                    payroll_reference=reference,
                    amount=component.amount,
                    recovered_at=now,
                    payroll_run=run,
                    journal_entry=journal,
                )
                record_audit_event(
                    action=AuditAction.POSTED,
                    target=advance_allocation,
                    branch=run.branch,
                    new_state=snapshot(advance_allocation),
                )
            elif component.source_type == "OvertimeRequest":
                overtime = (
                    OvertimeRequest.objects.select_for_update()
                    .filter(
                        public_id=component.source_id,
                        employee_id=employee_line.employee_id,
                        status=RequestStatus.APPROVED,
                    )
                    .first()
                )
                if overtime is None or overtime.payroll_inclusion_reference not in {
                    "",
                    reference_prefix,
                }:
                    raise ValidationError(
                        _("A frozen overtime source has already been included elsewhere."),
                        code="payroll_overtime_source_invalid",
                    )
                previous = snapshot(overtime)
                overtime.payroll_inclusion_reference = reference_prefix
                overtime.included_at = now
                overtime.save(
                    update_fields=["payroll_inclusion_reference", "included_at", "updated_at"]
                )
                record_audit_event(
                    action=AuditAction.POSTED,
                    target=overtime,
                    branch=run.branch,
                    previous_state=previous,
                    new_state=snapshot(overtime),
                )


@transaction.atomic
def post_payroll_run(*, payroll_run: PayrollRun, actor: User) -> PayrollRun:
    run = (
        PayrollRun.objects.select_for_update()
        .select_related("organization", "branch")
        .prefetch_related("employee_lines", "employee_lines__components")
        .get(pk=payroll_run.pk)
    )
    require_organization_permission(actor, POST_PAYROLL, run.organization)
    if run.status != PayrollRunStatus.APPROVED:
        raise ValidationError(
            _("Only an approved payroll can be posted."), code="payroll_not_approved"
        )
    period = resolve_period(organization=run.organization, accounting_date=run.accounting_date)
    validate_period_accepts_postings(period)
    lock_account_mappings_shared(run.organization_id)
    posting_lines = _payroll_posting_lines(run)
    journal = post_entry(
        organization=run.organization,
        accounting_date=run.accounting_date,
        document_date=run.period_end,
        lines=posting_lines,
        idempotency_key=f"hr-payroll-run:{run.public_id}:post",
        narration=str(_("استحقاق الرواتب %(run)s") % {"run": run.run_number}),
        source_document_type=PAYROLL_RUN_SOURCE,
        source_document_id=str(run.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=PAYROLL_ACCRUAL_RULE,
    )
    _allocate_posted_inputs(run=run, journal=journal)
    previous = snapshot(run)
    run.status = PayrollRunStatus.POSTED
    run.accrual_journal = journal
    run.posted_by = actor
    run.posted_at = timezone.now()
    run.save(
        update_fields=[
            "status",
            "accrual_journal",
            "posted_by",
            "posted_at",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.POSTED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
        source_document_type=PAYROLL_RUN_SOURCE,
        source_document_id=str(run.public_id),
        metadata={"journal_entry": journal.entry_number},
    )
    return run


@transaction.atomic
def release_payroll_run(*, payroll_run: PayrollRun, actor: User) -> PayrollRun:
    run = (
        PayrollRun.objects.select_for_update().select_related("organization").get(pk=payroll_run.pk)
    )
    require_organization_permission(actor, POST_PAYROLL, run.organization)
    if run.status != PayrollRunStatus.POSTED or run.accrual_journal_id is None:
        raise ValidationError(
            _("Only a posted payroll can be released for payment."),
            code="payroll_not_posted",
        )
    previous = snapshot(run)
    run.status = PayrollRunStatus.RELEASED
    run.released_by = actor
    run.released_at = timezone.now()
    run.save(update_fields=["status", "released_by", "released_at", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
        reason=str(_("Payroll released for payment")),
    )
    return run


def _allocation_signature(
    allocations: Sequence[tuple[PayrollEmployeeLine, Decimal]],
) -> dict[int, Decimal]:
    signature: dict[int, Decimal] = {}
    for line, requested in allocations:
        amount = quantize_money(requested)
        if amount <= ZERO:
            raise ValidationError(
                _("Every payroll payment allocation must be greater than zero."),
                code="payment_allocation_not_positive",
            )
        signature[line.pk] = quantize_money(signature.get(line.pk, ZERO) + amount)
    if not signature:
        raise ValidationError(
            _("A payroll payment needs at least one employee allocation."),
            code="payment_allocations_required",
        )
    return signature


def _locked_payment_lines(
    *, run: PayrollRun, signature: dict[int, Decimal]
) -> list[tuple[PayrollEmployeeLine, Decimal]]:
    locked = {
        line.pk: line
        for line in PayrollEmployeeLine.objects.select_for_update()
        .filter(payroll_run=run, pk__in=signature)
        .order_by("pk")
    }
    if set(locked) != set(signature):
        raise ValidationError(
            _("A payment allocation belongs to another payroll run."),
            code="payment_allocation_crosses_run",
        )
    result: list[tuple[PayrollEmployeeLine, Decimal]] = []
    for line_id in sorted(signature):
        line = locked[line_id]
        amount = signature[line_id]
        outstanding = line.outstanding_amount
        if amount > outstanding:
            raise ValidationError(
                _("The payment exceeds the employee's outstanding net pay."),
                code="payment_over_employee",
            )
        result.append((line, amount))
    return result


def _refresh_run_payment_status(run: PayrollRun) -> PayrollRun:
    paid = quantize_money(run.paid_total)
    if paid < ZERO or paid > run.net_total:
        raise ValidationError(
            _("Payroll payment allocations do not reconcile to approved net pay."),
            code="payroll_payment_reconciliation",
        )
    if paid == run.net_total:
        run.status = PayrollRunStatus.PAID
        run.paid_at = timezone.now()
    elif paid > ZERO:
        run.status = PayrollRunStatus.PARTIALLY_PAID
        run.paid_at = None
    else:
        run.status = PayrollRunStatus.RELEASED
        run.paid_at = None
    run.save(update_fields=["status", "paid_at", "updated_at"])
    return run


def _next_payment_number(*, organization: Organization, payment_date: datetime.date) -> str:
    sequence = (
        PayrollPayment.objects.filter(
            organization=organization, payment_date__year=payment_date.year
        ).count()
        + 1
    )
    return f"SAL-{payment_date.year}-{sequence:06d}"


@transaction.atomic
def create_payroll_payment(
    *,
    payroll_run: PayrollRun,
    payment_date: datetime.date,
    method: str,
    reference: str,
    reason: str,
    allocations: Sequence[tuple[PayrollEmployeeLine, Decimal]],
    idempotency_key: str,
    actor: User,
) -> PayrollPayment:
    organization = Organization.objects.select_for_update().get(pk=payroll_run.organization_id)
    run = (
        PayrollRun.objects.select_for_update()
        .select_related("branch", "organization")
        .get(pk=payroll_run.pk)
    )
    require_organization_permission(actor, PAY_PAYROLL, organization)
    key = idempotency_key.strip()
    if not key:
        raise ValidationError(_("A payment idempotency key is required."))
    signature = _allocation_signature(allocations)
    existing = (
        PayrollPayment.objects.filter(organization=organization, idempotency_key=key)
        .prefetch_related("allocations")
        .first()
    )
    if existing is not None:
        existing_signature = {
            row.employee_line_id: row.amount for row in existing.allocations.all()
        }
        if (
            existing.payroll_run_id != run.pk
            or existing.payment_date != payment_date
            or existing.method != method
            or existing.reference != reference.strip()
            or existing_signature != signature
        ):
            raise ValidationError(
                _("The payroll payment idempotency key was reused for another request."),
                code="payment_idempotency_conflict",
            )
        return existing
    if run.status not in {PayrollRunStatus.RELEASED, PayrollRunStatus.PARTIALLY_PAID}:
        raise ValidationError(
            _("Only a released payroll with an outstanding balance can be paid."),
            code="payroll_not_released",
        )
    if method not in PAYMENT_METHOD_ROLES:
        raise ValidationError(_("Unknown payroll payment method."), code="unknown_method")
    if not reference.strip():
        raise ValidationError(
            _("A payroll payment reference is required."), code="reference_required"
        )
    if run.released_at and payment_date < run.released_at.date():
        raise ValidationError(
            _("The payment date cannot precede payroll release."), code="payment_before_release"
        )
    locked_allocations = _locked_payment_lines(run=run, signature=signature)
    amount = quantize_money(sum((value for _line, value in locked_allocations), ZERO))
    period = resolve_period(organization=organization, accounting_date=payment_date)
    validate_period_accepts_postings(period)
    lock_account_mappings_shared(organization.pk)
    payable = _mapped_account(run, PAYROLL_PAYABLE, payment_date)
    source = _mapped_account(run, PAYMENT_METHOD_ROLES[method], payment_date)
    public_id = uuid.uuid4()
    journal = post_entry(
        organization=organization,
        accounting_date=payment_date,
        document_date=payment_date,
        lines=[
            PostingLine(account=payable, branch=run.branch, debit=amount),
            PostingLine(account=source, branch=run.branch, credit=amount),
        ],
        idempotency_key=f"hr-payroll-payment:{public_id}:post",
        narration=reason.strip() or str(_("صرف رواتب %(run)s") % {"run": run.run_number}),
        source_document_type=PAYROLL_PAYMENT_SOURCE,
        source_document_id=str(public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=PAYROLL_PAYMENT_RULE,
    )
    payment = PayrollPayment(
        public_id=public_id,
        payroll_run=run,
        organization=organization,
        branch=run.branch,
        payment_number=_next_payment_number(organization=organization, payment_date=payment_date),
        payment_date=payment_date,
        method=method,
        amount=amount,
        reference=reference.strip(),
        reason=reason.strip(),
        idempotency_key=key,
        journal_entry=journal,
        created_by=actor,
    )
    payment.full_clean()
    payment.save()
    PayrollPaymentAllocation.objects.bulk_create(
        [
            PayrollPaymentAllocation(payment=payment, employee_line=line, amount=value)
            for line, value in locked_allocations
        ]
    )
    previous = snapshot(run)
    _refresh_run_payment_status(run)
    record_audit_event(
        action=AuditAction.POSTED,
        target=payment,
        branch=run.branch,
        new_state=snapshot(payment),
        source_document_type=PAYROLL_PAYMENT_SOURCE,
        source_document_id=str(payment.public_id),
        metadata={
            "journal_entry": journal.entry_number,
            "allocation_count": len(locked_allocations),
        },
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
        reason=str(_("Payroll payment posted")),
    )
    return payment


@transaction.atomic
def reverse_payroll_payment(
    *,
    payment: PayrollPayment,
    reversal_date: datetime.date,
    reason: str,
    actor: User,
) -> PayrollPayment:
    organization = Organization.objects.select_for_update().get(pk=payment.organization_id)
    run = (
        PayrollRun.objects.select_for_update()
        .select_related("branch", "organization")
        .get(pk=payment.payroll_run_id)
    )
    locked = (
        PayrollPayment.objects.select_for_update()
        .select_related("journal_entry")
        .prefetch_related("allocations")
        .get(pk=payment.pk)
    )
    require_organization_permission(actor, PAY_PAYROLL, organization)
    if not reason.strip():
        raise ValidationError(_("A payroll payment reversal requires a reason."))
    if locked.reversal_of_id is not None:
        raise ValidationError(_("A reversal payment cannot itself be reversed."))
    if locked.status != PayrollPaymentStatus.POSTED or hasattr(locked, "reversal"):
        raise ValidationError(
            _("This payroll payment is already reversed."), code="payment_already_reversed"
        )
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"hr-payroll-payment:{locked.public_id}:reverse",
        reason=reason.strip(),
        accounting_date=reversal_date,
    )
    reversal = PayrollPayment.objects.create(
        payroll_run=run,
        organization=organization,
        branch=run.branch,
        payment_number=_next_payment_number(organization=organization, payment_date=reversal_date),
        payment_date=reversal_date,
        method=locked.method,
        amount=locked.amount,
        reference=f"REV-{locked.reference}"[:200],
        reason=reason.strip(),
        idempotency_key=f"reverse:{locked.public_id}",
        journal_entry=reversal_journal,
        reversal_of=locked,
        created_by=actor,
    )
    PayrollPaymentAllocation.objects.bulk_create(
        [
            PayrollPaymentAllocation(
                payment=reversal,
                employee_line_id=allocation.employee_line_id,
                amount=allocation.amount,
            )
            for allocation in locked.allocations.all()
        ]
    )
    locked.status = PayrollPaymentStatus.REVERSED
    locked.save(update_fields=["status", "updated_at"])
    previous = snapshot(run)
    _refresh_run_payment_status(run)
    record_audit_event(
        action=AuditAction.REVERSED,
        target=locked,
        branch=run.branch,
        new_state=snapshot(locked),
        reason=reason.strip(),
        source_document_type=PAYROLL_PAYMENT_SOURCE,
        source_document_id=str(locked.public_id),
        metadata={"reversal_payment": str(reversal.public_id)},
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
        reason=reason.strip(),
    )
    return reversal


@transaction.atomic
def reverse_payroll_run(
    *,
    payroll_run: PayrollRun,
    reversal_date: datetime.date,
    reason: str,
    actor: User,
) -> PayrollRun:
    organization = Organization.objects.select_for_update().get(pk=payroll_run.organization_id)
    run = (
        PayrollRun.objects.select_for_update()
        .select_related("branch")
        .prefetch_related("employee_lines__components")
        .get(pk=payroll_run.pk)
    )
    require_organization_permission(actor, POST_PAYROLL, organization)
    if not reason.strip():
        raise ValidationError(_("A payroll reversal requires a reason."))
    if run.status not in {PayrollRunStatus.POSTED, PayrollRunStatus.RELEASED}:
        raise ValidationError(
            _("Reverse all salary payments before reversing payroll accrual."),
            code="payroll_has_payments",
        )
    if run.payments.filter(status=PayrollPaymentStatus.POSTED, reversal_of__isnull=True).exists():
        raise ValidationError(
            _("Reverse all salary payments before reversing payroll accrual."),
            code="payroll_has_payments",
        )
    if run.accrual_journal_id is None:
        raise ValidationError(_("The payroll accrual journal is missing."))
    accrual_journal = run.accrual_journal
    assert accrual_journal is not None  # noqa: S101 - guarded by accrual_journal_id above
    reversal_journal = reverse_entry(
        entry=accrual_journal,
        idempotency_key=f"hr-payroll-run:{run.public_id}:reverse",
        reason=reason.strip(),
        accounting_date=reversal_date,
    )
    now = timezone.now()
    for original in run.deduction_allocations.select_for_update().filter(reversal_of__isnull=True):
        deduction_reversal = DeductionAllocation.objects.create(
            deduction=original.deduction,
            payroll_reference=f"{original.payroll_reference}:REV",
            amount=original.amount,
            allocated_at=now,
            payroll_run=run,
            journal_entry=reversal_journal,
            reversal_of=original,
        )
        record_audit_event(
            action=AuditAction.REVERSED,
            target=deduction_reversal,
            branch=run.branch,
            new_state=snapshot(deduction_reversal),
            reason=reason.strip(),
        )
    for advance_original in run.advance_recovery_allocations.select_for_update().filter(
        reversal_of__isnull=True
    ):
        advance_reversal = AdvanceRecoveryAllocation.objects.create(
            advance=advance_original.advance,
            payroll_reference=f"{advance_original.payroll_reference}:REV",
            amount=advance_original.amount,
            recovered_at=now,
            payroll_run=run,
            journal_entry=reversal_journal,
            reversal_of=advance_original,
        )
        record_audit_event(
            action=AuditAction.REVERSED,
            target=advance_reversal,
            branch=run.branch,
            new_state=snapshot(advance_reversal),
            reason=reason.strip(),
        )
    overtime_ids = {
        component.source_id
        for employee_line in run.employee_lines.all()
        for component in employee_line.components.all()
        if component.source_type == "OvertimeRequest" and component.source_id
    }
    for overtime in OvertimeRequest.objects.select_for_update().filter(
        public_id__in=overtime_ids,
        payroll_inclusion_reference=str(run.public_id),
    ):
        previous_overtime = snapshot(overtime)
        overtime.payroll_inclusion_reference = ""
        overtime.included_at = None
        overtime.save(update_fields=["payroll_inclusion_reference", "included_at", "updated_at"])
        record_audit_event(
            action=AuditAction.REVERSED,
            target=overtime,
            branch=run.branch,
            previous_state=previous_overtime,
            new_state=snapshot(overtime),
            reason=reason.strip(),
        )
    previous = snapshot(run)
    run.status = PayrollRunStatus.REVERSED
    run.reversal_journal = reversal_journal
    run.paid_at = None
    run.save(update_fields=["status", "reversal_journal", "paid_at", "updated_at"])
    record_audit_event(
        action=AuditAction.REVERSED,
        target=run,
        branch=run.branch,
        previous_state=previous,
        new_state=snapshot(run),
        reason=reason.strip(),
        source_document_type=PAYROLL_RUN_SOURCE,
        source_document_id=str(run.public_id),
        metadata={"reversal_journal": reversal_journal.entry_number},
    )
    return run
