"""
The stock ledger kernel: moving weighted average, posting, and reversal.

This module knows inventory arithmetic and nothing about users. Who may post
is answered one layer up, in `apps/inventory/commands.py`, exactly as
`apps/accounting/services.py` and `commands.py` divide the same way.

## What the arithmetic must guarantee

    quantity == 0  =>  value == 0  =>  average == 0
    quantity  > 0  =>  value >= 0

The zero case is the interesting one. When an outbound brings a balance to
exactly zero it is valued at the **entire remaining book value**, not at
`quantity x average` (ADR-018 §4). Otherwise repeated rounding leaves a few
dinars of value sitting against no goods, and the only ways out of that are an
adjusting entry nobody asked for or a rewrite of history. Charging the
difference to the goods that actually left is both simpler and truer.

## Order

Valuation follows **posting order**, never effective-date order. A receipt
backdated to the 3rd and posted on the 10th affects the average from the 10th
forward; it does not re-price the issues of the 5th and 7th, which have been
reported and reconciled already.

## Dates

Two timestamps, and only one of them dates anything. `effective_at` is the
physical moment; the **business date** is the operational and accounting date,
derived through the branch's timezone and operating-day cutoff (ADR-008). The
accounting period is validated on the business date, so an event at 01:30 on
1 August under an 03:00 cutoff needs the **July** period open and not the
August one — it belongs to exactly one business day, so it may require exactly
one period.

## Locking

Five things are serialised, and they are taken in a fixed order (ADR-019 §6,
extended by ADR-020 §11 and ADR-021 §7):

1. the source document rows, by the caller above this kernel — parent
   aggregate before child event, where there is one;
2. the **warehouse freeze locks**, in shared mode, sorted by id — a physical
   count takes them exclusively while it photographs a warehouse, so a posting
   and a freeze can never interleave. Sorted because a transfer holds two, and
   two transfers in opposite directions would otherwise deadlock;
3. the organization's account-mapping lock, in **shared** mode — many
   postings may hold it at once, and a mapping mutation takes the exclusive
   form, so no posting can resolve an account while it is being re-homed;
4. the stock keys the posting touches, sorted canonically;
5. then the organization's posted-order counter.

Always that order. A transaction holding a key and waiting for the counter,
against one holding the counter and waiting for that key, is a deadlock — and
it is the deadlock that a "lock the counter first, it's quick" instinct
produces. The mapping lock sits above the keys for the same reason: a mutation
that took keys first and then asked for the mapping would invert the order
against every posting.

Step 4 is per *event*, not per call. A caller that posts two entries in one
transaction — a transfer receipt releases from in-transit and lands at the
destination — takes every key up front through `acquire_stock_key_locks`, so
the two are ordered canonically against each other rather than by the order
the calls happen to be written. Advisory locks are re-entrant, so the
per-entry acquisitions afterwards are no-ops.

`select_for_update()` cannot lock a balance row that does not exist yet, and
the first receipt into a new warehouse is exactly that case. So the key lock is
a **PostgreSQL transaction advisory lock** over a canonical key string, which
exists whether or not the row does and is released by commit or rollback
without any cleanup path to forget.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, JournalEntry, PeriodState, SourceEvent
from apps.accounting.services import resolve_period
from apps.core.locks import lock_account_mappings_shared, lock_warehouses_shared
from apps.core.models import AuditAction
from apps.core.money import MONEY_PLACES, quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.core.source_identity import canonical_source_identity
from apps.inventory.models import (
    INBOUND_MOVEMENT_TYPES,
    SIGNLESS_MOVEMENT_TYPES,
    InventoryItem,
    InventoryLot,
    ItemPackageConversion,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
    StockPostingSequence,
    ValuationLayer,
    Warehouse,
    WarehouseType,
)
from apps.organizations.business_dates import business_date_for
from apps.organizations.models import Organization

if TYPE_CHECKING:
    # Typing only. The kernel reads the acting user from the ambient audit
    # context and never takes one as an argument, so importing the model at
    # runtime would suggest a coupling that does not exist.
    from apps.users.models import User

ZERO_MONEY = Decimal(0).scaleb(-MONEY_PLACES)
ZERO = Decimal("0")

#: Outbound types that may take an expired lot off the shelf, because getting
#: it off the shelf is what they are for (Task 1.6 §F). Ordinary issue is
#: deliberately absent and stays absent.
#:
#: `RETURN_OUT` joins them at Task 2.13, and the case is the same one: goods
#: that arrived spoiled or too near their date are among the commonest things
#: a restaurant sends back, and refusing to move an expired lot would force
#: the storekeeper to waste it instead — destroying a legitimate claim against
#: the supplier in order to obey a rule written to stop expired food reaching
#: a kitchen. Nothing here reaches a kitchen. Task 2.0 §10 is silent on the
#: point; this is a decision, recorded rather than assumed.
EXPIRED_LOT_REMOVAL_TYPES = frozenset(
    {
        MovementType.WASTE,
        MovementType.COUNT_LOSS,
        MovementType.MANUAL_ADJUSTMENT,
        MovementType.TRANSFER_SHORTAGE,
        MovementType.RETURN_OUT,
    }
)

#: The costing method this kernel implements. Named on every allocation that
#: is ever written, so a later strategy cannot be mistaken for this one.
MOVING_WEIGHTED_AVERAGE = "MOVING_WEIGHTED_AVERAGE"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class EffectDirection(Enum):
    """
    Which way a **signless** movement type pushes the position.

    Only ever supplied for a type in `SIGNLESS_MOVEMENT_TYPES`. Offering it on
    a receipt or an issue would let a caller drive stock out through the
    receipt path and past the availability check, which is precisely what
    deriving the sign from the type prevents.
    """

    IN = "IN"
    OUT = "OUT"
    #: Revalue a position without moving any goods. Quantity is zero — the one
    #: case where a zero quantity is meaningful rather than a mistake.
    VALUE_ONLY = "VALUE_ONLY"


@dataclass(frozen=True)
class MovementInput:
    """
    One requested effect: a quantity of one item, in one warehouse.

    `quantity` is always **positive** — the direction comes from
    `movement_type`, which is a closed set with a fixed sign. A caller that
    could pass a negative receipt would be able to issue stock through the
    receipt path and bypass the availability check.

    The exception is a type that has no fixed sign, where `direction` is
    required and says which way it goes; see `SIGNLESS_MOVEMENT_TYPES`.
    """

    warehouse: Warehouse
    item: InventoryItem
    movement_type: str
    quantity: Decimal
    effect_key: str
    lot: InventoryLot | None = None
    #: Required for an inbound. Ignored for an outbound, which is valued at
    #: the current average by definition.
    unit_cost: Decimal | None = None
    source_conversion: ItemPackageConversion | None = None
    #: The inventory-control account this value should enter, resolved by the
    #: document layer. Meaningful on an **inbound**: it establishes the
    #: position's account when the position is empty, and must match the
    #: standing account when it is not. An outbound ignores it — value leaves
    #: the account it entered, which the position already knows (ADR-019 §5).
    control_account: Account | None = None
    #: An exact inbound value that must not be re-derived from
    #: `quantity x unit_cost` — see `apply_inbound`. Set only where the caller
    #: computed it against a posted movement, as a return does.
    inbound_value: Decimal | None = None
    #: An exact **outbound** value, for the same reason in the other direction
    #: — see `apply_outbound`. A transfer receipt takes the value its own
    #: dispatch allocated, not the pooled in-transit average (ADR-020 §4).
    outbound_value: Decimal | None = None
    #: Required for a signless movement type, refused for every other. See
    #: `EffectDirection`.
    direction: EffectDirection | None = None
    #: The signed amount a `VALUE_ONLY` effect moves. Positive writes the
    #: position up. Meaningless — and refused — in any other direction.
    value_adjustment: Decimal | None = None


@dataclass
class _StockKey:
    """A canonical `(warehouse, item, lot)`, ordered deterministically."""

    warehouse_id: int
    item_id: int
    lot_id: int | None

    @property
    def sort_key(self) -> tuple[int, int, int]:
        # A lot-less position sorts as 0. Deterministic ordering is the whole
        # point: two multi-item events that locked in caller order would
        # deadlock the moment one listed rice before chicken and the other
        # listed chicken before rice.
        return (self.warehouse_id, self.item_id, self.lot_id or 0)

    @property
    def canonical(self) -> str:
        return f"{self.warehouse_id}:{self.item_id}:{self.lot_id or 0}"


@dataclass
class _Position:
    """The working balance for one key inside a posting."""

    key: _StockKey
    balance: StockBalance
    quantity: Decimal = field(default_factory=lambda: ZERO)
    value: Decimal = field(default_factory=lambda: ZERO)
    average: Decimal = field(default_factory=lambda: ZERO)
    #: The account this position's current stock cycle belongs to, or None
    #: while the position is empty.
    control_account: Account | None = None


# ---------------------------------------------------------------------------
# The arithmetic — pure, so it can be property-tested without a database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValuationStep:
    """The result of applying one movement to one position."""

    quantity_before: Decimal
    value_before: Decimal
    average_before: Decimal
    quantity_delta: Decimal
    value_delta: Decimal
    unit_cost: Decimal
    quantity_after: Decimal
    value_after: Decimal
    average_after: Decimal


def apply_inbound(
    *,
    quantity: Decimal,
    unit_cost: Decimal,
    before_quantity: Decimal,
    before_value: Decimal,
    value_in: Decimal | None = None,
) -> ValuationStep:
    """
    Add goods at a known cost and re-blend the average.

        value_in    = quantize_money(quantity x unit_cost)
        new_value   = old_value + value_in
        new_average = quantize_unit_price(new_value / new_quantity)

    The average is derived from the totals rather than the totals from the
    average. Deriving it the other way accumulates the rounding error of every
    receipt into the stored value, and the ledger stops summing.

    `value_in` overrides the product when the caller holds an exact figure
    that must not be re-derived — a return taking back the entire remaining
    value of the issue it goes against. It is the inbound twin of the
    full-depletion rule: both exist so that a closing movement lands on the
    exact figure rather than near it, leaving no residual for anybody to
    chase. It is never a caller's opinion about cost; it is arithmetic the
    caller already did against a posted movement.
    """
    value_in = (
        quantize_money(quantity * unit_cost) if value_in is None else quantize_money(value_in)
    )
    after_quantity = quantize_quantity(before_quantity + quantity)
    after_value = quantize_money(before_value + value_in)
    after_average = (
        quantize_unit_price(after_value / after_quantity) if after_quantity > ZERO else ZERO
    )
    return ValuationStep(
        quantity_before=before_quantity,
        value_before=before_value,
        average_before=(
            quantize_unit_price(before_value / before_quantity) if before_quantity > ZERO else ZERO
        ),
        quantity_delta=quantity,
        value_delta=value_in,
        unit_cost=quantize_unit_price(unit_cost),
        quantity_after=after_quantity,
        value_after=after_value,
        average_after=after_average,
    )


def apply_outbound(
    *,
    quantity: Decimal,
    before_quantity: Decimal,
    before_value: Decimal,
    value_out: Decimal | None = None,
) -> ValuationStep:
    """
    Remove goods at the current average — except at the last one out.

    Bringing the balance to exactly zero takes the **entire remaining value**,
    not `quantity x average`. That is what makes `Q == 0 => V == 0` true by
    construction rather than by an adjusting entry, and it never mutates a
    movement that has already been posted.

    `value_out` overrides the average when the caller holds an exact figure
    that must not be re-derived — the twin of `apply_inbound`'s `value_in`.
    A transfer receipt is the motivating case: one in-transit position pools
    several transfers of the same item, so its average is a blend of them all,
    and receiving against it at that average would move a quantity from one
    transfer out at another transfer's cost (ADR-020 §4). The exact figure
    comes from the receipt's own dispatch allocation.

    **The full-depletion rule is deliberately not applied to an exact value.**
    Silently widening the caller's figure to soak up a residual would hide the
    very disagreement between the allocation and the ledger that §K asks to be
    reported; `post_stock_entry` refuses that case with a named error instead.

    Availability is the caller's business: this is arithmetic, and refusing
    here would put the negative-stock policy in two places.
    """
    before_average = (
        quantize_unit_price(before_value / before_quantity) if before_quantity > ZERO else ZERO
    )
    after_quantity = quantize_quantity(before_quantity - quantity)

    if value_out is not None:
        exact = quantize_money(value_out)
        after_value = quantize_money(before_value - exact)
        after_average = (
            quantize_unit_price(after_value / after_quantity) if after_quantity > ZERO else ZERO
        )
        return ValuationStep(
            quantity_before=before_quantity,
            value_before=before_value,
            average_before=before_average,
            quantity_delta=-quantity,
            value_delta=-exact,
            unit_cost=(
                quantize_unit_price(exact / quantity) if quantity > ZERO else before_average
            ),
            quantity_after=after_quantity,
            value_after=after_value,
            average_after=after_average,
        )

    if after_quantity == ZERO:
        value_out = before_value
        after_value = ZERO
        after_average = ZERO
    else:
        value_out = quantize_money(quantity * before_average)
        after_value = quantize_money(before_value - value_out)
        after_average = (
            quantize_unit_price(after_value / after_quantity) if after_quantity > ZERO else ZERO
        )

    unit_cost = quantize_unit_price(value_out / quantity) if quantity > ZERO else before_average
    return ValuationStep(
        quantity_before=before_quantity,
        value_before=before_value,
        average_before=before_average,
        quantity_delta=-quantity,
        value_delta=-value_out,
        unit_cost=unit_cost,
        quantity_after=after_quantity,
        value_after=after_value,
        average_after=after_average,
    )


def apply_value_only(
    *,
    value_adjustment: Decimal,
    before_quantity: Decimal,
    before_value: Decimal,
) -> ValuationStep:
    """
    Restate what a position is worth without touching what is on the shelf.

        new_value   = old_value + adjustment
        new_average = quantize_unit_price(new_value / quantity)

    The quantity is untouched, so the average moves by exactly the adjustment
    spread over the goods that are there — which is the whole point: a
    revaluation says the stock was always worth this, not that more of it
    arrived.

    Two refusals live in `post_stock_entry` rather than here, because both are
    policy about a position rather than arithmetic: a revaluation against zero
    quantity (`value_only_needs_quantity`) and one that would drive the value
    below zero (`value_only_would_go_negative`). This function is pure so it
    can be property-tested without a database, and a pure function that also
    enforced policy would put that policy in two places.

    `unit_cost` on the resulting step is the **new average**, not the
    adjustment per unit. The movement's unit cost has always meant "what one
    base unit was worth in this movement", and after a revaluation that is what
    a unit is now worth.
    """
    after_value = quantize_money(before_value + value_adjustment)
    after_average = (
        quantize_unit_price(after_value / before_quantity) if before_quantity > ZERO else ZERO
    )
    return ValuationStep(
        quantity_before=before_quantity,
        value_before=before_value,
        average_before=(
            quantize_unit_price(before_value / before_quantity) if before_quantity > ZERO else ZERO
        ),
        quantity_delta=ZERO,
        value_delta=quantize_money(value_adjustment),
        unit_cost=after_average,
        quantity_after=before_quantity,
        value_after=after_value,
        average_after=after_average,
    )


def apply_reversal(
    *,
    quantity_delta: Decimal,
    value_delta: Decimal,
    before_quantity: Decimal,
    before_value: Decimal,
) -> ValuationStep:
    """
    Mirror an original movement exactly — its quantity and its value.

    **Not today's average.** Reversing a receipt of 100 kg that cost 500,000
    removes 100 kg and 500,000, whatever the average has since become.
    Anything else would let a reversal create or destroy value out of the
    passage of time.
    """
    after_quantity = quantize_quantity(before_quantity + quantity_delta)
    after_value = quantize_money(before_value + value_delta)
    if after_quantity == ZERO:
        after_value = ZERO
    after_average = (
        quantize_unit_price(after_value / after_quantity) if after_quantity > ZERO else ZERO
    )
    unit_cost = (
        quantize_unit_price(abs(value_delta) / abs(quantity_delta))
        if quantity_delta != ZERO
        else ZERO
    )
    return ValuationStep(
        quantity_before=before_quantity,
        value_before=before_value,
        average_before=(
            quantize_unit_price(before_value / before_quantity) if before_quantity > ZERO else ZERO
        ),
        quantity_delta=quantity_delta,
        value_delta=value_delta,
        unit_cost=unit_cost,
        quantity_after=after_quantity,
        value_after=after_value,
        average_after=after_average,
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def request_fingerprint(
    *,
    command: str,
    effective_at: datetime.datetime | None,
    source: tuple[str, str, str],
    effects: Sequence[MovementInput],
) -> str:
    """
    A digest of what the posting actually asks for.

    The key answers "have I seen this request id before". This answers "and
    was it the same request". Without it, a caller reusing a key with a
    corrected quantity would silently receive the uncorrected posting and
    believe the correction had landed.

    `effective_at` is the **caller's** value, and `None` when they did not send
    one. It must not be the resolved `timezone.now()`: the server's clock moves
    between a request and its retry, so hashing the resolved value would give
    every retry a different fingerprint and turn idempotency into a permanent
    `idempotency_key_conflict`. A caller who sends nothing is saying "now", and
    a retry saying "now" is the same request.

    Effects are **sorted** before hashing. A caller who sends the same lines in
    a different order is sending the same request, and a fingerprint over an
    unordered collection in caller order would call that a conflict.

    Amounts are quantized and stringified: a float in a fingerprint hashes
    differently on different machines.
    """
    payload = {
        "command": command,
        "effective_at": effective_at.isoformat() if effective_at is not None else None,
        "source": list(source),
        "effects": sorted(
            [
                [
                    effect.effect_key,
                    effect.warehouse.pk,
                    effect.item.pk,
                    effect.lot.pk if effect.lot is not None else None,
                    effect.movement_type,
                    str(quantize_quantity(effect.quantity)),
                    str(quantize_unit_price(effect.unit_cost))
                    if effect.unit_cost is not None
                    else None,
                    # The exact-value overrides are part of what is being
                    # asked for: two receipts of the same quantity against
                    # different transfer allocations are different requests,
                    # and a fingerprint blind to them would call the second a
                    # retry of the first.
                    str(quantize_money(effect.inbound_value))
                    if effect.inbound_value is not None
                    else None,
                    str(quantize_money(effect.outbound_value))
                    if effect.outbound_value is not None
                    else None,
                    # Direction and revaluation amount, for the same reason: a
                    # gain and a loss of the same quantity are opposite
                    # requests, and a write-up and a write-down of the same
                    # magnitude differ only in a sign the hash must see.
                    effect.direction.value if effect.direction is not None else None,
                    str(quantize_money(effect.value_adjustment))
                    if effect.value_adjustment is not None
                    else None,
                ]
                for effect in effects
            ],
            key=json.dumps,
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _replay(
    *, organization: Organization, idempotency_key: str, fingerprint: str
) -> StockLedgerEntry | None:
    """
    The posting this key already produced **in this organization**, if any.

    Scoped in the query itself. A lookup on the key alone would hand another
    organization's posting to whoever guessed their key.
    """
    existing = StockLedgerEntry.objects.filter(
        organization=organization, idempotency_key=idempotency_key
    ).first()
    if existing is None:
        return None
    if existing.request_fingerprint != fingerprint:
        raise ValidationError(
            _(
                "Idempotency key %(key)s was already used in %(organization)s for a different posting."
            ),
            code="idempotency_key_conflict",
            params={"key": idempotency_key, "organization": organization.code},
        )
    return existing


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def _lock_stock_key(key: _StockKey) -> None:
    """
    Take a transaction-scoped advisory lock over one stock position.

    An advisory lock rather than a row lock because the row may not exist yet:
    `SELECT ... FOR UPDATE` on a missing row locks nothing at all, so two
    concurrent first receipts would both see "no balance" and both insert.
    A lock over the *key* has no such gap.

    `hashtextextended` is PostgreSQL's own hash, so the value is identical
    across processes and Python versions. A collision between two different
    keys costs a little needless serialisation and can never cost correctness.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key.canonical])


