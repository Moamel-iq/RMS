"""
Supplier credit notes — Task 2.14.

The supplier's agreed figure for a return. Task 2.13 parked the book value of
returned goods in `SUPPLIER_RETURN_CLEARING` and deliberately recognised no
variance, because at the gate nobody knew what the supplier would credit. This
document is the answer arriving on paper — **possibly a partial one**: a note
allocates explicitly to return lines, may settle part of a line, and a line
may be settled by several notes across time. Posting writes the entry ADR-022
§2 (as amended) deferred to here:

    Dr  SUPPLIER_PAYABLE             the note's whole agreed credit
        Cr  SUPPLIER_RETURN_CLEARING   the settled book value — that much claim closed
        Cr/Dr PURCHASE_RETURN_VARIANCE the difference, either direction

with the variance line absent when the figures agree. It never moves stock:
the goods left with the return (PRC-051's last clause, asserted by tests).

## The settlement arithmetic

Per return line, the standing settlements and the open claim always sum to
the line's posted book value, to the fils. A partial allocation settles the
quantized proportional share of the line's **remaining** clearing value —

    settled = quantize_money(remaining_book × credited_qty / remaining_qty)

— and an allocation that takes the last of the quantity takes the exact
remaining value, so no rounding residual can strand in the clearing account.
Both `remaining` figures are net of **posted** settlements: a draft has
settled nothing and enters no formula, though its quantity does reserve the
bound the way a draft return reserves availability.

## Locking order

    1. the credit note row
    2. the return it answers
    3. the mapping advisory lock, shared
    4. the cited return lines, in pk order — never caller order
    5. the standing allocations against those lines
    6. the allocated invoices, in pk order

The return lines are the contention point for two notes racing one
remainder: the loser waits there, recomputes under the lock, and is refused
on the bound rather than double-settling the claim.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    PURCHASE_RETURN_VARIANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_RETURN_CLEARING,
    SourceEvent,
)
from apps.accounting.services import (
    post_entry,
    resolve_default_account,
    resolve_period,
    reverse_entry,
)
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.locks import lock_account_mappings_shared
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.organizations.business_dates import resolve_business_day
from apps.procurement.invoices import normalize_invoice_number, outstanding_amount
from apps.procurement.models import (
    SupplierCreditAllocation,
    SupplierCreditNote,
    SupplierCreditNoteStatus,
    SupplierCreditReturnAllocation,
    SupplierInvoice,
    SupplierInvoiceStatus,
    SupplierReturn,
    SupplierReturnLine,
    SupplierReturnStatus,
)
from apps.procurement.services import next_document_number
from apps.users.models import User

ZERO = Decimal("0.000")

#: The canonical source type the journal records (Task 2.0 §15).
SOURCE_DOCUMENT_TYPE = "PROCUREMENT_SUPPLIER_CREDIT_NOTE"

#: Stamped on the journal so an entry always says which rule produced it.
#: Shorter than the source type on purpose: `posting_rule_version` is 32
#: characters wide and the rule name only has to be unique, not descriptive.
POSTING_RULE = "procurement-credit-note-v1"

DOCUMENT_TYPE = "SUPPLIER_CREDIT_NOTE"


# ---------------------------------------------------------------------------
# Derived figures
# ---------------------------------------------------------------------------


def credit_allocated_to(invoice: SupplierInvoice) -> Decimal:
    """
    What posted credit notes have already netted against one invoice.

    Only POSTED notes count: a draft's allocations are intent, and a reversed
    note gave its credit back. Derived every time, never stored — the Task
    2.11 reasoning, again.
    """
    total = SupplierCreditAllocation.objects.filter(
        invoice=invoice, credit_note__status=SupplierCreditNoteStatus.POSTED
    ).aggregate(total=Sum("allocated_amount"))["total"]
    return total or ZERO


def allocated_total(credit_note: SupplierCreditNote) -> Decimal:
    """The sum of one note's allocation rows, whatever its status."""
    total = credit_note.allocations.aggregate(total=Sum("allocated_amount"))["total"]
    return total or ZERO


def unallocated_credit(credit_note: SupplierCreditNote) -> Decimal:
    """
    What one posted note has not netted against any invoice.

    This is PRC-051's "standing supplier credit": a debit the payable account
    carries until an invoice or a payment run consumes it. Zero for a draft
    or a reversed note, because neither has put anything in the ledger.
    """
    if credit_note.status != SupplierCreditNoteStatus.POSTED:
        return ZERO
    return (credit_note.amount or ZERO) - allocated_total(credit_note)


