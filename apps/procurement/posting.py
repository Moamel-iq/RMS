"""
The authoritative goods-receipt POST and REVERSE.

Task 2.8 built the receipt and refused to post it. This module crosses that
boundary, and the shape of the crossing is the whole design:

    Dr  inventory control     accepted_base_quantity x posted_unit_cost
        Cr  GRNI                                       the same figure

**There is one command, not two.** No `post_receipt_stock` sits beside a
`post_receipt_accounting`, because a system that can do the first without the
second will eventually do exactly that, on the night somebody's mapping is
missing. `post_goods_receipt` writes the receipt status, the stock movements,
the balances, the valuation layers, the location placement, the gapless
document number, the balanced journal, every line-level link between them and
the audit event — inside one `transaction.atomic()`. A failure anywhere leaves
the receipt DRAFT and both ledgers untouched.

**It adds no second posting path.** Stock moves through `post_stock_entry` and
value through `post_entry`, the same two functions inventory's own documents
use, with the same advisory locks, the same moving average, the same period
validation and the same immutability triggers (PRC-029). What is new here is a
*document* that calls them, which is what `opening.py`, `operations.py`,
`counts.py`, `adjustments.py` and `transfers.py` each are. Procurement adding
a receipt aggregate is not procurement adding a kernel.

**Only accepted goods move** (PRC-025). Rejected quantity produces no
movement, no balance change, no location quantity, no GRNI and no payable. It
stays on the document as the evidence the supplier will be argued with, and a
line that is wholly rejected posts nothing at all — no zero-quantity movement
and no zero-value journal line, because a zero row is a claim that something
happened.

## Lock order

Preserved from ADR-019 and inventory's documented global order, which this
extends rather than reinterprets:

    1. the goods receipt row                  select_for_update
    2. the organization's mapping lock        shared, advisory
    3. the purchase order, its version rows
       and the receipt lines                  select_for_update, by pk
    4. warehouse freeze locks                 inside the kernel
    5. the stock keys, canonically sorted     advisory, inside the kernel
    6. the inventory posted-sequence          inside the kernel
    7. the procurement document-number counter
    8. the journal-number counter             inside post_entry

Step 2 is above step 3 deliberately. Taking a row lock on the order before the
mapping lock is what produced the deadlock ADR-019 §5 records, and the order
here is the one that avoids it: nothing takes a *row* lock between the mapping
lock and the kernel.

## What the reversal does and does not re-decide

A reversal mirrors what posted. It takes back each movement's own quantity and
value, credits the accounts the original debited, and picks the accepted
quantity back out of the location it was put into. It never re-resolves a
mapping and never re-reads a price: the question "what did this receipt do" has
one answer, and it was settled the day it posted. Only its *date* is current,
because undoing something is an event that happens now.

Availability still applies. If the goods have been issued, transferred or
counted away, they are not there to un-receive and the kernel refuses it —
which is the correct answer, and the reason a reversal is not a substitute for
a supplier return (Task 2.15).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
    Account,
    JournalEntry,
    JournalLine,
    OrganizationAccountMapping,
    SourceEvent,
)
from apps.accounting.services import post_entry, resolve_period, reverse_entry
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.locks import lock_account_mappings_shared
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.inventory.accounts import resolve_inventory_account
from apps.inventory.ledger import (
    MovementInput,
    link_journal_entry,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.locations import pick, put_away
from apps.inventory.models import (
    InventoryAccountMapping,
    MovementType,
    StockLocationBalance,
    StockMovement,
)
from apps.inventory.operations import require_cost_center_where_the_account_demands_one
from apps.organizations.business_dates import resolve_business_day
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseOrderVersion,
)
from apps.procurement.services import next_document_number
from apps.users.models import User

ZERO = Decimal("0.000")

#: The canonical source type both ledgers record. Procurement's own vocabulary,
#: not an inventory document type: `INVENTORY_RECEIPT` already means the
#: operational receipt document, and one string meaning two things is how a
#: source drill-down starts leading to the wrong screen (ADR-017).
SOURCE_DOCUMENT_TYPE = "PROCUREMENT_GOODS_RECEIPT"

#: Stamped on the journal so an entry always says which rule produced it. Bump
#: when the accounting treatment changes, never when the code moves.
POSTING_RULE = "procurement-goods-receipt-v1"

DOCUMENT_TYPE = "GOODS_RECEIPT"


@dataclass
class _Plan:
    """The accounts and effects one posting resolved, before any of it exists."""

    effects: list[MovementInput]
    #: line pk -> (inventory control account, GRNI account)
    accounts: dict[int, tuple[Account, Account]]
    #: line pk -> the mapping rows that produced the control account
    mappings: dict[int, tuple[InventoryAccountMapping | None, OrganizationAccountMapping | None]]
    #: line pk -> (base unit cost, posted value). Both stored on the line.
    values: dict[int, tuple[Decimal, Decimal]]


def effect_key(line: GoodsReceiptLine) -> str:
    """The immutable per-line effect identity, derived from the line UID."""
    return f"goods-receipt-line:{line.line_uid}"


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def _require_inspection_is_complete(lines: list[GoodsReceiptLine]) -> None:
    """
    Every line fully disposed of: `accepted + rejected == delivered`.

    An undisposed remainder is refused rather than treated as rejected or as
    accepted. Three of twelve cartons nobody has looked at are goods that are
    neither in stock nor claimed against the supplier, and picking either
    answer for the operator would silently decide a question they have not
    answered — one way losing stock, the other way inventing it.
    """
    for line in lines:
        disposed = line.accepted_base_quantity + line.rejected_base_quantity
        if disposed != line.delivered_base_quantity:
            raise ValidationError(
                _(
                    "Line %(sequence)s delivered %(delivered)s and has disposed of "
                    "%(disposed)s. Inspect the remainder before posting."
                ),
                code="inspection_incomplete",
                params={
                    "sequence": line.sequence,
                    "delivered": format(line.delivered_base_quantity, "f"),
                    "disposed": format(disposed, "f"),
                },
            )


def _price_basis_for(receipt: GoodsReceipt, line: GoodsReceiptLine) -> Decimal:
    """
    The unit price this line is entitled to post at, checked against its basis.

    A linked line took its price from the order **version that was live when
    the goods arrived**, and `receipt.order_version` records which. A revision
    since then produced a `PurchaseOrderVersion` snapshot; the live order line
    now says something else, and reading it would value this delivery at a
    price agreed after it happened (PRC-019, F4).

    So the stored price is the answer, and this only refuses to let it be a
    price the named version never carried. An unlinked receipt has no basis to
    check against — its price was entered and authorized at entry (PRC-028) —
    and a catalogue price is never consulted by either path (PRC-005).
    """
    if line.order_line_id is None or receipt.order_id is None or receipt.order_version is None:
        return line.unit_price

    snapshot_row = PurchaseOrderVersion.objects.filter(
        order_id=receipt.order_id, version=receipt.order_version
    ).first()
    agreed: Decimal | None
    if snapshot_row is None:
        # No revision has happened since: the live line is still that version.
        agreed = line.order_line.unit_price if line.order_line else line.unit_price
    else:
        agreed = _price_in_snapshot(snapshot_row, line)

    if agreed is not None and agreed != line.unit_price:
        raise ValidationError(
            _(
                "Line %(sequence)s holds %(held)s but version %(version)s of the order "
                "says %(agreed)s. Re-enter the line against the current order."
            ),
            code="price_basis_mismatch",
            params={
                "sequence": line.sequence,
                "held": format(line.unit_price, "f"),
                "version": receipt.order_version,
                "agreed": format(agreed, "f"),
            },
        )
    return line.unit_price


def _price_in_snapshot(version: PurchaseOrderVersion, line: GoodsReceiptLine) -> Decimal | None:
    """The frozen price for this line's order line, or None if it was not in that version."""
    if line.order_line is None:  # pragma: no cover - guarded by the caller
        return None
    wanted = str(line.order_line.line_uid)
    for row in version.lines:
        if isinstance(row, dict) and row.get("line_uid") == wanted:
            price = row.get("unit_price")
            return Decimal(str(price)) if price is not None else None
    return None


