"""
Opening documents under real concurrency, at real COMMIT boundaries
(Task 1.3 §M, §V 42–43).

Same harness as `test_ledger_concurrency`: `transaction=True`, one database
connection per thread, and a barrier so the racers genuinely race. The combined
posting takes document row → stock keys → posted-order counter → document
number → journal number, in that order on every path — these tests are what
holds that claim to account.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connections
from django.utils import timezone

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_OPENING_EQUITY,
    Account,
    AccountRole,
    JournalEntry,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.commands import (
    add_opening_line,
    create_opening,
    post_opening,
    submit_opening,
)
from apps.inventory.models import (
    OpeningStockDocument,
    OpeningStockStatus,
    StockLedgerEntry,
    StockMovement,
)
from apps.inventory.opening import OpeningLineInput
from apps.inventory.services import create_item, create_item_category, create_warehouse
from apps.organizations.models import Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(django_db_setup: Any, django_db_blocker: Any) -> dict[str, Any]:
    """A committed organization with accounting, mappings, items, and users."""
    call_command("seed_units", verbosity=0)
    organization = create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=timezone.localdate().year)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)

    effective = date(timezone.localdate().year, 1, 1)
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_CONTROL),
        account=Account.objects.get(organization=organization, code="1-03-01-001"),
        effective_from=effective,
    )
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_OPENING_EQUITY),
        account=Account.objects.get(organization=organization, code="3-02-01-001"),
        effective_from=effective,
    )

    root = create_item_category(organization=organization, code="FOOD", name_ar="أغذية")
    leaf = create_item_category(organization=organization, code="MEAT", name_ar="لحوم", parent=root)
    kilogram = UnitOfMeasure.objects.get(code="KG")
    rice = create_item(
        organization=organization,
        code="RICE-272",
        name_ar="رز",
        category=leaf,
        item_type="RAW_MATERIAL",
        base_unit=kilogram,
    )
    chicken = create_item(
        organization=organization,
        code="CHK",
        name_ar="دجاج",
        category=leaf,
        item_type="RAW_MATERIAL",
        base_unit=kilogram,
    )
    main = create_warehouse(branch=branch, code="MAIN", name_ar="الرئيسي")
    cold = create_warehouse(branch=branch, code="COLD", name_ar="المبردات")

    preparer = User.objects.create_user(username="preparer", password="pw-not-real-1234")
    grant_branch_access(user=preparer, branch=branch, role=Role.MANAGER)
    approver = User.objects.create_user(username="approver", password="pw-not-real-1234")
    grant_organization_access(
        user=approver, organization=organization, role=Role.ACCOUNTING_MANAGER
    )

    # No teardown, deliberately: `transaction=True` truncates every table, and
    # truncation is the only exit — the triggers refuse deletes.
    return {
        "organization": organization,
        "branch": branch,
        "main": main,
        "cold": cold,
        "rice": rice,
        "chicken": chicken,
        "preparer": User.objects.get(pk=preparer.pk),
        "approver": User.objects.get(pk=approver.pk),
    }


def _in_thread(work: Any) -> Any:
    def runner() -> Any:
        try:
            return work()
        finally:
            connections.close_all()

    return runner


def _race(*jobs: Any) -> list[Any]:
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
        return [future.result(timeout=60) for future in futures]


def _submitted_document(
    world: dict[str, Any], *, evidence: str, lines: list[tuple[str, str, str, str]]
) -> OpeningStockDocument:
    """A SUBMITTED document with (warehouse_key, item_key, qty, cost) lines."""
    document = create_opening(
        actor=world["preparer"],
        organization=world["organization"],
        branch=world["branch"],
        cutoff_at=timezone.now(),
        evidence_reference=evidence,
    )
    for warehouse_key, item_key, quantity, cost in lines:
        add_opening_line(
            actor=world["preparer"],
            document=document,
            line=OpeningLineInput(
                warehouse=world[warehouse_key],
                item=world[item_key],
                base_quantity=Decimal(quantity),
                unit_cost=Decimal(cost),
            ),
        )
    return submit_opening(actor=world["preparer"], document=document)


class TestConcurrentOpenings:
    def test_two_openings_in_one_organization_post_concurrently_without_deadlock(
        self, world: dict[str, Any]
    ) -> None:
        """
        §V 43. Different warehouses, opposite line order — the canonical key
        sort and the fixed counter order leave no cycle to deadlock on, and
        the numbers come out gapless.
        """
        first = _submitted_document(
            world,
            evidence="A",
            lines=[("main", "rice", "10", "1000"), ("main", "chicken", "5", "4000")],
        )
        second = _submitted_document(
            world,
            evidence="B",
            lines=[("cold", "chicken", "3", "4000"), ("cold", "rice", "7", "1000")],
        )

        def post(document: OpeningStockDocument) -> Any:
            return post_opening(actor=world["approver"], document=document)

        results = _race(lambda: post(first), lambda: post(second))
        assert all(isinstance(r, OpeningStockDocument) for r in results), results

        numbers = sorted(document.document_number for document in results)
        year = timezone.localdate().year
        assert numbers == [f"OPN-{year}-000001", f"OPN-{year}-000002"]
        assert StockMovement.objects.count() == 4
        assert JournalEntry.objects.count() == 2

    def test_a_concurrent_duplicate_post_creates_one_economic_event(
        self, world: dict[str, Any]
    ) -> None:
        """
        §V 42. Two approvers double-click the same submitted document. The
        row lock serialises them; the loser finds it already posted; exactly
        one stock entry and one journal exist.
        """
        document = _submitted_document(world, evidence="C", lines=[("main", "rice", "10", "1000")])

        def post() -> Any:
            return post_opening(actor=world["approver"], document=document)

        results = _race(post, post)
        succeeded = [r for r in results if isinstance(r, OpeningStockDocument)]
        refused = [r for r in results if isinstance(r, ValidationError)]
        assert len(succeeded) == 1, results
        assert len(refused) == 1, results
        assert refused[0].code == "already_posted"

        assert StockLedgerEntry.objects.count() == 1
        assert JournalEntry.objects.count() == 1
        assert StockMovement.objects.count() == 1
        final = OpeningStockDocument.objects.get(pk=document.pk)
        assert final.status == OpeningStockStatus.POSTED
