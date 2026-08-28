"""
Production drafting under two requests at once, against real COMMITs.

`transaction=True` throughout: a concurrency rule tested inside a rolled-back
test transaction proves only that one thread can count. Every assertion is about
the **committed** state afterwards, and none of them is about which thread won —
"the first one wins" is not a property a system can promise, and a test that
asserted it would be asserting the scheduler.

## What is actually guaranteed here

**One lock, taken in one order.** Every command that touches a draft takes the
`ProductionBatch` row first — `_lock_actual_row` and `_lock_requirement` exist so
that no command has to remember to. That single choice is what makes all nine
races below have an answer:

* two commands on one batch **serialize**, so neither reads a count the other is
  about to invalidate;
* they serialize in the **same order regardless of payload**, so two operators
  working from opposite ends of a requirement list cannot deadlock;
* a check performed under the lock is a **fact**, which is why two simultaneous
  removals cannot both conclude that a row is left over.

Creation is different and deliberately so: two creations are two rows until the
idempotency key says otherwise, and what decides is the unique constraint on
`(organization, idempotency_key)` — not a service that looked first. The check is
an optimisation over the constraint, never a substitute for it.

## The tenth race

`test_production_scale.py::TestARealCommitBoundary` holds "an inconsistent raw
rescale cannot commit". It lives there because it is the same claim as the rest of
that module's subject — the deferred consistency trigger — and splitting one
invariant across two files would leave neither of them readable.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connections, transaction

from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.lifecycle import activate_recipe_version
from apps.kitchen.models import (
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchLine,
    Recipe,
    RecipeVersion,
)
from apps.kitchen.production import (
    add_production_batch_substitute,
    create_production_batch,
    discard_production_batch,
    remove_production_batch_substitute,
    rescale_production_batch,
    update_production_batch_actuals,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import (
    PRODUCTION_DATE,
    build_complete_draft,
    carry_to_approved,
    codes_of,
)

pytestmark = pytest.mark.django_db(transaction=True)


def race(work: Callable[[int], None]) -> list[BaseException | None]:
    """Run `work` twice in parallel and report what each attempt raised."""
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
    """Every refusal code across both attempts, so either order reads the same."""
    found: set[str] = set()
    for problem in errors:
        if isinstance(problem, ValidationError):
            found |= codes_of(problem)
    return found


# ---------------------------------------------------------------------------
# 1 & 2 — creation
# ---------------------------------------------------------------------------


class TestTwoCreationsAtOnce:
    def test_the_same_key_and_request_produces_one_batch(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        manager: User,
    ) -> None:
        """
        Race 1. Both threads may believe they are first.

        The unique constraint on `(organization, idempotency_key)` decides, and
        the loser's transaction rolls back whole — so there is no batch left
        holding requirements nobody planned.
        """

        def work(_index: int) -> None:
            create_production_batch(
                recipe=batch_recipe[0],
                branch=branch,
                warehouse=store,
                planned_business_date=PRODUCTION_DATE,
                multiplier=Decimal("2"),
                actor=manager,
                idempotency_key="RACE-CREATE-1",
            )

        race(work)
        assert ProductionBatch.objects.count() == 1
        batch = ProductionBatch.objects.get()
        assert batch.lines.count() >= 1
        assert ProductionBatchActualLine.objects.filter(line__batch=batch).count() == (
            batch.lines.count()
        )

    def test_a_creation_racing_a_replacement_version_keeps_one_answer(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        Race 2. A creation and an activation of a replacement, at once.

        Whichever lands first, the batch names **one** version and its
        requirements were flattened from that same version. What must never happen
        is a header pointing at one structure and requirements taken from another —
        which is what re-resolving anywhere but creation would produce.
        """
        recipe, _first = batch_recipe
        replacement = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            reference="KM-RCP-004/2026/09",
        )

        def work(index: int) -> None:
            if index == 0:
                create_production_batch(
                    recipe=recipe,
                    branch=branch,
                    warehouse=store,
                    planned_business_date=PRODUCTION_DATE,
                    multiplier=Decimal("2"),
                    actor=manager,
                    idempotency_key="RACE-CREATE-2",
                )
            else:
                activate_recipe_version(
                    version=replacement, actor=approver, effective_from=PRODUCTION_DATE
                )

        race(work)
        for batch in ProductionBatch.objects.all():
            versions = {line.source_version_id for line in batch.lines.all()}
            # A nested requirement would legitimately name a child version, so the
            # claim is that the root's own lines came from the batch's version --
            # not that every requirement did.
            assert batch.recipe_version_id in versions
            assert (
                batch.lines.filter(component_path="")
                .exclude(source_version=batch.recipe_version)
                .count()
                == 0
            )


# ---------------------------------------------------------------------------
# 3 to 8 — two commands on one draft
# ---------------------------------------------------------------------------


