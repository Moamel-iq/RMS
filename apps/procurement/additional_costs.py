"""Structured direct and landed costs on supplier invoices.

Procurement owns the commercial evidence and allocation policy. Inventory owns
the value-only ledger effect. The dependency therefore points one way:
Procurement calls the public stock kernel; Inventory never imports suppliers,
matches or invoices.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, CostCenter
from apps.core.allocation import AllocationItem, allocate
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.inventory.ledger import (
    EffectDirection,
    MovementInput,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.models import MovementType, StockLedgerEntry, StockMovement
from apps.procurement.models import (
    GoodsReceiptStatus,
    PurchaseMatchAllocation,
    PurchaseMatchStatus,
    SupplierInvoice,
    SupplierInvoiceCharge,
    SupplierInvoiceChargeAllocation,
    SupplierInvoiceChargeAllocationBasis,
    SupplierInvoiceChargeCategory,
    SupplierInvoiceChargeManualShare,
    SupplierInvoiceChargeTreatment,
    SupplierInvoicePosting,
    SupplierInvoiceStatus,
)
from apps.users.models import User

ZERO = Decimal("0.000")
LANDED_COST_SOURCE_TYPE = "PROCUREMENT_SUPPLIER_INVOICE_LANDED_COST"


@dataclass(frozen=True)
class LandedCostPreviewRow:
    """One deterministic landed-cost result before it reaches either ledger."""

    charge: SupplierInvoiceCharge
    match_allocation: PurchaseMatchAllocation
    sequence: int
    allocated_amount: Decimal

    @property
    def receipt_line(self):  # type: ignore[no-untyped-def]
        return self.match_allocation.goods_receipt_line

    @property
    def effect_key(self) -> str:
        return f"landed:{self.charge.public_id}:{self.match_allocation.allocation_uid}"


def _require_charge_draft(invoice: SupplierInvoice) -> SupplierInvoice:
    locked = SupplierInvoice.objects.select_for_update().get(pk=invoice.pk)
    if locked.status != SupplierInvoiceStatus.DRAFT:
        raise ValidationError(
            _("Additional costs can be changed only while the invoice is a draft."),
            code="invoice_charge_not_editable",
        )
    return locked


def _next_order(invoice: SupplierInvoice) -> int:
    last = (
        SupplierInvoiceCharge.objects.filter(invoice=invoice)
        .order_by("-line_order")
        .values_list("line_order", flat=True)
        .first()
    )
    return (last or 0) + 1


def _validate_charge_shape(
    *,
    invoice: SupplierInvoice,
    treatment: str,
    direct_account: Account | None,
    cost_center: CostCenter | None,
) -> None:
    if treatment == SupplierInvoiceChargeTreatment.DIRECT_EXPENSE:
        if direct_account is None or cost_center is None:
            raise ValidationError(
                _("A direct cost requires both an eligible account and a cost center."),
                code="direct_charge_needs_account_and_cost_center",
            )
        # Kept in the invoice service because direct invoice lines and direct
        # charges share exactly one account-eligibility rule.
        from apps.procurement.invoices import _validate_direct_account

        _validate_direct_account(
            organization_id=invoice.organization_id,
            account=direct_account,
            cost_center=cost_center,
        )
    elif treatment == SupplierInvoiceChargeTreatment.LANDED_COST:
        if direct_account is not None or cost_center is not None:
            raise ValidationError(
                _("A landed cost takes its stock accounts from receipt evidence."),
                code="landed_cost_has_direct_account",
            )
    else:
        raise ValidationError(_("Unknown charge treatment."), code="unknown_charge_treatment")


@transaction.atomic
def create_charge(
    *,
    invoice: SupplierInvoice,
    actor: User,
    category: str,
    treatment: str,
    description: str,
    amount: Decimal,
    allocation_basis: str = SupplierInvoiceChargeAllocationBasis.RECEIPT_VALUE,
    direct_account: Account | None = None,
    cost_center: CostCenter | None = None,
    evidence_reference: str = "",
    notes: str = "",
) -> SupplierInvoiceCharge:
    """Add one positive actual cost to a draft invoice."""
    locked = _require_charge_draft(invoice)
    value = quantize_money(amount)
    if value <= ZERO:
        raise ValidationError(
            _("An additional cost must be greater than zero."), code="amount_not_positive"
        )
    if category not in SupplierInvoiceChargeCategory.values:
        raise ValidationError(_("Unknown charge category."), code="unknown_charge_category")
    if allocation_basis not in SupplierInvoiceChargeAllocationBasis.values:
        raise ValidationError(_("Unknown allocation basis."), code="unknown_allocation_basis")
    _validate_charge_shape(
        invoice=locked,
        treatment=treatment,
        direct_account=direct_account,
        cost_center=cost_center,
    )
    charge = SupplierInvoiceCharge(
        invoice=locked,
        line_order=_next_order(locked),
        category=category,
        treatment=treatment,
        description=description.strip(),
        amount=value,
        direct_account=direct_account,
        cost_center=cost_center,
        allocation_basis=allocation_basis,
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
        created_by=actor,
    )
    if not charge.description:
        raise ValidationError(
            _("An additional cost needs a description."), code="description_required"
        )
    charge.full_clean()
    charge.save()
    _recalculate_invoice(locked)
    record_audit_event(
        action=AuditAction.CREATED,
        target=charge,
        branch=locked.branch,
        new_state=snapshot(charge),
    )
    return charge


@transaction.atomic
def update_charge(
    *,
    charge: SupplierInvoiceCharge,
    category: str,
    treatment: str,
    description: str,
    amount: Decimal,
    allocation_basis: str,
    direct_account: Account | None,
    cost_center: CostCenter | None,
    evidence_reference: str = "",
    notes: str = "",
) -> SupplierInvoiceCharge:
    """Correct a charge without changing its invoice or stable line order."""
    locked_invoice = _require_charge_draft(charge.invoice)
    locked = SupplierInvoiceCharge.objects.select_for_update().get(pk=charge.pk)
    previous = snapshot(locked)
    value = quantize_money(amount)
    if value <= ZERO:
        raise ValidationError(
            _("An additional cost must be greater than zero."), code="amount_not_positive"
        )
    if category not in SupplierInvoiceChargeCategory.values:
        raise ValidationError(_("Unknown charge category."), code="unknown_charge_category")
    if allocation_basis not in SupplierInvoiceChargeAllocationBasis.values:
        raise ValidationError(_("Unknown allocation basis."), code="unknown_allocation_basis")
    _validate_charge_shape(
        invoice=locked_invoice,
        treatment=treatment,
        direct_account=direct_account,
        cost_center=cost_center,
    )
    locked.category = category
    locked.treatment = treatment
    locked.description = description.strip()
    locked.amount = value
    locked.direct_account = direct_account
    locked.cost_center = cost_center
    locked.allocation_basis = allocation_basis
    locked.evidence_reference = evidence_reference.strip()
    locked.notes = notes.strip()
    if not locked.description:
        raise ValidationError(
            _("An additional cost needs a description."), code="description_required"
        )
    locked.full_clean()
    locked.save()
    _recalculate_invoice(locked_invoice)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked_invoice.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_charge(*, charge: SupplierInvoiceCharge) -> None:
    """Remove a charge from a draft and re-derive the invoice payable."""
    locked_invoice = _require_charge_draft(charge.invoice)
    locked = SupplierInvoiceCharge.objects.select_for_update().get(pk=charge.pk)
    previous = snapshot(locked)
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked_invoice.branch,
        previous_state=previous,
    )
    locked.delete()
    _recalculate_invoice(locked_invoice)


def _recalculate_invoice(invoice: SupplierInvoice) -> None:
    from apps.procurement.invoices import _recalculate

    invoice.charges_total = quantize_money(
        sum(invoice.charges.values_list("amount", flat=True), start=ZERO)
    )
    invoice.save(update_fields=["charges_total", "updated_at"])
    _recalculate(invoice)


def _target_allocations(
    charge: SupplierInvoiceCharge, *, lock: bool
) -> list[PurchaseMatchAllocation]:
    invoice = charge.invoice
    query = PurchaseMatchAllocation.objects.filter(
        match__supplier_invoice=invoice,
        match__status=PurchaseMatchStatus.READY,
    ).select_related(
        "match",
        "goods_receipt_line",
        "goods_receipt_line__receipt",
        "goods_receipt_line__receipt__warehouse",
        "goods_receipt_line__item",
        "goods_receipt_line__item__base_unit",
        "goods_receipt_line__lot",
        "goods_receipt_line__movement",
        "goods_receipt_line__inventory_account",
    )
    if lock:
        # Limit the lock to the allocation and its non-null receipt line;
        # nullable lot/movement joins are evidence reads and PostgreSQL will
        # correctly refuse an unrestricted FOR UPDATE over their outer joins.
        query = query.select_for_update(of=("self", "goods_receipt_line"))
    rows = list(query.order_by("sequence", "pk"))
    if not rows:
        raise ValidationError(
            _("This landed cost is waiting for a ready invoice match."),
            code="landed_cost_waiting_for_match",
        )
    for row in rows:
        receipt_line = row.goods_receipt_line
        receipt = receipt_line.receipt
        if receipt.status != GoodsReceiptStatus.POSTED:
            raise ValidationError(
                _("A landed-cost target must still be a posted receipt."),
                code="landed_cost_receipt_not_live",
            )
        if receipt.supplier_id != invoice.supplier_id:
            raise ValidationError(
                _("Release 1 does not capitalise cross-supplier freight."),
                code="landed_cost_supplier_mismatch",
            )
        if receipt.organization_id != invoice.organization_id:
            raise ValidationError(
                _("The landed-cost receipt belongs to another organization."),
                code="organization_mismatch",
            )
        if receipt_line.movement_id is None or receipt_line.inventory_account_id is None:
            raise ValidationError(
                _("The receipt target has no posted inventory evidence."),
                code="landed_cost_receipt_has_no_stock_evidence",
            )
    return rows


def _assert_no_downstream_outbound(row: PurchaseMatchAllocation) -> None:
    receipt_line = row.goods_receipt_line
    movement = receipt_line.movement
    assert movement is not None  # noqa: S101 - validated by `_target_allocations`
    downstream = (
        StockMovement.objects.filter(
            warehouse_id=movement.warehouse_id,
            item_id=movement.item_id,
            lot_id=movement.lot_id,
            posted_sequence__gt=movement.posted_sequence,
            base_quantity__lt=ZERO,
        )
        .select_related("entry")
        .order_by("posted_sequence")
        .first()
    )
    if downstream is not None:
        raise ValidationError(
            _(
                "Receipt line %(receipt)s cannot be capitalised because stock has already "
                "left its position in movement %(movement)s. Change the charge to a direct cost."
            ),
            code="landed_cost_has_downstream_outbound",
            params={
                "receipt": receipt_line.line_uid,
                "movement": downstream.pk,
            },
        )


def preview_charge_allocations(
    charge: SupplierInvoiceCharge, *, lock: bool = False, check_downstream: bool = True
) -> list[LandedCostPreviewRow]:
    """Allocate one landed cost exactly across its live match evidence."""
    if charge.treatment != SupplierInvoiceChargeTreatment.LANDED_COST:
        return []
    targets = _target_allocations(charge, lock=lock)
    if check_downstream:
        for target in targets:
            _assert_no_downstream_outbound(target)

    if charge.allocation_basis == SupplierInvoiceChargeAllocationBasis.MANUAL:
        share_query = charge.manual_shares.select_related("match_allocation")
        if lock:
            share_query = share_query.select_for_update(of=("self",))
        shares = {row.match_allocation_id: row.amount for row in share_query.all()}
        unknown = set(shares) - {row.pk for row in targets}
        if unknown:
            raise ValidationError(
                _("A manual share cites evidence outside the current ready match."),
                code="manual_share_target_not_live",
            )
        if quantize_money(sum(shares.values(), start=ZERO)) != charge.amount:
            raise ValidationError(
                _("Manual shares must add exactly to the charge amount."),
                code="manual_shares_do_not_balance",
            )
        amounts = {pk: quantize_money(value) for pk, value in shares.items()}
    else:
        if charge.allocation_basis == SupplierInvoiceChargeAllocationBasis.BASE_QUANTITY:
            dimensions = {row.goods_receipt_line.item.base_unit.dimension for row in targets}
            if len(dimensions) != 1:
                raise ValidationError(
                    _("Base-quantity allocation cannot mix mass, volume and count."),
                    code="mixed_quantity_dimensions",
                )
            weights = [row.matched_base_quantity for row in targets]
        else:
            weights = [row.receipt_allocated_value for row in targets]
        results = allocate(
            charge.amount,
            [
                AllocationItem(sequence=index, weight=weight)
                for index, weight in enumerate(weights, start=1)
            ],
        )
        amounts = {
            target.pk: result.amount for target, result in zip(targets, results, strict=True)
        }

    preview = [
        LandedCostPreviewRow(
            charge=charge,
            match_allocation=target,
            sequence=index,
            allocated_amount=amounts.get(target.pk, ZERO),
        )
        for index, target in enumerate(targets, start=1)
        if amounts.get(target.pk, ZERO) > ZERO
    ]
    if quantize_money(sum((row.allocated_amount for row in preview), start=ZERO)) != charge.amount:
        raise ValidationError(
            _("The landed-cost allocation does not equal its charge."),
            code="landed_cost_allocation_mismatch",
        )
    return preview


@transaction.atomic
def save_manual_shares(
    *,
    charge: SupplierInvoiceCharge,
    actor: User,
    shares: Mapping[int, Decimal],
) -> list[SupplierInvoiceChargeManualShare]:
    """Replace a manual allocation while an approved invoice waits to post."""
    locked_charge = (
        SupplierInvoiceCharge.objects.select_for_update()
        .select_related("invoice")
        .get(pk=charge.pk)
    )
    invoice = locked_charge.invoice
    if invoice.status != SupplierInvoiceStatus.APPROVED:
        raise ValidationError(
            _("Manual landed-cost shares are entered after approval and before posting."),
            code="manual_shares_wrong_invoice_status",
        )
    if locked_charge.allocation_basis != SupplierInvoiceChargeAllocationBasis.MANUAL:
        raise ValidationError(_("This charge is not manually allocated."), code="charge_not_manual")
    targets = _target_allocations(locked_charge, lock=True)
    target_ids = {row.pk for row in targets}
    normalized = {
        int(pk): quantize_money(value)
        for pk, value in shares.items()
        if quantize_money(value) > ZERO
    }
    if set(normalized) - target_ids:
        raise ValidationError(
            _("A manual share cites evidence outside this invoice's ready match."),
            code="manual_share_target_not_live",
        )
    if quantize_money(sum(normalized.values(), start=ZERO)) != locked_charge.amount:
        raise ValidationError(
            _("Manual shares must add exactly to the charge amount."),
            code="manual_shares_do_not_balance",
        )
    SupplierInvoiceChargeManualShare.objects.filter(charge=locked_charge).delete()
    created = [
        SupplierInvoiceChargeManualShare.objects.create(
            charge=locked_charge,
            match_allocation_id=pk,
            amount=amount,
            created_by=actor,
        )
        for pk, amount in sorted(normalized.items())
    ]
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked_charge,
        branch=invoice.branch,
        reason="manual landed-cost allocation",
        metadata={"shares": {str(pk): format(value, "f") for pk, value in normalized.items()}},
    )
    return created


def plan_landed_costs(invoice: SupplierInvoice, *, lock: bool = True) -> list[LandedCostPreviewRow]:
    """Return every exact landed-cost row for an invoice in charge order."""
    charges = SupplierInvoiceCharge.objects.filter(
        invoice=invoice, treatment=SupplierInvoiceChargeTreatment.LANDED_COST
    ).select_related("invoice")
    if lock:
        charges = charges.select_for_update()
    rows: list[LandedCostPreviewRow] = []
    for charge in charges.order_by("line_order"):
        rows.extend(preview_charge_allocations(charge, lock=lock))
    return rows


def landed_cost_effects(rows: Iterable[LandedCostPreviewRow]) -> list[MovementInput]:
    """Translate frozen procurement targets into value-only stock effects."""
    effects: list[MovementInput] = []
    for row in rows:
        receipt_line = row.receipt_line
        movement = receipt_line.movement
        assert movement is not None  # noqa: S101 - preview requires posted evidence
        assert receipt_line.inventory_account is not None  # noqa: S101 - same
        effects.append(
            MovementInput(
                warehouse=movement.warehouse,
                item=receipt_line.item,
                lot=receipt_line.lot,
                movement_type=MovementType.MANUAL_ADJUSTMENT,
                quantity=ZERO,
                effect_key=row.effect_key,
                control_account=receipt_line.inventory_account,
                direction=EffectDirection.VALUE_ONLY,
                value_adjustment=row.allocated_amount,
            )
        )
    return effects


def post_landed_cost_entry(
    *,
    invoice: SupplierInvoice,
    posting_public_id: object,
    rows: list[LandedCostPreviewRow],
) -> StockLedgerEntry | None:
    """Write all landed effects as one stock event inside invoice posting."""
    if not rows:
        return None
    return post_stock_entry(
        organization=invoice.organization,
        effects=landed_cost_effects(rows),
        idempotency_key=f"supplier-invoice-landed-cost:{posting_public_id}",
        effective_at=timezone.now(),
        business_date=invoice.business_date,
        source_document_type=LANDED_COST_SOURCE_TYPE,
        source_document_id=str(posting_public_id),
        source_event="POSTED",
        reference=invoice.supplier_reference or invoice.supplier_invoice_number,
        reason=f"Landed costs for supplier invoice {invoice.supplier_invoice_number}",
    )


def persist_landed_allocations(
    *,
    posting: SupplierInvoicePosting,
    rows: list[LandedCostPreviewRow],
    stock_entry: StockLedgerEntry | None,
) -> list[SupplierInvoiceChargeAllocation]:
    """Freeze allocation identities, accounts and movements after both ledgers post."""
    if not rows:
        return []
    if stock_entry is None:  # pragma: no cover - caller construction
        raise ValidationError(
            _("Landed costs have no stock entry."), code="landed_cost_has_no_stock_entry"
        )
    movements = {movement.effect_key: movement for movement in stock_entry.movements.all()}
    created: list[SupplierInvoiceChargeAllocation] = []
    for row in rows:
        receipt_line = row.receipt_line
        movement = movements[row.effect_key]
        control_account = movement.control_account
        if control_account is None:  # pragma: no cover - Inventory kernel contract
            raise ValidationError(
                _("A landed-cost movement has no stored control account."),
                code="landed_cost_control_account_missing",
            )
        created.append(
            SupplierInvoiceChargeAllocation.objects.create(
                posting=posting,
                charge=row.charge,
                match_allocation=row.match_allocation,
                sequence=row.sequence,
                match_allocation_uid=row.match_allocation.allocation_uid,
                receipt_line=receipt_line,
                receipt_line_uid=receipt_line.line_uid,
                item=receipt_line.item,
                warehouse=movement.warehouse,
                lot=receipt_line.lot,
                matched_base_quantity=row.match_allocation.matched_base_quantity,
                receipt_allocated_value=row.match_allocation.receipt_allocated_value,
                allocated_amount=row.allocated_amount,
                control_account=control_account,
                inventory_movement=movement,
            )
        )
    total = quantize_money(sum((row.allocated_amount for row in created), start=ZERO))
    if total != posting.landed_cost_value:
        raise ValidationError(
            _("Stored landed-cost allocations do not equal the posting."),
            code="stored_landed_cost_allocation_mismatch",
        )
    return created


def _assert_no_outbound_after_landed_effect(posting: SupplierInvoicePosting) -> None:
    for allocation in posting.landed_cost_allocations.select_related("inventory_movement"):
        movement = allocation.inventory_movement
        downstream = StockMovement.objects.filter(
            warehouse_id=movement.warehouse_id,
            item_id=movement.item_id,
            lot_id=movement.lot_id,
            posted_sequence__gt=movement.posted_sequence,
            base_quantity__lt=ZERO,
        ).exists()
        if downstream:
            raise ValidationError(
                _(
                    "This invoice's landed cost has since flowed into an outbound stock event. "
                    "Reverse that downstream event before reversing the invoice."
                ),
                code="landed_cost_has_downstream_dependency",
            )


def reverse_landed_cost_entry(
    *,
    posting: SupplierInvoicePosting,
    reason: str,
    business_date: datetime.date,
) -> StockLedgerEntry | None:
    """Reverse the exact stored value-only entry, never current mappings or targets."""
    if posting.stock_entry_id is None:
        return None
    _assert_no_outbound_after_landed_effect(posting)
    stock_entry = posting.stock_entry
    if stock_entry is None:  # pragma: no cover - foreign-key evidence is inconsistent
        raise ValidationError(
            _("The landed-cost posting has no stock entry."),
            code="landed_cost_has_no_stock_entry",
        )
    return reverse_stock_entry(
        entry=stock_entry,
        idempotency_key=f"supplier-invoice-landed-cost-reverse:{posting.public_id}",
        reason=reason,
        business_date=business_date,
    )
