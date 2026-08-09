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
    create_opening,
    delete_opening,
    may_see_cost,
    post_opening,
    replace_opening_lines,
    resolve_movement,
    resolve_opening_document,
    return_opening_to_draft,
    reverse_opening,
    submit_opening,
    update_opening,
    visible_movements,
    visible_opening_documents,
    visible_stock,
)
from apps.inventory.models import ConversionType, ItemType, WarehouseType
from apps.inventory.opening import OpeningLineInput
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


# ---------------------------------------------------------------------------
# Opening stock documents — commands, not CRUD over ledger rows
# ---------------------------------------------------------------------------
#
# The API authenticates, resolves scope, parses exact Decimal strings, and
# calls the command layer. It writes no StockMovement, no StockBalance, no
# JournalEntry, and no JournalLine — the atomic posting service does, behind
# the lifecycle these endpoints drive.


class OpeningLineIn(Schema):
    warehouse_id: int
    item_id: int
    #: Strings, both directions. JSON's only numeric type is binary floating
    #: point, and a unit cost that has been through one is a different cost.
    unit_cost: str
    lot_id: int | None = None
    package_conversion_id: int | None = None
    entered_package_quantity: str | None = None
    measured_base_quantity: str | None = None
    base_quantity: str | None = None


class OpeningLineOut(Schema):
    id: int
    sequence: int
    warehouse_id: int
    warehouse_code: str
    item_id: int
    item_code: str
    item_name_ar: str
    base_unit_code: str
    lot_code: str | None
    base_quantity: str
    movement_id: int | None
    inventory_account_code: str | None
    unit_cost: str | None = None
    total_value: str | None = None


class OpeningOut(Schema):
    id: int
    public_id: str
    document_number: str
    status: str
    organization_id: int
    branch_id: int
    branch_code: str
    cutoff_at: datetime.datetime
    business_date: datetime.date
    evidence_reference: str
    narration: str
    created_by: str | None
    submitted_by: str | None
    submitted_at: datetime.datetime | None
    posted_by: str | None
    posted_at: datetime.datetime | None
    reversed_by: str | None
    reversed_at: datetime.datetime | None
    reversal_reason: str
    stock_entry_id: int | None
    journal_entry_number: str | None
    reversal_journal_entry_number: str | None
    line_count: int
    total_value: str | None = None
    lines: list[OpeningLineOut] = []


class OpeningIn(Schema):
    organization_id: int
    branch_id: int
    cutoff_at: datetime.datetime
    evidence_reference: str
    narration: str = ""
    lines: list[OpeningLineIn] = []


class OpeningPatch(Schema):
    cutoff_at: datetime.datetime | None = None
    evidence_reference: str | None = None
    narration: str | None = None
    #: Wholesale replacement, DRAFT only — the same shape journal drafts use.
    lines: list[OpeningLineIn] | None = None


class ReasonIn(Schema):
    reason: str


def _optional_decimal(value: str | None, *, field: str) -> Decimal | None:
    return _decimal(value, field=field) if value is not None else None


def _line_input(actor: User, payload: OpeningLineIn) -> OpeningLineInput:
    """
    One requested line, every identifier resolved with the caller.

    The lot and the conversion are resolved *through the item*, so an id
    belonging to another item — or another organization — is a 404 before the
    domain service ever sees it.
    """
    from apps.inventory.models import InventoryLot, ItemPackageConversion
    from apps.organizations.authorization import resolve_warehouse

    warehouse = resolve_warehouse(actor, payload.warehouse_id)
    item = resolve_item(actor, payload.item_id)

    lot = None
    if payload.lot_id is not None:
        lot = InventoryLot.objects.filter(pk=payload.lot_id, item=item).first()
        if lot is None:
            raise ValidationError(f"Lot {payload.lot_id} does not exist.", code="unknown_lot")

    conversion = None
    if payload.package_conversion_id is not None:
        conversion = ItemPackageConversion.objects.filter(
            pk=payload.package_conversion_id, item=item
        ).first()
        if conversion is None:
            raise ValidationError(
                f"Conversion {payload.package_conversion_id} does not exist.",
                code="unknown_conversion",
            )

    return OpeningLineInput(
        warehouse=warehouse,
        item=item,
        lot=lot,
        package_conversion=conversion,
        unit_cost=_decimal(payload.unit_cost, field="unit_cost"),
        entered_package_quantity=_optional_decimal(
            payload.entered_package_quantity, field="entered_package_quantity"
        ),
        measured_base_quantity=_optional_decimal(
            payload.measured_base_quantity, field="measured_base_quantity"
        ),
        base_quantity=_optional_decimal(payload.base_quantity, field="base_quantity"),
    )


