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
from django.db.models import Sum
from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.inventory.adjustments import AdjustmentLineInput
from apps.inventory.commands import (
    add_adjustment_line,
    add_unexpected_count_line,
    approve_stock_count,
    blind_count_sheet,
    cancel_stock_count,
    create_adjustment,
    create_document,
    create_opening,
    create_reason_code,
    create_stock_count,
    create_transfer,
    create_transfer_receipt,
    create_transfer_shortage,
    delete_adjustment,
    delete_document,
    delete_opening,
    delete_stock_count,
    delete_transfer,
    delete_transfer_receipt,
    dispatch_transfer,
    may_see_cost,
    post_adjustment,
    post_document,
    post_opening,
    post_transfer_receipt,
    post_transfer_shortage,
    record_stock_counts,
    replace_document_lines,
    replace_opening_lines,
    replace_transfer_lines,
    replace_transfer_receipt_lines,
    resolve_adjustment,
    resolve_count,
    resolve_count_line,
    resolve_document,
    resolve_document_line,
    resolve_movement,
    resolve_opening_document,
    resolve_reason_code,
    resolve_receipt,
    resolve_shortage,
    resolve_transfer,
    resolve_transfer_line,
    return_opening_to_draft,
    reverse_adjustment,
    reverse_dispatch,
    reverse_document,
    reverse_opening,
    reverse_stock_count,
    reverse_transfer_receipt,
    reverse_transfer_shortage,
    start_stock_count,
    submit_opening,
    submit_stock_count,
    update_adjustment,
    update_document,
    update_opening,
    update_reason_code,
    update_stock_count,
    update_transfer,
    update_transfer_receipt,
    visible_adjustments,
    visible_counts,
    visible_documents,
    visible_in_transit,
    visible_movements,
    visible_opening_documents,
    visible_reason_codes,
    visible_stock,
    visible_transfers,
)
from apps.inventory.counts import ApprovedCost, CountEntry
from apps.inventory.models import (
    ConversionType,
    InventoryDocumentStatus,
    InventoryDocumentType,
    ItemType,
    WarehouseType,
)
from apps.inventory.opening import OpeningLineInput
from apps.inventory.operations import DocumentLineInput
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
from apps.inventory.transfers import ReceiptLineInput, TransferLineInput
from apps.organizations.authorization import (
    OutOfScope,
    PermissionMissing,
    require_reachable_organization_permission,
    resolve_branch,
    resolve_organization,
    resolve_warehouse,
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
# Operational documents: receipts, issues, returns-in
# ---------------------------------------------------------------------------
#
# One shape, three mounted paths. The document type is fixed by the route
# rather than taken from the payload: `/receipts/` posts receipts and nothing
# else, so a caller cannot turn an issue into a receipt by editing a field,
# and an id from one series can never resolve under another.


class DocumentLineIn(Schema):
    item_id: int
    lot_id: int | None = None
    package_conversion_id: int | None = None
    #: Strings, both directions. JSON's only numeric type is binary floating
    #: point, and a quantity through one is no longer the quantity counted.
    entered_package_quantity: str | None = None
    measured_base_quantity: str | None = None
    base_quantity: str | None = None
    #: Receipts only. An issue and a return are valued by the ledger.
    unit_cost: str | None = None
    #: Returns only: the posted issue line being returned against.
    source_issue_line_id: int | None = None
    #: Waste only, and mandatory there.
    reason_code_id: int | None = None
    line_comment: str = ""


class DocumentLineOut(Schema):
    id: int
    sequence: int
    item_id: int
    item_code: str
    item_name_ar: str
    base_unit_code: str
    lot_code: str | None
    base_quantity: str
    movement_id: int | None
    inventory_account_code: str | None
    contra_account_code: str | None
    source_issue_line_id: int | None
    reason_code: str | None
    line_comment: str
    unit_cost: str | None = None
    total_value: str | None = None


class DocumentOut(Schema):
    id: int
    public_id: str
    document_number: str
    document_type: str
    status: str
    organization_id: int
    branch_id: int
    branch_code: str
    warehouse_id: int
    warehouse_code: str
    effective_at: datetime.datetime
    business_date: datetime.date
    evidence_reference: str
    narration: str
    cost_center_code: str | None
    created_by: str | None
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
    lines: list[DocumentLineOut] = []


class DocumentIn(Schema):
    organization_id: int
    branch_id: int
    warehouse_id: int
    effective_at: datetime.datetime
    evidence_reference: str
    narration: str = ""
    cost_center_id: int | None = None
    lines: list[DocumentLineIn] = []


class DocumentPatch(Schema):
    effective_at: datetime.datetime | None = None
    evidence_reference: str | None = None
    narration: str | None = None
    cost_center_id: int | None = None
    lines: list[DocumentLineIn] | None = None


def _document_line_input(actor: User, payload: DocumentLineIn) -> DocumentLineInput:
    """
    One requested line, every identifier resolved with the caller.

    The lot and the conversion are resolved *through the item*, so an id
    belonging to another item — or another organization — is a 404 before the
    domain service sees it. The source issue line is resolved through the
    caller's warehouse scope for the same reason.
    """
    from apps.inventory.models import InventoryLot, ItemPackageConversion

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

    source_line = None
    if payload.source_issue_line_id is not None:
        source_line = resolve_document_line(actor, payload.source_issue_line_id)

    return DocumentLineInput(
        item=item,
        lot=lot,
        package_conversion=conversion,
        entered_package_quantity=_optional_decimal(
            payload.entered_package_quantity, field="entered_package_quantity"
        ),
        measured_base_quantity=_optional_decimal(
            payload.measured_base_quantity, field="measured_base_quantity"
        ),
        base_quantity=_optional_decimal(payload.base_quantity, field="base_quantity"),
        unit_cost=_optional_decimal(payload.unit_cost, field="unit_cost"),
        source_issue_line=source_line,
        reason_code=(
            resolve_reason_code(actor, payload.reason_code_id)
            if payload.reason_code_id is not None
            else None
        ),
        line_comment=payload.line_comment,
    )


def _serialize_document_line(line: Any, *, with_cost: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": line.pk,
        "sequence": line.sequence,
        "item_id": line.item_id,
        "item_code": line.item.code,
        "item_name_ar": line.item.name_ar,
        "base_unit_code": line.item.base_unit.code,
        "lot_code": line.lot.code if line.lot else None,
        "base_quantity": f"{line.base_quantity:f}",
        "movement_id": line.movement_id,
        "inventory_account_code": (
            line.inventory_account.code if line.inventory_account_id else None
        ),
        "contra_account_code": line.contra_account.code if line.contra_account_id else None,
        "source_issue_line_id": line.source_issue_line_id,
        "reason_code": line.reason_code.code if line.reason_code_id else None,
        "line_comment": line.line_comment,
    }
    if with_cost:
        payload["unit_cost"] = f"{line.unit_cost:f}" if line.unit_cost is not None else None
        payload["total_value"] = f"{line.total_value:f}" if line.total_value is not None else None
    return payload


def _serialize_document(document: Any, *, with_cost: bool, with_lines: bool) -> dict[str, Any]:
    lines = list(
        document.lines.select_related(
            "item",
            "item__base_unit",
            "lot",
            "inventory_account",
            "contra_account",
            "reason_code",
        ).order_by("sequence")
    )
    payload: dict[str, Any] = {
        "id": document.pk,
        "public_id": str(document.public_id),
        "document_number": document.document_number,
        "document_type": document.document_type,
        "status": document.status,
        "organization_id": document.organization_id,
        "branch_id": document.branch_id,
        "branch_code": document.branch.code,
        "warehouse_id": document.warehouse_id,
        "warehouse_code": document.warehouse.code,
        "effective_at": document.effective_at,
        "business_date": document.business_date,
        "evidence_reference": document.evidence_reference,
        "narration": document.narration,
        "cost_center_code": document.cost_center.code if document.cost_center_id else None,
        "created_by": str(document.created_by) if document.created_by else None,
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
            [_serialize_document_line(line, with_cost=with_cost) for line in lines]
            if with_lines
            else []
        ),
    }
    if with_cost:
        total = sum((line.total_value or Decimal("0") for line in lines), Decimal("0"))
        payload["total_value"] = f"{total:f}"
    return payload


def _list_documents(request: HttpRequest, document_type: str, status: str | None) -> Any:
    actor = _actor(request)
    documents = visible_documents(actor, document_type=document_type)
    if status is not None:
        documents = documents.filter(status=status)
    with_cost = may_see_cost(actor)
    return [
        _serialize_document(document, with_cost=with_cost, with_lines=False)
        for document in documents
    ]