def _lock_mappings(organization: Organization) -> None:
    """
    Hold the organization's account-mapping lock in **shared** mode.

    Many postings hold it at once, so ordinary throughput is unaffected. A
    mapping or category mutation takes the exclusive form, so the two cannot
    interleave: a posting either resolves its accounts entirely before the
    re-homing begins, or entirely after it has committed and been checked.

    Without this, the Task 1.3 apply-then-verify guard has a real gap. That
    guard re-resolves the *committed* stock it can see; a posting that had
    already read the old mapping but not yet committed its balance row is
    invisible to it, so the mutation would commit, the posting would commit,
    and standing stock would sit in one account while its ledger claimed
    another — with nothing left to detect it.

    Taken here, inside the kernel, rather than being left to each caller: a
    posting path that forgot it would reopen the window silently.
    """
    lock_account_mappings_shared(organization.pk)


def acquire_stock_key_locks(effects: Sequence[MovementInput]) -> None:
    """
    Take the advisory locks these effects will need, in canonical order.

    For a caller that must inspect ledger state *before* posting — the opening
    document checks that no key has prior history — and must not let a
    concurrent posting slip between the inspection and the post. PostgreSQL
    advisory locks are re-entrant within a session, so `post_stock_entry`
    re-acquiring the same keys later in the transaction is a no-op, and the
    global lock order (document row → stock keys → counters) is preserved.
    """
    keys: dict[str, _StockKey] = {}
    for effect in effects:
        key = _key_of(effect)
        keys.setdefault(key.canonical, key)
    for key in sorted(keys.values(), key=lambda key: key.sort_key):
        _lock_stock_key(key)


