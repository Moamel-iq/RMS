"""
Kernel hardening.

Rules added after the first review of Task 0.6: hierarchy exclusivity, entry
shape, period lifecycle ordering, derived fiscal-year closure, and the
reversal error the API should actually see.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    CostCenter,
    FiscalYear,
    JournalEntryStatus,
    PeriodState,
)
from apps.accounting.services import (
    close_period,
    create_account,
    post_entry,
    reopen_period,
    resolve_period,
    reverse_entry,
    soft_close_period,
)
from apps.accounting.validators import PostingLine
from apps.organizations.models import Branch, Organization

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db


def sale_lines(
    cash: Account, sales: Account, branch: Branch, hall: CostCenter, amount: str = "1000"
) -> list[PostingLine]:
    return [
        PostingLine(account=cash, branch=branch, debit=Decimal(amount)),
        PostingLine(account=sales, branch=branch, credit=Decimal(amount), cost_center=hall),
    ]


class TestHierarchyExclusivity:
    def test_a_posting_account_cannot_acquire_a_child(
        self, organization: Organization, chart: None
    ) -> None:
        """
        Structural: only a four-segment detail code is postable, and no valid
        code extends one. There is no code a child of 1-01-01-001 could have.
        """
        with pytest.raises(ValidationError):
            create_account(
                organization=organization,
                code="1-01-01-001-001",
                name_ar="ابن",
                name_en="Child",
            )

    def test_an_account_with_posting_history_cannot_become_a_parent(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Guarded explicitly as well, so a future change to the code scheme
        cannot quietly open the hole.
        """
        from apps.accounting.validators import validate_parent_has_no_posting_history

        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="history",
        )
        with pytest.raises(ValidationError) as exc:
            validate_parent_has_no_posting_history(cash)
        assert exc.value.code == "parent_has_posting_history"

    def test_an_account_without_history_may_become_a_parent(
        self, organization: Organization, chart: None
    ) -> None:
        subgroup = Account.objects.get(organization=organization, code="1-01-01")
        from apps.accounting.validators import validate_parent_has_no_posting_history

        validate_parent_has_no_posting_history(subgroup)  # must not raise

    def test_an_account_with_children_cannot_receive_a_line(
        self, organization: Organization, sales: Account, branch: Branch, hall: CostCenter
    ) -> None:
        group = Account.objects.get(organization=organization, code="1-01-01")
        assert group.children.exists()
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=group, branch=branch, debit=Decimal("10")),
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("10"), cost_center=hall
                    ),
                ],
                idempotency_key="has-children",
            )
        # Postability is reported first; it is the more useful message and, for
        # a group account, the two conditions always coincide.
        assert exc.value.code == "account_not_postable"


