"""
Kitchen master-data commands.

Every write goes through here. Models stay free of business logic, and no
caller can construct a half-valid row by assigning fields directly.

Three disciplines the accounting kernel taught this project, applied to master
data that never posts:

* **Re-read before you write.** `previous_state` comes from the database, not
  from a form-mutated instance — a bound `ModelForm` mutates its instance
  during validation, so a "before" snapshot taken from it already holds the new
  values.
* **Lock what races.** Anything that has to be unique among siblings — the next
  version number, the next line order, the single primary serving, the sum of a
  line's step shares — takes `select_for_update()` on the parent first, so two
  concurrent requests serialise instead of both winning.
* **Validate the persisted state, not the argument.** Every mutation re-reads
  its version and refuses unless that row is still `DRAFT`. A stale instance a
  caller has held since before a discard cannot be used to modify anything.

**Task 3.1 has no submit, approve, activate or supersede service, and this is
deliberate rather than unfinished.** Task 3.2A owns the lifecycle, in
`lifecycle.py`. A placeholder that flipped a status would be a lifecycle without
its rules — the maker-checker constraint, the effective-date exclusion and the
immutability triggers all arrive together or none of them do.

**Task 3.2B adds the four component commands** at the end of this module. They
are draft structure like every other command here, and they follow the same
three disciplines — with one addition of their own: they take the organization's
**component graph lock** before any row lock, because a cycle is a contradiction
that lives across rows and no row lock can see it. `graph.py` explains why.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.quantity import ensure_decimal, quantize_calculation, quantize_factor
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import ConversionType, InventoryItem, ItemType, PackageUnit
from apps.inventory.selectors import effective_conversion
from apps.kitchen.graph import lock_component_graph, read_graph, validate_component_edge
from apps.kitchen.models import (
    MeasurementBasis,
    Recipe,
    RecipeBranch,
    RecipeCategory,
    RecipeComponent,
    RecipeLine,
    RecipeLineCostClass,
    RecipeLineSubstitute,
    RecipeServing,
    RecipeStep,
    RecipeStepIngredient,
    RecipeType,
    RecipeVersion,
    RecipeVersionStatus,
    ServingRoundingPolicy,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.units.services import convert
from apps.users.models import User

#: Item types a batch recipe may produce. A batch recipe that made a
#: `RAW_MATERIAL` would be claiming the kitchen manufactures flour (RCP-008).
PRODUCIBLE_ITEM_TYPES = frozenset({ItemType.SEMI_FINISHED, ItemType.FINISHED_GOOD})


# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------


def _require_code(code: str) -> str:
    """Canonicalise a code, and refuse an empty one."""
    canonical = code.strip().upper()
    if not canonical:
        raise ValidationError({"code": _("الرمز مطلوب.")})
    return canonical


def _require_positive(value: object, field: str) -> Decimal:
    quantity = quantize_calculation(ensure_decimal(value, field=field), field=field)
    if quantity <= 0:
        raise ValidationError({field: _("الكمية يجب أن تكون أكبر من صفر.")})
    return quantity


def _lock_draft(version: RecipeVersion) -> RecipeVersion:
    """
    Re-read this version under a row lock and refuse unless it is still a draft.

    The argument may be a stale instance a caller has held since before the
    draft was discarded, or since Task 3.2 approved it. The database row is the
    authority; the Python object is a memory of one.
    """
    current = (
        RecipeVersion.objects.select_for_update()
        .filter(pk=version.pk)
        .select_related("recipe")
        .first()
    )
    if current is None:
        raise ValidationError(_("النسخة لم تعد موجودة."))
    if current.status != RecipeVersionStatus.DRAFT:
        raise ValidationError(_("لا يمكن تعديل نسخة غير مسودة."))
    return current


def _require_same_organization(organization: Organization, *, item: InventoryItem) -> None:
    if item.organization_id != organization.pk:
        raise ValidationError({"item": _("الصنف يتبع مؤسسة أخرى.")})


def _validate_provenance(source_document: str, source_page: int | None) -> tuple[str, int | None]:
    """
    Both halves of a provenance, or neither (RCP-119).

    A half-filled provenance looks like an answer to "who says so" and is not
    one, so the service refuses it before the database does — the error a user
    reads should name the field, not the constraint.
    """
    document = source_document.strip()
    if bool(document) != (source_page is not None):
        raise ValidationError(
            {"source_page": _("المصدر يحتاج اسم المستند ورقم الصفحة معاً، أو لا شيء منهما.")}
        )
    if source_page is not None and source_page <= 0:
        raise ValidationError({"source_page": _("رقم الصفحة يجب أن يكون موجباً.")})
    return document, source_page


# ---------------------------------------------------------------------------
# Recipe categories
# ---------------------------------------------------------------------------


@transaction.atomic
def create_recipe_category(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    name_en: str = "",
    notes: str = "",
    source_document: str = "",
    source_page: int | None = None,
    source_sha256: str = "",
    source_reference: str = "",
    source_note: str = "",
) -> RecipeCategory:
    """Add a way of grouping this organization's dishes."""
    document, page = _validate_provenance(source_document, source_page)
    category = RecipeCategory(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        notes=notes.strip(),
        source_document=document,
        source_page=page,
        source_sha256=source_sha256.strip(),
        source_reference=source_reference.strip(),
        source_note=source_note.strip(),
    )
    category.full_clean()
    category.save()
    record_audit_event(action=AuditAction.CREATED, target=category, new_state=snapshot(category))
    return category


@transaction.atomic
def update_recipe_category(
    *,
    category: RecipeCategory,
    name_ar: str,
    name_en: str = "",
    notes: str = "",
    is_active: bool = True,
) -> RecipeCategory:
    """
    Correct a category, or archive and reactivate one.

    The code and the organization are absent from the signature on purpose: a
    code is what reports group by, and re-homing a category would move recipes
    across a tenancy boundary.
    """
    current = RecipeCategory.objects.select_for_update().get(pk=category.pk)
    previous = snapshot(current)

    current.name_ar = name_ar.strip()
    current.name_en = name_en.strip()
    current.notes = notes.strip()
    current.is_active = is_active
    current.full_clean()
    current.save()

    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
    )
    return current


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


