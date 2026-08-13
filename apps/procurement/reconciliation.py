"""
Procurement reconciliation: verify only, never repair.

Reuses `apps.inventory.reconciliation.Discrepancy` rather than declaring a
second one, because an operator reading a reconciliation report should not have
to learn which module produced each row.

**There is no repair mode, and that is a decision rather than an omission.** A
verifier that fixes what it finds destroys the only evidence that something
went wrong, and the second time it runs it reports clean — so the defect that
produced the mismatch is never diagnosed. A planted manual journal stays
visible here until a human decides what actually happened.

Five equalities, each catching a different failure:

    accepted quantity  == the receipt's stock movement quantity
                       == its location quantity effect, where the warehouse
                          uses bins
    posted value       == the movement value
                       == the inventory-control debit
                       == the GRNI credit
    rejected quantity  produces no movement and no journal effect at all
    posted receipts    == the order line's consumed quantity, excluding
                          reversed receipts
    GRNI outstanding   == posted receipt value not yet cleared by an invoice
                          (zero until Task 2.12 posts one)
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from apps.accounting.models import JournalEntry
from apps.inventory.models import StockLocationMovement, StockMovement
from apps.inventory.reconciliation import Discrepancy
from apps.organizations.models import Organization
from apps.procurement.invoices import (
    SOURCE_DOCUMENT_TYPE as INVOICE_SOURCE_TYPE,
)
from apps.procurement.invoices import supplier_outstanding
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseMatchAllocation,
    PurchaseMatchStatus,
    PurchaseOrderLine,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
)
from apps.procurement.posting import SOURCE_DOCUMENT_TYPE

ZERO = Decimal("0.000")


def verify_goods_receipt(receipt: GoodsReceipt) -> list[Discrepancy]:
    """
    One posted receipt, across every representation of what it did.

    A draft is skipped rather than reported clean: it has posted nothing, so
    there is nothing to reconcile and saying "consistent" would overstate it.
    """
    if receipt.status not in (GoodsReceiptStatus.POSTED, GoodsReceiptStatus.REVERSED):
        return []

    label = receipt.number or str(receipt.public_id)
    problems: list[Discrepancy] = []
    lines = list(receipt.lines.select_related("movement", "item", "lot").order_by("sequence"))

    line_total = sum((line.posted_value or ZERO for line in lines), ZERO)
    if receipt.posted_value != line_total:
        problems.append(
            Discrepancy(
                scope=label,
                field="document_total",
                expected=line_total,
                actual=receipt.posted_value if receipt.posted_value is not None else "missing",
            )
        )

    if receipt.stock_entry is None:
        problems.append(
            Discrepancy(scope=label, field="stock_entry", expected="present", actual="missing")
        )
        return problems

    movements = list(receipt.stock_entry.movements.all())
    movement_total = sum((abs(movement.inventory_value) for movement in movements), ZERO)
    if movement_total != line_total:
        problems.append(
            Discrepancy(
                scope=label, field="movement_total", expected=line_total, actual=movement_total
            )
        )

    quantity_total = sum((abs(movement.base_quantity) for movement in movements), ZERO)
    accepted_total = sum((line.accepted_base_quantity for line in lines), ZERO)
    if quantity_total != accepted_total:
        problems.append(
            Discrepancy(
                scope=label,
                field="movement_quantity",
                expected=accepted_total,
                actual=quantity_total,
            )
        )

    problems.extend(_verify_journal(receipt, label=label, line_total=line_total))
    problems.extend(_verify_lines(receipt, lines, label=label))
    return problems


def _verify_journal(receipt: GoodsReceipt, *, label: str, line_total: Decimal) -> list[Discrepancy]:
    """Inventory control debited and GRNI credited, both for the posted total."""
    if receipt.journal_entry is None:
        return [
            Discrepancy(scope=label, field="journal_entry", expected="present", actual="missing")
        ]

    problems: list[Discrepancy] = []
    journal_lines = list(receipt.journal_entry.lines.all())
    debits = sum((row.debit for row in journal_lines), ZERO)
    credits = sum((row.credit for row in journal_lines), ZERO)
    if debits != line_total:
        problems.append(
            Discrepancy(scope=label, field="journal_debits", expected=line_total, actual=debits)
        )
    if credits != line_total:
        problems.append(
            Discrepancy(scope=label, field="journal_credits", expected=line_total, actual=credits)
        )
    return problems


def _verify_lines(
    receipt: GoodsReceipt, lines: list[GoodsReceiptLine], *, label: str
) -> list[Discrepancy]:
    """
    Per line: the movement carries the accepted quantity at the posted value,
    a rejected-only line carries no movement at all, and where the warehouse
    uses bins the located effect equals the accepted quantity.
    """
    problems: list[Discrepancy] = []
    for line in lines:
        scope = f"{label}#{line.sequence}"
        if line.accepted_base_quantity <= ZERO:
            # PRC-025: rejected goods never entered inventory.
            if line.movement is not None:
                problems.append(
                    Discrepancy(
                        scope=scope,
                        field="rejected_line_posted_stock",
                        expected="no movement",
                        actual=str(line.movement.pk),
                    )
                )
            continue

        if line.movement is None:
            problems.append(
                Discrepancy(scope=scope, field="movement", expected="present", actual="missing")
            )
            continue
        if abs(line.movement.inventory_value) != (line.posted_value or ZERO):
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="line_vs_movement",
                    expected=line.posted_value or ZERO,
                    actual=abs(line.movement.inventory_value),
                )
            )
        if abs(line.movement.base_quantity) != line.accepted_base_quantity:
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="line_vs_movement_quantity",
                    expected=line.accepted_base_quantity,
                    actual=abs(line.movement.base_quantity),
                )
            )
        problems.extend(_verify_location_effect(receipt, line, scope=scope))
    return problems


def _verify_location_effect(
    receipt: GoodsReceipt, line: GoodsReceiptLine, *, scope: str
) -> list[Discrepancy]:
    """
    Where a bin was named, the put-away moved the accepted quantity into it.

    Read from the location movements this receipt's own stock movement
    produced, not from the bin's current balance: the balance moves every time
    somebody picks from it, and comparing against it would report a mismatch
    for the entirely normal act of using the stock.
    """
    if receipt.location is None or line.movement is None:
        return []
    placed: Decimal | None = StockLocationMovement.objects.filter(
        stock_movement=line.movement, location=receipt.location
    ).aggregate(total=Sum("base_quantity"))["total"]
    moved = placed or ZERO
    if moved != line.accepted_base_quantity:
        return [
            Discrepancy(
                scope=scope,
                field="location_quantity",
                expected=line.accepted_base_quantity,
                actual=moved,
            )
        ]
    return []


def verify_order_received_quantities(organization: Organization) -> list[Discrepancy]:
    """
    Every order line's consumed quantity equals its posted receipts.

    Reversed and draft receipts are excluded, which is the point: a reversed
    receipt gave its stock back and must release the ordered quantity it was
    holding, and a draft never held any (F7).
    """
    problems: list[Discrepancy] = []
    order_lines = PurchaseOrderLine.objects.filter(order__organization=organization).select_related(
        "order", "item"
    )
    for order_line in order_lines:
        posted: Decimal | None = GoodsReceiptLine.objects.filter(
            order_line=order_line, receipt__status=GoodsReceiptStatus.POSTED
        ).aggregate(total=Sum("accepted_base_quantity"))["total"]
        received = posted or ZERO
        if received > order_line.ordered_base_quantity:
            problems.append(
                Discrepancy(
                    scope=f"{order_line.order.number}#{order_line.sequence}",
                    field="received_exceeds_ordered",
                    expected=order_line.ordered_base_quantity,
                    actual=received,
                )
            )
    return problems


def verify_grni(organization: Organization) -> list[Discrepancy]:
    """
    Every posted receipt's GRNI credit is accounted for, and no journal cites a
    procurement receipt that did not produce it.

    The second half is the one that catches a planted entry: a manual journal
    naming `PROCUREMENT_GOODS_RECEIPT` with an id no receipt owns is a
    fabricated source identity, and it stays visible here until somebody
    explains it.
    """
    problems: list[Discrepancy] = []
    known = {
        str(public_id)
        for public_id in GoodsReceipt.objects.filter(organization=organization).values_list(
            "public_id", flat=True
        )
    }
    cited = JournalEntry.objects.filter(
        organization=organization, source_document_type=SOURCE_DOCUMENT_TYPE
    ).values_list("source_document_id", flat=True)
    for document_id in set(cited):
        if document_id not in known:
            problems.append(
                Discrepancy(
                    scope=SOURCE_DOCUMENT_TYPE,
                    field="journal_cites_unknown_receipt",
                    expected="a receipt in this organization",
                    actual=document_id,
                )
            )
    return problems


def verify_procurement(organization: Organization) -> list[str]:
    """
    The whole procurement posting surface for one organization, as text.

    Returns operator-readable strings for the same reason
    `verify_inventory_accounting` does: this is what a management command
    prints and what a runbook pastes, and a caller that wants structure calls
    the individual verifiers.
    """
    problems: list[Discrepancy] = []
    receipts = (
        GoodsReceipt.objects.filter(organization=organization)
        .exclude(status=GoodsReceiptStatus.DRAFT)
        .select_related("stock_entry", "journal_entry", "location")
    )
    for receipt in receipts:
        problems.extend(verify_goods_receipt(receipt))
    problems.extend(verify_order_received_quantities(organization))
    problems.extend(verify_grni(organization))
    for invoice in SupplierInvoice.objects.filter(organization=organization).exclude(
        status=SupplierInvoiceStatus.DRAFT
    ):
        problems.extend(verify_supplier_invoice(invoice))
    problems.extend(verify_invoice_charges(organization))
    problems.extend(verify_supplier_payables(organization))
    problems.extend(verify_matching(organization))
    return [str(problem) for problem in problems]


__all__ = [
    "verify_goods_receipt",
    "verify_grni",
    "verify_invoice_charges",
    "verify_order_received_quantities",
    "verify_procurement",
    "verify_matching",
    "verify_supplier_invoice",
    "verify_supplier_payables",
]


# ---------------------------------------------------------------------------
# Supplier invoices (Task 2.10)
# ---------------------------------------------------------------------------


def verify_supplier_invoice(invoice: SupplierInvoice) -> list[Discrepancy]:
    """
    One posted invoice, across every representation of what it did.

        posted amount == sum of stored line net amounts
                      == the journal's debits
                      == the payable credit

    and, separately from all of those, that it moved no stock at all.
    """
    if invoice.status not in (SupplierInvoiceStatus.POSTED, SupplierInvoiceStatus.REVERSED):
        return []

    label = invoice.number or str(invoice.public_id)
    problems: list[Discrepancy] = []
    lines = list(invoice.lines.order_by("sequence"))

    line_total = sum((line.net_amount for line in lines), ZERO)
    if invoice.total_amount != line_total:
        problems.append(
            Discrepancy(
                scope=label,
                field="document_total",
                expected=line_total,
                actual=invoice.total_amount,
            )
        )
    if invoice.posted_amount != line_total:
        problems.append(
            Discrepancy(
                scope=label,
                field="posted_amount",
                expected=line_total,
                actual=invoice.posted_amount if invoice.posted_amount is not None else "missing",
            )
        )

    if invoice.journal_entry is None:
        problems.append(
            Discrepancy(scope=label, field="journal_entry", expected="present", actual="missing")
        )
        return problems

    journal_lines = list(invoice.journal_entry.lines.select_related("account"))
    debits = sum((row.debit for row in journal_lines), ZERO)
    credits = sum((row.credit for row in journal_lines), ZERO)
    if debits != line_total:
        problems.append(
            Discrepancy(scope=label, field="journal_debits", expected=line_total, actual=debits)
        )
    if credits != line_total:
        problems.append(
            Discrepancy(scope=label, field="payable_credit", expected=line_total, actual=credits)
        )

    # PRC-038, asserted rather than assumed: this document is the reason the
    # goods receipt and the invoice are two models, so the claim that it moves
    # no stock is worth one query.
    moved = StockMovement.objects.filter(
        entry__source_document_type=INVOICE_SOURCE_TYPE,
        entry__source_document_id=str(invoice.public_id),
    ).count()
    if moved:
        problems.append(Discrepancy(scope=label, field="stock_movements", expected=0, actual=moved))

    for line in lines:
        scope = f"{label}#{line.sequence}"
        expected = line.line_amount + line.allocated_freight - line.allocated_discount
        if line.net_amount != expected:
            problems.append(
                Discrepancy(
                    scope=scope, field="line_net_amount", expected=expected, actual=line.net_amount
                )
            )
        if line.journal_line is None:
            problems.append(
                Discrepancy(scope=scope, field="journal_line", expected="present", actual="missing")
            )
    return problems


def verify_invoice_charges(organization: Organization) -> list[Discrepancy]:
    """
    Every document-level charge is fully allocated across its own lines.

    `allocate` guarantees the parts sum to the whole, so this checks the
    guarantee held rather than re-deriving it — a residual that went missing
    between allocation and storage is exactly the drift a reconciliation exists
    to surface (PRC-039).
    """
    problems: list[Discrepancy] = []
    invoices = SupplierInvoice.objects.filter(organization=organization).exclude(
        status=SupplierInvoiceStatus.DRAFT
    )
    for invoice in invoices:
        lines = list(invoice.lines.all())
        if not lines:
            continue
        freight = sum((line.allocated_freight for line in lines), ZERO)
        discount = sum((line.allocated_discount for line in lines), ZERO)
        label = invoice.number or str(invoice.public_id)
        if freight != invoice.freight_amount:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="allocated_freight",
                    expected=invoice.freight_amount,
                    actual=freight,
                )
            )
        if discount != invoice.discount_amount:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="allocated_discount",
                    expected=invoice.discount_amount,
                    actual=discount,
                )
            )
    return problems


def verify_supplier_payables(organization: Organization) -> list[Discrepancy]:
    """
    Each supplier's derived outstanding equals its posted invoices.

    Trivially true today and deliberately written anyway: Task 2.14 and Task
    2.15 subtract credit and payment allocations from the same expression, and
    the equation this asserts is where their arithmetic will be caught.

    Also catches the fabricated journal: an entry citing
    `PROCUREMENT_SUPPLIER_INVOICE` with an id no invoice owns is reported and
    left exactly where it is. There is no repair mode.
    """
    problems: list[Discrepancy] = []
    suppliers = Supplier.objects.filter(organization=organization)
    for supplier in suppliers:
        posted: Decimal | None = SupplierInvoice.objects.filter(
            supplier=supplier, status=SupplierInvoiceStatus.POSTED
        ).aggregate(total=Sum("posted_amount"))["total"]
        expected = posted or ZERO
        actual = supplier_outstanding(supplier)
        if actual != expected:
            problems.append(
                Discrepancy(
                    scope=supplier.code,
                    field="supplier_outstanding",
                    expected=expected,
                    actual=actual,
                )
            )

    known = {
        str(public_id)
        for public_id in SupplierInvoice.objects.filter(organization=organization).values_list(
            "public_id", flat=True
        )
    }
    cited = JournalEntry.objects.filter(
        organization=organization, source_document_type=INVOICE_SOURCE_TYPE
    ).values_list("source_document_id", flat=True)
    for document_id in set(cited):
        if document_id not in known:
            problems.append(
                Discrepancy(
                    scope=INVOICE_SOURCE_TYPE,
                    field="journal_cites_unknown_invoice",
                    expected="an invoice in this organization",
                    actual=document_id,
                )
            )
    return problems


# ---------------------------------------------------------------------------
# Three-way matching (Task 2.11)
# ---------------------------------------------------------------------------


def verify_matching(organization: Organization) -> list[Discrepancy]:
    """
    Every allocation adds up, and matching has moved nothing.

    Four equalities per source line, plus the claim Task 2.11 exists to keep:

        receipt accepted quantity == active matched + unmatched
        receipt posted value      == active allocated + remaining
        invoice base quantity     == active matched + unmatched
        invoice net amount        == active allocated + remaining

    The last two are what make Task 2.12's arithmetic safe. If the allocations
    against a line did not sum back to what the line said, the variance it
    computes would be a difference between two figures that no longer describe
    the same thing.
    """
    from apps.procurement.matching import (
        invoice_line_availability,
        matched_quantity_for_invoice_line,
        matched_quantity_for_order_line,
        matched_quantity_for_receipt_line,
        receipt_availability,
    )

    problems: list[Discrepancy] = []
    active = PurchaseMatchAllocation.objects.exclude(match__status=PurchaseMatchStatus.CANCELLED)

    for line in GoodsReceiptLine.objects.filter(
        receipt__organization=organization, receipt__status=GoodsReceiptStatus.POSTED
    ).select_related("receipt", "item"):
        label = f"{line.receipt.number or line.receipt.public_id}#{line.sequence}"
        left = receipt_availability(line)
        matched = matched_quantity_for_receipt_line(line)
        if matched + left.quantity != line.accepted_base_quantity:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="receipt_quantity_coverage",
                    expected=line.accepted_base_quantity,
                    actual=matched + left.quantity,
                )
            )
        allocated: Decimal | None = active.filter(goods_receipt_line=line).aggregate(
            total=Sum("receipt_allocated_value")
        )["total"]
        posted = line.posted_value or ZERO
        if (allocated or ZERO) + left.value != posted:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="receipt_value_coverage",
                    expected=posted,
                    actual=(allocated or ZERO) + left.value,
                )
            )

    for invoice_line in (
        SupplierInvoiceLine.objects.filter(
            invoice__organization=organization, line_type="INVENTORY"
        )
        .exclude(invoice__status=SupplierInvoiceStatus.DRAFT)
        .select_related("invoice", "item")
    ):
        invoice = invoice_line.invoice
        label = f"{invoice.number or invoice.supplier_invoice_number}#{invoice_line.sequence}"
        invoice_left = invoice_line_availability(invoice_line)
        invoice_matched = matched_quantity_for_invoice_line(invoice_line)
        invoiced = invoice_line.base_quantity or ZERO
        if invoice_matched + invoice_left.quantity != invoiced:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="invoice_quantity_coverage",
                    expected=invoiced,
                    actual=invoice_matched + invoice_left.quantity,
                )
            )
        invoice_allocated: Decimal | None = active.filter(
            supplier_invoice_line=invoice_line
        ).aggregate(total=Sum("invoice_allocated_value"))["total"]
        if (invoice_allocated or ZERO) + invoice_left.value != invoice_line.net_amount:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="invoice_value_coverage",
                    expected=invoice_line.net_amount,
                    actual=(invoice_allocated or ZERO) + invoice_left.value,
                )
            )

    for allocation in PurchaseMatchAllocation.objects.filter(
        match__organization=organization
    ).select_related("match"):
        expected = allocation.invoice_allocated_value - allocation.receipt_allocated_value
        if allocation.price_variance != expected:
            problems.append(
                Discrepancy(
                    scope=f"{allocation.match} #{allocation.sequence}",
                    field="price_variance",
                    expected=expected,
                    actual=allocation.price_variance,
                )
            )

    for order_line in PurchaseOrderLine.objects.filter(
        order__organization=organization
    ).select_related("order"):
        matched = matched_quantity_for_order_line(order_line)
        if matched > order_line.ordered_base_quantity:
            problems.append(
                Discrepancy(
                    scope=f"{order_line.order.number}#{order_line.sequence}",
                    field="order_over_allocation",
                    expected=order_line.ordered_base_quantity,
                    actual=matched,
                )
            )

    problems.extend(_verify_matching_moved_nothing(organization))
    return problems


def _verify_matching_moved_nothing(organization: Organization) -> list[Discrepancy]:
    """
    The Task 2.12 boundary, asserted as reconciliation and not only as a unit
    test.

    A journal or a stock movement citing a purchase match would mean somebody
    built financial posting into the matching workspace, which is the one
    thing Task 2.11 must not do — and a reconciliation that runs nightly finds
    it whether or not anybody remembered to run the test.
    """
    from apps.procurement.matching import AUDIT_DOCUMENT_TYPE

    problems: list[Discrepancy] = []
    journals = JournalEntry.objects.filter(
        organization=organization, source_document_type=AUDIT_DOCUMENT_TYPE
    ).count()
    if journals:
        problems.append(
            Discrepancy(
                scope=AUDIT_DOCUMENT_TYPE, field="journal_entries", expected=0, actual=journals
            )
        )
    movements = StockMovement.objects.filter(
        entry__source_document_type=AUDIT_DOCUMENT_TYPE
    ).count()
    if movements:
        problems.append(
            Discrepancy(
                scope=AUDIT_DOCUMENT_TYPE, field="stock_movements", expected=0, actual=movements
            )
        )
    return problems
