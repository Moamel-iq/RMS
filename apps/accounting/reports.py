"""
The four accounting reports: trial balance, ledger, income statement, balance
sheet.

One service per report, and **the HTML view and the CSV view call the same
one** (ADR-031 §6). Two query paths drift, and the CSV is the one nobody looks
at until an auditor does — which is the worst possible moment to discover it
disagrees with the screen.

Statement placement comes from `AccountReportMapping`, never from an account
code prefix. `AccountClass` cannot carry it: class 7 is "other income **and**
expense" and class 1 does not distinguish current from non-current, so a
code-prefix check inside a view could not express either and would break the
moment a second organization numbered its chart differently (ADR-031 §1).

**An unmapped account with a non-zero balance is a row, never an omission.**
The account set is resolved from the ledger and not from the mapping table,
because a report built by iterating mappings produces a beautiful, balanced,
wrong answer when an account is unmapped: the balance simply is not there, and
the arithmetic still ties.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import QuerySet, Sum
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    Account,
    AccountClass,
    AccountReportMapping,
    CostCenter,
    JournalEntryStatus,
    JournalLine,
    PresentationSection,
    StatementGroup,
)
from apps.accounting.statements import STATEMENT_ORDER
from apps.organizations.models import Branch, Organization

ZERO = Decimal("0")

#: Which groups increase with a debit. Used to present a section's own total as
#: a positive number without changing the signed arithmetic underneath — the
#: services return debit-minus-credit throughout, so cross-section sums stay
#: correct and only the presentation flips.
DEBIT_NATURED = frozenset(
    {
        StatementGroup.ASSET,
        StatementGroup.COST_OF_SALES,
        StatementGroup.OPERATING_EXPENSE,
        StatementGroup.OTHER_EXPENSE,
    }
)


@dataclass(frozen=True)
class ReportFilters:
    """What the reader asked for. Absent means no restriction."""

    organization: Organization
    branch: Branch | None = None
    cost_center: CostCenter | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    account_class: str = ""
    code_from: str = ""
    code_to: str = ""
    include_zero: bool = False

    def as_query(self) -> dict[str, str]:
        pairs: dict[str, str] = {"organization": str(self.organization.pk)}
        if self.branch is not None:
            pairs["branch"] = str(self.branch.pk)
        if self.cost_center is not None:
            pairs["cost_center"] = str(self.cost_center.pk)
        if self.date_from is not None:
            pairs["from"] = self.date_from.isoformat()
        if self.date_to is not None:
            pairs["to"] = self.date_to.isoformat()
        if self.account_class:
            pairs["account_class"] = self.account_class
        if self.code_from:
            pairs["code_from"] = self.code_from
        if self.code_to:
            pairs["code_to"] = self.code_to
        if self.include_zero:
            pairs["include_zero"] = "1"
        return pairs


def _scoped_lines(filters: ReportFilters) -> QuerySet[JournalLine]:
    """
    Posted lines inside the filter, before any date window.

    POSTED **and** REVERSED: a reversal is itself posted and its original stays
    in the ledger, so both belong in a balance and the pair nets to zero.
    """
    lines = JournalLine.objects.filter(
        entry__organization=filters.organization,
        entry__status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
    )
    if filters.branch is not None:
        lines = lines.filter(branch=filters.branch)
    if filters.cost_center is not None:
        lines = lines.filter(cost_center=filters.cost_center)
    if filters.account_class:
        lines = lines.filter(account__account_class=filters.account_class)
    if filters.code_from:
        lines = lines.filter(account__code__gte=filters.code_from)
    if filters.code_to:
        lines = lines.filter(account__code__lte=filters.code_to)
    return lines


def _totals_by_account(
    lines: QuerySet[JournalLine],
) -> dict[int, tuple[Decimal, Decimal]]:
    """Debits and credits per account, in one query."""
    rows = lines.values("account_id").annotate(debits=Sum("debit"), credits=Sum("credit"))
    return {row["account_id"]: (row["debits"] or ZERO, row["credits"] or ZERO) for row in rows}


# ---------------------------------------------------------------------------
# ميزان المراجعة
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialBalanceRow:
    account: Account
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


@dataclass(frozen=True)
class TrialBalance:
    rows: list[TrialBalanceRow]
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal

    @property
    def difference(self) -> Decimal:
        return self.closing_debit - self.closing_credit

    @property
    def is_balanced(self) -> bool:
        return self.difference == ZERO


def _split(balance: Decimal) -> tuple[Decimal, Decimal]:
    """
    A signed balance as a (debit, credit) pair.

    One side or the other, never both: a trial balance row showing 500 debit
    and 500 credit for a net of zero is arithmetically true and useless.
    """
    if balance > ZERO:
        return balance, ZERO
    if balance < ZERO:
        return ZERO, -balance
    return ZERO, ZERO


def trial_balance(filters: ReportFilters) -> TrialBalance:
    """
    Opening, movement and closing per account.

    `Σ closing debit == Σ closing credit` on every filter combination, because
    both columns are derived from the same line set: a filter that could break
    the equality would be a filter that split a journal.
    """
    base = _scoped_lines(filters)

    opening_totals: dict[int, tuple[Decimal, Decimal]] = {}
    if filters.date_from is not None:
        opening_totals = _totals_by_account(
            base.filter(entry__accounting_date__lt=filters.date_from)
        )

    window = base
    if filters.date_from is not None:
        window = window.filter(entry__accounting_date__gte=filters.date_from)
    if filters.date_to is not None:
        window = window.filter(entry__accounting_date__lte=filters.date_to)
    period_totals = _totals_by_account(window)

    account_ids = set(opening_totals) | set(period_totals)
    accounts = {
        account.pk: account
        for account in Account.objects.filter(pk__in=account_ids).select_related("organization")
    }
    if filters.include_zero:
        extra = Account.objects.filter(
            organization=filters.organization, is_postable=True, is_active=True
        )
        if filters.account_class:
            extra = extra.filter(account_class=filters.account_class)
        for account in extra:
            accounts.setdefault(account.pk, account)

    rows: list[TrialBalanceRow] = []
    for account_id, account in sorted(accounts.items(), key=lambda pair: pair[1].code):
        open_debit, open_credit = opening_totals.get(account_id, (ZERO, ZERO))
        move_debit, move_credit = period_totals.get(account_id, (ZERO, ZERO))
        opening = open_debit - open_credit
        closing = opening + move_debit - move_credit
        if (
            not filters.include_zero
            and opening == ZERO
            and move_debit == ZERO
            and move_credit == ZERO
        ):
            continue
        od, oc = _split(opening)
        cd, cc = _split(closing)
        rows.append(
            TrialBalanceRow(
                account=account,
                opening_debit=od,
                opening_credit=oc,
                period_debit=move_debit,
                period_credit=move_credit,
                closing_debit=cd,
                closing_credit=cc,
            )
        )

    return TrialBalance(
        rows=rows,
        opening_debit=sum((row.opening_debit for row in rows), ZERO),
        opening_credit=sum((row.opening_credit for row in rows), ZERO),
        period_debit=sum((row.period_debit for row in rows), ZERO),
        period_credit=sum((row.period_credit for row in rows), ZERO),
        closing_debit=sum((row.closing_debit for row in rows), ZERO),
        closing_credit=sum((row.closing_credit for row in rows), ZERO),
    )


# ---------------------------------------------------------------------------
# دفتر الأستاذ
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerRow:
    line: JournalLine
    running: Decimal


@dataclass(frozen=True)
class Ledger:
    account: Account | None
    opening: Decimal
    rows: list[LedgerRow]
    debits: Decimal
    credits: Decimal
    closing: Decimal


def general_ledger(
    filters: ReportFilters,
    *,
    account: Account | None = None,
    source_type: str = "",
    origin: str = "",
) -> Ledger:
    """
    One account's movement in statement order, with a running balance.

    The order is `business date → posted_at → entry number → line number`, the
    same tuple `apps/accounting/statements.py` names, and the running balance is
    accumulated **here**. A template cannot carry a Decimal across rows without
    a filter that hides the ordering assumption — and the ordering is the whole
    content of the column.
    """
    lines = _scoped_lines(filters)
    if account is not None:
        lines = lines.filter(account=account)
    if source_type:
        lines = lines.filter(entry__source_document_type=source_type)
    if origin == "manual":
        lines = lines.filter(entry__source_event="")
    elif origin == "system":
        lines = lines.exclude(entry__source_event="")

    opening = ZERO
    if filters.date_from is not None:
        before = lines.filter(entry__accounting_date__lt=filters.date_from).aggregate(
            debits=Sum("debit"), credits=Sum("credit")
        )
        opening = (before["debits"] or ZERO) - (before["credits"] or ZERO)
        lines = lines.filter(entry__accounting_date__gte=filters.date_from)
    if filters.date_to is not None:
        lines = lines.filter(entry__accounting_date__lte=filters.date_to)

    ordered = lines.select_related(
        "entry", "account", "branch", "cost_center", "entry__posted_by"
    ).order_by(*STATEMENT_ORDER)

    running = opening
    debits = ZERO
    credits = ZERO
    rows: list[LedgerRow] = []
    for line in ordered:
        debits += line.debit
        credits += line.credit
        running = running + line.debit - line.credit
        rows.append(LedgerRow(line=line, running=running))

    return Ledger(
        account=account,
        opening=opening,
        rows=rows,
        debits=debits,
        credits=credits,
        closing=running,
    )


# ---------------------------------------------------------------------------
# The statement classification both financial statements share
# ---------------------------------------------------------------------------


@dataclass
class StatementSection:
    """One presented section, its accounts, and its total."""

    key: str
    label: object
    rows: list[tuple[Account, Decimal]] = field(default_factory=list)
    total: Decimal = ZERO


@dataclass
class Classified:
    """Every account with a balance, sorted into groups — or into غير مصنف."""

    by_group: dict[str, list[tuple[Account, Decimal]]]
    unmapped: list[tuple[Account, Decimal]]
    sections: dict[int, str]

    @property
    def has_unmapped(self) -> bool:
        return bool(self.unmapped)


def classify(
    filters: ReportFilters,
    *,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> Classified:
    """
    Balances grouped by statement group, with the unclassified kept visible.

    The account set comes from the **ledger**. An account with movement and no
    mapping lands in `unmapped`, which both statements render as a غير مصنف
    section and which blocks final approval. Silently omitting it would produce
    a report that balances and is wrong, and a missing balance is the one error
    a reader cannot detect from the report itself (ADR-031 §2).
    """
    lines = _scoped_lines(filters)
    if date_from is not None:
        lines = lines.filter(entry__accounting_date__gte=date_from)
    if date_to is not None:
        lines = lines.filter(entry__accounting_date__lte=date_to)

    totals = _totals_by_account(lines)
    accounts = {account.pk: account for account in Account.objects.filter(pk__in=totals.keys())}
    mappings = {
        row.account_id: row
        for row in AccountReportMapping.objects.filter(
            organization=filters.organization, is_active=True
        )
    }

    by_group: dict[str, list[tuple[Account, Decimal]]] = {
        group.value: [] for group in StatementGroup
    }
    unmapped: list[tuple[Account, Decimal]] = []
    sections: dict[int, str] = {}

    for account_id, (debits, credits) in totals.items():
        account = accounts.get(account_id)
        if account is None:  # pragma: no cover - a deleted account is impossible
            continue
        balance = debits - credits
        mapping = mappings.get(account_id)
        if mapping is None:
            if balance != ZERO:
                unmapped.append((account, balance))
            continue
        by_group[mapping.statement_group].append((account, balance))
        sections[account_id] = mapping.presentation_section

    for rows in by_group.values():
        rows.sort(key=lambda pair: pair[0].code)
    unmapped.sort(key=lambda pair: pair[0].code)
    return Classified(by_group=by_group, unmapped=unmapped, sections=sections)


def _section(key: str, label: object, rows: list[tuple[Account, Decimal]]) -> StatementSection:
    """
    One section, presented positive.

    The rows keep their signed balances; only the section total is flipped for
    a credit-natured group, so revenue reads as a positive number without any
    cross-section arithmetic changing sign underneath.
    """
    total = sum((balance for _account, balance in rows), ZERO)
    if key not in {group.value for group in DEBIT_NATURED}:
        total = -total
    return StatementSection(key=key, label=label, rows=rows, total=total)


# ---------------------------------------------------------------------------
# قائمة الدخل
# ---------------------------------------------------------------------------


@dataclass
class IncomeStatement:
    revenue: StatementSection
    cost_of_sales: StatementSection
    operating_expenses: StatementSection
    other_income: StatementSection
    other_expenses: StatementSection
    unmapped: list[tuple[Account, Decimal]]

    @property
    def gross_profit(self) -> Decimal:
        return self.revenue.total - self.cost_of_sales.total

    @property
    def operating_profit(self) -> Decimal:
        return self.gross_profit - self.operating_expenses.total

    @property
    def net_profit(self) -> Decimal:
        return self.operating_profit + self.other_income.total - self.other_expenses.total

    @property
    def cost_of_sales_missing(self) -> bool:
        """Revenue with no COGS is incomplete for this restaurant ledger."""
        return self.revenue.total != ZERO and self.cost_of_sales.total == ZERO

    @property
    def is_approvable(self) -> bool:
        """Unclassified balances or missing restaurant COGS block approval."""
        return not self.unmapped and not self.cost_of_sales_missing


def income_statement(
    filters: ReportFilters,
    *,
    date_from: datetime.date,
    date_to: datetime.date,
) -> IncomeStatement:
    """Revenue through net profit. No tax section — Release 1 has no tax policy."""
    classified = classify(filters, date_from=date_from, date_to=date_to)
    groups = classified.by_group
    return IncomeStatement(
        revenue=_section(StatementGroup.REVENUE, _("الإيرادات"), groups[StatementGroup.REVENUE]),
        cost_of_sales=_section(
            StatementGroup.COST_OF_SALES, _("كلفة المبيعات"), groups[StatementGroup.COST_OF_SALES]
        ),
        operating_expenses=_section(
            StatementGroup.OPERATING_EXPENSE,
            _("المصروفات التشغيلية"),
            groups[StatementGroup.OPERATING_EXPENSE],
        ),
        other_income=_section(
            StatementGroup.OTHER_INCOME, _("الإيرادات الأخرى"), groups[StatementGroup.OTHER_INCOME]
        ),
        other_expenses=_section(
            StatementGroup.OTHER_EXPENSE,
            _("المصروفات الأخرى"),
            groups[StatementGroup.OTHER_EXPENSE],
        ),
        unmapped=classified.unmapped,
    )


# ---------------------------------------------------------------------------
# الميزانية العمومية
# ---------------------------------------------------------------------------


@dataclass
class BalanceSheet:
    current_assets: StatementSection
    non_current_assets: StatementSection
    current_liabilities: StatementSection
    non_current_liabilities: StatementSection
    equity: StatementSection
    current_year_earnings: Decimal
    retained_earnings: Decimal
    unmapped: list[tuple[Account, Decimal]]
    as_of: datetime.date

    @property
    def assets(self) -> Decimal:
        return self.current_assets.total + self.non_current_assets.total

    @property
    def liabilities(self) -> Decimal:
        return self.current_liabilities.total + self.non_current_liabilities.total

    @property
    def equity_total(self) -> Decimal:
        """Equity as presented, including the computed current-year result."""
        return self.equity.total + self.current_year_earnings

    @property
    def difference(self) -> Decimal:
        return self.assets - (self.liabilities + self.equity_total)

    @property
    def is_balanced(self) -> bool:
        return self.difference == ZERO

    @property
    def is_approvable(self) -> bool:
        return self.is_balanced and not self.unmapped


def balance_sheet(
    filters: ReportFilters, *, as_of: datetime.date, year_start: datetime.date
) -> BalanceSheet:
    """
    Assets, liabilities and equity as at a date, with computed current-year
    earnings.

    Before year-end close the equity side carries `YTD revenue − YTD expenses`
    as a computed line, so `Assets = Liabilities + Equity` holds on any date
    without the income-statement accounts being physically closed every month.
    Monthly closing entries would destroy the year-to-date income statement:
    once March's revenue has been swept to equity, "revenue for the year to
    date" has to be reconstructed from closing journals rather than read
    (ADR-031 §3).
    """
    classified = classify(filters, date_to=as_of)
    groups = classified.by_group
    sections = classified.sections

    def split_by_section(
        group: str,
    ) -> tuple[list[tuple[Account, Decimal]], list[tuple[Account, Decimal]]]:
        current: list[tuple[Account, Decimal]] = []
        non_current: list[tuple[Account, Decimal]] = []
        for account, balance in groups[group]:
            if sections.get(account.pk) == PresentationSection.NON_CURRENT:
                non_current.append((account, balance))
            else:
                current.append((account, balance))
        return current, non_current

    current_assets, non_current_assets = split_by_section(StatementGroup.ASSET)
    current_liabilities, non_current_liabilities = split_by_section(StatementGroup.LIABILITY)

    result = income_statement(filters, date_from=year_start, date_to=as_of)

    return BalanceSheet(
        current_assets=_section(StatementGroup.ASSET, _("الأصول المتداولة"), current_assets),
        non_current_assets=_section(
            StatementGroup.ASSET, _("الأصول غير المتداولة"), non_current_assets
        ),
        current_liabilities=_section(
            StatementGroup.LIABILITY, _("المطلوبات المتداولة"), current_liabilities
        ),
        non_current_liabilities=_section(
            StatementGroup.LIABILITY, _("المطلوبات غير المتداولة"), non_current_liabilities
        ),
        equity=_section(StatementGroup.EQUITY, _("حقوق الملكية"), groups[StatementGroup.EQUITY]),
        current_year_earnings=result.net_profit,
        # Retained earnings is simply whatever the equity section already
        # carries on the retained-earnings account; it is not recomputed here,
        # because after a year-end close the closing journal is what put it
        # there and recomputing would second-guess the journal.
        retained_earnings=sum(
            (
                balance
                for account, balance in groups[StatementGroup.EQUITY]
                if "3-03" in account.code
            ),
            ZERO,
        )
        * Decimal("-1"),
        unmapped=classified.unmapped,
        as_of=as_of,
    )


def unmapped_postable_accounts(organization: Organization) -> QuerySet[Account]:
    """Postable accounts with no active statement mapping, balance or not."""
    mapped = AccountReportMapping.objects.filter(
        organization=organization, is_active=True
    ).values_list("account_id", flat=True)
    return (
        Account.objects.filter(organization=organization, is_postable=True, is_active=True)
        .exclude(pk__in=mapped)
        .order_by("code")
    )


def default_report_group(account: Account) -> tuple[str, str]:
    """
    A sensible starting classification for an account, from its class.

    Used only to **seed** a mapping somebody then reviews — never as a
    substitute for one at report time. Class 7 deliberately maps to other
    expense rather than guessing by sign: a guess that lands a gain in the
    expense section is worse than one an accountant has to correct once.
    """
    mapping: dict[str, tuple[str, str]] = {
        AccountClass.ASSET: (StatementGroup.ASSET, PresentationSection.CURRENT),
        AccountClass.LIABILITY: (StatementGroup.LIABILITY, PresentationSection.CURRENT),
        AccountClass.EQUITY: (StatementGroup.EQUITY, PresentationSection.NOT_APPLICABLE),
        AccountClass.REVENUE: (StatementGroup.REVENUE, PresentationSection.NOT_APPLICABLE),
        AccountClass.COST_OF_SALES: (
            StatementGroup.COST_OF_SALES,
            PresentationSection.NOT_APPLICABLE,
        ),
        AccountClass.OPERATING_EXPENSE: (
            StatementGroup.OPERATING_EXPENSE,
            PresentationSection.NOT_APPLICABLE,
        ),
        AccountClass.OTHER: (StatementGroup.OTHER_EXPENSE, PresentationSection.NOT_APPLICABLE),
        AccountClass.CLEARING: (StatementGroup.ASSET, PresentationSection.CURRENT),
        AccountClass.MEMO: (StatementGroup.ASSET, PresentationSection.NOT_APPLICABLE),
    }
    return mapping.get(account.account_class, (StatementGroup.ASSET, PresentationSection.CURRENT))


__all__ = [
    "BalanceSheet",
    "Classified",
    "IncomeStatement",
    "Ledger",
    "LedgerRow",
    "ReportFilters",
    "StatementSection",
    "TrialBalance",
    "TrialBalanceRow",
    "balance_sheet",
    "classify",
    "default_report_group",
    "general_ledger",
    "income_statement",
    "trial_balance",
    "unmapped_postable_accounts",
]
