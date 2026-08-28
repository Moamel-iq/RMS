"""
ميزان المراجعة · دفتر الأستاذ · قائمة الدخل · الميزانية العمومية ·
ذمم الموردين · ذمم التطبيقات · الفترات المحاسبية.

Read-only, all of it. There is no repair endpoint anywhere in this file: where
a report disagrees with itself it returns the difference and says it is not
approvable, because an endpoint that could make the two sides agree would make
the books wrong and would do it without anyone reading the number that
explained why.

Three properties worth stating, because a caller can rely on them:

* **`unmapped` is always present.** An account with a balance and no statement
  group appears in `unmapped` and sets `is_approvable` to false. It is never
  dropped from a report that then still ties (ADR-031 §2) — a statement that
  balances by omission is the worst possible answer.
* **Every amount is a string.** Same reason as the write side: JSON has one
  numeric type and it is a binary float.
* **The pre-close endpoint is a report, not an attempt.** It collects *every*
  blocker rather than raising on the first, which is the difference between
  closing a month in one pass and closing it in six.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from apps.accounting.period_services import period_activity, period_close_blockers
from apps.accounting.report_commands import (
    build_report_filters,
    read_application_receivables,
    read_balance_sheet,
    read_general_ledger,
    read_income_statement,
    read_supplier_liabilities,
    read_trial_balance,
)
from apps.accounting.reports import BalanceSheet, IncomeStatement, StatementSection
from apps.core.money import money_export
from apps.users.models import User

router = Router(tags=["accounting-reports"])


def _actor(request: HttpRequest) -> User:
    user: User = request.user  # type: ignore[assignment]
    return user


class AccountRowOut(Schema):
    account_id: int
    account_code: str
    account_name_ar: str
    amount: str


class SectionOut(Schema):
    key: str
    label: str
    total: str
    rows: list[AccountRowOut]


class TrialBalanceRowOut(Schema):
    account_id: int
    account_code: str
    account_name_ar: str
    opening_debit: str
    opening_credit: str
    period_debit: str
    period_credit: str
    closing_debit: str
    closing_credit: str


class TrialBalanceOut(Schema):
    organization_id: int
    rows: list[TrialBalanceRowOut]
    opening_debit: str
    opening_credit: str
    period_debit: str
    period_credit: str
    closing_debit: str
    closing_credit: str
    difference: str
    is_balanced: bool


class LedgerRowOut(Schema):
    entry_id: int
    entry_number: str
    accounting_date: datetime.date
    line_number: int
    account_id: int
    account_code: str
    branch_id: int | None
    narration: str
    source_document_type: str
    #: A string, not an integer. Upstream documents identify themselves by
    #: UUID as often as by primary key, and a caller that parsed this as a
    #: number would fail on the first journal Sales or Procurement produced.
    source_document_id: str
    debit: str
    credit: str
    running: str


class LedgerOut(Schema):
    organization_id: int
    account_id: int | None
    account_code: str
    opening: str
    debits: str
    credits: str
    closing: str
    rows: list[LedgerRowOut]


class IncomeStatementOut(Schema):
    organization_id: int
    date_from: datetime.date
    date_to: datetime.date
    revenue: SectionOut
    cost_of_sales: SectionOut
    operating_expenses: SectionOut
    other_income: SectionOut
    other_expenses: SectionOut
    gross_profit: str
    operating_profit: str
    net_profit: str
    unmapped: list[AccountRowOut]
    is_approvable: bool


class BalanceSheetOut(Schema):
    organization_id: int
    as_of: datetime.date
    year_start: datetime.date
    current_assets: SectionOut
    non_current_assets: SectionOut
    current_liabilities: SectionOut
    non_current_liabilities: SectionOut
    equity: SectionOut
    current_year_earnings: str
    retained_earnings: str
    assets: str
    liabilities: str
    equity_total: str
    difference: str
    unmapped: list[AccountRowOut]
    is_balanced: bool
    is_approvable: bool


class SupplierPositionOut(Schema):
    supplier_id: int | None
    supplier_code: str
    supplier_name: str
    net_position: str


class SupplierLiabilitiesOut(Schema):
    organization_id: int
    rows: list[SupplierPositionOut]
    total: str


class ApplicationPositionOut(Schema):
    application_id: int | None
    application_name: str
    balance: str


class ApplicationReceivablesOut(Schema):
    organization_id: int
    as_of: datetime.date
    rows: list[ApplicationPositionOut]
    total: str


class BlockerOut(Schema):
    code: str
    severity: str
    message: str
    count: int
    is_blocking: bool


class PreCloseOut(Schema):
    period_id: int
    new_state: str
    journal_count: int
    blockers: list[BlockerOut]
    is_closeable: bool


def _rows(pairs: list[tuple[Any, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "account_id": account.pk,
            "account_code": account.code,
            "account_name_ar": account.name,
            "amount": money_export(amount),
        }
        for account, amount in pairs
    ]


def _section(section: StatementSection) -> dict[str, Any]:
    return {
        "key": section.key,
        "label": str(section.label),
        "total": money_export(section.total),
        "rows": _rows(section.rows),
    }


# ---------------------------------------------------------------------------
# ميزان المراجعة
# ---------------------------------------------------------------------------


@router.get("/reports/trial-balance/", response=TrialBalanceOut, summary="ميزان المراجعة")
def trial_balance_endpoint(
    request: HttpRequest,
    organization_id: int | None = None,
    branch_id: int | None = None,
    cost_center_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    account_class: str = "",
    code_from: str = "",
    code_to: str = "",
    include_zero: bool = False,
) -> dict[str, Any]:
    actor = _actor(request)
    filters = build_report_filters(
        actor=actor,
        organization_id=organization_id,
        branch_id=branch_id,
        cost_center_id=cost_center_id,
        date_from=date_from,
        date_to=date_to,
        account_class=account_class,
        code_from=code_from,
        code_to=code_to,
        include_zero=include_zero,
    )
    report = read_trial_balance(actor=actor, filters=filters)
    return {
        "organization_id": filters.organization.pk,
        "rows": [
            {
                "account_id": row.account.pk,
                "account_code": row.account.code,
                "account_name_ar": row.account.name,
                "opening_debit": money_export(row.opening_debit),
                "opening_credit": money_export(row.opening_credit),
                "period_debit": money_export(row.period_debit),
                "period_credit": money_export(row.period_credit),
                "closing_debit": money_export(row.closing_debit),
                "closing_credit": money_export(row.closing_credit),
            }
            for row in report.rows
        ],
        "opening_debit": money_export(report.opening_debit),
        "opening_credit": money_export(report.opening_credit),
        "period_debit": money_export(report.period_debit),
        "period_credit": money_export(report.period_credit),
        "closing_debit": money_export(report.closing_debit),
        "closing_credit": money_export(report.closing_credit),
        "difference": money_export(report.difference),
        "is_balanced": report.is_balanced,
    }


# ---------------------------------------------------------------------------
# دفتر الأستاذ
# ---------------------------------------------------------------------------


@router.get("/reports/general-ledger/", response=LedgerOut, summary="دفتر الأستاذ")
def general_ledger_endpoint(
    request: HttpRequest,
    account_id: int | None = None,
    organization_id: int | None = None,
    branch_id: int | None = None,
    cost_center_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    source_type: str = "",
    origin: str = "",
) -> dict[str, Any]:
    actor = _actor(request)
    filters = build_report_filters(
        actor=actor,
        organization_id=organization_id,
        branch_id=branch_id,
        cost_center_id=cost_center_id,
        date_from=date_from,
        date_to=date_to,
    )
    ledger = read_general_ledger(
        actor=actor,
        filters=filters,
        account_id=account_id,
        source_type=source_type,
        origin=origin,
    )
    return {
        "organization_id": filters.organization.pk,
        "account_id": ledger.account.pk if ledger.account is not None else None,
        "account_code": ledger.account.code if ledger.account is not None else "",
        "opening": money_export(ledger.opening),
        "debits": money_export(ledger.debits),
        "credits": money_export(ledger.credits),
        "closing": money_export(ledger.closing),
        "rows": [
            {
                "entry_id": row.line.entry_id,
                "entry_number": row.line.entry.entry_number,
                "accounting_date": row.line.entry.accounting_date,
                "line_number": row.line.line_number,
                "account_id": row.line.account_id,
                "account_code": row.line.account.code,
                "branch_id": row.line.branch_id,
                "narration": row.line.narration,
                "source_document_type": row.line.entry.source_document_type,
                "source_document_id": row.line.entry.source_document_id,
                "debit": money_export(row.line.debit),
                "credit": money_export(row.line.credit),
                "running": money_export(row.running),
            }
            for row in ledger.rows
        ],
    }


# ---------------------------------------------------------------------------
# قائمة الدخل · الميزانية العمومية
# ---------------------------------------------------------------------------


def _income(
    report: IncomeStatement, organization_id: int, *, start: Any, end: Any
) -> dict[str, Any]:
    return {
        "organization_id": organization_id,
        "date_from": start,
        "date_to": end,
        "revenue": _section(report.revenue),
        "cost_of_sales": _section(report.cost_of_sales),
        "operating_expenses": _section(report.operating_expenses),
        "other_income": _section(report.other_income),
        "other_expenses": _section(report.other_expenses),
        "gross_profit": money_export(report.gross_profit),
        "operating_profit": money_export(report.operating_profit),
        "net_profit": money_export(report.net_profit),
        "unmapped": _rows(report.unmapped),
        "is_approvable": report.is_approvable,
    }


def _balance(report: BalanceSheet, organization_id: int, *, year_start: Any) -> dict[str, Any]:
    return {
        "organization_id": organization_id,
        "as_of": report.as_of,
        "year_start": year_start,
        "current_assets": _section(report.current_assets),
        "non_current_assets": _section(report.non_current_assets),
        "current_liabilities": _section(report.current_liabilities),
        "non_current_liabilities": _section(report.non_current_liabilities),
        "equity": _section(report.equity),
        "current_year_earnings": money_export(report.current_year_earnings),
        "retained_earnings": money_export(report.retained_earnings),
        "assets": money_export(report.assets),
        "liabilities": money_export(report.liabilities),
        "equity_total": money_export(report.equity_total),
        "difference": money_export(report.difference),
        "unmapped": _rows(report.unmapped),
        "is_balanced": report.is_balanced,
        "is_approvable": report.is_approvable,
    }


@router.get("/reports/income-statement/", response=IncomeStatementOut, summary="قائمة الدخل")
def income_statement_endpoint(
    request: HttpRequest,
    organization_id: int | None = None,
    branch_id: int | None = None,
    cost_center_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> dict[str, Any]:
    actor = _actor(request)
    today = timezone.localdate()
    start = date_from or today.replace(month=1, day=1)
    end = date_to or today
    filters = build_report_filters(
        actor=actor,
        organization_id=organization_id,
        branch_id=branch_id,
        cost_center_id=cost_center_id,
        date_from=start,
        date_to=end,
    )
    report = read_income_statement(actor=actor, filters=filters, date_from=start, date_to=end)
    return _income(report, filters.organization.pk, start=start, end=end)


@router.get("/reports/balance-sheet/", response=BalanceSheetOut, summary="الميزانية العمومية")
def balance_sheet_endpoint(
    request: HttpRequest,
    organization_id: int | None = None,
    branch_id: int | None = None,
    cost_center_id: int | None = None,
    as_of: datetime.date | None = None,
    year_start: datetime.date | None = None,
) -> dict[str, Any]:
    actor = _actor(request)
    today = timezone.localdate()
    day = as_of or today
    start = year_start or day.replace(month=1, day=1)
    filters = build_report_filters(
        actor=actor,
        organization_id=organization_id,
        branch_id=branch_id,
        cost_center_id=cost_center_id,
        date_to=day,
    )
    report = read_balance_sheet(actor=actor, filters=filters, as_of=day, year_start=start)
    return _balance(report, filters.organization.pk, year_start=start)


# ---------------------------------------------------------------------------
# ذمم الموردين · ذمم التطبيقات — reconciliation workspaces
# ---------------------------------------------------------------------------


@router.get(
    "/subledgers/supplier-liabilities/",
    response=SupplierLiabilitiesOut,
    summary="ذمم الموردين — read-only, from Procurement's own documents",
)
def supplier_liabilities_endpoint(
    request: HttpRequest, organization_id: int | None = None
) -> dict[str, Any]:
    from decimal import Decimal

    organization, rows = read_supplier_liabilities(
        actor=_actor(request), organization_id=organization_id
    )
    total = sum((row.get("net_position") or Decimal("0") for row in rows), Decimal("0"))
    return {
        "organization_id": organization.pk,
        "rows": [
            {
                "supplier_id": row.get("supplier_id"),
                "supplier_code": str(row.get("supplier_code") or ""),
                "supplier_name": str(row.get("supplier_name") or ""),
                "net_position": money_export(row.get("net_position") or Decimal("0")),
            }
            for row in rows
        ],
        "total": money_export(total),
    }


@router.get(
    "/subledgers/application-receivables/",
    response=ApplicationReceivablesOut,
    summary="ذمم التطبيقات — read-only, from the Sales receivable ledger",
)
def application_receivables_endpoint(
    request: HttpRequest, organization_id: int | None = None, as_of: datetime.date | None = None
) -> dict[str, Any]:
    from decimal import Decimal

    organization, positions = read_application_receivables(
        actor=_actor(request), organization_id=organization_id, as_of=as_of
    )
    total = sum((position.balance for position in positions), Decimal("0"))
    return {
        "organization_id": organization.pk,
        "as_of": as_of or timezone.localdate(),
        "rows": [
            {
                "application_id": getattr(position, "application_id", None),
                "application_name": str(getattr(position, "application_name", "") or ""),
                "balance": money_export(position.balance),
            }
            for position in positions
        ],
        "total": money_export(total),
    }


# ---------------------------------------------------------------------------
# الفترات المحاسبية — the pre-close report
# ---------------------------------------------------------------------------


@router.get(
    "/periods/{period_id}/pre-close/",
    response=PreCloseOut,
    summary="Every blocker standing between this period and a close",
)
def pre_close_endpoint(
    request: HttpRequest, period_id: int, new_state: str = "CLOSED"
) -> dict[str, Any]:
    """
    Collects **all** blockers, unlike the close command which stops at the first.

    A close attempt is right to refuse immediately; a preview is not. An
    accountant clearing a month needs the whole list at once, and a preview
    that revealed one problem per attempt would turn one afternoon into six.
    """
    from apps.accounting.commands import _resolve_period

    actor = _actor(request)
    period = _resolve_period(actor, period_id)
    blockers = period_close_blockers(period=period, new_state=new_state)
    activity = period_activity(period=period)
    return {
        "period_id": period.pk,
        "new_state": new_state,
        "journal_count": activity.posted_entries,
        "blockers": [
            {
                "code": blocker.code,
                "severity": blocker.severity,
                "message": str(blocker.message),
                "count": blocker.count,
                "is_blocking": blocker.is_blocking,
            }
            for blocker in blockers
        ],
        "is_closeable": not any(blocker.is_blocking for blocker in blockers),
    }