class TestTwoCommandsOnOneDraft:
    def test_an_actual_edit_racing_a_rescale(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        Race 3. The pair that would deadlock under the wrong lock order.

        A rescale takes batch → lines → actuals; an actual edit takes the batch
        first for exactly this reason. Both orders reaching the batch row first is
        what turns a deadlock into a wait.

        Either outcome is correct: the rescale refuses because an edit exists, or
        it goes first and the edit lands on the rescaled plan. What is asserted is
        that the committed batch is *coherent* — the trigger from 0015 sees to
        that — and that nothing crashed on a lock.
        """
        line = production_draft.lines.get()
        actual = line.actuals.get()

        def work(index: int) -> None:
            if index == 0:
                update_production_batch_actuals(
                    actual=actual, entered_quantity=Decimal("3.5"), actor=manager
                )
            else:
                rescale_production_batch(
                    batch=production_draft, multiplier=Decimal("4"), actor=manager
                )

        errors = race(work)
        for problem in errors:
            assert not isinstance(problem, Exception) or isinstance(problem, ValidationError), (
                f"unexpected failure: {problem!r}"
            )
        refreshed = ProductionBatch.objects.get(pk=production_draft.pk)
        assert refreshed.multiplier in {Decimal("2.500000"), Decimal("4.000000")}
        for row in refreshed.lines.all():
            assert row.actuals.count() >= 1

    def test_a_substitute_addition_racing_a_rescale(
        self, substituted_draft: ProductionBatch, barley: InventoryItem, manager: User
    ) -> None:
        """
        Race 4. Adding a stand-in while somebody changes the scale.

        A reset-and-rescale deletes every actual row and writes fresh defaults. If
        the addition were not serialized against it, the substitute could be
        written *after* the delete and *before* the defaults — leaving a
        requirement with a stand-in and no primary row, which is a batch that says
        the kitchen used only barley when nobody said that.
        """
        line = substituted_draft.lines.get()

        def work(index: int) -> None:
            if index == 0:
                add_production_batch_substitute(
                    line=line,
                    item=barley,
                    entered_quantity=Decimal("1"),
                    actor=manager,
                    reason="نقص",
                )
            else:
                rescale_production_batch(
                    batch=substituted_draft,
                    multiplier=Decimal("3"),
                    actor=manager,
                    reset_actuals=True,
                    reason="إعادة ضبط أثناء التسابق.",
                )

        race(work)
        refreshed = ProductionBatchLine.objects.get(pk=line.pk)
        rows = list(refreshed.actuals.all())
        assert rows, "a requirement is never left with no actual row"
        assert sum(1 for row in rows if row.substitute_id is None) == 1, (
            "exactly one primary row, whichever command went first"
        )

    def test_two_edits_of_one_actual_row(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        Race 5. Both write; the row ends at one of the two values, never a blend.

        The row lock is what makes that true. Without it the two could interleave
        `entered_quantity` from one and `base_quantity` from the other, producing a
        row whose entered figure and base figure describe different consumptions.
        """
        actual = production_draft.lines.get().actuals.get()

        def work(index: int) -> None:
            update_production_batch_actuals(
                actual=actual,
                entered_quantity=Decimal("6") if index == 0 else Decimal("9"),
                actor=manager,
            )

        race(work)
        row = ProductionBatchActualLine.objects.get(pk=actual.pk)
        assert row.entered_quantity in {Decimal("6.000000"), Decimal("9.000000")}
        assert row.base_quantity == row.entered_quantity

    def test_a_discard_racing_an_edit(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        Race 6. One thread throws the draft away while the other types into it.

        Either the edit lands and is then discarded with it, or the discard goes
        first and the edit is refused by name — never a 500 from a row that
        vanished mid-command, which is what the `_lock_actual_row` existence guard
        is for.
        """
        actual = production_draft.lines.get().actuals.get()

        def work(index: int) -> None:
            if index == 0:
                discard_production_batch(
                    batch=production_draft, actor=manager, reason="أُلغيت أثناء التعديل."
                )
            else:
                update_production_batch_actuals(
                    actual=actual, entered_quantity=Decimal("2"), actor=manager
                )

        errors = race(work)
        for problem in errors:
            assert problem is None or isinstance(problem, ValidationError), (
                f"unexpected failure: {problem!r}"
            )
        if not ProductionBatch.objects.filter(pk=production_draft.pk).exists():
            assert not ProductionBatchActualLine.objects.filter(pk=actual.pk).exists()

    def test_a_reset_and_rescale_racing_an_edit(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        Race 7. The command whose whole purpose is to discard the other's work.

        Serialized, so the outcome is one of two coherent states: the edit is
        written and then deliberately reset, or the reset completes and the edit
        lands on the fresh default. What must not survive is a row the reset
        deleted still being updated, or an updated row the reset never saw.
        """
        actual = production_draft.lines.get().actuals.get()

        def work(index: int) -> None:
            if index == 0:
                rescale_production_batch(
                    batch=production_draft,
                    multiplier=Decimal("5"),
                    actor=manager,
                    reset_actuals=True,
                    reason="إعادة ضبط أثناء التعديل.",
                )
            else:
                update_production_batch_actuals(
                    actual=actual, entered_quantity=Decimal("8"), actor=manager
                )

        errors = race(work)
        for problem in errors:
            assert problem is None or isinstance(problem, ValidationError), (
                f"unexpected failure: {problem!r}"
            )
        line = ProductionBatch.objects.get(pk=production_draft.pk).lines.get()
        assert line.actuals.count() == 1
        row = line.actuals.get()
        assert row.base_quantity == row.entered_quantity

    def test_two_callers_in_opposite_payload_order_do_not_deadlock(
        self, nested_race_draft: ProductionBatch, manager: User
    ) -> None:
        """
        Race 8. Two operators editing the same requirements from opposite ends.

        The ordinary case, not the exotic one, and the reason the lock order is
        documented rather than emergent. Both threads take the batch row first, so
        one waits; if either had begun with its own first requirement, this is the
        test that would hang and then fail on a deadlock.
        """
        rows = list(
            ProductionBatchActualLine.objects.filter(line__batch=nested_race_draft).order_by(
                "line__line_order", "entry_order"
            )
        )
        assert len(rows) > 1

        def work(index: int) -> None:
            ordered = rows if index == 0 else list(reversed(rows))
            for position, row in enumerate(ordered, start=1):
                update_production_batch_actuals(
                    actual=row, entered_quantity=Decimal(position), actor=manager
                )

        errors = race(work)
        for problem in errors:
            assert problem is None or isinstance(problem, ValidationError), (
                f"deadlock or crash rather than a wait: {problem!r}"
            )
        assert survivors(errors) >= 1


# ---------------------------------------------------------------------------
# 9 — the last two rows
# ---------------------------------------------------------------------------


class TestTheLastActualRowsCannotBothBeRemoved:
    def test_two_removals_cannot_empty_a_requirement(
        self, substituted_draft: ProductionBatch, barley: InventoryItem, manager: User
    ) -> None:
        """
        Race 9, and the reason the count is taken **under the batch lock**.

        Two rows, two threads, each removing a different one. Both would look and
        see "one other row remains" if the look were unsynchronized, and both would
        delete — leaving a requirement with nothing to say what was consumed. That
        is not "no consumption", it is "nobody said", and readiness would then
        refuse a batch nobody could see the cause of.

        Exactly one removal succeeds. The other is refused by name.
        """
        line = substituted_draft.lines.get()
        primary = line.actuals.get()
        substitute = add_production_batch_substitute(
            line=line,
            item=barley,
            entered_quantity=Decimal("1"),
            actor=manager,
            reason="نقص في السوق",
        )
        assert line.actuals.count() == 2
        targets = [primary, substitute]

        def work(index: int) -> None:
            remove_production_batch_substitute(
                actual=targets[index], actor=manager, reason="حذف متسابق"
            )

        errors = race(work)
        remaining = ProductionBatchActualLine.objects.filter(line=line).count()
        assert remaining == 1, f"exactly one row survives, found {remaining}"
        assert survivors(errors) == 1
        assert "production_actual_last_row_is_not_removable" in raised(errors)

    def test_removing_the_only_row_is_refused_outright(
        self, production_draft: ProductionBatch, manager: User
    ) -> None:
        """The same rule without a race, so the refusal is not an artefact of timing."""
        actual = production_draft.lines.get().actuals.get()
        with pytest.raises(ValidationError) as exc:
            remove_production_batch_substitute(actual=actual, actor=manager)
        assert "production_actual_last_row_is_not_removable" in codes_of(exc.value)


# ---------------------------------------------------------------------------
# A draft with several requirements, for the opposite-order race
# ---------------------------------------------------------------------------


@pytest.fixture
def nested_race_draft(
    organization: Organization,
    branch: Branch,
    store: Warehouse,
    cooked_rice: InventoryItem,
    kilogram: UnitOfMeasure,
    litre: UnitOfMeasure,
    rice: InventoryItem,
    oil: InventoryItem,
    manager: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> ProductionBatch:
    """A draft with two requirements, so "opposite order" is a real arrangement."""
    from apps.kitchen.services import (
        add_recipe_line,
        create_recipe,
        set_recipe_branches,
    )

    from .conftest import carry_to_active

    recipe = create_recipe(
        organization=organization,
        code="RACE-DISH",
        name="طبخة للتسابق",
        recipe_type="BATCH",
        output_item=cooked_rice,
        created_by=manager,
    )
    set_recipe_branches(recipe=recipe, branches=[branch])
    version = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
    add_recipe_line(version=version, item=oil, entered_quantity=Decimal("2"), entered_unit=litre)
    carry_to_active(
        version,
        submitter=manager,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )
    return create_production_batch(
        recipe=recipe,
        branch=branch,
        warehouse=store,
        planned_business_date=PRODUCTION_DATE,
        multiplier=Decimal("2"),
        actor=manager,
        idempotency_key="RACE-ORDER-1",
    )
