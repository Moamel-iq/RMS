"""
The posting service.

These are the invariants from docs/specs/accounting-kernel-invariants.md.
Each is a defect if it fails, not a style disagreement.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
)
from apps.accounting.selectors import (
    account_balance,
    trial_balance,
    trial_balance_totals,
)
from apps.accounting.services import (
    archive_account,
    close_period,
    post_entry,
    reopen_period,
    resolve_period,
    reverse_entry,
    soft_close_period,
)
from apps.accounting.validators import PostingLine
from apps.core.context import audit_context
from apps.core.models import AuditAction, AuditEvent
from apps.organizations.models import Branch, Organization
from apps.users.models import User

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db


def sale_lines(
    cash: Account, sales: Account, branch: Branch, hall: CostCenter, amount: str = "100000"
) -> list[PostingLine]:
    """A simple cash sale: debit cash, credit revenue."""
    return [
        PostingLine(account=cash, branch=branch, debit=Decimal(amount)),
        PostingLine(account=sales, branch=branch, credit=Decimal(amount), cost_center=hall),
    ]


def close_through(organization: Organization, period: AccountingPeriod) -> None:
    """
    Close every period up to and including this one, in order.

    Closing is sequential now, so a test that wants March closed must close
    January and February first.
    """
    earlier = AccountingPeriod.objects.filter(
        fiscal_year__organization=organization, period_number__lte=period.period_number
    ).order_by("period_number")
    for each in earlier:
        close_period(period=each, reason="month end")


class TestBalance:
    def test_a_balanced_entry_posts(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="sale-1",
        )
        assert entry.status == JournalEntryStatus.POSTED
        assert entry.lines.count() == 2

    def test_an_unbalanced_entry_is_refused(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=Decimal("100")),
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("99"), cost_center=hall
                    ),
                ],
                idempotency_key="unbalanced",
            )
        assert exc.value.code == "unbalanced"
        assert not JournalEntry.objects.filter(idempotency_key="unbalanced").exists()

    def test_balance_is_compared_on_stored_values(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Two amounts that differ below the stored precision are the same posted
        amount, so the entry balances once quantized.
        """
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100.0001")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100.0002"), cost_center=hall
                ),
            ],
            idempotency_key="sub-precision",
        )
        debits = sum(line.debit for line in entry.lines.all())
        credits = sum(line.credit for line in entry.lines.all())
        assert debits == credits == Decimal("100.000")

    def test_the_database_refuses_an_unbalanced_entry_written_directly(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The posting service can be bypassed by raw SQL. The deferred constraint
        trigger cannot.
        """
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="tamper-balance",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO accounting_journalline "
                    "(entry_id, line_number, account_id, branch_id, cost_center_id,"
                    " debit, credit, narration) "
                    "VALUES (%s, 99, %s, %s, NULL, 5, 0, '')",
                    [entry.pk, cash.pk, branch.pk],
                )
                # The balance trigger is DEFERRABLE INITIALLY DEFERRED, so it
                # normally fires at COMMIT — which inside a test transaction
                # would be teardown, long after the assertion. Forcing the
                # deferred checks to run now is what makes it observable.
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_at_least_two_lines_are_required(
        self, organization: Organization, cash: Account, branch: Branch
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[PostingLine(account=cash, branch=branch, debit=Decimal("1"))],
                idempotency_key="one-line",
            )
        assert exc.value.code == "too_few_lines"


class TestLineShape:
    def test_a_line_cannot_carry_both_sides(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(
                        account=cash, branch=branch, debit=Decimal("10"), credit=Decimal("10")
                    ),
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("10"), cost_center=hall
                    ),
                ],
                idempotency_key="both-sides",
            )
        assert exc.value.code == "both_sides"

    def test_a_line_needs_an_amount(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch),
                    PostingLine(account=sales, branch=branch, cost_center=hall),
                ],
                idempotency_key="zero-lines",
            )
        assert exc.value.code == "zero_amount"

    def test_a_negative_amount_is_refused(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """Use the other side; a negative debit is a credit written wrongly."""
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=Decimal("-10")),
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("-10"), cost_center=hall
                    ),
                ],
                idempotency_key="negative",
            )
        assert exc.value.code == "negative_amount"

    def test_no_float_may_reach_an_amount(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=100.5),  # type: ignore[arg-type]
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("100.5"), cost_center=hall
                    ),
                ],
                idempotency_key="float",
            )
        assert exc.value.code == "float_in_quantity_path"


class TestPostableAccounts:
    def test_posting_to_a_group_account_is_refused(
        self,
        organization: Organization,
        group_account: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=group_account, branch=branch, debit=Decimal("10")),
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("10"), cost_center=hall
                    ),
                ],
                idempotency_key="group-account",
            )
        assert exc.value.code == "account_not_postable"

    def test_the_database_also_refuses_a_group_account(
        self,
        organization: Organization,
        group_account: Account,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="postable-db",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO accounting_journalline "
                    "(entry_id, line_number, account_id, branch_id, cost_center_id,"
                    " debit, credit, narration) VALUES (%s, 98, %s, %s, NULL, 1, 0, '')",
                    [entry.pk, group_account.pk, branch.pk],
                )

    def test_posting_to_an_archived_account_is_refused(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        # Through the service, not by writing the flag: since Phase 5 the
        # database requires `archived_at` and `is_active` to agree, so setting
        # one by hand is a row the constraint refuses — and the test would be
        # proving something the application cannot produce anyway.
        archive_account(account=cash, reason="withdrawn")
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=sale_lines(cash, sales, branch, hall),
                idempotency_key="archived",
            )
        assert exc.value.code == "account_inactive"


class TestCostCenterPolicy:
    def test_revenue_requires_a_cost_center(
        self, organization: Organization, cash: Account, sales: Account, branch: Branch
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=Decimal("10")),
                    PostingLine(account=sales, branch=branch, credit=Decimal("10")),
                ],
                idempotency_key="no-cc",
            )
        assert exc.value.code == "cost_center_required"

    def test_cash_does_not_require_one(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        "Which cost centre does the bank balance belong to" has no answer, and
        a forced value there corrupts the analysis it was meant to serve.
        """
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="cash-no-cc",
        )
        cash_line = entry.lines.get(account=cash)
        assert cash_line.cost_center is None

    def test_the_policy_comes_from_the_account(
        self, organization: Organization, cash: Account, sales: Account, rent: Account
    ) -> None:
        assert sales.requires_cost_center is True
        assert rent.requires_cost_center is True
        assert cash.requires_cost_center is False