def _require_order_still_accepts_goods(receipt: GoodsReceipt) -> None:
    """
    A cancelled order receives nothing, checked against the locked row.

    `create_goods_receipt` already refused a cancelled order at draft time.
    This is the same question asked again at the moment it matters, because
    the order can be cancelled while the draft sits on somebody's screen and
    posting is the act that makes the answer permanent.
    """
    if receipt.order is None:
        return
    if receipt.order.status == PurchaseOrderStatus.CANCELLED:
        raise ValidationError(
            _("Order %(number)s was cancelled and cannot receive goods."),
            code="order_cancelled",
            params={"number": receipt.order.number},
        )
    if receipt.order.status == PurchaseOrderStatus.DRAFT:  # pragma: no cover - refused at draft
        raise ValidationError(
            _("Goods cannot be received against a draft order."), code="order_not_approved"
        )


def _require_within_the_ordered_quantity(lines: list[GoodsReceiptLine]) -> None:
    """
    No line may push its order line past what was ordered (PRC-023, PRC-030).

    Counted against **posted** receipts only, and this draft's own accepted
    quantity — draft reservations are `add_receipt_line`'s concern and are
    deliberately not counted here, because two drafts racing to post must each
    be measured against what has actually happened, not against each other's
    intentions. The order lines were locked before this ran, so the count
    cannot move underneath it.
    """
    for line in lines:
        if line.order_line is None:
            continue
        posted: Decimal | None = GoodsReceiptLine.objects.filter(
            order_line=line.order_line, receipt__status=GoodsReceiptStatus.POSTED
        ).aggregate(total=Sum("accepted_base_quantity"))["total"]
        already = posted or ZERO
        ordered = line.order_line.ordered_base_quantity
        if already + line.accepted_base_quantity > ordered:
            raise ValidationError(
                _(
                    "Line %(sequence)s would take %(item)s to %(total)s against "
                    "%(ordered)s ordered. Revise the order first."
                ),
                code="over_receipt",
                params={
                    "sequence": line.sequence,
                    "item": line.item.code,
                    "total": format(already + line.accepted_base_quantity, "f"),
                    "ordered": format(ordered, "f"),
                },
            )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _plan(receipt: GoodsReceipt, lines: list[GoodsReceiptLine]) -> _Plan:
    """
    Resolve every account and value before a single effect exists.

    One missing mapping fails here — before a movement, a balance, a number or
    a journal line has been written — so there is nothing partial to clean up
    (PRC-034). The GRNI role is organization-scoped and resolved once: which
    item arrived says nothing about who is owed for it.
    """
    grni = resolve_inventory_account(
        organization=receipt.organization,
        role=GOODS_RECEIVED_NOT_INVOICED,
        item=None,
        on_date=receipt.received_at,
    )
    require_cost_center_where_the_account_demands_one(account=grni.account, cost_center=None)

    effects: list[MovementInput] = []
    accounts: dict[int, tuple[Account, Account]] = {}
    mappings: dict[
        int, tuple[InventoryAccountMapping | None, OrganizationAccountMapping | None]
    ] = {}
    values: dict[int, tuple[Decimal, Decimal]] = {}

    for line in lines:
        # Raises unless the stored price is still the one its order version
        # agreed to. The stored price is always the figure that posts; this
        # only refuses to let it be a price nobody agreed for this delivery.
        _price_basis_for(receipt, line)
        base_cost = line.base_unit_cost
        posted_value = quantize_money(line.accepted_base_quantity * base_cost)
        values[line.pk] = (base_cost, posted_value)

        if line.accepted_base_quantity <= ZERO:
            # Wholly rejected. No movement, no accounts, no journal line — the
            # goods never entered inventory and nothing is owed for them.
            continue

        control = resolve_inventory_account(
            organization=receipt.organization,
            role=INVENTORY_CONTROL,
            item=line.item,
            on_date=receipt.received_at,
        )
        require_cost_center_where_the_account_demands_one(account=control.account, cost_center=None)
        accounts[line.pk] = (control.account, grni.account)
        mappings[line.pk] = (control.inventory_mapping, control.organization_mapping)
        effects.append(
            MovementInput(
                warehouse=receipt.warehouse,
                item=line.item,
                movement_type=MovementType.RECEIPT,
                quantity=line.accepted_base_quantity,
                effect_key=effect_key(line),
                lot=line.lot,
                unit_cost=base_cost,
                source_conversion=line.conversion,
                control_account=control.account,
            )
        )

    if not effects:
        raise ValidationError(
            _(
                "Every line on this receipt was rejected. Nothing entered stock and "
                "nothing is owed, so there is nothing to post — record the rejection "
                "and return the goods."
            ),
            code="nothing_accepted",
        )
    return _Plan(effects=effects, accounts=accounts, mappings=mappings, values=values)


