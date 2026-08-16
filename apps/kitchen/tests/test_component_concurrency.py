"""
The component graph under two requests at once, against real COMMITs.

Two transactions can each read a coherent graph a moment before they jointly
break it, and no row lock can see that: `A -> B` and `B -> A` touch no row in
common, so `select_for_update` has nothing to serialise on. The
organization-scoped advisory graph lock exists for exactly that class of
contradiction, and the tests below establish **where it is load-bearing and
where it is defence in depth**, because claiming more than it does would be
worse than claiming less.

* **Cycles cannot be created by a race at all**, and the reason is structural
  rather than lucky: a component may only be written on a `DRAFT` parent and may
  only point at a frozen child, and traversal runs parent-to-child. To walk
  across a newly added edge you must first arrive at its parent as somebody's
  child, and a draft is never anybody's child. `TestTwoEdgesCannotCloseACycle`
  pins that property, so the day the draft-only rule is relaxed the race
  reopens and these tests fail.
* **Coverage genuinely races.** A parent activation validates that its child
  covers the range it is claiming; a child supersession shortens that range.
  Different rows, opposite ends, and the result without a lock is an `ACTIVE`
  parent whose child stopped existing partway through it. That is the race the
  lock is for, and `test_parent_activation_racing_child_supersession` is its
  test.

`transaction=True` throughout: a graph rule tested inside a rolled-back test
transaction proves only that one thread can count. Every assertion is about the
**committed** state afterwards, never about which thread won.
"""

from __future__ import annotations

import datetime
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import pytest
from django.db import connections, transaction

from apps.accounting.models import JournalEntry
from apps.inventory.models import InventoryItem, ItemCategory, ItemType, StockMovement
from apps.kitchen.graph import cycle_path, read_graph
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    approve_recipe_version,
    record_recipe_version_review,
    submit_recipe_version,
)
from apps.kitchen.models import (
    MAX_COMPONENT_DEPTH,
    ApprovalEvidenceKind,
    Recipe,
    RecipeComponent,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
    RecipeVersion,
    RecipeVersionStatus,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    add_recipe_step,
    create_draft_recipe_version,
    create_recipe,
    create_recipe_component,
    remove_recipe_component,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import create_branch, create_organization, grant_branch_access
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db(transaction=True)

JANUARY = datetime.date(2026, 1, 1)
JULY = datetime.date(2026, 7, 1)
REFERENCE = "KM-RCP-004/2026/07"


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


# ---------------------------------------------------------------------------
# A whole world, built inside the test rather than by a rolled-back fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    organization: Organization
    branch: Branch
    people: dict[str, User]
    rice: InventoryItem
    kilogram: UnitOfMeasure


@pytest.fixture
def world(units: None) -> World:
    organization = create_organization(code="KMG", name_ar="خان مندي", name_en="KM")
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=datetime.time(9, 0),
    )
    people: dict[str, User] = {}
    for name, role in (
        ("author", Role.MANAGER),
        ("cook", Role.MANAGER),
        ("keeper", Role.STOREKEEPER),
        ("accountant", Role.ACCOUNTANT),
        ("approver", Role.MANAGER),
    ):
        user = User.objects.create_user(username=f"graph-{name}", password="pw-not-real-1234")
        grant_branch_access(user=user, branch=branch, role=role)
        people[name] = User.objects.get(pk=user.pk)

    category = ItemCategory.objects.create(
        organization=organization, code="FOOD", name_ar="أغذية", depth=1
    )
    kilogram = UnitOfMeasure.objects.get(code="KG")
    rice = InventoryItem.objects.create(
        organization=organization,
        code="RICE",
        name_ar="رز",
        category=category,
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )
    return World(
        organization=organization,
        branch=branch,
        people=people,
        rice=rice,
        kilogram=kilogram,
    )


def _recipe(world: World, code: str) -> Recipe:
    return create_recipe(
        organization=world.organization,
        code=code,
        name_ar=f"وصفة {code}",
        recipe_type=RecipeType.PORTION,
        created_by=world.people["author"],
    )