def _create_document(request: HttpRequest, document_type: str, payload: DocumentIn) -> Status[Any]:
    from apps.accounting.models import CostCenter

    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    branch = resolve_branch(actor, payload.branch_id)
    warehouse = resolve_warehouse(actor, payload.warehouse_id)

    cost_center = None
    if payload.cost_center_id is not None:
        cost_center = CostCenter.objects.filter(
            pk=payload.cost_center_id, organization=organization
        ).first()
        if cost_center is None:
            raise ValidationError(
                f"Cost center {payload.cost_center_id} does not exist.",
                code="unknown_cost_center",
            )

    document = create_document(
        actor=actor,
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=document_type,
        effective_at=payload.effective_at,
        evidence_reference=payload.evidence_reference,
        narration=payload.narration,
        cost_center=cost_center,
    )
    if payload.lines:
        replace_document_lines(
            actor=actor,
            document=document,
            lines=[_document_line_input(actor, line) for line in payload.lines],
        )
    document = resolve_document(actor, document.pk, document_type=document_type)
    return Status(
        201, _serialize_document(document, with_cost=may_see_cost(actor), with_lines=True)
    )


def _patch_document(
    request: HttpRequest, document_type: str, document_id: int, payload: DocumentPatch
) -> Any:
    from apps.accounting.models import CostCenter

    actor = _actor(request)
    document = resolve_document(actor, document_id, document_type=document_type)

    cost_center = None
    if payload.cost_center_id is not None:
        cost_center = CostCenter.objects.filter(
            pk=payload.cost_center_id, organization=document.organization
        ).first()
        if cost_center is None:
            raise ValidationError(
                f"Cost center {payload.cost_center_id} does not exist.",
                code="unknown_cost_center",
            )

    update_document(
        actor=actor,
        document=document,
        effective_at=payload.effective_at,
        evidence_reference=payload.evidence_reference,
        narration=payload.narration,
        cost_center=cost_center,
    )
    if payload.lines is not None:
        replace_document_lines(
            actor=actor,
            document=document,
            lines=[_document_line_input(actor, line) for line in payload.lines],
        )
    document = resolve_document(actor, document_id, document_type=document_type)
    return _serialize_document(document, with_cost=may_see_cost(actor), with_lines=True)


def _register_document_endpoints(path: str, document_type: str, label: str) -> None:
    """
    Mount one CRUD-plus-commands set for a document type.

    Registered from a loop rather than written three times: the three sets are
    identical apart from their path and type, and three hand-copied blocks
    would drift the moment one gained a check the others needed.
    """

    @router.get(
        f"/{path}/", response=list[DocumentOut], summary=f"{label}s in scope", exclude_unset=True
    )
    def list_documents(request: HttpRequest, status: str | None = None) -> Any:
        return _list_documents(request, document_type, status)

    @router.post(
        f"/{path}/",
        response={201: DocumentOut},
        summary=f"Create a draft {label}",
        exclude_unset=True,
    )
    def create_endpoint(request: HttpRequest, payload: DocumentIn) -> Status[Any]:
        return _create_document(request, document_type, payload)

    @router.get(
        f"/{path}/{{document_id}}/",
        response=DocumentOut,
        summary=f"One {label}",
        exclude_unset=True,
    )
    def read_endpoint(request: HttpRequest, document_id: int) -> Any:
        actor = _actor(request)
        document = resolve_document(actor, document_id, document_type=document_type)
        return _serialize_document(document, with_cost=may_see_cost(actor), with_lines=True)

    @router.patch(
        f"/{path}/{{document_id}}/",
        response=DocumentOut,
        summary=f"Amend a draft {label}",
        exclude_unset=True,
    )
    def patch_endpoint(request: HttpRequest, document_id: int, payload: DocumentPatch) -> Any:
        return _patch_document(request, document_type, document_id, payload)

    @router.delete(
        f"/{path}/{{document_id}}/", response={204: None}, summary=f"Delete a draft {label}"
    )
    def delete_endpoint(request: HttpRequest, document_id: int) -> Status[None]:
        actor = _actor(request)
        document = resolve_document(actor, document_id, document_type=document_type)
        delete_document(actor=actor, document=document)
        return Status(204, None)

    @router.post(
        f"/{path}/{{document_id}}/post/",
        response=DocumentOut,
        summary=f"Post the {label} to both ledgers",
    )
    def post_endpoint(request: HttpRequest, document_id: int) -> Any:
        actor = _actor(request)
        document = resolve_document(actor, document_id, document_type=document_type)
        posted = post_document(actor=actor, document=document)
        return _serialize_document(posted, with_cost=may_see_cost(actor), with_lines=True)

    @router.post(
        f"/{path}/{{document_id}}/reverse/",
        response=DocumentOut,
        summary=f"Reverse the whole {label}",
    )
    def reverse_endpoint(request: HttpRequest, document_id: int, payload: ReasonIn) -> Any:
        actor = _actor(request)
        document = resolve_document(actor, document_id, document_type=document_type)
        reversed_document = reverse_document(actor=actor, document=document, reason=payload.reason)
        return _serialize_document(
            reversed_document, with_cost=may_see_cost(actor), with_lines=True
        )


_register_document_endpoints("receipts", InventoryDocumentType.RECEIPT, "goods receipt")
_register_document_endpoints("issues", InventoryDocumentType.ISSUE, "consumption issue")
_register_document_endpoints("returns-in", InventoryDocumentType.RETURN_IN, "return-in")
# Task 1.6. Waste joins the loop rather than getting a hand-written set,
# because it *is* one of these documents — and a fourth hand-copied block would
# be the first to miss whatever the other three gain next.
_register_document_endpoints("waste", InventoryDocumentType.WASTE, "waste note")


# ---------------------------------------------------------------------------
# Transfers, receipts and shortages (Task 1.5 §V)
# ---------------------------------------------------------------------------
#
# Command-oriented, like the operational documents: a transfer is dispatched
# through `/dispatch/`, not by PATCHing a status. `StockMovement` stays
# read-only everywhere.
#
# The route constrains the object at every level. A receipt id submitted under
# another transfer's route is a 404, never somebody else's receipt returned
# politely — `resolve_receipt(actor, id, transfer=...)` does the constraining,
# so there is no moment where an out-of-scope row sits in a local variable.


class TransferLineIn(Schema):
    item_id: int
    lot_id: int | None = None
    package_conversion_id: int | None = None
    #: Strings, both directions. JSON's only numeric type is binary floating
    #: point, and a quantity through one is no longer the quantity counted.
    entered_package_quantity: str | None = None
    measured_base_quantity: str | None = None
    base_quantity: str | None = None


class TransferLineOut(Schema):
    id: int
    sequence: int
    item_id: int
    item_code: str
    item_name_ar: str
    base_unit_code: str
    lot_code: str | None
    base_quantity: str
    received_quantity: str
    shortage_quantity: str
    remaining_quantity: str
    unit_cost: str | None = None
    total_value: str | None = None
    remaining_value: str | None = None


class TransferOut(Schema):
    id: int
    public_id: str
    transfer_number: str
    status: str
    organization_id: int
    source_warehouse_id: int
    source_warehouse_code: str
    source_branch_code: str
    destination_warehouse_id: int
    destination_warehouse_code: str
    destination_branch_code: str
    is_cross_branch: bool
    effective_at: datetime.datetime
    business_date: datetime.date
    evidence_reference: str
    narration: str
    created_by: str | None
    dispatched_by: str | None
    dispatched_at: datetime.datetime | None
    reversed_by: str | None
    reversed_at: datetime.datetime | None
    reversal_reason: str
    stock_entry_id: int | None
    journal_entry_number: str | None
    line_count: int
    total_value: str | None = None
    remaining_value: str | None = None
    lines: list[TransferLineOut] = []


class TransferIn(Schema):
    organization_id: int
    source_warehouse_id: int
    destination_warehouse_id: int
    effective_at: datetime.datetime
    evidence_reference: str
    narration: str = ""
    lines: list[TransferLineIn] = []


class TransferPatch(Schema):
    effective_at: datetime.datetime | None = None
    evidence_reference: str | None = None
    narration: str | None = None
    lines: list[TransferLineIn] | None = None


class TransferReceiptLineIn(Schema):
    transfer_line_id: int
    entered_package_quantity: str | None = None
    measured_base_quantity: str | None = None
    base_quantity: str | None = None


