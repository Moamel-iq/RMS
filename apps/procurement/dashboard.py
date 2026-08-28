"""
The procurement overview, as one scoped read.

Two redactions rather than one, and they are not interchangeable:

* **Scope** comes from `visible_supplier_invoices`, which is deliberately
  narrower than the rest of this module (PRC-060). An invoice is money the
  organization owes, so reaching a branch is not enough — the caller needs
  authority over the organization itself. Counting invoices through a wider
  selector here would leak the size of another organization's debt.
* **Cost** comes from `procurement.view_supplier_cost`, decided by the caller
  and passed in. Amounts are **omitted** rather than zeroed, so a buyer
  without the permission gets a screen with fewer cards instead of a screen
  claiming the month's purchases were free.

Supplier concentration is the one derived figure here that is not a total: it
answers "how much of this month rests on one supplier", which is a risk
question a list of invoices never asks out loud.

Everything here is a read. Nothing writes, posts, or caches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, Sum

from apps.procurement.models import SupplierInvoiceStatus
from apps.procurement.selectors import visible_supplier_invoices, visible_suppliers
from apps.users.models import User

ZERO = Decimal("0")

#: Suppliers listed by value before the rest collapse into one "others" row.
TOP_SUPPLIERS = 6


@dataclass(frozen=True)
class SupplierRow:
    """One supplier's share of the period."""

    code: str
    name: str
    invoice_count: int
    total: Decimal | None = None
    share: Decimal | None = None


@dataclass(frozen=True)
class ProcurementOverview:
    """
    Everything the overview screen renders, already scoped and redacted.

    `posted_total`, `payable_total` and every `total` on a row are `None` for a
    caller without cost rights. The template asks `is not None`, so a missing
    figure removes its card rather than printing a zero.
    """

    supplier_count: int
    invoice_count: int
    posted_count: int
    draft_count: int
    approved_count: int
    rows: list[SupplierRow] = field(default_factory=list)
    posted_total: Decimal | None = None
    payable_total: Decimal | None = None

    @property
    def top_share(self) -> Decimal | None:
        """The largest supplier's share, which is the concentration risk."""
        if not self.rows or self.rows[0].share is None:
            return None
        return self.rows[0].share


def procurement_overview(user: User, *, include_cost: bool) -> ProcurementOverview:
    """
    Build the overview for every invoice `user` has authority over.

    `include_cost` is the caller's decision, not this function's: the view
    holds the request and therefore the permission, and passing it in keeps the
    redaction testable without a request object.
    """
    invoices = visible_supplier_invoices(user)
    live = invoices.exclude(status=SupplierInvoiceStatus.REVERSED)
    posted = live.filter(status=SupplierInvoiceStatus.POSTED)

    overview = ProcurementOverview(
        supplier_count=visible_suppliers(user).filter(is_active=True).count(),
        invoice_count=live.count(),
        posted_count=posted.count(),
        draft_count=live.filter(status=SupplierInvoiceStatus.DRAFT).count(),
        approved_count=live.filter(status=SupplierInvoiceStatus.APPROVED).count(),
    )
    if not include_cost:
        # Counts are not money and stay visible: a buyer still tracks how many
        # invoices are waiting, just not what they are worth.
        rows = [
            SupplierRow(
                code=entry["supplier__code"],
                name=entry["supplier__name"],
                invoice_count=entry["invoices"],
            )
            for entry in posted.values("supplier__code", "supplier__name")
            .annotate(invoices=Count("id"))
            .order_by("-invoices")[:TOP_SUPPLIERS]
        ]
        return ProcurementOverview(
            supplier_count=overview.supplier_count,
            invoice_count=overview.invoice_count,
            posted_count=overview.posted_count,
            draft_count=overview.draft_count,
            approved_count=overview.approved_count,
            rows=rows,
        )

    posted_total = posted.aggregate(total=Sum("total_amount"))["total"] or ZERO
    grouped = (
        posted.values("supplier__code", "supplier__name")
        .annotate(total=Sum("total_amount"), invoices=Count("id"))
        .order_by("-total")[:TOP_SUPPLIERS]
    )
    rows = [
        SupplierRow(
            code=entry["supplier__code"],
            name=entry["supplier__name"],
            invoice_count=entry["invoices"],
            total=entry["total"] or ZERO,
            share=(
                ((entry["total"] or ZERO) * 100 / posted_total).quantize(Decimal("0.1"))
                if posted_total
                else ZERO
            ),
        )
        for entry in grouped
    ]
    return ProcurementOverview(
        supplier_count=overview.supplier_count,
        invoice_count=overview.invoice_count,
        posted_count=overview.posted_count,
        draft_count=overview.draft_count,
        approved_count=overview.approved_count,
        rows=rows,
        posted_total=posted_total,
        # What is posted is what is owed until a payment is posted against it.
        payable_total=posted_total,
    )
