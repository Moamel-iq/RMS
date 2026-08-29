"""
Working out which invoices a settlement pays, and in what order.

The operator arrives with a supplier and a target: the open balance of the
cycle multiplied by the share agreed to be paid by the due date. The target is
almost never the exact sum of any run of whole invoices, so this module names
the three honest answers rather than silently picking one.

## The three plans

    UNDER   as many of the oldest invoices as fit **without** passing the target
    OVER    the oldest invoices until the total first passes the target
    EXACT   whole invoices as far as they go, then part of the next one

`UNDER` and `OVER` settle whole invoices only — a supplier's ledger reads
better when an invoice is either paid or not, and a part-paid invoice is a
conversation neither side wants. `EXACT` exists because sometimes the money
that must move is the money that must move, and it is the only plan that
part-pays; it says so, and the screen shows which invoice it splits.

## FIFO, and why it is not negotiable here

Oldest first, always. Paying a newer invoice while an older one stands is how
a supplier's aging report and the business's own disagree, and the aging
report is what the supplier calls about. The plans differ in *where they
stop*, never in *what order they go*.

## What this module does not do

It computes; it posts nothing. Turning a plan into money is
`apps.procurement.payments`, which already knows the journal, the allocations
and the reversal. Keeping the arithmetic separate is what lets the screen show
the operator three answers before any of them is real.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from django.utils import timezone

from apps.core.money import quantize_money
from apps.procurement.cycles import collecting_cycle, cycle_invoices, days_remaining
from apps.procurement.invoices import outstanding_amount
from apps.procurement.models import Supplier, SupplierInvoice, SupplierPaymentCycle

ZERO = Decimal("0")


class PlanKind(StrEnum):
    """Which of the three answers a plan is."""

    UNDER = "UNDER"
    OVER = "OVER"
    EXACT = "EXACT"


#: What each plan is called on the screen, and what it promises.
PLAN_LABELS: dict[str, tuple[str, str]] = {
    PlanKind.UNDER: (
        "التسديد الأقل",
        "أكبر عدد من أقدم الفواتير كاملةً دون تجاوز المبلغ المستهدف.",
    ),
    PlanKind.OVER: (
        "التسديد الأعلى",
        "أقدم الفواتير كاملةً حتى يتجاوز مجموعها المبلغ المستهدف بأقل فرق.",
    ),
    PlanKind.EXACT: (
        "مطابقة المبلغ",
        "الفواتير الكاملة ثم جزء من التالية، ليطابق المسدَّد المستهدف بالضبط.",
    ),
}


@dataclass(frozen=True)
class PlannedAllocation:
    """
    One invoice, how much of it this plan pays, and what it owed.

    `outstanding` is carried rather than looked up again, for two reasons. A
    plan is a statement about a moment, and re-reading the balance while the
    screen is open would let the numbers shift under the operator between the
    total they were shown and the one they confirmed. And the three plans
    would otherwise each re-query every invoice — the same figure fetched
    three times to produce one answer.
    """

    invoice: SupplierInvoice
    amount: Decimal
    outstanding: Decimal

    @property
    def is_partial(self) -> bool:
        return self.amount < self.outstanding


@dataclass(frozen=True)
class SettlementPlan:
    """One way to reach — or miss — the target, with everything to show it."""

    kind: str
    allocations: list[PlannedAllocation] = field(default_factory=list)

    @property
    def label(self) -> str:
        return PLAN_LABELS[self.kind][0]

    @property
    def description(self) -> str:
        return PLAN_LABELS[self.kind][1]

    @property
    def total(self) -> Decimal:
        running: Decimal = sum((line.amount for line in self.allocations), ZERO)
        return quantize_money(running)

    @property
    def invoice_count(self) -> int:
        return len(self.allocations)

    @property
    def splits_an_invoice(self) -> bool:
        return any(line.is_partial for line in self.allocations)

    def difference_from(self, target: Decimal) -> Decimal:
        """Signed: negative is short of the target, positive is over it."""
        return quantize_money(self.total - target)


@dataclass(frozen=True)
class InvoiceRow:
    """One open invoice and the balance the plans were built from."""

    invoice: SupplierInvoice
    outstanding: Decimal


@dataclass(frozen=True)
class PlanRow:
    """One plan and how far it lands from the target. Signed."""

    plan: SettlementPlan
    difference: Decimal


@dataclass(frozen=True)
class CycleInvoiceRow:
    """One invoice in a cycle, and how it is sitting."""

    invoice: SupplierInvoice
    outstanding: Decimal
    settled: Decimal


@dataclass(frozen=True)
class SettlementWorkspace:
    """
    Everything the settlement screen shows before anybody commits to anything.

    A plan may be empty — a supplier with no open invoices, or a target
    smaller than the oldest invoice — and an empty plan is shown rather than
    hidden, because "nothing fits under 30 million" is the answer to the
    question the operator asked.
    """

    supplier: Supplier
    cycle: SupplierPaymentCycle | None
    #: The open invoices and what each still owes, read once. The screen needs
    #: both numbers on every row, and re-deriving the balance per invoice in a
    #: template would put a query behind a table cell.
    owings: list[Owing]
    open_total: Decimal
    minimum_percent: Decimal | None
    target: Decimal
    plans: list[SettlementPlan]
    on_date: datetime.date

    @property
    def invoices(self) -> list[SupplierInvoice]:
        """The open invoices alone, oldest first."""
        return [invoice for invoice, _owing in self.owings]

    @property
    def invoice_rows(self) -> list[InvoiceRow]:
        """One row per open invoice, carrying the balance the plans used."""
        return [InvoiceRow(invoice=invoice, outstanding=owing) for invoice, owing in self.owings]

    @property
    def plan_rows(self) -> list[PlanRow]:
        """
        Each plan beside its difference from the target.

        Computed here rather than in the template because `difference_from`
        takes the target as an argument, and a template cannot pass one — the
        alternative was a filter whose whole job would be to call this.
        """
        return [
            PlanRow(plan=plan, difference=plan.difference_from(self.target)) for plan in self.plans
        ]

    @property
    def target_exact(self) -> str:
        """
        The target as an exact string, for the hidden field that posts it back.

        Not `stringformat:"f"` in the template: Django's `%f` converts through
        a binary float, and this figure is re-entered and compared rather than
        merely read. `Decimal.__format__` is exact at any magnitude.
        """
        return format(self.target, "f")

    @property
    def open_total_exact(self) -> str:
        """The displayed balance, exactly, for the drift check to compare."""
        return format(self.open_total, "f")

    @property
    def days_remaining(self) -> int | None:
        return None if self.cycle is None else days_remaining(self.cycle, on=self.on_date)

    @property
    def is_overdue(self) -> bool:
        remaining = self.days_remaining
        return remaining is not None and remaining < 0


def open_owings(supplier: Supplier) -> list[Owing]:
    """
    The supplier's unsettled posted invoices, oldest first.

    Across every cycle rather than only the open one: money paid settles the
    oldest debt, and an invoice stranded in a cycle that expired unpaid is the
    oldest debt there is. The cycle decides *when* payment is due, never
    *which* invoice a dinar lands on.
    """
    from apps.procurement.models import SupplierInvoiceStatus

    invoices = (
        SupplierInvoice.objects.filter(supplier=supplier, status=SupplierInvoiceStatus.POSTED)
        .select_related("cycle")
        .order_by("invoice_date", "number", "pk")
    )
    owings = ((invoice, outstanding_amount(invoice)) for invoice in invoices)
    return [(invoice, owing) for invoice, owing in owings if owing > ZERO]


#: An invoice and what it still owes, read once and passed to every planner.
Owing = tuple[SupplierInvoice, Decimal]


def _under(owings: list[Owing], target: Decimal) -> SettlementPlan:
    """As many whole invoices as fit without passing the target."""
    lines: list[PlannedAllocation] = []
    running = ZERO
    for invoice, owing in owings:
        if running + owing > target:
            # FIFO: stop at the first that does not fit rather than skipping
            # ahead to a smaller one further down. Skipping would pay a newer
            # invoice while an older stands, which is the whole thing FIFO
            # exists to prevent.
            break
        lines.append(PlannedAllocation(invoice=invoice, amount=owing, outstanding=owing))
        running += owing
    return SettlementPlan(kind=PlanKind.UNDER, allocations=lines)


def _over(owings: list[Owing], target: Decimal) -> SettlementPlan:
    """The oldest invoices until the total first passes the target."""
    lines: list[PlannedAllocation] = []
    running = ZERO
    for invoice, owing in owings:
        lines.append(PlannedAllocation(invoice=invoice, amount=owing, outstanding=owing))
        running += owing
        if running >= target:
            break
    return SettlementPlan(kind=PlanKind.OVER, allocations=lines)


def _exact(owings: list[Owing], target: Decimal) -> SettlementPlan:
    """Whole invoices as far as they go, then part of the next one."""
    lines: list[PlannedAllocation] = []
    running = ZERO
    for invoice, owing in owings:
        if running >= target:
            break
        take = min(owing, quantize_money(target - running))
        if take <= ZERO:
            break
        lines.append(PlannedAllocation(invoice=invoice, amount=take, outstanding=owing))
        running += take
    return SettlementPlan(kind=PlanKind.EXACT, allocations=lines)


def target_for(open_total: Decimal, minimum_percent: Decimal | None) -> Decimal:
    """
    What must be paid by the due date: the open balance times the agreed share.

    With no agreed share the target is the whole balance, which is the honest
    default — a supplier who conceded no floor is owed all of it, and the
    screen should not invent a smaller number on their behalf.
    """
    if minimum_percent is None:
        return quantize_money(open_total)
    return quantize_money(open_total * minimum_percent / 100)


def workspace_for(
    supplier: Supplier, *, target: Decimal | None = None, on: datetime.date | None = None
) -> SettlementWorkspace:
    """
    The whole screen's worth of facts, computed once.

    `target` overrides the agreed amount, for the operator who types a figure
    of their own. Everything else is derived, so the three plans and the total
    they are compared against can never come from two different reads.
    """
    on_date = on or timezone.localdate()
    cycle = collecting_cycle(supplier)
    owings = open_owings(supplier)
    running: Decimal = sum((owing for _, owing in owings), ZERO)
    open_total = quantize_money(running)

    # The floor the cycle opened under, not the supplier's record today: a
    # renegotiation must not restate what this cycle was to be settled by.
    minimum = cycle.minimum_settlement_percent if cycle else supplier.minimum_settlement_percent
    wanted = quantize_money(target) if target is not None else target_for(open_total, minimum)

    plans = [_under(owings, wanted), _over(owings, wanted), _exact(owings, wanted)]
    # Two plans that pay the same invoices for the same money are one plan
    # wearing two names, and showing both invites the operator to look for a
    # difference that is not there.
    unique: list[SettlementPlan] = []
    for plan in plans:
        signature = [(line.invoice.pk, line.amount) for line in plan.allocations]
        if any(
            [(line.invoice.pk, line.amount) for line in kept.allocations] == signature
            for kept in unique
        ):
            continue
        unique.append(plan)

    return SettlementWorkspace(
        supplier=supplier,
        cycle=cycle,
        owings=owings,
        open_total=open_total,
        minimum_percent=minimum,
        target=wanted,
        plans=unique,
        on_date=on_date,
    )


def cycle_invoice_rows(cycle: SupplierPaymentCycle) -> list[CycleInvoiceRow]:
    """The cycle's invoices with what each still owes, for the detail panel."""
    rows: list[CycleInvoiceRow] = []
    for invoice in cycle_invoices(cycle):
        outstanding = outstanding_amount(invoice)
        rows.append(
            CycleInvoiceRow(
                invoice=invoice,
                outstanding=outstanding,
                settled=quantize_money((invoice.posted_amount or ZERO) - outstanding),
            )
        )
    return rows
