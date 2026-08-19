"""Capability permissions and organization scope for Human Resources."""

from __future__ import annotations

from enum import Enum

from django.contrib.auth.models import Permission

from apps.organizations.models import Role
from apps.organizations.permissions import group_for_role


class PermissionScope(Enum):
    ORGANIZATION_AUTHORITY = "ORGANIZATION_AUTHORITY"


APP_LABEL = "hr"

VIEW_EMPLOYEE = f"{APP_LABEL}.view_employee_workspace"
MANAGE_EMPLOYEE = f"{APP_LABEL}.manage_employee"
TERMINATE_EMPLOYEE = f"{APP_LABEL}.terminate_employee"
VIEW_EMPLOYEE_PERSONAL = f"{APP_LABEL}.view_employee_personal"
VIEW_EMPLOYEE_SALARY = f"{APP_LABEL}.view_employee_salary"
VIEW_CONTRACT = f"{APP_LABEL}.view_contract_workspace"
MANAGE_CONTRACT = f"{APP_LABEL}.manage_contract"
APPROVE_CONTRACT = f"{APP_LABEL}.approve_contract"
VIEW_SHIFT = f"{APP_LABEL}.view_shift_workspace"
MANAGE_SHIFT = f"{APP_LABEL}.manage_shift"
ASSIGN_SHIFT = f"{APP_LABEL}.assign_shift"
VIEW_ATTENDANCE = f"{APP_LABEL}.view_attendance_workspace"
RECORD_ATTENDANCE = f"{APP_LABEL}.record_attendance"
CORRECT_ATTENDANCE = f"{APP_LABEL}.correct_attendance"
APPROVE_ATTENDANCE = f"{APP_LABEL}.approve_attendance"
VIEW_LEAVE = f"{APP_LABEL}.view_leave_workspace"
REQUEST_LEAVE = f"{APP_LABEL}.request_leave"
APPROVE_LEAVE = f"{APP_LABEL}.approve_leave"
CLASSIFY_ABSENCE = f"{APP_LABEL}.classify_absence"
VIEW_OVERTIME = f"{APP_LABEL}.view_overtime_workspace"
MANAGE_OVERTIME = f"{APP_LABEL}.manage_overtime"
APPROVE_OVERTIME = f"{APP_LABEL}.approve_overtime"
VIEW_DEDUCTION = f"{APP_LABEL}.view_deduction_workspace"
MANAGE_DEDUCTION = f"{APP_LABEL}.manage_deduction"
APPROVE_DEDUCTION = f"{APP_LABEL}.approve_deduction"
VIEW_ADVANCE = f"{APP_LABEL}.view_advance_workspace"
MANAGE_ADVANCE = f"{APP_LABEL}.manage_advance"
APPROVE_ADVANCE = f"{APP_LABEL}.approve_advance"
DISBURSE_ADVANCE = f"{APP_LABEL}.disburse_advance"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_EMPLOYEE,
    MANAGE_EMPLOYEE,
    TERMINATE_EMPLOYEE,
    VIEW_EMPLOYEE_PERSONAL,
    VIEW_EMPLOYEE_SALARY,
    VIEW_CONTRACT,
    MANAGE_CONTRACT,
    APPROVE_CONTRACT,
    VIEW_SHIFT,
    MANAGE_SHIFT,
    ASSIGN_SHIFT,
    VIEW_ATTENDANCE,
    RECORD_ATTENDANCE,
    CORRECT_ATTENDANCE,
    APPROVE_ATTENDANCE,
    VIEW_LEAVE,
    REQUEST_LEAVE,
    APPROVE_LEAVE,
    CLASSIFY_ABSENCE,
    VIEW_OVERTIME,
    MANAGE_OVERTIME,
    APPROVE_OVERTIME,
    VIEW_DEDUCTION,
    MANAGE_DEDUCTION,
    APPROVE_DEDUCTION,
    VIEW_ADVANCE,
    MANAGE_ADVANCE,
    APPROVE_ADVANCE,
    DISBURSE_ADVANCE,
)

PERMISSION_SCOPE = dict.fromkeys(ALL_PERMISSIONS, PermissionScope.ORGANIZATION_AUTHORITY)

_FULL = frozenset(ALL_PERMISSIONS)
_MANAGER = frozenset(
    {
        VIEW_EMPLOYEE,
        MANAGE_EMPLOYEE,
        TERMINATE_EMPLOYEE,
        VIEW_EMPLOYEE_PERSONAL,
        VIEW_EMPLOYEE_SALARY,
        VIEW_CONTRACT,
        MANAGE_CONTRACT,
        APPROVE_CONTRACT,
        VIEW_SHIFT,
        MANAGE_SHIFT,
        ASSIGN_SHIFT,
        VIEW_ATTENDANCE,
        RECORD_ATTENDANCE,
        CORRECT_ATTENDANCE,
        APPROVE_ATTENDANCE,
        VIEW_LEAVE,
        REQUEST_LEAVE,
        APPROVE_LEAVE,
        CLASSIFY_ABSENCE,
        VIEW_OVERTIME,
        MANAGE_OVERTIME,
        APPROVE_OVERTIME,
        VIEW_DEDUCTION,
        MANAGE_DEDUCTION,
        APPROVE_DEDUCTION,
        VIEW_ADVANCE,
        MANAGE_ADVANCE,
        APPROVE_ADVANCE,
        DISBURSE_ADVANCE,
    }
)
_ACCOUNTING_MANAGER = frozenset(
    {
        VIEW_EMPLOYEE,
        VIEW_EMPLOYEE_SALARY,
        VIEW_CONTRACT,
        APPROVE_CONTRACT,
        VIEW_SHIFT,
        VIEW_ATTENDANCE,
        VIEW_LEAVE,
        VIEW_OVERTIME,
        VIEW_DEDUCTION,
        APPROVE_DEDUCTION,
        VIEW_ADVANCE,
        APPROVE_ADVANCE,
        DISBURSE_ADVANCE,
    }
)
_ACCOUNTANT = frozenset(
    {
        VIEW_EMPLOYEE,
        VIEW_EMPLOYEE_SALARY,
        VIEW_CONTRACT,
        VIEW_SHIFT,
        VIEW_ATTENDANCE,
        VIEW_LEAVE,
        VIEW_OVERTIME,
        VIEW_DEDUCTION,
        VIEW_ADVANCE,
    }
)
_VIEWER = frozenset({VIEW_EMPLOYEE})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: _FULL,
    Role.MANAGER.value: _MANAGER,
    Role.ACCOUNTING_MANAGER.value: _ACCOUNTING_MANAGER,
    Role.ACCOUNTANT.value: _ACCOUNTANT,
    Role.VIEWER.value: _VIEWER,
    Role.PURCHASING.value: frozenset(),
    Role.STOREKEEPER.value: frozenset(),
    Role.CASHIER.value: frozenset(),
}


def permissions_for_role(role: Role | str) -> frozenset[str]:
    value = role.value if isinstance(role, Role) else str(role)
    return ROLE_PERMISSIONS.get(value, frozenset())


def scope_of(permission: str) -> PermissionScope:
    try:
        return PERMISSION_SCOPE[permission]
    except KeyError:
        raise ValueError(f"{permission} is not an HR permission") from None


def sync_role_groups() -> None:
    known = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label=APP_LABEL)
    }
    missing = [name for name in ALL_PERMISSIONS if name not in known]
    if missing:
        raise LookupError(f"HR permissions are not migrated: {sorted(missing)}")
    ours = set(known.values())
    for role, permission_names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in permission_names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)