class TransferReceiptLineOut(Schema):
    id: int
    sequence: int
    transfer_line_id: int
    item_code: str
    base_unit_code: str
    lot_code: str | None
    base_quantity: str
    unit_cost: str | None = None
    allocated_value: str | None = None


class TransferReceiptOut(Schema):
    id: int
    public_id: str
    transfer_id: int
    receipt_number: str
    status: str
    effective_at: datetime.datetime
    business_date: datetime.date
    source_business_date: datetime.date | None
    evidence_reference: str
    narration: str
    received_by: str | None
    posted_at: datetime.datetime | None
    reversed_by: str | None
    reversed_at: datetime.datetime | None
    reversal_reason: str
    source_stock_entry_id: int | None
    destination_stock_entry_id: int | None
    source_journal_entry_number: str | None
    destination_journal_entry_number: str | None
    is_cross_branch: bool
    line_count: int
    total_value: str | None = None
    lines: list[TransferReceiptLineOut] = []


class TransferReceiptIn(Schema):
    effective_at: datetime.datetime
    evidence_reference: str
    narration: str = ""
    lines: list[TransferReceiptLineIn] = []


class TransferReceiptPatch(Schema):
    effective_at: datetime.datetime | None = None
    evidence_reference: str | None = None
    narration: str | None = None
    lines: list[TransferReceiptLineIn] | None = None


class TransferShortageLineOut(Schema):
    id: int
    sequence: int
    transfer_line_id: int
    item_code: str
    base_quantity: str
    unit_cost: str | None = None
    allocated_value: str | None = None


class TransferShortageOut(Schema):
    id: int
    public_id: str
    transfer_id: int
    shortage_number: str
    status: str
    effective_at: datetime.datetime
    business_date: datetime.date
    reason: str
    evidence_reference: str
    cost_center_code: str
    closed_by: str | None
    posted_at: datetime.datetime | None
    reversed_by: str | None
    reversed_at: datetime.datetime | None
    reversal_reason: str
    stock_entry_id: int | None
    journal_entry_number: str | None
    line_count: int
    total_value: str | None = None
    lines: list[TransferShortageLineOut] = []


class TransferShortageIn(Schema):
    effective_at: datetime.datetime
    reason: str
    evidence_reference: str
    cost_center_id: int


class InTransitOut(Schema):
    transfer_id: int
    transfer_number: str
    source_warehouse_code: str
    destination_warehouse_code: str
    item_code: str
    item_name_ar: str
    base_unit_code: str
    lot_code: str | None
    remaining_quantity: str
    remaining_value: str | None = None


def _transfer_line_input(actor: User, payload: TransferLineIn) -> TransferLineInput:
    """One requested line, every identifier resolved with the caller."""
    from apps.inventory.models import InventoryLot, ItemPackageConversion

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

    return TransferLineInput(
        item=item,
        lot=lot,
        package_conversion=conversion,
        entered_package_quantity=_optional_decimal(
            payload.entered_package_quantity, field="entered_package_quantity"
        ),
        measured_base_quantity=_optional_decimal(
            payload.measured_base_quantity, field="measured_base_quantity"
        ),
        base_quantity=_optional_decimal(payload.base_quantity, field="base_quantity"),
    )


def _resolved_totals(line: Any) -> tuple[Decimal, Decimal]:
    """How much of one transfer line has been received and written off."""
    received = line.receipt_lines.filter(receipt__status=InventoryDocumentStatus.POSTED).aggregate(
        total=Sum("base_quantity")
    )["total"] or Decimal("0")
    short = line.shortage_lines.filter(shortage__status=InventoryDocumentStatus.POSTED).aggregate(
        total=Sum("base_quantity")
    )["total"] or Decimal("0")
    return received, short


def _serialize_transfer_line(line: Any, *, with_cost: bool) -> dict[str, Any]:
    received, short = _resolved_totals(line)
    payload: dict[str, Any] = {
        "id": line.pk,
        "sequence": line.sequence,
        "item_id": line.item_id,
        "item_code": line.item.code,
        "item_name_ar": line.item.name_ar,
        "base_unit_code": line.item.base_unit.code,
        "lot_code": line.lot.code if line.lot else None,
        "base_quantity": f"{line.base_quantity:f}",
        "received_quantity": f"{received:f}",
        "shortage_quantity": f"{short:f}",
        "remaining_quantity": f"{line.remaining_quantity:f}",
    }
    if with_cost:
        payload["unit_cost"] = f"{line.unit_cost:f}" if line.unit_cost is not None else None
        payload["total_value"] = f"{line.total_value:f}" if line.total_value is not None else None
        payload["remaining_value"] = f"{line.remaining_value:f}"
    return payload


def _serialize_transfer(transfer: Any, *, with_cost: bool, with_lines: bool) -> dict[str, Any]:
    lines = list(
        transfer.lines.select_related("item", "item__base_unit", "lot").order_by("sequence")
    )
    payload: dict[str, Any] = {
        "id": transfer.pk,
        "public_id": str(transfer.public_id),
        "transfer_number": transfer.transfer_number,
        "status": transfer.status,
        "organization_id": transfer.organization_id,
        "source_warehouse_id": transfer.source_warehouse_id,
        "source_warehouse_code": transfer.source_warehouse.code,
        "source_branch_code": transfer.source_warehouse.branch.code,
        "destination_warehouse_id": transfer.destination_warehouse_id,
        "destination_warehouse_code": transfer.destination_warehouse.code,
        "destination_branch_code": transfer.destination_warehouse.branch.code,
        "is_cross_branch": transfer.is_cross_branch,
        "effective_at": transfer.effective_at,
        "business_date": transfer.business_date,
        "evidence_reference": transfer.evidence_reference,
        "narration": transfer.narration,
        "created_by": str(transfer.created_by) if transfer.created_by else None,
        "dispatched_by": str(transfer.dispatched_by) if transfer.dispatched_by else None,
        "dispatched_at": transfer.dispatched_at,
        "reversed_by": str(transfer.reversed_by) if transfer.reversed_by else None,
        "reversed_at": transfer.reversed_at,
        "reversal_reason": transfer.reversal_reason,
        "stock_entry_id": transfer.stock_entry_id,
        "journal_entry_number": (
            transfer.journal_entry.entry_number if transfer.journal_entry_id else None
        ),
        "line_count": len(lines),
        "lines": (
            [_serialize_transfer_line(line, with_cost=with_cost) for line in lines]
            if with_lines
            else []
        ),
    }
    if with_cost:
        payload["total_value"] = (
            f"{sum((line.total_value or Decimal('0') for line in lines), Decimal('0')):f}"
        )
        payload["remaining_value"] = (
            f"{sum((line.remaining_value for line in lines), Decimal('0')):f}"
        )
    return payload


def _serialize_receipt(receipt: Any, *, with_cost: bool, with_lines: bool) -> dict[str, Any]:
    lines = list(
        receipt.lines.select_related(
            "transfer_line",
            "transfer_line__item",
            "transfer_line__item__base_unit",
            "transfer_line__lot",
        ).order_by("sequence")
    )
    payload: dict[str, Any] = {
        "id": receipt.pk,
        "public_id": str(receipt.public_id),
        "transfer_id": receipt.transfer_id,
        "receipt_number": receipt.receipt_number,
        "status": receipt.status,
        "effective_at": receipt.effective_at,
        "business_date": receipt.business_date,
        "source_business_date": receipt.source_business_date,
        "evidence_reference": receipt.evidence_reference,
        "narration": receipt.narration,
        "received_by": str(receipt.received_by) if receipt.received_by else None,
        "posted_at": receipt.posted_at,
        "reversed_by": str(receipt.reversed_by) if receipt.reversed_by else None,
        "reversed_at": receipt.reversed_at,
        "reversal_reason": receipt.reversal_reason,
        "source_stock_entry_id": receipt.source_stock_entry_id,
        "destination_stock_entry_id": receipt.destination_stock_entry_id,
        "source_journal_entry_number": (
            receipt.source_journal_entry.entry_number if receipt.source_journal_entry_id else None
        ),
        "destination_journal_entry_number": (
            receipt.destination_journal_entry.entry_number
            if receipt.destination_journal_entry_id
            else None
        ),
        "is_cross_branch": receipt.transfer.is_cross_branch,
        "line_count": len(lines),
        "lines": (
            [
                {
                    "id": line.pk,
                    "sequence": line.sequence,
                    "transfer_line_id": line.transfer_line_id,
                    "item_code": line.transfer_line.item.code,
                    "base_unit_code": line.transfer_line.item.base_unit.code,
                    "lot_code": (
                        line.transfer_line.lot.code if line.transfer_line.lot_id else None
                    ),
                    "base_quantity": f"{line.base_quantity:f}",
                    **(
                        {
                            "unit_cost": (
                                f"{line.unit_cost:f}" if line.unit_cost is not None else None
                            ),
                            "allocated_value": (
                                f"{line.allocated_value:f}"
                                if line.allocated_value is not None
                                else None
                            ),
                        }
                        if with_cost
                        else {}
                    ),
                }
                for line in lines
            ]
            if with_lines
            else []
        ),
    }
    if with_cost:
        total = sum((line.allocated_value or Decimal("0") for line in lines), Decimal("0"))
        payload["total_value"] = f"{total:f}"
    return payload


