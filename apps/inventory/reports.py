"""
Inventory reports: scoped reads over the ledger, and nothing else.

Every function here returns rows. None of them writes, posts, reprices, or
repairs anything — a report that could change a figure it is reporting would
be the one place a discrepancy could be made to disappear.

## The two historical modes, and why one "as of" would be a lie

A movement carries two times, and they answer different questions:

    posted_at / posted_sequence   when the system learned it
    effective_at                  which business moment it belongs to

They diverge whenever somebody posts late, which in a restaurant is most
Mondays. A delivery that physically arrived on the 28th and was keyed in on the
2nd is `effective_at = 28th`, `posted_sequence = whatever came after the 1st`.

`ReportMode.POSTED_AS_OF` answers **"what did the books say at that moment?"**
— the audit question. It takes a prefix of the posting order, which is exactly
what the valuation kernel replayed, so the stored `quantity_after`,
`value_after` and `average_after` of the last included movement *are* the
position. Nothing is recomputed.

`ReportMode.EFFECTIVE_DATE` answers **"which movements belong to that business
period, knowing everything we know now?"** — the management question. Its
movement set is **not** a prefix of the posting order (the late-keyed delivery
sits inside it while later-effective, earlier-posted movements sit outside), so
the stored running totals do not apply. Quantities and values are summed —
they are additive — and the average is derived as value ÷ quantity.

**Neither is corruption, and the two legitimately disagree.** A report that
offered one unlabelled "as of" filter would silently pick one and be wrong for
half its readers. Every historical report states its mode.

Posted movements are never repriced. This module reads `inventory_value` as the
kernel computed it and never re-derives a unit cost from today's average.

## Valuation redaction

Cost, value and average are *omitted* for a caller without
`inventory.view_valuation`, never blanked — an empty cell still says a number
belongs there. The row builders take `include_valuation` and leave the keys out
entirely, so a template, a JSON payload and a CSV writer all inherit the same
decision from one place.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from django.db.models import Q, QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, JournalLine
from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.inventory.models import (
    BranchItemSetting,
    InventoryAdjustmentLine,
    InventoryDocumentType,
    InventoryLot,
    InventoryMovementDocumentLine,
    StockBalance,
    StockCountLine,
    StockCountStatus,
    StockLocationBalance,
    StockMovement,
    StockTransfer,
    StockTransferStatus,
    Warehouse,
)
from apps.inventory.reconciliation import _control_account_of, verify_inventory_against_gl
from apps.inventory.selectors import readable_warehouses
from apps.organizations.models import Organization
from apps.users.models import User

ZERO = Decimal("0")


class ReportMode(StrEnum):
    """Which time a historical report slices on. Always displayed, never implied."""

    POSTED_AS_OF = "POSTED_AS_OF"
    EFFECTIVE_DATE = "EFFECTIVE_DATE"

    @property
    def label(self) -> Any:
        return REPORT_MODE_LABELS[self]


REPORT_MODE_LABELS: dict[ReportMode, Any] = {
    ReportMode.POSTED_AS_OF: _("حسب تاريخ الترحيل — ما كانت تقوله الدفاتر آنذاك"),
    ReportMode.EFFECTIVE_DATE: _("حسب تاريخ الاستحقاق — بمعلومات اليوم"),
}


def resolve_mode(raw: str | None) -> ReportMode:
    """The requested mode, defaulting to the audit answer.

    `POSTED_AS_OF` is the default deliberately: it is the one that reproduces
    a figure somebody printed last month, and a reader who did not choose a
    mode is usually trying to reconcile against something.
    """
    if raw == ReportMode.EFFECTIVE_DATE.value:
        return ReportMode.EFFECTIVE_DATE
    return ReportMode.POSTED_AS_OF


# ---------------------------------------------------------------------------
# Filters and scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportFilters:
    """
    What the reader asked for. All optional; absent means "no restriction".

    A frozen dataclass rather than loose kwargs so the screen, the export and
    the tests provably pass the *same* filters — §E's scope parity is only
    real if there is one object to pass.
    """

    organization_id: int | None = None
    branch_id: int | None = None
    warehouse_id: int | None = None
    category_id: int | None = None
    item_id: int | None = None
    lot_id: int | None = None
    cost_center_id: int | None = None
    reason_code_id: int | None = None
    include_inactive: bool = False
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    #: Expiry horizon in days, for the expiry report.
    within_days: int | None = None
    mode: ReportMode = ReportMode.POSTED_AS_OF
    search: str = ""

    def as_query(self) -> dict[str, str]:
        """The filters as querystring pairs, for export links and pagination."""
        pairs: dict[str, str] = {}
        for name in (
            "organization_id",
            "branch_id",
            "warehouse_id",
            "category_id",
            "item_id",
            "lot_id",
            "cost_center_id",
            "reason_code_id",
        ):
            value = getattr(self, name)
            if value is not None:
                pairs[name] = str(value)
        if self.include_inactive:
            pairs["include_inactive"] = "1"
        if self.date_from:
            pairs["date_from"] = self.date_from.isoformat()
        if self.date_to:
            pairs["date_to"] = self.date_to.isoformat()
        if self.within_days is not None:
            pairs["within_days"] = str(self.within_days)
        if self.search:
            pairs["q"] = self.search
        pairs["mode"] = self.mode.value
        return pairs


def scoped_warehouses(user: User, filters: ReportFilters) -> QuerySet[Warehouse]:
    """
    The warehouses this caller may read, narrowed by the requested filters.

    Scope first, filters second, and never the other way round: a filter that
    named a warehouse the caller cannot reach must return nothing, not that
    warehouse's rows.
    """
    warehouses = readable_warehouses(user)
    if filters.organization_id is not None:
        warehouses = warehouses.filter(branch__organization_id=filters.organization_id)
    if filters.branch_id is not None:
        warehouses = warehouses.filter(branch_id=filters.branch_id)
    if filters.warehouse_id is not None:
        warehouses = warehouses.filter(pk=filters.warehouse_id)
    return warehouses


def _apply_item_filters(
    queryset: QuerySet[Any], filters: ReportFilters, prefix: str = ""
) -> QuerySet[Any]:
    """Category, item and lot narrowing, shared by every item-shaped report."""
    if filters.category_id is not None:
        queryset = queryset.filter(
            Q(**{f"{prefix}item__category_id": filters.category_id})
            | Q(**{f"{prefix}item__category__parent_id": filters.category_id})
            | Q(**{f"{prefix}item__category__parent__parent_id": filters.category_id})
        )
    if filters.item_id is not None:
        queryset = queryset.filter(**{f"{prefix}item_id": filters.item_id})
    if filters.lot_id is not None:
        queryset = queryset.filter(**{f"{prefix}lot_id": filters.lot_id})
    if filters.search:
        queryset = queryset.filter(
            Q(**{f"{prefix}item__code__icontains": filters.search})
            | Q(**{f"{prefix}item__name_ar__icontains": filters.search})
            | Q(**{f"{prefix}item__name_en__icontains": filters.search})
        )
    return queryset


def _movement_cutoff(
    queryset: QuerySet[StockMovement], filters: ReportFilters
) -> QuerySet[StockMovement]:
    """
    Apply the date window on whichever time the mode names.

    This is the only place the two modes differ in *what is included*; how the
    included rows are then combined differs in `_positions_from`.
    """
    if filters.mode is ReportMode.EFFECTIVE_DATE:
        if filters.date_from is not None:
            queryset = queryset.filter(effective_at__date__gte=filters.date_from)
        if filters.date_to is not None:
            queryset = queryset.filter(effective_at__date__lte=filters.date_to)
        return queryset
    if filters.date_from is not None:
        queryset = queryset.filter(posted_at__date__gte=filters.date_from)
    if filters.date_to is not None:
        queryset = queryset.filter(posted_at__date__lte=filters.date_to)
    return queryset


def scoped_movements(user: User, filters: ReportFilters) -> QuerySet[StockMovement]:
    """Every movement this caller may read, under the requested filters."""
    movements = StockMovement.objects.filter(warehouse__in=scoped_warehouses(user, filters))
    movements = _apply_item_filters(movements, filters)
    return _movement_cutoff(movements, filters)


# ---------------------------------------------------------------------------
# 1. Stock valuation
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """One (warehouse, item, lot) position, however it was arrived at."""

    warehouse: Warehouse
    item: Any
    lot: InventoryLot | None
    quantity: Decimal
    value: Decimal
    average: Decimal
    control_account: Any = None
    last_movement_at: datetime.datetime | None = None
    last_posted_sequence: int = 0


def _positions_from(movements: QuerySet[StockMovement], *, mode: ReportMode) -> list[Position]:
    """
    Fold a movement set into positions, the way the mode requires.

    `POSTED_AS_OF` takes a prefix of the posting order, so the last included
    movement's stored running totals *are* the position and nothing is
    recomputed — the kernel already did this arithmetic and repeating it would
    be a second opinion about a settled figure.

    `EFFECTIVE_DATE` includes a set that is not a prefix, so those running
    totals do not apply. Quantity and value are summed because they are
    additive; the average is derived as value ÷ quantity, which is the honest
    answer for a set that was never replayed in this order and is labelled as
    such on every screen that shows it.
    """
    rows = movements.select_related(
        "warehouse",
        "warehouse__branch",
        "item",
        "item__base_unit",
        "item__category",
        "lot",
        "control_account",
    ).order_by("posted_sequence")

    positions: dict[tuple[int, int, int | None], Position] = {}
    for movement in rows:
        key = (movement.warehouse_id, movement.item_id, movement.lot_id)
        current = positions.get(key)
        if current is None:
            current = Position(
                warehouse=movement.warehouse,
                item=movement.item,
                lot=movement.lot,
                quantity=ZERO,
                value=ZERO,
                average=ZERO,
                control_account=movement.control_account,
            )
            positions[key] = current

        if mode is ReportMode.POSTED_AS_OF:
            current.quantity = movement.quantity_after
            current.value = movement.value_after
            current.average = movement.average_after
        else:
            current.quantity = quantize_quantity(current.quantity + movement.base_quantity)
            current.value = quantize_money(current.value + movement.inventory_value)
            current.average = (
                quantize_unit_price(current.value / current.quantity)
                if current.quantity != ZERO
                else ZERO
            )
        current.control_account = movement.control_account or current.control_account
        current.last_movement_at = movement.effective_at
        current.last_posted_sequence = movement.posted_sequence

    return sorted(
        positions.values(),
        key=lambda position: (
            position.warehouse.code,
            position.item.code,
            position.lot.code if position.lot else "",
        ),
    )


def stock_valuation(
    user: User, filters: ReportFilters, *, include_valuation: bool
) -> list[dict[str, Any]]:
    """
    Quantity and value per warehouse / item / lot.

    With no date window this reads `StockBalance` — the projection the ledger
    maintains — because that is what "now" means and re-folding every movement
    to rediscover it would be slower and no more true. With a window it folds
    the movements, in the mode's own way.
    """
    windowed = filters.date_from is not None or filters.date_to is not None

    if windowed:
        positions = _positions_from(scoped_movements(user, filters), mode=filters.mode)
    else:
        balances = StockBalance.objects.filter(warehouse__in=scoped_warehouses(user, filters))
        balances = _apply_item_filters(balances, filters)
        if not filters.include_inactive:
            balances = balances.exclude(quantity=ZERO, value=ZERO)
        positions = [
            Position(
                warehouse=balance.warehouse,
                item=balance.item,
                lot=balance.lot,
                quantity=balance.quantity,
                value=balance.value,
                average=balance.average_cost,
                control_account=balance.control_account,
                last_movement_at=balance.last_movement.effective_at
                if balance.last_movement
                else None,
                last_posted_sequence=balance.last_posted_sequence,
            )
            for balance in balances.select_related(
                "warehouse",
                "warehouse__branch",
                "item",
                "item__base_unit",
                "item__category",
                "lot",
                "control_account",
                "last_movement",
            ).order_by("warehouse__code", "item__code", "lot__code")
        ]

    return [_valuation_row(position, include_valuation=include_valuation) for position in positions]


def _valuation_row(position: Position, *, include_valuation: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "branch_code": position.warehouse.branch.code,
        "warehouse_code": position.warehouse.code,
        "warehouse_name": position.warehouse.name_ar,
        "item_code": position.item.code,
        "item_name": position.item.name_ar,
        "category_code": position.item.category.code,
        "lot_code": position.lot.code if position.lot else "",
        "expiry_date": position.lot.expiry_date if position.lot else None,
        "unit": position.item.base_unit.code,
        "quantity": position.quantity,
        "is_active": position.item.is_active,
        "last_movement_at": position.last_movement_at,
        "last_posted_sequence": position.last_posted_sequence,
    }
    if include_valuation:
        row["average_cost"] = position.average
        row["value"] = position.value
        row["control_account"] = position.control_account.code if position.control_account else ""
    return row


# ---------------------------------------------------------------------------
# 2. Stock card / item ledger
# ---------------------------------------------------------------------------


def stock_card(
    user: User, filters: ReportFilters, *, include_valuation: bool
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    One position's whole history, with the running totals the kernel recorded.

    Returns `(opening, movements)`. The opening row is what the position was
    immediately before the first movement shown — taken from that movement's
    own `*_before` columns, so it is the kernel's figure rather than a
    subtraction performed here.

    Always ordered by `posted_sequence`, in both modes: a stock card ordered by
    effective date would show running totals that jump backwards, because the
    running totals were computed in posting order and no other.
    """
    movements = (
        scoped_movements(user, filters)
        .select_related(
            "entry", "item", "item__base_unit", "warehouse", "lot", "entry__posted_by", "reverses"
        )
        .order_by("posted_sequence")
    )

    first = movements.first()
    opening: dict[str, Any] = {
        "quantity": first.quantity_before if first else ZERO,
    }
    if include_valuation and first is not None:
        opening["value"] = first.value_before
        opening["average"] = first.average_before
    elif include_valuation:
        opening["value"] = ZERO
        opening["average"] = ZERO

    rows: list[dict[str, Any]] = []
    for movement in movements:
        inbound = movement.base_quantity > ZERO
        row: dict[str, Any] = {
            "posted_sequence": movement.posted_sequence,
            "effective_at": movement.effective_at,
            "posted_at": movement.posted_at,
            "movement_type": movement.movement_type,
            "movement_type_label": movement.get_movement_type_display(),
            "source_document_type": movement.entry.source_document_type,
            "source_document_id": movement.entry.source_document_id,
            "source_event": movement.entry.source_event,
            "reference": movement.entry.reference,
            "warehouse_code": movement.warehouse.code,
            "item_code": movement.item.code,
            "lot_code": movement.lot.code if movement.lot else "",
            "unit": movement.item.base_unit.code,
            "quantity_in": movement.base_quantity if inbound else ZERO,
            "quantity_out": -movement.base_quantity if not inbound else ZERO,
            "quantity_after": movement.quantity_after,
            "actor": movement.entry.posted_by.username if movement.entry.posted_by else "",
            "is_reversal": movement.reverses_id is not None,
        }
        if include_valuation:
            row["value_in"] = movement.inventory_value if inbound else ZERO
            row["value_out"] = -movement.inventory_value if not inbound else ZERO
            row["unit_cost"] = movement.unit_cost
            row["value_after"] = movement.value_after
            row["average_after"] = movement.average_after
        rows.append(row)

    return opening, rows


