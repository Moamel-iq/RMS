"""Leave, overtime, deduction, and employee-advance acceptance tests."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.hr.attendance import calculate_attendance_day
from apps.hr.models import (
    AdvanceRecoveryAllocation,
    AdvanceStatus,
    AdvanceType,
    ContractType,
    DeductionAllocation,
    DeductionType,
    Employee,
    EmployeeDeduction,
    EmployeePaymentMethod,
    LeaveRequest,
    LeaveType,
    OvertimeRequest,
    OvertimeSource,
    PaidTreatment,
    PayrollPolicy,
    RecoveryMode,
    RequestStatus,
    Shift,
    WageBasis,
)
from apps.hr.services import (
    approve_advance,
    approve_contract,
    approve_deduction,
    approve_overtime_request,
    assign_shift,
    create_advance,
    create_contract,
    create_deduction,
    create_employee,
    create_leave_request,
    create_leave_type,
    create_overtime_request,
    create_shift,
    decide_leave_request,
    default_policy_values,
    submit_advance,
    submit_deduction,
    submit_leave_request,
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
BAGHDAD = ZoneInfo("Asia/Baghdad")
DAY = datetime.date(2026, 8, 10)


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM-OPS", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def other_organization() -> Organization:
    return create_organization(code="OTHER-OPS", name_ar="منافس", name_en="Other")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK-OPS",
        name_ar="البنوك",
        name_en="Al-Bunook",
        timezone="Asia/Baghdad",
        business_day_start_time=time(9),
    )


def _actor(username: str, organization: Organization, role: Role) -> User:
    user = User.objects.create_user(username=username, password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=role)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def maker(organization: Organization) -> User:
    return _actor("operations-maker", organization, Role.MANAGER)


@pytest.fixture
def checker(organization: Organization) -> User:
    return _actor("operations-checker", organization, Role.OWNER)


@pytest.fixture
def viewer(organization: Organization) -> User:
    return _actor("operations-viewer", organization, Role.VIEWER)


@pytest.fixture
def employee(organization: Organization, branch: Branch, maker: User) -> Employee:
    return create_employee(
        organization=organization,
        code="OPS-001",
        name_ar="موظف العمليات",
        name_en="Operations Employee",
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
    policy.overtime_multiplier = Decimal("1.500")
    policy.max_overtime_minutes = 120
    policy.save(update_fields=["overtime_multiplier", "max_overtime_minutes", "updated_at"])
    return policy


@pytest.fixture
def approved_contract(
    employee: Employee, policy: PayrollPolicy, maker: User, checker: User
) -> None:
    contract = create_contract(
        employee=employee,
        actor=maker,
        fixed_allowances=[],
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
    approve_contract(contract=contract, actor=checker)


@pytest.fixture
def shift(branch: Branch, maker: User) -> Shift:
    return create_shift(
        branch=branch,
        code="DAY",
        actor=maker,
        name_ar="الوردية الصباحية",
        name_en="Day",
        start_time=time(9),
        end_time=time(17),
        crosses_midnight=False,
        scheduled_minutes=480,
        break_minutes=60,
        grace_minutes=5,
        late_threshold_minutes=1,
        early_departure_threshold_minutes=5,
        effective_from=datetime.date(2026, 1, 1),
        effective_to=None,
        is_active=True,
        notes="",
    )


@pytest.fixture
def assigned(employee: Employee, shift: Shift, maker: User) -> None:
    assign_shift(
        employee=employee,
        shift=shift,
        effective_from=datetime.date(2026, 1, 1),
        effective_to=None,
        rotation_code="",
        notes="",
        actor=maker,
    )


@pytest.fixture
def leave_type(organization: Organization, maker: User) -> LeaveType:
    return create_leave_type(
        organization=organization,
        actor=maker,
        code="ANNUAL",
        name_ar="إجازة سنوية",
        name_en="Annual leave",
        paid_treatment=PaidTreatment.PAID,
        requires_evidence=False,
        is_active=True,
        notes="",
    )


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login


def _leave(
    *, employee: Employee, leave_type: LeaveType, actor: User, start_hour: int = 9
) -> LeaveRequest:
    return create_leave_request(
        employee=employee,
        leave_type=leave_type,
        start_at=datetime.datetime(2026, 8, 10, start_hour, tzinfo=BAGHDAD),
        end_at=datetime.datetime(2026, 8, 10, 17, tzinfo=BAGHDAD),
        reason="إجازة مخططة",
        evidence_reference="",
        evidence_file=None,
        actor=actor,
    )


def test_leave_is_maker_checker_non_overlapping_and_changes_attendance_projection(
    employee: Employee,
    leave_type: LeaveType,
    maker: User,
    checker: User,
    assigned: None,
) -> None:
    request = _leave(employee=employee, leave_type=leave_type, actor=maker)
    submit_leave_request(request=request, actor=maker)
    with pytest.raises(ValidationError, match="creator cannot approve"):
        decide_leave_request(request=request, approve=True, reason="", actor=maker)

    approved = decide_leave_request(request=request, approve=True, reason="", actor=checker)
    result = calculate_attendance_day(employee=employee, business_date=DAY)
    assert approved.status == RequestStatus.APPROVED
    assert result.status == "APPROVED_PAID_LEAVE"
    assert result.absence_candidate is False

    overlap = _leave(employee=employee, leave_type=leave_type, actor=maker, start_hour=10)
    submit_leave_request(request=overlap, actor=maker)
    with pytest.raises(ValidationError, match="may not overlap"):
        decide_leave_request(request=overlap, approve=True, reason="", actor=checker)
    with pytest.raises(DatabaseError), transaction.atomic():
        LeaveRequest.objects.filter(pk=overlap.pk).update(reason="إعادة كتابة بعد الإرسال")

    with pytest.raises(DatabaseError), transaction.atomic():
        LeaveRequest.objects.create(
            organization=employee.organization,
            employee=employee,
            leave_type=leave_type,
            start_at=datetime.datetime(2026, 8, 10, 12, tzinfo=BAGHDAD),
            end_at=datetime.datetime(2026, 8, 10, 13, tzinfo=BAGHDAD),
            requested_minutes=60,
            paid_treatment=PaidTreatment.PAID,
            reason="تداخل مباشر",
            status=RequestStatus.APPROVED,
            requested_by=maker,
            approved_by=checker,
            approved_at=timezone.now(),
        )


def test_leave_type_can_require_evidence(
    employee: Employee, organization: Organization, maker: User
) -> None:
    medical = create_leave_type(
        organization=organization,
        actor=maker,
        code="MEDICAL",
        name_ar="إجازة مرضية",
        name_en="Medical",
        paid_treatment=PaidTreatment.POLICY,
        requires_evidence=True,
        is_active=True,
        notes="",
    )
    with pytest.raises(ValidationError, match="requires evidence"):
        _leave(employee=employee, leave_type=medical, actor=maker)


def test_overtime_snapshots_policy_cap_and_requires_a_different_approver(
    employee: Employee,
    maker: User,
    checker: User,
    approved_contract: None,
    assigned: None,
) -> None:
    def create(minutes: int) -> OvertimeRequest:
        return create_overtime_request(
            employee=employee,
            business_date=DAY,
            requested_minutes=minutes,
            source=OvertimeSource.REQUESTED,
            classification="يوم عمل",
            reason="إغلاق متأخر",
            evidence_reference="OPS-OT-1",
            evidence_file=None,
            actor=maker,
        )

    with pytest.raises(ValidationError, match="exceeds"):
        create(121)
    overtime = create(90)
    submit_overtime_request(overtime=overtime, actor=maker)
    with pytest.raises(ValidationError, match="creator cannot approve"):
        approve_overtime_request(overtime=overtime, approved_minutes=80, actor=maker)
    approved = approve_overtime_request(overtime=overtime, approved_minutes=80, actor=checker)
    assert approved.status == RequestStatus.APPROVED
    assert approved.approved_minutes == 80
    assert approved.multiplier == Decimal("1.500")
    with pytest.raises(DatabaseError), transaction.atomic():
        type(approved).objects.filter(pk=approved.pk).update(reason="إعادة كتابة")


def test_deduction_requires_evidence_tracks_remaining_and_cannot_over_allocate(
    employee: Employee, maker: User, checker: User
) -> None:
    def create(evidence_reference: str) -> EmployeeDeduction:
        return create_deduction(
            employee=employee,
            deduction_type=DeductionType.ADMINISTRATIVE,
            original_amount=Decimal("1000.000"),
            effective_period=datetime.date(2026, 8, 1),
            recovery_mode=RecoveryMode.INSTALMENTS,
            instalment_count=2,
            evidence_reference=evidence_reference,
            evidence_file=None,
            reason="استقطاع مثبت",
            actor=maker,
        )

    with pytest.raises(ValidationError, match="evidence"):
        create("")
    deduction = create("DOC-DED-1")
    submit_deduction(deduction=deduction, actor=maker)
    with pytest.raises(ValidationError, match="creator cannot approve"):
        approve_deduction(deduction=deduction, approved_amount=Decimal("700.000"), actor=maker)
    approved = approve_deduction(
        deduction=deduction, approved_amount=Decimal("700.000"), actor=checker
    )
    allocation = DeductionAllocation.objects.create(
        deduction=approved,
        payroll_reference="PAY-2026-08",
        amount=Decimal("400.000"),
        allocated_at=timezone.now(),
    )
    assert approved.remaining_amount == Decimal("300.000")
    with pytest.raises(DatabaseError), transaction.atomic():
        DeductionAllocation.objects.create(
            deduction=approved,
            payroll_reference="PAY-2026-09",
            amount=Decimal("400.000"),
            allocated_at=timezone.now(),
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        DeductionAllocation.objects.filter(pk=allocation.pk).update(amount=Decimal("1.000"))


def test_advance_approval_is_not_disbursement_and_recovery_cannot_precede_cash(
    employee: Employee, maker: User, checker: User
) -> None:
    advance = create_advance(
        employee=employee,
        advance_type=AdvanceType.SALARY_ADVANCE,
        principal_amount=Decimal("500000.000"),
        request_date=DAY,
        recovery_mode=RecoveryMode.INSTALMENTS,
        instalment_amount=Decimal("100000.000"),
        instalment_count=5,
        first_recovery_period=datetime.date(2026, 9, 1),
        payment_method=EmployeePaymentMethod.CASH,
        evidence_reference="ADV-1",
        reason="سلفة راتب",
        actor=maker,
    )
    submit_advance(advance=advance, actor=maker)
    with pytest.raises(ValidationError, match="creator cannot approve"):
        approve_advance(advance=advance, actor=maker)
    approved = approve_advance(advance=advance, actor=checker)
    assert approved.status == AdvanceStatus.APPROVED
    assert approved.disbursed_amount == Decimal("0.000")
    assert approved.recovered_amount == Decimal("0.000")
    assert approved.outstanding_amount == Decimal("0.000")
    with pytest.raises(DatabaseError), transaction.atomic():
        AdvanceRecoveryAllocation.objects.create(
            advance=approved,
            payroll_reference="PAY-2026-09",
            amount=Decimal("100000.000"),
            recovered_at=timezone.now(),
        )


def test_all_operation_screens_have_arabic_full_page_and_htmx_fallbacks(
    employee: Employee,
    leave_type: LeaveType,
    maker: User,
    other_organization: Organization,
    viewer: User,
    approved_contract: None,
    assigned: None,
    client_for: Callable[[User], Client],
) -> None:
    leave = _leave(employee=employee, leave_type=leave_type, actor=maker)
    overtime = create_overtime_request(
        employee=employee,
        business_date=DAY,
        requested_minutes=60,
        source=OvertimeSource.REQUESTED,
        classification="يوم عمل",
        reason="اختبار الشاشة",
        evidence_reference="OT-SCREEN",
        evidence_file=None,
        actor=maker,
    )
    deduction = create_deduction(
        employee=employee,
        deduction_type=DeductionType.ADMINISTRATIVE,
        original_amount=Decimal("100.000"),
        effective_period=datetime.date(2026, 8, 1),
        recovery_mode=RecoveryMode.ONE_TIME,
        instalment_count=1,
        evidence_reference="DED-SCREEN",
        evidence_file=None,
        reason="اختبار الشاشة",
        actor=maker,
    )
    advance = create_advance(
        employee=employee,
        advance_type=AdvanceType.SALARY_ADVANCE,
        principal_amount=Decimal("1000.000"),
        request_date=DAY,
        recovery_mode=RecoveryMode.ONE_TIME,
        instalment_amount=Decimal("1000.000"),
        instalment_count=1,
        first_recovery_period=datetime.date(2026, 9, 1),
        payment_method=EmployeePaymentMethod.CASH,
        evidence_reference="ADV-SCREEN",
        reason="اختبار الشاشة",
        actor=maker,
    )
    client = client_for(maker)
    for name in (
        "leave_list",
        "leave_approvals",
        "leave_calendar",
        "leave_types",
        "absence_list",
        "overtime_list",
        "deduction_list",
        "advance_list",
    ):
        response = client.get(reverse(f"hr:{name}"), {"date": DAY.isoformat()})
        assert response.status_code == 200, name
        assert "الموارد البشرية" in response.content.decode(), name

    for name in ("leave_create", "overtime_create", "deduction_create", "advance_create"):
        response = client.get(reverse(f"hr:{name}"), HTTP_HX_REQUEST="true")
        content = response.content.decode()
        assert response.status_code == 200, name
        assert "<html" not in content.lower(), name
        assert f'hx-post="{reverse(f"hr:{name}")}"' in content, name

    for name, obj in (
        ("leave_detail", leave),
        ("overtime_detail", overtime),
        ("deduction_detail", deduction),
        ("advance_detail", advance),
    ):
        detail = client.get(reverse(f"hr:{name}", args=[obj.pk]))
        assert detail.status_code == 200, name
        assert employee.name_ar in detail.content.decode(), name
    assert (
        leave_type.name_ar
        in client.get(reverse("hr:leave_detail", args=[leave.pk])).content.decode()
    )
    assert client_for(viewer).get(reverse("hr:leave_list")).status_code == 403
    outsider = _actor("operations-outsider", other_organization, Role.MANAGER)
    assert client_for(outsider).get(reverse("hr:leave_detail", args=[leave.pk])).status_code == 404
