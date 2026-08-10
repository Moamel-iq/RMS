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


def _warehouse_key(warehouse_id: int) -> str:
    return f"warehouse-freeze:{warehouse_id}"


def lock_warehouses_shared(warehouse_ids: list[int]) -> None:
    """
    Hold each warehouse's freeze lock in **shared** mode, in canonical order.

    Taken by every posting, above the stock keys, and it is what makes the
    physical-count freeze actually hold (Task 1.6 §I).

    Reading `Warehouse.frozen_by_count` without it is not enough. Under READ
    COMMITTED a posting cannot see an uncommitted freeze, so a count could
    photograph a warehouse while a posting that will land in it is already in
    flight — and the snapshot would be wrong about a warehouse nobody had
    written to yet. Worse, the *stock keys* alone cannot close that hole: a
    first-ever receipt of an item the warehouse has never held takes a key the
    count never snapshotted, precisely because there was nothing there to
    snapshot.

    Shared, so postings never serialise against each other — only against the
    rare freeze, which takes the exclusive form.

    **Sorted, and that is not optional.** A transfer touches two warehouses,
    and two transfers running in opposite directions would take the same two
    locks in opposite order and deadlock — the same defect the stock keys are
    sorted to avoid, one level up.
    """
    _lock_warehouses(warehouse_ids, "pg_advisory_xact_lock_shared")


def lock_warehouses_exclusive(warehouse_ids: list[int]) -> None:
    """
    Hold each warehouse's freeze lock **exclusively**, in canonical order.

    Taken by the three operations that change whether a warehouse is frozen:
    starting a count, cancelling one, and posting one. It waits for every
    in-flight posting into those warehouses to commit and blocks the next, so
    the freeze flag and the postings that must respect it can never interleave.
    """
    _lock_warehouses(warehouse_ids, "pg_advisory_xact_lock")


def _lock_warehouses(warehouse_ids: list[int], function: str) -> None:
    if not warehouse_ids:
        return
    with connection.cursor() as cursor:
        for warehouse_id in sorted(set(warehouse_ids)):
            cursor.execute(
                f"SELECT {function}(hashtextextended(%s, 0))",  # noqa: S608 - fixed literals
                [_warehouse_key(warehouse_id)],
            )
