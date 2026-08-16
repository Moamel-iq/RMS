"""
Counts, waste and adjustments racing each other at real COMMIT (Task 1.6 §AA).

Every test runs its work in real threads against a real database, so the locks
are the actual PostgreSQL locks and a deadlock is a real deadlock rather than a
story about one. `transaction=True` is what makes that possible: the usual test
transaction would keep both threads inside one connection and prove nothing.

Twelve races, and each protects a different figure:

   1. count start against an issue          either the issue is in the snapshot
                                            or it is refused — never neither
   2. count start against a transfer receipt no arrival lands outside the snapshot
   3. two counts, one warehouse             exactly one starts
   4. approval against another posting      the freeze holds until it is released
   5. cancellation against a posting        nothing slips between unfreeze and commit
   6. approval against a mapping mutation   no mixed mapping and no deadlock
   7. two adjustments, one stock key        deterministic, serialised
   8. a manual loss against an issue        no negative stock either way round
   9. an identical waste retry              one posting only
  10. an identical approval retry           one adjustment and one journal only
  11. opposite-order multi-item postings    no deadlock: keys are canonical
  12. period close against count start      exactly one of the two wins
"""

from __future__ import annotations

import datetime
import threading
import time
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from datetime import time as clock
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connections, transaction
from django.utils import timezone

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_ADJUSTMENT,
    INVENTORY_CONSUMPTION,
    INVENTORY_CONTROL,
    INVENTORY_COUNT_VARIANCE,
    INVENTORY_IN_TRANSIT,
    INVENTORY_WASTE_EXPENSE,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
)
from apps.accounting.services import (
    close_account_mapping,
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
    resolve_period,
    soft_close_period,
)
from apps.inventory.adjustments import AdjustmentLineInput
from apps.inventory.commands import (
    add_adjustment_line,
    add_document_line,
    add_transfer_line,
    approve_stock_count,
    cancel_stock_count,
    create_adjustment,
    create_document,
    create_reason_code,
    create_stock_count,
    create_transfer,
    create_transfer_receipt,
    dispatch_transfer,
    post_adjustment,
    post_document,
    post_transfer_receipt,
    record_stock_counts,
    replace_transfer_receipt_lines,
    start_stock_count,
    submit_stock_count,
)
from apps.inventory.counts import CountEntry
from apps.inventory.models import (
    InventoryDocumentType,
    MovementType,
    ReasonCodeApplication,
    StockBalance,
    StockCount,
    StockCountLine,
    StockCountStatus,
    StockMovement,
    Warehouse,
)
from apps.inventory.operations import DocumentLineInput
from apps.inventory.transfers import ReceiptLineInput, TransferLineInput
from apps.organizations.models import Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_organization_access,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db(transaction=True)

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
ZERO = Decimal("0")

#: How long a racing thread holds its transaction open after doing its work,
#: so the other thread is certainly waiting on a lock rather than merely slow.
LINGER = 0.6


