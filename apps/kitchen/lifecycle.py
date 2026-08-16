"""
The recipe version lifecycle: submission, review, approval, effect and
supersession.

Separated from `services.py` because the two answer different questions.
`services.py` maintains a draft — what the recipe *is*. This module decides
when that draft becomes something the business may cost a meal against, and it
is the only place a version's status ever moves.

Every command follows the discipline the accounting kernel established, in this
order and no other:

    transaction.atomic()
      -> select_for_update() on the version, and on the recipe where a sibling
         has to be read consistently
      -> re-read the authoritative row; the argument is a memory, not a fact
      -> lifecycle validation  (is this transition legal from *this* status)
      -> structural validation (is the version complete)
      -> evidence validation   (is the control satisfied)
      -> range validation      (does the claim collide)
      -> the transition
      -> audit
      -> commit

**Locks are taken in one documented order**, and every command here obeys it:

    component graph  ->  Recipe  ->  RecipeVersion  ->  RecipeVersionBranchScope
    (advisory, per organization)     (both ascending id)

The **component graph lock comes first**, above every row lock, and is taken by
every command that certifies or changes the graph: submission, approval,
activation and supersession. Task 3.2B added it because a cycle is a
contradiction that lives *across* rows — `A → B` and `B → A` touch no row in
common, so no row lock can serialise them — and because certifying a graph while
somebody completes a cycle in it certifies the cycle. `graph.py` sets it out.

Any command that touches **two** versions — activation with a supersession,
and supersession itself — takes the `Recipe` row lock next. That is what makes
the order between two sibling versions irrelevant: every such command is
already serialised on the recipe, so two of them cannot each hold one version
and wait for the other. The commands that touch a single version take no recipe
lock, because there is nothing for them to race against.

**Nothing here posts.** No stock movement, no journal entry, no balance, no
cost. Approving a recipe is an agreement about a document; the production batch
of Task 3.5 is the event that moves value (RCP-002). Tests count the rows
before and after every command in this module rather than trusting that
sentence.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import ConversionType
from apps.inventory.selectors import effective_conversion
from apps.kitchen.graph import (
    lock_component_graph,
    require_effective_coverage,
    supersession_blockers,
    validate_version_graph,
)
from apps.kitchen.models import (
    APPROVED_VERSION_STATUSES,
    DEMO_CODE_PREFIX,
    REQUIRED_REVIEW_TYPES,
    RESOLVABLE_VERSION_STATUSES,
    ApprovalEvidenceKind,
    Recipe,
    RecipeLine,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionReview,
    RecipeVersionStatus,
)
from apps.organizations.models import Branch
from apps.units.services import convert
from apps.users.models import User

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise

#: A share total starts at nothing, in the same Decimal world the shares live
#: in — never `0`, which would drag an int into a Decimal sum.
ZERO = Decimal("0")

#: One day. Supersession closes the predecessor at the day *before* the
#: replacement begins, which is only a seam with no gap and no overlap because
#: the range convention is inclusive at both ends (RCP-016).
ONE_DAY = datetime.timedelta(days=1)


# ---------------------------------------------------------------------------
# Stable domain errors
# ---------------------------------------------------------------------------
#
# Every refusal in this module carries a `code`. A caller that has to branch on
# *which* refusal happened must not have to match on a translated Arabic
# sentence, and a report that counts refusals must not change its meaning when
# somebody improves the wording.


def _refuse(message: str | _StrPromise, code: str, field: str | None = None) -> ValidationError:
    if field is None:
        return ValidationError(message, code=code)
    return ValidationError({field: ValidationError(message, code=code)})


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def _organization_of(version_id: int) -> int:
    """
    Which organization's component-graph lock this command needs.

    An unlocked read that chooses a *lock key* and nothing else. A version's
    organization is immutable, so it cannot pick the wrong lock; if the row is
    gone, the authoritative re-read that follows is what refuses.
    """
    organization_id = (
        RecipeVersion.objects.filter(pk=version_id)
        .values_list("recipe__organization_id", flat=True)
        .first()
    )
    if organization_id is None:
        raise _refuse(_("النسخة لم تعد موجودة."), "recipe_version_missing")
    return int(organization_id)


def _lock_version(version_id: int, *, expected: frozenset[str] | set[str]) -> RecipeVersion:
    """
    Re-read a version under a row lock and refuse unless it is where the caller
    thinks it is.

    Takes an **id**, not an instance, on purpose. The instance a caller holds
    may be a memory from before somebody else approved, rejected or superseded
    the row, and a service that accepted it would be tempted to read a field
    off it. Every command in this module starts here, and none of them trusts a
    status it was handed.
    """
    current = (
        RecipeVersion.objects.select_for_update()
        .filter(pk=version_id)
        .select_related("recipe", "recipe__organization", "output_unit")
        .first()
    )
    if current is None:
        raise _refuse(_("النسخة لم تعد موجودة."), "recipe_version_missing")
    if current.status not in expected:
        raise _refuse(
            _("النسخة في حالة %(status)s ولا تقبل هذا الإجراء.")
            % {"status": current.get_status_display()},
            "recipe_version_illegal_transition",
        )
    return current


# ---------------------------------------------------------------------------
# Structural completeness — what "ready for review" means
# ---------------------------------------------------------------------------


def submission_problems(version: RecipeVersion) -> list[str]:
    """
    Everything that stops this draft being reviewable, in one pass.

    Collected rather than raised one at a time, because a chef who fixes four
    problems in four round trips learns to distrust the screen. The screen
    shows this list live; `submit_recipe_version` refuses on the same list, so
    the panel and the command can never disagree.

    Read-only. It resolves conversions and units but writes nothing.
    """
    problems: list[str] = []
    recipe = version.recipe

    if not recipe.is_active:
        problems.append(str(_("الوصفة مؤرشفة ولا تقبل نسخة جديدة.")))

    if version.batch_size <= 0:
        problems.append(str(_("حجم الدفعة يجب أن يكون أكبر من صفر.")))
    if version.expected_output_quantity <= 0:
        problems.append(str(_("الناتج المتوقع يجب أن يكون أكبر من صفر.")))

    problems.extend(_output_problems(version))
    problems.extend(_line_problems(version))
    problems.extend(_component_problems(version))
    problems.extend(_method_problems(version))
    problems.extend(_serving_problems(version))
    return problems


def _component_problems(version: RecipeVersion) -> list[str]:
    """
    Whatever the nested-recipe graph would refuse, as sentences for the panel.

    Runs the **same** check the command runs — `validate_version_graph` — and
    renders its refusal rather than re-implementing it. Two implementations of
    "is this graph acceptable" would eventually disagree, and the one on the
    screen would be the one that was wrong.

    Read-only and unlocked, which is correct here: this is the live panel a chef
    watches while typing. The authoritative check runs again under the graph
    lock inside `submit_recipe_version`.
    """
    try:
        validate_version_graph(version)
    except ValidationError as error:
        return [str(message) for message in error.messages]
    return []


def _output_problems(version: RecipeVersion) -> list[str]:
    """The output item and the output unit have to describe the same thing."""
    problems: list[str] = []
    recipe = version.recipe

    if recipe.recipe_type == RecipeType.BATCH:
        if recipe.output_item is None:
            problems.append(str(_("وصفة الدفعة يجب أن تنتج صنفاً مخزنياً.")))
        else:
            if recipe.output_item.organization_id != recipe.organization_id:
                problems.append(str(_("الصنف الناتج يتبع مؤسسة أخرى.")))
            if not recipe.output_item.is_active:
                problems.append(str(_("الصنف الناتج غير فعّال.")))
            try:
                convert(
                    version.expected_output_quantity,
                    from_unit=version.output_unit,
                    to_unit=recipe.output_item.base_unit,
                )
            except ValidationError:
                # The KD-19 refusal: mass against volume is not guessed.
                problems.append(str(_("وحدة الناتج لا تتحول إلى وحدة الصنف الناتج الأساسية.")))
    elif recipe.output_item is not None:
        problems.append(str(_("وصفة الحصة لا تنتج صنفاً مخزنياً.")))

    return problems


def _line_problems(version: RecipeVersion) -> list[str]:
    """Every ingredient row, checked the way approval will have to defend it."""
    problems: list[str] = []
    organization_id = version.recipe.organization_id
    lines = list(
        version.lines.select_related("item", "entered_unit", "package_unit").order_by("line_order")
    )
    if not lines:
        problems.append(str(_("النسخة بلا مكوّنات.")))
        return problems

    reference_date = version.effective_from or timezone.localdate()
    for line in lines:
        label = line.item.code
        if line.item.organization_id != organization_id:
            problems.append(str(_("المكوّن %(code)s يتبع مؤسسة أخرى.") % {"code": label}))
        if not line.item.is_active:
            problems.append(str(_("المكوّن %(code)s غير فعّال.") % {"code": label}))
        if line.base_quantity is None or line.base_quantity <= 0:
            problems.append(str(_("المكوّن %(code)s بلا كمية معتمدة.") % {"code": label}))
        problems.extend(_package_problems(line, label, reference_date))
        if bool(line.source_document) != (line.source_page is not None):
            problems.append(str(_("مصدر المكوّن %(code)s ناقص.") % {"code": label}))

    problems.extend(_substitute_problems(lines))
    return problems


def _package_problems(line: RecipeLine, label: str, reference_date: datetime.date) -> list[str]:
    """A package quantity has to still be re-derivable from its snapshot."""
    if line.package_unit is None:
        return []
    if line.conversion_factor is None:
        return [str(_("المكوّن %(code)s بعبوة بلا معامل تحويل محفوظ.") % {"code": label})]
    conversion = effective_conversion(
        item=line.item, package_unit=line.package_unit, on_date=reference_date
    )
    if (
        conversion is not None
        and conversion.conversion_type == ConversionType.VARIABLE
        and line.measured_quantity is None
    ):
        return [str(_("المكوّن %(code)s بعبوة متغيرة الوزن بلا كمية مقاسة.") % {"code": label})]
    return []


def _substitute_problems(lines: list[RecipeLine]) -> list[str]:
    """Ranked alternatives, and the ranking has to be an order."""
    problems: list[str] = []
    for line in lines:
        ranks = [
            substitute.priority for substitute in line.substitutes.all() if substitute.is_active
        ]
        if len(ranks) != len(set(ranks)):
            problems.append(
                str(_("بدائل المكوّن %(code)s تتشارك نفس الأولوية.") % {"code": line.item.code})
            )
        if any(rank <= 0 for rank in ranks):
            problems.append(
                str(_("بدائل المكوّن %(code)s تحمل أولوية غير موجبة.") % {"code": line.item.code})
            )
    return problems


def _method_problems(version: RecipeVersion) -> list[str]:
    """
    The method has to be captured as steps, not only as a paragraph.

    RCP-063: prose cannot be sequenced, checked off or diffed. A version whose
    only record of how the dish is made is an overview paragraph is a version
    whose method has not been captured, and approval is exactly where that
    stops being acceptable.
    """
    problems: list[str] = []
    if not version.instructions.strip():
        problems.append(str(_("النسخة بلا نظرة عامة على الطريقة.")))

    steps = list(version.steps.prefetch_related("ingredient_links").order_by("sequence"))
    if not steps:
        problems.append(str(_("النسخة بلا خطوات طريقة.")))

    # A line whose steps between them claim more than the whole quantity is
    # describing more of an ingredient than the recipe contains (RCP-067).
    shares: dict[int, Decimal] = {}
    for step in steps:
        for link in step.ingredient_links.all():
            shares[link.recipe_line_id] = shares.get(link.recipe_line_id, ZERO) + link.share
    over = [line_id for line_id, total in shares.items() if total > 1]
    if over:
        problems.append(str(_("مجموع حصص أحد المكوّنات عبر الخطوات يتجاوز الكمية الكاملة.")))
    return problems


def _serving_problems(version: RecipeVersion) -> list[str]:
    """Exactly one default answer to "what does one of these cost"."""
    problems: list[str] = []
    servings = list(version.servings.select_related("serving_unit"))
    active = [serving for serving in servings if serving.is_active]
    if not active:
        problems.append(str(_("النسخة بلا تعريف حصة.")))
        return problems
    primaries = [serving for serving in active if serving.is_primary]
    if len(primaries) != 1:
        problems.append(str(_("يجب أن يكون هناك تعريف حصة رئيسي واحد بالضبط.")))
    return problems


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


@transaction.atomic
def submit_recipe_version(*, version: RecipeVersion, actor: User) -> RecipeVersion:
    """
    Freeze a draft and open it for review.

    From here the structure is immutable — lines, substitutes, steps, links and
    servings all refuse insert, update and delete at the database — so every
    reviewer reads the same thing. That is the point of having a `SUBMITTED`
    state at all: reviewing a document somebody is still editing proves nothing
    about the document that eventually gets approved.

    A submission cannot be silently taken back. Returning to editing means
    rejecting this version and preparing the next one, which is §C's rule and
    the reason a rejection keeps its reason.

    Takes the component graph lock **before** the version's row lock. A
    submission certifies the graph as much as an approval does, and certifying
    it while somebody is completing a cycle from the far end would freeze the
    cycle into a reviewable document.
    """
    lock_component_graph(_organization_of(version.pk))
    current = _lock_version(version.pk, expected={RecipeVersionStatus.DRAFT})
    validate_version_graph(current)
    problems = submission_problems(current)
    if problems:
        raise ValidationError(
            [ValidationError(problem, code="recipe_version_incomplete") for problem in problems]
        )

    previous = snapshot(current)
    current.status = RecipeVersionStatus.SUBMITTED
    current.submitted_by = actor
    current.submitted_at = timezone.now()
    current.full_clean(exclude=["status"])
    current.save(update_fields=["status", "submitted_by", "submitted_at", "updated_at"])

    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
    )
    return current


# ---------------------------------------------------------------------------
# Review evidence
# ---------------------------------------------------------------------------


def _require_demo_namespace_match(recipe: Recipe, evidence_kind: str) -> None:
    """
    Fictional evidence belongs to the demo namespace, and only there.

    Both directions matter. Demo evidence on a real recipe would let invented
    figures reach a costing screen with a signature beside them; a *signed
    form* claim on a demo recipe would make a screenshot of fiction look like
    the branch's own approved record. RCP-126 is about the second one, and it
    is the one people actually get wrong.
    """
    is_demo = recipe.code.startswith(DEMO_CODE_PREFIX)
    if evidence_kind == ApprovalEvidenceKind.DEMO_FICTIONAL and not is_demo:
        raise _refuse(
            _("لا يجوز اعتماد وصفة حقيقية على دليل تجريبي."),
            "recipe_evidence_outside_demo_namespace",
        )
    if evidence_kind == ApprovalEvidenceKind.SIGNED_FORM and is_demo:
        raise _refuse(
            _("الوصفة التجريبية لا تحمل نموذج اعتماد موقّعاً."),
            "recipe_evidence_claims_a_real_signature",
        )


@transaction.atomic
def record_recipe_version_review(
    *,
    version: RecipeVersion,
    review_type: str,
    reviewer: User,
    decision: str,
    reason: str = "",
    evidence_reference: str = "",
    evidence_kind: str = "",
    note: str = "",
) -> RecipeVersionReview:
    """
    Record one party's signature on a submitted version.

    Three review types are recorded here — the kitchen's, the store's and the
    accountant's. The fourth, `FINAL`, is written by `approve_recipe_version`
    or `reject_recipe_version`, because it is the act that moves the version
    rather than an opinion about it.

    Append-only, and one row per type: a signature is not edited. A reviewer
    who has changed their mind since signing does not overwrite the record; the
    version is rejected and the next one is prepared.
    """
    if review_type == RecipeReviewType.FINAL:
        raise _refuse(
            _("الاعتماد النهائي يُسجّل بأمر الاعتماد، لا كمراجعة."),
            "recipe_review_final_is_not_a_review",
            field="review_type",
        )
    if review_type not in REQUIRED_REVIEW_TYPES:
        raise _refuse(_("نوع مراجعة غير معروف."), "recipe_review_unknown_type", field="review_type")
    if decision not in RecipeReviewDecision.values:
        raise _refuse(_("قرار غير معروف."), "recipe_review_unknown_decision", field="decision")

    current = _lock_version(version.pk, expected={RecipeVersionStatus.SUBMITTED})

    if current.reviews.filter(review_type=review_type).exists():
        raise _refuse(
            _("هذه المراجعة مسجّلة مسبقاً على هذه النسخة."),
            "recipe_review_already_recorded",
            field="review_type",
        )

    reason = reason.strip()
    if decision == RecipeReviewDecision.REJECTED and not reason:
        raise _refuse(_("سبب الرفض مطلوب."), "recipe_review_reason_required", field="reason")

    evidence_reference = evidence_reference.strip()
    if review_type == RecipeReviewType.ACCOUNTING and decision == RecipeReviewDecision.APPROVED:
        if not evidence_reference or not evidence_kind:
            raise _refuse(
                _("مراجعة الكلفة يجب أن تسمّي الدليل الذي اطّلعت عليه."),
                "recipe_review_evidence_required",
                field="evidence_reference",
            )
    if evidence_kind:
        _require_demo_namespace_match(current.recipe, evidence_kind)

    _require_distinct_parties(current, review_type=review_type, actor=reviewer)

    review = RecipeVersionReview(
        version=current,
        review_type=review_type,
        reviewer=reviewer,
        decision=decision,
        reviewed_at=timezone.now(),
        reason=reason,
        evidence_reference=evidence_reference,
        evidence_kind=evidence_kind,
        note=note.strip(),
    )
    review.full_clean()
    review.save()

    record_audit_event(
        action=(
            AuditAction.APPROVED
            if decision == RecipeReviewDecision.APPROVED
            else AuditAction.REJECTED
        ),
        target=review,
        new_state=snapshot(review),
        reason=reason,
    )
    return review


def _require_distinct_parties(version: RecipeVersion, *, review_type: str, actor: User) -> None:
    """
    The workbook's control is three *people*, not three checkboxes.

    `KM-RCP-004` assigns the approved quantity to "الشيف + المحاسب + المدير".
    A control satisfied by one person wearing three hats is not the control the
    form describes, so the two reviews that attest different professional
    judgements — the kitchen's and the accountant's — must be given by two
    different people. The store's review is a fourth signature on the same
    page and is deliberately *not* forced apart from the kitchen's: in a small
    branch the person who knows the cut is the person who knows the sack, and
    inventing a separation the source does not claim would be as wrong as
    dropping one it does.

    The final approver is held apart from everybody by `approve_recipe_version`.
    """
    incompatible: dict[str, str] = {
        RecipeReviewType.KITCHEN.value: RecipeReviewType.ACCOUNTING.value,
        RecipeReviewType.ACCOUNTING.value: RecipeReviewType.KITCHEN.value,
    }
    other = incompatible.get(review_type)
    if other is None:
        return
    clash = version.reviews.filter(review_type=other, reviewer=actor).exists()
    if clash:
        raise _refuse(
            _("لا يجوز أن يوقّع الشخص نفسه مراجعة المطبخ ومراجعة الكلفة معاً."),
            "recipe_review_incompatible_roles",
            field="reviewer",
        )


def review_gaps(version: RecipeVersion) -> list[str]:
    """
    Which required signatures are missing or refused, in review order.

    Read under whatever lock the caller already holds — `approve_recipe_version`
    calls it *after* locking, which is what makes the check meaningful.
    """
    recorded = {review.review_type: review for review in version.reviews.all()}
    gaps: list[str] = []
    for review_type in REQUIRED_REVIEW_TYPES:
        review = recorded.get(review_type)
        label = RecipeReviewType(review_type).label
        if review is None:
            gaps.append(str(_("%(review)s غير مسجّلة.") % {"review": label}))
        elif review.decision == RecipeReviewDecision.REJECTED:
            gaps.append(str(_("%(review)s مرفوضة.") % {"review": label}))
    return gaps


# ---------------------------------------------------------------------------
# Approval and refusal
# ---------------------------------------------------------------------------


@transaction.atomic
def approve_recipe_version(
    *,
    version: RecipeVersion,
    actor: User,
    approval_reference: str,
    approval_evidence_kind: str,
    note: str = "",
) -> RecipeVersion:
    """
    The manager's signature. Maker-checker, and never the author.

    Everything is re-read under the version's lock before anything moves — the
    status, every review row, and who wrote and submitted the version — because
    the interesting failure is not "somebody approved without reviews", it is
    "somebody approved while the third review was still being written". An
    in-memory review list gathered before the lock would have missed it.

    **Approval does not make a version effective.** It records that the control
    is satisfied. Claiming a date range for a branch is `activate_recipe_version`,
    a separate command with a separate permission, because agreeing that a
    recipe is correct and deciding that it takes effect on Sunday are two
    decisions and the second one is the one that changes what a meal costs.

    Re-runs the component graph checks under the graph lock. Between submission
    and this moment a child may have been rejected, a sibling recipe archived,
    or a cycle completed from the other end — and approval is the moment the
    graph acquires authority, so it is the moment to check it again (RCP-076).
    """
    lock_component_graph(_organization_of(version.pk))
    current = _lock_version(version.pk, expected={RecipeVersionStatus.SUBMITTED})
    validate_version_graph(current)

    if current.created_by_id == actor.pk:
        raise _refuse(
            _("لا يجوز أن يعتمد كاتب النسخة نسخته."),
            "recipe_version_approver_is_the_author",
            field="approved_by",
        )
    if current.submitted_by_id == actor.pk:
        raise _refuse(
            _("لا يجوز أن يعتمد مُرسل النسخة نسخته."),
            "recipe_version_approver_is_the_submitter",
            field="approved_by",
        )
    if current.reviews.filter(reviewer=actor).exists():
        raise _refuse(
            _("لا يجوز أن يمنح المراجع الاعتماد النهائي على مراجعته."),
            "recipe_version_approver_is_a_reviewer",
            field="approved_by",
        )

    gaps = review_gaps(current)
    if gaps:
        raise ValidationError(
            [ValidationError(gap, code="recipe_version_review_missing") for gap in gaps]
        )

    approval_reference = approval_reference.strip()
    if not approval_reference:
        raise _refuse(
            _("مرجع الاعتماد مطلوب."),
            "recipe_version_approval_reference_required",
            field="approval_reference",
        )
    if approval_evidence_kind not in ApprovalEvidenceKind.values:
        raise _refuse(
            _("نوع دليل الاعتماد غير معروف."),
            "recipe_version_evidence_kind_unknown",
            field="approval_evidence_kind",
        )
    _require_demo_namespace_match(current.recipe, approval_evidence_kind)

    previous = snapshot(current)
    RecipeVersionReview.objects.create(
        version=current,
        review_type=RecipeReviewType.FINAL,
        reviewer=actor,
        decision=RecipeReviewDecision.APPROVED,
        reviewed_at=timezone.now(),
        evidence_reference=approval_reference,
        evidence_kind=approval_evidence_kind,
        note=note.strip(),
    )

    current.status = RecipeVersionStatus.APPROVED
    current.approved_by = actor
    current.approved_at = timezone.now()
    current.approval_reference = approval_reference
    current.approval_evidence_kind = approval_evidence_kind
    current.full_clean(exclude=["status"])
    current.save(
        update_fields=[
            "status",
            "approved_by",
            "approved_at",
            "approval_reference",
            "approval_evidence_kind",
            "updated_at",
        ]
    )

    record_audit_event(
        action=AuditAction.APPROVED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
        reason=approval_reference,
    )
    return current


@transaction.atomic
def reject_recipe_version(*, version: RecipeVersion, actor: User, reason: str) -> RecipeVersion:
    """
    Refuse a submitted version, with a reason and an actor.

    The row is kept. A refusal is evidence — it is the record of what somebody
    would not sign, and deleting it would leave the next reader wondering why
    version 3 exists at all. Correction is a new version, never an edit to this
    one (§C).
    """
    reason = reason.strip()
    if not reason:
        raise _refuse(
            _("سبب الرفض مطلوب."), "recipe_version_rejection_reason_required", field="reason"
        )

    current = _lock_version(version.pk, expected={RecipeVersionStatus.SUBMITTED})
    previous = snapshot(current)

    if not current.reviews.filter(review_type=RecipeReviewType.FINAL).exists():
        RecipeVersionReview.objects.create(
            version=current,
            review_type=RecipeReviewType.FINAL,
            reviewer=actor,
            decision=RecipeReviewDecision.REJECTED,
            reviewed_at=timezone.now(),
            reason=reason,
        )

    current.status = RecipeVersionStatus.REJECTED
    current.rejected_by = actor
    current.rejected_at = timezone.now()
    current.rejection_reason = reason
    current.full_clean(exclude=["status"])
    current.save(
        update_fields=["status", "rejected_by", "rejected_at", "rejection_reason", "updated_at"]
    )

    record_audit_event(
        action=AuditAction.REJECTED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
        reason=reason,
    )
    return current


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------


def applicable_branches(recipe: Recipe) -> QuerySet[Branch]:
    """
    The branches an organization-wide activation would claim.

    A recipe that names its branches applies at those; a recipe that names none
    applies organization-wide, which is Task 3.1's `RecipeBranch` rule and stays
    true here. Inactive branches are excluded: claiming a range at a branch that
    is closed would occupy it against a future reopening for no benefit.
    """
    named = Branch.objects.filter(recipe_applicability__recipe=recipe, is_active=True)
    if named.exists():
        return named.order_by("code")
    return Branch.objects.filter(organization_id=recipe.organization_id, is_active=True).order_by(
        "code"
    )


@transaction.atomic
def activate_recipe_version(
    *,
    version: RecipeVersion,
    actor: User,
    effective_from: datetime.date,
    effective_to: datetime.date | None = None,
    branches: list[Branch] | None = None,
    supersedes: RecipeVersion | None = None,
    reason: str = "",
) -> RecipeVersion:
    """
    Put an approved version into effect over an explicit range and branch set.

    `branches=None` means organization-wide, and organization-wide is
    **materialised**: one scope row per applicable branch, flagged
    `is_organization_wide` so the screen can still say "everywhere" rather than
    listing five branches. That is what makes the overlap constraint able to do
    its job — see `RecipeVersionBranchScope` for why an empty list could not.

    `supersedes` closes the predecessor in **this** transaction, at the day
    before `effective_from`, which is RCP-016 verbatim. Without it, activating
    a replacement over a predecessor's open range is simply refused by the
    exclusion constraint, and that refusal is the correct answer: the operator
    has not said what happens to the version already in force.

    The range is `[effective_from, effective_to]`, inclusive at both ends, and
    a null `effective_to` is open-ended.
    """
    if effective_to is not None and effective_to < effective_from:
        raise _refuse(
            _("تاريخ النهاية يسبق تاريخ البداية."),
            "recipe_version_range_is_inverted",
            field="effective_to",
        )

    # Canonical lock order: the component graph, then the recipe, then versions
    # by ascending id. The graph lock is above the recipe lock because an
    # activation both certifies the graph and may supersede a version something
    # else depends on, and both of those are graph-wide questions.
    lock_component_graph(_organization_of(version.pk))
    Recipe.objects.select_for_update().get(pk=version.recipe_id)
    if supersedes is not None:
        _supersede_locked(
            predecessor_id=supersedes.pk,
            replacement=version,
            replacement_effective_from=effective_from,
            actor=actor,
            reason=reason,
        )

    current = _lock_version(version.pk, expected={RecipeVersionStatus.APPROVED})
    if supersedes is not None and supersedes.recipe_id != current.recipe_id:
        raise _refuse(
            _("النسخة المستبدَلة تتبع وصفة أخرى."),
            "recipe_version_supersedes_another_recipe",
            field="supersedes",
        )

    claimed = list(branches) if branches is not None else list(applicable_branches(current.recipe))
    organization_wide = branches is None
    if not claimed:
        raise _refuse(
            _("لا يوجد فرع فعّال لتفعيل النسخة عليه."),
            "recipe_version_no_branch_in_scope",
            field="branches",
        )
    _validate_branches(current, claimed)

    # RCP-074/RCP-075. Draft-time eligibility was about the child being agreed;
    # this is about it being *effective*. A parent effective in March whose
    # blend expired in February claims to contain something that did not exist,
    # and nothing downstream would notice until a costing gap months later.
    validate_version_graph(current)
    require_effective_coverage(
        parent_version=current,
        branches=[branch.pk for branch in claimed],
        effective_from=effective_from,
        effective_to=effective_to,
    )

    previous = snapshot(current)
    current.status = RecipeVersionStatus.ACTIVE
    current.activated_by = actor
    current.activated_at = timezone.now()
    current.effective_from = effective_from
    current.effective_to = effective_to
    current.full_clean(exclude=["status"])
    current.save(
        update_fields=[
            "status",
            "activated_by",
            "activated_at",
            "effective_from",
            "effective_to",
            "updated_at",
        ]
    )

    for branch in claimed:
        scope = RecipeVersionBranchScope(
            version=current,
            recipe=current.recipe,
            branch=branch,
            effective_from=effective_from,
            effective_to=effective_to,
            is_organization_wide=organization_wide,
            note=reason.strip(),
        )
        scope.full_clean()
        scope.save()

    record_audit_event(
        action=AuditAction.APPROVED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
        reason=reason,
        metadata={
            "effective_from": effective_from.isoformat(),
            "effective_to": effective_to.isoformat() if effective_to else None,
            "branches": sorted(branch.code for branch in claimed),
            "organization_wide": organization_wide,
        },
    )
    return current


def _validate_branches(version: RecipeVersion, claimed: list[Branch]) -> None:
    """No foreign branch, no duplicate, and nothing outside the recipe's reach."""
    organization_id = version.recipe.organization_id
    seen: set[int] = set()
    allowed = {branch.pk for branch in applicable_branches(version.recipe)}
    for branch in claimed:
        if branch.organization_id != organization_id:
            raise _refuse(
                _("الفرع %(code)s يتبع مؤسسة أخرى.") % {"code": branch.code},
                "recipe_version_foreign_branch",
                field="branches",
            )
        if branch.pk in seen:
            raise _refuse(
                _("الفرع %(code)s مكرر.") % {"code": branch.code},
                "recipe_version_duplicate_branch",
                field="branches",
            )
        if branch.pk not in allowed:
            raise _refuse(
                _("الوصفة لا تنطبق على الفرع %(code)s.") % {"code": branch.code},
                "recipe_version_branch_outside_applicability",
                field="branches",
            )
        seen.add(branch.pk)


