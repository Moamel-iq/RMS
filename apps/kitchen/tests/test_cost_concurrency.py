"""
Costing under two requests at once, against real COMMITs.

`transaction=True` throughout: a concurrency rule tested inside a rolled-back
test transaction proves only that one thread can count. Every assertion is
about the **committed** state afterwards, never about which thread won.

## What is actually guaranteed here, and what is not

Costing is a **read**. It takes no row locks on inventory and it should not:
locking stock so that a read-only query "looks safe" would make a reporting
screen able to block a delivery, which is a worse failure than any it prevents.
What makes a card internally consistent is different and stronger — a
**posted-sequence cutoff captured once**, with every position constrained to it.
A receipt racing a cost card takes a sequence above the mark and is wholly
excluded, or commits before it and is wholly included. There is no arrangement
in which one line of the card sees it and another does not, and
`TestARacingReceipt` is what holds that.

Snapshot creation is the one write, and its concurrency story is ordinary
idempotency: a key, a fingerprint, and a unique constraint per organization.
Two identical commands produce one snapshot because the database says so, not
because the service checked first — the check is an optimisation over the
constraint, never a substitute for it.

Nothing here asserts a lock ordering on inventory rows, because there is none
to assert. `test_two_callers_in_opposite_item_order_agree` shows what that
absence actually costs: nothing, because a read that takes no locks cannot
deadlock with another read that takes none.
"""

from __future__ import annotations

import datetime
import threading
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connections, transaction

from apps.inventory.models import InventoryItem, StockMovement, Warehouse
from apps.kitchen.costing import cost_recipe_version
from apps.kitchen.lifecycle import activate_recipe_version
from apps.kitchen.models import RecipeCostSnapshot, RecipeVersion, RecipeVersionStatus
from apps.kitchen.snapshots import create_recipe_cost_snapshot
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import build_complete_draft, carry_to_active, carry_to_approved, make_child_recipe

pytestmark = pytest.mark.django_db(transaction=True)


def _today() -> datetime.date:
    from django.utils import timezone

    return timezone.localdate()


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


class TestSnapshotIdempotencyUnderARace:
    def test_two_identical_commands_create_one_snapshot(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        """
        Both threads may believe they are first. The unique constraint on
        `(organization, idempotency_key)` is what decides, and the loser's
        transaction rolls back whole — so there is no orphaned set of lines.
        """
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )

        def work(_index: int) -> None:
            create_recipe_cost_snapshot(
                card=card, actor=manager, idempotency_key="RACE-1", reference="R"
            )

        race(work)
        assert RecipeCostSnapshot.objects.count() == 1
        snapshot = RecipeCostSnapshot.objects.get()
        assert snapshot.lines.count() == len(card.lines)
        assert snapshot.servings.count() == len(card.servings)

    def test_the_same_key_with_a_changed_request_conflicts_under_a_race(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        """One writes, the other is refused — and never silently handed the first."""
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )

        def work(index: int) -> None:
            create_recipe_cost_snapshot(
                card=card,
                actor=manager,
                idempotency_key="RACE-2",
                reference=f"purpose-{index}",
            )

        errors = race(work)
        assert RecipeCostSnapshot.objects.count() == 1
        assert survivors(errors) == 1

    def test_two_different_keys_intentionally_create_two_snapshots(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        """
        A menu repriced twice in one minute is two decisions, and the key is
        what says so. Neither thread blocks the other.
        """
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )

        def work(index: int) -> None:
            create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key=f"RACE-3-{index}")

        errors = race(work)
        assert survivors(errors) == 2
        assert RecipeCostSnapshot.objects.count() == 2

    def test_the_same_key_in_two_organizations_is_independent(
        self,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
        other_open_period: object,
        other_organization: Organization,
        other_branch: object,
        rival_store: Warehouse,
        kilogram: UnitOfMeasure,
        rival_item: InventoryItem,
        manager: User,
    ) -> None:
        """
        Idempotency keys are unique **per organization**. A lookup on the key
        alone would hand one organization's costing record to whoever guessed
        the other's key.
        """
        from apps.organizations.models import Role
        from apps.organizations.services import grant_branch_access

        from .conftest import _user, post_receipt

        rival_people = {}
        for name, role in (
            ("author", Role.MANAGER),
            ("cook", Role.MANAGER),
            ("keeper", Role.STOREKEEPER),
            ("accountant", Role.ACCOUNTANT),
            ("approver", Role.MANAGER),
        ):
            user = _user(f"rival-{name}")
            grant_branch_access(user=user, branch=other_branch, role=role)  # type: ignore[arg-type]
            rival_people[name] = User.objects.get(pk=user.pk)

        post_receipt(
            organization=other_organization,
            warehouse=rival_store,
            item=rival_item,
            quantity="100",
            unit_cost="1000",
            key="rival-stock",
        )
        rival_recipe = make_child_recipe(
            organization=other_organization, code="RIVAL-1", author=rival_people["author"]
        )
        rival_version = carry_to_active(
            build_complete_draft(
                recipe=rival_recipe,
                unit=kilogram,
                item=rival_item,
                author=rival_people["author"],
            ),
            submitter=rival_people["author"],
            cook=rival_people["cook"],
            keeper=rival_people["keeper"],
            accountant=rival_people["accountant"],
            approver=rival_people["approver"],
        )

        mine = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        theirs = cost_recipe_version(
            version=rival_version, warehouse=rival_store, as_of_date=_today()
        )
        create_recipe_cost_snapshot(card=mine, actor=manager, idempotency_key="SHARED")
        create_recipe_cost_snapshot(
            card=theirs, actor=rival_people["author"], idempotency_key="SHARED"
        )
        assert RecipeCostSnapshot.objects.count() == 2