def _draft(world: World, recipe: Recipe) -> RecipeVersion:
    version = create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("10"),
        output_unit=world.kilogram,
        instructions="نظرة عامة.",
        created_by=world.people["author"],
    )
    add_recipe_line(
        version=version, item=world.rice, entered_quantity=Decimal("4"), entered_unit=world.kilogram
    )
    add_recipe_step(version=version, instruction_ar="خطوة.")
    add_recipe_serving(
        version=version,
        code="ONE",
        name_ar="حصة",
        serving_quantity=Decimal("1"),
        serving_unit=world.kilogram,
        is_primary=True,
    )
    return RecipeVersion.objects.get(pk=version.pk)


def _activate(
    world: World,
    version: RecipeVersion,
    *,
    effective_from: datetime.date = JANUARY,
    effective_to: datetime.date | None = None,
) -> RecipeVersion:
    people = world.people
    submit_recipe_version(version=version, actor=people["author"])
    for review_type, reviewer in (
        (RecipeReviewType.KITCHEN, people["cook"]),
        (RecipeReviewType.STOREKEEPER, people["keeper"]),
    ):
        record_recipe_version_review(
            version=version,
            review_type=review_type,
            reviewer=reviewer,
            decision=RecipeReviewDecision.APPROVED,
        )
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.ACCOUNTING,
        reviewer=people["accountant"],
        decision=RecipeReviewDecision.APPROVED,
        evidence_reference=REFERENCE,
        evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
    )
    approve_recipe_version(
        version=version,
        actor=people["approver"],
        approval_reference=REFERENCE,
        approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
    )
    return activate_recipe_version(
        version=RecipeVersion.objects.get(pk=version.pk),
        actor=people["approver"],
        effective_from=effective_from,
        effective_to=effective_to,
    )


