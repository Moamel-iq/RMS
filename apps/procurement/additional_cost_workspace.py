"""
The read screen over ACCOUNT invoice lines — main's additional-cost workspace.

Renamed out of `additional_costs.py` during the merge that brought the charge
documents in. Both modules had been called `additional_costs` and meant
different things by it: that one is the domain service (draft a charge,
allocate it across the receipt lines it landed on, post and reverse the landed
cost), and this one reads the lines back for a screen.

Keeping both was not a preference. Each has live importers on its own side —
`invoices.py`, `models.py` and `api.py` reach for the service, `views.py` and
`test_financial_workspaces.py` for this — so dropping either would have deleted
working code rather than resolved a duplicate.

Still no posting control here, which was the original point: an additional cost
is billed on a supplier invoice and the invoice owns its posting, so there is
exactly one path to the ledger for a charge the supplier billed once.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Q, QuerySet

from apps.procurement.models import (
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceLineType,
    SupplierInvoiceStatus,
)

if TYPE_CHECKING:
    from apps.users.models import User

ZERO = Decimal("0")

#: The statuses in which an `ACCOUNT` line may still be edited or removed. A
#: line stops being editable the moment somebody other than its author has
#: agreed the claim is real.
EDITABLE_INVOICE_STATUSES = frozenset({SupplierInvoiceStatus.DRAFT})


@dataclass(frozen=True)
class AdditionalCostFilters:
    """What narrows the additional-cost workspace."""

    search: str = ""
    supplier_id: int | None = None
    invoice_id: int | None = None
    account_id: int | None = None
    cost_center_id: int | None = None
    status: str = ""
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    overdue_only: bool = False


def visible_additional_costs(user: User) -> QuerySet[SupplierInvoiceLine]:
    """
    Every `ACCOUNT` invoice line this caller may read.

    Scoped through `visible_supplier_invoices`, deliberately: an additional cost
    is part of an invoice, so it is readable exactly where its invoice is. A
    separate scope here would be a second answer to "who may see this charge",
    and the two would drift.
    """
    from apps.procurement.selectors import visible_supplier_invoices

    return SupplierInvoiceLine.objects.filter(
        line_type=SupplierInvoiceLineType.ACCOUNT,
        invoice__in=visible_supplier_invoices(user),
    ).select_related(
        "invoice",
        "invoice__supplier",
        "invoice__branch",
        "account",
        "cost_center",
        "journal_line",
    )


def additional_costs(
    user: User, filters: AdditionalCostFilters, *, today: datetime.date | None = None
) -> QuerySet[SupplierInvoiceLine]:
    """
    The filtered workspace, in a deterministic order.

    `today` is passed in rather than read here, so the overdue filter answers
    against the caller's explicit business date. A report whose meaning depends
    on the server clock is a report that changes overnight without anybody
    editing anything.
    """
    rows = visible_additional_costs(user)
    if filters.search:
        term = filters.search.strip()
        rows = rows.filter(
            Q(description__icontains=term)
            | Q(note__icontains=term)
            | Q(invoice__number__icontains=term)
            | Q(invoice__supplier_invoice_number__icontains=term)
            | Q(invoice__supplier__name_ar__icontains=term)
            | Q(invoice__supplier__code__icontains=term)
            | Q(account__code__icontains=term)
        )
    if filters.supplier_id:
        rows = rows.filter(invoice__supplier_id=filters.supplier_id)
    if filters.invoice_id:
        rows = rows.filter(invoice_id=filters.invoice_id)
    if filters.account_id:
        rows = rows.filter(account_id=filters.account_id)
    if filters.cost_center_id:
        rows = rows.filter(cost_center_id=filters.cost_center_id)
    if filters.status:
        rows = rows.filter(invoice__status=filters.status)
    if filters.date_from:
        rows = rows.filter(invoice__invoice_date__gte=filters.date_from)
    if filters.date_to:
        rows = rows.filter(invoice__invoice_date__lte=filters.date_to)
    if filters.overdue_only:
        rows = rows.filter(
            invoice__due_date__lt=today or datetime.date.today(),
            invoice__status__in=[SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.POSTED],
        )
    return rows.order_by("-invoice__invoice_date", "-invoice_id", "sequence")


def is_editable(line: SupplierInvoiceLine) -> bool:
    """
    Whether this cost may still be corrected in place.

    Editable only while its invoice is `DRAFT`. Once the invoice is `APPROVED`
    the claim has been agreed by a second person; once it is `POSTED` it has
    reached the ledger. After either, correction is the invoice's own
    reversal-and-replacement workflow, never an edit to one line.
    """
    return line.invoice.status in EDITABLE_INVOICE_STATUSES


def draftable_invoices_for(user: User, supplier_id: int | None = None) -> QuerySet[SupplierInvoice]:
    """
    The DRAFT invoices a new additional cost could be added to.

    An additional cost is never created on its own: it is a line on an invoice
    that already exists. Where a supplier has no draft invoice the screen offers
    to create one rather than silently creating and posting an invoice to hold
    the charge.
    """
    from apps.procurement.selectors import visible_supplier_invoices

    rows = visible_supplier_invoices(user).filter(status=SupplierInvoiceStatus.DRAFT)
    if supplier_id:
        rows = rows.filter(supplier_id=supplier_id)
    return rows.select_related("supplier").order_by("-invoice_date", "-pk")


@dataclass(frozen=True)
class AdditionalCostTotals:
    """What the filtered set adds up to, for the header strip."""

    line_count: int
    invoice_count: int
    posted_total: Decimal
    draft_total: Decimal

    @property
    def total(self) -> Decimal:
        return self.posted_total + self.draft_total


def totals_for(rows: QuerySet[SupplierInvoiceLine]) -> AdditionalCostTotals:
    """
    Totals split by whether the charge has reached the ledger.

    Posted and unposted are never added into one headline figure without saying
    so: one is money the organization owes, the other is a claim somebody is
    still typing.
    """
    posted = ZERO
    draft = ZERO
    invoices: set[int] = set()
    count = 0
    for line in rows:
        count += 1
        invoices.add(line.invoice_id)
        if line.invoice.status == SupplierInvoiceStatus.POSTED:
            posted += line.net_amount
        elif line.invoice.status != SupplierInvoiceStatus.REVERSED:
            draft += line.net_amount
    return AdditionalCostTotals(
        line_count=count,
        invoice_count=len(invoices),
        posted_total=posted,
        draft_total=draft,
    )


__all__ = [
    "EDITABLE_INVOICE_STATUSES",
    "AdditionalCostFilters",
    "AdditionalCostTotals",
    "additional_costs",
    "draftable_invoices_for",
    "is_editable",
    "totals_for",
    "visible_additional_costs",
]
