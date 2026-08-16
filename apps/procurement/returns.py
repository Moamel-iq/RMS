"""
Supplier returns: goods going back out to the supplier they came from.

The fourth procurement event, and the first that sends stock *out*.

    Dr  SUPPLIER_RETURN_CLEARING     the book value that left
        Cr  Inventory control        the same figure, per control account

and nothing else.

## Why no variance here

The supplier has not yet said what they will credit. Booking a gain or a loss
against an expectation would put a figure on the profit and loss that nobody
has agreed to, and ADR-022 §2's worked example — a return that credits more
than it removed — is the case that makes the difference visible. That
difference is `PURCHASE_RETURN_VARIANCE`, recognised by Task 2.14's credit
note. Until then the clearing balance **is** the claim outstanding, which is
what the breakdown's "supplier-credit-expected state" amounts to.

An `expected_credit_value` may be recorded per line. It is commercial metadata:
it posts nothing, no journal figure is derived from it, and a test says so.

## Why no payable and no GRNI

Both were considered and both are wrong at this event. Debiting the payable
would move a liability with no document stating its amount. Debiting GRNI would
make the return's accounting depend on whether an invoice had arrived — and
Task 2.13 depends on 2.9, not on the invoice, because goods go back before
anyone has agreed a price often enough that the breakdown says so outright.
A clearing account settles both: one shape whatever the invoice has done.

## Valuation

Stock leaves at the **standing moving average**, like every other outbound, and
a return that empties a position surrenders its whole remaining book value
(ADR-018's full-depletion rule, PRC-048). Never at the receipt price: that
would need a cost layer this system does not keep, and under a moving average
it would be a fiction — if 100 kg arrived at 1,000 and 100 kg at 2,000, no
kilogram in that warehouse *is* the cheap one.

## Lock order

    1. the return row                      select_for_update
    2. the source receipt and its lines     select_for_update, by pk
    3. the organization's mapping lock       shared, advisory
    4. the return's own lines                select_for_update, by sequence
    5. the warehouse freeze lock             inside the kernel
    6. every stock key, canonically          inside the kernel
    7. the procurement document-number counter
    8. the journal-number counter            inside post_entry

Step 3 sits above every row lock below it, which is the order ADR-019 §5
requires and the one `apps/procurement/posting.py` already documents. The
receipt is locked above the mapping lock rather than below it because the
return-quantity bound is read from it, and two returns racing for the same
remainder must contend there rather than both seeing it as available.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INVENTORY_CONTROL,
    SUPPLIER_RETURN_CLEARING,
    Account,
    OrganizationAccountMapping,
    SourceEvent,
)
from apps.accounting.services import post_entry, resolve_period, reverse_entry
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.locks import lock_account_mappings_shared
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.accounts import resolve_inventory_account
from apps.inventory.ledger import MovementInput, link_journal_entry, post_stock_entry
from apps.inventory.models import InventoryAccountMapping, InventoryReasonCode, MovementType
from apps.inventory.operations import require_cost_center_where_the_account_demands_one
from apps.organizations.business_dates import resolve_business_day
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    SupplierReturn,
    SupplierReturnLine,
    SupplierReturnStatus,
)
from apps.procurement.services import next_document_number
from apps.users.models import User

ZERO = Decimal("0.000")

#: The canonical source type both the journal and the stock entry record.
SOURCE_DOCUMENT_TYPE = "PROCUREMENT_SUPPLIER_RETURN"

#: Stamped on the journal so an entry always says which rule produced it.
POSTING_RULE = "procurement-supplier-return-v1"

DOCUMENT_TYPE = "SUPPLIER_RETURN"


def effect_key(line: SupplierReturnLine) -> str:
    """The kernel's handle on one line's movement, stable across a retry."""
    return f"supplier-return-line:{line.line_uid}"


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def returned_quantity_for(line: GoodsReceiptLine) -> Decimal:
    """
    How much of one delivery line has been sent back and not un-sent.

    Derived, never stored — the same reasoning Task 2.11 records for matching
    availability. A cached column would have to be found and corrected the day
    somebody reversed a return inside a failed transaction, and this cannot be
    wrong because there is nothing to be wrong.
    """
    total = (
        SupplierReturnLine.objects.filter(goods_receipt_line=line)
        .exclude(supplier_return__status=SupplierReturnStatus.REVERSED)
        .aggregate(total=Sum("returned_base_quantity"))["total"]
    )
    return total or ZERO


def return_availability(line: GoodsReceiptLine) -> Decimal:
    """
    What is left of one delivery line to send back.

    The delivery's **accepted** quantity less what has already gone. Not its
    delivered quantity: a rejected carton never entered stock and cannot be
    returned from it.

    This exists because the kernel cannot supply it. `_require_available`
    refuses to drive a warehouse position negative, which is a different
    question — with stock on hand from three other deliveries, a line that
    accepted fifty kilograms could be returned for fifty twice and the kernel
    would allow both. The bound is per delivery line, and only procurement
    knows which delivery the goods came from.
    """
    return line.accepted_base_quantity - returned_quantity_for(line)


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def _require_draft(supplier_return: SupplierReturn) -> SupplierReturn:
    locked = SupplierReturn.objects.select_for_update().get(pk=supplier_return.pk)
    if locked.status != SupplierReturnStatus.DRAFT:
        raise ValidationError(
            _("This return has been posted and can no longer be edited."),
            code="return_not_editable",
        )
    return locked


@transaction.atomic
def create_supplier_return(
    *,
    receipt: GoodsReceipt,
    created_by: User,
    returned_at: datetime.date,
    business_date: datetime.date | None = None,
    reason_code: InventoryReasonCode | None = None,
    reason: str = "",
    evidence_reference: str = "",
    notes: str = "",
) -> SupplierReturn:
    """
    Open a draft return against one posted delivery.

    The delivery is the argument rather than the supplier: a return is always
    *of something that arrived*, and taking the supplier alone would allow a
    return that cites no delivery and therefore has no book value, no lot and
    no quantity bound.
    """
    locked_receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if locked_receipt.status != GoodsReceiptStatus.POSTED:
        raise ValidationError(
            _("Only a posted delivery has stock to send back."), code="receipt_not_posted"
        )

    supplier_return = SupplierReturn(
        organization=locked_receipt.organization,
        branch=locked_receipt.branch,
        supplier=locked_receipt.supplier,
        receipt=locked_receipt,
        warehouse=locked_receipt.warehouse,
        location=locked_receipt.location,
        returned_at=returned_at,
        business_date=business_date or returned_at,
        reason_code=reason_code,
        reason=reason.strip(),
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
        created_by=created_by,
    )
    supplier_return.full_clean()
    supplier_return.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=supplier_return,
        branch=supplier_return.branch,
        new_state=snapshot(supplier_return),
    )
    return supplier_return


@transaction.atomic
def add_return_line(
    *,
    supplier_return: SupplierReturn,
    receipt_line: GoodsReceiptLine,
    returned_base_quantity: Decimal,
    expected_credit_value: Decimal | None = None,
    note: str = "",
) -> SupplierReturnLine:
    """
    Send part or all of one delivery line back.

    The quantity is checked against what that line accepted less what has
    already been returned, under a lock on the receipt line — so two returns
    racing for the same remainder contend on the same row rather than both
    reading the pre-commit figure.
    """
    locked = _require_draft(supplier_return)
    quantity = quantize_quantity(returned_base_quantity)
    if quantity <= ZERO:
        raise ValidationError(
            _("A returned quantity must be greater than zero."), code="quantity_not_positive"
        )

    # `select_related` names only non-nullable relations: joining `lot` would
    # make this FOR UPDATE over the nullable side of an outer join, which
    # PostgreSQL refuses outright.
    locked_line = (
        GoodsReceiptLine.objects.select_for_update()
        .select_related("item", "receipt")
        .get(pk=receipt_line.pk)
    )
    if locked_line.receipt_id != locked.receipt_id:
        raise ValidationError(
            _("That delivery line belongs to a different receipt."), code="receipt_line_mismatch"
        )
    if locked_line.accepted_base_quantity <= ZERO:
        raise ValidationError(
            _("That delivery line was wholly rejected and brought nothing into stock."),
            code="receipt_line_rejected",
        )

    available = return_availability(locked_line)
    if quantity > available:
        raise ValidationError(
            _(
                "Delivery line %(item)s has %(available)s left to return; %(asked)s would "
                "exceed what was accepted."
            ),
            code="return_over_quantity",
            params={
                "item": locked_line.item.code,
                "available": format(available, "f"),
                "asked": format(quantity, "f"),
            },
        )

    credit = quantize_money(expected_credit_value) if expected_credit_value is not None else None
    line = SupplierReturnLine(
        supplier_return=locked,
        sequence=_next_sequence(locked),
        goods_receipt_line=locked_line,
        item=locked_line.item,
        lot=locked_line.lot,
        returned_base_quantity=quantity,
        expected_credit_value=credit,
        note=note.strip(),
    )
    line.full_clean()
    line.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=line,
        branch=locked.branch,
        new_state=snapshot(line),
    )
    return line


def _next_sequence(supplier_return: SupplierReturn) -> int:
    highest = (
        SupplierReturnLine.objects.filter(supplier_return=supplier_return)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    return (highest or 0) + 1


@transaction.atomic
def remove_return_line(*, line: SupplierReturnLine) -> None:
    """Take a line off a draft. A trigger refuses anything further."""
    locked = _require_draft(line.supplier_return)
    previous = snapshot(line)
    record_audit_event(
        action=AuditAction.DELETED,
        target=line,
        branch=locked.branch,
        previous_state=previous,
    )
    line.delete()


@transaction.atomic
def delete_supplier_return(*, supplier_return: SupplierReturn) -> None:
    """Discard a draft nobody posted."""
    locked = _require_draft(supplier_return)
    previous = snapshot(locked)
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
    )
    locked.delete()


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class _Plan:
    """The accounts and effects one posting resolved, before any of it exists."""

    effects: list[MovementInput] = field(default_factory=list)
    #: line pk -> (inventory control account, clearing account)
    accounts: dict[int, tuple[Account, Account]] = field(default_factory=dict)
    #: line pk -> (item mapping, organization mapping)
    mappings: dict[
        int, tuple[InventoryAccountMapping | None, OrganizationAccountMapping | None]
    ] = field(default_factory=dict)
    clearing: Account | None = None


def _plan(supplier_return: SupplierReturn, lines: list[SupplierReturnLine]) -> _Plan:
    """
    Resolve every account before a single effect exists.

    One missing mapping fails here — before a movement, a balance, a number or
    a journal line — so there is nothing partial to clean up (PRC-034,
    PRC-036). No value is computed: the kernel decides what an outbound is
    worth, and only it knows what a full depletion surrendered.
    """
    clearing = resolve_inventory_account(
        organization=supplier_return.organization,
        role=SUPPLIER_RETURN_CLEARING,
        item=None,
        on_date=supplier_return.business_date,
    )
    require_cost_center_where_the_account_demands_one(account=clearing.account, cost_center=None)

    plan = _Plan(clearing=clearing.account)
    for line in lines:
        control = resolve_inventory_account(
            organization=supplier_return.organization,
            role=INVENTORY_CONTROL,
            item=line.item,
            on_date=supplier_return.business_date,
        )
        require_cost_center_where_the_account_demands_one(account=control.account, cost_center=None)
        plan.accounts[line.pk] = (control.account, clearing.account)
        plan.mappings[line.pk] = (control.inventory_mapping, control.organization_mapping)
        plan.effects.append(
            MovementInput(
                warehouse=supplier_return.warehouse,
                item=line.item,
                movement_type=MovementType.RETURN_OUT,
                quantity=line.returned_base_quantity,
                effect_key=effect_key(line),
                lot=line.lot,
                control_account=control.account,
            )
        )
    return plan


def _journal_lines(
    lines: list[SupplierReturnLine], *, plan: _Plan, supplier_return: SupplierReturn
) -> list[PostingLine]:
    """
    One credit per distinct control account, one debit to the clearing account.

    The mirror of a receipt's shape, and grouped for the same reason:
    `INVENTORY_CONTROL` is item-overridable and the clearing role is not, so a
    return of five items mapping to three control accounts produces three
    credits and a single debit. Every figure is a sum of the stored 3-decimal
    line values the kernel wrote, and the total is never rounded on its own.
    """
    credit_by_account: dict[int, Decimal] = {}
    account_by_id: dict[int, Account] = {}

    for line in lines:
        control, _clearing = plan.accounts[line.pk]
        account_by_id[control.pk] = control
        value = line.posted_value or ZERO
        credit_by_account[control.pk] = credit_by_account.get(control.pk, ZERO) + value

    assert plan.clearing is not None  # noqa: S101 - resolved in `_plan`
    total = quantize_money(sum(credit_by_account.values(), start=ZERO))
    posting_lines = [PostingLine(account=plan.clearing, branch=supplier_return.branch, debit=total)]
    posting_lines.extend(
        PostingLine(
            account=account_by_id[account_id],
            branch=supplier_return.branch,
            credit=amount,
        )
        for account_id, amount in sorted(
            credit_by_account.items(), key=lambda pair: account_by_id[pair[0]].code
        )
    )
    return posting_lines


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@transaction.atomic
def post_supplier_return(*, supplier_return: SupplierReturn, actor: User) -> SupplierReturn:
    """
    Send the goods back, atomically.

    One transaction produces the stock movements, the balances, the valuation,
    the gapless document number, the balanced journal, every line-level link
    between them, and the audit event — or none of it (PRC-036).

    The idempotency key is derived from the return's own `public_id` rather
    than accepted from the caller, for the reason `apps.procurement.posting`
    records: posting *this return* is the whole command, the return is the
    payload, and a posted return is frozen by a trigger — so a retry cannot
    present the same key with a different payload. The kernel still refuses a
    duplicate on the source identity independently.
    """
    # 1. The return row.
    locked = SupplierReturn.objects.select_for_update().get(pk=supplier_return.pk)
    if locked.status == SupplierReturnStatus.POSTED:
        raise ValidationError(_("This return is already posted."), code="already_posted")
    if locked.status != SupplierReturnStatus.DRAFT:
        raise ValidationError(
            _("A reversed return cannot be posted again. Record a new one."),
            code="return_not_draft",
        )
    if not locked.evidence_reference:
        raise ValidationError(
            _("Posting a return needs the evidence it rests on."), code="evidence_required"
        )

    lines = list(
        locked.lines.select_related(
            "item",
            "item__category",
            "item__category__parent",
            "item__category__parent__parent",
            "lot",
            "goods_receipt_line",
        ).order_by("sequence")
    )
    if not lines:
        raise ValidationError(_("An empty return cannot be posted."), code="no_lines")

    # 2. The source delivery and the lines this return cites, locked before the
    # bound is re-read: a draft can sit for a day, and another return may have
    # taken the remainder since.
    receipt = GoodsReceipt.objects.select_for_update().get(pk=locked.receipt_id)
    if receipt.status != GoodsReceiptStatus.POSTED:
        raise ValidationError(
            _("Delivery %(number)s is no longer posted."),
            code="receipt_not_posted",
            params={"number": receipt.number or str(receipt.public_id)},
        )
    receipt_line_ids = sorted({line.goods_receipt_line_id for line in lines})
    list(
        GoodsReceiptLine.objects.select_for_update().filter(pk__in=receipt_line_ids).order_by("pk")
    )
    _require_still_within_the_accepted_quantity(lines)

    day = resolve_business_day(locked.branch, timezone.now())
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = resolve_period(organization=locked.organization, accounting_date=locked.business_date)
    validate_period_accepts_postings(period)

    # 3. The organization's mappings, shared — above every row lock below it.
    lock_account_mappings_shared(locked.organization_id)

    # 4. This return's own lines.
    list(
        SupplierReturnLine.objects.select_for_update()
        .filter(supplier_return=locked)
        .order_by("sequence")
    )

    plan = _plan(locked, lines)

    # 5/6. Stock: the movements, the balances, the valuation, the location
    # release and the posted-order counter — the kernel's own, including the
    # negative-stock refusal (PRC-050) with no procurement bypass.
    stock_entry = post_stock_entry(
        organization=locked.organization,
        effects=plan.effects,
        idempotency_key=f"procurement-supplier-return:{locked.public_id}",
        business_date=locked.business_date,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        reference=locked.evidence_reference,
        reason=locked.reason or "supplier return",
    )
    movement_by_key = {movement.effect_key: movement for movement in stock_entry.movements.all()}

    # The value is learned here and nowhere else. An outbound leaves at the
    # standing average, and a movement that empties a position surrenders the
    # whole remaining book value — a figure only the kernel can produce.
    for line in lines:
        movement = movement_by_key[effect_key(line)]
        line.posted_value = abs(movement.inventory_value)

    # 7. The gapless number, drawn only now that nothing can fail for a domain
    # reason — an abandoned attempt must not burn one.
    locked.number = next_document_number(
        organization=locked.organization,
        document_type=DOCUMENT_TYPE,
        year=period.fiscal_year.year,
    )

    # 8. The journal.
    journal = post_entry(
        organization=locked.organization,
        accounting_date=locked.business_date,
        lines=_journal_lines(lines, plan=plan, supplier_return=locked),
        idempotency_key=f"procurement-supplier-return-journal:{locked.public_id}",
        document_date=locked.returned_at,
        narration=locked.reason or str(_("إرجاع بضاعة إلى المورد")),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE,
    )
    # Naming the journal on the stock entry arms the kernel's conditional
    # control-account invariant over these movements.
    link_journal_entry(entry=stock_entry, journal=journal)

    for line in lines:
        control, clearing = plan.accounts[line.pk]
        line.movement = movement_by_key[effect_key(line)]
        line.inventory_account = control
        line.contra_account = clearing
        line.save(
            update_fields=[
                "movement",
                "posted_value",
                "inventory_account",
                "contra_account",
                "updated_at",
            ]
        )

    locked.posted_value = quantize_money(
        sum((line.posted_value or ZERO for line in lines), start=ZERO)
    )
    locked.stock_entry = stock_entry
    locked.journal_entry = journal
    locked.status = SupplierReturnStatus.POSTED
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
            "journal_entry": journal.entry_number,
            "line_count": len(lines),
            "posted_value": format(locked.posted_value, "f"),
        },
    )
    return locked


def _require_still_within_the_accepted_quantity(lines: list[SupplierReturnLine]) -> None:
    """
    Re-check the bound against the deliveries as they stand now.

    Not redundant with `add_return_line`: another return may have taken the
    remainder since, and the row locks that made each individual addition safe
    were released when that transaction committed.
    """
    by_receipt_line: dict[int, Decimal] = {}
    for line in lines:
        by_receipt_line[line.goods_receipt_line_id] = (
            by_receipt_line.get(line.goods_receipt_line_id, ZERO) + line.returned_base_quantity
        )
    for receipt_line_id, mine in by_receipt_line.items():
        receipt_line = GoodsReceiptLine.objects.select_related("item").get(pk=receipt_line_id)
        taken_elsewhere = (
            SupplierReturnLine.objects.filter(goods_receipt_line_id=receipt_line_id)
            .exclude(supplier_return__status=SupplierReturnStatus.REVERSED)
            .exclude(supplier_return=lines[0].supplier_return)
            .aggregate(total=Sum("returned_base_quantity"))["total"]
            or ZERO
        )
        if taken_elsewhere + mine > receipt_line.accepted_base_quantity:
            raise ValidationError(
                _(
                    "Delivery line %(item)s has been returned elsewhere since; this return "
                    "would send back more than arrived."
                ),
                code="return_over_quantity",
                params={"item": receipt_line.item.code},
            )


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_supplier_return(
    *, supplier_return: SupplierReturn, actor: User, reason: str
) -> SupplierReturn:
    """
    Take back a posted return — the whole document, never a line.

    The goods come back onto the shelf and the clearing balance goes back to
    where it was. The mirror is exact: the reversing journal mirrors the
    original lines whatever the mappings have since become, and the stock
    reversal restores the value the movements actually removed rather than
    today's average. Nothing is re-resolved, because "what did this return do"
    has one answer and it was settled the day it posted.

    Once reversed, the delivery is returnable again — `return_availability`
    counts only returns that still stand — and the receipt itself becomes
    reversible again, because `live_dependency` says a reversed return holds
    nothing.
    """
    locked = SupplierReturn.objects.select_for_update().get(pk=supplier_return.pk)
    if locked.status == SupplierReturnStatus.REVERSED:
        raise ValidationError(_("This return is already reversed."), code="already_reversed")
    if locked.status != SupplierReturnStatus.POSTED:
        raise ValidationError(_("Only a posted return can be reversed."), code="return_not_posted")
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")

    _require_no_downstream_dependency(locked)

    now = timezone.now()
    reversal_business_date = resolve_business_day(locked.branch, now).business_date
    assert locked.stock_entry is not None  # noqa: S101 - a constraint guarantees it
    assert locked.journal_entry is not None  # noqa: S101 - a constraint guarantees it

    from apps.inventory.ledger import reverse_stock_entry

    reversal_entry = reverse_stock_entry(
        entry=locked.stock_entry,
        idempotency_key=f"procurement-supplier-return-reverse:{locked.public_id}",
        business_date=reversal_business_date,
        reason=reason.strip(),
    )
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"procurement-supplier-return-journal-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )
    link_journal_entry(entry=reversal_entry, journal=reversal_journal)

    locked.status = SupplierReturnStatus.REVERSED
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


def _require_no_downstream_dependency(supplier_return: SupplierReturn) -> None:
    """
    Nothing later may already depend on this return.

    Task 2.14's credit note will cite one and take a value from it, and
    reversing underneath it would leave the note crediting a return that did
    not happen. That model does not exist yet, so this walks the relations that
    do — a loop over related accessors rather than a list of imports, so the
    credit note gets the guard by declaring the relation rather than by
    somebody remembering this function.

    Withdrawn dependents do not count, the same convention Tasks 2.9, 2.11 and
    2.12 use: a model says which of its rows still stand by declaring
    `live_dependency`, and without one every row counts, which is the safe
    default for a relation nobody has considered yet.
    """
    ignored = {"history", "lines"}
    for relation in supplier_return._meta.related_objects:
        name = relation.get_accessor_name()
        if not name or name in ignored:
            continue
        related = getattr(supplier_return, name, None)
        if related is None or not hasattr(related, "exists"):
            continue
        live = getattr(relation.related_model, "live_dependency", None)
        if live is not None:
            related = related.filter(live)
        if related.exists():
            raise ValidationError(
                _(
                    "Another document (%(relation)s) already depends on this return. "
                    "Reverse it first."
                ),
                code="return_has_dependents",
                params={"relation": name},
            )


def return_timeline(supplier_return: SupplierReturn) -> list[dict[str, Any]]:
    """The dated facts about one return, oldest first, for the detail screen."""
    events: list[dict[str, Any]] = [
        {
            "label": _("سُجّل"),
            "at": supplier_return.created_at,
            "who": supplier_return.created_by.username,
        }
    ]
    if supplier_return.posted_at is not None:
        events.append(
            {
                "label": _("رُحّل"),
                "at": supplier_return.posted_at,
                "who": supplier_return.posted_by.username if supplier_return.posted_by else "",
            }
        )
    if supplier_return.reversed_at is not None:
        events.append(
            {
                "label": _("عُكس"),
                "at": supplier_return.reversed_at,
                "who": supplier_return.reversed_by.username if supplier_return.reversed_by else "",
                "note": supplier_return.reversal_reason,
            }
        )
    return events


__all__ = [
    "DOCUMENT_TYPE",
    "POSTING_RULE",
    "SOURCE_DOCUMENT_TYPE",
    "add_return_line",
    "create_supplier_return",
    "delete_supplier_return",
    "effect_key",
    "post_supplier_return",
    "remove_return_line",
    "return_availability",
    "return_timeline",
    "returned_quantity_for",
    "reverse_supplier_return",
]