def _validate_output_item(
    *,
    organization: Organization,
    recipe_type: str,
    output_item: InventoryItem | None,
) -> None:
    """
    A batch recipe produces a stocked item; a portion recipe produces a plate.

    The plate is deliberately **not** an `InventoryItem` — the boundary Phase 1
    documented and tested — so a portion recipe naming one is a category error,
    not a convenience.
    """
    if recipe_type == RecipeType.BATCH:
        if output_item is None:
            raise ValidationError({"output_item": _("وصفة الدفعة تحتاج صنف ناتج.")})
        if output_item.organization_id != organization.pk:
            raise ValidationError({"output_item": _("الصنف الناتج يتبع مؤسسة أخرى.")})
        if output_item.item_type not in PRODUCIBLE_ITEM_TYPES:
            raise ValidationError(
                {"output_item": _("الصنف الناتج يجب أن يكون نصف مصنّع أو منتج تام مخزون.")}
            )
    elif output_item is not None:
        raise ValidationError({"output_item": _("وصفة الحصة لا تنتج صنفاً مخزنياً.")})


@transaction.atomic
def create_recipe(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    recipe_type: str,
    name_en: str = "",
    description_ar: str = "",
    description_en: str = "",
    category: RecipeCategory | None = None,
    output_item: InventoryItem | None = None,
    notes: str = "",
    created_by: User | None = None,
    source_document: str = "",
    source_page: int | None = None,
    source_sha256: str = "",
    source_reference: str = "",
    source_note: str = "",
) -> Recipe:
    """
    Add a dish to the organization's recipe master.

    Creates **no version**. A recipe with no draft is a recipe somebody has
    named and not yet described, which is a real and common state — and
    creating an empty draft automatically would produce a version whose author
    never opened it.
    """
    if category is not None and category.organization_id != organization.pk:
        raise ValidationError({"category": _("المجموعة تتبع مؤسسة أخرى.")})
    _validate_output_item(
        organization=organization, recipe_type=recipe_type, output_item=output_item
    )
    document, page = _validate_provenance(source_document, source_page)

    recipe = Recipe(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        description_ar=description_ar.strip(),
        description_en=description_en.strip(),
        recipe_type=recipe_type,
        category=category,
        output_item=output_item,
        notes=notes.strip(),
        created_by=created_by,
        source_document=document,
        source_page=page,
        source_sha256=source_sha256.strip(),
        source_reference=source_reference.strip(),
        source_note=source_note.strip(),
    )
    recipe.full_clean()
    recipe.save()
    record_audit_event(action=AuditAction.CREATED, target=recipe, new_state=snapshot(recipe))
    return recipe


@transaction.atomic
def update_recipe(
    *,
    recipe: Recipe,
    name_ar: str,
    name_en: str = "",
    description_ar: str = "",
    description_en: str = "",
    category: RecipeCategory | None = None,
    output_item: InventoryItem | None = None,
    notes: str = "",
) -> Recipe:
    """
    Correct a recipe's details.

    `code`, `organization` and `recipe_type` are absent from the signature.
    The first two for the reasons the supplier master gives — identity and
    tenancy. The third because a recipe that has a version describing how to
    produce a stocked item cannot quietly become a plate assembled to order;
    the lines and the output item would still be there, meaning something else.
    """
    current = Recipe.objects.select_for_update().get(pk=recipe.pk)
    previous = snapshot(current)

    if category is not None and category.organization_id != current.organization_id:
        raise ValidationError({"category": _("المجموعة تتبع مؤسسة أخرى.")})
    _validate_output_item(
        organization=current.organization,
        recipe_type=current.recipe_type,
        output_item=output_item,
    )

    current.name_ar = name_ar.strip()
    current.name_en = name_en.strip()
    current.description_ar = description_ar.strip()
    current.description_en = description_en.strip()
    current.category = category
    current.output_item = output_item
    current.notes = notes.strip()
    current.full_clean()
    current.save()

    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
    )
    return current


@transaction.atomic
def archive_recipe(*, recipe: Recipe, reason: str = "") -> Recipe:
    """
    Retire a recipe without deleting it.

    Never a delete: the code stays reserved forever, because reusing
    `MANDI-01` for a different dish would silently rewrite every report that
    groups by recipe — and, once Task 3.5 lands, every posted batch that names
    this one.
    """
    current = Recipe.objects.select_for_update().get(pk=recipe.pk)
    previous = snapshot(current)
    current.is_active = False
    current.full_clean()
    current.save(update_fields=["is_active", "updated_at"])
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
        reason=reason,
    )
    return current


@transaction.atomic
def reactivate_recipe(*, recipe: Recipe, reason: str = "") -> Recipe:
    """Bring an archived recipe back into use."""
    current = Recipe.objects.select_for_update().get(pk=recipe.pk)
    previous = snapshot(current)
    current.is_active = True
    current.full_clean()
    current.save(update_fields=["is_active", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
        reason=reason,
    )
    return current


@transaction.atomic
def set_recipe_branches(*, recipe: Recipe, branches: list[Branch]) -> list[RecipeBranch]:
    """
    Say which branches this recipe applies at.

    An empty list means **organization-wide**, which is the common case and the
    reason it is the default rather than an error. Branch applicability
    restricts where a recipe may be used; it grants nobody authority over the
    recipe itself, which stays organization master data (RCP-017).
    """
    current = Recipe.objects.select_for_update().get(pk=recipe.pk)
    for branch in branches:
        if branch.organization_id != current.organization_id:
            raise ValidationError({"branches": _("الفرع يتبع مؤسسة أخرى.")})

    previous = {
        "branches": sorted(current.branch_applicability.values_list("branch__code", flat=True))
    }
    current.branch_applicability.all().delete()
    rows = [RecipeBranch.objects.create(recipe=current, branch=branch) for branch in branches]

    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state={"branches": sorted(branch.code for branch in branches)},
    )
    return rows


# ---------------------------------------------------------------------------
# Draft versions
# ---------------------------------------------------------------------------


