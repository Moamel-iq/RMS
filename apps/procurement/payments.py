"""
Supplier payments — Task 2.15, the phase's last posting document.

    Dr  SUPPLIER_PAYABLE    the allocated amount
    Dr  SUPPLIER_ADVANCE    the unallocated remainder, where any
        Cr  cash or bank    the full amount

The source account is resolved by the payment's `method` through an
effective-dated role (PRC-056). Partial payment across several invoices is
normal (PRC-053); over-allocation is impossible on both sides (PRC-054),
checked against each invoice's **outstanding** — net of credit notes and
other payments, stricter than the stated "its total" and deliberately so —
under the invoice row locks at posting. The remainder is an asset, never a
negative payable (PRC-055).

Consuming a standing advance or a credit note's standing credit against a
later invoice has no approved journal shape anywhere and is **deferred, not
designed** — the discipline that scoped the credit note, kept.

## Locking order

    1. the payment row
    2. the mapping advisory lock, shared
    3. the allocations, then each allocated invoice row in pk order
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    SUPPLIER_ADVANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_PAYMENT_BANK,
    SUPPLIER_PAYMENT_CASH,
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
from apps.core.services import record_audit_event, snapshot
from apps.organizations.business_dates import resolve_business_day
from apps.procurement.cycles import close_cycle_if_settled, reopen_cycle
from apps.procurement.invoices import outstanding_amount
from apps.procurement.models import (
    PaymentAllocation,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceStatus,
    SupplierPayment,
    SupplierPaymentCycle,
    SupplierPaymentMethod,
    SupplierPaymentStatus,
)
from apps.procurement.services import next_document_number
from apps.users.models import User

ZERO = Decimal("0.000")

SOURCE_DOCUMENT_TYPE = "PROCUREMENT_SUPPLIER_PAYMENT"
POSTING_RULE = "procurement-payment-v1"
DOCUMENT_TYPE = "SUPPLIER_PAYMENT"

#: Which role answers for each method. The dict is the whole of the routing:
#: no account, id or code is ever named here (PRC-034, PRC-056).
METHOD_ROLES = {
    SupplierPaymentMethod.CASH: SUPPLIER_PAYMENT_CASH,
    SupplierPaymentMethod.BANK: SUPPLIER_PAYMENT_BANK,
}


# ---------------------------------------------------------------------------
# Derived figures
# ---------------------------------------------------------------------------


def paid_allocated_to(invoice: SupplierInvoice) -> Decimal:
    """
    What posted payments have already settled against one invoice.

    Only POSTED payments count; a draft's allocations are intent and a
    reversed payment gave its money back. Derived, never stored.
    """
    total = PaymentAllocation.objects.filter(
        invoice=invoice, payment__status=SupplierPaymentStatus.POSTED
    ).aggregate(total=Sum("allocated_amount"))["total"]
    return total or ZERO


def allocated_total(payment: SupplierPayment) -> Decimal:
    """The sum of one payment's allocation rows, whatever its status."""
    total = payment.allocations.aggregate(total=Sum("allocated_amount"))["total"]
    return total or ZERO


def advance_remainder(payment: SupplierPayment) -> Decimal:
    """
    What one posted payment did not settle against any invoice — the figure
    sitting in `SUPPLIER_ADVANCE` for it (PRC-055). Zero for a draft or a
    reversed payment.
    """
    if payment.status != SupplierPaymentStatus.POSTED:
        return ZERO
    return (payment.amount or ZERO) - allocated_total(payment)


def standing_advances(supplier: Supplier) -> Decimal:
    """Every posted payment's unallocated remainder for one supplier."""
    total = ZERO
    for payment in SupplierPayment.objects.filter(
        supplier=supplier, status=SupplierPaymentStatus.POSTED
    ):
        total += advance_remainder(payment)
    return total


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def _require_draft(payment: SupplierPayment) -> SupplierPayment:
    locked = SupplierPayment.objects.select_for_update().get(pk=payment.pk)
    if locked.status != SupplierPaymentStatus.DRAFT:
        raise ValidationError(
            _("This payment has been posted and can no longer be edited."),
            code="payment_not_editable",
        )
    return locked


