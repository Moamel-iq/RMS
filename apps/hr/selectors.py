"""Scope-safe Human Resources selectors."""

from __future__ import annotations

from django.db.models import QuerySet
from django.http import Http404

from apps.hr.models import AttendanceEvent, Employee, EmployeeContract, Shift, ShiftAssignment
from apps.hr.permissions import VIEW_ATTENDANCE, VIEW_CONTRACT, VIEW_EMPLOYEE, VIEW_SHIFT
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