@transaction.atomic
def create_draft_recipe_version(
    *,
    recipe: Recipe,
    expected_output_quantity: object,
    output_unit: UnitOfMeasure,
    batch_size: object = Decimal("1"),
    preparation_loss: object | None = None,
    cooking_yield: object | None = None,
    instructions: str = "",
    notes: str = "",
    created_by: User | None = None,
    source_document: str = "",
    source_page: int | None = None,
    source_sha256: str = "",
    source_reference: str = "",
    source_note: str = "",
) -> RecipeVersion:
    """
    Open a draft of this recipe's composition.

    The version number comes from the recipe's own monotonic allocator,
    drawn under a lock so two concurrent requests cannot both take the same
    one. Numbers are never reused after a discard — the allocator remembers
    the highest ever issued, not the highest still present — because a version
    number is a name people quote in conversation, and `v2` meaning two
    different things is worse than a gap.

    Exactly one draft may be open per recipe in Task 3.1 — held by a partial
    unique index, not by this check alone. Task 3.2 relaxes it, when an
    approved version and a new draft can legitimately coexist.
    """
    locked = Recipe.objects.select_for_update().get(pk=recipe.pk)
    document, page = _validate_provenance(source_document, source_page)

    locked.last_version_number += 1
    locked.save(update_fields=["last_version_number", "updated_at"])

    version = RecipeVersion(
        recipe=locked,
        version_number=locked.last_version_number,
        status=RecipeVersionStatus.DRAFT,
        batch_size=_require_positive(batch_size, "batch_size"),
        expected_output_quantity=_require_positive(
            expected_output_quantity, "expected_output_quantity"
        ),
        output_unit=output_unit,
        preparation_loss=(
            quantize_calculation(ensure_decimal(preparation_loss, field="preparation_loss"))
            if preparation_loss is not None
            else None
        ),
        cooking_yield=(
            quantize_calculation(ensure_decimal(cooking_yield, field="cooking_yield"))
            if cooking_yield is not None
            else None
        ),
        instructions=instructions.strip(),
        notes=notes.strip(),
        created_by=created_by,
        source_document=document,
        source_page=page,
        source_sha256=source_sha256.strip(),
        source_reference=source_reference.strip(),
        source_note=source_note.strip(),
    )
    version.full_clean()
    version.save()
    record_audit_event(action=AuditAction.CREATED, target=version, new_state=snapshot(version))
    return version


@transaction.atomic
def update_draft_recipe_version(
    *,
    version: RecipeVersion,
    expected_output_quantity: object,
    output_unit: UnitOfMeasure,
    batch_size: object = Decimal("1"),
    preparation_loss: object | None = None,
    cooking_yield: object | None = None,
    instructions: str = "",
    notes: str = "",
) -> RecipeVersion:
    """
    Correct an open draft.

    Changing `output_unit` re-derives every serving's `base_quantity` and
    factor, because a serving is defined *relative to the output basis* and
    silently leaving stale factors behind would misprice every portion. A
    serving whose unit cannot convert to the new basis makes the change
    impossible, and the service says so rather than dropping the serving.
    """
    current = _lock_draft(version)
    previous = snapshot(current)

    current.batch_size = _require_positive(batch_size, "batch_size")
    current.expected_output_quantity = _require_positive(
        expected_output_quantity, "expected_output_quantity"
    )
    current.output_unit = output_unit
    current.preparation_loss = (
        quantize_calculation(ensure_decimal(preparation_loss, field="preparation_loss"))
        if preparation_loss is not None
        else None
    )
    current.cooking_yield = (
        quantize_calculation(ensure_decimal(cooking_yield, field="cooking_yield"))
        if cooking_yield is not None
        else None
    )
    current.instructions = instructions.strip()
    current.notes = notes.strip()
    current.full_clean()
    current.save()

    for serving in current.servings.select_related("serving_unit"):
        base, factor = _serving_basis(
            version=current, quantity=serving.serving_quantity, unit=serving.serving_unit
        )
        serving.base_quantity = base
        serving.factor_of_batch = factor
        serving.full_clean()
        serving.save(update_fields=["base_quantity", "factor_of_batch", "updated_at"])

    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
    )
    return current


@transaction.atomic
def delete_draft_recipe_version(*, version: RecipeVersion, reason: str = "") -> None:
    """
    Discard a draft nobody has built on.

    Safe to delete precisely because Task 3.1 has no approval: nothing outside
    the draft can reference it, since production batches (Task 3.4) may only
    name an approved version. Its lines, steps, servings and links cascade with
    it. The version number is **not** released.

    The audit event is written before the delete, with the target's identity
    captured as text, so the trail survives the row.
    """
    current = _lock_draft(version)
    previous = snapshot(current)
    identity = f"{current.recipe.code} v{current.version_number}"
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.RecipeVersion",
        target_id=str(current.pk),
        previous_state=previous,
        new_state={"discarded": identity},
        reason=reason,
    )
    current.delete()


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def _line_basis(
    *,
    item: InventoryItem,
    quantity: Decimal,
    entered_unit: UnitOfMeasure | None,
    package_unit: PackageUnit | None,
    measured_base_quantity: object | None,
    on_date: datetime.date,
) -> tuple[Decimal, Decimal | None, int | None]:
    """
    Convert an entered quantity into the item's base unit, once, at entry.

    Two entry modes, and exactly one applies:

    * **A unit of measure.** Converted through `apps.units`, which refuses a
      unit outside the item's dimension — 350 g of a base-unit-`KG` item
      converts; 350 ml does not, and that is a data-entry error caught at the
      unit layer rather than a puzzle for costing.
    * **A package.** The effective `ItemPackageConversion` is resolved by date
      and its factor **snapshotted onto the line**, so correcting the sack size
      next year cannot restate what this recipe meant. A `VARIABLE` package has
      no arithmetic answer — one meat container is whatever it weighed — so the
      caller must supply the measured base quantity, exactly as posting a
      variable-weight receipt must.

    Returns the base quantity, and the conversion factor and version to
    snapshot when a package was used.
    """
    if (entered_unit is None) == (package_unit is None):
        raise ValidationError(
            {"entered_unit": _("أدخل الكمية بوحدة قياس أو بعبوة، وليس بالاثنين معاً.")}
        )

    if entered_unit is not None:
        # `convert` raises when the dimensions differ — mass against volume is
        # the KD-19 case, and it is refused rather than guessed.
        base = convert(quantity, from_unit=entered_unit, to_unit=item.base_unit)
        return quantize_calculation(base, field="base_quantity"), None, None

    # The exclusivity check above leaves exactly one possibility here.
    package = package_unit
    if package is None:  # pragma: no cover - unreachable past the check above
        raise ValidationError({"package_unit": _("العبوة مطلوبة.")})

    conversion = effective_conversion(item=item, package_unit=package, on_date=on_date)
    if conversion is None:
        raise ValidationError({"package_unit": _("لا يوجد تحويل فعّال لهذه العبوة في هذا التاريخ.")})

    if conversion.conversion_type == ConversionType.VARIABLE:
        if measured_base_quantity is None:
            raise ValidationError(
                {"base_quantity": _("العبوة متغيرة الوزن — أدخل الكمية الأساس المقاسة.")}
            )
        base = _require_positive(measured_base_quantity, "base_quantity")
    else:
        base = quantize_calculation(quantity * conversion.factor_to_base, field="base_quantity")
    return base, quantize_factor(conversion.factor_to_base), conversion.version