def credited_quantity_for(line: SupplierReturnLine) -> Decimal:
    """
    How much of one return line standing notes have credited — draft and
    posted both, because a draft reserves the remainder the way a draft
    return reserves availability. Derived, never stored.
    """
    total = SupplierCreditReturnAllocation.objects.filter(
        supplier_return_line=line,
        credit_note__status__in=(
            SupplierCreditNoteStatus.DRAFT,
            SupplierCreditNoteStatus.POSTED,
        ),
    ).aggregate(total=Sum("credited_base_quantity"))["total"]
    return total or ZERO


def settled_book_value_for(line: SupplierReturnLine) -> Decimal:
    """
    How much of one return line's book value posted notes have closed.

    Posted only: a draft has settled nothing, whatever quantity it reserves.
    """
    total = SupplierCreditReturnAllocation.objects.filter(
        supplier_return_line=line,
        credit_note__status=SupplierCreditNoteStatus.POSTED,
    ).aggregate(total=Sum("settled_book_value"))["total"]
    return total or ZERO


def remaining_credit_quantity(line: SupplierReturnLine) -> Decimal:
    """What is left of one return line for a note to credit."""
    return line.returned_base_quantity - credited_quantity_for(line)


def remaining_book_value(line: SupplierReturnLine) -> Decimal:
    """What is left of one return line's claim in the clearing account."""
    return (line.posted_value or ZERO) - settled_book_value_for(line)


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def _require_draft(credit_note: SupplierCreditNote) -> SupplierCreditNote:
    locked = SupplierCreditNote.objects.select_for_update().get(pk=credit_note.pk)
    if locked.status != SupplierCreditNoteStatus.DRAFT:
        raise ValidationError(
            _("This credit note has been posted and can no longer be edited."),
            code="credit_note_not_editable",
        )
    return locked


@transaction.atomic
def create_supplier_credit_note(
    *,
    supplier_return: SupplierReturn,
    created_by: User,
    supplier_document_number: str,
    credit_date: datetime.date,
    amount: Decimal,
    business_date: datetime.date | None = None,
    reason: str = "",
    notes: str = "",
) -> SupplierCreditNote:
    """
    Open a draft note against one posted return.

    The return is the argument, not the supplier: a Release 1 credit note is
    always the answer to a claim, and the claim is the return's clearing
    balance. The supplier, branch and organization all follow from it. A note
    citing only an invoice or nothing at all has no approved contra account
    anywhere and is refused by this signature rather than by a runtime check.
    """
    locked_return = SupplierReturn.objects.select_for_update().get(pk=supplier_return.pk)
    if locked_return.status != SupplierReturnStatus.POSTED:
        raise ValidationError(
            _("Only a posted return has a claim to settle."), code="return_not_posted"
        )

    reference = supplier_document_number.strip()
    if not reference:
        raise ValidationError(
            _("The supplier's document number is required."), code="document_number_required"
        )
    value = quantize_money(amount)
    if value <= ZERO:
        raise ValidationError(_("A credit must be greater than zero."), code="amount_not_positive")

    credit_note = SupplierCreditNote(
        organization=locked_return.organization,
        branch=locked_return.branch,
        supplier=locked_return.supplier,
        supplier_return=locked_return,
        supplier_document_number=reference,
        supplier_document_number_key=normalize_invoice_number(reference),
        credit_date=credit_date,
        business_date=business_date or credit_date,
        amount=value,
        reason=reason.strip(),
        notes=notes.strip(),
        created_by=created_by,
    )
    credit_note.full_clean(exclude=["supplier_document_number_key"])
    credit_note.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=credit_note,
        branch=credit_note.branch,
        new_state=snapshot(credit_note),
    )
    return credit_note


