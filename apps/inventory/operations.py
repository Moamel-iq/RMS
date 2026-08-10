"""
Operational stock documents: goods receipt, consumption issue, return-in.

Three documents, one lifecycle — DRAFT → POSTED → REVERSED — and one posting
routine that differs only in how each side of the journal is found:

    RECEIPT     Dr resolved INVENTORY_CONTROL      Cr GOODS_RECEIVED_NOT_INVOICED
    ISSUE       Dr resolved INVENTORY_CONSUMPTION  Cr the account the stock is in
    RETURN_IN   Dr the original issue's control    Cr the original issue's consumption

What each of those sentences protects is worth stating.

**A receipt is not a supplier invoice.** Goods arrived; nobody is owed a
stated amount until an invoice says so. The credit is a clearing liability
that Procurement will clear in Phase 2 — not a payable, not a payment, not a
cash purchase.

**An issue credits the account the stock actually sits in**, read from the
balance's own control-account identity, never from a fresh resolution. A
mapping changed since the goods arrived must not credit them out of an
account they were never in (§D).

**A return is not a reversal.** It is a new operational fact — unused stock
coming back — valued from the issue it goes back against so the pair nets to
zero however the average has moved. A reversal corrects a document that
should not exist; a return records something that happened.

## Locking

Every posting takes, in this order (ADR-019):

    1. the document row                     select_for_update
    2. the organization's mapping lock      shared
    3. the source issue lines, for a return select_for_update
    4. the stock keys, sorted canonically   advisory, inside the kernel
    5. the inventory posted-sequence        inside the kernel
    6. the document-number sequence
    7. the journal-number sequence          inside post_entry

Step 3 is where two concurrent returns against one issue are serialised: they
contend for the same issue-line row before either can read how much is left
to return.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONSUMPTION,
    INVENTORY_CONTROL,
    Account,
    AccountingPeriod,
    CostCenter,
    JournalEntry,
    JournalLine,
    OrganizationAccountMapping,
    SourceEvent,
)
from apps.accounting.services import post_entry, resolve_period, reverse_entry
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.context import get_actor
from apps.core.locks import lock_account_mappings_shared
from apps.core.models import AuditAction
from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.accounts import resolve_inventory_account
from apps.inventory.ledger import (
    MovementInput,
    link_journal_entry,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.models import (
    DOCUMENT_NUMBER_PREFIX,
    ConversionType,
    InventoryAccountMapping,
    InventoryDocumentSequence,
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    InventoryMovementDocument,
    InventoryMovementDocumentLine,
    ItemPackageConversion,
    MovementType,
    StockBalance,
    StockMovement,
    Warehouse,
)
from apps.organizations.business_dates import resolve_business_day
from apps.organizations.models import Branch, Organization
from apps.users.models import User

ZERO = Decimal("0")

#: The posting rule stamped on each journal. Bump one when its accounting
#: treatment changes, so old entries still say which rule produced them.
POSTING_RULE: dict[str, str] = {
    InventoryDocumentType.RECEIPT: "inventory-receipt-v1",
    InventoryDocumentType.ISSUE: "inventory-issue-v1",
    InventoryDocumentType.RETURN_IN: "inventory-return-in-v1",
}

#: Which stock movement each document type produces.
MOVEMENT_TYPE: dict[str, str] = {
    InventoryDocumentType.RECEIPT: MovementType.RECEIPT,
    InventoryDocumentType.ISSUE: MovementType.ISSUE,
    InventoryDocumentType.RETURN_IN: MovementType.RETURN_IN,
}

OPERATIONAL_TYPES = frozenset(MOVEMENT_TYPE)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentLineInput:
    """One requested line, before validation derives its base quantity."""

    item: InventoryItem
    lot: InventoryLot | None = None
    package_conversion: ItemPackageConversion | None = None
    entered_package_quantity: Decimal | None = None
    measured_base_quantity: Decimal | None = None
    base_quantity: Decimal | None = None
    #: Receipt only. An issue and a return are valued by the ledger.
    unit_cost: Decimal | None = None
    #: Return only: the issue line being returned against.
    source_issue_line: InventoryMovementDocumentLine | None = None


def _require_status(document: InventoryMovementDocument, status: str, code: str) -> None:
    if document.status != status:
        raise ValidationError(
            _("Document %(doc)s is %(actual)s, not %(expected)s."),
            code=code,
            params={
                "doc": document.document_number or str(document.public_id),
                "actual": document.get_status_display(),
                "expected": status,
            },
        )


def _aware(moment: datetime.datetime) -> datetime.datetime:
    if timezone.is_naive(moment):
        raise ValidationError(
            _("The effective moment must state its timezone."), code="effective_at_must_be_aware"
        )
    return moment


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def create_document(
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    document_type: str,
    effective_at: datetime.datetime,
    evidence_reference: str,
    narration: str = "",
    cost_center: CostCenter | None = None,
) -> InventoryMovementDocument:
    """Start a draft receipt, issue, or return for one warehouse."""
    if document_type not in OPERATIONAL_TYPES:
        raise ValidationError(
            _("%(type)s is not an operational document type."),
            code="unknown_document_type",
            params={"type": document_type},
        )
    if branch.organization_id != organization.pk:
        raise ValidationError(
            _("Branch %(code)s belongs to another organization."),
            code="branch_organization_mismatch",
            params={"code": branch.code},
        )
    if not branch.is_active:
        raise ValidationError(
            _("Branch %(code)s is closed."), code="branch_inactive", params={"code": branch.code}
        )
    if warehouse.branch_id != branch.pk:
        raise ValidationError(
            _("Warehouse %(code)s belongs to another branch."),
            code="warehouse_branch_mismatch",
            params={"code": warehouse.code},
        )
    if not warehouse.is_active:
        raise ValidationError(
            _("Warehouse %(code)s is archived."),
            code="warehouse_inactive",
            params={"code": warehouse.code},
        )
    if warehouse.is_system:
        # In-transit stock is dispatched into and received out of by transfer
        # documents, which are Task 1.5. An operational receipt or issue here
        # would claim goods appeared in transit from nowhere.
        raise ValidationError(
            _("Warehouse %(code)s is system-controlled and takes no operational documents."),
            code="operational_document_into_system_warehouse",
            params={"code": warehouse.code},
        )
    if not evidence_reference.strip():
        raise ValidationError(
            _("An operational document needs its evidence reference."),
            code="evidence_reference_required",
        )
    if cost_center is not None and cost_center.organization_id != organization.pk:
        raise ValidationError(
            _("Cost center %(code)s belongs to another organization."),
            code="cost_center_organization_mismatch",
            params={"code": cost_center.code},
        )
    if cost_center is not None and not cost_center.is_active:
        raise ValidationError(
            _("Cost center %(code)s is archived."),
            code="cost_center_inactive",
            params={"code": cost_center.code},
        )
    if cost_center is not None and document_type != InventoryDocumentType.ISSUE:
        # A receipt has no consumption destination, and a return reuses the
        # original issue's — offering a fresh one would let the pair land in
        # two different managerial buckets and never net out.
        raise ValidationError(
            _("Only a consumption issue carries a cost center."),
            code="cost_center_not_applicable",
        )

    moment = _aware(effective_at)
    document = InventoryMovementDocument(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=document_type,
        effective_at=moment,
        # A preview until posting fixes it, exactly as an opening draft's is.
        business_date=resolve_business_day(branch, moment).business_date,
        evidence_reference=evidence_reference.strip(),
        narration=narration.strip(),
        cost_center=cost_center,
        created_by=get_actor(),
    )
    document.full_clean()
    document.save()
    record_audit_event(
        action=AuditAction.CREATED, target=document, branch=branch, new_state=snapshot(document)
    )
    return document


@transaction.atomic
def update_document(
    *,
    document: InventoryMovementDocument,
    effective_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    narration: str | None = None,
    cost_center: CostCenter | None = None,
    clear_cost_center: bool = False,
) -> InventoryMovementDocument:
    """Amend a draft's header. Anything past DRAFT is immutable."""
    locked = InventoryMovementDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    before = snapshot(locked)

    if effective_at is not None:
        locked.effective_at = _aware(effective_at)
        locked.business_date = resolve_business_day(
            locked.branch, locked.effective_at
        ).business_date
    if evidence_reference is not None:
        if not evidence_reference.strip():
            raise ValidationError(
                _("An operational document needs its evidence reference."),
                code="evidence_reference_required",
            )
        locked.evidence_reference = evidence_reference.strip()
    if narration is not None:
        locked.narration = narration.strip()
    if clear_cost_center:
        locked.cost_center = None
    elif cost_center is not None:
        if cost_center.organization_id != locked.organization_id:
            raise ValidationError(
                _("Cost center %(code)s belongs to another organization."),
                code="cost_center_organization_mismatch",
                params={"code": cost_center.code},
            )
        if locked.document_type != InventoryDocumentType.ISSUE:
            raise ValidationError(
                _("Only a consumption issue carries a cost center."),
                code="cost_center_not_applicable",
            )
        locked.cost_center = cost_center

    locked.full_clean()
    locked.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_document(*, document: InventoryMovementDocument, reason: str = "") -> None:
    """Discard a draft. Only a draft — the trigger refuses anything later."""
    locked = InventoryMovementDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.branch,
        previous_state=snapshot(locked),
        reason=reason,
    )
    locked.lines.all().delete()
    locked.delete()


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def _validate_line_target(document: InventoryMovementDocument, line: DocumentLineInput) -> None:
    if line.item.organization_id != document.organization_id:
        raise ValidationError(
            _("Item %(code)s belongs to another organization."),
            code="item_organization_mismatch",
            params={"code": line.item.code},
        )
    if not line.item.is_active:
        raise ValidationError(
            _("Item %(code)s is archived."),
            code="item_inactive",
            params={"code": line.item.code},
        )

    if line.item.tracks_lots:
        if line.lot is None:
            raise ValidationError(
                _("Item %(code)s tracks lots, so the line needs one."),
                code="lot_required",
                params={"code": line.item.code},
            )
        if line.lot.item_id != line.item.pk:
            raise ValidationError(
                _("Lot %(lot)s belongs to another item."),
                code="lot_item_mismatch",
                params={"lot": line.lot.code},
            )
        if line.item.tracks_expiry and line.lot.expiry_date is None:
            raise ValidationError(
                _("Item %(code)s tracks expiry, so its lots need an expiry date."),
                code="lot_expiry_required",
                params={"code": line.item.code},
            )
    elif line.lot is not None:
        raise ValidationError(
            _("Item %(code)s does not track lots, so the line must not name one."),
            code="lot_not_allowed",
            params={"code": line.item.code},
        )


