"""
Procurement report reads — Task 2.16. Nothing here writes.

Twelve reports under the Phase 1 contract: scope from the caller's own
organizations (never an id widening access), cost keys **omitted** rather
than blanked without `view_supplier_cost`, exact Decimals end to end, and no
repair path anywhere (PRC-059). Every figure is derived from the documents
the way the verifiers derive it, so a report and a reconciliation can never
tell two stories.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db.models import Q, Sum
from django.utils import timezone

from apps.inventory.reports import ReportFilters
from apps.organizations.authorization import organizations_with_permission
from apps.procurement.credit_notes import (
    settled_book_value_for,
    unallocated_credit,
)
from apps.procurement.invoices import outstanding_amount, supplier_outstanding
from apps.procurement.matching import invoice_line_match_state
from apps.procurement.models import (
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PaymentAllocation,
    PurchaseMatchAllocation,
    PurchaseMatchStatus,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Supplier,
    SupplierCreditNote,
    SupplierCreditNoteStatus,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceLineType,
    SupplierInvoicePostingStatus,
    SupplierInvoiceStatus,
    SupplierPayment,
    SupplierPaymentStatus,
    SupplierReturn,
    SupplierReturnStatus,
)
from apps.procurement.payments import advance_remainder
from apps.procurement.payments import allocated_total as payment_allocated_total
from apps.procurement.permissions import VIEW_PROCUREMENT_REPORT
from apps.users.models import User

ZERO = Decimal("0.000")


@dataclass(frozen=True)
class ProcurementReportFilters(ReportFilters):
    """
    The Phase 1 filters plus a supplier, because "this supplier only" is the
    first question every procurement report gets asked.

    Subclassing `ReportFilters` rather than redeclaring it keeps the shared
    report chrome — mode label, export provenance, pagination querystring —
    working on the same object the query services receive. The inventory-only
    fields (warehouse, lot, cost centre …) are simply never read here.
    """

    supplier_id: int | None = None

    def as_query(self) -> dict[str, str]:
        pairs = super().as_query()
        if self.supplier_id is not None:
            pairs["supplier_id"] = str(self.supplier_id)
        return pairs


def _organization_ids(user: User, filters: ProcurementReportFilters) -> list[int]:
    allowed = list(
        organizations_with_permission(user, VIEW_PROCUREMENT_REPORT).values_list("id", flat=True)
    )
    if filters.organization_id is not None:
        return [pk for pk in allowed if pk == filters.organization_id]
    return allowed


def _suppliers(user: User, filters: ProcurementReportFilters) -> list[Supplier]:
    queryset = Supplier.objects.filter(
        organization_id__in=_organization_ids(user, filters)
    ).select_related("organization")
    if filters.supplier_id is not None:
        queryset = queryset.filter(pk=filters.supplier_id)
    if filters.search:
        queryset = queryset.filter(
            Q(code__icontains=filters.search) | Q(name_ar__icontains=filters.search)
        )
    return list(queryset.order_by("organization__code", "code"))


def _in_window(filters: ProcurementReportFilters, field: str) -> Q:
    """A date-window Q over one field; empty when no window was asked for."""
    window = Q()
    if filters.date_from:
        window &= Q(**{f"{field}__gte": filters.date_from})
    if filters.date_to:
        window &= Q(**{f"{field}__lte": filters.date_to})
    return window


# ---------------------------------------------------------------------------
# 1. Supplier aging
# ---------------------------------------------------------------------------


def supplier_aging(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """
    What is owed, in buckets by due date, per supplier — with standing credit
    and standing advances beside it, because "what do we actually owe this
    supplier" is all three questions at once.
    """
    today = timezone.localdate()
    rows: list[dict[str, Any]] = []
    for supplier in _suppliers(user, filters):
        buckets = {"current": ZERO, "d30": ZERO, "d60": ZERO, "d90": ZERO, "older": ZERO}
        open_total = ZERO
        for invoice in SupplierInvoice.objects.filter(
            supplier=supplier, status=SupplierInvoiceStatus.POSTED
        ):
            open_amount = outstanding_amount(invoice)
            if open_amount <= ZERO:
                continue
            open_total += open_amount
            age = (today - invoice.due_date).days
            if age <= 0:
                buckets["current"] += open_amount
            elif age <= 30:
                buckets["d30"] += open_amount
            elif age <= 60:
                buckets["d60"] += open_amount
            elif age <= 90:
                buckets["d90"] += open_amount
            else:
                buckets["older"] += open_amount
        credit = sum(
            (
                unallocated_credit(note)
                for note in SupplierCreditNote.objects.filter(
                    supplier=supplier, status=SupplierCreditNoteStatus.POSTED
                )
            ),
            start=ZERO,
        )
        advances = sum(
            (
                advance_remainder(payment)
                for payment in SupplierPayment.objects.filter(
                    supplier=supplier, status=SupplierPaymentStatus.POSTED
                )
            ),
            start=ZERO,
        )
        if open_total == ZERO and credit == ZERO and advances == ZERO:
            continue
        row: dict[str, Any] = {
            "supplier_code": supplier.code,
            "supplier_name": supplier.name_ar,
        }
        if include_cost:
            row.update(
                {
                    "current": buckets["current"],
                    "d30": buckets["d30"],
                    "d60": buckets["d60"],
                    "d90": buckets["d90"],
                    "older": buckets["older"],
                    "open_total": open_total,
                    "standing_credit": credit,
                    "advances": advances,
                    "net_position": supplier_outstanding(supplier),
                }
            )
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 2. Supplier statement
# ---------------------------------------------------------------------------


#: A statement is a chronology, and the authoritative chronology of a posted
#: document is its **journal**: `posted_at` is the moment the ledger accepted
#: it, and `entry_number` is the gapless order in which it did so. The
#: documents carry a business *date* rather than a time, so within one
#: business date the journal is the only record of what actually happened
#: first — and it is the same order the general ledger itself would show.
#:
#: An earlier cut ordered same-day rows by document *kind* (charges before
#: settlements). That was deterministic and wrong: it would show a payment
#: made on Monday and an invoice posted on Tuesday-for-Monday as though the
#: invoice came first, and a reader reconciling against the ledger would find
#: two different stories. Task 2.0 §12 requires a running balance and states
#: no kind precedence, so nothing was owed to that ordering.
#:
#: Kind survives only as a **tie-break** beneath the journal keys, for the
#: case two documents share a posting instant and a number cannot separate
#: them — which the gapless sequence makes impossible in practice, but a sort
#: key with no total order is a sort key that reorders itself between runs.
_STATEMENT_KIND_ORDER = {"فاتورة": 0, "إشعار دائن": 1, "دفعة": 2}

#: Sorts before every real posting instant. Only reachable for a document
#: whose journal is missing or unposted, which the posting services and the
#: `journal_entry_posted_records_when` constraint both forbid; it exists so a
#: defect upstream produces a stable order rather than a TypeError.
_BEFORE_ANY_POSTING = datetime.datetime.min.replace(tzinfo=datetime.UTC)


@dataclass(frozen=True)
class _StatementEvent:
    """One posted money document, with everything the statement orders by."""

    business_date: datetime.date
    posted_at: datetime.datetime
    entry_number: str
    kind: str
    number: str
    debit: Decimal
    credit: Decimal
    advance: Decimal


def _event(
    document: Any, *, kind: str, debit: Decimal, credit: Decimal, advance: Decimal
) -> _StatementEvent:
    """One document's statement event, reading its journal for the chronology."""
    journal = document.journal_entry
    return _StatementEvent(
        business_date=document.business_date,
        posted_at=(journal.posted_at if journal is not None else None) or _BEFORE_ANY_POSTING,
        entry_number=(journal.entry_number if journal is not None else "") or "",
        kind=kind,
        number=document.number or "",
        debit=debit,
        credit=credit,
        advance=advance,
    )


