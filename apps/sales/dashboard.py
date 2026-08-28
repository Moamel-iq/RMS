"""
لوحة المبيعات — the aggregates behind the Sales dashboard.

**Reads only.** Nothing here writes, and nothing here re-decides. Every figure
is read back out of documents that already posted, through the caller's own
selectors, so a branch manager's dashboard and their sales-day list disagree
about nothing.

## Decimal, everywhere, without exception

No `float` appears in this module and none may be introduced. A dashboard is
where one would be most tempting and least visible: a percentage looks like a
display concern until somebody exports it, and `0.1 + 0.2` on a screen is a
figure the ledger cannot reproduce (ADR-027 §2). Shares are computed as
`Decimal` and quantized once for display; money is quantized once at the end of
an aggregate and never per term.

## Net revenue is the ledger's arithmetic, not a convenient one

    net_revenue = gross                            (Cr SALES_REVENUE)
                − restaurant_discount              (Dr SALES_DISCOUNT)
                − returns                          (Dr SALES_RETURNS)
                + returned_restaurant_discount     (Cr SALES_DISCOUNT)

That is exactly what the four accounts hold after a period, which is the point:
the number on this screen is one a person can find in the general ledger rather
than one only this module knows how to build. The application-funded discount
appears beside it and is **not** subtracted — it reduces neither revenue nor the
receivable, because the application reimburses it (ADR-028 §3), and subtracting
it here would make the dashboard disagree with every journal in the module.

## Cost is separately permitted, separately sourced, and separately honest

`cost_summary` is the only function here that touches cost, it is called only
when the caller holds `view_sales_cost`, and the view **omits** its whole card
otherwise rather than blanking it.

Recipe figures come from frozen `RecipeCostSnapshot` evidence. Direct-stock
resale figures come from the line's own immutable issue movement. Nothing is
re-costed here: both routes read the evidence that existed when the sale was
posted, never today's recipe or today's moving average.

Lines with no snapshot behind them are **counted and reported, never costed at
zero**. Margin is then computed over the costed lines only, and the uncosted
count is shown beside it. Mixing the two would divide a partial food cost by a
complete revenue and produce a food-cost percentage that is wrong in the
direction that looks good — which is the one direction nobody checks.

## Why the expensive halves are separate functions

`reconciliation_summary` rebuilds one `DailyReconciliation` per branch per day
and `cost_summary` walks the snapshot evidence. Both are real work, and both are
loaded by the view through their own htmx request so a slow card never holds up
the headline. They are separate functions here rather than one `dashboard()`
returning everything for exactly that reason.
"""

from __future__ import annotations

import bisect
import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from django.db.models import Count, Q, QuerySet, Sum
from django.utils.translation import gettext_lazy as _

from apps.core.money import quantize_money
from apps.sales.daily_reconciliation import reconcile_range
from apps.sales.models import (
    CashierShift,
    CashierShiftStatus,
    DeliveryApplicationSettlement,
    FulfillmentSource,
    MenuPriceVersion,
    PriceScope,
    SalesAdjustmentLine,
    SalesAdjustmentReasonKind,
    SalesAdjustmentStatus,
    SalesDay,
    SalesDayLine,
    SalesDayStatus,
    SettlementStatus,
    TenderDestination,
)
from apps.sales.receivables import ApplicationPosition, positions_for
from apps.sales.selectors import (
    visible_cashier_shifts,
    visible_sales_adjustments,
    visible_sales_days,
    visible_settlements,
)

if TYPE_CHECKING:
    from apps.users.models import User

ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: How many rows the "top items" card shows. Ten, because the card exists to
#: answer "what sells", and a list long enough to need scrolling answers it
#: worse than a list short enough to read.
TOP_ITEM_LIMIT = 10

#: The default window. A fortnight, matching المطابقة اليومية, so the two
#: screens open on the same period and a figure carried between them means the
#: same thing.
DEFAULT_WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DashboardScope:
    """
    Which organization, which branches and which dates a card is about.

    `branch_ids` of `None` means *every branch the caller reaches* rather than
    every branch — the narrowing is done by `visible_sales_days`, which is
    branch-scoped, and this field only narrows further. A filter that widened
    would be a filter that granted, which is the direction a filter must never
    fail in.
    """

    organization_id: int
    date_from: datetime.date
    date_to: datetime.date
    branch_ids: tuple[int, ...] | None = None


