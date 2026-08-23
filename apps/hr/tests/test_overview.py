"""
Contracts for the human-resources overview screen.

The screen's one hard promise: salary renders only as an aggregate, only for a
caller holding `hr.view_employee_salary`, and it is **absent** — not zero —
for everyone else. A zero payroll on a viewer's screen would read as "nobody
is paid here", which is a statement, not a redaction.

Payroll is also read from the APPROVED contract and nowhere else: a draft is
an intention, and an overview that counted intentions would disagree with the
payroll run built from the same table.
"""

from __future__ import annotations

import datetime
from datetime import time
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from apps.hr.dashboard import hr_overview
from apps.hr.models import (
    ContractType,
    Employee,
    EmployeeContract,
    EmployeePaymentMethod,
    PayrollPolicy,
    WageBasis,
)
from apps.hr.services import (
    approve_contract,
    create_contract,
    create_employee,
    default_policy_values,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_organization_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-for-tests-only"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM-HR-OV", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK-OV",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9),
    )


def _actor(username: str, organization: Organization, role: Role) -> User:
    user = User.objects.create_user(username=username, password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=role)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def maker(organization: Organization) -> User:
    return _actor("hr-ov-maker", organization, Role.MANAGER)


@pytest.fixture
def checker(organization: Organization) -> User:
    return _actor("hr-ov-checker", organization, Role.OWNER)


@pytest.fixture
def viewer(organization: Organization) -> User:
    """Reads the workspace; holds no salary permission."""
    return _actor("hr-ov-viewer", organization, Role.VIEWER)


@pytest.fixture
def policy(organization: Organization, maker: User) -> PayrollPolicy:
    return default_policy_values(organization=organization, actor=maker)


@pytest.fixture
def employee(organization: Organization, branch: Branch, maker: User) -> Employee:
    return create_employee(
        organization=organization,
        code="EMP-OV-1",
        name_ar="موظف اللوحة",
        name_en="Overview Employee",
        phone="07700000010",
        email="overview@example.test",
        identity_number="ID-OV-SECRET",
        date_of_birth=datetime.date(1992, 3, 4),
        gender="ذكر",
        marital_status="أعزب",
        address="بغداد",
        emergency_contact="07700000011",
        branch=branch,
        department="الصالة",
        job_title="نادل",
        workplace="فرع البنوك",
        hire_date=datetime.date(2026, 1, 1),
        payment_method=EmployeePaymentMethod.BANK,
        payment_reference="BANK-OV-SECRET",
        notes="",
        actor=maker,
    )


def _contract(
    *, employee: Employee, policy: PayrollPolicy, actor: User, salary: str = "750000.000"
) -> EmployeeContract:
    return create_contract(
        employee=employee,
        actor=actor,
        fixed_allowances=[],
        contract_type=ContractType.PERMANENT,
        start_date=datetime.date(2026, 1, 1),
        end_date=None,
        branch=employee.branch,
        job_title=employee.job_title,
        department=employee.department,
        wage_basis=WageBasis.MONTHLY,
        basic_salary=Decimal(salary),
        scheduled_work_days=Decimal("26.000"),
        scheduled_hours=Decimal("208.000"),
        probation_days=90,
        default_shift_code="DAY",
        payment_method=EmployeePaymentMethod.BANK,
        payroll_policy=policy,
        notes="",
    )


def test_payroll_counts_only_the_approved_contract(
    employee: Employee, policy: PayrollPolicy, maker: User, checker: User
) -> None:
    contract = _contract(employee=employee, policy=policy, actor=maker)

    # A draft is an intention: headcount shows, payroll does not.
    before = hr_overview(maker, include_salary=True)
    assert before.active_count == 1
    assert before.approved_contract_count == 0
    assert before.draft_contract_count == 1
    assert before.monthly_payroll == Decimal("0")

    approve_contract(contract=contract, actor=checker)

    after = hr_overview(maker, include_salary=True)
    assert after.approved_contract_count == 1
    assert after.monthly_payroll == Decimal("750000.000")
    assert after.average_salary == Decimal("750000")
    assert [(row.name, row.headcount, row.share) for row in after.departments] == [
        ("الصالة", 1, Decimal("100.0"))
    ]


def test_without_salary_rights_the_money_is_absent_not_zero(
    employee: Employee, policy: PayrollPolicy, maker: User, checker: User
) -> None:
    approve_contract(
        contract=_contract(employee=employee, policy=policy, actor=maker), actor=checker
    )

    redacted = hr_overview(maker, include_salary=False)

    # None, not Decimal("0") — the template tests `is not None` and drops the
    # card. Zero would claim nobody here is paid.
    assert redacted.monthly_payroll is None
    assert redacted.average_salary is None
    assert [row.monthly_salary for row in redacted.departments] == [None]
    # The org chart survives: who works where is not payroll.
    assert redacted.departments[0].headcount == 1
    assert redacted.active_count == 1


def test_the_viewer_screen_carries_no_salary_figure(
    employee: Employee, policy: PayrollPolicy, maker: User, checker: User, viewer: User
) -> None:
    approve_contract(
        contract=_contract(employee=employee, policy=policy, actor=maker, salary="937000.000"),
        actor=checker,
    )
    url = reverse("hr:overview")

    def _client(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    entitled = _client(maker).get(url).content.decode()
    assert "الرواتب الشهرية" in entitled
    assert "937,000" in entitled

    viewer_body = _client(viewer).get(url).content.decode()
    assert "الرواتب الشهرية" not in viewer_body
    assert "937,000" not in viewer_body
    # The headcount is an org chart, and the viewer's post covers it.
    assert "الملاك النشط" in viewer_body
