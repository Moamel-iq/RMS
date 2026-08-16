"""
Procurement's import kinds, registered into the Task 1.7 framework.

Task 2.17. Three kinds — the supplier master, the supplier-item catalogue,
and purchase-request **drafts** — on the same batch model, the same
preview-then-apply lifecycle, the same file security and the same
all-or-nothing apply the inventory kinds already earned. This module defines
the validators, the writers, the required columns, the compound row
identities and the per-kind permissions, and `register()` (called from this
app's `AppConfig.ready`) writes them into the framework's registries.
Inventory never imports procurement; procurement reaches in and signs up.

The boundary §16.8 draws is kept by construction: the two master kinds write
master data through `create_supplier`/`update_supplier` and the catalogue
services, and the draft kind produces a purchase-request **draft** for
somebody to review, submit and approve — it draws no number, submits
nothing, posts nothing, and no import kind for any posted document exists.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.inventory.models import ImportKind, InventoryItem, PackageUnit
from apps.organizations.models import Branch, Organization
from apps.procurement.models import (
    PurchaseRequest,
    PurchaseRequestStatus,
    Supplier,
    SupplierItem,
)
from apps.procurement.permissions import (
    CREATE_PURCHASE_REQUEST,
    IMPORT_SUPPLIER,
    IMPORT_SUPPLIER_ITEM,
)
from apps.users.models import User


def _framework() -> Any:
    """The inventory imports module, late, so registration stays one-way."""
    from apps.inventory import imports as inventory_imports

    return inventory_imports


REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    ImportKind.SUPPLIER: ("code", "name_ar"),
    ImportKind.SUPPLIER_ITEM: ("supplier_code", "item_code", "effective_from"),
    ImportKind.PURCHASE_REQUEST_DRAFT: (
        "warehouse_code",
        "required_date",
        "purpose",
        "item_code",
        "quantity",
    ),
}


# ---------------------------------------------------------------------------
# Validators. Read one row's strings, return (errors, cleaned). They may read
# the database and must never write it, and everything they resolve is
# resolved inside the batch's own organization and branch — a row naming
# another organization's supplier finds nothing rather than finding it.
# ---------------------------------------------------------------------------


def _date(raw: str, field: str, errors: dict[str, list[str]]) -> datetime.date | None:
    if not raw:
        errors.setdefault(field, []).append(_("قيمة مطلوبة."))
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        errors.setdefault(field, []).append(_("التاريخ يجب أن يكون بصيغة YYYY-MM-DD."))
        return None


def _integer(raw: str, field: str, errors: dict[str, list[str]]) -> int | None:
    if not raw:
        return None
    if not raw.isdigit():
        errors.setdefault(field, []).append(_("ليست قيمة صحيحة."))
        return None
    return int(raw)


def validate_supplier(
    row: dict[str, str], *, organization: Organization, branch: Branch | None
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """
    One supplier row. The code is the identity; everything else corrects.

    Text is cleaned to **exactly what `create_supplier` would store** — the
    same `strip()`, the same phone canonicalisation. Two things follow, and
    both matter. The preview shows the value that will actually land, rather
    than the one that was typed; and a row asking for what the record already
    holds compares equal at apply time and counts as `unchanged`, instead of
    reporting a change because "07701234567" is stored as "+9647701234567".
    A phone that cannot be canonicalised is a row error here rather than an
    exception thrown halfway through the apply.
    """
    from apps.users.phone import try_normalize_iraqi_mobile

    framework = _framework()
    errors: dict[str, list[str]] = {}
    cleaned: dict[str, Any] = {}

    code = row.get("code", "").strip().upper()
    if not code:
        errors["code"] = [_("الرمز مطلوب.")]
    else:
        cleaned["code"] = code
    if not row.get("name_ar", "").strip():
        errors["name_ar"] = [_("الاسم العربي مطلوب.")]
    else:
        cleaned["name_ar"] = row["name_ar"].strip()

    for field in ("name_en", "contact_name", "email", "address", "notes"):
        cleaned[field] = row.get(field, "").strip()

    raw_phone = row.get("phone", "").strip()
    if not raw_phone:
        # A supplier with no number on file is ordinary, not malformed.
        cleaned["phone"] = ""
    else:
        canonical = try_normalize_iraqi_mobile(raw_phone)
        if canonical is None:
            errors["phone"] = [_("ليس رقم هاتف عراقي صحيح.")]
        else:
            cleaned["phone"] = canonical

    terms = _integer(row.get("payment_terms_days", ""), "payment_terms_days", errors)
    cleaned["payment_terms_days"] = terms if terms is not None else 0

    raw_limit = row.get("credit_limit", "")
    if raw_limit:
        limit = framework._decimal(raw_limit, "credit_limit", errors, quantity=False)
        if limit is not None:
            if limit < 0:
                errors.setdefault("credit_limit", []).append(_("لا يمكن أن يكون سالباً."))
            else:
                cleaned["credit_limit"] = limit
    else:
        cleaned["credit_limit"] = None
    return errors, cleaned


def validate_supplier_item(
    row: dict[str, str], *, organization: Organization, branch: Branch | None
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """One catalogue row: this supplier's terms for this item, from a date."""
    framework = _framework()
    errors: dict[str, list[str]] = {}
    cleaned: dict[str, Any] = {}

    supplier_code = row.get("supplier_code", "").strip().upper()
    if not supplier_code:
        errors["supplier_code"] = [_("رمز المورد مطلوب.")]
    else:
        supplier = Supplier.objects.filter(organization=organization, code=supplier_code).first()
        if supplier is None:
            errors["supplier_code"] = [_("لا يوجد مورد بهذا الرمز في هذه المؤسسة.")]
        elif not supplier.is_active:
            errors["supplier_code"] = [_("المورد موقوف.")]
        else:
            cleaned["supplier_id"] = supplier.pk

    item_code = row.get("item_code", "").strip().upper()
    if not item_code:
        errors["item_code"] = [_("رمز الصنف مطلوب.")]
    else:
        item = InventoryItem.objects.filter(organization=organization, code=item_code).first()
        if item is None:
            errors["item_code"] = [_("لا يوجد صنف بهذا الرمز في هذه المؤسسة.")]
        elif not item.is_active:
            errors["item_code"] = [_("الصنف موقوف.")]
        else:
            cleaned["item_id"] = item.pk

    effective_from = _date(row.get("effective_from", ""), "effective_from", errors)
    if effective_from is not None:
        cleaned["effective_from"] = effective_from

    package_code = row.get("package_unit_code", "").strip().upper()
    if package_code:
        package = PackageUnit.objects.filter(organization=organization, code=package_code).first()
        if package is None:
            errors["package_unit_code"] = [_("لا توجد وحدة تعبئة بهذا الرمز.")]
        else:
            cleaned["package_unit_id"] = package.pk
    else:
        cleaned["package_unit_id"] = None

    raw_price = row.get("last_quoted_price", "")
    if raw_price:
        price = framework._decimal(raw_price, "last_quoted_price", errors, quantity=False)
        if price is not None:
            if price < 0:
                errors.setdefault("last_quoted_price", []).append(_("لا يمكن أن يكون سالباً."))
            else:
                cleaned["last_quoted_price"] = price
    else:
        cleaned["last_quoted_price"] = None

    raw_moq = row.get("minimum_order_quantity", "")
    if raw_moq:
        moq = framework._decimal(raw_moq, "minimum_order_quantity", errors)
        if moq is not None:
            if moq <= 0:
                errors.setdefault("minimum_order_quantity", []).append(
                    _("يجب أن يكون أكبر من صفر.")
                )
            else:
                cleaned["minimum_order_quantity"] = moq
    else:
        cleaned["minimum_order_quantity"] = None

    cleaned["lead_time_days"] = _integer(row.get("lead_time_days", ""), "lead_time_days", errors)
    preferred = framework._boolean(row.get("is_preferred", "لا"), "is_preferred", errors)
    cleaned["is_preferred"] = bool(preferred)
    # Stripped for the same reason the supplier's fields are: the service
    # strips before storing, so an unstripped comparison would call every
    # re-import a change.
    cleaned["supplier_sku"] = row.get("supplier_sku", "").strip()
    cleaned["notes"] = row.get("notes", "").strip()
    return errors, cleaned


