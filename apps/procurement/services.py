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

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.organizations.models import Organization
from apps.procurement.models import Supplier
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
