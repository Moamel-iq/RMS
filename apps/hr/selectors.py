"""Scope-safe Human Resources selectors."""

from __future__ import annotations

from django.db.models import QuerySet
from django.http import Http404

from apps.hr.models import Employee, EmployeeContract
from apps.hr.permissions import VIEW_CONTRACT, VIEW_EMPLOYEE
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