# ---------------------------------------------------------------------------
# 3. In-transit and open transfers
# ---------------------------------------------------------------------------

OPEN_TRANSFER_STATUSES = (
    StockTransferStatus.DISPATCHED,
    StockTransferStatus.PARTIALLY_RECEIVED,
)


def in_transit_aging(
    user: User,
    filters: ReportFilters,
    *,
    include_valuation: bool,
    today: datetime.date | None = None,
) -> list[dict[str, Any]]:
    """
    What left one warehouse and has not arrived at the other, and how long ago.

    Age is measured from the dispatch business date, not from `created_at`: a
    transfer keyed in late was still in transit from the day it left, and
    ageing it from the keystroke would hide exactly the stale ones worth
    chasing.
    """
    today = today or timezone.localdate()
    reachable = readable_warehouses(user)
    transfers = StockTransfer.objects.filter(
        source_warehouse__in=reachable, status__in=OPEN_TRANSFER_STATUSES
    )
    if filters.organization_id is not None:
        transfers = transfers.filter(organization_id=filters.organization_id)
    if filters.branch_id is not None:
        transfers = transfers.filter(
            Q(source_warehouse__branch_id=filters.branch_id)
            | Q(destination_warehouse__branch_id=filters.branch_id)
        )
    if filters.warehouse_id is not None:
        transfers = transfers.filter(
            Q(source_warehouse_id=filters.warehouse_id)
            | Q(destination_warehouse_id=filters.warehouse_id)
        )

    rows: list[dict[str, Any]] = []
    for transfer in (
        transfers.select_related(
            "source_warehouse",
            "source_warehouse__branch",
            "destination_warehouse",
            "destination_warehouse__branch",
        )
        .prefetch_related("lines__item", "lines__item__base_unit", "lines__lot")
        .order_by("-business_date", "transfer_number")
    ):
        lines = (
            _apply_item_filters(transfer.lines.all(), filters)
            if (filters.item_id or filters.category_id or filters.lot_id or filters.search)
            else transfer.lines.all()
        )
        for line in lines:
            received_quantity = quantize_quantity(line.base_quantity - line.remaining_quantity)
            row: dict[str, Any] = {
                "transfer_number": transfer.transfer_number,
                "transfer_id": transfer.pk,
                "status": transfer.status,
                "status_label": transfer.get_status_display(),
                "source_branch": transfer.source_warehouse.branch.code,
                "source_warehouse": transfer.source_warehouse.code,
                "destination_branch": transfer.destination_warehouse.branch.code,
                "destination_warehouse": transfer.destination_warehouse.code,
                "dispatch_date": transfer.business_date,
                "item_code": line.item.code,
                "item_name": line.item.name_ar,
                "lot_code": line.lot.code if line.lot else "",
                "unit": line.item.base_unit.code,
                "dispatched_quantity": line.base_quantity,
                "received_quantity": received_quantity,
                "remaining_quantity": line.remaining_quantity,
                "age_days": (today - transfer.business_date).days
                if transfer.business_date
                else None,
            }
            if include_valuation:
                # `total_value` is null only on a line that has not dispatched,
                # and an undispatched line cannot be in an open transfer.
                dispatched_value = line.total_value or ZERO
                row["dispatched_value"] = dispatched_value
                row["received_value"] = quantize_money(dispatched_value - line.remaining_value)
                row["remaining_value"] = line.remaining_value
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 4. Expiry
# ---------------------------------------------------------------------------

