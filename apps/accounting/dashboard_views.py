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

from apps.accounting.dashboard import accounting_overview, trial_balance_table
from apps.accounting.models import (
    Account,
    AccountingPeriod,
    AccountReportMapping,
    AccrualDocument,
    BankAccount,
    Cashbox,
    ExpenseVoucher,
    FinancialDocumentStatus,
    JournalEntry,
    JournalEntryStatus,
    PeriodState,
    PrepaymentScheduleLine,
    ScheduleLineStatus,
)
from apps.accounting.permissions import VIEW_CHART_OF_ACCOUNTS, VIEW_JOURNAL
from apps.accounting.reports import ReportFilters, balance_sheet, income_statement
from apps.accounting.selectors import (
    account_balance,
    role_usage,
    trial_balance_totals,
)
from apps.accounting.views import AccountingViewMixin
from apps.core.money import money_audit_with_currency, money_with_currency
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
    #: A card that is a panel rather than one figure names its own fragment
    #: template; the default renders `value` / `hint` / `state` as a tile.
    template: str | None = None


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


def _cash_balance(organization: Organization) -> dict[str, Any]:
    total = sum(
        (
            account_balance(account=cashbox.account, branch=cashbox.branch)
            for cashbox in Cashbox.objects.filter(
                organization=organization, is_active=True
            ).select_related("account", "branch")
        ),
        Decimal("0"),
    )
    return {
        "value": money_with_currency(total),
        "hint": _("مجموع أرصدة الصناديق"),
        "state": "ok",
    }


def _bank_balance(organization: Organization) -> dict[str, Any]:
    total = sum(
        (
            account_balance(account=bank.account)
            for bank in BankAccount.objects.filter(
                organization=organization, is_active=True
            ).select_related("account")
        ),
        Decimal("0"),
    )
    return {
        "value": money_with_currency(total),
        "hint": _("مجموع الأرصدة البنكية"),
        "state": "ok",
    }


def _system_reader() -> Any:
    """
    A caller for the source modules' own scoped report services.

    The dashboard has already resolved the organization against the signed-in
    user's scope before any card runs, so the figure shown is one they are
    entitled to. The procurement and sales services take a user for *their*
    scoping, and handing them a superuser stops a card silently under-reporting
    because the reader happens to hold no procurement post. What the card shows
    is still only the organization the caller reached.
    """
    from apps.users.models import User

    return User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()


def _supplier_liabilities(organization: Organization) -> dict[str, Any]:
    from apps.procurement.reports import ProcurementReportFilters, supplier_aging

    rows = supplier_aging(
        _system_reader(),
        ProcurementReportFilters(organization_id=organization.pk),
        include_cost=True,
    )
    total = sum((row.get("net_position") or Decimal("0") for row in rows), Decimal("0"))
    return {
        "value": money_with_currency(total),
        "hint": _("مشتقّة من مستندات المشتريات — لا جدول أرصدة"),
        "state": "ok",
    }


def _application_receivables(organization: Organization) -> dict[str, Any]:
    from apps.sales.receivables import positions_for

    positions = positions_for(
        _system_reader(), organization_id=organization.pk, as_of=timezone.localdate()
    )
    total = sum((position.balance for position in positions), Decimal("0"))
    return {
        "value": money_with_currency(total),
        "hint": _("من سجل ذمم المبيعات المُلحَق"),
        "state": "ok",
    }


def _unposted_expenses(organization: Organization) -> dict[str, Any]:
    count = ExpenseVoucher.objects.filter(
        organization=organization,
        status__in=[FinancialDocumentStatus.DRAFT, FinancialDocumentStatus.APPROVED],
    ).count()
    return {
        "value": str(count),
        "hint": _("سندات مصروف لم تُرحَّل"),
        "state": "warn" if count else "ok",
    }


def _active_accruals(organization: Organization) -> dict[str, Any]:
    count = AccrualDocument.objects.filter(
        organization=organization, status=FinancialDocumentStatus.POSTED
    ).count()
    return {"value": str(count), "hint": _("مستحقات قائمة لم تُعكس"), "state": "ok"}


def _prepayments_due(organization: Organization) -> dict[str, Any]:
    count = PrepaymentScheduleLine.objects.filter(
        prepayment__organization=organization,
        prepayment__status=FinancialDocumentStatus.POSTED,
        status=ScheduleLineStatus.PLANNED,
        period_end__lte=timezone.localdate(),
    ).count()
    return {
        "value": str(count),
        "hint": _("أقساط مقدمات مستحقة الترحيل"),
        "state": "warn" if count else "ok",
    }