def _days(user: User, scope: DashboardScope) -> QuerySet[SalesDay]:
    rows = visible_sales_days(user).filter(
        organization_id=scope.organization_id,
        business_date__gte=scope.date_from,
        business_date__lte=scope.date_to,
    )
    if scope.branch_ids is not None:
        rows = rows.filter(branch_id__in=scope.branch_ids)
    return rows


def posted_days(user: User, scope: DashboardScope) -> QuerySet[SalesDay]:
    """The days that reached the ledger. A draft contributes to no figure here."""
    return _days(user, scope).filter(status=SalesDayStatus.POSTED)


def posted_lines(user: User, scope: DashboardScope) -> QuerySet[SalesDayLine]:
    return SalesDayLine.objects.filter(sales_day__in=posted_days(user, scope))


def posted_adjustment_lines(user: User, scope: DashboardScope) -> QuerySet[SalesAdjustmentLine]:
    """
    Adjustment lines that posted, dated by the **adjustment's** own business
    date rather than by the day it corrects.

    That is deliberate and it is the only defensible choice: a return decided in
    September against August's trading is September's return, and its journal
    carries September's accounting date. Filing it under August would make this
    screen disagree with the ledger it is summarising.
    """
    return SalesAdjustmentLine.objects.filter(
        adjustment__in=visible_sales_adjustments(user).filter(
            organization_id=scope.organization_id,
            status=SalesAdjustmentStatus.POSTED,
            business_date__gte=scope.date_from,
            business_date__lte=scope.date_to,
            **({"branch_id__in": scope.branch_ids} if scope.branch_ids is not None else {}),
        )
    )


def _sum(rows: QuerySet[Any], field: str) -> Decimal:
    return rows.aggregate(total=Sum(field))["total"] or ZERO


def _share(part: Decimal, whole: Decimal) -> Decimal:
    """
    A percentage as an exact Decimal, or zero when there is nothing to share.

    Quantized to two places once, here, rather than by a template filter: a
    share rounded at render time is a share nobody can reproduce from the two
    numbers beside it.
    """
    if whole == ZERO:
        return ZERO
    return (part / whole * HUNDRED).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Headline:
    """
    Every figure on the top strip of لوحة المبيعات.

    One dataclass rather than a dict, so a template that asks for a key this
    module never produced fails in a test rather than rendering an empty cell.
    """

    gross: Decimal
    restaurant_discount: Decimal
    application_discount: Decimal
    commission: Decimal
    other_fees: Decimal
    customer_charge: Decimal
    returns_gross: Decimal
    returned_discount: Decimal
    returns_net: Decimal
    cancelled_gross: Decimal
    corrected_gross: Decimal
    net_revenue: Decimal
    cash_sales: Decimal
    card_sales: Decimal
    application_sales: Decimal
    quantity: Decimal
    order_count: int
    line_count: int
    day_count: int
    draft_day_count: int
    reversed_day_count: int

    @property
    def discount_share(self) -> Decimal:
        """Restaurant-funded discount as a share of gross."""
        return _share(self.restaurant_discount, self.gross)

    @property
    def returns_share(self) -> Decimal:
        return _share(self.returns_gross, self.gross)

    @property
    def commission_share(self) -> Decimal:
        """Commission against **application** sales, not against all sales."""
        return _share(self.commission, self.application_sales + self.commission + self.other_fees)


