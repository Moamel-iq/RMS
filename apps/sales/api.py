"""
The Sales API, under `/api/v1/sales/`.

Four rules, inherited unchanged from `apps/kitchen/api.py` and restated here
because Phase 4 is where breaking one would cost the most:

* **No writable path skips a service.** Every mutation calls a function in
  `apps/sales/`; nothing here calls `Model.objects.create` and nothing here
  computes a figure. A journal, an application receivable and a
  theoretical-consumption contribution all follow from one call, and an API that
  wrote a row directly would produce the first without the other two.
* **An identifier never widens access.** Everything is resolved through
  `apps/sales/selectors.py`, which filters by the caller's own scope, so another
  organization's sales day is a **404** and not a 403. A 403 would confirm the
  document exists, and ids are sequential.
* **Money, quantities and rates cross the boundary as exact strings**, both
  directions. JSON's only numeric type is binary floating point, and a
  commission of `22499.999999999996` would be nobody's fault and everybody's
  problem.
* **Cost keys are omitted, never nulled.** `view_sales_cost` decides whether a
  payload carries cost at all. A `null` says a number exists and that the caller
  is not trusted with it, which is a different statement from the one intended.

## Commands, not CRUD

This is a posting module, so the writable half is named for the **transition**
rather than for the verb: `POST /days/{id}/post`, not `PATCH /days/{id}`. There
is **no `PATCH` and no `DELETE`** on anything that has left `DRAFT` or `OPEN`,
because a posted day is frozen by a database trigger and a verb that implied
otherwise would be the API contradicting it.

Two transitions appear here that the checkpoint's route list does not name —
`POST /settlements/{id}/return` and `POST /shifts/{id}/reopen`. Both are the
*documented way back* from a state the API can otherwise reach and never leave,
both exist as services with their own permissions, and both are on the screens.
Omitting them would have left an API caller able to reconcile a settlement and
unable to withdraw it.

## Scope and authority are two questions and both are asked

The selector says which rows this caller can see; `require_branch_permission` and
`require_organization_permission` say what they may do with them (ADR-016). A
reversal takes the *organization-wide* check rather than the branch one, exactly
as the screens do: undoing a posted economic event is supervisory, and authority
over one branch is authority over a part of something that has no parts.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja import Router, Schema, Status

from apps.organizations.authorization import (
    OutOfScope,
    PermissionMissing,
    has_organization_master_data_permission,
    has_organization_permission,
    require_branch_permission,
    require_organization_permission,
    require_reachable_organization_permission,
    resolve_branch,
    resolve_organization,
)
from apps.sales.adjustment_posting import post_sales_adjustment, reverse_sales_adjustment
from apps.sales.adjustment_services import (
    add_adjustment_line,
    create_sales_adjustment,
)
from apps.sales.adjustment_services import (
    totals_for as adjustment_totals_for,
)
from apps.sales.daily_reconciliation import reconcile_range
from apps.sales.dashboard import (
    DashboardScope,
    application_mix,
    cashier_summary,
    channel_mix,
    cost_summary,
    headline_for,
    receivable_summary,
    returns_breakdown,
    top_menu_items,
)
from apps.sales.day_services import (
    add_sales_line,
    create_sales_day,
    return_sales_day_to_draft,
    set_tender_summary,
    submit_sales_day,
)
from apps.sales.day_services import (
    totals_for as day_totals_for,
)
from apps.sales.models import (
    ApplicationReceivableEntry,
    CashierShift,
    DeliveryApplicationSettlement,
    SalesAdjustment,
    SalesDay,
)
from apps.sales.permissions import (
    APPROVE_CASHIER_CLOSING,
    CLOSE_CASHIER_SHIFT,
    CREATE_DAILY_SALES,
    MANAGE_APPLICATION_SETTLEMENTS,
    MANAGE_SALES_ADJUSTMENTS,
    POST_DAILY_SALES,
    REVERSE_DAILY_SALES,
    SUBMIT_DAILY_SALES,
    VIEW_APPLICATION_RECEIVABLES,
    VIEW_SALES,
    VIEW_SALES_COST,
    VIEW_SALES_REPORTS,
)
from apps.sales.posting import post_sales_day, reverse_sales_day
from apps.sales.receivables import ledger_for
from apps.sales.selectors import (
    resolve_delivery_application,
    resolve_receivable_entry,
    resolve_sales_day_line,
    visible_agreements,
    visible_cashier_shifts,
    visible_delivery_applications,
    visible_discount_programs,
    visible_menu_items,
    visible_menu_prices,
    visible_sales_adjustments,
    visible_sales_channels,
    visible_sales_days,
    visible_settlements,
)
from apps.sales.settlement_posting import post_settlement, reverse_settlement
from apps.sales.settlement_services import (
    add_settlement_adjustment,
    allocate_entry,
    create_settlement,
    reconcile_settlement,
    return_settlement_to_draft,
    three_way_for,
)
from apps.sales.shift_posting import approve_cashier_shift, reverse_cashier_shift
from apps.sales.shift_services import (
    close_cashier_shift,
    expected_by_tender,
    open_cashier_shift,
    reopen_cashier_shift,
    set_tender_count,
)
from apps.users.models import User

router = Router(tags=["sales"])

#: How many rows a list endpoint returns at most. Bounded rather than paged,
#: because every list here is filtered by branch and date and an unbounded read
#: over a year of trading is a question nobody meant to ask.
PAGE_LIMIT = 200


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _actor(request: HttpRequest) -> User:
    """The signed-in caller. `django_auth` has already refused anonymity."""
    user: User = request.user  # type: ignore[assignment]
    return user


def _require_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SALES):
        raise PermissionMissing("view_sales is not held.")
    return actor


def _require_reports(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_SALES_REPORTS):
        raise PermissionMissing("view_sales_reports is not held.")
    return actor


def _decimal(value: str | None, field: str) -> Decimal | None:
    """
    One exact Decimal out of a string, or a refusal naming the field.

    `Decimal("1.2e400")` and `Decimal("nan")` both parse and neither is money,
    so they are refused here rather than reaching a service that would store
    them. A malformed figure is the caller's to fix, which is why it is a 422.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation, ValueError, TypeError:
        raise ValidationError(
            _("%(field)s must be a decimal sent as a string.") % {"field": field},
            code="invalid_decimal",
        ) from None
    if not parsed.is_finite():
        raise ValidationError(
            _("%(field)s must be a finite decimal.") % {"field": field}, code="invalid_decimal"
        )
    return parsed


def _required_decimal(value: str | None, field: str) -> Decimal:
    parsed = _decimal(value, field)
    if parsed is None:
        raise ValidationError(
            _("%(field)s is required.") % {"field": field}, code="invalid_decimal"
        )
    return parsed


def _by_public_id(rows: QuerySet[Any], public_id: str, label: str) -> Any:
    """
    Resolve a public id **inside** a selector's queryset.

    The queryset arrives already narrowed to the caller's own scope, so a
    document belonging to another organization is simply not in it and the
    answer is the same 404 a document that never existed gets. This is the
    fetch-then-check rule kept: there is no moment where an out-of-scope row
    exists in a local variable.
    """
    try:
        identity = uuid.UUID(str(public_id))
    except ValueError, AttributeError, TypeError:
        raise OutOfScope(f"{label} {public_id} does not exist.") from None
    row = rows.filter(public_id=identity).first()
    if row is None:
        raise OutOfScope(f"{label} {public_id} does not exist.")
    return row


def _may_read_cost(actor: User, organization: Any) -> bool:
    return has_organization_master_data_permission(actor, VIEW_SALES_COST, organization)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _date(value: datetime.date | None) -> str | None:
    return None if value is None else value.isoformat()


# ---------------------------------------------------------------------------
# Schemas — reads
# ---------------------------------------------------------------------------


