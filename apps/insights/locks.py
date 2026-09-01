"""
One analysis at a time, per organization.

Two concurrent runs over the same organization are not merely wasteful. They
read the same ledgers at two different cutoffs, and both then try to open the
same case: one wins the unique index and the other has to refetch, so the
*findings* survive — but their observations would carry two different windows
under one run's story, and a reader comparing them would be comparing two
different photographs labelled the same.

An advisory lock rather than a row lock, following `apps/kitchen/graph.py`:
the thing being protected is an activity, not a row that necessarily exists
yet. Transaction-scoped, so a crashed run releases it with no cleanup path for
anybody to forget.

Deliberately **not** reusing `lock_account_mappings_exclusive`: that key is
taken in shared mode by every posting in the organization, so an analysis
holding it would serialise against the business doing its work — the exact
opposite of what a read-only observer should cost.
"""

from __future__ import annotations

from django.db import connection


def _run_key(organization_id: int) -> str:
    return f"insights-run:{organization_id}"


def lock_insights_run(organization_id: int) -> None:
    """
    Hold one organization's analysis lock exclusively, for this transaction.

    Blocks rather than failing fast: a scheduled run that arrives while a
    manual one is finishing should wait a moment and then proceed, not report
    an error to a person who did nothing wrong.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [_run_key(organization_id)],
        )


def try_lock_insights_run(organization_id: int) -> bool:
    """
    Take the lock if it is free, and say so rather than waiting.

    For the caller that would rather tell somebody "an analysis is already
    running" than hold an HTTP worker open until it finishes.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
            [_run_key(organization_id)],
        )
        row = cursor.fetchone()
    return bool(row and row[0])


__all__ = ["lock_insights_run", "try_lock_insights_run"]