def headline_for(user: User, scope: DashboardScope) -> Headline:
    """The top strip: what sold, what was given away, and what came back."""
    lines = posted_lines(user, scope)
    adjustments = posted_adjustment_lines(user, scope)

    gross = _sum(lines, "gross_amount")
    restaurant_discount = _sum(lines, "restaurant_discount")
    returns_gross = _sum(adjustments, "adjusted_gross")
    returned_discount = _sum(adjustments, "adjusted_restaurant_discount")

    by_kind = {
        row["adjustment__reason_kind"]: row["total"] or ZERO
        for row in adjustments.values("adjustment__reason_kind").annotate(
            total=Sum("adjusted_gross")
        )
    }

    cash = lines.filter(channel__default_tender=TenderDestination.CASH)
    card = lines.filter(channel__default_tender=TenderDestination.CARD)
    application = lines.filter(delivery_application__isnull=False)

    cash_returned = adjustments.filter(
        original_line__channel__default_tender=TenderDestination.CASH
    )
    card_returned = adjustments.filter(
        original_line__channel__default_tender=TenderDestination.CARD
    )
    application_returned = adjustments.filter(original_line__delivery_application__isnull=False)

    days = _days(user, scope)
    return Headline(
        gross=quantize_money(gross),
        restaurant_discount=quantize_money(restaurant_discount),
        application_discount=quantize_money(_sum(lines, "application_discount")),
        commission=quantize_money(
            _sum(lines, "commission_amount") - _sum(adjustments, "adjusted_commission")
        ),
        other_fees=quantize_money(
            _sum(lines, "other_fee_amount") - _sum(adjustments, "adjusted_other_fees")
        ),
        customer_charge=quantize_money(
            _sum(lines, "customer_charge") - _sum(adjustments, "adjusted_customer_charge")
        ),
        returns_gross=quantize_money(returns_gross),
        returned_discount=quantize_money(returned_discount),
        returns_net=quantize_money(_sum(adjustments, "adjusted_net_amount")),
        cancelled_gross=quantize_money(
            by_kind.get(SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT, ZERO)
        ),
        corrected_gross=quantize_money(
            by_kind.get(SalesAdjustmentReasonKind.FINANCIAL_CORRECTION, ZERO)
        ),
        # The ledger's own arithmetic, spelled out in the module docstring.
        net_revenue=quantize_money(gross - restaurant_discount - returns_gross + returned_discount),
        cash_sales=quantize_money(
            _sum(cash, "net_amount") - _sum(cash_returned, "adjusted_net_amount")
        ),
        card_sales=quantize_money(
            _sum(card, "net_amount") - _sum(card_returned, "adjusted_net_amount")
        ),
        application_sales=quantize_money(
            _sum(application, "net_amount") - _sum(application_returned, "adjusted_net_amount")
        ),
        quantity=_sum(lines, "quantity"),
        order_count=int(lines.aggregate(total=Sum("order_count"))["total"] or 0),
        line_count=lines.count(),
        day_count=days.filter(status=SalesDayStatus.POSTED).count(),
        draft_day_count=days.filter(
            status__in=[SalesDayStatus.DRAFT, SalesDayStatus.SUBMITTED]
        ).count(),
        reversed_day_count=days.filter(status=SalesDayStatus.REVERSED).count(),
    )


# ---------------------------------------------------------------------------
# Mixes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MixRow:
    """One slice of a mix: what it is, what it sold, and its share of gross."""

    code: str
    label: str
    gross: Decimal
    net: Decimal
    quantity: Decimal
    line_count: int
    share: Decimal


def _mix(
    lines: QuerySet[SalesDayLine], *, code_field: str, label_field: str, group: str
) -> list[MixRow]:
    rows = (
        lines.values(group, code_field, label_field)
        .annotate(
            gross=Sum("gross_amount"),
            net=Sum("net_amount"),
            quantity=Sum("quantity"),
            lines=Count("id"),
        )
        .order_by("-gross")
    )
    materialised = list(rows)
    total = sum((row["gross"] or ZERO for row in materialised), ZERO)
    return [
        MixRow(
            code=row[code_field] or "",
            label=row[label_field] or "",
            gross=quantize_money(row["gross"] or ZERO),
            net=quantize_money(row["net"] or ZERO),
            quantity=row["quantity"] or ZERO,
            line_count=row["lines"],
            share=_share(row["gross"] or ZERO, total),
        )
        for row in materialised
    ]


def channel_mix(user: User, scope: DashboardScope) -> list[MixRow]:
    """Gross by sales channel, largest first."""
    return _mix(
        posted_lines(user, scope),
        code_field="channel__code",
        label_field="channel__name",
        group="channel_id",
    )


def application_mix(user: User, scope: DashboardScope) -> list[MixRow]:
    """Gross by delivery application. Hall and takeaway lines are not here."""
    return _mix(
        posted_lines(user, scope).filter(delivery_application__isnull=False),
        code_field="delivery_application__code",
        label_field="delivery_application__name",
        group="delivery_application_id",
    )


