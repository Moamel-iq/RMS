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
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import InventoryItem, ItemPackageConversion, PackageUnit
from apps.organizations.models import Organization
from apps.procurement.models import Supplier, SupplierItem
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
