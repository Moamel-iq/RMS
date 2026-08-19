"""
الفترات المحاسبية — period state, and the pre-close check.

The kernel already owns the *transitions*: `soft_close_period`, `close_period`
and `reopen_period` in `services.py`, with their chronological ordering and the
registered domain vetoes. Nothing here replaces them.

What is here is the thing the kernel deliberately does not do: **collect every
blocker at once**.

`_run_period_close_guards` raises on the first veto, which is correct for a
transition — the close must not proceed — and useless as a preview. An
accountant told "an inventory count is open" fixes it, tries again, and is told
about a draft journal; fixes that, and is told about an unposted voucher. One
close becomes six attempts across three days. So the preview runs each check
independently, catches what each raises, and returns the whole list.

Report-only. There is no repair button and no `--fix`: every blocker here is
somebody else's document, and a screen that could clear them from this side
would be making decisions that belong to the module that owns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    AccountingPeriod,
    AccrualDocument,
    ExpenseVoucher,
    FinancialDocumentStatus,
    JournalEntry,
    JournalEntryStatus,
    PeriodState,
    PrepaymentScheduleLine,
    ScheduleLineStatus,
)
from apps.accounting.selectors import role_usage
from apps.organizations.models import Organization

ZERO = Decimal("0")

#: A blocker that must be cleared before the period closes.
BLOCKING = "BLOCKING"
#: Worth a human's attention; does not by itself stop a close.
ADVISORY = "ADVISORY"


@dataclass(frozen=True)
class Blocker:
    """One reason a period is not ready to close, and where to go to fix it."""

    severity: str
    code: str
    message: str
    count: int = 0
    url_name: str | None = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == BLOCKING


def _draft_journals(period: AccountingPeriod) -> Blocker | None:
    count = JournalEntry.objects.filter(period=period, status=JournalEntryStatus.DRAFT).count()
    if not count:
        return None
    return Blocker(
        severity=BLOCKING,
        code="draft_journals",
        message=str(_("مسودات قيود لم تُرحَّل داخل الفترة")),
        count=count,
        url_name="accounting:journal_list",
    )


def _unposted_expense_vouchers(period: AccountingPeriod) -> Blocker | None:
    count = ExpenseVoucher.objects.filter(
        organization=period.fiscal_year.organization,
        business_date__gte=period.start_date,
        business_date__lte=period.end_date,
        status__in=[FinancialDocumentStatus.DRAFT, FinancialDocumentStatus.APPROVED],
    ).count()
    if not count:
        return None
    return Blocker(
        severity=BLOCKING,
        code="unposted_expense_vouchers",
        message=str(_("سندات مصروف غير مُرحَّلة داخل الفترة")),
        count=count,
        url_name="accounting:expense_list",
    )


def _unposted_accruals(period: AccountingPeriod) -> Blocker | None:
    count = AccrualDocument.objects.filter(
        organization=period.fiscal_year.organization,
        business_date__gte=period.start_date,
        business_date__lte=period.end_date,
        status__in=[FinancialDocumentStatus.DRAFT, FinancialDocumentStatus.APPROVED],
    ).count()
    if not count:
        return None
    return Blocker(
        severity=BLOCKING,
        code="unposted_accruals",
        message=str(_("مستحقات غير مُرحَّلة داخل الفترة")),
        count=count,
        url_name="accounting:deferral_list",
    )


def _due_prepayment_lines(period: AccountingPeriod) -> Blocker | None:
    """
    Schedule rows whose period has ended and which have not been amortized.

    Blocking rather than advisory: closing over them leaves the expense in the
    prepaid account, so the month understates its own cost and the next one
    overstates it — and neither figure looks wrong on its own.
    """
    count = PrepaymentScheduleLine.objects.filter(
        prepayment__organization=period.fiscal_year.organization,
        prepayment__status=FinancialDocumentStatus.POSTED,
        status=ScheduleLineStatus.PLANNED,
        period_end__lte=period.end_date,
    ).count()
    if not count:
        return None
    return Blocker(
        severity=BLOCKING,
        code="due_prepayment_lines",
        message=str(_("أقساط مقدمات مستحقة لم تُرحَّل")),
        count=count,
        url_name="accounting:deferral_list",
    )


def _unmapped_roles(period: AccountingPeriod) -> Blocker | None:
    organization = period.fiscal_year.organization
    missing = [row for row in role_usage(organizations=[organization]) if row.unresolved]
    if not missing:
        return None
    return Blocker(
        severity=ADVISORY,
        code="unmapped_roles",
        message=str(_("أدوار محاسبية بلا حساب سارٍ — أي ترحيل يحلّها سيفشل")),
        count=len(missing),
        url_name="accounting:role_list",
    )


def _domain_guards(period: AccountingPeriod, new_state: str) -> list[Blocker]:
    """
    Every registered domain veto, run **independently**.

    The kernel's own runner stops at the first raise, which is right for the
    transition and wrong for a preview. Each guard is called in its own
    try/except here so an open inventory count and an unposted receipt are both
    reported in one pass.
    """
    from apps.accounting.services import _PERIOD_CLOSE_GUARDS

    found: list[Blocker] = []
    for guard in _PERIOD_CLOSE_GUARDS:
        try:
            guard(period, new_state)
        except ValidationError as veto:
            found.append(
                Blocker(
                    severity=BLOCKING,
                    code=getattr(veto, "code", None) or "domain_guard",
                    message="؛ ".join(str(message) for message in veto.messages),
                    count=1,
                )
            )
        except Exception as unexpected:  # noqa: BLE001 - a broken guard is a finding
            # A guard that raises something other than ValidationError is a
            # defect in that guard. Reported rather than allowed to abort the
            # whole preview, because the other guards' answers are still worth
            # having.
            found.append(
                Blocker(
                    severity=ADVISORY,
                    code="guard_error",
                    message=str(_("تعذّر تشغيل أحد فحوص الإقفال: %(error)s"))
                    % {"error": unexpected},
                    count=1,
                )
            )
    return found


def _out_of_order(period: AccountingPeriod) -> Blocker | None:
    """An earlier period in the same year that is not closed yet."""
    earlier = (
        AccountingPeriod.objects.filter(
            fiscal_year=period.fiscal_year, period_number__lt=period.period_number
        )
        .exclude(state=PeriodState.CLOSED)
        .order_by("period_number")
        .first()
    )
    if earlier is None:
        return None
    return Blocker(
        severity=BLOCKING,
        code="close_out_of_order",
        message=str(_("فترة أسبق ما زالت مفتوحة: %(period)s")) % {"period": str(earlier)},
        count=1,
    )


def period_close_blockers(
    *, period: AccountingPeriod, new_state: str = PeriodState.CLOSED
) -> list[Blocker]:
    """
    Everything standing between this period and a close — all of it, at once.

    Ordered blocking-first so the screen reads as a work list rather than as a
    wall of text.
    """
    checks = (
        _out_of_order,
        _draft_journals,
        _unposted_expense_vouchers,
        _unposted_accruals,
        _due_prepayment_lines,
        _unmapped_roles,
    )
    found: list[Blocker] = []
    for check in checks:
        blocker = check(period)
        if blocker is not None:
            found.append(blocker)
    found.extend(_domain_guards(period, new_state))
    found.sort(key=lambda row: (0 if row.is_blocking else 1, row.code))
    return found


@dataclass(frozen=True)
class PeriodActivity:
    """What a period actually carries. Shown beside its state."""

    posted_entries: int
    draft_entries: int
    debits: Decimal
    credits: Decimal


def period_activity(*, period: AccountingPeriod) -> PeriodActivity:
    from django.db.models import Sum

    from apps.accounting.models import JournalLine

    posted = JournalEntry.objects.filter(
        period=period,
        status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
    )
    totals = JournalLine.objects.filter(entry__in=posted).aggregate(
        debits=Sum("debit"), credits=Sum("credit")
    )
    return PeriodActivity(
        posted_entries=posted.count(),
        draft_entries=JournalEntry.objects.filter(
            period=period, status=JournalEntryStatus.DRAFT
        ).count(),
        debits=totals["debits"] or ZERO,
        credits=totals["credits"] or ZERO,
    )


def fiscal_year_summary(*, organization: Organization) -> list[dict[str, Any]]:
    """Every fiscal year with its periods' states, for the year list screen."""
    from apps.accounting.models import FiscalYear

    rows: list[dict[str, Any]] = []
    for year in FiscalYear.objects.filter(organization=organization).order_by("-year"):
        periods = list(year.periods.order_by("period_number"))
        rows.append(
            {
                "fiscal_year": year,
                "periods": periods,
                "open_count": sum(1 for row in periods if row.state == PeriodState.OPEN),
                "soft_closed_count": sum(
                    1 for row in periods if row.state == PeriodState.SOFT_CLOSED
                ),
                "closed_count": sum(1 for row in periods if row.state == PeriodState.CLOSED),
                # Derived, never stored — the periods are what postings are
                # actually checked against, so a stored flag would be a second
                # source of truth that could disagree with them.
                "is_closed": year.is_closed,
            }
        )
    return rows


__all__ = [
    "ADVISORY",
    "BLOCKING",
    "Blocker",
    "PeriodActivity",
    "fiscal_year_summary",
    "period_activity",
    "period_close_blockers",
]