def acquire_movement_key_locks(movements: Sequence[StockMovement]) -> None:
    """
    Take the advisory locks a set of **already posted** movements sits on.

    For a caller reversing more than one entry in one transaction. Each
    `reverse_stock_entry` sorts its own movements canonically, but two of them
    called in sequence order their keys by the order the calls are written —
    which is how one path takes `(A, B)` and another takes `(B, A)`. Taking
    every key up front puts them all in one canonical order and removes the
    cycle. Re-entrant, so the per-entry acquisitions afterwards are no-ops.
    """
    acquire_stock_key_locks([_effect_of(movement) for movement in movements])


def _next_posted_sequence(*, organization: Organization) -> int:
    """
    Take the organization's next posted-order number under a row lock.

    Acquired **after** every stock key, never before — see the module
    docstring on lock ordering.
    """
    sequence, _created = StockPostingSequence.objects.get_or_create(organization=organization)
    locked = StockPostingSequence.objects.select_for_update().get(pk=sequence.pk)
    locked.last_sequence += 1
    locked.save(update_fields=["last_sequence"])
    return int(locked.last_sequence)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_period_is_open(*, organization: Organization, business_date: datetime.date) -> None:
    """
    Stock postings need an OPEN period **for their business date**.

    The business date, not `effective_at.date()`. An event at 01:30 on the 1st
    of August under an 03:00 cutoff belongs to the 31st of July, and requiring
    August as well would demand two open periods for an event that happened on
    one business day — and would refuse a legitimate late-night posting the
    moment the new month was closed ahead of it (ADR-008, amended at Task 1.4).

    There is no soft-close override: a stock movement changes what the accounts
    will say, and a period that has stopped accepting entries has stopped
    accepting the things that cause them. The count-adjustment exception
    arrives with the count document in Task 1.6, attached to a real document
    rather than to a flag.
    """
    period = resolve_period(organization=organization, accounting_date=business_date)
    if period.state != PeriodState.OPEN:
        raise ValidationError(
            _("Period %(period)s is %(state)s and does not accept stock postings."),
            code="period_not_open",
            params={"period": str(period), "state": period.state},
        )


