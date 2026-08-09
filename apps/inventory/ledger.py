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

## Locking

Two things must be serialised, and they are taken in a fixed order:

1. the stock keys the posting touches, sorted canonically;
2. then the organization's posted-order counter.

Always that order. A transaction holding a key and waiting for the counter,
against one holding the counter and waiting for that key, is a deadlock — and
it is the deadlock that a "lock the counter first, it's quick" instinct
produces.

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
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import PeriodState, SourceEvent
from apps.accounting.services import resolve_period
from apps.core.models import AuditAction
from apps.core.money import MONEY_PLACES, quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.core.source_identity import canonical_source_identity
from apps.inventory.models import (
    INBOUND_MOVEMENT_TYPES,
    OUTBOUND_MOVEMENT_TYPES,
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
)
from apps.organizations.models import Organization

if TYPE_CHECKING:
    # Typing only. The kernel reads the acting user from the ambient audit
    # context and never takes one as an argument, so importing the model at
    # runtime would suggest a coupling that does not exist.
    from apps.users.models import User

ZERO_MONEY = Decimal(0).scaleb(-MONEY_PLACES)
ZERO = Decimal("0")

#: The costing method this kernel implements. Named on every allocation that
#: is ever written, so a later strategy cannot be mistaken for this one.
MOVING_WEIGHTED_AVERAGE = "MOVING_WEIGHTED_AVERAGE"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MovementInput:
    """
    One requested effect: a quantity of one item, in one warehouse.

    `quantity` is always **positive** — the direction comes from
    `movement_type`, which is a closed set with a fixed sign. A caller that
    could pass a negative receipt would be able to issue stock through the
    receipt path and bypass the availability check.
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
    *, quantity: Decimal, unit_cost: Decimal, before_quantity: Decimal, before_value: Decimal
) -> ValuationStep:
    """
    Add goods at a known cost and re-blend the average.

        value_in    = quantize_money(quantity x unit_cost)
        new_value   = old_value + value_in
        new_average = quantize_unit_price(new_value / new_quantity)

    The average is derived from the totals rather than the totals from the
    average. Deriving it the other way accumulates the rounding error of every
    receipt into the stored value, and the ledger stops summing.
    """
    value_in = quantize_money(quantity * unit_cost)
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
    *, quantity: Decimal, before_quantity: Decimal, before_value: Decimal
) -> ValuationStep:
    """
    Remove goods at the current average — except at the last one out.

    Bringing the balance to exactly zero takes the **entire remaining value**,
    not `quantity x average`. That is what makes `Q == 0 => V == 0` true by
    construction rather than by an adjusting entry, and it never mutates a
    movement that has already been posted.

    Availability is the caller's business: this is arithmetic, and refusing
    here would put the negative-stock policy in two places.
    """
    before_average = (
        quantize_unit_price(before_value / before_quantity) if before_quantity > ZERO else ZERO
    )
    after_quantity = quantize_quantity(before_quantity - quantity)

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


def _validate_period_is_open(
    *, organization: Organization, effective_at: datetime.datetime
) -> None:
    """
    Stock postings need an OPEN period.

    Task 1.2 has no soft-close override: a stock movement changes what the
    accounts will say, and a period that has stopped accepting entries has
    stopped accepting the things that cause them. The count-adjustment
    exception arrives with the count document in Task 1.6, attached to a real
    document rather than to a flag.
    """
    period = resolve_period(organization=organization, accounting_date=effective_at.date())
    if period.state != PeriodState.OPEN:
        raise ValidationError(
            _("Period %(period)s is %(state)s and does not accept stock postings."),
            code="period_not_open",
            params={"period": str(period), "state": period.state},
        )


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
    if effect.quantity <= ZERO:
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

    _validate_lot(effect)

    if effect.movement_type in INBOUND_MOVEMENT_TYPES:
        if effect.unit_cost is None:
            raise ValidationError(
                _("An inbound movement needs a unit cost."), code="unit_cost_required"
            )
        if effect.unit_cost < ZERO:
            raise ValidationError(_("A unit cost cannot be negative."), code="unit_cost_negative")


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
    """
    if effect.lot is None or effect.movement_type not in OUTBOUND_MOVEMENT_TYPES:
        return
    if effect.movement_type == MovementType.WASTE:
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
    locked = StockBalance.objects.select_for_update().get(pk=balance.pk)
    return _Position(
        key=key,
        balance=locked,
        quantity=locked.quantity,
        value=locked.value,
        average=locked.average_cost,
    )