def _derive_base_quantity(document: InventoryMovementDocument, line: DocumentLineInput) -> Decimal:
    """
    The authoritative quantity, from whichever way it was entered.

    Direct base entry, a FIXED package count, or a VARIABLE package count with
    a measured weight — one of the three, never a mixture. The conversion is
    snapshotted onto the stored line, so a later factor version cannot restate
    what this line meant, and it is validated against the **business date**
    rather than the physical timestamp.
    """
    conversion = line.package_conversion
    if conversion is None:
        if line.entered_package_quantity is not None or line.measured_base_quantity is not None:
            raise ValidationError(
                _("A package quantity needs the package conversion it was counted in."),
                code="package_conversion_required",
            )
        if line.base_quantity is None:
            raise ValidationError(_("The line needs a quantity."), code="quantity_required")
        return quantize_quantity(line.base_quantity)

    if conversion.item_id != line.item.pk:
        raise ValidationError(
            _("The conversion belongs to another item."), code="conversion_item_mismatch"
        )
    if not conversion.is_active:
        raise ValidationError(
            _("Conversion version %(version)s is no longer active."),
            code="conversion_inactive",
            params={"version": conversion.version},
        )
    covers = conversion.effective_from <= document.business_date and (
        conversion.effective_to is None or document.business_date <= conversion.effective_to
    )
    if not covers:
        raise ValidationError(
            _("The conversion is not effective on %(date)s."),
            code="conversion_not_effective",
            params={"date": document.business_date.isoformat()},
        )
    if line.base_quantity is not None:
        raise ValidationError(
            _("Enter either a package count or a base quantity, not both."),
            code="quantity_entered_twice",
        )
    if line.entered_package_quantity is None or line.entered_package_quantity <= ZERO:
        raise ValidationError(
            _("The package count must be greater than zero."), code="package_quantity_required"
        )
    if not conversion.allows_fractional and line.entered_package_quantity % 1 != 0:
        raise ValidationError(
            _("This package does not come in fractions."),
            code="fractional_packages_not_allowed",
        )

    if conversion.conversion_type == ConversionType.VARIABLE:
        # The stored factor is a planning estimate; the scale is the truth.
        if line.measured_base_quantity is None or line.measured_base_quantity <= ZERO:
            raise ValidationError(
                _("A variable package needs the measured base quantity."),
                code="measured_quantity_required",
            )
        return quantize_quantity(line.measured_base_quantity)

    if line.measured_base_quantity is not None:
        raise ValidationError(
            _("A fixed package converts arithmetically; a measured quantity means VARIABLE."),
            code="measured_only_for_variable",
        )
    return quantize_quantity(line.entered_package_quantity * conversion.factor_to_base)


