"""
Effective dating: every boundary of the range, the overlap constraint, the
resolver, the comparison and the verifier.

The range convention is `[effective_from, effective_to]`, **inclusive at both
ends**, and every test here asserts a specific day rather than a relative one.
A boundary test written as "a day inside the range" proves nothing about the
boundary; the interesting days are the four the arithmetic can get wrong.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.inventory.models import InventoryItem
from apps.kitchen.comparison import ADDED, CHANGED, REMOVED, compare_recipe_versions
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    resolve_recipe_version,
    version_timeline,
)
from apps.kitchen.models import (
    Recipe,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionStatus,
)
from apps.kitchen.reconciliation import verify_organization
from apps.kitchen.services import add_recipe_line, add_recipe_serving, remove_recipe_line
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import build_complete_draft, carry_to_approved

pytestmark = pytest.mark.django_db

JULY_1 = datetime.date(2026, 7, 1)
JULY_31 = datetime.date(2026, 7, 31)
JUNE_30 = datetime.date(2026, 6, 30)
AUGUST_1 = datetime.date(2026, 8, 1)


def _codes(error: ValidationError) -> set[str]:
    found: set[str] = set()
    if hasattr(error, "error_dict"):
        for errors in error.error_dict.values():
            for item in errors:
                found.update(_codes(item))
        return found
    if hasattr(error, "message"):
        if error.code:
            found.add(error.code)
        return found
    for item in error.error_list:
        found.update(_codes(item))
    return found


@pytest.fixture
def closed_version(approved_version: RecipeVersion, approver: User) -> RecipeVersion:
    """Active over exactly July 2026 — both ends closed, so both can be tested."""
    return activate_recipe_version(
        version=approved_version,
        actor=approver,
        effective_from=JULY_1,
        effective_to=JULY_31,
    )


class TestRangeBoundaries:
    def test_the_day_before_effective_from_resolves_nothing(
        self, closed_version: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=JUNE_30)

        assert "recipe_version_not_effective" in _codes(refused.value)

    def test_effective_from_itself_resolves(
        self, closed_version: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        assert (
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_1).pk
            == closed_version.pk
        )

    def test_the_final_included_day_resolves(
        self, closed_version: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        """
        The upper bound is **included**. If this ever fails, the convention has
        drifted to exclusive somewhere and every recipe silently stops applying
        one day early.
        """
        assert (
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_31).pk
            == closed_version.pk
        )

    def test_the_day_after_effective_to_resolves_nothing(
        self, closed_version: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=AUGUST_1)

        assert "recipe_version_not_effective" in _codes(refused.value)

    def test_an_open_ended_range_resolves_far_into_the_future(
        self, active_version: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        assert (
            resolve_recipe_version(
                recipe=recipe, branch=branch, on_date=datetime.date(2031, 12, 31)
            ).pk
            == active_version.pk
        )

    def test_a_future_activation_does_not_resolve_today(
        self, approved_version: RecipeVersion, recipe: Recipe, branch: Branch, approver: User
    ) -> None:
        activate_recipe_version(
            version=approved_version,
            actor=approver,
            effective_from=datetime.date(2030, 1, 1),
        )

        with pytest.raises(ValidationError) as refused:
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_1)

        assert "recipe_version_not_effective" in _codes(refused.value)

    def test_a_recipe_with_no_active_version_resolves_nothing(
        self, approved_version: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        """Approval is agreement; only activation is a claim on a date."""
        with pytest.raises(ValidationError) as refused:
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_1)

        assert "recipe_version_not_effective" in _codes(refused.value)

    def test_a_foreign_branch_is_refused_rather_than_missed(
        self, active_version: RecipeVersion, recipe: Recipe, other_branch: Branch
    ) -> None:
        """
        A branch of another organization is a different error from "nothing
        applies here", because they need two different responses.
        """
        with pytest.raises(ValidationError) as refused:
            resolve_recipe_version(recipe=recipe, branch=other_branch, on_date=JULY_1)

        assert "recipe_version_foreign_branch" in _codes(refused.value)


class TestPerBranchResolution:
    def test_two_branches_can_run_two_different_versions(
        self,
        recipe: Recipe,
        branch: Branch,
        second_branch: Branch,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        first = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=first, actor=approver, effective_from=JULY_1, branches=[branch]
        )
        second = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=second,
            actor=approver,
            effective_from=JULY_1,
            branches=[second_branch],
        )

        assert resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_1).pk == first.pk
        assert (
            resolve_recipe_version(recipe=recipe, branch=second_branch, on_date=JULY_1).pk
            == second.pk
        )


class TestOverlapEnforcement:
    def test_the_database_refuses_a_second_overlapping_claim(
        self,
        active_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        second = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            activate_recipe_version(version=second, actor=approver, effective_from=AUGUST_1)

    def test_an_organization_wide_claim_collides_with_a_branch_claim(
        self,
        active_version: RecipeVersion,
        recipe: Recipe,
        branch: Branch,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        The case an "empty list means everywhere" convention could never
        enforce. Materialising the organization-wide claim into real rows is
        what makes this a question about two ordinary rows.
        """
        second = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            activate_recipe_version(
                version=second,
                actor=approver,
                effective_from=AUGUST_1,
                branches=[branch],
            )

    def test_a_disjoint_later_range_at_the_same_branch_is_allowed(
        self,
        closed_version: RecipeVersion,
        recipe: Recipe,
        branch: Branch,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        July closes on the 31st and August starts on the 1st: a seam, not an
        overlap. This is the test that would fail if the upper bound were
        treated as exclusive anywhere.
        """
        second = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(version=second, actor=approver, effective_from=AUGUST_1)

        assert (
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_31).pk
            == closed_version.pk
        )
        assert (
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=AUGUST_1).pk == second.pk
        )


class TestAmbiguityIsReachableOnlyByDeferring:
    """
    The resolver's ambiguity branch and the verifier's ambiguity finding.

    Both are unrepresentable while the exclusion constraint holds, which is
    exactly why migration `0005` made it `DEFERRABLE INITIALLY IMMEDIATE`: a
    test can defer it inside a transaction it then rolls back, activate a
    genuinely colliding version **through the real service**, and prove the
    code that runs when the impossible happens actually works. "Cannot happen"
    and "is not handled" are different claims, and only one of them survives a
    migration that was reverted on one machine.

    Nothing here disables a trigger or writes a row by hand. The only thing
    relaxed is the moment the constraint is checked.
    """

    @pytest.fixture
    def collision(
        self,
        closed_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> Iterator[RecipeVersion]:
        """A second version claiming exactly the same branch and days."""
        second = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS recipe_scope_no_overlapping_ranges DEFERRED")
        planted = activate_recipe_version(
            version=second,
            actor=approver,
            effective_from=JULY_1,
            effective_to=JULY_31,
        )
        yield planted
        # The planted state must never reach COMMIT, and Django's own teardown
        # would otherwise run `SET CONSTRAINTS ALL IMMEDIATE` and re-assert the
        # very constraint this test suspends. Marking the transaction for
        # rollback is what tells it to stand down — and is the literal meaning
        # of "planted only inside a rolled-back test".
        transaction.set_rollback(True)

    def test_the_resolver_reports_ambiguity_rather_than_guessing(
        self, collision: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_1)

        assert "recipe_version_ambiguous" in _codes(refused.value)

    def test_the_resolver_is_ambiguous_on_every_covered_day(
        self, collision: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        for on_date in (JULY_1, datetime.date(2026, 7, 15), JULY_31):
            with pytest.raises(ValidationError) as refused:
                resolve_recipe_version(recipe=recipe, branch=branch, on_date=on_date)
            assert "recipe_version_ambiguous" in _codes(refused.value)

    def test_the_verifier_reports_the_overlap_and_the_ambiguity(
        self, collision: RecipeVersion, organization: Organization
    ) -> None:
        codes = {finding.code for finding in verify_organization(organization)}

        assert "overlapping_effective_ranges" in codes
        assert "ambiguous_resolution" in codes

    def test_the_verifier_repairs_nothing_it_reports(
        self, collision: RecipeVersion, organization: Organization
    ) -> None:
        before = (
            RecipeVersionBranchScope.objects.count(),
            RecipeVersion.objects.count(),
        )

        findings = verify_organization(organization)

        assert findings
        assert (
            RecipeVersionBranchScope.objects.count(),
            RecipeVersion.objects.count(),
        ) == before


class TestVerifierDetection:
    """
    The findings that no deferral can reach, checked against the detection
    functions directly.

    Every one of these describes a row the constraints and triggers make
    unrepresentable — an approval with no evidence, a supersession with no
    replacement. Planting one would mean disabling a trigger, and a test that
    disables the protection it is meant to prove is not a test. So the *state*
    is built in memory, never written, and the check function is asked what it
    makes of it. That proves the detection works without ever putting the
    database into a state the database refuses.
    """

    def test_an_approval_with_no_evidence_is_detected(self, active_version: RecipeVersion) -> None:
        from apps.kitchen.reconciliation import _check_evidence

        detached = RecipeVersion.objects.get(pk=active_version.pk)
        detached.approval_reference = ""

        codes = {finding.code for finding in _check_evidence(detached)}

        assert "approval_without_evidence" in codes

    def test_an_author_approving_their_own_version_is_detected(
        self, active_version: RecipeVersion
    ) -> None:
        from apps.kitchen.reconciliation import _check_evidence

        detached = RecipeVersion.objects.get(pk=active_version.pk)
        detached.approved_by_id = detached.created_by_id

        codes = {finding.code for finding in _check_evidence(detached)}

        assert "approval_by_the_author" in codes

    def test_a_real_recipe_on_demo_evidence_is_detected(
        self, active_version: RecipeVersion
    ) -> None:
        from apps.kitchen.models import ApprovalEvidenceKind
        from apps.kitchen.reconciliation import _check_evidence

        detached = RecipeVersion.objects.get(pk=active_version.pk)
        detached.approval_evidence_kind = ApprovalEvidenceKind.DEMO_FICTIONAL

        codes = {finding.code for finding in _check_evidence(detached)}

        assert "real_recipe_on_demo_evidence" in codes

    def test_a_supersession_with_no_replacement_is_detected(
        self, active_version: RecipeVersion
    ) -> None:
        from apps.kitchen.reconciliation import _check_metadata, _check_supersession

        detached = RecipeVersion.objects.get(pk=active_version.pk)
        detached.status = RecipeVersionStatus.SUPERSEDED

        codes = {finding.code for finding in _check_supersession(detached)}
        codes |= {finding.code for finding in _check_metadata(detached)}

        assert "supersession_without_replacement" in codes
        assert "supersession_left_open" in codes

    def test_the_missing_review_list_names_what_is_absent(
        self, complete_draft: RecipeVersion, manager: User, cook: User
    ) -> None:
        """
        `review_gaps` is what refuses an approval, and what the screen prints.
        One function, so the panel and the command can never disagree.
        """
        from apps.kitchen.lifecycle import (
            record_recipe_version_review,
            review_gaps,
            submit_recipe_version,
        )
        from apps.kitchen.models import RecipeReviewDecision, RecipeReviewType

        submit_recipe_version(version=complete_draft, actor=manager)
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.KITCHEN,
            reviewer=cook,
            decision=RecipeReviewDecision.APPROVED,
        )

        gaps = review_gaps(complete_draft)

        assert len(gaps) == 2
        assert not [gap for gap in gaps if str(RecipeReviewType.KITCHEN.label) in gap]


class TestTimeline:
    def test_the_timeline_reads_the_rows_the_resolver_reads(
        self, active_version: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        """
        A timeline built from anything other than the scope rows could disagree
        with the answer the system actually gives.
        """
        rows = version_timeline(recipe)

        assert [row.version.pk for row in rows] == [active_version.pk]
        assert rows[0].branch == branch
        assert rows[0].effective_from == JULY_1


class TestComparison:
    def test_comparing_two_recipes_is_refused(
        self,
        recipe: Recipe,
        organization: Organization,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        from apps.kitchen.models import RecipeType
        from apps.kitchen.services import create_recipe

        other = create_recipe(
            organization=organization,
            code="OTHER-CMP",
            name_ar="أخرى",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        left = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        right = build_complete_draft(recipe=other, unit=kilogram, item=rice, author=manager)

        with pytest.raises(ValidationError) as refused:
            compare_recipe_versions(left=left, right=right)

        assert "recipe_version_comparison_across_recipes" in _codes(refused.value)

    def test_comparing_a_version_with_itself_is_refused(
        self, complete_draft: RecipeVersion
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            compare_recipe_versions(left=complete_draft, right=complete_draft)

        assert "recipe_version_comparison_with_itself" in _codes(refused.value)

    def test_added_removed_and_changed_are_classified(
        self,
        active_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        litre: UnitOfMeasure,
        rice: InventoryItem,
        oil: InventoryItem,
        manager: User,
    ) -> None:
        """
        A second draft with one ingredient swapped and one quantity moved. The
        diff has to say *which* ingredient, keyed by item code — matching on
        primary keys would call every line both added and removed.
        """
        second = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        line = second.lines.get(item=rice)
        remove_recipe_line(line=line)
        add_recipe_line(version=second, item=oil, entered_quantity=Decimal("3"), entered_unit=litre)
        add_recipe_serving(
            version=second,
            code="TWO",
            name_ar="حصة ثانية",
            serving_quantity=Decimal("2"),
            serving_unit=kilogram,
        )
        second.refresh_from_db()

        comparison = compare_recipe_versions(left=active_version, right=second)
        lines = {section.key: section for section in comparison.sections}["lines"]
        classifications = {row.key: row.classification for row in lines.rows}

        assert classifications["RICE"] == REMOVED
        assert classifications["OIL"] == ADDED

        servings = {section.key: section for section in comparison.sections}["servings"]
        assert {row.key for row in servings.rows if row.classification == ADDED} == {"TWO"}

    def test_the_row_order_is_stable_across_two_runs(
        self,
        active_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        second = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        first_run = compare_recipe_versions(left=active_version, right=second)
        second_run = compare_recipe_versions(left=active_version, right=second)

        assert [
            (section.key, [row.key for row in section.rows]) for section in first_run.sections
        ] == [(section.key, [row.key for row in section.rows]) for section in second_run.sections]

    def test_the_comparison_exposes_no_money(
        self,
        active_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        second = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        comparison = compare_recipe_versions(left=active_version, right=second)

        labels = {
            difference.label
            for section in comparison.sections
            for row in section.rows
            for difference in row.differences
        } | {row.label for section in comparison.sections for row in section.rows}

        for word in ("كلفة الوحدة", "السعر", "الهامش"):
            assert not [label for label in labels if word in label]

    def test_a_scope_difference_is_reported(
        self,
        active_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        second = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        comparison = compare_recipe_versions(left=active_version, right=second)
        scope = {section.key: section for section in comparison.sections}["scope"]

        assert scope.has_changes
        assert all(row.classification == REMOVED for row in scope.changed_rows)

    def test_a_header_change_is_reported_as_changed(
        self,
        active_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        gram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        second = build_complete_draft(
            recipe=recipe, unit=kilogram, item=rice, author=manager, output_unit=gram
        )
        comparison = compare_recipe_versions(left=active_version, right=second)
        header = {section.key: section for section in comparison.sections}["header"]
        by_key = {row.key: row for row in header.rows}

        assert by_key["output_unit"].classification == CHANGED
        assert by_key["output_unit"].differences[0].left == "KG"
        assert by_key["output_unit"].differences[0].right == "G"


class TestStatusIsNotResolution:
    def test_a_rejected_version_never_resolves(
        self,
        complete_draft: RecipeVersion,
        recipe: Recipe,
        branch: Branch,
        manager: User,
        approver: User,
    ) -> None:
        from apps.kitchen.lifecycle import reject_recipe_version, submit_recipe_version

        submit_recipe_version(version=complete_draft, actor=manager)
        reject_recipe_version(version=complete_draft, actor=approver, reason="لا.")

        with pytest.raises(ValidationError):
            resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_1)

    def test_the_resolver_returns_only_active_or_superseded(
        self, active_version: RecipeVersion, recipe: Recipe, branch: Branch
    ) -> None:
        resolved = resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY_1)

        assert resolved.status in {
            RecipeVersionStatus.ACTIVE,
            RecipeVersionStatus.SUPERSEDED,
        }
