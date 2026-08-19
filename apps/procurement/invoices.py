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

## What posts, and in what shape

Task 2.0 §9 gives the invoice posting one shape, and as of Task 2.12 the whole
of it is live:

    Dr  each direct charge account                     A
    Dr  GRNI, per account the deliveries credited      R
    Dr  purchase price variance clearing               D    (Cr if negative)
        Cr  supplier payable                           A + V

`R` is what the deliveries posted, `V` is what the supplier charges for the
same goods, and `D = V - R` is the difference — parked in a clearing account,
not classified as cost of sales, because whether it belongs to stock still on
hand or to what has been consumed is a question no supplier invoice can answer
(ADR-022, amended at Task 2.12).

An `ACCOUNT` line — a delivery charge, a repair, a subscription — takes the
first debit and needs no match. An `INVENTORY` line takes the second and third
and needs a `READY` match covering it in full. **The whole document waits or
none of it does**: half-posting an invoice would create a payable for part of
what is owed, which is a worse answer than creating none.

An invoice that agrees with its delivery produces a clean two-line entry. The
variance line is absent rather than zero — the kernel refuses a zero-value line
and it would be noise even if it did not.

**It never touches stock**, and Task 2.12 does not change that. A price
difference does not restate a posted movement (PRC-043): the moving average is
a function of posting order, and repricing a receipt would restate every issue
that followed it, including issues in closed periods. Carrying the difference
into inventory value where the stock is still on hand (PRC-044) is a separate,
permissioned act that is **deferred and not elected** — it needs a permission,
a source identity and an allocation policy that no approved document defines.

## Generations

Each posting is a `SupplierInvoicePosting` generation, and the journal's source
identity is the generation's `public_id` rather than the invoice's. An invoice
may legitimately reach the ledger twice — posted, reversed because the match
was wrong, posted again from a corrected match — and keying on the invoice
would make the second entry look like a retry of the first.

## Lock order

    1. the active purchase match           select_for_update
    2. the invoice row                     select_for_update
    3. the organization's mapping lock     shared, advisory
    4. the invoice lines                   select_for_update, by sequence
    5. the procurement document-number counter
    6. the journal-number counter          inside post_entry

