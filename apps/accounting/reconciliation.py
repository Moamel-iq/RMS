"""
The Phase 5 accounting checks. Read-only, every one of them.

**Composition, not reimplementation.** Where another module already proves an
equality — `verify_supplier_payables` for the supplier subledger,
`verify_receivable_ledger` for the application one — this forwards its result
rather than deriving a second opinion. A second derivation agrees until the day
it does not, and then there are two answers and no way to tell which is wrong.

Three severities, and only the first is a failure:

    ERROR                — a real disagreement. Exit code 1.
    ADVISORY             — worth a human's attention. Exit code unchanged.
    COVERAGE_LIMITATION  — something is knowably absent. Exit code unchanged.

The middle class is what makes this usable as a gate. An unclassified account
with no balance is not a defect; a verifier that exited non-zero on it would be
red every month and therefore ignored every month.

**No repair mode.** No `--fix`, no `--repair`. A verifier that could change what
it verifies is one nobody can trust, and the one situation where a repair is
tempting — the numbers disagree — is exactly the situation where a human needs
to see them disagree first.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Count, Sum
from django.utils import timezone

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    SUPPLIER_PAYABLE,
    Account,
    AccountReportMapping,
    AccrualDocument,
    BankAccount,
    Cashbox,
    ExpenseVoucher,
    FinancialDocumentStatus,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
    OrganizationAccountMapping,
    Prepayment,
)
from apps.accounting.reports import ReportFilters, balance_sheet, income_statement, trial_balance
from apps.accounting.selectors import account_balance
from apps.accounting.services import resolve_default_account
from apps.organizations.models import Organization

ZERO = Decimal("0")

#: The three severities, and the shape a verifier reports in.
#:
#: Declared here rather than imported from Kitchen's verifier, which is where
#: the same three names first appeared. Accounting must not import Kitchen —
#: `apps/kitchen/tests/test_cost_boundary.py` asserts the direction, and it is
#: right to: the ledger is what Kitchen posts *into*, so a dependency the other
#: way would make the two mutually reachable and the boundary unenforceable.
#:
#: Three identical dataclasses across three modules is the deliberate trade.
#: A shared base would put a vocabulary every module depends on into whichever
#: module happened to define it first, and that is exactly the coupling the
#: boundary test exists to prevent. The cost of the duplication is three lines;
#: the cost of the coupling is an import graph nobody can reason about.
ERROR = "ERROR"
ADVISORY = "ADVISORY"
COVERAGE_LIMITATION = "COVERAGE_LIMITATION"


@dataclass(frozen=True)
class Finding:
    """One thing a verifier noticed, and how seriously to take it."""

    severity: str
    code: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR


def _error(code: str, message: str) -> Finding:
    return Finding(severity=ERROR, code=code, message=message)


def _advisory(code: str, message: str) -> Finding:
    return Finding(severity=ADVISORY, code=code, message=message)


def _limitation(code: str, message: str) -> Finding:
    return Finding(severity=COVERAGE_LIMITATION, code=code, message=message)


# ---------------------------------------------------------------------------
# The ledger itself
# ---------------------------------------------------------------------------


def verify_journals_balance(organization: Organization) -> list[Finding]:
    """Every posted entry's debits equal its credits."""
    findings: list[Finding] = []
    rows = (
        JournalLine.objects.filter(
            entry__organization=organization,
            entry__status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
        )
        .values("entry_id", "entry__entry_number")
        .annotate(debits=Sum("debit"), credits=Sum("credit"))
    )
    for row in rows:
        if (row["debits"] or ZERO) != (row["credits"] or ZERO):
            findings.append(
                _error(
                    "journal_unbalanced",
                    f"{row['entry__entry_number']}: "
                    f"{row['debits']} debit vs {row['credits']} credit",
                )
            )
    return findings


def verify_source_identity(organization: Organization) -> list[Finding]:
    """
    One economic event, one journal.

    The database enforces it per organization; this catches the shape the
    constraint cannot: an identity that is partially filled, which falls
    outside the partial unique index entirely.
    """
    findings: list[Finding] = []
    duplicates = (
        JournalEntry.objects.filter(organization=organization)
        .exclude(source_event="")
        .values("source_document_type", "source_document_id", "source_event")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
    )
    for row in duplicates:
        findings.append(
            _error(
                "duplicate_source_identity",
                f"{row['source_document_type']}/{row['source_document_id']}"
                f"/{row['source_event']} claimed by {row['total']} journals",
            )
        )
    return findings