def _business_date_of(
    effects: Sequence[MovementInput], *, moment: datetime.datetime
) -> datetime.date:
    """
    The business date these effects fall on, derived from their own branches.

    A fallback for callers with no document above them — the kernel's own
    tests, and any future path that posts without one. Every document layer
    passes its **stored snapshot** instead, so a later change to the branch
    cutoff cannot move a posted document between periods.

    Effects that resolve to different business dates are refused rather than
    silently taking the first: two dates means two operational days, and a
    single posting cannot belong to both.
    """
    dates = {
        business_date_for(effect.warehouse.branch, moment): effect.warehouse.branch.code
        for effect in effects
    }
    if len(dates) > 1:
        raise ValidationError(
            _("These effects fall on different business dates: %(dates)s."),
            code="ambiguous_business_date",
            params={"dates": ", ".join(sorted(str(date) for date in dates))},
        )
    return next(iter(dates))


def _validate_effect(effect: MovementInput, *, organization: Organization) -> None:
    """Everything checkable about one requested effect, before any lock is taken."""
    if effect.warehouse.branch.organization_id != organization.pk:
        raise ValidationError(
            _("Warehouse %(code)s belongs to another organization."),
            code="warehouse_organization_mismatch",
            params={"code": effect.warehouse.code},
        )
    if effect.item.organization_id != organization.pk:
        raise ValidationError(
            _("Item %(code)s belongs to another organization."),
            code="item_organization_mismatch",
            params={"code": effect.item.code},
        )
    if not effect.warehouse.is_active:
        raise ValidationError(
            _("Warehouse %(code)s is archived."),
            code="warehouse_inactive",
            params={"code": effect.warehouse.code},
        )
    if effect.direction is EffectDirection.VALUE_ONLY:
        if effect.quantity != ZERO:
            raise ValidationError(
                _("A value-only effect moves no goods, so its quantity must be zero."),
                code="value_only_carries_quantity",
            )
    elif effect.quantity <= ZERO:
        raise ValidationError(
            _("A movement quantity must be greater than zero; the type carries the direction."),
            code="quantity_not_positive",
        )
    if effect.movement_type not in MovementType.values:
        raise ValidationError(
            _("%(type)s is not a known movement type."),
            code="unknown_movement_type",
            params={"type": effect.movement_type},
        )
    if effect.movement_type == MovementType.REVERSAL:
        raise ValidationError(
            _("A reversal is posted by reverse_stock_entry, not as an ordinary effect."),
            code="reversal_is_not_an_effect",
        )
    if not effect.effect_key.strip():
        raise ValidationError(_("Every effect needs an effect key."), code="effect_key_required")

    _validate_direction(effect)
    _validate_lot(effect)

    if _direction_of(effect) is EffectDirection.IN:
        if effect.unit_cost is None:
            raise ValidationError(
                _("An inbound movement needs a unit cost."), code="unit_cost_required"
            )
        if effect.unit_cost < ZERO:
            raise ValidationError(_("A unit cost cannot be negative."), code="unit_cost_negative")


def _validate_direction(effect: MovementInput) -> None:
    """
    A signless type must state its direction; every other type must not.

    Both halves matter. Without the first, `MANUAL_ADJUSTMENT` falls through to
    whichever branch happens to be last and a gain silently posts as a loss.
    Without the second, a caller could label a `RECEIPT` as `OUT` and take
    stock off the shelf through a path that never checks availability.
    """
    signless = effect.movement_type in SIGNLESS_MOVEMENT_TYPES
    if signless and effect.direction is None:
        raise ValidationError(
            _("%(type)s carries no direction of its own, so the effect must state one."),
            code="direction_required",
            params={"type": effect.movement_type},
        )
    if not signless and effect.direction is not None:
        raise ValidationError(
            _("%(type)s already carries its own direction and must not be given another."),
            code="direction_not_allowed",
            params={"type": effect.movement_type},
        )
    if effect.direction is EffectDirection.VALUE_ONLY:
        if effect.value_adjustment is None or effect.value_adjustment == ZERO:
            raise ValidationError(
                _("A value-only effect needs a non-zero signed value adjustment."),
                code="value_adjustment_required",
            )
    elif effect.value_adjustment is not None:
        raise ValidationError(
            _("A value adjustment belongs only to a value-only effect."),
            code="value_adjustment_not_allowed",
        )


def _direction_of(effect: MovementInput) -> EffectDirection:
    """The one place the sign of an effect is decided."""
    if effect.direction is not None:
        return effect.direction
    if effect.movement_type in INBOUND_MOVEMENT_TYPES:
        return EffectDirection.IN
    return EffectDirection.OUT


def _validate_lot(effect: MovementInput) -> None:
    """
    A lot-tracked item requires a lot; an untracked one refuses to have any.

    Not cosmetic: the lot is part of the valuation key, so an item that
    sometimes carried one and sometimes did not would hold two balances for
    one shelf and neither would be right.
    """
    if effect.item.tracks_lots:
        if effect.lot is None:
            raise ValidationError(
                _("Item %(code)s tracks lots, so every movement needs one."),
                code="lot_required",
                params={"code": effect.item.code},
            )
        if effect.lot.item_id != effect.item.pk:
            raise ValidationError(
                _("Lot %(lot)s belongs to another item."),
                code="lot_item_mismatch",
                params={"lot": effect.lot.code},
            )
        if effect.item.tracks_expiry and effect.lot.expiry_date is None:
            raise ValidationError(
                _("Item %(code)s tracks expiry, so its lots need an expiry date."),
                code="lot_expiry_required",
                params={"code": effect.item.code},
            )
    elif effect.lot is not None:
        raise ValidationError(
            _("Item %(code)s does not track lots, so a movement must not name one."),
            code="lot_not_allowed",
            params={"code": effect.item.code},
        )


