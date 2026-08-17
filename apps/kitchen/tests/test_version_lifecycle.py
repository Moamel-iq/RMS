"""
The lifecycle itself: submission, review, approval, refusal, effect and
supersession.

Every test here goes through the command services rather than assigning a
status, because assigning a status is exactly what no caller may do and a test
that did it would be proving something about a different system.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounting.models import JournalEntry, JournalLine
from apps.inventory.models import InventoryItem, StockBalance, StockMovement
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    applicable_branches,
    approve_recipe_version,
    record_recipe_version_review,
    reject_recipe_version,
    resolve_recipe_version,
    submission_problems,
    submit_recipe_version,
    supersede_recipe_version,
)
from apps.kitchen.models import (
    ApprovalEvidenceKind,
    Recipe,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionReview,
    RecipeVersionStatus,
)
from apps.kitchen.services import (
    create_recipe,
    set_recipe_branches,
    update_draft_recipe_version,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import build_complete_draft, carry_to_approved

pytestmark = pytest.mark.django_db

JULY = datetime.date(2026, 7, 1)
AUGUST = datetime.date(2026, 8, 1)
REFERENCE = "KM-RCP-004/2026/07"


def _codes(error: ValidationError) -> set[str]:
    """
    Every stable code inside a raised ValidationError, however it nested.

    The leaf test is `message`, not `error_list`: a single-message
    `ValidationError` carries **both**, and its `error_list` is `[itself]`, so
    recursing on the list first would loop straight past the code.
    """
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


class TestSubmission:
    def test_a_complete_draft_submits(self, complete_draft: RecipeVersion, manager: User) -> None:
        version = submit_recipe_version(version=complete_draft, actor=manager)

        assert version.status == RecipeVersionStatus.SUBMITTED
        assert version.submitted_by == manager
        assert version.submitted_at is not None

    def test_a_draft_with_no_line_cannot_be_submitted(
        self, draft: RecipeVersion, manager: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            submit_recipe_version(version=draft, actor=manager)

        assert "recipe_version_incomplete" in _codes(refused.value)
        draft.refresh_from_db()
        assert draft.status == RecipeVersionStatus.DRAFT

    def test_the_panel_and_the_command_refuse_on_the_same_list(
        self, draft: RecipeVersion, manager: User
    ) -> None:
        """
        The completeness panel must never say "ready" to something submission
        would refuse. Sharing the function is what guarantees it.
        """
        problems = submission_problems(draft)
        assert problems

        with pytest.raises(ValidationError) as refused:
            submit_recipe_version(version=draft, actor=manager)

        assert set(refused.value.messages) == set(problems)

    def test_a_batch_recipe_needs_a_convertible_output_unit(
        self,
        organization: Organization,
        cooked_rice: InventoryItem,
        rice: InventoryItem,
        kilogram: UnitOfMeasure,
        litre: UnitOfMeasure,
        manager: User,
    ) -> None:
        """
        The KD-19 refusal, at the approval boundary: a version whose output is
        litres of an item measured in kilograms is refused, not guessed.
        """
        recipe = create_recipe(
            organization=organization,
            code="BATCH-1",
            name_ar="دفعة",
            recipe_type=RecipeType.BATCH,
            output_item=cooked_rice,
            created_by=manager,
        )
        version = build_complete_draft(
            recipe=recipe,
            unit=kilogram,
            item=rice,
            author=manager,
            output_unit=litre,
        )

        problems = submission_problems(version)

        assert any("وحدة الناتج" in problem for problem in problems)

    def test_an_archived_recipe_cannot_submit(
        self, complete_draft: RecipeVersion, manager: User
    ) -> None:
        Recipe.objects.filter(pk=complete_draft.recipe_id).update(is_active=False)
        complete_draft.refresh_from_db()

        with pytest.raises(ValidationError):
            submit_recipe_version(version=complete_draft, actor=manager)

    def test_a_submitted_version_cannot_be_submitted_again(
        self, complete_draft: RecipeVersion, manager: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)

        with pytest.raises(ValidationError) as refused:
            submit_recipe_version(version=complete_draft, actor=manager)

        assert "recipe_version_illegal_transition" in _codes(refused.value)

    def test_a_stale_draft_instance_cannot_edit_a_submitted_version(
        self, complete_draft: RecipeVersion, manager: User, kilogram: UnitOfMeasure
    ) -> None:
        """
        The caller holds the object from before submission. The row is what
        decides, and it now refuses.
        """
        stale = RecipeVersion.objects.get(pk=complete_draft.pk)
        submit_recipe_version(version=complete_draft, actor=manager)

        with pytest.raises(ValidationError):
            update_draft_recipe_version(
                version=stale,
                expected_output_quantity=Decimal("99"),
                output_unit=kilogram,
            )


class TestReviewEvidence:
    def test_the_three_reviews_are_recorded(
        self,
        complete_draft: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        for review_type, reviewer in (
            (RecipeReviewType.KITCHEN, cook),
            (RecipeReviewType.STOREKEEPER, keeper),
        ):
            record_recipe_version_review(
                version=complete_draft,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
            )
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=accountant,
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )

        assert complete_draft.reviews.count() == 3

    def test_a_review_cannot_be_recorded_on_a_draft(
        self, complete_draft: RecipeVersion, cook: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            record_recipe_version_review(
                version=complete_draft,
                review_type=RecipeReviewType.KITCHEN,
                reviewer=cook,
                decision=RecipeReviewDecision.APPROVED,
            )

        assert "recipe_version_illegal_transition" in _codes(refused.value)

    def test_the_costing_review_must_name_its_evidence(
        self, complete_draft: RecipeVersion, manager: User, accountant: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)

        with pytest.raises(ValidationError) as refused:
            record_recipe_version_review(
                version=complete_draft,
                review_type=RecipeReviewType.ACCOUNTING,
                reviewer=accountant,
                decision=RecipeReviewDecision.APPROVED,
            )

        assert "recipe_review_evidence_required" in _codes(refused.value)

    def test_a_refusal_needs_a_reason(
        self, complete_draft: RecipeVersion, manager: User, keeper: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)

        with pytest.raises(ValidationError) as refused:
            record_recipe_version_review(
                version=complete_draft,
                review_type=RecipeReviewType.STOREKEEPER,
                reviewer=keeper,
                decision=RecipeReviewDecision.REJECTED,
            )

        assert "recipe_review_reason_required" in _codes(refused.value)

    def test_one_person_cannot_sign_both_the_kitchen_and_the_costing_review(
        self, complete_draft: RecipeVersion, manager: User, cook: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.KITCHEN,
            reviewer=cook,
            decision=RecipeReviewDecision.APPROVED,
        )

        with pytest.raises(ValidationError) as refused:
            record_recipe_version_review(
                version=complete_draft,
                review_type=RecipeReviewType.ACCOUNTING,
                reviewer=cook,
                decision=RecipeReviewDecision.APPROVED,
                evidence_reference=REFERENCE,
                evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
            )

        assert "recipe_review_incompatible_roles" in _codes(refused.value)

    def test_the_same_review_cannot_be_recorded_twice(
        self, complete_draft: RecipeVersion, manager: User, cook: User, approver: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.KITCHEN,
            reviewer=cook,
            decision=RecipeReviewDecision.APPROVED,
        )

        with pytest.raises(ValidationError) as refused:
            record_recipe_version_review(
                version=complete_draft,
                review_type=RecipeReviewType.KITCHEN,
                reviewer=approver,
                decision=RecipeReviewDecision.APPROVED,
            )

        assert "recipe_review_already_recorded" in _codes(refused.value)

    def test_the_final_review_type_is_not_offered_to_the_review_command(
        self, complete_draft: RecipeVersion, manager: User, approver: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)

        with pytest.raises(ValidationError) as refused:
            record_recipe_version_review(
                version=complete_draft,
                review_type=RecipeReviewType.FINAL,
                reviewer=approver,
                decision=RecipeReviewDecision.APPROVED,
            )

        assert "recipe_review_final_is_not_a_review" in _codes(refused.value)


class TestApproval:
    def test_approval_records_the_actor_the_evidence_and_a_final_signoff(
        self, approved_version: RecipeVersion, approver: User
    ) -> None:
        assert approved_version.status == RecipeVersionStatus.APPROVED
        assert approved_version.approved_by == approver
        assert approved_version.approval_reference == REFERENCE
        assert approved_version.approval_evidence_kind == ApprovalEvidenceKind.SIGNED_FORM

        final = approved_version.reviews.get(review_type=RecipeReviewType.FINAL)
        assert final.reviewer == approver
        assert final.decision == RecipeReviewDecision.APPROVED

    def test_approval_does_not_make_a_version_effective(
        self, approved_version: RecipeVersion, branch: Branch
    ) -> None:
        assert approved_version.effective_from is None
        assert not approved_version.branch_scopes.exists()

        with pytest.raises(ValidationError) as refused:
            resolve_recipe_version(recipe=approved_version.recipe, branch=branch, on_date=JULY)

        assert "recipe_version_not_effective" in _codes(refused.value)

    @pytest.mark.parametrize(
        "missing", list(REQUIRED_REVIEW_TYPES := ("KITCHEN", "STOREKEEPER", "ACCOUNTING"))
    )
    def test_approval_refuses_while_any_required_review_is_missing(
        self,
        missing: str,
        complete_draft: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        present = {
            RecipeReviewType.KITCHEN: cook,
            RecipeReviewType.STOREKEEPER: keeper,
            RecipeReviewType.ACCOUNTING: accountant,
        }
        for review_type, reviewer in present.items():
            if review_type == missing:
                continue
            record_recipe_version_review(
                version=complete_draft,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
                evidence_reference=(
                    REFERENCE if review_type == RecipeReviewType.ACCOUNTING else ""
                ),
                evidence_kind=(
                    ApprovalEvidenceKind.SIGNED_FORM
                    if review_type == RecipeReviewType.ACCOUNTING
                    else ""
                ),
            )

        with pytest.raises(ValidationError) as refused:
            approve_recipe_version(
                version=complete_draft,
                actor=approver,
                approval_reference=REFERENCE,
                approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
            )

        assert "recipe_version_review_missing" in _codes(refused.value)

    def test_a_refused_review_blocks_approval(
        self,
        complete_draft: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.KITCHEN,
            reviewer=cook,
            decision=RecipeReviewDecision.APPROVED,
        )
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.STOREKEEPER,
            reviewer=keeper,
            decision=RecipeReviewDecision.REJECTED,
            reason="الوحدة على البطاقة لا تطابق وحدة الصنف.",
        )
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=accountant,
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )

        with pytest.raises(ValidationError) as refused:
            approve_recipe_version(
                version=complete_draft,
                actor=approver,
                approval_reference=REFERENCE,
                approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
            )

        assert "recipe_version_review_missing" in _codes(refused.value)

    def test_the_author_cannot_approve_their_own_version(
        self,
        complete_draft: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        for review_type, reviewer in (
            (RecipeReviewType.KITCHEN, cook),
            (RecipeReviewType.STOREKEEPER, keeper),
        ):
            record_recipe_version_review(
                version=complete_draft,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
            )
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=accountant,
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )

        with pytest.raises(ValidationError) as refused:
            approve_recipe_version(
                version=complete_draft,
                actor=manager,
                approval_reference=REFERENCE,
                approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
            )

        assert "recipe_version_approver_is_the_author" in _codes(refused.value)

    def test_a_reviewer_cannot_give_the_final_approval(
        self,
        complete_draft: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        for review_type, reviewer in (
            (RecipeReviewType.KITCHEN, cook),
            (RecipeReviewType.STOREKEEPER, keeper),
        ):
            record_recipe_version_review(
                version=complete_draft,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
            )
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=accountant,
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )

        with pytest.raises(ValidationError) as refused:
            approve_recipe_version(
                version=complete_draft,
                actor=cook,
                approval_reference=REFERENCE,
                approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
            )

        assert "recipe_version_approver_is_a_reviewer" in _codes(refused.value)

    def test_approval_needs_a_reference(
        self,
        complete_draft: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        for review_type, reviewer in (
            (RecipeReviewType.KITCHEN, cook),
            (RecipeReviewType.STOREKEEPER, keeper),
        ):
            record_recipe_version_review(
                version=complete_draft,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
            )
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=accountant,
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )

        with pytest.raises(ValidationError) as refused:
            approve_recipe_version(
                version=complete_draft,
                actor=approver,
                approval_reference="   ",
                approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
            )

        assert "recipe_version_approval_reference_required" in _codes(refused.value)

    def test_a_real_recipe_cannot_be_approved_on_fictional_evidence(
        self,
        complete_draft: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """KD-02 at the approval boundary, and RCP-126 at the same time."""
        submit_recipe_version(version=complete_draft, actor=manager)
        for review_type, reviewer in (
            (RecipeReviewType.KITCHEN, cook),
            (RecipeReviewType.STOREKEEPER, keeper),
        ):
            record_recipe_version_review(
                version=complete_draft,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
            )
        record_recipe_version_review(
            version=complete_draft,
            review_type=RecipeReviewType.ACCOUNTING,
            reviewer=accountant,
            decision=RecipeReviewDecision.APPROVED,
            evidence_reference=REFERENCE,
            evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
        )

        with pytest.raises(ValidationError) as refused:
            approve_recipe_version(
                version=complete_draft,
                actor=approver,
                approval_reference="DEMO",
                approval_evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
            )

        assert "recipe_evidence_outside_demo_namespace" in _codes(refused.value)


class TestRefusal:
    def test_rejection_keeps_the_row_with_its_reason(
        self, complete_draft: RecipeVersion, manager: User, approver: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)

        version = reject_recipe_version(
            version=complete_draft, actor=approver, reason="الكميات غير مؤكدة."
        )

        assert version.status == RecipeVersionStatus.REJECTED
        assert version.rejected_by == approver
        assert version.rejection_reason == "الكميات غير مؤكدة."
        assert RecipeVersion.objects.filter(pk=version.pk).exists()

    def test_rejection_needs_a_reason(
        self, complete_draft: RecipeVersion, manager: User, approver: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)

        with pytest.raises(ValidationError) as refused:
            reject_recipe_version(version=complete_draft, actor=approver, reason="  ")

        assert "recipe_version_rejection_reason_required" in _codes(refused.value)

    def test_a_rejected_version_cannot_be_approved(
        self, complete_draft: RecipeVersion, manager: User, approver: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        reject_recipe_version(version=complete_draft, actor=approver, reason="لا.")

        with pytest.raises(ValidationError) as refused:
            approve_recipe_version(
                version=complete_draft,
                actor=approver,
                approval_reference=REFERENCE,
                approval_evidence_kind=ApprovalEvidenceKind.SIGNED_FORM,
            )

        assert "recipe_version_illegal_transition" in _codes(refused.value)

    def test_a_rejected_version_cannot_return_to_draft(
        self, complete_draft: RecipeVersion, manager: User, approver: User, kilogram: UnitOfMeasure
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)
        reject_recipe_version(version=complete_draft, actor=approver, reason="لا.")
        complete_draft.refresh_from_db()

        with pytest.raises(ValidationError):
            update_draft_recipe_version(
                version=complete_draft,
                expected_output_quantity=Decimal("5"),
                output_unit=kilogram,
            )

    def test_a_new_version_may_be_drafted_after_a_rejection(
        self,
        complete_draft: RecipeVersion,
        recipe: Recipe,
        manager: User,
        approver: User,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
    ) -> None:
        """Correction is a new version, which is what §C says it must be."""
        submit_recipe_version(version=complete_draft, actor=manager)
        reject_recipe_version(version=complete_draft, actor=approver, reason="لا.")

        replacement = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)

        assert replacement.version_number == complete_draft.version_number + 1


class TestEffect:
    def test_activation_claims_every_applicable_branch(
        self, approved_version: RecipeVersion, approver: User, branch: Branch
    ) -> None:
        version = activate_recipe_version(
            version=approved_version, actor=approver, effective_from=JULY
        )

        assert version.status == RecipeVersionStatus.ACTIVE
        assert version.effective_from == JULY
        assert version.effective_to is None
        scopes = list(version.branch_scopes.all())
        assert [scope.branch_id for scope in scopes] == [branch.pk]
        assert all(scope.is_organization_wide for scope in scopes)

    def test_activation_can_name_one_branch(
        self,
        approved_version: RecipeVersion,
        approver: User,
        branch: Branch,
        second_branch: Branch,
    ) -> None:
        version = activate_recipe_version(
            version=approved_version,
            actor=approver,
            effective_from=JULY,
            branches=[second_branch],
        )

        claimed = {scope.branch_id for scope in version.branch_scopes.all()}
        assert claimed == {second_branch.pk}
        assert not version.branch_scopes.filter(is_organization_wide=True).exists()

    def test_a_foreign_branch_is_refused(
        self, approved_version: RecipeVersion, approver: User, other_branch: Branch
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            activate_recipe_version(
                version=approved_version,
                actor=approver,
                effective_from=JULY,
                branches=[other_branch],
            )

        assert "recipe_version_foreign_branch" in _codes(refused.value)

    def test_a_branch_outside_the_recipe_applicability_is_refused(
        self,
        recipe: Recipe,
        complete_draft: RecipeVersion,
        branch: Branch,
        second_branch: Branch,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        set_recipe_branches(recipe=recipe, branches=[branch])
        version = carry_to_approved(
            complete_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

        with pytest.raises(ValidationError) as refused:
            activate_recipe_version(
                version=version,
                actor=approver,
                effective_from=JULY,
                branches=[second_branch],
            )

        assert "recipe_version_branch_outside_applicability" in _codes(refused.value)

    def test_an_organization_wide_activation_covers_every_active_branch(
        self,
        approved_version: RecipeVersion,
        approver: User,
        branch: Branch,
        second_branch: Branch,
    ) -> None:
        version = activate_recipe_version(
            version=approved_version, actor=approver, effective_from=JULY
        )

        claimed = {scope.branch_id for scope in version.branch_scopes.all()}
        assert claimed == {branch.pk, second_branch.pk}
        assert set(applicable_branches(version.recipe)) == {branch, second_branch}

    def test_an_inverted_range_is_refused(
        self, approved_version: RecipeVersion, approver: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            activate_recipe_version(
                version=approved_version,
                actor=approver,
                effective_from=AUGUST,
                effective_to=JULY,
            )

        assert "recipe_version_range_is_inverted" in _codes(refused.value)

    def test_a_submitted_version_cannot_be_activated(
        self, complete_draft: RecipeVersion, manager: User, approver: User
    ) -> None:
        submit_recipe_version(version=complete_draft, actor=manager)

        with pytest.raises(ValidationError) as refused:
            activate_recipe_version(version=complete_draft, actor=approver, effective_from=JULY)

        assert "recipe_version_illegal_transition" in _codes(refused.value)


class TestSupersession:
    def _second_version(
        self,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> RecipeVersion:
        draft = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        return carry_to_approved(
            draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

    def test_activation_over_an_open_range_is_refused_without_a_supersession(
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
        """
        The exclusion constraint's refusal is the correct answer: the operator
        has not said what happens to the version already in force.
        """
        second = self._second_version(
            recipe, kilogram, rice, manager, cook, keeper, accountant, approver
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            activate_recipe_version(version=second, actor=approver, effective_from=AUGUST)

    def test_supersession_closes_the_predecessor_at_the_seam(
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
        second = self._second_version(
            recipe, kilogram, rice, manager, cook, keeper, accountant, approver
        )

        activate_recipe_version(
            version=second,
            actor=approver,
            effective_from=AUGUST,
            supersedes=active_version,
        )

        active_version.refresh_from_db()
        assert active_version.status == RecipeVersionStatus.SUPERSEDED
        assert active_version.effective_to == datetime.date(2026, 7, 31)
        assert active_version.superseded_by_version_id == second.pk
        assert all(
            scope.effective_to == datetime.date(2026, 7, 31)
            for scope in active_version.branch_scopes.all()
        )

    def test_a_version_cannot_supersede_itself(
        self, active_version: RecipeVersion, approver: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            supersede_recipe_version(
                version=active_version, replacement=active_version, actor=approver
            )

        assert "recipe_version_supersedes_itself" in _codes(refused.value)

    def test_supersession_across_recipes_is_refused(
        self,
        active_version: RecipeVersion,
        organization: Organization,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        other_recipe = create_recipe(
            organization=organization,
            code="OTHER-1",
            name_ar="وصفة أخرى",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        other = carry_to_approved(
            build_complete_draft(recipe=other_recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(version=other, actor=approver, effective_from=AUGUST)

        with pytest.raises(ValidationError) as refused:
            supersede_recipe_version(version=active_version, replacement=other, actor=approver)

        assert "recipe_version_supersedes_another_recipe" in _codes(refused.value)

    def test_a_replacement_starting_before_the_predecessor_is_refused(
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
        second = self._second_version(
            recipe, kilogram, rice, manager, cook, keeper, accountant, approver
        )

        with pytest.raises(ValidationError) as refused:
            activate_recipe_version(
                version=second,
                actor=approver,
                effective_from=datetime.date(2026, 6, 1),
                supersedes=active_version,
            )

        assert "recipe_version_replacement_starts_too_early" in _codes(refused.value)

    def test_a_superseded_version_still_resolves_for_its_own_dates(
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
        The charter's rule, verbatim: a recipe changed in August must not
        silently change what July cost.
        """
        second = self._second_version(
            recipe, kilogram, rice, manager, cook, keeper, accountant, approver
        )
        activate_recipe_version(
            version=second,
            actor=approver,
            effective_from=AUGUST,
            supersedes=active_version,
        )

        july = resolve_recipe_version(
            recipe=recipe, branch=branch, on_date=datetime.date(2026, 7, 15)
        )
        august = resolve_recipe_version(
            recipe=recipe, branch=branch, on_date=datetime.date(2026, 8, 15)
        )

        assert july.pk == active_version.pk
        assert august.pk == second.pk


