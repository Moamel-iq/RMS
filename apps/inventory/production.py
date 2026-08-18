"""
The narrow public interface a production document posts through.

## Why this module exists at all

`PRODUCTION_IN` and `PRODUCTION_OUT` have been inventory's own movement types
since Phase 1, and `InventoryLot.produced_by_document_type` has carried the
comment "nothing writes them in Phase 1" for just as long. This is what writes
them. Nothing here knows what a recipe is, what a batch multiplier is, or that
any recipe module exists: it takes a warehouse, some quantities to consume, one
quantity to produce, and a source identity, and it does what inventory does
with movements. The dependency arrow is unchanged: the production document
calls this, and this calls nothing above it.

It exists as its own module rather than as another branch inside
`operations.py` because a production event is not an `InventoryMovementDocument`
and never becomes one. Bolting it onto the document planner would have meant a
fifth document type that no `InventoryDocumentType` enum should contain.

## The one hard problem: the output's value

Value conservation says the produced goods are worth exactly what was consumed
to make them (ADR-025, RCP-034). The consumed value is the kernel's own moving
average and is only known once the outbound movements have been valued — but
the whole event has to be **one** stock ledger entry, because a production
batch is one economic event with one source identity, and the entry is where
that identity lives when there is no journal.

So the outbound values are **projected before the call and posted in it**:

1. take the same locks the kernel will take, in the same order — the warehouse
   freezes, the organization's mapping lock, then every stock key the event
   touches, canonically sorted;
2. read each input position and replay `ledger.apply_outbound` over it, in the
   kernel's own canonical order, accumulating positions locally;
3. sum the value deltas — that is the output's `inbound_value`;
4. hand the whole set, inputs and output together, to `post_stock_entry`.

The projection is exact rather than an approximation because it *calls the
kernel's arithmetic* rather than restating it, and because nothing can move
between step 1 and step 4: the locks are already held and are re-entrant, so
the kernel's own acquisitions inside `post_stock_entry` are no-ops.

The alternative — recompute `quantity x average` here — would have been a
second implementation of the exact-depletion rule (ADR-018 §4), and the two
would have disagreed the first time a batch emptied a position.

`assert_projection_matched` closes the loop afterwards by comparing the
projection against what the kernel actually wrote. It is cheap, it runs on
every posting, and it means a future change to either side is a failed posting
rather than a silent value leak.

## The journal, and its legitimate silence

Written here rather than by the caller, for the same reason the movements are:
the nets are per **control account**, and the control account of an outbound is
a fact about the position it left, which only this layer can see. Inputs leave
through the accounts their balances carry (ADR-019 §7); the output enters
through the account the caller resolved for it.

When every account nets to zero — the common case, one shared inventory control
account — **no journal is written at all**, and that is a correct posting, not
a failed one. Two entries that always net to zero are motion without
information (RCP-036).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INVENTORY_CONTROL,
    Account,
    AccountingPeriod,
    JournalEntry,
    SourceEvent,
)
from apps.accounting.services import post_entry, resolve_period, reverse_entry
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.inventory.accounts import resolve_inventory_account
from apps.inventory.ledger import (
    MovementInput,
    acquire_stock_key_locks,
    acquire_warehouse_freeze_locks,
    apply_outbound,
    link_journal_entry,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.locations import pick
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockLocation,
    StockMovement,
    Warehouse,
)
from apps.organizations.models import Branch, Organization

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionConsumption:
    """One quantity of one item, out of one lot and optionally one bin."""

    warehouse: Warehouse
    item: InventoryItem
    quantity: Decimal
    effect_key: str
    lot: InventoryLot | None = None
    location: StockLocation | None = None


@dataclass(frozen=True)
class ProductionYield:
    """The one thing the event produces."""

    warehouse: Warehouse
    item: InventoryItem
    quantity: Decimal
    effect_key: str
    lot: InventoryLot | None = None
    #: Resolved by the caller through the ordinary effective-dated mapping.
    #: Meaningful on an inbound: it establishes the position's account when the
    #: position is empty and must match the standing account when it is not.
    control_account: Account | None = None


@dataclass(frozen=True)
class ProductionPosting:
    """What one posted production event produced, keyed for the caller."""

    entry: StockLedgerEntry
    #: `effect_key` -> the movement the kernel wrote.
    movements: dict[str, StockMovement]
    output_movement: StockMovement
    consumed_value: Decimal
    output_value: Decimal
    journal: JournalEntry | None

    @property
    def has_journal(self) -> bool:
        return self.journal is not None


# ---------------------------------------------------------------------------
# What the caller needs resolved, and must not resolve for itself
# ---------------------------------------------------------------------------


def resolve_output_control_account(
    *, organization: Organization, item: InventoryItem, on_date: datetime.date
) -> Account:
    """
    The `INVENTORY_CONTROL` account the produced goods enter, on that date.

    Exposed here so a production document does not have to reach into the
    account-mapping machinery itself. The role is resolved through the same
    effective-dated policy every other inbound uses — a produced item is
    inventory, and no production-specific role exists or should (spec §15).
    """
    return resolve_inventory_account(
        organization=organization,
        role=INVENTORY_CONTROL,
        item=item,
        on_date=on_date,
    ).account


def production_period(
    *, organization: Organization, business_date: datetime.date
) -> AccountingPeriod:
    """
    The open period the batch's business date falls in, or a named refusal.

    The kernel checks this again inside `post_stock_entry`, and that is the
    check that actually protects the ledger. This one exists so the caller can
    fail **before** it draws a gapless document number — a number consumed by a
    posting that a closed period then refused is a permanent gap.
    """
    period = resolve_period(organization=organization, accounting_date=business_date)
    validate_period_accepts_postings(period)
    return period


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def project_consumed_value(consumptions: Sequence[ProductionConsumption]) -> Decimal:
    """
    What these consumptions will be worth, under locks the caller already holds.

    Replays `ledger.apply_outbound` in the kernel's own canonical key order,
    accumulating each position as it goes — so two effects against the same
    position see the second one at the average the first one left, exactly as
    the kernel will.

    Callers must already hold the stock key locks (see `post_production_entry`,
    which is the only intended caller and takes them first). Reading a position
    without its lock would project a value another transaction could change
    before the posting used it.
    """
    positions: dict[tuple[int, int, int], tuple[Decimal, Decimal]] = {}
    total = ZERO

    for consumption in _canonical(consumptions):
        key = _position_key(consumption)
        if key not in positions:
            balance = StockBalance.objects.filter(
                warehouse=consumption.warehouse,
                item=consumption.item,
                lot=consumption.lot,
            ).first()
            positions[key] = (
                (balance.quantity, balance.value) if balance is not None else (ZERO, ZERO)
            )

        before_quantity, before_value = positions[key]
        step = apply_outbound(
            quantity=quantize_quantity(consumption.quantity),
            before_quantity=before_quantity,
            before_value=before_value,
        )
        positions[key] = (step.quantity_after, step.value_after)
        # `value_delta` is negative on an outbound: value leaving the position.
        total += -step.value_delta

    return quantize_money(total)


def assert_projection_matched(*, projected: Decimal, movements: Sequence[StockMovement]) -> None:
    """
    The projection against what the kernel actually wrote.

    A closed loop rather than an act of faith. If this ever fires, the two
    implementations of outbound valuation have diverged and the correct outcome
    is a refused posting — a production event that silently created or
    destroyed value would reconcile against nothing afterwards.
    """
    posted = quantize_money(sum((-movement.inventory_value for movement in movements), ZERO))
    if posted != projected:  # pragma: no cover - a kernel divergence, not a state
        raise ValidationError(
            _(
                "The projected consumption value %(projected)s does not match the posted "
                "%(posted)s. Nothing was posted."
            ),
            code="production_value_projection_diverged",
            params={"projected": str(projected), "posted": str(posted)},
        )


def _canonical(
    consumptions: Sequence[ProductionConsumption],
) -> list[ProductionConsumption]:
    """The kernel's own ordering: warehouse, item, lot — then the effect key."""
    return sorted(
        consumptions,
        key=lambda row: (*_position_key(row), row.effect_key),
    )