#: The windows the screen offers. Configurable through `within_days`; these
#: are the buckets a row is labelled with.
EXPIRY_WINDOWS = (7, 30, 90)


def expiry(
    user: User,
    filters: ReportFilters,
    *,
    include_valuation: bool,
    today: datetime.date | None = None,
) -> list[dict[str, Any]]:
    """
    Dated stock, soonest first, with expired rows at the top.

    Only positions holding stock appear: an expired lot at zero has already
    been dealt with, and listing it forever would bury the ones that have not.
    """
    today = today or timezone.localdate()
    horizon = filters.within_days
    balances = StockBalance.objects.filter(
        warehouse__in=scoped_warehouses(user, filters), lot__expiry_date__isnull=False
    ).exclude(quantity=ZERO)
    balances = _apply_item_filters(balances, filters)
    if horizon is not None:
        balances = balances.filter(lot__expiry_date__lte=today + datetime.timedelta(days=horizon))

    rows: list[dict[str, Any]] = []
    for balance in balances.select_related(
        "warehouse", "warehouse__branch", "item", "item__base_unit", "lot"
    ).order_by("lot__expiry_date", "warehouse__code", "item__code"):
        # The queryset already excludes lotless and undated rows; the guard is
        # for the type checker, which cannot see through a filter.
        lot = balance.lot
        if lot is None or lot.expiry_date is None:  # pragma: no cover
            continue
        days = (lot.expiry_date - today).days
        row: dict[str, Any] = {
            "branch_code": balance.warehouse.branch.code,
            "warehouse_code": balance.warehouse.code,
            "item_code": balance.item.code,
            "item_name": balance.item.name_ar,
            "lot_code": lot.code,
            "expiry_date": lot.expiry_date,
            "days_to_expiry": days,
            "is_expired": days < 0,
            "bucket": _expiry_bucket(days),
            "unit": balance.item.base_unit.code,
            "quantity": balance.quantity,
        }
        if include_valuation:
            row["value"] = balance.value
        rows.append(row)
    return rows


