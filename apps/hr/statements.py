"""Deterministic employee payroll, receivable, deduction, and attendance statements."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import QuerySet
from django.http import Http404

from apps.hr.models import (
    Employee,
    EmployeeAdvance,
    EmployeeDeduction,
    PayrollEmployeeLine,
    PayrollPaymentAllocation,
    PayrollRunStatus,
)
from apps.hr.permissions import VIEW_EMPLOYEE_STATEMENT
from apps.organizations.authorization import organizations_with_permission
from apps.users.models import User

ZERO = Decimal("0.000")


@dataclass(frozen=True)
class ReceivableEvent:
    event_date: datetime.date
    kind: str
    reference: str
    debit: Decimal
    credit: Decimal
    balance: Decimal


@dataclass(frozen=True)
class EmployeeStatement:
    employee: Employee
    payroll_lines: QuerySet[PayrollEmployeeLine]
    payment_allocations: QuerySet[PayrollPaymentAllocation]
    advances: tuple[EmployeeAdvance, ...]
    deductions: tuple[EmployeeDeduction, ...]
    receivable_events: tuple[ReceivableEvent, ...]
    payroll_outstanding: Decimal
    advance_receivable: Decimal
    deduction_remaining: Decimal


def visible_statement_employees(actor: User) -> QuerySet[Employee]:
    return Employee.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_EMPLOYEE_STATEMENT)
    )


def resolve_statement_employee(actor: User, pk: int) -> Employee:
    employee = (
        visible_statement_employees(actor)
        .select_related("organization", "branch")
        .filter(pk=pk)
        .first()
    )
    if employee is None:
        raise Http404
    return employee


def resolve_statement_line(actor: User, pk: int) -> PayrollEmployeeLine:
    line = (
        PayrollEmployeeLine.objects.filter(
            pk=pk,
            employee__in=visible_statement_employees(actor),
        )
        .select_related(
            "employee",
            "contract",
            "payroll_run",
            "payroll_run__branch",
            "payroll_run__approved_by",
            "payroll_run__released_by",
        )
        .prefetch_related("components", "payment_allocations__payment")
        .first()
    )
    if line is None:
        raise Http404
    return line


def build_employee_statement(employee: Employee) -> EmployeeStatement:
    payroll_lines = (
        employee.payroll_lines.exclude(payroll_run__status=PayrollRunStatus.REVERSED)
        .select_related("payroll_run", "payroll_run__branch", "contract")
        .prefetch_related("payment_allocations__payment")
        .order_by("-payroll_run__period_end", "-id")
    )
    payment_allocations = (
        PayrollPaymentAllocation.objects.filter(
            employee_line__employee=employee,
        )
        .select_related("payment", "employee_line", "employee_line__payroll_run")
        .order_by("-payment__payment_date", "-payment_id")
    )
    advances = tuple(
        employee.advances.prefetch_related("disbursements", "recoveries").order_by(
            "request_date", "id"
        )
    )
    deductions = tuple(
        employee.deductions.prefetch_related("allocations").order_by("effective_period", "id")
    )

    raw_events: list[tuple[datetime.date, str, str, Decimal, Decimal]] = []
    for advance in advances:
        for disbursement in advance.disbursements.all():
            amount = disbursement.net_amount
            raw_events.append(
                (
                    disbursement.disbursement_date,
                    "صرف سلفة" if amount >= ZERO else "عكس صرف سلفة",
                    disbursement.evidence_reference,
                    max(amount, ZERO),
                    max(-amount, ZERO),
                )
            )
        for recovery in advance.recoveries.all():
            amount = recovery.net_amount
            raw_events.append(
                (
                    recovery.recovered_at.date(),
                    "استرداد من الراتب" if amount >= ZERO else "عكس استرداد",
                    recovery.payroll_reference,
                    max(-amount, ZERO),
                    max(amount, ZERO),
                )
            )
    raw_events.sort(key=lambda event: (event[0], event[1], event[2]))
    balance = ZERO
    events: list[ReceivableEvent] = []
    for event_date, kind, reference, debit, credit in raw_events:
        balance += debit - credit
        events.append(
            ReceivableEvent(
                event_date=event_date,
                kind=kind,
                reference=reference,
                debit=debit,
                credit=credit,
                balance=balance,
            )
        )

    return EmployeeStatement(
        employee=employee,
        payroll_lines=payroll_lines,
        payment_allocations=payment_allocations,
        advances=advances,
        deductions=deductions,
        receivable_events=tuple(events),
        payroll_outstanding=sum((line.outstanding_amount for line in payroll_lines), ZERO),
        advance_receivable=sum((advance.outstanding_amount for advance in advances), ZERO),
        deduction_remaining=sum((row.remaining_amount for row in deductions), ZERO),
    )