@transaction.atomic
def supersede_recipe_version(
    *,
    version: RecipeVersion,
    replacement: RecipeVersion,
    actor: User,
    reason: str = "",
) -> RecipeVersion:
    """
    Close an active version because a named later one takes over.

    The predecessor's range ends the day before the replacement's begins, and
    every one of its branch scope rows closes on the same day. It stays
    resolvable for its own dates forever — that is the whole reason effective
    dating exists, and why a superseded row is never deleted.

    Usable two ways: on its own, when the replacement is already active over a
    later disjoint range and the link needs recording; or from inside
    `activate_recipe_version`, which is the ordinary path and the one RCP-016
    describes.
    """
    if replacement.recipe_id != version.recipe_id:
        raise _refuse(
            _("النسخة البديلة تتبع وصفة أخرى."),
            "recipe_version_supersedes_another_recipe",
            field="replacement",
        )
    lock_component_graph(_organization_of(version.pk))
    Recipe.objects.select_for_update().get(pk=version.recipe_id)
    locked_replacement = RecipeVersion.objects.select_for_update().filter(pk=replacement.pk).first()
    if locked_replacement is None or locked_replacement.effective_from is None:
        raise _refuse(
            _("النسخة البديلة بلا تاريخ سريان."),
            "recipe_version_replacement_not_dated",
            field="replacement",
        )
    return _supersede_locked(
        predecessor_id=version.pk,
        replacement=locked_replacement,
        replacement_effective_from=locked_replacement.effective_from,
        actor=actor,
        reason=reason,
    )


