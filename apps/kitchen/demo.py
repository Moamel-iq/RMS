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

Two new inventory items are created, and each has one purpose that no existing
item can serve. `DEMO-RICE-COOKED` is named in RCP-056: a batch recipe needs a
produced output and none of the five Phase 1 demo items is producible.
`DEMO-MEAL-READY` arrives with Task 3.4, because a production draft must show a
**stocked semi-finished leaf left unexpanded** — and the item playing that part
is `DEMO-RICE-COOKED`, which cannot also be the output of the recipe consuming
it. No other item is added.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.inventory.models import InventoryItem, ItemCategory, ItemType, Warehouse
from apps.kitchen.costing import cost_recipe_version
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
    ProductionBatch,
    ProductionBatchStatus,
    Recipe,
    RecipeCategory,
    RecipeLineCostClass,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
    RecipeVersion,
    RecipeVersionStatus,
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
    create_recipe_component,
    link_step_ingredient,
    set_recipe_branches,
)
from apps.kitchen.snapshots import create_recipe_cost_snapshot
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
    recipes.extend(
        seed_demo_components(organization=organization, created_by=created_by, branches=branches)
    )
    recipes.extend(
        seed_demo_cost(organization=organization, created_by=created_by, branches=branches)
    )
    recipes.extend(
        seed_demo_production(organization=organization, created_by=created_by, branches=branches)
    )
    recipes.extend(
        seed_demo_plated_recipe(organization=organization, created_by=created_by, branches=branches)
    )
    # Postings last, because they consume the stock the inventory demo put on
    # the shelf and produce the evidence every Task 3.5 screen reads.
    seed_demo_postings(organization=organization, created_by=created_by, branches=branches)
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


# ---------------------------------------------------------------------------
# The nested-recipe graph (Task 3.2B)
# ---------------------------------------------------------------------------
#
# A small graph that makes all five things RCP-070 - RCP-072 claim visible on
# one screen:
#
#   DEMO-RCP-DISH v1  -> DEMO-BLEND-MARINADE v1  -> DEMO-BLEND-SPICE v1
#                     (a stocked semi-finished input arrives as a RecipeLine,
#                      never as a component)
#
#   DEMO-RCP-DISH v2  -> DEMO-BLEND-MARINADE v2
#
# v1 of the dish keeps naming v1 of the marinade after v2 of the marinade
# exists. That is the whole of RCP-072 and it is the one thing a reader should
# be able to see without reading any code.
#
# Depth 2, well inside the limit of 3. Nothing invalid is ever seeded: a cycle
# or an over-deep graph belongs only in a test that rolls back.

DEMO_SPICE_CODE = "DEMO-BLEND-SPICE"
DEMO_MARINADE_CODE = "DEMO-BLEND-MARINADE"
DEMO_DISH_CODE = "DEMO-RCP-DISH"

#: The dish's first version runs a closed range so the demo shows a **parent**
#: supersession as well as a child one - two different corrections on one
#: screen. It is not a workaround: a child may be superseded under an open-ended
#: parent freely, because the parent's reference to it is a frozen foreign key
#: and stays valid afterwards.
DEMO_DISH_FIRST_TO = datetime.date(2026, 5, 31)


def _blend_recipe(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    created_by: User | None,
    branches: list[Branch],
) -> tuple[Recipe, RecipeVersion | None]:
    """A non-stocked demo sub-recipe: no output item, so only ever a component."""
    return _lifecycle_recipe(
        organization=organization,
        code=code,
        name_ar=name_ar,
        created_by=created_by,
        branches=branches,
    )


