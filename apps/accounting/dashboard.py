"""
لوحة المحاسبة — the figures the landing page renders with itself.

The page already had one half: a registry of htmx cards (`dashboard_views`),
each fetched on its own so a slow trial balance cannot blank a period lookup.
This module is the other half — the headline. Five counts, two role balances
and a period summary cost less than the round trips isolating them would, so
they render with the page; the trial balance, the one read that grows with the
ledger, stays a fragment and only its *shape* is defined here.

Nothing takes a user. The view resolves the organization against the caller's
`view_journal` scope before any read runs, and accounting has no second, finer
money permission the way inventory and procurement do — the ledger is either
readable or it is not — so there is nothing to redact below the organization.

Absent is still not zero. A role with no account in force has no balance to
show, so `inventory` and `payable` are None in that case rather than 0: a zero
there would read as "the stock is worth nothing", which is a different claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INVENTORY_CONTROL,
    SUPPLIER_PAYABLE,
    Account,
    AccountingPeriod,
    AccountReportMapping,
    AccountRole,
    CostCenter,
    ExpenseVoucher,
    FinancialDocumentStatus,
    FiscalYear,
    JournalEntry,
    JournalEntryStatus,
    PeriodState,
    PrepaymentScheduleLine,
    ScheduleLineStatus,
)
from apps.accounting.selectors import (
    account_balances,
    posted_lines,
    role_usage,
    trial_balance,
)
from apps.accounting.services import resolve_default_account
from apps.organizations.models import Organization

_ZERO = Decimal("0")
_LEDGER = (JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED)


@dataclass(frozen=True)
class RoleBalance:
    """The ledger balance of the account a role resolves to today."""

    code: str
    name: str
    #: Debits minus credits, signed — the kernel's convention.
    balance: Decimal

    @property
    def magnitude(self) -> Decimal:
        return abs(self.balance)

    @property
    def is_credit(self) -> bool:
        return self.balance < _ZERO


@dataclass(frozen=True)
class Gap:
    """Something the ledger does not yet reflect, and where to go to fix it."""

    key: str
    label: Any
    detail: Any
    count: int
    url_name: str


@dataclass(frozen=True)
class PeriodSummary:
    """The fiscal year in force and how many of its periods are still open."""

    fiscal_year: FiscalYear | None
    period_count: int
    open_count: int
    soft_closed_count: int
    closed_count: int
    #: The period covering today, whatever its state; None when no year does.
    current: AccountingPeriod | None


@dataclass(frozen=True)
class AccountingOverview:
    posted_entry_count: int
    posted_line_count: int
    #: Posted entries whose lines do not sum equal. The kernel refuses to post
    #: one, so anything above zero is a database-level incident, not a task.
    unbalanced_entry_count: int
    draft_entry_count: int
    inventory: RoleBalance | None
    payable: RoleBalance | None
    account_count: int
    cost_center_count: int
    role_count: int
    gaps: tuple[Gap, ...]
    periods: PeriodSummary

    @property
    def is_sound(self) -> bool:
        return self.unbalanced_entry_count == 0


@dataclass(frozen=True)
class TrialRow:
    code: str
    name: str
    debits: Decimal
    credits: Decimal
    balance: Decimal

    @property
    def magnitude(self) -> Decimal:
        return abs(self.balance)

    @property
    def is_credit(self) -> bool:
        return self.balance < _ZERO

    @property
    def movement(self) -> Decimal:
        return self.debits + self.credits


@dataclass(frozen=True)
class TrialBalanceTable:
    """The moving accounts, the largest by movement first, totals over all."""

    rows: tuple[TrialRow, ...]
    account_count: int
    total_debits: Decimal
    total_credits: Decimal

    @property
    def hidden_count(self) -> int:
        return self.account_count - len(self.rows)

    @property
    def difference(self) -> Decimal:
        return self.total_debits - self.total_credits

    @property
    def is_balanced(self) -> bool:
        return self.difference == _ZERO


def _role_balance(
    organization: Organization, role_code: str, balances: dict[int, Decimal]
) -> RoleBalance | None:
    """The balance behind a role today — or None when no account is in force."""
    try:
        mapping = resolve_default_account(
            organization=organization, account_role=role_code, on_date=timezone.localdate()
        )
    except ValidationError:
        return None
    account = mapping.account
    return RoleBalance(
        code=account.code,
        name=account.name_ar,
        balance=balances.get(account.pk, _ZERO),
    )


def _unbalanced_entries(organization: Organization) -> int:
    return (
        JournalEntry.objects.filter(organization=organization, status__in=_LEDGER)
        .annotate(
            debits=Coalesce(Sum("lines__debit"), _ZERO),
            credits=Coalesce(Sum("lines__credit"), _ZERO),
        )
        .exclude(debits=F("credits"))
        .count()
    )


def _gaps(organization: Organization) -> tuple[Gap, ...]:
    """
    What has been written down somewhere but not yet in the ledger.

    Each item is a document or a setting that stops figures from reaching the
    books; the list holds only the ones whose count is above zero, so an empty
    tuple means "nothing is waiting", not "nothing was checked".
    """
    from apps.sales.models import SalesDay, SalesDayStatus

    today = timezone.localdate()
    candidates = (
        Gap(
            "journals",
            _("مسودات القيود"),
            _("قيد لم يُرحَّل بعد"),
            JournalEntry.objects.filter(
                organization=organization, status=JournalEntryStatus.DRAFT
            ).count(),
            "accounting:journal_list",
        ),
        Gap(
            "sales",
            _("أيام المبيعات"),
            _("يوم مبيعات لم يُرحَّل — إيراده خارج الدفتر"),
            SalesDay.objects.filter(
                organization=organization,
                status__in=[SalesDayStatus.DRAFT, SalesDayStatus.SUBMITTED],
            ).count(),
            "sales:day_list",
        ),
        Gap(
            "expenses",
            _("سندات المصروف"),
            _("سند مصروف لم يُرحَّل"),
            ExpenseVoucher.objects.filter(
                organization=organization,
                status__in=[FinancialDocumentStatus.DRAFT, FinancialDocumentStatus.APPROVED],
            ).count(),
            "accounting:expense_list",
        ),
        Gap(
            "prepayments",
            _("أقساط المقدمات"),
            _("قسط مستحقّ الترحيل"),
            PrepaymentScheduleLine.objects.filter(
                prepayment__organization=organization,
                prepayment__status=FinancialDocumentStatus.POSTED,
                status=ScheduleLineStatus.PLANNED,
                period_end__lte=today,
            ).count(),
            "accounting:deferral_list",
        ),
        Gap(
            "roles",
            _("أدوار بلا حساب"),
            _("دور حساب بلا ربط سارٍ — أي ترحيل يحلّه يفشل"),
            sum(1 for row in role_usage(organizations=[organization]) if row.unresolved),
            "accounting:role_list",
        ),
        Gap(
            "unclassified",
            _("حسابات غير مصنّفة"),
            _("حساب بلا مجموعة قوائم — يعطّل القوائم المالية"),
            Account.objects.filter(organization=organization, is_postable=True, is_active=True)
            .exclude(
                pk__in=AccountReportMapping.objects.filter(
                    organization=organization, is_active=True
                ).values_list("account_id", flat=True)
            )
            .count(),
            "accounting:chart_list",
        ),
    )
    return tuple(gap for gap in candidates if gap.count)


def _periods(organization: Organization) -> PeriodSummary:
    today = timezone.localdate()
    year = (
        FiscalYear.objects.filter(
            organization=organization, start_date__lte=today, end_date__gte=today
        ).first()
        or FiscalYear.objects.filter(organization=organization).order_by("-year").first()
    )
    if year is None:
        return PeriodSummary(None, 0, 0, 0, 0, None)

    periods = list(AccountingPeriod.objects.filter(fiscal_year=year).order_by("period_number"))
    by_state = dict.fromkeys(PeriodState.values, 0)
    for period in periods:
        by_state[period.state] = by_state.get(period.state, 0) + 1
    current = next((p for p in periods if p.start_date <= today <= p.end_date), None)
    return PeriodSummary(
        fiscal_year=year,
        period_count=len(periods),
        open_count=by_state[PeriodState.OPEN],
        soft_closed_count=by_state[PeriodState.SOFT_CLOSED],
        closed_count=by_state[PeriodState.CLOSED],
        current=current,
    )


def accounting_overview(organization: Organization) -> AccountingOverview:
    """The landing page's headline for one organization, all from the ledger."""
    balances = account_balances(organization=organization)
    return AccountingOverview(
        posted_entry_count=JournalEntry.objects.filter(
            organization=organization, status__in=_LEDGER
        ).count(),
        posted_line_count=posted_lines(organization=organization).count(),
        unbalanced_entry_count=_unbalanced_entries(organization),
        draft_entry_count=JournalEntry.objects.filter(
            organization=organization, status=JournalEntryStatus.DRAFT
        ).count(),
        inventory=_role_balance(organization, INVENTORY_CONTROL, balances),
        payable=_role_balance(organization, SUPPLIER_PAYABLE, balances),
        account_count=Account.objects.filter(
            organization=organization, is_postable=True, is_active=True
        ).count(),
        cost_center_count=CostCenter.objects.filter(
            organization=organization, is_active=True
        ).count(),
        role_count=AccountRole.objects.filter(is_active=True).count(),
        gaps=_gaps(organization),
        periods=_periods(organization),
    )


def trial_balance_table(organization: Organization, *, limit: int = 12) -> TrialBalanceTable:
    """
    The moving accounts for the panel: the `limit` largest by movement, in
    code order, with totals over **every** moving account — so the two columns
    at the foot still prove the ledger even when rows are held back.
    """
    rows = [
        TrialRow(
            code=str(row["code"]),
            name=str(row["name_ar"]),
            debits=Decimal(str(row["debits"])),
            credits=Decimal(str(row["credits"])),
            balance=Decimal(str(row["balance"])),
        )
        for row in trial_balance(organization=organization)
    ]
    shown = sorted(
        sorted(rows, key=lambda row: row.movement, reverse=True)[:limit],
        key=lambda row: row.code,
    )
    return TrialBalanceTable(
        rows=tuple(shown),
        account_count=len(rows),
        total_debits=sum((row.debits for row in rows), _ZERO),
        total_credits=sum((row.credits for row in rows), _ZERO),
    )


__all__ = [
    "AccountingOverview",
    "Gap",
    "PeriodSummary",
    "RoleBalance",
    "TrialBalanceTable",
    "TrialRow",
    "accounting_overview",
    "trial_balance_table",
]