def returnable(line: InventoryMovementDocumentLine) -> tuple[Decimal, Decimal]:
    """
    How much of an issue line is still returnable, in quantity and value.

        remaining_quantity = issued quantity − active returned quantity
        remaining_value    = issued value    − active returned value

    "Active" excludes returns whose document was itself reversed: reversing a
    return puts the quantity back on the shelf of what may be returned, which
    is the whole point of reversing it.
    """
    returned = line.return_lines.filter(document__status=InventoryDocumentStatus.POSTED).aggregate(
        quantity=Sum("base_quantity"), value=Sum("total_value")
    )
    used_quantity = returned["quantity"] or ZERO
    used_value = returned["value"] or ZERO
    return (
        quantize_quantity(line.base_quantity - used_quantity),
        quantize_money((line.total_value or ZERO) - used_value),
    )


def _validate_return_source(
    document: InventoryMovementDocument, line: DocumentLineInput
) -> InventoryMovementDocumentLine:
    """Everything a return's original must be, before any value is taken from it."""
    source = line.source_issue_line
    if source is None:
        raise ValidationError(
            _("A return line must name the issue line it returns against."),
            code="source_issue_required",
        )
    if source.document.document_type != InventoryDocumentType.ISSUE:
        raise ValidationError(
            _("A return goes back against a consumption issue, not a %(type)s."),
            code="source_is_not_an_issue",
            params={"type": source.document.get_document_type_display()},
        )
    if source.document.status != InventoryDocumentStatus.POSTED:
        raise ValidationError(
            _("The original issue is %(status)s; only a posted issue can be returned against."),
            code="source_issue_not_posted",
            params={"status": source.document.get_status_display()},
        )
    if source.document.organization_id != document.organization_id:
        raise ValidationError(
            _("The original issue belongs to another organization."),
            code="source_issue_organization_mismatch",
        )
    if source.item_id != line.item.pk:
        raise ValidationError(
            _("The original issue was for %(item)s."),
            code="return_item_mismatch",
            params={"item": source.item.code},
        )
    if source.lot_id != (line.lot.pk if line.lot is not None else None):
        raise ValidationError(
            _("The original issue was for a different lot."), code="return_lot_mismatch"
        )
    if source.document.warehouse_id != document.warehouse_id:
        # Returning into a different warehouse would be a transfer wearing a
        # return's clothes, and it would move value between two positions
        # without either journal saying so. Transfers are Task 1.5.
        raise ValidationError(
            _("Stock returns to the warehouse it was issued from, %(code)s. Use a transfer."),
            code="return_warehouse_mismatch",
            params={"code": source.document.warehouse.code},
        )
    return source