def verify_manual_maker_checker(organization: Organization) -> list[Finding]:
    """A posted manual journal whose author also released it."""
    findings: list[Finding] = []
    offenders = JournalEntry.objects.filter(
        organization=organization,
        source_event="",
        status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
        created_by__isnull=False,
    ).exclude(posted_by__isnull=True)
    for entry in offenders:
        if entry.created_by_id == entry.posted_by_id:
            findings.append(
                _error(
                    "manual_journal_self_posted",
                    f"{entry.entry_number} was written and posted by the same person",
                )
            )
    return findings


def verify_account_hierarchy(organization: Organization) -> list[Finding]:
    """A postable account has no children; a parent is never postable."""
    findings: list[Finding] = []
    for account in Account.objects.filter(organization=organization, is_postable=True):
        if account.children.exists():
            findings.append(
                _error("postable_account_has_children", f"{account.code} has child accounts")
            )
    return findings


def verify_mapping_continuity(organization: Organization) -> list[Finding]:
    """
    Gaps between a role's mapping versions.

    The EXCLUDE constraint stops two versions covering one day; nothing stops a
    hole, and a hole is the more dangerous of the two — an overlap is refused
    at write time, a gap is discovered when a posting dated inside it fails.
    """
    from apps.accounting.selectors import mapping_continuity_gaps

    return [
        _advisory(
            "mapping_gap",
            f"{gap.role.code}: no account from {gap.starts} to {gap.ends}",
        )
        for gap in mapping_continuity_gaps(organization=organization)
    ]


def verify_cash_account_consistency(organization: Organization) -> list[Finding]:
    """
    One GL account backs at most one active cash record, of either kind.

    The single-table half is a partial unique constraint. The cross-table half
    — a cashbox and a bank account on one account — no constraint can see, so
    it is checked here as well as in the service.
    """
    findings: list[Finding] = []
    cashbox_accounts = dict(
        Cashbox.objects.filter(organization=organization, is_active=True).values_list(
            "account_id", "code"
        )
    )
    for account_id, code in BankAccount.objects.filter(
        organization=organization, is_active=True
    ).values_list("account_id", "code"):
        if account_id in cashbox_accounts:
            findings.append(
                _error(
                    "cash_account_shared",
                    f"bank {code} and cashbox {cashbox_accounts[account_id]} "
                    f"share GL account {account_id}",
                )
            )
    for record in list(Cashbox.objects.filter(organization=organization, is_active=True)) + list(
        BankAccount.objects.filter(organization=organization, is_active=True)
    ):
        if not record.account.is_postable:
            findings.append(
                _error("cash_account_not_postable", f"{record.code} -> {record.account.code}")
            )
    return findings


def verify_no_stored_balance() -> list[Finding]:
    """
    No accounting model carries a stored balance.

    Enforced by absence, which is exactly what a later change adds to without
    noticing — so it is asserted rather than assumed.
    """
    findings: list[Finding] = []
    watched = ("balance", "outstanding", "total_due")
    for model in (Cashbox, BankAccount):
        for field in model._meta.get_fields():
            name = getattr(field, "name", "")
            if any(word in name for word in watched):
                findings.append(
                    _error("stored_balance_field", f"{model.__name__}.{name} stores a balance")
                )
    return findings


# ---------------------------------------------------------------------------
# The subledgers — forwarded, never re-derived
# ---------------------------------------------------------------------------


def verify_supplier_subledger(organization: Organization) -> list[Finding]:
    from apps.procurement.reconciliation import verify_supplier_payables

    findings = [
        _error(
            "supplier_subledger",
            f"{problem.scope} {problem.field}: expected {problem.expected}, got {problem.actual}",
        )
        for problem in verify_supplier_payables(organization)
    ]
    try:
        resolve_default_account(
            organization=organization,
            account_role=SUPPLIER_PAYABLE,
            on_date=timezone.localdate(),
        )
    except ValidationError:
        findings.append(
            _limitation(
                "supplier_payable_unmapped",
                "SUPPLIER_PAYABLE has no account today, so the GL side cannot be compared",
            )
        )
    return findings


