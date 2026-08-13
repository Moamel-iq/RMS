"""
Supplier invoices and the payable they create.

An invoice is the third of the seven procurement events and the first that is
purely about money. The goods arrived on a different day, for a quantity the
receipt already recorded; this document says what the supplier is charging and
when it falls due.

    Dr  the account the charge belongs to
        Cr  SUPPLIER_PAYABLE

**It never touches stock** (PRC-038). Not a quantity, not a lot, not a
movement, not a valuation. A test counts `StockMovement` and
`StockLocationMovement` either side of a posting and asserts both are
unchanged, because "we did not mean to" is not an invariant.

**The supplier's balance is not stored.** `apps.procurement.selectors
.supplier_outstanding` derives it from posted invoices, and Tasks 2.14 and 2.15
will subtract credit and payment allocations from the same expression. A stored
balance would be a second source of truth and the second one always drifts.

## What posts here, and what deliberately does not

Task 2.0 §9 gives the invoice posting exactly one shape:

    Dr  GRNI                     the **matched receipt** value
    Dr  purchase price variance  the difference
        Cr  supplier payable     the invoiced value

Both debits depend on a match allocation, and matching is Task 2.11. So an
`ACCOUNT` line — a delivery charge, a repair, a subscription, anything that
never entered stock — has a complete route today and posts. An `INVENTORY`
line does not, and this module refuses to guess one.

Refusing matters more than it might look. Posting the *invoiced* amount to
GRNI would balance, and would be wrong: it would clear a variance nobody
computed, leave Task 2.12 nothing to recognise, and silently absorb into a
clearing account the exact difference that the three-way match exists to
surface (PRC-043). The invoice therefore approves, holds, and says why —
`SupplierInvoice.blocking_lines` is the list, `invoice_awaiting_matching` is
the error code, and Task 2.12 activates the path.

This is the same boundary discipline Task 2.8 held against Task 2.9. The
difference is that here the boundary runs *through* a document rather than
around one: the same invoice may carry postable and unpostable lines, and the
answer is that the whole document waits. Half-posting an invoice would create
a payable for part of what is owed.

## Lock order

    1. the invoice row                      select_for_update
    2. the organization's mapping lock       shared, advisory
    3. the invoice lines                     select_for_update, by sequence
    4. the procurement document-number counter
    5. the journal-number counter            inside post_entry

Step 2 sits above every row lock below it, which is the order ADR-019 §5
requires and the one `apps/procurement/posting.py` already documents.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    SUPPLIER_PAYABLE,
    Account,
    AccountClass,
    CostCenter,
    JournalEntry,
    JournalLine,
    OrganizationAccountMapping,
    SourceEvent,
)
from apps.accounting.services import (
    post_entry,
    resolve_default_account,
    resolve_period,
    reverse_entry,
)
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.allocation import AllocationItem, allocate
from apps.core.locks import lock_account_mappings_shared
from apps.core.models import AuditAction
from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import InventoryAccountMapping, InventoryItem
from apps.organizations.business_dates import business_date_for, resolve_business_day
from apps.organizations.models import Branch
from apps.procurement.lifecycle import lock_and_require_status
from apps.procurement.models import (
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrderLine,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceLineType,
    SupplierInvoiceStatus,
    SupplierItem,
)
from apps.procurement.services import next_document_number
from apps.users.models import User

ZERO = Decimal("0.000")

#: The canonical source type both the journal and the audit trail record.
SOURCE_DOCUMENT_TYPE = "PROCUREMENT_SUPPLIER_INVOICE"

#: Stamped on the journal so an entry always says which rule produced it.
POSTING_RULE = "procurement-supplier-invoice-v1"

DOCUMENT_TYPE = "SUPPLIER_INVOICE"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def normalize_invoice_number(value: str) -> str:
    """
    The comparison form of a supplier's invoice reference.

    Surrounding whitespace is meaningless and is removed, so `"INV-001"` and
    `" INV-001 "` are the same debt and cannot both be entered. Case is folded
    upward, so `"inv-001"` cannot become a second copy of the same claim
    either — Task 2.0 does not state a case rule, and duplicate payment is
    expensive enough that the stricter reading is the safe one. Both choices
    are recorded in the runbook as deliberate.

    What is **not** touched matters as much. Leading zeros survive, so
    `"INV-001"` and `"INV-0001"` stay different documents. Internal spacing
    survives, so `"INV 001"` is not silently merged with `"INV001"` — two
    suppliers' numbering habits are not ours to reconcile. And nothing is ever
    parsed as an integer: `"0042"` is a reference, not a number.
    """
    return value.strip().upper()


def due_date_for(*, invoice_date: datetime.date, payment_terms_days: int) -> datetime.date:
    """
    When an invoice falls due, from the terms that applied to it.

    A helper rather than a property, because the terms are a **snapshot**: the
    caller passes the number stored on the invoice, never `supplier
    .payment_terms_days`. Renegotiating a supplier's terms in March must not
    restate the due date of an invoice received in January.
    """
    return invoice_date + datetime.timedelta(days=payment_terms_days)


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def _require_draft(invoice: SupplierInvoice) -> SupplierInvoice:
    """Only a draft may be edited, re-read under a row lock."""
    return lock_and_require_status(
        SupplierInvoice,
        invoice.pk,
        {SupplierInvoiceStatus.DRAFT},
        code="invoice_not_editable",
        message=_("This invoice has been approved and can no longer be edited."),
    )


@transaction.atomic
def create_supplier_invoice(
    *,
    supplier: Supplier,
    branch: Branch,
    created_by: User,
    supplier_invoice_number: str,
    invoice_date: datetime.date,
    business_date: datetime.date | None = None,
    supplier_reference: str = "",
    freight_amount: Decimal | None = None,
    discount_amount: Decimal | None = None,
    notes: str = "",
) -> SupplierInvoice:
    """
    Open a draft invoice for what a supplier says is owed.

    The payment terms are copied from the supplier here and never read live
    again, and the due date is computed from that copy — the same snapshot rule
    the purchase order already follows, and the only way a due date computed
    later can be right.
    """
    if branch.organization_id != supplier.organization_id:
        raise ValidationError(
            _("The supplier belongs to another organization."), code="organization_mismatch"
        )
    reference = supplier_invoice_number.strip()
    if not reference:
        raise ValidationError(
            _("An invoice needs the supplier's own reference number."),
            code="supplier_invoice_number_required",
        )

    terms = supplier.payment_terms_days
    invoice = SupplierInvoice(
        organization=branch.organization,
        branch=branch,
        supplier=supplier,
        created_by=created_by,
        supplier_invoice_number=reference,
        supplier_invoice_number_key=normalize_invoice_number(reference),
        supplier_reference=supplier_reference.strip(),
        invoice_date=invoice_date,
        business_date=business_date or business_date_for(branch, timezone.now()),
        payment_terms_days=terms,
        due_date=due_date_for(invoice_date=invoice_date, payment_terms_days=terms),
        freight_amount=quantize_money(freight_amount or ZERO),
        discount_amount=quantize_money(discount_amount or ZERO),
        notes=notes.strip(),
    )
    invoice.full_clean(exclude=["supplier_invoice_number_key"])
    invoice.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=invoice,
        branch=branch,
        new_state=snapshot(invoice),
    )
    return invoice


@transaction.atomic
def update_supplier_invoice(
    *,
    invoice: SupplierInvoice,
    supplier_invoice_number: str | None = None,
    invoice_date: datetime.date | None = None,
    business_date: datetime.date | None = None,
    supplier_reference: str | None = None,
    freight_amount: Decimal | None = None,
    discount_amount: Decimal | None = None,
    notes: str | None = None,
) -> SupplierInvoice:
    """
    Correct a draft.

    The signature is the allowlist: `supplier` and `branch` are absent, because
    changing who an invoice is from, or which branch answers for it, is a
    different document rather than a correction of this one.
    """
    locked = _require_draft(invoice)
    previous = snapshot(locked)

    if supplier_invoice_number is not None:
        reference = supplier_invoice_number.strip()
        if not reference:
            raise ValidationError(
                _("An invoice needs the supplier's own reference number."),
                code="supplier_invoice_number_required",
            )
        locked.supplier_invoice_number = reference
        locked.supplier_invoice_number_key = normalize_invoice_number(reference)
    if invoice_date is not None:
        locked.invoice_date = invoice_date
        locked.due_date = due_date_for(
            invoice_date=invoice_date, payment_terms_days=locked.payment_terms_days
        )
    if business_date is not None:
        locked.business_date = business_date
    if supplier_reference is not None:
        locked.supplier_reference = supplier_reference.strip()
    if freight_amount is not None:
        locked.freight_amount = quantize_money(freight_amount)
    if discount_amount is not None:
        locked.discount_amount = quantize_money(discount_amount)
    if notes is not None:
        locked.notes = notes.strip()

    locked.full_clean(exclude=["supplier_invoice_number_key"])
    locked.save()
    _recalculate(locked)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_supplier_invoice(*, invoice: SupplierInvoice) -> None:
    """Drop a draft. A trigger refuses anything further along."""
    locked = _require_draft(invoice)
    previous = snapshot(locked)
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
    )
    locked.delete()


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


@transaction.atomic
def add_inventory_line(
    *,
    invoice: SupplierInvoice,
    item: InventoryItem,
    base_quantity: Decimal,
    unit_price: Decimal,
    supplier_item: SupplierItem | None = None,
    order_line: PurchaseOrderLine | None = None,
    receipt_line: GoodsReceiptLine | None = None,
    description: str = "",
    note: str = "",
) -> SupplierInvoiceLine:
    """
    Bill for goods.

    The order and receipt references are **evidence, not allocation**. Naming a
    receipt line here says "I believe this charge covers that delivery"; it does
    not consume the receipt's matchable remainder, does not mark anything
    matched, and does not clear any variance. Those are Task 2.11 records
    (PRC-040 – PRC-042), and creating them implicitly would make the matching
    screen a rubber stamp over decisions nobody took.
    """
    locked = _require_draft(invoice)
    if item.organization_id != locked.organization_id:
        raise ValidationError(
            _("The item belongs to another organization."), code="organization_mismatch"
        )
    if base_quantity <= 0:
        raise ValidationError(
            _("An invoiced quantity must be greater than zero."), code="quantity_not_positive"
        )
    if unit_price < 0:
        raise ValidationError(_("A price cannot be negative."), code="price_negative")

    order_version: int | None = None
    if order_line is not None:
        if order_line.order.organization_id != locked.organization_id:
            raise ValidationError(
                _("That order belongs to another organization."), code="organization_mismatch"
            )
        if order_line.order.supplier_id != locked.supplier_id:
            raise ValidationError(
                _("That order was placed with a different supplier."),
                code="order_supplier_mismatch",
            )
        if order_line.item_id != item.pk:
            raise ValidationError(
                _("The order line is for a different item."), code="order_line_item_mismatch"
            )
        order_version = order_line.order.version

    if receipt_line is not None:
        receipt = receipt_line.receipt
        if receipt.organization_id != locked.organization_id:
            raise ValidationError(
                _("That receipt belongs to another organization."), code="organization_mismatch"
            )
        if receipt.supplier_id != locked.supplier_id:
            raise ValidationError(
                _("That delivery came from a different supplier."),
                code="receipt_supplier_mismatch",
            )
        if receipt_line.item_id != item.pk:
            raise ValidationError(
                _("The receipt line is for a different item."), code="receipt_line_item_mismatch"
            )
        if receipt.status != GoodsReceiptStatus.POSTED:
            raise ValidationError(
                _(
                    "Delivery %(number)s is not posted. An invoice can only cite goods "
                    "that actually reached stock."
                ),
                code="receipt_not_posted",
                params={"number": receipt.number or str(receipt.public_id)},
            )
        if supplier_item is None:
            supplier_item = receipt_line.supplier_item

    if supplier_item is not None and supplier_item.supplier_id != locked.supplier_id:
        raise ValidationError(
            _("That catalogue row belongs to another supplier."), code="supplier_mismatch"
        )

    quantity = quantize_quantity(base_quantity)
    price = quantize_unit_price(unit_price)
    amount = quantize_money(base_quantity * unit_price)
    line = SupplierInvoiceLine(
        invoice=locked,
        sequence=_next_sequence(locked),
        line_type=SupplierInvoiceLineType.INVENTORY,
        item=item,
        supplier_item=supplier_item,
        order_line=order_line,
        order_version=order_version,
        receipt_line=receipt_line,
        base_quantity=quantity,
        description=description.strip() or item.name_ar,
        quantity=quantity,
        unit_price=price,
        line_amount=amount,
        net_amount=amount,
        note=note.strip(),
    )
    line.full_clean()
    line.save()
    _recalculate(locked)
    record_audit_event(
        action=AuditAction.CREATED, target=line, branch=locked.branch, new_state=snapshot(line)
    )
    return SupplierInvoiceLine.objects.get(pk=line.pk)


@transaction.atomic
def add_account_line(
    *,
    invoice: SupplierInvoice,
    account: Account,
    quantity: Decimal,
    unit_price: Decimal,
    description: str = "",
    cost_center: CostCenter | None = None,
    note: str = "",
) -> SupplierInvoiceLine:
    """
    Bill for something that never entered stock.

    The account is chosen by the person entering the invoice, which is not the
    thing PRC-034 forbids: that rule stops a *posting service* naming an
    account, and this one names none — `SUPPLIER_PAYABLE` still comes from an
    effective-dated role mapping. Which expense a delivery charge belongs to is
    a judgement only the person holding the document can make, and the
    alternative is a role per expense category, invented by us.

    What is constrained is the *shape* of the choice: the account must belong
    to this organization, be active and postable, and supply a cost centre
    where its own policy demands one.
    """
    locked = _require_draft(invoice)
    _validate_direct_account(
        organization_id=locked.organization_id, account=account, cost_center=cost_center
    )
    if quantity <= 0:
        raise ValidationError(
            _("A quantity must be greater than zero."), code="quantity_not_positive"
        )
    if unit_price < 0:
        raise ValidationError(_("A price cannot be negative."), code="price_negative")
    if not description.strip():
        raise ValidationError(
            _("A direct charge needs a description saying what it is for."),
            code="description_required",
        )

    amount = quantize_money(quantity * unit_price)
    line = SupplierInvoiceLine(
        invoice=locked,
        sequence=_next_sequence(locked),
        line_type=SupplierInvoiceLineType.ACCOUNT,
        account=account,
        cost_center=cost_center,
        description=description.strip(),
        quantity=quantize_quantity(quantity),
        unit_price=quantize_unit_price(unit_price),
        line_amount=amount,
        net_amount=amount,
        note=note.strip(),
    )
    line.full_clean()
    line.save()
    _recalculate(locked)
    record_audit_event(
        action=AuditAction.CREATED, target=line, branch=locked.branch, new_state=snapshot(line)
    )
    return SupplierInvoiceLine.objects.get(pk=line.pk)


#: The account classes a supplier may bill directly to.
#:
#: An expense (`COST_OF_SALES`, `OPERATING_EXPENSE`, `OTHER`) or a capitalised
#: purchase (`ASSET`) is a thing a supplier can charge for. A liability, an
#: equity account, revenue, a clearing account or a memo account is not: those
#: are where postings *land*, not what anybody sells. Selecting one would
#: produce an entry that balances and means nothing — `Dr` supplier payable
#: `Cr` supplier payable being the clearest example.
DIRECT_LINE_ACCOUNT_CLASSES = frozenset(
    {
        AccountClass.ASSET,
        AccountClass.COST_OF_SALES,
        AccountClass.OPERATING_EXPENSE,
        AccountClass.OTHER,
    }
)


def _refuse_role_owned_account(*, organization_id: int, account: Account) -> None:
    """
    An account a posting rule owns may not be hand-picked on an invoice line.

    The class check above is not enough on its own, and the gap it leaves is
    the dangerous one: `INVENTORY_CONTROL` is a perfectly ordinary `ASSET`, so
    without this an operator could bill a direct line straight into the
    inventory control account — inflating stock value with no stock behind it
    and breaking `verify_inventory_against_gl` in a way no procurement report
    would explain.

    GRNI is the second: hand-picking it would clear, by typing, exactly the
    balance Task 2.10 refuses to clear without a match. And supplier payable
    is the third, producing a balanced entry that says nothing.

    So the rule is stated structurally rather than as a list of codes: an
    account that is the target of any account-role mapping in this
    organization belongs to the posting rule that resolves it, and a person
    entering an invoice does not get to choose it (ADR-019).
    """
    mapping: OrganizationAccountMapping | InventoryAccountMapping | None = (
        OrganizationAccountMapping.objects.filter(organization_id=organization_id, account=account)
        .select_related("account_role")
        .first()
    )
    if mapping is None:
        mapping = (
            InventoryAccountMapping.objects.filter(organization_id=organization_id, account=account)
            .select_related("account_role")
            .first()
        )
    if mapping is not None:
        raise ValidationError(
            _(
                "Account %(code)s carries the %(role)s role and is written by a posting "
                "rule. An invoice line cannot name it directly."
            ),
            code="account_is_role_owned",
            params={"code": account.code, "role": mapping.account_role.code},
        )


def _validate_direct_account(
    *, organization_id: int, account: Account, cost_center: CostCenter | None
) -> None:
    """
    An account line may only name an account this organization can post to,
    and only one a supplier could plausibly be charging for.
    """
    if account.organization_id != organization_id:
        raise ValidationError(
            _("That account belongs to another organization."), code="organization_mismatch"
        )
    if not account.is_active:
        raise ValidationError(
            _("Account %(code)s is archived."),
            code="account_inactive",
            params={"code": account.code},
        )
    if not account.is_postable:
        raise ValidationError(
            _("Account %(code)s is a heading and takes no postings."),
            code="account_not_postable",
            params={"code": account.code},
        )
    if account.account_class not in DIRECT_LINE_ACCOUNT_CLASSES:
        raise ValidationError(
            _(
                "Account %(code)s is a %(kind)s account. A supplier bills for an expense "
                "or an asset, not for one of these."
            ),
            code="account_class_not_billable",
            params={"code": account.code, "kind": account.get_account_class_display()},
        )
    _refuse_role_owned_account(organization_id=organization_id, account=account)
    if account.requires_cost_center and cost_center is None:
        raise ValidationError(
            _("Account %(code)s requires a cost center."),
            code="cost_center_required",
            params={"code": account.code},
        )
    if cost_center is not None and cost_center.organization_id != organization_id:
        raise ValidationError(
            _("That cost center belongs to another organization."), code="organization_mismatch"
        )


@transaction.atomic
def remove_invoice_line(*, line: SupplierInvoiceLine) -> None:
    """Drop a line from a draft. Sequences are not renumbered."""
    locked_invoice = _require_draft(line.invoice)
    previous = snapshot(line)
    record_audit_event(
        action=AuditAction.DELETED,
        target=line,
        branch=locked_invoice.branch,
        previous_state=previous,
    )
    line.delete()
    _recalculate(locked_invoice)


def _next_sequence(invoice: SupplierInvoice) -> int:
    highest = (
        SupplierInvoiceLine.objects.filter(invoice=invoice)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    return (highest or 0) + 1


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def _recalculate(invoice: SupplierInvoice) -> SupplierInvoice:
    """
    Re-derive every stored total from the lines, and re-allocate the charges.

    Freight and discount are spread with `apps.core.allocation.allocate` —
    largest remainder over each line's explicit `sequence` — so the parts sum
    exactly to the whole, the answer does not depend on the order a queryset
    happened to return, and no residual is hidden in whichever line came last
    (PRC-039). Weighted by `line_amount`, because a delivery charge belongs
    proportionally to what was delivered.

    The document total is then the sum of the stored line net amounts. It is
    never computed a second, independent way — that is precisely how a total
    stops agreeing with its own lines (ADR-012).
    """
    lines = list(invoice.lines.order_by("sequence"))
    if not lines:
        invoice.lines_total = ZERO
        invoice.total_amount = quantize_money(invoice.freight_amount - invoice.discount_amount)
        invoice.save(update_fields=["lines_total", "total_amount", "updated_at"])
        return invoice

    lines_total = quantize_money(sum((line.line_amount for line in lines), start=ZERO))
    freight = _shares(lines, total=invoice.freight_amount)
    discount = _shares(lines, total=invoice.discount_amount)

    for line in lines:
        line.allocated_freight = freight[line.sequence]
        line.allocated_discount = discount[line.sequence]
        line.net_amount = quantize_money(
            line.line_amount + line.allocated_freight - line.allocated_discount
        )
        line.save(
            update_fields=["allocated_freight", "allocated_discount", "net_amount", "updated_at"]
        )

    invoice.lines_total = lines_total
    invoice.total_amount = quantize_money(sum((line.net_amount for line in lines), start=ZERO))
    invoice.save(update_fields=["lines_total", "total_amount", "updated_at"])
    return invoice


def _shares(lines: list[SupplierInvoiceLine], *, total: Decimal) -> dict[int, Decimal]:
    """
    One document-level charge, split across the lines by value.

    Where every line is zero-valued the weights would all be zero, which
    `allocate` refuses — correctly, since there is no proportion to divide by.
    The charge is then spread evenly by giving each line weight one, which is
    the only defensible answer and is deterministic for the same reason the
    weighted case is.
    """
    if total == ZERO:
        return {line.sequence: ZERO for line in lines}
    weights = [line.line_amount for line in lines]
    if all(weight == ZERO for weight in weights):
        weights = [Decimal("1") for _line in lines]
    results = allocate(
        total,
        [
            AllocationItem(sequence=line.sequence, weight=weight)
            for line, weight in zip(lines, weights, strict=True)
        ],
    )
    return {result.sequence: result.amount for result in results}


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


@transaction.atomic
def approve_supplier_invoice(*, invoice: SupplierInvoice, actor: User) -> SupplierInvoice:
    """
    Agree that the claim is real.

    Approval freezes the commercial terms: a trigger permits only posting or a
    return to draft from here, because an approval attached to a document that
    was edited afterwards records agreement to something nobody agreed to.
    """
    locked = lock_and_require_status(
        SupplierInvoice,
        invoice.pk,
        {SupplierInvoiceStatus.DRAFT},
        code="invoice_not_draft",
        message=_("Only a draft invoice can be approved."),
    )
    previous = snapshot(locked)
    lines = list(locked.lines.order_by("sequence"))
    if not lines:
        raise ValidationError(_("An empty invoice cannot be approved."), code="no_lines")
    _require_totals_agree(locked, lines)

    locked.status = SupplierInvoiceStatus.APPROVED
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    locked.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def return_supplier_invoice_to_draft(
    *, invoice: SupplierInvoice, actor: User, reason: str
) -> SupplierInvoice:
    """
    Send an approved invoice back for correction.

    The alternative to this is editing an approved document in place, which the
    trigger refuses. A reason is required because an approval being withdrawn
    is a fact somebody will ask about.
    """
    locked = lock_and_require_status(
        SupplierInvoice,
        invoice.pk,
        {SupplierInvoiceStatus.APPROVED},
        code="invoice_not_approved",
        message=_("Only an approved invoice can be returned to draft."),
    )
    if not reason.strip():
        raise ValidationError(_("Returning an invoice needs a reason."), code="reason_required")
    previous = snapshot(locked)
    locked.status = SupplierInvoiceStatus.DRAFT
    locked.approved_by = None
    locked.approved_at = None
    locked.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


def _require_totals_agree(invoice: SupplierInvoice, lines: list[SupplierInvoiceLine]) -> None:
    """
    The stored total is the sum of the stored line net amounts.

    Checked rather than assumed. `_recalculate` runs on every mutation, but an
    invoice is money and the cost of asserting it again here is one comparison.
    """
    expected = quantize_money(sum((line.net_amount for line in lines), start=ZERO))
    if invoice.total_amount != expected:
        raise ValidationError(  # pragma: no cover - _recalculate keeps these equal
            _("The invoice total %(total)s is not the sum of its lines %(lines)s."),
            code="total_disagrees_with_lines",
            params={"total": format(invoice.total_amount, "f"), "lines": format(expected, "f")},
        )
    if invoice.total_amount <= ZERO:
        raise ValidationError(
            _("An invoice for nothing cannot be approved."), code="total_not_positive"
        )


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@dataclass
class _Plan:
    """The accounts and amounts one posting resolved, before any of it exists."""

    #: line pk -> (account, cost centre, amount)
    lines: dict[int, tuple[Account, CostCenter | None, Decimal]]
    payable: Account
    payable_mapping: OrganizationAccountMapping
    total: Decimal


def _plan(invoice: SupplierInvoice, lines: list[SupplierInvoiceLine]) -> _Plan:
    """
    Resolve every account before a single effect exists.

    One missing mapping fails here — before a number, a journal line or a
    status change — so there is nothing partial to clean up (PRC-034, PRC-036).
    """
    payable_mapping = resolve_default_account(
        organization=invoice.organization,
        account_role=SUPPLIER_PAYABLE,
        on_date=invoice.business_date,
    )
    payable = payable_mapping.account
    if payable.requires_cost_center:
        raise ValidationError(
            _("Account %(code)s requires a cost center, which a payable cannot supply."),
            code="mapping_requires_cost_center",
            params={"code": payable.code},
        )

    resolved: dict[int, tuple[Account, CostCenter | None, Decimal]] = {}
    for line in lines:
        # `blocking_lines` already refused an INVENTORY line above; this is the
        # same fact stated where the account would have been chosen.
        assert line.account is not None  # noqa: S101 - the line-type constraint guarantees it
        _validate_direct_account(
            organization_id=invoice.organization_id,
            account=line.account,
            cost_center=line.cost_center,
        )
        resolved[line.pk] = (line.account, line.cost_center, line.net_amount)

    total = quantize_money(sum((amount for _a, _c, amount in resolved.values()), start=ZERO))
    if total <= ZERO:
        raise ValidationError(
            _("An invoice for nothing cannot be posted."), code="total_not_positive"
        )
    return _Plan(lines=resolved, payable=payable, payable_mapping=payable_mapping, total=total)


def _journal_lines(invoice: SupplierInvoice, *, plan: _Plan) -> list[PostingLine]:
    """
    One debit per distinct account and cost centre, one credit to the payable.

    Grouped, because two lines charging the same expense account are one debit
    to that account. The credit is single: the supplier is owed one amount,
    whatever it was made up of. Every figure is a sum of stored 3-decimal line
    values and the total is never rounded on its own.
    """
    debits: dict[tuple[int, int | None], Decimal] = {}
    accounts: dict[int, Account] = {}
    centers: dict[int, CostCenter] = {}

    for account, cost_center, amount in plan.lines.values():
        accounts[account.pk] = account
        if cost_center is not None:
            centers[cost_center.pk] = cost_center
        key = (account.pk, cost_center.pk if cost_center else None)
        debits[key] = debits.get(key, ZERO) + amount

    posting_lines = [
        PostingLine(
            account=accounts[account_id],
            branch=invoice.branch,
            cost_center=centers.get(center_id) if center_id is not None else None,
            debit=amount,
        )
        for (account_id, center_id), amount in sorted(
            debits.items(), key=lambda pair: (accounts[pair[0][0]].code, pair[0][1] or 0)
        )
    ]
    posting_lines.append(
        PostingLine(account=plan.payable, branch=invoice.branch, credit=plan.total)
    )
    return posting_lines


@transaction.atomic
def post_supplier_invoice(*, invoice: SupplierInvoice, actor: User) -> SupplierInvoice:
    """
    Post an approved invoice to the ledger, atomically.

    One transaction produces the status change, the gapless document number,
    the balanced journal, every line-level link between them, and the audit
    event — or none of it.

    The idempotency key is derived from the invoice's own `public_id` rather
    than accepted from the caller, for the reason `apps.procurement.posting`
    records: posting *this invoice* is the whole command, the invoice is the
    payload, and a posted invoice is frozen by a trigger — so a retry cannot
    present the same key with a different payload. The kernel still refuses a
    duplicate on the source identity independently.
    """
    # 1. The invoice row. The shared helper is inlined so posting can tell
    # "already posted" from "still a draft" from "reversed", which the helper
    # deliberately answers with one code.
    locked = SupplierInvoice.objects.select_for_update().get(pk=invoice.pk)
    if locked.status == SupplierInvoiceStatus.POSTED:
        raise ValidationError(_("This invoice is already posted."), code="already_posted")
    if locked.status == SupplierInvoiceStatus.REVERSED:
        raise ValidationError(
            _("A reversed invoice cannot be posted again. Record a new one."),
            code="invoice_reversed",
        )
    if locked.status != SupplierInvoiceStatus.APPROVED:
        raise ValidationError(
            _("An invoice must be approved before it can be posted."), code="invoice_not_approved"
        )

    lines = list(locked.lines.select_related("account", "cost_center", "item").order_by("sequence"))
    if not lines:  # pragma: no cover - approval refuses an empty invoice
        raise ValidationError(_("An empty invoice cannot be posted."), code="no_lines")
    _require_totals_agree(locked, lines)
    _require_every_line_has_a_route(locked, lines)

    day = resolve_business_day(locked.branch, timezone.now())
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = resolve_period(organization=locked.organization, accounting_date=locked.business_date)
    validate_period_accepts_postings(period)

    # 2. The organization's mappings, shared — above every row lock below it,
    # so a mapping mutation cannot interleave with the resolution in `_plan`.
    lock_account_mappings_shared(locked.organization_id)

    # 3. The lines, locked before their amounts are read into the journal.
    list(SupplierInvoiceLine.objects.select_for_update().filter(invoice=locked).order_by("pk"))

    plan = _plan(locked, lines)

    # 4. The gapless number, drawn only now that nothing can fail for a domain
    # reason — an abandoned attempt must not burn one.
    locked.number = next_document_number(
        organization=locked.organization,
        document_type=DOCUMENT_TYPE,
        year=period.fiscal_year.year,
    )

    # 5. The journal.
    journal = post_entry(
        organization=locked.organization,
        accounting_date=locked.business_date,
        lines=_journal_lines(locked, plan=plan),
        idempotency_key=f"procurement-supplier-invoice:{locked.public_id}",
        document_date=locked.invoice_date,
        narration=locked.notes or f"{locked.supplier.code} {locked.supplier_invoice_number}",
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE,
    )

    _link_lines(lines, plan=plan, journal=journal)

    locked.posted_amount = plan.total
    locked.journal_entry = journal
    locked.status = SupplierInvoiceStatus.POSTED
    locked.posted_by = actor
    locked.posted_at = timezone.now()
    locked.save(
        update_fields=[
            "business_date_timezone",
            "business_day_start",
            "number",
            "posted_amount",
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
            "posted_amount": format(plan.total, "f"),
        },
    )
    return locked


def _require_every_line_has_a_route(
    invoice: SupplierInvoice, lines: list[SupplierInvoiceLine]
) -> None:
    """
    Refuse to post an invoice any line of which has no complete accounting.

    Today that means any `INVENTORY` line: Task 2.0 §9 posts the **matched
    receipt value** to GRNI and the difference to purchase price variance, and
    both come from a Task 2.11 match allocation. Posting the invoiced amount to
    GRNI instead would balance and be wrong — it would clear a variance nobody
    computed and leave Task 2.12 nothing to recognise.

    The whole document waits, not the line. Posting half an invoice would
    create a payable for part of what is owed, which is a worse answer than
    creating none.
    """
    blocked = [line for line in lines if line.line_type == SupplierInvoiceLineType.INVENTORY]
    if not blocked:
        return
    raise ValidationError(
        _(
            "Lines %(lines)s bill for goods, and goods are cleared against the delivery "
            "they came from. Three-way matching decides how much of the receipt this "
            "invoice clears and where any difference belongs; until then the invoice "
            "stays approved."
        ),
        code="invoice_awaiting_matching",
        params={"lines": ", ".join(str(line.sequence) for line in blocked)},
    )


def _link_lines(lines: list[SupplierInvoiceLine], *, plan: _Plan, journal: JournalEntry) -> None:
    """
    Write the immutable traceability, while the lines are still unfrozen.

    The trigger permits exactly these two columns to change outside `DRAFT`,
    which is what keeps this window open long enough and no longer.
    """
    by_account: dict[tuple[int, int | None], JournalLine] = {
        (row.account_id, row.cost_center_id): row for row in journal.lines.filter(debit__gt=ZERO)
    }
    for line in lines:
        account, cost_center, _amount = plan.lines[line.pk]
        line.journal_line = by_account[(account.pk, cost_center.pk if cost_center else None)]
        line.resolved_organization_mapping = plan.payable_mapping
        line.save(update_fields=["journal_line", "resolved_organization_mapping", "updated_at"])


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_supplier_invoice(
    *, invoice: SupplierInvoice, actor: User, reason: str
) -> SupplierInvoice:
    """
    Take back a posted invoice — the whole document, never a line.

    The mirror is exact: the reversing journal mirrors the original lines,
    whatever the mappings have since become. Nothing is re-resolved, because
    the question "what did this invoice do" has one answer and it was settled
    the day it posted. Only the date is current, since undoing something is an
    event that happens now.

    The payable falls back to zero as a consequence rather than as a separate
    write — there is no stored balance to correct, which is the point of
    deriving it.
    """
    locked = SupplierInvoice.objects.select_for_update().get(pk=invoice.pk)
    if locked.status == SupplierInvoiceStatus.REVERSED:
        raise ValidationError(_("This invoice is already reversed."), code="already_reversed")
    if locked.status != SupplierInvoiceStatus.POSTED:
        raise ValidationError(
            _("Only a posted invoice can be reversed."), code="invoice_not_posted"
        )
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")

    _require_no_downstream_dependency(locked)

    now = timezone.now()
    reversal_business_date = resolve_business_day(locked.branch, now).business_date
    assert locked.journal_entry is not None  # noqa: S101 - a constraint guarantees it

    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"procurement-supplier-invoice-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )

    locked.status = SupplierInvoiceStatus.REVERSED
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


def _require_no_downstream_dependency(invoice: SupplierInvoice) -> None:
    """
    Nothing later may already depend on this invoice.

    Tasks 2.11, 2.14 and 2.15 attach match allocations, credit-note
    allocations and payment allocations to an invoice line, and each takes a
    value from it. Reversing underneath one would leave it citing a debt that
    no longer exists.

    None of those models exist yet, so this walks the relations that **do** —
    written as a loop over related accessors rather than a list of imports, so
    a later task that adds `payment_allocations` to `SupplierInvoiceLine` gets
    the guard by declaring the relation rather than by remembering this
    function. A guard that reports zero rows today is not evidence that none
    will ever exist; it is the extension point holding the line until one does.
    """
    ignored = {"history"}
    for line in invoice.lines.all():
        for relation in line._meta.related_objects:
            name = relation.get_accessor_name()
            if not name or name in ignored:
                continue
            related = getattr(line, name, None)
            if related is None or not hasattr(related, "exists"):
                continue
            if related.exists():
                raise ValidationError(
                    _(
                        "Another document (%(relation)s) already depends on this invoice. "
                        "Reverse it first."
                    ),
                    code="invoice_has_dependents",
                    params={"relation": name},
                )


# ---------------------------------------------------------------------------
# The payable, derived
# ---------------------------------------------------------------------------


def outstanding_amount(invoice: SupplierInvoice) -> Decimal:
    """
    What is still owed on one invoice.

    Today it is the posted amount, because nothing yet reduces it. The shape is
    what matters: Task 2.14 subtracts active credit allocations and Task 2.15
    subtracts payment allocations from this one expression, and neither has to
    find and correct a stored balance to do it.
    """
    if invoice.status != SupplierInvoiceStatus.POSTED:
        return ZERO
    return invoice.posted_amount or ZERO


def supplier_outstanding(supplier: Supplier) -> Decimal:
    """Everything still owed to one supplier, derived from posted documents."""
    total: Decimal | None = SupplierInvoice.objects.filter(
        supplier=supplier, status=SupplierInvoiceStatus.POSTED
    ).aggregate(total=Sum("posted_amount"))["total"]
    return total or ZERO


def invoice_timeline(invoice: SupplierInvoice) -> list[dict[str, Any]]:
    """The dated facts about one invoice, oldest first, for the detail screen."""
    events: list[dict[str, Any]] = [
        {"label": _("سُجّلت"), "at": invoice.created_at, "who": invoice.created_by.username}
    ]
    if invoice.approved_at is not None:
        events.append(
            {
                "label": _("اعتُمدت"),
                "at": invoice.approved_at,
                "who": invoice.approved_by.username if invoice.approved_by else "",
            }
        )
    if invoice.posted_at is not None:
        events.append(
            {
                "label": _("رُحّلت"),
                "at": invoice.posted_at,
                "who": invoice.posted_by.username if invoice.posted_by else "",
            }
        )
    if invoice.reversed_at is not None:
        events.append(
            {
                "label": _("عُكست"),
                "at": invoice.reversed_at,
                "who": invoice.reversed_by.username if invoice.reversed_by else "",
                "note": invoice.reversal_reason,
            }
        )
    return events
