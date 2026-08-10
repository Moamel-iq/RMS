"""
Deriving a branch's business date from a moment in time (ADR-008).

The operating day runs 24 hours from `Branch.business_day_start_time`, in the
branch's own timezone. A sale at 01:30 belongs to the day that started the
previous morning — so the business date is **never** `date(timestamp)`, and
deriving it that way is always a bug (CLAUDE.md).

This lives in `apps.organizations` because the branch owns both inputs — its
timezone and its cutoff — and because every module that dates a document
(inventory, sales, payroll) needs the same single answer.

## Two timestamps, one authority

    effective_at    the actual physical moment the event happened
    business_date   the authoritative operational and accounting date

Everything that *dates* a document uses the business date: accounting-period
validation, account-mapping and conversion effective-date selection, document
numbering by business year, daily reporting, and the journal's accounting
date. `effective_at` is retained alongside it and never substituted for it —
a physical timestamp answers "when", and the business date answers "which
day's figures does this belong to". Requiring the *calendar* month of
`effective_at` to be open as well would demand a second accounting period for
an event that belongs to exactly one (ADR-008, amended at Task 1.4).

## Snapshots

A branch's timezone or cutoff can change. Re-deriving a stored business date
afterwards would silently move a submitted or posted document into a
different period, so a document that has been committed to a business date
also stores **the timezone and cutoff it was derived with** and re-derives
only from those. `resolve_business_day` produces that snapshot;
`business_date_from_snapshot` replays it.
"""

from __future__ import annotations

import datetime
import zoneinfo
from dataclasses import dataclass

from django.utils import timezone

from apps.organizations.models import Branch


@dataclass(frozen=True)
class BusinessDay:
    """A derived business date, with the branch settings that produced it."""

    business_date: datetime.date
    timezone_name: str
    day_start: datetime.time


def business_date_from_snapshot(
    moment: datetime.datetime, *, timezone_name: str, day_start: datetime.time
) -> datetime.date:
    """
    The operating day a moment falls in, under *given* branch settings.

    Takes the settings explicitly so a stored snapshot can be replayed. The
    moment must be timezone-aware: a naive datetime carries no fact about when
    it happened, and guessing a zone would date the document in whichever zone
    the server happens to run.
    """
    if timezone.is_naive(moment):
        raise ValueError("a business date needs an aware datetime, not a naive one")

    local = moment.astimezone(zoneinfo.ZoneInfo(timezone_name))
    if local.time() < day_start:
        return local.date() - datetime.timedelta(days=1)
    return local.date()


def resolve_business_day(branch: Branch, moment: datetime.datetime) -> BusinessDay:
    """
    Derive the business date from the branch's settings **as they stand now**,
    and return it together with those settings so a caller can store both.

    A document that stores the snapshot keeps its business date stable when
    the branch's cutoff is later changed; one that re-derives from the live
    branch would move between periods without anybody deciding it should.
    """
    return BusinessDay(
        business_date=business_date_from_snapshot(
            moment,
            timezone_name=branch.timezone,
            day_start=branch.business_day_start_time,
        ),
        timezone_name=branch.timezone,
        day_start=branch.business_day_start_time,
    )


def business_date_for(branch: Branch, moment: datetime.datetime) -> datetime.date:
    """
    The operating day a moment falls in, for this branch's current settings.

    The convenience form of `resolve_business_day` for callers that do not
    store a snapshot — a live report, a default on a draft. Anything that
    *commits* to a date should keep the snapshot instead.
    """
    return resolve_business_day(branch, moment).business_date
