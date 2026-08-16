"""Accounting reads. Nothing here writes."""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.db.models import DecimalField, QuerySet, Sum, Value
from django.db.models.functions import Coalesce

from apps.accounting.models import (
    Account,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
)
from apps.core.money import MONEY_PLACES
from apps.organizations.models import Branch, Organization

_ZERO = Value(Decimal("0"), output_field=DecimalField(max_digits=30, decimal_places=MONEY_PLACES))


def posted_lines(*, organization: Organization) -> QuerySet[JournalLine]:
    """
    Every line that counts.

    Only POSTED and REVERSED entries: a reversal is itself posted, and its
    original stays in the ledger, so both belong in a balance. Drafts do not.
    """
    return JournalLine.objects.filter(
        entry__organization=organization,
        entry__status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
    )


def account_balance(
    *,
    account: Account,
    branch: Branch | None = None,
    cost_center: CostCenter | None = None,
    up_to: datetime.date | None = None,
) -> Decimal:
    """
    Debits minus credits for one account, derived from the ledger.

    Never a stored number. A balance that can drift from its own movements is
    the failure this architecture exists to prevent.
    """
    lines = posted_lines(organization=account.organization).filter(account=account)
    if branch is not None:
        lines = lines.filter(branch=branch)
    if cost_center is not None:
        lines = lines.filter(cost_center=cost_center)
    if up_to is not None:
        lines = lines.filter(entry__accounting_date__lte=up_to)

    totals = lines.aggregate(
        debits=Coalesce(Sum("debit"), _ZERO), credits=Coalesce(Sum("credit"), _ZERO)
    )
    balance: Decimal = totals["debits"] - totals["credits"]
    return balance


def trial_balance(
    *, organization: Organization, up_to: datetime.date | None = None
) -> list[dict[str, object]]:
    """
    Every postable account with a movement, and its debit and credit totals.

    The smoke test for the whole kernel: the two columns must be equal, and
    they can only be equal if every entry balanced.
    """
    lines = posted_lines(organization=organization)
    if up_to is not None:
        lines = lines.filter(entry__accounting_date__lte=up_to)

    rows = (
        lines.values("account__code", "account__name_ar", "account__name_en")
        .annotate(debits=Coalesce(Sum("debit"), _ZERO), credits=Coalesce(Sum("credit"), _ZERO))
        .order_by("account__code")
    )
    return [
        {
            "code": row["account__code"],
            "name_ar": row["account__name_ar"],
            "name_en": row["account__name_en"],
            "debits": row["debits"],
            "credits": row["credits"],
            "balance": row["debits"] - row["credits"],
        }
        for row in rows
    ]


def trial_balance_totals(
    *, organization: Organization, up_to: datetime.date | None = None
) -> tuple[Decimal, Decimal]:
    """Total debits and total credits. They must be equal."""
    lines = posted_lines(organization=organization)
    if up_to is not None:
        lines = lines.filter(entry__accounting_date__lte=up_to)
    totals = lines.aggregate(
        debits=Coalesce(Sum("debit"), _ZERO), credits=Coalesce(Sum("credit"), _ZERO)
    )
    return totals["debits"], totals["credits"]


def entry_by_idempotency_key(*, organization: Organization, key: str) -> JournalEntry | None:
    """
    The entry a key produced, within one organization.

    The organization is required, not optional. A lookup on the key alone
    would return another organization's journal to anyone who guessed their
    key — and keys are frequently predictable, because upstream modules build
    them from document numbers.
    """
    return JournalEntry.objects.filter(organization=organization, idempotency_key=key).first()
