"""
Replay the stock ledger and compare it with the projection.

`StockBalance` is a cache. The movements are the truth. This module recomputes
the cache from the truth and reports where they differ.

**A mismatch is a defect, not a chore.** Nothing here writes to `StockBalance`,
and that is deliberate: a projection that can be quietly corrected proves
nothing, because the correction erases the evidence of whatever caused the
divergence. The command reports; a human decides.

The rebuild order is `posted_sequence` — the order valuation was actually
computed in — and never `effective_at`. Replaying by effective date would
produce a different, defensible-looking set of averages that no report has
ever shown, and then "disagrees with the ledger" would mean nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import QuerySet

from apps.core.money import quantize_money
from apps.inventory.models import StockBalance, StockMovement
from apps.organizations.models import Organization

ZERO = Decimal("0")


@dataclass(frozen=True)
class ReplayedPosition:
    """What the ledger says one `(warehouse, item, lot)` should hold."""

    warehouse_id: int
    item_id: int
    lot_id: int | None
    quantity: Decimal
    value: Decimal
    last_posted_sequence: int


@dataclass(frozen=True)
class Mismatch:
    """One stock position where the projection and the ledger disagree."""

    organization_code: str
    branch_code: str
    warehouse_code: str
    item_code: str
    lot_code: str | None
    field: str
    projected: Decimal | int
    replayed: Decimal | int

    def __str__(self) -> str:
        lot = f"/{self.lot_code}" if self.lot_code else ""
        return (
            f"{self.organization_code}/{self.branch_code}/{self.warehouse_code} "
            f"{self.item_code}{lot}: {self.field} "
            f"projected={self.projected} replayed={self.replayed}"
        )


def replay_movements(
    movements: QuerySet[StockMovement],
) -> dict[tuple[int, int, int], ReplayedPosition]:
    """
    Fold a movement stream into positions, in posted order.

    Pure in everything that matters: it reads movements and returns numbers.
    It writes nothing, so it can be run against production safely and its
    result compared with whatever the projection currently claims.

    Each movement already carries its own arithmetic, so this **adds the
    deltas** rather than re-deriving them. Re-deriving would test the replay
    against itself; adding what was actually posted tests the projection
    against the ledger, which is the question being asked.
    """
    positions: dict[tuple[int, int, int], ReplayedPosition] = {}
    for movement in movements.order_by("posted_sequence").iterator():
        key = (movement.warehouse_id, movement.item_id, movement.lot_id or 0)
        current = positions.get(key)
        quantity = (current.quantity if current else ZERO) + movement.base_quantity
        value = (current.value if current else ZERO) + movement.inventory_value
        # The same rule the kernel applies, so a position emptied by a
        # full-depletion movement replays to exactly zero rather than to a
        # rounding residual.
        if quantity == ZERO:
            value = ZERO
        positions[key] = ReplayedPosition(
            warehouse_id=movement.warehouse_id,
            item_id=movement.item_id,
            lot_id=movement.lot_id,
            quantity=quantity,
            value=quantize_money(value),
            last_posted_sequence=int(movement.posted_sequence),
        )
    return positions


def _mismatch(
    balance: StockBalance, field: str, projected: Decimal | int, replayed: Decimal | int
) -> Mismatch:
    """One disagreement, named in codes an operator can act on."""
    return Mismatch(
        organization_code=balance.organization.code,
        branch_code=balance.warehouse.branch.code,
        warehouse_code=balance.warehouse.code,
        item_code=balance.item.code,
        lot_code=balance.lot.code if balance.lot else None,
        field=field,
        projected=projected,
        replayed=replayed,
    )


def verify_organization(organization: Organization) -> list[Mismatch]:
    """
    Compare every projected balance in one organization with the ledger.

    Reports in both directions: a projection that has drifted from the ledger,
    and a position the ledger knows about that the projection has never heard
    of. The second is the more alarming, and a comparison that only walked the
    balance rows would miss it entirely.
    """
    replayed = replay_movements(StockMovement.objects.filter(organization=organization))
    balances = list(
        StockBalance.objects.filter(organization=organization).select_related(
            "warehouse", "warehouse__branch", "item", "lot", "organization"
        )
    )

    mismatches: list[Mismatch] = []
    seen: set[tuple[int, int, int]] = set()

    for balance in balances:
        key = (balance.warehouse_id, balance.item_id, balance.lot_id or 0)
        seen.add(key)
        position = replayed.get(key)

        if position is None:
            if balance.quantity != ZERO or balance.value != ZERO:
                mismatches.append(_mismatch(balance, "exists_in_ledger", balance.quantity, ZERO))
            continue

        if balance.quantity != position.quantity:
            mismatches.append(_mismatch(balance, "quantity", balance.quantity, position.quantity))
        if balance.value != position.value:
            mismatches.append(_mismatch(balance, "value", balance.value, position.value))
        if int(balance.last_posted_sequence) != position.last_posted_sequence:
            mismatches.append(
                _mismatch(
                    balance,
                    "last_posted_sequence",
                    int(balance.last_posted_sequence),
                    position.last_posted_sequence,
                )
            )

    for key, position in replayed.items():
        if key in seen:
            continue
        # The ledger moved stock into a position with no balance row at all.
        # Reported with the ids it has: the row that would name them in code
        # form is exactly the row that is missing.
        mismatches.append(
            Mismatch(
                organization_code=organization.code,
                branch_code="?",
                warehouse_code=str(position.warehouse_id),
                item_code=str(position.item_id),
                lot_code=str(position.lot_id) if position.lot_id else None,
                field="missing_balance_row",
                projected=ZERO,
                replayed=position.quantity,
            )
        )

    return mismatches