def top_menu_items(
    user: User, scope: DashboardScope, *, limit: int = TOP_ITEM_LIMIT
) -> list[MixRow]:
    """The best-selling items by gross. Ties break on quantity, then on code."""
    rows = (
        posted_lines(user, scope)
        .values("menu_item_id", "menu_item__code", "menu_item__name")
        .annotate(
            gross=Sum("gross_amount"),
            net=Sum("net_amount"),
            quantity=Sum("quantity"),
            lines=Count("id"),
        )
        .order_by("-gross", "-quantity", "menu_item__code")
    )
    materialised = list(rows[:limit])
    total = _sum(posted_lines(user, scope), "gross_amount")
    return [
        MixRow(
            code=row["menu_item__code"] or "",
            label=row["menu_item__name"] or "",
            gross=quantize_money(row["gross"] or ZERO),
            net=quantize_money(row["net"] or ZERO),
            quantity=row["quantity"] or ZERO,
            line_count=row["lines"],
            share=_share(row["gross"] or ZERO, total),
        )
        for row in materialised
    ]


# ---------------------------------------------------------------------------
# Returns and cancellations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReturnRow:
    """One reason kind, with the quantity it did or did not take back."""

    reason_kind: str
    label: Any
    gross: Decimal
    net: Decimal
    quantity: Decimal
    line_count: int
    reduces_consumption: bool