def seed_demo_components(
    *,
    organization: Organization,
    created_by: User | None,
    branches: list[Branch],
) -> list[Recipe]:
    """
    The nested demo scenario, built bottom-up through the real services.

    Idempotent the same way the rest of this module is: every step runs only if
    the state before it is still there, so a second run adds no recipe, no
    version, no component, no scope row and no review.
    """
    people = ensure_demo_reviewers()
    submitter = created_by or people["kitchen"]
    made: list[Recipe] = []

    # 10 - the leaf: a spice blend that exists only inside other recipes.
    spice, spice_draft = _blend_recipe(
        organization=organization,
        code=DEMO_SPICE_CODE,
        name_ar="خلطة بهارات تجريبية",
        created_by=created_by,
        branches=branches,
    )
    made.append(spice)
    if spice_draft is not None:
        approved = _carry_to_approved(spice_draft, people, submitter)
        activate_recipe_version(
            version=approved,
            actor=people["approver"],
            effective_from=DEMO_FIRST_EFFECTIVE,
            reason=DEMO_BANNER,
        )
    spice_v1 = spice.versions.filter(status=RecipeVersionStatus.ACTIVE).first()

    # 11 - the middle level: a marinade that contains the spice blend.
    marinade, marinade_draft = _blend_recipe(
        organization=organization,
        code=DEMO_MARINADE_CODE,
        name_ar="تتبيلة تجريبية",
        created_by=created_by,
        branches=branches,
    )
    made.append(marinade)
    if marinade_draft is not None and spice_v1 is not None:
        create_recipe_component(
            version=marinade_draft,
            component_version=spice_v1,
            multiplier=Decimal("0.25"),
            note=DEMO_BANNER,
            actor=created_by,
        )
        approved = _carry_to_approved(marinade_draft, people, submitter)
        activate_recipe_version(
            version=approved,
            actor=people["approver"],
            effective_from=DEMO_FIRST_EFFECTIVE,
            reason=DEMO_BANNER,
        )
    marinade_v1 = marinade.versions.filter(status=RecipeVersionStatus.ACTIVE).first()

    # 12 - the dish. It contains the marinade as a component, and a stocked
    #      semi-finished item as an ordinary line: both shapes on one screen,
    #      and the mutual exclusion visible rather than described.
    dish, dish_draft = _lifecycle_recipe(
        organization=organization,
        code=DEMO_DISH_CODE,
        name_ar="طبق تجريبي بوصفات فرعية",
        created_by=created_by,
        branches=branches,
    )
    made.append(dish)
    if dish_draft is not None and marinade_v1 is not None:
        create_recipe_component(
            version=dish_draft,
            component_version=marinade_v1,
            multiplier=Decimal("0.5"),
            note=DEMO_BANNER,
            actor=created_by,
        )
        _add_stocked_input(version=dish_draft, organization=organization)
        approved = _carry_to_approved(dish_draft, people, submitter)
        activate_recipe_version(
            version=approved,
            actor=people["approver"],
            effective_from=DEMO_FIRST_EFFECTIVE,
            effective_to=DEMO_DISH_FIRST_TO,
            reason=DEMO_BANNER,
        )

    # 13 - a newer marinade, and a **new dish version** that adopts it. The
    #      historical dish keeps naming the historical marinade; nothing is
    #      re-pointed, which is the point.
    # Exactly one version means the replacement pair has not been seeded yet.
    # Checking for "no ACTIVE version" would never fire: the dish's first
    # version *is* active, just with a closed range.
    dish_v1 = dish.versions.filter(effective_to=DEMO_DISH_FIRST_TO).first()
    if dish_v1 is not None and dish.versions.count() == 1 and marinade_v1 is not None:
        # The new marinade keeps its own spice blend, at a different
        # multiplier - so the comparison screen shows a real change on the
        # component row rather than an empty replacement.
        marinade_v2_draft = _complete_draft(recipe=marinade, created_by=created_by)
        if spice_v1 is not None:
            create_recipe_component(
                version=marinade_v2_draft,
                component_version=spice_v1,
                multiplier=Decimal("0.30"),
                note=DEMO_BANNER,
                actor=created_by,
            )
        marinade_v2 = _carry_to_approved(marinade_v2_draft, people, submitter)
        activate_recipe_version(
            version=marinade_v2,
            actor=people["approver"],
            effective_from=DEMO_SECOND_EFFECTIVE,
            supersedes=marinade_v1,
            reason=DEMO_BANNER,
        )
        dish_v2_draft = _complete_draft(recipe=dish, created_by=created_by)
        create_recipe_component(
            version=dish_v2_draft,
            component_version=RecipeVersion.objects.get(pk=marinade_v2.pk),
            multiplier=Decimal("0.5"),
            note=DEMO_BANNER,
            actor=created_by,
        )
        _add_stocked_input(version=dish_v2_draft, organization=organization)
        approved = _carry_to_approved(dish_v2_draft, people, submitter)
        activate_recipe_version(
            version=approved,
            actor=people["approver"],
            effective_from=DEMO_SECOND_EFFECTIVE,
            supersedes=dish_v1,
            reason=DEMO_BANNER,
        )

    return made


def _add_stocked_input(*, version: RecipeVersion, organization: Organization) -> None:
    """
    The **other** shape: a stocked semi-finished item, consumed as a line.

    RCP-070 in the demo data rather than in a comment. This item has a book
    value and its ingredient tree is never expanded again; the marinade beside
    it has no book value and is expanded from its exact child version. Adding
    this as a component instead is refused by the service and by a trigger.
    """
    cooked = InventoryItem.objects.filter(organization=organization, code=COOKED_RICE_CODE).first()
    if cooked is None:
        return
    add_recipe_line(
        version=version,
        item=cooked,
        entered_quantity=Decimal("1"),
        entered_unit=unit_by_code("KG"),
        note=f"{DEMO_BANNER} — مدخل نصف مصنّع مخزني، يُستهلك كسطر لا كوصفة فرعية.",
    )


# ---------------------------------------------------------------------------
# Task 3.3 - a costable recipe, and one frozen costing decision
# ---------------------------------------------------------------------------
#
# The nested demo graph above is built from blends whose own lines are valued
# demo stock, so it costs. What it lacks is a **packaging** line and a second
# serving, and those two are half of what `KM-RCP-004`'s cost summary shows. So
# one more recipe is added here rather than reshaping an existing one: the
# Task 3.2B scenario keeps proving what it was built to prove, and this proves
# costing.
#
# No new inventory item is created. Every leaf below is an item the inventory
# demo already posted stock for, which is what makes the card add up to a real
# number rather than to a hole.
#
# **This seed posts nothing.** No stock movement, no balance change, no journal
# entry - the only rows it writes are a recipe, its versions, and one
# append-only cost snapshot. A test counts all three.

DEMO_COST_CODE = "DEMO-RCP-COST"

#: The warehouse whose average the demo card reads. Named, never defaulted:
#: the whole point of the costing contract is that a warehouse is an input.
DEMO_COST_WAREHOUSE_CODE = "DEMO-MAIN"