def _expiry_bucket(days: int) -> str:
    if days < 0:
        return "EXPIRED"
    for window in EXPIRY_WINDOWS:
        if days <= window:
            return f"WITHIN_{window}"
    return "LATER"


# ---------------------------------------------------------------------------
# 5. Reorder / low stock
# ---------------------------------------------------------------------------


def reorder(user: User, filters: ReportFilters, *, include_valuation: bool) -> list[dict[str, Any]]:
    """
    Stocked items against their reorder point, per branch.

    Compares the branch's whole holding, summed across its warehouses, because
    the reorder point is a branch decision — splitting it per store would
    report a shortage every time stock sat in the kitchen rather than the main
    store.

    Suggests nothing and orders nothing: there are no purchase orders in Phase
    1, and inventing one here would be inventing a business process.
    """
    reachable = scoped_warehouses(user, filters)
    settings_rows = BranchItemSetting.objects.filter(
        is_stocked=True, branch__warehouses__in=reachable
    ).distinct()
    if filters.branch_id is not None:
        settings_rows = settings_rows.filter(branch_id=filters.branch_id)
    settings_rows = _apply_item_filters(settings_rows, filters)

    holdings = {
        (row["branch_id"], row["item_id"]): row["held"]
        for row in StockBalance.objects.filter(warehouse__in=reachable)
        .values("branch_id", "item_id")
        .annotate(held=Sum("quantity"))
    }
    values = {
        (row["branch_id"], row["item_id"]): row["held_value"]
        for row in StockBalance.objects.filter(warehouse__in=reachable)
        .values("branch_id", "item_id")
        .annotate(held_value=Sum("value"))
    }

    rows: list[dict[str, Any]] = []
    for setting in settings_rows.select_related(
        "branch", "item", "item__base_unit", "item__category"
    ).order_by("branch__code", "item__code"):
        held = holdings.get((setting.branch_id, setting.item_id), ZERO) or ZERO
        point = setting.reorder_point
        below = point is not None and held < point
        shortage = quantize_quantity(point - held) if (below and point is not None) else ZERO
        row: dict[str, Any] = {
            "branch_code": setting.branch.code,
            "item_code": setting.item.code,
            "item_name": setting.item.name_ar,
            "unit": setting.item.base_unit.code,
            "on_hand": quantize_quantity(held),
            "reorder_point": point,
            "reorder_quantity": setting.reorder_quantity,
            "shortage": shortage,
            "is_below": below,
            "is_at_point": point is not None and held == point,
        }
        if include_valuation:
            row["value"] = quantize_money(
                values.get((setting.branch_id, setting.item_id), ZERO) or ZERO
            )
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 6. Waste summary
# ---------------------------------------------------------------------------