@pytest.fixture
def world(django_db_setup: Any, django_db_blocker: Any) -> dict[str, Any]:
    """Two branches, mapped accounts, stocked shelves, and two actors."""
    from apps.inventory.services import create_item, create_item_category, create_warehouse

    call_command("seed_units", verbosity=0)
    organization = create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")
    first = create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=clock(9, 0),
    )
    second = create_branch(
        organization=organization,
        code="KARRADA",
        name_ar="الكرادة",
        name_en="Karrada",
        business_day_start_time=clock(9, 0),
    )
    year = timezone.localdate().year
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=year)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)

    effective = datetime.date(year, 1, 1)
    for role_code, account_code in {
        INVENTORY_CONTROL: "1-03-01-001",
        INVENTORY_IN_TRANSIT: "1-03-02-001",
        GOODS_RECEIVED_NOT_INVOICED: "2-01-02-001",
        INVENTORY_CONSUMPTION: "5-01-02-001",
        INVENTORY_WASTE_EXPENSE: "6-02-01-002",
        INVENTORY_COUNT_VARIANCE: "7-09-02-001",
        INVENTORY_ADJUSTMENT: "7-09-03-001",
    }.items():
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=role_code),
            account=Account.objects.get(organization=organization, code=account_code),
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
        code="CHICK-1",
        name_ar="دجاج",
        category=leaf,
        item_type="RAW_MATERIAL",
        base_unit=kilogram,
    )
    main = create_warehouse(branch=first, code="MAIN", name_ar="الرئيسي")
    kitchen = create_warehouse(branch=first, code="KITCHEN", name_ar="المطبخ")

    conductor = User.objects.create_user(username="counter", password="pw-not-real-1234")
    grant_organization_access(user=conductor, organization=organization, role=Role.OWNER)
    approver = User.objects.create_user(username="approver", password="pw-not-real-1234")
    grant_organization_access(user=approver, organization=organization, role=Role.OWNER)

    world: dict[str, Any] = {
        "organization": organization,
        "first": first,
        "second": second,
        "main": main,
        "kitchen": kitchen,
        "rice": rice,
        "chicken": chicken,
        "actor": User.objects.get(pk=conductor.pk),
        "approver": User.objects.get(pk=approver.pk),
        "center": CostCenter.objects.get(organization=organization, code="WAREHOUSE"),
    }
    world["spoil"] = create_reason_code(
        actor=world["actor"],
        organization=organization,
        code="SPOIL",
        name_ar="تلف",
        applies_to=ReasonCodeApplication.WASTE,
    )
    world["fix"] = create_reason_code(
        actor=world["actor"],
        organization=organization,
        code="FIX",
        name_ar="تصحيح",
        applies_to=ReasonCodeApplication.MANUAL_ADJUSTMENT,
    )
    _receive(world, main, rice, "1000", "1000")
    _receive(world, main, chicken, "500", "2000")
    return world


def _receive(
    world: dict[str, Any], warehouse: Warehouse, item: Any, quantity: str, cost: str
) -> None:
    actor = world["actor"]
    document = create_document(
        actor=actor,
        organization=world["organization"],
        branch=warehouse.branch,
        warehouse=warehouse,
        document_type=InventoryDocumentType.RECEIPT,
        effective_at=timezone.now(),
        evidence_reference="DN-SEED",
    )
    add_document_line(
        actor=actor,
        document=document,
        line=DocumentLineInput(item=item, base_quantity=Decimal(quantity), unit_cost=Decimal(cost)),
    )
    post_document(actor=actor, document=document)


def _issue(world: dict[str, Any], item: Any, quantity: str) -> Any:
    actor = world["actor"]
    document = create_document(
        actor=actor,
        organization=world["organization"],
        branch=world["first"],
        warehouse=world["main"],
        document_type=InventoryDocumentType.ISSUE,
        effective_at=timezone.now(),
        evidence_reference="REQ",
        cost_center=world["center"],
    )
    add_document_line(
        actor=actor,
        document=document,
        line=DocumentLineInput(item=item, base_quantity=Decimal(quantity)),
    )
    return post_document(actor=actor, document=document)


def _waste_draft(world: dict[str, Any], quantity: str) -> Any:
    actor = world["actor"]
    document = create_document(
        actor=actor,
        organization=world["organization"],
        branch=world["first"],
        warehouse=world["main"],
        document_type=InventoryDocumentType.WASTE,
        effective_at=timezone.now(),
        evidence_reference="W-1",
        cost_center=world["center"],
    )
    add_document_line(
        actor=actor,
        document=document,
        line=DocumentLineInput(
            item=world["rice"], base_quantity=Decimal(quantity), reason_code=world["spoil"]
        ),
    )
    return document


