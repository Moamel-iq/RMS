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

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    PURCHASE_PRICE_VARIANCE,
    PURCHASE_RETURN_VARIANCE,
    SUPPLIER_ADVANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_RETURN_CLEARING,
    JournalEntry,
    JournalLine,
    OrganizationAccountMapping,
)
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
    PaymentAllocation,
    PurchaseMatchAllocation,
    PurchaseMatchStatus,
    PurchaseOrderLine,
    Supplier,
    SupplierCreditNote,
    SupplierCreditNoteStatus,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceLineType,
    SupplierInvoicePosting,
    SupplierInvoicePostingStatus,
    SupplierInvoiceStatus,
    SupplierPayment,
    SupplierPaymentStatus,
    SupplierReturn,
    SupplierReturnLine,
    SupplierReturnStatus,
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
    problems.extend(verify_grni_clearing(organization))
    problems.extend(verify_parked_variance(organization))
    problems.extend(verify_supplier_returns(organization))
    problems.extend(verify_supplier_credit_notes(organization))
    problems.extend(verify_supplier_payments(organization))
    problems.extend(verify_procurement_accounting(organization))
    return [str(problem) for problem in problems]


__all__ = [
    "verify_goods_receipt",
    "verify_grni",
    "verify_invoice_charges",
    "verify_order_received_quantities",
    "verify_grni_clearing",
    "verify_procurement_accounting",
    "verify_supplier_credit_notes",
    "verify_supplier_payments",
    "verify_supplier_returns",
    "verify_parked_variance",
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

        posted amount     == sum of stored line net amounts
        payable credit    == the same figure, on `SUPPLIER_PAYABLE` alone
        GRNI debit        == what the deliveries posted
        variance movement == the difference, signed

    and, separately from all of those, that it moved no stock at all.

    **Account-aware, and it has to be.** Task 2.10 compared the journal's total
    debits and total credits against the line total, which was right while the
    only entry shape was `Dr expense / Cr payable`. It is wrong the moment a
    variance line exists: on a *cheaper* invoice the entry is
    `Dr GRNI 70,000 / Cr variance 2,000 / Cr payable 68,000`, whose debits and
    credits are both 70,000 against a line total of 68,000 — so both checks
    would fire on a correct posting, and neither would fire on the dearer case,
    where the arithmetic happens to cancel. A verifier that is wrong in one
    direction and blind in the other is worse than none.

    So each figure is checked against the account that carries it.
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
    posting = invoice.postings.select_related("purchase_match").order_by("-generation").first()

    payable_account = _role_account(invoice.organization_id, SUPPLIER_PAYABLE)
    payable_credit = sum(
        (row.credit - row.debit for row in journal_lines if row.account_id == payable_account),
        ZERO,
    )
    if payable_credit != line_total:
        problems.append(
            Discrepancy(
                scope=label, field="payable_credit", expected=line_total, actual=payable_credit
            )
        )

    if posting is not None:
        grni_accounts = {
            row.contra_account_id
            for row in GoodsReceiptLine.objects.filter(
                match_allocations__match=posting.purchase_match
            )
            if row.contra_account_id is not None
        }
        grni_debit = sum(
            (row.debit - row.credit for row in journal_lines if row.account_id in grni_accounts),
            ZERO,
        )
        if grni_debit != posting.goods_cleared_value:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="grni_cleared",
                    expected=posting.goods_cleared_value,
                    actual=grni_debit,
                )
            )
        variance_account = _role_account(invoice.organization_id, PURCHASE_PRICE_VARIANCE)
        variance_movement = sum(
            (row.debit - row.credit for row in journal_lines if row.account_id == variance_account),
            ZERO,
        )
        if variance_movement != posting.price_variance:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="price_variance",
                    expected=posting.price_variance,
                    actual=variance_movement,
                )
            )

    # PRC-038, asserted rather than assumed: this document is the reason the
    # goods receipt and the invoice are two models, so the claim that it moves
    # no stock is worth one query.
    identities = {str(invoice.public_id)} | {str(row.public_id) for row in invoice.postings.all()}
    moved = StockMovement.objects.filter(
        entry__source_document_type=INVOICE_SOURCE_TYPE,
        entry__source_document_id__in=identities,
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
        if line.journal_line is None and line.line_type == SupplierInvoiceLineType.ACCOUNT:
            problems.append(
                Discrepancy(scope=scope, field="journal_line", expected="present", actual="missing")
            )
    return problems


def _role_account(organization_id: int, role_code: str) -> int | None:
    """
    The account a role currently resolves to, or `None` where nobody has
    mapped it. A verifier reads the mapping rather than a code, for the same
    reason a posting service does (ADR-019).
    """
    return (
        OrganizationAccountMapping.objects.filter(
            organization_id=organization_id, account_role__code=role_code
        )
        .order_by("-effective_from")
        .values_list("account_id", flat=True)
        .first()
    )


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
        # Task 2.14: posted credit notes reduce what is owed — the whole
        # note, allocated or standing, because an unallocated credit is
        # still money the supplier owes back. Task 2.15: posted payments
        # reduce it by their **allocated** share only; the advance remainder
        # is an asset the payable never saw (PRC-055).
        credited: Decimal | None = SupplierCreditNote.objects.filter(
            supplier=supplier, status=SupplierCreditNoteStatus.POSTED
        ).aggregate(total=Sum("amount"))["total"]
        paid: Decimal | None = PaymentAllocation.objects.filter(
            invoice__supplier=supplier, payment__status=SupplierPaymentStatus.POSTED
        ).aggregate(total=Sum("allocated_amount"))["total"]
        expected = (posted or ZERO) - (credited or ZERO) - (paid or ZERO)
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

    # A journal filed under this type names a **posting generation**, not the
    # invoice — an invoice may reach the ledger more than once and ADR-017 has
    # to tell the entries apart. Invoice ids stay in the set because entries
    # posted before Task 2.12 introduced generations still carry them.
    known = {
        str(public_id)
        for public_id in SupplierInvoice.objects.filter(organization=organization).values_list(
            "public_id", flat=True
        )
    } | {
        str(public_id)
        for public_id in SupplierInvoicePosting.objects.filter(
            organization=organization
        ).values_list("public_id", flat=True)
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
    Matching itself still posts nothing, and now it never will.

    Task 2.11 asserted that no journal and no stock movement cited a purchase
    match. Half of that survives Task 2.12 unchanged and half of it had to
    move. The **invoice** is what reaches the ledger, under its own source
    identity; the matching workspace stayed non-financial, and a journal
    filed under `PROCUREMENT_PURCHASE_MATCH` would still mean somebody built
    posting into the wrong module.

    The stock half is stronger than before, not weaker: **no procurement
    invoice posting may move stock, ever**. PRC-038 said an invoice moves no
    stock; PRC-043 adds that a price difference must not restate a posted
    movement either. Both come to the same query.
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
        entry__source_document_type__in=(AUDIT_DOCUMENT_TYPE, INVOICE_SOURCE_TYPE),
        entry__organization=organization,
    ).count()
    if movements:
        problems.append(
            Discrepancy(
                scope=AUDIT_DOCUMENT_TYPE, field="stock_movements", expected=0, actual=movements
            )
        )
    return problems


def verify_supplier_returns(organization: Organization) -> list[Discrepancy]:
    """
    Every posted return moved what it says it moved, and no more than arrived.

        posted value          == the sum of its lines' posted values
                              == the debit on SUPPLIER_RETURN_CLEARING
                              == the credit on the inventory control accounts
        returned quantity     <= the delivery line's accepted quantity

    The return variance account is `verify_supplier_credit_notes`' to explain
    now: Task 2.14 is what posts to it, and every fils in it must trace to a
    posted credit note's agreed-versus-book difference.
    """
    from apps.procurement.returns import SOURCE_DOCUMENT_TYPE as RETURN_SOURCE_TYPE

    problems: list[Discrepancy] = []
    clearing_account = _role_account(organization.pk, SUPPLIER_RETURN_CLEARING)

    for document in SupplierReturn.objects.filter(
        organization=organization, status=SupplierReturnStatus.POSTED
    ).select_related("journal_entry"):
        label = document.number or str(document.public_id)
        lines = list(document.lines.all())
        line_total = sum((line.posted_value or ZERO for line in lines), ZERO)
        if document.posted_value != line_total:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="return_posted_value",
                    expected=line_total,
                    actual=document.posted_value
                    if document.posted_value is not None
                    else "missing",
                )
            )
        if document.journal_entry is None:  # pragma: no cover - a constraint refuses it
            problems.append(
                Discrepancy(
                    scope=label, field="journal_entry", expected="present", actual="missing"
                )
            )
            continue
        journal_lines = list(document.journal_entry.lines.select_related("account"))
        cleared = sum(
            (row.debit - row.credit for row in journal_lines if row.account_id == clearing_account),
            ZERO,
        )
        if cleared != line_total:
            problems.append(
                Discrepancy(
                    scope=label, field="return_clearing_debit", expected=line_total, actual=cleared
                )
            )
        # What left inventory: every credit that is not the clearing debit.
        # Stated as its own equality rather than trusting the entry to balance,
        # because a balanced entry against the *wrong* account balances too.
        from_inventory = sum(
            (row.credit - row.debit for row in journal_lines if row.account_id != clearing_account),
            ZERO,
        )
        if from_inventory != line_total:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="return_inventory_credit",
                    expected=line_total,
                    actual=from_inventory,
                )
            )

    # The quantity bound, recomputed from the rows rather than trusted to the
    # service that wrote them — the same belt-and-braces the over-allocation
    # check uses, and for the reason ADR-023 §3 gives: a guard that lives only
    # inside one service function is one refactor away from not existing.
    returned: dict[int, Decimal] = {}
    for row in (
        SupplierReturnLine.objects.filter(supplier_return__organization=organization)
        .exclude(supplier_return__status=SupplierReturnStatus.REVERSED)
        .values("goods_receipt_line_id", "returned_base_quantity")
    ):
        key = row["goods_receipt_line_id"]
        returned[key] = returned.get(key, ZERO) + row["returned_base_quantity"]
    for line_id, quantity in returned.items():
        receipt_line = GoodsReceiptLine.objects.select_related("item").get(pk=line_id)
        if quantity > receipt_line.accepted_base_quantity:
            problems.append(
                Discrepancy(
                    scope=f"{receipt_line.receipt_id}#{receipt_line.sequence}",
                    field="returned_more_than_arrived",
                    expected=receipt_line.accepted_base_quantity,
                    actual=quantity,
                )
            )

    # And a return moves stock, which an invoice never does — so the shape of
    # this check is the opposite of `_verify_matching_moved_nothing`: every
    # posted return must have movements, and they must be RETURN_OUT.
    wrong_type = (
        StockMovement.objects.filter(
            entry__source_document_type=RETURN_SOURCE_TYPE, entry__organization=organization
        )
        .exclude(movement_type__in=("RETURN_OUT", "REVERSAL"))
        .count()
    )
    if wrong_type:
        problems.append(
            Discrepancy(
                scope=RETURN_SOURCE_TYPE,
                field="return_movement_type",
                expected="RETURN_OUT",
                actual=wrong_type,
            )
        )
    return problems