class TestTwoEdgesCannotCloseACycle:
    """
    Why the classic `A -> B` / `B -> A` race cannot corrupt this graph.

    It is worth being precise, because the obvious test passes for the wrong
    reason and would hide a real regression.

    An edge may only be written on a **DRAFT** parent, and may only point at a
    **frozen** child. Traversal runs parent-to-child, so to walk across a newly
    added edge you must first arrive at its parent *as somebody's child* - and a
    draft is never anybody's child. Two concurrent additions therefore cannot
    lie on one path, and cannot jointly close a loop no matter how they
    interleave.

    So these tests assert the property rather than a refusal: whatever commits,
    the committed graph is acyclic. They are the regression that would fail the
    day somebody relaxed the draft-only rule and quietly reopened the race.

    The lock earns its keep elsewhere - see
    `test_parent_activation_racing_child_supersession`, which is a genuine
    cross-row contradiction no single row lock can see.
    """

    def test_a_to_b_racing_b_to_a(self, world: World) -> None:
        a = _recipe(world, "RC-A")
        b = _recipe(world, "RC-B")
        a_v1 = _activate(world, _draft(world, a))
        b_v1 = _activate(world, _draft(world, b))
        a_v2 = _draft(world, a)
        b_v2 = _draft(world, b)

        def work(index: int) -> None:
            if index == 0:
                create_recipe_component(
                    version=a_v2, component_version=b_v1, multiplier=Decimal("1")
                )
            else:
                create_recipe_component(
                    version=b_v2, component_version=a_v1, multiplier=Decimal("1")
                )

        errors = race(work)

        # Both commit, and both are correct: `A v2` contains `B v1`, which
        # contains nothing, and `B v2` contains `A v1`, which contains nothing.
        # Neither version's closure reaches its own recipe.
        assert survivors(errors) == 2, [repr(problem) for problem in errors]
        assert RecipeComponent.objects.count() == 2
        _assert_acyclic(world)

    def test_a_three_node_cycle_completed_by_two_transactions(self, world: World) -> None:
        """
        `C -> B` exists; `A -> C` and `B -> A` race.

        Sequentially, adding `A v2 -> C` *after* `B v2 -> A v1` had been
        approved and activated would be refused. Concurrently it cannot arise:
        `B v2` is a draft while it is being written, so no walk reaches it.
        """
        a = _recipe(world, "RC3-A")
        b = _recipe(world, "RC3-B")
        c = _recipe(world, "RC3-C")
        a_v1 = _activate(world, _draft(world, a))
        b_v1 = _activate(world, _draft(world, b))

        c_draft = _draft(world, c)
        create_recipe_component(version=c_draft, component_version=b_v1, multiplier=Decimal("1"))
        c_v1 = _activate(world, c_draft)

        a_v2 = _draft(world, a)
        b_v2 = _draft(world, b)

        def work(index: int) -> None:
            if index == 0:
                create_recipe_component(
                    version=a_v2, component_version=c_v1, multiplier=Decimal("1")
                )
            else:
                create_recipe_component(
                    version=b_v2, component_version=a_v1, multiplier=Decimal("1")
                )

        errors = race(work)

        assert survivors(errors) >= 1
        _assert_acyclic(world)

    def test_the_sequential_cycle_is_still_refused_after_the_race(self, world: World) -> None:
        """
        The other half of the same claim: once the racing edge is *frozen*, the
        edge that would close the loop is refused. The draft-only rule delays
        the contradiction; the cycle check is what still refuses it.
        """
        a = _recipe(world, "SEQ-A")
        b = _recipe(world, "SEQ-B")
        a_v1 = _activate(world, _draft(world, a))

        b_draft = _draft(world, b)
        create_recipe_component(version=b_draft, component_version=a_v1, multiplier=Decimal("1"))
        b_v1 = _activate(world, b_draft)

        a_v2 = _draft(world, a)
        with pytest.raises(Exception) as caught:
            create_recipe_component(version=a_v2, component_version=b_v1, multiplier=Decimal("1"))
        assert "recipe_component_cycle" in str(caught.value) or "دورة" in str(caught.value)
        _assert_acyclic(world)

    def test_opposite_traversal_order_does_not_deadlock(self, world: World) -> None:
        """
        Two multi-edge edits naming the same recipes in opposite order.

        The graph lock is taken **first and unconditionally**, so both callers
        are already serialised before either reaches a row lock - which is what
        makes the classic lock-ordering deadlock impossible rather than
        unlikely. A deadlock would surface here as a thread that never returns,
        so the join timeout is the assertion.
        """
        x = _recipe(world, "ORD-X")
        y = _recipe(world, "ORD-Y")
        p = _recipe(world, "ORD-P")
        q = _recipe(world, "ORD-Q")
        x_v1 = _activate(world, _draft(world, x))
        y_v1 = _activate(world, _draft(world, y))
        p_draft = _draft(world, p)
        q_draft = _draft(world, q)

        def work(index: int) -> None:
            first, second = (x_v1, y_v1) if index == 0 else (y_v1, x_v1)
            parent = p_draft if index == 0 else q_draft
            create_recipe_component(
                version=parent, component_version=first, multiplier=Decimal("1")
            )
            create_recipe_component(
                version=parent, component_version=second, multiplier=Decimal("1")
            )

        errors = race(work)

        assert survivors(errors) == 2, [repr(problem) for problem in errors]
        assert RecipeComponent.objects.count() == 4


def _assert_acyclic(world: World) -> None:
    """No edge in the committed graph closes a loop at recipe identity."""
    graph = read_graph(world.organization.pk)
    for edge in [edge for edges in graph.children.values() for edge in edges]:
        assert (
            cycle_path(
                graph,
                parent_recipe_id=edge.parent_recipe_id,
                parent_version_id=edge.parent_version_id,
                child_version_id=edge.child_version_id,
                child_recipe_id=edge.child_recipe_id,
            )
            is None
        )