#: What the demo dish is portioned into. Four definitions because they are
#: **alternatives**, and each one is there to be looked at:
#:
#: * `FULL` is the **primary**, so it is the plate-cost basis.
#: * `HALF` is the physical factor 0.500 the recipe book states.
#: * `SMALL` deliberately does not divide the output evenly, so the leftover
#:   and its cost are visible on the screen rather than described.
#: * `TINY` makes 10,000 servings from the same output, which is the case the
#:   first pass of Task 3.3 refused to allocate. It is here so a reader can see
#:   that the compact summary reconstructs the exact total at a count no screen
#:   would ever list.
#:
#: Every quantity is a row, never a constant in a service (RCP-082).
DEMO_COST_SERVINGS: list[tuple[str, str, str, bool]] = [
    ("FULL", "حصة كاملة تجريبية", "1", True),
    ("HALF", "نصف حصة تجريبية", "0.5", False),
    ("SMALL", "حصة صغيرة تجريبية", "0.35", False),
    ("TINY", "حصة تجريبية دقيقة", "0.001", False),
]


def _cost_warehouse(*, organization: Organization) -> Warehouse | None:
    """The demo warehouse, if the inventory demo has been seeded."""
    return Warehouse.objects.filter(
        branch__organization=organization, code=DEMO_COST_WAREHOUSE_CODE
    ).first()


def _cost_draft(
    *, recipe: Recipe, organization: Organization, created_by: User | None
) -> RecipeVersion:
    """
    A draft whose every leaf is a **valued** demo item.

    Rice and oil are `FOOD`, the container is `PACKAGING`: the split the
    workbook's summary needs, and the reason this recipe exists beside the
    nested one.
    """
    kg = unit_by_code("KG")
    litre = unit_by_code("L")
    piece = unit_by_code("PIECE")
    items = {
        item.code: item
        for item in InventoryItem.objects.filter(
            organization=organization, code__startswith="DEMO-"
        )
    }

    version = create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("10"),
        output_unit=kg,
        instructions=f"{DEMO_BANNER}. وصفة تجريبية لعرض بطاقة الكلفة.",
        notes=DEMO_BANNER,
        created_by=created_by,
    )
    if "DEMO-RICE" in items:
        add_recipe_line(
            version=version,
            item=items["DEMO-RICE"],
            entered_quantity=Decimal("4"),
            entered_unit=kg,
            cost_class=RecipeLineCostClass.FOOD,
            note=DEMO_BANNER,
        )
    if "DEMO-OIL" in items:
        add_recipe_line(
            version=version,
            item=items["DEMO-OIL"],
            entered_quantity=Decimal("0.5"),
            entered_unit=litre,
            cost_class=RecipeLineCostClass.FOOD,
            note=DEMO_BANNER,
        )
    if "DEMO-CONTAINER" in items:
        add_recipe_line(
            version=version,
            item=items["DEMO-CONTAINER"],
            entered_quantity=Decimal("10"),
            entered_unit=piece,
            cost_class=RecipeLineCostClass.PACKAGING,
            note=DEMO_BANNER,
        )
    add_recipe_step(version=version, instruction_ar="خطوة تجريبية.", note=DEMO_BANNER)
    for code, name, quantity, primary in DEMO_COST_SERVINGS:
        add_recipe_serving(
            version=version,
            code=code,
            name_ar=name,
            serving_quantity=Decimal(quantity),
            serving_unit=kg,
            is_primary=primary,
        )
    return RecipeVersion.objects.get(pk=version.pk)


def seed_demo_cost(
    *,
    organization: Organization,
    created_by: User | None,
    branches: list[Branch],
) -> list[Recipe]:
    """
    One costable recipe, one preview draft, and one immutable snapshot.

    Idempotent like everything else here, and in one extra place: the snapshot
    key carries the as-of date, so a second run on the same day returns the
    original snapshot through the idempotency path and a run on a later day
    records a **new** decision rather than colliding with the old one. A key
    with no date in it would raise `idempotency_key_conflict` the next morning,
    which is a real bug dressed as a safety feature.
    """
    people = ensure_demo_reviewers()
    submitter = created_by or people["kitchen"]
    made: list[Recipe] = []

    recipe, created = _recipe(
        organization=organization,
        code=DEMO_COST_CODE,
        name_ar="طبق تجريبي لبطاقة الكلفة",
        recipe_type=RecipeType.PORTION,
        category=None,
        output_item=None,
        created_by=created_by,
    )
    made.append(recipe)
    if created:
        set_recipe_branches(recipe=recipe, branches=branches)

    # 14 - the costable version. It carries a nested component as well as its
    #      own lines, so one card shows a direct leaf, a one-level roll-up and
    #      a two-level one - and the same item (rice) on three different paths,
    #      which is what keeps the paths separate rows.
    if not recipe.versions.exists():
        draft = _cost_draft(recipe=recipe, organization=organization, created_by=created_by)
        marinade = RecipeVersion.objects.filter(
            recipe__organization=organization,
            recipe__code=DEMO_MARINADE_CODE,
            status=RecipeVersionStatus.ACTIVE,
        ).first()
        if marinade is not None:
            create_recipe_component(
                version=draft,
                component_version=marinade,
                multiplier=Decimal("0.5"),
                note=DEMO_BANNER,
                actor=created_by,
            )
        approved = _carry_to_approved(draft, people, submitter)
        # From the **second** effective date, not the first: the component is
        # marinade v2, which only starts on that day, and RCP-074 requires the
        # child to be effective on the parent's `effective_from`. Activating a
        # month earlier would claim a dish that contained a blend nobody had
        # approved yet.
        activate_recipe_version(
            version=approved,
            actor=people["approver"],
            effective_from=DEMO_SECOND_EFFECTIVE,
            reason=DEMO_BANNER,
        )

    active = recipe.versions.filter(status=RecipeVersionStatus.ACTIVE).first()

    # 15 - a draft on top, so the preview banner has something to sit on. A
    #      preview is non-authoritative by construction and cannot be
    #      snapshotted; the screen says so and the service refuses it.
    if active is not None and recipe.versions.count() == 1:
        _cost_draft(recipe=recipe, organization=organization, created_by=created_by)

    # 16 - one frozen costing decision, through the real service.
    warehouse = _cost_warehouse(organization=organization)
    if active is not None and warehouse is not None:
        _seed_demo_snapshot(version=active, warehouse=warehouse, actor=created_by)

    return made


