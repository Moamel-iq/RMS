"""
The deferred balance constraint at a real COMMIT.

`test_posting.py` proves the trigger fires by forcing deferred checks with
SET CONSTRAINTS ALL IMMEDIATE. That is a focused test, and on its own it would
leave the suite green while never once exercising what actually happens at a
transaction boundary in production.

These tests use `transaction=True`, so the atomic block ends in a genuine
COMMIT and the deferred trigger fires the way it will on a live database.
They are slower — the database is truncated between them — which is why there
are only as many as the point requires.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from apps.accounting.models import Account, CostCenter, JournalEntry, JournalLine
from apps.accounting.services import (
    configure_accounting,
    open_fiscal_year,
    post_entry,
)
from apps.accounting.validators import PostingLine
from apps.organizations.services import create_branch, create_organization

from .conftest import POSTING_DATE, TEST_YEAR


@pytest.fixture
def ledger():  # type: ignore[no-untyped-def]
    """Build an organization, branch, and chart inside a transactional test."""
    organization = create_organization(code="KM", name="خان مندي")
    configure_accounting(organization=organization)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name="البنوك",
        business_day_start_time=time(9, 0),
    )
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)
    return {
        "organization": organization,
        "branch": branch,
        "cash": Account.objects.get(organization=organization, code="1-01-01-001"),
        "sales": Account.objects.get(organization=organization, code="4-01-01-001"),
        "hall": CostCenter.objects.get(organization=organization, code="HALL"),
    }


@pytest.mark.django_db(transaction=True)
def test_an_unbalancing_line_is_refused_at_commit(ledger) -> None:  # type: ignore[no-untyped-def]
    """
    Production semantics: nothing complains while the statement runs, and the
    transaction is refused when it tries to commit.
    """
    entry = post_entry(
        organization=ledger["organization"],
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=ledger["cash"], branch=ledger["branch"], debit=Decimal("500")),
            PostingLine(
                account=ledger["sales"],
                branch=ledger["branch"],
                credit=Decimal("500"),
                cost_center=ledger["hall"],
            ),
        ],
        idempotency_key="commit-balance",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO accounting_journalline "
                    "(entry_id, line_number, account_id, branch_id, cost_center_id,"
                    " debit, credit, narration) VALUES (%s, 50, %s, %s, NULL, 7, 0, '')",
                    [entry.pk, ledger["cash"].pk, ledger["branch"].pk],
                )
                # No exception here. The trigger is deferred; the failure
                # arrives when this atomic block commits.

    # The rejected transaction left nothing behind.
    assert JournalLine.objects.filter(entry=entry).count() == 2
    debits = sum(line.debit for line in JournalLine.objects.filter(entry=entry))
    credits = sum(line.credit for line in JournalLine.objects.filter(entry=entry))
    assert debits == credits == Decimal("500.000")


@pytest.mark.django_db(transaction=True)
def test_a_balanced_entry_survives_a_real_commit(ledger) -> None:  # type: ignore[no-untyped-def]
    """The other half: the deferred check must not reject a valid entry."""
    with transaction.atomic():
        entry = post_entry(
            organization=ledger["organization"],
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=ledger["cash"], branch=ledger["branch"], debit=Decimal("250")),
                PostingLine(
                    account=ledger["sales"],
                    branch=ledger["branch"],
                    credit=Decimal("250"),
                    cost_center=ledger["hall"],
                ),
            ],
            idempotency_key="commit-ok",
        )

    entry.refresh_from_db()
    assert JournalEntry.objects.filter(pk=entry.pk).exists()
    assert entry.lines.count() == 2


@pytest.mark.django_db(transaction=True)
def test_deleting_one_line_of_a_posted_entry_is_refused(ledger) -> None:  # type: ignore[no-untyped-def]
    """
    Removing a line would leave the entry unbalanced. Two guards catch it: the
    immutability trigger immediately, and the balance trigger at commit had it
    got past.
    """
    entry = post_entry(
        organization=ledger["organization"],
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=ledger["cash"], branch=ledger["branch"], debit=Decimal("100")),
            PostingLine(
                account=ledger["sales"],
                branch=ledger["branch"],
                credit=Decimal("100"),
                cost_center=ledger["hall"],
            ),
        ],
        idempotency_key="commit-delete",
    )
    line = entry.lines.first()
    assert line is not None

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM accounting_journalline WHERE id = %s", [line.pk])

    assert entry.lines.count() == 2