@transaction.atomic
def add_recipe_line(
    *,
    version: RecipeVersion,
    item: InventoryItem,
    entered_quantity: object,
    entered_unit: UnitOfMeasure | None = None,
    package_unit: PackageUnit | None = None,
    measured_base_quantity: object | None = None,
    measured_quantity: object | None = None,
    loss_rate: object | None = None,
    cost_class: str = RecipeLineCostClass.FOOD,
    preparation_stage: str = "",
    measurement_basis: str = MeasurementBasis.RAW,
    is_optional: bool = False,
    note: str = "",
    line_order: int | None = None,
    on_date: datetime.date | None = None,
    source_document: str = "",
    source_page: int | None = None,
    source_sha256: str = "",
    source_reference: str = "",
    source_note: str = "",
) -> RecipeLine:
    """
    Add an ingredient or a packaging line to an open draft.

    `line_order` is drawn under the version's lock when the caller does not
    supply one, so two concurrent additions cannot claim the same position.
    """
    current = _lock_draft(version)
    _require_same_organization(current.recipe.organization, item=item)
    if not item.is_active:
        raise ValidationError({"item": _("الصنف مؤرشف.")})
    document, page = _validate_provenance(source_document, source_page)

    quantity = _require_positive(entered_quantity, "entered_quantity")
    base, factor, conversion_version = _line_basis(
        item=item,
        quantity=quantity,
        entered_unit=entered_unit,
        package_unit=package_unit,
        measured_base_quantity=measured_base_quantity,
        on_date=on_date or timezone.localdate(),
    )

    if line_order is None:
        highest = current.lines.aggregate(highest=Max("line_order"))["highest"] or 0
        line_order = highest + 1

    line = RecipeLine(
        version=current,
        line_order=line_order,
        item=item,
        entered_unit=entered_unit,
        entered_quantity=quantity,
        base_quantity=base,
        measured_quantity=(
            _require_positive(measured_quantity, "measured_quantity")
            if measured_quantity is not None
            else None
        ),
        package_unit=package_unit,
        conversion_factor=factor,
        conversion_version=conversion_version,
        loss_rate=(
            quantize_calculation(ensure_decimal(loss_rate, field="loss_rate"))
            if loss_rate is not None
            else None
        ),
        cost_class=cost_class,
        preparation_stage=preparation_stage,
        measurement_basis=measurement_basis,
        is_optional=is_optional,
        note=note.strip(),
        source_document=document,
        source_page=page,
        source_sha256=source_sha256.strip(),
        source_reference=source_reference.strip(),
        source_note=source_note.strip(),
    )
    line.full_clean()
    line.save()
    record_audit_event(action=AuditAction.CREATED, target=line, new_state=snapshot(line))
    return line


@transaction.atomic
def update_recipe_line(
    *,
    line: RecipeLine,
    entered_quantity: object,
    entered_unit: UnitOfMeasure | None = None,
    package_unit: PackageUnit | None = None,
    measured_base_quantity: object | None = None,
    measured_quantity: object | None = None,
    loss_rate: object | None = None,
    cost_class: str = RecipeLineCostClass.FOOD,
    preparation_stage: str = "",
    measurement_basis: str = MeasurementBasis.RAW,
    is_optional: bool = False,
    note: str = "",
    on_date: datetime.date | None = None,
) -> RecipeLine:
    """
    Correct a line on an open draft.

    The item is absent from the signature: replacing chicken with lamb is a
    different ingredient, not a corrected quantity, and the step links pointing
    at this line would silently come to mean something else. Remove the line
    and add the right one.
    """
    current_line = (
        RecipeLine.objects.select_for_update().select_related("version", "item").get(pk=line.pk)
    )
    _lock_draft(current_line.version)
    previous = snapshot(current_line)

    quantity = _require_positive(entered_quantity, "entered_quantity")
    base, factor, conversion_version = _line_basis(
        item=current_line.item,
        quantity=quantity,
        entered_unit=entered_unit,
        package_unit=package_unit,
        measured_base_quantity=measured_base_quantity,
        on_date=on_date or timezone.localdate(),
    )

    current_line.entered_unit = entered_unit
    current_line.entered_quantity = quantity
    current_line.base_quantity = base
    current_line.package_unit = package_unit
    current_line.conversion_factor = factor
    current_line.conversion_version = conversion_version
    current_line.measured_quantity = (
        _require_positive(measured_quantity, "measured_quantity")
        if measured_quantity is not None
        else None
    )
    current_line.loss_rate = (
        quantize_calculation(ensure_decimal(loss_rate, field="loss_rate"))
        if loss_rate is not None
        else None
    )
    current_line.cost_class = cost_class
    current_line.preparation_stage = preparation_stage
    current_line.measurement_basis = measurement_basis
    current_line.is_optional = is_optional
    current_line.note = note.strip()
    current_line.full_clean()
    current_line.save()

    record_audit_event(
        action=AuditAction.UPDATED,
        target=current_line,
        previous_state=previous,
        new_state=snapshot(current_line),
    )
    return current_line


@transaction.atomic
def remove_recipe_line(*, line: RecipeLine, reason: str = "") -> None:
    """
    Take a line off an open draft.

    Its substitutes and step links cascade with it — a substitute for an
    ingredient the recipe no longer contains is not information, and a step
    that adds a line that is gone is a step that lies.
    """
    current_line = RecipeLine.objects.select_for_update().select_related("version").get(pk=line.pk)
    _lock_draft(current_line.version)
    previous = snapshot(current_line)
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.RecipeLine",
        target_id=str(current_line.pk),
        previous_state=previous,
        new_state={"removed": current_line.item.code},
        reason=reason,
    )
    current_line.delete()


# ---------------------------------------------------------------------------
# Substitutes
# ---------------------------------------------------------------------------