def _position_key(consumption: ProductionConsumption) -> tuple[int, int, int]:
    return (
        consumption.warehouse.pk,
        consumption.item.pk,
        consumption.lot.pk if consumption.lot is not None else 0,
    )


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def _effect_of_consumption(consumption: ProductionConsumption) -> MovementInput:
    return MovementInput(
        warehouse=consumption.warehouse,
        item=consumption.item,
        movement_type=MovementType.PRODUCTION_OUT,
        quantity=consumption.quantity,
        effect_key=consumption.effect_key,
        lot=consumption.lot,
    )


def _effect_of_yield(
    produced: ProductionYield, *, unit_cost: Decimal, value: Decimal
) -> MovementInput:
    return MovementInput(
        warehouse=produced.warehouse,
        item=produced.item,
        movement_type=MovementType.PRODUCTION_IN,
        quantity=produced.quantity,
        effect_key=produced.effect_key,
        lot=produced.lot,
        unit_cost=unit_cost,
        control_account=produced.control_account,
        # The exact-figure channel the kernel built for returns and transfer
        # receipts. The output is worth what the inputs were worth; it is not
        # `quantity x unit_cost` re-derived and rounded a second time.
        inbound_value=value,
    )


@transaction.atomic
def post_production_entry(
    *,
    organization: Organization,
    branch: Branch,
    consumptions: Sequence[ProductionConsumption],
    produced: ProductionYield,
    business_date: datetime.date,
    effective_at: datetime.datetime,
    idempotency_key: str,
    source_document_type: str,
    source_document_id: str,
    posting_rule_version: str,
    reference: str = "",
    reason: str = "",
) -> ProductionPosting:
    """
    Consume a set of inputs and produce one output, atomically, as one entry.

    Value is conserved to the fils: the output's inbound value is exactly the
    sum of the consumed movements' values, and `assert_projection_matched`
    proves it against what the kernel wrote rather than assuming it.

    The journal is written **only if the per-account nets need one**. A batch
    whose inputs and output share one inventory control account nets to zero on
    every account, and no `JournalEntry` row is created — see the module
    docstring, and RCP-036.
    """
    if not consumptions:
        raise ValidationError(
            _("A production posting needs at least one consumed input."),
            code="production_has_no_inputs",
        )
    if quantize_quantity(produced.quantity) <= ZERO:
        raise ValidationError(
            _("A production posting needs a positive output quantity."),
            code="production_output_not_positive",
        )

    effects = [_effect_of_consumption(row) for row in consumptions]
    output_probe = MovementInput(
        warehouse=produced.warehouse,
        item=produced.item,
        movement_type=MovementType.PRODUCTION_IN,
        quantity=produced.quantity,
        effect_key=produced.effect_key,
        lot=produced.lot,
        unit_cost=Decimal("1"),
    )

    # The kernel's lock order, taken here for the *whole* event so the
    # projection below reads positions nothing else can move. Re-entrant, so
    # `post_stock_entry`'s own acquisitions are no-ops.
    acquire_warehouse_freeze_locks([*effects, output_probe])
    acquire_stock_key_locks([*effects, output_probe])

    consumed_value = project_consumed_value(consumptions)
    if consumed_value <= ZERO:
        raise ValidationError(
            _(
                "The inputs of this production carry no book value, so the output would "
                "be free stock. Value the inputs first."
            ),
            code="production_consumed_value_not_positive",
        )

    quantity = quantize_quantity(produced.quantity)
    entry = post_stock_entry(
        organization=organization,
        effects=[
            *effects,
            _effect_of_yield(
                produced,
                # Carried for the movement's own unit-cost column, at unit
                # price precision rather than money precision — this is a rate,
                # not an amount. The *value* is the exact figure above and is
                # never re-derived from it, which is what keeps yield loss in
                # the unit cost instead of in a rounding difference.
                unit_cost=quantize_unit_price(consumed_value / quantity),
                value=consumed_value,
            ),
        ],
        idempotency_key=idempotency_key,
        effective_at=effective_at,
        business_date=business_date,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=SourceEvent.POSTED,
        reference=reference,
        reason=reason,
    )

    movements = {movement.effect_key: movement for movement in entry.movements.all()}
    consumed_movements = [movements[row.effect_key] for row in consumptions]
    assert_projection_matched(projected=consumed_value, movements=consumed_movements)

    output_movement = movements[produced.effect_key]
    _release_named_locations(consumptions, movements=movements)

    journal = _journal_for(
        organization=organization,
        branch=branch,
        consumed=consumed_movements,
        output=output_movement,
        business_date=business_date,
        idempotency_key=f"{idempotency_key}:journal",
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        posting_rule_version=posting_rule_version,
        reason=reason,
    )
    if journal is not None:
        link_journal_entry(entry=entry, journal=journal)

    return ProductionPosting(
        entry=entry,
        movements=movements,
        output_movement=output_movement,
        consumed_value=consumed_value,
        output_value=quantize_money(output_movement.inventory_value),
        journal=journal,
    )