def returns_breakdown(user: User, scope: DashboardScope) -> list[ReturnRow]:
    """
    Posted adjustments by reason kind, carrying `reduces_consumption`.

    That flag is on the row rather than left to the reader because the
    asymmetry is the single most misread thing in this module: only
    `CANCELLED_BEFORE_FULFILLMENT` reduces theoretical consumption. A return
    was cooked, its ingredients left, and subtracting it would manufacture an
    unexplained usage variance of exactly the returned quantity (ADR-028 §8).
    Showing all three kinds side by side with the flag visible is cheaper than
    explaining it again every time somebody reads the screen.
    """
    labels = dict(SalesAdjustmentReasonKind.choices)
    rows = (
        posted_adjustment_lines(user, scope)
        .values("adjustment__reason_kind")
        .annotate(
            gross=Sum("adjusted_gross"),
            net=Sum("adjusted_net_amount"),
            quantity=Sum("adjusted_quantity"),
            lines=Count("id"),
        )
        .order_by("-gross")
    )
    return [
        ReturnRow(
            reason_kind=row["adjustment__reason_kind"],
            label=labels.get(row["adjustment__reason_kind"], row["adjustment__reason_kind"]),
            gross=quantize_money(row["gross"] or ZERO),
            net=quantize_money(row["net"] or ZERO),
            quantity=row["quantity"] or ZERO,
            line_count=row["lines"],
            reduces_consumption=(
                row["adjustment__reason_kind"]
                == SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT
            ),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Receivables and settlements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceivableSummary:
    """What the delivery companies owe, and how old it is."""

    positions: tuple[ApplicationPosition, ...]
    outstanding: Decimal
    overdue: Decimal
    settlements_posted: int
    settlements_open: int
    settled_in_period: Decimal
    unexplained_open: Decimal

    @property
    def overdue_share(self) -> Decimal:
        return _share(self.overdue, self.outstanding)


def receivable_summary(user: User, scope: DashboardScope) -> ReceivableSummary:
    """
    The receivable position as of the window's end, plus the settlements over it.

    `overdue` is everything past ninety days, read off the aging buckets rather
    than recomputed — the buckets are the module's one definition of age and a
    second one here would eventually disagree with the ذمم التطبيقات screen.

    Nothing is written off. An aged balance stays a balance until a settlement
    or an authorized adjustment clears it (ADR-028 §9).
    """
    positions = positions_for(user, organization_id=scope.organization_id, as_of=scope.date_to)
    outstanding = sum((row.balance for row in positions), ZERO)
    overdue = sum(
        (bucket.amount for row in positions for bucket in row.buckets if bucket.days_to is None),
        ZERO,
    )

    settlements = visible_settlements(user).filter(
        organization_id=scope.organization_id,
        business_date__gte=scope.date_from,
        business_date__lte=scope.date_to,
    )
    if scope.branch_ids is not None:
        settlements = settlements.filter(branch_id__in=scope.branch_ids)

    posted = settlements.filter(status=SettlementStatus.POSTED)
    unexplained = sum(
        (
            _unexplained_of(settlement)
            for settlement in settlements.filter(
                status__in=[SettlementStatus.DRAFT, SettlementStatus.RECONCILED]
            )
        ),
        ZERO,
    )
    return ReceivableSummary(
        positions=tuple(positions),
        outstanding=quantize_money(outstanding),
        overdue=quantize_money(overdue),
        settlements_posted=posted.count(),
        settlements_open=settlements.filter(
            status__in=[SettlementStatus.DRAFT, SettlementStatus.RECONCILED]
        ).count(),
        settled_in_period=quantize_money(_sum(posted, "expected_amount")),
        unexplained_open=quantize_money(unexplained),
    )


def _unexplained_of(settlement: DeliveryApplicationSettlement) -> Decimal:
    """
    How much of an open settlement is still unclaimed, both legs together.

    Summed as absolute values rather than netted: a statement leg short by 100
    and a remittance leg over by 100 is two arguments with the counterparty, not
    a settlement that agrees. Netting them would report zero and let the pair
    reconcile, which is exactly what ADR-028 §7 refuses.
    """
    from apps.sales.settlement_services import three_way_for

    three_way = three_way_for(settlement)
    return abs(three_way.unexplained_statement) + abs(three_way.unexplained_remittance)


# ---------------------------------------------------------------------------
# The till
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CashierSummary:
    """Every drawer in the window, and what it was out by."""

    shifts: int
    open_shifts: int
    closed_shifts: int
    approved_shifts: int
    variance: Decimal
    shortage: Decimal
    overage: Decimal
    counted_cash: Decimal
    expected_cash: Decimal
    worst_shortage: CashierShift | None


def cashier_summary(user: User, scope: DashboardScope) -> CashierSummary:
    """
    The till figures, with shortage and overage kept apart.

    Two figures rather than one net variance, and the separation is the whole
    value: a month of alternating 5,000 shortages and 5,000 overages nets to
    zero and is a serious control finding, while a month that is genuinely
    clean also nets to zero. One number cannot tell them apart.
    """
    shifts = visible_cashier_shifts(user).filter(
        organization_id=scope.organization_id,
        business_date__gte=scope.date_from,
        business_date__lte=scope.date_to,
    )
    if scope.branch_ids is not None:
        shifts = shifts.filter(branch_id__in=scope.branch_ids)

    #: Only an **approved** shift has posted its difference, so only an approved
    #: shift's variance is a figure the ledger agrees with.
    approved = shifts.filter(status=CashierShiftStatus.APPROVED)
    shortage = _sum(approved.filter(variance_amount__lt=ZERO), "variance_amount")
    overage = _sum(approved.filter(variance_amount__gt=ZERO), "variance_amount")
    return CashierSummary(
        shifts=shifts.count(),
        open_shifts=shifts.filter(status=CashierShiftStatus.OPEN).count(),
        closed_shifts=shifts.filter(status=CashierShiftStatus.CLOSED).count(),
        approved_shifts=approved.count(),
        variance=quantize_money(shortage + overage),
        shortage=quantize_money(-shortage),
        overage=quantize_money(overage),
        counted_cash=quantize_money(_sum(approved, "counted_cash")),
        expected_cash=quantize_money(_sum(approved, "expected_cash")),
        worst_shortage=approved.order_by("variance_amount").first(),
    )


# ---------------------------------------------------------------------------
# Reconciliation status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationSummary:
    """How many of the window's days reconcile, and how many do not."""

    days: int
    clean: int
    errors: int
    advisories: int
    limitations: int
    dirty_days: tuple[Any, ...]

    @property
    def clean_share(self) -> Decimal:
        return _share(Decimal(self.clean), Decimal(self.days))


def reconciliation_summary(user: User, scope: DashboardScope) -> ReconciliationSummary:
    """
    المطابقة اليومية, counted rather than listed.

    Composed from `reconcile_range` rather than re-derived, so the dashboard and
    the report cannot disagree about whether a day is clean. This is the
    expensive card on the screen and it is loaded through its own htmx request
    for that reason.
    """
    rows = reconcile_range(
        user,
        branch_ids=list(scope.branch_ids) if scope.branch_ids is not None else None,
        date_from=scope.date_from,
        date_to=scope.date_to,
    )
    rows = [row for row in rows if row.branch.organization_id == scope.organization_id]
    dirty = [row for row in rows if not row.is_clean]
    return ReconciliationSummary(
        days=len(rows),
        clean=len([row for row in rows if row.is_clean]),
        errors=sum(len(row.errors) for row in rows),
        advisories=sum(len(row.advisories) for row in rows),
        limitations=sum(len(row.limitations) for row in rows),
        dirty_days=tuple(dirty),
    )


# ---------------------------------------------------------------------------
# Cost and margin — `view_sales_cost` only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostSummary:
    """
    Food cost and margin over the lines that actually carry cost evidence.

    `uncosted_lines` and `uncosted_gross` sit beside every figure here on
    purpose. A margin computed over part of the revenue and presented as if it
    covered all of it is worse than no margin at all, and the failure is silent:
    the percentage is simply lower than the truth, which is the direction that
    never gets questioned.
    """

    costed_gross: Decimal
    costed_restaurant_discount: Decimal
    costed_net: Decimal
    food_cost: Decimal
    gross_profit: Decimal
    uncosted_lines: int
    uncosted_gross: Decimal
    costed_lines: int

    @property
    def food_cost_percent(self) -> Decimal:
        return _share(self.food_cost, self.costed_net)

    @property
    def margin_percent(self) -> Decimal:
        return _share(self.gross_profit, self.costed_net)

    @property
    def is_complete(self) -> bool:
        """Whether every posted line in the window had a snapshot behind it."""
        return self.uncosted_lines == 0


def cost_summary(user: User, scope: DashboardScope) -> CostSummary:
    """
    Value recipe lines from snapshots and direct-stock lines from posted issues.

    The lookup, and why it is shaped this way:

    1. Posted lines are grouped by `(serving, business_date)` first, so the
       Python loop below runs once per distinct serving-and-date rather than
       once per line. A branch selling twelve items every day for a fortnight
       is a few dozen groups, not a few hundred rows.
    2. Every authoritative snapshot serving for those servings, dated at or
       before the window's end, is fetched in **one** query and indexed by
       serving.
    3. Each group takes the latest snapshot at or before **its own** business
       date. Not the window's end, and not today: valuing August with a
       September snapshot restates August using purchase prices August never
       saw, which is precisely what a frozen snapshot exists to prevent.

    A group with no snapshot at or before its date is counted as uncosted and
    contributes nothing to `food_cost`. It is never valued at zero.
    """
    from apps.kitchen.models import RecipeCostSnapshotServing

    base_lines = posted_lines(user, scope)
    groups = list(
        base_lines.filter(fulfillment_source=FulfillmentSource.RECIPE_SERVING)
        .values("serving_id", "sales_day__business_date")
        .annotate(
            quantity=Sum("quantity"),
            gross=Sum("gross_amount"),
            restaurant_discount=Sum("restaurant_discount"),
            lines=Count("id"),
        )
    )
    direct_rows = list(
        base_lines.filter(fulfillment_source=FulfillmentSource.DIRECT_STOCK).values(
            "pk",
            "gross_amount",
            "restaurant_discount",
            "direct_stock_fulfillment__cogs_value",
        )
    )
    if not groups and not direct_rows:
        return CostSummary(ZERO, ZERO, ZERO, ZERO, ZERO, 0, ZERO, 0)

    serving_ids = {row["serving_id"] for row in groups}
    evidence: dict[int, tuple[list[datetime.date], list[Decimal]]] = {}
    snapshot_rows = (
        RecipeCostSnapshotServing.objects.filter(
            serving_id__in=serving_ids,
            snapshot__organization_id=scope.organization_id,
            snapshot__is_authoritative=True,
            snapshot__as_of_date__lte=scope.date_to,
        )
        .values("serving_id", "snapshot__as_of_date", "cost_per_serving")
        .order_by("serving_id", "snapshot__as_of_date", "snapshot_id")
    )
    for evidence_row in snapshot_rows:
        dates, costs = evidence.setdefault(evidence_row["serving_id"], ([], []))
        dates.append(evidence_row["snapshot__as_of_date"])
        costs.append(evidence_row["cost_per_serving"])

    costed_gross = ZERO
    costed_discount = ZERO
    food_cost = ZERO
    costed_lines = 0
    uncosted_lines = 0
    uncosted_gross = ZERO

    for row in groups:
        cost = _cost_at(evidence.get(row["serving_id"]), row["sales_day__business_date"])
        if cost is None:
            uncosted_lines += row["lines"]
            uncosted_gross += row["gross"] or ZERO
            continue
        costed_lines += row["lines"]
        costed_gross += row["gross"] or ZERO
        costed_discount += row["restaurant_discount"] or ZERO
        food_cost += (row["quantity"] or ZERO) * cost

    # A resale line carries stronger evidence than a recipe snapshot: its own
    # posted stock movement at the moving average of that exact posting. Read
    # the frozen fulfillment allocation; never revalue it from today's stock.
    for direct_row in direct_rows:
        # The reverse one-to-one traversal is a LEFT OUTER JOIN, so a line with
        # no fulfillment row reads back as None. django-stubs types the column
        # as a bare Decimal; widen it so the uncosted branch stays reachable.
        cost = cast(Decimal | None, direct_row["direct_stock_fulfillment__cogs_value"])
        if cost is None:
            uncosted_lines += 1
            uncosted_gross += direct_row["gross_amount"] or ZERO
            continue
        costed_lines += 1
        costed_gross += direct_row["gross_amount"] or ZERO
        costed_discount += direct_row["restaurant_discount"] or ZERO
        food_cost += cost

    costed_net = costed_gross - costed_discount
    quantized_cost = quantize_money(food_cost)
    return CostSummary(
        costed_gross=quantize_money(costed_gross),
        costed_restaurant_discount=quantize_money(costed_discount),
        costed_net=quantize_money(costed_net),
        food_cost=quantized_cost,
        gross_profit=quantize_money(costed_net) - quantized_cost,
        uncosted_lines=uncosted_lines,
        uncosted_gross=quantize_money(uncosted_gross),
        costed_lines=costed_lines,
    )


def _cost_at(
    evidence: tuple[list[datetime.date], list[Decimal]] | None, on_date: datetime.date
) -> Decimal | None:
    """
    The latest snapshot cost at or before a date, or `None` when there is none.

    `bisect` over the already-sorted dates rather than a query per group: the
    rows arrive ordered from the database and re-asking it per group would be
    one round trip per serving per day.
    """
    if evidence is None:
        return None
    dates, costs = evidence
    position = bisect.bisect_right(dates, on_date)
    if position == 0:
        return None
    return costs[position - 1]


# ---------------------------------------------------------------------------
# Card registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PricePremiumRow:
    """One base→channel price pair and the items that carry it."""

    base_price: Decimal
    channel_price: Decimal
    items: tuple[str, ...]

    @property
    def premium_share(self) -> Decimal:
        """The channel uplift as a share of the base price, 1 dp."""
        return _share(self.channel_price - self.base_price, self.base_price)


def price_premiums(user: User, scope: DashboardScope) -> list[PricePremiumRow]:
    """
    Where the channel price sits above the hall price, grouped by the pair.

    Reads the price lists, not the sales lines, so it answers on a day with no
    posted sale. Only an *active* channel price on or before `date_to` counts,
    compared with the branch default in force on the same day, and only pairs
    where the channel is dearer are listed — a channel price equal to the hall
    price is not a premium, and a cheaper one is a different screen's problem.
    """
    from apps.organizations.selectors import accessible_branches

    branches = accessible_branches(user).filter(organization_id=scope.organization_id)
    if scope.branch_ids is not None:
        branches = branches.filter(pk__in=scope.branch_ids)
    on = scope.date_to

    def _in_force(queryset: QuerySet[MenuPriceVersion]) -> QuerySet[MenuPriceVersion]:
        return queryset.filter(is_active=True, effective_from__lte=on).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=on)
        )

    base = {
        (row.menu_item_id, row.branch_id): row.unit_price
        for row in _in_force(
            MenuPriceVersion.objects.filter(branch__in=branches, scope=PriceScope.BRANCH_DEFAULT)
        ).order_by("effective_from")
    }
    pairs: dict[tuple[Decimal, Decimal], list[str]] = {}
    for row in (
        _in_force(MenuPriceVersion.objects.filter(branch__in=branches, scope=PriceScope.CHANNEL))
        .select_related("menu_item")
        .order_by("menu_item__display_order", "menu_item__code")
    ):
        hall = base.get((row.menu_item_id, row.branch_id))
        if hall is None or row.unit_price <= hall:
            continue
        pairs.setdefault((hall, row.unit_price), []).append(row.menu_item.name)
    return [
        PricePremiumRow(base_price=hall, channel_price=channel, items=tuple(items))
        for (hall, channel), items in sorted(pairs.items(), key=lambda kv: -len(kv[1]))
    ]


