"""
ذمم التطبيقات — reading the receivable ledger checkpoint 3 already writes.

**Nothing here writes, and nothing here adds a field.** The ledger is
`ApplicationReceivableEntry`, it is append-only, and what an application owes is
`SUM(debit) − SUM(credit)` over it, computed every time it is asked for
(ADR-027 §5). `selectors.receivable_balance` already derives that figure and
this module does not derive a second one — it consumes it, and adds the two
questions a balance alone cannot answer: *how old* is the debt, and *how much of
one entry is still unclaimed*.

## Why aging is FIFO over the entries and not a stored bucket

An aged balance has to say which *sales* are old, not merely how large the total
is. So the credits are applied against the debits oldest-first and whatever
survives is what is genuinely outstanding, dated by the debit that created it.

Applying credits proportionally across every open debit would be arithmetically
tidier and operationally useless: a company that settled January and skipped
February would show every bucket partly paid, and the pattern that says "they
stopped paying in February" — which is the only thing an aging report is for —
would be averaged out of existence.

Nothing here is a bucket the ledger stores. Recompute it and it agrees with the
entries by construction; store it and it is one more number that can disagree
with them, discovered mid-argument with the counterparty.

## No automatic write-off, ever

ADR-028 §9 is explicit: aging is *reported*, and deciding a debt is bad is a
decision with an owner. There is no threshold in this module past which anything
happens on its own, and adding one would be this module quietly making a
provisioning policy that belongs to whoever signs the accounts.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import QuerySet, Sum

from apps.sales.models import (
    ApplicationReceivableEntry,
    DeliveryApplication,
    DeliveryApplicationSettlementAllocation,
    SettlementStatus,
)
from apps.sales.selectors import (
    receivable_balance,
    visible_delivery_applications,
    visible_receivable_entries,
)

if TYPE_CHECKING:
    from apps.users.models import User

ZERO = Decimal("0")

#: `(label, days_from, days_to)`, the last bucket open-ended. Thirty-day steps
#: because a delivery application's `settlement_cycle_days` defaults to thirty:
#: the buckets have to line up with the contract they are measuring, or the
#: first bucket would mix debts that are late with debts that are not yet due.
AGING_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0-30", 0, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("90+", 91, None),
)


@dataclass(frozen=True)
class AgingBucket:
    """One age band and what sits in it."""

    label: str
    days_from: int
    days_to: int | None
    amount: Decimal


@dataclass(frozen=True)
class ApplicationPosition:
    """
    What one delivery application owes, and how old the debt is.

    `balance` comes from `selectors.receivable_balance` — the same one aggregate
    every other caller uses — rather than from summing the buckets, so the two
    are computed independently and a disagreement between them would be visible
    rather than papered over.
    """

    delivery_application: DeliveryApplication
    balance: Decimal
    oldest_open_date: datetime.date | None
    buckets: tuple[AgingBucket, ...]
    #: When the oldest open debt is contractually due, from the application's
    #: own `settlement_cycle_days`. Reported; it refuses nothing, because a
    #: company that settles late is a commercial problem rather than a
    #: data-entry error.
    expected_settlement_date: datetime.date | None


def ledger_for(
    user: User,
    *,
    delivery_application: DeliveryApplication,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> QuerySet[ApplicationReceivableEntry]:
    """
    Every movement for one application, in the order it happened.

    Scoped through `visible_receivable_entries`, so a caller reading the
    organization still sees only their own branches' trading. Ordered by
    business date and then by primary key, which is a *total* order: two
    movements on the same day have to appear in the order they were written or
    the running balance beside them means nothing.
    """
    rows = visible_receivable_entries(user).filter(delivery_application=delivery_application)
    if date_from is not None:
        rows = rows.filter(business_date__gte=date_from)
    if date_to is not None:
        rows = rows.filter(business_date__lte=date_to)
    return rows.order_by("business_date", "pk")


def running_balance(
    entries: Iterable[ApplicationReceivableEntry],
) -> list[tuple[ApplicationReceivableEntry, Decimal]]:
    """
    Each movement paired with the balance after it.

    Computed in Python over an already-ordered sequence rather than as a window
    function, because the screen needs the rows anyway and a second query that
    ordered differently would produce a column that does not match the rows
    beside it.
    """
    balance = ZERO
    result: list[tuple[ApplicationReceivableEntry, Decimal]] = []
    for entry in entries:
        balance += entry.signed_amount
        result.append((entry, balance))
    return result


def unallocated_debit(entry: ApplicationReceivableEntry) -> Decimal:
    """
    How much of one debit entry no **posted** settlement has claimed yet.

    Posted settlements only, and that matters twice. It matches the containment
    trigger in `0010` exactly, so a screen never offers an allocation the
    database will refuse; and it is what makes reversal work — reversing a
    settlement leaves its allocation rows in place as evidence of what was
    claimed, and the entry becomes open again because the settlement they belong
    to is no longer posted.
    """
    if entry.debit <= ZERO:
        return ZERO
    claimed = DeliveryApplicationSettlementAllocation.objects.filter(
        receivable_entry=entry, settlement__status=SettlementStatus.POSTED
    ).aggregate(total=Sum("allocated_amount"))["total"]
    return entry.debit - (claimed or ZERO)


@dataclass
class _OpenDebit:
    """A debit and what is left of it once the credits have been applied."""

    business_date: datetime.date
    remaining: Decimal


def _open_debits(entries: Iterable[ApplicationReceivableEntry]) -> list[_OpenDebit]:
    """
    Apply credits to debits oldest-first, and return what survives.

    See the module docstring for why this is FIFO. A credit larger than every
    open debit — a settlement recorded before the sales it pays for, which
    happens — simply exhausts the queue and leaves nothing; it is not carried
    forward as a negative age, because a negative age is not a fact about
    anything.
    """
    open_rows: list[_OpenDebit] = []
    for entry in entries:
        if entry.debit > ZERO:
            open_rows.append(_OpenDebit(business_date=entry.business_date, remaining=entry.debit))
            continue
        remaining = entry.credit
        for row in open_rows:
            if remaining <= ZERO:
                break
            if row.remaining <= ZERO:
                continue
            applied = min(row.remaining, remaining)
            row.remaining -= applied
            remaining -= applied
    return [row for row in open_rows if row.remaining > ZERO]


def _bucket(
    open_rows: list[_OpenDebit], as_of: datetime.date
) -> tuple[tuple[AgingBucket, ...], datetime.date | None]:
    """Age the surviving debits, and name the oldest."""
    totals: dict[str, Decimal] = {label: ZERO for label, _from, _to in AGING_BUCKETS}
    oldest: datetime.date | None = None
    for row in open_rows:
        business_date = row.business_date
        amount = row.remaining
        age = (as_of - business_date).days
        for label, days_from, days_to in AGING_BUCKETS:
            if age >= days_from and (days_to is None or age <= days_to):
                totals[label] += amount
                break
        if oldest is None or business_date < oldest:
            oldest = business_date
    buckets = tuple(
        AgingBucket(label=label, days_from=days_from, days_to=days_to, amount=totals[label])
        for label, days_from, days_to in AGING_BUCKETS
    )
    return buckets, oldest


def positions_for_applications(
    user: User,
    applications: Sequence[DeliveryApplication],
    *,
    as_of: datetime.date,
) -> list[ApplicationPosition]:
    """
    Age a named set of applications in one pass.

    The entries for every application on the page are read **once** and grouped
    in Python. A per-application query would be one round trip per delivery
    company per page render, on a screen whose whole purpose is to be opened
    every morning.
    """
    if not applications:
        return []

    rows = (
        visible_receivable_entries(user)
        .filter(delivery_application__in=applications, business_date__lte=as_of)
        .order_by("delivery_application_id", "business_date", "pk")
    )
    by_application: dict[int, list[ApplicationReceivableEntry]] = {}
    for entry in rows:
        by_application.setdefault(entry.delivery_application_id, []).append(entry)

    positions: list[ApplicationPosition] = []
    for application in applications:
        entries = by_application.get(application.pk, [])
        buckets, oldest = _bucket(_open_debits(entries), as_of)
        due: datetime.date | None = None
        if oldest is not None:
            due = oldest + datetime.timedelta(days=application.settlement_cycle_days)
        positions.append(
            ApplicationPosition(
                delivery_application=application,
                balance=receivable_balance(
                    delivery_application_id=application.pk,
                    organization_id=application.organization_id,
                    as_of=as_of,
                ),
                oldest_open_date=oldest,
                buckets=buckets,
                expected_settlement_date=due,
            )
        )
    return positions


def positions_for(
    user: User, *, organization_id: int, as_of: datetime.date
) -> list[ApplicationPosition]:
    """
    Every delivery application's position in one organization, as at a date.

    Applications with no movement at all are included with a zero balance,
    deliberately: "this company owes nothing" and "this company is not on the
    report" are different statements, and only the first one is checkable.
    """
    applications = list(
        visible_delivery_applications(user).filter(organization_id=organization_id).order_by("code")
    )
    return positions_for_applications(user, applications, as_of=as_of)


__all__ = [
    "AGING_BUCKETS",
    "AgingBucket",
    "ApplicationPosition",
    "ledger_for",
    "positions_for",
    "positions_for_applications",
    "running_balance",
    "unallocated_debit",
]