def _seed_demo_snapshot(
    *, version: RecipeVersion, warehouse: Warehouse, actor: User | None
) -> None:
    """
    Cost the active demo version today and freeze it.

    **Today is named here, not defaulted.** The inventory demo's movements are
    stamped with the wall clock at seeding time, so a fixed historical date
    would read an empty ledger and produce a card full of holes. A seed
    choosing a date is a different act from a service assuming one.

    Skipped silently when any leaf is unvalued: a snapshot may only be built
    from a complete card, and the demo must not be the one place that rule
    bends.
    """
    as_of = timezone.localdate()
    card = cost_recipe_version(version=version, warehouse=warehouse, as_of_date=as_of)
    if not card.is_complete:
        return
    create_recipe_cost_snapshot(
        card=card,
        actor=actor,
        idempotency_key=f"DEMO-COST-SNAPSHOT-{as_of:%Y%m%d}",
        reference="DEMO-NOT-A-REAL-DECISION",
        reason=DEMO_BANNER,
        note=DEMO_BANNER,
    )


# ---------------------------------------------------------------------------
# Task 3.4 - one visible production draft
# ---------------------------------------------------------------------------
#
# A production draft is the first kitchen document carrying a **plan** and a
# **reality** at once, and almost every rule Task 3.4 enforces is only legible
# when the two disagree. So this scenario is built to disagree, deliberately and
# in each way the module has an opinion about:
#
#   DEMO-RCP-PROD v1 (BATCH, produces DEMO-MEAL-READY)
#     - a DIRECT line of DEMO-RICE, consumed BELOW plan
#     - a DIRECT line of DEMO-RICE-COOKED, a **stocked** semi-finished item, so
#       one requirement and never re-expanded (RCP-071)
#     - a DIRECT line of DEMO-CONTAINER, optional, consumed at ZERO
#     - a COMPONENT path into DEMO-BLEND-MARINADE, whose own line reaches a
#       second requirement by a nested path (RCP-079/080)
#
# and on the rice requirement, two approved stand-ins actually used:
#
#     - DEMO-RICE-COOKED, in the same dimension, used **partially** beside the
#       primary row, so the requirement has a comparable quantity;
#     - DEMO-OIL, in litres, another dimension entirely — the case the screen
#       must show separately and never add up.
#
# The multiplier is 2.5: non-integral and greater than one, so the scaled
# expected output and every scaled requirement are figures somebody had to
# compute rather than read off the recipe. The actual output is deliberately
# below the expected, because a yield of exactly 100% is the one number that
# demonstrates nothing.
#
# **This seed posts nothing.** No stock movement, no balance change, no journal,
# no document number, and no status but DRAFT. A test counts each of them.

DEMO_PRODUCTION_RECIPE_CODE = "DEMO-RCP-PROD"

#: The second produced item the demo needs, and the reason there are two.
#:
#: RCP-056 names `DEMO-RICE-COOKED` as the demo's produced item, and it is
#: already spoken for: `DEMO-RCP-RICE` makes it. One item cannot be both the
#: output of the recipe being produced and a stocked input to it, so showing
#: "a stocked semi-finished leaf is one requirement and is never re-expanded"
#: needs a second producible item to be the output. One item, one purpose.
DEMO_MEAL_CODE = "DEMO-MEAL-READY"

#: Named, never `today`. The version in force is resolved from the branch **and**
#: this date, so a seed reading the wall clock would demo a different version
#: every month — and eventually none at all, once the last one is superseded.
DEMO_PRODUCTION_DATE = datetime.date(2026, 7, 15)

#: Non-integral and greater than one. See the note above.
DEMO_PRODUCTION_MULTIPLIER = Decimal("2.5")

#: Fixed, so a second seeding run is a **retry** and returns the original batch.
DEMO_PRODUCTION_KEY = "DEMO-PRODUCTION-BATCH-1"

ZERO_QUANTITY = Decimal("0")


@transaction.atomic
def ensure_meal_item(*, organization: Organization) -> InventoryItem:
    """The producible output of the demo production recipe."""
    existing: InventoryItem | None = _seeded(
        InventoryItem, organization=organization, code=DEMO_MEAL_CODE
    )
    if existing is not None:
        return existing

    category = ItemCategory.objects.filter(organization=organization, code="DEMO-GRAINS").first()
    if category is None:
        category = ItemCategory.objects.filter(organization=organization).first()
    item = InventoryItem(
        organization=organization,
        code=DEMO_MEAL_CODE,
        name_ar="وجبة جاهزة تجريبية",
        category=category,
        item_type=ItemType.SEMI_FINISHED,
        base_unit=unit_by_code("KG"),
        notes=DEMO_BANNER,
    )
    item.full_clean()
    item.save()
    return item


def _production_warehouse(*, organization: Organization) -> Warehouse | None:
    """The demo warehouse the batch draws on, if the inventory demo was seeded."""
    return Warehouse.objects.filter(
        branch__organization=organization, code=DEMO_COST_WAREHOUSE_CODE
    ).first()