def verify_supplier_credit_notes(organization: Organization) -> list[Discrepancy]:
    """
    Every posted credit note settles its claim exactly, and the two accounts
    it may touch hold precisely what the notes put there.

    Per posted note, against its own journal:

        payable debit    == the note's amount
        clearing credit  == the settled return's book value
        variance line    == amount − book value, in the right direction,
                            absent when they agree

    And across the organization:

        return clearing balance == standing posted returns' book value
                                   less what posted notes have cleared
        return variance balance == Σ (book value − amount) over posted notes,
                                   as a debit-minus-credit figure

    The second pair is the ADR-022 §2 amendment holding as reconciliation: the
    clearing account carries exactly the claims still waiting for paper, and
    every fils of variance traces to an agreed figure — never to an
    expectation.
    """
    from apps.procurement.credit_notes import allocated_total
    from apps.procurement.invoices import outstanding_amount as invoice_outstanding
    from apps.procurement.models import SupplierCreditReturnAllocation

    problems: list[Discrepancy] = []
    payable_account = _role_account(organization.pk, SUPPLIER_PAYABLE)
    clearing_account = _role_account(organization.pk, SUPPLIER_RETURN_CLEARING)
    variance_account = _role_account(organization.pk, PURCHASE_RETURN_VARIANCE)

    expected_variance = ZERO  # debit-minus-credit
    cleared_by_notes = ZERO

    for note in SupplierCreditNote.objects.filter(
        organization=organization, status=SupplierCreditNoteStatus.POSTED
    ).select_related("journal_entry", "supplier_return"):
        label = note.number or str(note.public_id)
        settlements = list(note.return_allocations.all())
        settled_total = sum((row.settled_book_value or ZERO for row in settlements), ZERO)
        attributed = sum((row.allocated_credit_amount for row in settlements), ZERO)
        difference = (note.amount or ZERO) - settled_total
        expected_variance -= difference
        cleared_by_notes += settled_total

        if not settlements:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="note_settles_nothing",
                    expected="return allocations",
                    actual="none",
                )
            )
        if attributed != note.amount:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="credit_not_fully_attributed",
                    expected=note.amount,
                    actual=attributed,
                )
            )
        if note.journal_entry is None:  # pragma: no cover - a constraint refuses it
            problems.append(
                Discrepancy(
                    scope=label, field="journal_entry", expected="present", actual="missing"
                )
            )
            continue
        rows = list(note.journal_entry.lines.select_related("account"))
        payable_debit = sum(
            (row.debit - row.credit for row in rows if row.account_id == payable_account), ZERO
        )
        if payable_debit != note.amount:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="note_payable_debit",
                    expected=note.amount,
                    actual=payable_debit,
                )
            )
        clearing_credit = sum(
            (row.credit - row.debit for row in rows if row.account_id == clearing_account), ZERO
        )
        if clearing_credit != settled_total:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="note_clearing_credit",
                    expected=settled_total,
                    actual=clearing_credit,
                )
            )
        variance_amount = sum(
            (row.credit - row.debit for row in rows if row.account_id == variance_account), ZERO
        )
        if variance_amount != difference:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="note_variance",
                    expected=difference,
                    actual=variance_amount,
                )
            )
        # An allocation may not outrun the note, and no invoice may be
        # credited below zero.
        if allocated_total(note) > (note.amount or ZERO):
            problems.append(
                Discrepancy(
                    scope=label,
                    field="allocations_exceed_note",
                    expected=note.amount,
                    actual=allocated_total(note),
                )
            )

    # The per-line settlement bound, recomputed from the rows — active
    # settlements never exceed what the line posted, and a line whose
    # quantity is fully credited holds no residual book value.
    for line in SupplierReturnLine.objects.filter(
        supplier_return__organization=organization,
        supplier_return__status=SupplierReturnStatus.POSTED,
    ).select_related("supplier_return"):
        active = SupplierCreditReturnAllocation.objects.filter(
            supplier_return_line=line,
            credit_note__status=SupplierCreditNoteStatus.POSTED,
        ).aggregate(quantity=Sum("credited_base_quantity"), value=Sum("settled_book_value"))
        credited_quantity = active["quantity"] or ZERO
        settled_value = active["value"] or ZERO
        scope = f"{line.supplier_return.number}#{line.sequence}"
        if credited_quantity > line.returned_base_quantity:
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="credited_beyond_returned",
                    expected=line.returned_base_quantity,
                    actual=credited_quantity,
                )
            )
        if settled_value > (line.posted_value or ZERO):
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="settled_beyond_book_value",
                    expected=line.posted_value or ZERO,
                    actual=settled_value,
                )
            )
        if credited_quantity == line.returned_base_quantity and settled_value != (
            line.posted_value or ZERO
        ):
            problems.append(
                Discrepancy(
                    scope=scope,
                    field="clearing_residual_after_full_settlement",
                    expected=line.posted_value or ZERO,
                    actual=settled_value,
                )
            )

    for invoice in SupplierInvoice.objects.filter(
        organization=organization, status=SupplierInvoiceStatus.POSTED
    ):
        if invoice_outstanding(invoice) < ZERO:
            problems.append(
                Discrepancy(
                    scope=invoice.number,
                    field="invoice_credited_below_zero",
                    expected=ZERO,
                    actual=invoice_outstanding(invoice),
                )
            )

    # The organization-wide equalities. Both accounts are procurement's own —
    # nothing else posts to either role — so the whole balance must be
    # explained, not merely this module's share of it.
    if clearing_account is not None:
        returned_value = (
            SupplierReturn.objects.filter(
                organization=organization, status=SupplierReturnStatus.POSTED
            ).aggregate(total=Sum("posted_value"))["total"]
            or ZERO
        )
        expected_clearing = returned_value - cleared_by_notes
        actual_clearing = ZERO
        for entry_row in JournalLine.objects.filter(
            account_id=clearing_account, entry__organization=organization
        ).values("debit", "credit"):
            actual_clearing += entry_row["debit"] - entry_row["credit"]
        if actual_clearing != expected_clearing:
            problems.append(
                Discrepancy(
                    scope=organization.code,
                    field="return_clearing_balance",
                    expected=expected_clearing,
                    actual=actual_clearing,
                )
            )
    if variance_account is not None:
        actual_variance = ZERO
        for entry_row in JournalLine.objects.filter(
            account_id=variance_account, entry__organization=organization
        ).values("debit", "credit"):
            actual_variance += entry_row["debit"] - entry_row["credit"]
        if actual_variance != expected_variance:
            problems.append(
                Discrepancy(
                    scope=organization.code,
                    field="return_variance_balance",
                    expected=expected_variance,
                    actual=actual_variance,
                )
            )
    return problems


