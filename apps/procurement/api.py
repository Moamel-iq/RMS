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

from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.organizations.authorization import (
    PermissionMissing,
    require_reachable_organization_permission,
    resolve_organization,
)
from apps.procurement.models import Supplier
from apps.procurement.permissions import MANAGE_SUPPLIERS, VIEW_SUPPLIER, VIEW_SUPPLIER_COST
from apps.procurement.selectors import resolve_supplier, visible_suppliers
from apps.procurement.services import create_supplier, update_supplier
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