class MenuItemOut(Schema):
    id: int
    public_id: str
    code: str
    name_ar: str
    name_en: str
    organization_id: int
    category_code: str | None
    recipe_code: str | None
    serving_code: str
    fulfillment_source: str
    is_active: bool


class MenuPriceOut(Schema):
    id: int
    public_id: str
    menu_item_code: str
    branch_code: str
    scope: str
    channel_code: str | None
    delivery_application_code: str | None
    unit_price: str
    effective_from: str
    effective_to: str | None
    is_active: bool


class ChannelOut(Schema):
    id: int
    public_id: str
    code: str
    name_ar: str
    category: str
    default_tender: str
    cost_center_code: str
    requires_cashier: bool
    requires_delivery_application: bool
    is_active: bool


class ApplicationOut(Schema):
    id: int
    public_id: str
    code: str
    name_ar: str
    settlement_cycle_days: int
    receivable_account_code: str | None
    is_active: bool


class AgreementOut(Schema):
    id: int
    public_id: str
    branch_code: str
    delivery_application_code: str
    commission_basis: str
    commission_percent: str
    fixed_fee_per_order: str
    settlement_lag_days: int
    effective_from: str
    effective_to: str | None


class DiscountOut(Schema):
    id: int
    public_id: str
    code: str
    name_ar: str
    discount_percent: str | None
    discount_amount: str | None
    restaurant_funded_share: str
    application_funded_share: str
    branch_code: str | None
    channel_code: str | None
    delivery_application_code: str | None
    menu_item_code: str | None
    effective_from: str
    effective_to: str | None


class DayOut(Schema):
    id: int
    public_id: str
    number: str
    organization_id: int
    branch_code: str
    business_date: str
    status: str
    line_count: int
    gross: str
    restaurant_discount: str
    application_discount: str
    commission: str
    other_fees: str
    net_cash: str
    net_card: str
    net_application: str


class DayLineOut(Schema):
    id: int
    public_id: str
    sequence: int
    menu_item_code: str
    channel_code: str
    delivery_application_code: str | None
    recipe_code: str
    recipe_version: int
    serving_code: str
    unit_price: str
    quantity: str
    order_count: int
    gross_amount: str
    restaurant_discount: str
    application_discount: str
    commission_basis: str
    commission_amount: str
    other_fee_amount: str
    customer_charge: str
    net_amount: str


class TenderOut(Schema):
    tender: str
    declared_amount: str


class DayDetailOut(DayOut):
    lines: list[DayLineOut]
    tenders: list[TenderOut]


class AdjustmentOut(Schema):
    id: int
    public_id: str
    number: str
    branch_code: str
    sales_day_public_id: str
    business_date: str
    reason_kind: str
    status: str
    reason: str
    evidence_reference: str
    line_count: int
    gross: str
    restaurant_discount: str
    application_discount: str
    commission: str
    other_fees: str
    net_cash: str
    net_card: str
    net_application: str


class AdjustmentLineOut(Schema):
    id: int
    public_id: str
    sequence: int
    original_line_id: int
    menu_item_code: str
    adjusted_quantity: str
    unit_price: str
    adjusted_gross: str
    adjusted_restaurant_discount: str
    adjusted_application_discount: str
    adjusted_commission: str
    adjusted_other_fees: str
    adjusted_customer_charge: str
    adjusted_net_amount: str
    line_reason: str


class AdjustmentDetailOut(AdjustmentOut):
    lines: list[AdjustmentLineOut]


class ReceivableEntryOut(Schema):
    id: int
    business_date: str
    source: str
    source_document_type: str
    source_document_id: str
    debit: str
    credit: str
    narration: str


class AgingBucketOut(Schema):
    label: str
    days_from: int
    days_to: int | None
    amount: str


class ReceivablePositionOut(Schema):
    delivery_application_code: str
    delivery_application_public_id: str
    balance: str
    oldest_open_date: str | None
    expected_settlement_date: str | None
    buckets: list[AgingBucketOut]


class ReceivableDetailOut(Schema):
    position: ReceivablePositionOut
    entries: list[ReceivableEntryOut]


class SettlementOut(Schema):
    id: int
    public_id: str
    number: str
    branch_code: str
    delivery_application_code: str
    period_start: str
    period_end: str
    business_date: str
    statement_reference: str
    statement_date: str
    status: str
    remittance_destination: str
    expected_amount: str
    statement_amount: str
    remitted_amount: str
    statement_commission_amount: str


class ThreeWayOut(Schema):
    expected: str
    statement: str
    remitted: str
    statement_gap: str
    remittance_gap: str
    explained_statement: str
    explained_remittance: str
    unexplained_statement: str
    unexplained_remittance: str
    total_variance: str
    accrued_commission: str
    statement_commission: str
    commission_gap: str
    is_reconcilable: bool


class AllocationOut(Schema):
    id: int
    receivable_entry_id: int
    business_date: str
    allocated_amount: str


class SettlementAdjustmentOut(Schema):
    id: int
    leg: str
    reason: str
    amount: str
    explanation: str
    approved_by: str | None


class SettlementDetailOut(SettlementOut):
    three_way: ThreeWayOut
    allocations: list[AllocationOut]
    adjustments: list[SettlementAdjustmentOut]


class TenderCountOut(Schema):
    tender: str
    expected_amount: str
    counted_amount: str


class ShiftOut(Schema):
    id: int
    public_id: str
    number: str
    branch_code: str
    business_date: str
    cashier: str
    status: str
    opening_float: str
    expected_cash: str
    counted_cash: str
    variance_amount: str
    sales_day_public_id: str | None
    closed_by: str | None
    approved_by: str | None


class ShiftDetailOut(ShiftOut):
    counts: list[TenderCountOut]
    expected_by_tender: dict[str, str]


class ReconciliationLegOut(Schema):
    tender: str
    declared: str
    derived: str
    difference: str


class FindingOut(Schema):
    severity: str
    code: str
    message: str


class ReconciliationOut(Schema):
    branch_code: str
    business_date: str
    sales_day_public_id: str | None
    shift_public_id: str | None
    legs: list[ReconciliationLegOut]
    counted_cash: str | None
    cash_variance: str | None
    adjustments_total: str
    receivable_movement: str
    cancelled_quantity: str
    is_clean: bool
    findings: list[FindingOut]


class MixRowOut(Schema):
    code: str
    label: str
    gross: str
    net: str
    quantity: str
    line_count: int
    share: str


class DashboardOut(Schema):
    organization_id: int
    date_from: str
    date_to: str
    gross: str
    restaurant_discount: str
    application_discount: str
    commission: str
    other_fees: str
    returns_gross: str
    net_revenue: str
    cash_sales: str
    card_sales: str
    application_sales: str
    day_count: int
    line_count: int
    receivable_outstanding: str
    receivable_overdue: str
    cashier_shortage: str
    cashier_overage: str
    channels: list[MixRowOut]
    applications: list[MixRowOut]
    top_items: list[MixRowOut]
    returns: list[MixRowOut]


class DashboardCostOut(Schema):
    """
    The cost half, on its own route and behind its own permission.

    Split off rather than made optional on `DashboardOut`, and the reason is a
    property of the serializer: a response schema fills an unset optional field
    with `null`, and a `null` food cost says a number exists and that this
    caller is not trusted with it — which is the statement the module refuses to
    make. Absence has to be structural, so it is a different route, exactly as
    `apps/kitchen/api.py` guards its costing endpoints.
    """

    organization_id: int
    date_from: str
    date_to: str
    costed_gross: str
    costed_net: str
    food_cost: str
    gross_profit: str
    food_cost_percent: str
    margin_percent: str
    costed_lines: int
    uncosted_lines: int
    uncosted_gross: str
    is_complete: bool


# ---------------------------------------------------------------------------
# Schemas — commands
# ---------------------------------------------------------------------------


