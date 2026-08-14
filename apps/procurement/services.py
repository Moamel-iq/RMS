"""
Procurement master-data commands.

Every write goes through here. Models stay free of business logic, and no
caller can construct a half-valid row by assigning fields directly.

Supplier master data is not a posted ledger, so these are ordinary create and
update services rather than the command-and-reversal shape the accounting
kernel needs. What they share with the kernel is the discipline: validate
first, take `previous_state` from the database rather than from a form-mutated
instance, and record an audit event for anything consequential.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    InventoryLot,
    InventoryReasonCode,
    ItemPackageConversion,
    PackageUnit,
    StockLocation,
    Warehouse,
)
from apps.organizations.models import Branch, Organization
from apps.procurement.lifecycle import lock_and_require_status
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    ProcurementDocumentSequence,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseOrderVersion,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
    QualityResult,
    Supplier,
    SupplierItem,
    SupplierQuotation,
    SupplierQuotationLine,
    SupplierQuotationStatus,
)
from apps.users.models import User
from apps.users.phone import normalize_iraqi_mobile


def canonical_code(value: str) -> str:
    """
    The one form a supplier code is ever stored in.

    `strip().upper()` before validation and before storage, so uniqueness is
    case-insensitive in effect without a functional index: `meat-01`,
    `Meat-01` and `MEAT-01 ` are one supplier.
    """
    return value.strip().upper()


def _require_code(value: str) -> str:
    canonical = canonical_code(value)
    if not canonical:
        raise ValidationError(_("A code is required."), code="code_required")
    return canonical


def _clean_phone(value: str) -> str:
    """
    Canonicalise a supplier's phone, or refuse it.

    Suppliers are chased by phone constantly in this business, and two
    spellings of one number is how a late delivery gets called about twice.
    The same canonical form users are stored in, so a search matches whichever
    way it was typed.

    Blank stays blank. A supplier with no number on file is ordinary, and an
    empty string is not a malformed number.
    """
    text = value.strip()
    return normalize_iraqi_mobile(text) if text else ""


@transaction.atomic
def create_supplier(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    name_en: str = "",
    contact_name: str = "",
    phone: str = "",
    email: str = "",
    address: str = "",
    payment_terms_days: int = 0,
    credit_limit: Decimal | None = None,
    notes: str = "",
) -> Supplier:
    """
    Add a supplier to the organization.

    No balance is created, because there is no balance field. A new supplier
    is owed nothing because nothing has been posted against them — not because
    a zero was written somewhere that could later disagree with the documents.
    """
    supplier = Supplier(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        contact_name=contact_name.strip(),
        phone=_clean_phone(phone),
        email=email.strip(),
        address=address.strip(),
        payment_terms_days=payment_terms_days,
        credit_limit=credit_limit,
        notes=notes.strip(),
    )
    supplier.full_clean()
    supplier.save()
    record_audit_event(action=AuditAction.CREATED, target=supplier, new_state=snapshot(supplier))
    return supplier


@transaction.atomic
def update_supplier(
    *,
    supplier: Supplier,
    name_ar: str,
    name_en: str = "",
    contact_name: str = "",
    phone: str = "",
    email: str = "",
    address: str = "",
    payment_terms_days: int = 0,
    credit_limit: Decimal | None = None,
    notes: str = "",
    is_active: bool = True,
) -> Supplier:
    """
    Correct a supplier's details, or archive and reactivate one.

    The code and the organization are absent from the signature on purpose. A
    code is the identity every report groups by, and re-homing a supplier to
    another organization would move its whole document history across a
    tenancy boundary.

    Archiving is `is_active=False` and is never a delete: posted invoices,
    receipts and payments point at this row, and the code stays reserved
    forever. Reusing `MEAT-01` for a different company would silently rewrite
    every report that groups by supplier.

    Payment terms change here and take effect on **new** documents only.
    Documents already raised carry their own snapshot, so January's due dates
    do not move when March's terms are renegotiated.
    """
    previous = snapshot(Supplier.objects.get(pk=supplier.pk))

    supplier.name_ar = name_ar.strip()
    supplier.name_en = name_en.strip()
    supplier.contact_name = contact_name.strip()
    supplier.phone = _clean_phone(phone)
    supplier.email = email.strip()
    supplier.address = address.strip()
    supplier.payment_terms_days = payment_terms_days
    supplier.credit_limit = credit_limit
    supplier.notes = notes.strip()
    supplier.is_active = is_active
    supplier.full_clean()
    supplier.save()

    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=supplier,
        previous_state=previous,
        new_state=snapshot(supplier),
    )
    return supplier


# ---------------------------------------------------------------------------
# Supplier item catalogue
# ---------------------------------------------------------------------------


def _active_conversion(
    *, item: InventoryItem, package_unit: PackageUnit, on: datetime.date
) -> ItemPackageConversion | None:
    """The conversion the item itself declares for this package, on a date."""
    return (
        ItemPackageConversion.objects.filter(
            item=item,
            package_unit=package_unit,
            is_active=True,
            effective_from__lte=on,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on))
        .order_by("-effective_from")
        .first()
    )


def _validate_catalogue_row(
    *,
    supplier: Supplier,
    item: InventoryItem,
    package_unit: PackageUnit | None,
    effective_from: datetime.date,
    effective_to: datetime.date | None,
) -> None:
    """
    The things a catalogue row cannot be, checked before anything is saved.

    The package check is the one that matters. A supplier may not name a
    package the *item* has no conversion for, because a receipt against that
    row would have no factor to snapshot — and the failure would then surface
    at the receipt, in the warehouse, at the moment goods are being counted,
    instead of here where somebody chose it.
    """
    if supplier.organization_id != item.organization_id:
        raise ValidationError(
            _("The supplier and the item belong to different organizations."),
            code="organization_mismatch",
        )
    if effective_to is not None and effective_to < effective_from:
        raise ValidationError(
            _("The effective period ends before it begins."), code="period_reversed"
        )
    if package_unit is None:
        return
    if package_unit.organization_id != item.organization_id:
        raise ValidationError(
            _("The package unit belongs to another organization."),
            code="organization_mismatch",
        )
    if _active_conversion(item=item, package_unit=package_unit, on=effective_from) is None:
        raise ValidationError(
            _(
                "Item %(item)s has no conversion for package %(package)s effective on "
                "%(date)s. Record the conversion on the item first."
            ),
            code="no_conversion_for_package",
            params={
                "item": item.code,
                "package": package_unit.code,
                "date": effective_from.isoformat(),
            },
        )


@transaction.atomic
def create_supplier_item(
    *,
    supplier: Supplier,
    item: InventoryItem,
    effective_from: datetime.date,
    package_unit: PackageUnit | None = None,
    supplier_sku: str = "",
    supplier_description: str = "",
    last_quoted_price: Decimal | None = None,
    lead_time_days: int | None = None,
    minimum_order_quantity: Decimal | None = None,
    is_preferred: bool = False,
    effective_to: datetime.date | None = None,
    notes: str = "",
) -> SupplierItem:
    """
    Add one supplier terms row for one item.

    `last_quoted_price` is planning information. Nothing in the posting path
    reads it, and an architectural test proves nothing does — PRC-005. A
    receipt carries its own price, and that is what values the stock.
    """
    _validate_catalogue_row(
        supplier=supplier,
        item=item,
        package_unit=package_unit,
        effective_from=effective_from,
        effective_to=effective_to,
    )

    # Versions run per (supplier, item, package), so a later period for the
    # same three is the next version rather than a duplicate of the first.
    # Assigned here rather than left at the default: the unique constraint
    # counts the triple *and* the version, so a second row defaulting to 1
    # would collide with the row it succeeds.
    highest = (
        SupplierItem.objects.filter(supplier=supplier, item=item, package_unit=package_unit)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )

    row = SupplierItem(
        organization=supplier.organization,
        supplier=supplier,
        item=item,
        package_unit=package_unit,
        # Stripped and otherwise untouched. The case belongs to whoever issued
        # the reference, exactly as ADR-017 argues for `source_document_id`.
        supplier_sku=supplier_sku.strip(),
        supplier_description=supplier_description.strip(),
        last_quoted_price=last_quoted_price,
        lead_time_days=lead_time_days,
        minimum_order_quantity=minimum_order_quantity,
        is_preferred=is_preferred,
        effective_from=effective_from,
        effective_to=effective_to,
        version=(highest or 0) + 1,
        notes=notes.strip(),
    )
    row.full_clean()
    row.save()
    record_audit_event(action=AuditAction.CREATED, target=row, new_state=snapshot(row))
    return row


@transaction.atomic
def update_supplier_item(
    *,
    supplier_item: SupplierItem,
    supplier_sku: str = "",
    supplier_description: str = "",
    last_quoted_price: Decimal | None = None,
    lead_time_days: int | None = None,
    minimum_order_quantity: Decimal | None = None,
    is_preferred: bool = False,
    effective_to: datetime.date | None = None,
    notes: str = "",
    is_active: bool = True,
) -> SupplierItem:
    """
    Correct a catalogue row, or archive and reactivate one.

    Supplier, item, package and `effective_from` are absent from the signature
    on purpose. Changing any of them makes this a **different** row rather than
    a corrected one, and a document that referenced it would quietly come to
    mean something else. Those go through `supersede_supplier_item`, which
    leaves the original readable.
    """
    previous = snapshot(SupplierItem.objects.get(pk=supplier_item.pk))

    if effective_to is not None and effective_to < supplier_item.effective_from:
        raise ValidationError(
            _("The effective period ends before it begins."), code="period_reversed"
        )

    supplier_item.supplier_sku = supplier_sku.strip()
    supplier_item.supplier_description = supplier_description.strip()
    supplier_item.last_quoted_price = last_quoted_price
    supplier_item.lead_time_days = lead_time_days
    supplier_item.minimum_order_quantity = minimum_order_quantity
    supplier_item.is_preferred = is_preferred
    supplier_item.effective_to = effective_to
    supplier_item.notes = notes.strip()
    supplier_item.is_active = is_active
    supplier_item.full_clean()
    supplier_item.save()

    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=supplier_item,
        previous_state=previous,
        new_state=snapshot(supplier_item),
    )
    return supplier_item


@transaction.atomic
def supersede_supplier_item(
    *,
    supplier_item: SupplierItem,
    effective_from: datetime.date,
    package_unit: PackageUnit | None = None,
    supplier_sku: str | None = None,
    supplier_description: str | None = None,
    last_quoted_price: Decimal | None = None,
    lead_time_days: int | None = None,
    minimum_order_quantity: Decimal | None = None,
    is_preferred: bool | None = None,
    notes: str = "",
) -> SupplierItem:
    """
    Close the current terms and open the next version from a date.

    The old row keeps its period and stays readable, so any document that
    referenced it still points at what it actually meant. The replacement
    starts the day after the old one closes, which the exclusion constraint
    would enforce anyway — this arithmetic exists to satisfy that rule, not to
    be trusted instead of it.

    The replacement takes `version + 1` for the same
    `(supplier, item, package)`, so "how have this supplier terms moved over
    time" is answerable by ordering rather than by guesswork.
    """
    if effective_from <= supplier_item.effective_from:
        raise ValidationError(
            _("New terms must begin after the terms they replace."),
            code="supersede_not_later",
        )

    current = SupplierItem.objects.select_for_update().get(pk=supplier_item.pk)
    previous = snapshot(current)

    closes_on = effective_from - datetime.timedelta(days=1)
    if current.effective_to is None or current.effective_to > closes_on:
        current.effective_to = closes_on
    # Preference moves with the terms: the superseded row is no longer where
    # this item is normally bought, whatever the replacement turns out to be.
    was_preferred = current.is_preferred
    current.is_preferred = False
    current.full_clean()
    current.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
        reason="superseded",
    )

    package = package_unit if package_unit is not None else current.package_unit
    replacement = create_supplier_item(
        supplier=current.supplier,
        item=current.item,
        effective_from=effective_from,
        package_unit=package,
        supplier_sku=current.supplier_sku if supplier_sku is None else supplier_sku,
        supplier_description=(
            current.supplier_description if supplier_description is None else supplier_description
        ),
        last_quoted_price=last_quoted_price,
        lead_time_days=lead_time_days if lead_time_days is not None else current.lead_time_days,
        minimum_order_quantity=(
            minimum_order_quantity
            if minimum_order_quantity is not None
            else current.minimum_order_quantity
        ),
        is_preferred=was_preferred if is_preferred is None else is_preferred,
        notes=notes,
    )
    # `create_supplier_item` already numbered it: the version sequence belongs
    # to creation, not to this particular way of reaching it.
    return replacement


# ---------------------------------------------------------------------------
# Purchase requests
# ---------------------------------------------------------------------------

#: Prefix per procurement document type. The rest join as their tasks land.
DOCUMENT_NUMBER_PREFIX = {
    "PURCHASE_REQUEST": "PR",
    "SUPPLIER_QUOTATION": "QT",
    "PURCHASE_ORDER": "PO",
    "GOODS_RECEIPT": "GRN",
    "SUPPLIER_INVOICE": "SINV",
    "SUPPLIER_RETURN": "SRET",
}


def next_document_number(*, organization: Organization, document_type: str, year: int) -> str:
    """
    The next gapless number for this type and year, under a row lock.

    The lock is the point. Two people submitting at the same instant both read
    the same counter without it, and a duplicated document number is the kind
    of defect nobody notices until an auditor asks which of the two `PR-2026-
    000014` documents was approved.
    """
    sequence, _created = ProcurementDocumentSequence.objects.get_or_create(
        organization=organization, document_type=document_type, year=year
    )
    locked = ProcurementDocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    locked.last_number += 1
    locked.save(update_fields=["last_number"])
    return f"{DOCUMENT_NUMBER_PREFIX[document_type]}-{year}-{locked.last_number:06d}"


def _require_draft(request: PurchaseRequest) -> PurchaseRequest:
    """
    Only a draft may be edited — PRC-011. Returns the row it locked.

    A submitted request is what somebody is being asked to approve. Editing it
    underneath them would mean an approval attached to a document that no
    longer says what was approved.

    The status is re-read **from the database under a row lock**, never taken
    from the instance the caller passed. A caller holding an object loaded
    before submission would otherwise carry a stale `DRAFT` past this guard and
    add a line to a document somebody had already approved — and no database
    constraint would catch it, because "which lines existed when this was
    approved" is not something a column can say.
    """
    return lock_and_require_status(
        PurchaseRequest,
        request.pk,
        {PurchaseRequestStatus.DRAFT},
        code="request_not_editable",
        message=_("This request has been submitted and can no longer be edited."),
    )


@transaction.atomic
def create_purchase_request(
    *,
    branch: Branch,
    requested_by: User,
    warehouse: Warehouse,
    required_date: datetime.date,
    purpose: str,
    location: StockLocation | None = None,
    notes: str = "",
) -> PurchaseRequest:
    """
    Open a draft request for a branch.

    No number is drawn yet. A draft that is abandoned would otherwise burn one
    out of a gapless sequence, and a gap in a document series is a question
    somebody has to answer years later.
    """
    if warehouse.branch_id != branch.pk:
        raise ValidationError(
            _("Warehouse %(code)s does not belong to this branch."),
            code="warehouse_branch_mismatch",
            params={"code": warehouse.code},
        )
    if location is not None and location.warehouse_id != warehouse.pk:
        raise ValidationError(
            _("Location %(code)s is not in this warehouse."),
            code="location_warehouse_mismatch",
            params={"code": location.code},
        )
    if not purpose.strip():
        raise ValidationError(_("A purpose is required."), code="purpose_required")

    request = PurchaseRequest(
        organization=branch.organization,
        branch=branch,
        requested_by=requested_by,
        warehouse=warehouse,
        location=location,
        required_date=required_date,
        purpose=purpose.strip(),
        notes=notes.strip(),
    )
    request.full_clean()
    request.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=request,
        branch=branch,
        new_state=snapshot(request),
    )
    return request


@transaction.atomic
def add_request_line(
    *,
    request: PurchaseRequest,
    item: InventoryItem,
    entered_quantity: Decimal,
    package_unit: PackageUnit | None = None,
    preferred_supplier: Supplier | None = None,
    note: str = "",
) -> PurchaseRequestLine:
    """
    Add a wanted item to a draft, resolving its base quantity once.

    The conversion is snapshotted — the row, its version and its factor —
    exactly as a posted movement snapshots it. Nothing here reaches the ledger,
    but the order raised from this request will, and it must buy the amount
    that was approved rather than the amount today's factor would imply.
    """
    request = _require_draft(request)

    if item.organization_id != request.organization_id:
        raise ValidationError(
            _("The item belongs to another organization."), code="organization_mismatch"
        )
    if entered_quantity <= 0:
        raise ValidationError(
            _("A requested quantity must be greater than zero."), code="quantity_not_positive"
        )

    conversion = None
    factor = None
    if package_unit is not None:
        conversion = _active_conversion(
            item=item, package_unit=package_unit, on=request.required_date
        )
        if conversion is None:
            raise ValidationError(
                _("Item %(item)s has no conversion for package %(package)s on %(date)s."),
                code="no_conversion_for_package",
                params={
                    "item": item.code,
                    "package": package_unit.code,
                    "date": request.required_date.isoformat(),
                },
            )
        factor = conversion.factor_to_base
        base = quantize_quantity(entered_quantity * factor)
    else:
        base = quantize_quantity(entered_quantity)

    highest = (
        PurchaseRequestLine.objects.filter(request=request)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    line = PurchaseRequestLine(
        request=request,
        sequence=(highest or 0) + 1,
        item=item,
        package_unit=package_unit,
        conversion=conversion,
        conversion_version=conversion.version if conversion else None,
        conversion_factor=factor,
        entered_quantity=quantize_quantity(entered_quantity),
        base_quantity=base,
        preferred_supplier=preferred_supplier,
        note=note.strip(),
    )
    line.full_clean()
    line.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=line,
        branch=request.branch,
        new_state=snapshot(line),
    )
    return line


@transaction.atomic
def remove_request_line(*, line: PurchaseRequestLine) -> None:
    """Drop a line from a draft. Sequences are not renumbered."""
    _require_draft(line.request)
    previous = snapshot(line)
    branch = line.request.branch
    record_audit_event(
        action=AuditAction.DELETED,
        target=line,
        branch=branch,
        previous_state=previous,
    )
    line.delete()


@transaction.atomic
def submit_purchase_request(*, request: PurchaseRequest, actor: User) -> PurchaseRequest:
    """
    Freeze the lines and draw the document number.

    An empty request cannot be submitted: asking for nothing is not a request,
    and an approver would have nothing to decide about.
    """
    locked = _require_draft(request)
    previous = snapshot(locked)

    if not locked.lines.exists():
        raise ValidationError(
            _("A request with no lines cannot be submitted."), code="request_has_no_lines"
        )

    locked.status = PurchaseRequestStatus.SUBMITTED
    locked.submitted_by = actor
    locked.submitted_at = timezone.now()
    locked.number = next_document_number(
        organization=locked.organization,
        document_type="PURCHASE_REQUEST",
        year=locked.required_date.year,
    )
    locked.full_clean()
    locked.save()

    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


def _decide(
    *,
    request: PurchaseRequest,
    actor: User,
    status: str,
    action: AuditAction,
    reason: str,
    from_statuses: tuple[str, ...],
) -> PurchaseRequest:
    locked = PurchaseRequest.objects.select_for_update().get(pk=request.pk)
    previous = snapshot(locked)

    if locked.status not in from_statuses:
        raise ValidationError(
            _("Request %(number)s is %(status)s and cannot change to %(target)s."),
            code="illegal_transition",
            params={
                "number": locked.number or str(locked.public_id),
                "status": locked.status,
                "target": status,
            },
        )
    if locked.submitted_by_id == actor.pk:
        raise ValidationError(
            _("The person who submitted a request cannot decide it."),
            code="maker_is_not_checker",
        )

    locked.status = status
    locked.decided_by = actor
    locked.decided_at = timezone.now()
    locked.decision_reason = reason.strip()
    locked.full_clean()
    locked.save()

    record_audit_event(
        action=action,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


@transaction.atomic
def approve_purchase_request(
    *, request: PurchaseRequest, actor: User, reason: str = ""
) -> PurchaseRequest:
    """
    Agree the need. Still no stock and no money.

    Maker-checker is enforced here **and** by a database constraint. The
    service message is the useful one; the constraint is the one that holds
    when somebody reaches past the service.
    """
    return _decide(
        request=request,
        actor=actor,
        status=PurchaseRequestStatus.APPROVED,
        action=AuditAction.APPROVED,
        reason=reason,
        from_statuses=(PurchaseRequestStatus.SUBMITTED,),
    )


@transaction.atomic
def reject_purchase_request(
    *, request: PurchaseRequest, actor: User, reason: str
) -> PurchaseRequest:
    """Refuse the need. A reason is required and is not optional prose."""
    if not reason.strip():
        raise ValidationError(_("A reason is required."), code="reason_required")
    return _decide(
        request=request,
        actor=actor,
        status=PurchaseRequestStatus.REJECTED,
        action=AuditAction.REJECTED,
        reason=reason,
        from_statuses=(PurchaseRequestStatus.SUBMITTED,),
    )


@transaction.atomic
def cancel_purchase_request(
    *, request: PurchaseRequest, actor: User, reason: str
) -> PurchaseRequest:
    """
    Withdraw a request that is no longer wanted.

    Available from `DRAFT`, `SUBMITTED` and `APPROVED`, because a need can
    evaporate after somebody agreed to it and before anything was ordered.
    Cancelling from a draft is the one case where maker-checker does not
    apply — nobody has submitted it, so there is no checker to be.
    """
    if not reason.strip():
        raise ValidationError(_("A reason is required."), code="reason_required")

    locked = PurchaseRequest.objects.select_for_update().get(pk=request.pk)
    if locked.status == PurchaseRequestStatus.DRAFT:
        previous = snapshot(locked)
        locked.status = PurchaseRequestStatus.CANCELLED
        locked.decision_reason = reason.strip()
        # `decided_by` stays null: there was no submission, so the
        # maker-checker constraint has nothing to compare and correctly says
        # nothing about this transition.
        locked.full_clean()
        locked.save()
        record_audit_event(
            action=AuditAction.CANCELLED,
            target=locked,
            branch=locked.branch,
            previous_state=previous,
            new_state=snapshot(locked),
            reason=reason.strip(),
        )
        return locked

    return _decide(
        request=request,
        actor=actor,
        status=PurchaseRequestStatus.CANCELLED,
        action=AuditAction.CANCELLED,
        reason=reason,
        from_statuses=(PurchaseRequestStatus.SUBMITTED, PurchaseRequestStatus.APPROVED),
    )


# ---------------------------------------------------------------------------
# Supplier quotations
# ---------------------------------------------------------------------------


def _require_quotation_draft(quotation: SupplierQuotation) -> SupplierQuotation:
    """
    Only a draft may be edited, re-read under a row lock.

    Same reasoning as `_require_draft`: a status taken from the instance the
    caller happens to be holding is a status that may already be out of date,
    and the failure would be a priced line appearing on an offer somebody had
    already compared against.
    """
    return lock_and_require_status(
        SupplierQuotation,
        quotation.pk,
        {SupplierQuotationStatus.DRAFT},
        code="quotation_not_editable",
        message=_("This quotation has been received and can no longer be edited."),
    )


@transaction.atomic
def create_supplier_quotation(
    *,
    supplier: Supplier,
    recorded_by: User,
    quoted_at: datetime.date,
    request: PurchaseRequest | None = None,
    supplier_reference: str = "",
    valid_until: datetime.date | None = None,
    freight_amount: Decimal = Decimal("0.000"),
    other_charges: Decimal = Decimal("0.000"),
    evidence_reference: str = "",
    notes: str = "",
) -> SupplierQuotation:
    """
    Open a draft quotation. Nothing is committed by recording a price.

    A quotation may answer a request or stand alone: a buyer often asks what
    something costs before anybody raises a formal request, and refusing to
    record that would push the number into a notebook where no comparison can
    reach it.
    """
    if request is not None and request.organization_id != supplier.organization_id:
        raise ValidationError(
            _("The request belongs to another organization."), code="organization_mismatch"
        )
    if valid_until is not None and valid_until < quoted_at:
        raise ValidationError(
            _("A quotation cannot expire before it was given."), code="validity_reversed"
        )
    if freight_amount < 0 or other_charges < 0:
        raise ValidationError(_("Charges cannot be negative."), code="charge_negative")

    quotation = SupplierQuotation(
        organization=supplier.organization,
        supplier=supplier,
        request=request,
        recorded_by=recorded_by,
        quoted_at=quoted_at,
        valid_until=valid_until,
        # The supplier's own reference, kept as they wrote it.
        supplier_reference=supplier_reference.strip(),
        freight_amount=quantize_money(freight_amount),
        other_charges=quantize_money(other_charges),
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
    )
    quotation.full_clean()
    quotation.save()
    record_audit_event(action=AuditAction.CREATED, target=quotation, new_state=snapshot(quotation))
    return quotation


@transaction.atomic
def add_quotation_line(
    *,
    quotation: SupplierQuotation,
    item: InventoryItem,
    quantity: Decimal,
    unit_price: Decimal,
    package_unit: PackageUnit | None = None,
    supplier_item: SupplierItem | None = None,
    note: str = "",
) -> SupplierQuotationLine:
    """
    Price one item on a draft quotation.

    `line_total` is `quantity × unit_price`, quantized **once** at the storage
    boundary. Quantizing the multiplication's operands first and then again
    afterwards is how a total stops matching the price somebody was quoted
    (ADR-006).
    """
    quotation = _require_quotation_draft(quotation)

    if item.organization_id != quotation.organization_id:
        raise ValidationError(
            _("The item belongs to another organization."), code="organization_mismatch"
        )
    if quantity <= 0:
        raise ValidationError(
            _("A quoted quantity must be greater than zero."), code="quantity_not_positive"
        )
    if unit_price < 0:
        raise ValidationError(_("A price cannot be negative."), code="price_negative")
    if supplier_item is not None and supplier_item.supplier_id != quotation.supplier_id:
        raise ValidationError(
            _("That catalogue row belongs to another supplier."), code="supplier_mismatch"
        )

    conversion = None
    factor = None
    if package_unit is not None:
        conversion = _active_conversion(
            item=item, package_unit=package_unit, on=quotation.quoted_at
        )
        if conversion is None:
            raise ValidationError(
                _("Item %(item)s has no conversion for package %(package)s on %(date)s."),
                code="no_conversion_for_package",
                params={
                    "item": item.code,
                    "package": package_unit.code,
                    "date": quotation.quoted_at.isoformat(),
                },
            )
        factor = conversion.factor_to_base
        base = quantize_quantity(quantity * factor)
    else:
        base = quantize_quantity(quantity)

    highest = (
        SupplierQuotationLine.objects.filter(quotation=quotation)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    line = SupplierQuotationLine(
        quotation=quotation,
        sequence=(highest or 0) + 1,
        item=item,
        supplier_item=supplier_item,
        package_unit=package_unit,
        conversion=conversion,
        conversion_version=conversion.version if conversion else None,
        conversion_factor=factor,
        quantity=quantize_quantity(quantity),
        base_quantity=base,
        unit_price=quantize_unit_price(unit_price),
        line_total=quantize_money(quantity * unit_price),
        note=note.strip(),
    )
    line.full_clean()
    line.save()
    record_audit_event(action=AuditAction.CREATED, target=line, new_state=snapshot(line))
    return line


@transaction.atomic
def remove_quotation_line(*, line: SupplierQuotationLine) -> None:
    """Drop a line from a draft. Sequences are not renumbered."""
    _require_quotation_draft(line.quotation)
    previous = snapshot(line)
    record_audit_event(action=AuditAction.DELETED, target=line, previous_state=previous)
    line.delete()


@transaction.atomic
def submit_supplier_quotation(*, quotation: SupplierQuotation, actor: User) -> SupplierQuotation:
    """
    Record the offer as received, and draw its number.

    Evidence becomes mandatory here rather than at creation: a draft is
    somebody typing while reading a message, and a submitted quotation is a
    figure the business will make a decision on. A price nobody can trace back
    to what the supplier actually sent is the same problem an opening balance
    without a count sheet has.
    """
    locked = _require_quotation_draft(quotation)
    previous = snapshot(locked)

    if not locked.lines.exists():
        raise ValidationError(
            _("A quotation with no lines cannot be submitted."), code="quotation_has_no_lines"
        )
    if not locked.evidence_reference:
        raise ValidationError(
            _("An evidence reference is required before a quotation is used."),
            code="evidence_required",
        )

    locked.status = SupplierQuotationStatus.SUBMITTED
    locked.number = next_document_number(
        organization=locked.organization,
        document_type="SUPPLIER_QUOTATION",
        year=locked.quoted_at.year,
    )
    locked.full_clean()
    locked.save()

    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def decline_supplier_quotation(
    *, quotation: SupplierQuotation, actor: User, reason: str
) -> SupplierQuotation:
    """
    Set an offer aside without deleting it.

    A declined quotation is the other half of every award: "we chose this one"
    means nothing without the offers it was chosen over, and a comparison whose
    losing entries were deleted cannot be re-read a year later.
    """
    if not reason.strip():
        raise ValidationError(_("A reason is required."), code="reason_required")

    locked = SupplierQuotation.objects.select_for_update().get(pk=quotation.pk)
    if locked.status not in {
        SupplierQuotationStatus.SUBMITTED,
        SupplierQuotationStatus.DRAFT,
    }:
        raise ValidationError(
            _("Quotation %(number)s is %(status)s and cannot be declined."),
            code="illegal_transition",
            params={
                "number": locked.number or str(locked.public_id),
                "status": locked.status,
            },
        )

    previous = snapshot(locked)
    locked.status = SupplierQuotationStatus.DECLINED
    locked.notes = f"{locked.notes}\n{reason.strip()}".strip()
    locked.full_clean()
    locked.save()
    record_audit_event(
        action=AuditAction.REJECTED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


def _require_order_draft(order: PurchaseOrder) -> PurchaseOrder:
    """
    Only a draft may be edited, re-read under a row lock.

    The third guard of this shape in procurement, and the third for the same
    reason: a status taken from the caller's instance is a status that may
    already be stale, and here the failure would be a line changing on an order
    the supplier has already been sent.
    """
    return lock_and_require_status(
        PurchaseOrder,
        order.pk,
        {PurchaseOrderStatus.DRAFT},
        code="order_not_editable",
        message=_("This order has been approved and can no longer be edited."),
    )


@transaction.atomic
def create_purchase_order(
    *,
    supplier: Supplier,
    branch: Branch,
    warehouse: Warehouse,
    created_by: User,
    ordered_on: datetime.date,
    request: PurchaseRequest | None = None,
    quotation: SupplierQuotation | None = None,
    location: StockLocation | None = None,
    expected_on: datetime.date | None = None,
    supplier_reference: str = "",
    notes: str = "",
) -> PurchaseOrder:
    """
    Open a draft order. Nothing is committed and nothing is owed.

    Payment terms are copied from the supplier here and never read live again:
    an order placed in January keeps January's terms when March renegotiates
    them, which is the only way a due date computed later can be right.
    """
    if branch.organization_id != supplier.organization_id:
        raise ValidationError(
            _("The supplier belongs to another organization."), code="organization_mismatch"
        )
    if warehouse.branch_id != branch.pk:
        raise ValidationError(
            _("Warehouse %(code)s does not belong to this branch."),
            code="warehouse_branch_mismatch",
            params={"code": warehouse.code},
        )
    if location is not None and location.warehouse_id != warehouse.pk:
        raise ValidationError(
            _("Location %(code)s is not in this warehouse."),
            code="location_warehouse_mismatch",
            params={"code": location.code},
        )
    if request is not None and request.organization_id != supplier.organization_id:
        raise ValidationError(
            _("The request belongs to another organization."), code="organization_mismatch"
        )
    if quotation is not None:
        if quotation.organization_id != supplier.organization_id:
            raise ValidationError(
                _("The quotation belongs to another organization."),
                code="organization_mismatch",
            )
        if quotation.supplier_id != supplier.pk:
            raise ValidationError(
                _("That quotation was given by a different supplier."),
                code="quotation_supplier_mismatch",
            )
    if expected_on is not None and expected_on < ordered_on:
        raise ValidationError(
            _("A delivery cannot be expected before the order was placed."),
            code="expected_before_ordered",
        )

    order = PurchaseOrder(
        organization=branch.organization,
        branch=branch,
        supplier=supplier,
        request=request,
        quotation=quotation,
        warehouse=warehouse,
        location=location,
        created_by=created_by,
        ordered_on=ordered_on,
        expected_on=expected_on,
        payment_terms_days=supplier.payment_terms_days,
        supplier_reference=supplier_reference.strip(),
        notes=notes.strip(),
    )
    order.full_clean()
    order.save()
    record_audit_event(
        action=AuditAction.CREATED, target=order, branch=branch, new_state=snapshot(order)
    )
    return order


@transaction.atomic
def add_order_line(
    *,
    order: PurchaseOrder,
    item: InventoryItem,
    ordered_quantity: Decimal,
    unit_price: Decimal,
    package_unit: PackageUnit | None = None,
    supplier_item: SupplierItem | None = None,
    note: str = "",
) -> PurchaseOrderLine:
    """
    Agree one item at one price on a draft order.

    `line_total` is `quantity × price`, quantized once at the storage boundary.
    """
    order = _require_order_draft(order)

    if item.organization_id != order.organization_id:
        raise ValidationError(
            _("The item belongs to another organization."), code="organization_mismatch"
        )
    if ordered_quantity <= 0:
        raise ValidationError(
            _("An ordered quantity must be greater than zero."), code="quantity_not_positive"
        )
    if unit_price < 0:
        raise ValidationError(_("A price cannot be negative."), code="price_negative")
    if supplier_item is not None and supplier_item.supplier_id != order.supplier_id:
        raise ValidationError(
            _("That catalogue row belongs to another supplier."), code="supplier_mismatch"
        )

    conversion = None
    factor = None
    if package_unit is not None:
        conversion = _active_conversion(item=item, package_unit=package_unit, on=order.ordered_on)
        if conversion is None:
            raise ValidationError(
                _("Item %(item)s has no conversion for package %(package)s on %(date)s."),
                code="no_conversion_for_package",
                params={
                    "item": item.code,
                    "package": package_unit.code,
                    "date": order.ordered_on.isoformat(),
                },
            )
        factor = conversion.factor_to_base
        base = quantize_quantity(ordered_quantity * factor)
    else:
        base = quantize_quantity(ordered_quantity)

    highest = (
        PurchaseOrderLine.objects.filter(order=order)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    line = PurchaseOrderLine(
        order=order,
        sequence=(highest or 0) + 1,
        item=item,
        supplier_item=supplier_item,
        package_unit=package_unit,
        conversion=conversion,
        conversion_version=conversion.version if conversion else None,
        conversion_factor=factor,
        ordered_quantity=quantize_quantity(ordered_quantity),
        ordered_base_quantity=base,
        unit_price=quantize_unit_price(unit_price),
        line_total=quantize_money(ordered_quantity * unit_price),
        note=note.strip(),
    )
    line.full_clean()
    line.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=line,
        branch=order.branch,
        new_state=snapshot(line),
    )
    return line


@transaction.atomic
def remove_order_line(*, line: PurchaseOrderLine) -> None:
    """Drop a line from a draft. Sequences are not renumbered."""
    _require_order_draft(line.order)
    previous = snapshot(line)
    record_audit_event(
        action=AuditAction.DELETED,
        target=line,
        branch=line.order.branch,
        previous_state=previous,
    )
    line.delete()


@transaction.atomic
def approve_purchase_order(*, order: PurchaseOrder, actor: User, reason: str = "") -> PurchaseOrder:
    """
    Agree to spend the money, and draw the document number.

    Maker-checker: whoever prepared the order cannot approve it. Enforced here
    and by a database constraint, because a spending commitment is exactly the
    kind of rule somebody eventually tries to route around.

    Still creates no stock, no journal and no payable.
    """
    locked = _require_order_draft(order)
    previous = snapshot(locked)

    if not locked.lines.exists():
        raise ValidationError(
            _("An order with no lines cannot be approved."), code="order_has_no_lines"
        )
    if locked.created_by_id == actor.pk:
        raise ValidationError(
            _("The person who prepared an order cannot approve it."),
            code="maker_is_not_checker",
        )

    locked.status = PurchaseOrderStatus.APPROVED
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    locked.number = next_document_number(
        organization=locked.organization,
        document_type="PURCHASE_ORDER",
        year=locked.ordered_on.year,
    )
    locked.full_clean()
    locked.save()

    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


@transaction.atomic
def issue_purchase_order(*, order: PurchaseOrder, actor: User) -> PurchaseOrder:
    """
    Send the agreed order to the supplier.

    After this the commercial terms are what somebody else has been told, and
    changing them is a revision with its own version and reason (Task 2.7)
    rather than an edit.
    """
    locked = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
    previous = snapshot(locked)

    if locked.status != PurchaseOrderStatus.APPROVED:
        raise ValidationError(
            _("Only an approved order may be issued; %(number)s is %(status)s."),
            code="illegal_transition",
            params={"number": locked.number, "status": locked.status},
        )

    locked.status = PurchaseOrderStatus.ISSUED
    locked.issued_by = actor
    locked.issued_at = timezone.now()
    locked.full_clean()
    locked.save()

    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def cancel_purchase_order(*, order: PurchaseOrder, actor: User, reason: str) -> PurchaseOrder:
    """
    Withdraw an order. Terminal, and it creates no financial entry.

    Task 2.7 adds the guard that refuses this once goods have been received
    against the order; until receipts exist there is nothing to protect.
    """
    if not reason.strip():
        raise ValidationError(_("A reason is required."), code="reason_required")

    locked = lock_and_require_status(
        PurchaseOrder,
        order.pk,
        {
            PurchaseOrderStatus.DRAFT,
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.ISSUED,
        },
        code="already_cancelled",
        message=_("This order is already cancelled."),
    )
    previous = snapshot(locked)

    # Cancelling closes the *unreceived* remainder. Goods already accepted are
    # a fact and stay on the books; what a cancellation withdraws is the
    # expectation of anything further. Receipts arrive in Task 2.8, and this
    # guard is written against the real interface rather than around it.
    received = sum(
        (received_base_quantity(line) for line in locked.lines.all()),
        start=Decimal("0.000"),
    )
    if received > 0:
        note = (
            f"Cancelled with {format(received, 'f')} already received; "
            "the received quantity stands and only the remainder is withdrawn."
        )
        locked.notes = "\n".join(part for part in (locked.notes, note) if part).strip()

    locked.status = PurchaseOrderStatus.CANCELLED
    locked.cancelled_by = actor
    locked.cancelled_at = timezone.now()
    locked.cancellation_reason = reason.strip()
    locked.full_clean()
    locked.save()

    record_audit_event(
        action=AuditAction.CANCELLED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


# ---------------------------------------------------------------------------
# Purchase order change control
# ---------------------------------------------------------------------------

# What a revision may change is the signature of `revise_purchase_order`, and
# `supplier` is absent from it on purpose. Changing who an order is with is not
# a revision of that order, it is a different order — and once goods have been
# received, it would re-attribute stock somebody else delivered.


def received_base_quantity(line: PurchaseOrderLine) -> Decimal:
    """
    How much of this order line has already been accepted into stock.

    **POSTED receipts only.** A draft is somebody typing while a lorry is
    still being unloaded, and a reversed receipt has given its stock back — so
    neither reserves anything on the order. An inspected-but-unposted receipt
    does not reserve either, because Task 2.0 §7 gives this document three
    statuses and "inspected" is not one of them: inspection is line data on a
    draft, and until the draft posts no stock exists to protect.

    That answer has a consequence worth stating: two drafts can each be within
    the outstanding quantity while together exceeding it. `add_receipt_line`
    therefore also subtracts other drafts against the same order line, so the
    over-receipt guard holds before anything posts as well as after.
    """
    accepted: Decimal | None = GoodsReceiptLine.objects.filter(
        order_line=line, receipt__status=GoodsReceiptStatus.POSTED
    ).aggregate(total=Sum("accepted_base_quantity"))["total"]
    return accepted or Decimal("0.000")


def _snapshot_lines(order: PurchaseOrder) -> list[dict[str, object]]:
    """
    The lines as they stand, frozen for a version row.

    Decimals become **strings**, not floats: a snapshot that went through
    binary floating point would be a record of a price nobody agreed to.
    """
    return [
        {
            "line_uid": str(line.line_uid),
            "sequence": line.sequence,
            "item_code": line.item.code,
            "item_name_ar": line.item.name_ar,
            "package_code": line.package_unit.code if line.package_unit else None,
            "conversion_factor": (
                format(line.conversion_factor, "f") if line.conversion_factor else None
            ),
            "ordered_quantity": format(line.ordered_quantity, "f"),
            "ordered_base_quantity": format(line.ordered_base_quantity, "f"),
            "unit_price": format(line.unit_price, "f"),
            "line_total": format(line.line_total, "f"),
            "note": line.note,
        }
        for line in order.lines.select_related("item", "package_unit").order_by("sequence")
    ]


def _snapshot_header(order: PurchaseOrder) -> dict[str, object]:
    return {
        "number": order.number,
        "status": order.status,
        "supplier_code": order.supplier.code,
        "warehouse_code": order.warehouse.code,
        "location_code": order.location.code if order.location else None,
        "ordered_on": order.ordered_on.isoformat(),
        "expected_on": order.expected_on.isoformat() if order.expected_on else None,
        "payment_terms_days": order.payment_terms_days,
        "supplier_reference": order.supplier_reference,
        "notes": order.notes,
        "total_amount": format(order.total_amount, "f"),
    }


@transaction.atomic
def revise_purchase_order(
    *,
    order: PurchaseOrder,
    actor: User,
    reason: str,
    warehouse: Warehouse | None = None,
    location: StockLocation | None = None,
    clear_location: bool = False,
    expected_on: datetime.date | None = None,
    supplier_reference: str | None = None,
    notes: str | None = None,
    line_quantities: dict[str, Decimal] | None = None,
    line_prices: dict[str, Decimal] | None = None,
) -> PurchaseOrder:
    """
    Supersede an approved or issued order with a new version.

    The previous version is copied into a `PurchaseOrderVersion` before
    anything changes, so what the supplier was told stays readable exactly as
    they received it. The live row then moves to `version + 1`.

    Lines are addressed by `line_uid` rather than by primary key or sequence:
    the uid is stable for the life of the document and is what a downstream
    receipt will point at, so a revision cannot silently re-target a different
    line by renumbering.

    Creates no stock, no journal, no payable and no GRNI. A revision is a
    change to a commitment, and a commitment is still not a liability.
    """
    if not reason.strip():
        raise ValidationError(
            _("A revision must record why the order changed."), code="reason_required"
        )

    locked = lock_and_require_status(
        PurchaseOrder,
        order.pk,
        {PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.ISSUED},
        code="order_not_revisable",
        message=_("Only an approved or issued order is revised; a draft is edited."),
    )
    previous = snapshot(locked)

    # Freeze what the order says now, before touching it.
    PurchaseOrderVersion.objects.create(
        order=locked,
        version=locked.version,
        header=_snapshot_header(locked),
        lines=_snapshot_lines(locked),
        reason=reason.strip(),
        revised_by=actor,
        revised_at=timezone.now(),
    )

    received_anything = any(received_base_quantity(line) > 0 for line in locked.lines.all())

    if warehouse is not None or location is not None or clear_location:
        if received_anything:
            raise ValidationError(
                _("The destination cannot change once goods have been received."),
                code="destination_locked_after_receipt",
            )
        destination = warehouse or locked.warehouse
        if destination.branch_id != locked.branch_id:
            raise ValidationError(
                _("Warehouse %(code)s does not belong to this branch."),
                code="warehouse_branch_mismatch",
                params={"code": destination.code},
            )
        wanted = None if clear_location else (location or locked.location)
        if wanted is not None and wanted.warehouse_id != destination.pk:
            raise ValidationError(
                _("Location %(code)s is not in this warehouse."),
                code="location_warehouse_mismatch",
                params={"code": wanted.code},
            )
        locked.warehouse = destination
        locked.location = wanted

    # `None` means "leave it alone"; the signature is the allowlist, and unlike
    # a set of field names it cannot be bypassed by a caller passing a string.
    if expected_on is not None:
        locked.expected_on = expected_on
    if supplier_reference is not None:
        locked.supplier_reference = supplier_reference.strip()
    if notes is not None:
        locked.notes = notes.strip()

    for uid, quantity in (line_quantities or {}).items():
        line = locked.lines.select_for_update().filter(line_uid=uid).first()
        if line is None:
            raise ValidationError(
                _("Line %(uid)s is not on this order."),
                code="line_not_on_order",
                params={"uid": uid},
            )
        if quantity <= 0:
            raise ValidationError(
                _("A revised quantity must be greater than zero."),
                code="quantity_not_positive",
            )
        base = (
            quantize_quantity(quantity * line.conversion_factor)
            if line.conversion_factor is not None
            else quantize_quantity(quantity)
        )
        accepted = received_base_quantity(line)
        if base < accepted:
            raise ValidationError(
                _(
                    "Line %(item)s has already accepted %(accepted)s; it cannot be "
                    "revised down to %(wanted)s."
                ),
                code="below_received_quantity",
                params={
                    "item": line.item.code,
                    "accepted": format(accepted, "f"),
                    "wanted": format(base, "f"),
                },
            )
        line.ordered_quantity = quantize_quantity(quantity)
        line.ordered_base_quantity = base
        line.line_total = quantize_money(quantity * line.unit_price)
        line.full_clean()
        line.save()

    for uid, price in (line_prices or {}).items():
        line = locked.lines.select_for_update().filter(line_uid=uid).first()
        if line is None:
            raise ValidationError(
                _("Line %(uid)s is not on this order."),
                code="line_not_on_order",
                params={"uid": uid},
            )
        if price < 0:
            raise ValidationError(_("A price cannot be negative."), code="price_negative")
        line.unit_price = quantize_unit_price(price)
        line.line_total = quantize_money(line.ordered_quantity * price)
        line.full_clean()
        line.save()

    locked.version += 1
    locked.revised_at = timezone.now()
    locked.full_clean()
    locked.save()

    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


# ---------------------------------------------------------------------------
# Goods receipts
# ---------------------------------------------------------------------------


def _require_receipt_draft(receipt: GoodsReceipt) -> GoodsReceipt:
    """Only a draft may be edited or inspected, re-read under a row lock."""
    return lock_and_require_status(
        GoodsReceipt,
        receipt.pk,
        {GoodsReceiptStatus.DRAFT},
        code="receipt_not_editable",
        message=_("This receipt has been posted and can no longer be edited."),
    )


@transaction.atomic
def create_goods_receipt(
    *,
    supplier: Supplier,
    branch: Branch,
    warehouse: Warehouse,
    created_by: User,
    received_at: datetime.date,
    order: PurchaseOrder | None = None,
    location: StockLocation | None = None,
    delivered_at: datetime.datetime | None = None,
    delivery_reference: str = "",
    evidence_reference: str = "",
    notes: str = "",
) -> GoodsReceipt:
    """
    Open a draft receipt for goods that have physically arrived.

    Nothing posts here. A draft records what turned up so it can be inspected;
    stock and the GRNI journal move together in Task 2.9, and separating them
    would create inventory with no accounting behind it.

    The order's **version** is copied rather than joined to. A revision after
    delivery must not change what this delivery was measured against.
    """
    if branch.organization_id != supplier.organization_id:
        raise ValidationError(
            _("The supplier belongs to another organization."), code="organization_mismatch"
        )
    if warehouse.branch_id != branch.pk:
        raise ValidationError(
            _("Warehouse %(code)s does not belong to this branch."),
            code="warehouse_branch_mismatch",
            params={"code": warehouse.code},
        )
    if location is not None and location.warehouse_id != warehouse.pk:
        raise ValidationError(
            _("Location %(code)s is not in this warehouse."),
            code="location_warehouse_mismatch",
            params={"code": location.code},
        )

    order_version = None
    if order is not None:
        # Read the order under a lock: its status decides whether goods may be
        # booked against it at all, and a copy loaded a moment ago may already
        # be cancelled.
        locked_order = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
        if locked_order.organization_id != supplier.organization_id:
            raise ValidationError(
                _("The order belongs to another organization."),
                code="organization_mismatch",
            )
        if locked_order.supplier_id != supplier.pk:
            raise ValidationError(
                _("That order was placed with a different supplier."),
                code="order_supplier_mismatch",
            )
        if locked_order.status == PurchaseOrderStatus.CANCELLED:
            raise ValidationError(
                _("Order %(number)s is cancelled and cannot receive goods."),
                code="order_cancelled",
                params={"number": locked_order.number},
            )
        if locked_order.status == PurchaseOrderStatus.DRAFT:
            raise ValidationError(
                _("Goods cannot be received against a draft order."),
                code="order_not_approved",
            )
        order = locked_order
        order_version = locked_order.version

    receipt = GoodsReceipt(
        organization=branch.organization,
        branch=branch,
        supplier=supplier,
        order=order,
        order_version=order_version,
        warehouse=warehouse,
        location=location,
        created_by=created_by,
        received_at=received_at,
        delivered_at=delivered_at,
        delivery_reference=delivery_reference.strip(),
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
    )
    receipt.full_clean()
    receipt.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=receipt,
        branch=branch,
        new_state=snapshot(receipt),
    )
    return receipt


def _remaining_on_order_line(line: PurchaseOrderLine) -> Decimal:
    """How much of an order line has not yet been received."""
    return line.ordered_base_quantity - received_base_quantity(line)


@transaction.atomic
def add_receipt_line(
    *,
    receipt: GoodsReceipt,
    item: InventoryItem,
    delivered_quantity: Decimal,
    unit_price: Decimal | None = None,
    package_unit: PackageUnit | None = None,
    order_line: PurchaseOrderLine | None = None,
    supplier_item: SupplierItem | None = None,
    measured_base_quantity: Decimal | None = None,
    lot: InventoryLot | None = None,
    expiry_date: datetime.date | None = None,
    note: str = "",
) -> GoodsReceiptLine:
    """
    Record one delivered item on a draft receipt.

    The price comes from the order line when one is named, and must be entered
    when none is. It is never taken from the supplier catalogue: a catalogue
    price is planning information that no posting path may read (PRC-005), and
    a receipt that silently used one would value stock at a number nobody
    agreed to for this delivery.

    A `VARIABLE` package demands `measured_base_quantity` (PRC-026). Twelve
    lambs is not a quantity of meat; the scale reading is, and the planning
    factor is an estimate that must never become a stock figure.
    """
    receipt = _require_receipt_draft(receipt)

    if item.organization_id != receipt.organization_id:
        raise ValidationError(
            _("The item belongs to another organization."), code="organization_mismatch"
        )
    if delivered_quantity <= 0:
        raise ValidationError(
            _("A delivered quantity must be greater than zero."),
            code="quantity_not_positive",
        )

    if order_line is not None:
        if receipt.order_id is None or order_line.order_id != receipt.order_id:
            raise ValidationError(
                _("That line belongs to a different purchase order."),
                code="order_line_mismatch",
            )
        if order_line.item_id != item.pk:
            raise ValidationError(
                _("The order line is for a different item."), code="order_line_item_mismatch"
            )
        if unit_price is None:
            unit_price = order_line.unit_price

    if unit_price is None:
        raise ValidationError(
            _(
                "A receipt line needs a price. Link an order line, or enter the "
                "price agreed for this delivery."
            ),
            code="price_required",
        )
    if unit_price < 0:
        raise ValidationError(_("A price cannot be negative."), code="price_negative")
    if supplier_item is not None and supplier_item.supplier_id != receipt.supplier_id:
        raise ValidationError(
            _("That catalogue row belongs to another supplier."), code="supplier_mismatch"
        )

    conversion = None
    factor = None
    if package_unit is not None:
        conversion = _active_conversion(
            item=item, package_unit=package_unit, on=receipt.received_at
        )
        if conversion is None:
            raise ValidationError(
                _("Item %(item)s has no conversion for package %(package)s on %(date)s."),
                code="no_conversion_for_package",
                params={
                    "item": item.code,
                    "package": package_unit.code,
                    "date": receipt.received_at.isoformat(),
                },
            )
        factor = conversion.factor_to_base
        if conversion.conversion_type == ConversionType.VARIABLE:
            if measured_base_quantity is None or measured_base_quantity <= 0:
                raise ValidationError(
                    _(
                        "Package %(package)s is variable: the measured quantity is "
                        "what arrived, not the planning factor."
                    ),
                    code="measured_quantity_required",
                    params={"package": package_unit.code},
                )
            base = quantize_quantity(measured_base_quantity)
        else:
            base = quantize_quantity(delivered_quantity * factor)
    else:
        base = quantize_quantity(delivered_quantity)

    _validate_receipt_lot(item=item, lot=lot, expiry_date=expiry_date)

    if order_line is not None:
        remaining = _remaining_on_order_line(order_line)
        already = sum(
            (
                other.delivered_base_quantity
                for other in GoodsReceiptLine.objects.filter(
                    order_line=order_line, receipt__status=GoodsReceiptStatus.DRAFT
                ).exclude(receipt=receipt)
            ),
            start=Decimal("0.000"),
        )
        if base > remaining - already:
            raise ValidationError(
                _(
                    "Line %(item)s has %(remaining)s outstanding on the order; "
                    "%(delivered)s would over-receive it. Revise the order first."
                ),
                code="over_receipt",
                params={
                    "item": item.code,
                    "remaining": format(remaining - already, "f"),
                    "delivered": format(base, "f"),
                },
            )

    highest = (
        GoodsReceiptLine.objects.filter(receipt=receipt)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
    )
    line = GoodsReceiptLine(
        receipt=receipt,
        sequence=(highest or 0) + 1,
        order_line=order_line,
        item=item,
        supplier_item=supplier_item,
        package_unit=package_unit,
        conversion=conversion,
        conversion_version=conversion.version if conversion else None,
        conversion_factor=factor,
        delivered_quantity=quantize_quantity(delivered_quantity),
        delivered_base_quantity=base,
        measured_base_quantity=(
            quantize_quantity(measured_base_quantity)
            if measured_base_quantity is not None
            else None
        ),
        lot=lot,
        expiry_date=expiry_date,
        unit_price=quantize_unit_price(unit_price),
        note=note.strip(),
    )
    line.full_clean()
    line.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=line,
        branch=receipt.branch,
        new_state=snapshot(line),
    )
    return line


def _validate_receipt_lot(
    *, item: InventoryItem, lot: InventoryLot | None, expiry_date: datetime.date | None
) -> None:
    """
    Lot and expiry follow the item's own rules, unchanged (PRC-027).

    Procurement inherits inventory's vocabulary here rather than inventing a
    parallel one: an item that tracks lots needs one on every movement, and an
    item that does not must never acquire one through a side door.
    """
    if item.tracks_lots and lot is None:
        raise ValidationError(
            _("Item %(item)s is lot-tracked; a lot is required."),
            code="lot_required",
            params={"item": item.code},
        )
    if not item.tracks_lots and lot is not None:
        raise ValidationError(
            _("Item %(item)s is not lot-tracked and cannot take a lot."),
            code="lot_prohibited",
            params={"item": item.code},
        )
    if lot is not None and lot.item_id != item.pk:
        raise ValidationError(_("That lot belongs to a different item."), code="lot_item_mismatch")
    if expiry_date is not None and not item.tracks_expiry:
        raise ValidationError(
            _("Item %(item)s does not track expiry."),
            code="expiry_not_tracked",
            params={"item": item.code},
        )


@transaction.atomic
def remove_receipt_line(*, line: GoodsReceiptLine) -> None:
    """Drop a line from a draft. Sequences are not renumbered."""
    _require_receipt_draft(line.receipt)
    previous = snapshot(line)
    record_audit_event(
        action=AuditAction.DELETED,
        target=line,
        branch=line.receipt.branch,
        previous_state=previous,
    )
    line.delete()


@transaction.atomic
def inspect_receipt_line(
    *,
    line: GoodsReceiptLine,
    accepted_base_quantity: Decimal,
    actor: User,
    rejection_reason: InventoryReasonCode | None = None,
    note: str = "",
) -> GoodsReceiptLine:
    """
    Say how much of a delivered line is actually accepted.

    The rejected quantity is **derived** — delivered minus accepted — rather
    than entered separately. Two numbers that must sum to a third is two
    chances to disagree with it, and the database constraint would then be
    catching a typo the screen could have prevented.

    Only the accepted quantity will ever enter stock (PRC-025). Rejected goods
    are recorded so the supplier can be argued with and so quality can be
    reported on, and they post nothing.
    """
    receipt = _require_receipt_draft(line.receipt)
    locked = GoodsReceiptLine.objects.select_for_update().get(pk=line.pk)
    previous = snapshot(locked)

    if accepted_base_quantity < 0:
        raise ValidationError(
            _("An accepted quantity cannot be negative."), code="accepted_negative"
        )
    if accepted_base_quantity > locked.delivered_base_quantity:
        raise ValidationError(
            _("Only %(delivered)s was delivered; %(accepted)s cannot be accepted."),
            code="accepted_above_delivered",
            params={
                "delivered": format(locked.delivered_base_quantity, "f"),
                "accepted": format(accepted_base_quantity, "f"),
            },
        )

    accepted = quantize_quantity(accepted_base_quantity)
    rejected = quantize_quantity(locked.delivered_base_quantity - accepted)

    if rejected > 0 and rejection_reason is None:
        raise ValidationError(
            _("Rejecting goods requires a reason."), code="rejection_reason_required"
        )
    if rejection_reason is not None and rejection_reason.organization_id != receipt.organization_id:
        raise ValidationError(
            _("That reason code belongs to another organization."),
            code="organization_mismatch",
        )

    locked.accepted_base_quantity = accepted
    locked.rejected_base_quantity = rejected
    locked.rejection_reason = rejection_reason if rejected > 0 else None
    locked.quality_result = (
        QualityResult.ACCEPTED
        if rejected == 0
        else QualityResult.REJECTED
        if accepted == 0
        else QualityResult.PARTIAL
    )
    if note:
        locked.note = note.strip()
    locked.full_clean()
    locked.save()

    receipt.inspected_by = actor
    receipt.inspected_at = timezone.now()
    receipt.full_clean()
    receipt.save(update_fields=["inspected_by", "inspected_at", "updated_at"])

    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=receipt.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason="inspection",
    )
    return locked
