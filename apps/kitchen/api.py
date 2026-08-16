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

**There is no submit, approve, activate, supersede or cost endpoint.** Task 3.2
owns the lifecycle and Task 3.3 owns costing; publishing a route to a service
that does not exist would be a promise the system cannot keep.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from apps.inventory.selectors import resolve_item, resolve_package_unit
from apps.kitchen.models import MeasurementBasis, RecipeLineCostClass, ServingRoundingPolicy
from apps.kitchen.permissions import MANAGE_RECIPE, VIEW_RECIPE
from apps.kitchen.selectors import (
    resolve_category,
    resolve_line,
    resolve_recipe,
    resolve_serving,
    resolve_step,
    resolve_substitute,
    resolve_version,
    visible_recipes,
    visible_step_ingredients,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_line_substitute,
    add_recipe_serving,
    add_recipe_step,
    archive_recipe,
    create_draft_recipe_version,
    create_recipe,
    delete_draft_recipe_version,
    link_step_ingredient,
    reactivate_recipe,
    remove_recipe_line,
    remove_recipe_line_substitute,
    remove_recipe_serving,
    remove_recipe_step,
    unlink_step_ingredient,
    update_draft_recipe_version,
    update_recipe,
    update_recipe_line,
    update_recipe_serving,
    update_recipe_step,
)
from apps.organizations.authorization import (
    PermissionMissing,
    require_reachable_organization_permission,
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
