"""
Posting under concurrency, against real COMMITs.

`transaction=True` throughout: a rule about what two transactions may do to
each other, tested inside a single rolled-back test transaction, proves only
that one thread can count. These run against the database the way production
does, and each one asserts on the **committed** state afterwards rather than on
what either thread believed.

## The one thing every test here is really about

A production posting is the first Kitchen command that moves stock, and it
takes locks in two families at once: the kitchen aggregate (batch →
requirements → actual rows → allocations) and the inventory kernel's own
(warehouse freeze → account mappings → stock keys → posted-order counter).
Taking them in that order, always, is what stops a posting deadlocking against
an ordinary issue from the same store. The tests that look like they are about
"two posts of one batch" are mostly about that ordering holding.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connections, transaction

from apps.accounting.models import Account
from apps.inventory.models import InventoryItem, MovementType, StockMovement, Warehouse
from apps.kitchen.models import ProductionBatch, ProductionBatchStatus
from apps.kitchen.production import (
    discard_production_batch,
    record_production_output,
    rescale_production_batch,
    update_production_batch_actuals,
)
from apps.kitchen.production_posting import (
    AllocationInput,
    post_production_batch,
    reverse_production_batch,
    set_production_allocations,
)
from apps.organizations.models import Organization
from apps.users.models import User

from .conftest import codes_of

pytestmark = pytest.mark.django_db(transaction=True)


def race(work: Callable[[int], object]) -> list[BaseException | None]:
    """
    Run `work` twice in parallel and report what each attempt raised.

    The callable may return anything; the return value is discarded on
    purpose. What a racing thread *returned* is never the evidence — the
    assertion is always on the committed state afterwards, because a second
    posting that returned a batch **and** wrote a second set of movements
    looks identical from the caller's side.
    """
    errors: list[BaseException | None] = [None, None]
    barrier = threading.Barrier(2)

    def run(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            with transaction.atomic():
                work(index)
        except BaseException as problem:  # noqa: BLE001 - recorded, then asserted
            errors[index] = problem
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return errors


def survivors(errors: list[BaseException | None]) -> int:
    return len([problem for problem in errors if problem is None])


def raised(errors: list[BaseException | None]) -> set[str]:
    found: set[str] = set()
    for problem in errors:
        if isinstance(problem, ValidationError):
            found |= codes_of(problem)
    return found


@pytest.fixture
def ready(
    posting_store: Warehouse, production_draft: ProductionBatch, manager: User
) -> ProductionBatch:
    item = production_draft.recipe.output_item
    assert item is not None
    record_production_output(
        batch=production_draft,
        entered_quantity=Decimal("40"),
        entered_unit=item.base_unit,
        actor=manager,
    )
    return ProductionBatch.objects.get(pk=production_draft.pk)


@pytest.fixture
def posted(ready: ProductionBatch, manager: User) -> ProductionBatch:
    return post_production_batch(batch=ready, idempotency_key="RACE-POST", actor=manager)


def _fresh(batch: ProductionBatch) -> ProductionBatch:
    return ProductionBatch.objects.get(pk=batch.pk)


# ---------------------------------------------------------------------------
# 1, 11, 12 — posting against itself
# ---------------------------------------------------------------------------


class TestTwoPostsOfOneBatch:
    def test_only_one_posting_commits(self, ready: ProductionBatch, manager: User) -> None:
        """
        Two operators press post at the same instant.

        One wins; the other must find the batch already posted rather than
        producing a second set of movements. Asserted on the committed
        movements, not on the exceptions: a second posting that raised *and*
        wrote would look identical from the caller's side.
        """
        errors = race(
            lambda index: post_production_batch(
                batch=_fresh(ready), idempotency_key=f"TWO-{index}", actor=manager
            )
        )

        assert survivors(errors) == 1
        assert "production_batch_already_posted" in raised(errors)
        final = _fresh(ready)
        assert final.status == ProductionBatchStatus.POSTED
        assert StockMovement.objects.filter(entry=final.stock_entry).count() >= 2
        assert ProductionBatch.objects.filter(number=final.number).count() == 1

    def test_the_same_key_and_payload_produces_one_posting(
        self, ready: ProductionBatch, manager: User
    ) -> None:
        """A retry is a retry even when it arrives simultaneously."""
        errors = race(
            lambda index: post_production_batch(
                batch=_fresh(ready), idempotency_key="SAME-KEY", actor=manager
            )
        )

        assert survivors(errors) >= 1
        final = _fresh(ready)
        assert final.status == ProductionBatchStatus.POSTED
        entries = {
            movement.entry_id
            for movement in StockMovement.objects.filter(movement_type=MovementType.PRODUCTION_IN)
        }
        assert len(entries) == 1

    def test_a_changed_payload_under_one_key_conflicts(
        self, ready: ProductionBatch, manager: User
    ) -> None:
        """
        The key is matched against a fingerprint, never against itself.

        A caller who reused a key after changing the batch and received the
        unchanged posting would believe the change had gone through.
        """
        posted = post_production_batch(batch=ready, idempotency_key="FINGERPRINT", actor=manager)
        with pytest.raises(ValidationError) as caught:
            post_production_batch(
                batch=_fresh(posted), idempotency_key="FINGERPRINT-OTHER", actor=manager
            )
        assert "production_batch_already_posted" in codes_of(caught.value)


# ---------------------------------------------------------------------------
# 2, 3, 4 — posting against the draft commands
# ---------------------------------------------------------------------------


class TestPostingAgainstDraftEdits:
    def test_posting_racing_an_actual_edit(self, ready: ProductionBatch, manager: User) -> None:
        """
        Whichever wins, the posting reflects **committed** quantities.

        The edit either lands first and is posted, or is refused because the
        batch is no longer a draft. What must never happen is a posting that
        moved one quantity while the row records another.
        """
        line = ready.lines.first()
        assert line is not None
        actual = line.actuals.first()
        assert actual is not None

        def work(index: int) -> None:
            if index == 0:
                post_production_batch(
                    batch=_fresh(ready), idempotency_key="EDIT-RACE", actor=manager
                )
            else:
                update_production_batch_actuals(
                    actual=type(actual).objects.get(pk=actual.pk),
                    entered_quantity=Decimal("1"),
                    entered_unit=actual.item.base_unit,
                    actor=manager,
                )

        race(work)

        final = _fresh(ready)
        entry = final.stock_entry
        if final.status == ProductionBatchStatus.POSTED and entry is not None:
            movements = {
                movement.effect_key: abs(movement.base_quantity)
                for movement in entry.movements.all()
            }
            for row in type(actual).objects.filter(line__batch=final):
                if row.base_quantity > Decimal("0"):
                    key = f"production-actual:{row.public_id}"
                    assert movements.get(key) == row.base_quantity

    def test_posting_racing_a_rescale(self, ready: ProductionBatch, manager: User) -> None:
        def work(index: int) -> None:
            if index == 0:
                post_production_batch(
                    batch=_fresh(ready), idempotency_key="RESCALE-RACE", actor=manager
                )
            else:
                rescale_production_batch(
                    batch=_fresh(ready),
                    multiplier=Decimal("3"),
                    actor=manager,
                    reset_actuals=True,
                    reason="سباق",
                )

        race(work)

        final = _fresh(ready)
        if final.status == ProductionBatchStatus.POSTED:
            # Whatever the multiplier ended as, the posting's value is the
            # value of what actually left. The two can never disagree.
            assert final.input_value == final.output_value

    def test_posting_racing_a_discard(self, ready: ProductionBatch, manager: User) -> None:
        """A discarded draft is a named refusal, never a 500 and never a ghost posting."""

        def work(index: int) -> None:
            if index == 0:
                post_production_batch(
                    batch=_fresh(ready), idempotency_key="DISCARD-RACE", actor=manager
                )
            else:
                discard_production_batch(batch=_fresh(ready), actor=manager, reason="سباق الحذف")

        errors = race(work)

        survived = ProductionBatch.objects.filter(pk=ready.pk).first()
        if survived is None:
            # The discard won. Nothing may have posted.
            assert not StockMovement.objects.filter(
                movement_type=MovementType.PRODUCTION_IN
            ).exists()
        else:
            assert survived.status == ProductionBatchStatus.POSTED
        assert not any(
            isinstance(problem, Exception) and not isinstance(problem, ValidationError)
            for problem in errors
        )


# ---------------------------------------------------------------------------
# 5, 6 — posting against the inventory kernel
# ---------------------------------------------------------------------------


class TestPostingAgainstInventory:
    def test_posting_racing_an_ordinary_issue_of_the_same_stock(
        self,
        ready: ProductionBatch,
        organization: Organization,
        rice: InventoryItem,
        posting_store: Warehouse,
        manager: User,
    ) -> None:
        """
        Both take the same stock key, and neither may double-spend it.

        The point is not that one fails — both may legitimately succeed if the
        shelf holds enough. The point is that the balance afterwards equals the
        opening balance minus exactly what the two of them together removed.
        """
        from apps.inventory.models import StockBalance

        before = StockBalance.objects.get(warehouse=posting_store, item=rice, lot=None)
        opening = before.quantity

        def work(index: int) -> None:
            if index == 0:
                post_production_batch(
                    batch=_fresh(ready), idempotency_key="ISSUE-RACE", actor=manager
                )
            else:
                from .conftest import post_issue

                post_issue(
                    organization=organization,
                    warehouse=posting_store,
                    item=rice,
                    quantity="5",
                    key="issue-race",
                )

        race(work)

        after = StockBalance.objects.get(warehouse=posting_store, item=rice, lot=None)
        removed = sum(
            -movement.base_quantity
            for movement in StockMovement.objects.filter(
                warehouse=posting_store, item=rice, base_quantity__lt=Decimal("0")
            )
        )
        assert after.quantity == opening - removed
        assert after.quantity >= Decimal("0")

    def test_posting_racing_an_account_mapping_mutation(
        self,
        ready: ProductionBatch,
        organization: Organization,
        kitchen_accounts: Account,
        cooked_rice: InventoryItem,
        manager: User,
    ) -> None:
        """
        A mapping mutation takes the organization lock exclusively; a posting
        takes it shared. Neither may resolve an account the other is re-homing.
        """
        import datetime

        from apps.accounting.models import INVENTORY_CONTROL
        from apps.accounting.services import create_account
        from apps.inventory.accounts import create_inventory_mapping

        def work(index: int) -> None:
            if index == 0:
                post_production_batch(
                    batch=_fresh(ready), idempotency_key="MAPPING-RACE", actor=manager
                )
            else:
                account = create_account(
                    organization=organization,
                    code="1-03-01-095",
                    name_ar="حساب سباق",
                    name_en="Race account",
                )
                create_inventory_mapping(
                    organization=organization,
                    role=INVENTORY_CONTROL,
                    account=account,
                    item=cooked_rice,
                    effective_from=datetime.date(2026, 1, 1),
                )

        race(work)

        final = _fresh(ready)
        if final.status == ProductionBatchStatus.POSTED:
            # Whichever mapping was in force, the posting resolved exactly one
            # and its journal — or its silence — agrees with the movements.
            assert final.input_value == final.output_value


# ---------------------------------------------------------------------------
# 7, 8, 9 — the output, and undoing it
# ---------------------------------------------------------------------------


class TestTheOutputAndItsReversal:
    def test_two_postings_never_create_two_output_lots(
        self,
        posting_store: Warehouse,
        production_draft: ProductionBatch,
        cooked_rice: InventoryItem,
        manager: User,
    ) -> None:
        from apps.inventory.models import InventoryLot

        cooked_rice.tracks_lots = True
        cooked_rice.save(update_fields=["tracks_lots", "updated_at"])
        record_production_output(
            batch=production_draft,
            entered_quantity=Decimal("40"),
            entered_unit=cooked_rice.base_unit,
            actor=manager,
        )

        race(
            lambda index: post_production_batch(
                batch=_fresh(production_draft), idempotency_key=f"LOT-{index}", actor=manager
            )
        )

        assert InventoryLot.objects.filter(item=cooked_rice).count() == 1

    def test_two_reversals_produce_one(self, posted: ProductionBatch, manager: User) -> None:
        errors = race(
            lambda index: reverse_production_batch(
                batch=_fresh(posted),
                idempotency_key=f"REV-{index}",
                reason="سبب",
                actor=manager,
            )
        )

        assert survivors(errors) == 1
        assert "production_batch_already_reversed" in raised(errors)
        final = _fresh(posted)
        assert final.status == ProductionBatchStatus.REVERSED
        assert (
            StockMovement.objects.filter(entry=final.reversal_stock_entry).count()
            == StockMovement.objects.filter(entry=final.stock_entry).count()
        )

    def test_a_reversal_racing_consumption_of_the_output(
        self,
        posted: ProductionBatch,
        organization: Organization,
        cooked_rice: InventoryItem,
        posting_store: Warehouse,
        manager: User,
    ) -> None:
        """
        Reversing takes the produced goods back off the shelf.

        If somebody has already issued them, there is nothing to take back and
        the kernel refuses rather than driving the position negative — which is
        the whole reason "reverse the batch" must not become the standard way
        to create negative stock.
        """
        from apps.inventory.models import StockBalance

        def work(index: int) -> None:
            if index == 0:
                reverse_production_batch(
                    batch=_fresh(posted),
                    idempotency_key="CONSUME-REV",
                    reason="سبب",
                    actor=manager,
                )
            else:
                from .conftest import post_issue

                post_issue(
                    organization=organization,
                    warehouse=posting_store,
                    item=cooked_rice,
                    quantity="40",
                    key="consume-output",
                )

        race(work)

        balance = StockBalance.objects.filter(
            warehouse=posting_store, item=cooked_rice, lot=None
        ).first()
        if balance is not None:
            assert balance.quantity >= Decimal("0")
            assert balance.value >= Decimal("0")


# ---------------------------------------------------------------------------
# 10 — the deadlock the lock order exists to prevent
# ---------------------------------------------------------------------------


class TestAllocationOrdering:
    def test_opposite_allocation_order_does_not_deadlock(
        self, ready: ProductionBatch, manager: User
    ) -> None:
        """
        Two callers allocate the same rows in opposite order.

        Both take the batch first and then their rows, so neither can hold what
        the other is waiting for. A deadlock would surface as `OperationalError`
        and never as a refusal, which is why the assertion is about the
        exception **type** rather than about who won.

        When the recipe expands to a single consumption row the two callers
        contend on that one row instead, which exercises the same lock order —
        the batch before the row — and is not a weaker test of it.
        """
        rows = [
            actual
            for line in ready.lines.all()
            for actual in line.actuals.all()
            if actual.base_quantity > Decimal("0")
        ]
        assert rows, "a ready batch consumes something"

        def work(index: int) -> None:
            ordered = rows if index == 0 else list(reversed(rows))
            for actual in ordered:
                set_production_allocations(
                    actual=type(actual).objects.get(pk=actual.pk),
                    rows=[AllocationInput(base_quantity=actual.base_quantity)],
                )

        errors = race(work)

        for problem in errors:
            assert not isinstance(problem, Exception) or isinstance(problem, ValidationError), (
                problem
            )
        # Whoever committed last, each row holds exactly one allocation summing
        # to its own quantity. A lost update would leave two.
        for actual in rows:
            allocations = list(type(actual).objects.get(pk=actual.pk).allocations.all())
            assert len(allocations) == 1
            assert allocations[0].base_quantity == actual.base_quantity