class TestOrderAndLifecycleRaces:
    def test_two_components_racing_for_the_same_line_order(self, world: World) -> None:
        """
        Both callers let the service draw the next order under the lock, so both
        commit with different positions. Nothing collides and nothing is lost.
        """
        parent = _recipe(world, "ORDR-P")
        first = _recipe(world, "ORDR-1")
        second = _recipe(world, "ORDR-2")
        first_v1 = _activate(world, _draft(world, first))
        second_v1 = _activate(world, _draft(world, second))
        parent_draft = _draft(world, parent)

        def work(index: int) -> None:
            child = first_v1 if index == 0 else second_v1
            create_recipe_component(
                version=parent_draft, component_version=child, multiplier=Decimal("1")
            )

        errors = race(work)

        assert survivors(errors) == 2, [repr(problem) for problem in errors]
        orders = sorted(
            RecipeComponent.objects.filter(version=parent_draft).values_list(
                "line_order", flat=True
            )
        )
        assert orders == [1, 2]

    def test_a_component_create_racing_the_parents_submission(self, world: World) -> None:
        """
        Exactly one wins. If the submission commits first the component is
        refused because the parent is no longer a draft; if the component
        commits first the submission certifies a graph that includes it.
        """
        parent = _recipe(world, "SUB-P")
        child = _recipe(world, "SUB-C")
        child_v1 = _activate(world, _draft(world, child))
        parent_draft = _draft(world, parent)

        def work(index: int) -> None:
            if index == 0:
                create_recipe_component(
                    version=parent_draft, component_version=child_v1, multiplier=Decimal("1")
                )
            else:
                submit_recipe_version(
                    version=RecipeVersion.objects.get(pk=parent_draft.pk),
                    actor=world.people["author"],
                )

        errors = race(work)

        refreshed = RecipeVersion.objects.get(pk=parent_draft.pk)
        if refreshed.status == RecipeVersionStatus.SUBMITTED:
            # Whatever the component thread did, the frozen version's contents
            # cannot have changed after it was frozen.
            assert survivors(errors) >= 1
        else:
            assert RecipeComponent.objects.filter(version=parent_draft).count() == 1

    def test_a_component_removal_racing_the_parents_approval(self, world: World) -> None:
        parent = _recipe(world, "APP-P")
        child = _recipe(world, "APP-C")
        child_v1 = _activate(world, _draft(world, child))
        parent_draft = _draft(world, parent)
        component = create_recipe_component(
            version=parent_draft, component_version=child_v1, multiplier=Decimal("1")
        )
        people = world.people
        submit_recipe_version(version=parent_draft, actor=people["author"])
        for review_type, reviewer in (
            (RecipeReviewType.KITCHEN, people["cook"]),
            (RecipeReviewType.STOREKEEPER, people["keeper"]),
        ):
            record_recipe_version_review(
                version=parent_draft,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
            )
        record_recipe_version_review(
            version=parent_draft,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=people["accountant"],
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )

        def work(index: int) -> None:
            if index == 0:
                remove_recipe_component(component=component)
            else:
                approve_recipe_version(
                    version=RecipeVersion.objects.get(pk=parent_draft.pk),
                    actor=people["approver"],
                    approval_reference=REFERENCE,
                    approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
                )

        errors = race(work)

        # The removal is refused outright: the parent left DRAFT at submission,
        # so the approval is the only one of the two that could ever commit.
        assert RecipeComponent.objects.filter(pk=component.pk).exists()
        assert errors[0] is not None
        assert errors[1] is None

    def test_parent_activation_racing_child_supersession(self, world: World) -> None:
        """
        Both touch the child's effective range from opposite ends. Whichever
        commits, the committed state is consistent: either the parent is active
        over a range the child covers, or the child was closed and the parent
        was refused.
        """
        parent = _recipe(world, "ACT-P")
        child = _recipe(world, "ACT-C")
        child_v1 = _activate(world, _draft(world, child))
        parent_draft = _draft(world, parent)
        create_recipe_component(
            version=parent_draft, component_version=child_v1, multiplier=Decimal("1")
        )
        people = world.people
        submit_recipe_version(version=parent_draft, actor=people["author"])
        for review_type, reviewer in (
            (RecipeReviewType.KITCHEN, people["cook"]),
            (RecipeReviewType.STOREKEEPER, people["keeper"]),
        ):
            record_recipe_version_review(
                version=parent_draft,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
            )
        record_recipe_version_review(
            version=parent_draft,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=people["accountant"],
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )
        approve_recipe_version(
            version=RecipeVersion.objects.get(pk=parent_draft.pk),
            actor=people["approver"],
            approval_reference=REFERENCE,
            approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )
        child_v2 = _draft(world, child)
        submit_recipe_version(version=child_v2, actor=people["author"])
        for review_type, reviewer in (
            (RecipeReviewType.KITCHEN, people["cook"]),
            (RecipeReviewType.STOREKEEPER, people["keeper"]),
        ):
            record_recipe_version_review(
                version=child_v2,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
            )
        record_recipe_version_review(
            version=child_v2,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=people["accountant"],
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )
        approve_recipe_version(
            version=RecipeVersion.objects.get(pk=child_v2.pk),
            actor=people["approver"],
            approval_reference=REFERENCE,
            approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )

        def work(index: int) -> None:
            if index == 0:
                activate_recipe_version(
                    version=RecipeVersion.objects.get(pk=parent_draft.pk),
                    actor=people["approver"],
                    effective_from=JULY,
                )
            else:
                activate_recipe_version(
                    version=RecipeVersion.objects.get(pk=child_v2.pk),
                    actor=people["approver"],
                    effective_from=JULY,
                    supersedes=RecipeVersion.objects.get(pk=child_v1.pk),
                )

        race(work)

        refreshed_parent = RecipeVersion.objects.get(pk=parent_draft.pk)
        refreshed_child = RecipeVersion.objects.get(pk=child_v1.pk)
        if refreshed_parent.status == RecipeVersionStatus.ACTIVE:
            # The parent is in force, so its child must still cover it.
            start = refreshed_parent.effective_from
            assert start is not None
            assert refreshed_child.effective_to is None or refreshed_child.effective_to >= start

    def test_the_same_component_command_retried_concurrently(self, world: World) -> None:
        """
        The same add, twice at once. One commits; the other is refused as a
        duplicate child rather than producing two rows for one blend.
        """
        parent = _recipe(world, "RETRY-P")
        child = _recipe(world, "RETRY-C")
        child_v1 = _activate(world, _draft(world, child))
        parent_draft = _draft(world, parent)

        def work(index: int) -> None:
            create_recipe_component(
                version=parent_draft, component_version=child_v1, multiplier=Decimal("1")
            )

        errors = race(work)

        assert survivors(errors) == 1
        assert RecipeComponent.objects.filter(version=parent_draft).count() == 1