@transaction.atomic
def add_recipe_line_substitute(
    *,
    line: RecipeLine,
    substitute_item: InventoryItem,
    priority: int | None = None,
    reason: str = "",
    note: str = "",
    source_document: str = "",
    source_page: int | None = None,
    source_sha256: str = "",
    source_reference: str = "",
    source_note: str = "",
) -> RecipeLineSubstitute:
    """
    Record an acceptable stand-in for this line's item.

    Guidance, never automation (RCP-022): nothing in this module substitutes
    anything. When a production batch is built in Task 3.4 the screen offers
    this list, and the batch records what was **actually** consumed.

    A line may carry several ranked alternatives. `priority` is drawn under the
    version's lock when the caller does not supply one, because the rank is an
    **order** rather than a suggestion: two substitutes both at priority 1
    would leave the batch screen choosing by primary key, which is not a
    business decision.
    """
    current_line = (
        RecipeLine.objects.select_for_update().select_related("version", "item").get(pk=line.pk)
    )
    version = _lock_draft(current_line.version)

    if substitute_item.pk == current_line.item_id:
        raise ValidationError({"substitute_item": _("البديل لا يمكن أن يكون نفس الصنف.")})
    _require_same_organization(version.recipe.organization, item=substitute_item)
    document, page = _validate_provenance(source_document, source_page)

    if priority is None:
        highest = current_line.substitutes.filter(is_active=True).aggregate(
            highest=Max("priority")
        )["highest"]
        priority = (highest or 0) + 1

    substitute = RecipeLineSubstitute(
        line=current_line,
        substitute_item=substitute_item,
        priority=priority,
        reason=reason.strip(),
        note=note.strip(),
        source_document=document,
        source_page=page,
        source_sha256=source_sha256.strip(),
        source_reference=source_reference.strip(),
        source_note=source_note.strip(),
    )
    substitute.full_clean()
    substitute.save()
    record_audit_event(
        action=AuditAction.CREATED, target=substitute, new_state=snapshot(substitute)
    )
    return substitute


@transaction.atomic
def update_recipe_line_substitute(
    *,
    substitute: RecipeLineSubstitute,
    priority: int | None = None,
    reason: str = "",
    note: str = "",
    is_active: bool = True,
) -> RecipeLineSubstitute:
    """Reorder, annotate, or archive a substitute."""
    current = (
        RecipeLineSubstitute.objects.select_for_update()
        .select_related("line", "line__version")
        .get(pk=substitute.pk)
    )
    _lock_draft(current.line.version)
    previous = snapshot(current)

    if priority is not None:
        current.priority = priority
    current.reason = reason.strip()
    current.note = note.strip()
    current.is_active = is_active
    current.full_clean()
    current.save()

    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
    )
    return current


@transaction.atomic
def remove_recipe_line_substitute(*, substitute: RecipeLineSubstitute, reason: str = "") -> None:
    """Take a substitute off a line."""
    current = (
        RecipeLineSubstitute.objects.select_for_update()
        .select_related("line", "line__version", "substitute_item")
        .get(pk=substitute.pk)
    )
    _lock_draft(current.line.version)
    previous = snapshot(current)
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.RecipeLineSubstitute",
        target_id=str(current.pk),
        previous_state=previous,
        new_state={"removed": current.substitute_item.code},
        reason=reason,
    )
    current.delete()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@transaction.atomic
def add_recipe_step(
    *,
    version: RecipeVersion,
    instruction_ar: str,
    sequence: int | None = None,
    instruction_en: str = "",
    stage: str = "",
    expected_duration: datetime.timedelta | None = None,
    temperature_c: object | None = None,
    heat_instruction_ar: str = "",
    checkpoint_ar: str = "",
    is_critical: bool = False,
    media_reference: str = "",
    note: str = "",
    source_document: str = "",
    source_page: int | None = None,
    source_sha256: str = "",
    source_reference: str = "",
    source_note: str = "",
) -> RecipeStep:
    """
    Add a numbered step to an open draft's method.

    `temperature_c` stays `None` unless a source gives a **number**. The recipe
    book gives نار هادئة, جمر, قدر الضغط and تنور — heat instructions, never
    degrees — and those belong in `heat_instruction_ar`. Nothing here infers a
    Celsius value from a qualitative instruction, because an invented
    temperature becomes food-safety guidance nobody approved (RCP-068).
    """
    current = _lock_draft(version)
    if not instruction_ar.strip():
        raise ValidationError({"instruction_ar": _("نص الخطوة بالعربية مطلوب.")})
    document, page = _validate_provenance(source_document, source_page)

    if sequence is None:
        highest = current.steps.aggregate(highest=Max("sequence"))["highest"] or 0
        sequence = highest + 1

    step = RecipeStep(
        version=current,
        sequence=sequence,
        instruction_ar=instruction_ar.strip(),
        instruction_en=instruction_en.strip(),
        stage=stage,
        expected_duration=expected_duration,
        temperature_c=(
            quantize_calculation(ensure_decimal(temperature_c, field="temperature_c"))
            if temperature_c is not None
            else None
        ),
        heat_instruction_ar=heat_instruction_ar.strip(),
        checkpoint_ar=checkpoint_ar.strip(),
        is_critical=is_critical,
        media_reference=media_reference.strip(),
        note=note.strip(),
        source_document=document,
        source_page=page,
        source_sha256=source_sha256.strip(),
        source_reference=source_reference.strip(),
        source_note=source_note.strip(),
    )
    step.full_clean()
    step.save()
    record_audit_event(action=AuditAction.CREATED, target=step, new_state=snapshot(step))
    return step


@transaction.atomic
def update_recipe_step(
    *,
    step: RecipeStep,
    instruction_ar: str,
    sequence: int | None = None,
    instruction_en: str = "",
    stage: str = "",
    expected_duration: datetime.timedelta | None = None,
    temperature_c: object | None = None,
    heat_instruction_ar: str = "",
    checkpoint_ar: str = "",
    is_critical: bool = False,
    media_reference: str = "",
    note: str = "",
) -> RecipeStep:
    """Correct a step on an open draft."""
    current = RecipeStep.objects.select_for_update().select_related("version").get(pk=step.pk)
    _lock_draft(current.version)
    previous = snapshot(current)

    if not instruction_ar.strip():
        raise ValidationError({"instruction_ar": _("نص الخطوة بالعربية مطلوب.")})

    if sequence is not None:
        current.sequence = sequence
    current.instruction_ar = instruction_ar.strip()
    current.instruction_en = instruction_en.strip()
    current.stage = stage
    current.expected_duration = expected_duration
    current.temperature_c = (
        quantize_calculation(ensure_decimal(temperature_c, field="temperature_c"))
        if temperature_c is not None
        else None
    )
    current.heat_instruction_ar = heat_instruction_ar.strip()
    current.checkpoint_ar = checkpoint_ar.strip()
    current.is_critical = is_critical
    current.media_reference = media_reference.strip()
    current.note = note.strip()
    current.full_clean()
    current.save()

    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
    )
    return current


