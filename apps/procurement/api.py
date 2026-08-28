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
from django.http import HttpRequest, JsonResponse
from ninja import Router, Schema, Status

from apps.accounting.models import Account, CostCenter
from apps.inventory.models import InventoryItem, InventoryLot, InventoryReasonCode, StockLocation
from apps.inventory.selectors import resolve_item, resolve_package_unit
from apps.organizations.authorization import (
    OutOfScope,
    PermissionMissing,
    accessible_warehouses,
    require_organization_permission,
    require_reachable_organization_permission,
    require_warehouse_permission,
    resolve_branch,
    resolve_organization,
)
from apps.organizations.selectors import accessible_branches
from apps.procurement.credit_notes import (
    add_credit_allocation,
    add_return_allocation,
    create_supplier_credit_note,
    delete_supplier_credit_note,
    post_supplier_credit_note,
    remove_credit_allocation,
    remove_return_allocation,
    reverse_supplier_credit_note,
    unallocated_credit,
)
from apps.procurement.credit_terms import (
    activate_credit_term,
    create_credit_term_draft,
    delete_credit_term_draft,
    update_credit_term_draft,
)
from apps.procurement.invoices import (
    add_account_line,
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    delete_supplier_invoice,
    outstanding_amount,
    post_supplier_invoice,
    return_supplier_invoice_to_draft,
    reverse_supplier_invoice,
    update_supplier_invoice,
)
from apps.procurement.matching import (
    add_allocation,
    cancel_purchase_match,
    coverage_for_invoice,
    create_purchase_match,
    delete_purchase_match,
    live_posting_for,
    mark_match_ready,
    remove_allocation,
)
from apps.procurement.models import (
    GoodsReceiptLine,
    PurchaseMatch,
    PurchaseMatchAllocation,
    PurchaseOrderLine,
    Supplier,
    SupplierCreditNote,
    SupplierCreditTerm,
    SupplierCreditTermStatus,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierItem,
    SupplierPayment,
    SupplierReturn,
    SupplierReturnLine,
)
from apps.procurement.payments import (
    add_payment_allocation,
    create_supplier_payment,
    delete_supplier_payment,
    post_supplier_payment,
    remove_payment_allocation,
    reverse_supplier_payment,
)
from apps.procurement.permissions import (
    APPROVE_SUPPLIER_CREDIT_TERM,
    APPROVE_SUPPLIER_INVOICE,
    CANCEL_PURCHASE_MATCH,
    CREATE_SUPPLIER_CREDIT_NOTE,
    CREATE_SUPPLIER_CREDIT_TERM,
    CREATE_SUPPLIER_INVOICE,
    CREATE_SUPPLIER_PAYMENT,
    CREATE_SUPPLIER_RETURN,
    MANAGE_SUPPLIER_ITEMS,
    MANAGE_SUPPLIERS,
    MATCH_SUPPLIER_INVOICE,
    POST_SUPPLIER_CREDIT_NOTE,
    POST_SUPPLIER_INVOICE,
    POST_SUPPLIER_PAYMENT,
    POST_SUPPLIER_RETURN,
    REVERSE_SUPPLIER_CREDIT_NOTE,
    REVERSE_SUPPLIER_INVOICE,
    REVERSE_SUPPLIER_PAYMENT,
    REVERSE_SUPPLIER_RETURN,
    VIEW_PURCHASE_MATCH,
    VIEW_SUPPLIER,
    VIEW_SUPPLIER_COST,
    VIEW_SUPPLIER_CREDIT_NOTE,
    VIEW_SUPPLIER_CREDIT_TERM,
    VIEW_SUPPLIER_INVOICE,
    VIEW_SUPPLIER_ITEM,
    VIEW_SUPPLIER_PAYMENT,
    VIEW_SUPPLIER_RETURN,
)
from apps.procurement.returns import (
    add_return_line,
    create_supplier_return,
    delete_supplier_return,
    post_supplier_return,
    remove_return_line,
    reverse_supplier_return,
)
from apps.procurement.selectors import (
    resolve_credit_allocation,
    resolve_credit_return_allocation,
    resolve_match_allocation,
    resolve_payment_allocation,
    resolve_purchase_match,
    resolve_return_line,
    resolve_supplier,
    resolve_supplier_credit_note,
    resolve_supplier_credit_term,
    resolve_supplier_invoice,
    resolve_supplier_item,
    resolve_supplier_payment,
    resolve_supplier_return,
    visible_goods_receipts,
    visible_purchase_matches,
    visible_purchase_orders,
    visible_supplier_credit_notes,
    visible_supplier_credit_terms,
    visible_supplier_invoices,
    visible_supplier_items,
    visible_supplier_payments,
    visible_supplier_returns,
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
    name: str
    contact_name: str
    phone: str
    email: str
    is_active: bool
    #: Omitted entirely without `view_supplier_cost`, not blanked — the same
    #: rule inventory applies to valuation. A null `credit_limit` would be
    #: indistinguishable from a supplier with no agreed limit, which is a
    #: different statement from "you were not shown it".
    payment_terms_days: int | None = None
    minimum_settlement_percent: str | None = None
    balance_reset_date: datetime.date | None = None
    credit_limit: str | None = None


class SupplierIn(Schema):
    organization_id: int
    code: str
    name: str
    contact_name: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    payment_terms_days: int = 0
    minimum_settlement_percent: str | None = None
    balance_reset_date: datetime.date | None = None
    credit_limit: str | None = None
    notes: str = ""


class SupplierUpdateIn(Schema):
    name: str
    contact_name: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    minimum_settlement_percent: str | None = None
    balance_reset_date: datetime.date | None = None
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
        "name": supplier.name,
        "contact_name": supplier.contact_name,
        "phone": supplier.phone,
        "email": supplier.email,
        "is_active": supplier.is_active,
    }
    if include_cost:
        body["payment_terms_days"] = supplier.payment_terms_days
        body["minimum_settlement_percent"] = (
            format(supplier.minimum_settlement_percent, "f")
            if supplier.minimum_settlement_percent is not None
            else None
        )
        body["balance_reset_date"] = supplier.balance_reset_date
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
        name=payload.name,
        contact_name=payload.contact_name,
        phone=payload.phone,
        email=payload.email,
        address=payload.address,
        payment_terms_days=payload.payment_terms_days,
        minimum_settlement_percent=_money(
            payload.minimum_settlement_percent, field="minimum_settlement_percent"
        ),
        balance_reset_date=payload.balance_reset_date,
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
        name=payload.name,
        contact_name=payload.contact_name,
        phone=payload.phone,
        email=payload.email,
        address=payload.address,
        minimum_settlement_percent=_money(
            payload.minimum_settlement_percent, field="minimum_settlement_percent"
        ),
        balance_reset_date=payload.balance_reset_date,
        credit_limit=_money(payload.credit_limit, field="credit_limit"),
        notes=payload.notes,
        is_active=payload.is_active,
    )
    return _serialize(updated, include_cost=True)


# ---------------------------------------------------------------------------
# Effective-dated supplier credit terms
# ---------------------------------------------------------------------------


class SupplierCreditTermOut(Schema):
    id: int
    public_id: str
    organization_id: int
    supplier_id: int
    supplier_code: str
    version: int
    status: str
    name: str
    net_days: int
    effective_from: str
    effective_to: str | None
    supersedes_id: int | None
    created_by_id: int | None
    approved_by_id: int | None
    approved_at: str | None


class SupplierCreditTermIn(Schema):
    supplier_id: int
    name: str
    net_days: int
    effective_from: str
    effective_to: str | None = None
    notes: str = ""


class SupplierCreditTermUpdateIn(Schema):
    name: str
    net_days: int
    effective_from: str
    effective_to: str | None = None
    notes: str = ""


def _serialize_credit_term(term: SupplierCreditTerm) -> dict[str, Any]:
    return {
        "id": term.pk,
        "public_id": str(term.public_id),
        "organization_id": term.organization_id,
        "supplier_id": term.supplier_id,
        "supplier_code": term.supplier.code,
        "version": term.version,
        "status": term.status,
        "name": term.name,
        "net_days": term.net_days,
        "effective_from": term.effective_from.isoformat(),
        "effective_to": term.effective_to.isoformat() if term.effective_to else None,
        "supersedes_id": term.supersedes_id,
        "created_by_id": term.created_by_id,
        "approved_by_id": term.approved_by_id,
        "approved_at": term.approved_at.isoformat() if term.approved_at else None,
    }


def _require_credit_term_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SUPPLIER_CREDIT_TERM):
        raise PermissionMissing(f"{VIEW_SUPPLIER_CREDIT_TERM} is not held.")
    return actor


@router.get(
    "/supplier-credit-terms/",
    response=list[SupplierCreditTermOut],
    summary="List supplier credit-term versions",
)
def list_supplier_credit_terms(request: HttpRequest, status: str | None = None) -> Any:
    actor = _require_credit_term_view(request)
    queryset = visible_supplier_credit_terms(actor)
    if status:
        queryset = queryset.filter(status=status.strip().upper())
    return [
        _serialize_credit_term(term) for term in queryset.order_by("supplier__code", "-version")
    ]


