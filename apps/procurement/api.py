"""
Supplier master-data API.

Master data is not a posted ledger, so this is service-backed CRUD rather than
the command shape a posting API needs. The Phase 0 rules are unchanged:

* No writable path that skips the services. Every mutation calls
  `apps/procurement/services.py`; nothing calls `Model.objects.create`.
* An identifier never widens access. Suppliers are resolved through
  `apps/procurement/selectors.py`, which filters by the caller's own scope, so
  another organization's supplier is a **404** and not a 403.
* Money crosses the boundary as an **exact string**, both directions. JSON's
  only numeric type is binary floating point, and a credit limit that arrived
  as 1000000.0000000001 would be nobody's fault and everybody's problem.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.inventory.selectors import resolve_item, resolve_package_unit
from apps.organizations.authorization import (
    PermissionMissing,
    require_reachable_organization_permission,
    resolve_organization,
)
from apps.procurement.models import Supplier, SupplierItem
from apps.procurement.permissions import (
    MANAGE_SUPPLIER_ITEMS,
    MANAGE_SUPPLIERS,
    VIEW_SUPPLIER,
    VIEW_SUPPLIER_COST,
    VIEW_SUPPLIER_ITEM,
)
from apps.procurement.selectors import (
    resolve_supplier,
    resolve_supplier_item,
    visible_supplier_items,
    visible_suppliers,
)
from apps.procurement.services import (
    create_supplier,
    create_supplier_item,
    update_supplier,
)
from apps.users.models import User

router = Router(tags=["procurement"])


def _actor(request: HttpRequest) -> User:
    user: User = request.user  # type: ignore[assignment]
    return user


def _require_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SUPPLIER):
        raise PermissionMissing(f"{VIEW_SUPPLIER} is not held.")
    return actor


def _money(value: str | None, *, field: str) -> Decimal | None:
    """Parse an exact decimal from a string, or None from an absent value."""
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation, TypeError, ValueError:
        raise ValidationError(f"{field} is not a valid decimal.", code="invalid_decimal") from None
    if not parsed.is_finite():
        raise ValidationError(f"{field} is not a finite decimal.", code="invalid_decimal")
    return parsed


class SupplierOut(Schema):
    id: int
    public_id: str
    organization_id: int
    code: str
    name_ar: str
    name_en: str
    contact_name: str
    phone: str
    email: str
    is_active: bool
    #: Omitted entirely without `view_supplier_cost`, not blanked — the same
    #: rule inventory applies to valuation. A null `credit_limit` would be
    #: indistinguishable from a supplier with no agreed limit, which is a
    #: different statement from "you were not shown it".
    payment_terms_days: int | None = None
    credit_limit: str | None = None


class SupplierIn(Schema):
    organization_id: int
    code: str
    name_ar: str
    name_en: str = ""
    contact_name: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    payment_terms_days: int = 0
    credit_limit: str | None = None
    notes: str = ""


class SupplierUpdateIn(Schema):
    name_ar: str
    name_en: str = ""
    contact_name: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    payment_terms_days: int = 0
    credit_limit: str | None = None
    notes: str = ""
    is_active: bool = True


def _serialize(supplier: Supplier, *, include_cost: bool) -> dict[str, Any]:
    """
    One supplier, with the commercial terms omitted rather than blanked.

    A null `credit_limit` in the response would be indistinguishable from a
    supplier with no agreed limit. Dropping the key says something different
    and truer: this caller was not shown it.
    """
    body: dict[str, Any] = {
        "id": supplier.pk,
        "public_id": str(supplier.public_id),
        "organization_id": supplier.organization_id,
        "code": supplier.code,
        "name_ar": supplier.name_ar,
        "name_en": supplier.name_en,
        "contact_name": supplier.contact_name,
        "phone": supplier.phone,
        "email": supplier.email,
        "is_active": supplier.is_active,
    }
    if include_cost:
        body["payment_terms_days"] = supplier.payment_terms_days
        body["credit_limit"] = (
            format(supplier.credit_limit, "f") if supplier.credit_limit is not None else None
        )
    return body


@router.get("/suppliers/", response=list[SupplierOut], summary="List suppliers")
def list_suppliers(request: HttpRequest) -> Any:
    actor = _require_view(request)
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    return [
        _serialize(supplier, include_cost=include_cost)
        for supplier in visible_suppliers(actor).order_by("code")
    ]


@router.get("/suppliers/{supplier_id}/", response=SupplierOut, summary="Read one supplier")
def read_supplier(request: HttpRequest, supplier_id: int) -> Any:
    actor = _require_view(request)
    return _serialize(
        resolve_supplier(actor, supplier_id), include_cost=actor.has_perm(VIEW_SUPPLIER_COST)
    )


@router.post("/suppliers/", response={201: SupplierOut}, summary="Create a supplier")
def create(request: HttpRequest, payload: SupplierIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    require_reachable_organization_permission(actor, MANAGE_SUPPLIERS, organization)

    supplier = create_supplier(
        organization=organization,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        contact_name=payload.contact_name,
        phone=payload.phone,
        email=payload.email,
        address=payload.address,
        payment_terms_days=payload.payment_terms_days,
        credit_limit=_money(payload.credit_limit, field="credit_limit"),
        notes=payload.notes,
    )
    return Status(201, _serialize(supplier, include_cost=True))


@router.put("/suppliers/{supplier_id}/", response=SupplierOut, summary="Update a supplier")
def update(request: HttpRequest, supplier_id: int, payload: SupplierUpdateIn) -> Any:
    actor = _actor(request)
    supplier = resolve_supplier(actor, supplier_id)
    require_reachable_organization_permission(actor, MANAGE_SUPPLIERS, supplier.organization)

    updated = update_supplier(
        supplier=supplier,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        contact_name=payload.contact_name,
        phone=payload.phone,
        email=payload.email,
        address=payload.address,
        payment_terms_days=payload.payment_terms_days,
        credit_limit=_money(payload.credit_limit, field="credit_limit"),
        notes=payload.notes,
        is_active=payload.is_active,
    )
    return _serialize(updated, include_cost=True)


# ---------------------------------------------------------------------------
# Supplier item catalogue
# ---------------------------------------------------------------------------


class SupplierItemOut(Schema):
    id: int
    organization_id: int
    supplier_id: int
    supplier_code: str
    item_id: int
    item_code: str
    item_name_ar: str
    base_unit_code: str
    package_unit_id: int | None
    package_unit_code: str | None
    supplier_sku: str
    supplier_description: str
    lead_time_days: int | None
    #: Exact strings. A minimum order quantity that had been through a binary
    #: float is no longer the quantity anybody agreed to.
    minimum_order_quantity: str | None
    is_preferred: bool
    effective_from: str
    effective_to: str | None
    version: int
    is_active: bool
    #: Omitted without `view_supplier_cost`, never blanked.
    last_quoted_price: str | None = None


class SupplierItemIn(Schema):
    supplier_id: int
    item_id: int
    effective_from: str
    package_unit_id: int | None = None
    supplier_sku: str = ""
    supplier_description: str = ""
    last_quoted_price: str | None = None
    lead_time_days: int | None = None
    minimum_order_quantity: str | None = None
    is_preferred: bool = False
    effective_to: str | None = None
    notes: str = ""


def _date(value: str | None, *, field: str) -> datetime.date | None:
    if value is None or value == "":
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ValidationError(
            f"{field} is not an ISO date (YYYY-MM-DD).", code="invalid_date"
        ) from None


def _serialize_catalogue(row: SupplierItem, *, include_cost: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": row.pk,
        "organization_id": row.organization_id,
        "supplier_id": row.supplier_id,
        "supplier_code": row.supplier.code,
        "item_id": row.item_id,
        "item_code": row.item.code,
        "item_name_ar": row.item.name_ar,
        "base_unit_code": row.item.base_unit.code,
        "package_unit_id": row.package_unit_id,
        "package_unit_code": row.package_unit.code if row.package_unit else None,
        "supplier_sku": row.supplier_sku,
        "supplier_description": row.supplier_description,
        "lead_time_days": row.lead_time_days,
        "minimum_order_quantity": (
            format(row.minimum_order_quantity, "f")
            if row.minimum_order_quantity is not None
            else None
        ),
        "is_preferred": row.is_preferred,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "version": row.version,
        "is_active": row.is_active,
    }
    if include_cost:
        body["last_quoted_price"] = (
            format(row.last_quoted_price, "f") if row.last_quoted_price is not None else None
        )
    return body


def _require_catalogue_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SUPPLIER_ITEM):
        raise PermissionMissing(f"{VIEW_SUPPLIER_ITEM} is not held.")
    return actor


@router.get("/catalogue/", response=list[SupplierItemOut], summary="List catalogue rows")
def list_catalogue(
    request: HttpRequest, supplier_id: int | None = None, item_id: int | None = None
) -> Any:
    actor = _require_catalogue_view(request)
    rows = visible_supplier_items(actor)
    if supplier_id is not None:
        rows = rows.filter(supplier_id=supplier_id)
    if item_id is not None:
        rows = rows.filter(item_id=item_id)
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    return [
        _serialize_catalogue(row, include_cost=include_cost)
        for row in rows.order_by("supplier__code", "item__code", "-effective_from")
    ]


@router.get(
    "/catalogue/{supplier_item_id}/", response=SupplierItemOut, summary="Read one catalogue row"
)
def read_catalogue_row(request: HttpRequest, supplier_item_id: int) -> Any:
    actor = _require_catalogue_view(request)
    return _serialize_catalogue(
        resolve_supplier_item(actor, supplier_item_id),
        include_cost=actor.has_perm(VIEW_SUPPLIER_COST),
    )


@router.post("/catalogue/", response={201: SupplierItemOut}, summary="Add a catalogue row")
def create_catalogue_row(request: HttpRequest, payload: SupplierItemIn) -> Status[Any]:
    actor = _actor(request)
    supplier = resolve_supplier(actor, payload.supplier_id)
    require_reachable_organization_permission(actor, MANAGE_SUPPLIER_ITEMS, supplier.organization)
    item = resolve_item(actor, payload.item_id)
    package = (
        resolve_package_unit(actor, payload.package_unit_id)
        if payload.package_unit_id is not None
        else None
    )

    effective_from = _date(payload.effective_from, field="effective_from")
    if effective_from is None:
        raise ValidationError("effective_from is required.", code="date_required")

    row = create_supplier_item(
        supplier=supplier,
        item=item,
        package_unit=package,
        effective_from=effective_from,
        effective_to=_date(payload.effective_to, field="effective_to"),
        supplier_sku=payload.supplier_sku,
        supplier_description=payload.supplier_description,
        last_quoted_price=_money(payload.last_quoted_price, field="last_quoted_price"),
        lead_time_days=payload.lead_time_days,
        minimum_order_quantity=_money(
            payload.minimum_order_quantity, field="minimum_order_quantity"
        ),
        is_preferred=payload.is_preferred,
        notes=payload.notes,
    )
    return Status(201, _serialize_catalogue(row, include_cost=True))
