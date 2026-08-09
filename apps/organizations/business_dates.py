"""
Deriving a branch's business date from a moment in time (ADR-008).

The operating day runs 24 hours from `Branch.business_day_start_time`, in the
branch's own timezone. A sale at 01:30 belongs to the day that started the
previous morning — so the business date is **never** `date(timestamp)`, and
deriving it that way is always a bug (CLAUDE.md).

This lives in `apps.organizations` because the branch owns both inputs — its
timezone and its cutoff — and because every module that dates a document
(inventory, sales, payroll) needs the same single answer.
"""

from __future__ import annotations

import datetime
import zoneinfo

from django.utils import timezone

from apps.organizations.models import Branch


def business_date_for(branch: Branch, moment: datetime.datetime) -> datetime.date:
    """
    The operating day a moment falls in, for this branch.

    The moment must be timezone-aware. A naive datetime carries no fact about
    when it happened, and guessing a zone for it would silently date the
    document in whichever zone the server happens to run.
    """
    if timezone.is_naive(moment):
        raise ValueError("business_date_for needs an aware datetime, not a naive one")

    local = moment.astimezone(zoneinfo.ZoneInfo(branch.timezone))
    if local.time() < branch.business_day_start_time:
        return local.date() - datetime.timedelta(days=1)
    return local.date()