class DayIn(Schema):
    organization_id: int
    branch_id: int
    business_date: datetime.date
    notes: str = ""


class DayLineIn(Schema):
    menu_item_id: int
    channel_id: int
    quantity: str
    delivery_application_id: int | None = None
    order_count: int = 0
    discount_program_id: int | None = None
    manual_discount_amount: str | None = None
    manual_discount_reason: str = ""
    other_fee_amount: str = "0"
    notes: str = ""


class TenderIn(Schema):
    tender: str
    declared_amount: str
    notes: str = ""


class ReasonIn(Schema):
    reason: str = ""


class AdjustmentIn(Schema):
    sales_day_public_id: str
    reason_kind: str
    business_date: datetime.date
    reason: str
    evidence_reference: str
    notes: str = ""


class AdjustmentLineIn(Schema):
    original_line_id: int
    adjusted_quantity: str = "0"
    adjusted_gross: str | None = None
    line_reason: str = ""


class SettlementIn(Schema):
    organization_id: int
    branch_id: int
    delivery_application_id: int
    period_start: datetime.date
    period_end: datetime.date
    business_date: datetime.date
    statement_reference: str
    statement_date: datetime.date
    statement_amount: str
    remitted_amount: str
    statement_commission_amount: str = "0"
    remittance_destination: str
    evidence_reference: str
    notes: str = ""


class AllocationIn(Schema):
    receivable_entry_id: int
    allocated_amount: str


class SettlementAdjustmentIn(Schema):
    leg: str
    reason: str
    amount: str
    explanation: str = ""
    #: The approver is named by id rather than implied by the caller, because
    #: `UNEXPLAINED_APPROVED` requires somebody to have decided and the caller
    #: recording it may not be that person. Whoever is named must be able to
    #: exercise `manage_application_settlements` over this settlement's
    #: organization — see `_resolve_settlement_approver` for why a global user
    #: lookup here was a way to forge an approval.
    approver_id: int | None = None


class ShiftIn(Schema):
    organization_id: int
    branch_id: int
    business_date: datetime.date
    cashier_id: int
    opening_float: str = "0"
    notes: str = ""


class CountIn(Schema):
    tender: str
    counted_amount: str
    notes: str = ""


class CloseIn(Schema):
    sales_day_public_id: str
    notes: str = ""


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _menu_item_out(item: Any) -> dict[str, Any]:
    return {
        "id": item.pk,
        "public_id": str(item.public_id),
        "code": item.code,
        "name_ar": item.name_ar,
        "name_en": item.name_en,
        "organization_id": item.organization_id,
        "category_code": item.category.code if item.category_id else None,
        "recipe_code": item.recipe.code if item.recipe_id else None,
        "serving_code": item.serving_code,
        "fulfillment_source": item.fulfillment_source,
        "is_active": item.is_active,
    }


def _price_out(price: Any) -> dict[str, Any]:
    return {
        "id": price.pk,
        "public_id": str(price.public_id),
        "menu_item_code": price.menu_item.code,
        "branch_code": price.branch.code,
        "scope": price.scope,
        "channel_code": price.channel.code if price.channel_id else None,
        "delivery_application_code": (
            price.delivery_application.code if price.delivery_application_id else None
        ),
        "unit_price": str(price.unit_price),
        "effective_from": price.effective_from.isoformat(),
        "effective_to": _date(price.effective_to),
        "is_active": price.is_active,
    }


def _channel_out(channel: Any) -> dict[str, Any]:
    return {
        "id": channel.pk,
        "public_id": str(channel.public_id),
        "code": channel.code,
        "name_ar": channel.name_ar,
        "category": channel.category,
        "default_tender": channel.default_tender,
        "cost_center_code": channel.cost_center.code,
        "requires_cashier": channel.requires_cashier,
        "requires_delivery_application": channel.requires_delivery_application,
        "is_active": channel.is_active,
    }


def _application_out(application: Any) -> dict[str, Any]:
    return {
        "id": application.pk,
        "public_id": str(application.public_id),
        "code": application.code,
        "name_ar": application.name_ar,
        "settlement_cycle_days": application.settlement_cycle_days,
        "receivable_account_code": (
            application.receivable_account.code if application.receivable_account_id else None
        ),
        "is_active": application.is_active,
    }


def _agreement_out(agreement: Any) -> dict[str, Any]:
    return {
        "id": agreement.pk,
        "public_id": str(agreement.public_id),
        "branch_code": agreement.branch.code,
        "delivery_application_code": agreement.delivery_application.code,
        "commission_basis": agreement.commission_basis,
        "commission_percent": str(agreement.commission_percent),
        "fixed_fee_per_order": str(agreement.fixed_fee_per_order),
        "settlement_lag_days": agreement.settlement_lag_days,
        "effective_from": agreement.effective_from.isoformat(),
        "effective_to": _date(agreement.effective_to),
    }


def _discount_out(program: Any) -> dict[str, Any]:
    return {
        "id": program.pk,
        "public_id": str(program.public_id),
        "code": program.code,
        "name_ar": program.name_ar,
        "discount_percent": _money(program.discount_percent),
        "discount_amount": _money(program.discount_amount),
        "restaurant_funded_share": str(program.restaurant_funded_share),
        "application_funded_share": str(program.application_funded_share),
        "branch_code": program.branch.code if program.branch_id else None,
        "channel_code": program.channel.code if program.channel_id else None,
        "delivery_application_code": (
            program.delivery_application.code if program.delivery_application_id else None
        ),
        "menu_item_code": program.menu_item.code if program.menu_item_id else None,
        "effective_from": program.effective_from.isoformat(),
        "effective_to": _date(program.effective_to),
    }


def _day_out(day: SalesDay) -> dict[str, Any]:
    totals = day_totals_for(day)
    return {
        "id": day.pk,
        "public_id": str(day.public_id),
        "number": day.number,
        "organization_id": day.organization_id,
        "branch_code": day.branch.code,
        "business_date": day.business_date.isoformat(),
        "status": day.status,
        "line_count": totals.line_count,
        "gross": str(totals.gross),
        "restaurant_discount": str(totals.restaurant_discount),
        "application_discount": str(totals.application_discount),
        "commission": str(totals.commission),
        "other_fees": str(totals.other_fees),
        "net_cash": str(totals.net_cash),
        "net_card": str(totals.net_card),
        "net_application": str(totals.net_application),
    }


def _day_line_out(line: Any) -> dict[str, Any]:
    return {
        "id": line.pk,
        "public_id": str(line.public_id),
        "sequence": line.sequence,
        "menu_item_code": line.menu_item.code,
        "channel_code": line.channel.code,
        "delivery_application_code": (
            line.delivery_application.code if line.delivery_application_id else None
        ),
        "recipe_code": line.recipe.code,
        "recipe_version": line.recipe_version.version_number,
        "serving_code": line.serving.code,
        "unit_price": str(line.unit_price),
        "quantity": str(line.quantity),
        "order_count": line.order_count,
        "gross_amount": str(line.gross_amount),
        "restaurant_discount": str(line.restaurant_discount),
        "application_discount": str(line.application_discount),
        "commission_basis": line.commission_basis,
        "commission_amount": str(line.commission_amount),
        "other_fee_amount": str(line.other_fee_amount),
        "customer_charge": str(line.customer_charge),
        "net_amount": str(line.net_amount),
    }


def _day_detail_out(day: SalesDay) -> dict[str, Any]:
    payload = _day_out(day)
    payload["lines"] = [
        _day_line_out(line)
        for line in day.lines.select_related(
            "menu_item", "channel", "delivery_application", "recipe", "recipe_version", "serving"
        ).order_by("sequence")
    ]
    payload["tenders"] = [
        {"tender": row.tender, "declared_amount": str(row.declared_amount)}
        for row in day.tender_summaries.order_by("tender")
    ]
    return payload