def _adjustment_draft(world: dict[str, Any], item: Any, kind: str, quantity: str) -> Any:
    actor = world["actor"]
    document = create_adjustment(
        actor=actor,
        organization=world["organization"],
        branch=world["first"],
        warehouse=world["main"],
        effective_at=timezone.now(),
        evidence_reference="MEMO",
        reason="تصحيح",
        cost_center=world["center"],
    )
    add_adjustment_line(
        actor=actor,
        document=document,
        line=AdjustmentLineInput(
            kind=kind,
            item=item,
            reason_code=world["fix"],
            base_quantity=Decimal(quantity),
            unit_cost=Decimal("1000") if kind == "QUANTITY_GAIN" else None,
        ),
    )
    return document


def _new_count(world: dict[str, Any], warehouse: Warehouse | None = None) -> StockCount:
    return create_stock_count(
        actor=world["actor"],
        organization=world["organization"],
        branch=(warehouse or world["main"]).branch,
        warehouse=warehouse or world["main"],
        reference="SHEET",
        reason="جرد",
        cost_center=world["center"],
    )


def _submitted_count(world: dict[str, Any], counted: str = "990") -> StockCount:
    """A count started, counted and submitted, ready for approval."""
    count = start_stock_count(actor=world["actor"], count=_new_count(world))
    entries = [
        CountEntry(
            line=line,
            base_quantity=Decimal(counted)
            if line.item_id == world["rice"].pk
            else line.book_quantity,
        )
        for line in StockCountLine.objects.filter(count=count)
    ]
    record_stock_counts(actor=world["actor"], count=count, entries=entries)
    return submit_stock_count(actor=world["actor"], count=count)


def _in_thread(work: Any) -> Any:
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
        return [future.result(timeout=120) for future in futures]


def _hold(work: Any, *, linger: float = LINGER) -> Any:
    """Do the work, then hold the transaction open so the race is real."""
    with transaction.atomic():
        result = work()
        time.sleep(linger)
    return result


def _balance(warehouse: Warehouse, item: Any) -> StockBalance:
    balance = StockBalance.objects.filter(warehouse=warehouse, item=item, lot=None).first()
    assert balance is not None
    return balance


def _codes(outcome: Any) -> str:
    return getattr(outcome, "code", "") or ""


# ---------------------------------------------------------------------------
# 1–3. Starting a count against live traffic
# ---------------------------------------------------------------------------