@transaction.atomic
def remove_recipe_step(*, step: RecipeStep, reason: str = "") -> None:
    """
    Take a step off an open draft.

    Its ingredient links cascade. **No line quantity changes** — a line's
    quantity is the whole quantity regardless of how many steps mention it
    (RCP-066), so removing the step that added the saffron does not remove the
    saffron.
    """
    current = RecipeStep.objects.select_for_update().select_related("version").get(pk=step.pk)
    _lock_draft(current.version)
    previous = snapshot(current)
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.RecipeStep",
        target_id=str(current.pk),
        previous_state=previous,
        new_state={"removed_sequence": current.sequence},
        reason=reason,
    )
    current.delete()


@transaction.atomic
def link_step_ingredient(
    *,
    step: RecipeStep,
    recipe_line: RecipeLine,
    share: object = Decimal("1"),
    note: str = "",
) -> RecipeStepIngredient:
    """
    Record that this line's ingredient enters at this step.

    Documentation, never arithmetic. The per-row bound `0 < share <= 1` is a
    check constraint; the **per-line sum** is held here, under the version's
    lock, because a sum across rows is not something a check constraint can
    see. Under 1 is legal and means the method has not described where the rest
    goes; over 1 claims to add more of an ingredient than the recipe contains.
    """
    current_step = RecipeStep.objects.select_for_update().select_related("version").get(pk=step.pk)
    version = _lock_draft(current_step.version)

    if recipe_line.version_id != current_step.version_id:
        raise ValidationError({"recipe_line": _("المكوّن يتبع نسخة أخرى.")})

    value = quantize_calculation(ensure_decimal(share, field="share"), field="share")
    if value <= 0 or value > 1:
        raise ValidationError({"share": _("الحصة يجب أن تكون أكبر من صفر ولا تتجاوز واحداً.")})

    already = RecipeStepIngredient.objects.filter(recipe_line=recipe_line).exclude(
        step=current_step
    ).aggregate(total=Sum("share"))["total"] or Decimal("0")
    if already + value > Decimal("1"):
        raise ValidationError(
            {"share": _("مجموع حصص هذا المكوّن عبر الخطوات لا يمكن أن يتجاوز واحداً.")}
        )

    link = RecipeStepIngredient(
        step=current_step, recipe_line=recipe_line, share=value, note=note.strip()
    )
    link.full_clean()
    link.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=link,
        new_state=snapshot(link),
        metadata={"version": str(version.pk)},
    )
    return link


@transaction.atomic
def unlink_step_ingredient(*, link: RecipeStepIngredient, reason: str = "") -> None:
    """Remove a step-to-line link. The line keeps its whole quantity."""
    current = (
        RecipeStepIngredient.objects.select_for_update()
        .select_related("step", "step__version")
        .get(pk=link.pk)
    )
    _lock_draft(current.step.version)
    previous = snapshot(current)
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.RecipeStepIngredient",
        target_id=str(current.pk),
        previous_state=previous,
        new_state={"unlinked": True},
        reason=reason,
    )
    current.delete()


# ---------------------------------------------------------------------------
# Servings
# ---------------------------------------------------------------------------


def _serving_basis(
    *, version: RecipeVersion, quantity: object, unit: UnitOfMeasure
) -> tuple[Decimal, Decimal]:
    """
    A serving's quantity in the output unit, and its share of one batch.

    `convert` refuses a unit outside the output basis's dimension, which is
    where KD-19 lands: the دقوس recipe yields cups of 80 ml and the plate cards
    consume 125 g, and **no density has been agreed**, so the conversion is
    refused rather than invented. The operator sees a clear domain error and
    the two figures stay unreconciled until somebody decides the density.
    """
    amount = _require_positive(quantity, "serving_quantity")
    base = convert(amount, from_unit=unit, to_unit=version.output_unit)
    base = quantize_calculation(base, field="serving_quantity")
    factor = quantize_factor(base / version.expected_output_quantity, field="factor_of_batch")
    if factor <= 0:
        raise ValidationError({"serving_quantity": _("نسبة الحصة يجب أن تكون أكبر من صفر.")})
    return base, factor


@transaction.atomic
def add_recipe_serving(
    *,
    version: RecipeVersion,
    code: str,
    name_ar: str,
    serving_quantity: object,
    serving_unit: UnitOfMeasure,
    name_en: str = "",
    is_primary: bool = False,
    rounding_increment: object | None = None,
    rounding_policy: str = ServingRoundingPolicy.NONE,
    measurement_basis: str = MeasurementBasis.COOKED,
    display_order: int | None = None,
    note: str = "",
    source_document: str = "",
    source_page: int | None = None,
    source_sha256: str = "",
    source_reference: str = "",
    source_note: str = "",
) -> RecipeServing:
    """
    Define a way of dividing this version's output.

    `factor_of_batch` is derived here and never supplied: a factor that
    disagreed with its own quantity would misprice everything downstream, and
    the only way to keep them in step is to have one of them computed.

    Nothing in this function names a dish, an animal or a cut. A half chicken
    is `serving_quantity=0.5` of a `حبة` output basis; a 350 g portion is
    `0.350 KG` against a `KG` basis. Both are rows (RCP-082).
    """
    current = _lock_draft(version)
    document, page = _validate_provenance(source_document, source_page)
    base, factor = _serving_basis(version=current, quantity=serving_quantity, unit=serving_unit)

    if display_order is None:
        highest = current.servings.aggregate(highest=Max("display_order"))["highest"] or 0
        display_order = highest + 1

    if is_primary:
        # One default answer to "what does one cost". Cleared under the
        # version's lock so two concurrent primaries cannot both survive; the
        # partial unique index is the backstop if they try.
        current.servings.filter(is_primary=True).update(is_primary=False)

    serving = RecipeServing(
        version=current,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        serving_quantity=_require_positive(serving_quantity, "serving_quantity"),
        serving_unit=serving_unit,
        base_quantity=base,
        factor_of_batch=factor,
        is_primary=is_primary,
        rounding_increment=(
            ensure_decimal(rounding_increment, field="rounding_increment")
            if rounding_increment is not None
            else None
        ),
        rounding_policy=rounding_policy,
        measurement_basis=measurement_basis,
        display_order=display_order,
        source_document=document,
        source_page=page,
        source_sha256=source_sha256.strip(),
        source_reference=source_reference.strip(),
        source_note=source_note.strip(),
    )
    serving.full_clean()
    serving.save()
    record_audit_event(action=AuditAction.CREATED, target=serving, new_state=snapshot(serving))
    return serving


