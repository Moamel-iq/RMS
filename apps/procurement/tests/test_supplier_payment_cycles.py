"""
The payment cycle, and the three plans that settle it.

Two subjects, tested apart because they fail apart. The cycle decides **when**
a supplier's invoices fall due — one window opened by the first invoice, shared
by everything raised into it. The planner decides **which** invoices a given
sum settles, and in what order.

The planner's arithmetic is exercised over plain `(invoice, amount)` pairs
rather than through a posting chain: what is being tested is where each plan
stops, and building twenty posted invoices to prove that would test the
posting services instead.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.organizations.models import Organization
from apps.procurement.cycles import (
    close_cycle_if_settled,
    collecting_cycle,
    cycle_for_invoice,
    days_remaining,
    due_date_for_cycle,
    reopen_cycle,
    unsettled_cycles,
)
from apps.procurement.models import (
    Supplier,
    SupplierInvoice,
    SupplierPaymentCycle,
    SupplierPaymentCycleStatus,
)
from apps.procurement.services import create_supplier
from apps.procurement.settlement import (
    Owing,
    PlanKind,
    _exact,
    _over,
    _under,
    target_for,
)

pytestmark = pytest.mark.django_db

MILLION = Decimal("1000000")


@pytest.fixture
def grocery(organization: Organization) -> Supplier:
    return create_supplier(
        organization=organization,
        code="groc-01",
        name="مورد البقالة",
        payment_terms_days=30,
        minimum_settlement_percent=Decimal("60"),
    )


def _open(supplier: Supplier, on: datetime.date, terms: int = 30) -> SupplierPaymentCycle:
    return cycle_for_invoice(supplier=supplier, invoice_date=on, payment_terms_days=terms)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


class TestTheCycleWindow:
    def test_the_first_invoice_opens_a_cycle_due_the_terms_later(self, grocery: Supplier) -> None:
        """1/8 with thirty-day terms falls due on 31/8."""
        cycle = _open(grocery, datetime.date(2026, 8, 1))
        assert cycle.opened_on == datetime.date(2026, 8, 1)
        assert cycle.due_date == datetime.date(2026, 8, 31)
        assert cycle.sequence == 1
        assert cycle.status == SupplierPaymentCycleStatus.COLLECTING

    def test_a_later_invoice_joins_it_and_shares_the_due_date(self, grocery: Supplier) -> None:
        """
        The point of the whole model: 6/8 does not get thirty days of its own.

        It falls due on 31/8 with the cycle, which is twenty-five days away —
        not thirty.
        """
        first = _open(grocery, datetime.date(2026, 8, 1))
        second = _open(grocery, datetime.date(2026, 8, 6))
        assert second.pk == first.pk
        assert second.due_date == datetime.date(2026, 8, 31)
        assert days_remaining(second, on=datetime.date(2026, 8, 6)) == 25

    def test_an_invoice_on_the_due_date_still_joins(self, grocery: Supplier) -> None:
        """The window closes *after* its due date, not before it."""
        first = _open(grocery, datetime.date(2026, 8, 1))
        same = _open(grocery, datetime.date(2026, 8, 31))
        assert same.pk == first.pk

    def test_an_invoice_after_the_due_date_opens_a_new_cycle(self, grocery: Supplier) -> None:
        """
        A cycle stops collecting once it is due, even unpaid.

        Letting 5/9 join a cycle due 31/8 would make it overdue in the moment
        it was keyed — and the invoice's own `due_date >= invoice_date`
        constraint would refuse the row.
        """
        first = _open(grocery, datetime.date(2026, 8, 1))
        second = _open(grocery, datetime.date(2026, 9, 5))
        assert second.pk != first.pk
        assert second.sequence == 2
        assert second.due_date == datetime.date(2026, 10, 5)

        first.refresh_from_db()
        assert first.status == SupplierPaymentCycleStatus.DUE, (
            "the old window stops collecting but stays owed"
        )
        assert len(unsettled_cycles(grocery)) == 2

    def test_the_terms_and_the_floor_are_snapshotted(self, grocery: Supplier) -> None:
        """Renegotiating afterwards must not restate what was already due."""
        cycle = _open(grocery, datetime.date(2026, 8, 1))
        assert cycle.payment_terms_days == 30
        assert cycle.minimum_settlement_percent == Decimal("60")

        from apps.procurement.services import update_supplier

        update_supplier(
            supplier=grocery, name=grocery.name, minimum_settlement_percent=Decimal("20")
        )
        cycle.refresh_from_db()
        assert cycle.minimum_settlement_percent == Decimal("60")
        assert cycle.due_date == datetime.date(2026, 8, 31)

    def test_days_remaining_goes_negative_once_it_is_late(self, grocery: Supplier) -> None:
        """ "Eleven days late" and "due today" are different things to be told."""
        cycle = _open(grocery, datetime.date(2026, 8, 1))
        assert days_remaining(cycle, on=datetime.date(2026, 8, 31)) == 0
        assert days_remaining(cycle, on=datetime.date(2026, 9, 11)) == -11

    def test_the_database_refuses_a_second_collecting_cycle(self, grocery: Supplier) -> None:
        _open(grocery, datetime.date(2026, 8, 1))
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SupplierPaymentCycle.objects.create(
                    organization=grocery.organization,
                    supplier=grocery,
                    sequence=99,
                    status=SupplierPaymentCycleStatus.COLLECTING,
                    opened_on=datetime.date(2026, 8, 2),
                    due_date=datetime.date(2026, 9, 1),
                    payment_terms_days=30,
                )

    def test_a_settled_cycle_is_dated_and_an_open_one_is_not(self, grocery: Supplier) -> None:
        cycle = _open(grocery, datetime.date(2026, 8, 1))
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SupplierPaymentCycle.objects.filter(pk=cycle.pk).update(
                    status=SupplierPaymentCycleStatus.SETTLED, settled_on=None
                )

    def test_cash_terms_make_a_cycle_that_is_due_the_day_it_opens(
        self, organization: Organization
    ) -> None:
        cash = create_supplier(
            organization=organization, code="cash-01", name="مورد نقدي", payment_terms_days=0
        )
        cycle = _open(cash, datetime.date(2026, 8, 1), terms=0)
        assert cycle.due_date == cycle.opened_on
        assert days_remaining(cycle, on=datetime.date(2026, 8, 1)) == 0


class TestClosingAndReopening:
    def test_an_empty_cycle_does_not_close(self, grocery: Supplier) -> None:
        """
        Nothing in it is not the same as nothing owed.

        A cycle with no posted invoice yet has settled nothing, and closing it
        would send the next invoice into a second window for no reason.
        """
        cycle = _open(grocery, datetime.date(2026, 8, 1))
        assert close_cycle_if_settled(cycle).status == SupplierPaymentCycleStatus.COLLECTING

    def test_reopening_lands_on_due_never_back_on_collecting(self, grocery: Supplier) -> None:
        """
        The debt is real again; the window is not open to new invoices again.

        That is also what makes reopening always safe: a reopened cycle can
        never clash with the supplier's current collecting window, because the
        two states answer different questions.
        """
        first = _open(grocery, datetime.date(2026, 8, 1))
        SupplierPaymentCycle.objects.filter(pk=first.pk).update(
            status=SupplierPaymentCycleStatus.SETTLED, settled_on=datetime.date(2026, 8, 20)
        )
        later = _open(grocery, datetime.date(2026, 8, 21))

        first.refresh_from_db()
        assert reopen_cycle(first).status == SupplierPaymentCycleStatus.DUE

        later.refresh_from_db()
        assert later.status == SupplierPaymentCycleStatus.COLLECTING, "untouched"

    def test_reopening_a_cycle_that_was_never_settled_changes_nothing(
        self, grocery: Supplier
    ) -> None:
        cycle = _open(grocery, datetime.date(2026, 8, 1))
        assert reopen_cycle(cycle).status == SupplierPaymentCycleStatus.COLLECTING

    def test_collecting_cycle_finds_the_window_taking_invoices(self, grocery: Supplier) -> None:
        assert collecting_cycle(grocery) is None
        cycle = _open(grocery, datetime.date(2026, 8, 1))
        found = collecting_cycle(grocery)
        assert found is not None and found.pk == cycle.pk

    def test_a_late_account_may_stand_with_several_due_windows(self, grocery: Supplier) -> None:
        """
        Several unpaid windows at once is what a late account looks like.

        The dates are deliberately not one window each. With thirty-day terms:
        1/8 opens a cycle due 31/8; 1/9 is past it and opens a second due 1/10;
        1/10 falls **on** that due date and joins it; 1/11 is past it and opens
        a third. Three windows from four invoices — which is the arithmetic
        this whole model exists to get right, and the reason the assertion is
        not simply "one per invoice".
        """
        for month in (8, 9, 10, 11):
            _open(grocery, datetime.date(2026, month, 1))

        cycles = unsettled_cycles(grocery)
        assert [cycle.due_date for cycle in cycles] == [
            datetime.date(2026, 8, 31),
            datetime.date(2026, 10, 1),
            datetime.date(2026, 12, 1),
        ]
        assert [cycle.status for cycle in cycles] == [
            SupplierPaymentCycleStatus.DUE,
            SupplierPaymentCycleStatus.DUE,
            SupplierPaymentCycleStatus.COLLECTING,
        ]


class TestTheDueDateArithmetic:
    @pytest.mark.parametrize(
        ("opened", "terms", "due"),
        [
            ((2026, 8, 1), 30, (2026, 8, 31)),
            ((2026, 8, 1), 0, (2026, 8, 1)),
            ((2026, 2, 15), 45, (2026, 4, 1)),
            ((2026, 12, 20), 30, (2027, 1, 19)),
        ],
    )
    def test_it_is_plain_calendar_arithmetic(
        self, opened: tuple[int, int, int], terms: int, due: tuple[int, int, int]
    ) -> None:
        assert due_date_for_cycle(
            opened_on=datetime.date(*opened), payment_terms_days=terms
        ) == datetime.date(*due)


# ---------------------------------------------------------------------------
# The three plans
# ---------------------------------------------------------------------------


def _owings(*amounts: str) -> list[Owing]:
    """
    Invoices owing these amounts, oldest first.

    Unsaved rows: the planners read a primary key and nothing else, and giving
    them one by hand is cheaper and clearer than posting a chain of real
    invoices to produce the same five numbers.
    """
    return [
        (SupplierInvoice(pk=index + 1), Decimal(amount)) for index, amount in enumerate(amounts)
    ]


class TestTheTarget:
    def test_it_is_the_open_balance_times_the_agreed_share(self) -> None:
        assert target_for(50 * MILLION, Decimal("60")) == 30 * MILLION

    def test_with_no_agreed_share_the_whole_balance_is_due(self) -> None:
        """
        A supplier who conceded no floor is owed all of it, and the screen may
        not invent a smaller number on their behalf.
        """
        assert target_for(50 * MILLION, None) == 50 * MILLION


class TestThePlans:
    #: The worked example: five invoices, a 30,000,000 target. Four of them
    #: come to 28 million; five come to 35.
    OWINGS = ("7000000", "7000000", "7000000", "7000000", "7000000")
    TARGET = 30 * MILLION

    def test_under_takes_the_most_whole_invoices_that_fit(self) -> None:
        plan = _under(_owings(*self.OWINGS), self.TARGET)
        assert plan.kind == PlanKind.UNDER
        assert plan.invoice_count == 4
        assert plan.total == 28 * MILLION
        assert plan.difference_from(self.TARGET) == -2 * MILLION
        assert not plan.splits_an_invoice

    def test_over_takes_one_more_and_passes_the_target(self) -> None:
        plan = _over(_owings(*self.OWINGS), self.TARGET)
        assert plan.kind == PlanKind.OVER
        assert plan.invoice_count == 5
        assert plan.total == 35 * MILLION
        assert plan.difference_from(self.TARGET) == 5 * MILLION
        assert not plan.splits_an_invoice

    def test_exact_matches_the_target_by_splitting_the_next_invoice(self) -> None:
        plan = _exact(_owings(*self.OWINGS), self.TARGET)
        assert plan.kind == PlanKind.EXACT
        assert plan.invoice_count == 5
        assert plan.total == self.TARGET
        assert plan.difference_from(self.TARGET) == 0
        assert plan.splits_an_invoice
        assert plan.allocations[-1].amount == 2 * MILLION
        assert plan.allocations[-1].outstanding == 7 * MILLION

    def test_every_plan_pays_oldest_first(self) -> None:
        """
        FIFO is not negotiable: the plans differ in where they stop, never in
        what order they go.
        """
        owings = _owings(*self.OWINGS)
        for plan in (
            _under(owings, self.TARGET),
            _over(owings, self.TARGET),
            _exact(owings, self.TARGET),
        ):
            ids = [line.invoice.pk for line in plan.allocations]
            assert ids == sorted(ids), plan.kind

    def test_under_stops_rather_than_skipping_to_a_smaller_invoice(self) -> None:
        """
        A big invoice that does not fit ends the plan. Skipping past it to a
        smaller one further down would pay a newer debt while an older stands,
        which is the whole thing FIFO exists to prevent.
        """
        plan = _under(_owings("40000000", "1000000"), self.TARGET)
        assert plan.invoice_count == 0
        assert plan.total == 0

    def test_over_takes_the_one_that_does_not_fit(self) -> None:
        plan = _over(_owings("40000000", "1000000"), self.TARGET)
        assert plan.invoice_count == 1
        assert plan.total == 40 * MILLION

    def test_an_exact_hit_makes_all_three_agree(self) -> None:
        owings = _owings("10000000", "20000000", "5000000")
        for plan in (
            _under(owings, self.TARGET),
            _over(owings, self.TARGET),
            _exact(owings, self.TARGET),
        ):
            assert plan.total == self.TARGET, plan.kind
            assert plan.invoice_count == 2, plan.kind
            assert not plan.splits_an_invoice, plan.kind

    def test_a_target_of_nothing_plans_nothing(self) -> None:
        for plan in (
            _under(_owings(*self.OWINGS), Decimal("0")),
            _exact(_owings(*self.OWINGS), Decimal("0")),
        ):
            assert plan.invoice_count == 0

    def test_a_target_beyond_the_balance_takes_everything_and_no_more(self) -> None:
        """A plan may not invent debt: it stops at what is actually owed."""
        owings = _owings(*self.OWINGS)
        for plan in (_under(owings, 100 * MILLION), _exact(owings, 100 * MILLION)):
            assert plan.total == 35 * MILLION, plan.kind
            assert plan.invoice_count == 5, plan.kind
            assert not plan.splits_an_invoice, plan.kind