def _journal_lines(
    receipt: GoodsReceipt, lines: list[GoodsReceiptLine], *, plan: _Plan
) -> list[PostingLine]:
    """
    One debit per distinct control account, one credit to GRNI (PRC-033).

    Five items mapping to three control accounts produce three debits and a
    single credit, because `INVENTORY_CONTROL` is item-overridable and GRNI is
    not. Every figure is a sum of stored 3-decimal line values and the total is
    never rounded on its own (ADR-012).
    """
    debit_by_account: dict[int, Decimal] = {}
    account_by_id: dict[int, Account] = {}
    grni: Account | None = None

    for line in lines:
        if line.pk not in plan.accounts:
            continue
        control, grni_account = plan.accounts[line.pk]
        grni = grni_account
        _cost, value = plan.values[line.pk]
        account_by_id[control.pk] = control
        debit_by_account[control.pk] = debit_by_account.get(control.pk, ZERO) + value

    total = sum(debit_by_account.values(), ZERO)
    posting_lines = [
        PostingLine(account=account_by_id[account_id], branch=receipt.branch, debit=amount)
        for account_id, amount in sorted(
            debit_by_account.items(), key=lambda pair: account_by_id[pair[0]].code
        )
    ]
    assert grni is not None  # noqa: S101 - _plan raises when no line is accepted
    posting_lines.append(PostingLine(account=grni, branch=receipt.branch, credit=total))
    return posting_lines


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@transaction.atomic
def post_goods_receipt(*, receipt: GoodsReceipt, actor: User) -> GoodsReceipt:
    """
    Post an inspected draft to stock and the accounts, atomically.

    The idempotency key is derived from the receipt's own `public_id` rather
    than accepted from the caller. Posting *this receipt* is the command, the
    receipt is the payload, and a posted receipt is frozen by a database
    trigger — so a retry cannot present the same key with a different payload,
    and there is no second key vocabulary for anybody to get wrong. The kernel
    still refuses a duplicate on the source identity independently, which is
    the guarantee that survives somebody composing a key by hand later.
    """
    # 1. The receipt row. `lock_and_require_status` inlined, because posting
    # needs to tell "already posted" from "reversed" and the shared helper
    # deliberately raises one code for both.
    locked = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if locked.status == GoodsReceiptStatus.POSTED:
        raise ValidationError(_("This receipt is already posted."), code="already_posted")
    if locked.status != GoodsReceiptStatus.DRAFT:
        raise ValidationError(
            _("A reversed receipt cannot be posted again. Record a new receipt."),
            code="receipt_not_draft",
        )
    if not locked.evidence_reference:
        raise ValidationError(
            _("Posting a receipt needs the delivery evidence it rests on."),
            code="evidence_required",
        )

    lines = list(
        locked.lines.select_related(
            "item",
            "item__category",
            "item__category__parent",
            "item__category__parent__parent",
            "item__base_unit",
            "lot",
            "conversion",
            "order_line",
        ).order_by("sequence")
    )
    if not lines:
        raise ValidationError(_("An empty receipt cannot be posted."), code="no_lines")
    _require_inspection_is_complete(lines)

    # The business date is the day the goods arrived, entered rather than
    # derived — but which day that is depends on the branch's timezone and
    # cutoff, so both are snapshotted here and frozen with the posting. A
    # cutoff changed tomorrow cannot restate which period this belongs to.
    day = resolve_business_day(locked.branch, timezone.now())
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = resolve_period(organization=locked.organization, accounting_date=locked.received_at)
    validate_period_accepts_postings(period)

    # 2. The organization's mappings, shared — above every row lock below it in
    # the global order, so a mapping mutation cannot interleave with the
    # resolution in `_plan` (ADR-019 §5).
    lock_account_mappings_shared(locked.organization_id)

    # 3. The order, its lines and this receipt's lines. Locked before the
    # over-receipt count is read, so two receipts racing for one remainder
    # contend here rather than both seeing the same quantity as available.
    if locked.order_id is not None:
        locked.order = PurchaseOrder.objects.select_for_update().get(pk=locked.order_id)
        _require_order_still_accepts_goods(locked)
        order_line_ids = sorted(
            line.order_line_id for line in lines if line.order_line_id is not None
        )
        if order_line_ids:
            list(
                PurchaseOrderLine.objects.select_for_update()
                .filter(pk__in=order_line_ids)
                .order_by("pk")
            )
    _require_within_the_ordered_quantity(lines)

    plan = _plan(locked, lines)

    # 4/5/6. Stock: movements, balances, valuation layers, the posted-order
    # counter, and the location release discipline — the kernel's own.
    stock_entry = post_stock_entry(
        organization=locked.organization,
        effects=plan.effects,
        idempotency_key=f"procurement-goods-receipt:{locked.public_id}",
        business_date=locked.received_at,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        reference=locked.evidence_reference,
        reason=locked.notes or "goods receipt",
    )
    movement_by_key = {movement.effect_key: movement for movement in stock_entry.movements.all()}

    # The bin, if one was named. Warehouse quantity went up by the accepted
    # amount and landed in the unlocated pool; putting it away moves it into
    # the named location and keeps `located + unlocated == warehouse` true by
    # construction rather than by a later repair (ADR-018 §2).
    if locked.location is not None:
        for line in lines:
            if line.accepted_base_quantity <= ZERO:
                continue
            put_away(
                location=locked.location,
                item=line.item,
                lot=line.lot,
                quantity=line.accepted_base_quantity,
                reference=f"GRN {locked.public_id}",
                stock_movement=movement_by_key[effect_key(line)],
            )

    # 7. The gapless human number, drawn only now that nothing can fail for a
    # domain reason — an abandoned attempt must not burn one.
    locked.number = next_document_number(
        organization=locked.organization,
        document_type=DOCUMENT_TYPE,
        year=period.fiscal_year.year,
    )

    # 8. The journal.
    journal = post_entry(
        organization=locked.organization,
        accounting_date=locked.received_at,
        lines=_journal_lines(locked, lines, plan=plan),
        idempotency_key=f"procurement-goods-receipt-journal:{locked.public_id}",
        document_date=locked.received_at,
        narration=locked.notes or str(_("استلام بضاعة من مورد")),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE,
    )
    # Naming the journal on the stock entry is what arms the kernel's
    # conditional control-account invariant over these movements.
    link_journal_entry(entry=stock_entry, journal=journal)

    _link_lines(lines, plan=plan, movements=movement_by_key, journal=journal)

    locked.posted_value = quantize_money(
        sum((plan.values[line.pk][1] for line in lines), start=ZERO)
    )
    locked.stock_entry = stock_entry
    locked.journal_entry = journal
    locked.status = GoodsReceiptStatus.POSTED
    locked.posted_by = actor
    locked.posted_at = timezone.now()
    locked.save(
        update_fields=[
            "business_date_timezone",
            "business_day_start",
            "number",
            "posted_value",
            "stock_entry",
            "journal_entry",
            "status",
            "posted_by",
            "posted_at",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(locked),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "number": locked.number,
            "stock_entry": stock_entry.pk,
            "journal_entry": journal.entry_number,
            "line_count": len(lines),
            "posted_value": format(locked.posted_value, "f"),
        },
    )
    return locked