@dataclass(frozen=True)
class Card:
    """
    One independently-loaded dashboard card.

    Declared as data so the view, the template and the route all read the same
    list. A card added here appears on the screen, is fetched by its own htmx
    request and is smoke-testable by slug, without three edits that can
    disagree.
    """

    slug: str
    label: Any
    template: str
    #: `True` when the card carries cost, margin or food-cost figures. The view
    #: **omits** such a card entirely without `view_sales_cost` — it is never
    #: rendered blank, because a blank card says a number exists and that the
    #: reader is not trusted with it, which is a different statement.
    needs_cost: bool = False


CARDS: tuple[Card, ...] = (
    Card("channels", _("مزيج القنوات"), "sales/cards/_channels.html"),
    Card("applications", _("مزيج التطبيقات"), "sales/cards/_applications.html"),
    Card("items", _("الأصناف الأكثر مبيعاً"), "sales/cards/_items.html"),
    Card("prices", _("أسعار التطبيقات"), "sales/cards/_prices.html"),
    Card("returns", _("المرتجعات والإلغاءات"), "sales/cards/_returns.html"),
    Card("receivables", _("ذمم التطبيقات"), "sales/cards/_receivables.html"),
    Card("cashier", _("فروقات الصندوق"), "sales/cards/_cashier.html"),
    Card("reconciliation", _("حالة المطابقة اليومية"), "sales/cards/_reconciliation.html"),
    Card("cost", _("الكلفة والهامش"), "sales/cards/_cost.html", needs_cost=True),
)