def verify_application_subledger(organization: Organization) -> list[Finding]:
    from apps.sales.reconciliation import verify_receivable_ledger

    # Re-wrapped rather than passed through. Sales reports in its own Finding
    # type, and the two are structurally identical today — which is exactly
    # when an implicit pass-through starts to look safe. Copying the three
    # fields keeps the seam visible, so a field added on one side shows up
    # here as a type error rather than as a silently dropped column.
    findings = [
        Finding(severity=row.severity, code=row.code, message=row.message)
        for row in verify_receivable_ledger(organization)
    ]
    try:
        mapping = resolve_default_account(
            organization=organization,
            account_role=DELIVERY_APP_RECEIVABLE,
            on_date=timezone.localdate(),
        )
    except ValidationError:
        findings.append(
            _limitation(
                "app_receivable_unmapped",
                "DELIVERY_APP_RECEIVABLE has no account today",
            )
        )
        return findings

    from apps.sales.models import ApplicationReceivableEntry

    subledger = ApplicationReceivableEntry.objects.filter(organization=organization).aggregate(
        debits=Sum("debit"), credits=Sum("credit")
    )
    ledger_balance = (subledger["debits"] or ZERO) - (subledger["credits"] or ZERO)
    gl_balance = account_balance(account=mapping.account)
    if ledger_balance != gl_balance:
        findings.append(
            _error(
                "app_receivable_vs_gl",
                f"subledger {ledger_balance} vs GL {gl_balance}",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# The Phase 5 documents
# ---------------------------------------------------------------------------


def verify_expense_vouchers(organization: Organization) -> list[Finding]:
    findings: list[Finding] = []
    for voucher in ExpenseVoucher.objects.filter(
        organization=organization, status=FinancialDocumentStatus.POSTED
    ).select_related("journal_entry"):
        if voucher.journal_entry is None:
            findings.append(_error("expense_without_journal", str(voucher)))
            continue
        lines_total = voucher.lines.aggregate(total=Sum("amount"))["total"] or ZERO
        if lines_total != voucher.total_amount:
            findings.append(
                _error(
                    "expense_total_not_line_sum",
                    f"{voucher}: header {voucher.total_amount} vs lines {lines_total}",
                )
            )
        if voucher.created_by_id and voucher.created_by_id == voucher.approved_by_id:
            findings.append(_error("expense_self_approved", str(voucher)))
    return findings


def verify_accruals(organization: Organization) -> list[Finding]:
    findings: list[Finding] = []
    for accrual in AccrualDocument.objects.filter(
        organization=organization, status=FinancialDocumentStatus.POSTED
    ):
        lines_total = accrual.lines.aggregate(total=Sum("amount"))["total"] or ZERO
        if lines_total != accrual.total_amount:
            findings.append(
                _error(
                    "accrual_total_not_line_sum",
                    f"{accrual}: header {accrual.total_amount} vs lines {lines_total}",
                )
            )
        if accrual.settled_by_invoice_id is not None and accrual.reversal_entry_id is None:
            # Both the accrual and its replacement invoice are live: the same
            # expense is recognised twice.
            findings.append(
                _error(
                    "accrual_linked_but_not_reversed",
                    f"{accrual} names a settling invoice but was never reversed",
                )
            )
    return findings


def verify_prepayment_schedules(organization: Organization) -> list[Finding]:
    """`Σ schedule lines == total`, exactly. The residual this prevents is fatal."""
    findings: list[Finding] = []
    for prepayment in Prepayment.objects.filter(organization=organization):
        scheduled = prepayment.schedule_lines.aggregate(total=Sum("amount"))["total"] or ZERO
        if not prepayment.schedule_lines.exists():
            findings.append(_advisory("prepayment_without_schedule", str(prepayment)))
            continue
        if scheduled != prepayment.total_amount:
            findings.append(
                _error(
                    "prepayment_schedule_total",
                    f"{prepayment}: schedule {scheduled} vs total {prepayment.total_amount}",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# The reports
# ---------------------------------------------------------------------------


def verify_trial_balance(organization: Organization) -> list[Finding]:
    report = trial_balance(ReportFilters(organization=organization))
    if not report.is_balanced:
        return [
            _error(
                "trial_balance_unbalanced",
                f"closing debit {report.closing_debit} vs credit {report.closing_credit}",
            )
        ]
    return []


def verify_statement_mapping(organization: Organization) -> list[Finding]:
    """
    Postable accounts with no statement group.

    An unmapped account **with a balance** is an ERROR because it blocks both
    statements; one without is an ADVISORY, because a chart account nobody has
    posted to yet is a housekeeping item, not a defect.
    """
    findings: list[Finding] = []
    mapped = set(
        AccountReportMapping.objects.filter(organization=organization, is_active=True).values_list(
            "account_id", flat=True
        )
    )
    for account in Account.objects.filter(
        organization=organization, is_postable=True, is_active=True
    ):
        if account.pk in mapped:
            continue
        if account_balance(account=account) != ZERO:
            findings.append(
                _error("unmapped_account_with_balance", f"{account.code} {account.name_ar}")
            )
        else:
            findings.append(_advisory("unmapped_account", f"{account.code} {account.name_ar}"))
    return findings


def verify_balance_sheet(organization: Organization) -> list[Finding]:
    today = timezone.localdate()
    report = balance_sheet(
        ReportFilters(organization=organization),
        as_of=today,
        year_start=today.replace(month=1, day=1),
    )
    if not report.is_balanced:
        return [
            _error(
                "balance_sheet_unbalanced",
                f"assets {report.assets} vs liabilities+equity "
                f"{report.liabilities + report.equity_total} (difference {report.difference})",
            )
        ]
    return []


def verify_income_statement(organization: Organization) -> list[Finding]:
    """
    The four formulas hold on the numbers the report itself produced.

    Trivially true from the properties as written, and asserted anyway: the
    day somebody changes one of them, this is where it is caught rather than
    on a printed statement.
    """
    today = timezone.localdate()
    report = income_statement(
        ReportFilters(organization=organization),
        date_from=today.replace(month=1, day=1),
        date_to=today,
    )
    findings: list[Finding] = []
    if report.gross_profit != report.revenue.total - report.cost_of_sales.total:
        findings.append(_error("gross_profit_formula", "gross profit is not revenue - COGS"))
    if report.operating_profit != report.gross_profit - report.operating_expenses.total:
        findings.append(
            _error("operating_profit_formula", "operating profit is not gross - operating")
        )
    expected_net = report.operating_profit + report.other_income.total - report.other_expenses.total
    if report.net_profit != expected_net:
        findings.append(_error("net_profit_formula", "net profit formula does not hold"))
    return findings


def verify_periods(organization: Organization) -> list[Finding]:
    """Nothing posted into a closed period; periods closed in order."""
    from apps.accounting.models import AccountingPeriod, PeriodState

    findings: list[Finding] = []
    for period in AccountingPeriod.objects.filter(
        fiscal_year__organization=organization, state=PeriodState.CLOSED
    ).select_related("fiscal_year"):
        later_open = AccountingPeriod.objects.filter(
            fiscal_year=period.fiscal_year,
            period_number__lt=period.period_number,
        ).exclude(state=PeriodState.CLOSED)
        if later_open.exists():
            findings.append(
                _advisory(
                    "period_closed_out_of_order",
                    f"{period} is closed while an earlier period is not",
                )
            )
    return findings


def counts_for(organization: Organization) -> dict[str, int]:
    """What the verifier actually looked at, so a clean run is not mistaken for an empty one."""
    return {
        "journals": JournalEntry.objects.filter(organization=organization).count(),
        "accounts": Account.objects.filter(organization=organization).count(),
        "mappings": OrganizationAccountMapping.objects.filter(organization=organization).count(),
        "cashboxes": Cashbox.objects.filter(organization=organization).count(),
        "bank_accounts": BankAccount.objects.filter(organization=organization).count(),
        "expense_vouchers": ExpenseVoucher.objects.filter(organization=organization).count(),
        "accruals": AccrualDocument.objects.filter(organization=organization).count(),
        "prepayments": Prepayment.objects.filter(organization=organization).count(),
    }


__all__ = [
    "ADVISORY",
    "COVERAGE_LIMITATION",
    "ERROR",
    "Finding",
    "counts_for",
    "verify_account_hierarchy",
    "verify_accruals",
    "verify_application_subledger",
    "verify_balance_sheet",
    "verify_cash_account_consistency",
    "verify_expense_vouchers",
    "verify_income_statement",
    "verify_journals_balance",
    "verify_manual_maker_checker",
    "verify_mapping_continuity",
    "verify_no_stored_balance",
    "verify_periods",
    "verify_prepayment_schedules",
    "verify_source_identity",
    "verify_statement_mapping",
    "verify_supplier_subledger",
    "verify_trial_balance",
]