def _adjustment_out(adjustment: SalesAdjustment) -> dict[str, Any]:
    totals = adjustment_totals_for(adjustment)
    return {
        "id": adjustment.pk,
        "public_id": str(adjustment.public_id),
        "number": adjustment.number,
        "branch_code": adjustment.branch.code,
        "sales_day_public_id": str(adjustment.sales_day.public_id),
        "business_date": adjustment.business_date.isoformat(),
        "reason_kind": adjustment.reason_kind,
        "status": adjustment.status,
        "reason": adjustment.reason,
        "evidence_reference": adjustment.evidence_reference,
        "line_count": totals.line_count,
        "gross": str(totals.gross),
        "restaurant_discount": str(totals.restaurant_discount),
        "application_discount": str(totals.application_discount),
        "commission": str(totals.commission),
        "other_fees": str(totals.other_fees),
        "net_cash": str(totals.net_cash),
        "net_card": str(totals.net_card),
        "net_application": str(totals.net_application),
    }


def _adjustment_detail_out(adjustment: SalesAdjustment) -> dict[str, Any]:
    payload = _adjustment_out(adjustment)
    payload["lines"] = [
        {
            "id": line.pk,
            "public_id": str(line.public_id),
            "sequence": line.sequence,
            "original_line_id": line.original_line_id,
            "menu_item_code": line.original_line.menu_item.code,
            "adjusted_quantity": str(line.adjusted_quantity),
            "unit_price": str(line.unit_price),
            "adjusted_gross": str(line.adjusted_gross),
            "adjusted_restaurant_discount": str(line.adjusted_restaurant_discount),
            "adjusted_application_discount": str(line.adjusted_application_discount),
            "adjusted_commission": str(line.adjusted_commission),
            "adjusted_other_fees": str(line.adjusted_other_fees),
            "adjusted_customer_charge": str(line.adjusted_customer_charge),
            "adjusted_net_amount": str(line.adjusted_net_amount),
            "line_reason": line.line_reason,
        }
        for line in adjustment.lines.select_related(
            "original_line", "original_line__menu_item"
        ).order_by("sequence")
    ]
    return payload


def _entry_out(entry: ApplicationReceivableEntry) -> dict[str, Any]:
    return {
        "id": entry.pk,
        "business_date": entry.business_date.isoformat(),
        "source": entry.source,
        "source_document_type": entry.source_document_type,
        "source_document_id": entry.source_document_id,
        "debit": str(entry.debit),
        "credit": str(entry.credit),
        "narration": entry.narration,
    }


def _position_out(position: Any) -> dict[str, Any]:
    return {
        "delivery_application_code": position.delivery_application.code,
        "delivery_application_public_id": str(position.delivery_application.public_id),
        "balance": str(position.balance),
        "oldest_open_date": _date(position.oldest_open_date),
        "expected_settlement_date": _date(position.expected_settlement_date),
        "buckets": [
            {
                "label": bucket.label,
                "days_from": bucket.days_from,
                "days_to": bucket.days_to,
                "amount": str(bucket.amount),
            }
            for bucket in position.buckets
        ],
    }


def _settlement_out(settlement: DeliveryApplicationSettlement) -> dict[str, Any]:
    return {
        "id": settlement.pk,
        "public_id": str(settlement.public_id),
        "number": settlement.number,
        "branch_code": settlement.branch.code,
        "delivery_application_code": settlement.delivery_application.code,
        "period_start": settlement.period_start.isoformat(),
        "period_end": settlement.period_end.isoformat(),
        "business_date": settlement.business_date.isoformat(),
        "statement_reference": settlement.statement_reference,
        "statement_date": settlement.statement_date.isoformat(),
        "status": settlement.status,
        "remittance_destination": settlement.remittance_destination,
        "expected_amount": str(settlement.expected_amount),
        "statement_amount": str(settlement.statement_amount),
        "remitted_amount": str(settlement.remitted_amount),
        "statement_commission_amount": str(settlement.statement_commission_amount),
    }


def _settlement_detail_out(settlement: DeliveryApplicationSettlement) -> dict[str, Any]:
    payload = _settlement_out(settlement)
    three_way = three_way_for(settlement)
    payload["three_way"] = {
        "expected": str(three_way.expected),
        "statement": str(three_way.statement),
        "remitted": str(three_way.remitted),
        "statement_gap": str(three_way.statement_gap),
        "remittance_gap": str(three_way.remittance_gap),
        "explained_statement": str(three_way.explained_statement),
        "explained_remittance": str(three_way.explained_remittance),
        "unexplained_statement": str(three_way.unexplained_statement),
        "unexplained_remittance": str(three_way.unexplained_remittance),
        "total_variance": str(three_way.total_variance),
        "accrued_commission": str(three_way.accrued_commission),
        "statement_commission": str(three_way.statement_commission),
        "commission_gap": str(three_way.commission_gap),
        "is_reconcilable": three_way.is_reconcilable,
    }
    payload["allocations"] = [
        {
            "id": row.pk,
            "receivable_entry_id": row.receivable_entry_id,
            "business_date": row.receivable_entry.business_date.isoformat(),
            "allocated_amount": str(row.allocated_amount),
        }
        for row in settlement.allocations.select_related("receivable_entry").order_by("pk")
    ]
    payload["adjustments"] = [
        {
            "id": row.pk,
            "leg": row.leg,
            "reason": row.reason,
            "amount": str(row.amount),
            "explanation": row.explanation,
            "approved_by": str(row.approved_by) if row.approved_by_id else None,
        }
        for row in settlement.adjustments.select_related("approved_by").order_by("pk")
    ]
    return payload


def _shift_out(shift: CashierShift) -> dict[str, Any]:
    return {
        "id": shift.pk,
        "public_id": str(shift.public_id),
        "number": shift.number,
        "branch_code": shift.branch.code,
        "business_date": shift.business_date.isoformat(),
        "cashier": str(shift.cashier),
        "status": shift.status,
        "opening_float": str(shift.opening_float),
        "expected_cash": str(shift.expected_cash),
        "counted_cash": str(shift.counted_cash),
        "variance_amount": str(shift.variance_amount),
        "sales_day_public_id": (
            str(shift.sales_day.public_id) if shift.sales_day is not None else None
        ),
        "closed_by": str(shift.closed_by) if shift.closed_by_id else None,
        "approved_by": str(shift.approved_by) if shift.approved_by_id else None,
    }


def _shift_detail_out(shift: CashierShift) -> dict[str, Any]:
    payload = _shift_out(shift)
    payload["counts"] = [
        {
            "tender": row.tender,
            "expected_amount": str(row.expected_amount),
            "counted_amount": str(row.counted_amount),
        }
        for row in shift.tender_counts.order_by("tender")
    ]
    payload["expected_by_tender"] = {
        tender: str(amount) for tender, amount in expected_by_tender(shift).items()
    }
    return payload


def _reconciliation_out(row: Any) -> dict[str, Any]:
    return {
        "branch_code": row.branch.code,
        "business_date": row.business_date.isoformat(),
        "sales_day_public_id": str(row.sales_day.public_id) if row.sales_day else None,
        "shift_public_id": str(row.shift.public_id) if row.shift else None,
        "legs": [
            {
                "tender": leg.tender,
                "declared": str(leg.declared),
                "derived": str(leg.derived),
                "difference": str(leg.difference),
            }
            for leg in row.legs
        ],
        "counted_cash": _money(row.counted_cash),
        "cash_variance": _money(row.cash_variance),
        "adjustments_total": str(row.adjustments_total),
        "receivable_movement": str(row.receivable_movement),
        "cancelled_quantity": str(row.cancelled_quantity),
        "is_clean": row.is_clean,
        "findings": [
            {"severity": finding.severity, "code": finding.code, "message": str(finding.message)}
            for finding in row.findings
        ],
    }