def waste_summary(
    user: User, filters: ReportFilters, *, include_valuation: bool
) -> list[dict[str, Any]]:
    """Every waste line, with the reason and the cost centre that carried it."""
    lines = InventoryMovementDocumentLine.objects.filter(
        document__document_type=InventoryDocumentType.WASTE,
        document__warehouse__in=scoped_warehouses(user, filters),
        document__status="POSTED",
    )
    lines = _apply_item_filters(lines, filters)
    if filters.reason_code_id is not None:
        lines = lines.filter(reason_code_id=filters.reason_code_id)
    if filters.cost_center_id is not None:
        lines = lines.filter(document__cost_center_id=filters.cost_center_id)
    if filters.date_from is not None:
        lines = lines.filter(document__business_date__gte=filters.date_from)
    if filters.date_to is not None:
        lines = lines.filter(document__business_date__lte=filters.date_to)

    rows: list[dict[str, Any]] = []
    for line in lines.select_related(
        "document",
        "document__warehouse",
        "document__warehouse__branch",
        "document__cost_center",
        "item",
        "item__base_unit",
        "lot",
        "reason_code",
    ).order_by("-document__business_date", "document__document_number", "sequence"):
        row: dict[str, Any] = {
            "document_number": line.document.document_number,
            "business_date": line.document.business_date,
            "branch_code": line.document.warehouse.branch.code,
            "warehouse_code": line.document.warehouse.code,
            "item_code": line.item.code,
            "item_name": line.item.name_ar,
            "lot_code": line.lot.code if line.lot else "",
            "unit": line.item.base_unit.code,
            "quantity": line.base_quantity,
            "reason_code": line.reason_code.code if line.reason_code else "",
            "reason_name": line.reason_code.name_ar if line.reason_code else "",
            "comment": line.line_comment,
            "cost_center": line.document.cost_center.code if line.document.cost_center else "",
        }
        if include_valuation:
            row["value"] = line.total_value
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 7. Count variance
# ---------------------------------------------------------------------------