CARDS_BY_SLUG: dict[str, Card] = {card.slug: card for card in CARDS}


def card_context(slug: str, user: User, scope: DashboardScope) -> dict[str, Any]:
    """Build exactly the one card's data. Nothing else is computed."""
    if slug == "channels":
        return {"rows": channel_mix(user, scope)}
    if slug == "applications":
        return {"rows": application_mix(user, scope)}
    if slug == "items":
        return {"rows": top_menu_items(user, scope)}
    if slug == "prices":
        return {"rows": price_premiums(user, scope)}
    if slug == "returns":
        return {"rows": returns_breakdown(user, scope)}
    if slug == "receivables":
        return {"summary": receivable_summary(user, scope)}
    if slug == "cashier":
        return {"summary": cashier_summary(user, scope)}
    if slug == "reconciliation":
        return {"summary": reconciliation_summary(user, scope)}
    if slug == "cost":
        return {"summary": cost_summary(user, scope)}
    raise KeyError(slug)  # pragma: no cover - the view resolves the slug first


def default_window(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """The dashboard's opening period. A fortnight ending today."""
    return today - datetime.timedelta(days=DEFAULT_WINDOW_DAYS), today


def branch_ids_of(branches: Sequence[Any]) -> tuple[int, ...]:
    return tuple(branch.pk for branch in branches)


__all__ = [
    "CARDS",
    "CARDS_BY_SLUG",
    "DEFAULT_WINDOW_DAYS",
    "TOP_ITEM_LIMIT",
    "Card",
    "CashierSummary",
    "CostSummary",
    "DashboardScope",
    "Headline",
    "MixRow",
    "ReceivableSummary",
    "ReconciliationSummary",
    "ReturnRow",
    "application_mix",
    "branch_ids_of",
    "card_context",
    "cashier_summary",
    "channel_mix",
    "cost_summary",
    "default_window",
    "headline_for",
    "posted_adjustment_lines",
    "posted_days",
    "posted_lines",
    "receivable_summary",
    "reconciliation_summary",
    "returns_breakdown",
    "top_menu_items",
]