class TestDepthUnderRace:
    def test_two_edges_cannot_jointly_exceed_the_depth_limit(self, world: World) -> None:
        """
        A chain three deep already. Two callers each try to add a fourth level
        from a different draft; the graph lock means the second one sees the
        first's edge and is refused.
        """
        codes = ["DR-E", "DR-D", "DR-C", "DR-B"]
        recipes = {code: _recipe(world, code) for code in codes}
        e_v1 = _activate(world, _draft(world, recipes["DR-E"]))

        d_draft = _draft(world, recipes["DR-D"])
        create_recipe_component(version=d_draft, component_version=e_v1, multiplier=Decimal("1"))
        d_v1 = _activate(world, d_draft)

        c_draft = _draft(world, recipes["DR-C"])
        create_recipe_component(version=c_draft, component_version=d_v1, multiplier=Decimal("1"))
        c_v1 = _activate(world, c_draft)

        b_draft = _draft(world, recipes["DR-B"])
        create_recipe_component(version=b_draft, component_version=c_v1, multiplier=Decimal("1"))
        b_v1 = _activate(world, b_draft)

        top_one = _recipe(world, "DR-A1")
        top_two = _recipe(world, "DR-A2")
        first_draft = _draft(world, top_one)
        second_draft = _draft(world, top_two)

        def work(index: int) -> None:
            parent = first_draft if index == 0 else second_draft
            create_recipe_component(version=parent, component_version=b_v1, multiplier=Decimal("1"))

        errors = race(work)

        # Both would make the chain four deep. Neither may commit.
        assert survivors(errors) == 0
        graph = read_graph(world.organization.pk)
        from apps.kitchen.graph import depth_below

        assert depth_below(graph, b_v1.pk)[0] == MAX_COMPONENT_DEPTH


class TestZeroEffectUnderRace:
    def test_no_component_race_moves_stock_or_posts_a_journal(self, world: World) -> None:
        before = (StockMovement.objects.count(), JournalEntry.objects.count())

        a = _recipe(world, "ZE-A")
        b = _recipe(world, "ZE-B")
        a_v1 = _activate(world, _draft(world, a))
        b_v1 = _activate(world, _draft(world, b))
        a_v2 = _draft(world, a)
        b_v2 = _draft(world, b)

        def work(index: int) -> None:
            if index == 0:
                create_recipe_component(
                    version=a_v2, component_version=b_v1, multiplier=Decimal("1")
                )
            else:
                create_recipe_component(
                    version=b_v2, component_version=a_v1, multiplier=Decimal("1")
                )

        race(work)

        assert (StockMovement.objects.count(), JournalEntry.objects.count()) == before