def count_variance(
    user: User, filters: ReportFilters, *, include_valuation: bool
) -> list[dict[str, Any]]:
    """
    Counted against book, per count line, with both people named.

    Only counts that reached a verdict appear — an in-progress sheet has no
    variance yet, and showing one would be showing a half-counted warehouse as
    though it were short.
    """
    lines = StockCountLine.objects.filter(
        count__warehouse__in=scoped_warehouses(user, filters),
        count__status__in=[StockCountStatus.POSTED, StockCountStatus.REVERSED],
    )
    lines = _apply_item_filters(lines, filters)
    if filters.date_from is not None:
        lines = lines.filter(count__business_date__gte=filters.date_from)
    if filters.date_to is not None:
        lines = lines.filter(count__business_date__lte=filters.date_to)

    rows: list[dict[str, Any]] = []
    for line in lines.select_related(
        "count",
        "count__warehouse",
        "count__warehouse__branch",
        "count__conducted_by",
        "count__approved_by",
        "item",
        "item__base_unit",
        "lot",
        "reason_code",
    ).order_by("-count__business_date", "count__count_number", "sequence"):
        row: dict[str, Any] = {
            "count_number": line.count.count_number,
            "count_id": line.count.pk,
            "status": line.count.status,
            "business_date": line.count.business_date,
            "branch_code": line.count.warehouse.branch.code,
            "warehouse_code": line.count.warehouse.code,
            "conductor": line.count.conducted_by.username if line.count.conducted_by else "",
            "approver": line.count.approved_by.username if line.count.approved_by else "",
            "item_code": line.item.code,
            "item_name": line.item.name_ar,
            "lot_code": line.lot.code if line.lot else "",
            "unit": line.item.base_unit.code,
            "book_quantity": line.book_quantity,
            "counted_quantity": line.counted_quantity,
            "variance_quantity": line.variance_quantity,
            "is_unexpected": line.is_unexpected,
            "reason_code": line.reason_code.code if line.reason_code else "",
        }
        if include_valuation:
            row["variance_value"] = line.variance_value
            row["book_value"] = line.book_value
            row["book_average"] = line.book_average
            # Only set where the count found stock the books valued at
            # nothing, so somebody had to state a cost. Blank elsewhere is the
            # truth, not a gap.
            row["approved_unit_cost"] = line.approved_unit_cost
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 8. Adjustments
# ---------------------------------------------------------------------------


