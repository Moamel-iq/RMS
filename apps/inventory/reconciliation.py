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

from apps.accounting.models import Account, JournalLine
from apps.core.money import quantize_money
from apps.inventory.models import (
    OpeningStockDocument,
    OpeningStockStatus,
    StockBalance,
    StockMovement,
)
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


# ---------------------------------------------------------------------------
# Task 1.3 — the accounting side of the mirror
# ---------------------------------------------------------------------------
#
# Three read-only comparisons, and the same rule for all of them: a mismatch
# is a defect to report, never a figure to repair. Nothing here writes a
# balance or a journal.
#
# Historical effects are attributed to the account **they actually posted
# to** — the immutable snapshot on the opening line — and never re-resolved
# through today's mapping. Re-resolving would make the report agree with the
# chart instead of with history, which is the exact failure reconciliation
# exists to catch.


@dataclass(frozen=True)
class Discrepancy:
    """One reconciliation failure, in operator-readable terms."""

    scope: str
    field: str
    expected: Decimal | int | str
    actual: Decimal | int | str

    def __str__(self) -> str:
        return f"{self.scope}: {self.field} expected={self.expected} actual={self.actual}"


def verify_opening_document(document: OpeningStockDocument) -> list[Discrepancy]:
    """
    One posted opening, across every representation of its value:

        sum of stored line values
        == sum of its OPENING movement values
        == sum of its inventory debit journal lines
        == its opening-equity credit
    """
    if document.status not in (OpeningStockStatus.POSTED, OpeningStockStatus.REVERSED):
        return []
    label = document.document_number or str(document.public_id)
    problems: list[Discrepancy] = []

    line_total = sum((line.total_value for line in document.lines.all()), ZERO)

    assert document.stock_entry is not None  # noqa: S101 - POSTED links one by constraint
    movement_total = sum(
        (movement.inventory_value for movement in document.stock_entry.movements.all()), ZERO
    )
    if movement_total != line_total:
        problems.append(
            Discrepancy(
                scope=label, field="movement_total", expected=line_total, actual=movement_total
            )
        )

    assert document.journal_entry is not None  # noqa: S101
    journal_lines = list(document.journal_entry.lines.all())
    debit_total = sum((journal_line.debit for journal_line in journal_lines), ZERO)
    credit_total = sum((journal_line.credit for journal_line in journal_lines), ZERO)
    if debit_total != line_total:
        problems.append(
            Discrepancy(
                scope=label, field="journal_debits", expected=line_total, actual=debit_total
            )
        )
    if credit_total != line_total:
        problems.append(
            Discrepancy(
                scope=label, field="opening_equity_credit", expected=line_total, actual=credit_total
            )
        )

    # Per-line: each stored value equals the movement it became.
    for line in document.lines.select_related("movement"):
        if line.movement is None or line.movement.inventory_value != line.total_value:
            problems.append(
                Discrepancy(
                    scope=f"{label}#{line.sequence}",
                    field="line_vs_movement",
                    expected=line.total_value,
                    actual=line.movement.inventory_value if line.movement else "missing",
                )
            )
    return problems


def _control_account_of(movement: StockMovement) -> int | None:
    """
    The inventory-control account a movement's value actually entered.

    An OPENING movement carries its opening line's snapshot. A REVERSAL
    carries its original's, because a mirror moves the same money the same
    way. Anything else — in Task 1.3 there is nothing else — is unattributed
    and reported as such.
    """
    line = getattr(movement, "opening_line", None)
    if line is not None:
        return int(line.inventory_account_id) if line.inventory_account_id else None
    if movement.reverses_id is not None:
        original = movement.reverses
        original_line = getattr(original, "opening_line", None)
        if original_line is not None and original_line.inventory_account_id:
            return int(original_line.inventory_account_id)
    return None


def verify_inventory_against_gl(organization: Organization) -> list[Discrepancy]:
    """
    Current inventory book value, grouped by the control account attached to
    the posted effects, against the GL balance of each such account per
    branch.

    The GL side sums **every** journal line on those accounts — including
    lines no inventory posting created. That is deliberate: a manual journal
    against an inventory-control account is precisely the kind of drift this
    report exists to surface.
    """
    problems: list[Discrepancy] = []

    # Inventory side: sum movement values per (branch, control account).
    inventory_side: dict[tuple[int, int], Decimal] = {}
    unattributed: list[StockMovement] = []
    movements = StockMovement.objects.filter(organization=organization).select_related(
        "reverses", "branch"
    )
    for movement in movements:
        account_id = _control_account_of(movement)
        if account_id is None:
            unattributed.append(movement)
            continue
        key = (movement.branch_id, account_id)
        inventory_side[key] = inventory_side.get(key, ZERO) + movement.inventory_value

    for movement in unattributed:
        problems.append(
            Discrepancy(
                scope=f"{organization.code}/movement#{movement.posted_sequence}",
                field="unattributed_movement_value",
                expected="a control account",
                actual=str(movement.inventory_value),
            )
        )

    # GL side: the balance of each involved account per branch.
    account_ids = {account_id for (_branch_id, account_id) in inventory_side}
    gl_side: dict[tuple[int, int], Decimal] = {}
    gl_lines = JournalLine.objects.filter(
        account__organization=organization, account_id__in=account_ids
    ).select_related("branch")
    for journal_line in gl_lines:
        key = (journal_line.branch_id, journal_line.account_id)
        gl_side[key] = gl_side.get(key, ZERO) + (journal_line.debit - journal_line.credit)

    branches = {branch.pk: branch.code for branch in organization.branches.all()}
    accounts = {account.pk: account.code for account in Account.objects.filter(pk__in=account_ids)}

    for key in sorted(set(inventory_side) | set(gl_side)):
        branch_id, account_id = key
        stock_value = quantize_money(inventory_side.get(key, ZERO))
        gl_value = quantize_money(gl_side.get(key, ZERO))
        if stock_value != gl_value:
            problems.append(
                Discrepancy(
                    scope=(
                        f"{organization.code}/{branches.get(branch_id, branch_id)}/"
                        f"{accounts.get(account_id, account_id)}"
                    ),
                    field="inventory_vs_gl",
                    expected=stock_value,
                    actual=gl_value,
                )
            )
    return problems


def verify_inventory_accounting(organization: Organization) -> list[str]:
    """
    The full Task 1.3 reconciliation for one organization, as report lines.

    Three comparisons: every posted opening against its own effects, the
    balance projection against the ledger replay, and the inventory book
    value against the general ledger. Read-only throughout.
    """
    lines: list[str] = []
    for document in OpeningStockDocument.objects.filter(
        organization=organization,
        status__in=[OpeningStockStatus.POSTED, OpeningStockStatus.REVERSED],
    ):
        lines.extend(str(problem) for problem in verify_opening_document(document))
    lines.extend(str(mismatch) for mismatch in verify_organization(organization))
    lines.extend(str(problem) for problem in verify_inventory_against_gl(organization))
    return lines