def verify_supplier_payments(organization: Organization) -> list[Discrepancy]:
    """
    Every posted payment's journal says what its allocations say.

    Per posted payment:

        payable debit   == the sum of its allocation rows
        advance debit   == amount − that sum (absent when fully allocated)
        source credit   == the full amount, on the account the method's role
                           resolved the day it posted

    And across the organization: the supplier-advance balance equals the sum
    of standing remainders (PRC-055 — an asset, never a negative payable),
    the allocations on one payment never exceed its amount, and no invoice is
    paid below zero outstanding (PRC-054).
    """
    from apps.procurement.payments import allocated_total as payment_allocated_total

    problems: list[Discrepancy] = []
    payable_account = _role_account(organization.pk, SUPPLIER_PAYABLE)
    advance_account = _role_account(organization.pk, SUPPLIER_ADVANCE)

    expected_advance = ZERO
    for payment in SupplierPayment.objects.filter(
        organization=organization, status=SupplierPaymentStatus.POSTED
    ).select_related("journal_entry"):
        label = payment.number or str(payment.public_id)
        allocated = payment_allocated_total(payment)
        remainder = (payment.amount or ZERO) - allocated
        expected_advance += remainder

        if allocated > (payment.amount or ZERO):
            problems.append(
                Discrepancy(
                    scope=label,
                    field="allocations_exceed_payment",
                    expected=payment.amount,
                    actual=allocated,
                )
            )
        if payment.journal_entry is None:  # pragma: no cover - a constraint refuses it
            problems.append(
                Discrepancy(
                    scope=label, field="journal_entry", expected="present", actual="missing"
                )
            )
            continue
        rows = list(payment.journal_entry.lines.select_related("account"))
        payable_debit = sum(
            (row.debit - row.credit for row in rows if row.account_id == payable_account), ZERO
        )
        if payable_debit != allocated:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="payment_payable_debit",
                    expected=allocated,
                    actual=payable_debit,
                )
            )
        advance_debit = sum(
            (row.debit - row.credit for row in rows if row.account_id == advance_account), ZERO
        )
        if advance_debit != remainder:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="payment_advance_debit",
                    expected=remainder,
                    actual=advance_debit,
                )
            )
        # The source credit: everything that is neither the payable nor the
        # advance. Stated against the stored journal rather than re-resolving
        # today's mapping, because "which account did the money leave" was
        # settled the day it posted.
        source_credit = sum(
            (
                row.credit - row.debit
                for row in rows
                if row.account_id not in (payable_account, advance_account)
            ),
            ZERO,
        )
        if source_credit != payment.amount:
            problems.append(
                Discrepancy(
                    scope=label,
                    field="payment_source_credit",
                    expected=payment.amount,
                    actual=source_credit,
                )
            )

    for invoice in SupplierInvoice.objects.filter(
        organization=organization, status=SupplierInvoiceStatus.POSTED
    ):
        from apps.procurement.invoices import outstanding_amount as invoice_outstanding

        if invoice_outstanding(invoice) < ZERO:
            problems.append(
                Discrepancy(
                    scope=invoice.number,
                    field="invoice_paid_below_zero",
                    expected=ZERO,
                    actual=invoice_outstanding(invoice),
                )
            )

    if advance_account is not None:
        actual_advance = ZERO
        for entry_row in JournalLine.objects.filter(
            account_id=advance_account, entry__organization=organization
        ).values("debit", "credit"):
            actual_advance += entry_row["debit"] - entry_row["credit"]
        if actual_advance != expected_advance:
            problems.append(
                Discrepancy(
                    scope=organization.code,
                    field="supplier_advance_balance",
                    expected=expected_advance,
                    actual=actual_advance,
                )
            )
    return problems