def _supersede_locked(
    *,
    predecessor_id: int,
    replacement: RecipeVersion,
    replacement_effective_from: datetime.date,
    actor: User,
    reason: str,
) -> RecipeVersion:
    """
    The supersession itself, with the recipe already locked by the caller.

    Order matters and is not incidental: the **version** closes first, then its
    scope rows follow it. The scope trigger refuses any movement while the
    version is still `ACTIVE`, so a raw `UPDATE` cannot quietly end a live
    recipe's effect at one branch without a supersession to explain it.
    """
    if predecessor_id == replacement.pk:
        raise _refuse(
            _("لا يجوز أن تستبدل النسخة نفسها."),
            "recipe_version_supersedes_itself",
            field="replacement",
        )

    predecessor = _lock_version(predecessor_id, expected={RecipeVersionStatus.ACTIVE})
    if predecessor.recipe_id != replacement.recipe_id:
        raise _refuse(
            _("النسخة المستبدَلة تتبع وصفة أخرى."),
            "recipe_version_supersedes_another_recipe",
            field="replacement",
        )

    close_at = replacement_effective_from - ONE_DAY
    if predecessor.effective_from is not None and close_at < predecessor.effective_from:
        raise _refuse(
            _("النسخة البديلة تبدأ قبل بداية النسخة الحالية؛ هذا ليس استبدالاً."),
            "recipe_version_replacement_starts_too_early",
            field="replacement",
        )
    if predecessor.effective_to is not None and predecessor.effective_to < close_at:
        # Already closed earlier than the replacement begins. Recording the
        # link is the whole job; moving the date backwards would rewrite a
        # period the branch already worked through.
        close_at = predecessor.effective_to

    # §L. Closing this version must not strand an ACTIVE parent that named it
    # as a component. The parent keeps naming this exact version forever, so
    # shortening the range underneath it would leave the parent claiming to
    # contain something that stops existing partway through its own effective
    # period — and nothing would say so until a costing gap appeared.
    blockers = supersession_blockers(child_version=predecessor, close_at=close_at)
    if blockers:
        raise ValidationError(
            [
                ValidationError(
                    _(
                        "النسخة مستعملة كمكوّن في %(parent)s @ %(branch)s حتى "
                        "%(until)s؛ استبدلها هناك أولاً."
                    )
                    % {
                        "parent": (
                            f"{blocker.parent_version.recipe.code} "
                            f"v{blocker.parent_version.version_number}"
                        ),
                        "branch": blocker.branch_code,
                        "until": (
                            blocker.parent_effective_to.isoformat()
                            if blocker.parent_effective_to
                            else str(_("أجل مفتوح"))
                        ),
                    },
                    code="recipe_component_dependency_blocks_supersession",
                )
                for blocker in blockers
            ]
        )

    previous = snapshot(predecessor)
    predecessor.status = RecipeVersionStatus.SUPERSEDED
    predecessor.effective_to = close_at
    predecessor.superseded_at = timezone.now()
    predecessor.superseded_by_version = replacement
    predecessor.full_clean(exclude=["status"])
    predecessor.save(
        update_fields=[
            "status",
            "effective_to",
            "superseded_at",
            "superseded_by_version",
            "updated_at",
        ]
    )

    for scope in predecessor.branch_scopes.select_for_update().order_by("pk"):
        if scope.effective_to == close_at:
            continue
        scope.effective_to = close_at
        scope.full_clean()
        scope.save(update_fields=["effective_to", "updated_at"])

    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=predecessor,
        previous_state=previous,
        new_state=snapshot(predecessor),
        reason=reason,
        metadata={
            "superseded_by": f"v{replacement.version_number}",
            "closed_at": close_at.isoformat(),
        },
    )
    return predecessor


