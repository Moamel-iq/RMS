"""
The lifecycle under two requests at once, against real COMMITs.

`transaction=True` throughout, because the whole subject here is what the
database does at commit. A uniqueness or exclusion rule tested inside a
rolled-back test transaction proves only that one thread can count.

Every test is the same shape: two threads meet at a barrier, both attempt the
same transition, and the assertion is about the **committed** state afterwards —
never about which thread won.
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
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    approve_recipe_version,
    record_recipe_version_review,
    reject_recipe_version,
    resolve_recipe_version,
    submit_recipe_version,
)
from apps.kitchen.models import (
    ApprovalEvidenceKind,
    Recipe,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionStatus,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    add_recipe_step,
    archive_recipe,
    create_draft_recipe_version,
    create_recipe,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db(transaction=True)

JULY = datetime.date(2026, 7, 1)
AUGUST = datetime.date(2026, 8, 1)
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
        thread.join(timeout=30)
    return errors


def survivors(errors: list[BaseException | None]) -> int:
    return len([problem for problem in errors if problem is None])


# ---------------------------------------------------------------------------
# A whole world, built inside the test rather than by a rolled-back fixture
# ---------------------------------------------------------------------------
#
# `transaction=True` truncates the tables between tests, so the ordinary
# fixtures cannot be reused: each test builds exactly what it needs. Typed
# rather than a dict, so a mistyped key is a failure at check time instead of a
# `KeyError` inside a thread whose exception the harness deliberately swallows.


@dataclass(frozen=True)
class World:
    organization: Organization
    branch: Branch
    second_branch: Branch
    people: dict[str, User]
    recipe: Recipe
    rice: InventoryItem
    kilogram: UnitOfMeasure


@pytest.fixture
def world(units: None) -> World:
    organization = create_organization(code="KMC", name_ar="خان مندي", name_en="KM")
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=datetime.time(9, 0),
    )
    second_branch = create_branch(
        organization=organization,
        code="KARRADA",
        name_ar="الكرادة",
        name_en="Karrada",
        business_day_start_time=datetime.time(9, 0),
    )
    people: dict[str, User] = {}
    for name, role in (
        ("author", Role.MANAGER),
        ("cook", Role.MANAGER),
        ("keeper", Role.STOREKEEPER),
        ("accountant", Role.ACCOUNTANT),
        ("approver", Role.MANAGER),
        ("second_approver", Role.MANAGER),
    ):
        user = User.objects.create_user(username=f"race-{name}", password="pw-not-real-1234")
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
    recipe = create_recipe(
        organization=organization,
        code="RACE-1",
        name_ar="وصفة سباق",
        recipe_type=RecipeType.PORTION,
        created_by=people["author"],
    )
    return World(
        organization=organization,
        branch=branch,
        second_branch=second_branch,
        people=people,
        recipe=recipe,
        rice=rice,
        kilogram=kilogram,
    )


def make_draft(world: World) -> RecipeVersion:
    """A submittable draft on the race recipe."""
    recipe = world.recipe
    people = world.people
    version = create_draft_recipe_version(
        recipe=recipe,
        expected_output_quantity=Decimal("10"),
        output_unit=world.kilogram,
        instructions="نظرة عامة.",
        created_by=people["author"],
    )
    add_recipe_line(
        version=version,
        item=world.rice,
        entered_quantity=Decimal("2"),
        entered_unit=world.kilogram,
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


def carry(world: World, version: RecipeVersion) -> RecipeVersion:
    """Submit and gather every signature, leaving the version APPROVED."""
    people = world.people
    submit_recipe_version(version=version, actor=people["author"])
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.KITCHEN,
        reviewer=people["cook"],
        decision=RecipeReviewDecision.APPROVED,
    )
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.STOREKEEPER,
        reviewer=people["keeper"],
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
    return approve_recipe_version(
        version=version,
        actor=people["approver"],
        approval_reference=REFERENCE,
        approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
    )


class TestTwoApprovalsOfOneSubmission:
    def test_only_one_approval_commits(self, world: World) -> None:
        """
        Two managers press approve at the same instant. The version's row lock
        serialises them, and the second finds a status it may no longer move —
        so exactly one approval exists, with one approver on it.
        """
        version = make_draft(world)
        people = world.people
        submit_recipe_version(version=version, actor=people["author"])
        for review_type, reviewer, extra in (
            (RecipeReviewType.KITCHEN, people["cook"], {}),
            (RecipeReviewType.STOREKEEPER, people["keeper"], {}),
            (
                RecipeReviewType.ACCOUNTING,
                people["accountant"],
                {
                    "evidence_reference": REFERENCE,
                    "evidence_kind": ApprovalEvidenceKind.SIGNED_FORM,
                },
            ),
        ):
            record_recipe_version_review(
                version=version,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
                **extra,
            )
        approvers = [people["approver"], people["second_approver"]]

        def work(index: int) -> None:
            approve_recipe_version(
                version=version,
                actor=approvers[index],
                approval_reference=REFERENCE,
                approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
            )

        errors = race(work)

        assert survivors(errors) == 1
        version.refresh_from_db()
        assert version.status == RecipeVersionStatus.APPROVED
        assert version.reviews.filter(review_type=RecipeReviewType.FINAL).count() == 1


class TestApprovalRacingRejection:
    def test_exactly_one_of_them_commits(self, world: World) -> None:
        version = make_draft(world)
        people = world.people
        submit_recipe_version(version=version, actor=people["author"])
        for review_type, reviewer, extra in (
            (RecipeReviewType.KITCHEN, people["cook"], {}),
            (RecipeReviewType.STOREKEEPER, people["keeper"], {}),
            (
                RecipeReviewType.ACCOUNTING,
                people["accountant"],
                {
                    "evidence_reference": REFERENCE,
                    "evidence_kind": ApprovalEvidenceKind.SIGNED_FORM,
                },
            ),
        ):
            record_recipe_version_review(
                version=version,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
                **extra,
            )

        def work(index: int) -> None:
            if index == 0:
                approve_recipe_version(
                    version=version,
                    actor=people["approver"],
                    approval_reference=REFERENCE,
                    approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
                )
            else:
                reject_recipe_version(
                    version=version,
                    actor=people["second_approver"],
                    reason="رفض متزامن.",
                )

        errors = race(work)

        assert survivors(errors) == 1
        version.refresh_from_db()
        assert version.status in {
            RecipeVersionStatus.APPROVED,
            RecipeVersionStatus.REJECTED,
        }
        assert version.reviews.filter(review_type=RecipeReviewType.FINAL).count() == 1


class TestTwoActivationsCannotOverlap:
    def test_only_one_claim_on_the_branch_survives(self, world: World) -> None:
        """
        The most important race in this task. Two approved versions of one
        recipe are activated over the same open range at the same instant; the
        exclusion constraint is what decides, and it decides at COMMIT.
        """
        first = carry(world, make_draft(world))
        second = carry(world, make_draft(world))
        people = world.people
        versions = [first, second]

        def work(index: int) -> None:
            activate_recipe_version(
                version=versions[index], actor=people["approver"], effective_from=JULY
            )

        errors = race(work)

        assert survivors(errors) == 1
        active = RecipeVersion.objects.filter(
            recipe=world.recipe, status=RecipeVersionStatus.ACTIVE
        )
        assert active.count() == 1
        assert (
            RecipeVersionBranchScope.objects.filter(
                recipe=world.recipe, branch=world.branch
            ).count()
            == 1
        )

    def test_the_resolver_never_sees_a_committed_ambiguity(self, world: World) -> None:
        first = carry(world, make_draft(world))
        second = carry(world, make_draft(world))
        people = world.people
        versions = [first, second]

        def work(index: int) -> None:
            activate_recipe_version(
                version=versions[index], actor=people["approver"], effective_from=JULY
            )

        race(work)

        resolved = resolve_recipe_version(recipe=world.recipe, branch=world.branch, on_date=JULY)
        assert resolved.status == RecipeVersionStatus.ACTIVE

    def test_two_branches_activate_side_by_side_without_colliding(self, world: World) -> None:
        """The constraint is per branch, so this race has two winners."""
        first = carry(world, make_draft(world))
        second = carry(world, make_draft(world))
        people = world.people
        plans = [
            (first, world.branch),
            (second, world.second_branch),
        ]

        def work(index: int) -> None:
            version, branch = plans[index]
            activate_recipe_version(
                version=version,
                actor=people["approver"],
                effective_from=JULY,
                branches=[branch],
            )

        errors = race(work)

        assert survivors(errors) == 2
        assert (
            resolve_recipe_version(recipe=world.recipe, branch=world.branch, on_date=JULY).pk
            == first.pk
        )
        assert (
            resolve_recipe_version(recipe=world.recipe, branch=world.second_branch, on_date=JULY).pk
            == second.pk
        )


class TestTwoSupersessionsOfOnePair:
    def test_only_one_supersession_commits(self, world: World) -> None:
        people = world.people
        first = activate_recipe_version(
            version=carry(world, make_draft(world)),
            actor=people["approver"],
            effective_from=JULY,
        )
        second = carry(world, make_draft(world))

        def work(index: int) -> None:
            activate_recipe_version(
                version=second,
                actor=people["approver"],
                effective_from=AUGUST,
                supersedes=first,
            )

        errors = race(work)

        assert survivors(errors) == 1
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.status == RecipeVersionStatus.SUPERSEDED
        assert first.effective_to == datetime.date(2026, 7, 31)
        assert second.status == RecipeVersionStatus.ACTIVE


class TestArchivalRacingActivation:
    def test_a_recipe_archived_mid_activation_leaves_a_consistent_state(self, world: World) -> None:
        """
        Archiving takes no recipe lock and activation does, so the two can run
        in either order. Whichever wins, the committed state must still be
        one the verifier accepts — an active version with a complete scope, or
        no active version at all.
        """
        from apps.kitchen.reconciliation import verify_organization

        approved = carry(world, make_draft(world))
        people = world.people

        def work(index: int) -> None:
            if index == 0:
                activate_recipe_version(
                    version=approved, actor=people["approver"], effective_from=JULY
                )
            else:
                archive_recipe(recipe=world.recipe, reason="أرشفة متزامنة")

        race(work)

        assert verify_organization(world.organization) == []


class TestTheRacesPostNothing:
    def test_no_race_moves_stock_or_writes_a_journal(self, world: World) -> None:
        before = (StockMovement.objects.count(), JournalEntry.objects.count())
        first = carry(world, make_draft(world))
        second = carry(world, make_draft(world))
        people = world.people
        versions = [first, second]

        def work(index: int) -> None:
            activate_recipe_version(
                version=versions[index], actor=people["approver"], effective_from=JULY
            )

        race(work)

        assert (StockMovement.objects.count(), JournalEntry.objects.count()) == before


class TestOneOpenVersionPerRecipe:
    def test_two_drafts_cannot_be_opened_at_once(self, world: World) -> None:
        """
        Two versions in flight would race for one effective range and the loser
        would find out only after every reviewer had signed. The partial unique
        index refuses the second, at COMMIT.
        """
        recipe = world.recipe
        people = world.people

        def work(index: int) -> None:
            create_draft_recipe_version(
                recipe=recipe,
                expected_output_quantity=Decimal("10"),
                output_unit=world.kilogram,
                created_by=people["author"],
            )

        errors = race(work)

        assert survivors(errors) == 1
        assert (
            RecipeVersion.objects.filter(recipe=recipe, status=RecipeVersionStatus.DRAFT).count()
            == 1
        )

    def test_the_version_numbers_never_collide(self, world: World) -> None:
        """
        The Task 3.2 §B.1 race, restated for the lifecycle: the allocator is a
        locked read-modify-write on the recipe row, so the surviving version's
        number is the recipe's own high-water mark.
        """
        recipe = world.recipe
        people = world.people

        def work(index: int) -> None:
            create_draft_recipe_version(
                recipe=recipe,
                expected_output_quantity=Decimal("10"),
                output_unit=world.kilogram,
                created_by=people["author"],
            )

        race(work)

        recipe.refresh_from_db()
        versions = list(RecipeVersion.objects.filter(recipe=recipe))
        assert len(versions) == 1
        assert recipe.last_version_number == versions[0].version_number


class TestTwoReviewsOfOneType:
    def test_only_one_signature_per_review_type_commits(self, world: World) -> None:
        version = make_draft(world)
        people = world.people
        submit_recipe_version(version=version, actor=people["author"])
        reviewers = [people["cook"], people["approver"]]

        def work(index: int) -> None:
            record_recipe_version_review(
                version=version,
                review_type=RecipeReviewType.KITCHEN,
                reviewer=reviewers[index],
                decision=RecipeReviewDecision.APPROVED,
            )

        errors = race(work)

        assert survivors(errors) == 1
        assert version.reviews.filter(review_type=RecipeReviewType.KITCHEN).count() == 1
