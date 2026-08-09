"""
Inventory master-data API.

Master data is not a posted ledger, so this is service-backed CRUD rather than
the command shape the accounting API needs. Everything else is unchanged from
Phase 0 and deliberately so:

* No writable path that skips the services. Every mutation below calls
  `apps/inventory/services.py`; nothing calls `Model.objects.create`.
* An identifier never widens access. Objects are resolved through
  `apps/inventory/selectors.py`, which filters by the caller's own scope, so
  another organization's item is a **404** and not a 403.
* Decimals cross the boundary as **exact strings**, both directions. A
  conversion factor is a technical identity and JSON's only numeric type is
  binary floating point.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.inventory.commands import (
    may_see_cost,
    resolve_movement,
    visible_movements,
    visible_stock,
)
from apps.inventory.models import ConversionType, ItemType, WarehouseType
from apps.inventory.permissions import (
    MANAGE_CATEGORIES,
    MANAGE_CONVERSIONS,
    MANAGE_ITEMS,
    MANAGE_PACKAGE_UNITS,
    MANAGE_WAREHOUSES,
    VIEW_ITEM,
    VIEW_STOCK,
)
from apps.inventory.selectors import (
    resolve_category,
    resolve_item,
    resolve_package_unit,
    visible_categories,
    visible_conversions,
    visible_items,
    visible_package_units,
    visible_warehouses,
)
from apps.inventory.services import (
    create_item,
    create_item_category,
    create_item_conversion,
    create_package_unit,
    create_warehouse,
)
from apps.organizations.authorization import (
    PermissionMissing,
    require_reachable_organization_permission,
    resolve_branch,
    resolve_organization,
)
from apps.users.models import User

router = Router(tags=["inventory"])


def _actor(request: HttpRequest) -> User:
    user: User = request.user  # type: ignore[assignment]
    return user


def _decimal(value: str, *, field: str) -> Decimal:
    """
    Parse an exact decimal from a string.

    A string on the wire and a `Decimal` in Python, with nothing float-shaped
    in between — see the module docstring.
    """
    try:
        parsed = Decimal(value)
    except InvalidOperation, TypeError, ValueError:
        raise ValidationError(f"{field} is not a valid decimal.", code="invalid_decimal") from None
    if not parsed.is_finite():
        raise ValidationError(f"{field} is not a finite decimal.", code="invalid_decimal")
    return parsed


def _require_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_ITEM):
        raise PermissionMissing(f"{VIEW_ITEM} is not held.")
    return actor


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CategoryIn(Schema):
    organization_id: int
    code: str
    name_ar: str
    name_en: str = ""
    parent_id: int | None = None


class CategoryOut(Schema):
    id: int
    organization_id: int
    code: str
    name_ar: str
    name_en: str
    parent_id: int | None
    depth: int
    is_active: bool


class PackageUnitIn(Schema):
    organization_id: int
    code: str
    name_ar: str
    name_en: str = ""


class PackageUnitOut(Schema):
    id: int
    organization_id: int
    code: str
    name_ar: str
    name_en: str
    is_active: bool


class ItemIn(Schema):
    organization_id: int
    code: str
    name_ar: str
    category_id: int
    item_type: str
    base_unit_id: int
    name_en: str = ""
    tracks_lots: bool = False
    tracks_expiry: bool = False
    shelf_life_days: int | None = None
    notes: str = ""


class ItemOut(Schema):
    id: int
    organization_id: int
    code: str
    name_ar: str
    name_en: str
    category_id: int
    item_type: str
    base_unit_id: int
    base_unit_code: str
    tracks_lots: bool
    tracks_expiry: bool
    is_variable_weight: bool
    is_active: bool


class ConversionIn(Schema):
    item_id: int
    package_unit_id: int
    #: A string. A conversion factor is a technical identity and must not be
    #: rounded by a JSON parser on its way in.
    factor_to_base: str
    effective_from: datetime.date
    conversion_type: str = ConversionType.FIXED
    effective_to: datetime.date | None = None
    allows_fractional: bool = True
    minimum_increment: str | None = None
    is_default_purchase_package: bool = False


class ConversionOut(Schema):
    id: int
    item_id: int
    item_code: str
    package_unit_id: int
    package_unit_code: str
    #: A string, at full 12-place precision, always with a period.
    factor_to_base: str
    base_unit_code: str
    conversion_type: str
    effective_from: datetime.date
    effective_to: datetime.date | None
    version: int
    is_default_purchase_package: bool
    is_active: bool


class WarehouseIn(Schema):
    branch_id: int
    code: str
    name_ar: str
    name_en: str = ""
    warehouse_type: str = WarehouseType.PHYSICAL


class WarehouseOut(Schema):
    id: int
    branch_id: int
    code: str
    name_ar: str
    name_en: str
    warehouse_type: str
    is_system: bool
    is_active: bool


def _serialize_item(item: Any) -> dict[str, Any]:
    return {
        "id": item.pk,
        "organization_id": item.organization_id,
        "code": item.code,
        "name_ar": item.name_ar,
        "name_en": item.name_en,
        "category_id": item.category_id,
        "item_type": item.item_type,
        "base_unit_id": item.base_unit_id,
        "base_unit_code": item.base_unit.code,
        "tracks_lots": item.tracks_lots,
        "tracks_expiry": item.tracks_expiry,
        "is_variable_weight": item.is_variable_weight,
        "is_active": item.is_active,
    }


def _serialize_conversion(conversion: Any) -> dict[str, Any]:
    return {
        "id": conversion.pk,
        "item_id": conversion.item_id,
        "item_code": conversion.item.code,
        "package_unit_id": conversion.package_unit_id,
        "package_unit_code": conversion.package_unit.code,
        "factor_to_base": conversion.factor_display,
        "base_unit_code": conversion.item.base_unit.code,
        "conversion_type": conversion.conversion_type,
        "effective_from": conversion.effective_from,
        "effective_to": conversion.effective_to,
        "version": conversion.version,
        "is_default_purchase_package": conversion.is_default_purchase_package,
        "is_active": conversion.is_active,
    }


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@router.get("/categories/", response=list[CategoryOut], summary="List item categories")
def list_categories(request: HttpRequest) -> Any:
    return list(visible_categories(_require_view(request)))


@router.post("/categories/", response={201: CategoryOut}, summary="Create an item category")
def create_category(request: HttpRequest, payload: CategoryIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    require_reachable_organization_permission(actor, MANAGE_CATEGORIES, organization)

    parent = resolve_category(actor, payload.parent_id) if payload.parent_id else None
    category = create_item_category(
        organization=organization,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        parent=parent,
    )
    return Status(201, category)


# ---------------------------------------------------------------------------
# Package units
# ---------------------------------------------------------------------------


@router.get("/package-units/", response=list[PackageUnitOut], summary="List package units")
def list_package_units(request: HttpRequest) -> Any:
    return list(visible_package_units(_require_view(request)))


@router.post("/package-units/", response={201: PackageUnitOut}, summary="Create a package unit")
def create_package(request: HttpRequest, payload: PackageUnitIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    require_reachable_organization_permission(actor, MANAGE_PACKAGE_UNITS, organization)

    package_unit = create_package_unit(
        organization=organization,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
    )
    return Status(201, package_unit)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


@router.get("/items/", response=list[ItemOut], summary="List inventory items")
def list_items(request: HttpRequest) -> Any:
    return [_serialize_item(item) for item in visible_items(_require_view(request))]


@router.get("/items/{item_id}/", response=ItemOut, summary="Read one item")
def read_item(request: HttpRequest, item_id: int) -> Any:
    return _serialize_item(resolve_item(_require_view(request), item_id))


@router.post("/items/", response={201: ItemOut}, summary="Create an inventory item")
def create_inventory_item(request: HttpRequest, payload: ItemIn) -> Status[Any]:
    from apps.units.models import UnitOfMeasure

    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    require_reachable_organization_permission(actor, MANAGE_ITEMS, organization)

    if payload.item_type not in ItemType.values:
        raise ValidationError("Unknown item type.", code="unknown_item_type")

    category = resolve_category(actor, payload.category_id)
    base_unit = UnitOfMeasure.objects.filter(pk=payload.base_unit_id).first()
    if base_unit is None:
        raise ValidationError("Unknown base unit.", code="unknown_base_unit")

    item = create_item(
        organization=organization,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        category=category,
        item_type=payload.item_type,
        base_unit=base_unit,
        tracks_lots=payload.tracks_lots,
        tracks_expiry=payload.tracks_expiry,
        shelf_life_days=payload.shelf_life_days,
        notes=payload.notes,
    )
    return Status(201, _serialize_item(item))


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


@router.get("/conversions/", response=list[ConversionOut], summary="List item package conversions")
def list_conversions(request: HttpRequest) -> Any:
    conversions = visible_conversions(_require_view(request)).select_related("item__base_unit")
    return [_serialize_conversion(conversion) for conversion in conversions]


@router.post("/conversions/", response={201: ConversionOut}, summary="Create an item conversion")
def create_conversion(request: HttpRequest, payload: ConversionIn) -> Status[Any]:
    actor = _actor(request)
    item = resolve_item(actor, payload.item_id)
    require_reachable_organization_permission(actor, MANAGE_CONVERSIONS, item.organization)

    package_unit = resolve_package_unit(actor, payload.package_unit_id)
    if payload.conversion_type not in ConversionType.values:
        raise ValidationError("Unknown conversion type.", code="unknown_conversion_type")

    conversion = create_item_conversion(
        item=item,
        package_unit=package_unit,
        factor_to_base=_decimal(payload.factor_to_base, field="factor_to_base"),
        effective_from=payload.effective_from,
        conversion_type=payload.conversion_type,
        effective_to=payload.effective_to,
        allows_fractional=payload.allows_fractional,
        minimum_increment=(
            _decimal(payload.minimum_increment, field="minimum_increment")
            if payload.minimum_increment is not None
            else None
        ),
        is_default_purchase_package=payload.is_default_purchase_package,
    )
    return Status(201, _serialize_conversion(conversion))


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------


@router.get("/warehouses/", response=list[WarehouseOut], summary="List warehouses in scope")
def list_warehouses(request: HttpRequest) -> Any:
    return list(visible_warehouses(_actor(request)))


@router.post("/warehouses/", response={201: WarehouseOut}, summary="Create a warehouse")
def create_new_warehouse(request: HttpRequest, payload: WarehouseIn) -> Status[Any]:
    from apps.organizations.authorization import require_branch_permission

    actor = _actor(request)
    branch = resolve_branch(actor, payload.branch_id)
    require_branch_permission(actor, MANAGE_WAREHOUSES, branch)

    warehouse = create_warehouse(
        branch=branch,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        warehouse_type=payload.warehouse_type,
    )
    return Status(201, warehouse)


# ---------------------------------------------------------------------------
# Stock and movements — READ ONLY
# ---------------------------------------------------------------------------
#
# There is no POST, PATCH, or DELETE for a movement anywhere in this API, and
# there must never be. Stock moves through a posting service that locks the
# position, checks availability, computes the average, and writes an immutable
# movement; a generic write endpoint over `StockMovement` would be a way to
# put a row in the ledger without any of that happening.
#
# Cost is a separate permission from quantity. A storekeeper holds `view_stock`
# and not `view_valuation`: they must know what they are moving and have no
# business knowing what it cost. The value fields are **omitted**, not blanked
# — a null where a number belongs still says "there is a number here".


class StockOut(Schema):
    warehouse_id: int
    warehouse_code: str
    branch_code: str
    item_id: int
    item_code: str
    item_name_ar: str
    base_unit_code: str
    lot_id: int | None
    lot_code: str | None
    #: Exact strings. JSON's only numeric type is binary floating point, and a
    #: quantity that has been through one is no longer the quantity that was
    #: counted.
    quantity: str
    value: str | None = None
    average_cost: str | None = None


class MovementOut(Schema):
    id: int
    entry_id: int
    posted_sequence: int
    movement_type: str
    warehouse_id: int
    warehouse_code: str
    item_id: int
    item_code: str
    lot_code: str | None
    base_quantity: str
    quantity_after: str
    effective_at: datetime.datetime
    posted_at: datetime.datetime
    source_document_type: str
    source_document_id: str
    source_event: str
    effect_key: str
    unit_cost: str | None = None
    inventory_value: str | None = None
    average_after: str | None = None
    reverses_movement_id: int | None = None


def _serialize_balance(balance: Any, *, with_cost: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "warehouse_id": balance.warehouse_id,
        "warehouse_code": balance.warehouse.code,
        "branch_code": balance.warehouse.branch.code,
        "item_id": balance.item_id,
        "item_code": balance.item.code,
        "item_name_ar": balance.item.name_ar,
        "base_unit_code": balance.item.base_unit.code,
        "lot_id": balance.lot_id,
        "lot_code": balance.lot.code if balance.lot else None,
        "quantity": f"{balance.quantity:f}",
    }
    if with_cost:
        payload["value"] = f"{balance.value:f}"
        payload["average_cost"] = f"{balance.average_cost:f}"
    return payload


def _serialize_movement(movement: Any, *, with_cost: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": movement.pk,
        "entry_id": movement.entry_id,
        "posted_sequence": int(movement.posted_sequence),
        "movement_type": movement.movement_type,
        "warehouse_id": movement.warehouse_id,
        "warehouse_code": movement.warehouse.code,
        "item_id": movement.item_id,
        "item_code": movement.item.code,
        "lot_code": movement.lot.code if movement.lot else None,
        "base_quantity": f"{movement.base_quantity:f}",
        "quantity_after": f"{movement.quantity_after:f}",
        "effective_at": movement.effective_at,
        "posted_at": movement.posted_at,
        "source_document_type": movement.entry.source_document_type,
        "source_document_id": movement.entry.source_document_id,
        "source_event": movement.entry.source_event,
        "effect_key": movement.effect_key,
        "reverses_movement_id": movement.reverses_id,
    }
    if with_cost:
        payload["unit_cost"] = f"{movement.unit_cost:f}"
        payload["inventory_value"] = f"{movement.inventory_value:f}"
        payload["average_after"] = f"{movement.average_after:f}"
    return payload


@router.get("/stock/", response=list[StockOut], summary="Stock on hand, scoped")
def list_stock(
    request: HttpRequest,
    warehouse_id: int | None = None,
    item_id: int | None = None,
) -> Any:
    actor = _actor(request)
    if not actor.has_perm(VIEW_STOCK):
        raise PermissionMissing(f"{VIEW_STOCK} is not held.")

    balances = visible_stock(actor)
    if warehouse_id is not None:
        balances = balances.filter(warehouse_id=warehouse_id)
    if item_id is not None:
        balances = balances.filter(item_id=item_id)

    with_cost = may_see_cost(actor)
    return [
        _serialize_balance(balance, with_cost=with_cost)
        for balance in balances.order_by("warehouse__code", "item__code")
    ]


@router.get("/movements/", response=list[MovementOut], summary="Movement history, scoped")
def list_movements(
    request: HttpRequest,
    warehouse_id: int | None = None,
    item_id: int | None = None,
    limit: int = 100,
) -> Any:
    actor = _actor(request)
    if not actor.has_perm(VIEW_STOCK):
        raise PermissionMissing(f"{VIEW_STOCK} is not held.")

    movements = visible_movements(actor)
    if warehouse_id is not None:
        movements = movements.filter(warehouse_id=warehouse_id)
    if item_id is not None:
        movements = movements.filter(item_id=item_id)

    with_cost = may_see_cost(actor)
    capped = max(1, min(limit, 500))
    return [
        _serialize_movement(movement, with_cost=with_cost)
        for movement in movements.order_by("-posted_sequence")[:capped]
    ]


@router.get("/movements/{movement_id}/", response=MovementOut, summary="One movement")
def read_movement(request: HttpRequest, movement_id: int) -> Any:
    actor = _actor(request)
    if not actor.has_perm(VIEW_STOCK):
        raise PermissionMissing(f"{VIEW_STOCK} is not held.")
    # Resolved WITH the caller, so a foreign movement is a 404 and never a 403
    # that would confirm the id names something real.
    movement = resolve_movement(actor, movement_id)
    return _serialize_movement(movement, with_cost=may_see_cost(actor))
