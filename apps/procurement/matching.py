"""
Three-way matching: what covers what, and what the difference is.

The purchase order said a price, the delivery said a quantity and parked a
provisional value in GRNI, and the invoice says an amount. Matching is the act
of stating, explicitly and auditably, which part of which delivery covers which
part of which invoice — and therefore what the difference between the two is.

    price_variance = invoice_allocated_value - receipt_allocated_value

**Nothing here reaches a ledger.** No journal, no stock movement, no payable,
no GRNI clearing, no revaluation. The supplier invoice stays `APPROVED`
throughout, and a test in `test_matching.py` asserts every one of those
absences. Task 2.12 turns a `READY` match into the posting Task 2.0 §9
describes; until then a `READY` match means "the evidence is complete", which
is a different sentence from "the money has moved" and the screens say so.

## Why there is a header at all

Task 2.0 §9 sketches `MatchAllocation` rows and no aggregate. The rows are kept
exactly as specified — they carry the economics — but they are gathered under
`PurchaseMatch`, because Task 2.12 must post from a set of allocations that
cannot change underneath it. Without a header, "the allocations for this
invoice" is a query whose answer moves every time somebody adds a row, and a
financial posting cannot be built on a moving target. `READY` is the freeze.

## Availability is derived, never stored

Every remainder is computed from the active allocation rows:

    receipt available  = posted accepted quantity - active matched quantity
    invoice available  = approved invoice quantity - active matched quantity
    order available    = ordered quantity        - active matched quantity

A stored "remaining" column would be a second figure that drifts the first time
a match is cancelled, and cancelling a match is an ordinary correction.

## Exact value allocation

A partial allocation takes its proportional share; the **final** allocation
against a line takes the exact remainder. That is what makes

    sum(active allocations) + remaining == the original stored value

true by construction rather than approximately, and it is the same
largest-remainder discipline `apps/core/allocation.py` applies within a
document — here applied across time, because allocations arrive one at a time
rather than all at once.

The basis is the receipt's own **posted** value, never today's warehouse
average. The average has moved since; what the delivery parked in GRNI has not.

## Lock order

    1. the match header                      select_for_update
    2. the supplier invoice                  select_for_update
    3. the invoice lines, by primary key     select_for_update
    4. the goods receipt lines, by pk        select_for_update
    5. the purchase order lines, by pk       select_for_update
    6. the existing active allocations       select_for_update

Every step sorts by primary key rather than by the order the caller listed
things, so two multi-line matches naming the same rows in opposite order take
them in the same database order and cannot deadlock.

No account-mapping lock appears here, and its absence is deliberate: this
module resolves no account and posts no journal, so taking the mapping lock
would be claiming a dependency that does not exist.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.procurement.lifecycle import lock_and_require_status
from apps.procurement.models import (
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseMatch,
    PurchaseMatchAllocation,
    PurchaseMatchStatus,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceLineType,
    SupplierInvoiceStatus,
)
from apps.users.models import User

ZERO = Decimal("0.000")

DOCUMENT_TYPE = "PURCHASE_MATCH"

#: What the audit trail records this document as. There is no accounting
#: source identity, because Task 2.11 writes no journal — inventing a
#: `SourceEvent` for a document that reaches no ledger would put a lie in the
#: one table whose whole job is saying where a posting came from (ADR-017).
AUDIT_DOCUMENT_TYPE = "PROCUREMENT_PURCHASE_MATCH"


# ---------------------------------------------------------------------------
# Derived matching state (PRC-042)
# ---------------------------------------------------------------------------


class LineMatchState:
    """
    The four answers Task 2.0 PRC-042 names, derived and never stored.

    A stored flag would be a second opinion that goes stale the moment an
    allocation is added or a match cancelled, and the stale one is always the
    flag.
    """

    UNMATCHED = "UNMATCHED"
    PARTIALLY_MATCHED = "PARTIALLY_MATCHED"
    MATCHED = "MATCHED"
    EXCEPTION = "EXCEPTION"


def invoice_line_match_state(line: SupplierInvoiceLine) -> str:
    """
    Where one invoice line stands.

    `EXCEPTION` is the fully-covered line whose money still disagrees: the
    quantity reconciles and the value does not. Task 2.0 names the state and
    does not define which case earns it, so this is the reading — and it is
    the one a human actually needs flagged, because a fully matched line with
    a variance is exactly the row somebody has to decide about in Task 2.12.
    """
    if line.line_type != SupplierInvoiceLineType.INVENTORY:
        return LineMatchState.UNMATCHED
    matched = matched_quantity_for_invoice_line(line)
    invoiced = line.base_quantity or ZERO
    if matched <= ZERO:
        return LineMatchState.UNMATCHED
    if matched < invoiced:
        return LineMatchState.PARTIALLY_MATCHED
    variance = variance_for_invoice_line(line)
    return LineMatchState.EXCEPTION if variance != ZERO else LineMatchState.MATCHED


def _active_allocations() -> Any:
    """Allocations whose match still counts. A cancelled match consumes nothing."""
    return PurchaseMatchAllocation.objects.exclude(match__status=PurchaseMatchStatus.CANCELLED)


def matched_quantity_for_invoice_line(line: SupplierInvoiceLine) -> Decimal:
    total: Decimal | None = (
        _active_allocations()
        .filter(supplier_invoice_line=line)
        .aggregate(total=Sum("matched_base_quantity"))["total"]
    )
    return total or ZERO


def matched_quantity_for_receipt_line(line: GoodsReceiptLine) -> Decimal:
    total: Decimal | None = (
        _active_allocations()
        .filter(goods_receipt_line=line)
        .aggregate(total=Sum("matched_base_quantity"))["total"]
    )
    return total or ZERO


def matched_quantity_for_order_line(line: PurchaseOrderLine) -> Decimal:
    total: Decimal | None = (
        _active_allocations()
        .filter(purchase_order_line=line)
        .aggregate(total=Sum("matched_base_quantity"))["total"]
    )
    return total or ZERO


def variance_for_invoice_line(line: SupplierInvoiceLine) -> Decimal:
    total: Decimal | None = (
        _active_allocations()
        .filter(supplier_invoice_line=line)
        .aggregate(total=Sum("price_variance"))["total"]
    )
    return total or ZERO


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Availability:
    """What is left of one source line, in quantity and in value."""

    quantity: Decimal
    value: Decimal


def receipt_availability(line: GoodsReceiptLine) -> Availability:
    """
    What a posted delivery line still has to give.

    The value basis is the receipt's own `posted_value` — what it actually
    parked in GRNI — minus what active allocations have already taken. Never
    today's moving average: the average has changed since the goods arrived,
    and matching is about the delivery, not about the warehouse.
    """
    allocated: dict[str, Decimal | None] = (
        _active_allocations()
        .filter(goods_receipt_line=line)
        .aggregate(quantity=Sum("matched_base_quantity"), value=Sum("receipt_allocated_value"))
    )
    return Availability(
        quantity=quantize_quantity(line.accepted_base_quantity - (allocated["quantity"] or ZERO)),
        value=quantize_money((line.posted_value or ZERO) - (allocated["value"] or ZERO)),
    )


def invoice_line_availability(line: SupplierInvoiceLine) -> Availability:
    """
    What an approved invoice line still has to be covered.

    The value basis is the line's stored `net_amount`, which is what it charges
    **after** any Task 2.10 freight and discount allocation. Re-pricing the
    line here would silently disagree with the document the supplier sent.
    """
    allocated: dict[str, Decimal | None] = (
        _active_allocations()
        .filter(supplier_invoice_line=line)
        .aggregate(quantity=Sum("matched_base_quantity"), value=Sum("invoice_allocated_value"))
    )
    return Availability(
        quantity=quantize_quantity((line.base_quantity or ZERO) - (allocated["quantity"] or ZERO)),
        value=quantize_money(line.net_amount - (allocated["value"] or ZERO)),
    )


def order_line_availability(line: PurchaseOrderLine) -> Decimal:
    """How much of an order line is not yet accounted for by an active match."""
    return quantize_quantity(line.ordered_base_quantity - matched_quantity_for_order_line(line))


def _share(*, remaining_value: Decimal, remaining_quantity: Decimal, taking: Decimal) -> Decimal:
    """
    This allocation's share of what a line has left.

    The last allocation against a line takes the **exact** remainder rather
    than a computed proportion, which is what makes

        sum(allocations) + remaining == the original stored value

    hold exactly. Computing the final share proportionally would leave a
    residual of a millifils that nothing owns and reconciliation would report
    forever.
    """
    if remaining_quantity <= ZERO:  # pragma: no cover - guarded before this is called
        return ZERO
    if taking == remaining_quantity:
        return remaining_value
    return quantize_money(remaining_value * taking / remaining_quantity)


# ---------------------------------------------------------------------------
# The match
# ---------------------------------------------------------------------------


def _require_draft(match: PurchaseMatch) -> PurchaseMatch:
    """Only a draft may be edited, re-read under a row lock."""
    return lock_and_require_status(
        PurchaseMatch,
        match.pk,
        {PurchaseMatchStatus.DRAFT},
        code="match_not_editable",
        message=_("This match is no longer a draft and cannot be edited."),
    )


@transaction.atomic
def create_purchase_match(
    *, invoice: SupplierInvoice, created_by: User, notes: str = ""
) -> PurchaseMatch:
    """
    Open a draft match against one approved supplier invoice.

    The invoice is re-read under a lock: a match against a reversed invoice is
    evidence about a debt that no longer exists, and a copy loaded a moment ago
    may already have been reversed.
    """
    locked_invoice = SupplierInvoice.objects.select_for_update().get(pk=invoice.pk)
    if locked_invoice.status == SupplierInvoiceStatus.REVERSED:
        raise ValidationError(
            _("That invoice has been reversed and cannot be matched."), code="invoice_reversed"
        )
    if locked_invoice.status == SupplierInvoiceStatus.DRAFT:
        raise ValidationError(
            _("An invoice must be approved before it can be matched."),
            code="invoice_not_approved",
        )
    if not _matchable_lines(locked_invoice):
        raise ValidationError(
            _(
                "This invoice bills for no goods. Direct expense lines are posted by "
                "Task 2.10 and are never matched against a delivery."
            ),
            code="invoice_has_no_matchable_lines",
        )
    if (
        PurchaseMatch.objects.filter(supplier_invoice=locked_invoice)
        .exclude(status=PurchaseMatchStatus.CANCELLED)
        .exists()
    ):
        raise ValidationError(
            _("This invoice already has an active match. Cancel it before starting another."),
            code="match_already_active",
        )

    match = PurchaseMatch(
        organization=locked_invoice.organization,
        supplier=locked_invoice.supplier,
        supplier_invoice=locked_invoice,
        created_by=created_by,
        notes=notes.strip(),
    )
    match.full_clean()
    match.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=match,
        branch=locked_invoice.branch,
        new_state=snapshot(match),
    )
    return match


def _matchable_lines(invoice: SupplierInvoice) -> list[SupplierInvoiceLine]:
    """
    Only goods lines are matched.

    A direct expense line from Task 2.10 has a complete accounting route
    already and reached the ledger when the invoice posted — matching it
    against a delivery would be matching a delivery charge against goods.
    """
    return [
        line
        for line in invoice.lines.order_by("sequence")
        if line.line_type == SupplierInvoiceLineType.INVENTORY
    ]


@transaction.atomic
def add_allocation(
    *,
    match: PurchaseMatch,
    invoice_line: SupplierInvoiceLine,
    receipt_line: GoodsReceiptLine,
    matched_base_quantity: Decimal,
    created_by: User,
    note: str = "",
) -> PurchaseMatchAllocation:
    """
    State that this much of one delivery covers this much of one invoice line.

    Everything is checked against rows locked in the documented order, so two
    allocations racing for the same remainder contend on the same rows rather
    than both reading the pre-commit figure. That race is the whole reason
    availability is not a cached column.
    """
    locked = _require_draft(match)
    quantity = quantize_quantity(matched_base_quantity)
    if quantity <= ZERO:
        raise ValidationError(
            _("A matched quantity must be greater than zero."), code="quantity_not_positive"
        )

    # 2. The invoice, then 3. its line.
    invoice = SupplierInvoice.objects.select_for_update().get(pk=locked.supplier_invoice_id)
    _require_invoice_is_matchable(invoice)
    locked_invoice_line = SupplierInvoiceLine.objects.select_for_update().get(pk=invoice_line.pk)
    _require_line_belongs_to(locked_invoice_line, invoice)

    # 4. The receipt line, and the receipt behind it.
    locked_receipt_line = (
        GoodsReceiptLine.objects.select_for_update()
        .select_related("receipt", "item")
        .get(pk=receipt_line.pk)
    )
    _require_receipt_is_matchable(locked_receipt_line, invoice)

    # 5. The order line the receipt named, and the version it was measured
    # against. Copied rather than joined to: a revision after this allocation
    # must not restate what was matched.
    order_line = locked_receipt_line.order_line
    order_version: int | None = None
    if order_line is not None:
        order_line = (
            PurchaseOrderLine.objects.select_for_update()
            .select_related("order")
            .get(pk=order_line.pk)
        )
        _require_order_is_matchable(order_line)
        order_version = locked_receipt_line.receipt.order_version or order_line.order.version

    _require_same_item(locked_invoice_line, locked_receipt_line)

    # 6. The existing allocations, so availability is read behind the lock.
    list(
        PurchaseMatchAllocation.objects.select_for_update()
        .filter(
            supplier_invoice_line=locked_invoice_line,
        )
        .order_by("pk")
    )
    list(
        PurchaseMatchAllocation.objects.select_for_update()
        .filter(goods_receipt_line=locked_receipt_line)
        .order_by("pk")
    )

    receipt_left = receipt_availability(locked_receipt_line)
    invoice_left = invoice_line_availability(locked_invoice_line)
    _require_within(quantity, receipt_left.quantity, what="receipt", line=locked_receipt_line)
    _require_within(quantity, invoice_left.quantity, what="invoice", line=locked_invoice_line)
    if order_line is not None:
        available = order_line_availability(order_line)
        if quantity > available:
            raise ValidationError(
                _(
                    "Order line %(item)s has %(available)s unmatched; %(asked)s would "
                    "exceed what was ordered."
                ),
                code="order_over_allocation",
                params={
                    "item": order_line.item.code,
                    "available": format(available, "f"),
                    "asked": format(quantity, "f"),
                },
            )

    receipt_value = _share(
        remaining_value=receipt_left.value,
        remaining_quantity=receipt_left.quantity,
        taking=quantity,
    )
    invoice_value = _share(
        remaining_value=invoice_left.value,
        remaining_quantity=invoice_left.quantity,
        taking=quantity,
    )

    allocation = PurchaseMatchAllocation(
        match=locked,
        sequence=_next_sequence(locked),
        supplier_invoice_line=locked_invoice_line,
        goods_receipt_line=locked_receipt_line,
        purchase_order_line=order_line,
        purchase_order_version=order_version,
        matched_base_quantity=quantity,
        receipt_allocated_value=receipt_value,
        invoice_allocated_value=invoice_value,
        price_variance=quantize_money(invoice_value - receipt_value),
        created_by=created_by,
        note=note.strip(),
    )
    allocation.full_clean()
    allocation.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=allocation,
        branch=invoice.branch,
        new_state=snapshot(allocation),
    )
    return allocation


def _require_within(quantity: Decimal, available: Decimal, *, what: str, line: Any) -> None:
    if quantity <= available:
        return
    label = line.item.code if getattr(line, "item", None) else str(line)
    raise ValidationError(
        _("%(what)s line %(label)s has %(available)s unmatched; %(asked)s would exceed it."),
        code=f"{what}_over_allocation",
        params={
            "what": what,
            "label": label,
            "available": format(available, "f"),
            "asked": format(quantity, "f"),
        },
    )


def _require_invoice_is_matchable(invoice: SupplierInvoice) -> None:
    if invoice.status == SupplierInvoiceStatus.REVERSED:
        raise ValidationError(
            _("That invoice has been reversed and cannot be matched."), code="invoice_reversed"
        )
    if invoice.status == SupplierInvoiceStatus.DRAFT:
        raise ValidationError(
            _("An invoice must be approved before it can be matched."),
            code="invoice_not_approved",
        )


def _require_line_belongs_to(line: SupplierInvoiceLine, invoice: SupplierInvoice) -> None:
    if line.invoice_id != invoice.pk:
        raise ValidationError(
            _("That line belongs to a different invoice."), code="invoice_line_mismatch"
        )
    if line.line_type != SupplierInvoiceLineType.INVENTORY:
        raise ValidationError(
            _(
                "Line %(sequence)s is a direct charge. It posted with the invoice and is "
                "never matched against a delivery."
            ),
            code="line_not_matchable",
            params={"sequence": line.sequence},
        )


def _require_receipt_is_matchable(line: GoodsReceiptLine, invoice: SupplierInvoice) -> None:
    receipt = line.receipt
    if receipt.organization_id != invoice.organization_id:
        raise ValidationError(
            _("That delivery belongs to another organization."), code="organization_mismatch"
        )
    if receipt.supplier_id != invoice.supplier_id:
        raise ValidationError(
            _("That delivery came from a different supplier."), code="supplier_mismatch"
        )
    if receipt.status != GoodsReceiptStatus.POSTED:
        raise ValidationError(
            _(
                "Delivery %(number)s is %(status)s. Only a posted delivery has a value to "
                "match against."
            ),
            code="receipt_not_posted",
            params={
                "number": receipt.number or str(receipt.public_id),
                "status": receipt.get_status_display(),
            },
        )
    if line.accepted_base_quantity <= ZERO:
        raise ValidationError(
            _("That delivery line was wholly rejected and brought nothing into stock."),
            code="receipt_line_rejected",
        )


def _require_order_is_matchable(line: PurchaseOrderLine) -> None:
    if line.order.status == PurchaseOrderStatus.CANCELLED:
        raise ValidationError(
            _("Order %(number)s was cancelled."),
            code="order_cancelled",
            params={"number": line.order.number},
        )


def _require_same_item(invoice_line: SupplierInvoiceLine, receipt_line: GoodsReceiptLine) -> None:
    """
    One item, one match.

    Comparing base quantities across two different items would be arithmetic
    on numbers that mean different things — sixty kilograms of rice covering
    sixty kilograms of chicken.
    """
    if invoice_line.item_id != receipt_line.item_id:
        raise ValidationError(
            _("The invoice line and the delivery line are for different items."),
            code="item_mismatch",
        )


def _next_sequence(match: PurchaseMatch) -> int:
    highest = (
        PurchaseMatchAllocation.objects.filter(match=match)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    return (highest or 0) + 1


@transaction.atomic
def remove_allocation(*, allocation: PurchaseMatchAllocation) -> None:
    """Drop an allocation from a draft. Sequences are not renumbered."""
    locked = _require_draft(allocation.match)
    previous = snapshot(allocation)
    record_audit_event(
        action=AuditAction.DELETED,
        target=allocation,
        branch=locked.supplier_invoice.branch,
        previous_state=previous,
    )
    allocation.delete()


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def allocation_fingerprint(match: PurchaseMatch) -> str:
    """
    A canonical hash of what this match claims.

    Ordered by `allocation_uid` rather than by primary key or by the order a
    queryset happened to return, so the same economic set always hashes the
    same way whatever order it was entered in. Every figure goes in as an
    exact decimal string: a float would make the hash a function of binary
    rounding.
    """
    rows = [
        {
            "invoice_line": str(row.supplier_invoice_line.line_uid),
            "receipt_line": str(row.goods_receipt_line.line_uid),
            "order_line": (
                str(row.purchase_order_line.line_uid) if row.purchase_order_line else None
            ),
            "order_version": row.purchase_order_version,
            "quantity": format(row.matched_base_quantity, "f"),
            "receipt_value": format(row.receipt_allocated_value, "f"),
            "invoice_value": format(row.invoice_allocated_value, "f"),
        }
        for row in match.allocations.select_related(
            "supplier_invoice_line", "goods_receipt_line", "purchase_order_line"
        )
    ]
    rows.sort(key=lambda row: (row["invoice_line"], row["receipt_line"]))
    payload = json.dumps(
        {"invoice": str(match.supplier_invoice.public_id), "allocations": rows},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@transaction.atomic
def mark_match_ready(*, match: PurchaseMatch, actor: User) -> PurchaseMatch:
    """
    Freeze the evidence.

    `READY` says the allocations are agreed and immutable. It says nothing
    about money: the invoice is still `APPROVED`, no journal exists, and Task
    2.12 is what turns this into a posting. A screen that implied otherwise
    would be the worst thing this module could ship.

    Idempotent. A retry of the same command over the same allocation set
    returns the match already frozen; a retry over a *different* set is a
    different economic claim and is refused rather than silently accepted.
    """
    locked = PurchaseMatch.objects.select_for_update().get(pk=match.pk)
    fingerprint = allocation_fingerprint(locked)

    if locked.status == PurchaseMatchStatus.READY:
        if locked.allocation_fingerprint == fingerprint:
            return locked
        raise ValidationError(
            _("This match is already ready, and the allocations presented differ from it."),
            code="idempotency_key_conflict",
        )
    if locked.status == PurchaseMatchStatus.CANCELLED:
        raise ValidationError(
            _("A cancelled match cannot be made ready. Start a new one."),
            code="match_cancelled",
        )

    allocations = list(
        locked.allocations.select_related(
            "supplier_invoice_line", "goods_receipt_line", "purchase_order_line"
        ).order_by("sequence")
    )
    if not allocations:
        raise ValidationError(
            _("A match with no allocations claims nothing."), code="no_allocations"
        )

    invoice = SupplierInvoice.objects.select_for_update().get(pk=locked.supplier_invoice_id)
    _require_invoice_is_matchable(invoice)
    _require_allocations_still_hold(allocations)

    locked.status = PurchaseMatchStatus.READY
    locked.ready_by = actor
    locked.ready_at = timezone.now()
    locked.allocation_fingerprint = fingerprint
    locked.number = _next_match_number(locked)
    locked.save(
        update_fields=[
            "status",
            "ready_by",
            "ready_at",
            "allocation_fingerprint",
            "number",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=invoice.branch,
        new_state=snapshot(locked),
        source_document_type=AUDIT_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "number": locked.number,
            "allocation_count": len(allocations),
            "price_variance": format(locked.total_price_variance, "f"),
        },
    )
    return locked


def _require_allocations_still_hold(allocations: list[PurchaseMatchAllocation]) -> None:
    """
    Re-check every allocation against the documents as they stand now.

    A draft can sit for a day. In that time a delivery may have been reversed
    or an order cancelled, and freezing a match that cites one would produce
    evidence about something that no longer happened.

    Availability is re-checked too, and this is not redundant with
    `add_allocation`: another match may have taken the remainder since, and
    the row locks that made each individual addition safe were released when
    that transaction committed.
    """
    for allocation in allocations:
        receipt = allocation.goods_receipt_line.receipt
        if receipt.status != GoodsReceiptStatus.POSTED:
            raise ValidationError(
                _("Delivery %(number)s is no longer posted."),
                code="receipt_not_posted",
                params={"number": receipt.number or str(receipt.public_id)},
            )
        order_line = allocation.purchase_order_line
        if order_line is not None and order_line.order.status == PurchaseOrderStatus.CANCELLED:
            raise ValidationError(
                _("Order %(number)s has been cancelled since this allocation was made."),
                code="order_cancelled",
                params={"number": order_line.order.number},
            )

    # Availability, per line, counting only what other matches hold.
    for allocation in allocations:
        receipt_line = allocation.goods_receipt_line
        taken_elsewhere = (
            _active_allocations()
            .filter(goods_receipt_line=receipt_line)
            .exclude(match=allocation.match)
            .aggregate(total=Sum("matched_base_quantity"))["total"]
            or ZERO
        )
        mine = (
            _active_allocations()
            .filter(goods_receipt_line=receipt_line, match=allocation.match)
            .aggregate(total=Sum("matched_base_quantity"))["total"]
            or ZERO
        )
        if taken_elsewhere + mine > receipt_line.accepted_base_quantity:
            raise ValidationError(
                _(
                    "Delivery line %(item)s has been matched elsewhere since; this match "
                    "would over-allocate it."
                ),
                code="receipt_over_allocation",
                params={"item": receipt_line.item.code},
            )


def _next_match_number(match: PurchaseMatch) -> str:
    """
    A gapless human number per organization and year, drawn at READY.

    Its own counter rather than `next_document_number`: a match is not a
    procurement *document* in the numbering sense — it draws no ledger number
    and burns nothing when abandoned — and adding it to that sequence would
    put gaps in a series an auditor reads as gapless.
    """
    year = (match.ready_at or timezone.now()).year
    highest = (
        PurchaseMatch.objects.filter(
            organization=match.organization, number__startswith=f"MTC-{year}-"
        )
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
    )
    nextn = int(highest.rsplit("-", 1)[1]) + 1 if highest else 1
    return f"MTC-{year}-{nextn:06d}"


@transaction.atomic
def cancel_purchase_match(*, match: PurchaseMatch, actor: User, reason: str) -> PurchaseMatch:
    """
    Withdraw a match, draft or ready.

    This is the correction mechanism. A `READY` match is immutable, so putting
    it back into `DRAFT` would mean un-freezing evidence somebody may already
    have acted on — cancel it, with a reason, and build a replacement.

    Cancelling releases the quantity and value the match was holding, because
    availability counts active matches only. That is the whole reason
    availability is derived.
    """
    locked = PurchaseMatch.objects.select_for_update().get(pk=match.pk)
    if locked.status == PurchaseMatchStatus.CANCELLED:
        raise ValidationError(_("This match is already cancelled."), code="already_cancelled")
    if not reason.strip():
        raise ValidationError(_("A cancellation needs a reason."), code="reason_required")

    previous = snapshot(locked)
    locked.status = PurchaseMatchStatus.CANCELLED
    locked.cancelled_by = actor
    locked.cancelled_at = timezone.now()
    locked.cancellation_reason = reason.strip()
    locked.save(
        update_fields=[
            "status",
            "cancelled_by",
            "cancelled_at",
            "cancellation_reason",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.CANCELLED,
        target=locked,
        branch=locked.supplier_invoice.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


@transaction.atomic
def delete_purchase_match(*, match: PurchaseMatch) -> None:
    """Discard a draft that was never agreed. A trigger refuses anything further."""
    locked = _require_draft(match)
    previous = snapshot(locked)
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.supplier_invoice.branch,
        previous_state=previous,
    )
    locked.delete()


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def coverage_for_invoice(invoice: SupplierInvoice) -> list[dict[str, Any]]:
    """
    Line by line: what was invoiced, what is matched, what is left.

    Quantity exceptions are read from this rather than stored, and they are
    kept distinct from price variance throughout — an unmatched quantity and a
    disagreeing price are different problems with different answers, and one
    table calling both "variance" is how they stop being told apart.
    """
    rows: list[dict[str, Any]] = []
    for line in _matchable_lines(invoice):
        left = invoice_line_availability(line)
        rows.append(
            {
                "line": line,
                "invoiced_quantity": line.base_quantity or ZERO,
                "matched_quantity": matched_quantity_for_invoice_line(line),
                "unmatched_quantity": left.quantity,
                "invoiced_value": line.net_amount,
                "unmatched_value": left.value,
                "price_variance": variance_for_invoice_line(line),
                "state": invoice_line_match_state(line),
            }
        )
    return rows


def unmatched_receipt_lines(*, organization_id: int, supplier_id: int | None = None) -> Any:
    """
    Posted delivery lines that still have quantity nobody has claimed.

    The matching queue's left-hand side: what arrived and has not been billed,
    which in a restaurant is usually the thing somebody wants to know about
    before month end.
    """
    queryset = (
        GoodsReceiptLine.objects.filter(
            receipt__organization_id=organization_id,
            receipt__status=GoodsReceiptStatus.POSTED,
            accepted_base_quantity__gt=ZERO,
        )
        .select_related("receipt", "receipt__supplier", "item", "order_line")
        .order_by("-receipt__id", "sequence")
    )
    if supplier_id is not None:
        queryset = queryset.filter(receipt__supplier_id=supplier_id)

    rows: list[GoodsReceiptLine] = []
    for line in queryset:
        left = receipt_availability(line)
        if left.quantity <= ZERO:
            continue
        # Annotated for the screen rather than stored. A queue is a read, and
        # a column that cached this would drift the first time a match was
        # cancelled — which is an ordinary correction, not an exception.
        line.unmatched_quantity = left.quantity  # type: ignore[attr-defined]
        line.unmatched_value = left.value  # type: ignore[attr-defined]
        rows.append(line)
    return rows