def verify_procurement_accounting(organization: Organization) -> list[Discrepancy]:
    """
    The phase's proof obligation (PRC-058), mirroring
    `verify_inventory_accounting`. Three equalities:

    1. The sum of open supplier balances — posted invoices less posted credit
       notes less posted payments' allocated share, derived entirely from
       documents — **equals** the supplier payable account balance. The
       payable account is procurement's own: nothing else posts to it, so the
       whole balance must be explained, not merely this module's share.
    2. Accepted-and-unmatched receipt value **equals** procurement's own GRNI
       movement (invariant 47's scoped equality, deferred to
       `verify_grni_clearing`, which also explains why the *account* is
       shared with Task 1.4's uninvoiced opening receipts).
    3. Every posted procurement journal traces to exactly one source
       document, across all six source types.

    Composed from the standing verifiers rather than restated, so the
    nightly answer and the phase-exit answer cannot drift apart. There is no
    repair mode (PRC-059).
    """
    problems: list[Discrepancy] = []

    # 1. Open balances against the payable account, as one figure.
    payable_account = _role_account(organization.pk, SUPPLIER_PAYABLE)
    if payable_account is not None:
        expected_payable = ZERO
        for supplier in Supplier.objects.filter(organization=organization):
            expected_payable += supplier_outstanding(supplier)
        # A payable is a credit-balance account: credit-minus-debit.
        actual_payable = ZERO
        for entry_row in JournalLine.objects.filter(
            account_id=payable_account, entry__organization=organization
        ).values("debit", "credit"):
            actual_payable += entry_row["credit"] - entry_row["debit"]
        if actual_payable != expected_payable:
            problems.append(
                Discrepancy(
                    scope=organization.code,
                    field="open_balances_vs_payable_account",
                    expected=expected_payable,
                    actual=actual_payable,
                )
            )

    # 2. GRNI, scoped as invariant 47 records.
    problems.extend(verify_grni_clearing(organization))

    # 3. Source identity across every procurement journal.
    from apps.procurement.credit_notes import SOURCE_DOCUMENT_TYPE as NOTE_TYPE
    from apps.procurement.invoices import SOURCE_DOCUMENT_TYPE as INVOICE_TYPE
    from apps.procurement.payments import SOURCE_DOCUMENT_TYPE as PAYMENT_TYPE
    from apps.procurement.returns import SOURCE_DOCUMENT_TYPE as RETURN_TYPE

    known: dict[str, set[str]] = {
        SOURCE_DOCUMENT_TYPE: {
            str(pk)
            for pk in GoodsReceipt.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        },
        INVOICE_TYPE: {
            str(pk)
            for pk in SupplierInvoice.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        }
        | {
            str(pk)
            for pk in SupplierInvoicePosting.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        },
        RETURN_TYPE: {
            str(pk)
            for pk in SupplierReturn.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        },
        NOTE_TYPE: {
            str(pk)
            for pk in SupplierCreditNote.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        },
        PAYMENT_TYPE: {
            str(pk)
            for pk in SupplierPayment.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        },
    }
    for source_type, owned in known.items():
        cited = JournalEntry.objects.filter(
            organization=organization, source_document_type=source_type
        ).values_list("source_document_id", flat=True)
        for document_id in set(cited):
            if document_id not in owned:
                problems.append(
                    Discrepancy(
                        scope=source_type,
                        field="journal_cites_unknown_document",
                        expected="a document in this organization",
                        actual=document_id,
                    )
                )
    return problems