class TestOrganizationIsolation:
    def test_an_account_from_another_organization_is_refused(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        branch: Branch,
    ) -> None:
        from django.core.management import call_command

        call_command("seed_chart_of_accounts", organization="RIVAL", verbosity=0)
        foreign = Account.objects.get(organization=other_organization, code="4-01-01-001")
        foreign_cc = CostCenter.objects.get(organization=other_organization, code="HALL")

        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=Decimal("10")),
                    PostingLine(
                        account=foreign, branch=branch, credit=Decimal("10"), cost_center=foreign_cc
                    ),
                ],
                idempotency_key="foreign-account",
            )
        assert exc.value.code == "account_organization_mismatch"

    def test_a_branch_from_another_organization_is_refused(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        sales: Account,
        hall: CostCenter,
    ) -> None:
        from datetime import time

        from apps.organizations.services import create_branch

        foreign_branch = create_branch(
            organization=other_organization,
            code="OTHERBR",
            name="فرع آخر",
            business_day_start_time=time(9, 0),
        )
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=foreign_branch, debit=Decimal("10")),
                    PostingLine(
                        account=sales, branch=foreign_branch, credit=Decimal("10"), cost_center=hall
                    ),
                ],
                idempotency_key="foreign-branch",
            )
        assert exc.value.code == "branch_organization_mismatch"

    def test_a_cost_center_from_another_organization_is_refused(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
    ) -> None:
        from apps.accounting.services import create_cost_center

        foreign_cc = create_cost_center(organization=other_organization, code="HALL", name="صالة")
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=Decimal("10")),
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("10"), cost_center=foreign_cc
                    ),
                ],
                idempotency_key="foreign-cc",
            )
        assert exc.value.code == "cost_center_organization_mismatch"


class TestIdempotency:
    def test_the_same_key_posts_once(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """A retried request after a timeout must not double-post."""
        first = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="retry-me",
        )
        second = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="retry-me",
        )
        assert first.pk == second.pk
        assert JournalEntry.objects.filter(idempotency_key="retry-me").count() == 1

    def test_different_keys_post_separately(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="a",
        )
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="b",
        )
        assert JournalEntry.objects.count() == 2


class TestAtomicity:
    def test_a_failed_posting_leaves_nothing_behind(
        self, organization: Organization, cash: Account, sales: Account, branch: Branch
    ) -> None:
        """A half-posted entry is worse than a failed one; it looks complete."""
        before_entries = JournalEntry.objects.count()
        before_lines = JournalLine.objects.count()

        with pytest.raises(ValidationError):
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=Decimal("10")),
                    PostingLine(account=sales, branch=branch, credit=Decimal("10")),
                ],
                idempotency_key="will-fail",
            )

        assert JournalEntry.objects.count() == before_entries
        assert JournalLine.objects.count() == before_lines