def _statement_sort_key(
    event: _StatementEvent,
) -> tuple[datetime.date, datetime.datetime, str, int, str]:
    return (
        event.business_date,
        event.posted_at,
        event.entry_number,
        _STATEMENT_KIND_ORDER[event.kind],
        event.number,
    )


def supplier_statement(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """
    Every posted money document for the filtered suppliers, running balance.

    Invoices raise the balance; credit notes lower it by their whole amount;
    payments lower it by their **allocated** share, with the advance shown on
    the row rather than folded into the balance — an advance is an asset, not
    a smaller debt.

    Ordered by the ledger's own chronology; see `_STATEMENT_KIND_ORDER`.
    """
    rows: list[dict[str, Any]] = []
    for supplier in _suppliers(user, filters):
        events: list[_StatementEvent] = []
        for invoice in (
            SupplierInvoice.objects.filter(supplier=supplier, status=SupplierInvoiceStatus.POSTED)
            .filter(_in_window(filters, "business_date"))
            .select_related("journal_entry")
        ):
            events.append(
                _event(
                    invoice,
                    kind="فاتورة",
                    debit=invoice.posted_amount or ZERO,
                    credit=ZERO,
                    advance=ZERO,
                )
            )
        for note in (
            SupplierCreditNote.objects.filter(
                supplier=supplier, status=SupplierCreditNoteStatus.POSTED
            )
            .filter(_in_window(filters, "business_date"))
            .select_related("journal_entry")
        ):
            events.append(
                _event(
                    note,
                    kind="إشعار دائن",
                    debit=ZERO,
                    credit=note.amount or ZERO,
                    advance=ZERO,
                )
            )
        for payment in (
            SupplierPayment.objects.filter(supplier=supplier, status=SupplierPaymentStatus.POSTED)
            .filter(_in_window(filters, "business_date"))
            .select_related("journal_entry")
        ):
            events.append(
                _event(
                    payment,
                    kind="دفعة",
                    debit=ZERO,
                    credit=payment_allocated_total(payment),
                    advance=advance_remainder(payment),
                )
            )
        balance = ZERO
        for event in sorted(events, key=_statement_sort_key):
            date, kind, number = event.business_date, event.kind, event.number
            debit, credit, advance = event.debit, event.credit, event.advance
            balance += debit - credit
            row: dict[str, Any] = {
                "supplier_code": supplier.code,
                "date": date,
                "document_kind": kind,
                "number": number,
            }
            if include_cost:
                row.update(
                    {
                        "charged": debit,
                        "settled": credit,
                        "advance": advance,
                        "balance": balance,
                    }
                )
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 3 & 4. Open purchase orders, outstanding receipt quantity
# ---------------------------------------------------------------------------


def open_purchase_orders(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """Ordered, received and outstanding, per issued order line."""
    from apps.procurement.services import received_base_quantity

    rows: list[dict[str, Any]] = []
    lines = (
        PurchaseOrderLine.objects.filter(
            order__organization_id__in=_organization_ids(user, filters),
            order__status=PurchaseOrderStatus.ISSUED,
        )
        .select_related("order", "order__supplier", "item")
        .order_by("order__number", "sequence")
    )
    if filters.supplier_id is not None:
        lines = lines.filter(order__supplier_id=filters.supplier_id)
    if filters.item_id is not None:
        lines = lines.filter(item_id=filters.item_id)
    for line in lines:
        received = received_base_quantity(line)
        outstanding = line.ordered_base_quantity - received
        if outstanding <= ZERO:
            continue
        row: dict[str, Any] = {
            "order_number": line.order.number,
            "supplier_code": line.order.supplier.code,
            "item_code": line.item.code,
            "item_name": line.item.name_ar,
            "ordered": line.ordered_base_quantity,
            "received": received,
            "outstanding": outstanding,
        }
        if include_cost:
            row["unit_price"] = line.unit_price
        rows.append(row)
    return rows


def outstanding_receipt_quantity(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """Ordered but not delivered, folded per item across every open order."""
    totals: dict[str, dict[str, Any]] = {}
    for row in open_purchase_orders(user, filters, include_cost=False):
        entry = totals.setdefault(
            row["item_code"],
            {
                "item_code": row["item_code"],
                "item_name": row["item_name"],
                "ordered": ZERO,
                "received": ZERO,
                "outstanding": ZERO,
                "order_count": 0,
            },
        )
        entry["ordered"] += row["ordered"]
        entry["received"] += row["received"]
        entry["outstanding"] += row["outstanding"]
        entry["order_count"] += 1
    return sorted(totals.values(), key=lambda entry: str(entry["item_code"]))


# ---------------------------------------------------------------------------
# 5. GRNI exceptions
# ---------------------------------------------------------------------------


def grni_exceptions(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """
    Received and not invoiced, ageing — the account that must reconcile.

    Per posted receipt line: accepted value not yet covered by a live
    posting's allocations, exactly the derivation `verify_grni_clearing`
    proves against the GL.
    """
    today = timezone.localdate()
    rows: list[dict[str, Any]] = []
    lines = (
        GoodsReceiptLine.objects.filter(
            receipt__organization_id__in=_organization_ids(user, filters),
            receipt__status=GoodsReceiptStatus.POSTED,
            accepted_base_quantity__gt=ZERO,
        )
        .select_related("receipt", "receipt__supplier", "item")
        .order_by("receipt__number", "sequence")
    )
    if filters.supplier_id is not None:
        lines = lines.filter(receipt__supplier_id=filters.supplier_id)
    for line in lines:
        cleared = (
            PurchaseMatchAllocation.objects.filter(
                goods_receipt_line=line,
                match__postings__status=SupplierInvoicePostingStatus.LIVE,
            ).aggregate(total=Sum("receipt_allocated_value"))["total"]
            or ZERO
        )
        open_value = (line.posted_value or ZERO) - cleared
        if open_value <= ZERO:
            continue
        row: dict[str, Any] = {
            "receipt_number": line.receipt.number,
            "supplier_code": line.receipt.supplier.code,
            "item_code": line.item.code,
            "received_at": line.receipt.received_at,
            "age_days": (today - line.receipt.received_at).days,
            "accepted_quantity": line.accepted_base_quantity,
        }
        if include_cost:
            row.update({"accepted_value": line.posted_value, "uninvoiced_value": open_value})
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 6. Invoice without receipt
# ---------------------------------------------------------------------------


def invoice_without_receipt(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """Invoiced and never delivered: goods lines no match has ever covered."""
    rows: list[dict[str, Any]] = []
    lines = (
        SupplierInvoiceLine.objects.filter(
            invoice__organization_id__in=_organization_ids(user, filters),
            invoice__status__in=(
                SupplierInvoiceStatus.APPROVED,
                SupplierInvoiceStatus.POSTED,
            ),
            line_type=SupplierInvoiceLineType.INVENTORY,
        )
        .select_related("invoice", "invoice__supplier", "item")
        .order_by("invoice__number", "sequence")
    )
    if filters.supplier_id is not None:
        lines = lines.filter(invoice__supplier_id=filters.supplier_id)
    for line in lines:
        if invoice_line_match_state(line) != "UNMATCHED":
            continue
        row: dict[str, Any] = {
            "invoice_number": line.invoice.number or line.invoice.supplier_invoice_number,
            "supplier_code": line.invoice.supplier.code,
            "item_code": line.item.code if line.item else "",
            "status": line.invoice.get_status_display(),
            "quantity": line.quantity,
        }
        if include_cost:
            row.update({"unit_price": line.unit_price, "line_amount": line.line_amount})
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 7 & 9. Matching exceptions, price variance
# ---------------------------------------------------------------------------


def matching_exceptions(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """
    Allocations whose price variance is not zero, on standing matches that no
    live posting has acted on yet — the decisions still waiting for somebody.
    Once the invoice posts from the match, the same variance moves to the
    posted price-variance report below; it is an explanation there, not a
    pending decision here.
    """
    rows: list[dict[str, Any]] = []
    allocations = (
        PurchaseMatchAllocation.objects.filter(
            match__organization_id__in=_organization_ids(user, filters),
            match__status__in=(PurchaseMatchStatus.DRAFT, PurchaseMatchStatus.READY),
        )
        .exclude(price_variance=ZERO)
        .exclude(match__postings__status=SupplierInvoicePostingStatus.LIVE)
        .select_related("match", "match__supplier", "supplier_invoice_line__item")
        .order_by("match__number", "sequence")
    )
    if filters.supplier_id is not None:
        allocations = allocations.filter(match__supplier_id=filters.supplier_id)
    for allocation in allocations:
        row: dict[str, Any] = {
            "match_number": allocation.match.number or "مسودة",
            "supplier_code": allocation.match.supplier.code,
            "item_code": allocation.supplier_invoice_line.item.code
            if allocation.supplier_invoice_line.item
            else "",
            "matched_quantity": allocation.matched_base_quantity,
        }
        if include_cost:
            row.update(
                {
                    "receipt_value": allocation.receipt_allocated_value,
                    "invoice_value": allocation.invoice_allocated_value,
                    "price_variance": allocation.price_variance,
                }
            )
        rows.append(row)
    return rows


def price_variance(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """Where the invoice differed from the delivery, per posted allocation."""
    rows: list[dict[str, Any]] = []
    allocations = (
        PurchaseMatchAllocation.objects.filter(
            match__organization_id__in=_organization_ids(user, filters),
            match__postings__status=SupplierInvoicePostingStatus.LIVE,
        )
        .select_related("match", "match__supplier", "supplier_invoice_line__item")
        .order_by("match__number", "sequence")
    )
    if filters.supplier_id is not None:
        allocations = allocations.filter(match__supplier_id=filters.supplier_id)
    for allocation in allocations:
        row: dict[str, Any] = {
            "match_number": allocation.match.number,
            "supplier_code": allocation.match.supplier.code,
            "item_code": allocation.supplier_invoice_line.item.code
            if allocation.supplier_invoice_line.item
            else "",
            "matched_quantity": allocation.matched_base_quantity,
        }
        if include_cost:
            row.update(
                {
                    "receipt_value": allocation.receipt_allocated_value,
                    "invoice_value": allocation.invoice_allocated_value,
                    "price_variance": allocation.price_variance,
                }
            )
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 8. Purchase spend
# ---------------------------------------------------------------------------


def purchase_spend(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """Posted invoice value by supplier and month."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    invoices = SupplierInvoice.objects.filter(
        organization_id__in=_organization_ids(user, filters),
        status=SupplierInvoiceStatus.POSTED,
    ).select_related("supplier")
    if filters.supplier_id is not None:
        invoices = invoices.filter(supplier_id=filters.supplier_id)
    invoices = invoices.filter(_in_window(filters, "business_date"))
    for invoice in invoices:
        month = invoice.business_date.strftime("%Y-%m")
        key = (invoice.supplier.code, month)
        entry = totals.setdefault(
            key,
            {
                "supplier_code": invoice.supplier.code,
                "supplier_name": invoice.supplier.name_ar,
                "month": month,
                "invoice_count": 0,
                **({"spend": ZERO} if include_cost else {}),
            },
        )
        entry["invoice_count"] += 1
        if include_cost:
            entry["spend"] += invoice.posted_amount or ZERO
    return sorted(totals.values(), key=lambda entry: (entry["month"], entry["supplier_code"]))


# ---------------------------------------------------------------------------
# 10. Return and credit status
# ---------------------------------------------------------------------------


def return_credit_status(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """Returned, credited, outstanding — per posted return line."""
    rows: list[dict[str, Any]] = []
    returns = (
        SupplierReturn.objects.filter(
            organization_id__in=_organization_ids(user, filters),
            status=SupplierReturnStatus.POSTED,
        )
        .select_related("supplier")
        .order_by("number")
    )
    if filters.supplier_id is not None:
        returns = returns.filter(supplier_id=filters.supplier_id)
    for supplier_return in returns:
        for line in supplier_return.lines.select_related("item").order_by("sequence"):
            settled = settled_book_value_for(line)
            remaining = (line.posted_value or ZERO) - settled
            row: dict[str, Any] = {
                "return_number": supplier_return.number,
                "supplier_code": supplier_return.supplier.code,
                "item_code": line.item.code,
                "returned_quantity": line.returned_base_quantity,
                "state": ("مُسوّى" if remaining == ZERO else ("جزئي" if settled > ZERO else "قائم")),
            }
            if include_cost:
                row.update(
                    {
                        "book_value": line.posted_value,
                        "settled_value": settled,
                        "open_claim": remaining,
                    }
                )
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 11. Payment allocations
# ---------------------------------------------------------------------------


def payment_allocations(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """What each posted payment covered, and what remains unallocated."""
    rows: list[dict[str, Any]] = []
    payments = (
        SupplierPayment.objects.filter(
            organization_id__in=_organization_ids(user, filters),
            status=SupplierPaymentStatus.POSTED,
        )
        .select_related("supplier")
        .order_by("number")
    )
    if filters.supplier_id is not None:
        payments = payments.filter(supplier_id=filters.supplier_id)
    payments = payments.filter(_in_window(filters, "business_date"))
    for payment in payments:
        allocations = list(
            PaymentAllocation.objects.filter(payment=payment)
            .select_related("invoice")
            .order_by("sequence")
        )
        covered = "، ".join(row.invoice.number for row in allocations) or "—"
        row: dict[str, Any] = {
            "payment_number": payment.number,
            "supplier_code": payment.supplier.code,
            "method": payment.get_method_display(),
            "paid_at": payment.paid_at,
            "covered_invoices": covered,
        }
        if include_cost:
            row.update(
                {
                    "amount": payment.amount,
                    "allocated": payment_allocated_total(payment),
                    "advance": advance_remainder(payment),
                }
            )
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 12. Procurement to GL
# ---------------------------------------------------------------------------


def procurement_to_gl(
    user: User, filters: ProcurementReportFilters, *, include_cost: bool
) -> list[dict[str, Any]]:
    """
    The PRC-058 equalities as rows the reader can check by eye, one
    organization at a time — the same derivations
    `verify_procurement_accounting` proves, never a second formula.
    """
    from apps.organizations.models import Organization
    from apps.procurement.reconciliation import (
        verify_grni_clearing,
        verify_procurement_accounting,
    )

    rows: list[dict[str, Any]] = []
    for organization_id in _organization_ids(user, filters):
        organization = Organization.objects.get(pk=organization_id)
        problems = verify_procurement_accounting(organization)
        grni_problems = verify_grni_clearing(organization)
        rows.append(
            {
                "organization": organization.code,
                "check": "أرصدة الموردين المفتوحة مقابل حساب الذمم",
                "state": "غير مطابق"
                if any(p.field == "open_balances_vs_payable_account" for p in problems)
                else "مطابق",
            }
        )
        rows.append(
            {
                "organization": organization.code,
                "check": "قيمة الاستلام غير المفوترة مقابل حركة GRNI",
                "state": "غير مطابق" if grni_problems else "مطابق",
            }
        )
        rows.append(
            {
                "organization": organization.code,
                "check": "كل قيد شراء يقتفي مستنداً واحداً",
                "state": "غير مطابق"
                if any(p.field == "journal_cites_unknown_document" for p in problems)
                else "مطابق",
            }
        )
    return rows