@transaction.atomic
def add_return_allocation(
    *,
    credit_note: SupplierCreditNote,
    return_line: SupplierReturnLine,
    credited_base_quantity: Decimal,
    allocated_credit_amount: Decimal,
    note: str = "",
) -> SupplierCreditReturnAllocation:
    """
    Say which slice of the claim this note settles, line by line.

    The quantity is checked against the line's remainder under a lock on the
    return line, so two drafts racing for it contend on the same row. The
    settled book value is deliberately **not** written here — it is computed
    at posting, under locks, from the remainder as it stands then.
    """
    locked = _require_draft(credit_note)
    quantity = quantize_quantity(credited_base_quantity)
    credit = quantize_money(allocated_credit_amount)
    if quantity <= ZERO:
        raise ValidationError(
            _("A credited quantity must be greater than zero."), code="quantity_not_positive"
        )
    if credit <= ZERO:
        raise ValidationError(
            _("An allocated credit must be greater than zero."), code="credit_not_positive"
        )

    locked_line = (
        SupplierReturnLine.objects.select_for_update().select_related("item").get(pk=return_line.pk)
    )
    if locked_line.supplier_return_id != locked.supplier_return_id:
        raise ValidationError(
            _("That line belongs to a different return."), code="return_line_mismatch"
        )

    remaining = remaining_credit_quantity(locked_line)
    if quantity > remaining:
        raise ValidationError(
            _(
                "Return line %(item)s has %(remaining)s left to credit; %(asked)s would "
                "credit more than was returned."
            ),
            code="credit_over_quantity",
            params={
                "item": locked_line.item.code,
                "remaining": format(remaining, "f"),
                "asked": format(quantity, "f"),
            },
        )
    attributed = (
        locked.return_allocations.aggregate(total=Sum("allocated_credit_amount"))["total"] or ZERO
    )
    if attributed + credit > (locked.amount or ZERO):
        raise ValidationError(
            _("The attributed credit would exceed the note's own amount."),
            code="credit_over_note",
        )

    highest = (
        SupplierCreditReturnAllocation.objects.filter(credit_note=locked)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    allocation = SupplierCreditReturnAllocation(
        credit_note=locked,
        sequence=(highest or 0) + 1,
        supplier_return_line=locked_line,
        credited_base_quantity=quantity,
        allocated_credit_amount=credit,
        note=note.strip(),
    )
    allocation.full_clean()
    allocation.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=allocation,
        branch=locked.branch,
        new_state=snapshot(allocation),
    )
    return allocation


@transaction.atomic
def remove_return_allocation(*, allocation: SupplierCreditReturnAllocation) -> None:
    """Take a settlement slice off a draft. A trigger refuses anything further."""
    locked = _require_draft(allocation.credit_note)
    previous = snapshot(allocation)
    record_audit_event(
        action=AuditAction.DELETED,
        target=allocation,
        branch=locked.branch,
        previous_state=previous,
    )
    allocation.delete()


@transaction.atomic
def add_credit_allocation(
    *,
    credit_note: SupplierCreditNote,
    invoice: SupplierInvoice,
    allocated_amount: Decimal,
    note: str = "",
) -> SupplierCreditAllocation:
    """
    Net part of this note against one posted invoice.

    Checked twice, deliberately: here, so the person drafting sees the refusal
    while the document is in front of them, and again at posting under the
    invoice row locks, because another note may have taken the invoice's
    remainder while this one sat as a draft.
    """
    locked = _require_draft(credit_note)
    value = quantize_money(allocated_amount)
    if value <= ZERO:
        raise ValidationError(
            _("An allocation must be greater than zero."), code="allocation_not_positive"
        )

    locked_invoice = SupplierInvoice.objects.select_for_update().get(pk=invoice.pk)
    if locked_invoice.supplier_id != locked.supplier_id:
        raise ValidationError(
            _("That invoice belongs to a different supplier."), code="invoice_supplier_mismatch"
        )
    if locked_invoice.status != SupplierInvoiceStatus.POSTED:
        raise ValidationError(
            _("Only a posted invoice has an outstanding balance to reduce."),
            code="invoice_not_posted",
        )

    remaining = outstanding_amount(locked_invoice) - credit_allocated_to(locked_invoice)
    if value > remaining:
        raise ValidationError(
            _(
                "Invoice %(number)s has %(remaining)s outstanding; %(asked)s would "
                "credit more than it owes."
            ),
            code="allocation_over_invoice",
            params={
                "number": locked_invoice.number,
                "remaining": format(remaining, "f"),
                "asked": format(value, "f"),
            },
        )
    if allocated_total(locked) + value > (locked.amount or ZERO):
        raise ValidationError(
            _("The allocations would exceed the note's own amount."),
            code="allocation_over_note",
        )

    highest = (
        SupplierCreditAllocation.objects.filter(credit_note=locked)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    allocation = SupplierCreditAllocation(
        credit_note=locked,
        sequence=(highest or 0) + 1,
        invoice=locked_invoice,
        allocated_amount=value,
        note=note.strip(),
    )
    allocation.full_clean()
    allocation.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=allocation,
        branch=locked.branch,
        new_state=snapshot(allocation),
    )
    return allocation


