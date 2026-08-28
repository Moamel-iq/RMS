"""Shift scheduling and append-only attendance acceptance tests."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from datetime import time
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.test import Client
from django.urls import reverse

from apps.hr.attendance import calculate_attendance_day, effective_events
from apps.hr.models import (
    AttendanceApprovalStatus,
    AttendanceEvent,
    AttendanceEventSource,
    AttendanceEventType,
    Employee,
    EmployeePaymentMethod,
    Shift,
    ShiftAssignment,
)
from apps.hr.services import (
    approve_attendance_day,
    assign_shift,
    correct_attendance_event,
    create_employee,
    create_shift,
    record_attendance_event,
    reopen_attendance_day,
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
DAY = datetime.date(2026, 8, 1)


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM-ATT", name="خان مندي")


@pytest.fixture
def other_organization() -> Organization:
    return create_organization(code="OTHER-ATT", name="منافس")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK-ATT",
        name="البنوك",
        timezone="Asia/Baghdad",
        business_day_start_time=time(9),
    )


def _actor(username: str, organization: Organization, role: Role = Role.MANAGER) -> User:
    user = User.objects.create_user(username=username, password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=role)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def manager(organization: Organization) -> User:
    return _actor("attendance-manager", organization)


@pytest.fixture
def viewer(organization: Organization) -> User:
    return _actor("attendance-viewer", organization, Role.VIEWER)


@pytest.fixture
def employee(organization: Organization, branch: Branch, manager: User) -> Employee:
    return create_employee(
        organization=organization,
        code="ATT-001",
        name="موظف الحضور",
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
        actor=manager,
    )


def _shift(
    *,
    branch: Branch,
    actor: User,
    code: str = "NIGHT",
    start: time = time(22),
    end: time = time(6),
    scheduled_minutes: int = 420,
) -> Shift:
    return create_shift(
        branch=branch,
        code=code,
        actor=actor,
        name="الوردية الليلية" if end <= start else "الوردية الصباحية",
        start_time=start,
        end_time=end,
        crosses_midnight=end <= start,
        scheduled_minutes=scheduled_minutes,
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
def night_shift(branch: Branch, manager: User) -> Shift:
    return _shift(branch=branch, actor=manager)


@pytest.fixture
def assigned(employee: Employee, night_shift: Shift, manager: User) -> ShiftAssignment:
    return assign_shift(
        employee=employee,
        shift=night_shift,
        effective_from=datetime.date(2026, 1, 1),
        effective_to=None,
        rotation_code="A-B",
        notes="",
        actor=manager,
    )


def _event(
    *,
    employee: Employee,
    branch: Branch,
    actor: User,
    occurred_at: datetime.datetime,
    event_type: str,
    reference: str = "",
) -> AttendanceEvent:
    return record_attendance_event(
        employee=employee,
        branch=branch,
        business_date=DAY,
        occurred_at=occurred_at,
        event_type=event_type,
        source=AttendanceEventSource.MANUAL,
        device_reference=reference,
        notes="",
        actor=actor,
    )


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login


def test_cross_midnight_projection_keeps_the_schedule_business_date(
    employee: Employee,
    branch: Branch,
    manager: User,
    assigned: ShiftAssignment,
) -> None:
    _event(
        employee=employee,
        branch=branch,
        actor=manager,
        occurred_at=datetime.datetime(2026, 8, 1, 22, 8, tzinfo=BAGHDAD),
        event_type=AttendanceEventType.CHECK_IN,
    )
    _event(
        employee=employee,
        branch=branch,
        actor=manager,
        occurred_at=datetime.datetime(2026, 8, 2, 6, 8, tzinfo=BAGHDAD),
        event_type=AttendanceEventType.CHECK_OUT,
    )
    result = calculate_attendance_day(employee=employee, business_date=DAY)

    assert result.scheduled_start == datetime.datetime(2026, 8, 1, 22, tzinfo=BAGHDAD)
    assert result.scheduled_end == datetime.datetime(2026, 8, 2, 6, tzinfo=BAGHDAD)
    assert result.worked_minutes == 480
    assert result.lateness_minutes == 3
    assert result.overtime_candidate_minutes == 60
    assert result.status == "LATE"


def test_assignment_and_shift_versions_cannot_overlap(
    employee: Employee,
    branch: Branch,
    manager: User,
    night_shift: Shift,
    assigned: ShiftAssignment,
) -> None:
    with pytest.raises(ValidationError, match="assignments may not overlap"):
        assign_shift(
            employee=employee,
            shift=night_shift,
            effective_from=DAY,
            effective_to=None,
            rotation_code="",
            notes="",
            actor=manager,
        )
    with pytest.raises(ValidationError, match="versions with the same code may not overlap"):
        _shift(branch=branch, actor=manager, code="NIGHT")


def test_database_freezes_used_shift_and_attendance_events_are_append_only(
    employee: Employee,
    branch: Branch,
    manager: User,
    night_shift: Shift,
    assigned: ShiftAssignment,
) -> None:
    event = _event(
        employee=employee,
        branch=branch,
        actor=manager,
        occurred_at=datetime.datetime(2026, 8, 1, 22, tzinfo=BAGHDAD),
        event_type=AttendanceEventType.CHECK_IN,
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        Shift.objects.filter(pk=night_shift.pk).update(start_time=time(21))
    with pytest.raises(DatabaseError), transaction.atomic():
        AttendanceEvent.objects.filter(pk=event.pk).update(notes="rewritten")
    with pytest.raises(DatabaseError), transaction.atomic():
        AttendanceEvent.objects.filter(pk=event.pk).delete()


def test_correction_preserves_original_and_projection_uses_only_the_new_leaf(
    employee: Employee,
    branch: Branch,
    manager: User,
    assigned: ShiftAssignment,
) -> None:
    original = _event(
        employee=employee,
        branch=branch,
        actor=manager,
        occurred_at=datetime.datetime(2026, 8, 1, 22, 30, tzinfo=BAGHDAD),
        event_type=AttendanceEventType.CHECK_IN,
    )
    replacement = correct_attendance_event(
        event=original,
        business_date=DAY,
        occurred_at=datetime.datetime(2026, 8, 1, 22, 3, tzinfo=BAGHDAD),
        event_type=AttendanceEventType.CHECK_IN,
        reason="مطابقة سجل الباب",
        notes="",
        actor=manager,
    )

    assert AttendanceEvent.objects.filter(pk=original.pk).exists()
    assert replacement.supersedes == original
    assert replacement.correction_reason == "مطابقة سجل الباب"
    assert list(effective_events(employee, DAY)) == [replacement]
    with pytest.raises(ValidationError, match="already been superseded"):
        correct_attendance_event(
            event=original,
            business_date=DAY,
            occurred_at=replacement.occurred_at,
            event_type=AttendanceEventType.CHECK_IN,
            reason="محاولة قديمة",
            notes="",
            actor=manager,
        )


def test_approved_day_blocks_correction_until_a_reasoned_reopen(
    employee: Employee,
    branch: Branch,
    manager: User,
    assigned: ShiftAssignment,
) -> None:
    event = _event(
        employee=employee,
        branch=branch,
        actor=manager,
        occurred_at=datetime.datetime(2026, 8, 1, 22, tzinfo=BAGHDAD),
        event_type=AttendanceEventType.CHECK_IN,
    )
    approval = approve_attendance_day(employee=employee, business_date=DAY, actor=manager)
    assert approval.status == AttendanceApprovalStatus.APPROVED
    assert approval.result_snapshot["status"] == "MISSING_PUNCH"
    with pytest.raises(ValidationError, match="Reopen"):
        correct_attendance_event(
            event=event,
            business_date=DAY,
            occurred_at=event.occurred_at,
            event_type=AttendanceEventType.CHECK_IN,
            reason="تصحيح",
            notes="",
            actor=manager,
        )
    reopened = reopen_attendance_day(
        employee=employee, business_date=DAY, reason="ورد سجل الخروج", actor=manager
    )
    assert reopened.status == AttendanceApprovalStatus.REOPENED
    assert reopened.approved_by is None


def test_import_reference_is_idempotent_and_a_conflicting_retry_is_rejected(
    employee: Employee,
    branch: Branch,
    manager: User,
    assigned: ShiftAssignment,
) -> None:
    moment = datetime.datetime(2026, 8, 1, 22, tzinfo=BAGHDAD)
    first = _event(
        employee=employee,
        branch=branch,
        actor=manager,
        occurred_at=moment,
        event_type=AttendanceEventType.CHECK_IN,
        reference="DEVICE-ATT-001",
    )
    retry = _event(
        employee=employee,
        branch=branch,
        actor=manager,
        occurred_at=moment,
        event_type=AttendanceEventType.CHECK_IN,
        reference="DEVICE-ATT-001",
    )
    assert retry.pk == first.pk
    with pytest.raises(ValidationError, match="already used with different values"):
        _event(
            employee=employee,
            branch=branch,
            actor=manager,
            occurred_at=moment + datetime.timedelta(minutes=1),
            event_type=AttendanceEventType.CHECK_IN,
            reference="DEVICE-ATT-001",
        )


def test_scope_and_capability_boundaries_return_404_and_403(
    night_shift: Shift,
    other_organization: Organization,
    viewer: User,
    client_for: Callable[[User], Client],
) -> None:
    outsider = _actor("attendance-outsider", other_organization)
    assert (
        client_for(outsider).get(reverse("hr:shift_update", args=[night_shift.pk])).status_code
        == 404
    )
    assert client_for(viewer).get(reverse("hr:shift_list")).status_code == 403
    assert client_for(viewer).get(reverse("hr:attendance_list")).status_code == 403


def test_shift_and_attendance_screens_are_arabic_htmx_workspaces(
    employee: Employee,
    night_shift: Shift,
    manager: User,
    client_for: Callable[[User], Client],
) -> None:
    client = client_for(manager)
    shift_list = client.get(reverse("hr:shift_list"))
    shift_form = client.get(reverse("hr:shift_create"), HTTP_HX_REQUEST="true")
    attendance = client.get(
        reverse("hr:attendance_list"), {"date": DAY.isoformat()}, HTTP_HX_REQUEST="true"
    )
    event_form = client.get(reverse("hr:attendance_create"), HTTP_HX_REQUEST="true")

    assert shift_list.status_code == 200
    assert night_shift.name in shift_list.content.decode()
    assert shift_form.status_code == 200
    assert "<html" not in shift_form.content.decode().lower()
    assert 'hx-post="/hr/shifts/new/"' in shift_form.content.decode()
    assert attendance.status_code == 200
    assert employee.name in attendance.content.decode()
    assert "<html" not in attendance.content.decode().lower()
    assert 'hx-get="/hr/attendance/"' in attendance.content.decode()
    assert event_form.status_code == 200
    assert "تسجيل حدث حضور" in event_form.content.decode()