def _release_named_locations(
    consumptions: Sequence[ProductionConsumption],
    *,
    movements: dict[str, StockMovement],
) -> None:
    """
    Take the named bins down by what left them.

    The kernel already keeps `sum(located) <= warehouse quantity` true through
    `release_for_outbound`, which empties bins in code order when nobody names
    one. That is a safety net, not a picking record. When the operator *did*
    name a bin, `pick` records the movement against that bin — which is what
    makes "where did the rice for batch 12 come from" answerable.

    Ordered by location id so two postings naming the same two bins in opposite
    order cannot deadlock against each other.
    """
    named = sorted(
        (row for row in consumptions if row.location is not None),
        key=lambda row: (row.location.pk if row.location else 0, row.effect_key),
    )
    for row in named:
        assert row.location is not None  # noqa: S101 - filtered above
        pick(
            location=row.location,
            item=row.item,
            lot=row.lot,
            quantity=row.quantity,
            stock_movement=movements[row.effect_key],
            reference=f"production:{row.effect_key}",
        )


def _journal_for(
    *,
    organization: Organization,
    branch: Branch,
    consumed: Sequence[StockMovement],
    output: StockMovement,
    business_date: datetime.date,
    idempotency_key: str,
    source_document_type: str,
    source_document_id: str,
    posting_rule_version: str,
    reason: str,
) -> JournalEntry | None:
    """
    The per-account net of one production event, or `None` when it is zero.

    Value **leaves** an input's control account and **enters** the output's.
    Both sides are netted per account, so a batch that consumes three items
    homed to one account and produces into another writes two lines and not
    four. An account whose net is exactly zero contributes no line, and when
    every net is zero there is nothing to say and nothing is written.
    """
    nets: dict[int, Decimal] = {}
    accounts: dict[int, Account] = {}

    for movement in consumed:
        account = movement.control_account
        if account is None:
            continue
        accounts[account.pk] = account
        # An outbound's `inventory_value` is negative: value leaving.
        nets[account.pk] = nets.get(account.pk, ZERO) + movement.inventory_value

    if output.control_account is not None:
        accounts[output.control_account.pk] = output.control_account
        nets[output.control_account.pk] = (
            nets.get(output.control_account.pk, ZERO) + output.inventory_value
        )

    lines: list[PostingLine] = []
    for account_id, net in sorted(nets.items(), key=lambda pair: accounts[pair[0]].code):
        amount = quantize_money(net)
        if amount == ZERO:
            continue
        lines.append(
            PostingLine(
                account=accounts[account_id],
                branch=branch,
                debit=amount if amount > ZERO else ZERO,
                credit=-amount if amount < ZERO else ZERO,
            )
        )

    if not lines:
        # The legitimate silence. Every account nets to zero, the stock ledger
        # carries the event's identity, and a journal saying nothing is not
        # written (RCP-036).
        return None

    return post_entry(
        organization=organization,
        accounting_date=business_date,
        lines=lines,
        idempotency_key=idempotency_key,
        document_date=business_date,
        narration=reason,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=SourceEvent.POSTED,
        posting_rule_version=posting_rule_version,
    )


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionReversal:
    entry: StockLedgerEntry
    journal: JournalEntry | None