def _production_draft_version(
    *, recipe: Recipe, organization: Organization, created_by: User | None
) -> RecipeVersion:
    """
    The version the batch is drafted from: three direct shapes and two approvals.

    Substitutes are added here rather than afterwards because
    `add_recipe_line_substitute` refuses anything but a draft — an approval added
    after activation would be a change to a version somebody signed.
    """
    kg = unit_by_code("KG")
    piece = unit_by_code("PIECE")
    items = {
        item.code: item
        for item in InventoryItem.objects.filter(
            organization=organization, code__startswith="DEMO-"
        )
    }

    version = create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("20"),
        output_unit=kg,
        instructions=f"{DEMO_BANNER}. وصفة تجريبية لعرض مسودة الإنتاج.",
        notes=DEMO_BANNER,
        created_by=created_by,
    )

    rice_line = None
    if "DEMO-RICE" in items:
        rice_line = add_recipe_line(
            version=version,
            item=items["DEMO-RICE"],
            entered_quantity=Decimal("6"),
            entered_unit=kg,
            cost_class=RecipeLineCostClass.FOOD,
            note=DEMO_BANNER,
        )
    # A **stocked** semi-finished input. One requirement, never re-expanded: its
    # book value already contains its own ingredients (RCP-071).
    if COOKED_RICE_CODE in items:
        add_recipe_line(
            version=version,
            item=items[COOKED_RICE_CODE],
            entered_quantity=Decimal("3"),
            entered_unit=kg,
            cost_class=RecipeLineCostClass.FOOD,
            note=f"{DEMO_BANNER} — مدخل مخزني نصف مصنّع لا يُوسَّع.",
        )
    # Optional, and consumed at zero below. An optional line the kitchen skipped
    # is a fact about a real batch, and readiness must not refuse it.
    if "DEMO-CONTAINER" in items:
        add_recipe_line(
            version=version,
            item=items["DEMO-CONTAINER"],
            entered_quantity=Decimal("20"),
            entered_unit=piece,
            cost_class=RecipeLineCostClass.PACKAGING,
            is_optional=True,
            note=f"{DEMO_BANNER} — سطر اختياري.",
        )

    if rice_line is not None:
        # Same dimension, so a partial substitution here has a comparable figure.
        if COOKED_RICE_CODE in items:
            add_recipe_line_substitute(
                line=rice_line,
                substitute_item=items[COOKED_RICE_CODE],
                reason=f"{DEMO_BANNER} — بديل بنفس بُعد القياس.",
                note=DEMO_BANNER,
            )
        # Another dimension entirely. RCP-022 approves **items**, never
        # conversions, so a kitchen may legitimately accept a stand-in that
        # nothing converts to — and the screen must then say so rather than add
        # litres to kilograms.
        if "DEMO-OIL" in items:
            add_recipe_line_substitute(
                line=rice_line,
                substitute_item=items["DEMO-OIL"],
                reason=f"{DEMO_BANNER} — بديل ببُعد قياس مختلف.",
                note=DEMO_BANNER,
            )

    add_recipe_step(version=version, instruction_ar="خطوة تجريبية للإنتاج.", note=DEMO_BANNER)
    add_recipe_serving(
        version=version,
        code="PORTION",
        name_ar="حصة إنتاج تجريبية",
        serving_quantity=Decimal("1"),
        serving_unit=kg,
        is_primary=True,
    )
    return RecipeVersion.objects.get(pk=version.pk)


def seed_demo_production(
    *,
    organization: Organization,
    created_by: User | None,
    branches: list[Branch],
) -> list[Recipe]:
    """
    One producible recipe and one DRAFT batch of it, through the real services.

    Idempotent the way the rest of this module is, and in one place that needed
    thought: the batch is created with a **fixed** idempotency key, so a second
    run is a retry that returns the original rather than drafting a second batch.
    Each edit below is then guarded by the state it would change — on the value
    rather than on a flag — so a second run finds the work done and writes
    nothing, even if somebody edited the demo batch by hand in between.
    """
    from apps.kitchen.production import create_production_batch, record_production_output

    people = ensure_demo_reviewers()
    submitter = created_by or people["kitchen"]
    made: list[Recipe] = []

    meal = ensure_meal_item(organization=organization)
    recipe, created = _recipe(
        organization=organization,
        code=DEMO_PRODUCTION_RECIPE_CODE,
        name_ar="وجبة تجريبية للإنتاج",
        recipe_type=RecipeType.BATCH,
        category=None,
        output_item=meal,
        created_by=created_by,
    )
    made.append(recipe)
    if created:
        set_recipe_branches(recipe=recipe, branches=branches)

    if not recipe.versions.exists():
        draft = _production_draft_version(
            recipe=recipe, organization=organization, created_by=created_by
        )
        # A non-stocked nested component, so the batch carries a COMPONENT path
        # beside its direct ones and its requirement list has more than one level
        # to display (RCP-079/080).
        marinade = RecipeVersion.objects.filter(
            recipe__organization=organization,
            recipe__code=DEMO_MARINADE_CODE,
            status=RecipeVersionStatus.ACTIVE,
        ).first()
        if marinade is not None:
            create_recipe_component(
                version=draft,
                component_version=marinade,
                multiplier=Decimal("0.4"),
                note=DEMO_BANNER,
                actor=created_by,
            )
        approved = _carry_to_approved(draft, people, submitter)
        # From the **second** effective date: the component is marinade v2,
        # which only starts then, and RCP-074 requires the child to be effective
        # on the parent's own `effective_from`.
        activate_recipe_version(
            version=approved,
            actor=people["approver"],
            effective_from=DEMO_SECOND_EFFECTIVE,
            reason=DEMO_BANNER,
        )

    warehouse = _production_warehouse(organization=organization)
    branch = branches[0] if branches else None
    if warehouse is None or branch is None:
        # The inventory demo has not been seeded, so there is no warehouse to
        # draft into. The recipe still exists; the batch waits for the store.
        return made

    batch = create_production_batch(
        recipe=recipe,
        branch=branch,
        warehouse=warehouse,
        planned_business_date=DEMO_PRODUCTION_DATE,
        multiplier=DEMO_PRODUCTION_MULTIPLIER,
        actor=created_by,
        idempotency_key=DEMO_PRODUCTION_KEY,
        notes=f"{DEMO_BANNER} — مسودة إنتاج للعرض فقط.",
    )
    _seed_demo_actuals(batch=batch, organization=organization, actor=created_by)

    # The actual output, deliberately **below** the expected 50 KG. A yield of
    # exactly 100% is the one figure that demonstrates nothing, and the gap
    # between the two is the whole subject of the yield report.
    refreshed = type(batch).objects.get(pk=batch.pk)
    if refreshed.actual_output_base_quantity is None:
        record_production_output(
            batch=refreshed,
            entered_quantity=Decimal("46"),
            entered_unit=unit_by_code("KG"),
            actor=created_by,
        )
    return made