class TestCountStartRaces:
    def test_an_issue_either_lands_in_the_snapshot_or_is_refused(
        self, world: dict[str, Any]
    ) -> None:
        """
        The one outcome that must not happen is an issue that commits *and* is
        absent from the snapshot: the count would then be reconciled against a
        book position that never existed.
        """
        count = _new_count(world)
        outcomes = _race(
            lambda: _hold(lambda: start_stock_count(actor=world["actor"], count=count)),
            lambda: _hold(lambda: _issue(world, world["rice"], "10")),
        )
        started, issued = outcomes

        line = StockCountLine.objects.filter(count=count, item=world["rice"]).first()
        if isinstance(issued, Exception):
            # The freeze won. Nothing left the shelf, so the snapshot is the
            # full 1000 and the issue was refused by name.
            assert _codes(issued) == "warehouse_frozen"
            assert line is not None
            assert line.book_quantity == Decimal("1000.000")
            assert _balance(world["main"], world["rice"]).quantity == Decimal("1000.000")
        else:
            # The issue won. It committed before the snapshot, so the snapshot
            # includes it — there is no third possibility.
            assert not isinstance(started, Exception)
            assert line is not None
            assert line.book_quantity == Decimal("990.000")
            assert _balance(world["main"], world["rice"]).quantity == Decimal("990.000")

    def test_no_transfer_receipt_lands_outside_the_snapshot(self, world: dict[str, Any]) -> None:
        """A receipt arriving into the counted warehouse, from the other branch."""
        actor = world["actor"]
        _receive(world, world["kitchen"], world["rice"], "200", "1000")
        transfer = create_transfer(
            actor=actor,
            organization=world["organization"],
            source_warehouse=world["kitchen"],
            destination_warehouse=world["main"],
            effective_at=timezone.now(),
            evidence_reference="TN",
        )
        add_transfer_line(
            actor=actor,
            transfer=transfer,
            line=TransferLineInput(item=world["rice"], base_quantity=Decimal("50")),
        )
        dispatched = dispatch_transfer(actor=actor, transfer=transfer)
        receipt = create_transfer_receipt(
            actor=actor,
            transfer=dispatched,
            effective_at=timezone.now(),
            evidence_reference="ARR",
        )
        replace_transfer_receipt_lines(
            actor=actor,
            receipt=receipt,
            lines=[
                ReceiptLineInput(transfer_line=dispatched.lines.get(), base_quantity=Decimal("50"))
            ],
        )

        count = _new_count(world)
        started, arrived = _race(
            lambda: _hold(lambda: start_stock_count(actor=actor, count=count)),
            lambda: _hold(lambda: post_transfer_receipt(actor=actor, receipt=receipt)),
        )

        line = StockCountLine.objects.filter(count=count, item=world["rice"]).first()
        assert line is not None
        standing = _balance(world["main"], world["rice"]).quantity
        if isinstance(arrived, Exception):
            assert _codes(arrived) == "warehouse_frozen"
            assert line.book_quantity == Decimal("1000.000")
        else:
            assert not isinstance(started, Exception)
            assert line.book_quantity == Decimal("1050.000")
        # Either way the snapshot equals what the ledger holds. That equality
        # is what `count_snapshot_mismatch` later depends on.
        assert line.book_quantity == standing

    def test_two_counts_race_and_exactly_one_starts(self, world: dict[str, Any]) -> None:
        first = _new_count(world)
        second = _new_count(world)
        outcomes = _race(
            lambda: _hold(lambda: start_stock_count(actor=world["actor"], count=first)),
            lambda: _hold(lambda: start_stock_count(actor=world["actor"], count=second)),
        )
        started = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        assert len(started) == 1
        assert (
            StockCount.objects.filter(
                warehouse=world["main"], status=StockCountStatus.IN_PROGRESS
            ).count()
            == 1
        )
        world["main"].refresh_from_db()
        assert world["main"].frozen_by_count_id == started[0].pk


# ---------------------------------------------------------------------------
# 4–6. Approval and cancellation against live traffic
# ---------------------------------------------------------------------------


