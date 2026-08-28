"""
The read screen over `Supplier.payment_terms_days` — main's credit-term summary.

Renamed out of `credit_terms.py` during the merge that brought the
effective-dated credit-term register in, for the reason its sibling
`additional_cost_workspace` records: two modules under one name, each with live
importers, each meaning something different by it.

The distinction is worth keeping straight. `credit_terms.py` is now the
register: terms with an effective range, drafted and activated, resolved for a
date. This module answers the flatter question its screen asks — what standing
terms do these suppliers carry, and how do the invoices that already snapshotted
them look now. The snapshot is what carries the correctness either way: an
invoice records the term it was created under, so changing a term never moves a
due date that was already agreed.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.procurement.models import Supplier, SupplierInvoice, SupplierInvoiceStatus

if TYPE_CHECKING:
    from django.utils.functional import Promise

    from apps.users.models import User

ZERO = Decimal("0")

#: Statuses whose invoices are still owed. A draft is not a debt and a reversed
#: invoice is not one either.
OPEN_INVOICE_STATUSES = frozenset({SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.POSTED})


def term_label(days: int) -> Promise | str:
    """
    The Arabic label for a term in days, following the owner's rule.

    Arabic pluralises differently from English and differently again between 2–10
    and 11+, which is why this is a function rather than a format string. Getting
    it wrong reads as broken to a native reader even when the number is right.
    """
    if days == 0:
        return _("عند الاستلام")
    if days == 1:
        return _("يوم واحد")
    if 2 <= days <= 10:
        return _("%(days)s أيام") % {"days": days}
    return _("%(days)s يوم") % {"days": days}


@dataclass(frozen=True)
class CreditTermFilters:
    """What narrows the credit-terms workspace."""

    search: str = ""
    #: `""`, `"active"` or `"archived"`.
    state: str = ""
    #: `""`, `"on_receipt"`, `"1_15"`, `"16_30"`, `"over_30"`.
    band: str = ""
    overdue_only: bool = False


@dataclass(frozen=True)
class SupplierTermRow:
    """One supplier, its default term, and how its invoices are sitting."""

    supplier: Supplier
    days: int
    open_count: int
    overdue_count: int
    next_due: datetime.date | None
    #: `None` when the caller may not read supplier cost. Absent, not zero.
    overdue_total: Decimal | None

    @property
    def label(self) -> Promise | str:
        return term_label(self.days)

    @property
    def has_overdue(self) -> bool:
        return self.overdue_count > 0


def _band_matches(days: int, band: str) -> bool:
    if band == "on_receipt":
        return days == 0
    if band == "1_15":
        return 1 <= days <= 15
    if band == "16_30":
        return 16 <= days <= 30
    if band == "over_30":
        return days > 30
    return True


def supplier_term_rows(
    user: User,
    filters: CreditTermFilters,
    *,
    today: datetime.date,
    include_cost: bool,
) -> list[SupplierTermRow]:
    """
    Every supplier this caller may read, with its term and invoice standing.

    `today` is a required argument rather than a default. Overdue is a claim
    about a date, and a screen that silently used the server clock would say
    something different at 23:59 and 00:01 with nothing having changed.

    `include_cost` is decided by the caller, once, so this function is not a
    second place that gets to decide who sees money.
    """
    from apps.procurement.selectors import visible_suppliers

    suppliers = visible_suppliers(user)
    if filters.search:
        term = filters.search.strip()
        suppliers = suppliers.filter(
            Q(code__icontains=term) | Q(name__icontains=term) | Q(name_en__icontains=term)
        )
    if filters.state == "active":
        suppliers = suppliers.filter(is_active=True)
    elif filters.state == "archived":
        suppliers = suppliers.filter(is_active=False)

    rows: list[SupplierTermRow] = []
    for supplier in suppliers.order_by("code"):
        days = supplier.payment_terms_days
        if not _band_matches(days, filters.band):
            continue

        open_invoices = SupplierInvoice.objects.filter(
            supplier=supplier, status__in=OPEN_INVOICE_STATUSES
        )
        overdue = open_invoices.filter(due_date__lt=today)
        overdue_count = overdue.count()
        if filters.overdue_only and overdue_count == 0:
            continue

        nxt = (
            open_invoices.filter(due_date__gte=today)
            .order_by("due_date")
            .values_list("due_date", flat=True)
            .first()
        )
        total: Decimal | None = None
        if include_cost:
            total = ZERO
            for amount in overdue.values_list("total_amount", flat=True):
                total += amount

        rows.append(
            SupplierTermRow(
                supplier=supplier,
                days=days,
                open_count=open_invoices.count(),
                overdue_count=overdue_count,
                next_due=nxt,
                overdue_total=total,
            )
        )
    return rows


@dataclass(frozen=True)
class InvoiceTermSnapshot:
    """
    What one invoice froze, beside what its supplier says today.

    Both figures are shown together on purpose: the whole point of the snapshot
    is that they are allowed to differ, and a screen that showed only one of
    them would make a correct system look broken to somebody who had just
    renegotiated.
    """

    invoice: SupplierInvoice
    snapshot_days: int
    supplier_days_now: int
    due_date: datetime.date
    days_remaining: int

    @property
    def drifted(self) -> bool:
        """The supplier's default has moved since this invoice was raised."""
        return self.snapshot_days != self.supplier_days_now

    @property
    def status_label(self) -> Promise:
        if self.days_remaining > 0:
            return _("غير مستحق")
        if self.days_remaining == 0:
            return _("يستحق اليوم")
        return _("متأخر")

    @property
    def is_overdue(self) -> bool:
        return self.days_remaining < 0


def snapshot_for(invoice: SupplierInvoice, *, today: datetime.date) -> InvoiceTermSnapshot:
    """One invoice's frozen terms, measured against an explicit date."""
    return InvoiceTermSnapshot(
        invoice=invoice,
        snapshot_days=invoice.payment_terms_days,
        supplier_days_now=invoice.supplier.payment_terms_days,
        due_date=invoice.due_date,
        days_remaining=(invoice.due_date - today).days,
    )


__all__ = [
    "OPEN_INVOICE_STATUSES",
    "CreditTermFilters",
    "InvoiceTermSnapshot",
    "SupplierTermRow",
    "snapshot_for",
    "supplier_term_rows",
    "term_label",
]
