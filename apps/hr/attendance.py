"""Deterministic attendance-day projection over append-only punch evidence."""

from __future__ import annotations

import datetime
import zoneinfo
from dataclasses import asdict, dataclass
from typing import Any

from django.db.models import Q, QuerySet

from apps.hr.models import (
    AttendanceEvent,
    AttendanceEventType,
    Employee,
    Shift,
    ShiftAssignment,
)


@dataclass(frozen=True)
class AttendanceDayResult:
    employee_id: int
    business_date: datetime.date
    shift_id: int | None
    scheduled_start: datetime.datetime | None
    scheduled_end: datetime.datetime | None
    first_check_in: datetime.datetime | None
    last_check_out: datetime.datetime | None
    worked_minutes: int
    break_minutes: int
    lateness_minutes: int
    early_departure_minutes: int
    overtime_candidate_minutes: int
    missing_punch: bool
    absence_candidate: bool
    status: str

    def snapshot(self) -> dict[str, Any]:
        values = asdict(self)
        for field in (
            "business_date",
            "scheduled_start",
            "scheduled_end",
            "first_check_in",
            "last_check_out",
        ):
            value = values[field]
            values[field] = value.isoformat() if value is not None else None
        return values


def assignment_for(employee: Employee, business_date: datetime.date) -> ShiftAssignment | None:
    return (
        ShiftAssignment.objects.select_related("shift", "shift__branch")
        .filter(
            employee=employee,
            effective_from__lte=business_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=business_date))
        .order_by("-effective_from", "-id")
        .first()
    )


def effective_events(employee: Employee, business_date: datetime.date) -> QuerySet[AttendanceEvent]:
    """Return only leaf events; every superseded event remains stored but is not projected."""
    return (
        AttendanceEvent.objects.filter(
            employee=employee,
            business_date=business_date,
            corrections__isnull=True,
        )
        .select_related("scheduled_shift", "branch")
        .order_by("occurred_at", "id")
    )


def _scheduled_bounds(
    shift: Shift, business_date: datetime.date
) -> tuple[datetime.datetime, datetime.datetime]:
    timezone = zoneinfo.ZoneInfo(shift.branch.timezone)
    start = datetime.datetime.combine(business_date, shift.start_time, timezone)
    end_date = (
        business_date + datetime.timedelta(days=1) if shift.crosses_midnight else business_date
    )
    end = datetime.datetime.combine(end_date, shift.end_time, timezone)
    return start, end


def _paired_break_minutes(events: list[AttendanceEvent]) -> int:
    opened: datetime.datetime | None = None
    total = 0
    for event in events:
        if event.event_type == AttendanceEventType.BREAK_OUT and opened is None:
            opened = event.occurred_at
        elif event.event_type == AttendanceEventType.BREAK_IN and opened is not None:
            total += max(0, int((event.occurred_at - opened).total_seconds() // 60))
            opened = None
    return total


def calculate_attendance_day(
    *, employee: Employee, business_date: datetime.date
) -> AttendanceDayResult:
    events = list(effective_events(employee, business_date))
    assignment = assignment_for(employee, business_date)
    shift = assignment.shift if assignment is not None else None
    if shift is None:
        shift = next((event.scheduled_shift for event in events if event.scheduled_shift_id), None)

    check_ins = [
        event.occurred_at for event in events if event.event_type == AttendanceEventType.CHECK_IN
    ]
    check_outs = [
        event.occurred_at for event in events if event.event_type == AttendanceEventType.CHECK_OUT
    ]
    first_check_in = min(check_ins) if check_ins else None
    last_check_out = max(check_outs) if check_outs else None
    break_minutes = _paired_break_minutes(events)
    worked_minutes = 0
    if (
        first_check_in is not None
        and last_check_out is not None
        and last_check_out >= first_check_in
    ):
        worked_minutes = max(
            0,
            int((last_check_out - first_check_in).total_seconds() // 60) - break_minutes,
        )

    scheduled_start: datetime.datetime | None = None
    scheduled_end: datetime.datetime | None = None
    lateness = 0
    early_departure = 0
    overtime = 0
    if shift is not None:
        scheduled_start, scheduled_end = _scheduled_bounds(shift, business_date)
        if first_check_in is not None:
            lateness = max(
                0,
                int((first_check_in - scheduled_start).total_seconds() // 60) - shift.grace_minutes,
            )
        if last_check_out is not None:
            early_departure = max(0, int((scheduled_end - last_check_out).total_seconds() // 60))
        overtime = max(0, worked_minutes - shift.scheduled_minutes)

    absence_candidate = not events
    missing_punch = bool(events) and (first_check_in is None or last_check_out is None)
    if absence_candidate:
        status = "ABSENCE_CANDIDATE"
    elif missing_punch:
        status = "MISSING_PUNCH"
    elif shift is not None and lateness >= shift.late_threshold_minutes:
        status = "LATE"
    elif shift is not None and early_departure >= shift.early_departure_threshold_minutes:
        status = "EARLY_DEPARTURE"
    else:
        status = "PRESENT"

    return AttendanceDayResult(
        employee_id=employee.pk,
        business_date=business_date,
        shift_id=shift.pk if shift is not None else None,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        first_check_in=first_check_in,
        last_check_out=last_check_out,
        worked_minutes=worked_minutes,
        break_minutes=break_minutes,
        lateness_minutes=lateness,
        early_departure_minutes=early_departure,
        overtime_candidate_minutes=overtime,
        missing_punch=missing_punch,
        absence_candidate=absence_candidate,
        status=status,
    )
