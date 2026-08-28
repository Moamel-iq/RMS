"""
ذمم الموردين and ذمم التطبيقات — the two reconciliation workspaces.

**Neither creates a model.** Supplier liability lives in Procurement's invoice,
credit-note, payment and allocation graph; delivery-application receivable lives
in Sales's append-only `ApplicationReceivableEntry`. Accounting builds no second
copy of either (ADR-029 §4).

The rejected alternative was a maintained balance table, which is faster to read
and has one disqualifying property: when it drifts, both sides of the
reconciliation come from the same drifted number, so the reconciliation reports
agreement. A balance that can disagree with its own movements is exactly the
failure this architecture exists to prevent.

So each page computes two numbers from two independent sources and puts them
side by side: the **subledger** side from the owning module's own report
service, and the **GL** side from the account carrying the role. Where the two
disagree the page names the amount and repairs nothing — an automatic plug
would make the two sides agree while making the books wrong, and would do it
without anyone reading the difference that explained the cause.

Reuse, not re-derivation. `apps.procurement.reports.supplier_aging` and
`supplier_statement`, and `apps.sales.receivables.positions_for` and
`ledger_for`, already exist and are already tested. A second formula here would
agree with them until the day it did not, and then there would be two answers
and no way to tell which was wrong.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.models import DELIVERY_APP_RECEIVABLE, SUPPLIER_PAYABLE
from apps.accounting.permissions import (
    VIEW_APPLICATION_RECEIVABLES,
    VIEW_SUPPLIER_LIABILITIES,
)
from apps.accounting.selectors import account_balance
from apps.accounting.services import resolve_default_account
from apps.accounting.views import AccountingDetailView, AccountingViewMixin
from apps.organizations.authorization import OutOfScope, organizations_with_permission
from apps.organizations.models import Organization

ZERO = Decimal("0")


def _chosen_organization(actor: Any, request: HttpRequest, permission: str) -> Organization | None:
    """
    The organization in view, resolved **with** the caller.

    A submitted id can only ever select from what the caller already reaches, so
    a guessed id finds nothing rather than being fetched and then refused.
    """
    organizations = organizations_with_permission(actor, permission).order_by("code")
    raw = request.GET.get("organization", "").strip()
    if raw.isdigit():
        organization = organizations.filter(pk=int(raw)).first()
        if organization is None:
            raise OutOfScope(_("Organization does not exist."))
        return organization
    return organizations.first()


def _role_balance(
    organization: Organization, role: str, on_date: datetime.date
) -> tuple[Decimal, Any]:
    """
    The GL side: the balance of the account that carries this role on a date.

    Resolved through `resolve_default_account`, the same indirection the posting
    services use — so the account this reconciliation checks is provably the
    account the postings landed in, rather than one named by a code somebody
    typed into a report.
    """
    try:
        # `resolve_default_account` returns the **mapping**, not the account.
        # Using its result directly would balance the wrong object and link to
        # the wrong id, and both would look plausible on the page.
        mapping = resolve_default_account(
            organization=organization, account_role=role, on_date=on_date
        )
    except ValidationError:
        # Unmapped. Reported as a missing account rather than as a zero, because
        # zero would read as "nothing is owed" when the truth is "nobody has
        # said where this lands".
        return ZERO, None
    account = mapping.account
    return account_balance(account=account, up_to=on_date), account


# ---------------------------------------------------------------------------
# ذمم الموردين
# ---------------------------------------------------------------------------


class SupplierLiabilityListView(AccountingViewMixin, View):
    """
    What is owed to every supplier, aged, beside what the GL says in total.

    The aging comes from `procurement.reports.supplier_aging`, which buckets on
    the **invoice's own due-date snapshot** rather than on today's payment
    terms: renegotiating a supplier's terms in March must not silently re-age
    January's invoices.
    """

    required_permission = VIEW_SUPPLIER_LIABILITIES
    template_name = "accounting/supplier_liability_list.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.procurement.reconciliation import verify_supplier_payables
        from apps.procurement.reports import ProcurementReportFilters, supplier_aging

        organizations = organizations_with_permission(
            self.actor, VIEW_SUPPLIER_LIABILITIES
        ).order_by("code")
        organization = _chosen_organization(self.actor, request, VIEW_SUPPLIER_LIABILITIES)

        raw_as_of = request.GET.get("as_of", "").strip()
        try:
            as_of = datetime.date.fromisoformat(raw_as_of) if raw_as_of else timezone.localdate()
        except ValueError:
            as_of = timezone.localdate()

        rows: list[dict[str, Any]] = []
        subledger_total = ZERO
        gl_balance = ZERO
        gl_account = None

        findings: list[Any] = []
        if organization is not None:
            filters = ProcurementReportFilters(organization_id=organization.pk)
            rows = supplier_aging(self.actor, filters, include_cost=True)
            # `net_position` is `supplier_outstanding` — posted invoices less
            # posted credit notes less **allocated** payments. That is the
            # figure the payable account actually carries; `open_total` is the
            # aged open-invoice total, which deliberately excludes standing
            # credit and advances and would therefore never tie to the GL.
            subledger_total = sum((row.get("net_position") or ZERO for row in rows), ZERO)
            gl_balance, gl_account = _role_balance(organization, SUPPLIER_PAYABLE, as_of)
            # Procurement's own check, forwarded rather than re-derived. A
            # second opinion here would agree with it until the day it did not.
            findings = verify_supplier_payables(organization)

        bucket = request.GET.get("bucket", "").strip()
        if bucket in {"current", "d30", "d60", "d90", "older"}:
            rows = [row for row in rows if (row.get(bucket) or ZERO) != ZERO]
        search = request.GET.get("q", "").strip().lower()
        if search:
            rows = [
                row
                for row in rows
                if search in str(row.get("supplier_name", "")).lower()
                or search in str(row.get("supplier_code", "")).lower()
            ]

        # A supplier payable is a credit balance in the GL, so the account's
        # signed balance is negative when money is owed. Compared as magnitudes
        # against the subledger, which reports what is owed as a positive.
        difference = subledger_total + gl_balance

        return self.render(
            request,
            {
                "organizations": organizations,
                "organization": organization,
                "as_of": as_of,
                "rows": rows,
                "subledger_total": subledger_total,
                "gl_balance": gl_balance,
                "gl_account": gl_account,
                "difference": difference,
                "reconciled": difference == ZERO and not findings,
                "findings": findings,
                "selected_bucket": bucket,
                "search": search,
                "page_title": _("ذمم الموردين"),
                "page_hint": _(
                    "الرصيد مشتق من مستندات المشتريات، ولا يوجد جدول أرصدة موردين. "
                    "أي فرق يُعرض ولا يُصلَح تلقائياً."
                ),
            },
        )

    def render(self, request: HttpRequest, context: dict[str, Any]) -> HttpResponse:
        from django.shortcuts import render as django_render

        context.setdefault(
            "list_base_template",
            "settings/_form_fragment.html"
            if request.headers.get("HX-Request") == "true"
            else "shell.html",
        )
        context.setdefault("inventory_ui", False)
        return django_render(request, self.template_name, context)


class SupplierLiabilityDetailView(AccountingDetailView):
    """One supplier's statement, in the order an auditor reads it."""

    required_permission = VIEW_SUPPLIER_LIABILITIES
    template_name = "accounting/supplier_liability_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.procurement.reports import ProcurementReportFilters, supplier_statement
        from apps.procurement.selectors import resolve_supplier

        supplier = resolve_supplier(self.actor, kwargs["pk"])
        raw_as_of = request.GET.get("as_of", "").strip()
        try:
            as_of = datetime.date.fromisoformat(raw_as_of) if raw_as_of else timezone.localdate()
        except ValueError:
            as_of = timezone.localdate()

        filters = ProcurementReportFilters(
            organization_id=supplier.organization_id, supplier_id=supplier.pk
        )
        rows = supplier_statement(self.actor, filters, include_cost=True)

        return self.render_detail(
            request,
            {
                "supplier": supplier,
                "rows": rows,
                "as_of": as_of,
                "page_title": _("كشف المورد %(name)s") % {"name": supplier.name},
                "page_hint": _(
                    "الترتيب: تاريخ العملية ثم وقت الترحيل ثم رقم القيد ثم نوع المستند "
                    "ثم رقمه. المستندات تُقرأ من هنا وتُدار من المشتريات."
                ),
            },
        )


