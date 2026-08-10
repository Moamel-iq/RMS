"""
Named PostgreSQL advisory locks shared by more than one module.

Lives in `apps.core` because both `apps.accounting` and `apps.inventory` take
the account-mapping lock, and neither may import the other (ADR-019). Keys are
plain integers here rather than model instances, so core stays beneath every
domain app.

An advisory lock rather than a row lock, for the same reason the stock-key
locks are: the thing being protected is a *decision* — "which account carries
this role right now" — and not a row that necessarily exists. It is scoped to
the transaction, so commit or rollback releases it with no cleanup path to
forget.
"""

from __future__ import annotations

from django.db import connection


def _mapping_key(organization_id: int) -> str:
    return f"account-mapping:{organization_id}"


def lock_account_mappings_shared(organization_id: int) -> None:
    """
    Hold one organization's account-mapping lock in **shared** mode.

    Taken by every posting that resolves an account. Shared, so concurrent
    postings do not serialise against each other — only against the rare
    mutation that takes the exclusive form.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
            [_mapping_key(organization_id)],
        )


def lock_account_mappings_exclusive(organization_id: int) -> None:
    """
    Hold one organization's account-mapping lock **exclusively**.

    Taken by any mutation that can change which account a role resolves to:
    creating, amending, closing, or archiving a mapping, and moving an item
    between categories. It waits for every in-flight posting in that
    organization to commit, and blocks new ones until it commits itself.

    That is what makes the Task 1.3 reclassification guard sound rather than
    merely usually-right. The guard compares the resolution before and after
    the change, but it can only see *committed* stock; a posting already in
    flight with the old mapping in hand would slip past it. Under this lock
    there is no such window.

    Mutations are rare — a chart decision, not an operation — so the cost of
    exclusivity is paid by the operation that can afford it.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [_mapping_key(organization_id)],
        )