@transaction.atomic
def create_supplier_payment(
    *,
    supplier: Supplier,
    branch: Any,
    created_by: User,
    paid_at: datetime.date,
    method: str,
    amount: Decimal,
    business_date: datetime.date | None = None,
    reference: str = "",
    notes: str = "",
) -> SupplierPayment:
    """Open a draft payment. Allocation is explicit and comes next (PRC-057)."""
    value = quantize_money(amount)
    if value <= ZERO:
        raise ValidationError(_("A payment must be greater than zero."), code="amount_not_positive")
    if method not in SupplierPaymentMethod.values:
        raise ValidationError(_("Unknown payment method."), code="unknown_method")
    if supplier.organization_id != branch.organization_id:
        raise ValidationError(
            _("That supplier belongs to a different organization."),
            code="supplier_organization_mismatch",
        )

    payment = SupplierPayment(
        organization=branch.organization,
        branch=branch,
        supplier=supplier,
        paid_at=paid_at,
        business_date=business_date or paid_at,
        method=method,
        amount=value,
        reference=reference.strip(),
        notes=notes.strip(),
        created_by=created_by,
    )
    payment.full_clean()
    payment.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=payment,
        branch=payment.branch,
        new_state=snapshot(payment),
    )
    return payment


@transaction.atomic
def add_payment_allocation(
    *,
    payment: SupplierPayment,
    invoice: SupplierInvoice,
    allocated_amount: Decimal,
    note: str = "",
) -> PaymentAllocation:
    """
    Point part of this payment at one posted invoice.

    Checked here so the person drafting sees the refusal, and re-checked at
    posting under the invoice row locks, because another payment or a credit
    note may have taken the invoice's remainder while this sat as a draft.
    """
    locked = _require_draft(payment)
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
            _("Only a posted invoice has an outstanding balance to pay."),
            code="invoice_not_posted",
        )

    remaining = outstanding_amount(locked_invoice)
    if value > remaining:
        raise ValidationError(
            _(
                "Invoice %(number)s has %(remaining)s outstanding; %(asked)s would pay "
                "more than it owes."
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
            _("The allocations would exceed the payment's own amount."),
            code="allocation_over_payment",
        )

    highest = (
        PaymentAllocation.objects.filter(payment=locked)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    allocation = PaymentAllocation(
        payment=locked,
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
def remove_payment_allocation(*, allocation: PaymentAllocation) -> None:
    """Take an allocation off a draft. A trigger refuses anything further."""
    locked = _require_draft(allocation.payment)
    previous = snapshot(allocation)
    record_audit_event(
        action=AuditAction.DELETED,
        target=allocation,
        branch=locked.branch,
        previous_state=previous,
    )
    allocation.delete()


@transaction.atomic
def delete_supplier_payment(*, payment: SupplierPayment) -> None:
    """Discard a draft nobody posted."""
    locked = _require_draft(payment)
    previous = snapshot(locked)
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
    )
    locked.delete()


# ---------------------------------------------------------------------------
# Drafting a whole settlement at once
# ---------------------------------------------------------------------------


@transaction.atomic
def draft_settlement(
    *,
    supplier: Supplier,
    branch: Any,
    created_by: User,
    paid_at: datetime.date,
    method: str,
    allocations: Sequence[tuple[SupplierInvoice, Decimal]],
    reference: str = "",
    notes: str = "",
) -> SupplierPayment:
    """
    Open one draft payment already pointed at the invoices a plan chose.

    The settlement screen works out *which* invoices a sum pays; this turns
    that answer into the ordinary document, through the ordinary services, so
    a settlement drafted from a plan and one keyed by hand are the same record
    with the same audit trail and the same posting path. There is no second
    way to pay a supplier.

    The payment's amount is the **sum of its allocations**, never a figure of
    its own. A plan is bounded by what the invoices actually owe, so a
    settlement built this way cannot exceed the open balance and cannot leave
    an accidental advance standing. Paying more than is owed stays possible,
    deliberately, and stays where it was: a payment keyed by hand, whose
    remainder the operator meant.

    Nothing is posted. The draft is confirmed on the payment screen by
    somebody holding `post_supplier_payment`, which is the maker-checker
    split this screen must not quietly collapse.
    """
    if not allocations:
        raise ValidationError(_("لا توجد فواتير في هذه الخطة."), code="settlement_plan_is_empty")

    total = quantize_money(sum((amount for _invoice, amount in allocations), start=ZERO))
    payment = create_supplier_payment(
        supplier=supplier,
        branch=branch,
        created_by=created_by,
        paid_at=paid_at,
        method=method,
        amount=total,
        reference=reference,
        notes=notes,
    )
    # Oldest first, and re-sorted here rather than trusted from the caller:
    # the allocation sequence is the order the payment reads in, and FIFO is
    # the claim this screen makes about itself.
    for invoice, amount in sorted(
        allocations, key=lambda row: (row[0].invoice_date, row[0].number, row[0].pk)
    ):
        add_payment_allocation(payment=payment, invoice=invoice, allocated_amount=amount)
    return payment


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@transaction.atomic
def post_supplier_payment(*, payment: SupplierPayment, actor: User) -> SupplierPayment:
    """
    Let the money go, atomically: number, balanced journal, audit — or none.
    """
    locked = SupplierPayment.objects.select_for_update().get(pk=payment.pk)
    if locked.status == SupplierPaymentStatus.POSTED:
        raise ValidationError(_("This payment is already posted."), code="already_posted")
    if locked.status != SupplierPaymentStatus.DRAFT:
        raise ValidationError(
            _("A reversed payment cannot be posted again. Record a new one."),
            code="payment_not_draft",
        )

    day = resolve_business_day(locked.branch, timezone.now())
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = resolve_period(organization=locked.organization, accounting_date=locked.business_date)
    validate_period_accepts_postings(period)

    lock_account_mappings_shared(locked.organization_id)

    allocations = list(
        locked.allocations.select_for_update().select_related("invoice").order_by("sequence")
    )
    allocated = quantize_money(sum((row.allocated_amount for row in allocations), start=ZERO))
    if allocated > (locked.amount or ZERO):
        raise ValidationError(
            _("The allocations exceed the payment's own amount."),
            code="allocation_over_payment",
        )
    invoice_ids = sorted({row.invoice_id for row in allocations})
    invoices = {
        invoice.pk: invoice
        for invoice in SupplierInvoice.objects.select_for_update()
        .filter(pk__in=invoice_ids)
        .order_by("pk")
    }
    for row in allocations:
        invoice = invoices[row.invoice_id]
        if invoice.status != SupplierInvoiceStatus.POSTED:
            raise ValidationError(
                _("Invoice %(number)s is no longer posted."),
                code="invoice_not_posted",
                params={"number": invoice.number},
            )
        remaining = outstanding_amount(invoice)
        if row.allocated_amount > remaining:
            raise ValidationError(
                _(
                    "Invoice %(number)s has %(remaining)s outstanding; the allocation of "
                    "%(asked)s no longer fits."
                ),
                code="allocation_over_invoice",
                params={
                    "number": invoice.number,
                    "remaining": format(remaining, "f"),
                    "asked": format(row.allocated_amount, "f"),
                },
            )

    payable = resolve_default_account(
        organization=locked.organization,
        account_role=SUPPLIER_PAYABLE,
        on_date=locked.business_date,
    ).account
    source = resolve_default_account(
        organization=locked.organization,
        account_role=METHOD_ROLES[SupplierPaymentMethod(locked.method)],
        on_date=locked.business_date,
    ).account
    remainder = quantize_money((locked.amount or ZERO) - allocated)
    advance = None
    if remainder > ZERO:
        # Resolved only when a line will exist: a fully allocated payment is
        # not required to map an account nothing posts to.
        advance = resolve_default_account(
            organization=locked.organization,
            account_role=SUPPLIER_ADVANCE,
            on_date=locked.business_date,
        ).account

    locked.number = next_document_number(
        organization=locked.organization,
        document_type=DOCUMENT_TYPE,
        year=period.fiscal_year.year,
    )

    lines: list[PostingLine] = []
    if allocated > ZERO:
        lines.append(PostingLine(account=payable, branch=locked.branch, debit=allocated))
    if remainder > ZERO:
        assert advance is not None  # noqa: S101 - resolved above
        lines.append(PostingLine(account=advance, branch=locked.branch, debit=remainder))
    lines.append(PostingLine(account=source, branch=locked.branch, credit=locked.amount))

    journal = post_entry(
        organization=locked.organization,
        accounting_date=locked.business_date,
        lines=lines,
        idempotency_key=f"procurement-supplier-payment:{locked.public_id}",
        document_date=locked.paid_at,
        narration=locked.notes or str(_("دفعة إلى المورد")),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE,
    )

    locked.journal_entry = journal
    locked.status = SupplierPaymentStatus.POSTED
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
            "amount": format(locked.amount, "f"),
            "allocated": format(allocated, "f"),
            "advance": format(remainder, "f"),
        },
    )

    # A cycle whose every invoice is now settled closes here rather than on a
    # schedule: the moment the last dinar lands is the moment the window is
    # over, and the next invoice should open a new one. Asked of every cycle
    # this payment touched, because one payment may finish more than one.
    for cycle in _cycles_touched(locked):
        close_cycle_if_settled(cycle)
    return locked


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_supplier_payment(
    *, payment: SupplierPayment, actor: User, reason: str
) -> SupplierPayment:
    """
    Take back a posted payment — the whole document, never an allocation.

    The mirror is exact and re-resolves nothing. The invoices it settled owe
    again, because `paid_allocated_to` counts only POSTED payments.
    """
    locked = SupplierPayment.objects.select_for_update().get(pk=payment.pk)
    if locked.status == SupplierPaymentStatus.REVERSED:
        raise ValidationError(_("This payment is already reversed."), code="already_reversed")
    if locked.status != SupplierPaymentStatus.POSTED:
        raise ValidationError(
            _("Only a posted payment can be reversed."), code="payment_not_posted"
        )
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")

    _require_no_downstream_dependency(locked)

    now = timezone.now()
    reversal_business_date = resolve_business_day(locked.branch, now).business_date
    assert locked.journal_entry is not None  # noqa: S101 - a constraint guarantees it

    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"procurement-supplier-payment-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )

    locked.status = SupplierPaymentStatus.REVERSED
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

    # The debt is real again, so the window it fell in has to be open again.
    # Refused where the supplier has since opened another: that is a genuine
    # conflict, and the operator has to decide it rather than have two open
    # windows appear silently.
    for cycle in _cycles_touched(locked):
        reopen_cycle(cycle)
    return locked