@router.get(
    "/supplier-credit-terms/{term_id}/",
    response=SupplierCreditTermOut,
    summary="Read one supplier credit-term version",
)
def read_supplier_credit_term(request: HttpRequest, term_id: int) -> Any:
    return _serialize_credit_term(
        resolve_supplier_credit_term(_require_credit_term_view(request), term_id)
    )


@router.post(
    "/supplier-credit-terms/",
    response={201: SupplierCreditTermOut},
    summary="Create a draft supplier credit term",
)
def create_credit_term_api(request: HttpRequest, payload: SupplierCreditTermIn) -> Status[Any]:
    actor = _actor(request)
    supplier = resolve_supplier(actor, payload.supplier_id)
    require_organization_permission(actor, CREATE_SUPPLIER_CREDIT_TERM, supplier.organization)
    current = (
        SupplierCreditTerm.objects.filter(
            supplier=supplier,
            status=SupplierCreditTermStatus.ACTIVE,
        )
        .order_by("-effective_from", "-version")
        .first()
    )
    term = create_credit_term_draft(
        supplier=supplier,
        name=payload.name,
        net_days=payload.net_days,
        effective_from=_required_date(payload.effective_from, field="effective_from"),
        effective_to=_date(payload.effective_to, field="effective_to"),
        notes=payload.notes,
        created_by=actor,
        supersedes=current,
    )
    return Status(201, _serialize_credit_term(term))


@router.put(
    "/supplier-credit-terms/{term_id}/",
    response=SupplierCreditTermOut,
    summary="Edit a draft supplier credit term",
)
def update_credit_term_api(
    request: HttpRequest, term_id: int, payload: SupplierCreditTermUpdateIn
) -> Any:
    actor = _actor(request)
    term = resolve_supplier_credit_term(actor, term_id)
    require_organization_permission(actor, CREATE_SUPPLIER_CREDIT_TERM, term.organization)
    updated = update_credit_term_draft(
        term=term,
        name=payload.name,
        net_days=payload.net_days,
        effective_from=_required_date(payload.effective_from, field="effective_from"),
        effective_to=_date(payload.effective_to, field="effective_to"),
        notes=payload.notes,
    )
    return _serialize_credit_term(updated)


@router.delete(
    "/supplier-credit-terms/{term_id}/",
    response={204: None},
    summary="Discard a draft supplier credit term",
)
def delete_credit_term_api(request: HttpRequest, term_id: int) -> Status[Any]:
    actor = _actor(request)
    term = resolve_supplier_credit_term(actor, term_id)
    require_organization_permission(actor, CREATE_SUPPLIER_CREDIT_TERM, term.organization)
    delete_credit_term_draft(term=term)
    return Status(204, None)


@router.post(
    "/supplier-credit-terms/{term_id}/activate/",
    response=SupplierCreditTermOut,
    summary="Activate a supplier credit-term version",
)
def activate_credit_term_api(request: HttpRequest, term_id: int) -> Any:
    actor = _actor(request)
    term = resolve_supplier_credit_term(actor, term_id)
    require_organization_permission(actor, APPROVE_SUPPLIER_CREDIT_TERM, term.organization)
    return _serialize_credit_term(activate_credit_term(term=term, actor=actor))


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
    item_name: str
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
        "item_name": row.item.name,
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


# ---------------------------------------------------------------------------
# Supplier invoices (Task 2.10)
# ---------------------------------------------------------------------------
#
# Commands, not CRUD (PRC-063). There is no writable endpoint over a posted
# invoice: `approve`, `post` and `reverse` are the only ways its state changes,
# and each one calls the same service the screen does. Every money value
# crosses the wire as an exact string in both directions.


class InvoiceLineOut(Schema):
    id: int
    sequence: int
    line_type: str
    description: str
    target: str
    item_id: int | None = None
    account_code: str | None = None
    cost_center_code: str | None = None
    receipt_line_id: int | None = None
    order_line_id: int | None = None
    quantity: str
    unit_price: str | None = None
    line_amount: str | None = None
    allocated_freight: str | None = None
    allocated_discount: str | None = None
    net_amount: str | None = None


class SupplierInvoiceOut(Schema):
    id: int
    public_id: str
    organization_id: int
    branch_id: int
    supplier_id: int
    supplier_code: str
    number: str
    supplier_invoice_number: str
    supplier_reference: str
    currency_code: str
    invoice_date: str
    business_date: str
    due_date: str
    payment_terms_days: int
    credit_term_public_id: str | None
    credit_term_version: int | None
    credit_term_name: str
    credit_term_net_days: int
    status: str
    matching_status: str
    created_by: str
    approved_by: str | None
    posted_by: str | None
    reversed_by: str | None
    approved_at: str | None
    posted_at: str | None
    reversed_at: str | None
    reversal_reason: str
    is_ready_to_post: bool
    blocking_line_sequences: list[int]
    journal_entry: str | None = None
    #: Omitted entirely without `view_supplier_cost`, never blanked — a null
    #: total is indistinguishable from an invoice for nothing, which is a
    #: different statement from "you were not shown it".
    lines_total: str | None = None
    freight_amount: str | None = None
    discount_amount: str | None = None
    total_amount: str | None = None
    posted_amount: str | None = None
    outstanding: str | None = None
    lines: list[InvoiceLineOut] = []


class SupplierInvoiceIn(Schema):
    branch_id: int
    supplier_id: int
    supplier_invoice_number: str
    invoice_date: str
    business_date: str | None = None
    supplier_reference: str = ""
    currency_code: str = "IQD"
    freight_amount: str | None = None
    discount_amount: str | None = None
    notes: str = ""


class SupplierInvoiceUpdateIn(Schema):
    supplier_invoice_number: str | None = None
    invoice_date: str | None = None
    business_date: str | None = None
    supplier_reference: str | None = None
    currency_code: str | None = None
    freight_amount: str | None = None
    discount_amount: str | None = None
    notes: str | None = None


class InvoiceInventoryLineIn(Schema):
    item_id: int
    base_quantity: str
    unit_price: str
    receipt_line_id: int | None = None
    order_line_id: int | None = None
    description: str = ""
    note: str = ""


class InvoiceAccountLineIn(Schema):
    account_id: int
    description: str
    quantity: str = "1.000"
    unit_price: str
    cost_center_id: int | None = None
    note: str = ""


class ReasonIn(Schema):
    reason: str


def _require_invoice_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SUPPLIER_INVOICE):
        raise PermissionMissing(f"{VIEW_SUPPLIER_INVOICE} is not held.")
    return actor


def _serialize_invoice_line(line: SupplierInvoiceLine, *, include_cost: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": line.pk,
        "sequence": line.sequence,
        "line_type": line.line_type,
        "description": line.description,
        "target": line.target_label,
        "item_id": line.item_id,
        "account_code": line.account.code if line.account else None,
        "cost_center_code": line.cost_center.code if line.cost_center else None,
        "receipt_line_id": line.receipt_line_id,
        "order_line_id": line.order_line_id,
        "quantity": format(line.quantity, "f"),
    }
    if include_cost:
        payload.update(
            {
                "unit_price": format(line.unit_price, "f"),
                "line_amount": format(line.line_amount, "f"),
                "allocated_freight": format(line.allocated_freight, "f"),
                "allocated_discount": format(line.allocated_discount, "f"),
                "net_amount": format(line.net_amount, "f"),
            }
        )
    return payload


def _serialize_invoice(invoice: SupplierInvoice, *, include_cost: bool) -> dict[str, Any]:
    lines = list(
        invoice.lines.select_related("item", "account", "cost_center").order_by("sequence")
    )
    payload: dict[str, Any] = {
        "id": invoice.pk,
        "public_id": str(invoice.public_id),
        "organization_id": invoice.organization_id,
        "branch_id": invoice.branch_id,
        "supplier_id": invoice.supplier_id,
        "supplier_code": invoice.supplier.code,
        "number": invoice.number,
        "supplier_invoice_number": invoice.supplier_invoice_number,
        "supplier_reference": invoice.supplier_reference,
        "currency_code": invoice.currency_code,
        "invoice_date": invoice.invoice_date.isoformat(),
        "business_date": invoice.business_date.isoformat(),
        "due_date": invoice.due_date.isoformat(),
        "payment_terms_days": invoice.payment_terms_days,
        "credit_term_public_id": (
            str(invoice.credit_term_public_id) if invoice.credit_term_public_id else None
        ),
        "credit_term_version": invoice.credit_term_version,
        "credit_term_name": invoice.credit_term_name,
        "credit_term_net_days": invoice.credit_term_net_days,
        "status": invoice.status,
        "matching_status": _invoice_matching_status(invoice),
        "created_by": invoice.created_by.username,
        "approved_by": invoice.approved_by.username if invoice.approved_by else None,
        "posted_by": invoice.posted_by.username if invoice.posted_by else None,
        "reversed_by": invoice.reversed_by.username if invoice.reversed_by else None,
        "approved_at": invoice.approved_at.isoformat() if invoice.approved_at else None,
        "posted_at": invoice.posted_at.isoformat() if invoice.posted_at else None,
        "reversed_at": invoice.reversed_at.isoformat() if invoice.reversed_at else None,
        "reversal_reason": invoice.reversal_reason,
        "is_ready_to_post": invoice.is_ready_to_post,
        "blocking_line_sequences": [line.sequence for line in invoice.blocking_lines],
        "journal_entry": invoice.journal_entry.entry_number if invoice.journal_entry else None,
        "lines": [_serialize_invoice_line(line, include_cost=include_cost) for line in lines],
    }
    if include_cost:
        payload.update(
            {
                "lines_total": format(invoice.lines_total, "f"),
                "freight_amount": format(invoice.freight_amount, "f"),
                "discount_amount": format(invoice.discount_amount, "f"),
                "total_amount": format(invoice.total_amount, "f"),
                "posted_amount": (
                    format(invoice.posted_amount, "f")
                    if invoice.posted_amount is not None
                    else None
                ),
                "outstanding": format(outstanding_amount(invoice), "f"),
            }
        )
    return payload