class TestEntryShape:
    def test_an_entry_needs_a_debit_and_a_credit(
        self, organization: Organization, cash: Account, chart: None, branch: Branch
    ) -> None:
        """Two debits balance against nothing; that is not an entry."""
        other_cash = Account.objects.get(organization=organization, code="1-01-02-001")
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=Decimal("10")),
                    PostingLine(account=other_cash, branch=branch, debit=Decimal("10")),
                ],
                idempotency_key="two-debits",
            )
        assert exc.value.code in {"one_sided_entry", "unbalanced"}

    def test_a_two_line_zero_value_entry_is_refused(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        It balances perfectly and records nothing. Balance alone was never a
        sufficient test of a real entry.
        """
        with pytest.raises(ValidationError) as exc:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(account=cash, branch=branch, debit=Decimal("0")),
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("0"), cost_center=hall
                    ),
                ],
                idempotency_key="zero-value",
            )
        assert exc.value.code in {"zero_amount", "zero_value_entry"}

    def test_every_line_amount_must_be_positive(
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
                    PostingLine(account=cash, branch=branch, debit=Decimal("10")),
                    PostingLine(
                        account=sales, branch=branch, credit=Decimal("10"), cost_center=hall
                    ),
                    PostingLine(account=cash, branch=branch, debit=Decimal("0")),
                ],
                idempotency_key="zero-third-line",
            )
        assert exc.value.code == "zero_amount"


class TestPeriodOrdering:
    def periods(self, organization: Organization) -> list[AccountingPeriod]:
        return list(
            AccountingPeriod.objects.filter(fiscal_year__organization=organization).order_by(
                "period_number"
            )
        )

    def test_a_period_cannot_close_before_an_earlier_one(self, organization: Organization) -> None:
        """
        Sealing February while January still accepts entries would let
        January's closing figures change after February carried them forward.
        """
        february = self.periods(organization)[1]
        with pytest.raises(ValidationError) as exc:
            close_period(period=february, reason="out of order")
        assert exc.value.code == "close_out_of_order"

    def test_closing_in_order_works(self, organization: Organization) -> None:
        january, february = self.periods(organization)[:2]
        close_period(period=january, reason="month end")
        close_period(period=february, reason="month end")
        january.refresh_from_db()
        february.refresh_from_db()
        assert january.state == PeriodState.CLOSED
        assert february.state == PeriodState.CLOSED

    def test_a_soft_closed_earlier_period_still_blocks_closing(
        self, organization: Organization
    ) -> None:
        """Soft-closed is not closed; the figures can still move."""
        january, february = self.periods(organization)[:2]
        soft_close_period(period=january)
        with pytest.raises(ValidationError) as exc:
            close_period(period=february)
        assert exc.value.code == "close_out_of_order"

    def test_soft_close_is_not_order_constrained(self, organization: Organization) -> None:
        """It is reversible and carries nothing forward."""
        march = self.periods(organization)[2]
        soft_close_period(period=march)
        march.refresh_from_db()
        assert march.state == PeriodState.SOFT_CLOSED

    def test_a_period_cannot_reopen_while_a_later_one_is_closed(
        self, organization: Organization
    ) -> None:
        january, february = self.periods(organization)[:2]
        close_period(period=january)
        close_period(period=february)
        with pytest.raises(ValidationError) as exc:
            reopen_period(period=january, reason="late invoice")
        assert exc.value.code == "reopen_out_of_order"

    def test_reopening_in_reverse_order_works(self, organization: Organization) -> None:
        january, february = self.periods(organization)[:2]
        close_period(period=january)
        close_period(period=february)
        reopen_period(period=february, reason="late invoice")
        reopen_period(period=january, reason="late invoice")
        january.refresh_from_db()
        assert january.state == PeriodState.OPEN

    def test_reopening_still_rejects_a_whitespace_reason(self, organization: Organization) -> None:
        january = self.periods(organization)[0]
        close_period(period=january)
        with pytest.raises(ValidationError) as exc:
            reopen_period(period=january, reason="\t \n")
        assert exc.value.code == "reopen_reason_required"


class TestFiscalYearClosureIsDerived:
    def test_a_year_is_not_closed_while_any_period_is_open(
        self, organization: Organization
    ) -> None:
        year = FiscalYear.objects.get(organization=organization)
        assert year.is_closed is False

    def test_a_year_is_closed_once_every_period_is(self, organization: Organization) -> None:
        """
        Derived, never stored: a stored flag would be a second source of truth
        that could disagree with the periods postings are checked against.
        """
        year = FiscalYear.objects.get(organization=organization)
        for period in year.periods.order_by("period_number"):
            close_period(period=period, reason="year end")
        assert year.is_closed is True

    def test_there_is_no_stored_closed_field(self, organization: Organization) -> None:
        field_names = {field.name for field in FiscalYear._meta.get_fields()}
        assert "is_closed" not in field_names
        assert "closed" not in field_names
        assert "status" not in field_names


class TestAdjustmentSemantics:
    def test_an_adjustment_may_be_dated_anywhere_in_an_open_period(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        No universal "adjustments must be dated at year end" rule: a month-end
        adjustment is a legitimate accounting act.
        """
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="mid-year-adjustment",
            is_adjustment=True,
        )
        assert entry.is_adjustment is True
        assert entry.accounting_date == POSTING_DATE

    def test_a_year_end_adjustment_is_just_a_date_plus_the_flag(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        year = FiscalYear.objects.get(organization=organization)
        entry = post_entry(
            organization=organization,
            accounting_date=year.end_date,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="year-end-adjustment",
            is_adjustment=True,
        )
        assert entry.is_adjustment is True
        assert entry.accounting_date == year.end_date
        assert entry.period.period_number == 12

    def test_an_ordinary_posting_is_not_flagged(
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
            idempotency_key="ordinary",
        )
        assert entry.is_adjustment is False


class TestReversalErrorAccuracy:
    def test_a_second_reversal_reports_already_reversed(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The accurate domain error. `not_posted` would be technically true and
        tell the caller nothing about what went wrong.
        """
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="rev-accuracy",
        )
        reverse_entry(entry=original, idempotency_key="rev-accuracy-1", reason="error")
        with pytest.raises(ValidationError) as exc:
            reverse_entry(entry=original, idempotency_key="rev-accuracy-2", reason="again")
        assert exc.value.code == "already_reversed"

    def test_a_draft_still_reports_not_posted(
        self, organization: Organization, chart: None
    ) -> None:
        """
        The other branch of the guard. Drafts have no create service yet — that
        is `accounting.create_draft` in Task 0.7 — so the row is built directly.
        """
        from apps.accounting.models import JournalEntry

        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        draft = JournalEntry.objects.create(
            organization=organization,
            period=period,
            entry_number="JE-DRAFT-1",
            accounting_date=POSTING_DATE,
            document_date=POSTING_DATE,
            status=JournalEntryStatus.DRAFT,
            idempotency_key="a-draft",
        )
        with pytest.raises(ValidationError) as exc:
            reverse_entry(entry=draft, idempotency_key="draft-rev", reason="x")
        assert exc.value.code == "not_posted"

    def test_a_posted_entry_cannot_be_pushed_back_to_draft(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Un-posting would erase the fact that it was ever posted. The only
        transition the trigger permits on a posted entry is POSTED -> REVERSED.
        """
        from django.db import IntegrityError, transaction

        from apps.accounting.models import JournalEntry

        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="no-unpost",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).update(status=JournalEntryStatus.DRAFT)


class TestSoftClosedAuthorization:
    def test_a_reversal_still_reaches_a_soft_closed_period(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Approved semantics. The dedicated permission that gates this arrives
        with Task 0.7; today the capability exists and nothing restricts who
        may use it.
        """
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=sale_lines(cash, sales, branch, hall),
            idempotency_key="soft-auth",
        )
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        soft_close_period(period=period)
        reversal = reverse_entry(
            entry=original, idempotency_key="soft-auth-rev", reason="correction"
        )
        assert reversal.status == JournalEntryStatus.POSTED

    def test_nothing_reaches_a_closed_period(
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
            idempotency_key="closed-auth",
        )
        january = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, period_number=1
        )
        close_period(period=january)
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        AccountingPeriod.objects.filter(
            fiscal_year__organization=organization, period_number=2
        ).update(state=PeriodState.CLOSED)
        close_period(period=period)

        with pytest.raises(ValidationError) as exc:
            reverse_entry(entry=original, idempotency_key="closed-auth-rev", reason="fix")
        assert exc.value.code == "period_closed"