def _validate_lot_is_not_expired(effect: MovementInput, *, on_date: datetime.date) -> None:
    """
    Expired goods do not leave the warehouse as ordinary stock.

    Blocked by default and blocked for everyone in Task 1.2. Writing off
    expired stock is a `WASTE` movement, which says what happened; issuing it
    to a kitchen says something else entirely and would be false.

    **In-transit stock is exempt, and it has to be.** Goods that expire while
    on the road still have to be got off the road: the receiving branch must
    be able to take delivery of what physically arrived, and a consignment
    that never arrives must be closeable as a shortage. Refusing both would
    strand the value in transit forever with no document able to move it —
    the rule would stop protecting the kitchen and start trapping the ledger
    (Task 1.5 §K). What arrives is still expired, and the waste document is
    what writes it off at the destination.

    **The three removal routes are exempt too** (Task 1.6 §F): waste, a count
    that finds the expired goods gone, and an authorized negative adjustment.
    Every one of them is a correction or a disposal that *says so* on the
    record. The rule exists to stop expired food reaching a kitchen through an
    ISSUE, and an exemption for the documents whose whole purpose is to get it
    out of the building costs that nothing. Refusing them instead would leave
    expired stock permanently on the books, which is the outcome the rule was
    written to avoid.
    """
    if effect.lot is None:
        return
    if _direction_of(effect) is not EffectDirection.OUT:
        return
    if effect.movement_type in EXPIRED_LOT_REMOVAL_TYPES:
        return
    if effect.warehouse.warehouse_type == WarehouseType.IN_TRANSIT:
        return
    if effect.lot.is_expired_on(on_date):
        raise ValidationError(
            _("Lot %(lot)s expired on %(date)s and cannot be issued."),
            code="lot_expired",
            params={"lot": effect.lot.code, "date": effect.lot.expiry_date},
        )


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def _key_of(effect: MovementInput) -> _StockKey:
    return _StockKey(
        warehouse_id=effect.warehouse.pk,
        item_id=effect.item.pk,
        lot_id=effect.lot.pk if effect.lot is not None else None,
    )


def _load_position(key: _StockKey, *, effect: MovementInput) -> _Position:
    """
    The current balance for one key, created at zero if it has never moved.

    Called only after `_lock_stock_key`, so the get-or-create cannot race:
    without the advisory lock two concurrent first receipts would both find
    nothing and both insert, and the unique constraint would turn one of them
    into an error instead of a correct second movement.
    """
    balance, _created = StockBalance.objects.get_or_create(
        warehouse=effect.warehouse,
        item=effect.item,
        lot=effect.lot,
        defaults={
            "organization_id": effect.item.organization_id,
            "branch_id": effect.warehouse.branch_id,
        },
    )
    # Re-read under a row lock as well. The advisory lock already serialises
    # this key; the row lock keeps anything that reaches the row by another
    # path — a rebuild, a repair — behind the same queue.
    # `of=("self",)` locks the balance row and not the joined account: the
    # account is a nullable FK, so an unqualified FOR UPDATE would try to lock
    # the nullable side of an outer join, which PostgreSQL refuses outright.
    locked = (
        StockBalance.objects.select_for_update(of=("self",))
        .select_related("control_account")
        .get(pk=balance.pk)
    )
    return _Position(
        key=key,
        balance=locked,
        quantity=locked.quantity,
        value=locked.value,
        average=locked.average_cost,
        control_account=locked.control_account,
    )


def _control_account_for(effect: MovementInput, position: _Position) -> Account | None:
    """
    Which control account this effect's value moves through, and the refusal
    when an inbound would blend two of them.

    An **inbound** into standing stock must use the account the stock already
    sits in. Letting a re-mapped receipt land in a different account would put
    one position's value in two places with no journal between them — the
    trial balance would drift by exactly the amount nobody reclassified, and
    nothing downstream could tell which half was which.

    An inbound into an *empty* position establishes the account: there is
    nothing to strand, so a mapping changed since the position last emptied
    takes effect exactly where it should.

    An **outbound** leaves through the account it entered — never a fresh
    resolution. This is what stops an ISSUE crediting stock to an account it
    was never in (§D).

    A **value-only** revaluation follows the outbound rule: it restates money
    already sitting in an account, so that account is the only possible answer.

    Keyed on the resolved direction rather than on the movement type, so a
    signless type reaches the right branch — a manual adjustment that adds
    goods is an inbound and must face the reclassification refusal like any
    other.
    """
    if _direction_of(effect) is EffectDirection.IN:
        if position.quantity > ZERO and position.control_account is not None:
            if (
                effect.control_account is not None
                and effect.control_account.pk != position.control_account.pk
            ):
                raise ValidationError(
                    _(
                        "%(item)s in %(warehouse)s holds stock in account %(current)s; this "
                        "posting resolves %(new)s. Reclassifying standing stock value needs "
                        "an explicit GL reclassification, which does not exist yet."
                    ),
                    code="inventory_account_reclassification_required",
                    params={
                        "item": effect.item.code,
                        "warehouse": effect.warehouse.code,
                        "current": position.control_account.code,
                        "new": effect.control_account.code,
                    },
                )
            return position.control_account
        return effect.control_account
    return position.control_account or effect.control_account


def _write_movement(
    *,
    entry: StockLedgerEntry,
    effect: MovementInput,
    step: ValuationStep,
    posted_sequence: int,
    movement_type: str,
    control_account: Account | None = None,
    reverses: StockMovement | None = None,
) -> StockMovement:
    """Insert one immutable movement. Nothing ever updates one."""
    return StockMovement.objects.create(
        entry=entry,
        organization_id=effect.item.organization_id,
        branch_id=effect.warehouse.branch_id,
        warehouse=effect.warehouse,
        item=effect.item,
        lot=effect.lot,
        movement_type=movement_type,
        control_account=control_account,
        effect_key=effect.effect_key.strip(),
        base_quantity=quantize_quantity(step.quantity_delta),
        inventory_value=quantize_money(step.value_delta),
        unit_cost=step.unit_cost,
        quantity_before=step.quantity_before,
        quantity_after=step.quantity_after,
        value_before=step.value_before,
        value_after=step.value_after,
        average_before=step.average_before,
        average_after=step.average_after,
        posted_sequence=posted_sequence,
        reverses=reverses,
        source_conversion=effect.source_conversion,
        effective_at=entry.effective_at,
    )


def _save_position(position: _Position, *, movement: StockMovement, step: ValuationStep) -> None:
    """Advance the projection to where the ledger now is."""
    balance = position.balance
    balance.quantity = step.quantity_after
    balance.value = step.value_after
    balance.average_cost = step.average_after
    # The cycle's account identity: carried while stock stands, released when
    # the position empties. Clearing it at zero is what lets the next inbound
    # establish a newly effective mapping without a reclassification.
    balance.control_account = movement.control_account if step.quantity_after > ZERO else None
    balance.last_movement = movement
    balance.last_posted_sequence = movement.posted_sequence
    balance.version += 1
    balance.save(
        update_fields=[
            "quantity",
            "value",
            "average_cost",
            "control_account",
            "last_movement",
            "last_posted_sequence",
            "version",
            "updated_at",
        ]
    )
    position.quantity = step.quantity_after
    position.value = step.value_after
    position.average = step.average_after
    position.control_account = balance.control_account

    # Locations hold quantity only (ADR-018 §2) and are optional, but an issue
    # posted without naming a bin would leave the bins claiming more than the
    # warehouse holds. Releasing here keeps
    # `sum(located) + unlocated == warehouse quantity` true by construction on
    # every posting path rather than by asking each caller to remember.
    #
    # One indexed query and no writes for a warehouse that uses no locations,
    # which is most of them.
    from apps.inventory.locations import release_for_outbound

    release_for_outbound(
        warehouse_id=balance.warehouse_id,
        item_id=balance.item_id,
        lot_id=balance.lot_id,
        quantity_after=step.quantity_after,
        stock_movement=movement,
    )