def validate_purchase_request_draft(
    row: dict[str, str], *, organization: Organization, branch: Branch | None
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """
    One wanted item on a draft-to-be. Rows sharing (warehouse, date, purpose)
    become lines of one draft; the grouping happens in the writer, and the
    validator's job is that every reference resolves inside this branch.
    """
    from apps.inventory.models import Warehouse

    framework = _framework()
    errors: dict[str, list[str]] = {}
    cleaned: dict[str, Any] = {}
    if branch is None:  # pragma: no cover - create_batch refused it already
        raise ValidationError(_("هذا النوع يحتاج فرعاً."), code="import_branch_required")

    warehouse_code = row.get("warehouse_code", "").strip().upper()
    if not warehouse_code:
        errors["warehouse_code"] = [_("رمز المخزن مطلوب.")]
    else:
        warehouse = Warehouse.objects.filter(
            branch=branch, code=warehouse_code, is_system=False
        ).first()
        if warehouse is None:
            errors["warehouse_code"] = [_("لا يوجد مخزن بهذا الرمز في هذا الفرع.")]
        else:
            cleaned["warehouse_id"] = warehouse.pk

    required = _date(row.get("required_date", ""), "required_date", errors)
    if required is not None:
        cleaned["required_date"] = required

    if not row.get("purpose", "").strip():
        errors["purpose"] = [_("الغرض مطلوب.")]
    else:
        cleaned["purpose"] = row["purpose"].strip()

    item_code = row.get("item_code", "").strip().upper()
    if not item_code:
        errors["item_code"] = [_("رمز الصنف مطلوب.")]
    else:
        item = InventoryItem.objects.filter(organization=organization, code=item_code).first()
        if item is None:
            errors["item_code"] = [_("لا يوجد صنف بهذا الرمز في هذه المؤسسة.")]
        elif not item.is_active:
            errors["item_code"] = [_("الصنف موقوف.")]
        else:
            cleaned["item_id"] = item.pk

    quantity = framework._decimal(row.get("quantity", ""), "quantity", errors)
    if quantity is not None:
        if quantity <= 0:
            errors.setdefault("quantity", []).append(_("يجب أن تكون الكمية أكبر من صفر."))
        else:
            cleaned["quantity"] = quantity

    package_code = row.get("package_unit_code", "").strip().upper()
    if package_code:
        package = PackageUnit.objects.filter(organization=organization, code=package_code).first()
        if package is None:
            errors["package_unit_code"] = [_("لا توجد وحدة تعبئة بهذا الرمز.")]
        else:
            cleaned["package_unit_id"] = package.pk
    else:
        cleaned["package_unit_id"] = None

    preferred_code = row.get("preferred_supplier_code", "").strip().upper()
    if preferred_code:
        preferred = Supplier.objects.filter(organization=organization, code=preferred_code).first()
        if preferred is None:
            errors["preferred_supplier_code"] = [_("لا يوجد مورد بهذا الرمز.")]
        elif not preferred.is_active:
            errors["preferred_supplier_code"] = [_("المورد موقوف.")]
        else:
            cleaned["preferred_supplier_id"] = preferred.pk
    else:
        cleaned["preferred_supplier_id"] = None

    cleaned["note"] = row.get("note", "").strip()
    return errors, cleaned


# ---------------------------------------------------------------------------
# Writers. One cleaned row in, one action out: created / updated / unchanged.
# Every write goes through the real service, so an import can never do what a
# person could not.
# ---------------------------------------------------------------------------

_SUPPLIER_FIELDS = (
    "name_ar",
    "name_en",
    "contact_name",
    "phone",
    "email",
    "address",
    "payment_terms_days",
    "notes",
)


def write_supplier(
    cleaned: dict[str, Any], *, organization: Organization, branch: Branch | None
) -> str:
    from apps.procurement.services import create_supplier, update_supplier

    limit = cleaned.get("credit_limit")
    credit_limit = Decimal(limit) if isinstance(limit, str) else limit
    texts: dict[str, str] = {
        field: str(cleaned.get(field) or "")
        for field in _SUPPLIER_FIELDS
        if field != "payment_terms_days"
    }
    terms = int(cleaned.get("payment_terms_days") or 0)

    existing = Supplier.objects.filter(organization=organization, code=cleaned["code"]).first()
    if existing is None:
        create_supplier(
            organization=organization,
            code=cleaned["code"],
            credit_limit=credit_limit,
            payment_terms_days=terms,
            name_ar=texts["name_ar"],
            name_en=texts["name_en"],
            contact_name=texts["contact_name"],
            phone=texts["phone"],
            email=texts["email"],
            address=texts["address"],
            notes=texts["notes"],
        )
        return "created"

    same = all(getattr(existing, field) == value for field, value in texts.items())
    if (
        same
        and existing.payment_terms_days == terms
        and existing.credit_limit == credit_limit
        and existing.is_active
    ):
        return "unchanged"
    update_supplier(
        supplier=existing,
        credit_limit=credit_limit,
        payment_terms_days=terms,
        is_active=True,
        name_ar=texts["name_ar"],
        name_en=texts["name_en"],
        contact_name=texts["contact_name"],
        phone=texts["phone"],
        email=texts["email"],
        address=texts["address"],
        notes=texts["notes"],
    )
    return "updated"


def write_supplier_item(
    cleaned: dict[str, Any], *, organization: Organization, branch: Branch | None
) -> str:
    from apps.procurement.services import create_supplier_item, update_supplier_item

    raw_price = cleaned.get("last_quoted_price")
    raw_moq = cleaned.get("minimum_order_quantity")
    price = Decimal(raw_price) if isinstance(raw_price, str) else raw_price
    moq = Decimal(raw_moq) if isinstance(raw_moq, str) else raw_moq
    lead = cleaned.get("lead_time_days")
    sku = str(cleaned.get("supplier_sku") or "")
    notes = str(cleaned.get("notes") or "")
    preferred = bool(cleaned.get("is_preferred"))

    effective_from = datetime.date.fromisoformat(cleaned["effective_from"])
    existing = SupplierItem.objects.filter(
        supplier_id=cleaned["supplier_id"],
        item_id=cleaned["item_id"],
        package_unit_id=cleaned["package_unit_id"],
        effective_from=effective_from,
    ).first()
    if existing is None:
        create_supplier_item(
            supplier=Supplier.objects.get(pk=cleaned["supplier_id"]),
            item=InventoryItem.objects.get(pk=cleaned["item_id"]),
            package_unit=(
                PackageUnit.objects.get(pk=cleaned["package_unit_id"])
                if cleaned["package_unit_id"]
                else None
            ),
            effective_from=effective_from,
            supplier_sku=sku,
            last_quoted_price=price,
            lead_time_days=lead,
            minimum_order_quantity=moq,
            is_preferred=preferred,
            notes=notes,
        )
        return "created"

    same = (
        existing.supplier_sku == sku
        and existing.last_quoted_price == price
        and existing.lead_time_days == lead
        and existing.minimum_order_quantity == moq
        and existing.is_preferred == preferred
        and existing.notes == notes
        and existing.is_active
    )
    if same:
        return "unchanged"
    update_supplier_item(
        supplier_item=existing,
        supplier_sku=sku,
        last_quoted_price=price,
        lead_time_days=lead,
        minimum_order_quantity=moq,
        is_preferred=preferred,
        notes=notes,
        is_active=True,
    )
    return "updated"


def write_purchase_request_draft(
    cleaned: dict[str, Any], *, organization: Organization, branch: Branch | None
) -> str:
    """
    Find-or-create the draft this row belongs to, then add its line.

    Rows sharing (warehouse, required date, purpose) land on one draft. The
    find is restricted to **drafts** — a submitted or approved request is
    somebody's decision in flight, and an import must never append to it.
    Within one apply the first row creates the draft and the rest reuse it;
    re-applying the same file is blocked upstream by the content hash.
    """
    from apps.inventory.models import Warehouse
    from apps.procurement.services import add_request_line, create_purchase_request

    if branch is None:  # pragma: no cover - create_batch refused it already
        raise ValidationError(_("هذا النوع يحتاج فرعاً."), code="import_branch_required")
    required_date = datetime.date.fromisoformat(cleaned["required_date"])
    request = (
        PurchaseRequest.objects.filter(
            branch=branch,
            warehouse_id=cleaned["warehouse_id"],
            required_date=required_date,
            purpose=cleaned["purpose"],
            status=PurchaseRequestStatus.DRAFT,
        )
        .order_by("id")
        .first()
    )
    created_request = False
    if request is None:
        from apps.core.context import get_actor

        actor = get_actor()
        if not isinstance(actor, User):  # pragma: no cover - apply runs signed in
            raise ValidationError(_("لا يوجد مستخدم معروف للتطبيق."), code="import_no_actor")
        request = create_purchase_request(
            branch=branch,
            requested_by=actor,
            warehouse=Warehouse.objects.get(pk=cleaned["warehouse_id"]),
            required_date=required_date,
            purpose=cleaned["purpose"],
        )
        created_request = True

    quantity = cleaned["quantity"]
    add_request_line(
        request=request,
        item=InventoryItem.objects.get(pk=cleaned["item_id"]),
        entered_quantity=Decimal(quantity) if isinstance(quantity, str) else quantity,
        package_unit=(
            PackageUnit.objects.get(pk=cleaned["package_unit_id"])
            if cleaned["package_unit_id"]
            else None
        ),
        preferred_supplier=(
            Supplier.objects.get(pk=cleaned["preferred_supplier_id"])
            if cleaned["preferred_supplier_id"]
            else None
        ),
        note=cleaned.get("note") or "",
    )
    return "created" if created_request else "updated"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _supplier_item_key(row: dict[str, str]) -> str:
    """A catalogue row's identity is supplier *and* item *and* start date."""
    return "|".join(
        (
            row.get("supplier_code", "").strip().upper(),
            row.get("item_code", "").strip().upper(),
            row.get("package_unit_code", "").strip().upper(),
            row.get("effective_from", "").strip(),
        )
    )


def _request_line_key(row: dict[str, str]) -> str:
    """One item per draft: the draft's triple plus the item it wants."""
    return "|".join(
        (
            row.get("warehouse_code", "").strip().upper(),
            row.get("required_date", "").strip(),
            row.get("purpose", "").strip(),
            row.get("item_code", "").strip().upper(),
        )
    )


def register() -> None:
    """
    Sign procurement's kinds into the framework. Idempotent; called from
    `AppConfig.ready`, which Django may run more than once.
    """
    from apps.inventory import import_views
    from apps.inventory import imports as framework

    framework.REQUIRED_COLUMNS.update(REQUIRED_COLUMNS)
    framework.VALIDATORS.update(
        {
            ImportKind.SUPPLIER: validate_supplier,
            ImportKind.SUPPLIER_ITEM: validate_supplier_item,
            ImportKind.PURCHASE_REQUEST_DRAFT: validate_purchase_request_draft,
        }
    )
    framework.WRITERS.update(
        {
            ImportKind.SUPPLIER: write_supplier,
            ImportKind.SUPPLIER_ITEM: write_supplier_item,
            ImportKind.PURCHASE_REQUEST_DRAFT: write_purchase_request_draft,
        }
    )
    framework.EXTERNAL_KEYS.update(
        {
            ImportKind.SUPPLIER_ITEM: _supplier_item_key,
            ImportKind.PURCHASE_REQUEST_DRAFT: _request_line_key,
        }
    )
    import_views.KIND_PERMISSIONS.update(
        {
            ImportKind.SUPPLIER: IMPORT_SUPPLIER,
            ImportKind.SUPPLIER_ITEM: IMPORT_SUPPLIER_ITEM,
            # The spec names no import permission for the draft kind, and the
            # safest dependency-correct reading is that importing a draft
            # requires exactly the authority to create one.
            ImportKind.PURCHASE_REQUEST_DRAFT: CREATE_PURCHASE_REQUEST,
        }
    )