# ---------------------------------------------------------------------------
# ذمم التطبيقات
# ---------------------------------------------------------------------------


class ApplicationReceivableListView(AccountingViewMixin, View):
    """
    What each delivery company owes, aged, beside the GL receivable account.

    The positions come from `apps.sales.receivables.positions_for`, which reads
    every application's entries in one pass and includes applications with a
    zero balance — "this company owes nothing" and "this company is missing
    from the report" are different statements and only the first is checkable.
    """

    required_permission = VIEW_APPLICATION_RECEIVABLES
    template_name = "accounting/application_receivable_list.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.sales.receivables import positions_for

        organizations = organizations_with_permission(
            self.actor, VIEW_APPLICATION_RECEIVABLES
        ).order_by("code")
        organization = _chosen_organization(self.actor, request, VIEW_APPLICATION_RECEIVABLES)

        raw_as_of = request.GET.get("as_of", "").strip()
        try:
            as_of = datetime.date.fromisoformat(raw_as_of) if raw_as_of else timezone.localdate()
        except ValueError:
            as_of = timezone.localdate()

        positions: list[Any] = []
        subledger_total = ZERO
        gl_balance = ZERO
        gl_account = None
        if organization is not None:
            positions = positions_for(self.actor, organization_id=organization.pk, as_of=as_of)
            subledger_total = sum((position.balance for position in positions), ZERO)
            gl_balance, gl_account = _role_balance(organization, DELIVERY_APP_RECEIVABLE, as_of)

        search = request.GET.get("q", "").strip().lower()
        if search:
            positions = [
                position
                for position in positions
                if search in position.delivery_application.code.lower()
                or search in position.delivery_application.name
            ]
        if request.GET.get("open_only") == "1":
            positions = [position for position in positions if position.balance != ZERO]

        # A receivable is a debit balance, so both sides are positive when money
        # is owed and the difference is a plain subtraction.
        difference = subledger_total - gl_balance

        from django.shortcuts import render as django_render

        return django_render(
            request,
            self.template_name,
            {
                "organizations": organizations,
                "organization": organization,
                "as_of": as_of,
                "positions": positions,
                "subledger_total": subledger_total,
                "gl_balance": gl_balance,
                "gl_account": gl_account,
                "difference": difference,
                "reconciled": difference == ZERO,
                "search": search,
                "open_only": request.GET.get("open_only", ""),
                "page_title": _("ذمم التطبيقات"),
                "page_hint": _(
                    "الرصيد مشتق من سجل ذمم المبيعات المُلحَق فقط، ولا يوجد رصيد "
                    "تطبيق قابل للتعديل. أي فرق يُعرض ولا يُصلَح."
                ),
                "list_base_template": (
                    "settings/_form_fragment.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
                "inventory_ui": False,
            },
        )


class ApplicationReceivableDetailView(AccountingDetailView):
    """One application's receivable ledger, with its running balance."""

    required_permission = VIEW_APPLICATION_RECEIVABLES
    template_name = "accounting/application_receivable_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.sales.receivables import ledger_for, positions_for_applications
        from apps.sales.selectors import resolve_delivery_application

        application = resolve_delivery_application(self.actor, kwargs["pk"])
        raw_as_of = request.GET.get("as_of", "").strip()
        try:
            as_of = datetime.date.fromisoformat(raw_as_of) if raw_as_of else timezone.localdate()
        except ValueError:
            as_of = timezone.localdate()

        entries = list(ledger_for(self.actor, delivery_application=application))
        positions = positions_for_applications(self.actor, [application], as_of=as_of)

        running = ZERO
        rows = []
        for entry in entries:
            # `signed_amount`, not a hand-rolled debit-minus-credit: the sign
            # convention belongs to the model that owns the ledger.
            running += entry.signed_amount
            rows.append({"entry": entry, "running": running})

        return self.render_detail(
            request,
            {
                "application": application,
                "rows": rows,
                "position": positions[0] if positions else None,
                "as_of": as_of,
                "page_title": _("ذمم %(name)s") % {"name": application.name},
                "page_hint": _(
                    "السجل مُلحَق فقط: التسوية والعكس يضيفان سطراً ولا يعدّلان سطراً. "
                    "المبيعات تملك هذه السطور والمحاسبة تقرأها."
                ),
            },
        )


__all__ = [
    "ApplicationReceivableDetailView",
    "ApplicationReceivableListView",
    "SupplierLiabilityDetailView",
    "SupplierLiabilityListView",
]
