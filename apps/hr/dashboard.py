"""
The human-resources overview, as one scoped read.

The sharpest redaction in the system lives here. A stock value leaking tells a
storekeeper what the goods are worth; a salary leaking tells a person what
their colleague earns. So the payroll figure sits behind
`hr.view_employee_salary`, is **omitted** rather than zeroed, and — the part
specific to this screen — is only ever an **aggregate**. No individual salary
appears on a dashboard under any permission: an overview that listed the top
earners would turn a summary screen into the one page worth photographing.

Department rows carry headcount always and money only with the permission,
because "who works where" is an org chart and "what each department costs" is
payroll.

Everything here is a read. Nothing writes, posts, or caches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, Sum

from apps.hr.models import ContractStatus, EmployeeStatus
from apps.hr.selectors import visible_contracts, visible_employees
from apps.users.models import User

ZERO = Decimal("0")


@dataclass(frozen=True)
class DepartmentRow:
    """One department's headcount, and its monthly cost when permitted."""

    name: str
    headcount: int
    monthly_salary: Decimal | None = None
    share: Decimal | None = None


@dataclass(frozen=True)
class HrOverview:
    """
    Everything the overview screen renders, already scoped and redacted.

    `monthly_payroll` and the money on each department row are `None` for a
    caller without salary rights. The template asks `is not None`, so a missing
    figure removes its card rather than printing a zero.
    """

    active_count: int
    on_leave_count: int
    total_count: int
    approved_contract_count: int
    draft_contract_count: int
    departments: list[DepartmentRow] = field(default_factory=list)
    monthly_payroll: Decimal | None = None
    average_salary: Decimal | None = None


def hr_overview(user: User, *, include_salary: bool) -> HrOverview:
    """
    Build the overview for everyone `user` can read.

    `include_salary` is the caller's decision, not this function's: the view
    holds the request and therefore the permission, and passing it in keeps
    the redaction testable without a request object.
    """
    employees = visible_employees(user)
    active = employees.filter(status=EmployeeStatus.ACTIVE)
    contracts = visible_contracts(user)
    approved = contracts.filter(status=ContractStatus.APPROVED)

    headcounts = {
        row["department"]: row["total"]
        for row in active.values("department").annotate(total=Count("id")).order_by()
    }

    overview = HrOverview(
        active_count=active.count(),
        on_leave_count=employees.filter(status=EmployeeStatus.ON_LEAVE).count(),
        total_count=employees.count(),
        approved_contract_count=approved.count(),
        draft_contract_count=contracts.filter(status=ContractStatus.DRAFT).count(),
        departments=[
            DepartmentRow(name=name or "—", headcount=count)
            for name, count in sorted(headcounts.items(), key=lambda kv: -kv[1])
        ],
    )
    if not include_salary:
        return overview

    # Salary comes from the APPROVED contract, not the employee row: the
    # contract is the versioned, signed fact, and a draft is not yet payroll.
    payroll = approved.aggregate(total=Sum("basic_salary"))["total"] or ZERO
    by_department = {
        row["department"]: row["total"] or ZERO
        for row in approved.values("department").annotate(total=Sum("basic_salary")).order_by()
    }
    departments = [
        DepartmentRow(
            name=row.name,
            headcount=row.headcount,
            monthly_salary=by_department.get(row.name if row.name != "—" else "", ZERO),
            share=(
                (
                    by_department.get(row.name if row.name != "—" else "", ZERO) * 100 / payroll
                ).quantize(Decimal("0.1"))
                if payroll
                else ZERO
            ),
        )
        for row in overview.departments
    ]
    return HrOverview(
        active_count=overview.active_count,
        on_leave_count=overview.on_leave_count,
        total_count=overview.total_count,
        approved_contract_count=overview.approved_contract_count,
        draft_contract_count=overview.draft_contract_count,
        departments=departments,
        monthly_payroll=payroll,
        average_salary=(
            (payroll / overview.active_count).quantize(Decimal("1"))
            if overview.active_count
            else ZERO
        ),
    )
