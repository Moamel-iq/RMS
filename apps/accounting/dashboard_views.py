"""
The Accounting module landing page.

Every panel is an **independent htmx fragment** rather than one context built
in a single view, and the reason is failure isolation: a trial balance over a
year of postings is a different cost from a period lookup, and one slow or
broken panel must not blank the other twelve. Each card renders its own error
in place and the page survives.

Cards are declared in one table below rather than hard-coded in the template,
so a card added by a later checkpoint appears on the page by adding a row —
and a card whose feature does not exist yet is simply absent rather than
present and empty.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    AccountReportMapping,
    JournalEntry,
    JournalEntryStatus,
    PeriodState,
)
from apps.accounting.permissions import VIEW_CHART_OF_ACCOUNTS, VIEW_JOURNAL
from apps.accounting.selectors import role_usage, trial_balance_totals
from apps.accounting.views import AccountingViewMixin
from apps.organizations.authorization import organizations_with_permission
from apps.organizations.models import Organization


@dataclass(frozen=True)
class Card:
    """One dashboard panel: what it is called, how it is computed, where it goes."""

    key: str
    label: Any
    #: Returns `{"value": ..., "hint": ..., "state": "ok"|"warn"|"muted"}`.
    compute: Callable[[Organization], dict[str, Any]]
    url_name: str | None = None
    permission: str = VIEW_JOURNAL


def _current_period(organization: Organization) -> dict[str, Any]:
    today = timezone.localdate()
    period = AccountingPeriod.objects.filter(
        fiscal_year__organization=organization,
        start_date__lte=today,
        end_date__gte=today,
    ).first()
    if period is None:
        return {
            "value": _("لا توجد"),
            "hint": _("لم تُفتح سنة مالية تغطي اليوم."),
            "state": "warn",
        }
    state = {
        PeriodState.OPEN.value: "ok",
        PeriodState.SOFT_CLOSED.value: "warn",
        PeriodState.CLOSED.value: "muted",
    }.get(period.state, "muted")
    return {"value": str(period), "hint": period.get_state_display(), "state": state}


def _trial_balance(organization: Organization) -> dict[str, Any]:
    debits, credits = trial_balance_totals(organization=organization)
    difference = debits - credits
    if difference == Decimal("0"):
        return {"value": _("متوازن"), "hint": _("مجموع المدين = مجموع الدائن"), "state": "ok"}
    return {
        "value": _("غير متوازن"),
        "hint": _("الفرق: %(amount)s") % {"amount": difference},
        "state": "warn",
    }


def _unmapped_roles(organization: Organization) -> dict[str, Any]:
    rows = role_usage(organizations=[organization])
    missing = [row for row in rows if row.unresolved]
    if not missing:
        return {"value": "0", "hint": _("كل الأدوار مربوطة"), "state": "ok"}
    return {
        "value": str(len(missing)),
        "hint": _("أدوار بلا حساب سارٍ — أي ترحيل يحلّها سيفشل"),
        "state": "warn",
    }


def _unclassified_accounts(organization: Organization) -> dict[str, Any]:
    """
    Postable accounts with no statement group.

    Counted whether or not they carry a balance, because the count is what an
    accountant acts on; the *blocking* subset — unclassified **and** non-zero —
    is named on the statements themselves (ADR-031 §2).
    """
    mapped = AccountReportMapping.objects.filter(
        organization=organization, is_active=True
    ).values_list("account_id", flat=True)
    count = (
        Account.objects.filter(organization=organization, is_postable=True, is_active=True)
        .exclude(pk__in=mapped)
        .count()
    )
    if count == 0:
        return {"value": "0", "hint": _("كل الحسابات مصنّفة"), "state": "ok"}
    return {"value": str(count), "hint": _("حسابات بلا مجموعة قوائم"), "state": "warn"}


def _draft_journals(organization: Organization) -> dict[str, Any]:
    count = JournalEntry.objects.filter(
        organization=organization, status=JournalEntryStatus.DRAFT
    ).count()
    return {
        "value": str(count),
        "hint": _("مسودات لم تُرحَّل بعد"),
        "state": "warn" if count else "ok",
    }


def _posted_journals(organization: Organization) -> dict[str, Any]:
    count = JournalEntry.objects.filter(
        organization=organization,
        status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
    ).count()
    return {"value": str(count), "hint": _("قيود في دفتر الأستاذ"), "state": "ok"}


#: The cards this checkpoint can compute. Later checkpoints append their own —
#: cash, bank, supplier liabilities, application receivables, expense vouchers,
#: accruals, prepayments, net profit, balance-sheet status — and each appears
#: on the page by adding one row here.
CARDS: tuple[Card, ...] = (
    Card("period", _("الفترة الحالية"), _current_period, "accounting:journal_list"),
    Card("trial_balance", _("ميزان المراجعة"), _trial_balance, "accounting:journal_list"),
    Card(
        "roles",
        _("أدوار بلا ربط"),
        _unmapped_roles,
        "accounting:role_list",
        VIEW_CHART_OF_ACCOUNTS,
    ),
    Card(
        "unclassified",
        _("حسابات غير مصنّفة"),
        _unclassified_accounts,
        "accounting:chart_list",
        VIEW_CHART_OF_ACCOUNTS,
    ),
    Card("drafts", _("مسودات القيود"), _draft_journals, "accounting:journal_list"),
    Card("posted", _("قيود مُرحَّلة"), _posted_journals, "accounting:journal_list"),
)

CARDS_BY_KEY = {card.key: card for card in CARDS}


class AccountingDashboardView(AccountingViewMixin, View):
    """
    The module landing page: card frames only.

    The frames carry `hx-get` at `card_view`; no figure is computed here, so
    the page itself is fast whatever the ledger contains.
    """

    required_permission = VIEW_JOURNAL
    template_name = "accounting/dashboard.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations = organizations_with_permission(self.actor, VIEW_JOURNAL).order_by("code")
        chosen = request.GET.get("organization", "").strip()
        organization = (
            organizations.filter(pk=int(chosen)).first()
            if chosen.isdigit()
            else organizations.first()
        )

        visible = [
            card
            for card in CARDS
            if organizations_with_permission(self.actor, card.permission).exists()
        ]
        return render(
            request,
            self.template_name,
            {
                "organization": organization,
                "organizations": organizations,
                "cards": [
                    {
                        "card": card,
                        "url": (
                            reverse("accounting:dashboard_card", args=[card.key])
                            + f"?organization={organization.pk}"
                            if organization is not None
                            else None
                        ),
                        "drill": reverse(card.url_name) if card.url_name else None,
                    }
                    for card in visible
                ],
                "as_of": timezone.localdate(),
                "page_title": _("لوحة المحاسبة"),
                "page_hint": _(
                    "كل بطاقة تُحسب عند الطلب من القيود المُرحَّلة. لا يوجد رصيد مخزّن "
                    "في هذا النظام يمكن أن يخالف دفتر الأستاذ."
                ),
                "list_base_template": (
                    "settings/_form_fragment.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
                "inventory_ui": False,
            },
        )


class DashboardCardView(AccountingViewMixin, View):
    """
    One card's figure.

    Its own request, so a card that raises renders an error inside its own
    frame and the other twelve still show their numbers. A single view
    computing all of them would turn one broken query into a blank page.
    """

    required_permission = VIEW_JOURNAL
    template_name = "accounting/_dashboard_card.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        card = CARDS_BY_KEY.get(kwargs["key"])
        if card is None:
            raise Http404(_("Unknown dashboard card."))

        organizations = organizations_with_permission(self.actor, card.permission)
        chosen = request.GET.get("organization", "").strip()
        organization = (
            organizations.filter(pk=int(chosen)).first()
            if chosen.isdigit()
            else organizations.order_by("code").first()
        )
        if organization is None:
            raise Http404(_("Organization does not exist."))

        try:
            payload = card.compute(organization)
        except Exception:  # noqa: BLE001 - one card failing must not blank the page
            payload = {
                "value": _("تعذّر الحساب"),
                "hint": _("حدث خطأ أثناء حساب هذه البطاقة."),
                "state": "warn",
            }

        return render(
            request,
            self.template_name,
            {
                "card": card,
                "payload": payload,
                "drill": reverse(card.url_name) if card.url_name else None,
                "as_of": timezone.localdate(),
            },
        )


__all__ = ["CARDS", "AccountingDashboardView", "Card", "DashboardCardView"]
