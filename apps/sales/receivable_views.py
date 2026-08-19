"""
ذمم التطبيقات — the screens over the receivable ledger.

**Read-only, both of them.** Nothing on either screen writes: the ledger is
written by `posting.py` when a day posts, by `adjustment_posting.py` when a
correction posts, and by `settlement_posting.py` when a statement is settled.
A screen that could add a movement here would be a fourth writer with no
document behind it, which is precisely what an append-only ledger exists to
prevent.

The list is one row per delivery application — balance, age, and when the
contract says the money is due. The detail is one application's movements in
order, each with the balance after it, so the question "why does this company
owe this" is answered by scrolling rather than by exporting.

`view_application_receivables` is `ORGANIZATION_MASTER_DATA`: reaching the
organization is enough to see what the delivery companies owe. The rows are then
narrowed again by `visible_receivable_entries`, which is branch-scoped — scope
and selector answer two different questions here and both are asked (ADR-016).
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.inventory.views import InventoryViewMixin
from apps.sales.permissions import VIEW_APPLICATION_RECEIVABLES
from apps.sales.receivables import (
    AGING_BUCKETS,
    ledger_for,
    positions_for_applications,
    running_balance,
)
from apps.sales.selectors import (
    resolve_delivery_application,
    visible_delivery_applications,
    visible_settlements,
)
from apps.sales.views import SalesListView

ZERO = Decimal("0")


def _as_of(request: HttpRequest) -> datetime.date:
    """
    The date the report is drawn at.

    Today unless the caller says otherwise, and a malformed value falls back to
    today rather than erroring: a mistyped date in a query string should not
    take the screen away from somebody who is trying to read a balance.
    """
    raw = request.GET.get("as_of", "").strip()
    if raw:
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            return timezone.localdate()
    return timezone.localdate()


class ApplicationReceivableListView(SalesListView):
    """
    One row per delivery application: what it owes, and how old the debt is.

    The rows are the *applications*, not the ledger entries, so the aging is
    computed for the page rather than for every company in the organization —
    `positions_for_applications` reads the entries once and groups them.
    """

    template_name = "sales/application_receivable_list.html"
    context_object_name = "applications"
    required_permission = VIEW_APPLICATION_RECEIVABLES
    page_title = _("ذمم التطبيقات")
    page_hint = _(
        "لا يوجد رصيد مخزَّن في أي مكان: ما يدين به التطبيق يُحتسب من حركات الذمة "
        "في كل مرة يُسأل عنها. الأعمار تُحتسب بمطابقة الدفعات مع أقدم المديونيات "
        "أولاً، فما يبقى مفتوحاً يحمل تاريخ البيع الذي أنشأه — وهذا ما يجعل التقرير "
        "يقول أيّ المبيعات تأخّرت، لا كم المبلغ فقط."
    )
    search_fields = ("code", "name_ar", "name_en")
    result_label = _("تطبيق")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_delivery_applications(self.actor).order_by("organization__code", "code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        as_of = _as_of(self.request)
        applications = list(context["applications"])
        context["as_of"] = as_of
        context["bucket_labels"] = [label for label, _from, _to in AGING_BUCKETS]
        context["positions"] = positions_for_applications(self.actor, applications, as_of=as_of)
        return context


class ApplicationReceivableDetailView(InventoryViewMixin, View):
    """
    One application's ledger: every movement, and the balance after each.

    `pk` is the **`DeliveryApplication`**, not an entry. The page is that
    company's account, and the entries are its lines — naming an entry in the
    URL would make the natural "open the account" link impossible to build.
    """

    module_key = "sales"
    required_permission = VIEW_APPLICATION_RECEIVABLES

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        application = resolve_delivery_application(self.actor, kwargs["pk"])
        as_of = _as_of(request)

        date_from_raw = request.GET.get("from", "").strip()
        date_from: datetime.date | None = None
        if date_from_raw:
            try:
                date_from = datetime.date.fromisoformat(date_from_raw)
            except ValueError:
                date_from = None

        entries = list(
            ledger_for(
                self.actor,
                delivery_application=application,
                date_from=date_from,
                date_to=as_of,
            )
        )
        # The opening figure is the balance carried into the window, so the
        # running column starts where the previous period left off rather than
        # at zero — a ledger whose first line reads zero when it should read a
        # month's debt is a ledger nobody trusts twice.
        opening = ZERO
        if date_from is not None:
            before = list(
                ledger_for(
                    self.actor,
                    delivery_application=application,
                    date_to=date_from - datetime.timedelta(days=1),
                )
            )
            opening = sum((row.signed_amount for row in before), ZERO)

        movements = [(entry, opening + balance) for entry, balance in running_balance(entries)]
        position = positions_for_applications(self.actor, [application], as_of=as_of)[0]

        context = {
            "application": application,
            "as_of": as_of,
            "date_from": date_from,
            "opening": opening,
            "movements": movements,
            "closing": movements[-1][1] if movements else opening,
            "position": position,
            "settlements": visible_settlements(self.actor).filter(delivery_application=application)[
                :10
            ],
            "page_title": _("ذمة %(code)s") % {"code": application.code},
            # `_form_fragment.html`, not `_list_fragment.html`. This template
            # extends `list_base_template` **directly** rather than through
            # `settings/base_list.html`, so the block it defines is `page`;
            # `_list_fragment.html` contains only `results`, Django silently
            # drops a child block the parent does not declare, and the htmx
            # form of this screen answered 200 with an empty body.
            "list_base_template": (
                "settings/_form_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }
        return render(request, "sales/application_receivable_detail.html", context)


__all__ = ["ApplicationReceivableDetailView", "ApplicationReceivableListView"]