def _mix_out(row: Any) -> dict[str, Any]:
    return {
        "code": row.code,
        "label": str(row.label),
        "gross": str(row.gross),
        "net": str(row.net),
        "quantity": str(row.quantity),
        "line_count": row.line_count,
        "share": str(row.share),
    }


# ---------------------------------------------------------------------------
# Reads — master data
# ---------------------------------------------------------------------------


@router.get("/menu-items", response=list[MenuItemOut], summary="List menu items")
def list_menu_items(request: HttpRequest, q: str = "", active: bool | None = None) -> list[Any]:
    rows = visible_menu_items(_require_view(request))
    if q:
        rows = rows.filter(code__icontains=q) | rows.filter(name_ar__icontains=q)
    if active is not None:
        rows = rows.filter(is_active=active)
    return [_menu_item_out(row) for row in rows.order_by("code")[:PAGE_LIMIT]]


@router.get("/menu-items/{public_id}", response=MenuItemOut, summary="One menu item")
def get_menu_item(request: HttpRequest, public_id: str) -> Any:
    rows = visible_menu_items(_require_view(request))
    return _menu_item_out(_by_public_id(rows, public_id, "Menu item"))


@router.get("/menu-prices", response=list[MenuPriceOut], summary="List menu prices")
def list_menu_prices(request: HttpRequest, menu_item_id: int | None = None) -> list[Any]:
    rows = visible_menu_prices(_require_view(request))
    if menu_item_id is not None:
        rows = rows.filter(menu_item_id=menu_item_id)
    return [
        _price_out(row) for row in rows.order_by("menu_item__code", "-effective_from")[:PAGE_LIMIT]
    ]


@router.get("/channels", response=list[ChannelOut], summary="List sales channels")
def list_channels(request: HttpRequest) -> list[Any]:
    rows = visible_sales_channels(_require_view(request)).order_by("display_order", "code")
    return [_channel_out(row) for row in rows[:PAGE_LIMIT]]


@router.get("/applications", response=list[ApplicationOut], summary="List delivery applications")
def list_applications(request: HttpRequest) -> list[Any]:
    rows = visible_delivery_applications(_require_view(request)).order_by("code")
    return [_application_out(row) for row in rows[:PAGE_LIMIT]]


@router.get("/agreements", response=list[AgreementOut], summary="List commission agreements")
def list_agreements(request: HttpRequest, delivery_application_id: int | None = None) -> list[Any]:
    rows = visible_agreements(_require_view(request))
    if delivery_application_id is not None:
        rows = rows.filter(delivery_application_id=delivery_application_id)
    return [
        _agreement_out(row)
        for row in rows.order_by("delivery_application__code", "-effective_from")[:PAGE_LIMIT]
    ]


@router.get("/discounts", response=list[DiscountOut], summary="List discount programmes")
def list_discounts(request: HttpRequest) -> list[Any]:
    rows = visible_discount_programs(_require_view(request)).order_by("code")
    return [_discount_out(row) for row in rows[:PAGE_LIMIT]]


# ---------------------------------------------------------------------------
# Reads — the daily document
# ---------------------------------------------------------------------------


