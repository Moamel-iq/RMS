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

from django.db.models import QuerySet, Sum

from apps.accounting.models import Account, JournalLine
from apps.core.money import quantize_money
from apps.inventory.models import (
    ACTIVE_COUNT_STATUSES,
    OPEN_TRANSFER_STATUSES,
    AdjustmentLineKind,
    InventoryAdjustmentDocument,
    InventoryDocumentStatus,
    InventoryMovementDocument,
    OpeningStockDocument,
    OpeningStockStatus,
    StockBalance,
    StockCount,
    StockCountStatus,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    StockTransferReceipt,
    StockTransferShortage,
    StockTransferStatus,
    Warehouse,
    WarehouseType,
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
    #: The last movement's own figures. Quantity and value above are summed
    #: independently of them on purpose — if the two disagree the *ledger* is
    #: inconsistent, not the projection, and that is worth knowing separately.
    #: The average is not summable, so the kernel's last word on it is the
    #: only honest expectation.
    average_cost: Decimal = ZERO
    control_account_id: int | None = None
    last_movement_id: int | None = None


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
            average_cost=movement.average_after,
            # Mirrors `ledger.py`: a position emptied to zero attributes to no
            # control account, because it carries no value for one to hold.
            # While stock remains, a reversal carries no account of its own, so
            # the last one actually stated stands — overwriting that with NULL
            # would make every reversed position look unattributed.
            control_account_id=(
                None
                if quantity == ZERO
                else (
                    movement.control_account_id
                    if movement.control_account_id is not None
                    else (current.control_account_id if current else None)
                )
            ),
            last_movement_id=movement.pk,
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


def verify_organization(
    organization: Organization,
    *,
    branch_id: int | None = None,
    warehouse_id: int | None = None,
    item_id: int | None = None,
) -> list[Mismatch]:
    """
    Compare every projected balance in one organization with the ledger.

    Reports in both directions: a projection that has drifted from the ledger,
    and a position the ledger knows about that the projection has never heard
    of. The second is the more alarming, and a comparison that only walked the
    balance rows would miss it entirely.

    The optional filters narrow **both** sides identically. Narrowing one side
    only would manufacture mismatches out of rows the other side legitimately
    excluded — which is why they are applied here rather than by the caller.

    Verification only. Nothing in this function writes.
    """
    movements = StockMovement.objects.filter(organization=organization)
    balances_queryset = StockBalance.objects.filter(organization=organization)
    if branch_id is not None:
        movements = movements.filter(branch_id=branch_id)
        balances_queryset = balances_queryset.filter(branch_id=branch_id)
    if warehouse_id is not None:
        movements = movements.filter(warehouse_id=warehouse_id)
        balances_queryset = balances_queryset.filter(warehouse_id=warehouse_id)
    if item_id is not None:
        movements = movements.filter(item_id=item_id)
        balances_queryset = balances_queryset.filter(item_id=item_id)

    replayed = replay_movements(movements)
    balances = list(
        balances_queryset.select_related(
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
        # The average is not summable, so the expectation is the last
        # movement's own figure rather than a re-derivation. A projection
        # whose average drifted from what the last posting computed is the
        # exact failure a value ÷ quantity check would miss, because value and
        # quantity can both be right while the cached average is stale.
        if balance.average_cost != position.average_cost:
            mismatches.append(
                _mismatch(balance, "average_cost", balance.average_cost, position.average_cost)
            )
        if balance.control_account_id != position.control_account_id:
            mismatches.append(
                _mismatch(
                    balance,
                    "control_account",
                    balance.control_account_id or 0,
                    position.control_account_id or 0,
                )
            )
        if balance.last_movement_id != position.last_movement_id:
            mismatches.append(
                _mismatch(
                    balance,
                    "last_movement",
                    balance.last_movement_id or 0,
                    position.last_movement_id or 0,
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

    Read from the movement's own immutable column, which Task 1.4 added for
    exactly this: the account is a fact captured at posting, not something to
    be reconstructed by walking to whichever document produced it and hoping
    the walk covers every kind. The document-line fallbacks below remain for
    rows posted before that column existed.
    """
    if movement.control_account_id is not None:
        return int(movement.control_account_id)

    for attribute in ("opening_line", "document_line"):
        line = getattr(movement, attribute, None)
        if line is not None and line.inventory_account_id:
            return int(line.inventory_account_id)
    original = movement.reverses
    if original is not None:
        if original.control_account_id is not None:
            return int(original.control_account_id)
        for attribute in ("opening_line", "document_line"):
            line = getattr(original, attribute, None)
            if line is not None and line.inventory_account_id:
                return int(line.inventory_account_id)
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
        "reverses", "branch", "control_account"
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


def verify_operational_document(document: InventoryMovementDocument) -> list[Discrepancy]:
    """
    One posted receipt, issue, or return, across every representation of its
    value:

        sum of stored line values
        == sum of its movement values
        == the inventory side of its journal
        == the contra side of its journal

    Which side is the debit differs by type — inventory is debited by a
    receipt and a return and credited by an issue — but the equality does not,
    so one comparison covers all three.
    """
    if document.status not in (
        InventoryDocumentStatus.POSTED,
        InventoryDocumentStatus.REVERSED,
    ):
        return []
    label = document.document_number or str(document.public_id)
    problems: list[Discrepancy] = []

    lines = list(document.lines.select_related("movement"))
    line_total = sum((line.total_value or ZERO for line in lines), ZERO)

    assert document.stock_entry is not None  # noqa: S101 - POSTED links one by constraint
    movement_total = sum(
        (abs(movement.inventory_value) for movement in document.stock_entry.movements.all()),
        ZERO,
    )
    if movement_total != line_total:
        problems.append(
            Discrepancy(
                scope=label, field="movement_total", expected=line_total, actual=movement_total
            )
        )

    assert document.journal_entry is not None  # noqa: S101
    journal_lines = list(document.journal_entry.lines.all())
    debit_total = sum((row.debit for row in journal_lines), ZERO)
    credit_total = sum((row.credit for row in journal_lines), ZERO)
    if debit_total != line_total:
        problems.append(
            Discrepancy(
                scope=label, field="journal_debits", expected=line_total, actual=debit_total
            )
        )
    if credit_total != line_total:
        problems.append(
            Discrepancy(
                scope=label, field="journal_credits", expected=line_total, actual=credit_total
            )
        )

    for line in lines:
        if line.movement is None or abs(line.movement.inventory_value) != (
            line.total_value or ZERO
        ):
            problems.append(
                Discrepancy(
                    scope=f"{label}#{line.sequence}",
                    field="line_vs_movement",
                    expected=line.total_value or ZERO,
                    actual=(abs(line.movement.inventory_value) if line.movement else "missing"),
                )
            )
    return problems


# ---------------------------------------------------------------------------
# Task 1.5 — the transfer subledger
# ---------------------------------------------------------------------------
#
# A transfer keeps its own remaining balance per line, which makes the value
# allocation of ADR-020 §5 possible without a race. A retained figure is a
# figure that can drift, so it is checked here against the one thing that
# cannot: the posted child events themselves.
#
# Two independent comparisons, and they catch different failures.
#
#   Per line   dispatched == received + shortage + remaining. Catches a
#              retained balance that stopped agreeing with its own children.
#   In transit the ledger's in-transit position == the sum of what every open
#              transfer says is still owed to it. Catches a transfer whose
#              children are self-consistent but whose stock is not there.


def verify_transfer(transfer: StockTransfer) -> list[Discrepancy]:
    """
    One dispatched transfer, line by line:

        dispatched quantity == active received + active shortage + remaining
        dispatched value    == active received + active shortage + remaining

    "Active" excludes reversed receipts and reversed closures, because those
    put the quantity and the value back on the transfer — which is exactly
    what reversing them is for.
    """
    if transfer.status == StockTransferStatus.DRAFT:
        return []
    label = transfer.transfer_number or str(transfer.public_id)
    problems: list[Discrepancy] = []

    for line in transfer.lines.select_related("item"):
        received = line.receipt_lines.filter(
            receipt__status=InventoryDocumentStatus.POSTED
        ).aggregate(quantity=Sum("base_quantity"), value=Sum("allocated_value"))
        short = line.shortage_lines.filter(
            shortage__status=InventoryDocumentStatus.POSTED
        ).aggregate(quantity=Sum("base_quantity"), value=Sum("allocated_value"))

        # A reversed dispatch has taken everything back, so nothing is owed.
        dispatched_quantity = (
            ZERO if transfer.status == StockTransferStatus.REVERSED else line.base_quantity
        )
        dispatched_value = (
            ZERO if transfer.status == StockTransferStatus.REVERSED else (line.total_value or ZERO)
        )

        resolved_quantity = (
            (received["quantity"] or ZERO) + (short["quantity"] or ZERO) + line.remaining_quantity
        )
        resolved_value = (
            (received["value"] or ZERO) + (short["value"] or ZERO) + line.remaining_value
        )
        scope = f"{label}#{line.sequence}"
        if resolved_quantity != dispatched_quantity:
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="dispatched_quantity",
                    expected=dispatched_quantity,
                    actual=resolved_quantity,
                )
            )
        if quantize_money(resolved_value) != quantize_money(dispatched_value):
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="dispatched_value",
                    expected=quantize_money(dispatched_value),
                    actual=quantize_money(resolved_value),
                )
            )
    return problems


def verify_in_transit(organization: Organization) -> list[Discrepancy]:
    """
    Every in-transit stock position against the transfers that still owe it.

    Both directions: a position the ledger holds that no open transfer claims
    is stranded value, and a transfer claiming stock the ledger does not hold
    is a claim on nothing. Either is a defect, and the second is the worse one.
    """
    problems: list[Discrepancy] = []

    claimed: dict[tuple[int, int, int], tuple[Decimal, Decimal]] = {}
    open_lines = StockTransferLine.objects.filter(
        transfer__organization=organization,
        transfer__status__in=list(OPEN_TRANSFER_STATUSES),
        remaining_quantity__gt=ZERO,
    ).select_related("transfer", "transfer__source_warehouse", "item", "lot")
    transit_by_branch: dict[int, int] = {}
    for line in open_lines:
        branch_id = line.transfer.source_warehouse.branch_id
        if branch_id not in transit_by_branch:
            warehouse = Warehouse.objects.filter(
                branch_id=branch_id, warehouse_type=WarehouseType.IN_TRANSIT
            ).first()
            if warehouse is None:  # pragma: no cover - a dispatch always creates one
                problems.append(
                    Discrepancy(
                        scope=f"{organization.code}/branch#{branch_id}",
                        field="missing_in_transit_warehouse",
                        expected="one in-transit warehouse",
                        actual="none",
                    )
                )
                continue
            transit_by_branch[branch_id] = warehouse.pk
        key = (transit_by_branch[branch_id], line.item_id, line.lot_id or 0)
        quantity, value = claimed.get(key, (ZERO, ZERO))
        claimed[key] = (
            quantity + line.remaining_quantity,
            value + line.remaining_value,
        )

    held: dict[tuple[int, int, int], tuple[Decimal, Decimal]] = {
        (balance.warehouse_id, balance.item_id, balance.lot_id or 0): (
            balance.quantity,
            balance.value,
        )
        for balance in StockBalance.objects.filter(
            organization=organization,
            warehouse__warehouse_type=WarehouseType.IN_TRANSIT,
        ).exclude(quantity=ZERO, value=ZERO)
    }

    for key in sorted(set(claimed) | set(held)):
        want_quantity, want_value = claimed.get(key, (ZERO, ZERO))
        have_quantity, have_value = held.get(key, (ZERO, ZERO))
        scope = f"{organization.code}/in-transit/{key[0]}/{key[1]}/{key[2] or '-'}"
        if want_quantity != have_quantity:
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="in_transit_quantity",
                    expected=want_quantity,
                    actual=have_quantity,
                )
            )
        if quantize_money(want_value) != quantize_money(have_value):
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="in_transit_value",
                    expected=quantize_money(want_value),
                    actual=quantize_money(have_value),
                )
            )
    return problems


def verify_transfer_receipt(receipt: StockTransferReceipt) -> list[Discrepancy]:
    """
    One posted receipt, across every representation of its value:

        sum of allocated line values
        == the in-transit release movements
        == the destination arrival movements
        == the destination inventory debit

    ...and for a cross-branch receipt, that the two branch-local journals meet
    at the same clearing figure. That equality is what makes the organization's
    inter-branch clearing account net to zero for the complete event; the two
    journals are written in one transaction precisely so it cannot half-happen.
    """
    if receipt.status != InventoryDocumentStatus.POSTED:
        return []
    label = receipt.receipt_number or str(receipt.public_id)
    problems: list[Discrepancy] = []

    lines = list(receipt.lines.select_related("transit_movement", "destination_movement"))
    total = sum((line.allocated_value or ZERO for line in lines), ZERO)

    for line in lines:
        for side, movement in (
            ("release", line.transit_movement),
            ("arrival", line.destination_movement),
        ):
            actual = abs(movement.inventory_value) if movement is not None else "missing"
            if actual != (line.allocated_value or ZERO):
                problems.append(
                    Discrepancy(
                        scope=f"{label}#{line.sequence}",
                        field=f"line_vs_{side}_movement",
                        expected=line.allocated_value or ZERO,
                        actual=actual,
                    )
                )

    destination = receipt.destination_journal_entry
    if destination is not None:
        debit = sum((row.debit for row in destination.lines.all()), ZERO)
        if debit != total:
            problems.append(
                Discrepancy(
                    scope=label, field="destination_inventory_debit", expected=total, actual=debit
                )
            )

    source = receipt.source_journal_entry
    if source is not None and destination is not None and source.pk != destination.pk:
        source_credit = sum((row.credit for row in source.lines.all()), ZERO)
        source_debit = sum((row.debit for row in source.lines.all()), ZERO)
        if source_debit != total or source_credit != total:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="source_clearing_value",
                    expected=total,
                    actual=source_debit,
                )
            )
    return problems


def verify_transfer_shortage(shortage: StockTransferShortage) -> list[Discrepancy]:
    """One posted closure: movement value == shortage expense debit."""
    if shortage.status != InventoryDocumentStatus.POSTED:
        return []
    label = shortage.shortage_number or str(shortage.public_id)
    problems: list[Discrepancy] = []

    lines = list(shortage.lines.select_related("transit_movement"))
    total = sum((line.allocated_value or ZERO for line in lines), ZERO)
    for line in lines:
        movement = line.transit_movement
        actual = abs(movement.inventory_value) if movement is not None else "missing"
        if actual != (line.allocated_value or ZERO):
            problems.append(
                Discrepancy(
                    scope=f"{label}#{line.sequence}",
                    field="line_vs_movement",
                    expected=line.allocated_value or ZERO,
                    actual=actual,
                )
            )
    journal = shortage.journal_entry
    if journal is not None:
        debit = sum((row.debit for row in journal.lines.all()), ZERO)
        if debit != total:
            problems.append(
                Discrepancy(
                    scope=label, field="shortage_expense_debit", expected=total, actual=debit
                )
            )
    return problems


# ---------------------------------------------------------------------------
# Task 1.6 — counts, adjustments, and the freeze
# ---------------------------------------------------------------------------
#
# Waste needs no comparison of its own: it posts through
# `InventoryMovementDocument`, so `verify_operational_document` already covers
# it line for line. That is one of the arguments for having put it there.
#
# Three new comparisons, each catching something the others cannot.
#
#   Count      book + posted variance == counted, per line; and the document's
#              own totals against its movements and its journal. Catches a
#              variance that stopped agreeing with the snapshot it came from.
#   Adjustment the signed line values against the signed movement values and
#              against the journal. Catches a value-only revaluation that
#              posted a different amount from the one authorized.
#   Freeze     every frozen warehouse has exactly one active owning count, and
#              every active count owns its warehouse. Catches a warehouse shut
#              by nothing, which is invisible until somebody tries to post.


def verify_stock_count(count: StockCount) -> list[Discrepancy]:
    """
    One posted count, from three directions:

        book quantity + posted variance == counted quantity   (per line)
        sum of line variance values     == sum of movement values
        sum of movement values          == the journal's inventory side

    The per-line identity is the one that matters most. It is the whole claim a
    count makes — "the books said this, we found that, and here is the
    difference" — and if it fails, the variance posted to the ledger is not the
    variance anybody counted.
    """
    if count.status not in (StockCountStatus.POSTED, StockCountStatus.REVERSED):
        return []
    label = count.count_number or str(count.public_id)
    problems: list[Discrepancy] = []

    lines = list(count.lines.select_related("movement", "item"))
    for line in lines:
        counted = line.counted_quantity
        if counted is None:  # pragma: no cover - submission demands one
            continue
        posted = line.movement.base_quantity if line.movement is not None else ZERO
        if line.book_quantity + posted != counted:
            problems.append(
                Discrepancy(
                    scope=f"{label}#{line.sequence}",
                    field="book_plus_variance",
                    expected=counted,
                    actual=line.book_quantity + posted,
                )
            )
        # The retained variance figure against the movement that carried it.
        stored_value = line.variance_value or ZERO
        movement_value = line.movement.inventory_value if line.movement is not None else ZERO
        if stored_value != movement_value:
            problems.append(
                Discrepancy(
                    scope=f"{label}#{line.sequence}",
                    field="variance_value",
                    expected=stored_value,
                    actual=movement_value,
                )
            )

    if count.stock_entry is None:
        # A count that found nothing posts nothing. Then every line must agree
        # with its own book, or something moved that no movement records.
        for line in lines:
            if line.variance_quantity not in (None, ZERO):
                problems.append(
                    Discrepancy(
                        scope=f"{label}#{line.sequence}",
                        field="unposted_variance",
                        expected=ZERO,
                        actual=line.variance_quantity,
                    )
                )
        return problems

    movements = list(count.stock_entry.movements.all())
    line_total = sum((line.variance_value or ZERO for line in lines), ZERO)
    movement_total = sum((movement.inventory_value for movement in movements), ZERO)
    if line_total != movement_total:
        problems.append(
            Discrepancy(
                scope=label, field="movement_total", expected=line_total, actual=movement_total
            )
        )

    if count.journal_entry is not None:
        journal_lines = list(count.journal_entry.lines.all())
        debits = sum((row.debit for row in journal_lines), ZERO)
        credits = sum((row.credit for row in journal_lines), ZERO)
        if debits != credits:  # pragma: no cover - the journal kernel refuses it
            problems.append(
                Discrepancy(scope=label, field="journal_balance", expected=debits, actual=credits)
            )
        # Gains debit inventory and losses credit it, so the journal's gross
        # movement is the sum of the absolute variances, never their net.
        gross = sum((abs(movement.inventory_value) for movement in movements), ZERO)
        if debits != gross:
            problems.append(
                Discrepancy(scope=label, field="journal_gross", expected=gross, actual=debits)
            )
    elif movement_total != ZERO or any(movement.inventory_value != ZERO for movement in movements):
        problems.append(
            Discrepancy(
                scope=label,
                field="journal_missing",
                expected="a journal for a count that moved value",
                actual="none",
            )
        )
    return problems


def verify_adjustment(document: InventoryAdjustmentDocument) -> list[Discrepancy]:
    """
    One posted manual adjustment:

        stored signed line value == its movement's signed value
        sum of positive values   == the journal's inventory debits
        sum of negative values   == the journal's inventory credits

    Signed throughout, because one adjustment may legitimately write one item
    up and another down, and an unsigned comparison would call that pair
    correct whichever way round the two amounts had gone.
    """
    if document.status not in (
        InventoryDocumentStatus.POSTED,
        InventoryDocumentStatus.REVERSED,
    ):
        return []
    label = document.document_number or str(document.public_id)
    problems: list[Discrepancy] = []

    lines = list(document.lines.select_related("movement"))
    for line in lines:
        stored = line.total_value or ZERO
        actual = line.movement.inventory_value if line.movement is not None else ZERO
        if stored != actual:
            problems.append(
                Discrepancy(
                    scope=f"{label}#{line.sequence}",
                    field="line_vs_movement",
                    expected=stored,
                    actual=actual,
                )
            )
        if line.kind == AdjustmentLineKind.VALUE_ONLY:
            if line.movement is not None and line.movement.base_quantity != ZERO:
                problems.append(
                    Discrepancy(
                        scope=f"{label}#{line.sequence}",
                        field="value_only_moved_quantity",
                        expected=ZERO,
                        actual=line.movement.base_quantity,
                    )
                )
            if stored != (line.value_adjustment or ZERO):
                problems.append(
                    Discrepancy(
                        scope=f"{label}#{line.sequence}",
                        field="authorized_revaluation",
                        expected=line.value_adjustment or ZERO,
                        actual=stored,
                    )
                )

    if document.journal_entry is None:  # pragma: no cover - POSTED always links one
        return problems
    increases = sum(
        (line.total_value or ZERO for line in lines if (line.total_value or ZERO) > ZERO), ZERO
    )
    decreases = sum(
        (abs(line.total_value or ZERO) for line in lines if (line.total_value or ZERO) < ZERO), ZERO
    )
    journal_lines = list(document.journal_entry.lines.all())
    debits = sum((row.debit for row in journal_lines), ZERO)
    if debits != increases + decreases:
        problems.append(
            Discrepancy(
                scope=label,
                field="journal_gross",
                expected=increases + decreases,
                actual=debits,
            )
        )
    return problems


def verify_warehouse_freezes(organization: Organization) -> list[Discrepancy]:
    """
    The freeze and its owner, checked from both ends.

    A warehouse frozen by a finished count is shut with nothing left that can
    open it; an active count whose warehouse is open has a snapshot that
    anything may have moved under. Triggers refuse both, so a failure here
    means the triggers were bypassed — which is exactly the thing worth
    reporting rather than repairing.
    """
    problems: list[Discrepancy] = []
    for warehouse in Warehouse.objects.filter(
        branch__organization=organization, frozen_by_count__isnull=False
    ).select_related("frozen_by_count"):
        owner = warehouse.frozen_by_count
        assert owner is not None  # noqa: S101 - filtered above
        if owner.status not in ACTIVE_COUNT_STATUSES:
            problems.append(
                Discrepancy(
                    scope=warehouse.code,
                    field="freeze_owner_status",
                    expected="IN_PROGRESS or SUBMITTED",
                    actual=owner.status,
                )
            )
        if owner.warehouse_id != warehouse.pk:
            problems.append(
                Discrepancy(
                    scope=warehouse.code,
                    field="freeze_owner_warehouse",
                    expected=warehouse.pk,
                    actual=owner.warehouse_id,
                )
            )

    for count in StockCount.objects.filter(
        organization=organization, status__in=sorted(ACTIVE_COUNT_STATUSES)
    ).select_related("warehouse"):
        if count.warehouse.frozen_by_count_id != count.pk:
            problems.append(
                Discrepancy(
                    scope=count.count_number or str(count.public_id),
                    field="count_owns_no_freeze",
                    expected=count.pk,
                    actual=count.warehouse.frozen_by_count_id or "none",
                )
            )
    return problems


def verify_inventory_accounting(organization: Organization) -> list[str]:
    """
    The full reconciliation for one organization, as report lines.

    Eleven comparisons: every posted opening against its own effects, every
    posted operational document against its own effects, every dispatched
    transfer against its posted children, every posted receipt and closure
    against their own effects, the in-transit ledger against the transfers
    that claim it, the balance projection against the ledger replay, and the
    inventory book value against the general ledger, every posted count
    against its own snapshot, every posted adjustment against its own
    movements, and every warehouse freeze against the count that owns it.

    Read-only throughout, and **no repair mode anywhere**. A projection that
    can be quietly corrected proves nothing, because the correction erases the
    evidence of whatever caused the divergence.
    """
    lines: list[str] = []
    for document in OpeningStockDocument.objects.filter(
        organization=organization,
        status__in=[OpeningStockStatus.POSTED, OpeningStockStatus.REVERSED],
    ):
        lines.extend(str(problem) for problem in verify_opening_document(document))
    for operational in InventoryMovementDocument.objects.filter(
        organization=organization,
        status__in=[InventoryDocumentStatus.POSTED, InventoryDocumentStatus.REVERSED],
    ):
        lines.extend(str(problem) for problem in verify_operational_document(operational))
    for transfer in StockTransfer.objects.filter(organization=organization).exclude(
        status=StockTransferStatus.DRAFT
    ):
        lines.extend(str(problem) for problem in verify_transfer(transfer))
    for receipt in StockTransferReceipt.objects.filter(
        transfer__organization=organization, status=InventoryDocumentStatus.POSTED
    ):
        lines.extend(str(problem) for problem in verify_transfer_receipt(receipt))
    for shortage in StockTransferShortage.objects.filter(
        transfer__organization=organization, status=InventoryDocumentStatus.POSTED
    ):
        lines.extend(str(problem) for problem in verify_transfer_shortage(shortage))
    lines.extend(str(problem) for problem in verify_in_transit(organization))
    for count in StockCount.objects.filter(
        organization=organization,
        status__in=[StockCountStatus.POSTED, StockCountStatus.REVERSED],
    ):
        lines.extend(str(problem) for problem in verify_stock_count(count))
    for adjustment in InventoryAdjustmentDocument.objects.filter(
        organization=organization,
        status__in=[InventoryDocumentStatus.POSTED, InventoryDocumentStatus.REVERSED],
    ):
        lines.extend(str(problem) for problem in verify_adjustment(adjustment))
    lines.extend(str(problem) for problem in verify_warehouse_freezes(organization))
    lines.extend(str(mismatch) for mismatch in verify_organization(organization))
    lines.extend(str(problem) for problem in verify_inventory_against_gl(organization))
    return lines