def _link_lines(
    lines: list[GoodsReceiptLine],
    *,
    plan: _Plan,
    movements: dict[str, StockMovement],
    journal: JournalEntry,
) -> None:
    """
    Write the immutable traceability, while the lines are still unfrozen.

    Called before the status flips, which is the only window the trigger
    leaves open — deliberately, so that a posted line can never be edited and
    an unposted one can never claim a movement.
    """
    journal_line_by_account: dict[int, JournalLine] = {
        journal_line.account_id: journal_line
        for journal_line in journal.lines.filter(debit__gt=ZERO)
    }
    for line in lines:
        base_cost, value = plan.values[line.pk]
        line.posted_unit_cost = base_cost
        line.posted_value = value
        if line.pk in plan.accounts:
            control, grni = plan.accounts[line.pk]
            inventory_mapping, organization_mapping = plan.mappings[line.pk]
            line.inventory_account = control
            line.contra_account = grni
            line.resolved_mapping = inventory_mapping
            line.resolved_organization_mapping = organization_mapping
            line.movement = movements[effect_key(line)]
            line.journal_line = journal_line_by_account[control.pk]
        line.save(
            update_fields=[
                "posted_unit_cost",
                "posted_value",
                "inventory_account",
                "contra_account",
                "resolved_mapping",
                "resolved_organization_mapping",
                "movement",
                "journal_line",
                "updated_at",
            ]
        )


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_goods_receipt(*, receipt: GoodsReceipt, actor: User, reason: str) -> GoodsReceipt:
    """
    Take back a posted receipt — the whole document, never a line.

    Both mirrors are exact. Each movement gives back its own quantity and
    value, and the reversing journal mirrors the original lines, whatever the
    averages or the mappings have since become. Nothing is re-resolved.

    Availability applies: goods that have been issued, transferred or counted
    away cannot be un-received, and the kernel refuses it before anything is
    written. That refusal is correct rather than inconvenient — a reversal
    says "this delivery did not happen", and stock that has been cooked did.

    The reversal is dated **today**, in a period that must accept postings. A
    correction is a new event, and backdating one into a closed period is the
    thing period closing exists to stop.
    """
    locked = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if locked.status == GoodsReceiptStatus.REVERSED:
        raise ValidationError(_("This receipt is already reversed."), code="already_reversed")
    if locked.status != GoodsReceiptStatus.POSTED:
        raise ValidationError(
            _("Only a posted receipt can be reversed."), code="receipt_not_posted"
        )
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")

    _require_no_downstream_dependency(locked)

    now = timezone.now()
    reversal_business_date = resolve_business_day(locked.branch, now).business_date

    assert locked.stock_entry is not None  # noqa: S101 - a constraint guarantees both
    assert locked.journal_entry is not None  # noqa: S101

    lines = list(locked.lines.select_related("item", "lot").order_by("sequence"))

    # Empty the bin before the warehouse total drops. The kernel's automatic
    # release would otherwise take the shortfall out of locations in code
    # order, which keeps the invariant true but takes it from whichever bin
    # sorts first — and this receipt knows which bin it filled.
    if locked.location is not None:
        _pick_back_from_the_original_location(locked, lines)

    reversing_stock = reverse_stock_entry(
        entry=locked.stock_entry,
        idempotency_key=f"procurement-goods-receipt-reverse:{locked.public_id}",
        reason=reason.strip(),
        effective_at=now,
        business_date=reversal_business_date,
    )
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"procurement-goods-receipt-journal-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )
    link_journal_entry(entry=reversing_stock, journal=reversal_journal)

    locked.status = GoodsReceiptStatus.REVERSED
    locked.reversed_by = actor
    locked.reversed_at = now
    locked.reversal_reason = reason.strip()
    locked.reversal_journal_entry = reversal_journal
    locked.save(
        update_fields=[
            "status",
            "reversed_by",
            "reversed_at",
            "reversal_reason",
            "reversal_journal_entry",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.REVERSED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(locked),
        reason=reason.strip(),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"reversal_journal": reversal_journal.entry_number},
    )
    return locked