def _invoice_response(invoice: SupplierInvoice, *, actor: User) -> Any:
    """Keep forbidden monetary keys out of the wire response, not merely null."""
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    payload = _serialize_invoice(invoice, include_cost=include_cost)
    # A response model materialises absent Optional fields as null. Returning
    # JsonResponse for this one authorization branch deliberately bypasses
    # that materialisation; PRC-061 requires omission, because null means a
    # value was shown and happened not to exist.
    return payload if include_cost else JsonResponse(payload)


def _invoice_created_response(
    invoice: SupplierInvoice, *, actor: User
) -> Status[Any] | JsonResponse:
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    payload = _serialize_invoice(invoice, include_cost=include_cost)
    return Status(201, payload) if include_cost else JsonResponse(payload, status=201)


def _invoice_matching_status(invoice: SupplierInvoice) -> str:
    if not invoice.lines.filter(line_type="INVENTORY").exists():
        return "DIRECT"
    match_status = (
        invoice.matches.exclude(status="CANCELLED").values_list("status", flat=True).first()
    )
    if match_status == "READY":
        return "MATCHED"
    if match_status == "DRAFT":
        return "IN_PROGRESS"
    return "UNMATCHED"


def _required_money(value: str, *, field: str) -> Decimal:
    parsed = _money(value, field=field)
    if parsed is None:
        raise ValidationError(f"{field} is required.", code="required")
    return parsed


def _required_date(value: str, *, field: str) -> datetime.date:
    parsed = _date(value, field=field)
    if parsed is None:
        raise ValidationError(f"{field} is required.", code="required")
    return parsed


@router.get(
    "/supplier-invoices/", response=list[SupplierInvoiceOut], summary="List supplier invoices"
)
def list_supplier_invoices(
    request: HttpRequest,
    status: str | None = None,
    supplier_id: int | None = None,
    branch_id: int | None = None,
    matching: str | None = None,
    overdue: bool = False,
    invoice_from: str | None = None,
    invoice_to: str | None = None,
    accounting_from: str | None = None,
    accounting_to: str | None = None,
    due_from: str | None = None,
    due_to: str | None = None,
    supplier_reference: str | None = None,
    number: str | None = None,
) -> Any:
    actor = _require_invoice_view(request)
    queryset = visible_supplier_invoices(actor)
    if status:
        queryset = queryset.filter(status=status.strip().upper())
    if supplier_id is not None:
        queryset = queryset.filter(supplier_id=supplier_id)
    if branch_id is not None:
        queryset = queryset.filter(branch_id=branch_id)
    match_state = (matching or "").strip().upper()
    if match_state == "DIRECT":
        queryset = queryset.exclude(lines__line_type="INVENTORY")
    elif match_state == "UNMATCHED":
        queryset = queryset.filter(lines__line_type="INVENTORY").exclude(
            matches__status__in=("DRAFT", "READY")
        )
    elif match_state == "IN_PROGRESS":
        queryset = queryset.filter(matches__status="DRAFT")
    elif match_state == "MATCHED":
        queryset = queryset.filter(matches__status="READY")
    for raw, lookup, field in (
        (invoice_from, "invoice_date__gte", "invoice_from"),
        (invoice_to, "invoice_date__lte", "invoice_to"),
        (accounting_from, "business_date__gte", "accounting_from"),
        (accounting_to, "business_date__lte", "accounting_to"),
        (due_from, "due_date__gte", "due_from"),
        (due_to, "due_date__lte", "due_to"),
    ):
        value = _date(raw, field=field)
        if value is not None:
            queryset = queryset.filter(**{lookup: value})
    if supplier_reference:
        queryset = queryset.filter(supplier_reference__icontains=supplier_reference.strip())
    if number:
        queryset = queryset.filter(number__icontains=number.strip())
    if overdue:
        queryset = queryset.filter(status="POSTED", due_date__lt=datetime.date.today())
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    payload = [
        _serialize_invoice(invoice, include_cost=include_cost)
        for invoice in queryset.order_by("-id").distinct()
    ]
    return payload if include_cost else JsonResponse(payload, safe=False)


@router.get(
    "/supplier-invoices/{invoice_id}/",
    response=SupplierInvoiceOut,
    summary="Read one supplier invoice",
)
def read_supplier_invoice(request: HttpRequest, invoice_id: int) -> Any:
    actor = _require_invoice_view(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    return _invoice_response(invoice, actor=actor)


@router.post(
    "/supplier-invoices/",
    response={201: SupplierInvoiceOut},
    summary="Record a supplier invoice",
)
def create_invoice(request: HttpRequest, payload: SupplierInvoiceIn) -> Any:
    actor = _actor(request)
    branch = resolve_branch(actor, payload.branch_id)
    require_organization_permission(actor, CREATE_SUPPLIER_INVOICE, branch.organization)
    supplier = resolve_supplier(actor, payload.supplier_id)

    invoice = create_supplier_invoice(
        supplier=supplier,
        branch=branch,
        created_by=actor,
        supplier_invoice_number=payload.supplier_invoice_number,
        invoice_date=_required_date(payload.invoice_date, field="invoice_date"),
        business_date=_date(payload.business_date, field="business_date"),
        supplier_reference=payload.supplier_reference,
        currency_code=payload.currency_code,
        freight_amount=_money(payload.freight_amount, field="freight_amount"),
        discount_amount=_money(payload.discount_amount, field="discount_amount"),
        notes=payload.notes,
    )
    return _invoice_created_response(invoice, actor=actor)


@router.patch(
    "/supplier-invoices/{invoice_id}/",
    response=SupplierInvoiceOut,
    summary="Correct a draft supplier invoice",
)
def update_invoice(request: HttpRequest, invoice_id: int, payload: SupplierInvoiceUpdateIn) -> Any:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, CREATE_SUPPLIER_INVOICE, invoice.organization)
    supplied = payload.model_dump(exclude_unset=True)
    updated = update_supplier_invoice(
        invoice=invoice,
        supplier_invoice_number=supplied.get("supplier_invoice_number"),
        invoice_date=(
            _required_date(supplied["invoice_date"], field="invoice_date")
            if supplied.get("invoice_date") is not None
            else None
        ),
        business_date=(
            _required_date(supplied["business_date"], field="business_date")
            if supplied.get("business_date") is not None
            else None
        ),
        supplier_reference=supplied.get("supplier_reference"),
        currency_code=supplied.get("currency_code"),
        freight_amount=(
            _required_money(supplied["freight_amount"], field="freight_amount")
            if supplied.get("freight_amount") is not None
            else None
        ),
        discount_amount=(
            _required_money(supplied["discount_amount"], field="discount_amount")
            if supplied.get("discount_amount") is not None
            else None
        ),
        notes=supplied.get("notes"),
    )
    return _invoice_response(updated, actor=actor)


@router.delete(
    "/supplier-invoices/{invoice_id}/", response={204: None}, summary="Discard a draft invoice"
)
def delete_invoice(request: HttpRequest, invoice_id: int) -> Status[Any]:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, CREATE_SUPPLIER_INVOICE, invoice.organization)
    delete_supplier_invoice(invoice=invoice)
    return Status(204, None)


@router.post(
    "/supplier-invoices/{invoice_id}/lines/inventory/",
    response={201: SupplierInvoiceOut},
    summary="Add a goods line",
)
def add_invoice_inventory_line(
    request: HttpRequest, invoice_id: int, payload: InvoiceInventoryLineIn
) -> Any:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, CREATE_SUPPLIER_INVOICE, invoice.organization)
    item = resolve_item(actor, payload.item_id)

    receipt_line = None
    if payload.receipt_line_id is not None:
        receipt_line = _resolve_receipt_line_for_invoice(actor, invoice, payload.receipt_line_id)
    order_line = None
    if payload.order_line_id is not None:
        order_line = _resolve_order_line_for_invoice(actor, invoice, payload.order_line_id)

    add_inventory_line(
        invoice=invoice,
        item=item,
        base_quantity=_required_money(payload.base_quantity, field="base_quantity"),
        unit_price=_required_money(payload.unit_price, field="unit_price"),
        receipt_line=receipt_line,
        order_line=order_line,
        description=payload.description,
        note=payload.note,
    )
    return _invoice_created_response(_reload(invoice), actor=actor)