def _serialize_opening_line(line: Any, *, with_cost: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": line.pk,
        "sequence": line.sequence,
        "warehouse_id": line.warehouse_id,
        "warehouse_code": line.warehouse.code,
        "item_id": line.item_id,
        "item_code": line.item.code,
        "item_name_ar": line.item.name_ar,
        "base_unit_code": line.item.base_unit.code,
        "lot_code": line.lot.code if line.lot else None,
        "base_quantity": f"{line.base_quantity:f}",
        "movement_id": line.movement_id,
        "inventory_account_code": line.inventory_account.code if line.inventory_account else None,
    }
    if with_cost:
        payload["unit_cost"] = f"{line.unit_cost:f}"
        payload["total_value"] = f"{line.total_value:f}"
    return payload


def _serialize_opening(document: Any, *, with_cost: bool, with_lines: bool) -> dict[str, Any]:
    lines = list(
        document.lines.select_related(
            "warehouse", "item", "item__base_unit", "lot", "inventory_account"
        ).order_by("sequence")
    )
    payload: dict[str, Any] = {
        "id": document.pk,
        "public_id": str(document.public_id),
        "document_number": document.document_number,
        "status": document.status,
        "organization_id": document.organization_id,
        "branch_id": document.branch_id,
        "branch_code": document.branch.code,
        "cutoff_at": document.cutoff_at,
        "business_date": document.business_date,
        "evidence_reference": document.evidence_reference,
        "narration": document.narration,
        "created_by": str(document.created_by) if document.created_by else None,
        "submitted_by": str(document.submitted_by) if document.submitted_by else None,
        "submitted_at": document.submitted_at,
        "posted_by": str(document.posted_by) if document.posted_by else None,
        "posted_at": document.posted_at,
        "reversed_by": str(document.reversed_by) if document.reversed_by else None,
        "reversed_at": document.reversed_at,
        "reversal_reason": document.reversal_reason,
        "stock_entry_id": document.stock_entry_id,
        "journal_entry_number": (
            document.journal_entry.entry_number if document.journal_entry_id else None
        ),
        "reversal_journal_entry_number": (
            document.reversal_journal_entry.entry_number
            if document.reversal_journal_entry_id
            else None
        ),
        "line_count": len(lines),
        "lines": (
            [_serialize_opening_line(line, with_cost=with_cost) for line in lines]
            if with_lines
            else []
        ),
    }
    if with_cost:
        total = sum((line.total_value for line in lines), Decimal("0"))
        payload["total_value"] = f"{total:f}"
    return payload


@router.get("/openings/", response=list[OpeningOut], summary="Opening documents in scope")
def list_openings(request: HttpRequest, status: str | None = None) -> Any:
    actor = _actor(request)
    documents = visible_opening_documents(actor)
    if status is not None:
        documents = documents.filter(status=status)
    with_cost = may_see_cost(actor)
    return [
        _serialize_opening(document, with_cost=with_cost, with_lines=False)
        for document in documents
    ]