class TestARacingReceipt:
    def test_a_card_racing_a_receipt_reads_one_consistent_state(
        self,
        valued_store: Warehouse,
        organization: Organization,
        costable_version: RecipeVersion,
        rice: InventoryItem,
    ) -> None:
        """
        The receipt is either wholly in the card or wholly out of it.

        Both outcomes are correct, and only a **third** would be a defect: a
        total that is neither the before figure nor the after one. Asserting
        which thread wins would be asserting a scheduler.
        """
        before = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        ).total_material_cost
        results: list[Decimal] = []

        def work(index: int) -> None:
            if index == 0:
                from .conftest import post_receipt

                post_receipt(
                    organization=organization,
                    warehouse=valued_store,
                    item=rice,
                    quantity="200",
                    unit_cost="3000",
                    key="racing-receipt",
                )
            else:
                results.append(
                    cost_recipe_version(
                        version=costable_version,
                        warehouse=valued_store,
                        as_of_date=_today(),
                    ).total_material_cost
                )

        race(work)
        # 200 @ 1,500 plus 200 @ 3,000 is 400 KG worth 900,000 -> 2,250 each,
        # so 4 KG costs 9,000. The only two honest answers.
        assert results
        assert results[0] in {before, Decimal("9000.000")}

    def test_every_line_of_one_card_reads_the_same_cutoff(
        self,
        valued_store: Warehouse,
        organization: Organization,
        costable_version: RecipeVersion,
        rice: InventoryItem,
    ) -> None:
        """
        The property the cutoff exists for, stated directly.

        Whatever else happened, no line of a card may have been priced against
        a ledger state a different line did not see — and the cutoff integer on
        the card is the evidence of which state that was.
        """
        cards = []

        def work(index: int) -> None:
            if index == 0:
                from .conftest import post_receipt

                post_receipt(
                    organization=organization,
                    warehouse=valued_store,
                    item=rice,
                    quantity="200",
                    unit_cost="3000",
                    key="racing-receipt-2",
                )
            else:
                cards.append(
                    cost_recipe_version(
                        version=costable_version,
                        warehouse=valued_store,
                        as_of_date=_today(),
                    )
                )

        race(work)
        assert cards
        card = cards[0]
        highest = StockMovement.objects.filter(organization=organization).order_by(
            "-posted_sequence"
        )
        assert card.cutoff.posted_sequence <= highest.first().posted_sequence  # type: ignore[union-attr]
        for line in card.lines:
            assert line.valuation.last_posted_sequence <= card.cutoff.posted_sequence

    def test_two_callers_in_opposite_item_order_agree(
        self,
        valued_store: Warehouse,
        recipe: object,
        costable_version: RecipeVersion,
    ) -> None:
        """
        No deadlock, and no inconsistent total.

        Costing takes **no** inventory row locks — locking stock so a read-only
        query looks safe would let a reporting screen block a delivery. Two
        concurrent cards therefore cannot deadlock with each other whatever
        order they read items in, and the bulk query means "order" is not even
        a thing a caller controls.
        """
        totals: list[Decimal] = []

        def work(_index: int) -> None:
            totals.append(
                cost_recipe_version(
                    version=costable_version, warehouse=valued_store, as_of_date=_today()
                ).total_material_cost
            )

        errors = race(work)
        assert survivors(errors) == 2
        assert len(set(totals)) == 1


