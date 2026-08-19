"""Payroll calculation, review, approval, and workspace acceptance tests."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    EMPLOYEE_RECEIVABLE,
    PAYROLL_ALLOWANCE_EXPENSE,
    PAYROLL_BANK,
    PAYROLL_CASH,
    PAYROLL_OTHER_LIABILITY,
    PAYROLL_OVERTIME_EXPENSE,
    PAYROLL_PAYABLE,
    PAYROLL_SALARY_EXPENSE,
    Account,
    AccountRole,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.hr.models import (
    AdvanceDisbursement,
    AdvanceType,
    AttendanceEventSource,
    AttendanceEventType,
    ContractType,
    DeductionType,
    Employee,
    EmployeeContract,
    EmployeePaymentMethod,
    OvertimeSource,
    PayrollPayment,
    PayrollPolicy,
    PayrollRun,
    PayrollRunStatus,
    ProrationBasis,
    RecoveryMode,
    ShiftAssignment,
    WageBasis,
)
from apps.hr.payroll import (
    approve_payroll_run,
    calculate_payroll_run,
    create_payroll_payment,
    create_payroll_run,
    post_payroll_run,
    release_payroll_run,
    reverse_payroll_payment,
    reverse_payroll_run,
    review_payroll_run,
)
from apps.hr.services import (
    approve_advance,
    approve_attendance_day,
    approve_contract,
    approve_deduction,
    approve_overtime_request,
    assign_shift,
    create_advance,
    create_contract,
    create_deduction,
    create_employee,
    create_overtime_request,
    create_shift,
    default_policy_values,
    record_attendance_event,
    submit_advance,
    submit_deduction,
    submit_overtime_request,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_organization_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"
DAY = datetime.date(2026, 8, 10)
BAGHDAD = ZoneInfo("Asia/Baghdad")


@pytest.fixture
def organization() -> Organization:
    organization = create_organization(code="KM-PAY", name_ar="خان مندي", name_en="Khan Mandi")
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=DAY.year)
    return organization


@pytest.fixture
def other_organization() -> Organization:
    return create_organization(code="OTHER-PAY", name_ar="منافس", name_en="Other")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK-PAY",
        name_ar="البنوك",
        name_en="Al-Bunook",
        timezone="Asia/Baghdad",
        business_day_start_time=time(9),
    )


@pytest.fixture
def payroll_account_mappings(organization: Organization) -> None:
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)
    role_accounts = {
        PAYROLL_SALARY_EXPENSE: "6-01-01-001",
        PAYROLL_ALLOWANCE_EXPENSE: "6-01-01-001",
        PAYROLL_OVERTIME_EXPENSE: "6-01-01-001",
        PAYROLL_PAYABLE: "2-02-01-001",
        EMPLOYEE_RECEIVABLE: "1-04-02-001",
        PAYROLL_OTHER_LIABILITY: "2-02-01-001",
        PAYROLL_CASH: "1-01-01-001",
        PAYROLL_BANK: "1-01-02-001",
    }
    accounts = {
        account.code: account
        for account in Account.objects.filter(
            organization=organization, code__in=set(role_accounts.values())
        )
    }
    for role_code, account_code in role_accounts.items():
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=role_code),
            account=accounts[account_code],
            effective_from=datetime.date(DAY.year, 1, 1),
        )


def _actor(username: str, organization: Organization, role: Role) -> User:
    user = User.objects.create_user(username=username, password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=role)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def maker(organization: Organization) -> User:
    # The maker needs approval authority so this fixture can independently
    # exercise the service-level maker/checker guard. Role-matrix coverage
    # verifies separately that ordinary managers do not receive that authority.
    return _actor("payroll-maker", organization, Role.OWNER)


@pytest.fixture
def reviewer(organization: Organization) -> User:
    return _actor("payroll-reviewer", organization, Role.MANAGER)


@pytest.fixture
def checker(organization: Organization) -> User:
    return _actor("payroll-checker", organization, Role.OWNER)


@pytest.fixture
def viewer(organization: Organization) -> User:
    return _actor("payroll-viewer", organization, Role.VIEWER)


@pytest.fixture
def employee(organization: Organization, branch: Branch, maker: User) -> Employee:
    return create_employee(
        organization=organization,
        code="PAY-001",
        name_ar="موظف الرواتب",
        name_en="Payroll Employee",
        phone="",
        email="",
        identity_number="",
        date_of_birth=None,
        gender="",
        marital_status="",
        address="",
        emergency_contact="",
        branch=branch,
        department="العمليات",
        job_title="مشرف",
        workplace="فرع البنوك",
        hire_date=datetime.date(2025, 1, 1),
        payment_method=EmployeePaymentMethod.CASH,
        payment_reference="",
        notes="",
        actor=maker,
    )


@pytest.fixture
def policy(organization: Organization, maker: User) -> PayrollPolicy:
    policy = default_policy_values(organization=organization, actor=maker)
    policy.proration_basis = ProrationBasis.SCHEDULED_WORKDAY
    policy.overtime_multiplier = Decimal("1.500")
    policy.max_overtime_minutes = 120
    policy.deduction_cap_percentage = Decimal("100.000")
    policy.save()
    return policy


@pytest.fixture
def contract(
    employee: Employee, policy: PayrollPolicy, maker: User, checker: User
) -> EmployeeContract:
    draft = create_contract(
        employee=employee,
        actor=maker,
        fixed_allowances=[{"name": "بدل نقل", "amount": "50000.000"}],
        contract_type=ContractType.PERMANENT,
        start_date=datetime.date(2026, 1, 1),
        end_date=None,
        branch=employee.branch,
        job_title=employee.job_title,
        department=employee.department,
        wage_basis=WageBasis.MONTHLY,
        basic_salary=Decimal("1000000.000"),
        scheduled_work_days=Decimal("26.000"),
        scheduled_hours=Decimal("208.000"),
        probation_days=0,
        default_shift_code="DAY",
        payment_method=EmployeePaymentMethod.CASH,
        payroll_policy=policy,
        notes="",
    )
    return approve_contract(contract=draft, actor=checker)


@pytest.fixture
def assigned(employee: Employee, branch: Branch, maker: User) -> ShiftAssignment:
    shift = create_shift(
        branch=branch,
        code="DAY",
        actor=maker,
        name_ar="صباحية",
        name_en="Day",
        start_time=time(9),
        end_time=time(17),
        crosses_midnight=False,
        scheduled_minutes=480,
        break_minutes=0,
        grace_minutes=0,
        late_threshold_minutes=1,
        early_departure_threshold_minutes=1,
        effective_from=datetime.date(2026, 1, 1),
        effective_to=None,
        is_active=True,
        notes="",
    )
    return assign_shift(
        employee=employee,
        shift=shift,
        effective_from=datetime.date(2026, 1, 1),
        effective_to=None,
        rotation_code="",
        notes="",
        actor=maker,
    )


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login


def _run(branch: Branch, policy: PayrollPolicy, actor: User) -> PayrollRun:
    return create_payroll_run(
        branch=branch,
        period_start=DAY,
        period_end=DAY,
        accounting_date=DAY,
        policy=policy,
        notes="",
        actor=actor,
    )


def _approve_day(employee: Employee, branch: Branch, maker: User, reviewer: User) -> None:
    for event_type, occurred_at in (
        (AttendanceEventType.CHECK_IN, datetime.datetime(2026, 8, 10, 9, tzinfo=BAGHDAD)),
        (AttendanceEventType.CHECK_OUT, datetime.datetime(2026, 8, 10, 17, tzinfo=BAGHDAD)),
    ):
        record_attendance_event(
            employee=employee,
            branch=branch,
            business_date=DAY,
            occurred_at=occurred_at,
            event_type=event_type,
            source=AttendanceEventSource.MANUAL,
            device_reference="",
            notes="",
            actor=maker,
        )
    approve_attendance_day(employee=employee, business_date=DAY, actor=reviewer)


def test_calculation_snapshots_approved_inputs_and_is_idempotent(
    employee: Employee,
    branch: Branch,
    policy: PayrollPolicy,
    contract: EmployeeContract,
    assigned: ShiftAssignment,
    maker: User,
    reviewer: User,
    checker: User,
    payroll_account_mappings: None,
    client_for: Callable[[User], Client],
) -> None:
    _approve_day(employee, branch, maker, reviewer)
    overtime = create_overtime_request(
        employee=employee,
        business_date=DAY,
        requested_minutes=60,
        source=OvertimeSource.REQUESTED,
        classification="يوم عمل",
        reason="إغلاق متأخر",
        evidence_reference="OT-PAY-1",
        evidence_file=None,
        actor=maker,
    )
    submit_overtime_request(overtime=overtime, actor=maker)
    approve_overtime_request(overtime=overtime, approved_minutes=60, actor=checker)
    deduction = create_deduction(
        employee=employee,
        deduction_type=DeductionType.ADMINISTRATIVE,
        original_amount=Decimal("100000.000"),
        effective_period=DAY.replace(day=1),
        recovery_mode=RecoveryMode.ONE_TIME,
        instalment_count=1,
        evidence_reference="DED-PAY-1",
        evidence_file=None,
        reason="استقطاع مثبت",
        actor=maker,
    )
    submit_deduction(deduction=deduction, actor=maker)
    approve_deduction(deduction=deduction, approved_amount=Decimal("100000.000"), actor=checker)
    advance = create_advance(
        employee=employee,
        advance_type=AdvanceType.SALARY_ADVANCE,
        principal_amount=Decimal("200000.000"),
        request_date=DAY,
        recovery_mode=RecoveryMode.INSTALMENTS,
        instalment_amount=Decimal("50000.000"),
        instalment_count=4,
        first_recovery_period=DAY.replace(day=1),
        payment_method=EmployeePaymentMethod.CASH,
        evidence_reference="ADV-PAY-1",
        reason="سلفة راتب",
        actor=maker,
    )
    submit_advance(advance=advance, actor=maker)
    approve_advance(advance=advance, actor=checker)
    AdvanceDisbursement.objects.create(
        advance=advance,
        amount=Decimal("200000.000"),
        disbursement_date=DAY,
        payment_method=EmployeePaymentMethod.CASH,
        evidence_reference="ADV-DISB-1",
        created_by=checker,
    )

    run = calculate_payroll_run(payroll_run=_run(branch, policy, maker), actor=maker)
    line = run.employee_lines.get(employee=employee)
    assert line.basic_pay == Decimal("1000000.000")
    assert line.fixed_allowances == Decimal("50000.000")
    assert line.overtime_pay == Decimal("187500.000")
    assert line.administrative_deduction == Decimal("100000.000")
    assert line.advance_recovery == Decimal("50000.000")
    assert line.gross_pay == Decimal("1237500.000")
    assert line.total_deductions == Decimal("150000.000")
    assert line.net_pay == Decimal("1087500.000")
    first_components = line.components.count()

    recalculated = calculate_payroll_run(payroll_run=run, actor=maker)
    current = recalculated.employee_lines.get(employee=employee)
    assert current.net_pay == Decimal("1087500.000")
    assert current.components.count() == first_components

    reviewed = review_payroll_run(payroll_run=recalculated, actor=reviewer)
    with pytest.raises(ValidationError, match="creator or calculator"):
        approve_payroll_run(payroll_run=reviewed, actor=maker)
    approved = approve_payroll_run(payroll_run=reviewed, actor=checker)
    assert approved.status == PayrollRunStatus.APPROVED
    with pytest.raises(DatabaseError), transaction.atomic():
        approved.employee_lines.update(net_pay=Decimal("1.000"))

    posted = post_payroll_run(payroll_run=approved, actor=checker)
    assert posted.status == PayrollRunStatus.POSTED
    assert posted.accrual_journal is not None
    assert sum((row.debit for row in posted.accrual_journal.lines.all()), Decimal("0")) == Decimal(
        "1237500.000"
    )
    assert sum((row.credit for row in posted.accrual_journal.lines.all()), Decimal("0")) == Decimal(
        "1237500.000"
    )
    assert posted.deduction_allocations.count() == 1
    assert posted.advance_recovery_allocations.count() == 1
    deduction.refresh_from_db()
    advance.refresh_from_db()
    overtime.refresh_from_db()
    assert deduction.remaining_amount == Decimal("0.000")
    assert advance.outstanding_amount == Decimal("150000.000")
    assert overtime.payroll_inclusion_reference == str(posted.public_id)

    released = release_payroll_run(payroll_run=posted, actor=checker)
    assert released.status == PayrollRunStatus.RELEASED
    payment_form = client_for(checker).get(
        reverse("hr:payroll_payment_create", args=[released.pk]), HTTP_HX_REQUEST="true"
    )
    assert payment_form.status_code == 200
    assert "<html" not in payment_form.content.decode().lower()
    assert "تخصيص الموظفين" in payment_form.content.decode()
    employee_line = released.employee_lines.get(employee=employee)
    payment_date = datetime.date(2026, 8, 20)
    partial = create_payroll_payment(
        payroll_run=released,
        payment_date=payment_date,
        method=EmployeePaymentMethod.CASH,
        reference="CASH-PAY-1",
        reason="صرف جزئي",
        allocations=[(employee_line, Decimal("400000.000"))],
        idempotency_key="payroll-partial-1",
        actor=checker,
    )
    released.refresh_from_db()
    assert released.status == PayrollRunStatus.PARTIALLY_PAID
    assert released.paid_total == Decimal("400000.000")
    replay = create_payroll_payment(
        payroll_run=released,
        payment_date=payment_date,
        method=EmployeePaymentMethod.CASH,
        reference="CASH-PAY-1",
        reason="صرف جزئي",
        allocations=[(employee_line, Decimal("400000.000"))],
        idempotency_key="payroll-partial-1",
        actor=checker,
    )
    assert replay.pk == partial.pk

    final = create_payroll_payment(
        payroll_run=released,
        payment_date=payment_date,
        method=EmployeePaymentMethod.BANK,
        reference="BANK-PAY-1",
        reason="إكمال الصرف",
        allocations=[(employee_line, Decimal("687500.000"))],
        idempotency_key="payroll-final-1",
        actor=checker,
    )
    released.refresh_from_db()
    assert released.status == PayrollRunStatus.PAID
    assert released.outstanding_total == Decimal("0.000")
    assert PayrollPayment.objects.filter(payroll_run=released).count() == 2

    reverse_payroll_payment(
        payment=final,
        reversal_date=payment_date,
        reason="اختبار عكس التحويل",
        actor=checker,
    )
    reverse_payroll_payment(
        payment=partial,
        reversal_date=payment_date,
        reason="اختبار عكس النقد",
        actor=checker,
    )
    released.refresh_from_db()
    assert released.status == PayrollRunStatus.RELEASED
    assert released.paid_total == Decimal("0.000")

    reversed_run = reverse_payroll_run(
        payroll_run=released,
        reversal_date=payment_date,
        reason="تصحيح تشغيل الرواتب",
        actor=checker,
    )
    assert reversed_run.status == PayrollRunStatus.REVERSED
    deduction.refresh_from_db()
    advance.refresh_from_db()
    overtime.refresh_from_db()
    assert deduction.remaining_amount == Decimal("100000.000")
    assert advance.outstanding_amount == Decimal("200000.000")
    assert overtime.payroll_inclusion_reference == ""


def test_unapproved_attendance_is_a_blocker_at_approval(
    employee: Employee,
    branch: Branch,
    policy: PayrollPolicy,
    contract: EmployeeContract,
    assigned: ShiftAssignment,
    maker: User,
    reviewer: User,
    checker: User,
) -> None:
    run = calculate_payroll_run(payroll_run=_run(branch, policy, maker), actor=maker)
    assert run.warning_count >= 1
    reviewed = review_payroll_run(payroll_run=run, actor=reviewer)
    with pytest.raises(ValidationError, match="blockers"):
        approve_payroll_run(payroll_run=reviewed, actor=checker)


def test_payroll_workspaces_are_scope_safe_arabic_and_htmx_enabled(
    branch: Branch,
    policy: PayrollPolicy,
    maker: User,
    viewer: User,
    other_organization: Organization,
    client_for: Callable[[User], Client],
) -> None:
    run = _run(branch, policy, maker)
    client = client_for(maker)
    listing = client.get(reverse("hr:payroll_list"))
    payment_listing = client.get(reverse("hr:payroll_payments"))
    form = client.get(reverse("hr:payroll_create"), HTTP_HX_REQUEST="true")
    detail = client.get(reverse("hr:payroll_detail", args=[run.pk]))
    assert listing.status_code == 200
    assert "احتساب الرواتب" in listing.content.decode()
    assert payment_listing.status_code == 200
    assert "صرف الرواتب" in payment_listing.content.decode()
    assert detail.status_code == 200
    assert run.run_number in detail.content.decode()
    assert form.status_code == 200
    assert "<html" not in form.content.decode().lower()
    assert f'hx-post="{reverse("hr:payroll_create")}"' in form.content.decode()
    assert client_for(viewer).get(reverse("hr:payroll_list")).status_code == 403
    outsider = _actor("payroll-outsider", other_organization, Role.MANAGER)
    assert client_for(outsider).get(reverse("hr:payroll_detail", args=[run.pk])).status_code == 404