@router.post(
    "/supplier-invoices/{invoice_id}/lines/account/",
    response={201: SupplierInvoiceOut},
    summary="Add a direct expense line",
)
def add_invoice_account_line(
    request: HttpRequest, invoice_id: int, payload: InvoiceAccountLineIn
) -> Any:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, CREATE_SUPPLIER_INVOICE, invoice.organization)

    # Resolved inside the invoice's own organization, never by id alone.
    account = Account.objects.filter(
        pk=payload.account_id, organization_id=invoice.organization_id
    ).first()
    if account is None:
        raise OutOfScope(f"Account {payload.account_id} does not exist.")
    cost_center = None
    if payload.cost_center_id is not None:
        cost_center = CostCenter.objects.filter(
            pk=payload.cost_center_id, organization_id=invoice.organization_id
        ).first()
        if cost_center is None:
            raise OutOfScope(f"Cost center {payload.cost_center_id} does not exist.")

    add_account_line(
        invoice=invoice,
        account=account,
        cost_center=cost_center,
        description=payload.description,
        quantity=_required_money(payload.quantity, field="quantity"),
        unit_price=_required_money(payload.unit_price, field="unit_price"),
        note=payload.note,
    )
    return _invoice_created_response(_reload(invoice), actor=actor)


@router.post(
    "/supplier-invoices/{invoice_id}/approve/",
    response=SupplierInvoiceOut,
    summary="Approve a supplier invoice",
)
def approve_invoice(request: HttpRequest, invoice_id: int) -> Any:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, APPROVE_SUPPLIER_INVOICE, invoice.organization)
    approved = approve_supplier_invoice(invoice=invoice, actor=actor)
    return _invoice_response(approved, actor=actor)


@router.post(
    "/supplier-invoices/{invoice_id}/return-to-draft/",
    response=SupplierInvoiceOut,
    summary="Return an approved supplier invoice to draft",
)
def return_invoice_to_draft(request: HttpRequest, invoice_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, APPROVE_SUPPLIER_INVOICE, invoice.organization)
    returned = return_supplier_invoice_to_draft(invoice=invoice, actor=actor, reason=payload.reason)
    return _invoice_response(returned, actor=actor)


@router.post(
    "/supplier-invoices/{invoice_id}/post/",
    response=SupplierInvoiceOut,
    summary="Post a supplier invoice",
)
def post_invoice(request: HttpRequest, invoice_id: int) -> Any:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, POST_SUPPLIER_INVOICE, invoice.organization)
    posted = post_supplier_invoice(invoice=invoice, actor=actor)
    return _invoice_response(posted, actor=actor)


@router.post(
    "/supplier-invoices/{invoice_id}/reverse/",
    response=SupplierInvoiceOut,
    summary="Reverse a posted supplier invoice",
)
def reverse_invoice(request: HttpRequest, invoice_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, REVERSE_SUPPLIER_INVOICE, invoice.organization)
    reversed_invoice = reverse_supplier_invoice(invoice=invoice, actor=actor, reason=payload.reason)
    return _invoice_response(reversed_invoice, actor=actor)


def _reload(invoice: SupplierInvoice) -> SupplierInvoice:
    """Re-read after a line change, so the totals in the response are the stored ones."""
    return SupplierInvoice.objects.select_related("supplier", "journal_entry").get(pk=invoice.pk)


def _resolve_receipt_line_for_invoice(
    actor: User, invoice: SupplierInvoice, line_id: int
) -> GoodsReceiptLine:
    """A receipt line the caller may reach, under this invoice's own supplier."""
    line = (
        GoodsReceiptLine.objects.filter(
            pk=line_id,
            receipt__in=visible_goods_receipts(actor),
            receipt__organization_id=invoice.organization_id,
        )
        .select_related("receipt")
        .first()
    )
    if line is None:
        raise OutOfScope(f"Receipt line {line_id} does not exist.")
    return line


def _resolve_order_line_for_invoice(
    actor: User, invoice: SupplierInvoice, line_id: int
) -> PurchaseOrderLine:
    """An order line the caller may reach, under this invoice's own organization."""
    line = (
        PurchaseOrderLine.objects.filter(
            pk=line_id,
            order__in=visible_purchase_orders(actor),
            order__organization_id=invoice.organization_id,
        )
        .select_related("order")
        .first()
    )
    if line is None:
        raise OutOfScope(f"Order line {line_id} does not exist.")
    return line


# ---------------------------------------------------------------------------
# Three-way matching (Task 2.11)
# ---------------------------------------------------------------------------
#
# Commands, not CRUD. Allocations are writable only while the match is a
# draft; once it is READY the only remaining command is cancellation. Nothing
# here posts, and there is deliberately no endpoint that could be mistaken for
# one — `ready` freezes evidence and returns a match whose invoice is still
# APPROVED.


class MatchAllocationOut(Schema):
    id: int
    sequence: int
    invoice_line_id: int
    invoice_line_sequence: int
    receipt_line_id: int
    receipt_number: str
    order_line_id: int | None = None
    order_version: int | None = None
    item_code: str
    matched_base_quantity: str
    #: Omitted without `view_supplier_cost`, never blanked.
    receipt_allocated_value: str | None = None
    invoice_allocated_value: str | None = None
    price_variance: str | None = None


class MatchCoverageOut(Schema):
    invoice_line_id: int
    sequence: int
    item_code: str
    invoiced_quantity: str
    matched_quantity: str
    unmatched_quantity: str
    state: str
    invoiced_value: str | None = None
    unmatched_value: str | None = None
    price_variance: str | None = None


class PurchaseMatchOut(Schema):
    id: int
    public_id: str
    organization_id: int
    supplier_id: int
    supplier_code: str
    supplier_invoice_id: int
    supplier_invoice_number: str
    #: The invoice's own state, restated here so no caller has to infer that a
    #: READY match means the invoice posted. It does not.
    supplier_invoice_status: str
    number: str
    status: str
    #: Whether a live posting generation stands on this match. Derived from
    #: the posting table, never stored on the match — a second copy of the
    #: fact would drift the first time a reversal touched one and not the
    #: other. False after a reversal, because the generation is history.
    is_financially_posted: bool
    posting_generation: int | None = None
    journal_entry: str | None = None
    total_matched_quantity: str
    total_price_variance: str | None = None
    #: The three figures the posting actually used, behind the cost
    #: permission. Omitted, not blanked, for a caller without it (PRC-061).
    goods_cleared_value: str | None = None
    invoice_matched_value: str | None = None
    posted_price_variance: str | None = None
    allocations: list[MatchAllocationOut] = []
    coverage: list[MatchCoverageOut] = []


class MatchAllocationIn(Schema):
    invoice_line_id: int
    receipt_line_id: int
    matched_base_quantity: str
    note: str = ""


def _require_match_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_PURCHASE_MATCH):
        raise PermissionMissing(f"{VIEW_PURCHASE_MATCH} is not held.")
    return actor


def _serialize_allocation(
    allocation: PurchaseMatchAllocation, *, include_cost: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": allocation.pk,
        "sequence": allocation.sequence,
        "invoice_line_id": allocation.supplier_invoice_line_id,
        "invoice_line_sequence": allocation.supplier_invoice_line.sequence,
        "receipt_line_id": allocation.goods_receipt_line_id,
        "receipt_number": allocation.goods_receipt_line.receipt.number,
        "order_line_id": allocation.purchase_order_line_id,
        "order_version": allocation.purchase_order_version,
        "item_code": allocation.goods_receipt_line.item.code,
        "matched_base_quantity": format(allocation.matched_base_quantity, "f"),
    }
    if include_cost:
        payload.update(
            {
                "receipt_allocated_value": format(allocation.receipt_allocated_value, "f"),
                "invoice_allocated_value": format(allocation.invoice_allocated_value, "f"),
                "price_variance": format(allocation.price_variance, "f"),
            }
        )
    return payload