@transaction.atomic
def reverse_production_entry(
    *,
    entry: StockLedgerEntry,
    journal: JournalEntry | None,
    idempotency_key: str,
    reason: str,
    business_date: datetime.date,
    effective_at: datetime.datetime,
) -> ProductionReversal:
    """
    Mirror a posted production event exactly.

    Every ingredient comes back at the value it left at and the output leaves
    at the value it entered at, whatever the averages have since become —
    `reverse_stock_entry` already guarantees that, and already refuses when the
    output is no longer there to take back, which is precisely the refusal a
    production reversal needs (RCP-040).

    A batch that correctly wrote no journal correctly writes no reversal
    journal. Creating one here "for symmetry" would put a pair of entries into
    the ledger for an event the ledger never recorded.
    """
    reversing = reverse_stock_entry(
        entry=entry,
        idempotency_key=idempotency_key,
        reason=reason,
        effective_at=effective_at,
        business_date=business_date,
    )
    reversing_journal: JournalEntry | None = None
    if journal is not None:
        reversing_journal = reverse_entry(
            entry=journal,
            idempotency_key=f"{idempotency_key}:journal",
            reason=reason,
            accounting_date=business_date,
        )
        link_journal_entry(entry=reversing, journal=reversing_journal)
    return ProductionReversal(entry=reversing, journal=reversing_journal)