@transaction.atomic
def add_line(
    *, document: InventoryMovementDocument, line: DocumentLineInput
) -> InventoryMovementDocumentLine:
    """Add one line to a draft."""
    locked = InventoryMovementDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    _validate_line_target(locked, line)

    base_quantity = _derive_base_quantity(locked, line)
    if base_quantity <= ZERO:
        raise ValidationError(
            _("The quantity must be greater than zero."), code="quantity_not_positive"
        )

    unit_cost: Decimal | None = None
    total_value: Decimal | None = None
    source_issue: InventoryMovementDocumentLine | None = None

    if locked.document_type == InventoryDocumentType.RECEIPT:
        if line.unit_cost is None:
            raise ValidationError(_("A receipt line needs a unit cost."), code="unit_cost_required")
        unit_cost = quantize_unit_price(line.unit_cost)
        if unit_cost <= ZERO:
            raise ValidationError(
                _("The unit cost must be greater than zero."), code="unit_cost_not_positive"
            )
        total_value = quantize_money(base_quantity * unit_cost)
        if total_value <= ZERO:
            raise ValidationError(
                _("The line value rounds to zero; a positive quantity needs a real value."),
                code="line_value_not_positive",
            )
    else:
        if line.unit_cost is not None:
            raise ValidationError(
                _("A %(type)s is valued by the ledger; it takes no entered cost."),
                code="unit_cost_not_accepted",
                params={"type": locked.get_document_type_display()},
            )
        if locked.document_type == InventoryDocumentType.RETURN_IN:
            source_issue = _validate_return_source(locked, line)
            remaining_quantity, _remaining_value = returnable(source_issue)
            if base_quantity > remaining_quantity:
                raise ValidationError(
                    _("Only %(remaining)s of that issue is still returnable."),
                    code="return_exceeds_issue",
                    params={"remaining": str(remaining_quantity)},
                )

    if locked.lines.filter(item=line.item, lot=line.lot).exists():
        raise ValidationError(
            _("This item and lot already have a line in this document."),
            code="duplicate_valuation_key",
        )

    last = locked.lines.order_by("-sequence").first()
    stored = InventoryMovementDocumentLine(
        document=locked,
        sequence=(last.sequence + 1) if last is not None else 1,
        item=line.item,
        lot=line.lot,
        package_conversion=line.package_conversion,
        entered_package_quantity=line.entered_package_quantity,
        measured_base_quantity=line.measured_base_quantity,
        base_quantity=base_quantity,
        unit_cost=unit_cost,
        total_value=total_value,
        source_issue_line=source_issue,
    )
    stored.full_clean()
    stored.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(stored),
        metadata={"line": stored.sequence, "item": line.item.code},
        reason="line added",
    )
    return stored


@transaction.atomic
def delete_line(*, line: InventoryMovementDocumentLine, reason: str = "") -> None:
    """Remove a line from a draft."""
    document = InventoryMovementDocument.objects.select_for_update().get(pk=line.document_id)
    _require_status(document, InventoryDocumentStatus.DRAFT, "not_a_draft")
    record_audit_event(
        action=AuditAction.UPDATED,
        target=document,
        branch=document.branch,
        previous_state=snapshot(line),
        reason=reason or "line removed",
        metadata={"line": line.sequence, "item": line.item.code},
    )
    line.delete()


@transaction.atomic
def replace_lines(
    *, document: InventoryMovementDocument, lines: Sequence[DocumentLineInput]
) -> InventoryMovementDocument:
    """Replace a draft's lines wholesale — the API PATCH shape."""
    locked = InventoryMovementDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    locked.lines.all().delete()
    for line in lines:
        add_line(document=locked, line=line)
    return locked


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def next_document_number(*, organization: Organization, document_type: str, year: int) -> str:
    """
    The next gapless number for this type and business year, under a row lock.

    Public because `apps.inventory.transfers` numbers its three documents from
    the same sequence table and by the same rule; two copies of a gapless
    counter is two chances to make it not gapless.
    """
    sequence, _created = InventoryDocumentSequence.objects.get_or_create(
        organization=organization, document_type=document_type, year=year
    )
    locked = InventoryDocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    locked.last_number += 1
    locked.save(update_fields=["last_number"])
    return f"{DOCUMENT_NUMBER_PREFIX[document_type]}-{year}-{locked.last_number:06d}"


