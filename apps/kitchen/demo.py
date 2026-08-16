"""
Kitchen demo data.

Nine recipes, built through the real services so every kitchen screen has
something on it. Five of them stay drafts — those are what the *editing*
screens need — and four walk the whole approval boundary, because a screen
that has never had a `SUPERSEDED` row on it has never actually been reviewed.

Idempotent, `DEMO`-namespaced, and never a fixture: a second run creates no
second recipe, no second version, no second line, step, substitute, serving,
link, review, scope row or transition.

**Everything here is fiction and says so.** Each record carries
`تجريبي — غير معتمد للإنتاج` in its notes, and no demo recipe uses a real Khan
Mandi dish name or any gram figure taken from the recipe book (RCP-126). The
shapes are modelled on the source documents — a batch that yields portions, a
plate assembled from components, a whole/half split — because the shapes are
what the screens have to render. The numbers are invented, and being invented
is the point: a demo screenshot that looked like the real menu is how
unapproved figures acquire authority.

One new inventory item is created, `DEMO-RICE-COOKED`, and it is named in
RCP-056 for exactly this reason: a batch recipe needs a produced output, and
none of the five Phase 1 demo items is producible. No other item is added.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction

from apps.inventory.models import InventoryItem, ItemCategory, ItemType
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    approve_recipe_version,
    record_recipe_version_review,
    reject_recipe_version,
    submit_recipe_version,
)
from apps.kitchen.models import (
    ApprovalEvidenceKind,
    MeasurementBasis,
    PreparationStage,
    Recipe,
    RecipeCategory,
    RecipeLineCostClass,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
    RecipeVersion,
    ServingRoundingPolicy,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_line_substitute,
    add_recipe_serving,
    add_recipe_step,
    archive_recipe,
    create_draft_recipe_version,
    create_recipe,
    create_recipe_category,
    link_step_ingredient,
    set_recipe_branches,
)
from apps.organizations.models import Branch, Organization
from apps.units.selectors import unit_by_code
from apps.users.models import User

NAMESPACE = "DEMO-KITCHEN-V1"

#: The banner every demo record carries, in the kitchen's own language.
DEMO_BANNER = "تجريبي — غير معتمد للإنتاج"

#: The one new item RCP-056 permits, because a batch recipe must produce
#: something and no Phase 1 demo item is producible.
COOKED_RICE_CODE = "DEMO-RICE-COOKED"

CATEGORIES: list[tuple[str, str]] = [
    ("DEMO-RCP-MAIN", "أطباق رئيسية تجريبية"),
    ("DEMO-RCP-PREP", "تحضيرات تجريبية"),
]


def _seeded(model: Any, **lookup: Any) -> Any:
    """The already-seeded row, if a previous run made it. Idempotency starts here."""
    return model.objects.filter(**lookup).first()


@transaction.atomic
def ensure_cooked_rice_item(*, organization: Organization) -> InventoryItem:
    """
    The one produced item the demo needs.

    `SEMI_FINISHED`, because it is cooked rice a batch makes and a plate later
    draws on — the stocked sub-recipe shape of §5B.1.
    """
    existing: InventoryItem | None = _seeded(
        InventoryItem, organization=organization, code=COOKED_RICE_CODE
    )
    if existing is not None:
        return existing

    category = ItemCategory.objects.filter(organization=organization, code="DEMO-GRAINS").first()
    if category is None:
        category = ItemCategory.objects.filter(organization=organization).first()
    item = InventoryItem(
        organization=organization,
        code=COOKED_RICE_CODE,
        name_ar="رز مطبوخ تجريبي",
        category=category,
        item_type=ItemType.SEMI_FINISHED,
        base_unit=unit_by_code("KG"),
        notes=DEMO_BANNER,
    )
    item.full_clean()
    item.save()
    return item


@transaction.atomic
def seed_demo_categories(*, organization: Organization) -> list[RecipeCategory]:
    """The two groupings the demo recipes sort into."""
    rows: list[RecipeCategory] = []
    for code, name in CATEGORIES:
        existing = _seeded(RecipeCategory, organization=organization, code=code)
        if existing is not None:
            rows.append(existing)
            continue
        rows.append(
            create_recipe_category(
                organization=organization, code=code, name_ar=name, notes=DEMO_BANNER
            )
        )
    return rows


def _recipe(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    recipe_type: str,
    category: RecipeCategory | None,
    output_item: InventoryItem | None,
    created_by: User | None,
    **provenance: Any,
) -> tuple[Recipe, bool]:
    existing = _seeded(Recipe, organization=organization, code=code)
    if existing is not None:
        return existing, False
    return (
        create_recipe(
            organization=organization,
            code=code,
            name_ar=name_ar,
            recipe_type=recipe_type,
            category=category,
            output_item=output_item,
            notes=DEMO_BANNER,
            created_by=created_by,
            **provenance,
        ),
        True,
    )


def _draft(*, recipe: Recipe, **kwargs: Any) -> RecipeVersion:
    existing = recipe.versions.first()
    if existing is not None:
        return existing
    return create_draft_recipe_version(recipe=recipe, **kwargs)


@transaction.atomic
def seed_demo_recipes(
    *, organization: Organization, created_by: User | None = None
) -> list[Recipe]:
    """
    Build the five demo recipes and everything hanging off them.

    Idempotent throughout: each helper looks for its row before creating one,
    so a second run adds nothing. That is checked by a test rather than
    trusted, because "idempotent" is the claim demo seeds most often get wrong.
    """
    categories = {row.code: row for row in seed_demo_categories(organization=organization)}
    cooked_rice = ensure_cooked_rice_item(organization=organization)
    items = {
        item.code: item
        for item in InventoryItem.objects.filter(
            organization=organization, code__startswith="DEMO-"
        )
    }
    branches = list(Branch.objects.filter(organization=organization, is_active=True)[:1])

    kg = unit_by_code("KG")
    piece = unit_by_code("PIECE")
    litre = unit_by_code("L")

    recipes: list[Recipe] = []

    # 1 — A batch recipe with a stocked output, structured steps, servings,
    #     a substitute, and both FOOD and PACKAGING lines.
    batch, created = _recipe(
        organization=organization,
        code="DEMO-RCP-RICE",
        name_ar="طبخة رز تجريبية",
        recipe_type=RecipeType.BATCH,
        category=categories.get("DEMO-RCP-PREP"),
        output_item=cooked_rice,
        created_by=created_by,
        source_document="نموذج تجريبي داخلي",
        source_page=1,
        source_reference="DEMO-1",
        source_note=DEMO_BANNER,
    )
    recipes.append(batch)
    if created:
        set_recipe_branches(recipe=batch, branches=branches)
    draft = _draft(
        recipe=batch,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("12"),
        output_unit=kg,
        preparation_loss=Decimal("0.02"),
        cooking_yield=Decimal("0.9"),
        instructions=f"{DEMO_BANNER}. نظرة عامة تجريبية على الطريقة.",
        notes=DEMO_BANNER,
        created_by=created_by,
        source_document="نموذج تجريبي داخلي",
        source_page=1,
        source_reference="DEMO-1",
    )
    if not draft.lines.exists() and "DEMO-RICE" in items:
        rice_line = add_recipe_line(
            version=draft,
            item=items["DEMO-RICE"],
            entered_quantity=Decimal("10"),
            entered_unit=kg,
            measured_quantity=Decimal("10.4"),
            loss_rate=Decimal("0.03"),
            cost_class=RecipeLineCostClass.FOOD,
            preparation_stage=PreparationStage.PREP,
            measurement_basis=MeasurementBasis.RAW,
            note=DEMO_BANNER,
            source_document="نموذج تجريبي داخلي",
            source_page=1,
        )
        if "DEMO-OIL" in items:
            add_recipe_line(
                version=draft,
                item=items["DEMO-OIL"],
                entered_quantity=Decimal("0.5"),
                entered_unit=litre,
                cost_class=RecipeLineCostClass.FOOD,
                preparation_stage=PreparationStage.COOK,
                note=DEMO_BANNER,
            )
        if "DEMO-CONTAINER" in items:
            # The food/packaging split the workbook's cost summary needs.
            add_recipe_line(
                version=draft,
                item=items["DEMO-CONTAINER"],
                entered_quantity=Decimal("12"),
                entered_unit=piece,
                cost_class=RecipeLineCostClass.PACKAGING,
                preparation_stage=PreparationStage.PACK,
                measurement_basis=MeasurementBasis.PLATED,
                note=DEMO_BANNER,
            )
        if "DEMO-MEAT" in items:
            # Guidance only: nothing substitutes on its own.
            add_recipe_line_substitute(
                line=rice_line,
                substitute_item=items["DEMO-MEAT"],
                priority=1,
                reason="بديل تجريبي للعرض فقط",
                note=DEMO_BANNER,
            )

    if not draft.steps.exists():
        # A step whose duration the "source" gives.
        timed = add_recipe_step(
            version=draft,
            instruction_ar="اغسل الرز التجريبي وانقعه.",
            stage=PreparationStage.PREP,
            expected_duration=datetime.timedelta(minutes=30),
            checkpoint_ar="يصفّى الماء بالكامل.",
            note=DEMO_BANNER,
            source_document="نموذج تجريبي داخلي",
            source_page=2,
        )
        # A step with a qualitative heat instruction and NO temperature. The
        # empty Celsius column is the point of this row (RCP-068).
        add_recipe_step(
            version=draft,
            instruction_ar="اتركه على نار هادئة حتى ينضج.",
            stage=PreparationStage.COOK,
            heat_instruction_ar="نار هادئة",
            checkpoint_ar="لا يلتصق القاع.",
            is_critical=True,
            note=DEMO_BANNER,
            source_document="نموذج تجريبي داخلي",
            source_page=2,
        )
        first_line = draft.lines.order_by("line_order").first()
        if first_line is not None:
            link_step_ingredient(step=timed, recipe_line=first_line, share=Decimal("1"))

    if not draft.servings.exists():
        # Whole and half over one output — the physical conversion of RCP-123.
        add_recipe_serving(
            version=draft,
            code="FULL",
            name_ar="حصة كاملة تجريبية",
            serving_quantity=Decimal("0.4"),
            serving_unit=kg,
            is_primary=True,
            measurement_basis=MeasurementBasis.COOKED,
            note=DEMO_BANNER,
        )
        add_recipe_serving(
            version=draft,
            code="HALF",
            name_ar="نصف حصة تجريبية",
            serving_quantity=Decimal("0.2"),
            serving_unit=kg,
            rounding_policy=ServingRoundingPolicy.DOWN,
            rounding_increment=Decimal("1"),
            measurement_basis=MeasurementBasis.COOKED,
        )
        # A weight-based serving, so the gram-serving shape is exercised.
        add_recipe_serving(
            version=draft,
            code="G350",
            name_ar="حصة وزنية تجريبية",
            serving_quantity=Decimal("350"),
            serving_unit=unit_by_code("G"),
            measurement_basis=MeasurementBasis.COOKED,
        )

    # 2 — A portion recipe: a plate assembled to order, producing no stock.
    plate, created = _recipe(
        organization=organization,
        code="DEMO-RCP-PLATE",
        name_ar="طبق تجريبي مركّب",
        recipe_type=RecipeType.PORTION,
        category=categories.get("DEMO-RCP-MAIN"),
        output_item=None,
        created_by=created_by,
    )
    recipes.append(plate)
    if created:
        set_recipe_branches(recipe=plate, branches=branches)
    plate_draft = _draft(
        recipe=plate,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("1"),
        output_unit=piece,
        instructions=DEMO_BANNER,
        created_by=created_by,
    )
    if not plate_draft.lines.exists():
        # The plate draws on the cooked output of recipe 1 — the stocked
        # sub-recipe relationship, without expanding its ingredient tree.
        add_recipe_line(
            version=plate_draft,
            item=cooked_rice,
            entered_quantity=Decimal("0.4"),
            entered_unit=kg,
            cost_class=RecipeLineCostClass.FOOD,
            measurement_basis=MeasurementBasis.COOKED,
            note=DEMO_BANNER,
        )
        if "DEMO-CHICKEN" in items:
            add_recipe_line(
                version=plate_draft,
                item=items["DEMO-CHICKEN"],
                entered_quantity=Decimal("0.5"),
                entered_unit=piece,
                cost_class=RecipeLineCostClass.FOOD,
                measurement_basis=MeasurementBasis.RAW,
                note=DEMO_BANNER,
            )
    if not plate_draft.servings.exists():
        add_recipe_serving(
            version=plate_draft,
            code="PLATE",
            name_ar="طبق واحد تجريبي",
            serving_quantity=Decimal("1"),
            serving_unit=piece,
            is_primary=True,
            measurement_basis=MeasurementBasis.PLATED,
        )

    # 3 — A recipe with a draft and no structure yet, so the completeness
    #     panel has something honest to report.
    bare, created = _recipe(
        organization=organization,
        code="DEMO-RCP-EMPTY",
        name_ar="وصفة تجريبية قيد التحضير",
        recipe_type=RecipeType.PORTION,
        category=categories.get("DEMO-RCP-MAIN"),
        output_item=None,
        created_by=created_by,
    )
    recipes.append(bare)
    _draft(
        recipe=bare,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("1"),
        output_unit=piece,
        notes=DEMO_BANNER,
        created_by=created_by,
    )

    # 4 — A recipe with no version at all: named, not yet described.
    named, _created = _recipe(
        organization=organization,
        code="DEMO-RCP-NAMED",
        name_ar="وصفة تجريبية مسجّلة بلا مسودة",
        recipe_type=RecipeType.PORTION,
        category=None,
        output_item=None,
        created_by=created_by,
    )
    recipes.append(named)

    # 5 — An archived recipe, so the list's archived state is visible and the
    #     reserved-code rule can be demonstrated.
    retired, created = _recipe(
        organization=organization,
        code="DEMO-RCP-RETIRED",
        name_ar="وصفة تجريبية مؤرشفة",
        recipe_type=RecipeType.PORTION,
        category=None,
        output_item=None,
        created_by=created_by,
    )
    if retired.is_active:
        archive_recipe(recipe=retired, reason="أرشفة تجريبية للعرض")
        retired.refresh_from_db()
    recipes.append(retired)

    recipes.extend(
        seed_demo_lifecycle(organization=organization, created_by=created_by, branches=branches)
    )
    return recipes


# ---------------------------------------------------------------------------
# The lifecycle, as something an operator can look at
# ---------------------------------------------------------------------------
#
# Recipes 1 – 5 above stay drafts: they are what the *editing* screens need.
# Recipes 6 – 9 below walk the whole approval boundary, because a screen that
# has never had a `SUPERSEDED` row on it has never actually been reviewed.
#
# Everything here carries `DEMO_FICTIONAL` evidence, which a database trigger
# permits only inside the `DEMO-` namespace and refuses everywhere else. A demo
# signoff that looked like a signed `KM-RCP-004` is exactly how unapproved
# figures acquire authority (RCP-126), so the evidence kind says what it is.


#: The four demo signatories. Data actors, never accounts anybody signs in
#: with — each one gets an unusable password, exactly as the inventory demo's
#: count conductor does. Four of them because `KM-RCP-004`'s control is four
#: signatures, and a demo that reused one user would show an approval the real
#: system would refuse.
DEMO_REVIEWERS: dict[str, tuple[str, str, str]] = {
    "kitchen": ("demo-kitchen-reviewer", "مراجع مطبخ", "تجريبي"),
    "storekeeper": ("demo-store-reviewer", "مراجع مخزن", "تجريبي"),
    "accounting": ("demo-cost-reviewer", "مراجع كلفة", "تجريبي"),
    "approver": ("demo-recipe-approver", "معتمِد وصفات", "تجريبي"),
}

#: An obviously fictional reference. Nobody could mistake it for a form number.
DEMO_EVIDENCE_REFERENCE = "DEMO-NOT-A-REAL-FORM"

#: Fixed dates, so a second seed resolves to the same version as the first and
#: the screens do not change meaning overnight.
DEMO_FIRST_EFFECTIVE = datetime.date(2026, 1, 1)
DEMO_SECOND_EFFECTIVE = datetime.date(2026, 6, 1)


def ensure_demo_reviewers() -> dict[str, User]:
    """The four signatories, created once and reused."""
    people: dict[str, User] = {}
    for role, (username, first, last) in DEMO_REVIEWERS.items():
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"first_name": first, "last_name": last, "is_active": True},
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        people[role] = user
    return people


def _lifecycle_recipe(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    created_by: User | None,
    branches: list[Branch],
) -> tuple[Recipe, RecipeVersion | None]:
    """A demo recipe with one complete draft, or the one a previous run made."""
    recipe, created = _recipe(
        organization=organization,
        code=code,
        name_ar=name_ar,
        recipe_type=RecipeType.PORTION,
        category=None,
        output_item=None,
        created_by=created_by,
    )
    if created:
        set_recipe_branches(recipe=recipe, branches=branches)
    if recipe.versions.exists():
        return recipe, None
    return recipe, _complete_draft(recipe=recipe, created_by=created_by)


def _complete_draft(*, recipe: Recipe, created_by: User | None) -> RecipeVersion:
    """A draft with a line, a step and a serving — enough to pass submission."""
    kg = unit_by_code("KG")
    items = InventoryItem.objects.filter(
        organization_id=recipe.organization_id, code__startswith="DEMO-"
    )
    ingredient = items.filter(code="DEMO-RICE").first() or items.first()
    version = create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("4"),
        output_unit=kg,
        instructions=f"{DEMO_BANNER}. نظرة عامة تجريبية.",
        notes=DEMO_BANNER,
        created_by=created_by,
    )
    if ingredient is not None:
        add_recipe_line(
            version=version,
            item=ingredient,
            entered_quantity=Decimal("2"),
            entered_unit=kg,
            note=DEMO_BANNER,
        )
    add_recipe_step(version=version, instruction_ar="خطوة تجريبية.", note=DEMO_BANNER)
    add_recipe_serving(
        version=version,
        code="ONE",
        name_ar="حصة تجريبية",
        serving_quantity=Decimal("1"),
        serving_unit=kg,
        is_primary=True,
    )
    return RecipeVersion.objects.get(pk=version.pk)


def _carry_to_approved(
    version: RecipeVersion, people: dict[str, User], submitter: User
) -> RecipeVersion:
    """
    Submit, gather the three signatures, and take the final approval.

    Five distinct people are involved and none of them is decorative: the
    author submits, three parties sign their own column, and a fourth approves.
    The approver is refused if they wrote it, submitted it, or reviewed it — so
    a demo that reused one user would produce an approval the real system does
    not allow, which is the opposite of what a demo is for.
    """
    submit_recipe_version(version=version, actor=submitter)
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.KITCHEN,
        reviewer=people["kitchen"],
        decision=RecipeReviewDecision.APPROVED,
        note=DEMO_BANNER,
    )
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.STOREKEEPER,
        reviewer=people["storekeeper"],
        decision=RecipeReviewDecision.APPROVED,
        note=DEMO_BANNER,
    )
    record_recipe_version_review(
        version=version,
        review_type=RecipeReviewType.ACCOUNTING,
        reviewer=people["accounting"],
        decision=RecipeReviewDecision.APPROVED,
        evidence_reference=DEMO_EVIDENCE_REFERENCE,
        evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
        note=DEMO_BANNER,
    )
    return approve_recipe_version(
        version=version,
        actor=people["approver"],
        approval_reference=DEMO_EVIDENCE_REFERENCE,
        approval_evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
    )


@transaction.atomic
def seed_demo_lifecycle(
    *,
    organization: Organization,
    created_by: User | None,
    branches: list[Branch],
) -> list[Recipe]:
    """
    Four more demo recipes, one per lifecycle state worth looking at.

    Idempotent by construction: each recipe is created only if its code is
    absent, and each transition runs only if the version is still in the state
    before it. A second run therefore adds no version, no scope row, no review
    and no transition — checked by a test, because "idempotent" is the claim a
    demo seed most often gets wrong.
    """
    people = ensure_demo_reviewers()
    # The author submits their own draft, which is ordinary. What the control
    # forbids is the author *approving* it, and `people["approver"]` is a fifth
    # person precisely so that separation is real rather than asserted.
    submitter = created_by or people["kitchen"]
    made: list[Recipe] = []

    # 6 — a version under review, so the signature panel has something on it.
    submitted, draft = _lifecycle_recipe(
        organization=organization,
        code="DEMO-RCP-REVIEW",
        name_ar="وصفة تجريبية قيد المراجعة",
        created_by=created_by,
        branches=branches,
    )
    made.append(submitted)
    if draft is not None:
        submit_recipe_version(version=draft, actor=submitter)
        record_recipe_version_review(
            version=draft,
            review_type=RecipeReviewType.KITCHEN,
            reviewer=people["kitchen"],
            decision=RecipeReviewDecision.APPROVED,
            note=DEMO_BANNER,
        )

    # 7 — approved and deliberately not activated: the state that proves
    #     approval and effect are two different decisions.
    approved, draft = _lifecycle_recipe(
        organization=organization,
        code="DEMO-RCP-APPROVED",
        name_ar="وصفة تجريبية معتمدة غير مفعّلة",
        created_by=created_by,
        branches=branches,
    )
    made.append(approved)
    if draft is not None:
        _carry_to_approved(draft, people, submitter)

    # 8 — rejected, with its reason kept.
    rejected, draft = _lifecycle_recipe(
        organization=organization,
        code="DEMO-RCP-REJECTED",
        name_ar="وصفة تجريبية مرفوضة",
        created_by=created_by,
        branches=branches,
    )
    made.append(rejected)
    if draft is not None:
        submit_recipe_version(version=draft, actor=submitter)
        reject_recipe_version(
            version=draft,
            actor=people["approver"],
            reason="سبب رفض تجريبي: الكميات غير مؤكدة.",
        )

    # 9 — the interesting one: an active version, and the superseded version it
    #     replaced, so a reader can see that January still resolves to v1 after
    #     v2 took over in June. That is the whole reason effective dating exists.
    dated, draft = _lifecycle_recipe(
        organization=organization,
        code="DEMO-RCP-DATED",
        name_ar="وصفة تجريبية مؤرّخة",
        created_by=created_by,
        branches=branches,
    )
    made.append(dated)
    if draft is not None:
        first = _carry_to_approved(draft, people, submitter)
        activate_recipe_version(
            version=first,
            actor=people["approver"],
            effective_from=DEMO_FIRST_EFFECTIVE,
            reason=DEMO_BANNER,
        )
        second = _carry_to_approved(
            _complete_draft(recipe=dated, created_by=created_by), people, submitter
        )
        activate_recipe_version(
            version=second,
            actor=people["approver"],
            effective_from=DEMO_SECOND_EFFECTIVE,
            supersedes=first,
            reason=DEMO_BANNER,
        )

    return made