def _serialize_match(match: PurchaseMatch, *, include_cost: bool) -> dict[str, Any]:
    posting = live_posting_for(match)
    allocations = list(
        match.allocations.select_related(
            "supplier_invoice_line",
            "goods_receipt_line",
            "goods_receipt_line__receipt",
            "goods_receipt_line__item",
            "purchase_order_line",
        ).order_by("sequence")
    )
    payload: dict[str, Any] = {
        "id": match.pk,
        "public_id": str(match.public_id),
        "organization_id": match.organization_id,
        "supplier_id": match.supplier_id,
        "supplier_code": match.supplier.code,
        "supplier_invoice_id": match.supplier_invoice_id,
        "supplier_invoice_number": match.supplier_invoice.supplier_invoice_number,
        "supplier_invoice_status": match.supplier_invoice.status,
        "number": match.number,
        "status": match.status,
        # Derived from the posting table rather than stored on the match, for
        # the reason PRC-042 gives about line state: a second copy of a fact
        # drifts the first time a reversal touches one and not the other.
        "is_financially_posted": posting is not None,
        "posting_generation": posting.generation if posting else None,
        "journal_entry": posting.journal_entry.entry_number if posting else None,
        "total_matched_quantity": format(match.total_matched_quantity, "f"),
        "allocations": [
            _serialize_allocation(row, include_cost=include_cost) for row in allocations
        ],
        "coverage": [
            {
                "invoice_line_id": row["line"].pk,
                "sequence": row["line"].sequence,
                "item_code": row["line"].item.code if row["line"].item else "",
                "invoiced_quantity": format(row["invoiced_quantity"], "f"),
                "matched_quantity": format(row["matched_quantity"], "f"),
                "unmatched_quantity": format(row["unmatched_quantity"], "f"),
                "state": row["state"],
                **(
                    {
                        "invoiced_value": format(row["invoiced_value"], "f"),
                        "unmatched_value": format(row["unmatched_value"], "f"),
                        "price_variance": format(row["price_variance"], "f"),
                    }
                    if include_cost
                    else {}
                ),
            }
            for row in coverage_for_invoice(match.supplier_invoice)
        ],
    }
    if include_cost:
        payload["total_price_variance"] = format(match.total_price_variance, "f")
        if posting is not None:
            payload["goods_cleared_value"] = format(posting.goods_cleared_value, "f")
            payload["invoice_matched_value"] = format(posting.invoice_matched_value, "f")
            payload["posted_price_variance"] = format(posting.price_variance, "f")
    return payload


@router.get("/matches/", response=list[PurchaseMatchOut], summary="List purchase matches")
def list_matches(request: HttpRequest, status: str | None = None) -> Any:
    actor = _require_match_view(request)
    queryset = visible_purchase_matches(actor)
    if status:
        queryset = queryset.filter(status=status.strip().upper())
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    return [
        _serialize_match(match, include_cost=include_cost) for match in queryset.order_by("-id")
    ]


@router.get("/matches/{match_id}/", response=PurchaseMatchOut, summary="Read one match")
def read_match(request: HttpRequest, match_id: int) -> Any:
    actor = _require_match_view(request)
    match = resolve_purchase_match(actor, match_id)
    return _serialize_match(match, include_cost=actor.has_perm(VIEW_SUPPLIER_COST))