@transaction.atomic
def update_recipe_serving(
    *,
    serving: RecipeServing,
    name_ar: str,
    serving_quantity: object,
    serving_unit: UnitOfMeasure,
    name_en: str = "",
    is_primary: bool = False,
    rounding_increment: object | None = None,
    rounding_policy: str = ServingRoundingPolicy.NONE,
    measurement_basis: str = MeasurementBasis.COOKED,
    display_order: int | None = None,
    is_active: bool = True,
) -> RecipeServing:
    """Correct a serving. The factor is always re-derived, never trusted."""
    current = RecipeServing.objects.select_for_update().select_related("version").get(pk=serving.pk)
    version = _lock_draft(current.version)
    previous = snapshot(current)

    base, factor = _serving_basis(version=version, quantity=serving_quantity, unit=serving_unit)

    if is_primary and not current.is_primary:
        version.servings.filter(is_primary=True).update(is_primary=False)

    current.name_ar = name_ar.strip()
    current.name_en = name_en.strip()
    current.serving_quantity = _require_positive(serving_quantity, "serving_quantity")
    current.serving_unit = serving_unit
    current.base_quantity = base
    current.factor_of_batch = factor
    current.is_primary = is_primary
    current.rounding_increment = (
        ensure_decimal(rounding_increment, field="rounding_increment")
        if rounding_increment is not None
        else None
    )
    current.rounding_policy = rounding_policy
    current.measurement_basis = measurement_basis
    if display_order is not None:
        current.display_order = display_order
    current.is_active = is_active
    current.full_clean()
    current.save()

    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
    )
    return current


@transaction.atomic
def remove_recipe_serving(*, serving: RecipeServing, reason: str = "") -> None:
    """Take a serving off an open draft."""
    current = RecipeServing.objects.select_for_update().select_related("version").get(pk=serving.pk)
    _lock_draft(current.version)
    previous = snapshot(current)
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.RecipeServing",
        target_id=str(current.pk),
        previous_state=previous,
        new_state={"removed": current.code},
        reason=reason,
    )
    current.delete()


# ---------------------------------------------------------------------------
# Nested components
# ---------------------------------------------------------------------------
#
# The four draft-only commands. Every one of them takes the same locks in the
# same order, and the order is the whole reason they are safe:
#
#     1. the organization's component graph lock   advisory, exclusive
#     2. the Recipe rows, ascending id             row locks
#     3. the RecipeVersion rows, ascending id      row locks
#
# The graph lock is taken first and unconditionally, so two callers naming the
# same two versions in opposite order are already serialised before either
# reaches a row lock. That is what makes "A → B racing B → A" impossible rather
# than merely unlikely, and it is why opposite caller order cannot deadlock.


def _graph_organization_of(version_id: int) -> int:
    """
    Which organization's graph lock a command on this version needs.

    An unlocked read, and deliberately so: it chooses a *lock key*, nothing
    more. A version's organization is immutable — the version cannot change
    recipe and the recipe cannot change organization — so this cannot pick the
    wrong lock, and if the row is gone the authoritative re-read that follows
    refuses. Reading it off the caller's object instead would be trusting a
    caller-supplied model, which this module does not do.
    """
    organization_id = (
        RecipeVersion.objects.filter(pk=version_id)
        .values_list("recipe__organization_id", flat=True)
        .first()
    )
    if organization_id is None:
        raise ValidationError(_("النسخة لم تعد موجودة."))
    return int(organization_id)


def _lock_component_child(component_version: RecipeVersion) -> RecipeVersion:
    """Re-read the child under a row lock. The caller's copy is a memory."""
    # `recipe__output_item` is deliberately absent from `select_related`: it is
    # nullable, so joining it makes the query an outer join and Postgres refuses
    # `FOR UPDATE` on the nullable side of one. The shape check reads
    # `output_item_id`, which is a column on `recipe` and needs no join at all.
    child = (
        RecipeVersion.objects.select_for_update()
        .filter(pk=component_version.pk)
        .select_related("recipe", "recipe__organization")
        .first()
    )
    if child is None:
        raise ValidationError({"component_version": _("النسخة الفرعية لم تعد موجودة.")})
    return child


def _lock_component_recipes(*recipe_ids: int) -> None:
    """Lock the Recipe rows involved, ascending id, before their versions."""
    for recipe_id in sorted(set(recipe_ids)):
        Recipe.objects.select_for_update().get(pk=recipe_id)


@transaction.atomic
def create_recipe_component(
    *,
    version: RecipeVersion,
    component_version: RecipeVersion,
    multiplier: object,
    line_order: int | None = None,
    note: str = "",
    actor: User | None = None,
    source_document: str = "",
    source_page: int | None = None,
    source_sha256: str = "",
    source_reference: str = "",
    source_note: str = "",
) -> RecipeComponent:
    """
    Add one non-stocked sub-recipe to an open draft, at one exact child version.

    The child is named by version and stays named by version forever (RCP-072).
    Nothing in this module ever re-points it: when the blend changes, the dish
    gets a **new version** that adopts the new blend, and the old dish keeps
    saying what it actually contained.

    `line_order` is drawn under the version's lock when the caller does not
    supply one, for the same reason a version number is: two concurrent adds
    that both computed `max + 1` from an unlocked read would choose the same
    position, and the unique constraint would refuse the second after the user
    had already filled the form.
    """
    organization_id = _graph_organization_of(version.pk)
    lock_component_graph(organization_id)

    _lock_component_recipes(version.recipe_id, component_version.recipe_id)
    current = _lock_draft(version)
    child = _lock_component_child(component_version)

    validate_component_edge(parent=current, child=child, graph=read_graph(organization_id))

    if RecipeComponent.objects.filter(version=current, component_recipe=child.recipe).exists():
        raise ValidationError(
            {
                "component_version": ValidationError(
                    _("الوصفة الفرعية %(code)s مضافة بالفعل إلى هذه النسخة.")
                    % {"code": child.recipe.code},
                    code="recipe_component_duplicate_child",
                )
            }
        )

    if line_order is None:
        highest = current.components.aggregate(highest=Max("line_order"))["highest"] or 0
        line_order = highest + 1

    document, page = _validate_provenance(source_document, source_page)
    component = RecipeComponent(
        version=current,
        recipe=current.recipe,
        line_order=line_order,
        component_version=child,
        component_recipe=child.recipe,
        multiplier=quantize_factor(
            ensure_decimal(multiplier, field="multiplier"), field="multiplier"
        ),
        note=note.strip(),
        created_by=actor,
        source_document=document,
        source_page=page,
        source_sha256=source_sha256.strip(),
        source_reference=source_reference.strip(),
        source_note=source_note.strip(),
    )
    component.full_clean()
    component.save()
    record_audit_event(action=AuditAction.CREATED, target=component, new_state=snapshot(component))
    return component