def require_cost_center_where_the_account_demands_one(
    *, account: Account, cost_center: CostCenter | None
) -> None:
    """Public for the same reason as `next_document_number`: transfers apply
    the identical account policy and must apply it identically."""
    if account.requires_cost_center and cost_center is None:
        raise ValidationError(
            _("Account %(code)s requires a cost center."),
            code="cost_center_required",
            params={"code": account.code},
        )


def _effect_of(
    document: InventoryMovementDocument,
    line: InventoryMovementDocumentLine,
    *,
    unit_cost: Decimal | None,
    control_account: Account | None,
    inbound_value: Decimal | None = None,
) -> MovementInput:
    return MovementInput(
        warehouse=document.warehouse,
        item=line.item,
        movement_type=MOVEMENT_TYPE[document.document_type],
        quantity=line.base_quantity,
        effect_key=f"{document.document_type.lower()}-line:{line.line_uid}",
        lot=line.lot,
        unit_cost=unit_cost,
        source_conversion=line.package_conversion,
        control_account=control_account,
        inbound_value=inbound_value,
    )


def _standing_control_account(
    document: InventoryMovementDocument, line: InventoryMovementDocumentLine
) -> Account | None:
    """
    The account the stock at this position currently sits in.

    Read from the balance, never resolved afresh — that is the §D rule that
    stops an issue crediting stock out of an account it never entered.
    """
    balance = (
        StockBalance.objects.filter(warehouse=document.warehouse, item=line.item, lot=line.lot)
        .select_related("control_account")
        .first()
    )
    return balance.control_account if balance is not None else None


@transaction.atomic
def post_document(*, document: InventoryMovementDocument) -> InventoryMovementDocument:
    """
    Post a draft to both ledgers, atomically.

    One transaction produces the movements, the balances, the valuation layers
    where the movement is inbound, the gapless document number, the balanced
    journal, every line-level link between them, and the audit event — or none
    of it. See the module docstring for the lock order.
    """
    # 1. The document row.
    locked = InventoryMovementDocument.objects.select_for_update().get(pk=document.pk)
    if locked.status == InventoryDocumentStatus.POSTED:
        raise ValidationError(_("This document is already posted."), code="already_posted")
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")

    actor = get_actor()
    if actor is None:
        raise ValidationError(
            _("Posting needs a signed-in actor to record."), code="actor_required"
        )

    lines = list(
        locked.lines.select_related(
            "item",
            "item__category",
            "item__category__parent",
            "item__category__parent__parent",
            "lot",
            "package_conversion",
            "source_issue_line",
            "source_issue_line__document",
        ).order_by("sequence")
    )
    if not lines:
        raise ValidationError(_("An empty document cannot be posted."), code="no_lines")
    for line in lines:
        _validate_line_target(locked, DocumentLineInput(item=line.item, lot=line.lot))

    # The business date is fixed here, with the branch settings that derived
    # it, and both are stored. A cutoff changed later cannot restate which
    # period this document belongs to (§B).
    day = resolve_business_day(locked.branch, locked.effective_at)
    locked.business_date = day.business_date
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = resolve_period(organization=locked.organization, accounting_date=day.business_date)
    validate_period_accepts_postings(period)

    # 2. The organization's mappings, shared — above the stock keys in the
    # global order, so no mutation can interleave with the resolution below.
    lock_account_mappings_shared(locked.organization_id)

    # 3. The source issue lines, for a return: locked before their remaining
    # returnable amount is read, so two concurrent returns against one issue
    # cannot both see the same quantity as available. Ordered by primary key,
    # so two returns naming the same two issues cannot deadlock.
    if locked.document_type == InventoryDocumentType.RETURN_IN:
        source_ids = sorted(
            line.source_issue_line_id for line in lines if line.source_issue_line_id is not None
        )
        list(
            InventoryMovementDocumentLine.objects.select_for_update()
            .filter(pk__in=source_ids)
            .order_by("pk")
        )

    return _post_effects(locked, lines, period=period, actor=actor)