def _serialize_shortage(shortage: Any, *, with_cost: bool) -> dict[str, Any]:
    lines = list(shortage.lines.select_related("transfer_line", "transfer_line__item"))
    payload: dict[str, Any] = {
        "id": shortage.pk,
        "public_id": str(shortage.public_id),
        "transfer_id": shortage.transfer_id,
        "shortage_number": shortage.shortage_number,
        "status": shortage.status,
        "effective_at": shortage.effective_at,
        "business_date": shortage.business_date,
        "reason": shortage.reason,
        "evidence_reference": shortage.evidence_reference,
        "cost_center_code": shortage.cost_center.code,
        "closed_by": str(shortage.closed_by) if shortage.closed_by else None,
        "posted_at": shortage.posted_at,
        "reversed_by": str(shortage.reversed_by) if shortage.reversed_by else None,
        "reversed_at": shortage.reversed_at,
        "reversal_reason": shortage.reversal_reason,
        "stock_entry_id": shortage.stock_entry_id,
        "journal_entry_number": (
            shortage.journal_entry.entry_number if shortage.journal_entry_id else None
        ),
        "line_count": len(lines),
        "lines": [
            {
                "id": line.pk,
                "sequence": line.sequence,
                "transfer_line_id": line.transfer_line_id,
                "item_code": line.transfer_line.item.code,
                "base_quantity": f"{line.base_quantity:f}",
                **(
                    {
                        "unit_cost": f"{line.unit_cost:f}" if line.unit_cost is not None else None,
                        "allocated_value": (
                            f"{line.allocated_value:f}"
                            if line.allocated_value is not None
                            else None
                        ),
                    }
                    if with_cost
                    else {}
                ),
            }
            for line in lines
        ],
    }
    if with_cost:
        total = sum((line.allocated_value or Decimal("0") for line in lines), Decimal("0"))
        payload["total_value"] = f"{total:f}"
    return payload


@router.get("/transfers/", response=list[TransferOut], summary="Transfers in scope")
def list_transfers(request: HttpRequest, status: str | None = None) -> Any:
    actor = _actor(request)
    transfers = visible_transfers(actor)
    if status is not None:
        transfers = transfers.filter(status=status)
    with_cost = may_see_cost(actor)
    return [
        _serialize_transfer(transfer, with_cost=with_cost, with_lines=False)
        for transfer in transfers
    ]


