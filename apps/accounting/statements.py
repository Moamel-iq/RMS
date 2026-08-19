"""
Account statements: opening, movement, closing, and a running balance.

One service, used by the cashbox screen, the bank screen, the account activity
page and — when checkpoint 8 arrives — the general ledger and its CSV. Two
query paths drift, and the CSV is the one nobody looks at until an auditor
does, which is the worst possible moment to discover it disagrees with the
screen (ADR-031 §6).

**The ordering is the entire content of a running balance.**

    business date → posted_at → entry number → line number

Get it wrong and every row is individually plausible while the column is
wrong in total, which is the hardest kind of error to see. `accounting_date`
alone is not enough: several entries share a date, and two postings on the same
date must accumulate in the order they were actually released to the ledger.
`posted_at` breaks that tie, the gapless entry number breaks the remaining tie
between two postings in the same transaction, and the line number orders within
one entry.

The running balance is accumulated **here**, never in a template. A template
cannot carry a `Decimal` across rows without a filter that hides the ordering
assumption, and hiding it is how it gets changed by accident.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import QuerySet

from apps.accounting.models import (
    Account,
    JournalEntryStatus,
    JournalLine,
)
from apps.accounting.selectors import account_balance
from apps.organizations.models import Branch

ZERO = Decimal("0")

#: The one true statement ordering. Named so the ledger, the cash statement and
#: the tests all sort by the same tuple rather than three hand-written copies
#: that agree until one of them is edited.
STATEMENT_ORDER: tuple[str, ...] = (
    "entry__accounting_date",
    "entry__posted_at",
    "entry__entry_number",
    "line_number",
)


@dataclass(frozen=True)
class StatementRow:
    """One posted line, and the balance after it."""

    line: JournalLine
    running: Decimal

    @property
    def entry(self) -> object:
        return self.line.entry


@dataclass(frozen=True)
class Statement:
    """A period of movement on one account, with both ends of it."""

    account: Account
    date_from: datetime.date
    date_to: datetime.date
    opening: Decimal
    rows: list[StatementRow]
    debits: Decimal
    credits: Decimal
    closing: Decimal

    @property
    def is_empty(self) -> bool:
        return not self.rows


def statement_lines(
    *,
    account: Account,
    date_from: datetime.date,
    date_to: datetime.date,
    branch: Branch | None = None,
) -> QuerySet[JournalLine]:
    """The posted lines a statement covers, in statement order."""
    lines = JournalLine.objects.filter(
        account=account,
        entry__status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
        entry__accounting_date__gte=date_from,
        entry__accounting_date__lte=date_to,
    )
    if branch is not None:
        lines = lines.filter(branch=branch)
    return lines.select_related("entry", "branch", "cost_center").order_by(*STATEMENT_ORDER)


def account_statement(
    *,
    account: Account,
    date_from: datetime.date,
    date_to: datetime.date,
    branch: Branch | None = None,
) -> Statement:
    """
    One account's movement over a window, with a running balance.

    The opening balance is the account's balance **up to the day before**
    `date_from` — not up to `date_from` itself, which would double-count
    everything posted on the first day of the window and then show it again in
    the first rows.

    Signed as debit-minus-credit throughout, whatever the account's normal
    balance. Flipping the sign for a credit-balance account is presentation and
    belongs on the screen: a service that returned "positive means more" would
    make two accounts' figures non-comparable and every cross-account total
    wrong.
    """
    opening = account_balance(
        account=account,
        branch=branch,
        up_to=date_from - datetime.timedelta(days=1),
    )

    rows: list[StatementRow] = []
    debits = ZERO
    credits = ZERO
    running = opening
    for line in statement_lines(
        account=account, date_from=date_from, date_to=date_to, branch=branch
    ):
        debits += line.debit
        credits += line.credit
        running = running + line.debit - line.credit
        rows.append(StatementRow(line=line, running=running))

    return Statement(
        account=account,
        date_from=date_from,
        date_to=date_to,
        opening=opening,
        rows=rows,
        debits=debits,
        credits=credits,
        closing=running,
    )


def parse_window(
    raw_from: str, raw_to: str, *, today: datetime.date
) -> tuple[datetime.date, datetime.date]:
    """
    A date window from two query-string values, defaulting to the current month.

    Unparseable input falls back to the default rather than raising: a mistyped
    date in a URL is a typo, and answering 500 to it turns a typo into an
    outage. The screen shows the window it actually used, so the fallback is
    visible rather than silent.
    """
    try:
        date_to = datetime.date.fromisoformat(raw_to.strip()) if raw_to.strip() else today
    except ValueError:
        date_to = today
    try:
        date_from = (
            datetime.date.fromisoformat(raw_from.strip())
            if raw_from.strip()
            else date_to.replace(day=1)
        )
    except ValueError:
        date_from = date_to.replace(day=1)
    if date_from > date_to:
        date_from = date_to.replace(day=1)
    return date_from, date_to


__all__ = [
    "STATEMENT_ORDER",
    "Statement",
    "StatementRow",
    "account_statement",
    "parse_window",
    "statement_lines",
]