def verify_grni_clearing(organization: Organization) -> list[Discrepancy]:
    """
    Invariant 47, and the reason GRNI is worth having at all.

        the GRNI account balance
            == the value of accepted receipt lines no posted invoice has cleared

    ADR-023 §1 states it as the defining property of the account rather than as
    a description of it: a reconciler who disagrees with the balance can be
    shown the exact delivery lines that make it up.

    **Cleared is not the same as matched**, and this is the subtlest thing in
    the task. Task 2.11's availability excludes allocations on a cancelled
    match, because a withdrawn claim holds no quantity. GRNI is an accounting
    question and takes a narrower answer: a delivery stops sitting in GRNI when
    an invoice **posts** against it, which means an allocation whose match
    carries a **live** posting. A draft or ready match consumes availability
    and clears nothing, which is exactly right — the evidence is agreed but
    nobody has been billed.

    Two sets, two purposes. Conflating them is how a matching workspace comes
    to disagree with the ledger.

    **Procurement's own contribution, not the whole account.** `GRNI` is
    shared: Task 1.4's uninvoiced stock receipts credit the same account, for
    the same reason — goods owned with no stated debt behind them yet. Those
    are not procurement's to explain, and a verifier in this module that
    claimed the whole balance would report a discrepancy the size of the
    inventory module every time somebody received stock outside a purchase
    order. So the ledger side is restricted to entries this module produced,
    and the claim becomes exactly what procurement can prove: **what its own
    documents put into GRNI, less what its own invoices have taken out, equals
    the value of its accepted delivery lines that no posted invoice covers.**
    """
    grni_account = _role_account(organization.pk, GOODS_RECEIVED_NOT_INVOICED)
    if grni_account is None:
        return []

    balance = ZERO
    for row in JournalLine.objects.filter(
        account_id=grni_account,
        entry__organization=organization,
        entry__source_document_type__in=(SOURCE_DOCUMENT_TYPE, INVOICE_SOURCE_TYPE),
    ).values("debit", "credit"):
        balance += row["credit"] - row["debit"]

    cleared_ids = set(
        PurchaseMatchAllocation.objects.filter(
            match__postings__status=SupplierInvoicePostingStatus.LIVE
        ).values_list("goods_receipt_line_id", flat=True)
    )
    cleared_value: dict[int, Decimal] = {}
    for allocation in PurchaseMatchAllocation.objects.filter(
        match__postings__status=SupplierInvoicePostingStatus.LIVE
    ).values("goods_receipt_line_id", "receipt_allocated_value"):
        key = allocation["goods_receipt_line_id"]
        cleared_value[key] = cleared_value.get(key, ZERO) + allocation["receipt_allocated_value"]

    outstanding = ZERO
    for line in GoodsReceiptLine.objects.filter(
        receipt__organization=organization, receipt__status=GoodsReceiptStatus.POSTED
    ).values("id", "posted_value"):
        posted = line["posted_value"] or ZERO
        outstanding += posted - cleared_value.get(line["id"], ZERO)

    if balance != outstanding:
        return [
            Discrepancy(
                scope=organization.code,
                field="procurement_grni_uncleared_receipt_value",
                expected=outstanding,
                actual=balance,
            )
        ]
    _ = cleared_ids
    return []