def _net_profit(organization: Organization) -> dict[str, Any]:
    today = timezone.localdate()
    report = income_statement(
        ReportFilters(organization=organization),
        date_from=today.replace(month=1, day=1),
        date_to=today,
    )
    return {
        "value": money_with_currency(report.net_profit),
        "hint": _("من بداية السنة حتى تاريخه"),
        "state": "ok" if report.is_approvable else "warn",
    }


def _balance_sheet_state(organization: Organization) -> dict[str, Any]:
    today = timezone.localdate()
    report = balance_sheet(
        ReportFilters(organization=organization),
        as_of=today,
        year_start=today.replace(month=1, day=1),
    )
    if report.is_balanced:
        return {
            "value": _("متوازنة"),
            "hint": _("الأصول = المطلوبات + حقوق الملكية"),
            "state": "ok",
        }
    return {
        "value": _("غير متوازنة"),
        "hint": _("الفرق: %(amount)s") % {"amount": money_audit_with_currency(report.difference)},
        "state": "warn",
    }


def _trial_balance_rows(organization: Organization) -> dict[str, Any]:
    """
    The trial balance panel: the one read that grows with the ledger, which
    is why it stays a fragment while the headline renders with the page.
    """
    table = trial_balance_table(organization)
    return {
        "table": table,
        "value": _("متوازن") if table.is_balanced else _("غير متوازن"),
        "hint": _("مجموع المدين = مجموع الدائن")
        if table.is_balanced
        else _("الفرق: %(amount)s") % {"amount": money_audit_with_currency(table.difference)},
        "state": "ok" if table.is_balanced else "warn",
    }


#: The cards this checkpoint can compute. Later checkpoints append their own —
#: cash, bank, supplier liabilities, application receivables, expense vouchers,
#: accruals, prepayments, net profit, balance-sheet status — and each appears
#: on the page by adding one row here.
CARDS: tuple[Card, ...] = (
    Card(
        "trial_balance_rows",
        _("ميزان المراجعة"),
        _trial_balance_rows,
        "accounting:trial_balance",
        template="accounting/cards/_trial_balance.html",
    ),
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
    Card("cash", _("رصيد الصناديق"), _cash_balance, "accounting:cashbox_list"),
    Card("bank", _("الرصيد البنكي"), _bank_balance, "accounting:bank_account_list"),
    Card(
        "supplier_liabilities",
        _("ذمم الموردين"),
        _supplier_liabilities,
        "accounting:supplier_liability_list",
    ),
    Card(
        "application_receivables",
        _("ذمم التطبيقات"),
        _application_receivables,
        "accounting:application_receivable_list",
    ),
    Card("expenses", _("مصروفات لم تُرحَّل"), _unposted_expenses, "accounting:expense_list"),
    Card("accruals", _("مستحقات قائمة"), _active_accruals, "accounting:deferral_list"),
    Card("prepayments", _("أقساط مستحقة"), _prepayments_due, "accounting:deferral_list"),
    Card("net_profit", _("صافي الربح"), _net_profit, "accounting:income_statement"),
    Card(
        "balance_sheet",
        _("الميزانية العمومية"),
        _balance_sheet_state,
        "accounting:balance_sheet",
    ),
)

CARDS_BY_KEY = {card.key: card for card in CARDS}

#: The cards the page still fetches as fragments, in the strip under the
#: panels: treasury and the statements — each a real computation over the
#: ledger, worth its own frame. The rest of the registry now feeds the
#: headline and the gaps panel directly and answers at its endpoint for
#: anything that still asks.
STRIP_KEYS: tuple[str, ...] = (
    "cash",
    "bank",
    "application_receivables",
    "accruals",
    "net_profit",
    "balance_sheet",
)


class AccountingDashboardView(AccountingViewMixin, View):
    """
    The module landing page.

    The headline — counts, two role balances, the gaps, the periods — renders
    with the page: each is a count or a one-query balance, cheaper than the
    round trip that would isolate it. The trial balance and the treasury and
    statement cards keep their own `hx-get`, so the one read that grows with
    the ledger, and the two that build a statement, never hold the rest.
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

        def frame(card: Card) -> dict[str, Any]:
            return {
                "card": card,
                "url": (
                    reverse("accounting:dashboard_card", args=[card.key])
                    + f"?organization={organization.pk}"
                    if organization is not None
                    else None
                ),
                "drill": reverse(card.url_name) if card.url_name else None,
            }

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
                "overview": accounting_overview(organization) if organization else None,
                "table_card": frame(CARDS_BY_KEY["trial_balance_rows"]),
                "strip_cards": [frame(card) for card in visible if card.key in STRIP_KEYS],
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
            card.template or self.template_name,
            {
                "card": card,
                "payload": payload,
                "drill": reverse(card.url_name) if card.url_name else None,
                "as_of": timezone.localdate(),
            },
        )


__all__ = ["CARDS", "AccountingDashboardView", "Card", "DashboardCardView"]