def _record_layer(
    *, entry: StockLedgerEntry, effect: MovementInput, movement: StockMovement, step: ValuationStep
) -> None:
    """
    Capture an inbound as a valuation layer.

    Written even though a moving average never reads it. That is the point of
    ADR-018 §3: with layers captured, introducing FIFO later is a new
    consumption strategy over data that already exists. Without them it is a
    migration of history that cannot be reconstructed.
    """
    ValuationLayer.objects.create(
        organization_id=effect.item.organization_id,
        movement=movement,
        warehouse=effect.warehouse,
        item=effect.item,
        lot=effect.lot,
        received_quantity=quantize_quantity(step.quantity_delta),
        # Untouched under moving average: nothing consumes a layer, so
        # claiming it had been partly consumed would be a fabrication.
        remaining_quantity=quantize_quantity(step.quantity_delta),
        unit_cost=step.unit_cost,
        posted_sequence=movement.posted_sequence,
        effective_at=entry.effective_at,
    )


@transaction.atomic
def post_stock_entry(
    *,
    organization: Organization,
    effects: Sequence[MovementInput],
    idempotency_key: str,
    effective_at: datetime.datetime | None = None,
    business_date: datetime.date | None = None,
    source_document_type: str = "",
    source_document_id: str = "",
    source_event: str = "",
    reference: str = "",
    reason: str = "",
    owned_freezes: Sequence[int] = (),
) -> StockLedgerEntry:
    """
    Post a set of stock effects. The only way stock moves.

    Atomic: the entry, every movement, every balance update, and the audit
    event all commit or none do. A half-posted event is worse than a failed
    one, because it looks complete.

    Idempotent on two independent keys, because they answer different
    questions:

    * `idempotency_key` — "is this the same *command* again?" A retry after a
      network timeout returns the posting already made.
    * `organization + type + id + event` — "is this the same *economic event*
      again?" Goods receipt 145 posts once, whatever key the caller composes.

    | Presented | Result |
    |---|---|
    | same key, same payload | the existing posting |
    | same key, different payload | `idempotency_key_conflict` |
    | same key, another organization | an independent posting |
    | same source identity, different key | `source_event_already_posted` |

    `business_date` is the operational date the posting belongs to, and the
    date its accounting period is checked against. Callers with a document
    pass their **stored snapshot**, so a later change to the branch's cutoff
    cannot move a posted document between periods; the kernel derives it from
    the effects' own branches only when nobody above it has committed to one.

    `owned_freezes` names warehouse ids whose **physical-count freeze this
    transaction itself holds**. It exists for exactly one caller: the count
    approval that must post its own variance into the warehouse it froze, and
    would otherwise be refused by its own freeze. Passing it without holding
    the exclusive freeze lock and being the count that took it is a way to post
    into somebody else's count, so it is not a general escape hatch and there
    is deliberately no permission that grants it — `apps.inventory.counts` is
    the only caller, and a test holds that line.
    """
    if not effects:
        raise ValidationError(_("A stock posting needs at least one effect."), code="no_effects")

    source = canonical_source_identity(
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=source_event,
    )
    # Naming a document without naming its event means the original posting of
    # it. The lifecycle has exactly two values and `REVERSED` is written only
    # by `reverse_stock_entry`, so there is no other reading — and filling it
    # in here keeps a caller from producing a half-populated identity, which
    # would sit outside the uniqueness index while looking like it was inside.
    if source.document_type and source.document_id and not source.event:
        source = canonical_source_identity(
            source_document_type=source.document_type,
            source_document_id=source.document_id,
            source_event=SourceEvent.POSTED,
        )
    if source.is_present and not source.is_complete:
        raise ValidationError(
            _("A source identity needs a type, an id, and an event, or none of the three."),
            code="incomplete_source_identity",
        )
    if source.event and source.event not in SourceEvent.values:
        raise ValidationError(
            _("%(event)s is not a known source event."),
            code="unknown_source_event",
            params={"event": source.event},
        )

    moment = effective_at or timezone.now()
    fingerprint = request_fingerprint(
        command="post_stock_entry",
        # The caller's value, not `moment`. See `request_fingerprint`: hashing
        # the resolved `now()` would give every retry a different fingerprint.
        effective_at=effective_at,
        source=source.as_tuple(),
        effects=effects,
    )
    existing = _replay(
        organization=organization, idempotency_key=idempotency_key, fingerprint=fingerprint
    )
    if existing is not None:
        return existing

    if source.event:
        already = StockLedgerEntry.objects.filter(
            organization=organization,
            source_document_type=source.document_type,
            source_document_id=source.document_id,
            source_event=source.event,
        ).first()
        if already is not None:
            raise ValidationError(
                _("%(type)s %(id)s has already been recorded as %(event)s."),
                code="source_event_already_posted",
                params={
                    "type": source.document_type,
                    "id": source.document_id,
                    "event": source.event,
                },
            )

    effective_business_date = business_date or _business_date_of(effects, moment=moment)
    _validate_period_is_open(organization=organization, business_date=effective_business_date)

    seen: set[str] = set()
    for effect in effects:
        _validate_effect(effect, organization=organization)
        _validate_lot_is_not_expired(effect, on_date=effective_business_date)
        if effect.effect_key.strip() in seen:
            raise ValidationError(
                _("Effect key %(key)s appears twice in one posting."),
                code="duplicate_effect_key",
                params={"key": effect.effect_key},
            )
        seen.add(effect.effect_key.strip())

    entry = StockLedgerEntry.objects.create(
        organization=organization,
        source_document_type=source.document_type,
        source_document_id=source.document_id,
        source_event=source.event,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        effective_at=moment,
        business_date=effective_business_date,
        posted_at=timezone.now(),
        posted_by=_current_actor(),
        reference=reference.strip(),
        reason=reason.strip(),
    )

    # The warehouse freeze locks, then the mapping lock, then every key in
    # canonical order, then the counter. Two events naming the same two keys in
    # opposite caller order take them in the same database order and cannot
    # deadlock; a mapping mutation waiting on the exclusive form cannot slip
    # between a posting's account resolution and its commit; and a count
    # freezing a warehouse cannot slip between this check and this commit.
    acquire_warehouse_freeze_locks(effects)
    _require_warehouses_are_not_frozen(effects, owned_freezes=owned_freezes)
    _lock_mappings(organization)
    ordered = sorted(effects, key=lambda effect: _key_of(effect).sort_key)
    positions: dict[str, _Position] = {}
    for effect in ordered:
        key = _key_of(effect)
        if key.canonical not in positions:
            _lock_stock_key(key)
            positions[key.canonical] = _load_position(key, effect=effect)

    for effect in ordered:
        key = _key_of(effect)
        position = positions[key.canonical]
        _check_warehouse_is_not_frozen(position)
        _assert_position_is_coherent(effect=effect, position=position)

        # Resolved before the arithmetic, so a refused reclassification costs
        # nothing and leaves nothing half-applied.
        control_account = _control_account_for(effect, position)

        direction = _direction_of(effect)
        if direction is EffectDirection.IN:
            assert effect.unit_cost is not None  # noqa: S101 - guaranteed by _validate_effect
            step = apply_inbound(
                quantity=quantize_quantity(effect.quantity),
                unit_cost=effect.unit_cost,
                before_quantity=position.quantity,
                before_value=position.value,
                value_in=effect.inbound_value,
            )
        elif direction is EffectDirection.VALUE_ONLY:
            assert effect.value_adjustment is not None  # noqa: S101 - _validate_direction
            _require_position_can_be_revalued(effect=effect, position=position)
            step = apply_value_only(
                value_adjustment=effect.value_adjustment,
                before_quantity=position.quantity,
                before_value=position.value,
            )
            _require_revaluation_stays_positive(effect=effect, step=step)
        else:
            step = apply_outbound(
                quantity=quantize_quantity(effect.quantity),
                before_quantity=position.quantity,
                before_value=position.value,
                value_out=effect.outbound_value,
            )
            _require_available(effect=effect, step=step)
            if effect.outbound_value is not None:
                _require_exact_outbound_is_supported(effect=effect, position=position, step=step)

        sequence = _next_posted_sequence(organization=organization)
        movement = _write_movement(
            entry=entry,
            effect=effect,
            step=step,
            posted_sequence=sequence,
            movement_type=effect.movement_type,
            control_account=control_account,
        )
        _save_position(position, movement=movement, step=step)
        if direction is EffectDirection.IN:
            _record_layer(entry=entry, effect=effect, movement=movement, step=step)

    record_audit_event(
        action=AuditAction.POSTED,
        target=entry,
        new_state=snapshot(entry),
        reason=reason or "stock posting",
        source_document_type=source.document_type,
        source_document_id=source.document_id,
    )
    return entry


