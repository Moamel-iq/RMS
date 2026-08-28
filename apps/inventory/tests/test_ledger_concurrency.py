"""
Concurrency, at real COMMIT boundaries.

`pytest.mark.django_db` wraps each test in a transaction that is rolled back,
so nothing another connection could see ever exists. These tests use
`transaction=True` instead: every posting genuinely commits, and the threads
below are genuinely racing.

That makes them slower and it makes them worth having. Overselling is not a
bug you find by reading code — it is two connections both seeing "10 on hand"
and both deciding 8 is available.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import time
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from apps.accounting.services import open_fiscal_year
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import (
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
)
from apps.inventory.services import create_item, create_item_category, create_warehouse
from apps.organizations.services import create_branch, create_organization
from apps.units.models import UnitOfMeasure

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(django_db_setup: Any, django_db_blocker: Any) -> dict[str, Any]:
    """
    A committed organization, branch, warehouses, and items.

    Built once per test and torn down explicitly, because `transaction=True`
    means nothing here is rolled back for us.
    """
    from django.core.management import call_command

    call_command("seed_units", verbosity=0)
    organization = create_organization(code="KM", name="خان مندي")
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name="البنوك",
        business_day_start_time=time(9, 0),
    )
    open_fiscal_year(organization=organization, year=timezone.localdate().year)

    root = create_item_category(organization=organization, code="FOOD", name="أغذية")
    leaf = create_item_category(organization=organization, code="MEAT", name="لحوم", parent=root)
    kilogram = UnitOfMeasure.objects.get(code="KG")
    rice = create_item(
        organization=organization,
        code="RICE-272",
        name="رز",
        category=leaf,
        item_type="RAW_MATERIAL",
        base_unit=kilogram,
    )
    chicken = create_item(
        organization=organization,
        code="CHK",
        name="دجاج",
        category=leaf,
        item_type="RAW_MATERIAL",
        base_unit=kilogram,
    )
    main = create_warehouse(branch=branch, code="MAIN", name="الرئيسي")

    # No teardown, deliberately. `transaction=True` truncates every table
    # afterwards, and truncation is the ONLY way this data leaves: the ledger
    # triggers refuse a DELETE outright, so a tidy-up that tried to remove the
    # movements by hand would fail — which is exactly the guarantee under test.
    return {
        "organization": organization,
        "branch": branch,
        "warehouse": main,
        "rice": rice,
        "chicken": chicken,
    }


def _receipt(world: dict[str, Any], item_key: str, quantity: str, cost: str) -> MovementInput:
    return MovementInput(
        warehouse=world["warehouse"],
        item=world[item_key],
        movement_type=MovementType.RECEIPT,
        quantity=Decimal(quantity),
        unit_cost=Decimal(cost),
        effect_key=f"line:{item_key}",
    )


def _issue(world: dict[str, Any], item_key: str, quantity: str) -> MovementInput:
    return MovementInput(
        warehouse=world["warehouse"],
        item=world[item_key],
        movement_type=MovementType.ISSUE,
        quantity=Decimal(quantity),
        effect_key=f"line:{item_key}",
    )


def _in_thread(work: Any) -> Any:
    """
    Run `work` on its own database connection and close it afterwards.

    A thread that leaves its connection open holds locks past the end of the
    test and hangs the next one.
    """

    def runner() -> Any:
        try:
            return work()
        finally:
            connections.close_all()

    return runner


def _race(*jobs: Any) -> list[Any]:
    """Start every job at once and collect what each returned or raised."""
    barrier = threading.Barrier(len(jobs))

    def wrapped(job: Any) -> Any:
        def run() -> Any:
            barrier.wait(timeout=10)
            try:
                return job()
            except Exception as error:  # noqa: BLE001 - the outcome IS the result
                return error

        return _in_thread(run)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(wrapped(job)) for job in jobs]
        return [future.result(timeout=30) for future in futures]


class TestConcurrentIssues:
    def test_two_issues_of_eight_against_ten_cannot_both_succeed(
        self, world: dict[str, Any]
    ) -> None:
        """
        The overselling test. Both connections see 10 on hand; only one may
        take 8.
        """
        post_stock_entry(
            organization=world["organization"],
            effects=[_receipt(world, "rice", "10", "1000")],
            idempotency_key="seed",
        )

        def issue(key: str) -> Any:
            return post_stock_entry(
                organization=world["organization"],
                effects=[_issue(world, "rice", "8")],
                idempotency_key=key,
            )

        results = _race(lambda: issue("a"), lambda: issue("b"))

        succeeded = [r for r in results if isinstance(r, StockLedgerEntry)]
        refused = [r for r in results if isinstance(r, ValidationError)]
        assert len(succeeded) == 1
        assert len(refused) == 1
        assert refused[0].code == "insufficient_stock"

        balance = StockBalance.objects.get(warehouse=world["warehouse"], item=world["rice"])
        assert balance.quantity == Decimal("2.000")


class TestConcurrentFirstReceipt:
    def test_two_first_receipts_into_an_absent_key_produce_one_row(
        self, world: dict[str, Any]
    ) -> None:
        """
        `SELECT ... FOR UPDATE` cannot lock a row that does not exist, so
        without the advisory lock both connections would find no balance and
        both insert one. The unique constraint would then turn a legitimate
        second receipt into an error.
        """

        def receipt(key: str, quantity: str, cost: str) -> Any:
            return post_stock_entry(
                organization=world["organization"],
                effects=[_receipt(world, "rice", quantity, cost)],
                idempotency_key=key,
            )

        results = _race(
            lambda: receipt("a", "10", "1000"),
            lambda: receipt("b", "10", "2000"),
        )
        assert all(isinstance(r, StockLedgerEntry) for r in results), results

        balances = StockBalance.objects.filter(warehouse=world["warehouse"], item=world["rice"])
        assert balances.count() == 1
        balance = balances.get()
        assert balance.quantity == Decimal("20.000")
        assert balance.value == Decimal("30000.000")
        assert balance.average_cost == Decimal("1500.000000")


class TestDeterministicLockOrder:
    def test_two_multi_item_events_in_opposite_order_do_not_deadlock(
        self, world: dict[str, Any]
    ) -> None:
        """
        One caller lists rice then chicken; the other lists chicken then rice.
        Locks are taken in canonical key order regardless, so there is no cycle
        to deadlock on.
        """

        def forwards() -> Any:
            return post_stock_entry(
                organization=world["organization"],
                effects=[
                    _receipt(world, "rice", "1", "100"),
                    _receipt(world, "chicken", "1", "200"),
                ],
                idempotency_key="forwards",
            )

        def backwards() -> Any:
            return post_stock_entry(
                organization=world["organization"],
                effects=[
                    _receipt(world, "chicken", "1", "200"),
                    _receipt(world, "rice", "1", "100"),
                ],
                idempotency_key="backwards",
            )

        results = _race(forwards, backwards)
        assert all(isinstance(r, StockLedgerEntry) for r in results), results
        assert StockMovement.objects.count() == 4


class TestConcurrentRetry:
    def test_two_in_flight_identical_retries_post_once(self, world: dict[str, Any]) -> None:
        """
        A client that retries before the first response arrives must not post
        twice. One of the two may lose the race on the unique key, which is a
        correct refusal — what must never happen is two postings.
        """

        def post() -> Any:
            return post_stock_entry(
                organization=world["organization"],
                effects=[_receipt(world, "rice", "5", "1000")],
                idempotency_key="the-same-key",
            )

        results = _race(post, post)
        entries = [r for r in results if isinstance(r, StockLedgerEntry)]
        assert entries, results
        assert StockLedgerEntry.objects.count() == 1
        assert StockMovement.objects.count() == 1
        assert StockBalance.objects.get(
            warehouse=world["warehouse"], item=world["rice"]
        ).quantity == Decimal("5.000")


class TestTheDatabaseHoldsTheLine:
    def test_a_duplicate_null_lot_balance_is_refused_at_commit(self, world: dict[str, Any]) -> None:
        """
        Written raw, because nothing in the application would try. The
        constraint is what protects the ledger from a repair script.
        """
        post_stock_entry(
            organization=world["organization"],
            effects=[_receipt(world, "rice", "1", "1")],
            idempotency_key="seed",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            StockBalance.objects.create(
                organization=world["organization"],
                branch=world["branch"],
                warehouse=world["warehouse"],
                item=world["rice"],
                lot=None,
            )

    def test_a_movement_update_is_refused_at_the_database(self, world: dict[str, Any]) -> None:
        post_stock_entry(
            organization=world["organization"],
            effects=[_receipt(world, "rice", "1", "1")],
            idempotency_key="seed",
        )
        movement = StockMovement.objects.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            StockMovement.objects.filter(pk=movement.pk).update(base_quantity=Decimal("999"))

        movement.refresh_from_db()
        assert movement.base_quantity == Decimal("1.000")

    def test_a_balance_whose_owner_disagrees_with_its_warehouse_is_refused(
        self, world: dict[str, Any]
    ) -> None:
        """
        Organization and branch are denormalised for tenancy filtering. A row
        whose denormalised owner disagreed with its warehouse would be visible
        to one filter and invisible to another.
        """
        stranger = create_organization(code="RIVAL", name="منافس")
        with pytest.raises(IntegrityError), transaction.atomic():
            StockBalance.objects.create(
                organization=stranger,
                branch=world["branch"],
                warehouse=world["warehouse"],
                item=world["rice"],
                lot=None,
            )