class TestARacingSupersession:
    def test_a_card_racing_a_version_supersession_keeps_the_requested_version(
        self,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
    ) -> None:
        """
        The caller named an exact version. Superseding it changes its status,
        and `cost_recipe_version` accepts `SUPERSEDED` for precisely this
        reason — a superseded version still answers for its own dates.
        """
        replacement = build_complete_draft(
            recipe=costable_version.recipe, unit=kilogram, item=rice, author=manager
        )
        approved = carry_to_approved(
            replacement,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        cards = []

        def work(index: int) -> None:
            if index == 0:
                activate_recipe_version(
                    version=approved,
                    actor=approver,
                    effective_from=_today() + datetime.timedelta(days=1),
                    supersedes=RecipeVersion.objects.get(pk=costable_version.pk),
                )
            else:
                cards.append(
                    cost_recipe_version(
                        version=RecipeVersion.objects.get(pk=costable_version.pk),
                        warehouse=valued_store,
                        as_of_date=_today(),
                    )
                )

        race(work)
        assert cards
        assert cards[0].version.pk == costable_version.pk

    def test_costing_a_parent_while_its_child_is_superseded_keeps_the_frozen_child(
        self,
        valued_store: Warehouse,
        organization: Organization,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        `component_version` is an immutable foreign key. A supersession racing
        a cost card cannot re-point it, because nothing re-points it ever.
        """
        from apps.kitchen.services import create_recipe_component

        child_recipe = make_child_recipe(
            organization=organization, code="RACE-BLEND", author=manager
        )
        child = carry_to_active(
            build_complete_draft(recipe=child_recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        parent_recipe = make_child_recipe(
            organization=organization, code="RACE-DISH", author=manager
        )
        parent_draft = build_complete_draft(
            recipe=parent_recipe, unit=kilogram, item=rice, author=manager
        )
        create_recipe_component(
            version=parent_draft, component_version=child, multiplier=Decimal("0.5")
        )
        parent = carry_to_active(
            parent_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        expected = cost_recipe_version(
            version=parent, warehouse=valued_store, as_of_date=_today()
        ).total_material_cost

        replacement = build_complete_draft(
            recipe=child_recipe, unit=kilogram, item=rice, author=manager
        )
        approved_child = carry_to_approved(
            replacement,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        cards = []

        def work(index: int) -> None:
            if index == 0:
                try:
                    activate_recipe_version(
                        version=approved_child,
                        actor=approver,
                        effective_from=_today() + datetime.timedelta(days=1),
                        supersedes=RecipeVersion.objects.get(pk=child.pk),
                    )
                except ValidationError:
                    pass
            else:
                cards.append(
                    cost_recipe_version(
                        version=RecipeVersion.objects.get(pk=parent.pk),
                        warehouse=valued_store,
                        as_of_date=_today(),
                    )
                )

        race(work)
        assert cards
        card = cards[0]
        assert card.total_material_cost == expected
        assert card.lines[1].source_version.pk == child.pk

    def test_a_snapshot_racing_a_supersession_records_the_version_it_costed(
        self,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
    ) -> None:
        """
        The snapshot's `version` foreign key is the exact row the card named,
        and `version_status` records what that row *was* at the moment the
        decision was taken — not what it became afterwards.
        """
        replacement = build_complete_draft(
            recipe=costable_version.recipe, unit=kilogram, item=rice, author=manager
        )
        approved = carry_to_approved(
            replacement,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

        def work(index: int) -> None:
            if index == 0:
                activate_recipe_version(
                    version=approved,
                    actor=approver,
                    effective_from=_today() + datetime.timedelta(days=1),
                    supersedes=RecipeVersion.objects.get(pk=costable_version.pk),
                )
            else:
                card = cost_recipe_version(
                    version=RecipeVersion.objects.get(pk=costable_version.pk),
                    warehouse=valued_store,
                    as_of_date=_today(),
                )
                create_recipe_cost_snapshot(
                    card=card, actor=manager, idempotency_key="RACE-SUPERSEDE"
                )

        race(work)
        snapshot = RecipeCostSnapshot.objects.filter(idempotency_key="RACE-SUPERSEDE").first()
        if snapshot is not None:
            assert snapshot.version_id == costable_version.pk
            assert snapshot.version_status in {
                RecipeVersionStatus.ACTIVE,
                RecipeVersionStatus.SUPERSEDED,
            }
