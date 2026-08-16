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

**There is still no cost endpoint and no component endpoint.** Task 3.3 owns
costing and Task 3.2B owns nested recipes; publishing a route to a service that
does not exist would be a promise the system cannot keep.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from apps.inventory.selectors import resolve_item, resolve_package_unit
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
from apps.kitchen.models import MeasurementBasis, RecipeLineCostClass, ServingRoundingPolicy
from apps.kitchen.permissions import (
    ACTIVATE_RECIPE_VERSION,
    APPROVE_RECIPE_VERSION,
    MANAGE_RECIPE,
    REJECT_RECIPE_VERSION,
    REVIEW_RECIPE_VERSION,
    SUBMIT_RECIPE_VERSION,
    VIEW_RECIPE,
)
from apps.kitchen.selectors import (
    components_for_version,
    resolve_category,
    resolve_component,
    resolve_line,
    resolve_recipe,
    resolve_serving,
    resolve_step,
    resolve_substitute,
    resolve_version,
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
from apps.organizations.authorization import (
    PermissionMissing,
    require_reachable_organization_permission,
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
# No cost route, no flatten route, no production route. Task 3.3 owns roll-up
# and Task 3.4 owns flattening.


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