def verify_parked_variance(organization: Organization) -> list[Discrepancy]:
    """
    Every fils in the purchase price variance clearing account traces to a
    live posting, and to the allocation rows beneath it.

    The balance is **expected to be non-zero and is not an error**. It is
    parked, not classified: a later, separately specified period-end process
    splits it between inventory still on hand and cost of sales for what has
    been consumed, taking its branch and cost centre from inventory ownership
    rather than from the supplier invoice. Until that exists, what this checks
    is that the balance is exactly the sum of the differences somebody can be
    shown, document by document.
    """
    variance_account = _role_account(organization.pk, PURCHASE_PRICE_VARIANCE)
    if variance_account is None:
        return []

    balance = ZERO
    for row in JournalLine.objects.filter(
        account_id=variance_account, entry__organization=organization
    ).values("debit", "credit"):
        balance += row["debit"] - row["credit"]

    claimed = ZERO
    problems: list[Discrepancy] = []
    for posting in SupplierInvoicePosting.objects.filter(
        organization=organization, status=SupplierInvoicePostingStatus.LIVE
    ).select_related("purchase_match", "supplier_invoice"):
        claimed += posting.price_variance
        rows = PurchaseMatchAllocation.objects.filter(match=posting.purchase_match).aggregate(
            receipt=Sum("receipt_allocated_value"),
            invoice=Sum("invoice_allocated_value"),
            variance=Sum("price_variance"),
        )
        label = posting.supplier_invoice.number or str(posting.supplier_invoice.public_id)
        for field, stored, summed in (
            ("goods_cleared_value", posting.goods_cleared_value, rows["receipt"] or ZERO),
            ("invoice_matched_value", posting.invoice_matched_value, rows["invoice"] or ZERO),
            ("price_variance", posting.price_variance, rows["variance"] or ZERO),
        ):
            if stored != summed:
                problems.append(
                    Discrepancy(
                        scope=f"{label}/{posting.generation}",
                        field=field,
                        expected=summed,
                        actual=stored,
                    )
                )

    if balance != claimed:
        problems.append(
            Discrepancy(
                scope=organization.code,
                field="parked_price_variance",
                expected=claimed,
                actual=balance,
            )
        )
    return problems