def _pick_back_from_the_original_location(
    receipt: GoodsReceipt, lines: list[GoodsReceiptLine]
) -> None:
    """
    Return the accepted quantity from the receipt's bin to the unlocated pool.

    Only what the bin still holds. Somebody moving goods between shelves is
    ordinary warehouse work and must not make a reversal impossible, so where
    the bin has less than the receipt put there, the kernel's deterministic
    auto-release covers the remainder as it does for any other outbound. The
    warehouse total falls by exactly the accepted quantity either way, which is
    the invariant; which bin it leaves is recorded in the location movements.
    """
    for line in lines:
        if line.accepted_base_quantity <= ZERO or receipt.location is None:
            continue
        held = _located_quantity_for(receipt, line)
        take = min(held, line.accepted_base_quantity)
        if take <= ZERO:
            continue
        pick(
            location=receipt.location,
            item=line.item,
            lot=line.lot,
            quantity=take,
            reference=f"GRN reversal {receipt.public_id}",
            stock_movement=line.movement,
        )


def _located_quantity_for(receipt: GoodsReceipt, line: GoodsReceiptLine) -> Decimal:
    """How much of this item and lot the receipt's own bin still holds."""
    total: Decimal | None = StockLocationBalance.objects.filter(
        location=receipt.location, item=line.item, lot=line.lot
    ).aggregate(held=Sum("quantity"))["held"]
    return total or ZERO


