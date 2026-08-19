"""Scope-safe Human Resources selectors."""

from __future__ import annotations

from django.db.models import QuerySet
from django.http import Http404

from apps.hr.models import (
    AttendanceEvent,
    Employee,
    EmployeeAdvance,
    EmployeeContract,
    EmployeeDeduction,
    LeaveRequest,
    OvertimeRequest,
    Shift,
    ShiftAssignment,
)
from apps.hr.permissions import (
    VIEW_ADVANCE,
    VIEW_ATTENDANCE,
    VIEW_CONTRACT,
    VIEW_DEDUCTION,
    VIEW_EMPLOYEE,
    VIEW_LEAVE,
    VIEW_OVERTIME,
    VIEW_SHIFT,
)
from apps.organizations.authorization import organizations_with_permission
from apps.users.models import User


def visible_employees(actor: User) -> QuerySet[Employee]:
    return Employee.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_EMPLOYEE)
    )


def resolve_employee(actor: User, pk: int) -> Employee:
    try:
        return (
            visible_employees(actor)
            .select_related("organization", "branch", "created_by")
            .get(pk=pk)
        )
    except Employee.DoesNotExist as error:
        raise Http404 from error


def visible_contracts(actor: User) -> QuerySet[EmployeeContract]:
    return EmployeeContract.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_CONTRACT)
    )


def resolve_contract(actor: User, pk: int) -> EmployeeContract:
    try:
        return (
            visible_contracts(actor)
            .select_related(
                "organization",
                "employee",
                "branch",
                "payroll_policy",
                "created_by",
                "approved_by",
            )
            .get(pk=pk)
        )
    except EmployeeContract.DoesNotExist as error:
        raise Http404 from error


def visible_shifts(actor: User) -> QuerySet[Shift]:
    return Shift.objects.filter(organization__in=organizations_with_permission(actor, VIEW_SHIFT))


def resolve_shift(actor: User, pk: int) -> Shift:
    try:
        return (
            visible_shifts(actor).select_related("organization", "branch", "created_by").get(pk=pk)
        )
    except Shift.DoesNotExist as error:
        raise Http404 from error


def visible_shift_assignments(actor: User) -> QuerySet[ShiftAssignment]:
    return ShiftAssignment.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_SHIFT)
    )


def visible_attendance_events(actor: User) -> QuerySet[AttendanceEvent]:
    return AttendanceEvent.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_ATTENDANCE)
    )


def resolve_attendance_event(actor: User, pk: int) -> AttendanceEvent:
    try:
        return (
            visible_attendance_events(actor)
            .select_related(
                "organization",
                "branch",
                "employee",
                "shift_assignment",
                "scheduled_shift",
                "created_by",
                "supersedes",
            )
            .get(pk=pk)
        )
    except AttendanceEvent.DoesNotExist as error:
        raise Http404 from error


def visible_leave_requests(actor: User) -> QuerySet[LeaveRequest]:
    return LeaveRequest.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_LEAVE)
    )


def resolve_leave_request(actor: User, pk: int) -> LeaveRequest:
    try:
        return (
            visible_leave_requests(actor)
            .select_related(
                "organization",
                "employee",
                "employee__branch",
                "leave_type",
                "requested_by",
                "approved_by",
            )
            .get(pk=pk)
        )
    except LeaveRequest.DoesNotExist as error:
        raise Http404 from error


def visible_overtime(actor: User) -> QuerySet[OvertimeRequest]:
    return OvertimeRequest.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_OVERTIME)
    )


def resolve_overtime(actor: User, pk: int) -> OvertimeRequest:
    try:
        return (
            visible_overtime(actor)
            .select_related(
                "organization", "employee", "employee__branch", "shift", "created_by", "approved_by"
            )
            .get(pk=pk)
        )
    except OvertimeRequest.DoesNotExist as error:
        raise Http404 from error


def visible_deductions(actor: User) -> QuerySet[EmployeeDeduction]:
    return EmployeeDeduction.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_DEDUCTION)
    )


def resolve_deduction(actor: User, pk: int) -> EmployeeDeduction:
    try:
        return (
            visible_deductions(actor)
            .select_related(
                "organization", "employee", "employee__branch", "created_by", "approved_by"
            )
            .prefetch_related("allocations")
            .get(pk=pk)
        )
    except EmployeeDeduction.DoesNotExist as error:
        raise Http404 from error


def visible_advances(actor: User) -> QuerySet[EmployeeAdvance]:
    return EmployeeAdvance.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_ADVANCE)
    )


def resolve_advance(actor: User, pk: int) -> EmployeeAdvance:
    try:
        return (
            visible_advances(actor)
            .select_related(
                "organization", "employee", "employee__branch", "created_by", "approved_by"
            )
            .prefetch_related("disbursements", "recoveries")
            .get(pk=pk)
        )
    except EmployeeAdvance.DoesNotExist as error:
        raise Http404 from error