@transaction.atomic
def remove_credit_allocation(*, allocation: SupplierCreditAllocation) -> None:
    """Take an allocation off a draft. A trigger refuses anything further."""
    locked = _require_draft(allocation.credit_note)
    previous = snapshot(allocation)
    record_audit_event(
        action=AuditAction.DELETED,
        target=allocation,
        branch=locked.branch,
        previous_state=previous,
    )
    allocation.delete()


@transaction.atomic
def delete_supplier_credit_note(*, credit_note: SupplierCreditNote) -> None:
    """Discard a draft nobody posted."""
    locked = _require_draft(credit_note)
    previous = snapshot(locked)
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
    )
    locked.delete()


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@transaction.atomic
def post_supplier_credit_note(
    *, credit_note: SupplierCreditNote, actor: User
) -> SupplierCreditNote:
    """
    Recognise the supplier's answer, atomically.

    One transaction produces the gapless number, the balanced journal, and the
    audit event — or none of it. No stock moves and no stock is read: the
    figure the claim closes at is the return's stored `posted_value`, written
    the day the goods left and never re-derived from an average that has
    moved since.
    """
    # 1. The note row.
    locked = SupplierCreditNote.objects.select_for_update().get(pk=credit_note.pk)
    if locked.status == SupplierCreditNoteStatus.POSTED:
        raise ValidationError(_("This credit note is already posted."), code="already_posted")
    if locked.status != SupplierCreditNoteStatus.DRAFT:
        raise ValidationError(
            _("A reversed credit note cannot be posted again. Record a new one."),
            code="credit_note_not_draft",
        )

    # 2. The return this note answers.
    settled_return = SupplierReturn.objects.select_for_update().get(pk=locked.supplier_return_id)
    if settled_return.status != SupplierReturnStatus.POSTED:
        raise ValidationError(
            _("Return %(number)s is no longer posted."),
            code="return_not_posted",
            params={"number": settled_return.number},
        )

    day = resolve_business_day(locked.branch, timezone.now())
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = resolve_period(organization=locked.organization, accounting_date=locked.business_date)
    validate_period_accepts_postings(period)

    # 3. The organization's mappings, shared — above every row lock below.
    lock_account_mappings_shared(locked.organization_id)

    # 4. The settlement, computed under locks. The cited return lines are the
    # contention point for two notes racing one remainder, locked in pk order
    # — never caller order — with the standing allocations against them.
    return_allocations = list(locked.return_allocations.select_for_update().order_by("sequence"))
    if not return_allocations:
        raise ValidationError(
            _("A credit note settles something: allocate it to the return's lines."),
            code="no_return_allocations",
        )
    attributed = quantize_money(
        sum((row.allocated_credit_amount for row in return_allocations), start=ZERO)
    )
    if attributed != (locked.amount or ZERO):
        raise ValidationError(
            _(
                "The note credits %(amount)s but its allocations attribute %(attributed)s. "
                "Every fils of the credit names the line it answers."
            ),
            code="credit_not_fully_attributed",
            params={
                "amount": format(locked.amount, "f"),
                "attributed": format(attributed, "f"),
            },
        )
    line_ids = sorted({row.supplier_return_line_id for row in return_allocations})
    lines_by_id = {
        line.pk: line
        for line in SupplierReturnLine.objects.select_for_update()
        .select_related("item")
        .filter(pk__in=line_ids)
        .order_by("pk")
    }
    list(
        SupplierCreditReturnAllocation.objects.select_for_update()
        .filter(
            supplier_return_line_id__in=line_ids,
            credit_note__status__in=(
                SupplierCreditNoteStatus.DRAFT,
                SupplierCreditNoteStatus.POSTED,
            ),
        )
        .order_by("pk")
    )

    settled_total = ZERO
    for row in return_allocations:
        line = lines_by_id[row.supplier_return_line_id]
        # The formula's remainders are net of POSTED settlements only — a
        # draft has settled nothing — while the *bound* counts drafts too,
        # excluding this note's own reservation.
        posted_credited = (
            SupplierCreditReturnAllocation.objects.filter(
                supplier_return_line=line,
                credit_note__status=SupplierCreditNoteStatus.POSTED,
            ).aggregate(total=Sum("credited_base_quantity"))["total"]
            or ZERO
        )
        remaining_quantity = line.returned_base_quantity - posted_credited
        if row.credited_base_quantity > remaining_quantity:
            raise ValidationError(
                _(
                    "Return line %(item)s has been credited elsewhere since; "
                    "%(asked)s exceeds its remaining %(remaining)s."
                ),
                code="credit_over_quantity",
                params={
                    "item": line.item.code,
                    "asked": format(row.credited_base_quantity, "f"),
                    "remaining": format(remaining_quantity, "f"),
                },
            )
        remaining_value = (line.posted_value or ZERO) - (
            SupplierCreditReturnAllocation.objects.filter(
                supplier_return_line=line,
                credit_note__status=SupplierCreditNoteStatus.POSTED,
            ).aggregate(total=Sum("settled_book_value"))["total"]
            or ZERO
        )
        if row.credited_base_quantity == remaining_quantity:
            # The final slice takes the exact remainder, so no rounding
            # residual can strand in the clearing account.
            settled = remaining_value
        else:
            settled = quantize_money(
                remaining_value * row.credited_base_quantity / remaining_quantity
            )
        row.settled_book_value = settled
        row.save(update_fields=["settled_book_value", "updated_at"])
        settled_total += settled
    settled_total = quantize_money(settled_total)

    # 5. The invoice allocations, re-checked under each invoice's row lock.
    allocations = list(
        locked.allocations.select_for_update().select_related("invoice").order_by("sequence")
    )
    invoice_ids = sorted({share.invoice_id for share in allocations})
    invoices = {
        invoice.pk: invoice
        for invoice in SupplierInvoice.objects.select_for_update()
        .filter(pk__in=invoice_ids)
        .order_by("pk")
    }
    if quantize_money(sum((share.allocated_amount for share in allocations), start=ZERO)) > (
        locked.amount or ZERO
    ):
        raise ValidationError(
            _("The allocations exceed the note's own amount."), code="allocation_over_note"
        )
    for share in allocations:
        invoice = invoices[share.invoice_id]
        if invoice.status != SupplierInvoiceStatus.POSTED:
            raise ValidationError(
                _("Invoice %(number)s is no longer posted."),
                code="invoice_not_posted",
                params={"number": invoice.number},
            )
        remaining = outstanding_amount(invoice) - credit_allocated_to(invoice)
        if share.allocated_amount > remaining:
            raise ValidationError(
                _(
                    "Invoice %(number)s has %(remaining)s outstanding; the allocation of "
                    "%(asked)s no longer fits."
                ),
                code="allocation_over_invoice",
                params={
                    "number": invoice.number,
                    "remaining": format(remaining, "f"),
                    "asked": format(share.allocated_amount, "f"),
                },
            )

    # 6. The accounts. The variance role is resolved only when a line will
    # exist: an organization whose figures agree is not required to map an
    # account nothing posts to.
    payable = resolve_default_account(
        organization=locked.organization,
        account_role=SUPPLIER_PAYABLE,
        on_date=locked.business_date,
    ).account
    clearing = resolve_default_account(
        organization=locked.organization,
        account_role=SUPPLIER_RETURN_CLEARING,
        on_date=locked.business_date,
    ).account
    difference = quantize_money((locked.amount or ZERO) - settled_total)
    variance = None
    if difference != ZERO:
        variance = resolve_default_account(
            organization=locked.organization,
            account_role=PURCHASE_RETURN_VARIANCE,
            on_date=locked.business_date,
        ).account

    # 7. The gapless number, drawn only now that nothing can fail for a
    # domain reason.
    locked.number = next_document_number(
        organization=locked.organization,
        document_type=DOCUMENT_TYPE,
        year=period.fiscal_year.year,
    )

    # 8. The journal. Debit-first, then credits in account-code order — the
    # same presentation every posting here uses. The clearing credit is the
    # sum of the settled book values, so the account holds exactly the claims
    # still open.
    lines = [PostingLine(account=payable, branch=locked.branch, debit=locked.amount)]
    if difference > ZERO:
        assert variance is not None  # noqa: S101 - resolved above
        credit_lines = [
            (clearing, settled_total),
            (variance, difference),
        ]
    elif difference < ZERO:
        assert variance is not None  # noqa: S101 - resolved above
        lines.append(PostingLine(account=variance, branch=locked.branch, debit=-difference))
        credit_lines = [(clearing, settled_total)]
    else:
        credit_lines = [(clearing, settled_total)]
    lines.extend(
        PostingLine(account=account, branch=locked.branch, credit=amount)
        for account, amount in sorted(credit_lines, key=lambda pair: pair[0].code)
    )

    journal = post_entry(
        organization=locked.organization,
        accounting_date=locked.business_date,
        lines=lines,
        idempotency_key=f"procurement-supplier-credit-note:{locked.public_id}",
        document_date=locked.credit_date,
        narration=locked.reason or str(_("إشعار دائن من المورد")),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE,
    )

    locked.journal_entry = journal
    locked.status = SupplierCreditNoteStatus.POSTED
    locked.posted_by = actor
    locked.posted_at = timezone.now()
    locked.save(
        update_fields=[
            "business_date_timezone",
            "business_day_start",
            "number",
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
            "return": settled_return.number,
            "amount": format(locked.amount, "f"),
            "settled_book_value": format(settled_total, "f"),
            "variance": format(difference, "f"),
        },
    )
    return locked


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_supplier_credit_note(
    *, credit_note: SupplierCreditNote, actor: User, reason: str
) -> SupplierCreditNote:
    """
    Take back a posted note — the whole document, never an allocation.

    The mirror is exact: the reversing journal restores the payable, reopens
    the clearing claim and takes back the variance, whatever the mappings have
    since become. The return becomes creditable again, because the standing-
    note index excludes reversed rows.
    """
    locked = SupplierCreditNote.objects.select_for_update().get(pk=credit_note.pk)
    if locked.status == SupplierCreditNoteStatus.REVERSED:
        raise ValidationError(_("This credit note is already reversed."), code="already_reversed")
    if locked.status != SupplierCreditNoteStatus.POSTED:
        raise ValidationError(
            _("Only a posted credit note can be reversed."), code="credit_note_not_posted"
        )
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")

    _require_no_downstream_dependency(locked)

    now = timezone.now()
    reversal_business_date = resolve_business_day(locked.branch, now).business_date
    assert locked.journal_entry is not None  # noqa: S101 - a constraint guarantees it

    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"procurement-supplier-credit-note-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )

    locked.status = SupplierCreditNoteStatus.REVERSED
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


