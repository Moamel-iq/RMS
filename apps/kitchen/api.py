"""
Recipe master-data API.

Master data is not a posted ledger, so this is service-backed CRUD rather than
the command shape a posting API needs. The standing rules are unchanged:

* No writable path that skips the services. Every mutation calls
  `apps/kitchen/services.py`; nothing calls `Model.objects.create`.
* An identifier never widens access. Everything is resolved through
  `apps/kitchen/selectors.py`, which filters by the caller's own scope, so
  another organization's recipe is a **404** and not a 403.
* Quantities cross the boundary as **exact strings**, both directions. JSON's
  only numeric type is binary floating point, and a serving factor that
  arrived as 0.5000000000000001 would be nobody's fault and everybody's
  problem.

The lifecycle half of this file is **commands**, not CRUD. A version that has
left `DRAFT` has no `PATCH` and no `DELETE`, because correcting an approved
recipe is a new version — and a verb that implied otherwise would be the API
contradicting the database.

Task 3.2B added the component endpoints and **Task 3.3 adds the costing ones**
at the end of this file. Two rules govern that half and are worth stating here
rather than only beside each route:

* **Money is a second permission.** Every costing route requires
  `view_recipe_cost` *in addition to* reaching the organization, and the recipe,
  version, component, step and serving endpoints above stay money-free. Cost
  keys are **omitted** from an unauthorized payload, never sent as null: a null
  says a number exists and you are not trusted with it, which is a different
  statement from the one intended.
* **Snapshots are a command, not CRUD.** There is a `POST` and there are two
  `GET`s. There is no `PATCH` and no `DELETE`, because the rows are append-only
  at the database and a verb that implied otherwise would be the API
  contradicting a trigger.

**Task 3.4 adds the production routes** at the very end. They are commands over
a draft — create, correct actuals, substitute, rescale, readiness, discard — and
there is deliberately no post, reverse, issue, consume, complete or journal verb
anywhere among them. Task 3.5 owns posting. They are also the first routes in
this module authorized at a **warehouse** rather than at an organization, and
they carry no money key at all.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja import Router, Schema

from apps.inventory.models import InventoryLot, StockLocation
from apps.inventory.selectors import (
    resolve_item,
    resolve_manageable_warehouse,
    resolve_package_unit,
)
from apps.kitchen.costing import (
    RecipeCostCard,
    cost_recipe_on_date,
    cost_recipe_version,
    preview_recipe_cost,
)
from apps.kitchen.graph import component_tree, flatten_tree
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    approve_recipe_version,
    covers_on_date,
    record_recipe_version_review,
    reject_recipe_version,
    resolve_recipe_version,
    submit_recipe_version,
    supersede_recipe_version,
)
from apps.kitchen.models import (
    MeasurementBasis,
    ProductionBatch,
    RecipeLineCostClass,
    ServingRoundingPolicy,
)
from apps.kitchen.permissions import (
    ACTIVATE_RECIPE_VERSION,
    APPROVE_RECIPE_VERSION,
    CREATE_PRODUCTION_BATCH,
    LINK_BATCH_DOCUMENT,
    MANAGE_RECIPE,
    POST_PRODUCTION_BATCH,
    REJECT_RECIPE_VERSION,
    REVERSE_PRODUCTION_BATCH,
    REVIEW_RECIPE_VERSION,
    SUBMIT_RECIPE_VERSION,
    VIEW_KITCHEN_REPORT,
    VIEW_PRODUCTION,
    VIEW_RECIPE,
    VIEW_RECIPE_COST,
)
from apps.kitchen.production import (
    ConsumptionComparison,
    add_production_batch_substitute,
    comparable_consumption,
    create_production_batch,
    discard_production_batch,
    has_recorded_consumption,
    preview_production_batch,
    production_batch_readiness,
    record_production_output,
    remove_production_batch_substitute,
    rescale_production_batch,
    update_production_batch_actuals,
    update_production_batch_notes,
)
from apps.kitchen.production_posting import (
    SOURCE_DOCUMENT_TYPE,
    AllocationInput,
    post_production_batch,
    reverse_production_batch,
    set_production_allocations,
)
from apps.kitchen.selectors import (
    components_for_version,
    production_lines_for,
    resolve_category,
    resolve_component,
    resolve_cost_snapshot,
    resolve_line,
    resolve_production_actual,
    resolve_production_allocation,
    resolve_production_batch,
    resolve_production_line,
    resolve_recipe,
    resolve_serving,
    resolve_step,
    resolve_substitute,
    resolve_version,
    visible_cost_snapshots,
    visible_production_batches,
    visible_recipes,
    visible_step_ingredients,
    visible_versions,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_line_substitute,
    add_recipe_serving,
    add_recipe_step,
    archive_recipe,
    create_draft_recipe_version,
    create_recipe,
    create_recipe_component,
    delete_draft_recipe_version,
    link_step_ingredient,
    reactivate_recipe,
    remove_recipe_component,
    remove_recipe_line,
    remove_recipe_line_substitute,
    remove_recipe_serving,
    remove_recipe_step,
    reorder_recipe_component,
    unlink_step_ingredient,
    update_draft_recipe_version,
    update_recipe,
    update_recipe_component,
    update_recipe_line,
    update_recipe_serving,
    update_recipe_step,
)
from apps.kitchen.snapshots import create_recipe_cost_snapshot
from apps.organizations.authorization import (
    OutOfScope,
    PermissionMissing,
    require_reachable_organization_permission,
    require_warehouse_permission,
    resolve_branch,
    resolve_organization,
)
from apps.units.selectors import unit_by_code
from apps.users.models import User

router = Router(tags=["kitchen"])


def _actor(request: HttpRequest) -> User:
    """The signed-in caller. `django_auth` has already refused anonymity."""
    user: User = request.user  # type: ignore[assignment]
    return user


def _require_view(request: HttpRequest) -> User:
    actor = _actor(request)
    if not actor.has_perm(VIEW_RECIPE):
        raise PermissionMissing("view_recipe is not held.")
    return actor


# --- Schemas ----------------------------------------------------------------


class RecipeIn(Schema):
    organization_id: int
    code: str
    name_ar: str
    recipe_type: str
    name_en: str = ""
    description_ar: str = ""
    description_en: str = ""
    category_id: int | None = None
    output_item_id: int | None = None
    notes: str = ""
    source_document: str = ""
    source_page: int | None = None
    source_reference: str = ""
    source_note: str = ""


class RecipePatch(Schema):
    name_ar: str
    name_en: str = ""
    description_ar: str = ""
    description_en: str = ""
    category_id: int | None = None
    output_item_id: int | None = None
    notes: str = ""


class RecipeOut(Schema):
    id: int
    public_id: str
    code: str
    name_ar: str
    name_en: str
    recipe_type: str
    organization_id: int
    category_id: int | None
    output_item_id: int | None
    is_active: bool
    source_document: str
    source_page: int | None


class VersionIn(Schema):
    expected_output_quantity: str
    output_unit_code: str
    batch_size: str = "1"
    preparation_loss: str | None = None
    cooking_yield: str | None = None
    instructions: str = ""
    notes: str = ""
    source_document: str = ""
    source_page: int | None = None
    source_reference: str = ""
    source_note: str = ""


class VersionOut(Schema):
    id: int
    public_id: str
    recipe_id: int
    version_number: int
    status: str
    batch_size: str
    expected_output_quantity: str
    output_unit: str


class LineIn(Schema):
    item_id: int
    entered_quantity: str
    entered_unit_code: str | None = None
    package_unit_id: int | None = None
    measured_base_quantity: str | None = None
    measured_quantity: str | None = None
    loss_rate: str | None = None
    cost_class: str = RecipeLineCostClass.FOOD
    preparation_stage: str = ""
    measurement_basis: str = MeasurementBasis.RAW
    is_optional: bool = False
    note: str = ""
    source_document: str = ""
    source_page: int | None = None
    source_reference: str = ""
    source_note: str = ""


class LineOut(Schema):
    id: int
    line_order: int
    item_id: int
    entered_quantity: str
    base_quantity: str
    measured_quantity: str | None
    cost_class: str
    measurement_basis: str
    source_document: str
    source_page: int | None


class SubstituteIn(Schema):
    substitute_item_id: int
    #: Omit to take the next free rank on this line.
    priority: int | None = None
    reason: str = ""
    note: str = ""


class SubstituteOut(Schema):
    id: int
    line_id: int
    substitute_item_id: int
    priority: int
    is_active: bool


class StepIn(Schema):
    instruction_ar: str
    sequence: int | None = None
    instruction_en: str = ""
    stage: str = ""
    expected_minutes: int | None = None
    temperature_c: str | None = None
    heat_instruction_ar: str = ""
    checkpoint_ar: str = ""
    is_critical: bool = False
    media_reference: str = ""
    note: str = ""
    source_document: str = ""
    source_page: int | None = None
    source_reference: str = ""
    source_note: str = ""


class StepOut(Schema):
    id: int
    version_id: int
    sequence: int
    instruction_ar: str
    stage: str
    expected_minutes: int | None
    temperature_c: str | None
    heat_instruction_ar: str


class StepLinkIn(Schema):
    recipe_line_id: int
    share: str = "1"
    note: str = ""


class StepLinkOut(Schema):
    id: int
    step_id: int
    recipe_line_id: int
    share: str


class ServingIn(Schema):
    code: str
    name_ar: str
    serving_quantity: str
    serving_unit_code: str
    name_en: str = ""
    is_primary: bool = False
    rounding_increment: str | None = None
    rounding_policy: str = ServingRoundingPolicy.NONE
    measurement_basis: str = MeasurementBasis.COOKED
    display_order: int | None = None
    source_document: str = ""
    source_page: int | None = None
    source_reference: str = ""
    source_note: str = ""


class ServingOut(Schema):
    id: int
    version_id: int
    code: str
    name_ar: str
    serving_quantity: str
    base_quantity: str
    factor_of_batch: str
    is_primary: bool
    is_active: bool


# --- Serialisers ------------------------------------------------------------


def _recipe_out(recipe: Any) -> dict[str, Any]:
    return {
        "id": recipe.pk,
        "public_id": str(recipe.public_id),
        "code": recipe.code,
        "name_ar": recipe.name_ar,
        "name_en": recipe.name_en,
        "recipe_type": recipe.recipe_type,
        "organization_id": recipe.organization_id,
        "category_id": recipe.category_id,
        "output_item_id": recipe.output_item_id,
        "is_active": recipe.is_active,
        "source_document": recipe.source_document,
        "source_page": recipe.source_page,
    }


def _version_out(version: Any) -> dict[str, Any]:
    return {
        "id": version.pk,
        "public_id": str(version.public_id),
        "recipe_id": version.recipe_id,
        "version_number": version.version_number,
        "status": version.status,
        "batch_size": str(version.batch_size),
        "expected_output_quantity": str(version.expected_output_quantity),
        "output_unit": version.output_unit.code,
    }


def _line_out(line: Any) -> dict[str, Any]:
    return {
        "id": line.pk,
        "line_order": line.line_order,
        "item_id": line.item_id,
        "entered_quantity": str(line.entered_quantity),
        "base_quantity": str(line.base_quantity),
        "measured_quantity": str(line.measured_quantity) if line.measured_quantity else None,
        "cost_class": line.cost_class,
        "measurement_basis": line.measurement_basis,
        "source_document": line.source_document,
        "source_page": line.source_page,
    }


def _step_out(step: Any) -> dict[str, Any]:
    minutes = int(step.expected_duration.total_seconds() // 60) if step.expected_duration else None
    return {
        "id": step.pk,
        "version_id": step.version_id,
        "sequence": step.sequence,
        "instruction_ar": step.instruction_ar,
        "stage": step.stage,
        "expected_minutes": minutes,
        "temperature_c": str(step.temperature_c) if step.temperature_c is not None else None,
        "heat_instruction_ar": step.heat_instruction_ar,
    }


def _serving_out(serving: Any) -> dict[str, Any]:
    return {
        "id": serving.pk,
        "version_id": serving.version_id,
        "code": serving.code,
        "name_ar": serving.name_ar,
        "serving_quantity": str(serving.serving_quantity),
        "base_quantity": str(serving.base_quantity),
        "factor_of_batch": str(serving.factor_of_batch),
        "is_primary": serving.is_primary,
        "is_active": serving.is_active,
    }


def _decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None and value != "" else None


# --- Recipes ----------------------------------------------------------------


@router.get("/recipes", response=list[RecipeOut], summary="List recipes")
def list_recipes(request: HttpRequest, q: str = "", recipe_type: str = "") -> list[dict[str, Any]]:
    _require_view(request)
    queryset = visible_recipes(_actor(request))
    if q:
        queryset = queryset.filter(code__icontains=q)
    if recipe_type:
        queryset = queryset.filter(recipe_type=recipe_type)
    return [_recipe_out(recipe) for recipe in queryset.order_by("code")]


@router.get("/recipes/{recipe_id}", response=RecipeOut, summary="One recipe")
def get_recipe(request: HttpRequest, recipe_id: int) -> dict[str, Any]:
    _require_view(request)
    return _recipe_out(resolve_recipe(_actor(request), recipe_id))


@router.post("/recipes", response={201: RecipeOut}, summary="Create a recipe")
def post_recipe(request: HttpRequest, payload: RecipeIn) -> tuple[int, dict[str, Any]]:
    organization = resolve_organization(_actor(request), payload.organization_id)
    require_reachable_organization_permission(_actor(request), MANAGE_RECIPE, organization)
    recipe = create_recipe(
        organization=organization,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        recipe_type=payload.recipe_type,
        description_ar=payload.description_ar,
        description_en=payload.description_en,
        category=(
            resolve_category(_actor(request), payload.category_id) if payload.category_id else None
        ),
        output_item=(
            resolve_item(_actor(request), payload.output_item_id)
            if payload.output_item_id
            else None
        ),
        notes=payload.notes,
        created_by=_actor(request),
        source_document=payload.source_document,
        source_page=payload.source_page,
        source_reference=payload.source_reference,
        source_note=payload.source_note,
    )
    return 201, _recipe_out(recipe)


@router.patch("/recipes/{recipe_id}", response=RecipeOut, summary="Correct a recipe")
def patch_recipe(request: HttpRequest, recipe_id: int, payload: RecipePatch) -> dict[str, Any]:
    recipe = resolve_recipe(_actor(request), recipe_id)
    require_reachable_organization_permission(_actor(request), MANAGE_RECIPE, recipe.organization)
    return _recipe_out(
        update_recipe(
            recipe=recipe,
            name_ar=payload.name_ar,
            name_en=payload.name_en,
            description_ar=payload.description_ar,
            description_en=payload.description_en,
            category=(
                resolve_category(_actor(request), payload.category_id)
                if payload.category_id
                else None
            ),
            output_item=(
                resolve_item(_actor(request), payload.output_item_id)
                if payload.output_item_id
                else None
            ),
            notes=payload.notes,
        )
    )


@router.post("/recipes/{recipe_id}/archive", response=RecipeOut, summary="Archive a recipe")
def post_recipe_archive(request: HttpRequest, recipe_id: int, reason: str = "") -> dict[str, Any]:
    recipe = resolve_recipe(_actor(request), recipe_id)
    require_reachable_organization_permission(_actor(request), MANAGE_RECIPE, recipe.organization)
    return _recipe_out(archive_recipe(recipe=recipe, reason=reason))


@router.post("/recipes/{recipe_id}/reactivate", response=RecipeOut, summary="Reactivate a recipe")
def post_recipe_reactivate(
    request: HttpRequest, recipe_id: int, reason: str = ""
) -> dict[str, Any]:
    recipe = resolve_recipe(_actor(request), recipe_id)
    require_reachable_organization_permission(_actor(request), MANAGE_RECIPE, recipe.organization)
    return _recipe_out(reactivate_recipe(recipe=recipe, reason=reason))


# --- Draft versions ---------------------------------------------------------


@router.post(
    "/recipes/{recipe_id}/versions", response={201: VersionOut}, summary="Open a draft version"
)
def post_version(
    request: HttpRequest, recipe_id: int, payload: VersionIn
) -> tuple[int, dict[str, Any]]:
    recipe = resolve_recipe(_actor(request), recipe_id)
    require_reachable_organization_permission(_actor(request), MANAGE_RECIPE, recipe.organization)
    version = create_draft_recipe_version(
        recipe=recipe,
        expected_output_quantity=Decimal(payload.expected_output_quantity),
        output_unit=unit_by_code(payload.output_unit_code),
        batch_size=Decimal(payload.batch_size),
        preparation_loss=_decimal(payload.preparation_loss),
        cooking_yield=_decimal(payload.cooking_yield),
        instructions=payload.instructions,
        notes=payload.notes,
        created_by=_actor(request),
        source_document=payload.source_document,
        source_page=payload.source_page,
        source_reference=payload.source_reference,
        source_note=payload.source_note,
    )
    return 201, _version_out(version)


@router.patch("/versions/{version_id}", response=VersionOut, summary="Correct a draft version")
def patch_version(request: HttpRequest, version_id: int, payload: VersionIn) -> dict[str, Any]:
    version = resolve_version(_actor(request), version_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, version.recipe.organization
    )
    return _version_out(
        update_draft_recipe_version(
            version=version,
            expected_output_quantity=Decimal(payload.expected_output_quantity),
            output_unit=unit_by_code(payload.output_unit_code),
            batch_size=Decimal(payload.batch_size),
            preparation_loss=_decimal(payload.preparation_loss),
            cooking_yield=_decimal(payload.cooking_yield),
            instructions=payload.instructions,
            notes=payload.notes,
        )
    )


@router.delete("/versions/{version_id}", response={204: None}, summary="Discard a draft version")
def delete_version(request: HttpRequest, version_id: int, reason: str = "") -> tuple[int, None]:
    version = resolve_version(_actor(request), version_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, version.recipe.organization
    )
    delete_draft_recipe_version(version=version, reason=reason)
    return 204, None


# --- Lines ------------------------------------------------------------------


@router.post("/versions/{version_id}/lines", response={201: LineOut}, summary="Add a line")
def post_line(request: HttpRequest, version_id: int, payload: LineIn) -> tuple[int, dict[str, Any]]:
    version = resolve_version(_actor(request), version_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, version.recipe.organization
    )
    line = add_recipe_line(
        version=version,
        item=resolve_item(_actor(request), payload.item_id),
        entered_quantity=Decimal(payload.entered_quantity),
        entered_unit=(
            unit_by_code(payload.entered_unit_code) if payload.entered_unit_code else None
        ),
        package_unit=(
            resolve_package_unit(_actor(request), payload.package_unit_id)
            if payload.package_unit_id
            else None
        ),
        measured_base_quantity=_decimal(payload.measured_base_quantity),
        measured_quantity=_decimal(payload.measured_quantity),
        loss_rate=_decimal(payload.loss_rate),
        cost_class=payload.cost_class,
        preparation_stage=payload.preparation_stage,
        measurement_basis=payload.measurement_basis,
        is_optional=payload.is_optional,
        note=payload.note,
        source_document=payload.source_document,
        source_page=payload.source_page,
        source_reference=payload.source_reference,
        source_note=payload.source_note,
    )
    return 201, _line_out(line)


@router.patch("/lines/{line_id}", response=LineOut, summary="Correct a line")
def patch_line(request: HttpRequest, line_id: int, payload: LineIn) -> dict[str, Any]:
    line = resolve_line(_actor(request), line_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, line.version.recipe.organization
    )
    return _line_out(
        update_recipe_line(
            line=line,
            entered_quantity=Decimal(payload.entered_quantity),
            entered_unit=(
                unit_by_code(payload.entered_unit_code) if payload.entered_unit_code else None
            ),
            package_unit=(
                resolve_package_unit(_actor(request), payload.package_unit_id)
                if payload.package_unit_id
                else None
            ),
            measured_base_quantity=_decimal(payload.measured_base_quantity),
            measured_quantity=_decimal(payload.measured_quantity),
            loss_rate=_decimal(payload.loss_rate),
            cost_class=payload.cost_class,
            preparation_stage=payload.preparation_stage,
            measurement_basis=payload.measurement_basis,
            is_optional=payload.is_optional,
            note=payload.note,
        )
    )


@router.delete("/lines/{line_id}", response={204: None}, summary="Remove a line")
def delete_line(request: HttpRequest, line_id: int, reason: str = "") -> tuple[int, None]:
    line = resolve_line(_actor(request), line_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, line.version.recipe.organization
    )
    remove_recipe_line(line=line, reason=reason)
    return 204, None


# --- Substitutes ------------------------------------------------------------


@router.post(
    "/lines/{line_id}/substitutes", response={201: SubstituteOut}, summary="Add a substitute"
)
def post_substitute(
    request: HttpRequest, line_id: int, payload: SubstituteIn
) -> tuple[int, dict[str, Any]]:
    line = resolve_line(_actor(request), line_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, line.version.recipe.organization
    )
    substitute = add_recipe_line_substitute(
        line=line,
        substitute_item=resolve_item(_actor(request), payload.substitute_item_id),
        priority=payload.priority,
        reason=payload.reason,
        note=payload.note,
    )
    return 201, {
        "id": substitute.pk,
        "line_id": substitute.line_id,
        "substitute_item_id": substitute.substitute_item_id,
        "priority": substitute.priority,
        "is_active": substitute.is_active,
    }


@router.delete("/substitutes/{substitute_id}", response={204: None}, summary="Remove a substitute")
def delete_substitute(
    request: HttpRequest, substitute_id: int, reason: str = ""
) -> tuple[int, None]:
    substitute = resolve_substitute(_actor(request), substitute_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, substitute.line.version.recipe.organization
    )
    remove_recipe_line_substitute(substitute=substitute, reason=reason)
    return 204, None


# --- Steps ------------------------------------------------------------------


@router.post("/versions/{version_id}/steps", response={201: StepOut}, summary="Add a step")
def post_step(request: HttpRequest, version_id: int, payload: StepIn) -> tuple[int, dict[str, Any]]:
    version = resolve_version(_actor(request), version_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, version.recipe.organization
    )
    step = add_recipe_step(
        version=version,
        sequence=payload.sequence,
        instruction_ar=payload.instruction_ar,
        instruction_en=payload.instruction_en,
        stage=payload.stage,
        expected_duration=(
            datetime.timedelta(minutes=payload.expected_minutes)
            if payload.expected_minutes
            else None
        ),
        temperature_c=_decimal(payload.temperature_c),
        heat_instruction_ar=payload.heat_instruction_ar,
        checkpoint_ar=payload.checkpoint_ar,
        is_critical=payload.is_critical,
        media_reference=payload.media_reference,
        note=payload.note,
        source_document=payload.source_document,
        source_page=payload.source_page,
        source_reference=payload.source_reference,
        source_note=payload.source_note,
    )
    return 201, _step_out(step)


@router.patch("/steps/{step_id}", response=StepOut, summary="Correct a step")
def patch_step(request: HttpRequest, step_id: int, payload: StepIn) -> dict[str, Any]:
    step = resolve_step(_actor(request), step_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, step.version.recipe.organization
    )
    return _step_out(
        update_recipe_step(
            step=step,
            sequence=payload.sequence,
            instruction_ar=payload.instruction_ar,
            instruction_en=payload.instruction_en,
            stage=payload.stage,
            expected_duration=(
                datetime.timedelta(minutes=payload.expected_minutes)
                if payload.expected_minutes
                else None
            ),
            temperature_c=_decimal(payload.temperature_c),
            heat_instruction_ar=payload.heat_instruction_ar,
            checkpoint_ar=payload.checkpoint_ar,
            is_critical=payload.is_critical,
            media_reference=payload.media_reference,
            note=payload.note,
        )
    )


@router.delete("/steps/{step_id}", response={204: None}, summary="Remove a step")
def delete_step(request: HttpRequest, step_id: int, reason: str = "") -> tuple[int, None]:
    step = resolve_step(_actor(request), step_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, step.version.recipe.organization
    )
    remove_recipe_step(step=step, reason=reason)
    return 204, None


@router.post("/steps/{step_id}/links", response={201: StepLinkOut}, summary="Link an ingredient")
def post_step_link(
    request: HttpRequest, step_id: int, payload: StepLinkIn
) -> tuple[int, dict[str, Any]]:
    step = resolve_step(_actor(request), step_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, step.version.recipe.organization
    )
    link = link_step_ingredient(
        step=step,
        recipe_line=resolve_line(_actor(request), payload.recipe_line_id),
        share=Decimal(payload.share),
        note=payload.note,
    )
    return 201, {
        "id": link.pk,
        "step_id": link.step_id,
        "recipe_line_id": link.recipe_line_id,
        "share": str(link.share),
    }


@router.delete("/step-links/{link_id}", response={204: None}, summary="Unlink an ingredient")
def delete_step_link(request: HttpRequest, link_id: int, reason: str = "") -> tuple[int, None]:
    link = visible_step_ingredients(_actor(request)).filter(pk=link_id).first()
    if link is None:
        from apps.organizations.authorization import OutOfScope

        raise OutOfScope(f"RecipeStepIngredient {link_id} does not exist.")
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, link.step.version.recipe.organization
    )
    unlink_step_ingredient(link=link, reason=reason)
    return 204, None


# --- Servings ---------------------------------------------------------------


@router.post("/versions/{version_id}/servings", response={201: ServingOut}, summary="Add a serving")
def post_serving(
    request: HttpRequest, version_id: int, payload: ServingIn
) -> tuple[int, dict[str, Any]]:
    version = resolve_version(_actor(request), version_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, version.recipe.organization
    )
    serving = add_recipe_serving(
        version=version,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        serving_quantity=Decimal(payload.serving_quantity),
        serving_unit=unit_by_code(payload.serving_unit_code),
        is_primary=payload.is_primary,
        rounding_increment=_decimal(payload.rounding_increment),
        rounding_policy=payload.rounding_policy,
        measurement_basis=payload.measurement_basis,
        display_order=payload.display_order,
        source_document=payload.source_document,
        source_page=payload.source_page,
        source_reference=payload.source_reference,
        source_note=payload.source_note,
    )
    return 201, _serving_out(serving)


@router.patch("/servings/{serving_id}", response=ServingOut, summary="Correct a serving")
def patch_serving(request: HttpRequest, serving_id: int, payload: ServingIn) -> dict[str, Any]:
    serving = resolve_serving(_actor(request), serving_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, serving.version.recipe.organization
    )
    return _serving_out(
        update_recipe_serving(
            serving=serving,
            name_ar=payload.name_ar,
            name_en=payload.name_en,
            serving_quantity=Decimal(payload.serving_quantity),
            serving_unit=unit_by_code(payload.serving_unit_code),
            is_primary=payload.is_primary,
            rounding_increment=_decimal(payload.rounding_increment),
            rounding_policy=payload.rounding_policy,
            measurement_basis=payload.measurement_basis,
            display_order=payload.display_order,
        )
    )


@router.delete("/servings/{serving_id}", response={204: None}, summary="Remove a serving")
def delete_serving(request: HttpRequest, serving_id: int, reason: str = "") -> tuple[int, None]:
    serving = resolve_serving(_actor(request), serving_id)
    require_reachable_organization_permission(
        _actor(request), MANAGE_RECIPE, serving.version.recipe.organization
    )
    remove_recipe_serving(serving=serving, reason=reason)
    return 204, None


# --- The version lifecycle ---------------------------------------------------
#
# Command endpoints, not writable CRUD. A version that has left `DRAFT` has no
# `PATCH` and no `DELETE`: correcting an approved recipe is a new version, and
# offering a verb that implied otherwise would be the API contradicting the
# database.


class LifecycleVersionOut(Schema):
    """The version with everything the lifecycle added to it."""

    id: int
    public_id: str
    recipe_id: int
    recipe_code: str
    version_number: int
    status: str
    batch_size: str
    expected_output_quantity: str
    output_unit: str
    effective_from: str | None
    effective_to: str | None
    submitted_by: str | None
    submitted_at: str | None
    approved_by: str | None
    approved_at: str | None
    approval_reference: str
    approval_evidence_kind: str
    activated_by: str | None
    activated_at: str | None
    rejected_by: str | None
    rejected_at: str | None
    rejection_reason: str
    superseded_at: str | None
    superseded_by_version_id: int | None
    branch_scopes: list[dict[str, Any]]
    reviews: list[dict[str, Any]]


class ReviewIn(Schema):
    review_type: str
    decision: str
    reason: str = ""
    evidence_reference: str = ""
    evidence_kind: str = ""
    note: str = ""


class ApproveIn(Schema):
    approval_reference: str
    approval_evidence_kind: str
    note: str = ""


class RejectIn(Schema):
    reason: str


class ActivateIn(Schema):
    #: ISO dates, exactly as decimals cross as strings. A date is a string in
    #: both directions here and never a timestamp somebody has to interpret in
    #: a timezone the API never stated.
    effective_from: str
    effective_to: str | None = None
    #: `None` means organization-wide, which activation materialises into one
    #: scope row per applicable branch.
    branch_ids: list[int] | None = None
    supersedes_version_id: int | None = None
    reason: str = ""


class SupersedeIn(Schema):
    replacement_version_id: int
    reason: str = ""


def _moment(value: Any) -> str | None:
    return value.isoformat() if value else None


def _actor_label(user: Any) -> str | None:
    return str(user) if user else None


def _review_out(review: Any) -> dict[str, Any]:
    return {
        "id": review.pk,
        "review_type": review.review_type,
        "decision": review.decision,
        "reviewer": str(review.reviewer),
        "reviewed_at": _moment(review.reviewed_at),
        "reason": review.reason,
        "evidence_reference": review.evidence_reference,
        "evidence_kind": review.evidence_kind,
    }


def _scope_out(scope: Any) -> dict[str, Any]:
    return {
        "id": scope.pk,
        "branch_id": scope.branch_id,
        "branch_code": scope.branch.code,
        "effective_from": scope.effective_from.isoformat(),
        "effective_to": _moment(scope.effective_to),
        "is_organization_wide": scope.is_organization_wide,
    }


def _lifecycle_out(version: Any) -> dict[str, Any]:
    return {
        **_version_out(version),
        "recipe_code": version.recipe.code,
        "effective_from": _moment(version.effective_from),
        "effective_to": _moment(version.effective_to),
        "submitted_by": _actor_label(version.submitted_by),
        "submitted_at": _moment(version.submitted_at),
        "approved_by": _actor_label(version.approved_by),
        "approved_at": _moment(version.approved_at),
        "approval_reference": version.approval_reference,
        "approval_evidence_kind": version.approval_evidence_kind,
        "activated_by": _actor_label(version.activated_by),
        "activated_at": _moment(version.activated_at),
        "rejected_by": _actor_label(version.rejected_by),
        "rejected_at": _moment(version.rejected_at),
        "rejection_reason": version.rejection_reason,
        "superseded_at": _moment(version.superseded_at),
        "superseded_by_version_id": version.superseded_by_version_id,
        "branch_scopes": [
            _scope_out(scope)
            for scope in version.branch_scopes.select_related("branch").order_by("branch__code")
        ],
        "reviews": [
            _review_out(review)
            for review in version.reviews.select_related("reviewer").order_by("review_type")
        ],
    }


def _lifecycle_version(request: HttpRequest, version_id: int, permission: str) -> Any:
    """Resolve with the caller, then check the authority. Never the other way."""
    version = resolve_version(_actor(request), version_id)
    require_reachable_organization_permission(
        _actor(request), permission, version.recipe.organization
    )
    return version


@router.get("/recipe-versions", response=list[LifecycleVersionOut], summary="List recipe versions")
def list_recipe_versions(
    request: HttpRequest,
    recipe_id: int | None = None,
    status: str = "",
    branch_id: int | None = None,
    on_date: str = "",
) -> list[dict[str, Any]]:
    actor = _require_view(request)
    versions = visible_versions(actor).select_related("recipe")
    if recipe_id is not None:
        versions = versions.filter(recipe_id=recipe_id)
    if status:
        versions = versions.filter(status=status)
    if branch_id is not None:
        versions = versions.filter(branch_scopes__branch_id=branch_id)
    if on_date:
        asked = datetime.date.fromisoformat(on_date)
        versions = versions.filter(branch_scopes__effective_from__lte=asked).filter(
            covers_on_date(asked)
        )
    return [_lifecycle_out(version) for version in versions.distinct().order_by("-pk")[:200]]


@router.get(
    "/recipe-versions/{version_id}", response=LifecycleVersionOut, summary="One recipe version"
)
def get_recipe_version(request: HttpRequest, version_id: int) -> dict[str, Any]:
    _require_view(request)
    return _lifecycle_out(resolve_version(_actor(request), version_id))


@router.post(
    "/recipe-versions/{version_id}/submit",
    response=LifecycleVersionOut,
    summary="Submit a draft for review",
)
def post_submit(request: HttpRequest, version_id: int) -> dict[str, Any]:
    version = _lifecycle_version(request, version_id, SUBMIT_RECIPE_VERSION)
    return _lifecycle_out(submit_recipe_version(version=version, actor=_actor(request)))


@router.post(
    "/recipe-versions/{version_id}/review",
    response={201: LifecycleVersionOut},
    summary="Record a review signoff",
)
def post_review(
    request: HttpRequest, version_id: int, payload: ReviewIn
) -> tuple[int, dict[str, Any]]:
    version = _lifecycle_version(request, version_id, REVIEW_RECIPE_VERSION)
    record_recipe_version_review(
        version=version,
        review_type=payload.review_type,
        reviewer=_actor(request),
        decision=payload.decision,
        reason=payload.reason,
        evidence_reference=payload.evidence_reference,
        evidence_kind=payload.evidence_kind,
        note=payload.note,
    )
    version.refresh_from_db()
    return 201, _lifecycle_out(version)


@router.post(
    "/recipe-versions/{version_id}/approve",
    response=LifecycleVersionOut,
    summary="Give the final approval",
)
def post_approve(request: HttpRequest, version_id: int, payload: ApproveIn) -> dict[str, Any]:
    version = _lifecycle_version(request, version_id, APPROVE_RECIPE_VERSION)
    return _lifecycle_out(
        approve_recipe_version(
            version=version,
            actor=_actor(request),
            approval_reference=payload.approval_reference,
            approval_evidence_kind=payload.approval_evidence_kind,
            note=payload.note,
        )
    )


@router.post(
    "/recipe-versions/{version_id}/reject",
    response=LifecycleVersionOut,
    summary="Refuse a submitted version",
)
def post_reject(request: HttpRequest, version_id: int, payload: RejectIn) -> dict[str, Any]:
    version = _lifecycle_version(request, version_id, REJECT_RECIPE_VERSION)
    return _lifecycle_out(
        reject_recipe_version(version=version, actor=_actor(request), reason=payload.reason)
    )


@router.post(
    "/recipe-versions/{version_id}/activate",
    response=LifecycleVersionOut,
    summary="Put an approved version into effect",
)
def post_activate(request: HttpRequest, version_id: int, payload: ActivateIn) -> dict[str, Any]:
    version = _lifecycle_version(request, version_id, ACTIVATE_RECIPE_VERSION)
    branches = None
    if payload.branch_ids is not None:
        # Resolved with the caller, so a submitted id can never widen scope.
        branches = [resolve_branch(_actor(request), pk) for pk in payload.branch_ids]
    supersedes = (
        resolve_version(_actor(request), payload.supersedes_version_id)
        if payload.supersedes_version_id is not None
        else None
    )
    return _lifecycle_out(
        activate_recipe_version(
            version=version,
            actor=_actor(request),
            effective_from=datetime.date.fromisoformat(payload.effective_from),
            effective_to=(
                datetime.date.fromisoformat(payload.effective_to) if payload.effective_to else None
            ),
            branches=branches,
            supersedes=supersedes,
            reason=payload.reason,
        )
    )


@router.post(
    "/recipe-versions/{version_id}/supersede",
    response=LifecycleVersionOut,
    summary="Close an active version in favour of a named replacement",
)
def post_supersede(request: HttpRequest, version_id: int, payload: SupersedeIn) -> dict[str, Any]:
    version = _lifecycle_version(request, version_id, ACTIVATE_RECIPE_VERSION)
    replacement = resolve_version(_actor(request), payload.replacement_version_id)
    return _lifecycle_out(
        supersede_recipe_version(
            version=version,
            replacement=replacement,
            actor=_actor(request),
            reason=payload.reason,
        )
    )


@router.get(
    "/recipes/{recipe_id}/effective-version",
    response=LifecycleVersionOut,
    summary="The version in effect for a branch on a business date",
)
def get_effective_version(
    request: HttpRequest, recipe_id: int, branch_id: int, on_date: str
) -> dict[str, Any]:
    """
    The resolver, exposed.

    `on_date` is required and has no default. A posting-facing read that
    quietly meant *today* would give the right answer during development and
    the wrong one the first time somebody re-ran a July report in September.
    """
    actor = _require_view(request)
    recipe = resolve_recipe(actor, recipe_id)
    branch = resolve_branch(actor, branch_id)
    version = resolve_recipe_version(
        recipe=recipe, branch=branch, on_date=datetime.date.fromisoformat(on_date)
    )
    return _lifecycle_out(version)


# --- Nested components -------------------------------------------------------
#
# Draft-only, and named that way in every summary. There is deliberately **no**
# writable route for a component under a frozen parent: adopting a different
# child version is a new parent version, not a PATCH, and an endpoint that
# offered otherwise would be the one place the whole exact-version rule leaked.
#
# No cost route and no flatten route **on a component**. Task 3.3 owns roll-up
# and Task 3.4 owns flattening, and both reach it through their own documents:
# the cost card, and the production preview at the end of this module. Exposing
# a flattened view of a component in isolation would invite a caller to treat it
# as a plan, which it is not — a plan names a branch, a date and a warehouse.


class ComponentOut(Schema):
    id: int
    version_id: int
    line_order: int
    component_version_id: int
    component_recipe_code: str
    component_recipe_name: str
    component_version_number: int
    component_version_status: str
    #: An exact string, never a JSON number. A JSON number is a binary float
    #: before any Python code sees it, and this is a conversion factor.
    multiplier: str
    note: str


class ComponentIn(Schema):
    component_version_id: int
    multiplier: str
    line_order: int | None = None
    note: str = ""
    source_document: str = ""
    source_page: int | None = None
    source_reference: str = ""
    source_note: str = ""


class ComponentPatchIn(Schema):
    multiplier: str
    component_version_id: int | None = None
    note: str = ""


class ComponentReorderIn(Schema):
    line_order: int


def _component_out(component: Any) -> dict[str, Any]:
    return {
        "id": component.pk,
        "version_id": component.version_id,
        "line_order": component.line_order,
        "component_version_id": component.component_version_id,
        "component_recipe_code": component.component_recipe.code,
        "component_recipe_name": component.component_recipe.name_ar,
        "component_version_number": component.component_version.version_number,
        "component_version_status": component.component_version.status,
        "multiplier": component.multiplier_display,
        "note": component.note,
    }


@router.get(
    "/recipe-versions/{version_id}/components",
    response=list[ComponentOut],
    summary="The nested sub-recipes of one version",
)
def list_components(request: HttpRequest, version_id: int) -> list[dict[str, Any]]:
    actor = _require_view(request)
    version = resolve_version(actor, version_id)
    return [_component_out(component) for component in components_for_version(version)]


@router.post(
    "/recipe-versions/{version_id}/components",
    response={201: ComponentOut},
    summary="Add a nested sub-recipe to a DRAFT version",
)
def post_component(
    request: HttpRequest, version_id: int, payload: ComponentIn
) -> tuple[int, dict[str, Any]]:
    actor = _actor(request)
    version = resolve_version(actor, version_id)
    require_reachable_organization_permission(actor, MANAGE_RECIPE, version.recipe.organization)
    # The child is resolved **through the caller** as well. A submitted id may
    # only ever select from what the caller already reaches; it can never widen
    # the scope it is submitted into (ADR-016).
    child = resolve_version(actor, payload.component_version_id)
    component = create_recipe_component(
        version=version,
        component_version=child,
        multiplier=Decimal(payload.multiplier),
        line_order=payload.line_order,
        note=payload.note,
        actor=actor,
        source_document=payload.source_document,
        source_page=payload.source_page,
        source_reference=payload.source_reference,
        source_note=payload.source_note,
    )
    return 201, _component_out(component)


@router.patch(
    "/recipe-components/{component_id}",
    response=ComponentOut,
    summary="Correct a nested sub-recipe on a DRAFT version",
)
def patch_component(
    request: HttpRequest, component_id: int, payload: ComponentPatchIn
) -> dict[str, Any]:
    actor = _actor(request)
    component = resolve_component(actor, component_id)
    require_reachable_organization_permission(actor, MANAGE_RECIPE, component.recipe.organization)
    child = (
        resolve_version(actor, payload.component_version_id)
        if payload.component_version_id is not None
        else None
    )
    return _component_out(
        update_recipe_component(
            component=component,
            multiplier=Decimal(payload.multiplier),
            component_version=child,
            note=payload.note,
        )
    )


@router.post(
    "/recipe-components/{component_id}/reorder",
    response=list[ComponentOut],
    summary="Move a nested sub-recipe within a DRAFT version",
)
def post_component_reorder(
    request: HttpRequest, component_id: int, payload: ComponentReorderIn
) -> list[dict[str, Any]]:
    actor = _actor(request)
    component = resolve_component(actor, component_id)
    require_reachable_organization_permission(actor, MANAGE_RECIPE, component.recipe.organization)
    ordered = reorder_recipe_component(component=component, line_order=payload.line_order)
    return [_component_out(row) for row in ordered]


@router.delete(
    "/recipe-components/{component_id}",
    response={204: None},
    summary="Remove a nested sub-recipe from a DRAFT version",
)
def delete_component(request: HttpRequest, component_id: int, reason: str = "") -> tuple[int, None]:
    actor = _actor(request)
    component = resolve_component(actor, component_id)
    require_reachable_organization_permission(actor, MANAGE_RECIPE, component.recipe.organization)
    remove_recipe_component(component=component, reason=reason)
    return 204, None


class ComponentTreeNodeOut(Schema):
    depth: int
    line_order: int
    recipe_code: str
    recipe_name: str
    version_number: int
    version_status: str
    multiplier: str
    cumulative_multiplier: str
    note: str


@router.get(
    "/recipe-versions/{version_id}/component-tree",
    response=list[ComponentTreeNodeOut],
    summary="The whole nested tree under one version, parents before children",
)
def get_component_tree(request: HttpRequest, version_id: int) -> list[dict[str, Any]]:
    """
    Read-only, and carries no cost column.

    `cumulative_multiplier` is the product of every multiplier from the root
    down, at full precision (RCP-073). It is a scaling identity for display; the
    quantity it will eventually scale is quantized once, at a production batch
    line, which is Task 3.4's.
    """
    actor = _require_view(request)
    version = resolve_version(actor, version_id)
    return [
        {
            "depth": node.depth,
            "line_order": node.line_order,
            "recipe_code": node.recipe.code,
            "recipe_name": node.recipe.name_ar,
            "version_number": node.version.version_number,
            "version_status": node.version.status,
            "multiplier": node.multiplier_display,
            "cumulative_multiplier": node.cumulative_display,
            "note": node.note,
        }
        for node in flatten_tree(component_tree(version))
    ]


# ---------------------------------------------------------------------------
# Task 3.3 - recipe costing and cost snapshots
# ---------------------------------------------------------------------------
#
# Every route below names its warehouse and its date explicitly. None of them
# defaults either one: a costing read that quietly meant *today* would be right
# during development and wrong the first time somebody re-ran a July card in
# September, and one that defaulted a warehouse would price a Baghdad recipe
# off whichever store happened to sort first.


def _require_cost(request: HttpRequest, organization: Any) -> User:
    """
    Reaching the organization is not enough; money needs its own permission.

    `view_recipe` and `review_recipe_version` both let somebody read the card
    and neither lets them read what it cost - that is RCP-027 and the same
    boundary inventory draws between `view_stock` and `view_valuation`. Checked
    against the **recipe's** organization, so a permission held elsewhere does
    not travel.
    """
    actor = _actor(request)
    require_reachable_organization_permission(actor, VIEW_RECIPE_COST, organization)
    return actor


class CostLineOut(Schema):
    line_number: int
    component_path: str
    source_kind: str
    source_recipe_code: str
    source_version_number: int
    recipe_line_id: int
    item_code: str
    item_name: str
    cost_class: str
    cumulative_multiplier: str
    effective_quantity: str
    valuation_quantity: str
    valuation_value: str
    valuation_lots: int
    unit_cost: str
    raw_extension: str
    allocated_extension: str


class MissingValuationOut(Schema):
    code: str
    item_code: str
    item_name: str
    warehouse_code: str
    component_path: str
    recipe_code: str
    version_number: int
    state: str


class CostServingOut(Schema):
    """
    One serving scenario, with its whole allocation in five numbers.

    `normal_*` and `elevated_*` are the complete distribution rather than a
    summary of it: every whole serving carries equal weight, so the certified
    allocator produces exactly two amounts and these are both, with their
    counts. `normal_count x normal + elevated_count x elevated + leftover`
    reconstructs `allocated_total` exactly, at any serving count.
    """

    code: str
    name_ar: str
    name_en: str
    is_primary: bool
    factor_of_batch: str
    cost_per_serving: str
    whole_serving_count: int
    remainder_quantity: str
    allocation_state: str
    allocated_total: str
    normal_cost_per_serving: str
    normal_serving_count: int
    elevated_cost_per_serving: str
    elevated_serving_count: int
    remainder_cost: str


class CostCardOut(Schema):
    recipe_code: str
    recipe_name: str
    version_number: int
    version_status: str
    warehouse_code: str
    branch_code: str
    as_of_date: datetime.date
    valuation_mode: str
    ledger_cutoff_sequence: int
    calculation_version: str
    is_authoritative: bool
    is_complete: bool
    output_quantity: str
    output_unit_code: str
    food_total: str
    packaging_total: str
    accompaniment_total: str
    total_material_cost: str
    cost_per_output_unit: str
    #: The plate-cost basis. `plate_cost` and `portions_per_batch` are absent
    #: only on a preview of a draft with no primary serving, and then
    #: `plate_cost_problem` says which.
    plate_cost: str | None
    portions_per_batch: str | None
    primary_serving_code: str | None
    plate_cost_problem: str | None
    lines: list[CostLineOut]
    missing_valuations: list[MissingValuationOut]
    servings: list[CostServingOut]


def _card_out(card: RecipeCostCard) -> dict[str, Any]:
    """
    Serialize a cost card. **Every decimal crosses as a quoted string.**

    JSON's only numeric type is binary floating point, so a total that left here
    as a number would arrive as a float in whatever language read it, and a
    costing figure that has been through a float is no longer the figure that
    was approved.
    """
    return {
        "recipe_code": card.recipe.code,
        "recipe_name": card.recipe.name_ar,
        "version_number": card.version.version_number,
        "version_status": card.version_status,
        "warehouse_code": card.warehouse.code,
        "branch_code": card.branch.code,
        "as_of_date": card.as_of_date,
        "valuation_mode": card.valuation_mode,
        "ledger_cutoff_sequence": card.cutoff.posted_sequence,
        "calculation_version": card.calculation_version,
        "is_authoritative": card.is_authoritative,
        "is_complete": card.is_complete,
        "output_quantity": str(card.output_quantity),
        "output_unit_code": card.output_unit_code,
        "food_total": str(card.food_total),
        "packaging_total": str(card.packaging_total),
        "accompaniment_total": str(card.accompaniment_total),
        "total_material_cost": str(card.total_material_cost),
        "cost_per_output_unit": str(card.cost_per_output_unit),
        "plate_cost": str(card.plate.plate_cost) if card.plate else None,
        "portions_per_batch": str(card.plate.portions_per_batch) if card.plate else None,
        "primary_serving_code": card.plate.serving.code if card.plate else None,
        "plate_cost_problem": card.plate_problem.code if card.plate_problem else None,
        "lines": [
            {
                "line_number": line.line_number,
                "component_path": line.path_display,
                "source_kind": str(line.kind),
                "source_recipe_code": line.source_recipe.code,
                "source_version_number": line.source_version.version_number,
                "recipe_line_id": line.recipe_line.pk,
                "item_code": line.item.code,
                "item_name": line.item.name_ar,
                "cost_class": line.cost_class,
                "cumulative_multiplier": line.multiplier_display,
                "effective_quantity": str(line.effective_quantity),
                "valuation_quantity": str(line.valuation.quantity),
                "valuation_value": str(line.valuation.value),
                "valuation_lots": line.valuation.lot_count,
                "unit_cost": str(line.unit_cost),
                "raw_extension": str(line.raw_extension),
                "allocated_extension": str(line.allocated_extension),
            }
            for line in card.lines
        ],
        "missing_valuations": [
            {
                "code": gap.code,
                "item_code": gap.item_code,
                "item_name": gap.item_name,
                "warehouse_code": gap.warehouse_code,
                "component_path": gap.component_path,
                "recipe_code": gap.recipe_code,
                "version_number": gap.version_number,
                "state": str(gap.state),
            }
            for gap in card.missing
        ],
        "servings": [
            {
                "code": serving.serving.code,
                "name_ar": serving.serving.name_ar,
                "name_en": serving.serving.name_en,
                "is_primary": serving.serving.is_primary,
                "factor_of_batch": serving.factor_display,
                "cost_per_serving": str(serving.cost_per_serving),
                "whole_serving_count": serving.whole_count,
                "remainder_quantity": str(serving.remainder_quantity),
                "allocation_state": str(serving.state),
                "allocated_total": str(serving.allocated_total),
                "normal_cost_per_serving": str(serving.normal_cost_per_serving),
                "normal_serving_count": serving.normal_serving_count,
                "elevated_cost_per_serving": str(serving.elevated_cost_per_serving),
                "elevated_serving_count": serving.elevated_serving_count,
                "remainder_cost": str(serving.remainder_cost),
            }
            for serving in card.servings
        ],
    }


@router.get(
    "/recipe-versions/{version_id}/cost-preview",
    response=CostCardOut,
    summary="A non-authoritative costing preview of a DRAFT or SUBMITTED version",
)
def get_cost_preview(
    request: HttpRequest, version_id: int, warehouse_id: int, as_of_date: datetime.date
) -> dict[str, Any]:
    """
    The card a reviewer is being asked to sign, before anybody signs it.

    `is_authoritative` is **false** in the payload and no snapshot can be built
    from this. Refused for a version that has already left `DRAFT` or
    `SUBMITTED`: an approved version has an authoritative answer and should be
    asked for it.
    """
    actor = _actor(request)
    version = resolve_version(actor, version_id)
    _require_cost(request, version.recipe.organization)
    warehouse = resolve_manageable_warehouse(actor, warehouse_id)
    return _card_out(
        preview_recipe_cost(version=version, warehouse=warehouse, as_of_date=as_of_date)
    )


@router.get(
    "/recipe-versions/{version_id}/cost",
    response=CostCardOut,
    summary="The authoritative cost of one exact version",
)
def get_version_cost(
    request: HttpRequest, version_id: int, warehouse_id: int, as_of_date: datetime.date
) -> dict[str, Any]:
    """
    Costs the **exact** version named, with no resolver anywhere in the path.

    A nested child is followed through its parent's frozen `component_version`
    and never re-resolved by date, so this answers the same way after that child
    is superseded as before (RCP-072).
    """
    actor = _actor(request)
    version = resolve_version(actor, version_id)
    _require_cost(request, version.recipe.organization)
    warehouse = resolve_manageable_warehouse(actor, warehouse_id)
    return _card_out(
        cost_recipe_version(version=version, warehouse=warehouse, as_of_date=as_of_date)
    )


@router.get(
    "/recipes/{recipe_id}/cost-on-date",
    response=CostCardOut,
    summary="What this recipe cost at this branch on this date",
)
def get_recipe_cost_on_date(
    request: HttpRequest,
    recipe_id: int,
    branch_id: int,
    warehouse_id: int,
    on_date: datetime.date,
) -> dict[str, Any]:
    """
    Version first, then costs - both halves driven by the same date (RCP-026).

    Returns `recipe_version_not_effective` for a date no version covers, which
    is the honest answer to "what did it cost before it existed".
    """
    actor = _actor(request)
    recipe = resolve_recipe(actor, recipe_id)
    _require_cost(request, recipe.organization)
    branch = resolve_branch(actor, branch_id)
    warehouse = resolve_manageable_warehouse(actor, warehouse_id)
    return _card_out(
        cost_recipe_on_date(recipe=recipe, branch=branch, warehouse=warehouse, on_date=on_date)
    )


class CostSnapshotIn(Schema):
    warehouse_id: int
    as_of_date: datetime.date
    idempotency_key: str
    reference: str = ""
    reason: str = ""
    note: str = ""


class CostSnapshotOut(Schema):
    id: int
    public_id: str
    recipe_code: str
    version_number: int
    version_status: str
    branch_code: str
    warehouse_code: str
    as_of_date: datetime.date
    valuation_mode: str
    ledger_cutoff_sequence: int
    calculation_version: str
    is_authoritative: bool
    output_quantity: str
    output_unit_code: str
    food_total: str
    packaging_total: str
    accompaniment_total: str
    total_material_cost: str
    cost_per_output_unit: str
    plate_cost: str
    portions_per_batch: str
    primary_serving_code: str
    reference: str
    reason: str
    note: str
    created_at: datetime.datetime


class CostSnapshotDetailOut(CostSnapshotOut):
    lines: list[CostLineOut]
    servings: list[CostServingOut]


def _snapshot_out(snapshot: Any) -> dict[str, Any]:
    return {
        "id": snapshot.pk,
        "public_id": str(snapshot.public_id),
        "recipe_code": snapshot.recipe_code,
        "version_number": snapshot.version_number,
        "version_status": snapshot.version_status,
        "branch_code": snapshot.branch.code,
        "warehouse_code": snapshot.warehouse_code,
        "as_of_date": snapshot.as_of_date,
        "valuation_mode": snapshot.valuation_mode,
        "ledger_cutoff_sequence": snapshot.ledger_cutoff_sequence,
        "calculation_version": snapshot.calculation_version,
        "is_authoritative": snapshot.is_authoritative,
        "output_quantity": str(snapshot.output_quantity),
        "output_unit_code": snapshot.output_unit_code,
        "food_total": str(snapshot.food_total),
        "packaging_total": str(snapshot.packaging_total),
        "accompaniment_total": str(snapshot.accompaniment_total),
        "total_material_cost": str(snapshot.total_material_cost),
        "cost_per_output_unit": str(snapshot.cost_per_output_unit),
        "plate_cost": str(snapshot.plate_cost),
        "portions_per_batch": str(snapshot.portions_per_batch),
        "primary_serving_code": snapshot.primary_serving_code,
        "reference": snapshot.reference,
        "reason": snapshot.reason,
        "note": snapshot.note,
        "created_at": snapshot.created_at,
    }


def _snapshot_detail_out(snapshot: Any) -> dict[str, Any]:
    payload = _snapshot_out(snapshot)
    payload["lines"] = [
        {
            "line_number": line.line_number,
            "component_path": line.component_path,
            "source_kind": line.source_kind,
            "source_recipe_code": line.source_recipe_code,
            "source_version_number": line.source_version_number,
            "recipe_line_id": line.recipe_line_id,
            "item_code": line.item_code,
            "item_name": line.item_name,
            "cost_class": line.cost_class,
            "cumulative_multiplier": str(line.cumulative_multiplier),
            "effective_quantity": str(line.effective_quantity),
            "valuation_quantity": str(line.valuation_quantity),
            "valuation_value": str(line.valuation_value),
            "valuation_lots": line.valuation_lot_count,
            "unit_cost": str(line.unit_cost),
            "raw_extension": str(line.raw_extension),
            "allocated_extension": str(line.allocated_extension),
        }
        for line in snapshot.lines.all()
    ]
    payload["servings"] = [
        {
            "code": serving.code,
            "name_ar": serving.name_ar,
            "name_en": serving.name_en,
            "is_primary": serving.is_primary,
            "factor_of_batch": str(serving.factor_of_batch),
            "cost_per_serving": str(serving.cost_per_serving),
            "whole_serving_count": serving.whole_serving_count,
            "remainder_quantity": str(serving.remainder_quantity),
            "allocation_state": serving.allocation_state,
            "allocated_total": str(serving.allocated_total),
            "normal_cost_per_serving": str(serving.minimum_allocated),
            "normal_serving_count": serving.normal_serving_count,
            "elevated_cost_per_serving": str(serving.maximum_allocated),
            "elevated_serving_count": serving.elevated_serving_count,
            "remainder_cost": str(serving.remainder_cost),
        }
        for serving in snapshot.servings.all()
    ]
    return payload


@router.post(
    "/recipe-versions/{version_id}/cost-snapshots",
    response={201: CostSnapshotDetailOut},
    summary="Freeze one authoritative cost card into an append-only record",
)
def post_cost_snapshot(
    request: HttpRequest, version_id: int, payload: CostSnapshotIn
) -> tuple[int, dict[str, Any]]:
    """
    A **command**, and the only write Task 3.3 performs.

    Idempotent on the key **and** a fingerprint of the request. A retry returns
    the original snapshot with no second set of lines; the same key with a
    different version, warehouse, date or purpose is `idempotency_key_conflict`,
    not a silent hand-back.

    Refused for a preview, a rejected version, or a card with any unvalued leaf.
    There is no partial mode: a costing record with a hole in it looks like a
    total.
    """
    actor = _actor(request)
    version = resolve_version(actor, version_id)
    _require_cost(request, version.recipe.organization)
    warehouse = resolve_manageable_warehouse(actor, payload.warehouse_id)
    card = cost_recipe_version(version=version, warehouse=warehouse, as_of_date=payload.as_of_date)
    snapshot = create_recipe_cost_snapshot(
        card=card,
        actor=actor,
        idempotency_key=payload.idempotency_key,
        reference=payload.reference,
        reason=payload.reason,
        note=payload.note,
    )
    return 201, _snapshot_detail_out(snapshot)


@router.get(
    "/recipe-cost-snapshots",
    response=list[CostSnapshotOut],
    summary="Cost snapshots this caller may read",
)
def list_cost_snapshots(
    request: HttpRequest,
    recipe_id: int | None = None,
    version_id: int | None = None,
    warehouse_id: int | None = None,
    as_of_date: datetime.date | None = None,
) -> list[dict[str, Any]]:
    """
    Scoped by `view_recipe_cost`, not by `view_recipe`.

    A storekeeper who legitimately reads every recipe card sees **no** snapshot
    here at all - not an empty-costed one. The filters narrow what is already
    in scope; none of them can widen it.
    """
    actor = _actor(request)
    rows = visible_cost_snapshots(actor)
    if recipe_id is not None:
        rows = rows.filter(recipe_id=recipe_id)
    if version_id is not None:
        rows = rows.filter(version_id=version_id)
    if warehouse_id is not None:
        rows = rows.filter(warehouse_id=warehouse_id)
    if as_of_date is not None:
        rows = rows.filter(as_of_date=as_of_date)
    return [_snapshot_out(snapshot) for snapshot in rows]


@router.get(
    "/recipe-cost-snapshots/{snapshot_id}",
    response=CostSnapshotDetailOut,
    summary="One cost snapshot, with its lines and serving scenarios",
)
def get_cost_snapshot(request: HttpRequest, snapshot_id: int) -> dict[str, Any]:
    """Out of scope is 404, never 403. There is no PATCH and no DELETE."""
    actor = _actor(request)
    snapshot = resolve_cost_snapshot(actor, snapshot_id)
    return _snapshot_detail_out(snapshot)


# ---------------------------------------------------------------------------
# Task 3.4 - production batch drafting
# ---------------------------------------------------------------------------
#
# **Commands, not CRUD.** A production batch is a document with a lifecycle, and
# the verbs below are the acts an operator performs on a draft: create it,
# correct its actual quantities, add an approved substitute, rescale it, ask
# whether it is ready, discard it.
#
# There is deliberately **no** post, reverse, issue, consume, complete, journal
# or inventory-post route, and no way to reach one. Task 3.5 owns posting, and a
# route to a service that does not exist would be a promise the system cannot
# keep - the same rule that kept the cost route out until Task 3.3 built it.
#
# **No money crosses these routes.** Reading a batch exposes no recipe cost, no
# unit cost, no standard cost and no snapshot value; a test reads the raw bytes
# to prove the keys are absent rather than null. Cost visibility remains
# `view_recipe_cost`'s alone, and holding `view_production` grants none of it.


def _require_production_view(request: HttpRequest, warehouse: Any) -> User:
    """
    Reading production is a **warehouse** question, not an organization one.

    A batch is custody of one store's stock. Somebody who reads the whole menu
    has no claim on what one branch cooked on Tuesday, so this asks
    `has_warehouse_permission` rather than reaching for the organization.
    """
    actor = _actor(request)
    require_warehouse_permission(actor, VIEW_PRODUCTION, warehouse)
    return actor


def _require_production_draft(request: HttpRequest, warehouse: Any) -> User:
    """Drafting and editing a draft, at this warehouse."""
    actor = _actor(request)
    require_warehouse_permission(actor, CREATE_PRODUCTION_BATCH, warehouse)
    return actor


class ProductionActualOut(Schema):
    id: int
    entry_order: int
    kind: str
    item_code: str
    item_name: str
    substitute_id: int | None
    entered_quantity: str
    entered_unit_code: str | None
    package_unit_code: str | None
    conversion_factor: str | None
    measured_base_quantity: str | None
    base_quantity: str
    reason: str
    note: str


class ProductionLineOut(Schema):
    id: int
    line_order: int
    component_path: str
    component_label_path: str
    source_kind: str
    source_recipe_code: str
    source_version_number: int
    item_code: str
    item_name: str
    base_unit_code: str
    source_base_quantity: str
    cumulative_multiplier: str
    planned_base_quantity: str
    cost_class: str
    is_optional: bool
    #: The actual quantity that may honestly be compared with the plan, or null.
    #: **Null is not zero.** It means the rows recorded against this requirement
    #: are in another dimension, and adding litres to kilograms would be
    #: inventing a figure the kitchen never measured. Every row is in `actuals`
    #: either way; Task 3.5 values each of them separately.
    comparable_actual_quantity: str | None
    variance: str | None
    is_quantitatively_comparable: bool
    comparison_statement: str
    actuals: list[ProductionActualOut]


class ProductionBatchOut(Schema):
    id: int
    public_id: str
    status: str
    number: str
    recipe_code: str
    recipe_name: str
    version_number: int
    branch_code: str
    warehouse_code: str
    planned_business_date: datetime.date
    multiplier: str
    expected_output_quantity: str
    expected_output_unit_code: str
    actual_output_entered_quantity: str | None
    actual_output_unit_code: str | None
    actual_output_base_quantity: str | None
    notes: str
    created_at: datetime.datetime


class ProductionBatchDetailOut(ProductionBatchOut):
    lines: list[ProductionLineOut]


class ReadinessProblemOut(Schema):
    code: str
    message: str
    line_order: int | None


class ReadinessOut(Schema):
    """
    What blocks, and what merely needs saying.

    Two lists rather than one flag with a severity, because they answer two
    different questions and a caller that conflated them would either refuse a
    correct batch or lose the explanation for a blank variance.
    """

    is_ready: bool
    problems: list[ReadinessProblemOut]
    observations: list[ReadinessProblemOut]


def _actual_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.pk,
        "entry_order": row.entry_order,
        "kind": row.kind,
        "item_code": row.item.code,
        "item_name": row.item.name_ar,
        "substitute_id": row.substitute_id,
        "entered_quantity": str(row.entered_quantity),
        "entered_unit_code": row.entered_unit.code if row.entered_unit_id else None,
        "package_unit_code": row.package_unit.code if row.package_unit_id else None,
        "conversion_factor": (
            str(row.conversion_factor) if row.conversion_factor is not None else None
        ),
        "measured_base_quantity": (
            str(row.measured_base_quantity) if row.measured_base_quantity is not None else None
        ),
        "base_quantity": str(row.base_quantity),
        "reason": row.reason,
        "note": row.note,
    }


def _production_line_out(line: Any) -> dict[str, Any]:
    comparison = ConsumptionComparison(
        line=line,
        comparable_quantity=comparable_consumption(line),
        recorded=has_recorded_consumption(line),
    )
    variance = comparison.variance
    return {
        "id": line.pk,
        "comparable_actual_quantity": (
            str(comparison.comparable_quantity)
            if comparison.comparable_quantity is not None
            else None
        ),
        "variance": str(variance) if variance is not None else None,
        "is_quantitatively_comparable": comparison.is_comparable,
        "comparison_statement": str(comparison.statement),
        "line_order": line.line_order,
        "component_path": line.component_path,
        "component_label_path": line.component_label_path,
        "source_kind": line.source_kind,
        "source_recipe_code": line.source_version.recipe.code,
        "source_version_number": line.source_version.version_number,
        "item_code": line.item_code,
        "item_name": line.item_name,
        "base_unit_code": line.base_unit_code,
        "source_base_quantity": str(line.source_base_quantity),
        "cumulative_multiplier": str(line.cumulative_multiplier),
        "planned_base_quantity": str(line.planned_base_quantity),
        "cost_class": line.cost_class,
        "is_optional": line.is_optional,
        "actuals": [_actual_out(row) for row in line.actuals.all()],
    }


def _batch_out(batch: Any) -> dict[str, Any]:
    """
    Serialize a batch. **Every decimal crosses as a quoted string**, and no key
    here names money.
    """
    return {
        "id": batch.pk,
        "public_id": str(batch.public_id),
        "status": batch.status,
        "number": batch.number,
        "recipe_code": batch.recipe.code,
        "recipe_name": batch.recipe.name_ar,
        "version_number": batch.recipe_version.version_number,
        "branch_code": batch.branch.code,
        "warehouse_code": batch.warehouse.code,
        "planned_business_date": batch.planned_business_date,
        "multiplier": str(batch.multiplier),
        "expected_output_quantity": str(batch.expected_output_quantity),
        "expected_output_unit_code": batch.expected_output_unit_code,
        "actual_output_entered_quantity": (
            str(batch.actual_output_entered_quantity)
            if batch.actual_output_entered_quantity is not None
            else None
        ),
        "actual_output_unit_code": (
            batch.actual_output_unit.code if batch.actual_output_unit_id else None
        ),
        "actual_output_base_quantity": (
            str(batch.actual_output_base_quantity)
            if batch.actual_output_base_quantity is not None
            else None
        ),
        "notes": batch.notes,
        "created_at": batch.created_at,
    }


def _batch_detail_out(batch: Any) -> dict[str, Any]:
    payload = _batch_out(batch)
    payload["lines"] = [_production_line_out(line) for line in production_lines_for(batch)]
    return payload


class ProductionBatchIn(Schema):
    recipe_id: int
    branch_id: int
    warehouse_id: int
    planned_business_date: datetime.date
    multiplier: str
    idempotency_key: str
    notes: str = ""


class ProductionBatchPatch(Schema):
    """
    Only the operator's own facts. The decision is frozen at creation.

    Absent from this payload, and deliberately: organization, branch, warehouse,
    recipe, recipe version, planned business date and the immutable source paths.
    A trigger refuses each of them, and offering a field here would be the API
    contradicting the database. The **multiplier** is absent too — it is editable,
    but only through `rescale`, because changing it also rewrites every planned
    quantity and a PATCH that moved one of the three would be refused at COMMIT.

    `notes` defaults to `None` rather than `""` so that "not sent" and "cleared"
    are different requests. Without that distinction a note could never be
    deleted through this route.
    """

    notes: str | None = None
    actual_output_quantity: str | None = None
    actual_output_unit_code: str | None = None


class ProductionRescaleIn(Schema):
    multiplier: str
    reset_actuals: bool = False
    reason: str = ""


class ProductionActualPatch(Schema):
    entered_quantity: str
    entered_unit_code: str | None = None
    package_unit_id: int | None = None
    measured_base_quantity: str | None = None
    note: str = ""


class ProductionSubstituteIn(Schema):
    item_id: int
    entered_quantity: str
    entered_unit_code: str | None = None
    package_unit_id: int | None = None
    measured_base_quantity: str | None = None
    reason: str = ""


@router.get(
    "/production-batches",
    response=list[ProductionBatchOut],
    summary="Production batches this caller may read",
)
def list_production_batches(
    request: HttpRequest,
    recipe_id: int | None = None,
    warehouse_id: int | None = None,
    planned_business_date: datetime.date | None = None,
) -> list[dict[str, Any]]:
    """
    Scoped by `view_production` at the **warehouse**, not by `view_recipe`.

    Somebody who reads every recipe card sees an empty list here unless they
    also hold production authority at a store. The filters narrow what is
    already in scope; none of them can widen it.
    """
    actor = _actor(request)
    rows = visible_production_batches(actor)
    if recipe_id is not None:
        rows = rows.filter(recipe_id=recipe_id)
    if warehouse_id is not None:
        rows = rows.filter(warehouse_id=warehouse_id)
    if planned_business_date is not None:
        rows = rows.filter(planned_business_date=planned_business_date)
    return [_batch_out(batch) for batch in rows]


@router.post(
    "/production-batches",
    response={201: ProductionBatchDetailOut},
    summary="Draft a batch from the version in force at a branch on a date",
)
def create_production_batch_endpoint(
    request: HttpRequest, payload: ProductionBatchIn
) -> tuple[int, dict[str, Any]]:
    """
    A **command**. Resolves the exact version once, expands it, and freezes it.

    `planned_business_date` is required and never defaulted: a batch drafted on
    Monday for Sunday's production must use Sunday's recipe.

    Idempotent on the key **and** a fingerprint of the request. The resolved
    version is deliberately absent from that fingerprint - it is a consequence
    of the request, not part of it, so an activation landing between two
    retries cannot turn an honest retry into a conflict.
    """
    actor = _actor(request)
    recipe = resolve_recipe(actor, payload.recipe_id)
    branch = resolve_branch(actor, payload.branch_id)
    warehouse = resolve_manageable_warehouse(actor, payload.warehouse_id)
    _require_production_draft(request, warehouse)
    batch = create_production_batch(
        recipe=recipe,
        branch=branch,
        warehouse=warehouse,
        planned_business_date=payload.planned_business_date,
        multiplier=Decimal(payload.multiplier),
        actor=actor,
        idempotency_key=payload.idempotency_key,
        notes=payload.notes,
    )
    return 201, _batch_detail_out(batch)


@router.get(
    "/production-batches/{batch_id}",
    response=ProductionBatchDetailOut,
    summary="One batch, with its requirements and actual consumption",
)
def get_production_batch(request: HttpRequest, batch_id: int) -> dict[str, Any]:
    """Out of scope is 404, never 403."""
    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_production_view(request, batch.warehouse)
    return _batch_detail_out(batch)


@router.patch(
    "/production-batches/{batch_id}",
    response=ProductionBatchDetailOut,
    summary="Record the actual output, or correct the notes",
)
def patch_production_batch(
    request: HttpRequest, batch_id: int, payload: ProductionBatchPatch
) -> dict[str, Any]:
    """
    Only reality. The recipe, version, warehouse, branch, date and multiplier
    are absent from the payload because they are frozen from creation - a
    trigger refuses them, and offering them here would be the API contradicting
    the database.
    """
    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_production_draft(request, batch.warehouse)

    if payload.actual_output_quantity is not None:
        unit = (
            unit_by_code(payload.actual_output_unit_code)
            if payload.actual_output_unit_code
            else batch.recipe_version.output_unit
        )
        record_production_output(
            batch=batch,
            entered_quantity=Decimal(payload.actual_output_quantity),
            entered_unit=unit,
            actor=actor,
        )
    if payload.notes is not None:
        # Through the service, not a `save()` here. The note is what an operator
        # writes to explain a variance somebody reads next month, and an edit
        # that left no audit trail would let the explanation change after the
        # fact with nothing to say it had. `None` means "not sent"; an empty
        # string is a deliberate clearing and is honoured.
        update_production_batch_notes(
            batch=ProductionBatch.objects.get(pk=batch.pk), notes=payload.notes, actor=actor
        )
    return _batch_detail_out(ProductionBatch.objects.get(pk=batch.pk))


@router.delete(
    "/production-batches/{batch_id}",
    response={204: None},
    summary="Discard a draft",
)
def delete_production_batch(
    request: HttpRequest, batch_id: int, reason: str = ""
) -> tuple[int, None]:
    """
    Cascades to its own requirement and actual rows and to nothing else.

    A reason is required once an operator has entered anything: discarding
    somebody's measurements without a word is the kind of thing a system should
    make you say out loud.
    """
    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_production_draft(request, batch.warehouse)
    discard_production_batch(batch=batch, actor=actor, reason=reason)
    return 204, None


@router.post(
    "/production-batches/{batch_id}/rescale",
    response=ProductionBatchDetailOut,
    summary="Change how many recipe batches this run is",
)
def post_production_rescale(
    request: HttpRequest, batch_id: int, payload: ProductionRescaleIn
) -> dict[str, Any]:
    """
    Refused outright once an operator has entered anything, unless the caller
    passes `reset_actuals` **and** a reason.

    Silently recomputing over somebody's measurements would be the system
    replacing a fact with an assumption, and nothing afterwards would say it
    happened.
    """
    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_production_draft(request, batch.warehouse)
    rescaled = rescale_production_batch(
        batch=batch,
        multiplier=Decimal(payload.multiplier),
        actor=actor,
        reset_actuals=payload.reset_actuals,
        reason=payload.reason,
    )
    return _batch_detail_out(rescaled)


@router.get(
    "/production-batches/{batch_id}/readiness",
    response=ReadinessOut,
    summary="Everything standing between this draft and a posting",
)
def get_production_readiness(request: HttpRequest, batch_id: int) -> dict[str, Any]:
    """
    Derived, never stored. There is no `READY` status: a stored flag would go
    stale the moment somebody edited a quantity.

    Checks **no stock** - availability, lots and expiry are Task 3.5's.
    """
    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_production_view(request, batch.warehouse)
    readiness = production_batch_readiness(batch)
    return {
        "is_ready": readiness.is_ready,
        "problems": [
            {"code": item.code, "message": item.message, "line_order": item.line_order}
            for item in readiness.problems
        ],
        # Non-blocking, and reported rather than dropped: a cross-dimension
        # substitution is legitimate, and a caller that saw only a blank variance
        # could not tell "not comparable" from "nobody looked".
        "observations": [
            {"code": item.code, "message": item.message, "line_order": item.line_order}
            for item in readiness.observations
        ],
    }


@router.patch(
    "/production-actual-lines/{actual_id}",
    response=ProductionActualOut,
    summary="Record what was actually consumed on one row",
)
def patch_production_actual(
    request: HttpRequest, actual_id: int, payload: ProductionActualPatch
) -> dict[str, Any]:
    """More than planned, less, or zero. A variance is a fact, never a refusal."""
    actor = _actor(request)
    actual = resolve_production_actual(actor, actual_id)
    _require_production_draft(request, actual.line.batch.warehouse)
    package = (
        resolve_package_unit(actor, payload.package_unit_id)
        if payload.package_unit_id is not None
        else None
    )
    updated = update_production_batch_actuals(
        actual=actual,
        entered_quantity=Decimal(payload.entered_quantity),
        entered_unit=(
            unit_by_code(payload.entered_unit_code) if payload.entered_unit_code else None
        ),
        package_unit=package,
        measured_base_quantity=(
            Decimal(payload.measured_base_quantity)
            if payload.measured_base_quantity is not None
            else None
        ),
        note=payload.note,
        actor=actor,
    )
    return _actual_out(updated)


@router.post(
    "/production-lines/{line_id}/substitutes",
    response={201: ProductionActualOut},
    summary="Record an approved stand-in beside the primary row",
)
def post_production_substitute(
    request: HttpRequest, line_id: int, payload: ProductionSubstituteIn
) -> tuple[int, dict[str, Any]]:
    """
    Beside, not instead of. A split is the case this exists for, and the caller
    decides what the primary row becomes - nothing here reduces it
    automatically, because "the rest was substituted" is an assumption.

    Refuses any item that is neither the requirement's own nor an active
    substitute **of that same source line**.
    """
    actor = _actor(request)
    line = resolve_production_line(actor, line_id)
    _require_production_draft(request, line.batch.warehouse)
    item = resolve_item(actor, payload.item_id)
    package = (
        resolve_package_unit(actor, payload.package_unit_id)
        if payload.package_unit_id is not None
        else None
    )
    row = add_production_batch_substitute(
        line=line,
        item=item,
        entered_quantity=Decimal(payload.entered_quantity),
        entered_unit=(
            unit_by_code(payload.entered_unit_code) if payload.entered_unit_code else None
        ),
        package_unit=package,
        measured_base_quantity=(
            Decimal(payload.measured_base_quantity)
            if payload.measured_base_quantity is not None
            else None
        ),
        reason=payload.reason,
        actor=actor,
    )
    return 201, _actual_out(row)


@router.delete(
    "/production-actual-lines/{actual_id}",
    response={204: None},
    summary="Withdraw one actual row",
)
def delete_production_actual(
    request: HttpRequest, actual_id: int, reason: str = ""
) -> tuple[int, None]:
    """
    Substitute or primary. What may not go is the **last** row.

    The primary row may be withdrawn when a substitution was complete - the
    kitchen used none of the planned item - because forcing a zero row to remain
    would force a statement about an item that never entered the pot. A
    requirement left with no actual row at all is refused: that is not "no
    consumption", it is "nobody said", and readiness would refuse it anyway.
    """
    actor = _actor(request)
    actual = resolve_production_actual(actor, actual_id)
    _require_production_draft(request, actual.line.batch.warehouse)
    remove_production_batch_substitute(actual=actual, actor=actor, reason=reason)
    return 204, None


class ProductionPreviewLineOut(Schema):
    component_path: str
    component_label_path: str
    source_kind: str
    item_code: str
    item_name: str
    cumulative_multiplier: str
    planned_base_quantity: str
    cost_class: str
    is_optional: bool


class ProductionPreviewOut(Schema):
    recipe_code: str
    version_number: int
    version_status: str
    multiplier: str
    expected_output_quantity: str
    output_unit_code: str
    lines: list[ProductionPreviewLineOut]


@router.get(
    "/recipes/{recipe_id}/production-preview",
    response=ProductionPreviewOut,
    summary="What a batch would contain, without creating one",
)
def get_production_preview(
    request: HttpRequest,
    recipe_id: int,
    branch_id: int,
    warehouse_id: int,
    planned_business_date: datetime.date,
    multiplier: str,
) -> dict[str, Any]:
    """
    Same resolver, same expansion, same arithmetic as the create command.

    Deliberately: a preview computed a second way is a preview that can
    disagree with the thing it previews. The warehouse is required so the
    permission is answered where it will be answered on create, rather than
    letting somebody preview into a store they may not draft into.
    """
    actor = _actor(request)
    recipe = resolve_recipe(actor, recipe_id)
    branch = resolve_branch(actor, branch_id)
    warehouse = resolve_manageable_warehouse(actor, warehouse_id)
    _require_production_view(request, warehouse)
    preview = preview_production_batch(
        recipe=recipe,
        branch=branch,
        planned_business_date=planned_business_date,
        multiplier=Decimal(multiplier),
    )
    return {
        "recipe_code": recipe.code,
        "version_number": preview.version.version_number,
        "version_status": str(preview.version.status),
        "multiplier": str(preview.multiplier),
        "expected_output_quantity": str(preview.expected_output_quantity),
        "output_unit_code": preview.output_unit_code,
        "lines": [
            {
                "component_path": leaf.path_display,
                "component_label_path": leaf.label_path,
                "source_kind": str(leaf.kind),
                "item_code": leaf.line.item.code,
                "item_name": leaf.line.item.name_ar,
                "cumulative_multiplier": leaf.multiplier_display,
                "planned_base_quantity": str(quantity),
                "cost_class": leaf.cost_class,
                "is_optional": leaf.is_optional,
            }
            for leaf, quantity in preview.planned
        ],
    }


# ---------------------------------------------------------------------------
# Task 3.5 — allocation, posting, reversal, and the valued evidence
#
# Two rules shape this block.
#
# **The service is authoritative.** Every endpoint below resolves a scoped
# object, checks a warehouse permission, validates a schema and calls a service.
# There is no generic PATCH on a posted batch, no writable movement endpoint, no
# way to name a `StockMovement` or a `JournalEntry`, and no path that reaches the
# ledger without going through `post_production_batch`.
#
# **Money is a separate endpoint, not a nullable field.** Cost visibility is
# `view_recipe_cost` and the repository's rule is omitted-not-blanked, so the
# posted values live on `/posting` behind that permission rather than as keys
# that turn into nulls. A reader without the permission does not receive the
# key at all, which is a different statement from receiving it empty.
# ---------------------------------------------------------------------------


class ProductionAllocationOut(Schema):
    """Where one consumption came from. Quantities only — no money here."""

    id: int
    allocation_order: int
    lot_code: str | None
    location_code: str | None
    base_quantity: str
    is_posted: bool


class ProductionAllocationIn(Schema):
    base_quantity: str
    lot_id: int | None = None
    location_id: int | None = None


class ProductionAllocationsIn(Schema):
    """
    The **whole** allocation set for one consumption row.

    Replace rather than append, because an allocation set is a single answer to
    a single question and appending would make a correction indistinguishable
    from a second consumption.
    """

    rows: list[ProductionAllocationIn]


class ProductionPostIn(Schema):
    idempotency_key: str
    reason: str = ""


class ProductionReverseIn(Schema):
    idempotency_key: str
    reason: str


class ProductionMovementOut(Schema):
    movement_type: str
    item_code: str
    lot_code: str | None
    base_quantity: str
    inventory_value: str
    unit_cost: str
    control_account_code: str | None


class ProductionPostingOut(Schema):
    """
    The valued evidence of one posting. Behind `view_recipe_cost`.

    `no_journal_reason` is a sentence rather than an empty field on purpose: a
    journal that is rightly absent and one that is wrongly missing look
    identical from the outside, and only one of them is acceptable.
    """

    status: str
    number: str
    posted_at: datetime.datetime | None
    business_date: datetime.date | None
    input_value: str | None
    output_value: str | None
    value_is_conserved: bool
    output_item_code: str | None
    output_quantity: str | None
    output_lot_code: str | None
    output_lot_expiry: datetime.date | None
    journal_entry_number: str | None
    no_journal_reason: str
    source_document_type: str
    source_document_id: str
    movements: list[ProductionMovementOut]
    reversal_movements: list[ProductionMovementOut]
    reversal_reason: str


def _allocation_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.pk,
        "allocation_order": row.allocation_order,
        "lot_code": row.lot.code if row.lot_id else None,
        "location_code": row.location.code if row.location_id else None,
        "base_quantity": str(row.base_quantity),
        "is_posted": row.movement_id is not None,
    }


def _allocation_rows(actor: User, actual: Any, rows: list[ProductionAllocationIn]) -> list[Any]:
    """
    Turn submitted ids into objects the caller already reaches.

    Resolved **with** the caller rather than fetched and checked afterwards, so
    a submitted lot or location id can only ever select from what is already
    visible and can never widen scope.
    """
    warehouse = actual.line.batch.warehouse
    resolved: list[Any] = []
    for row in rows:
        lot = None
        if row.lot_id is not None:
            lot = InventoryLot.objects.filter(
                pk=row.lot_id, item=actual.item, organization=actual.line.batch.organization
            ).first()
            if lot is None:
                raise OutOfScope(_("InventoryLot %(id)s does not exist.") % {"id": row.lot_id})
        location = None
        if row.location_id is not None:
            location = StockLocation.objects.filter(pk=row.location_id, warehouse=warehouse).first()
            if location is None:
                raise OutOfScope(
                    _("StockLocation %(id)s does not exist.") % {"id": row.location_id}
                )
        resolved.append(
            AllocationInput(base_quantity=Decimal(row.base_quantity), lot=lot, location=location)
        )
    return resolved


@router.get(
    "/production-actual-lines/{actual_id}/allocations",
    response=list[ProductionAllocationOut],
    summary="Which lots and bins one consumption row came from",
)
def list_production_allocations(request: HttpRequest, actual_id: int) -> list[dict[str, Any]]:
    actor = _actor(request)
    actual = resolve_production_actual(actor, actual_id)
    _require_production_view(request, actual.line.batch.warehouse)
    return [_allocation_out(row) for row in actual.allocations.all()]


@router.post(
    "/production-actual-lines/{actual_id}/allocations",
    response=list[ProductionAllocationOut],
    summary="Replace one consumption row's allocation set",
)
def replace_production_allocations(
    request: HttpRequest, actual_id: int, payload: ProductionAllocationsIn
) -> list[dict[str, Any]]:
    """
    The rows must sum to the consumption exactly, or the command is refused.

    Summing to less would post part of what the kitchen recorded using, which
    is a partial completion by another name and the one thing RCP-094 says a
    Release 1 batch never is.
    """
    actor = _actor(request)
    actual = resolve_production_actual(actor, actual_id)
    _require_production_draft(request, actual.line.batch.warehouse)
    written = set_production_allocations(
        actual=actual, rows=_allocation_rows(actor, actual, payload.rows)
    )
    return [_allocation_out(row) for row in written]


@router.patch(
    "/production-allocations/{allocation_id}",
    response=list[ProductionAllocationOut],
    summary="Correct one allocation row",
)
def patch_production_allocation(
    request: HttpRequest, allocation_id: int, payload: ProductionAllocationIn
) -> list[dict[str, Any]]:
    """
    Correcting one row still replaces the whole set.

    The service has one way in, so a correction reaching it by a different path
    would be a second way to write the same table — and the two would disagree
    the first time one of them changed.
    """
    actor = _actor(request)
    allocation = resolve_production_allocation(actor, allocation_id)
    actual = allocation.actual
    _require_production_draft(request, actual.line.batch.warehouse)

    wanted: list[Any] = []
    for row in actual.allocations.all():
        if row.pk == allocation.pk:
            wanted.extend(_allocation_rows(actor, actual, [payload]))
        else:
            wanted.append(
                AllocationInput(base_quantity=row.base_quantity, lot=row.lot, location=row.location)
            )
    written = set_production_allocations(actual=actual, rows=wanted)
    return [_allocation_out(row) for row in written]


@router.delete(
    "/production-allocations/{allocation_id}",
    response={204: None},
    summary="Remove one allocation row",
)
def delete_production_allocation(request: HttpRequest, allocation_id: int) -> tuple[int, None]:
    actor = _actor(request)
    allocation = resolve_production_allocation(actor, allocation_id)
    actual = allocation.actual
    _require_production_draft(request, actual.line.batch.warehouse)
    remaining = [
        AllocationInput(base_quantity=row.base_quantity, lot=row.lot, location=row.location)
        for row in actual.allocations.all()
        if row.pk != allocation.pk
    ]
    set_production_allocations(actual=actual, rows=remaining)
    return 204, None


@router.post(
    "/production-batches/{batch_id}/post",
    response=ProductionBatchDetailOut,
    summary="Commit the batch to both ledgers",
)
def post_production_batch_endpoint(
    request: HttpRequest, batch_id: int, payload: ProductionPostIn
) -> dict[str, Any]:
    """
    The only way a production batch reaches the stock ledger.

    Nothing is trusted from the caller except the batch's identity and the
    idempotency key: status, quantities, allocations and output are all re-read
    under lock by the service, so a stale client object cannot post a batch
    that has since changed.

    The response carries **no money**. What the posting was worth lives on
    `/posting`, behind `view_recipe_cost`.
    """
    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    require_warehouse_permission(actor, POST_PRODUCTION_BATCH, batch.warehouse)
    posted = post_production_batch(
        batch=batch,
        idempotency_key=payload.idempotency_key,
        actor=actor,
        reason=payload.reason,
    )
    return _batch_detail_out(posted)


@router.post(
    "/production-batches/{batch_id}/reverse",
    response=ProductionBatchDetailOut,
    summary="Mirror a posted batch exactly, once, with a reason",
)
def reverse_production_batch_endpoint(
    request: HttpRequest, batch_id: int, payload: ProductionReverseIn
) -> dict[str, Any]:
    """
    Elevated, and refused when the produced goods are no longer on the shelf to
    take back — a reversal must not become the standard way to drive a position
    negative.
    """
    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    require_warehouse_permission(actor, REVERSE_PRODUCTION_BATCH, batch.warehouse)
    reversed_batch = reverse_production_batch(
        batch=batch,
        idempotency_key=payload.idempotency_key,
        reason=payload.reason,
        actor=actor,
    )
    return _batch_detail_out(reversed_batch)


def _movement_out(movement: Any) -> dict[str, Any]:
    return {
        "movement_type": movement.movement_type,
        "item_code": movement.item.code,
        "lot_code": movement.lot.code if movement.lot_id else None,
        "base_quantity": str(movement.base_quantity),
        "inventory_value": str(movement.inventory_value),
        "unit_cost": str(movement.unit_cost),
        "control_account_code": (
            movement.control_account.code if movement.control_account_id else None
        ),
    }


@router.get(
    "/production-batches/{batch_id}/posting",
    response=ProductionPostingOut,
    summary="What one posting moved, and what it was worth",
)
def get_production_posting(request: HttpRequest, batch_id: int) -> dict[str, Any]:
    """
    The valued evidence, behind `view_recipe_cost` **and** `view_production`.

    A separate endpoint rather than nullable keys on the batch: cost visibility
    is omitted-not-blanked in this repository, and a key that becomes `null`
    tells the reader a number exists and that they are not trusted with it —
    which is a different statement from the one intended.
    """
    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_production_view(request, batch.warehouse)
    require_reachable_organization_permission(actor, VIEW_RECIPE_COST, batch.organization)

    # Narrowed into locals so the type checker sees the same guard a reader
    # does: every field below exists only on a posted batch.
    entry = batch.stock_entry
    reversal_entry = batch.reversal_stock_entry
    output_item = batch.output_item
    output_lot = batch.output_lot
    journal = batch.journal_entry

    movements = (
        list(entry.movements.select_related("item", "lot", "control_account").all())
        if entry is not None
        else []
    )
    reversal_movements = (
        list(reversal_entry.movements.select_related("item", "lot", "control_account").all())
        if reversal_entry is not None
        else []
    )
    return {
        "status": batch.status,
        "number": batch.number,
        "posted_at": batch.posted_at,
        "business_date": entry.business_date if entry is not None else None,
        "input_value": str(batch.input_value) if batch.input_value is not None else None,
        "output_value": str(batch.output_value) if batch.output_value is not None else None,
        "value_is_conserved": batch.input_value == batch.output_value,
        "output_item_code": output_item.code if output_item is not None else None,
        "output_quantity": (
            str(batch.actual_output_base_quantity)
            if batch.actual_output_base_quantity is not None
            else None
        ),
        "output_lot_code": output_lot.code if output_lot is not None else None,
        "output_lot_expiry": output_lot.expiry_date if output_lot is not None else None,
        "journal_entry_number": (journal.entry_number if journal is not None else None),
        "no_journal_reason": (
            str(_("لا يوجد قيد — صافي حسابات المخزون صفر."))
            if entry is not None and journal is None
            else ""
        ),
        "source_document_type": SOURCE_DOCUMENT_TYPE if entry is not None else "",
        "source_document_id": str(batch.public_id) if entry is not None else "",
        "movements": [_movement_out(row) for row in movements],
        "reversal_movements": [_movement_out(row) for row in reversal_movements],
        "reversal_reason": batch.reversal_reason,
    }


# ---------------------------------------------------------------------------
# Task 3.8 — consumption, the movement partition, and attribution
# ---------------------------------------------------------------------------
#
# Five reads and two commands. Read the list for what is absent as much as for
# what is present: no `PATCH` on a link, no `DELETE` on anything, no posting
# endpoint, no journal endpoint. A link is created or cancelled; nothing here
# moves stock.
#
# Every decimal crosses as an **exact string**, and every consumption response
# carries `coverage_code`. The theoretical and variance endpoints carry
# `SALES_NOT_INCLUDED_PHASE_4` on every response regardless of filters, because
# the missing input is a whole module rather than a date range.


def _require_kitchen_report(request: HttpRequest, organization: Any) -> User:
    """Reading a consumption report is an organization-scoped question."""
    actor = _actor(request)
    require_reachable_organization_permission(actor, VIEW_KITCHEN_REPORT, organization)
    return actor


def _require_link_authority(request: HttpRequest, warehouse: Any) -> User:
    """
    Attributing a document is a **warehouse** act, like production itself.

    A link is a statement about one kitchen store's own flow, made by the people
    who hold that store's stock.
    """
    actor = _actor(request)
    require_warehouse_permission(actor, LINK_BATCH_DOCUMENT, warehouse)
    return actor


class WarehouseFlowRowOut(Schema):
    warehouse: str
    item_code: str
    item_name: str
    base_unit_code: str
    opening: str
    closing: str
    identity_difference: str
    identity_holds: bool
    movement_count: int
    net_production_consumption: str
    direct_economic_consumption: str
    total_consumption: str
    custody_in: str
    custody_out: str
    supply_receipt: str
    production_output: str
    raw_material_waste: str
    produced_output_waste: str
    count_correction: str


class WarehouseFlowOut(Schema):
    identity_holds: bool
    classified_movement_count: int
    rows: list[WarehouseFlowRowOut]
    #: Present on every response. Custody is reported outside consumption and a
    #: consumer of this payload should not have to infer that from column names.
    custody_is_not_consumption: bool = True


def _flow_row_out(row: Any) -> dict[str, Any]:
    return {
        "warehouse": row.warehouse_code,
        "item_code": row.item_code,
        "item_name": row.item_name,
        "base_unit_code": row.base_unit_code,
        "opening": f"{row.opening:f}",
        "closing": f"{row.closing:f}",
        "identity_difference": f"{row.identity_difference:f}",
        "identity_holds": row.identity_holds,
        "movement_count": row.movement_count,
        "net_production_consumption": f"{row.net_production_consumption:f}",
        "direct_economic_consumption": f"{row.direct_economic_consumption:f}",
        "total_consumption": f"{row.total_consumption:f}",
        "custody_in": f"{row.custody_in:f}",
        "custody_out": f"{row.custody_out:f}",
        "supply_receipt": f"{row.supply_receipt:f}",
        "production_output": f"{row.production_output:f}",
        "raw_material_waste": f"{row.raw_material_waste:f}",
        "produced_output_waste": f"{row.produced_output_waste:f}",
        "count_correction": f"{row.count_correction:f}",
    }


def _flow_filters(
    warehouse_id: int | None,
    item_id: int | None,
    date_from: datetime.date | None,
    date_to: datetime.date | None,
) -> Any:
    from apps.kitchen.consumption import FlowFilters

    return FlowFilters(
        warehouse_id=warehouse_id, item_id=item_id, date_from=date_from, date_to=date_to
    )


@router.get(
    "/warehouse-flow/",
    response=WarehouseFlowOut,
    summary="Every posted movement at a kitchen store, in exactly one bucket",
)
def get_warehouse_flow(
    request: HttpRequest,
    warehouse_id: int | None = None,
    item_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> dict[str, Any]:
    """
    The partition, with its own proof attached.

    `identity_holds` is `(closing − opening) − Σ buckets == 0` per
    `(warehouse, item)`. A caller that ignores it is reading totals whose
    completeness it has chosen not to check.
    """
    from apps.kitchen.consumption import kitchen_warehouse_flow

    actor = _actor(request)
    flow = kitchen_warehouse_flow(actor, _flow_filters(warehouse_id, item_id, date_from, date_to))
    return {
        "identity_holds": flow.identity_holds,
        "classified_movement_count": flow.classified_count,
        "rows": [_flow_row_out(row) for row in flow.items],
    }


class BatchConsumptionAllocationOut(Schema):
    lot_code: str
    location_code: str
    base_quantity: str


class BatchConsumptionRowOut(Schema):
    actual_id: int
    kind: str
    is_substitute: bool
    substitute_reason: str
    component_path: str
    component_label_path: str
    source_recipe_line_id: int | None
    item_code: str
    item_name: str
    base_unit_code: str
    entered_quantity: str
    entered_unit_code: str
    base_quantity: str
    allocations: list[BatchConsumptionAllocationOut]


class BatchConsumptionOut(Schema):
    batch_number: str
    status: str
    is_reversed: bool
    quantity_matches: bool
    rows: list[BatchConsumptionRowOut]
    #: Stated rather than implied. Task 3.0 §11.2 defined batch consumption as
    #: `consumed − linked returns + linked waste`; ADR-026 supersedes that, and
    #: a consumer of this payload needs to know which of the two it is holding.
    links_change_this_arithmetic: bool = False


@router.get(
    "/production-batches/{batch_id}/actual-consumption",
    response=BatchConsumptionOut,
    summary="What one posted batch actually used",
)
def get_batch_actual_consumption(request: HttpRequest, batch_id: int) -> dict[str, Any]:
    """
    Quantities and evidence, with no money key at all.

    Values live on `/production-batches/{id}/posting` behind `view_recipe_cost`,
    exactly as Task 3.5 arranged. A nullable money key here would say a number
    exists and is being withheld, which is a different statement.
    """
    from apps.kitchen.consumption import batch_actual_consumption

    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_production_view(request, batch.warehouse)
    report = batch_actual_consumption(batch)
    return {
        "batch_number": batch.number,
        "status": batch.status,
        "is_reversed": report.is_reversed,
        "quantity_matches": report.quantity_matches,
        "rows": [
            {
                "actual_id": row.actual_id,
                "kind": row.kind,
                "is_substitute": row.is_substitute,
                "substitute_reason": row.substitute_reason,
                "component_path": row.source_line_path,
                "component_label_path": row.source_line_label,
                "source_recipe_line_id": row.source_recipe_line_id,
                "item_code": row.item_code,
                "item_name": row.item_name,
                "base_unit_code": row.base_unit_code,
                "entered_quantity": f"{row.entered_quantity:f}",
                "entered_unit_code": row.entered_unit_code,
                "base_quantity": f"{row.base_quantity:f}",
                "allocations": [
                    {
                        "lot_code": allocation.lot_code,
                        "location_code": allocation.location_code,
                        "base_quantity": f"{allocation.base_quantity:f}",
                    }
                    for allocation in row.allocations
                ],
            }
            for row in report.rows
        ],
    }


@router.get(
    "/consumption/actual/",
    response=WarehouseFlowOut,
    summary="Actual consumption for one kitchen store over a period",
)
def get_actual_consumption(
    request: HttpRequest,
    warehouse_id: int | None = None,
    item_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> dict[str, Any]:
    """
    The same partition, read as consumption.

    Deliberately the same payload shape as `/warehouse-flow/` rather than a
    reduced one: consumption is a *reading* of the partition, not a separate
    calculation, and two shapes would invite two implementations.
    """
    from apps.kitchen.consumption import period_actual_consumption

    actor = _actor(request)
    period = period_actual_consumption(
        actor, _flow_filters(warehouse_id, item_id, date_from, date_to)
    )
    return {
        "identity_holds": period.identity_holds,
        "classified_movement_count": period.flow.classified_count,
        "rows": [_flow_row_out(row) for row in period.items],
    }


class TheoreticalSourceOut(Schema):
    source_type: str
    status: str
    contribution_count: int
    total_quantity: str


class TheoreticalTotalOut(Schema):
    source_type: str
    equivalent_label: str
    item_code: str
    item_name: str
    base_unit_code: str
    effective_base_quantity: str
    contribution_count: int
    coverage_code: str


class TheoreticalConsumptionOut(Schema):
    #: `SALES_NOT_INCLUDED_PHASE_4`, on every response, without exception.
    coverage_code: str
    coverage_notice: str
    #: Constant `False` in Phase 3. Not derived from the data: a derived flag
    #: would eventually report `True` for a period with no sales in it.
    is_final: bool
    sources: list[TheoreticalSourceOut]
    totals: list[TheoreticalTotalOut]


def _meal_filters(
    branch_id: int | None,
    recipe_id: int | None,
    item_id: int | None,
    date_from: datetime.date | None,
    date_to: datetime.date | None,
) -> Any:
    from apps.kitchen.consumption_sources import MealUsageFilters

    return MealUsageFilters(
        branch_id=branch_id,
        recipe_id=recipe_id,
        item_id=item_id,
        date_from=date_from,
        date_to=date_to,
    )


@router.get(
    "/consumption/theoretical/",
    response=TheoreticalConsumptionOut,
    summary="Theoretical consumption, with the sales gap named",
)
def get_theoretical_consumption(
    request: HttpRequest,
    branch_id: int | None = None,
    recipe_id: int | None = None,
    item_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> dict[str, Any]:
    """
    Meal equivalents, per source, and **no combined total**.

    `sources` lists every declared source type, so `SALES` appears as
    `DEFERRED_TO_PHASE_4` rather than being silently absent. Adding the two
    available sources together is refused by omission: a `ProductionBatch` plan
    and a `MealRecord` expansion overlap physically, and no deduplication key
    linking a portion to the batch that produced it exists.
    """
    from apps.kitchen.consumption_sources import (
        SALES_COVERAGE_NOTICE,
        theoretical_consumption_coverage,
    )

    actor = _actor(request)
    coverage = theoretical_consumption_coverage(
        actor, _meal_filters(branch_id, recipe_id, item_id, date_from, date_to)
    )
    return {
        "coverage_code": coverage.coverage_code,
        "coverage_notice": str(SALES_COVERAGE_NOTICE),
        "is_final": coverage.is_final,
        "sources": [
            {
                "source_type": str(row.source_type),
                "status": str(row.status),
                "contribution_count": row.contribution_count,
                "total_quantity": f"{row.total_quantity:f}",
            }
            for row in coverage.sources
        ],
        "totals": [
            {
                "source_type": str(row.source_type),
                "equivalent_label": row.equivalent_label,
                "item_code": row.leaf_item_code,
                "item_name": row.leaf_item_name,
                "base_unit_code": row.base_unit_code,
                "effective_base_quantity": f"{row.effective_base_quantity:f}",
                "contribution_count": row.contribution_count,
                "coverage_code": row.coverage_code,
            }
            for row in coverage.totals
        ],
    }


class StandardVarianceRowOut(Schema):
    batch_number: str
    business_date: datetime.date
    recipe_code: str
    version: str
    component_path: str
    item_code: str
    item_name: str
    base_unit_code: str
    planned_base_quantity: str
    actual_base_quantity: str | None
    variance: str | None
    #: `NOT_QUANTITATIVELY_COMPARABLE` where the dimensions disagree, or
    #: `PARTIALLY_COMPARABLE_DIMENSIONS_EXCLUDED` where the variance is a true
    #: number over only part of the evidence. Never a zero: zero means "no
    #: deviation" and neither of these does.
    compatibility: str
    statement: str
    #: Recorded actual rows in another dimension, which `variance` excludes.
    excluded_rows: list[str]
    #: `False` when `excluded_rows` is non-empty: the variance is real but does
    #: not account for everything recorded against the requirement.
    variance_is_complete: bool


class UsageDiagnosticRowOut(Schema):
    item_code: str
    item_name: str
    base_unit_code: str
    net_production_consumption: str
    direct_economic_consumption: str
    total_consumption: str
    production_standard_requirement: str
    unexplained_by_production_plan: str
    staff_meal_equivalent: str
    complimentary_meal_equivalent: str
    custody_in: str
    custody_out: str
    raw_material_waste: str
    produced_output_waste: str
    count_correction: str
    coverage_code: str
    coverage_label: str
    finality_label: str


class UsageVarianceOut(Schema):
    #: Complete. Both sides are posted facts about the same batch.
    production_standard_variance: list[StandardVarianceRowOut]
    #: Partial, and every row says so.
    partial_diagnostic: list[UsageDiagnosticRowOut]
    coverage_code: str
    coverage_label: str
    finality_label: str
    #: Constant `False`. There is no filter that makes it true in Phase 3.
    final_sales_variance_available: bool
    missing_sources: list[str]
    identity_holds: bool
    notices: list[str]


@router.get(
    "/consumption/variance/",
    response=UsageVarianceOut,
    summary="Production standard variance (complete) and a labelled partial diagnostic",
)
def get_usage_variance(
    request: HttpRequest,
    warehouse_id: int | None = None,
    branch_id: int | None = None,
    recipe_id: int | None = None,
    batch_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
) -> dict[str, Any]:
    """
    Two outputs, never blended into one number.

    There is no field here holding "the" usage variance, and that absence is the
    contract: approved sold quantities do not exist before Phase 4, so the final
    figure cannot be computed and is not approximated.
    """
    from apps.kitchen.consumption_reconciliation import usage_variance_analysis
    from apps.kitchen.productivity import ProductionFilters

    actor = _actor(request)
    analysis = usage_variance_analysis(
        actor,
        flow=_flow_filters(warehouse_id, None, date_from, date_to),
        production=ProductionFilters(
            warehouse_id=warehouse_id,
            branch_id=branch_id,
            recipe_id=recipe_id,
            batch_id=batch_id,
            date_from=date_from,
            date_to=date_to,
        ),
        meals=_meal_filters(branch_id, recipe_id, None, date_from, date_to),
    )
    return {
        "production_standard_variance": [
            {
                "batch_number": row.batch.number,
                "business_date": row.batch.planned_business_date,
                "recipe_code": row.batch.recipe.code,
                "version": row.version_label,
                "component_path": row.component_path,
                "item_code": row.item_code,
                "item_name": row.item_name,
                "base_unit_code": row.base_unit_code,
                "planned_base_quantity": f"{row.planned_base_quantity:f}",
                "actual_base_quantity": (
                    f"{row.actual_base_quantity:f}"
                    if row.actual_base_quantity is not None
                    else None
                ),
                "variance": f"{row.variance:f}" if row.variance is not None else None,
                "compatibility": row.compatibility,
                "statement": row.statement,
                "excluded_rows": list(row.excluded_rows),
                "variance_is_complete": row.is_complete,
            }
            for row in analysis.production_variance
        ],
        "partial_diagnostic": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "base_unit_code": row.base_unit_code,
                "net_production_consumption": f"{row.net_production_consumption:f}",
                "direct_economic_consumption": f"{row.direct_economic_consumption:f}",
                "total_consumption": f"{row.total_consumption:f}",
                "production_standard_requirement": f"{row.production_standard_requirement:f}",
                "unexplained_by_production_plan": f"{row.unexplained_by_production_plan:f}",
                "staff_meal_equivalent": f"{row.staff_meal_equivalent:f}",
                "complimentary_meal_equivalent": f"{row.complimentary_meal_equivalent:f}",
                "custody_in": f"{row.custody_in:f}",
                "custody_out": f"{row.custody_out:f}",
                "raw_material_waste": f"{row.raw_material_waste:f}",
                "produced_output_waste": f"{row.produced_output_waste:f}",
                "count_correction": f"{row.count_correction:f}",
                "coverage_code": row.coverage_code,
                "coverage_label": row.coverage_label,
                "finality_label": row.finality_label,
            }
            for row in analysis.diagnostic
        ],
        "coverage_code": analysis.coverage_code,
        "coverage_label": analysis.coverage_label,
        "finality_label": analysis.finality_label,
        "final_sales_variance_available": analysis.final_sales_variance_available,
        "missing_sources": list(analysis.missing_sources),
        "identity_holds": analysis.identity_holds,
        "notices": [str(notice) for notice in analysis.notices],
    }


class BatchDocumentLinkOut(Schema):
    id: int
    public_id: uuid.UUID
    link_type: str
    status: str
    item_code: str
    base_unit_code: str
    attributed_quantity: str
    source_reference: str
    reason: str
    note: str
    cancellation_reason: str
    #: Constant, on every link payload. The one thing a reader must not have to
    #: guess about a row in this table.
    stock_effect: str = "none"
    journal_effect: str = "none"
    changes_batch_consumption: bool = False


def _link_out(link: Any) -> dict[str, Any]:
    return {
        "id": link.pk,
        "public_id": link.public_id,
        "link_type": link.link_type,
        "status": link.status,
        "item_code": link.item.code,
        "base_unit_code": link.item.base_unit.code,
        "attributed_quantity": link.quantity_display,
        "source_reference": link.source_reference,
        "reason": link.reason,
        "note": link.note,
        "cancellation_reason": link.cancellation_reason,
    }


class BatchDocumentLinkIn(Schema):
    link_type: str
    #: Exactly one of the two, and it must agree with `link_type`. Typed ids
    #: into the real Inventory line models rather than a `document_type` string
    #: plus a UUID: a caller-supplied table name cannot be checked by the
    #: database, and a link pointing at a deleted or foreign row would look
    #: exactly like a valid one.
    transfer_line_id: int | None = None
    waste_line_id: int | None = None
    production_line_id: int | None = None
    production_actual_line_id: int | None = None
    attributed_quantity: str
    reason: str
    note: str = ""


class BatchDocumentLinkCancelIn(Schema):
    reason: str


@router.get(
    "/production-batches/{batch_id}/document-links",
    response=list[BatchDocumentLinkOut],
    summary="Explanatory attributions on one batch",
)
def list_batch_document_links(request: HttpRequest, batch_id: int) -> list[dict[str, Any]]:
    from apps.kitchen.document_links import links_for_batch

    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_production_view(request, batch.warehouse)
    return [_link_out(link) for link in links_for_batch(batch)]


@router.post(
    "/production-batches/{batch_id}/document-links",
    response={201: BatchDocumentLinkOut},
    summary="Attribute one posted inventory line to one posted batch",
)
def create_batch_document_link_endpoint(
    request: HttpRequest, batch_id: int, payload: BatchDocumentLinkIn
) -> tuple[int, dict[str, Any]]:
    """
    Creates one explanatory row and touches no ledger.

    The quantity arrives as a **string** and is parsed with `Decimal`, because
    JSON's only numeric type is binary floating point and an attribution of
    0.30000000000000004 would be nobody's fault and everybody's problem.
    """
    from apps.inventory.models import InventoryMovementDocumentLine, StockTransferLine
    from apps.kitchen.document_links import create_batch_document_link
    from apps.kitchen.models import ProductionBatchActualLine, ProductionBatchLine

    actor = _actor(request)
    batch = resolve_production_batch(actor, batch_id)
    _require_link_authority(request, batch.warehouse)

    transfer_line = (
        StockTransferLine.objects.filter(pk=payload.transfer_line_id).first()
        if payload.transfer_line_id
        else None
    )
    waste_line = (
        InventoryMovementDocumentLine.objects.filter(pk=payload.waste_line_id).first()
        if payload.waste_line_id
        else None
    )
    line = (
        ProductionBatchLine.objects.filter(pk=payload.production_line_id, batch=batch).first()
        if payload.production_line_id
        else None
    )
    actual_line = (
        ProductionBatchActualLine.objects.filter(
            pk=payload.production_actual_line_id, line__batch=batch
        ).first()
        if payload.production_actual_line_id
        else None
    )

    link = create_batch_document_link(
        batch=batch,
        link_type=payload.link_type,
        transfer_line=transfer_line,
        waste_line=waste_line,
        line=line,
        actual_line=actual_line,
        attributed_quantity=Decimal(payload.attributed_quantity),
        reason=payload.reason,
        note=payload.note,
        actor=actor,
    )
    return 201, _link_out(link)


@router.post(
    "/document-links/{link_id}/cancel",
    response=BatchDocumentLinkOut,
    summary="Withdraw an attribution, with a reason that stays on the row",
)
def cancel_batch_document_link_endpoint(
    request: HttpRequest, link_id: int, payload: BatchDocumentLinkCancelIn
) -> dict[str, Any]:
    """
    Cancellation, never deletion, and no `PATCH` anywhere near it.

    An `ACTIVE` link is immutable at the database. Correcting one is cancelling
    it and making another, so a verb implying an edit would be the API
    contradicting a trigger.
    """
    from apps.kitchen.document_links import cancel_batch_document_link
    from apps.kitchen.selectors import resolve_batch_document_link

    actor = _actor(request)
    link = resolve_batch_document_link(actor, link_id)
    _require_link_authority(request, link.batch.warehouse)
    return _link_out(cancel_batch_document_link(link=link, reason=payload.reason, actor=actor))