def _seed_demo_actuals(*, batch: Any, organization: Organization, actor: User | None) -> None:
    """
    Make the plan and the reality disagree, in each of the ways that matter.

    Every edit is guarded by the value it would write, so a second seeding run
    finds the work already done and writes nothing — which is what keeps the
    audit trail free of a duplicate event per run.
    """
    from apps.kitchen.production import (
        add_production_batch_substitute,
        update_production_batch_actuals,
    )

    kg = unit_by_code("KG")
    litre = unit_by_code("L")
    items = {
        item.code: item
        for item in InventoryItem.objects.filter(
            organization=organization, code__startswith="DEMO-"
        )
    }

    for line in batch.lines.select_related("item", "item__base_unit").order_by("line_order"):
        primary = line.actuals.filter(substitute__isnull=True).first()
        if primary is None:
            continue

        if line.is_optional:
            # An optional requirement the kitchen skipped. Zero is a fact, and
            # readiness must not refuse a batch for recording one.
            if primary.base_quantity != ZERO_QUANTITY:
                update_production_batch_actuals(
                    actual=primary,
                    entered_quantity=Decimal("0"),
                    entered_unit=line.item.base_unit,
                    note=f"{DEMO_BANNER} — لم يُستعمل.",
                    actor=actor,
                )
            continue

        if line.item.code == "DEMO-RICE" and line.component_path == "":
            # Consumed BELOW plan, with the shortfall met by approved stand-ins.
            # Two facts about one requirement, which is exactly why they are two
            # rows rather than one adjusted quantity.
            shortfall = (line.planned_base_quantity * Decimal("0.75")).quantize(Decimal("0.000001"))
            if primary.base_quantity != shortfall:
                update_production_batch_actuals(
                    actual=primary,
                    entered_quantity=shortfall,
                    entered_unit=line.item.base_unit,
                    note=f"{DEMO_BANNER} — أقل من المخطط.",
                    actor=actor,
                )
            if (
                COOKED_RICE_CODE in items
                and not line.actuals.filter(item__code=COOKED_RICE_CODE).exists()
            ):
                add_production_batch_substitute(
                    line=line,
                    item=items[COOKED_RICE_CODE],
                    entered_quantity=Decimal("2"),
                    entered_unit=kg,
                    reason=f"{DEMO_BANNER} — استبدال جزئي بنفس البُعد.",
                    actor=actor,
                )
            if "DEMO-OIL" in items and not line.actuals.filter(item__code="DEMO-OIL").exists():
                add_production_batch_substitute(
                    line=line,
                    item=items["DEMO-OIL"],
                    entered_quantity=Decimal("1.5"),
                    entered_unit=litre,
                    reason=f"{DEMO_BANNER} — بديل ببُعد قياس مختلف.",
                    actor=actor,
                )
            continue

        # Everything else: consumed slightly ABOVE plan, so the variance report
        # has a case in each direction rather than only one.
        above = (line.planned_base_quantity * Decimal("1.1")).quantize(Decimal("0.000001"))
        if primary.base_quantity != above:
            update_production_batch_actuals(
                actual=primary,
                entered_quantity=above,
                entered_unit=line.item.base_unit,
                note=f"{DEMO_BANNER} — أكثر من المخطط.",
                actor=actor,
            )


# ---------------------------------------------------------------------------
# Task 3.5 — postings, a reversal, an output lot, and both journal cases
# ---------------------------------------------------------------------------

#: A second producible item, and the reason there are two.
#:
#: The demo has to show **both** journal outcomes, and they differ by exactly
#: one thing: whether the produced goods enter the same inventory control
#: account the ingredients left. `DEMO-MEAL-READY` shares the organization
#: default, so its batches net to zero on every account and write no journal at
#: all. `DEMO-MEAL-PLATED` carries a Demo-only item-scoped override, so its
#: batches have something to say and write one balanced entry.
#:
#: It also tracks lots, which is what makes the output-lot evidence visible on
#: a screen rather than only in a test.
DEMO_PLATED_CODE = "DEMO-MEAL-PLATED"
DEMO_PLATED_RECIPE_CODE = "DEMO-RCP-PROD-PLATED"
DEMO_PLATED_ACCOUNT_CODE = "1-03-01-090"