def _require_no_downstream_dependency(credit_note: SupplierCreditNote) -> None:
    """
    Nothing later may already depend on this note.

    Task 2.15's payment run will read standing credit, and reversing a note a
    settlement already consumed would leave money accounted for twice. That
    model does not exist yet; the loop over related accessors is the extension
    point holding the line until it does — the same convention every reversal
    guard here uses, `live_dependency` included.
    """
    ignored = {"history", "allocations", "return_allocations"}
    for relation in credit_note._meta.related_objects:
        name = relation.get_accessor_name()
        if not name or name in ignored:
            continue
        related = getattr(credit_note, name, None)
        if related is None or not hasattr(related, "exists"):
            continue
        live = getattr(relation.related_model, "live_dependency", None)
        if live is not None:
            related = related.filter(live)
        if related.exists():
            raise ValidationError(
                _(
                    "Another document (%(relation)s) already depends on this credit note. "
                    "Reverse it first."
                ),
                code="credit_note_has_dependents",
                params={"relation": name},
            )


def note_timeline(credit_note: SupplierCreditNote) -> list[dict[str, Any]]:
    """The dated facts about one note, oldest first, for the detail screen."""
    events: list[dict[str, Any]] = [
        {
            "label": _("سُجّل"),
            "at": credit_note.created_at,
            "who": credit_note.created_by.username,
        }
    ]
    if credit_note.posted_at is not None:
        events.append(
            {
                "label": _("رُحّل"),
                "at": credit_note.posted_at,
                "who": credit_note.posted_by.username if credit_note.posted_by else "",
            }
        )
    if credit_note.reversed_at is not None:
        events.append(
            {
                "label": _("عُكس"),
                "at": credit_note.reversed_at,
                "who": credit_note.reversed_by.username if credit_note.reversed_by else "",
                "note": credit_note.reversal_reason,
            }
        )
    return events


__all__ = [
    "DOCUMENT_TYPE",
    "POSTING_RULE",
    "SOURCE_DOCUMENT_TYPE",
    "add_credit_allocation",
    "add_return_allocation",
    "allocated_total",
    "create_supplier_credit_note",
    "credit_allocated_to",
    "credited_quantity_for",
    "delete_supplier_credit_note",
    "note_timeline",
    "post_supplier_credit_note",
    "remaining_book_value",
    "remaining_credit_quantity",
    "remove_credit_allocation",
    "remove_return_allocation",
    "reverse_supplier_credit_note",
    "settled_book_value_for",
    "unallocated_credit",
]