def _cycles_touched(payment: SupplierPayment) -> list[SupplierPaymentCycle]:
    """
    The distinct cycles this payment's allocations landed in.

    A payment settles the oldest debt first, and the oldest debt may sit in a
    window that expired months ago — so one payment can finish two cycles, and
    both have to be asked whether they are done.
    """
    seen: dict[int, SupplierPaymentCycle] = {}
    for allocation in payment.allocations.select_related("invoice__cycle"):
        cycle = allocation.invoice.cycle
        if cycle is not None:
            seen[cycle.pk] = cycle
    return list(seen.values())


def _require_no_downstream_dependency(payment: SupplierPayment) -> None:
    """The extension point every reversal guard here shares."""
    ignored = {"history", "allocations"}
    for relation in payment._meta.related_objects:
        name = relation.get_accessor_name()
        if not name or name in ignored:
            continue
        related = getattr(payment, name, None)
        if related is None or not hasattr(related, "exists"):
            continue
        live = getattr(relation.related_model, "live_dependency", None)
        if live is not None:
            related = related.filter(live)
        if related.exists():
            raise ValidationError(
                _(
                    "Another document (%(relation)s) already depends on this payment. "
                    "Reverse it first."
                ),
                code="payment_has_dependents",
                params={"relation": name},
            )