def _require_no_downstream_dependency(receipt: GoodsReceipt) -> None:
    """
    Nothing later may already depend on this receipt.

    Tasks 2.10 onwards attach invoices, matches and supplier returns to a
    posted receipt line, and each of them will take a value from it. Reversing
    underneath one would leave it citing a delivery that no longer happened.

    Those documents do not exist yet, so this checks the relations that do —
    and it is written as a loop over related accessors rather than a list of
    imports, so a later task that adds `matched_lines` to `GoodsReceiptLine`
    gets the guard by declaring the relation rather than by remembering this
    function. A dependency that reports zero rows is not evidence that none
    will ever exist; it is the extension point holding the line until one does.
    """
    known = {"history", "goods_receipt_lines"}
    for line in receipt.lines.all():
        for relation in line._meta.related_objects:
            name = relation.get_accessor_name()
            if name in known or name is None:
                continue
            related = getattr(line, name, None)
            if related is None or not hasattr(related, "exists"):
                continue
            if related.exists():
                raise ValidationError(
                    _(
                        "Another document (%(relation)s) already depends on this receipt. "
                        "Reverse it first."
                    ),
                    code="receipt_has_dependents",
                    params={"relation": name},
                )


# ---------------------------------------------------------------------------
# Read models the screens and the verifier both need
# ---------------------------------------------------------------------------