DEMO_POSTED_KEY = "DEMO-PRODUCTION-BATCH-POSTED"
DEMO_REVERSED_KEY = "DEMO-PRODUCTION-BATCH-REVERSED"
DEMO_JOURNAL_KEY = "DEMO-PRODUCTION-BATCH-JOURNAL"


def ensure_plated_item(*, organization: Organization) -> InventoryItem:
    """The lot-tracked producible output of the second demo production recipe."""
    existing: InventoryItem | None = _seeded(
        InventoryItem, organization=organization, code=DEMO_PLATED_CODE
    )
    if existing is not None:
        return existing

    category = ItemCategory.objects.filter(organization=organization, code="DEMO-GRAINS").first()
    if category is None:
        category = ItemCategory.objects.filter(organization=organization).first()
    item = InventoryItem(
        organization=organization,
        code=DEMO_PLATED_CODE,
        name_ar="طبق جاهز تجريبي",
        category=category,
        item_type=ItemType.SEMI_FINISHED,
        base_unit=unit_by_code("KG"),
        tracks_lots=True,
        tracks_expiry=True,
        shelf_life_days=3,
        notes=DEMO_BANNER,
    )
    item.full_clean()
    item.save()
    return item


def _ensure_plated_account_override(*, organization: Organization, item: InventoryItem) -> bool:
    """
    A Demo-only item-scoped `INVENTORY_CONTROL` override, through the real service.

    Written this way rather than by inserting a mapping row because the mapping
    machinery is what a production posting resolves against, and a demo that
    bypassed it would be demonstrating something the application does not do.

    Returns whether the override is in force, so the caller can skip the
    journal batch honestly when the accounting demo has not been seeded — an
    invented account would be worse than a missing example.
    """
    from apps.accounting.models import INVENTORY_CONTROL, Account, AccountRole
    from apps.accounting.services import create_account
    from apps.inventory.accounts import create_inventory_mapping
    from apps.inventory.models import InventoryAccountMapping

    role = AccountRole.objects.filter(code=INVENTORY_CONTROL).first()
    if role is None:
        return False
    if InventoryAccountMapping.objects.filter(
        organization=organization, account_role=role, item=item, is_active=True
    ).exists():
        return True
    if not Account.objects.filter(organization=organization, code="1-03-01").exists():
        # No chart, so no parent to hang a leaf from. The seed says nothing
        # rather than inventing one.
        return False

    account = Account.objects.filter(
        organization=organization, code=DEMO_PLATED_ACCOUNT_CODE
    ).first()
    if account is None:
        account = create_account(
            organization=organization,
            code=DEMO_PLATED_ACCOUNT_CODE,
            name_ar=f"مخزون الأطباق الجاهزة — {DEMO_BANNER}",
            name_en="Demo plated inventory",
        )
    create_inventory_mapping(
        organization=organization,
        role=INVENTORY_CONTROL,
        account=account,
        item=item,
        effective_from=datetime.date(DEMO_PRODUCTION_DATE.year, 1, 1),
    )
    return True


def _demo_batch(
    *,
    recipe: Recipe,
    branch: Branch,
    warehouse: Warehouse,
    key: str,
    note: str,
    output: Decimal,
    created_by: User | None,
    organization: Organization,
) -> Any:
    """
    One demo batch, drafted, filled in and given an output — but not posted.

    The **fixed** idempotency key is what makes a second seed a retry rather
    than a second batch, and every edit below is guarded on the value it would
    change rather than on a flag, so a second run finds the work done even if
    somebody edited the batch by hand in between.
    """
    from apps.kitchen.production import create_production_batch, record_production_output

    batch = create_production_batch(
        recipe=recipe,
        branch=branch,
        warehouse=warehouse,
        planned_business_date=DEMO_PRODUCTION_DATE,
        multiplier=DEMO_PRODUCTION_MULTIPLIER,
        actor=created_by,
        idempotency_key=key,
        notes=note,
    )
    _seed_demo_actuals(batch=batch, organization=organization, actor=created_by)
    refreshed = ProductionBatch.objects.get(pk=batch.pk)
    if refreshed.is_draft and refreshed.actual_output_base_quantity is None:
        record_production_output(
            batch=refreshed,
            entered_quantity=output,
            entered_unit=unit_by_code("KG"),
            actor=created_by,
        )
    return ProductionBatch.objects.get(pk=batch.pk)