@router.get("/days", response=list[DayOut], summary="List sales days")
def list_days(
    request: HttpRequest,
    branch_id: int | None = None,
    status: str = "",
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> list[Any]:
    rows = visible_sales_days(_require_view(request))
    if branch_id is not None:
        rows = rows.filter(branch_id=branch_id)
    if status:
        rows = rows.filter(status=status)
    if date_from is not None:
        rows = rows.filter(business_date__gte=date_from)
    if date_to is not None:
        rows = rows.filter(business_date__lte=date_to)
    return [_day_out(row) for row in rows.order_by("-business_date", "branch__code")[:PAGE_LIMIT]]


@router.get("/days/{public_id}", response=DayDetailOut, summary="One sales day, with its lines")
def get_day(request: HttpRequest, public_id: str) -> Any:
    rows = visible_sales_days(_require_view(request))
    return _day_detail_out(_by_public_id(rows, public_id, "Sales day"))


# ---------------------------------------------------------------------------
# Reads — adjustments
# ---------------------------------------------------------------------------


@router.get("/adjustments", response=list[AdjustmentOut], summary="List adjustments")
def list_adjustments(request: HttpRequest, status: str = "", reason_kind: str = "") -> list[Any]:
    rows = visible_sales_adjustments(_require_view(request))
    if status:
        rows = rows.filter(status=status)
    if reason_kind:
        rows = rows.filter(reason_kind=reason_kind)
    return [_adjustment_out(row) for row in rows[:PAGE_LIMIT]]


@router.get("/adjustments/{public_id}", response=AdjustmentDetailOut, summary="One adjustment")
def get_adjustment(request: HttpRequest, public_id: str) -> Any:
    rows = visible_sales_adjustments(_require_view(request))
    return _adjustment_detail_out(_by_public_id(rows, public_id, "Sales adjustment"))


# ---------------------------------------------------------------------------
# Reads — the receivable ledger
# ---------------------------------------------------------------------------


@router.get(
    "/applications/{public_id}/receivable",
    response=ReceivableDetailOut,
    summary="One application's receivable ledger and aging",
)
def get_application_receivable(
    request: HttpRequest,
    public_id: str,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> Any:
    actor = _actor(request)
    application = _by_public_id(
        visible_delivery_applications(actor), public_id, "Delivery application"
    )
    require_reachable_organization_permission(
        actor, VIEW_APPLICATION_RECEIVABLES, application.organization
    )
    entries = ledger_for(
        actor, delivery_application=application, date_from=date_from, date_to=date_to
    )
    from apps.sales.receivables import positions_for_applications

    as_of = date_to or datetime.date.max
    positions = positions_for_applications(actor, [application], as_of=as_of)
    if not positions:  # pragma: no cover - one application always yields one row
        raise OutOfScope(f"Delivery application {public_id} does not exist.")
    return {
        "position": _position_out(positions[0]),
        "entries": [_entry_out(entry) for entry in entries[:PAGE_LIMIT]],
    }


# ---------------------------------------------------------------------------
# Reads — settlements and shifts
# ---------------------------------------------------------------------------


@router.get("/settlements", response=list[SettlementOut], summary="List application settlements")
def list_settlements(request: HttpRequest, status: str = "") -> list[Any]:
    rows = visible_settlements(_require_view(request))
    if status:
        rows = rows.filter(status=status)
    return [_settlement_out(row) for row in rows.order_by("-business_date", "-pk")[:PAGE_LIMIT]]


@router.get(
    "/settlements/{public_id}",
    response=SettlementDetailOut,
    summary="One settlement, with its three-way comparison",
)
def get_settlement(request: HttpRequest, public_id: str) -> Any:
    rows = visible_settlements(_require_view(request))
    return _settlement_detail_out(_by_public_id(rows, public_id, "Settlement"))


@router.get("/shifts", response=list[ShiftOut], summary="List cashier shifts")
def list_shifts(request: HttpRequest, branch_id: int | None = None, status: str = "") -> list[Any]:
    rows = visible_cashier_shifts(_require_view(request))
    if branch_id is not None:
        rows = rows.filter(branch_id=branch_id)
    if status:
        rows = rows.filter(status=status)
    return [_shift_out(row) for row in rows.order_by("-business_date", "branch__code")[:PAGE_LIMIT]]


@router.get("/shifts/{public_id}", response=ShiftDetailOut, summary="One cashier shift")
def get_shift(request: HttpRequest, public_id: str) -> Any:
    rows = visible_cashier_shifts(_require_view(request))
    return _shift_detail_out(_by_public_id(rows, public_id, "Cashier shift"))


# ---------------------------------------------------------------------------
# Reads — reports
# ---------------------------------------------------------------------------


@router.get(
    "/reports/daily-reconciliation",
    response=list[ReconciliationOut],
    summary="المطابقة اليومية — declared against derived against counted",
)
def get_daily_reconciliation(
    request: HttpRequest,
    date_from: datetime.date,
    date_to: datetime.date,
    branch_id: int | None = None,
) -> list[Any]:
    actor = _require_reports(request)
    branch_ids = [branch_id] if branch_id is not None else None
    rows = reconcile_range(actor, branch_ids=branch_ids, date_from=date_from, date_to=date_to)
    return [_reconciliation_out(row) for row in rows]


@router.get("/dashboard", response=DashboardOut, summary="لوحة المبيعات")
def get_dashboard(
    request: HttpRequest,
    organization_id: int,
    date_from: datetime.date,
    date_to: datetime.date,
    branch_id: int | None = None,
) -> Any:
    """
    Every dashboard figure in one payload, cost keys **omitted** without authority.

    Omitted rather than nulled, which is the same rule the screen follows: a
    `null` food cost says a number exists and that this caller is not trusted
    with it, and a client would render the two states identically.
    """
    actor = _require_reports(request)
    organization = resolve_organization(actor, organization_id)
    require_reachable_organization_permission(actor, VIEW_SALES_REPORTS, organization)

    branch_ids: tuple[int, ...] | None = None
    if branch_id is not None:
        branch_ids = (resolve_branch(actor, branch_id).pk,)

    scope = DashboardScope(
        organization_id=organization.pk,
        date_from=date_from,
        date_to=date_to,
        branch_ids=branch_ids,
    )
    headline = headline_for(actor, scope)
    receivables = receivable_summary(actor, scope)
    till = cashier_summary(actor, scope)
    payload: dict[str, Any] = {
        "organization_id": organization.pk,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "gross": str(headline.gross),
        "restaurant_discount": str(headline.restaurant_discount),
        "application_discount": str(headline.application_discount),
        "commission": str(headline.commission),
        "other_fees": str(headline.other_fees),
        "returns_gross": str(headline.returns_gross),
        "net_revenue": str(headline.net_revenue),
        "cash_sales": str(headline.cash_sales),
        "card_sales": str(headline.card_sales),
        "application_sales": str(headline.application_sales),
        "day_count": headline.day_count,
        "line_count": headline.line_count,
        "receivable_outstanding": str(receivables.outstanding),
        "receivable_overdue": str(receivables.overdue),
        "cashier_shortage": str(till.shortage),
        "cashier_overage": str(till.overage),
        "channels": [_mix_out(row) for row in channel_mix(actor, scope)],
        "applications": [_mix_out(row) for row in application_mix(actor, scope)],
        "top_items": [_mix_out(row) for row in top_menu_items(actor, scope)],
        "returns": [
            {
                "code": row.reason_kind,
                "label": str(row.label),
                "gross": str(row.gross),
                "net": str(row.net),
                "quantity": str(row.quantity),
                "line_count": row.line_count,
                "share": "0",
            }
            for row in returns_breakdown(actor, scope)
        ],
    }
    return payload


@router.get(
    "/dashboard/cost",
    response=DashboardCostOut,
    summary="لوحة المبيعات — الكلفة والهامش (view_sales_cost)",
)
def get_dashboard_cost(
    request: HttpRequest,
    organization_id: int,
    date_from: datetime.date,
    date_to: datetime.date,
    branch_id: int | None = None,
) -> Any:
    """
    Food cost and margin, over the lines that carry frozen cost evidence.

    A **second permission** on top of reaching the organization, and a whole
    route rather than four keys: without `view_sales_cost` this answers 403 and
    `/dashboard` carries no cost field at all, so there is nowhere for a null to
    appear and be mistaken for a zero.

    `uncosted_lines` sits beside every figure. The percentages cover the costed
    lines only, and presenting them as if they covered all of it would be wrong
    in the direction that looks good.
    """
    actor = _require_reports(request)
    organization = resolve_organization(actor, organization_id)
    if not _may_read_cost(actor, organization):
        raise PermissionMissing("view_sales_cost is not held in this organization.")

    branch_ids: tuple[int, ...] | None = None
    if branch_id is not None:
        branch_ids = (resolve_branch(actor, branch_id).pk,)
    scope = DashboardScope(
        organization_id=organization.pk,
        date_from=date_from,
        date_to=date_to,
        branch_ids=branch_ids,
    )
    cost = cost_summary(actor, scope)
    return {
        "organization_id": organization.pk,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "costed_gross": str(cost.costed_gross),
        "costed_net": str(cost.costed_net),
        "food_cost": str(cost.food_cost),
        "gross_profit": str(cost.gross_profit),
        "food_cost_percent": str(cost.food_cost_percent),
        "margin_percent": str(cost.margin_percent),
        "costed_lines": cost.costed_lines,
        "uncosted_lines": cost.uncosted_lines,
        "uncosted_gross": str(cost.uncosted_gross),
        "is_complete": cost.is_complete,
    }


# ---------------------------------------------------------------------------
# Commands — the daily document
# ---------------------------------------------------------------------------


@router.post("/days", response={201: DayOut}, summary="Open a sales day")
def post_day(request: HttpRequest, payload: DayIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    branch = resolve_branch(actor, payload.branch_id)
    require_branch_permission(actor, CREATE_DAILY_SALES, branch)
    day = create_sales_day(
        organization=organization,
        branch=branch,
        business_date=payload.business_date,
        actor=actor,
        notes=payload.notes,
    )
    return Status(201, _day_out(day))


@router.post("/days/{public_id}/lines", response={201: DayDetailOut}, summary="Add a sales line")
def post_day_line(request: HttpRequest, public_id: str, payload: DayLineIn) -> Status[Any]:
    actor = _actor(request)
    day = _by_public_id(visible_sales_days(actor), public_id, "Sales day")
    require_branch_permission(actor, CREATE_DAILY_SALES, day.branch)

    from apps.sales.selectors import (
        resolve_delivery_application as _application,
    )
    from apps.sales.selectors import (
        resolve_discount_program,
        resolve_menu_item,
        resolve_sales_channel,
    )

    add_sales_line(
        day=day,
        menu_item=resolve_menu_item(actor, payload.menu_item_id),
        channel=resolve_sales_channel(actor, payload.channel_id),
        quantity=_required_decimal(payload.quantity, "quantity"),
        delivery_application=(
            _application(actor, payload.delivery_application_id)
            if payload.delivery_application_id is not None
            else None
        ),
        order_count=payload.order_count,
        discount_program=(
            resolve_discount_program(actor, payload.discount_program_id)
            if payload.discount_program_id is not None
            else None
        ),
        manual_discount_amount=_decimal(payload.manual_discount_amount, "manual_discount_amount"),
        manual_discount_reason=payload.manual_discount_reason,
        other_fee_amount=_required_decimal(payload.other_fee_amount, "other_fee_amount"),
        notes=payload.notes,
    )
    return Status(201, _day_detail_out(SalesDay.objects.get(pk=day.pk)))


@router.post("/days/{public_id}/tenders", response=DayDetailOut, summary="Declare a tender total")
def post_day_tender(request: HttpRequest, public_id: str, payload: TenderIn) -> Any:
    """
    What the operator says each tender took, which is not what the lines say.

    Kept as its own command because the two figures are compared on المطابقة
    اليومية and a difference between them is itself a finding — folding the
    declaration into the day would make it impossible to disagree with.
    """
    actor = _actor(request)
    day = _by_public_id(visible_sales_days(actor), public_id, "Sales day")
    require_branch_permission(actor, CREATE_DAILY_SALES, day.branch)
    set_tender_summary(
        day=day,
        tender=payload.tender,
        declared_amount=_required_decimal(payload.declared_amount, "declared_amount"),
        notes=payload.notes,
    )
    return _day_detail_out(SalesDay.objects.get(pk=day.pk))


@router.post("/days/{public_id}/submit", response=DayOut, summary="Submit a sales day")
def post_day_submit(request: HttpRequest, public_id: str) -> Any:
    actor = _actor(request)
    day = _by_public_id(visible_sales_days(actor), public_id, "Sales day")
    require_branch_permission(actor, SUBMIT_DAILY_SALES, day.branch)
    return _day_out(submit_sales_day(day=day, actor=actor))


@router.post("/days/{public_id}/return", response=DayOut, summary="Return a day to draft")
def post_day_return(request: HttpRequest, public_id: str, payload: ReasonIn) -> Any:
    actor = _actor(request)
    day = _by_public_id(visible_sales_days(actor), public_id, "Sales day")
    require_branch_permission(actor, CREATE_DAILY_SALES, day.branch)
    return _day_out(return_sales_day_to_draft(day=day, actor=actor, reason=payload.reason))


@router.post("/days/{public_id}/post", response=DayOut, summary="Post a sales day")
def post_day_post(request: HttpRequest, public_id: str) -> Any:
    actor = _actor(request)
    day = _by_public_id(visible_sales_days(actor), public_id, "Sales day")
    require_branch_permission(actor, POST_DAILY_SALES, day.branch)
    return _day_out(post_sales_day(day=day, actor=actor))


@router.post("/days/{public_id}/reverse", response=DayOut, summary="Reverse a posted day")
def post_day_reverse(request: HttpRequest, public_id: str, payload: ReasonIn) -> Any:
    actor = _actor(request)
    day = _by_public_id(visible_sales_days(actor), public_id, "Sales day")
    # Organization-wide, not branch: undoing a posted economic event is
    # supervisory, and authority over one branch is authority over a part of
    # something that has no parts.
    require_organization_permission(actor, REVERSE_DAILY_SALES, day.organization)
    return _day_out(reverse_sales_day(day=day, actor=actor, reason=payload.reason))


# ---------------------------------------------------------------------------
# Commands — adjustments
# ---------------------------------------------------------------------------


@router.post("/adjustments", response={201: AdjustmentOut}, summary="Draft an adjustment")
def post_adjustment(request: HttpRequest, payload: AdjustmentIn) -> Status[Any]:
    actor = _actor(request)
    day = _by_public_id(visible_sales_days(actor), payload.sales_day_public_id, "Sales day")
    require_branch_permission(actor, MANAGE_SALES_ADJUSTMENTS, day.branch)
    adjustment = create_sales_adjustment(
        sales_day=day,
        reason_kind=payload.reason_kind,
        business_date=payload.business_date,
        reason=payload.reason,
        evidence_reference=payload.evidence_reference,
        actor=actor,
        notes=payload.notes,
    )
    return Status(201, _adjustment_out(adjustment))


@router.post(
    "/adjustments/{public_id}/lines",
    response={201: AdjustmentDetailOut},
    summary="Add an adjustment line",
)
def post_adjustment_line(
    request: HttpRequest, public_id: str, payload: AdjustmentLineIn
) -> Status[Any]:
    actor = _actor(request)
    adjustment = _by_public_id(visible_sales_adjustments(actor), public_id, "Sales adjustment")
    require_branch_permission(actor, MANAGE_SALES_ADJUSTMENTS, adjustment.branch)
    add_adjustment_line(
        adjustment=adjustment,
        original_line=resolve_sales_day_line(actor, payload.original_line_id),
        adjusted_quantity=_required_decimal(payload.adjusted_quantity, "adjusted_quantity"),
        adjusted_gross=_decimal(payload.adjusted_gross, "adjusted_gross"),
        line_reason=payload.line_reason,
        actor=actor,
    )
    return Status(201, _adjustment_detail_out(SalesAdjustment.objects.get(pk=adjustment.pk)))


@router.post("/adjustments/{public_id}/post", response=AdjustmentOut, summary="Post an adjustment")
def post_adjustment_post(request: HttpRequest, public_id: str) -> Any:
    actor = _actor(request)
    adjustment = _by_public_id(visible_sales_adjustments(actor), public_id, "Sales adjustment")
    require_branch_permission(actor, MANAGE_SALES_ADJUSTMENTS, adjustment.branch)
    return _adjustment_out(post_sales_adjustment(adjustment=adjustment, actor=actor))


@router.post(
    "/adjustments/{public_id}/reverse", response=AdjustmentOut, summary="Reverse an adjustment"
)
def post_adjustment_reverse(request: HttpRequest, public_id: str, payload: ReasonIn) -> Any:
    actor = _actor(request)
    adjustment = _by_public_id(visible_sales_adjustments(actor), public_id, "Sales adjustment")
    # `reverse_daily_sales`, not `manage_sales_adjustments`. Read off the
    # already-migrated label: the adjustment permission says "record returns and
    # cancellations" and says nothing about reversal.
    require_organization_permission(actor, REVERSE_DAILY_SALES, adjustment.organization)
    return _adjustment_out(
        reverse_sales_adjustment(adjustment=adjustment, actor=actor, reason=payload.reason)
    )


# ---------------------------------------------------------------------------
# Commands — settlements
# ---------------------------------------------------------------------------


def _settlement_for(request: HttpRequest, public_id: str) -> tuple[User, Any]:
    actor = _actor(request)
    settlement = _by_public_id(visible_settlements(actor), public_id, "Settlement")
    require_organization_permission(actor, MANAGE_APPLICATION_SETTLEMENTS, settlement.organization)
    return actor, settlement


@router.post("/settlements", response={201: SettlementOut}, summary="Draft a settlement")
def post_settlement_create(request: HttpRequest, payload: SettlementIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    require_organization_permission(actor, MANAGE_APPLICATION_SETTLEMENTS, organization)
    settlement = create_settlement(
        organization=organization,
        branch=resolve_branch(actor, payload.branch_id),
        delivery_application=resolve_delivery_application(actor, payload.delivery_application_id),
        period_start=payload.period_start,
        period_end=payload.period_end,
        business_date=payload.business_date,
        statement_reference=payload.statement_reference,
        statement_date=payload.statement_date,
        statement_amount=_required_decimal(payload.statement_amount, "statement_amount"),
        remitted_amount=_required_decimal(payload.remitted_amount, "remitted_amount"),
        statement_commission_amount=_required_decimal(
            payload.statement_commission_amount, "statement_commission_amount"
        ),
        remittance_destination=payload.remittance_destination,
        evidence_reference=payload.evidence_reference,
        actor=actor,
        notes=payload.notes,
    )
    return Status(201, _settlement_out(settlement))


@router.post(
    "/settlements/{public_id}/allocations",
    response={201: SettlementDetailOut},
    summary="Allocate a receivable entry",
)
def post_settlement_allocation(
    request: HttpRequest, public_id: str, payload: AllocationIn
) -> Status[Any]:
    actor, settlement = _settlement_for(request, public_id)
    allocate_entry(
        settlement=settlement,
        receivable_entry=resolve_receivable_entry(actor, payload.receivable_entry_id),
        allocated_amount=_required_decimal(payload.allocated_amount, "allocated_amount"),
        actor=actor,
    )
    return Status(
        201, _settlement_detail_out(DeliveryApplicationSettlement.objects.get(pk=settlement.pk))
    )


def _resolve_settlement_approver(approver_id: int, settlement: Any) -> User:
    """
    The named approver, resolved **against the settlement** rather than looked
    up globally.

    The settlement was already resolved with the caller by `_settlement_for`,
    so the caller's own authority is settled before this runs; what is left is
    whether the person being *named* could have approved this document.

    `UNEXPLAINED_APPROVED` is the escape hatch ADR-028 §7 opens: a difference
    nobody can explain may still reach `DELIVERY_SETTLEMENT_VARIANCE`, but only
    wearing a name. A bare `User.objects.filter(pk=...)` let the caller write
    any active user in the database into that field — the Owner of another
    organization, or an id they guessed — and the row then carried an approval
    by somebody who never saw the settlement and could not have approved it.

    The rule is the one the permission table already states: the approver must
    be able to exercise `manage_application_settlements` over *this*
    settlement's organization. Anyone else is not an approver of this document,
    and naming them is not an approval.

    A user who does not exist, is inactive, or holds nothing here all get the
    identical refusal. Distinguishing them would turn the endpoint into an
    oracle for user ids across every tenant, which is the same disclosure a 403
    about a foreign document would be.
    """
    candidate = User.objects.filter(pk=approver_id, is_active=True).first()
    if candidate is None or not has_organization_permission(
        candidate, MANAGE_APPLICATION_SETTLEMENTS, settlement.organization
    ):
        raise ValidationError(
            _("The named approver may not approve settlements here."),
            code="approver_required",
        )
    return candidate


@router.post(
    "/settlements/{public_id}/adjustments",
    response={201: SettlementDetailOut},
    summary="Claim part of a variance leg",
)
def post_settlement_adjustment(
    request: HttpRequest, public_id: str, payload: SettlementAdjustmentIn
) -> Status[Any]:
    actor, settlement = _settlement_for(request, public_id)
    approver = None
    if payload.approver_id is not None:
        approver = _resolve_settlement_approver(payload.approver_id, settlement)
    add_settlement_adjustment(
        settlement=settlement,
        leg=payload.leg,
        reason=payload.reason,
        amount=_required_decimal(payload.amount, "amount"),
        explanation=payload.explanation,
        actor=actor,
        approver=approver,
    )
    return Status(
        201, _settlement_detail_out(DeliveryApplicationSettlement.objects.get(pk=settlement.pk))
    )


@router.post(
    "/settlements/{public_id}/reconcile", response=SettlementOut, summary="Reconcile a settlement"
)
def post_settlement_reconcile(request: HttpRequest, public_id: str) -> Any:
    actor, settlement = _settlement_for(request, public_id)
    return _settlement_out(reconcile_settlement(settlement=settlement, actor=actor))


@router.post(
    "/settlements/{public_id}/return", response=SettlementOut, summary="Return one to draft"
)
def post_settlement_return(request: HttpRequest, public_id: str, payload: ReasonIn) -> Any:
    actor, settlement = _settlement_for(request, public_id)
    return _settlement_out(
        return_settlement_to_draft(settlement=settlement, actor=actor, reason=payload.reason)
    )


@router.post("/settlements/{public_id}/post", response=SettlementOut, summary="Post a settlement")
def post_settlement_post(request: HttpRequest, public_id: str) -> Any:
    actor, settlement = _settlement_for(request, public_id)
    return _settlement_out(post_settlement(settlement=settlement, actor=actor))


@router.post(
    "/settlements/{public_id}/reverse", response=SettlementOut, summary="Reverse a settlement"
)
def post_settlement_reverse(request: HttpRequest, public_id: str, payload: ReasonIn) -> Any:
    # `manage_application_settlements`, not `reverse_daily_sales`: the migrated
    # label reads "create, reconcile, post **and reverse**", and the label is the
    # contract.
    actor, settlement = _settlement_for(request, public_id)
    return _settlement_out(
        reverse_settlement(settlement=settlement, actor=actor, reason=payload.reason)
    )


# ---------------------------------------------------------------------------
# Commands — the till
# ---------------------------------------------------------------------------


@router.post("/shifts", response={201: ShiftOut}, summary="Open a cashier shift")
def post_shift(request: HttpRequest, payload: ShiftIn) -> Status[Any]:
    actor = _actor(request)
    organization = resolve_organization(actor, payload.organization_id)
    branch = resolve_branch(actor, payload.branch_id)
    require_branch_permission(actor, CLOSE_CASHIER_SHIFT, branch)
    cashier = User.objects.filter(pk=payload.cashier_id, is_active=True).first()
    if cashier is None:
        raise OutOfScope(f"User {payload.cashier_id} does not exist.")
    shift = open_cashier_shift(
        organization=organization,
        branch=branch,
        business_date=payload.business_date,
        cashier=cashier,
        opening_float=_required_decimal(payload.opening_float, "opening_float"),
        actor=actor,
        notes=payload.notes,
    )
    return Status(201, _shift_out(shift))


@router.post(
    "/shifts/{public_id}/counts", response={201: ShiftDetailOut}, summary="Record a tender count"
)
def post_shift_count(request: HttpRequest, public_id: str, payload: CountIn) -> Status[Any]:
    actor = _actor(request)
    shift = _by_public_id(visible_cashier_shifts(actor), public_id, "Cashier shift")
    require_branch_permission(actor, CLOSE_CASHIER_SHIFT, shift.branch)
    set_tender_count(
        shift=shift,
        tender=payload.tender,
        counted_amount=_required_decimal(payload.counted_amount, "counted_amount"),
        actor=actor,
        notes=payload.notes,
    )
    return Status(201, _shift_detail_out(CashierShift.objects.get(pk=shift.pk)))


@router.post("/shifts/{public_id}/close", response=ShiftOut, summary="Close a cashier shift")
def post_shift_close(request: HttpRequest, public_id: str, payload: CloseIn) -> Any:
    actor = _actor(request)
    shift = _by_public_id(visible_cashier_shifts(actor), public_id, "Cashier shift")
    require_branch_permission(actor, CLOSE_CASHIER_SHIFT, shift.branch)
    day = _by_public_id(visible_sales_days(actor), payload.sales_day_public_id, "Sales day")
    return _shift_out(
        close_cashier_shift(shift=shift, sales_day=day, actor=actor, notes=payload.notes)
    )


@router.post("/shifts/{public_id}/reopen", response=ShiftOut, summary="Reopen a closed shift")
def post_shift_reopen(request: HttpRequest, public_id: str, payload: ReasonIn) -> Any:
    actor = _actor(request)
    shift = _by_public_id(visible_cashier_shifts(actor), public_id, "Cashier shift")
    require_branch_permission(actor, CLOSE_CASHIER_SHIFT, shift.branch)
    return _shift_out(reopen_cashier_shift(shift=shift, actor=actor, reason=payload.reason))


@router.post("/shifts/{public_id}/approve", response=ShiftOut, summary="Approve a closing")
def post_shift_approve(request: HttpRequest, public_id: str) -> Any:
    """
    Approve a counted drawer, and post its variance.

    The maker-checker rule is **not** enforced here. `approve_cashier_shift`
    re-checks `actor != closed_by` under the row lock and
    `sales_shift_approver_is_not_the_closer` refuses it at the database, so an
    API-level check would be a third copy of a rule that already holds — and the
    one place a copy could drift from the other two.
    """
    actor = _actor(request)
    shift = _by_public_id(visible_cashier_shifts(actor), public_id, "Cashier shift")
    require_branch_permission(actor, APPROVE_CASHIER_CLOSING, shift.branch)
    return _shift_out(approve_cashier_shift(shift=shift, actor=actor))


@router.post(
    "/shifts/{public_id}/reverse", response=ShiftOut, summary="Reverse an approved closing"
)
def post_shift_reverse(request: HttpRequest, public_id: str, payload: ReasonIn) -> Any:
    actor = _actor(request)
    shift = _by_public_id(visible_cashier_shifts(actor), public_id, "Cashier shift")
    require_organization_permission(actor, REVERSE_DAILY_SALES, shift.organization)
    return _shift_out(reverse_cashier_shift(shift=shift, actor=actor, reason=payload.reason))


__all__ = ["PAGE_LIMIT", "router"]
