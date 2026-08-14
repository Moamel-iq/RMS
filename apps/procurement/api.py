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

from apps.accounting.models import Account, CostCenter
from apps.inventory.selectors import resolve_item, resolve_package_unit
from apps.organizations.authorization import (
    OutOfScope,
    PermissionMissing,
    require_organization_permission,
    require_reachable_organization_permission,
    resolve_branch,
    resolve_organization,
)
from apps.procurement.invoices import (
    add_account_line,
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    delete_supplier_invoice,
    outstanding_amount,
    post_supplier_invoice,
    reverse_supplier_invoice,
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
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierItem,
)
from apps.procurement.permissions import (
    APPROVE_SUPPLIER_INVOICE,
    CANCEL_PURCHASE_MATCH,
    CREATE_SUPPLIER_INVOICE,
    MANAGE_SUPPLIER_ITEMS,
    MANAGE_SUPPLIERS,
    MATCH_SUPPLIER_INVOICE,
    POST_SUPPLIER_INVOICE,
    REVERSE_SUPPLIER_INVOICE,
    VIEW_PURCHASE_MATCH,
    VIEW_SUPPLIER,
    VIEW_SUPPLIER_COST,
    VIEW_SUPPLIER_INVOICE,
    VIEW_SUPPLIER_ITEM,
)
from apps.procurement.selectors import (
    resolve_match_allocation,
    resolve_purchase_match,
    resolve_supplier,
    resolve_supplier_invoice,
    resolve_supplier_item,
    visible_goods_receipts,
    visible_purchase_matches,
    visible_purchase_orders,
    visible_supplier_invoices,
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
    invoice_date: str
    business_date: str
    due_date: str
    payment_terms_days: int
    status: str
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
    freight_amount: str | None = None
    discount_amount: str | None = None
    notes: str = ""


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
        "invoice_date": invoice.invoice_date.isoformat(),
        "business_date": invoice.business_date.isoformat(),
        "due_date": invoice.due_date.isoformat(),
        "payment_terms_days": invoice.payment_terms_days,
        "status": invoice.status,
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
def list_supplier_invoices(request: HttpRequest, status: str | None = None) -> Any:
    actor = _require_invoice_view(request)
    queryset = visible_supplier_invoices(actor)
    if status:
        queryset = queryset.filter(status=status.strip().upper())
    include_cost = actor.has_perm(VIEW_SUPPLIER_COST)
    return [
        _serialize_invoice(invoice, include_cost=include_cost)
        for invoice in queryset.order_by("-id")
    ]


@router.get(
    "/supplier-invoices/{invoice_id}/",
    response=SupplierInvoiceOut,
    summary="Read one supplier invoice",
)
def read_supplier_invoice(request: HttpRequest, invoice_id: int) -> Any:
    actor = _require_invoice_view(request)
    invoice = resolve_supplier_invoice(actor, invoice_id)
    return _serialize_invoice(invoice, include_cost=actor.has_perm(VIEW_SUPPLIER_COST))


@router.post(
    "/supplier-invoices/",
    response={201: SupplierInvoiceOut},
    summary="Record a supplier invoice",
)
def create_invoice(request: HttpRequest, payload: SupplierInvoiceIn) -> Status[Any]:
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
        freight_amount=_money(payload.freight_amount, field="freight_amount"),
        discount_amount=_money(payload.discount_amount, field="discount_amount"),
        notes=payload.notes,
    )
    return Status(201, _serialize_invoice(invoice, include_cost=True))


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
) -> Status[Any]:
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
    return Status(201, _serialize_invoice(_reload(invoice), include_cost=True))


@router.post(
    "/supplier-invoices/{invoice_id}/lines/account/",
    response={201: SupplierInvoiceOut},
    summary="Add a direct expense line",
)
def add_invoice_account_line(
    request: HttpRequest, invoice_id: int, payload: InvoiceAccountLineIn
) -> Status[Any]:
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
    return Status(201, _serialize_invoice(_reload(invoice), include_cost=True))


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
    return _serialize_invoice(approved, include_cost=True)


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
    return _serialize_invoice(posted, include_cost=True)


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
    return _serialize_invoice(reversed_invoice, include_cost=True)


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