def seed_demo_postings(
    *,
    organization: Organization,
    created_by: User | None,
    branches: list[Branch],
) -> None:
    """
    Three postings the screens need, through the real posting service.

    * a **POSTED** batch whose accounts all net to zero, so it writes no
      journal at all — the common case, and the one whose correctness is
      invisible without the verifier;
    * a **POSTED then REVERSED** batch, so the reversal timeline and the
      exact-mirror evidence are visible;
    * a **POSTED** batch producing a lot-tracked item through a Demo-only
      account override, so both the output lot and a real netted journal
      appear on a screen.

    Nothing here inserts a movement, a balance, a journal or a lot. Every row
    is produced by `post_production_batch`, which is the only thing that may
    produce one.
    """
    from apps.kitchen.production_posting import post_production_batch, reverse_production_batch

    warehouse = _production_warehouse(organization=organization)
    branch = branches[0] if branches else None
    if warehouse is None or branch is None:
        return
    if not _inventory_control_is_mapped(organization=organization):
        # No chart of accounts, so the produced goods have nowhere to land.
        # The recipes and the draft still exist; the postings wait for the
        # accounting demo, because a posting into an invented account would be
        # a worse example than a missing one.
        return

    recipe = Recipe.objects.filter(
        organization=organization, code=DEMO_PRODUCTION_RECIPE_CODE
    ).first()
    if recipe is None:
        return

    # --- 1. The legitimate silence -----------------------------------------
    posted = _demo_batch(
        recipe=recipe,
        branch=branch,
        warehouse=warehouse,
        key=DEMO_POSTED_KEY,
        note=f"{DEMO_BANNER} — دفعة مرحّلة بلا قيد محاسبي.",
        output=Decimal("46"),
        created_by=created_by,
        organization=organization,
    )
    if posted.is_draft:
        post_production_batch(
            batch=posted,
            idempotency_key=f"{DEMO_POSTED_KEY}-POST",
            actor=created_by,
            reason=DEMO_BANNER,
        )

    # --- 2. Posted, then reversed ------------------------------------------
    reversible = _demo_batch(
        recipe=recipe,
        branch=branch,
        warehouse=warehouse,
        key=DEMO_REVERSED_KEY,
        note=f"{DEMO_BANNER} — دفعة مرحّلة ثم معكوسة.",
        output=Decimal("44"),
        created_by=created_by,
        organization=organization,
    )
    if reversible.is_draft:
        reversible = post_production_batch(
            batch=reversible,
            idempotency_key=f"{DEMO_REVERSED_KEY}-POST",
            actor=created_by,
            reason=DEMO_BANNER,
        )
    if reversible.status == ProductionBatchStatus.POSTED:
        reverse_production_batch(
            batch=reversible,
            idempotency_key=f"{DEMO_REVERSED_KEY}-REVERSE",
            reason=f"{DEMO_BANNER} — عكس تجريبي للعرض.",
            actor=created_by,
        )

    # --- 3. An output lot, and a journal that has something to say ---------
    #
    # Skipped, deliberately, when the accounting demo has not been seeded: the
    # journal case needs a real item-scoped account override, and an invented
    # account would be a worse example than a missing one.
    plated = Recipe.objects.filter(organization=organization, code=DEMO_PLATED_RECIPE_CODE).first()
    if plated is None or not _plated_override_is_in_force(organization=organization):
        return
    journalled = _demo_batch(
        recipe=plated,
        branch=branch,
        warehouse=warehouse,
        key=DEMO_JOURNAL_KEY,
        note=f"{DEMO_BANNER} — دفعة مرحّلة بقيد محاسبي ولوط ناتج.",
        output=Decimal("42"),
        created_by=created_by,
        organization=organization,
    )
    if journalled.is_draft:
        post_production_batch(
            batch=journalled,
            idempotency_key=f"{DEMO_JOURNAL_KEY}-POST",
            actor=created_by,
            reason=DEMO_BANNER,
        )


def _inventory_control_is_mapped(*, organization: Organization) -> bool:
    """
    Whether `INVENTORY_CONTROL` resolves for this organization on the demo date.

    Asked before anything posts rather than discovered inside the kernel: the
    kitchen demo runs in environments where only the recipe fixtures exist, and
    a seeder that raised there would make an unrelated test suite depend on the
    accounting chart.
    """
    from apps.accounting.models import INVENTORY_CONTROL
    from apps.accounting.services import resolve_default_account

    try:
        resolve_default_account(
            organization=organization,
            account_role=INVENTORY_CONTROL,
            on_date=DEMO_PRODUCTION_DATE,
        )
    except Exception:  # noqa: BLE001 - any refusal means "not mapped"
        return False
    return True


def _plated_override_is_in_force(*, organization: Organization) -> bool:
    """Whether the Demo-only item mapping exists, so the journal case is honest."""
    from apps.accounting.models import INVENTORY_CONTROL, AccountRole
    from apps.inventory.models import InventoryAccountMapping

    role = AccountRole.objects.filter(code=INVENTORY_CONTROL).first()
    if role is None:
        return False
    return InventoryAccountMapping.objects.filter(
        organization=organization,
        account_role=role,
        item__code=DEMO_PLATED_CODE,
        is_active=True,
    ).exists()


def seed_demo_plated_recipe(
    *,
    organization: Organization,
    created_by: User | None,
    branches: list[Branch],
) -> list[Recipe]:
    """
    The second producible recipe, its lot-tracked output, and the account
    override that gives its postings a journal to write.

    Master data is created unconditionally so the recipe list is the same
    everywhere; only the **override** depends on a seeded chart of accounts,
    and only the journal batch depends on the override.
    """
    item = ensure_plated_item(organization=organization)
    _ensure_plated_account_override(organization=organization, item=item)

    people = ensure_demo_reviewers()
    submitter = created_by or people["kitchen"]
    recipe, created = _recipe(
        organization=organization,
        code=DEMO_PLATED_RECIPE_CODE,
        name_ar="طبق تجريبي للإنتاج",
        recipe_type=RecipeType.BATCH,
        category=None,
        output_item=item,
        created_by=created_by,
    )
    if created:
        set_recipe_branches(recipe=recipe, branches=branches)
    if not recipe.versions.exists():
        draft = _production_draft_version(
            recipe=recipe, organization=organization, created_by=created_by
        )
        approved = _carry_to_approved(draft, people, submitter)
        activate_recipe_version(
            version=approved,
            actor=people["approver"],
            effective_from=DEMO_SECOND_EFFECTIVE,
            reason=DEMO_BANNER,
        )
    return [recipe]