class TestNumbering:
    def test_entries_are_numbered_sequentially(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        numbers = [
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=sale_lines(cash, sales, branch, hall),
                idempotency_key=f"seq-{index}",
            ).entry_number
            for index in range(3)
        ]
        assert numbers == ["JE-2026-000001", "JE-2026-000002", "JE-2026-000003"]

    def test_numbering_is_scoped_to_the_organization(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        from datetime import time

        from django.core.management import call_command

        from apps.organizations.services import create_branch

        first = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="km-1",
        )
        call_command("seed_chart_of_accounts", organization="RIVAL", verbosity=0)
        rival_branch = create_branch(
            organization=other_organization,
            code="RB",
            name="ر",
            business_day_start_time=time(9, 0),
        )
        rival_cash = Account.objects.get(organization=other_organization, code="1-01-01-001")
        rival_sales = Account.objects.get(organization=other_organization, code="4-01-01-001")
        rival_hall = CostCenter.objects.get(organization=other_organization, code="HALL")

        second = post_entry(
            organization=other_organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(rival_cash, rival_sales, rival_branch, rival_hall),
            idempotency_key="rival-1",
        )
        assert first.entry_number == second.entry_number == "JE-2026-000001"


class TestPeriods:
    def test_the_period_is_resolved_from_the_accounting_date(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="period",
        )
        assert entry.period.period_number == 3

    def test_a_closed_period_refuses_postings(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        close_through(organization, period)

        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=sale_lines(cash, sales, branch, hall),
                idempotency_key="into-closed",
            )
        assert exc.value.code == "period_closed"

    def test_a_soft_closed_period_refuses_routine_postings(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        soft_close_period(period=period)

        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=sale_lines(cash, sales, branch, hall),
                idempotency_key="into-soft",
            )
        assert exc.value.code == "period_soft_closed"

    def test_reopening_requires_a_reason(self, organization: Organization) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        close_through(organization, period)
        with pytest.raises(ValidationError) as exc:
            reopen_period(period=period, reason="   ")
        assert exc.value.code == "reopen_reason_required"

    def test_reopening_is_audited_with_its_reason(
        self, organization: Organization, actor: User
    ) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        with audit_context(actor=actor):
            close_through(organization, period)
            reopen_period(period=period, reason="supplier invoice arrived late")

        event = AuditEvent.objects.filter(action=AuditAction.PERIOD_REOPENED).latest("occurred_at")
        assert event.reason == "supplier invoice arrived late"
        assert event.actor == actor

    def test_posting_works_again_after_reopening(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        close_through(organization, period)
        reopen_period(period=period, reason="late invoice")
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="after-reopen",
        )
        assert entry.status == JournalEntryStatus.POSTED

    def test_a_date_with_no_period_is_refused(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=datetime.date(2099, 1, 1),
                lines=sale_lines(cash, sales, branch, hall),
                idempotency_key="no-period",
            )
        assert exc.value.code == "no_period"

    def test_a_year_has_twelve_periods_and_no_thirteenth(self, organization: Organization) -> None:
        periods = AccountingPeriod.objects.filter(fiscal_year__organization=organization)
        assert periods.count() == 12
        assert periods.filter(period_number=13).count() == 0


class TestImmutability:
    def test_a_posted_entry_cannot_be_deleted(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="no-delete",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).delete()

    def test_a_posted_line_cannot_be_changed(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="no-edit-line",
        )
        line = entry.lines.first()
        assert line is not None
        with pytest.raises(IntegrityError), transaction.atomic():
            JournalLine.objects.filter(pk=line.pk).update(debit=Decimal("1"))

    def test_a_posted_line_cannot_be_deleted(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="no-delete-line",
        )
        line = entry.lines.first()
        assert line is not None
        with pytest.raises(IntegrityError), transaction.atomic():
            JournalLine.objects.filter(pk=line.pk).delete()

    def test_a_posted_entrys_amount_cannot_be_rewritten_by_raw_sql(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="no-raw-edit",
        )
        line = entry.lines.first()
        assert line is not None
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounting_journalline SET debit = 1 WHERE id = %s", [line.pk]
                )