def _post_effects(
    document: InventoryMovementDocument,
    lines: list[InventoryMovementDocumentLine],
    *,
    period: AccountingPeriod,
    actor: User,
) -> InventoryMovementDocument:
    """The half of posting that differs per document type, then the half that does not."""
    if document.document_type == InventoryDocumentType.RECEIPT:
        plan = _plan_receipt(document, lines)
    elif document.document_type == InventoryDocumentType.ISSUE:
        plan = _plan_issue(document, lines)
    else:
        plan = _plan_return(document, lines)

    # 4/5. Stock effects, through the kernel's own locking and valuation.
    stock_entry = post_stock_entry(
        organization=document.organization,
        effects=plan.effects,
        idempotency_key=f"{document.document_type.lower()}:{document.public_id}",
        effective_at=document.effective_at,
        business_date=document.business_date,
        source_document_type=document.document_type,
        source_document_id=str(document.public_id),
        source_event=SourceEvent.POSTED,
        reference=document.evidence_reference,
        reason=document.narration or str(document.get_document_type_display()),
    )
    movement_by_key = {movement.effect_key: movement for movement in stock_entry.movements.all()}

    # An issue and a return learn their value only now: the kernel computed it
    # from the moving average, or mirrored it from the original issue.
    for line in lines:
        movement = movement_by_key[f"{document.document_type.lower()}-line:{line.line_uid}"]
        if line.total_value is None:
            line.total_value = abs(movement.inventory_value)
            line.unit_cost = movement.unit_cost

    # 6. The gapless human number, now that no domain reason can fail.
    document.document_number = next_document_number(
        organization=document.organization,
        document_type=document.document_type,
        year=period.fiscal_year.year,
    )

    # 7. The journal.
    journal = post_entry(
        organization=document.organization,
        accounting_date=document.business_date,
        lines=_journal_lines(document, lines, plan=plan),
        idempotency_key=f"{document.document_type.lower()}-journal:{document.public_id}",
        document_date=document.business_date,
        narration=document.narration or str(document.get_document_type_display()),
        source_document_type=document.document_type,
        source_document_id=str(document.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE[document.document_type],
    )
    # The stock posting names its journal, which is what arms the conditional
    # control-account invariant over its movements (§S).
    link_journal_entry(entry=stock_entry, journal=journal)

    _link_lines(document, lines, plan=plan, movements=movement_by_key, journal=journal)

    document.stock_entry = stock_entry
    document.journal_entry = journal
    document.status = InventoryDocumentStatus.POSTED
    document.posted_by = actor
    document.posted_at = timezone.now()
    document.save(
        update_fields=[
            "business_date",
            "business_date_timezone",
            "business_day_start",
            "document_number",
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
        target=document,
        branch=document.branch,
        new_state=snapshot(document),
        source_document_type=document.document_type,
        source_document_id=str(document.public_id),
        metadata={
            "document_number": document.document_number,
            "stock_entry": stock_entry.pk,
            "journal_entry": journal.entry_number,
            "line_count": len(lines),
        },
    )
    return document


@dataclass
class _Plan:
    """The resolved accounts and effects for one posting."""

    effects: list[MovementInput]
    #: line pk -> (inventory account, contra account, contra cost centre)
    accounts: dict[int, tuple[Account, Account, CostCenter | None]]
    #: line pk -> the resolution rows, for the snapshot
    mappings: dict[int, tuple[InventoryAccountMapping | None, OrganizationAccountMapping | None]]


def _plan_receipt(
    document: InventoryMovementDocument, lines: list[InventoryMovementDocumentLine]
) -> _Plan:
    """Dr resolved INVENTORY_CONTROL, Cr GOODS_RECEIVED_NOT_INVOICED."""
    grni = resolve_inventory_account(
        organization=document.organization,
        role=GOODS_RECEIVED_NOT_INVOICED,
        item=None,
        on_date=document.business_date,
    )
    require_cost_center_where_the_account_demands_one(
        account=grni.account, cost_center=document.cost_center
    )

    effects: list[MovementInput] = []
    accounts: dict[int, tuple[Account, Account, CostCenter | None]] = {}
    mappings: dict[
        int, tuple[InventoryAccountMapping | None, OrganizationAccountMapping | None]
    ] = {}
    for line in lines:
        control = resolve_inventory_account(
            organization=document.organization,
            role=INVENTORY_CONTROL,
            item=line.item,
            on_date=document.business_date,
        )
        require_cost_center_where_the_account_demands_one(
            account=control.account, cost_center=document.cost_center
        )
        accounts[line.pk] = (control.account, grni.account, None)
        mappings[line.pk] = (control.inventory_mapping, control.organization_mapping)
        effects.append(
            _effect_of(
                document,
                line,
                unit_cost=line.unit_cost,
                control_account=control.account,
            )
        )
    return _Plan(effects=effects, accounts=accounts, mappings=mappings)


def _plan_issue(
    document: InventoryMovementDocument, lines: list[InventoryMovementDocumentLine]
) -> _Plan:
    """
    Dr resolved INVENTORY_CONSUMPTION, Cr the account the stock is standing in.

    The credit side is read from the balance, not resolved: value leaves the
    account it entered. Where the position carries no account — stock posted
    before any mapping existed — the resolver answers, and the kernel's
    continuity check keeps the two consistent from then on.
    """
    effects: list[MovementInput] = []
    accounts: dict[int, tuple[Account, Account, CostCenter | None]] = {}
    mappings: dict[
        int, tuple[InventoryAccountMapping | None, OrganizationAccountMapping | None]
    ] = {}
    for line in lines:
        consumption = resolve_inventory_account(
            organization=document.organization,
            role=INVENTORY_CONSUMPTION,
            item=line.item,
            on_date=document.business_date,
        )
        require_cost_center_where_the_account_demands_one(
            account=consumption.account, cost_center=document.cost_center
        )
        standing = _standing_control_account(document, line)
        if standing is None:
            standing = resolve_inventory_account(
                organization=document.organization,
                role=INVENTORY_CONTROL,
                item=line.item,
                on_date=document.business_date,
            ).account
        accounts[line.pk] = (standing, consumption.account, document.cost_center)
        mappings[line.pk] = (consumption.inventory_mapping, consumption.organization_mapping)
        effects.append(_effect_of(document, line, unit_cost=None, control_account=standing))
    return _Plan(effects=effects, accounts=accounts, mappings=mappings)


def _plan_return(
    document: InventoryMovementDocument, lines: list[InventoryMovementDocumentLine]
) -> _Plan:
    """
    Mirror the returned part of the original issue, using **its** accounts.

    Today's consumption mapping is not consulted: the pair must net to zero in
    the account and cost centre the consumption actually landed in, or the
    expense stays booked somewhere the return never credited.

    The value is the original issue's, not the current average — and the final
    return of everything left takes the exact remaining value, so cumulative
    returns equal the original issue to the dinar with no residual.
    """
    effects: list[MovementInput] = []
    accounts: dict[int, tuple[Account, Account, CostCenter | None]] = {}
    mappings: dict[
        int, tuple[InventoryAccountMapping | None, OrganizationAccountMapping | None]
    ] = {}
    for line in lines:
        source = line.source_issue_line
        assert source is not None  # noqa: S101 - the trigger refuses a return without one
        remaining_quantity, remaining_value = returnable(source)
        if line.base_quantity > remaining_quantity:
            raise ValidationError(
                _("Only %(remaining)s of that issue is still returnable."),
                code="return_exceeds_issue",
                params={"remaining": str(remaining_quantity)},
            )
        if source.inventory_account is None or source.contra_account is None:
            raise ValidationError(  # pragma: no cover - a posted issue always has both
                _("The original issue carries no accounts to return against."),
                code="source_issue_has_no_accounts",
            )

        if line.base_quantity == remaining_quantity:
            # The last return takes everything left, so the cumulative returns
            # equal the issue exactly rather than leaving a rounding residual
            # that no later document could ever clear.
            value = remaining_value
        else:
            issue_unit_cost = source.unit_cost or ZERO
            value = quantize_money(line.base_quantity * issue_unit_cost)
            if value > remaining_value:
                value = remaining_value
        if value <= ZERO:
            raise ValidationError(
                _("The return value rounds to zero; there is nothing left to return."),
                code="return_value_not_positive",
            )

        unit_cost = quantize_unit_price(value / line.base_quantity)
        line.total_value = value
        line.unit_cost = unit_cost
        accounts[line.pk] = (
            source.inventory_account,
            source.contra_account,
            source.contra_cost_center,
        )
        mappings[line.pk] = (source.resolved_mapping, source.resolved_organization_mapping)
        effects.append(
            _effect_of(
                document,
                line,
                unit_cost=unit_cost,
                control_account=source.inventory_account,
                # The exact figure, not `quantity x unit_cost` re-derived: a
                # final return must land on the remaining value to the dinar.
                inbound_value=value,
            )
        )
    return _Plan(effects=effects, accounts=accounts, mappings=mappings)


def _journal_lines(
    document: InventoryMovementDocument,
    lines: list[InventoryMovementDocumentLine],
    *,
    plan: _Plan,
) -> list[PostingLine]:
    """
    Group the stored line values into balanced journal lines.

    Inventory is debited by a receipt and a return and credited by an issue;
    the contra side is the mirror. Grouping keys differ by what can legally
    vary within one document: inventory by account, contra by account **and
    cost centre**, because a return reuses each original issue's centre and
    two lines can legitimately carry different ones.

    Every figure is a sum of stored 3-decimal line values. The grand total is
    never rounded on its own.
    """
    inventory_side: dict[int, Decimal] = {}
    contra_side: dict[tuple[int, int | None], Decimal] = {}
    accounts: dict[int, Account] = {}
    centers: dict[int, CostCenter] = {}

    for line in lines:
        inventory_account, contra_account, contra_center = plan.accounts[line.pk]
        value = line.total_value or ZERO
        accounts[inventory_account.pk] = inventory_account
        accounts[contra_account.pk] = contra_account
        if contra_center is not None:
            centers[contra_center.pk] = contra_center
        inventory_side[inventory_account.pk] = (
            inventory_side.get(inventory_account.pk, ZERO) + value
        )
        contra_key = (contra_account.pk, contra_center.pk if contra_center else None)
        contra_side[contra_key] = contra_side.get(contra_key, ZERO) + value

    inventory_is_debit = document.document_type in (
        InventoryDocumentType.RECEIPT,
        InventoryDocumentType.RETURN_IN,
    )

    posting_lines: list[PostingLine] = []
    for account_id, amount in sorted(
        inventory_side.items(), key=lambda pair: accounts[pair[0]].code
    ):
        posting_lines.append(
            PostingLine(
                account=accounts[account_id],
                branch=document.branch,
                debit=amount if inventory_is_debit else ZERO,
                credit=ZERO if inventory_is_debit else amount,
            )
        )
    for (account_id, center_id), amount in sorted(
        contra_side.items(), key=lambda pair: (accounts[pair[0][0]].code, pair[0][1] or 0)
    ):
        posting_lines.append(
            PostingLine(
                account=accounts[account_id],
                branch=document.branch,
                cost_center=centers.get(center_id) if center_id is not None else None,
                debit=ZERO if inventory_is_debit else amount,
                credit=amount if inventory_is_debit else ZERO,
            )
        )
    return posting_lines


def _link_lines(
    document: InventoryMovementDocument,
    lines: list[InventoryMovementDocumentLine],
    *,
    plan: _Plan,
    movements: dict[str, StockMovement],
    journal: JournalEntry,
) -> None:
    """Write the immutable traceability, while the lines are still unfrozen."""
    inventory_is_debit = document.document_type in (
        InventoryDocumentType.RECEIPT,
        InventoryDocumentType.RETURN_IN,
    )
    journal_lines = list(journal.lines.all())
    by_account: dict[int, JournalLine] = {}
    for journal_line in journal_lines:
        is_inventory_line = (
            journal_line.debit > ZERO if inventory_is_debit else journal_line.credit > ZERO
        )
        if is_inventory_line:
            by_account[journal_line.account_id] = journal_line

    for line in lines:
        inventory_account, contra_account, contra_center = plan.accounts[line.pk]
        inventory_mapping, organization_mapping = plan.mappings[line.pk]
        line.inventory_account = inventory_account
        line.contra_account = contra_account
        line.contra_cost_center = contra_center
        line.resolved_mapping = inventory_mapping
        line.resolved_organization_mapping = organization_mapping
        line.movement = movements[f"{document.document_type.lower()}-line:{line.line_uid}"]
        line.journal_line = by_account[inventory_account.pk]
        line.save(
            update_fields=[
                "unit_cost",
                "total_value",
                "inventory_account",
                "contra_account",
                "contra_cost_center",
                "resolved_mapping",
                "resolved_organization_mapping",
                "movement",
                "journal_line",
                "updated_at",
            ]
        )


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_document(
    *, document: InventoryMovementDocument, reason: str
) -> InventoryMovementDocument:
    """
    Reverse the whole document — never a line, and never as a substitute for
    a return.

    Both mirrors are exact: each movement takes back its original quantity and
    value, and the reversing journal mirrors the original lines, whatever the
    averages or mappings have since become. Availability still applies, so a
    receipt whose goods have been consumed cannot be un-received.

    An issue with **active returns against it** cannot be reversed: the
    returns took their value from it, so undoing it would leave them
    referencing something that no longer happened. Reverse the returns first.
    """
    locked = InventoryMovementDocument.objects.select_for_update().get(pk=document.pk)
    if locked.status == InventoryDocumentStatus.REVERSED:
        raise ValidationError(_("This document is already reversed."), code="already_reversed")
    _require_status(locked, InventoryDocumentStatus.POSTED, "not_posted")
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")
    actor = get_actor()
    if actor is None:
        raise ValidationError(
            _("Reversing needs a signed-in actor to record."), code="actor_required"
        )

    if locked.document_type == InventoryDocumentType.ISSUE:
        dependent = InventoryMovementDocumentLine.objects.filter(
            source_issue_line__document=locked,
            document__status=InventoryDocumentStatus.POSTED,
        ).select_related("document")
        first = dependent.first()
        if first is not None:
            raise ValidationError(
                _(
                    "Return %(number)s took stock back against this issue. Reverse the "
                    "return first."
                ),
                code="issue_has_active_returns",
                params={"number": first.document.document_number},
            )

    now = timezone.now()
    reversal_business_date = resolve_business_day(locked.branch, now).business_date

    assert locked.stock_entry is not None  # noqa: S101 - a POSTED document links one
    assert locked.journal_entry is not None  # noqa: S101
    reversing_stock = reverse_stock_entry(
        entry=locked.stock_entry,
        idempotency_key=f"{locked.document_type.lower()}-reverse:{locked.public_id}",
        reason=reason.strip(),
        effective_at=now,
        business_date=reversal_business_date,
    )
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"{locked.document_type.lower()}-journal-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )
    link_journal_entry(entry=reversing_stock, journal=reversal_journal)

    locked.status = InventoryDocumentStatus.REVERSED
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
        source_document_type=locked.document_type,
        source_document_id=str(locked.public_id),
        metadata={"reversal_journal": reversal_journal.entry_number},
    )
    return locked