The match sits **above** the invoice even though the command is spelled
`post_supplier_invoice(invoice=...)`. Task 2.11 locks match-then-invoice in
both paths that hold both, so taking them the other way here would give two
services opposite orders on the same pair of rows. Step 3 sits above every row
lock below it, which is the order ADR-019 §5 requires and the one
`apps/procurement/posting.py` already documents.
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
    PURCHASE_PRICE_VARIANCE,
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
from apps.procurement.credit_terms import resolve_credit_term, term_name_ar
from apps.procurement.lifecycle import lock_and_require_status
from apps.procurement.matching import cancel_purchase_match
from apps.procurement.models import (
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseMatch,
    PurchaseMatchAllocation,
    PurchaseMatchStatus,
    PurchaseOrderLine,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceLineType,
    SupplierInvoicePosting,
    SupplierInvoicePostingStatus,
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
    currency_code: str = "IQD",
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

    credit_term = resolve_credit_term(supplier=supplier, on=invoice_date)
    terms = credit_term.net_days if credit_term is not None else supplier.payment_terms_days
    invoice = SupplierInvoice(
        organization=branch.organization,
        branch=branch,
        supplier=supplier,
        created_by=created_by,
        supplier_invoice_number=reference,
        supplier_invoice_number_key=normalize_invoice_number(reference),
        supplier_reference=supplier_reference.strip(),
        currency_code=currency_code.strip().upper(),
        invoice_date=invoice_date,
        business_date=business_date or business_date_for(branch, timezone.now()),
        payment_terms_days=terms,
        due_date=due_date_for(invoice_date=invoice_date, payment_terms_days=terms),
        credit_term=credit_term,
        credit_term_public_id=credit_term.public_id if credit_term is not None else None,
        credit_term_version=credit_term.version if credit_term is not None else None,
        credit_term_name=(credit_term.name_ar if credit_term is not None else term_name_ar(terms)),
        credit_term_net_days=terms,
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
    currency_code: str | None = None,
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
    if currency_code is not None:
        locked.currency_code = currency_code.strip().upper()
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

    # Approval, not draft creation, is the commercial decision boundary. A
    # term negotiated while the invoice waited in draft is therefore resolved
    # once here and copied in full. Later supplier changes cannot move this due
    # date or rewrite this evidence.
    credit_term = resolve_credit_term(supplier=locked.supplier, on=locked.invoice_date)
    if credit_term is None:
        terms = locked.supplier.payment_terms_days
        locked.credit_term = None
        locked.credit_term_public_id = None
        locked.credit_term_version = None
        locked.credit_term_name = term_name_ar(terms)
    else:
        terms = credit_term.net_days
        locked.credit_term = credit_term
        locked.credit_term_public_id = credit_term.public_id
        locked.credit_term_version = credit_term.version
        locked.credit_term_name = credit_term.name_ar
    locked.credit_term_net_days = terms
    locked.payment_terms_days = terms
    locked.due_date = due_date_for(
        invoice_date=locked.invoice_date,
        payment_terms_days=terms,
    )

    locked.status = SupplierInvoiceStatus.APPROVED
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    locked.save(
        update_fields=[
            "credit_term",
            "credit_term_public_id",
            "credit_term_version",
            "credit_term_name",
            "credit_term_net_days",
            "payment_terms_days",
            "due_date",
            "status",
            "approved_by",
            "approved_at",
            "updated_at",
        ]
    )
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

    **Not while a match stands against it.** Back in `DRAFT` the line freeze
    lifts and `_recalculate` rewrites every line's `net_amount` — and it does
    so even if nobody touches the matched line, because changing the freight or
    the discount re-runs the allocation across all of them. A frozen allocation
    would then be citing an invoiced value the line no longer states, and Task
    2.12 would clear GRNI against a figure nobody agreed. Withdraw the match
    first; that is what it is for.
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
    standing = (
        PurchaseMatch.objects.filter(supplier_invoice=locked)
        .exclude(status=PurchaseMatchStatus.CANCELLED)
        .values_list("number", "public_id")
        .first()
    )
    if standing is not None:
        raise ValidationError(
            _(
                "Match %(match)s stands against this invoice. Returning it to draft would "
                "let its amounts move underneath frozen allocations. Withdraw the match "
                "first."
            ),
            code="invoice_has_an_active_match",
            params={"match": standing[0] or str(standing[1])},
        )
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

    #: Direct-charge lines only: line pk -> (account, cost centre, amount).
    lines: dict[int, tuple[Account, CostCenter | None, Decimal]]
    payable: Account
    payable_mapping: OrganizationAccountMapping
    #: `A` — what the direct charges come to.
    total: Decimal

    #: The agreed evidence, and the rows beneath it. `None` on an invoice with
    #: no goods on it, which needs no match and never had one.
    match: PurchaseMatch | None = None
    allocations: list[PurchaseMatchAllocation] = field(default_factory=list)

    #: `R`, split by the account each delivery actually credited. Grouped by
    #: account rather than summed into one figure because a mapping
    #: supersession between two receipts leaves two GRNI accounts, and each
    #: must clear to exactly zero on its own.
    grni: dict[int, Decimal] = field(default_factory=dict)
    grni_accounts: dict[int, Account] = field(default_factory=dict)
    goods_cleared: Decimal = ZERO
    #: `V` — what the supplier charges for the same goods.
    invoice_matched: Decimal = ZERO
    #: `D = V - R`, signed.
    variance: Decimal = ZERO
    variance_account: Account | None = None

    @property
    def payable_total(self) -> Decimal:
        """`A + V`. The whole invoice, never a re-derivation of its parts."""
        return quantize_money(self.total + self.invoice_matched)


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
        if line.line_type != SupplierInvoiceLineType.ACCOUNT:
            continue
        assert line.account is not None  # noqa: S101 - the line-type constraint guarantees it
        _validate_direct_account(
            organization_id=invoice.organization_id,
            account=line.account,
            cost_center=line.cost_center,
        )
        resolved[line.pk] = (line.account, line.cost_center, line.net_amount)

    total = quantize_money(sum((amount for _a, _c, amount in resolved.values()), start=ZERO))
    plan = _Plan(lines=resolved, payable=payable, payable_mapping=payable_mapping, total=total)

    goods_lines = [line for line in lines if line.line_type == SupplierInvoiceLineType.INVENTORY]
    if goods_lines:
        _plan_the_goods(invoice, goods_lines, plan=plan)

    if plan.payable_total <= ZERO:
        raise ValidationError(
            _("An invoice for nothing cannot be posted."), code="total_not_positive"
        )
    return plan


def _plan_the_goods(
    invoice: SupplierInvoice, goods_lines: list[SupplierInvoiceLine], *, plan: _Plan
) -> None:
    """
    Resolve what the deliveries parked, what the supplier charges for it, and
    the difference — from the agreed match and from nothing else.

    Task 2.0 §9's entry needs both figures, which is why Task 2.11 stores them
    separately per allocation. This adds nothing to that arithmetic: it groups
    rows the database has already asserted are internally consistent.

    **The GRNI debit is the account each delivery actually credited**, read
    from `GoodsReceiptLine.contra_account`, not re-resolved from the role at
    today's date. That is deliberate and it is not a PRC-034 violation: the
    account was itself role-resolved when the receipt posted, and ADR-019's
    rule is that a service must not *name* an account, not that it must
    re-derive one it already recorded. Re-resolving would be actively wrong —
    a mapping superseded between the receipt and the invoice would debit an
    account the delivery never credited, leaving one GRNI account permanently
    debited and another permanently credited, and invariant 47 false for every
    receipt that straddled the change. Task 2.9 records the same reasoning for
    its own reversal: "Nothing is re-resolved."
    """
    match = (
        PurchaseMatch.objects.filter(supplier_invoice=invoice)
        .exclude(status=PurchaseMatchStatus.CANCELLED)
        .select_for_update()
        .first()
    )
    if match is None:
        raise ValidationError(
            _(
                "Lines %(lines)s bill for goods, and goods are cleared against the delivery "
                "they came from. Open a match, allocate the deliveries this invoice covers, "
                "and mark it ready."
            ),
            code="invoice_awaiting_matching",
            params={"lines": ", ".join(str(line.sequence) for line in goods_lines)},
        )
    if match.status != PurchaseMatchStatus.READY:
        raise ValidationError(
            _(
                "Match %(match)s is still a draft. Posting from an unfrozen match would "
                "post evidence somebody is still editing."
            ),
            code="match_not_ready",
            params={"match": match.number or str(match.public_id)},
        )

    allocations = list(
        match.allocations.select_related(
            "supplier_invoice_line", "goods_receipt_line", "goods_receipt_line__receipt"
        ).order_by("sequence")
    )
    if not allocations:  # pragma: no cover - readiness refuses an empty match
        raise ValidationError(
            _("A match with no allocations claims nothing."), code="no_allocations"
        )

    covered: dict[int, Decimal] = {}
    for allocation in allocations:
        covered[allocation.supplier_invoice_line_id] = (
            covered.get(allocation.supplier_invoice_line_id, ZERO)
            + allocation.matched_base_quantity
        )

    short = [
        line for line in goods_lines if covered.get(line.pk, ZERO) != (line.base_quantity or ZERO)
    ]
    if short:
        raise ValidationError(
            _(
                "Lines %(lines)s are only partly matched. A payable is created for the whole "
                "document or for none of it, so the invoice waits until every goods line is "
                "fully covered."
            ),
            code="invoice_partly_matched",
            params={"lines": ", ".join(str(line.sequence) for line in short)},
        )

    for allocation in allocations:
        receipt_line = allocation.goods_receipt_line
        grni = receipt_line.contra_account
        if grni is None:  # pragma: no cover - a posted receipt line always has one
            raise ValidationError(
                _("Delivery line %(id)s recorded no GRNI account."),
                code="receipt_line_has_no_grni",
                params={"id": receipt_line.pk},
            )
        plan.grni_accounts[grni.pk] = grni
        plan.grni[grni.pk] = plan.grni.get(grni.pk, ZERO) + allocation.receipt_allocated_value

    plan.match = match
    plan.allocations = allocations
    plan.goods_cleared = quantize_money(
        sum((row.receipt_allocated_value for row in allocations), start=ZERO)
    )
    plan.invoice_matched = quantize_money(
        sum((row.invoice_allocated_value for row in allocations), start=ZERO)
    )
    plan.variance = quantize_money(plan.invoice_matched - plan.goods_cleared)

    # The variance account is resolved only when there is a variance. A role
    # nobody has mapped must not block an invoice that agrees with its
    # delivery, which is the common case.
    if plan.variance != ZERO:
        plan.variance_account = resolve_default_account(
            organization=invoice.organization,
            account_role=PURCHASE_PRICE_VARIANCE,
            on_date=invoice.business_date,
        ).account
        if plan.variance_account.requires_cost_center:  # pragma: no cover - a clearing account
            raise ValidationError(
                _(
                    "Account %(code)s requires a cost center, which a purchase price "
                    "difference cannot supply."
                ),
                code="mapping_requires_cost_center",
                params={"code": plan.variance_account.code},
            )


def _journal_lines(invoice: SupplierInvoice, *, plan: _Plan) -> list[PostingLine]:
    """
    One debit per distinct account and cost centre, the goods side of the
    match, the difference, and one credit to the payable.

        Dr  each direct charge account                     A
        Dr  GRNI, per account the deliveries credited      R
        Dr  purchase price variance clearing               D    (Cr if negative)
            Cr  supplier payable                           A + V

    where `D = V - R`, so debits less credits is `A + R + D - (A + V)`, which
    is `R + (V - R) - V`, which is zero. Balanced by construction rather than
    by a reconciliation afterwards.

    Grouped, because two lines charging the same expense account are one debit
    to that account. The credit is single: the supplier is owed one amount,
    whatever it was made up of. Every figure is a sum of stored 3-decimal
    values and the total is never rounded on its own — in particular the
    payable is `A + V` from the stored line and allocation figures, never
    `A + R + D`, which would be the same number arrived at by a route that can
    drift.
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
    # The goods side: what each delivery parked in GRNI, cleared per account.
    for account_id, amount in sorted(
        plan.grni.items(), key=lambda pair: plan.grni_accounts[pair[0]].code
    ):
        posting_lines.append(
            PostingLine(account=plan.grni_accounts[account_id], branch=invoice.branch, debit=amount)
        )

    # The difference, on whichever side it falls — and **absent** when there is
    # none. Flip the side, never negate the amount: the kernel refuses a
    # negative figure and tells you to use the other side, and a zero-value
    # third line is refused outright. An invoice that agrees with its delivery
    # is the common case and must produce a clean two-line entry.
    if plan.variance != ZERO:
        assert plan.variance_account is not None  # noqa: S101 - resolved with the variance
        if plan.variance > ZERO:
            posting_lines.append(
                PostingLine(
                    account=plan.variance_account, branch=invoice.branch, debit=plan.variance
                )
            )
        else:
            posting_lines.append(
                PostingLine(
                    account=plan.variance_account, branch=invoice.branch, credit=-plan.variance
                )
            )

    posting_lines.append(
        PostingLine(account=plan.payable, branch=invoice.branch, credit=plan.payable_total)
    )
    return posting_lines


@transaction.atomic
def post_supplier_invoice(*, invoice: SupplierInvoice, actor: User) -> SupplierInvoice:
    """
    Post an approved invoice to the ledger, atomically.

    One transaction produces the posting record, the status change, the gapless
    document number, the balanced journal, every line-level link between them,
    and the audit event — or none of it.

    ## Generations

    An invoice may legitimately reach the ledger more than once: posted,
    reversed because the match was wrong, then posted again from a corrected
    match. Each of those is a `SupplierInvoicePosting` **generation**, and the
    journal's source identity is the *generation's* `public_id`, not the
    invoice's. Keying on the invoice would make the second posting look like a
    retry of the first, and the kernel would refuse it (ADR-017).

    Re-posting is permitted only from `REVERSED`, only when no live generation
    exists, and only against a new `READY` match. The invoice's own terms are
    unchanged throughout — the trigger enforces that — so a wrong *invoice* is
    still corrected by reversing it and raising a replacement document, never
    by editing this one.

    ## Locking

    The match header is taken **above** the invoice row, always, even though
    the command is spelled `post_supplier_invoice(invoice=...)`. Task 2.11's
    `add_allocation` and `mark_match_ready` both lock match-then-invoice, so
    taking them the other way here would give two services opposite orders on
    the same pair of rows and deadlock the moment somebody freezes a match
    while somebody else posts it.
    """
    # 1. The match, above everything. Resolved by query rather than by argument
    # precisely because the caller named the invoice.
    match = (
        PurchaseMatch.objects.select_for_update()
        .filter(supplier_invoice_id=invoice.pk)
        .exclude(status=PurchaseMatchStatus.CANCELLED)
        .first()
    )

    # 2. The invoice row. The shared helper is inlined so posting can tell
    # "already posted" from "still a draft" from "reversed", which the helper
    # deliberately answers with one code.
    locked = SupplierInvoice.objects.select_for_update().get(pk=invoice.pk)
    if locked.status == SupplierInvoiceStatus.POSTED:
        raise ValidationError(_("This invoice is already posted."), code="already_posted")
    if locked.status == SupplierInvoiceStatus.DRAFT:
        raise ValidationError(
            _("An invoice must be approved before it can be posted."), code="invoice_not_approved"
        )
    if locked.status == SupplierInvoiceStatus.REVERSED:
        _require_repostable(locked, match=match)

    lines = list(locked.lines.select_related("account", "cost_center", "item").order_by("sequence"))
    if not lines:  # pragma: no cover - approval refuses an empty invoice
        raise ValidationError(_("An empty invoice cannot be posted."), code="no_lines")
    _require_totals_agree(locked, lines)

    day = resolve_business_day(locked.branch, timezone.now())
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = resolve_period(organization=locked.organization, accounting_date=locked.business_date)
    validate_period_accepts_postings(period)

    # 3. The organization's mappings, shared — above every row lock below it,
    # so a mapping mutation cannot interleave with the resolution in `_plan`.
    lock_account_mappings_shared(locked.organization_id)

    # 4. The lines, locked before their amounts are read into the journal.
    list(SupplierInvoiceLine.objects.select_for_update().filter(invoice=locked).order_by("pk"))

    plan = _plan(locked, lines)

    # 5. The gapless number, drawn only now that nothing can fail for a domain
    # reason — an abandoned attempt must not burn one. A re-post keeps the
    # number it already drew: it is the same document reaching the ledger
    # again, and burning a second number would leave a gap an auditor reads as
    # a missing invoice.
    if not locked.number:
        locked.number = next_document_number(
            organization=locked.organization,
            document_type=DOCUMENT_TYPE,
            year=period.fiscal_year.year,
        )

    posted_at = timezone.now()
    posting = _new_posting(locked, plan=plan, actor=actor, posted_at=posted_at)

    # 6. The journal, named by the generation rather than by the invoice. The
    # posting's `public_id` exists before the row does, which is what lets the
    # journal carry it and the row cite the journal without either waiting on
    # the other.
    journal = post_entry(
        organization=locked.organization,
        accounting_date=locked.business_date,
        lines=_journal_lines(locked, plan=plan),
        idempotency_key=f"procurement-supplier-invoice:{posting.public_id}",
        document_date=locked.invoice_date,
        narration=locked.notes or f"{locked.supplier.code} {locked.supplier_invoice_number}",
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(posting.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE,
    )
    posting.journal_entry = journal
    posting.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=posting,
        branch=locked.branch,
        new_state=snapshot(posting),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(posting.public_id),
        metadata={"generation": posting.generation, "journal_entry": journal.entry_number},
    )

    _link_lines(lines, plan=plan, journal=journal)

    locked.posted_amount = plan.payable_total
    locked.journal_entry = journal
    locked.status = SupplierInvoiceStatus.POSTED
    locked.posted_by = actor
    locked.posted_at = posted_at
    # A re-post is not a reversed invoice any more. The reversal it is
    # recovering from is not lost: generation N carries it, which is the whole
    # reason generations exist.
    locked.reversed_by = None
    locked.reversed_at = None
    locked.reversal_reason = ""
    locked.reversal_journal_entry = None
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
            "reversed_by",
            "reversed_at",
            "reversal_reason",
            "reversal_journal_entry",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(locked),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(posting.public_id),
        metadata={
            "number": locked.number,
            "generation": posting.generation,
            "journal_entry": journal.entry_number,
            "line_count": len(lines),
            "posted_amount": format(plan.payable_total, "f"),
            "goods_cleared": format(plan.goods_cleared, "f"),
            "price_variance": format(plan.variance, "f"),
        },
    )
    return locked


def _require_repostable(invoice: SupplierInvoice, *, match: PurchaseMatch | None) -> None:
    """
    A reversed invoice may reach the ledger again, but only under conditions
    that make it a *correction* rather than a second debt.

    The previous generation must be fully reversed, no generation may be live,
    and there must be a new `READY` match. The invoice's own terms cannot have
    changed — the trigger refuses any edit to a reversed invoice — so what is
    being corrected is always the *evidence*, never the claim. A wrong claim is
    corrected by raising a replacement invoice, which is a different document
    with its own supplier reference.
    """
    live = SupplierInvoicePosting.objects.filter(
        supplier_invoice=invoice, status=SupplierInvoicePostingStatus.LIVE
    ).exists()
    if live:  # pragma: no cover - the invoice would not be REVERSED
        raise ValidationError(
            _("A live posting still stands on this invoice."), code="posting_already_live"
        )
    if match is None or match.status != PurchaseMatchStatus.READY:
        raise ValidationError(
            _(
                "This invoice was reversed. Posting it again needs a new match, allocated "
                "and marked ready; if the invoice itself was wrong, raise a replacement "
                "document instead."
            ),
            code="repost_needs_a_new_match",
        )


def _new_posting(
    invoice: SupplierInvoice, *, plan: _Plan, actor: User, posted_at: datetime.datetime
) -> SupplierInvoicePosting:
    """
    Build the next generation, unsaved.

    Unsaved because its `journal_entry` is not nullable and the journal does
    not exist yet — but its `public_id` does, assigned here, which is what the
    journal will carry as its source identity.

    The generation number is the highest so far plus one, counted over every
    generation including reversed ones. A reversed generation is not reused:
    the ledger has two entries and each names its own.
    """
    highest = (
        SupplierInvoicePosting.objects.filter(supplier_invoice=invoice)
        .order_by("-generation")
        .values_list("generation", flat=True)
        .first()
    )
    return SupplierInvoicePosting(
        organization=invoice.organization,
        supplier_invoice=invoice,
        purchase_match=plan.match,
        generation=(highest or 0) + 1,
        status=SupplierInvoicePostingStatus.LIVE,
        allocation_fingerprint=plan.match.allocation_fingerprint if plan.match else "",
        goods_cleared_value=plan.goods_cleared,
        invoice_matched_value=plan.invoice_matched,
        price_variance=plan.variance,
        direct_charge_value=plan.total,
        payable_value=plan.payable_total,
        posted_by=actor,
        posted_at=posted_at,
    )


def _link_lines(lines: list[SupplierInvoiceLine], *, plan: _Plan, journal: JournalEntry) -> None:
    """
    Write the immutable traceability, while the lines are still unfrozen.

    The trigger permits exactly these two columns to change outside `DRAFT`,
    which is what keeps this window open long enough and no longer.

    A direct charge points at its own debit. A goods line points at the GRNI
    debit its deliveries cleared — which is one line in every ordinary case,
    and is left unlinked in the one case it is not: a line whose allocations
    span two GRNI accounts, because a mapping was superseded between two
    deliveries. There is no single journal line to name there, and naming one
    of the two arbitrarily would be worse than naming none. The allocation
    rows still carry the full detail.
    """
    by_account: dict[tuple[int, int | None], JournalLine] = {
        (row.account_id, row.cost_center_id): row for row in journal.lines.filter(debit__gt=ZERO)
    }
    grni_by_line: dict[int, set[int]] = {}
    for allocation in plan.allocations:
        account_id = allocation.goods_receipt_line.contra_account_id
        if account_id is not None:
            grni_by_line.setdefault(allocation.supplier_invoice_line_id, set()).add(account_id)

    for line in lines:
        if line.line_type == SupplierInvoiceLineType.ACCOUNT:
            account, cost_center, _amount = plan.lines[line.pk]
            line.journal_line = by_account[(account.pk, cost_center.pk if cost_center else None)]
        else:
            accounts = grni_by_line.get(line.pk, set())
            line.journal_line = (
                by_account.get((next(iter(accounts)), None)) if len(accounts) == 1 else None
            )
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
    deriving it. So do the GRNI clearing and the parked difference.

    ## One command unwinds the whole thing, and the order is load-bearing

    A matched invoice's reversal also **cancels its match** and releases its
    allocations, in this transaction, in this order:

        1. reverse the journal
        2. mark the live generation REVERSED
        3. cancel the match, releasing the deliveries
        4. check that nothing else still depends on the invoice
        5. mark the invoice REVERSED

    Split into two operator actions there would be no legal order at all.
    Cancelling first is refused, because a match may not be withdrawn while a
    live posting stands on it — the ledger would be holding a delivery the
    matching workspace had already released. Reversing first is refused too,
    if the dependency check runs before the release, because the invoice's own
    allocations are live.

    Reversing the generation first dissolves both objections at once: a
    reversed generation governs nothing, so the cancellation is permitted, and
    once the match is cancelled its allocations stop counting as dependents.
    That single property — `live_dependency` meaning *live*, not *exists* — is
    what makes a correction path exist here at all.

    Afterwards the delivery is matchable again and the invoice may be posted
    again from a new match, as generation N+1. If the *invoice* was wrong
    rather than the match, it is not reopened: raise a replacement document.
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

    # `select_related` is deliberately absent: `purchase_match` is nullable, so
    # joining it turns the lock into FOR UPDATE over the nullable side of an
    # outer join, which PostgreSQL refuses outright.
    posting = (
        SupplierInvoicePosting.objects.select_for_update()
        .filter(supplier_invoice=locked, status=SupplierInvoicePostingStatus.LIVE)
        .first()
    )

    now = timezone.now()
    reversal_business_date = resolve_business_day(locked.branch, now).business_date
    assert locked.journal_entry is not None  # noqa: S101 - a constraint guarantees it

    # 1. The mirror.
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=(
            f"procurement-supplier-invoice-reverse:"
            f"{posting.public_id if posting else locked.public_id}"
        ),
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )

    # 2. The generation stops being live, which is what unblocks step 3.
    if posting is not None:
        posting.status = SupplierInvoicePostingStatus.REVERSED
        posting.reversal_journal_entry = reversal_journal
        posting.reversed_by = actor
        posting.reversed_at = now
        posting.reversal_reason = reason.strip()
        posting.save(
            update_fields=[
                "status",
                "reversal_journal_entry",
                "reversed_by",
                "reversed_at",
                "reversal_reason",
                "updated_at",
            ]
        )
        record_audit_event(
            action=AuditAction.REVERSED,
            target=posting,
            branch=locked.branch,
            new_state=snapshot(posting),
            reason=reason.strip(),
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posting.public_id),
            metadata={
                "generation": posting.generation,
                "reversal_journal": reversal_journal.entry_number,
            },
        )

        # 3. Release the evidence. Now permitted, and now the allocations stop
        # counting in step 4.
        if posting.purchase_match is not None:
            cancel_purchase_match(
                match=posting.purchase_match,
                actor=actor,
                reason=_("أُعكست الفاتورة: %(reason)s") % {"reason": reason.strip()},
            )

    # 4. Anything else — a credit note, a payment — still refuses.
    _require_no_downstream_dependency(locked)

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

    Written as a loop over related accessors rather than a list of imports, so
    a later task that adds `payment_allocations` to `SupplierInvoiceLine` gets
    the guard by declaring the relation rather than by remembering this
    function.

    **Withdrawn dependents do not count.** Task 2.11's match allocations are
    the first relation this ever fires on, and they are the *precondition* for
    the posting rather than a document downstream of it — so a bare existence
    check would make every matched invoice permanently irreversible, which is
    the one outcome CLAUDE.md's "corrections use reversal and replacement"
    cannot survive. Models say which of their rows still stand by declaring
    `live_dependency`: allocations answer "those whose match is not
    cancelled", and postings answer "those still live". Which is also why the
    reversal releases the match *before* it reaches here.

    The **header's** relations are walked as well as each line's — the Task
    2.13 lesson, applied before it could bite: a credit-note allocation cites
    the invoice at the header, and a walk over line relations alone would
    reverse a debt a posted note had already netted against. The reversal
    flow cancels the match and retires the generation before this runs, and
    both declare themselves dead through `live_dependency`, so the header
    walk changes nothing for the documented correction path.
    """

    def refuse_if_standing(name: str, related: object, model: type) -> None:
        # A dependent that has been withdrawn depends on nothing. The same
        # `live_dependency` convention Task 2.9 uses on the receipt side: a
        # model declares which of its rows still stand, and without one
        # every row counts, which is the safe default for a relation
        # nobody has considered yet.
        live = getattr(model, "live_dependency", None)
        if live is not None:
            related = related.filter(live)  # type: ignore[attr-defined]
        if related.exists():  # type: ignore[attr-defined]
            raise ValidationError(
                _(
                    "Another document (%(relation)s) already depends on this invoice. "
                    "Reverse it first."
                ),
                code="invoice_has_dependents",
                params={"relation": name},
            )

    ignored_on_header = {"history", "lines"}
    for relation in invoice._meta.related_objects:
        name = relation.get_accessor_name()
        if not name or name in ignored_on_header:
            continue
        related = getattr(invoice, name, None)
        if related is None or not hasattr(related, "exists"):
            continue
        refuse_if_standing(name, related, relation.related_model)

    ignored = {"history"}
    for line in invoice.lines.all():
        for relation in line._meta.related_objects:
            name = relation.get_accessor_name()
            if not name or name in ignored:
                continue
            related = getattr(line, name, None)
            if related is None or not hasattr(related, "exists"):
                continue
            refuse_if_standing(name, related, relation.related_model)


# ---------------------------------------------------------------------------
# The payable, derived
# ---------------------------------------------------------------------------


def outstanding_amount(invoice: SupplierInvoice) -> Decimal:
    """
    What is still owed on one invoice.

    The posted amount less what posted credit notes (Task 2.14) and posted
    payments (Task 2.15) have settled against it — the one expression every
    bound and every report reads. Derived every time from the documents;
    there is no stored balance to find and correct.
    """
    from apps.procurement.models import PaymentAllocation, SupplierCreditAllocation

    if invoice.status != SupplierInvoiceStatus.POSTED:
        return ZERO
    credited: Decimal | None = SupplierCreditAllocation.objects.filter(
        invoice=invoice, credit_note__status="POSTED"
    ).aggregate(total=Sum("allocated_amount"))["total"]
    paid: Decimal | None = PaymentAllocation.objects.filter(
        invoice=invoice, payment__status="POSTED"
    ).aggregate(total=Sum("allocated_amount"))["total"]
    return (invoice.posted_amount or ZERO) - (credited or ZERO) - (paid or ZERO)


def supplier_outstanding(supplier: Supplier) -> Decimal:
    """
    Everything still owed to one supplier, derived from posted documents.

    Posted invoices less posted credit notes — the **whole** note, allocated
    or standing. An unallocated credit is still money the supplier owes back,
    and a figure that ignored it would tell the buyer to pay it again.
    """
    from apps.procurement.models import PaymentAllocation, SupplierCreditNote

    invoiced: Decimal | None = SupplierInvoice.objects.filter(
        supplier=supplier, status=SupplierInvoiceStatus.POSTED
    ).aggregate(total=Sum("posted_amount"))["total"]
    credited: Decimal | None = SupplierCreditNote.objects.filter(
        supplier=supplier, status="POSTED"
    ).aggregate(total=Sum("amount"))["total"]
    # Only the allocated share of a payment reduces the payable; the advance
    # remainder is an asset the payable never saw (PRC-055).
    paid: Decimal | None = PaymentAllocation.objects.filter(
        invoice__supplier=supplier, payment__status="POSTED"
    ).aggregate(total=Sum("allocated_amount"))["total"]
    return (invoiced or ZERO) - (credited or ZERO) - (paid or ZERO)


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