class TestReversal:
    def test_a_reversal_mirrors_the_original(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="to-reverse",
        )
        reversal = reverse_entry(entry=original, idempotency_key="reversal-1", reason="keyed twice")

        original_lines = {
            line.account_id: line.debit - line.credit for line in original.lines.all()
        }
        reversal_lines = {
            line.account_id: line.debit - line.credit for line in reversal.lines.all()
        }
        for account_id, amount in original_lines.items():
            assert reversal_lines[account_id] == -amount

    def test_the_pair_nets_to_zero(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="net-zero",
        )
        reverse_entry(entry=original, idempotency_key="net-zero-rev", reason="error")
        assert account_balance(account=cash) == Decimal("0.000")
        assert account_balance(account=sales) == Decimal("0.000")

    def test_the_original_survives_and_is_marked_reversed(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """Nothing is deleted: original, reversal, and replacement all stay visible."""
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="survives",
        )
        reversal = reverse_entry(entry=original, idempotency_key="survives-rev", reason="error")

        original.refresh_from_db()
        assert original.status == JournalEntryStatus.REVERSED
        assert original.lines.count() == 2
        assert reversal.reverses_id == original.pk

    def test_a_reversal_requires_a_reason(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="reason-needed",
        )
        with pytest.raises(ValidationError) as exc:
            reverse_entry(entry=original, idempotency_key="r", reason="  ")
        assert exc.value.code == "reversal_reason_required"

    def test_an_entry_cannot_be_reversed_twice(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The first reversal moves the original to REVERSED, so the second
        attempt is caught by the "only a posted entry can be reversed" guard.
        `already_reversed` sits behind it as defence against a state where the
        reversal exists but the status update did not land.
        """
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="once-only",
        )
        reverse_entry(entry=original, idempotency_key="once-rev", reason="error")
        with pytest.raises(ValidationError) as exc:
            reverse_entry(entry=original, idempotency_key="twice-rev", reason="again")
        assert exc.value.code == "already_reversed"
        assert JournalEntry.objects.filter(reverses=original).count() == 1

    def test_reversal_is_audited(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
        actor: User,
    ) -> None:
        with audit_context(actor=actor):
            original = post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=sale_lines(cash, sales, branch, hall),
                idempotency_key="audited-rev",
            )
            reverse_entry(entry=original, idempotency_key="audited-rev-r", reason="duplicate")

        event = AuditEvent.objects.filter(action=AuditAction.REVERSED).latest("occurred_at")
        assert event.reason == "duplicate"
        assert event.actor == actor

    def test_a_reversal_may_enter_a_soft_closed_period(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """Corrections are exactly what a soft close is meant to still allow."""
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="soft-rev",
        )
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        soft_close_period(period=period)

        reversal = reverse_entry(entry=original, idempotency_key="soft-rev-r", reason="fix")
        assert reversal.status == JournalEntryStatus.POSTED

    def test_a_reversal_cannot_enter_a_closed_period(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="closed-rev",
        )
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        close_through(organization, period)

        with pytest.raises(ValidationError) as exc:
            reverse_entry(entry=original, idempotency_key="closed-rev-r", reason="fix")
        assert exc.value.code == "period_closed"


class TestTrialBalance:
    def test_debits_equal_credits(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        rent: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The smoke test for the whole kernel. The two columns can only match if
        every entry balanced.
        """
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall, "250000"),
            idempotency_key="tb-1",
        )
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=rent, branch=branch, debit=Decimal("400000"), cost_center=hall),
                PostingLine(account=cash, branch=branch, credit=Decimal("400000")),
            ],
            idempotency_key="tb-2",
        )
        debits, credits = trial_balance_totals(organization=organization)
        assert debits == credits
        assert debits == Decimal("650000.000")

    def test_balances_are_derived_not_stored(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall, "75000"),
            idempotency_key="derived",
        )
        assert account_balance(account=cash) == Decimal("75000.000")
        assert account_balance(account=sales) == Decimal("-75000.000")

    def test_the_trial_balance_lists_moved_accounts(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="tb-list",
        )
        codes = {row["code"] for row in trial_balance(organization=organization)}
        assert codes == {"1-01-01-001", "4-01-01-001"}

    def test_balances_can_be_filtered_by_branch(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        from datetime import time

        from apps.organizations.services import create_branch

        other = create_branch(
            organization=organization,
            code="KARRADA",
            name="الكرادة",
            business_day_start_time=time(9, 0),
        )
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall, "1000"),
            idempotency_key="br-1",
        )
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, other, hall, "2000"),
            idempotency_key="br-2",
        )
        assert account_balance(account=cash, branch=branch) == Decimal("1000.000")
        assert account_balance(account=cash, branch=other) == Decimal("2000.000")
        assert account_balance(account=cash) == Decimal("3000.000")