def acquire_warehouse_freeze_locks(effects: Sequence[MovementInput]) -> None:
    """
    Take the freeze lock for every warehouse an event touches, in shared mode.

    Public because a caller that posts more than one entry in one transaction —
    a transfer receipt, a count — must take them **once for the whole event**,
    before the first kernel call, for the same reason `acquire_stock_key_locks`
    exists: sorting within each call orders the locks by the order the calls
    happen to be written.
    """
    lock_warehouses_shared([effect.warehouse.pk for effect in effects])


def _require_warehouses_are_not_frozen(
    effects: Sequence[MovementInput], *, owned_freezes: Sequence[int] = ()
) -> None:
    """
    Refuse to post into a warehouse a count has frozen.

    Read **after** the freeze lock and **from the database**, not from the
    caller's `Warehouse` object: the caller may have loaded it before the
    freeze committed, and an in-memory copy of a stale row is exactly what this
    check must not trust.
    """
    warehouse_ids = sorted({effect.warehouse.pk for effect in effects} - set(owned_freezes))
    frozen = (
        Warehouse.objects.filter(pk__in=warehouse_ids, frozen_by_count__isnull=False)
        .order_by("code")
        .first()
    )
    if frozen is not None:
        raise ValidationError(
            _("Warehouse %(code)s is frozen for a physical count and accepts no postings."),
            code="warehouse_frozen",
            params={"code": frozen.code},
        )


def _check_warehouse_is_not_frozen(position: _Position) -> None:
    """
    A frozen **position** accepts nothing, in either direction.

    Per stock key, and distinct from the warehouse-wide freeze a count takes:
    that one is checked once per event in `_require_warehouses_are_not_frozen`,
    under a lock, because a warehouse-wide answer read per position would be
    the same answer computed many times and still racy.
    """
    if position.balance.is_frozen:
        raise ValidationError(
            _("Warehouse %(code)s is frozen for %(item)s and accepts no postings."),
            code="stock_position_frozen",
            params={
                "code": position.balance.warehouse.code,
                "item": position.balance.item.code,
            },
        )


def _require_position_can_be_revalued(*, effect: MovementInput, position: _Position) -> None:
    """
    A revaluation needs goods to revalue.

    Writing value onto an empty position would create the exact state
    `_assert_position_is_coherent` refuses to compute against — value standing
    against no quantity — and it would do so deliberately rather than by
    accident. If stock is genuinely there, the correction is a quantity gain
    with a cost, which says what was found; a value-only line says only that
    the money was wrong, and about nothing it cannot be.
    """
    if position.quantity <= ZERO:
        raise ValidationError(
            _("%(item)s in %(warehouse)s holds no quantity, so its value cannot be adjusted."),
            code="value_only_needs_quantity",
            params={"item": effect.item.code, "warehouse": effect.warehouse.code},
        )


def _require_revaluation_stays_positive(*, effect: MovementInput, step: ValuationStep) -> None:
    """Stock cannot be worth less than nothing, whoever authorized the write-down."""
    if step.value_after < ZERO:
        raise ValidationError(
            _(
                "Writing %(item)s in %(warehouse)s down by %(amount)s would leave a negative "
                "inventory value."
            ),
            code="value_only_would_go_negative",
            params={
                "item": effect.item.code,
                "warehouse": effect.warehouse.code,
                "amount": str(effect.value_adjustment),
            },
        )


def _assert_position_is_coherent(*, effect: MovementInput, position: _Position) -> None:
    """
    Refuse to compute against a position that is already impossible.

    Value sitting against no quantity, or negative value against positive
    quantity, are both unreachable through this service — the full-depletion
    rule and the availability check between them make sure of it. If one turns
    up anyway, something else wrote the row, and continuing would blend the
    corruption into an average that then looks legitimate.

    Failing loudly is the only honest response, and it names which of the two
    happened so the investigation starts in the right place.
    """
    if position.quantity == ZERO and position.value != ZERO:
        raise ValidationError(
            _("%(item)s in %(warehouse)s holds value %(value)s against no quantity."),
            code="residual_value_at_zero_quantity",
            params={
                "item": effect.item.code,
                "warehouse": effect.warehouse.code,
                "value": str(position.value),
            },
        )
    if position.quantity > ZERO and position.value < ZERO:
        raise ValidationError(
            _("%(item)s in %(warehouse)s holds negative value against positive quantity."),
            code="negative_value_with_positive_quantity",
            params={"item": effect.item.code, "warehouse": effect.warehouse.code},
        )


def _require_available(*, effect: MovementInput, step: ValuationStep) -> None:
    """
    Refuse to drive a position below zero. Refuse it for everyone.

    `inventory.override_negative_stock` exists in the vocabulary and is
    deliberately **not** consulted here. The approved moving-average contract
    does not define how a later receipt settles the valuation variance a
    negative position creates, and a permission cannot make an undefined
    accounting state valid. Activating the override is a separate task, after
    that policy is approved and tested — and it must relax the database
    constraint in the same migration, or the override would be refused by the
    very database it is an exception to.
    """
    if step.quantity_after < ZERO:
        raise ValidationError(
            _("%(item)s has %(available)s in %(warehouse)s; %(requested)s was requested."),
            code="insufficient_stock",
            params={
                "item": effect.item.code,
                "warehouse": effect.warehouse.code,
                "available": str(step.quantity_before),
                "requested": str(quantize_quantity(effect.quantity)),
            },
        )


def _require_exact_outbound_is_supported(
    *, effect: MovementInput, position: _Position, step: ValuationStep
) -> None:
    """
    Refuse an exact outbound value the position cannot actually support.

    Three ways it cannot, and none of them may be papered over by falling back
    to the pooled average — that fallback is precisely what would let a
    transfer receipt quietly take another transfer's money (ADR-020 §4):

    * the value asked for is not positive, so it is not a value at all;
    * it exceeds what the position holds, so the ledger cannot fund it;
    * it empties the quantity without emptying the value, or the reverse,
      which would leave the position in the incoherent state
      `_assert_position_is_coherent` refuses to compute against next time.

    Each is a disagreement between a caller's allocation and the ledger, and
    each is reported as one.
    """
    if step.value_after < ZERO:
        raise ValidationError(
            _(
                "%(item)s in %(warehouse)s holds %(available)s; this posting allocates "
                "%(requested)s out of it."
            ),
            code="allocated_value_exceeds_position_value",
            params={
                "item": effect.item.code,
                "warehouse": effect.warehouse.code,
                "available": str(position.value),
                "requested": str(quantize_money(-step.value_delta)),
            },
        )
    if step.quantity_after == ZERO and step.value_after != ZERO:
        raise ValidationError(
            _(
                "%(item)s in %(warehouse)s would empty to zero quantity holding "
                "%(residual)s of value."
            ),
            code="allocated_value_leaves_residual_at_zero_quantity",
            params={
                "item": effect.item.code,
                "warehouse": effect.warehouse.code,
                "residual": str(step.value_after),
            },
        )
    if step.quantity_after > ZERO and step.value_after == ZERO and position.value > ZERO:
        raise ValidationError(
            _("%(item)s in %(warehouse)s would keep %(quantity)s at no value at all."),
            code="allocated_value_strips_remaining_quantity",
            params={
                "item": effect.item.code,
                "warehouse": effect.warehouse.code,
                "quantity": str(step.quantity_after),
            },
        )