def posted_accepted_quantity(*, order_line_id: int) -> Decimal:
    """
    Irreversibly received quantity for one order line.

    Posted receipts only. A draft is somebody typing while a lorry is being
    unloaded, and a reversed receipt has given its stock back — neither
    permanently consumes an ordered quantity (F7).
    """
    total: Decimal | None = GoodsReceiptLine.objects.filter(
        order_line_id=order_line_id, receipt__status=GoodsReceiptStatus.POSTED
    ).aggregate(total=Sum("accepted_base_quantity"))["total"]
    return total or ZERO


def receipt_timeline(receipt: GoodsReceipt) -> list[dict[str, Any]]:
    """The dated facts about one receipt, oldest first, for the detail screen."""
    events: list[dict[str, Any]] = [
        {
            "label": _("سُجّل"),
            "at": receipt.created_at,
            "who": receipt.created_by.username,
        }
    ]
    if receipt.inspected_at is not None:
        events.append(
            {
                "label": _("فُحص"),
                "at": receipt.inspected_at,
                "who": receipt.inspected_by.username if receipt.inspected_by else "",
            }
        )
    if receipt.posted_at is not None:
        events.append(
            {
                "label": _("رُحّل"),
                "at": receipt.posted_at,
                "who": receipt.posted_by.username if receipt.posted_by else "",
            }
        )
    if receipt.reversed_at is not None:
        events.append(
            {
                "label": _("عُكس"),
                "at": receipt.reversed_at,
                "who": receipt.reversed_by.username if receipt.reversed_by else "",
                "note": receipt.reversal_reason,
            }
        )
    return events
