"""
Mapping mutations racing with stock postings, at real COMMIT boundaries
(Task 1.4 §C, §S 5–8).

The Task 1.3 reclassification guard compares the account resolution before and
after a mapping change — but it can only see *committed* stock. A posting that
had already read the old mapping and not yet committed would be invisible to
it: the mutation would commit, the posting would commit, and standing stock
would sit in one account while every new posting used another, with nothing
left to detect it.

The fix is a lock, and these tests are what hold it to account. Postings take
the organization's mapping lock **shared**; mutations take it **exclusively**.
Concurrency between postings is unaffected; a mutation waits for the in-flight
ones and blocks the next.

Each racing thread deliberately lingers inside its transaction after doing its
work, so the window is wide and the outcome deterministic rather than a matter
of scheduling luck.
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
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connections, transaction
from django.utils import timezone

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONSUMPTION,
    INVENTORY_CONTROL,
    Account,
    AccountRole,
    CostCenter,
    OrganizationAccountMapping,
)
from apps.accounting.services import (
    close_account_mapping,
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.core.context import audit_context
from apps.core.locks import lock_account_mappings_shared
from apps.inventory.accounts import create_inventory_mapping, resolve_inventory_account
from apps.inventory.commands import (
    add_document_line,
    create_document,
    post_document,
    post_stock_movements,
)
from apps.inventory.ledger import MovementInput
from apps.inventory.models import (
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryMovementDocument,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
)
from apps.inventory.operations import DocumentLineInput
from apps.inventory.services import (
    create_item,
    create_item_category,
    create_warehouse,
)
from apps.organizations.models import Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db(transaction=True)

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")

#: How long a racing thread holds its transaction open after doing its work.
#: Long enough that the other thread is certainly waiting on the lock rather
#: than merely being slow.
LINGER = 0.6


@pytest.fixture
def world(django_db_setup: Any, django_db_blocker: Any) -> dict[str, Any]:
    """A committed organization with accounting, mappings, items, and a poster."""
    call_command("seed_units", verbosity=0)
    organization = create_organization(code="KM", name="خان مندي")
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name="البنوك",
        business_day_start_time=clock(9, 0),
    )
    year = timezone.localdate().year
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=year)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)

    effective = datetime.date(year, 1, 1)
    control = Account.objects.get(organization=organization, code="1-03-01-001")
    other_control = Account.objects.get(organization=organization, code="1-03-02-001")
    for code, account in (
        (INVENTORY_CONTROL, control),
        (
            GOODS_RECEIVED_NOT_INVOICED,
            Account.objects.get(organization=organization, code="2-01-02-001"),
        ),
        (INVENTORY_CONSUMPTION, Account.objects.get(organization=organization, code="5-01-02-001")),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=account,
            effective_from=effective,
        )

    root = create_item_category(organization=organization, code="FOOD", name="أغذية")
    leaf = create_item_category(organization=organization, code="MEAT", name="لحوم", parent=root)
    spare = create_item_category(organization=organization, code="DRY", name="جافة", parent=root)
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

    poster = User.objects.create_user(username="poster", password="pw-not-real-1234")
    grant_branch_access(user=poster, branch=branch, role=Role.MANAGER)

    # No teardown: `transaction=True` truncates, and the ledger triggers refuse
    # a DELETE anyway — which is the guarantee under test elsewhere.
    return {
        "organization": organization,
        "branch": branch,
        "warehouse": main,
        "rice": rice,
        "chicken": chicken,
        "leaf": leaf,
        "spare": spare,
        "control": control,
        "other_control": other_control,
        "poster": User.objects.get(pk=poster.pk),
    }


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
        return [future.result(timeout=90) for future in futures]


def _post_receipt(
    world: dict[str, Any],
    *,
    item_key: str = "rice",
    quantity: str = "10",
    linger: float = 0.0,
    holding: threading.Event | None = None,
) -> StockLedgerEntry:
    """
    Put stock in, then hold the transaction open.

    The lingering matters: the mapping lock is transaction-scoped, so sleeping
    inside this outer block keeps it held. `holding` is set once the lock is
    genuinely held, so a racing mutation can be released at exactly the moment
    that makes the test mean something — rather than whenever the scheduler
    happens to run it.
    """
    actor = world["poster"]
    organization = world["organization"]
    item = world[item_key]
    with transaction.atomic():
        # Resolved here rather than passed in, because *resolving it under the
        # shared lock* is the subject of every test in this file. The
        # un-invoiced receipt used to do exactly this before posting; it was
        # withdrawn, so the call it made is written out.
        lock_account_mappings_shared(organization.pk)
        control = resolve_inventory_account(
            organization=organization,
            role=INVENTORY_CONTROL,
            item=item,
            on_date=timezone.localdate(),
        )
        entry = post_stock_movements(
            actor=actor,
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=world["warehouse"],
                    item=item,
                    movement_type=MovementType.RECEIPT,
                    quantity=Decimal(quantity),
                    effect_key=f"map-{item.code}-{quantity}",
                    unit_cost=Decimal("1000"),
                    control_account=control.account,
                )
            ],
            idempotency_key=f"map-{item.code}-{quantity}",
        )
        if holding is not None:
            holding.set()
        if linger:
            time.sleep(linger)
        return entry


class TestMappingChangeCannotRaceWithPosting:
    def test_a_default_mapping_change_waits_and_is_then_refused(
        self, world: dict[str, Any]
    ) -> None:
        """
        §S 5. The posting holds the shared lock while it commits; the mutation
        blocks, and by the time it runs the stock is committed and visible —
        so the reclassification guard refuses it.

        Without the lock the mutation would see no stock, allow itself, and
        leave the committed movement pointing at an account the mapping no
        longer names. The `holding` event releases the mutation only once the
        posting genuinely holds the lock, so this tests the lock rather than
        the scheduler.
        """
        mapping = OrganizationAccountMapping.objects.get(
            organization=world["organization"], account_role__code=INVENTORY_CONTROL
        )
        holding = threading.Event()

        def mutate() -> Any:
            assert holding.wait(timeout=30), "the posting never took the lock"
            with audit_context(actor=world["poster"]):
                # Yesterday, not today: an effective range is inclusive, so
                # closing it *today* still covers today and changes nothing
                # about the current resolution. Ending it yesterday is what
                # actually re-homes the role from now on.
                return close_account_mapping(
                    mapping=mapping,
                    effective_to=timezone.localdate() - datetime.timedelta(days=1),
                    reason="re-home",
                )

        results = _race(lambda: _post_receipt(world, linger=LINGER, holding=holding), mutate)
        posted = [r for r in results if isinstance(r, StockLedgerEntry)]
        refused = [r for r in results if isinstance(r, ValidationError)]

        assert len(posted) == 1, results
        assert len(refused) == 1, results
        assert refused[0].code == "inventory_account_reclassification_required"

        # The stock and its ledger agree, and the mapping still names the
        # account the stock is actually in.
        balance = StockBalance.objects.get()
        assert balance.control_account == world["control"]
        mapping.refresh_from_db()
        assert mapping.effective_to is None

    def test_a_category_move_waits_and_is_then_refused(self, world: dict[str, Any]) -> None:
        """§S 6. The same race through the other door: re-filing the item."""
        from apps.inventory.services import update_item

        create_inventory_mapping(
            organization=world["organization"],
            role=INVENTORY_CONTROL,
            account=world["other_control"],
            category=world["spare"],
            effective_from=datetime.date(timezone.localdate().year, 1, 1),
        )
        rice = world["rice"]
        holding = threading.Event()

        def move() -> Any:
            assert holding.wait(timeout=30), "the posting never took the lock"
            with audit_context(actor=world["poster"]):
                return update_item(
                    item=rice,
                    name=rice.name,
                    category=world["spare"],
                    item_type=rice.item_type,
                )

        results = _race(lambda: _post_receipt(world, linger=LINGER, holding=holding), move)
        posted = [r for r in results if isinstance(r, StockLedgerEntry)]
        refused = [r for r in results if isinstance(r, ValidationError)]

        assert len(posted) == 1, results
        assert len(refused) == 1, results
        assert refused[0].code == "inventory_account_reclassification_required"

        rice.refresh_from_db()
        assert rice.category_id == world["leaf"].pk
        assert StockBalance.objects.get().control_account == world["control"]

    def test_a_posting_arriving_after_a_change_sees_the_committed_state(
        self, world: dict[str, Any]
    ) -> None:
        """
        The mirror image, forced the other way. The mutation goes first while
        no stock exists — legitimate, so it is allowed — and the posting queued
        behind it sees the committed result rather than the stale one.

        With the test above, both orderings are now covered deterministically,
        and neither can produce a movement sitting in an account the mapping
        does not name.
        """
        holding = threading.Event()
        effective = datetime.date(timezone.localdate().year, 1, 1)

        def mutate() -> Any:
            """
            An open-ended item override, so what it changes does not depend on
            which business day the posting lands on — the branch cutoff can
            legitimately put "now" on yesterday's operating day.
            """
            with transaction.atomic():
                with audit_context(actor=world["poster"]):
                    created = create_inventory_mapping(
                        organization=world["organization"],
                        role=INVENTORY_CONTROL,
                        account=world["other_control"],
                        item=world["rice"],
                        effective_from=effective,
                    )
                holding.set()
                time.sleep(LINGER)
                return created

        def post() -> Any:
            assert holding.wait(timeout=30), "the mutation never took the lock"
            return _post_receipt(world)

        results = _race(mutate, post)
        posted = [r for r in results if isinstance(r, StockLedgerEntry)]
        assert len(posted) == 1, results

        # The posting queued behind the change and resolved the account the
        # mutation had just committed — not the one it would have read a
        # moment earlier.
        assert StockBalance.objects.get().control_account == world["other_control"]
        assert StockMovement.objects.get().control_account == world["other_control"]

    def test_a_mapping_change_before_any_stock_still_succeeds(self, world: dict[str, Any]) -> None:
        """
        The lock must not turn a legitimate mutation into a refusal. With no
        standing stock there is nothing to strand, so the change goes through.
        """
        mapping = OrganizationAccountMapping.objects.get(
            organization=world["organization"], account_role__code=INVENTORY_CONTROL
        )
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        with audit_context(actor=world["poster"]):
            closed = close_account_mapping(
                mapping=mapping, effective_to=yesterday, reason="no stock yet"
            )
        assert closed.effective_to == yesterday


class TestSharedLocksStillAllowConcurrency:
    def test_two_postings_overlap_rather_than_serialising(self, world: dict[str, Any]) -> None:
        """
        §S 7. The mapping lock is shared, so ordinary traffic is unaffected.
        Proven by the clock: two lingering postings must overlap in time, not
        queue behind one another.
        """
        spans: list[tuple[float, float]] = []
        lock = threading.Lock()

        def post(item_key: str) -> Any:
            started = time.monotonic()
            result = _post_receipt(world, item_key=item_key, linger=LINGER)
            with lock:
                spans.append((started, time.monotonic()))
            return result

        results = _race(lambda: post("rice"), lambda: post("chicken"))
        assert all(isinstance(r, StockLedgerEntry) for r in results), results

        (start_a, end_a), (start_b, end_b) = spans
        assert min(end_a, end_b) > max(start_a, start_b), (
            "the two postings serialised; the mapping lock is not shared"
        )
        assert StockMovement.objects.count() == 2


class TestTheGlobalLockOrderDoesNotDeadlock:
    def test_postings_and_a_mutation_started_together_all_finish(
        self, world: dict[str, Any]
    ) -> None:
        """
        §S 8. Two postings on different keys plus a mapping mutation, all
        released at once. Under one consistent order — document, mappings,
        stock keys, counters — there is no cycle to deadlock on, so every
        thread reaches a definite answer.
        """
        mapping = OrganizationAccountMapping.objects.get(
            organization=world["organization"],
            account_role__code=GOODS_RECEIVED_NOT_INVOICED,
        )

        def mutate() -> Any:
            with audit_context(actor=world["poster"]):
                # A non-control role: no standing value depends on it, so this
                # is a mutation that legitimately succeeds under contention.
                return close_account_mapping(
                    mapping=mapping,
                    effective_to=timezone.localdate() + datetime.timedelta(days=365),
                    reason="future close",
                )

        results = _race(
            lambda: _post_receipt(world, item_key="rice", linger=0.3),
            lambda: _post_receipt(world, item_key="chicken", linger=0.3),
            mutate,
        )
        # No deadlock, no timeout: every job produced a result rather than an
        # OperationalError about deadlock detection.
        for result in results:
            assert not isinstance(result, BaseException) or isinstance(result, ValidationError), (
                result
            )
        assert StockMovement.objects.count() == 2


class TestConcurrentDuplicatePosting:
    def test_two_posts_of_one_document_create_one_economic_event(
        self, world: dict[str, Any]
    ) -> None:
        """The document row lock, exercised the same way as the opening's."""
        actor = world["poster"]
        _post_receipt(world, quantity="50")

        centre = CostCenter.objects.filter(organization=world["organization"]).first()
        assert centre is not None, "the seeded chart brings cost centres with it"
        document = create_document(
            actor=actor,
            organization=world["organization"],
            branch=world["branch"],
            warehouse=world["warehouse"],
            document_type=InventoryDocumentType.ISSUE,
            effective_at=timezone.now(),
            evidence_reference="DN",
            cost_center=centre,
        )
        add_document_line(
            actor=actor,
            document=document,
            line=DocumentLineInput(item=world["rice"], base_quantity=Decimal("10")),
        )

        def post() -> Any:
            return post_document(actor=actor, document=document)

        results = _race(post, post)
        posted = [r for r in results if isinstance(r, InventoryMovementDocument)]
        refused = [r for r in results if isinstance(r, ValidationError)]
        assert len(posted) == 1, results
        assert len(refused) == 1, results
        assert refused[0].code == "already_posted"
        assert StockMovement.objects.filter(movement_type=MovementType.ISSUE).count() == 1
        assert InventoryMovementDocument.objects.get().status == (InventoryDocumentStatus.POSTED)