@router.post("/openings/", response={201: OpeningOut}, summary="Create a draft opening")
def create_opening_endpoint(request: HttpRequest, payload: OpeningIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    branch = resolve_branch(actor, payload.branch_id)
    document = create_opening(
        actor=actor,
        organization=organization,
        branch=branch,
        cutoff_at=payload.cutoff_at,
        evidence_reference=payload.evidence_reference,
        narration=payload.narration,
    )
    if payload.lines:
        replace_opening_lines(
            actor=actor,
            document=document,
            lines=[_line_input(actor, line) for line in payload.lines],
        )
    document = resolve_opening_document(actor, document.pk)
    return Status(201, _serialize_opening(document, with_cost=may_see_cost(actor), with_lines=True))


@router.get("/openings/{document_id}/", response=OpeningOut, summary="One opening")
def read_opening(request: HttpRequest, document_id: int) -> Any:
    actor = _actor(request)
    document = resolve_opening_document(actor, document_id)
    return _serialize_opening(document, with_cost=may_see_cost(actor), with_lines=True)


@router.patch("/openings/{document_id}/", response=OpeningOut, summary="Amend a draft opening")
def patch_opening(request: HttpRequest, document_id: int, payload: OpeningPatch) -> Any:
    actor = _actor(request)
    document = resolve_opening_document(actor, document_id)
    update_opening(
        actor=actor,
        document=document,
        cutoff_at=payload.cutoff_at,
        evidence_reference=payload.evidence_reference,
        narration=payload.narration,
    )
    if payload.lines is not None:
        replace_opening_lines(
            actor=actor,
            document=document,
            lines=[_line_input(actor, line) for line in payload.lines],
        )
    document = resolve_opening_document(actor, document_id)
    return _serialize_opening(document, with_cost=may_see_cost(actor), with_lines=True)


@router.delete("/openings/{document_id}/", response={204: None}, summary="Delete a draft opening")
def delete_opening_endpoint(request: HttpRequest, document_id: int) -> Status[None]:
    actor = _actor(request)
    document = resolve_opening_document(actor, document_id)
    delete_opening(actor=actor, document=document)
    return Status(204, None)


@router.post("/openings/{document_id}/submit/", response=OpeningOut, summary="Submit for posting")
def submit_opening_endpoint(request: HttpRequest, document_id: int) -> Any:
    actor = _actor(request)
    document = resolve_opening_document(actor, document_id)
    submitted = submit_opening(actor=actor, document=document)
    return _serialize_opening(submitted, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/openings/{document_id}/return-to-draft/",
    response=OpeningOut,
    summary="Return a submitted opening for correction",
)
def return_opening_endpoint(request: HttpRequest, document_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    document = resolve_opening_document(actor, document_id)
    returned = return_opening_to_draft(actor=actor, document=document, reason=payload.reason)
    return _serialize_opening(returned, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/openings/{document_id}/post/",
    response=OpeningOut,
    summary="Post to the stock ledger and the general ledger",
)
def post_opening_endpoint(request: HttpRequest, document_id: int) -> Any:
    actor = _actor(request)
    document = resolve_opening_document(actor, document_id)
    posted = post_opening(actor=actor, document=document)
    return _serialize_opening(posted, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/openings/{document_id}/reverse/",
    response=OpeningOut,
    summary="Reverse the whole opening",
)
def reverse_opening_endpoint(request: HttpRequest, document_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    document = resolve_opening_document(actor, document_id)
    reversed_document = reverse_opening(actor=actor, document=document, reason=payload.reason)
    return _serialize_opening(reversed_document, with_cost=may_see_cost(actor), with_lines=True)


# ---------------------------------------------------------------------------
# Reconciliation — read only, no repair endpoint exists
# ---------------------------------------------------------------------------


class ReconciliationOut(Schema):
    organization_code: str
    is_clean: bool
    mismatches: list[str]


@router.get(
    "/reconciliation/",
    response=ReconciliationOut,
    summary="Inventory-to-GL reconciliation, scoped and read-only",
)
def reconciliation_report(request: HttpRequest, organization_id: int) -> Any:
    from apps.inventory.reconciliation import verify_inventory_accounting

    actor = _actor(request)
    organization = resolve_organization(actor, organization_id)
    # Reconciliation reads both cost and ledger figures, so it needs both
    # halves of that story.
    from apps.accounting.permissions import VIEW_JOURNAL
    from apps.inventory.permissions import VIEW_VALUATION

    if not actor.has_perm(VIEW_VALUATION):
        raise PermissionMissing(f"{VIEW_VALUATION} is not held.")
    if not actor.has_perm(VIEW_JOURNAL):
        raise PermissionMissing(f"{VIEW_JOURNAL} is not held.")

    mismatches = verify_inventory_accounting(organization)
    return {
        "organization_code": organization.code,
        "is_clean": not mismatches,
        "mismatches": mismatches,
    }