class TestZeroEffect:
    """
    The boundary, proved by counting rather than asserted.

    Every command in the lifecycle runs, and the ledger and the general ledger
    are identical afterwards. Where the module stops is asserted by a test
    rather than promised by a comment — and the assertion is updated when a task
    legitimately moves the fence, never when it would be convenient.
    """

    def _counts(self) -> tuple[int, ...]:
        return (
            StockMovement.objects.count(),
            StockBalance.objects.count(),
            JournalEntry.objects.count(),
            JournalLine.objects.count(),
        )

    def test_the_whole_lifecycle_moves_no_stock_and_posts_no_journal(
        self,
        complete_draft: RecipeVersion,
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
        before = self._counts()

        first = carry_to_approved(
            complete_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(version=first, actor=approver, effective_from=JULY)

        second = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=second, actor=approver, effective_from=AUGUST, supersedes=first
        )

        third = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        submit_recipe_version(version=third, actor=manager)
        reject_recipe_version(version=third, actor=approver, reason="لا.")

        resolve_recipe_version(recipe=recipe, branch=branch, on_date=JULY)

        assert self._counts() == before

    def test_the_module_stops_where_production_begins(self) -> None:
        """
        The boundary that is still true, asserted rather than commented.

        Task 3.2A held `RecipeComponent` out and **3.2B brought it in**; 3.2B
        held `RecipeCostSnapshot` out and **3.3 brought it in**. Each time this
        test was rewritten rather than deleted — the fence moved twice, and it
        did not come down. `ProductionBatch` is Task 3.5's, `ProductionBatchLine`
        is where flattening lands at Task 3.4, and neither may appear before its
        task.
        """
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("kitchen").get_models()}

        assert "RecipeComponent" in names, "Task 3.2B owns the nested-recipe graph"
        assert "RecipeCostSnapshot" in names, "Task 3.3 owns cost snapshots"
        assert "ProductionBatch" not in names
        assert "ProductionBatchLine" not in names

    def test_no_lifecycle_row_stores_a_cost_or_a_price(self) -> None:
        """
        RCP-009: a recipe carries no cost field, and Task 3.3 did not add one.

        **Concrete fields only.** A `RecipeVersion` now has a reverse accessor
        to its cost snapshots, and that is the opposite of the defect this test
        exists to catch: a snapshot is a separate append-only record of what the
        books said on a date, not a cached figure on the version that starts
        drifting the moment the next receipt posts. Relations are excluded and
        the stored columns are still checked exactly as before.
        """
        forbidden = ("cost", "price", "amount", "margin", "value")
        for model in (RecipeVersion, RecipeVersionReview, RecipeVersionBranchScope):
            for field in model._meta.get_fields():
                if field.is_relation:
                    continue
                name = getattr(field, "name", "")
                assert not any(word in name for word in forbidden), (
                    f"{model.__name__}.{name} looks like money; costing is derived"
                )
