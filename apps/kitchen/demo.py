"""
Kitchen demo data.

Five recipes, built through the real services so every Task 3.1 screen has
something on it. Idempotent, `DEMO`-namespaced, and never a fixture: a second
run creates no second recipe, no second version, no second line, step,
substitute, serving or link.

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
from apps.kitchen.models import (
    MeasurementBasis,
    PreparationStage,
    Recipe,
    RecipeCategory,
    RecipeLineCostClass,
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

    return recipes