@router.post(
    "/supplier-invoices/{invoice_id}/match/",
    response={201: PurchaseMatchOut},
    summary="Open a match against a supplier invoice",
)
def open_match(request: HttpRequest, invoice_id: int) -> Status[Any]:
    actor = _actor(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    require_organization_permission(actor, MATCH_SUPPLIER_INVOICE, invoice.organization)
    match = create_purchase_match(invoice=invoice, created_by=actor)
    return Status(201, _serialize_match(match, include_cost=True))


@router.delete("/matches/{match_id}/", response={204: None}, summary="Discard a draft match")
def discard_match(request: HttpRequest, match_id: int) -> Status[Any]:
    actor = _actor(request)
    match = resolve_purchase_match(actor, match_id)
    require_organization_permission(actor, MATCH_SUPPLIER_INVOICE, match.organization)
    delete_purchase_match(match=match)
    return Status(204, None)


@router.post(
    "/matches/{match_id}/allocations/",
    response={201: PurchaseMatchOut},
    summary="Allocate a delivery against an invoice line",
)
def create_allocation(
    request: HttpRequest, match_id: int, payload: MatchAllocationIn
) -> Status[Any]:
    actor = _actor(request)
    match = resolve_purchase_match(actor, match_id)
    require_organization_permission(actor, MATCH_SUPPLIER_INVOICE, match.organization)

    # Both lines resolved inside this match's own documents, never by id alone.
    invoice_line = SupplierInvoiceLine.objects.filter(
        pk=payload.invoice_line_id, invoice=match.supplier_invoice
    ).first()
    if invoice_line is None:
        raise OutOfScope(f"Invoice line {payload.invoice_line_id} does not exist.")
    receipt_line = (
        GoodsReceiptLine.objects.filter(
            pk=payload.receipt_line_id,
            receipt__organization_id=match.organization_id,
            receipt__supplier_id=match.supplier_id,
        )
        .select_related("receipt")
        .first()
    )
    if receipt_line is None:
        raise OutOfScope(f"Receipt line {payload.receipt_line_id} does not exist.")

    add_allocation(
        match=match,
        invoice_line=invoice_line,
        receipt_line=receipt_line,
        matched_base_quantity=_required_money(
            payload.matched_base_quantity, field="matched_base_quantity"
        ),
        created_by=actor,
        note=payload.note,
    )
    return Status(201, _serialize_match(_reload_match(match), include_cost=True))


@router.delete(
    "/matches/{match_id}/allocations/{allocation_id}/",
    response={204: None},
    summary="Remove a draft allocation",
)
def delete_allocation(request: HttpRequest, match_id: int, allocation_id: int) -> Status[Any]:
    actor = _actor(request)
    match = resolve_purchase_match(actor, match_id)
    require_organization_permission(actor, MATCH_SUPPLIER_INVOICE, match.organization)
    allocation = resolve_match_allocation(actor, match=match, allocation_id=allocation_id)
    remove_allocation(allocation=allocation)
    return Status(204, None)


@router.post(
    "/matches/{match_id}/ready/",
    response=PurchaseMatchOut,
    summary="Freeze a match's evidence (posts nothing)",
)
def ready_match(request: HttpRequest, match_id: int) -> Any:
    actor = _actor(request)
    match = resolve_purchase_match(actor, match_id)
    require_organization_permission(actor, MATCH_SUPPLIER_INVOICE, match.organization)
    ready = mark_match_ready(match=match, actor=actor)
    return _serialize_match(ready, include_cost=True)


@router.post("/matches/{match_id}/cancel/", response=PurchaseMatchOut, summary="Withdraw a match")
def cancel_match(request: HttpRequest, match_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    match = resolve_purchase_match(actor, match_id)
    require_organization_permission(actor, CANCEL_PURCHASE_MATCH, match.organization)
    cancelled = cancel_purchase_match(match=match, actor=actor, reason=payload.reason)
    return _serialize_match(cancelled, include_cost=True)


def _reload_match(match: PurchaseMatch) -> PurchaseMatch:
    """Re-read after an allocation change, so the response carries stored totals."""
    return PurchaseMatch.objects.select_related("supplier", "supplier_invoice").get(pk=match.pk)


# ---------------------------------------------------------------------------
# Supplier returns (Task 2.13)
# ---------------------------------------------------------------------------


class ReturnLineOut(Schema):
    id: int
    sequence: int
    item_id: int
    item_code: str
    lot_code: str | None = None
    returned_base_quantity: str
    movement_id: int | None = None
    note: str
    #: Omitted entirely without `view_supplier_cost`, never blanked.
    posted_value: str | None = None
    expected_credit_value: str | None = None
    inventory_account: str | None = None
    clearing_account: str | None = None


class SupplierReturnOut(Schema):
    id: int
    public_id: str
    organization_id: int
    branch_id: int
    supplier_id: int
    supplier_code: str
    warehouse_code: str
    number: str
    status: str
    returned_at: str
    business_date: str
    reason_code: str | None = None
    reason: str
    evidence_reference: str
    journal_entry: str | None = None
    reversal_journal_entry: str | None = None
    posted_value: str | None = None
    lines: list[ReturnLineOut] = []


class SupplierReturnIn(Schema):
    supplier_id: int
    branch_id: int
    warehouse_id: int
    location_id: int | None = None
    returned_at: str
    reason_code_id: int | None = None
    reason: str = ""
    evidence_reference: str = ""
    notes: str = ""


class ReturnLineIn(Schema):
    item_id: int
    lot_id: int | None = None
    returned_base_quantity: str
    expected_credit_value: str | None = None
    note: str = ""


def _require_return_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SUPPLIER_RETURN):
        raise PermissionMissing(f"{VIEW_SUPPLIER_RETURN} is not held.")
    return actor


def _serialize_return_line(line: SupplierReturnLine, *, include_cost: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": line.pk,
        "sequence": line.sequence,
        "item_id": line.item_id,
        "item_code": line.item.code,
        "lot_code": line.lot.code if line.lot else None,
        "returned_base_quantity": format(line.returned_base_quantity, "f"),
        "movement_id": line.movement_id,
        "note": line.note,
    }
    if include_cost:
        payload.update(
            {
                "posted_value": (
                    format(line.posted_value, "f") if line.posted_value is not None else None
                ),
                "expected_credit_value": (
                    format(line.expected_credit_value, "f")
                    if line.expected_credit_value is not None
                    else None
                ),
                "inventory_account": (
                    line.inventory_account.code if line.inventory_account else None
                ),
                "clearing_account": line.contra_account.code if line.contra_account else None,
            }
        )
    return payload


def _serialize_return(supplier_return: SupplierReturn, *, include_cost: bool) -> dict[str, Any]:
    lines = list(
        supplier_return.lines.select_related(
            "item", "lot", "inventory_account", "contra_account"
        ).order_by("sequence")
    )
    payload: dict[str, Any] = {
        "id": supplier_return.pk,
        "public_id": str(supplier_return.public_id),
        "organization_id": supplier_return.organization_id,
        "branch_id": supplier_return.branch_id,
        "supplier_id": supplier_return.supplier_id,
        "supplier_code": supplier_return.supplier.code,
        "warehouse_code": supplier_return.warehouse.code,
        "number": supplier_return.number,
        "status": supplier_return.status,
        "returned_at": supplier_return.returned_at.isoformat(),
        "business_date": supplier_return.business_date.isoformat(),
        "reason_code": supplier_return.reason_code.code if supplier_return.reason_code else None,
        "reason": supplier_return.reason,
        "evidence_reference": supplier_return.evidence_reference,
        "journal_entry": (
            supplier_return.journal_entry.entry_number if supplier_return.journal_entry else None
        ),
        "reversal_journal_entry": (
            supplier_return.reversal_journal_entry.entry_number
            if supplier_return.reversal_journal_entry
            else None
        ),
        "lines": [_serialize_return_line(line, include_cost=include_cost) for line in lines],
    }
    if include_cost:
        payload["posted_value"] = (
            format(supplier_return.posted_value, "f")
            if supplier_return.posted_value is not None
            else None
        )
    return payload


@router.get("/supplier-returns/", response=list[SupplierReturnOut], summary="List supplier returns")
def list_supplier_returns(request: HttpRequest, status: str | None = None) -> Any:
    actor = _require_return_view(request)
    queryset = visible_supplier_returns(actor)
    if status:
        queryset = queryset.filter(status=status.strip().upper())
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    return [_serialize_return(row, include_cost=include_cost) for row in queryset.order_by("-id")]


@router.get(
    "/supplier-returns/{return_id}/",
    response=SupplierReturnOut,
    summary="Read one supplier return",
)
def read_supplier_return(request: HttpRequest, return_id: int) -> Any:
    actor = _require_return_view(request)
    supplier_return = resolve_supplier_return(actor, return_id)
    return _serialize_return(supplier_return, include_cost=actor.has_perm(VIEW_SUPPLIER_COST))


@router.post(
    "/supplier-returns/",
    response={201: SupplierReturnOut},
    summary="Open a standalone draft supplier return",
)
def create_return(request: HttpRequest, payload: SupplierReturnIn) -> Status[Any]:
    actor = _actor(request)
    warehouse = (
        accessible_warehouses(actor)
        .select_related("branch__organization")
        .filter(pk=payload.warehouse_id)
        .first()
    )
    supplier = visible_suppliers(actor).filter(pk=payload.supplier_id).first()
    branch = accessible_branches(actor).filter(pk=payload.branch_id).first()
    if (
        warehouse is None
        or supplier is None
        or branch is None
        or warehouse.branch.organization_id != supplier.organization_id
        or branch.organization_id != supplier.organization_id
    ):
        raise OutOfScope(
            "Supplier, branch, and warehouse must belong to the same accessible organization."
        )
    require_warehouse_permission(actor, CREATE_SUPPLIER_RETURN, warehouse)
    location = (
        StockLocation.objects.filter(pk=payload.location_id, warehouse=warehouse).first()
        if payload.location_id
        else None
    )
    if payload.location_id and location is None:
        raise OutOfScope(f"Stock location {payload.location_id} does not exist.")

    reason_code = None
    if payload.reason_code_id is not None:
        reason_code = InventoryReasonCode.objects.filter(
            pk=payload.reason_code_id,
            organization_id=warehouse.branch.organization_id,
        ).first()
        if reason_code is None:
            raise OutOfScope(f"Reason code {payload.reason_code_id} does not exist.")

    supplier_return = create_supplier_return(
        organization=warehouse.branch.organization,
        branch=branch,
        supplier=supplier,
        warehouse=warehouse,
        location=location,
        created_by=actor,
        returned_at=_required_date(payload.returned_at, field="returned_at"),
        reason_code=reason_code,
        reason=payload.reason,
        evidence_reference=payload.evidence_reference,
        notes=payload.notes,
    )
    return Status(201, _serialize_return(supplier_return, include_cost=True))


@router.delete(
    "/supplier-returns/{return_id}/", response={204: None}, summary="Discard a draft return"
)
def delete_return(request: HttpRequest, return_id: int) -> Status[Any]:
    actor = _actor(request)
    supplier_return = resolve_supplier_return(actor, return_id)
    require_warehouse_permission(actor, CREATE_SUPPLIER_RETURN, supplier_return.warehouse)
    delete_supplier_return(supplier_return=supplier_return)
    return Status(204, None)


@router.post(
    "/supplier-returns/{return_id}/lines/",
    response={201: SupplierReturnOut},
    summary="Add an inventory item to a standalone return",
)
def add_return_line_endpoint(
    request: HttpRequest, return_id: int, payload: ReturnLineIn
) -> Status[Any]:
    actor = _actor(request)
    supplier_return = resolve_supplier_return(actor, return_id)
    require_warehouse_permission(actor, CREATE_SUPPLIER_RETURN, supplier_return.warehouse)

    item = InventoryItem.objects.filter(
        pk=payload.item_id, organization_id=supplier_return.organization_id
    ).first()
    lot = (
        InventoryLot.objects.filter(pk=payload.lot_id, item=item).first()
        if payload.lot_id
        else None
    )
    if item is None or (payload.lot_id and lot is None):
        raise OutOfScope("Item or lot does not exist in this organization.")

    add_return_line(
        supplier_return=supplier_return,
        item=item,
        lot=lot,
        returned_base_quantity=_required_money(
            payload.returned_base_quantity, field="returned_base_quantity"
        ),
        expected_credit_value=_money(payload.expected_credit_value, field="expected_credit_value"),
        note=payload.note,
    )
    return Status(201, _serialize_return(_reload_return(supplier_return), include_cost=True))


@router.delete(
    "/supplier-returns/{return_id}/lines/{line_id}/",
    response={204: None},
    summary="Take a line off a draft return",
)
def delete_return_line(request: HttpRequest, return_id: int, line_id: int) -> Status[Any]:
    actor = _actor(request)
    supplier_return = resolve_supplier_return(actor, return_id)
    require_warehouse_permission(actor, CREATE_SUPPLIER_RETURN, supplier_return.warehouse)
    line = resolve_return_line(actor, supplier_return=supplier_return, line_id=line_id)
    remove_return_line(line=line)
    return Status(204, None)


@router.post(
    "/supplier-returns/{return_id}/post/",
    response=SupplierReturnOut,
    summary="Post a supplier return",
)
def post_return(request: HttpRequest, return_id: int) -> Any:
    actor = _actor(request)
    supplier_return = resolve_supplier_return(actor, return_id)
    require_warehouse_permission(actor, POST_SUPPLIER_RETURN, supplier_return.warehouse)
    posted = post_supplier_return(supplier_return=supplier_return, actor=actor)
    return _serialize_return(posted, include_cost=True)


@router.post(
    "/supplier-returns/{return_id}/reverse/",
    response=SupplierReturnOut,
    summary="Reverse a posted supplier return",
)
def reverse_return(request: HttpRequest, return_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    supplier_return = resolve_supplier_return(actor, return_id)
    require_warehouse_permission(actor, REVERSE_SUPPLIER_RETURN, supplier_return.warehouse)
    reversed_return = reverse_supplier_return(
        supplier_return=supplier_return, actor=actor, reason=payload.reason
    )
    return _serialize_return(reversed_return, include_cost=True)


def _reload_return(supplier_return: SupplierReturn) -> SupplierReturn:
    """Re-read after a line change, so the response carries the stored rows."""
    return SupplierReturn.objects.select_related(
        "supplier", "warehouse", "reason_code", "journal_entry"
    ).get(pk=supplier_return.pk)


# ---------------------------------------------------------------------------
# Supplier credit notes (Task 2.14)
# ---------------------------------------------------------------------------


class CreditAllocationOut(Schema):
    id: int
    sequence: int
    invoice_id: int
    invoice_number: str
    allocated_amount: str
    note: str


class CreditReturnAllocationOut(Schema):
    id: int
    sequence: int
    supplier_return_line_id: int
    item_code: str
    credited_base_quantity: str
    note: str
    #: Omitted entirely without `view_supplier_cost`, never blanked.
    allocated_credit_amount: str | None = None
    settled_book_value: str | None = None


class SupplierCreditNoteOut(Schema):
    id: int
    public_id: str
    organization_id: int
    branch_id: int
    supplier_id: int
    supplier_code: str
    supplier_return_id: int
    supplier_return_number: str
    number: str
    status: str
    supplier_document_number: str
    credit_date: str
    business_date: str
    reason: str
    journal_entry: str | None = None
    reversal_journal_entry: str | None = None
    #: Omitted entirely without `view_supplier_cost`, never blanked.
    amount: str | None = None
    book_value: str | None = None
    unallocated: str | None = None
    allocations: list[CreditAllocationOut] = []
    return_allocations: list[CreditReturnAllocationOut] = []


class SupplierCreditNoteIn(Schema):
    supplier_return_id: int
    supplier_document_number: str
    credit_date: str
    amount: str
    business_date: str | None = None
    reason: str = ""
    notes: str = ""


class CreditAllocationIn(Schema):
    invoice_id: int
    allocated_amount: str
    note: str = ""


class CreditReturnAllocationIn(Schema):
    return_line_id: int
    credited_base_quantity: str
    allocated_credit_amount: str
    note: str = ""


def _require_note_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SUPPLIER_CREDIT_NOTE):
        raise PermissionMissing(f"{VIEW_SUPPLIER_CREDIT_NOTE} is not held.")
    return actor


def _serialize_note(note: SupplierCreditNote, *, include_cost: bool) -> dict[str, Any]:
    allocations = list(note.allocations.select_related("invoice").order_by("sequence"))
    payload: dict[str, Any] = {
        "id": note.pk,
        "public_id": str(note.public_id),
        "organization_id": note.organization_id,
        "branch_id": note.branch_id,
        "supplier_id": note.supplier_id,
        "supplier_code": note.supplier.code,
        "supplier_return_id": note.supplier_return_id,
        "supplier_return_number": note.supplier_return.number,
        "number": note.number,
        "status": note.status,
        "supplier_document_number": note.supplier_document_number,
        "credit_date": note.credit_date.isoformat(),
        "business_date": note.business_date.isoformat(),
        "reason": note.reason,
        "journal_entry": note.journal_entry.entry_number if note.journal_entry else None,
        "reversal_journal_entry": (
            note.reversal_journal_entry.entry_number if note.reversal_journal_entry else None
        ),
        "allocations": [
            {
                "id": row.pk,
                "sequence": row.sequence,
                "invoice_id": row.invoice_id,
                "invoice_number": row.invoice.number,
                "allocated_amount": format(row.allocated_amount, "f") if include_cost else "",
                "note": row.note,
            }
            for row in allocations
        ],
        "return_allocations": [
            {
                "id": row.pk,
                "sequence": row.sequence,
                "supplier_return_line_id": row.supplier_return_line_id,
                "item_code": row.supplier_return_line.item.code,
                "credited_base_quantity": format(row.credited_base_quantity, "f"),
                "note": row.note,
                **(
                    {
                        "allocated_credit_amount": format(row.allocated_credit_amount, "f"),
                        "settled_book_value": (
                            format(row.settled_book_value, "f")
                            if row.settled_book_value is not None
                            else None
                        ),
                    }
                    if include_cost
                    else {}
                ),
            }
            for row in note.return_allocations.select_related(
                "supplier_return_line", "supplier_return_line__item"
            ).order_by("sequence")
        ],
    }
    if include_cost:
        payload.update(
            {
                "amount": format(note.amount, "f"),
                "book_value": (
                    format(note.supplier_return.posted_value, "f")
                    if note.supplier_return.posted_value is not None
                    else None
                ),
                "unallocated": format(unallocated_credit(note), "f"),
            }
        )
    return payload


@router.get(
    "/supplier-credit-notes/",
    response=list[SupplierCreditNoteOut],
    summary="List supplier credit notes",
)
def list_supplier_credit_notes(request: HttpRequest, status: str | None = None) -> Any:
    actor = _require_note_view(request)
    queryset = visible_supplier_credit_notes(actor)
    if status:
        queryset = queryset.filter(status=status.strip().upper())
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    return [_serialize_note(row, include_cost=include_cost) for row in queryset.order_by("-id")]


@router.get(
    "/supplier-credit-notes/{note_id}/",
    response=SupplierCreditNoteOut,
    summary="Read one supplier credit note",
)
def read_supplier_credit_note(request: HttpRequest, note_id: int) -> Any:
    actor = _require_note_view(request)
    note = resolve_supplier_credit_note(actor, note_id)
    return _serialize_note(note, include_cost=actor.has_perm(VIEW_SUPPLIER_COST))


@router.post(
    "/supplier-credit-notes/",
    response={201: SupplierCreditNoteOut},
    summary="Open a draft note against a posted return",
)
def create_credit_note(request: HttpRequest, payload: SupplierCreditNoteIn) -> Status[Any]:
    actor = _actor(request)
    supplier_return = visible_supplier_returns(actor).filter(pk=payload.supplier_return_id).first()
    if supplier_return is None:
        raise OutOfScope(f"Supplier return {payload.supplier_return_id} does not exist.")
    require_organization_permission(
        actor, CREATE_SUPPLIER_CREDIT_NOTE, supplier_return.organization
    )
    note = create_supplier_credit_note(
        supplier_return=supplier_return,
        created_by=actor,
        supplier_document_number=payload.supplier_document_number,
        credit_date=_required_date(payload.credit_date, field="credit_date"),
        amount=_required_money(payload.amount, field="amount"),
        business_date=_date(payload.business_date, field="business_date"),
        reason=payload.reason,
        notes=payload.notes,
    )
    return Status(201, _serialize_note(note, include_cost=True))


@router.delete(
    "/supplier-credit-notes/{note_id}/", response={204: None}, summary="Discard a draft note"
)
def delete_credit_note(request: HttpRequest, note_id: int) -> Status[Any]:
    actor = _actor(request)
    note = resolve_supplier_credit_note(actor, note_id)
    require_organization_permission(actor, CREATE_SUPPLIER_CREDIT_NOTE, note.organization)
    delete_supplier_credit_note(credit_note=note)
    return Status(204, None)


@router.post(
    "/supplier-credit-notes/{note_id}/return-allocations/",
    response={201: SupplierCreditNoteOut},
    summary="Settle part of a return line with this note",
)
def add_credit_return_allocation_endpoint(
    request: HttpRequest, note_id: int, payload: CreditReturnAllocationIn
) -> Status[Any]:
    actor = _actor(request)
    note = resolve_supplier_credit_note(actor, note_id)
    require_organization_permission(actor, CREATE_SUPPLIER_CREDIT_NOTE, note.organization)
    return_line = SupplierReturnLine.objects.filter(
        pk=payload.return_line_id, supplier_return_id=note.supplier_return_id
    ).first()
    if return_line is None:
        raise OutOfScope(f"Return line {payload.return_line_id} does not exist.")
    add_return_allocation(
        credit_note=note,
        return_line=return_line,
        credited_base_quantity=_required_money(
            payload.credited_base_quantity, field="credited_base_quantity"
        ),
        allocated_credit_amount=_required_money(
            payload.allocated_credit_amount, field="allocated_credit_amount"
        ),
        note=payload.note,
    )
    return Status(201, _serialize_note(_reload_note(note), include_cost=True))


@router.delete(
    "/supplier-credit-notes/{note_id}/return-allocations/{allocation_id}/",
    response={204: None},
    summary="Remove a draft settlement slice",
)
def delete_credit_return_allocation(
    request: HttpRequest, note_id: int, allocation_id: int
) -> Status[Any]:
    actor = _actor(request)
    note = resolve_supplier_credit_note(actor, note_id)
    require_organization_permission(actor, CREATE_SUPPLIER_CREDIT_NOTE, note.organization)
    allocation = resolve_credit_return_allocation(
        actor, credit_note=note, allocation_id=allocation_id
    )
    remove_return_allocation(allocation=allocation)
    return Status(204, None)


@router.post(
    "/supplier-credit-notes/{note_id}/allocations/",
    response={201: SupplierCreditNoteOut},
    summary="Net part of the note against a posted invoice",
)
def add_credit_allocation_endpoint(
    request: HttpRequest, note_id: int, payload: CreditAllocationIn
) -> Status[Any]:
    actor = _actor(request)
    note = resolve_supplier_credit_note(actor, note_id)
    require_organization_permission(actor, CREATE_SUPPLIER_CREDIT_NOTE, note.organization)
    invoice = resolve_supplier_invoice(actor, payload.invoice_id)
    add_credit_allocation(
        credit_note=note,
        invoice=invoice,
        allocated_amount=_required_money(payload.allocated_amount, field="allocated_amount"),
        note=payload.note,
    )
    return Status(201, _serialize_note(_reload_note(note), include_cost=True))


@router.delete(
    "/supplier-credit-notes/{note_id}/allocations/{allocation_id}/",
    response={204: None},
    summary="Remove a draft allocation",
)
def delete_credit_allocation(request: HttpRequest, note_id: int, allocation_id: int) -> Status[Any]:
    actor = _actor(request)
    note = resolve_supplier_credit_note(actor, note_id)
    require_organization_permission(actor, CREATE_SUPPLIER_CREDIT_NOTE, note.organization)
    allocation = resolve_credit_allocation(actor, credit_note=note, allocation_id=allocation_id)
    remove_credit_allocation(allocation=allocation)
    return Status(204, None)


@router.post(
    "/supplier-credit-notes/{note_id}/post/",
    response=SupplierCreditNoteOut,
    summary="Post a supplier credit note",
)
def post_credit_note(request: HttpRequest, note_id: int) -> Any:
    actor = _actor(request)
    note = resolve_supplier_credit_note(actor, note_id)
    require_organization_permission(actor, POST_SUPPLIER_CREDIT_NOTE, note.organization)
    posted = post_supplier_credit_note(credit_note=note, actor=actor)
    return _serialize_note(posted, include_cost=True)


@router.post(
    "/supplier-credit-notes/{note_id}/reverse/",
    response=SupplierCreditNoteOut,
    summary="Reverse a posted supplier credit note",
)
def reverse_credit_note(request: HttpRequest, note_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    note = resolve_supplier_credit_note(actor, note_id)
    require_organization_permission(actor, REVERSE_SUPPLIER_CREDIT_NOTE, note.organization)
    reversed_note = reverse_supplier_credit_note(
        credit_note=note, actor=actor, reason=payload.reason
    )
    return _serialize_note(reversed_note, include_cost=True)


def _reload_note(note: SupplierCreditNote) -> SupplierCreditNote:
    """Re-read after an allocation change, so the response carries stored rows."""
    return SupplierCreditNote.objects.select_related(
        "supplier", "supplier_return", "journal_entry"
    ).get(pk=note.pk)


# ---------------------------------------------------------------------------
# Supplier payments (Task 2.15)
# ---------------------------------------------------------------------------


class PaymentAllocationOut(Schema):
    id: int
    sequence: int
    invoice_id: int
    invoice_number: str
    allocated_amount: str
    note: str


class SupplierPaymentOut(Schema):
    id: int
    public_id: str
    organization_id: int
    branch_id: int
    supplier_id: int
    supplier_code: str
    number: str
    status: str
    method: str
    paid_at: str
    business_date: str
    reference: str
    journal_entry: str | None = None
    reversal_journal_entry: str | None = None
    #: Omitted entirely without `view_supplier_cost`, never blanked.
    amount: str | None = None
    allocated: str | None = None
    advance: str | None = None
    allocations: list[PaymentAllocationOut] = []


class SupplierPaymentIn(Schema):
    branch_id: int
    supplier_id: int
    paid_at: str
    method: str
    amount: str
    business_date: str | None = None
    reference: str = ""
    notes: str = ""


class PaymentAllocationIn(Schema):
    invoice_id: int
    allocated_amount: str
    note: str = ""


def _require_payment_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SUPPLIER_PAYMENT):
        raise PermissionMissing(f"{VIEW_SUPPLIER_PAYMENT} is not held.")
    return actor


def _serialize_payment(payment: SupplierPayment, *, include_cost: bool) -> dict[str, Any]:
    from apps.procurement.payments import advance_remainder
    from apps.procurement.payments import allocated_total as payment_allocated_total

    allocations = list(payment.allocations.select_related("invoice").order_by("sequence"))
    payload: dict[str, Any] = {
        "id": payment.pk,
        "public_id": str(payment.public_id),
        "organization_id": payment.organization_id,
        "branch_id": payment.branch_id,
        "supplier_id": payment.supplier_id,
        "supplier_code": payment.supplier.code,
        "number": payment.number,
        "status": payment.status,
        "method": payment.method,
        "paid_at": payment.paid_at.isoformat(),
        "business_date": payment.business_date.isoformat(),
        "reference": payment.reference,
        "journal_entry": (payment.journal_entry.entry_number if payment.journal_entry else None),
        "reversal_journal_entry": (
            payment.reversal_journal_entry.entry_number if payment.reversal_journal_entry else None
        ),
        "allocations": [
            {
                "id": row.pk,
                "sequence": row.sequence,
                "invoice_id": row.invoice_id,
                "invoice_number": row.invoice.number,
                "allocated_amount": format(row.allocated_amount, "f") if include_cost else "",
                "note": row.note,
            }
            for row in allocations
        ],
    }
    if include_cost:
        payload.update(
            {
                "amount": format(payment.amount, "f"),
                "allocated": format(payment_allocated_total(payment), "f"),
                "advance": format(advance_remainder(payment), "f"),
            }
        )
    return payload


@router.get(
    "/supplier-payments/", response=list[SupplierPaymentOut], summary="List supplier payments"
)
def list_supplier_payments(request: HttpRequest, status: str | None = None) -> Any:
    actor = _require_payment_view(request)
    queryset = visible_supplier_payments(actor)
    if status:
        queryset = queryset.filter(status=status.strip().upper())
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    return [_serialize_payment(row, include_cost=include_cost) for row in queryset.order_by("-id")]


@router.get(
    "/supplier-payments/{payment_id}/",
    response=SupplierPaymentOut,
    summary="Read one supplier payment",
)
def read_supplier_payment(request: HttpRequest, payment_id: int) -> Any:
    actor = _require_payment_view(request)
    payment = resolve_supplier_payment(actor, payment_id)
    return _serialize_payment(payment, include_cost=actor.has_perm(VIEW_SUPPLIER_COST))


@router.post(
    "/supplier-payments/",
    response={201: SupplierPaymentOut},
    summary="Open a draft payment",
)
def create_payment(request: HttpRequest, payload: SupplierPaymentIn) -> Status[Any]:
    actor = _actor(request)
    branch = resolve_branch(actor, payload.branch_id)
    require_organization_permission(actor, CREATE_SUPPLIER_PAYMENT, branch.organization)
    supplier = resolve_supplier(actor, payload.supplier_id)
    payment = create_supplier_payment(
        supplier=supplier,
        branch=branch,
        created_by=actor,
        paid_at=_required_date(payload.paid_at, field="paid_at"),
        method=payload.method,
        amount=_required_money(payload.amount, field="amount"),
        business_date=_date(payload.business_date, field="business_date"),
        reference=payload.reference,
        notes=payload.notes,
    )
    return Status(201, _serialize_payment(payment, include_cost=True))


@router.delete(
    "/supplier-payments/{payment_id}/", response={204: None}, summary="Discard a draft payment"
)
def delete_payment(request: HttpRequest, payment_id: int) -> Status[Any]:
    actor = _actor(request)
    payment = resolve_supplier_payment(actor, payment_id)
    require_organization_permission(actor, CREATE_SUPPLIER_PAYMENT, payment.organization)
    delete_supplier_payment(payment=payment)
    return Status(204, None)


@router.post(
    "/supplier-payments/{payment_id}/allocations/",
    response={201: SupplierPaymentOut},
    summary="Point part of the payment at a posted invoice",
)
def add_payment_allocation_endpoint(
    request: HttpRequest, payment_id: int, payload: PaymentAllocationIn
) -> Status[Any]:
    actor = _actor(request)
    payment = resolve_supplier_payment(actor, payment_id)
    require_organization_permission(actor, CREATE_SUPPLIER_PAYMENT, payment.organization)
    invoice = resolve_supplier_invoice(actor, payload.invoice_id)
    add_payment_allocation(
        payment=payment,
        invoice=invoice,
        allocated_amount=_required_money(payload.allocated_amount, field="allocated_amount"),
        note=payload.note,
    )
    return Status(201, _serialize_payment(_reload_payment(payment), include_cost=True))


@router.delete(
    "/supplier-payments/{payment_id}/allocations/{allocation_id}/",
    response={204: None},
    summary="Remove a draft allocation",
)
def delete_payment_allocation(
    request: HttpRequest, payment_id: int, allocation_id: int
) -> Status[Any]:
    actor = _actor(request)
    payment = resolve_supplier_payment(actor, payment_id)
    require_organization_permission(actor, CREATE_SUPPLIER_PAYMENT, payment.organization)
    allocation = resolve_payment_allocation(actor, payment=payment, allocation_id=allocation_id)
    remove_payment_allocation(allocation=allocation)
    return Status(204, None)


@router.post(
    "/supplier-payments/{payment_id}/post/",
    response=SupplierPaymentOut,
    summary="Post a supplier payment",
)
def post_payment(request: HttpRequest, payment_id: int) -> Any:
    actor = _actor(request)
    payment = resolve_supplier_payment(actor, payment_id)
    require_organization_permission(actor, POST_SUPPLIER_PAYMENT, payment.organization)
    posted = post_supplier_payment(payment=payment, actor=actor)
    return _serialize_payment(posted, include_cost=True)


@router.post(
    "/supplier-payments/{payment_id}/reverse/",
    response=SupplierPaymentOut,
    summary="Reverse a posted supplier payment",
)
def reverse_payment(request: HttpRequest, payment_id: int, payload: ReasonIn) -> Any:
    actor = _actor(request)
    payment = resolve_supplier_payment(actor, payment_id)
    require_organization_permission(actor, REVERSE_SUPPLIER_PAYMENT, payment.organization)
    reversed_payment = reverse_supplier_payment(payment=payment, actor=actor, reason=payload.reason)
    return _serialize_payment(reversed_payment, include_cost=True)


def _reload_payment(payment: SupplierPayment) -> SupplierPayment:
    """Re-read after an allocation change, so the response carries stored rows."""
    return SupplierPayment.objects.select_related("supplier", "journal_entry").get(pk=payment.pk)