@transaction.atomic
def update_recipe_component(
    *,
    component: RecipeComponent,
    multiplier: object,
    component_version: RecipeVersion | None = None,
    note: str = "",
) -> RecipeComponent:
    """
    Correct a component while its parent is still a draft.

    Adopting a different child **version** is permitted here and only here: a
    draft is a document somebody is still writing, and choosing the wrong blend
    version at 10am should be fixable at 10:05. The moment the parent leaves
    `DRAFT` the same change becomes a new parent version, refused by the service
    and by the trigger alike.

    The graph is re-validated in full, because swapping the child can introduce
    a cycle or a fourth level that the original edge did not have.
    """
    organization_id = _graph_organization_of(component.version_id)
    lock_component_graph(organization_id)

    current = (
        RecipeComponent.objects.select_for_update()
        .select_related("version", "component_version")
        .get(pk=component.pk)
    )
    target = component_version if component_version is not None else current.component_version
    _lock_component_recipes(current.recipe_id, target.recipe_id)
    parent = _lock_draft(current.version)
    child = _lock_component_child(target)

    previous = snapshot(current)

    if child.pk != current.component_version_id:
        validate_component_edge(parent=parent, child=child, graph=read_graph(organization_id))
        clash = RecipeComponent.objects.filter(
            version=parent, component_recipe=child.recipe
        ).exclude(pk=current.pk)
        if clash.exists():
            raise ValidationError(
                {
                    "component_version": ValidationError(
                        _("الوصفة الفرعية %(code)s مضافة بالفعل إلى هذه النسخة.")
                        % {"code": child.recipe.code},
                        code="recipe_component_duplicate_child",
                    )
                }
            )
        current.component_version = child
        current.component_recipe = child.recipe

    current.multiplier = quantize_factor(
        ensure_decimal(multiplier, field="multiplier"), field="multiplier"
    )
    current.note = note.strip()
    current.full_clean()
    current.save(
        update_fields=["component_version", "component_recipe", "multiplier", "note", "updated_at"]
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=current,
        previous_state=previous,
        new_state=snapshot(current),
    )
    return current


@transaction.atomic
def remove_recipe_component(*, component: RecipeComponent, reason: str = "") -> None:
    """
    Take a sub-recipe off an open draft.

    Removing an edge can only ever make the graph shallower and more acyclic, so
    there is nothing to re-validate — but the graph lock is still taken, because
    a removal racing an approval would otherwise let the approval certify a
    graph that no longer exists.
    """
    organization_id = _graph_organization_of(component.version_id)
    lock_component_graph(organization_id)

    current = (
        RecipeComponent.objects.select_for_update()
        .select_related("version", "component_recipe")
        .get(pk=component.pk)
    )
    _lock_component_recipes(current.recipe_id)
    _lock_draft(current.version)

    previous = snapshot(current)
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.RecipeComponent",
        target_id=str(current.pk),
        previous_state=previous,
        new_state={"removed": current.component_recipe.code},
        reason=reason,
    )
    current.delete()


@transaction.atomic
def reorder_recipe_component(
    *, component: RecipeComponent, line_order: int
) -> list[RecipeComponent]:
    """
    Move one component to a position, and renumber its siblings `1..n`.

    Renumbering rather than swapping, so the result is always a dense, ordered,
    gap-free sequence no matter what the caller asks for — a position past the
    end lands at the end, and a position below one lands first.

    **Two passes, and the first one is not optional.** `UNIQUE(version,
    line_order)` is checked per statement, so assigning the final numbers
    directly would collide the moment two rows swapped places. The rows are
    first moved above the current maximum, then brought down to their final
    positions, which is the same trick the allocation helper uses one level up.
    """
    if line_order < 1:
        raise ValidationError(
            {
                "line_order": ValidationError(
                    _("الترتيب يجب أن يكون رقماً موجباً."),
                    code="recipe_component_order_not_positive",
                )
            }
        )

    organization_id = _graph_organization_of(component.version_id)
    lock_component_graph(organization_id)

    current = (
        RecipeComponent.objects.select_for_update().select_related("version").get(pk=component.pk)
    )
    _lock_component_recipes(current.recipe_id)
    parent = _lock_draft(current.version)

    siblings = list(
        RecipeComponent.objects.select_for_update().filter(version=parent).order_by("line_order")
    )
    others = [row for row in siblings if row.pk != current.pk]
    index = max(0, min(len(others), line_order - 1))
    ordered = [*others[:index], current, *others[index:]]

    offset = max((row.line_order for row in siblings), default=0) + 1
    for position, row in enumerate(ordered):
        row.line_order = offset + position
        row.save(update_fields=["line_order", "updated_at"])
    for position, row in enumerate(ordered, start=1):
        row.line_order = position
        row.save(update_fields=["line_order", "updated_at"])

    record_audit_event(
        action=AuditAction.UPDATED,
        target=parent,
        new_state={"component_order": [row.component_recipe_id for row in ordered]},
        reason=str(_("إعادة ترتيب المكوّنات الفرعية")),
    )
    return ordered


__all__ = [
    "PRODUCIBLE_ITEM_TYPES",
    "add_recipe_line",
    "add_recipe_line_substitute",
    "add_recipe_serving",
    "add_recipe_step",
    "archive_recipe",
    "create_draft_recipe_version",
    "create_recipe_component",
    "create_recipe",
    "create_recipe_category",
    "delete_draft_recipe_version",
    "link_step_ingredient",
    "reactivate_recipe",
    "remove_recipe_line",
    "remove_recipe_line_substitute",
    "remove_recipe_component",
    "remove_recipe_serving",
    "remove_recipe_step",
    "reorder_recipe_component",
    "set_recipe_branches",
    "unlink_step_ingredient",
    "update_draft_recipe_version",
    "update_recipe",
    "update_recipe_category",
    "update_recipe_component",
    "update_recipe_line",
    "update_recipe_line_substitute",
    "update_recipe_serving",
    "update_recipe_step",
]