def payment_timeline(payment: SupplierPayment) -> list[dict[str, Any]]:
    """The dated facts about one payment, oldest first, for the detail screen."""
    events: list[dict[str, Any]] = [
        {"label": _("سُجّلت"), "at": payment.created_at, "who": payment.created_by.username}
    ]
    if payment.posted_at is not None:
        events.append(
            {
                "label": _("رُحّلت"),
                "at": payment.posted_at,
                "who": payment.posted_by.username if payment.posted_by else "",
            }
        )
    if payment.reversed_at is not None:
        events.append(
            {
                "label": _("عُكست"),
                "at": payment.reversed_at,
                "who": payment.reversed_by.username if payment.reversed_by else "",
                "note": payment.reversal_reason,
            }
        )
    return events


__all__ = [
    "DOCUMENT_TYPE",
    "METHOD_ROLES",
    "POSTING_RULE",
    "SOURCE_DOCUMENT_TYPE",
    "add_payment_allocation",
    "advance_remainder",
    "allocated_total",
    "create_supplier_payment",
    "delete_supplier_payment",
    "draft_settlement",
    "paid_allocated_to",
    "payment_timeline",
    "post_supplier_payment",
    "remove_payment_allocation",
    "reverse_supplier_payment",
    "standing_advances",
]