def adjustments(
    user: User, filters: ReportFilters, *, include_valuation: bool
) -> list[dict[str, Any]]:
    """Every posted adjustment line, by kind, with its reason and actor."""
    lines = InventoryAdjustmentLine.objects.filter(
        document__warehouse__in=scoped_warehouses(user, filters),
        document__status__in=["POSTED", "REVERSED"],
    )
    lines = _apply_item_filters(lines, filters)
    if filters.reason_code_id is not None:
        lines = lines.filter(reason_code_id=filters.reason_code_id)
    if filters.cost_center_id is not None:
        lines = lines.filter(document__cost_center_id=filters.cost_center_id)
    if filters.date_from is not None:
        lines = lines.filter(document__business_date__gte=filters.date_from)
    if filters.date_to is not None:
        lines = lines.filter(document__business_date__lte=filters.date_to)

    rows: list[dict[str, Any]] = []
    for line in lines.select_related(
        "document",
        "document__warehouse",
        "document__warehouse__branch",
        "document__cost_center",
        "document__posted_by",
        "item",
        "item__base_unit",
        "lot",
        "reason_code",
    ).order_by("-document__business_date", "document__document_number", "sequence"):
        row: dict[str, Any] = {
            "document_number": line.document.document_number,
            "document_id": line.document.pk,
            "status": line.document.status,
            "business_date": line.document.business_date,
            "branch_code": line.document.warehouse.branch.code,
            "warehouse_code": line.document.warehouse.code,
            "kind": line.kind,
            "kind_label": line.get_kind_display(),
            "item_code": line.item.code,
            "item_name": line.item.name_ar,
            "lot_code": line.lot.code if line.lot else "",
            "unit": line.item.base_unit.code,
            "quantity": line.base_quantity or ZERO,
            "reason_code": line.reason_code.code if line.reason_code else "",
            "comment": line.line_comment,
            "cost_center": line.document.cost_center.code if line.document.cost_center else "",
            "actor": line.document.posted_by.username if line.document.posted_by else "",
            "is_reversed": line.document.status == "REVERSED",
        }
        if include_valuation:
            row["unit_cost"] = line.unit_cost
            row["value_adjustment"] = line.value_adjustment
            row["total_value"] = line.total_value
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 10. Stock locations
# ---------------------------------------------------------------------------


