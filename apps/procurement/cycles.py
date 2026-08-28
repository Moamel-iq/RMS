"""
Supplier payment cycles: which window an invoice falls into, and when it closes.

The business buys from a supplier continuously and settles periodically. A due
date computed per invoice therefore described nobody's arrangement — "thirty
days" is thirty days from the *first* invoice of the run, and everything raised
before that day falls due with it.

## The three rules, and what each refuses

**One *collecting* cycle per supplier.** Enforced by a partial unique index,
not by this module remembering to check: two would be two answers to "which
window does this invoice join". Several may stand `DUE` at once, and a supplier
whose account has run late for months will have one per month.

**A cycle stops collecting at its due date.** An invoice dated after it moves
the window to `DUE` and opens a new one. Letting it join would make it overdue
in the moment it was keyed, and the invoice's own `due_date >= invoice_date`
constraint would refuse the row anyway.

**A cycle closes when it is paid off, never when it expires.** Passing the due
date makes it `DUE`, which is precisely what the reports exist to show; closing
it then would hide the debt somebody needs to see.

## What is snapshotted, and why

The cycle copies the terms and the settlement floor that applied when it
opened, and the invoice keeps its own copy besides. Renegotiating a supplier's
terms in March moves neither. A cycle that could be restated would make every
report of what was late disagree with what was late — and the aging report is
read by the person deciding whom to pay.
"""

from __future__ import annotations

import datetime

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.procurement.models import (
    Supplier,
    SupplierInvoice,
    SupplierInvoiceStatus,
    SupplierPaymentCycle,
    SupplierPaymentCycleStatus,
)


def due_date_for_cycle(*, opened_on: datetime.date, payment_terms_days: int) -> datetime.date:
    """The day a cycle opened on that date, under those terms, falls due."""
    return opened_on + datetime.timedelta(days=payment_terms_days)


def collecting_cycle(supplier: Supplier) -> SupplierPaymentCycle | None:
    """The window new invoices currently join, or None where there is not one."""
    return SupplierPaymentCycle.objects.filter(
        supplier=supplier, status=SupplierPaymentCycleStatus.COLLECTING
    ).first()


def unsettled_cycles(supplier: Supplier) -> list[SupplierPaymentCycle]:
    """Every window still owing something, oldest first."""
    return list(
        SupplierPaymentCycle.objects.filter(supplier=supplier)
        .exclude(status=SupplierPaymentCycleStatus.SETTLED)
        .order_by("sequence")
    )


@transaction.atomic
def cycle_for_invoice(
    *, supplier: Supplier, invoice_date: datetime.date, payment_terms_days: int
) -> SupplierPaymentCycle:
    """
    The cycle this invoice belongs to, opening one where none will take it.

    Locked before it is read: two invoices keyed for one supplier at the same
    moment must not each decide there is no open cycle and each open one — the
    partial unique index would refuse the second, and the operator would see a
    database error instead of an invoice.
    """
    locked = (
        SupplierPaymentCycle.objects.select_for_update()
        .filter(supplier=supplier, status=SupplierPaymentCycleStatus.COLLECTING)
        .first()
    )
    if locked is not None and invoice_date <= locked.due_date:
        return locked

    if locked is not None:
        # It has stopped collecting, but it has not been paid: it becomes
        # `DUE`, which is what the reports read, and the unique index is free
        # for the window this invoice opens.
        previous = snapshot(locked)
        locked.status = SupplierPaymentCycleStatus.DUE
        locked.save(update_fields=["status", "updated_at"])
        record_audit_event(
            action=AuditAction.UPDATED,
            target=locked,
            previous_state=previous,
            new_state=snapshot(locked),
            reason="the window closed to new invoices",
        )
    last = SupplierPaymentCycle.objects.filter(supplier=supplier).aggregate(Max("sequence"))
    cycle = SupplierPaymentCycle(
        organization=supplier.organization,
        supplier=supplier,
        sequence=(last["sequence__max"] or 0) + 1,
        status=SupplierPaymentCycleStatus.COLLECTING,
        opened_on=invoice_date,
        due_date=due_date_for_cycle(opened_on=invoice_date, payment_terms_days=payment_terms_days),
        payment_terms_days=payment_terms_days,
        minimum_settlement_percent=supplier.minimum_settlement_percent,
    )
    cycle.full_clean()
    cycle.save()
    record_audit_event(action=AuditAction.CREATED, target=cycle, new_state=snapshot(cycle))
    return cycle


def days_remaining(cycle: SupplierPaymentCycle, *, on: datetime.date | None = None) -> int:
    """
    Days left until the cycle falls due. Negative once it is overdue.

    Signed rather than floored at zero, because "eleven days late" and "due
    today" are different things to tell somebody deciding what to pay.
    """
    return (cycle.due_date - (on or timezone.localdate())).days


def cycle_invoices(cycle: SupplierPaymentCycle) -> list[SupplierInvoice]:
    """Every posted invoice in the cycle, oldest first — the order FIFO pays."""
    return list(
        SupplierInvoice.objects.filter(cycle=cycle, status=SupplierInvoiceStatus.POSTED)
        .select_related("supplier")
        .order_by("invoice_date", "number", "pk")
    )


@transaction.atomic
def close_cycle_if_settled(cycle: SupplierPaymentCycle) -> SupplierPaymentCycle:
    """
    Close a cycle whose every posted invoice is fully settled.

    Called after a payment posts. Idempotent, and silent when the cycle still
    owes something: a caller should not have to ask first, and a cycle that
    closed early would send the next invoice into a new window while the old
    one still had a balance somebody has to pay.
    """
    from apps.procurement.invoices import outstanding_amount

    locked = SupplierPaymentCycle.objects.select_for_update().get(pk=cycle.pk)
    if locked.status == SupplierPaymentCycleStatus.SETTLED:
        return locked

    invoices = cycle_invoices(locked)
    if not invoices:
        return locked
    if any(outstanding_amount(invoice) > 0 for invoice in invoices):
        return locked

    previous = snapshot(locked)
    locked.status = SupplierPaymentCycleStatus.SETTLED
    locked.settled_on = timezone.localdate()
    locked.full_clean()
    locked.save(update_fields=["status", "settled_on", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        reason="cycle settled in full",
    )
    return locked


@transaction.atomic
def reopen_cycle(cycle: SupplierPaymentCycle) -> SupplierPaymentCycle:
    """
    Put a settled cycle back to `DUE`, after a payment against it was reversed.

    `DUE` rather than `COLLECTING`: the debt is real again, but the window
    closed to new invoices when it closed, and a reversal is not a reason to
    reopen it to them. That also means reopening can never clash with the
    supplier's current collecting window — the two states are simply different
    questions.
    """
    locked = SupplierPaymentCycle.objects.select_for_update().get(pk=cycle.pk)
    if locked.status != SupplierPaymentCycleStatus.SETTLED:
        return locked

    previous = snapshot(locked)
    locked.status = SupplierPaymentCycleStatus.DUE
    locked.settled_on = None
    locked.full_clean()
    locked.save(update_fields=["status", "settled_on", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        reason="a payment against the cycle was reversed",
    )
    return locked