class TestApprovalRaces:
    def test_a_frozen_warehouse_refuses_a_posting_racing_approval(
        self, world: dict[str, Any]
    ) -> None:
        count = _submitted_count(world)
        draft = _waste_draft(world, "5")
        approved, wasted = _race(
            lambda: _hold(lambda: approve_stock_count(actor=world["approver"], count=count)),
            lambda: _hold(lambda: post_document(actor=world["actor"], document=draft)),
        )
        assert not isinstance(approved, Exception)
        # The waste either waited for the freeze to lift and then posted, or hit
        # the freeze and was refused. Both are correct; a *third* outcome —
        # posting inside the freeze — is what this test exists to exclude.
        if isinstance(wasted, Exception):
            assert _codes(wasted) == "warehouse_frozen"
            assert _balance(world["main"], world["rice"]).quantity == Decimal("990.000")
        else:
            assert _balance(world["main"], world["rice"]).quantity == Decimal("985.000")

    def test_a_posting_cannot_slip_between_unfreeze_and_cancellation(
        self, world: dict[str, Any]
    ) -> None:
        count = start_stock_count(actor=world["actor"], count=_new_count(world))
        draft = _waste_draft(world, "5")
        cancelled, wasted = _race(
            lambda: _hold(
                lambda: cancel_stock_count(actor=world["actor"], count=count, reason="توقف")
            ),
            lambda: _hold(lambda: post_document(actor=world["actor"], document=draft)),
        )
        assert not isinstance(cancelled, Exception)
        count.refresh_from_db()
        world["main"].refresh_from_db()
        assert count.status == StockCountStatus.CANCELLED
        assert world["main"].frozen_by_count_id is None
        if isinstance(wasted, Exception):
            assert _codes(wasted) == "warehouse_frozen"
        else:
            assert _balance(world["main"], world["rice"]).quantity == Decimal("995.000")

    def test_approval_and_a_mapping_mutation_neither_mix_nor_deadlock(
        self, world: dict[str, Any]
    ) -> None:
        count = _submitted_count(world)
        mapping = world["organization"].account_mappings.get(
            account_role__code=INVENTORY_COUNT_VARIANCE
        )
        approved, closed = _race(
            lambda: _hold(lambda: approve_stock_count(actor=world["approver"], count=count)),
            lambda: _hold(
                lambda: close_account_mapping(
                    mapping=mapping,
                    effective_to=timezone.localdate() + datetime.timedelta(days=30),
                    reason="إعادة تصنيف",
                )
            ),
        )
        # Neither may deadlock; PostgreSQL would have killed one with a
        # DeadlockDetected rather than a domain error.
        for outcome in (approved, closed):
            assert "deadlock" not in str(outcome).lower()
        assert not isinstance(approved, Exception)
        count.refresh_from_db()
        assert count.status == StockCountStatus.POSTED
        # One journal, on one variance account — never a blend of the two.
        journal = count.journal_entry
        assert journal is not None
        variance_lines = [
            line for line in journal.lines.all() if line.account.code.startswith("7-09-02")
        ]
        assert len({line.account_id for line in variance_lines}) == 1


# ---------------------------------------------------------------------------
# 7–8. Adjustments against each other and against an issue
# ---------------------------------------------------------------------------


class TestAdjustmentRaces:
    def test_two_adjustments_on_one_key_serialise_deterministically(
        self, world: dict[str, Any]
    ) -> None:
        first = _adjustment_draft(world, world["rice"], "QUANTITY_LOSS", "10")
        second = _adjustment_draft(world, world["rice"], "QUANTITY_LOSS", "20")
        outcomes = _race(
            lambda: _hold(lambda: post_adjustment(actor=world["actor"], document=first)),
            lambda: _hold(lambda: post_adjustment(actor=world["actor"], document=second)),
        )
        assert all(not isinstance(outcome, Exception) for outcome in outcomes)
        # 1000 - 10 - 20, whichever order they took the key in.
        assert _balance(world["main"], world["rice"]).quantity == Decimal("970.000")
        assert _balance(world["main"], world["rice"]).value == Decimal("970000.000")

    def test_a_manual_loss_racing_an_issue_never_goes_negative(self, world: dict[str, Any]) -> None:
        draft = _adjustment_draft(world, world["rice"], "QUANTITY_LOSS", "600")
        outcomes = _race(
            lambda: _hold(lambda: post_adjustment(actor=world["actor"], document=draft)),
            lambda: _hold(lambda: _issue(world, world["rice"], "600")),
        )
        survivors = [o for o in outcomes if not isinstance(o, Exception)]
        assert len(survivors) == 1, "1000 kg cannot satisfy two claims of 600"
        refused = [o for o in outcomes if isinstance(o, Exception)]
        assert _codes(refused[0]) == "insufficient_stock"
        assert _balance(world["main"], world["rice"]).quantity == Decimal("400.000")


# ---------------------------------------------------------------------------
# 9–11. Retries and canonical ordering
# ---------------------------------------------------------------------------