def location_balances(
    user: User, filters: ReportFilters, *, include_valuation: bool
) -> list[dict[str, Any]]:
    """
    What sits in which bin, and what the warehouse holds that no bin claims.

    The unlocated row is the interesting one and it is **derived** rather than
    read: `warehouse quantity − sum(located)`. A warehouse that has never used
    bins shows one unlocated row per position, which is the honest picture of
    most stores in this business rather than an empty screen.

    No value column at any permission level. `include_valuation` is accepted so
    every report shares one signature, and deliberately ignored: ADR-018 §2
    gives value to the warehouse, and a location that could show a figure would
    be a location that had one.
    """
    warehouses = scoped_warehouses(user, filters)

    located: dict[tuple[int, int, int | None], Decimal] = {}
    rows: list[dict[str, Any]] = []
    balances = _apply_item_filters(
        StockLocationBalance.objects.filter(warehouse__in=warehouses), filters
    )
    for balance in balances.select_related(
        "location", "warehouse", "warehouse__branch", "item", "item__base_unit", "lot"
    ).order_by("warehouse__code", "location__code", "item__code"):
        key = (balance.warehouse_id, balance.item_id, balance.lot_id)
        located[key] = located.get(key, ZERO) + balance.quantity
        rows.append(
            {
                "branch_code": balance.warehouse.branch.code,
                "warehouse_code": balance.warehouse.code,
                "location_code": balance.location.code,
                "location_name": balance.location.name_ar,
                "item_code": balance.item.code,
                "item_name": balance.item.name_ar,
                "lot_code": balance.lot.code if balance.lot else "",
                "unit": balance.item.base_unit.code,
                "quantity": balance.quantity,
                "is_unlocated": False,
            }
        )

    # One derived row per position the warehouse holds beyond its bins.
    warehouse_balances = _apply_item_filters(
        StockBalance.objects.filter(warehouse__in=warehouses).exclude(quantity=ZERO), filters
    )
    for balance in warehouse_balances.select_related(
        "warehouse", "warehouse__branch", "item", "item__base_unit", "lot"
    ).order_by("warehouse__code", "item__code"):
        key = (balance.warehouse_id, balance.item_id, balance.lot_id)
        remainder = quantize_quantity(balance.quantity - located.get(key, ZERO))
        if remainder == ZERO:
            continue
        rows.append(
            {
                "branch_code": balance.warehouse.branch.code,
                "warehouse_code": balance.warehouse.code,
                "location_code": "—",
                "location_name": _("غير مخصص لموقع"),
                "item_code": balance.item.code,
                "item_name": balance.item.name_ar,
                "lot_code": balance.lot.code if balance.lot else "",
                "unit": balance.item.base_unit.code,
                "quantity": remainder,
                "is_unlocated": True,
            }
        )

    return sorted(
        rows,
        key=lambda row: (row["warehouse_code"], row["item_code"], row["location_code"]),
    )


# ---------------------------------------------------------------------------
# 9. Inventory-to-GL reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationRow:
    """One control account's three figures, and whether they agree."""

    account_code: str
    account_name: str
    ledger_value: Decimal
    projection_value: Decimal
    gl_value: Decimal

    @property
    def difference(self) -> Decimal:
        return quantize_money(self.projection_value - self.gl_value)

    @property
    def agrees(self) -> bool:
        return self.difference == ZERO


def gl_reconciliation(organization: Organization) -> tuple[list[ReconciliationRow], list[Any]]:
    """
    Inventory value against the general ledger, per control account.

    Returns the per-account rows and the verifier's own discrepancy list. There
    is deliberately **no repair**: a difference here is evidence that something
    posted wrongly, and a button that made it go away would destroy the only
    signal that it happened.
    """
    discrepancies = verify_inventory_against_gl(organization)

    # The ledger side resolves each movement's control account through
    # `_control_account_of`, the same helper the verifier uses, because a
    # reversal carries no account of its own and inherits the one it reverses.
    # Re-deriving that here would be a second opinion about which account a
    # reversal belongs to, and the two would disagree the first time somebody
    # changed one of them.
    ledger: dict[int, Decimal] = {}
    for movement in StockMovement.objects.filter(organization=organization).select_related(
        "reverses", "control_account"
    ):
        account_id = _control_account_of(movement)
        if account_id is None:
            continue
        ledger[account_id] = ledger.get(account_id, ZERO) + movement.inventory_value

    projection: dict[int, Decimal] = {
        row["control_account_id"]: row["total"] or ZERO
        for row in StockBalance.objects.filter(
            organization=organization, control_account__isnull=False
        )
        .values("control_account_id")
        .annotate(total=Sum("value"))
    }

    account_ids = set(ledger) | set(projection)

    # **No status filter.** The verifier sums every line on these accounts on
    # purpose: a manual journal against inventory control is exactly the drift
    # this screen exists to surface, and so is the reversed half of a pair.
    # Filtering to POSTED here made this report disagree with the verifier by
    # the value of one reversed receipt.
    gl: dict[int, Decimal] = {}
    for journal_line in JournalLine.objects.filter(
        account__organization=organization, account_id__in=account_ids
    ):
        gl[journal_line.account_id] = (
            gl.get(journal_line.account_id, ZERO) + journal_line.debit - journal_line.credit
        )

    accounts = {account.pk: account for account in Account.objects.filter(pk__in=account_ids)}
    rows = [
        ReconciliationRow(
            account_code=accounts[account_id].code,
            account_name=accounts[account_id].name_ar,
            ledger_value=quantize_money(ledger.get(account_id, ZERO)),
            projection_value=quantize_money(projection.get(account_id, ZERO)),
            gl_value=quantize_money(gl.get(account_id, ZERO)),
        )
        for account_id in sorted(account_ids, key=lambda pk: accounts[pk].code)
    ]
    return rows, discrepancies