# ---------------------------------------------------------------------------
# The authoritative resolver
# ---------------------------------------------------------------------------


def covers_on_date(on_date: datetime.date) -> Q:
    """
    `effective_to IS NULL OR effective_to >= on_date`.

    The inclusive upper bound, written once. Every read of the effective range
    in this repository goes through it, so no caller can quietly express the
    convention as `>` and produce a one-day hole nobody notices until a report
    for the last day of a version comes back empty.
    """
    return Q(effective_to__isnull=True) | Q(effective_to__gte=on_date)


@dataclass(frozen=True)
class ResolvedVersion:
    """One version, and the scope row that claimed it for this date."""

    version: RecipeVersion
    scope: RecipeVersionBranchScope


def resolve_recipe_version(
    *,
    recipe: Recipe,
    branch: Branch,
    on_date: datetime.date,
) -> RecipeVersion:
    """
    The one version in effect for this recipe, at this branch, on this date.

    **The only resolution there is.** Not `latest()`, not the highest version
    number, not the most recently updated row, and never "the active one" —
    the charter's rule is verbatim and absolute: *"Historical sales must use the
    recipe version that was effective when the item was sold. A recipe changed
    in September must not silently change the theoretical cost of July sales."*
    A superseded version therefore still answers for its own dates, and that is
    a feature rather than a leftover.

    `on_date` has no default. A posting-facing read that quietly meant *today*
    would give the right answer during development and the wrong one the first
    time somebody re-ran a July report in September.

    Raises `recipe_version_not_effective` when nothing covers the date and
    `recipe_version_ambiguous` when more than one does. The second is
    unrepresentable while the exclusion constraint holds; it is still coded and
    still tested, because "cannot happen" and "is not handled" are different
    claims and only one of them survives a bad migration.

    Reads only. Mutates nothing, resolves no cost, and exposes no money.
    """
    if branch.organization_id != recipe.organization_id:
        raise _refuse(_("الفرع يتبع مؤسسة أخرى."), "recipe_version_foreign_branch", field="branch")

    matches = list(
        RecipeVersionBranchScope.objects.filter(
            recipe=recipe,
            branch=branch,
            effective_from__lte=on_date,
            version__status__in=sorted(RESOLVABLE_VERSION_STATUSES),
        )
        .filter(covers_on_date(on_date))
        .select_related("version", "version__recipe", "version__output_unit")
        .order_by("version__version_number")
    )

    if not matches:
        raise _refuse(
            _("لا توجد نسخة سارية لهذه الوصفة في هذا الفرع بتاريخ %(date)s.")
            % {"date": on_date.isoformat()},
            "recipe_version_not_effective",
        )
    if len(matches) > 1:
        raise _refuse(
            _("أكثر من نسخة سارية لهذه الوصفة في هذا الفرع بتاريخ %(date)s.")
            % {"date": on_date.isoformat()},
            "recipe_version_ambiguous",
        )
    return matches[0].version