class TestRetriesAndOrdering:
    def test_two_identical_waste_posts_produce_one_posting(self, world: dict[str, Any]) -> None:
        draft = _waste_draft(world, "10")
        outcomes = _race(
            lambda: post_document(actor=world["actor"], document=draft),
            lambda: post_document(actor=world["actor"], document=draft),
        )
        posted = [o for o in outcomes if not isinstance(o, Exception)]
        assert len(posted) == 1
        assert StockMovement.objects.filter(movement_type=MovementType.WASTE).count() == 1
        assert _balance(world["main"], world["rice"]).quantity == Decimal("990.000")

    def test_two_identical_approvals_produce_one_adjustment_and_one_journal(
        self, world: dict[str, Any]
    ) -> None:
        count = _submitted_count(world)
        before = JournalEntry.objects.count()
        outcomes = _race(
            lambda: approve_stock_count(actor=world["approver"], count=count),
            lambda: approve_stock_count(actor=world["approver"], count=count),
        )
        approved = [o for o in outcomes if not isinstance(o, Exception)]
        assert len(approved) == 1
        assert (
            StockMovement.objects.filter(
                movement_type__in=[MovementType.COUNT_GAIN, MovementType.COUNT_LOSS]
            ).count()
            == 1
        )
        assert JournalEntry.objects.count() == before + 1

    def test_opposite_order_multi_item_postings_do_not_deadlock(
        self, world: dict[str, Any]
    ) -> None:
        """
        Two documents naming the same two items in opposite order. Sorting the
        keys canonically inside the kernel is what makes this safe; caller
        order would put the two transactions in a cycle.
        """
        actor = world["actor"]

        def adjustment(first_item: Any, second_item: Any) -> Any:
            document = create_adjustment(
                actor=actor,
                organization=world["organization"],
                branch=world["first"],
                warehouse=world["main"],
                effective_at=timezone.now(),
                evidence_reference="MEMO",
                reason="تصحيح",
                cost_center=world["center"],
            )
            for item in (first_item, second_item):
                add_adjustment_line(
                    actor=actor,
                    document=document,
                    line=AdjustmentLineInput(
                        kind="QUANTITY_LOSS",
                        item=item,
                        reason_code=world["fix"],
                        base_quantity=Decimal("5"),
                    ),
                )
            return document

        forwards = adjustment(world["rice"], world["chicken"])
        backwards = adjustment(world["chicken"], world["rice"])
        outcomes = _race(
            lambda: _hold(lambda: post_adjustment(actor=actor, document=forwards)),
            lambda: _hold(lambda: post_adjustment(actor=actor, document=backwards)),
        )
        for outcome in outcomes:
            assert "deadlock" not in str(outcome).lower()
        assert all(not isinstance(outcome, Exception) for outcome in outcomes)
        assert _balance(world["main"], world["rice"]).quantity == Decimal("990.000")
        assert _balance(world["main"], world["chicken"]).quantity == Decimal("490.000")


# ---------------------------------------------------------------------------
# 12. Period close against a count start
# ---------------------------------------------------------------------------


class TestPeriodCloseRace:
    def test_a_close_and_a_count_start_cannot_both_win(self, world: dict[str, Any]) -> None:
        """
        Either the close commits first and the count refuses to start into a
        shut period, or the count starts first and the close is refused by the
        guard. What must never happen is both: a frozen warehouse in a closed
        period can neither post nor be reopened without accounting authority.
        """
        count = _new_count(world)
        period = resolve_period(
            organization=world["organization"], accounting_date=timezone.localdate()
        )
        started, closed = _race(
            lambda: _hold(lambda: start_stock_count(actor=world["actor"], count=count)),
            lambda: _hold(lambda: soft_close_period(period=period, reason="إقفال")),
        )
        both_won = not isinstance(started, Exception) and not isinstance(closed, Exception)
        assert not both_won, "a count must not be left active inside a soft-closed period"

        count.refresh_from_db()
        period.refresh_from_db()
        if isinstance(started, Exception):
            assert _codes(started) in {
                "period_soft_closed",
                "period_closed",
                "period_not_open",
            }
            assert count.status == StockCountStatus.DRAFT
        else:
            assert _codes(closed) == "active_inventory_count"
            assert count.status == StockCountStatus.IN_PROGRESS