@router.post("/transfers/", response={201: TransferOut}, summary="Create a draft transfer")
def create_transfer_endpoint(request: HttpRequest, payload: TransferIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    source = resolve_warehouse(actor, payload.source_warehouse_id)
    destination = resolve_warehouse(actor, payload.destination_warehouse_id)
    transfer = create_transfer(
        actor=actor,
        organization=organization,
        source_warehouse=source,
        destination_warehouse=destination,
        effective_at=payload.effective_at,
        evidence_reference=payload.evidence_reference,
        narration=payload.narration,
    )
    if payload.lines:
        replace_transfer_lines(
            actor=actor,
            transfer=transfer,
            lines=[_transfer_line_input(actor, line) for line in payload.lines],
        )
    transfer = resolve_transfer(actor, transfer.pk)
    return Status(
        201, _serialize_transfer(transfer, with_cost=may_see_cost(actor), with_lines=True)
    )


@router.get("/transfers/{transfer_id}/", response=TransferOut, summary="One transfer")
def read_transfer(request: HttpRequest, transfer_id: int) -> Any:
    actor = _actor(request)
    transfer = resolve_transfer(actor, transfer_id)
    return _serialize_transfer(transfer, with_cost=may_see_cost(actor), with_lines=True)


@router.patch("/transfers/{transfer_id}/", response=TransferOut, summary="Amend a draft transfer")
def patch_transfer(request: HttpRequest, transfer_id: int, payload: TransferPatch) -> Any:
    actor = _actor(request)
    transfer = resolve_transfer(actor, transfer_id)
    update_transfer(
        actor=actor,
        transfer=transfer,
        effective_at=payload.effective_at,
        evidence_reference=payload.evidence_reference,
        narration=payload.narration,
    )
    if payload.lines is not None:
        replace_transfer_lines(
            actor=actor,
            transfer=transfer,
            lines=[_transfer_line_input(actor, line) for line in payload.lines],
        )
    transfer = resolve_transfer(actor, transfer_id)
    return _serialize_transfer(transfer, with_cost=may_see_cost(actor), with_lines=True)


@router.delete("/transfers/{transfer_id}/", response={204: None}, summary="Delete a draft transfer")
def delete_transfer_endpoint(request: HttpRequest, transfer_id: int) -> Status[None]:
    actor = _actor(request)
    transfer = resolve_transfer(actor, transfer_id)
    delete_transfer(actor=actor, transfer=transfer)
    return Status(204, None)


@router.post(
    "/transfers/{transfer_id}/dispatch/", response=TransferOut, summary="Dispatch the goods"
)
def dispatch_endpoint(request: HttpRequest, transfer_id: int) -> Any:
    actor = _actor(request)
    transfer = resolve_transfer(actor, transfer_id)
    dispatched = dispatch_transfer(actor=actor, transfer=transfer)
    return _serialize_transfer(dispatched, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/transfers/{transfer_id}/reverse-dispatch/",
    response=TransferOut,
    summary="Reverse a dispatch nothing has happened against",
)
def reverse_dispatch_endpoint(request: HttpRequest, transfer_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    transfer = resolve_transfer(actor, transfer_id)
    reversed_transfer = reverse_dispatch(actor=actor, transfer=transfer, reason=payload.reason)
    return _serialize_transfer(reversed_transfer, with_cost=may_see_cost(actor), with_lines=True)


@router.get(
    "/transfers/{transfer_id}/receipts/",
    response=list[TransferReceiptOut],
    summary="Receipts against one transfer",
)
def list_receipts(request: HttpRequest, transfer_id: int) -> Any:
    actor = _actor(request)
    transfer = resolve_transfer(actor, transfer_id)
    with_cost = may_see_cost(actor)
    return [
        _serialize_receipt(receipt, with_cost=with_cost, with_lines=False)
        for receipt in transfer.receipts.select_related(
            "transfer",
            "transfer__source_warehouse",
            "transfer__destination_warehouse",
            "received_by",
            "reversed_by",
        ).order_by("-created_at", "-id")
    ]


@router.post(
    "/transfers/{transfer_id}/receipts/",
    response={201: TransferReceiptOut},
    summary="Create a draft receipt",
)
def create_receipt_endpoint(
    request: HttpRequest, transfer_id: int, payload: TransferReceiptIn
) -> Status[Any]:
    actor = _actor(request)
    transfer = resolve_transfer(actor, transfer_id)
    receipt = create_transfer_receipt(
        actor=actor,
        transfer=transfer,
        effective_at=payload.effective_at,
        evidence_reference=payload.evidence_reference,
        narration=payload.narration,
    )
    if payload.lines:
        _replace_receipt_lines(actor, receipt, payload.lines)
    receipt = resolve_receipt(actor, receipt.pk, transfer=transfer)
    return Status(201, _serialize_receipt(receipt, with_cost=may_see_cost(actor), with_lines=True))


def _replace_receipt_lines(actor: User, receipt: Any, lines: list[TransferReceiptLineIn]) -> None:
    """
    Resolve each line's transfer line **through the receipt's own transfer**.

    A transfer-line id from another transfer is refused as out of scope before
    the domain service sees it, so a caller cannot draw down one consignment's
    remaining value through another's receipt.
    """
    resolved = []
    for line in lines:
        target = resolve_transfer_line(actor, line.transfer_line_id)
        if target.transfer_id != receipt.transfer_id:
            raise OutOfScope(f"Transfer line {line.transfer_line_id} does not exist.")
        resolved.append(
            ReceiptLineInput(
                transfer_line=target,
                entered_package_quantity=_optional_decimal(
                    line.entered_package_quantity, field="entered_package_quantity"
                ),
                measured_base_quantity=_optional_decimal(
                    line.measured_base_quantity, field="measured_base_quantity"
                ),
                base_quantity=_optional_decimal(line.base_quantity, field="base_quantity"),
            )
        )
    replace_transfer_receipt_lines(actor=actor, receipt=receipt, lines=resolved)


@router.get("/transfer-receipts/{receipt_id}/", response=TransferReceiptOut, summary="One receipt")
def read_receipt(request: HttpRequest, receipt_id: int) -> Any:
    actor = _actor(request)
    receipt = resolve_receipt(actor, receipt_id)
    return _serialize_receipt(receipt, with_cost=may_see_cost(actor), with_lines=True)


@router.patch(
    "/transfer-receipts/{receipt_id}/",
    response=TransferReceiptOut,
    summary="Amend a draft receipt",
)
def patch_receipt(request: HttpRequest, receipt_id: int, payload: TransferReceiptPatch) -> Any:
    actor = _actor(request)
    receipt = resolve_receipt(actor, receipt_id)
    update_transfer_receipt(
        actor=actor,
        receipt=receipt,
        effective_at=payload.effective_at,
        evidence_reference=payload.evidence_reference,
        narration=payload.narration,
    )
    if payload.lines is not None:
        _replace_receipt_lines(actor, receipt, payload.lines)
    receipt = resolve_receipt(actor, receipt_id)
    return _serialize_receipt(receipt, with_cost=may_see_cost(actor), with_lines=True)


@router.delete(
    "/transfer-receipts/{receipt_id}/", response={204: None}, summary="Delete a draft receipt"
)
def delete_receipt_endpoint(request: HttpRequest, receipt_id: int) -> Status[None]:
    actor = _actor(request)
    receipt = resolve_receipt(actor, receipt_id)
    delete_transfer_receipt(actor=actor, receipt=receipt)
    return Status(204, None)


@router.post(
    "/transfer-receipts/{receipt_id}/post/",
    response=TransferReceiptOut,
    summary="Post the receipt to both ledgers",
)
def post_receipt_endpoint(request: HttpRequest, receipt_id: int) -> Any:
    actor = _actor(request)
    receipt = resolve_receipt(actor, receipt_id)
    posted = post_transfer_receipt(actor=actor, receipt=receipt)
    return _serialize_receipt(posted, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/transfer-receipts/{receipt_id}/reverse/",
    response=TransferReceiptOut,
    summary="Reverse one arrival",
)
def reverse_receipt_endpoint(request: HttpRequest, receipt_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    receipt = resolve_receipt(actor, receipt_id)
    reversed_receipt = reverse_transfer_receipt(actor=actor, receipt=receipt, reason=payload.reason)
    return _serialize_receipt(reversed_receipt, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/transfers/{transfer_id}/shortage/",
    response={201: TransferShortageOut},
    summary="Create a draft shortage closure",
)
def create_shortage_endpoint(
    request: HttpRequest, transfer_id: int, payload: TransferShortageIn
) -> Status[Any]:
    from apps.accounting.models import CostCenter

    actor = _actor(request)
    transfer = resolve_transfer(actor, transfer_id)
    cost_center = CostCenter.objects.filter(
        pk=payload.cost_center_id, organization_id=transfer.organization_id
    ).first()
    if cost_center is None:
        raise ValidationError(
            f"Cost center {payload.cost_center_id} does not exist.", code="unknown_cost_center"
        )
    shortage = create_transfer_shortage(
        actor=actor,
        transfer=transfer,
        effective_at=payload.effective_at,
        reason=payload.reason,
        evidence_reference=payload.evidence_reference,
        cost_center=cost_center,
    )
    shortage = resolve_shortage(actor, shortage.pk, transfer=transfer)
    return Status(201, _serialize_shortage(shortage, with_cost=may_see_cost(actor)))


@router.get(
    "/transfer-shortages/{shortage_id}/",
    response=TransferShortageOut,
    summary="One shortage closure",
)
def read_shortage(request: HttpRequest, shortage_id: int) -> Any:
    actor = _actor(request)
    shortage = resolve_shortage(actor, shortage_id)
    return _serialize_shortage(shortage, with_cost=may_see_cost(actor))


@router.post(
    "/transfer-shortages/{shortage_id}/post/",
    response=TransferShortageOut,
    summary="Write off everything still in transit",
)
def post_shortage_endpoint(request: HttpRequest, shortage_id: int) -> Any:
    actor = _actor(request)
    shortage = resolve_shortage(actor, shortage_id)
    posted = post_transfer_shortage(actor=actor, shortage=shortage)
    return _serialize_shortage(posted, with_cost=may_see_cost(actor))


@router.post(
    "/transfer-shortages/{shortage_id}/reverse/",
    response=TransferShortageOut,
    summary="Reverse a shortage closure",
)
def reverse_shortage_endpoint(request: HttpRequest, shortage_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    shortage = resolve_shortage(actor, shortage_id)
    reversed_shortage = reverse_transfer_shortage(
        actor=actor, shortage=shortage, reason=payload.reason
    )
    return _serialize_shortage(reversed_shortage, with_cost=may_see_cost(actor))


@router.get("/in-transit/", response=list[InTransitOut], summary="Goods standing in transit")
def in_transit_report(request: HttpRequest) -> Any:
    actor = _actor(request)
    with_cost = may_see_cost(actor)
    rows = []
    for line in visible_in_transit(actor):
        row: dict[str, Any] = {
            "transfer_id": line.transfer_id,
            "transfer_number": line.transfer.transfer_number,
            "source_warehouse_code": line.transfer.source_warehouse.code,
            "destination_warehouse_code": line.transfer.destination_warehouse.code,
            "item_code": line.item.code,
            "item_name_ar": line.item.name_ar,
            "base_unit_code": line.item.base_unit.code,
            "lot_code": line.lot.code if line.lot is not None else None,
            "remaining_quantity": f"{line.remaining_quantity:f}",
        }
        if with_cost:
            row["remaining_value"] = f"{line.remaining_value:f}"
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Reason codes, counts and adjustments (Task 1.6 §AD)
# ---------------------------------------------------------------------------
#
# Command-oriented, like everything above: a count is started through
# `/start/` and approved through `/approve/`, never by PATCHing a status.
#
# The blind-count endpoint is the one to read carefully. It returns a payload
# that has never contained the book quantity — not filtered out at the end, but
# never fetched — because a field that is fetched can be leaked by the next
# person who adds a line to a serializer.


def _lot_of(item: Any, lot_id: int | None) -> Any:
    """A lot resolved **through its item**, so a foreign id is a 404 here."""
    from apps.inventory.models import InventoryLot

    if lot_id is None:
        return None
    lot = InventoryLot.objects.filter(pk=lot_id, item=item).first()
    if lot is None:
        raise ValidationError(f"Lot {lot_id} does not exist.", code="unknown_lot")
    return lot


def _conversion_of(item: Any, conversion_id: int | None) -> Any:
    """A package conversion resolved through its item, for the same reason."""
    from apps.inventory.models import ItemPackageConversion

    if conversion_id is None:
        return None
    conversion = ItemPackageConversion.objects.filter(pk=conversion_id, item=item).first()
    if conversion is None:
        raise ValidationError(
            f"Conversion {conversion_id} does not exist.", code="unknown_conversion"
        )
    return conversion


class ReasonCodeIn(Schema):
    organization_id: int
    code: str
    name_ar: str
    applies_to: str
    name_en: str = ""
    requires_comment: bool = False
    requires_evidence: bool = False


class ReasonCodePatch(Schema):
    name_ar: str
    name_en: str = ""
    requires_comment: bool | None = None
    requires_evidence: bool | None = None
    is_active: bool | None = None


class ReasonCodeOut(Schema):
    id: int
    organization_id: int
    code: str
    name_ar: str
    name_en: str
    applies_to: str
    requires_comment: bool
    requires_evidence: bool
    is_active: bool


def _serialize_reason_code(code: Any) -> dict[str, Any]:
    return {
        "id": code.pk,
        "organization_id": code.organization_id,
        "code": code.code,
        "name_ar": code.name_ar,
        "name_en": code.name_en,
        "applies_to": code.applies_to,
        "requires_comment": code.requires_comment,
        "requires_evidence": code.requires_evidence,
        "is_active": code.is_active,
    }


@router.get("/reason-codes/", response=list[ReasonCodeOut], summary="Reason codes in scope")
def list_reason_codes(request: HttpRequest, applies_to: str | None = None) -> Any:
    actor = _actor(request)
    return [
        _serialize_reason_code(code) for code in visible_reason_codes(actor, applies_to=applies_to)
    ]


@router.post("/reason-codes/", response={201: ReasonCodeOut}, summary="Add a reason code")
def create_reason_code_endpoint(request: HttpRequest, payload: ReasonCodeIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    code = create_reason_code(
        actor=actor,
        organization=organization,
        code=payload.code,
        name_ar=payload.name_ar,
        applies_to=payload.applies_to,
        name_en=payload.name_en,
        requires_comment=payload.requires_comment,
        requires_evidence=payload.requires_evidence,
    )
    return Status(201, _serialize_reason_code(code))


@router.patch(
    "/reason-codes/{reason_code_id}/",
    response=ReasonCodeOut,
    summary="Rename or archive a reason code",
)
def patch_reason_code(request: HttpRequest, reason_code_id: int, payload: ReasonCodePatch) -> Any:
    actor = _actor(request)
    code = resolve_reason_code(actor, reason_code_id)
    updated = update_reason_code(
        actor=actor,
        reason_code=code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        requires_comment=payload.requires_comment,
        requires_evidence=payload.requires_evidence,
        is_active=payload.is_active,
    )
    return _serialize_reason_code(updated)


class StockCountIn(Schema):
    organization_id: int
    branch_id: int
    warehouse_id: int
    reference: str = ""
    reason: str = ""
    cost_center_id: int | None = None


class StockCountPatch(Schema):
    reference: str = ""
    reason: str = ""
    cost_center_id: int | None = None


class CountStartIn(Schema):
    effective_at: datetime.datetime | None = None


class BlindLineOut(Schema):
    """
    What the conductor sees. Every field here is one they must see to count.

    There is deliberately no `book_quantity`, no variance and no cost — and no
    optional variant of this schema that could grow one.
    """

    id: int
    sequence: int
    item_code: str
    item_name_ar: str
    base_unit_code: str
    lot_code: str | None
    tracks_lots: bool
    is_unexpected: bool
    counted_quantity: str | None
    line_note: str


class CountEntryIn(Schema):
    line_id: int
    base_quantity: str | None = None
    package_conversion_id: int | None = None
    entered_package_quantity: str | None = None
    measured_base_quantity: str | None = None
    note: str = ""


class CountEntriesIn(Schema):
    entries: list[CountEntryIn]


class UnexpectedLineIn(Schema):
    item_id: int
    lot_id: int | None = None
    base_quantity: str | None = None
    package_conversion_id: int | None = None
    entered_package_quantity: str | None = None
    measured_base_quantity: str | None = None
    note: str = ""


class ApprovedCostIn(Schema):
    line_id: int
    unit_cost: str
    zero_confirmed: bool = False


class CountApproveIn(Schema):
    costs: list[ApprovedCostIn] = []


class CountLineOut(Schema):
    """The reviewed sheet: book, counted and variance, for an approver."""

    id: int
    sequence: int
    item_code: str
    item_name_ar: str
    base_unit_code: str
    lot_code: str | None
    is_unexpected: bool
    book_quantity: str
    counted_quantity: str | None
    variance_quantity: str | None
    movement_id: int | None
    book_value: str | None = None
    book_average: str | None = None
    approved_unit_cost: str | None = None
    zero_cost_confirmed: bool | None = None
    variance_value: str | None = None


class StockCountOut(Schema):
    id: int
    public_id: str
    count_number: str
    status: str
    scope_type: str
    organization_id: int
    branch_id: int
    branch_code: str
    warehouse_id: int
    warehouse_code: str
    cutoff_at: datetime.datetime | None
    business_date: datetime.date | None
    reference: str
    reason: str
    cost_center_code: str | None
    conducted_by: str | None
    submitted_by: str | None
    approved_by: str | None
    cancelled_by: str | None
    cancellation_reason: str
    reversal_reason: str
    stock_entry_id: int | None
    journal_entry_number: str | None
    line_count: int
    lines: list[CountLineOut] = []


def _serialize_count_line(line: Any, *, with_cost: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": line.pk,
        "sequence": line.sequence,
        "item_code": line.item.code,
        "item_name_ar": line.item.name_ar,
        "base_unit_code": line.base_unit.code,
        "lot_code": line.lot.code if line.lot_id else None,
        "is_unexpected": line.is_unexpected,
        "book_quantity": f"{line.book_quantity:f}",
        "counted_quantity": (
            f"{line.counted_quantity:f}" if line.counted_quantity is not None else None
        ),
        "variance_quantity": (
            f"{line.variance_quantity:f}" if line.variance_quantity is not None else None
        ),
        "movement_id": line.movement_id,
    }
    if with_cost:
        payload["book_value"] = f"{line.book_value:f}"
        payload["book_average"] = f"{line.book_average:f}"
        payload["approved_unit_cost"] = (
            f"{line.approved_unit_cost:f}" if line.approved_unit_cost is not None else None
        )
        payload["zero_cost_confirmed"] = line.zero_cost_confirmed
        payload["variance_value"] = (
            f"{line.variance_value:f}" if line.variance_value is not None else None
        )
    return payload


def _serialize_count(count: Any, *, with_cost: bool, with_lines: bool) -> dict[str, Any]:
    lines = (
        list(count.lines.select_related("item", "base_unit", "lot").order_by("sequence"))
        if with_lines
        else []
    )
    return {
        "id": count.pk,
        "public_id": str(count.public_id),
        "count_number": count.count_number,
        "status": count.status,
        "scope_type": count.scope_type,
        "organization_id": count.organization_id,
        "branch_id": count.branch_id,
        "branch_code": count.branch.code,
        "warehouse_id": count.warehouse_id,
        "warehouse_code": count.warehouse.code,
        "cutoff_at": count.cutoff_at,
        "business_date": count.business_date,
        "reference": count.reference,
        "reason": count.reason,
        "cost_center_code": count.cost_center.code if count.cost_center_id else None,
        "conducted_by": str(count.conducted_by) if count.conducted_by_id else None,
        "submitted_by": str(count.submitted_by) if count.submitted_by_id else None,
        "approved_by": str(count.approved_by) if count.approved_by_id else None,
        "cancelled_by": str(count.cancelled_by) if count.cancelled_by_id else None,
        "cancellation_reason": count.cancellation_reason,
        "reversal_reason": count.reversal_reason,
        "stock_entry_id": count.stock_entry_id,
        "journal_entry_number": (
            count.journal_entry.entry_number if count.journal_entry_id else None
        ),
        "line_count": count.lines.count() if not with_lines else len(lines),
        "lines": [_serialize_count_line(line, with_cost=with_cost) for line in lines],
    }


@router.get(
    "/counts/", response=list[StockCountOut], summary="Stock counts in scope", exclude_unset=True
)
def list_counts(request: HttpRequest, status: str | None = None) -> Any:
    actor = _actor(request)
    counts_queryset = visible_counts(actor)
    if status is not None:
        counts_queryset = counts_queryset.filter(status=status)
    return [
        _serialize_count(count, with_cost=may_see_cost(actor), with_lines=False)
        for count in counts_queryset
    ]


@router.post(
    "/counts/", response={201: StockCountOut}, summary="Prepare a count", exclude_unset=True
)
def create_count_endpoint(request: HttpRequest, payload: StockCountIn) -> Status[Any]:
    from apps.accounting.models import CostCenter

    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    branch = resolve_branch(actor, payload.branch_id)
    warehouse = resolve_warehouse(actor, payload.warehouse_id)
    cost_center = None
    if payload.cost_center_id is not None:
        cost_center = CostCenter.objects.filter(
            pk=payload.cost_center_id, organization=organization
        ).first()
        if cost_center is None:
            raise OutOfScope(f"Cost center {payload.cost_center_id} does not exist.")
    count = create_stock_count(
        actor=actor,
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        reference=payload.reference,
        reason=payload.reason,
        cost_center=cost_center,
    )
    return Status(201, _serialize_count(count, with_cost=may_see_cost(actor), with_lines=True))


@router.get("/counts/{count_id}/", response=StockCountOut, summary="One count", exclude_unset=True)
def read_count(request: HttpRequest, count_id: int) -> Any:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    return _serialize_count(count, with_cost=may_see_cost(actor), with_lines=True)


@router.patch(
    "/counts/{count_id}/", response=StockCountOut, summary="Amend a draft count", exclude_unset=True
)
def patch_count(request: HttpRequest, count_id: int, payload: StockCountPatch) -> Any:
    from apps.accounting.models import CostCenter

    actor = _actor(request)
    count = resolve_count(actor, count_id)
    cost_center = None
    if payload.cost_center_id is not None:
        cost_center = CostCenter.objects.filter(
            pk=payload.cost_center_id, organization=count.organization
        ).first()
    updated = update_stock_count(
        actor=actor,
        count=count,
        reference=payload.reference,
        reason=payload.reason,
        cost_center=cost_center,
    )
    return _serialize_count(updated, with_cost=may_see_cost(actor), with_lines=True)


@router.delete("/counts/{count_id}/", response={204: None}, summary="Discard a draft count")
def delete_count_endpoint(request: HttpRequest, count_id: int) -> Status[None]:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    delete_stock_count(actor=actor, count=count)
    return Status(204, None)


@router.post(
    "/counts/{count_id}/start/",
    response=StockCountOut,
    summary="Freeze the warehouse and snapshot the book",
    exclude_unset=True,
)
def start_count_endpoint(request: HttpRequest, count_id: int, payload: CountStartIn) -> Any:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    started = start_stock_count(actor=actor, count=count, effective_at=payload.effective_at)
    return _serialize_count(started, with_cost=may_see_cost(actor), with_lines=True)


@router.get(
    "/counts/{count_id}/sheet/",
    response=list[BlindLineOut],
    summary="The blind counting sheet",
)
def blind_sheet_endpoint(request: HttpRequest, count_id: int) -> Any:
    """
    Deliberately blind, **whatever the caller may otherwise see**.

    A manager with `view_valuation` gets exactly the same payload as a
    storekeeper. The control is over what the person counting knows at the
    moment they count, not over what they are entitled to look up afterwards
    (§K).
    """
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    return [
        {
            "id": row["id"],
            "sequence": row["sequence"],
            "item_code": row["item__code"],
            "item_name_ar": row["item__name_ar"],
            "base_unit_code": row["base_unit__code"],
            "lot_code": row["lot__code"],
            "tracks_lots": row["item__tracks_lots"],
            "is_unexpected": row["is_unexpected"],
            "counted_quantity": (
                f"{row['counted_quantity']:f}" if row["counted_quantity"] is not None else None
            ),
            "line_note": row["line_note"],
        }
        for row in blind_count_sheet(actor=actor, count=count)
    ]


@router.patch(
    "/counts/{count_id}/lines/",
    response=list[BlindLineOut],
    summary="Record counted quantities",
)
def record_counts_endpoint(request: HttpRequest, count_id: int, payload: CountEntriesIn) -> Any:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    entries = []
    for entry in payload.entries:
        # Constrained to this count, so a line id from another one is a 404
        # rather than a write through the wrong document. Resolved once and
        # reused: a full sheet is many lines, and this is the hot path.
        line = resolve_count_line(actor, entry.line_id, count=count)
        entries.append(
            CountEntry(
                line=line,
                base_quantity=_optional_decimal(entry.base_quantity, field="base_quantity"),
                package_conversion=_conversion_of(line.item, entry.package_conversion_id),
                entered_package_quantity=_optional_decimal(
                    entry.entered_package_quantity, field="entered_package_quantity"
                ),
                measured_base_quantity=_optional_decimal(
                    entry.measured_base_quantity, field="measured_base_quantity"
                ),
                note=entry.note,
            )
        )
    record_stock_counts(actor=actor, count=count, entries=entries)
    return blind_sheet_endpoint(request, count_id)


@router.post(
    "/counts/{count_id}/unexpected/",
    response={201: BlindLineOut},
    summary="Record stock the books do not have",
)
def add_unexpected_endpoint(
    request: HttpRequest, count_id: int, payload: UnexpectedLineIn
) -> Status[Any]:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    item = resolve_item(actor, payload.item_id)
    line = add_unexpected_count_line(
        actor=actor,
        count=count,
        item=item,
        lot=_lot_of(item, payload.lot_id),
        base_quantity=_optional_decimal(payload.base_quantity, field="base_quantity"),
        package_conversion=_conversion_of(item, payload.package_conversion_id),
        entered_package_quantity=_optional_decimal(
            payload.entered_package_quantity, field="entered_package_quantity"
        ),
        measured_base_quantity=_optional_decimal(
            payload.measured_base_quantity, field="measured_base_quantity"
        ),
        note=payload.note,
    )
    return Status(
        201,
        {
            "id": line.pk,
            "sequence": line.sequence,
            "item_code": line.item.code,
            "item_name_ar": line.item.name_ar,
            "base_unit_code": line.base_unit.code,
            "lot_code": line.lot.code if line.lot_id else None,
            "tracks_lots": line.item.tracks_lots,
            "is_unexpected": True,
            "counted_quantity": (
                f"{line.counted_quantity:f}" if line.counted_quantity is not None else None
            ),
            "line_note": line.line_note,
        },
    )


@router.post(
    "/counts/{count_id}/submit/",
    response=StockCountOut,
    summary="Submit the count for approval",
    exclude_unset=True,
)
def submit_count_endpoint(request: HttpRequest, count_id: int) -> Any:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    submitted = submit_stock_count(actor=actor, count=count)
    return _serialize_count(submitted, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/counts/{count_id}/approve/",
    response=StockCountOut,
    summary="Approve, post the variance, and release the warehouse",
    exclude_unset=True,
)
def approve_count_endpoint(request: HttpRequest, count_id: int, payload: CountApproveIn) -> Any:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    costs = [
        ApprovedCost(
            line=resolve_count_line(actor, supplied.line_id, count=count),
            unit_cost=_decimal(supplied.unit_cost, field="unit_cost"),
            zero_confirmed=supplied.zero_confirmed,
        )
        for supplied in payload.costs
    ]
    posted = approve_stock_count(actor=actor, count=count, costs=costs)
    return _serialize_count(posted, with_cost=may_see_cost(actor), with_lines=True)


@router.post("/counts/{count_id}/cancel/", response=StockCountOut, summary="Cancel a count")
def cancel_count_endpoint(request: HttpRequest, count_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    cancelled = cancel_stock_count(actor=actor, count=count, reason=payload.reason)
    return _serialize_count(cancelled, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/counts/{count_id}/reverse/",
    response=StockCountOut,
    summary="Reverse a posted count",
    exclude_unset=True,
)
def reverse_count_endpoint(request: HttpRequest, count_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    count = resolve_count(actor, count_id)
    reversed_count = reverse_stock_count(actor=actor, count=count, reason=payload.reason)
    return _serialize_count(reversed_count, with_cost=may_see_cost(actor), with_lines=True)


# --- Manual adjustments -----------------------------------------------------


class AdjustmentLineIn(Schema):
    kind: str
    item_id: int
    reason_code_id: int
    lot_id: int | None = None
    package_conversion_id: int | None = None
    entered_package_quantity: str | None = None
    measured_base_quantity: str | None = None
    base_quantity: str | None = None
    unit_cost: str | None = None
    zero_cost_confirmed: bool = False
    value_adjustment: str | None = None
    line_comment: str = ""


class AdjustmentLineOut(Schema):
    id: int
    sequence: int
    kind: str
    item_code: str
    item_name_ar: str
    base_unit_code: str
    lot_code: str | None
    base_quantity: str
    reason_code: str
    line_comment: str
    movement_id: int | None
    unit_cost: str | None = None
    value_adjustment: str | None = None
    total_value: str | None = None
    control_account_code: str | None = None


class AdjustmentIn(Schema):
    organization_id: int
    branch_id: int
    warehouse_id: int
    effective_at: datetime.datetime
    evidence_reference: str
    reason: str
    cost_center_id: int | None = None
    lines: list[AdjustmentLineIn] = []


class AdjustmentPatch(Schema):
    effective_at: datetime.datetime | None = None
    evidence_reference: str | None = None
    reason: str | None = None
    cost_center_id: int | None = None


class AdjustmentOut(Schema):
    id: int
    public_id: str
    document_number: str
    status: str
    organization_id: int
    branch_id: int
    branch_code: str
    warehouse_id: int
    warehouse_code: str
    effective_at: datetime.datetime
    business_date: datetime.date
    evidence_reference: str
    reason: str
    cost_center_code: str | None
    created_by: str | None
    posted_by: str | None
    reversed_by: str | None
    reversal_reason: str
    stock_entry_id: int | None
    journal_entry_number: str | None
    line_count: int
    lines: list[AdjustmentLineOut] = []


def _serialize_adjustment_line(line: Any, *, with_cost: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": line.pk,
        "sequence": line.sequence,
        "kind": line.kind,
        "item_code": line.item.code,
        "item_name_ar": line.item.name_ar,
        "base_unit_code": line.item.base_unit.code,
        "lot_code": line.lot.code if line.lot_id else None,
        "base_quantity": f"{line.base_quantity:f}",
        "reason_code": line.reason_code.code,
        "line_comment": line.line_comment,
        "movement_id": line.movement_id,
    }
    if with_cost:
        payload["unit_cost"] = f"{line.unit_cost:f}" if line.unit_cost is not None else None
        payload["value_adjustment"] = (
            f"{line.value_adjustment:f}" if line.value_adjustment is not None else None
        )
        payload["total_value"] = f"{line.total_value:f}" if line.total_value is not None else None
        payload["control_account_code"] = (
            line.control_account.code if line.control_account_id else None
        )
    return payload


def _serialize_adjustment(document: Any, *, with_cost: bool, with_lines: bool) -> dict[str, Any]:
    lines = (
        list(
            document.lines.select_related(
                "item", "item__base_unit", "lot", "reason_code", "control_account"
            ).order_by("sequence")
        )
        if with_lines
        else []
    )
    return {
        "id": document.pk,
        "public_id": str(document.public_id),
        "document_number": document.document_number,
        "status": document.status,
        "organization_id": document.organization_id,
        "branch_id": document.branch_id,
        "branch_code": document.branch.code,
        "warehouse_id": document.warehouse_id,
        "warehouse_code": document.warehouse.code,
        "effective_at": document.effective_at,
        "business_date": document.business_date,
        "evidence_reference": document.evidence_reference,
        "reason": document.reason,
        "cost_center_code": document.cost_center.code if document.cost_center_id else None,
        "created_by": str(document.created_by) if document.created_by_id else None,
        "posted_by": str(document.posted_by) if document.posted_by_id else None,
        "reversed_by": str(document.reversed_by) if document.reversed_by_id else None,
        "reversal_reason": document.reversal_reason,
        "stock_entry_id": document.stock_entry_id,
        "journal_entry_number": (
            document.journal_entry.entry_number if document.journal_entry_id else None
        ),
        "line_count": document.lines.count() if not with_lines else len(lines),
        "lines": [_serialize_adjustment_line(line, with_cost=with_cost) for line in lines],
    }


def _adjustment_line_input(actor: Any, payload: AdjustmentLineIn) -> AdjustmentLineInput:
    item = resolve_item(actor, payload.item_id)
    return AdjustmentLineInput(
        kind=payload.kind,
        item=item,
        reason_code=resolve_reason_code(actor, payload.reason_code_id),
        lot=_lot_of(item, payload.lot_id),
        package_conversion=_conversion_of(item, payload.package_conversion_id),
        entered_package_quantity=_optional_decimal(
            payload.entered_package_quantity, field="entered_package_quantity"
        ),
        measured_base_quantity=_optional_decimal(
            payload.measured_base_quantity, field="measured_base_quantity"
        ),
        base_quantity=_optional_decimal(payload.base_quantity, field="base_quantity"),
        unit_cost=_optional_decimal(payload.unit_cost, field="unit_cost"),
        zero_cost_confirmed=payload.zero_cost_confirmed,
        value_adjustment=_optional_decimal(payload.value_adjustment, field="value_adjustment"),
        line_comment=payload.line_comment,
    )


@router.get(
    "/adjustments/",
    response=list[AdjustmentOut],
    summary="Adjustments in scope",
    exclude_unset=True,
)
def list_adjustments(request: HttpRequest, status: str | None = None) -> Any:
    actor = _actor(request)
    documents = visible_adjustments(actor)
    if status is not None:
        documents = documents.filter(status=status)
    return [
        _serialize_adjustment(document, with_cost=may_see_cost(actor), with_lines=False)
        for document in documents
    ]


@router.post(
    "/adjustments/",
    response={201: AdjustmentOut},
    summary="Create a draft adjustment",
    exclude_unset=True,
)
def create_adjustment_endpoint(request: HttpRequest, payload: AdjustmentIn) -> Status[Any]:
    from apps.accounting.models import CostCenter

    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    branch = resolve_branch(actor, payload.branch_id)
    warehouse = resolve_warehouse(actor, payload.warehouse_id)
    cost_center = None
    if payload.cost_center_id is not None:
        cost_center = CostCenter.objects.filter(
            pk=payload.cost_center_id, organization=organization
        ).first()
        if cost_center is None:
            raise OutOfScope(f"Cost center {payload.cost_center_id} does not exist.")

    document = create_adjustment(
        actor=actor,
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        effective_at=payload.effective_at,
        evidence_reference=payload.evidence_reference,
        reason=payload.reason,
        cost_center=cost_center,
    )
    for line in payload.lines:
        add_adjustment_line(
            actor=actor, document=document, line=_adjustment_line_input(actor, line)
        )
    return Status(
        201, _serialize_adjustment(document, with_cost=may_see_cost(actor), with_lines=True)
    )


@router.get(
    "/adjustments/{document_id}/",
    response=AdjustmentOut,
    summary="One adjustment",
    exclude_unset=True,
)
def read_adjustment(request: HttpRequest, document_id: int) -> Any:
    actor = _actor(request)
    document = resolve_adjustment(actor, document_id)
    return _serialize_adjustment(document, with_cost=may_see_cost(actor), with_lines=True)


@router.patch(
    "/adjustments/{document_id}/",
    response=AdjustmentOut,
    summary="Amend a draft adjustment",
    exclude_unset=True,
)
def patch_adjustment(request: HttpRequest, document_id: int, payload: AdjustmentPatch) -> Any:
    from apps.accounting.models import CostCenter

    actor = _actor(request)
    document = resolve_adjustment(actor, document_id)
    cost_center = None
    if payload.cost_center_id is not None:
        cost_center = CostCenter.objects.filter(
            pk=payload.cost_center_id, organization=document.organization
        ).first()
    updated = update_adjustment(
        actor=actor,
        document=document,
        effective_at=payload.effective_at,
        evidence_reference=payload.evidence_reference,
        reason=payload.reason,
        cost_center=cost_center,
    )
    return _serialize_adjustment(updated, with_cost=may_see_cost(actor), with_lines=True)


@router.delete(
    "/adjustments/{document_id}/", response={204: None}, summary="Delete a draft adjustment"
)
def delete_adjustment_endpoint(request: HttpRequest, document_id: int) -> Status[None]:
    actor = _actor(request)
    document = resolve_adjustment(actor, document_id)
    delete_adjustment(actor=actor, document=document)
    return Status(204, None)


@router.post(
    "/adjustments/{document_id}/lines/",
    response={201: AdjustmentLineOut},
    exclude_unset=True,
    summary="Add a line to a draft adjustment",
)
def add_adjustment_line_endpoint(
    request: HttpRequest, document_id: int, payload: AdjustmentLineIn
) -> Status[Any]:
    actor = _actor(request)
    document = resolve_adjustment(actor, document_id)
    line = add_adjustment_line(
        actor=actor, document=document, line=_adjustment_line_input(actor, payload)
    )
    return Status(201, _serialize_adjustment_line(line, with_cost=may_see_cost(actor)))


@router.post(
    "/adjustments/{document_id}/post/",
    response=AdjustmentOut,
    summary="Post the adjustment to both ledgers",
    exclude_unset=True,
)
def post_adjustment_endpoint(request: HttpRequest, document_id: int) -> Any:
    actor = _actor(request)
    document = resolve_adjustment(actor, document_id)
    posted = post_adjustment(actor=actor, document=document)
    return _serialize_adjustment(posted, with_cost=may_see_cost(actor), with_lines=True)


@router.post(
    "/adjustments/{document_id}/reverse/",
    response=AdjustmentOut,
    summary="Reverse a posted adjustment",
    exclude_unset=True,
)
def reverse_adjustment_endpoint(request: HttpRequest, document_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    document = resolve_adjustment(actor, document_id)
    reversed_document = reverse_adjustment(actor=actor, document=document, reason=payload.reason)
    return _serialize_adjustment(reversed_document, with_cost=may_see_cost(actor), with_lines=True)


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