def effective_versions_for(
    *, recipe: Recipe, branch: Branch, on_date: datetime.date
) -> list[ResolvedVersion]:
    """
    Every version claiming this branch and date — normally one, sometimes zero.

    The verifier's view of the same question the resolver answers, kept
    separate because a verifier that raised on ambiguity could not *report*
    ambiguity, which is its whole job.
    """
    rows = (
        RecipeVersionBranchScope.objects.filter(
            recipe=recipe,
            branch=branch,
            effective_from__lte=on_date,
            version__status__in=sorted(RESOLVABLE_VERSION_STATUSES),
        )
        .filter(covers_on_date(on_date))
        .select_related("version")
        .order_by("version__version_number")
    )
    return [ResolvedVersion(version=row.version, scope=row) for row in rows]


@dataclass(frozen=True)
class TimelineEntry:
    """One claim on the timeline: which version, at which branch, over which days."""

    version: RecipeVersion
    branch: Branch
    effective_from: datetime.date
    effective_to: datetime.date | None
    is_organization_wide: bool


def version_timeline(recipe: Recipe, *, branch: Branch | None = None) -> list[TimelineEntry]:
    """
    Every effective claim this recipe has ever made, oldest first.

    The screen's effective-date timeline, and deliberately a read of the scope
    rows rather than of the versions: the scope rows are what the resolver uses,
    so a timeline built from anything else could disagree with the answer the
    system actually gives.
    """
    rows = RecipeVersionBranchScope.objects.filter(recipe=recipe).select_related(
        "version", "branch"
    )
    if branch is not None:
        rows = rows.filter(branch=branch)
    return [
        TimelineEntry(
            version=row.version,
            branch=row.branch,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            is_organization_wide=row.is_organization_wide,
        )
        for row in rows.order_by("effective_from", "branch__code", "version__version_number")
    ]


def approved_evidence_is_complete(version: RecipeVersion) -> bool:
    """Whether this version carries everything an approval must carry."""
    if version.status not in APPROVED_VERSION_STATUSES:
        return True
    return bool(
        version.approved_by_id
        and version.approved_at
        and version.approval_reference
        and version.approval_evidence_kind
        and not review_gaps(version)
    )