def _write_movement(
    *,
    entry: StockLedgerEntry,
    effect: MovementInput,
    step: ValuationStep,
    posted_sequence: int,
    movement_type: str,
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
    balance.last_movement = movement
    balance.last_posted_sequence = movement.posted_sequence
    balance.version += 1
    balance.save(
        update_fields=[
            "quantity",
            "value",
            "average_cost",
            "last_movement",
            "last_posted_sequence",
            "version",
            "updated_at",
        ]
    )
    position.quantity = step.quantity_after
    position.value = step.value_after
    position.average = step.average_after


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
    source_document_type: str = "",
    source_document_id: str = "",
    source_event: str = "",
    reference: str = "",
    reason: str = "",
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

    _validate_period_is_open(organization=organization, effective_at=moment)

    seen: set[str] = set()
    for effect in effects:
        _validate_effect(effect, organization=organization)
        _validate_lot_is_not_expired(effect, on_date=moment.date())
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
        posted_at=timezone.now(),
        posted_by=_current_actor(),
        reference=reference.strip(),
        reason=reason.strip(),
    )

    # Lock every key first, in canonical order, then the counter. Two events
    # naming the same two keys in opposite caller order take them in the same
    # database order and cannot deadlock.
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

        if effect.movement_type in INBOUND_MOVEMENT_TYPES:
            assert effect.unit_cost is not None  # noqa: S101 - guaranteed by _validate_effect
            step = apply_inbound(
                quantity=quantize_quantity(effect.quantity),
                unit_cost=effect.unit_cost,
                before_quantity=position.quantity,
                before_value=position.value,
            )
        else:
            step = apply_outbound(
                quantity=quantize_quantity(effect.quantity),
                before_quantity=position.quantity,
                before_value=position.value,
            )
            _require_available(effect=effect, step=step)

        sequence = _next_posted_sequence(organization=organization)
        movement = _write_movement(
            entry=entry,
            effect=effect,
            step=step,
            posted_sequence=sequence,
            movement_type=effect.movement_type,
        )
        _save_position(position, movement=movement, step=step)
        if effect.movement_type in INBOUND_MOVEMENT_TYPES:
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


def _check_warehouse_is_not_frozen(position: _Position) -> None:
    """A frozen position accepts nothing, in either direction."""
    if position.balance.is_frozen:
        raise ValidationError(
            _("Warehouse %(code)s is frozen for %(item)s and accepts no postings."),
            code="stock_position_frozen",
            params={
                "code": position.balance.warehouse.code,
                "item": position.balance.item.code,
            },
        )
    if getattr(position.balance.warehouse, "is_frozen", False):  # pragma: no cover - Task 1.6
        raise ValidationError(
            _("Warehouse %(code)s is frozen."),
            code="warehouse_frozen",
            params={"code": position.balance.warehouse.code},
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

    _validate_period_is_open(organization=organization, effective_at=moment)

    originals = list(
        entry.movements.select_related("warehouse", "warehouse__branch", "item", "lot").order_by(
            "posted_sequence"
        )
    )
    if not originals:  # pragma: no cover - an entry always has movements
        raise ValidationError(_("Nothing to reverse."), code="no_movements")

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
        posted_at=timezone.now(),
        posted_by=_current_actor(),
        reference=entry.reference,
        reason=reason.strip(),
        reverses=entry,
    )

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

        sequence = _next_posted_sequence(organization=organization)
        movement = _write_movement(
            entry=reversal,
            effect=effect,
            step=step,
            posted_sequence=sequence,
            movement_type=MovementType.REVERSAL,
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
    )