def link_journal_entry(*, entry: StockLedgerEntry, journal: JournalEntry) -> StockLedgerEntry:
    """
    Record which journal this stock posting produced. Once, and never again.

    The link is what makes the §S invariant expressible at the database: an
    entry that reached the general ledger must have said, for every dinar it
    moved, which control account moved it. Reconstructing that linkage by
    walking every kind of document that might reference the entry would need
    extending on every task that adds one — and would be silently incomplete
    on the task that forgot.

    The entry immutability trigger allows this single NULL to non-NULL write
    and nothing else, so it cannot become a back door to editing history.
    """
    if entry.journal_entry_id is not None:
        raise ValidationError(
            _("Stock posting %(id)s already names a journal."),
            code="stock_entry_already_journalled",
            params={"id": entry.pk},
        )
    entry.journal_entry = journal
    entry.save(update_fields=["journal_entry", "updated_at"])
    return entry


def _current_actor() -> User | None:
    """
    The acting user bound by the audit context, if there is one.

    Imported lazily for the same reason the accounting kernel does: the
    context is request-scoped state, and a module-level import would tie the
    kernel to a web request it should know nothing about.
    """
    from apps.core.context import get_actor

    return get_actor()


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_stock_entry(
    *,
    entry: StockLedgerEntry,
    idempotency_key: str,
    reason: str,
    effective_at: datetime.datetime | None = None,
    business_date: datetime.date | None = None,
) -> StockLedgerEntry:
    """
    Post the exact opposite of an entry, as a new immutable entry.

    Each reversing movement mirrors its original's quantity and value —
    **not** today's average. Reversing a receipt of 100 kg that cost 500,000
    removes 100 kg and 500,000, whatever the average has since become;
    anything else would let a reversal create value out of the passage of time.

    Availability still applies. Receipt +100, issue -80, and a request to
    reverse the receipt is refused: the goods are no longer there to take back,
    and the issue must be corrected first. Exempting reversals from the check
    would make "reverse the receipt" the standard way to drive a balance
    negative, which is the one thing the check exists to prevent.
    """
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")

    if entry.reverses_id is not None:
        raise ValidationError(
            _("A reversal cannot itself be reversed. Post a fresh entry instead."),
            code="cannot_reverse_a_reversal",
        )
    if StockLedgerEntry.objects.filter(reverses=entry).exists():
        raise ValidationError(_("This posting has already been reversed."), code="already_reversed")

    organization = entry.organization
    moment = effective_at or timezone.now()
    fingerprint = hashlib.sha256(
        json.dumps({"command": "reverse_stock_entry", "entry": entry.pk}, sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()
    existing = _replay(
        organization=organization, idempotency_key=idempotency_key, fingerprint=fingerprint
    )
    if existing is not None:
        return existing

    originals = list(
        entry.movements.select_related("warehouse", "warehouse__branch", "item", "lot").order_by(
            "posted_sequence"
        )
    )
    if not originals:  # pragma: no cover - an entry always has movements
        raise ValidationError(_("Nothing to reverse."), code="no_movements")

    # A reversal is dated by when it is *made*, not by what it corrects: it is
    # a new economic event in the current period, and backdating it into the
    # original's closed month is what the reversal exists to avoid.
    reversal_business_date = business_date or _business_date_of(
        [_effect_of(movement) for movement in originals], moment=moment
    )
    _validate_period_is_open(organization=organization, business_date=reversal_business_date)

    reversal = StockLedgerEntry.objects.create(
        organization=organization,
        # The reversal is a distinct economic event about the same document,
        # so it carries the same type and id with the REVERSED event. That is
        # what keeps `POSTED` and `REVERSED` one document in every report.
        source_document_type=entry.source_document_type,
        source_document_id=entry.source_document_id,
        source_event=SourceEvent.REVERSED if entry.source_event else "",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        effective_at=moment,
        business_date=reversal_business_date,
        posted_at=timezone.now(),
        posted_by=_current_actor(),
        reference=entry.reference,
        reason=reason.strip(),
        reverses=entry,
    )

    reversal_effects = [_effect_of(movement) for movement in originals]
    acquire_warehouse_freeze_locks(reversal_effects)
    _require_warehouses_are_not_frozen(reversal_effects)
    _lock_mappings(organization)
    ordered = sorted(
        originals,
        key=lambda movement: (
            movement.warehouse_id,
            movement.item_id,
            movement.lot_id or 0,
        ),
    )
    positions: dict[str, _Position] = {}
    for original in ordered:
        effect = _effect_of(original)
        key = _key_of(effect)
        if key.canonical not in positions:
            _lock_stock_key(key)
            positions[key.canonical] = _load_position(key, effect=effect)

    for original in ordered:
        effect = _effect_of(original)
        position = positions[_key_of(effect).canonical]
        _check_warehouse_is_not_frozen(position)

        step = apply_reversal(
            quantity_delta=-original.base_quantity,
            value_delta=-original.inventory_value,
            before_quantity=position.quantity,
            before_value=position.value,
        )
        if step.quantity_after < ZERO:
            raise ValidationError(
                _(
                    "Reversing %(item)s would leave %(warehouse)s below zero: "
                    "%(available)s on hand, %(requested)s to remove."
                ),
                code="insufficient_stock",
                params={
                    "item": original.item.code,
                    "warehouse": original.warehouse.code,
                    "available": str(position.quantity),
                    "requested": str(original.base_quantity),
                },
            )
        if step.value_after < ZERO:
            # Reversing a value-only write-up whose value has since been issued
            # away. The quantity check above cannot see it, because a
            # revaluation moved no quantity for it to see.
            raise ValidationError(
                _(
                    "Reversing %(item)s would leave %(warehouse)s holding a negative "
                    "inventory value."
                ),
                code="reversal_would_go_negative",
                params={
                    "item": original.item.code,
                    "warehouse": original.warehouse.code,
                },
            )

        sequence = _next_posted_sequence(organization=organization)
        movement = _write_movement(
            entry=reversal,
            effect=effect,
            step=step,
            posted_sequence=sequence,
            movement_type=MovementType.REVERSAL,
            # The original's account, not a fresh resolution: a mirror moves
            # the same money the same way back, whatever the chart says now.
            control_account=original.control_account,
            reverses=original,
        )
        _save_position(position, movement=movement, step=step)

    record_audit_event(
        action=AuditAction.REVERSED,
        target=reversal,
        new_state=snapshot(reversal),
        reason=reason,
        source_document_type=reversal.source_document_type,
        source_document_id=reversal.source_document_id,
    )
    return reversal


def _effect_of(movement: StockMovement) -> MovementInput:
    """
    Reconstruct the shape of a movement so the reversal path can reuse the
    same locking and writing code. The quantity is only used for its key and
    its warehouse; the arithmetic comes from `apply_reversal`.
    """
    return MovementInput(
        warehouse=movement.warehouse,
        item=movement.item,
        movement_type=MovementType.REVERSAL,
        quantity=abs(movement.base_quantity),
        effect_key=f"reverse:{movement.effect_key}",
        lot=movement.lot,
        source_conversion=movement.source_conversion,
        control_account=movement.control_account,
    )
