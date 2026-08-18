"""
Drafting a production batch: expansion, scaling, actual quantities, readiness.

Task 3.4's command layer. Every service here writes a **draft** and nothing
else — no stock moves, no journal is written, no number is drawn, no lot is
created, and no status ever leaves `DRAFT`. That is not a convention: a check
constraint named `production_batch_is_draft_only_until_task_3_5` refuses a
`POSTED` row outright, so the boundary survives a service somebody writes
later, a migration, the admin, and a psql prompt.

## The plan and reality are different rows

`ProductionBatchLine` is what the recipe said. `ProductionBatchActualLine` is
what the kitchen did. Keeping them apart is RCP-030 and it is the whole design:
an operator who used 3 kg instead of 4 has recorded a fact, and a system that
made them edit the requirement to say so would be a system in which no batch
ever disagrees with its recipe — which is exactly the report the module exists
to produce.

So a variance is never refused. More, less, zero on an optional line, a split
across two approved substitutes: all permitted. What *is* refused is an item
nobody approved, because that is not a variance, it is a different recipe.

## Version selection happens once

Creation names a recipe, a branch and an explicit planned business date, and
`resolve_recipe_version` answers which structure governed that branch on that
day. The answer is stored and **never re-resolved** — not when a newer version
is activated, not when a child is superseded, not when the batch is reopened
next week. A wrong date is corrected by discarding the draft and drafting
again, because re-pointing an existing batch would change what a half-finished
document claims the kitchen is making.

## Canonical lock order

Documented here because a second caller that guesses will eventually deadlock
against the first:

    Recipe → exact RecipeVersion → ProductionBatch
    → ProductionBatchLine  by (line_order)
    → ProductionBatchActualLine  by (line_order, entry_order)

Never the order the payload arrived in. Two operators editing the same batch's
lines in opposite order is the ordinary case, not the exotic one.

**The batch comes first even when the command names a single row.** Editing one
actual quantity looks like a reason to lock that row and nothing else, and doing
so would invert this order against `rescale_production_batch` — which takes the
batch, then the lines, then the actuals — and deadlock under the ordinary case of
one operator rescaling while another types a quantity. `_lock_actual_row` and
`_lock_requirement` exist so no command has to remember that.

The batch lock is also what makes the actual rows **serialized**: with it held,
no other approved writer can add or remove a row of this batch, so "is a row left
to say what was consumed?" is a question with a stable answer rather than one two
simultaneous removals could each answer optimistically.

**No inventory stock-key lock is taken, deliberately.** Task 3.4 moves nothing,
so locking stock to make a draft "safe" would let a drafting screen block a
delivery — a worse failure than any it prevents. Availability, lots, expiry and
negative-stock refusal are Task 3.5's, at posting, where the stock actually
moves.

## Readiness is derived, never stored

`validate_production_batch_ready` returns problems. It is not a status, it
writes nothing, and there is no `READY` row anywhere — a stored readiness flag
would be a second opinion that goes stale the moment somebody edits a quantity,
and the only way to trust it would be to recompute it, which is this function.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.quantity import quantize_calculation, quantize_factor
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import InventoryItem, PackageUnit, Warehouse
from apps.kitchen.expansion import ExpandedLeaf, LeafKind, expand_recipe_version
from apps.kitchen.lifecycle import resolve_recipe_version
from apps.kitchen.models import (
    ActualLineKind,
    CostLineSource,
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchLine,
    ProductionBatchStatus,
    Recipe,
    RecipeLineSubstitute,
    RecipeVersion,
)
from apps.organizations.models import Branch
from apps.units.models import UnitOfMeasure
from apps.users.models import User

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise

ZERO = Decimal("0")

#: The identity conversion, at the stored factor precision.
ONE_FACTOR = quantize_factor(Decimal("1"))

_KIND_FROM_LEAF = {
    LeafKind.DIRECT: CostLineSource.DIRECT,
    LeafKind.COMPONENT: CostLineSource.COMPONENT,
}


def _refuse(message: str | _StrPromise, code: str, field: str | None = None) -> ValidationError:
    """A domain refusal with a stable code — 422 at the API, never a 500."""
    if field is None:
        return ValidationError(message, code=code)
    return ValidationError({field: ValidationError(message, code=code)})


def _positive(value: object, field: str) -> Decimal:
    """A Decimal above zero, or a named refusal. Never a float."""
    if isinstance(value, float):
        raise _refuse(_("استخدم Decimal وليس عدداً عشرياً ثنائياً."), "float_not_permitted", field)
    amount = Decimal(str(value))
    if amount <= ZERO:
        raise _refuse(_("القيمة يجب أن تكون أكبر من صفر."), "value_not_positive", field)
    return amount


def _not_negative(value: object, field: str) -> Decimal:
    """Zero is a fact — an optional line the kitchen skipped. Negative is not."""
    if isinstance(value, float):
        raise _refuse(_("استخدم Decimal وليس عدداً عشرياً ثنائياً."), "float_not_permitted", field)
    amount = Decimal(str(value))
    if amount < ZERO:
        raise _refuse(_("الكمية لا يمكن أن تكون سالبة."), "value_is_negative", field)
    return amount


# ---------------------------------------------------------------------------
# The scaling arithmetic — one definition, mirrored by the database
# ---------------------------------------------------------------------------
#
# `multiplier`, `expected_output_quantity` and every `planned_base_quantity`
# are three views of one decision, and migration 0015 enforces at COMMIT that
# they agree. That makes the arithmetic below a contract rather than an
# implementation detail: a service that computes a planned quantity a second way
# is a service whose writes the database will refuse.
#
# So there is exactly one definition of each product, every writer uses it —
# creation, rescale, and the preview that must agree with creation — and the
# trigger recomputes the same expression in SQL.
#
# Two things had to change for that mirror to be exact, and both were real
# defects the trigger surfaced rather than caused:
#
# 1. **The multiplier is quantized before it is used, not only before it is
#    stored.** `create` stored `quantize_calculation(scale)` but scaled the
#    expected output by the raw `scale`, so a multiplier of `2.5000005` stored
#    `2.500001` and produced an output computed from a figure the row does not
#    contain.
# 2. **The cumulative multiplier is taken at the precision it is stored at.**
#    The walk composes it at full precision (RCP-073) — up to three twelve-place
#    component factors, so up to thirty-six places — and its column holds twelve.
#    Creation scaled by the unrounded value while a later rescale scaled by the
#    stored one, so rescaling a two-level recipe to the multiplier it already had
#    could change its planned quantities. Twelve places is the storage boundary
#    for that fact; it is rounded once, there, and every consumer reads the
#    stored figure.

#: Enough digits for the scaling products to be **exact**.
#:
#: `source_base_quantity` holds 21 digits, `cumulative_multiplier` 32 and
#: `multiplier` 21 — an exact product of at most 74. PostgreSQL `numeric`
#: multiplication is exact, while Python's default context carries 28 and would
#: round the product before it was quantized. Below 74 the two can differ by one
#: unit in the sixth decimal, and the trigger would then refuse a rescale the
#: service had computed correctly by its own lights. Eighty leaves margin.
_EXACT_PRECISION = 80


def _scale(value: object, field: str = "multiplier") -> Decimal:
    """
    The multiplier as it will be **stored**: positive, quantized once, rechecked.

    Rechecked because quantizing can reach zero. A multiplier of `0.0000001`
    passes `_positive`, stores as `0.000000`, and then violates
    `production_batch_multiplier_is_positive` — a 500 where the operator should
    have read a sentence saying the figure is below the smallest storable scale.
    """
    stored = quantize_calculation(_positive(value, field))
    if stored <= ZERO:
        raise _refuse(
            _("المعامل أصغر من أصغر قيمة قابلة للتخزين."),
            "production_batch_multiplier_below_precision",
            field,
        )
    return stored


def scaled_expected_output(*, version_output: Decimal, multiplier: Decimal) -> Decimal:
    """`version expected output × multiplier`, quantized once. Mirrored by 0015."""
    with localcontext() as context:
        context.prec = _EXACT_PRECISION
        product = version_output * multiplier
    return quantize_calculation(product)


def scaled_line_quantity(
    *, source_base_quantity: Decimal, cumulative_multiplier: Decimal, multiplier: Decimal
) -> Decimal:
    """
    `source × cumulative × multiplier`, quantized once. Mirrored by 0015.

    `cumulative_multiplier` must be the value **as stored** — pass it through
    `quantize_factor` before the first write. Scaling by an unrounded cumulative
    and storing a rounded one is how creation and rescale came to disagree.
    """
    with localcontext() as context:
        context.prec = _EXACT_PRECISION
        product = source_base_quantity * cumulative_multiplier * multiplier
    return quantize_calculation(product)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def _lock_batch(batch_id: int, *, require_draft: bool = True) -> ProductionBatch:
    """
    Re-read one batch under a row lock, and refuse anything but a draft.

    Re-read rather than trusted: the caller's instance may be seconds old, and
    a status checked against a stale copy is not a check. The `DRAFT` test is
    here rather than in each caller because every command below needs it and a
    command that forgot would be the one that mattered.

    A batch that has **gone** is a named refusal, not a `DoesNotExist`. One
    operator discarding a draft while another types into it is an ordinary
    Tuesday, and the second operator should read a sentence rather than meet a
    500 — the row they were editing was legitimately thrown away.

    `require_draft=False` exists for the two Task 3.5 commands that legitimately
    act on a batch that is **not** a draft — posting reads one to decide whether
    a retry is a replay or a conflict, and reversal acts on a posted one. Both
    check the status themselves against the state they need, which is why the
    default stays `True`: an editing command that forgot the check would be the
    one that mattered, and the default is what a new command inherits.
    """
    batch = (
        ProductionBatch.objects.select_for_update()
        .select_related("recipe", "recipe_version", "branch", "warehouse", "organization")
        .filter(pk=batch_id)
        .first()
    )
    if batch is None:
        raise _gone("batch")
    if require_draft and not batch.is_draft:
        raise _refuse(
            _("هذه الدفعة لم تعد مسودة ولا يمكن تعديلها."),
            "production_batch_is_not_draft",
        )
    return batch


def _lock_lines(batch: ProductionBatch) -> list[ProductionBatchLine]:
    """
    Every requirement of one batch, locked in **line order**.

    Canonical order, never the payload's: two operators editing the same batch
    from opposite ends of the list is the ordinary case.
    """
    return list(
        ProductionBatchLine.objects.select_for_update().filter(batch=batch).order_by("line_order")
    )


def _lock_actuals(lines: list[ProductionBatchLine]) -> list[ProductionBatchActualLine]:
    """Every actual row under those requirements, in the same canonical order."""
    return list(
        ProductionBatchActualLine.objects.select_for_update()
        .filter(line__in=lines)
        .order_by("line__line_order", "entry_order")
    )


def _gone(what: str) -> ValidationError:
    """
    A row another committed transaction removed while this one was waiting.

    Named refusals rather than `DoesNotExist`, because every one of these is an
    ordinary thing for two operators to do at once: one discards a draft while
    the other edits a quantity in it. The second reads a sentence saying the row
    is gone; a 500 would tell them the system broke, when what happened is that
    somebody legitimately threw their work away.
    """
    if what == "batch":
        return _refuse(_("هذه الدفعة لم تعد موجودة."), "production_batch_no_longer_exists")
    if what == "line":
        return _refuse(_("هذا المتطلب لم يعد موجوداً."), "production_line_no_longer_exists")
    return _refuse(_("سطر الاستهلاك لم يعد موجوداً."), "production_actual_no_longer_exists")


def _lock_actual_row(actual_pk: int) -> tuple[ProductionBatch, ProductionBatchActualLine]:
    """
    The **batch first**, then the actual row — the documented order, in one place.

    Two reasons this is a helper rather than three near-identical preambles:

    * **The order is the deadlock story.** `rescale_production_batch` takes batch
      → lines → actuals. A command that took the actual row first and the batch
      second would deadlock against it under the ordinary case of one operator
      rescaling while another edits a quantity, and the module's own docstring
      names the order it would be violating.
    * **The batch lock is what serializes actual-row mutations.** Once it is held,
      no other approved writer can add or remove a row of this batch, so
      "how many rows are left" is a question with a stable answer. Checking that
      *before* taking the lock is how a requirement ends up with none.

    The batch id is read unlocked first, which is safe because it is frozen — a
    row cannot move to another batch. If either lookup finds nothing, another
    transaction committed a removal while this one waited, and that is a named
    refusal rather than an unhandled `DoesNotExist`.
    """
    owner = (
        ProductionBatchActualLine.objects.filter(pk=actual_pk)
        .values_list("line__batch_id", flat=True)
        .first()
    )
    if owner is None:
        raise _gone("actual")
    batch = _lock_batch(owner)
    locked = (
        ProductionBatchActualLine.objects.select_for_update()
        .select_related("line", "line__batch", "item", "item__base_unit")
        .filter(pk=actual_pk)
        .first()
    )
    if locked is None:
        raise _gone("actual")
    return batch, locked


def _lock_requirement(line_pk: int) -> tuple[ProductionBatch, ProductionBatchLine]:
    """The batch first, then one requirement. Same order, same reasons."""
    owner = (
        ProductionBatchLine.objects.filter(pk=line_pk).values_list("batch_id", flat=True).first()
    )
    if owner is None:
        raise _gone("line")
    batch = _lock_batch(owner)
    locked = (
        ProductionBatchLine.objects.select_for_update()
        .select_related("batch", "source_line", "item", "item__base_unit")
        .filter(pk=line_pk)
        .first()
    )
    if locked is None:
        raise _gone("line")
    return batch, locked


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def batch_fingerprint(
    *,
    recipe: Recipe,
    branch: Branch,
    warehouse: Warehouse,
    planned_business_date: datetime.date,
    multiplier: Decimal,
) -> str:
    """
    A digest of what the create command asked for.

    The resolved `RecipeVersion` and the flattened requirements are deliberately
    **absent**: they are consequences of the request, not part of it. Including
    the version would mean an activation landing between two retries turned an
    honest retry into a conflict, which is the opposite of what idempotency is
    for.

    Public ids rather than primary keys, because a fingerprint that changed
    with a sequence would call the same request different on a restored
    database.
    """
    payload = {
        "command": "create_production_batch",
        "recipe": str(recipe.public_id),
        "branch": branch.pk,
        "warehouse": warehouse.pk,
        "planned_business_date": planned_business_date.isoformat(),
        "multiplier": str(quantize_calculation(multiplier)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _replay(*, organization: Any, idempotency_key: str, fingerprint: str) -> ProductionBatch | None:
    """
    The batch this key already produced **in this organization**, if any.

    Scoped in the query itself. A lookup on the key alone would hand another
    organization's production plan to whoever guessed their key.
    """
    existing = ProductionBatch.objects.filter(
        organization=organization, idempotency_key=idempotency_key
    ).first()
    if existing is None:
        return None
    if existing.request_fingerprint != fingerprint:
        raise ValidationError(
            _("مفتاح التكرار %(key)s مستخدم في %(organization)s لطلب مختلف."),
            code="idempotency_key_conflict",
            params={"key": idempotency_key, "organization": organization.code},
        )
    return existing


# ---------------------------------------------------------------------------
# Creating a batch
# ---------------------------------------------------------------------------


def _validate_shape(*, recipe: Recipe, branch: Branch, warehouse: Warehouse) -> None:
    """
    One organization, one branch, one warehouse, and a recipe that produces.

    Checked here so the operator reads an Arabic sentence naming the field, and
    again by a database trigger so a hand-made insert cannot slip past.
    """
    if branch.organization_id != recipe.organization_id:
        raise _refuse(_("الفرع يتبع مؤسسة أخرى."), "production_batch_foreign_branch", "branch")
    if warehouse.branch_id != branch.pk:
        raise _refuse(
            _("المخزن لا يتبع هذا الفرع."),
            "production_batch_wrong_warehouse",
            "warehouse",
        )
    if recipe.output_item_id is None:
        # RCP-032: producing a portion recipe would create stock of an item
        # that deliberately does not exist.
        raise _refuse(
            _("لا يمكن إنتاج وصفة بلا صنف ناتج — هذه وصفة تحضير عند الطلب."),
            "production_batch_recipe_has_no_output_item",
            "recipe",
        )


def _write_lines(
    *, batch: ProductionBatch, leaves: list[ExpandedLeaf], multiplier: Decimal
) -> list[ProductionBatchLine]:
    """
    Freeze the expansion into requirement rows.

    The quantity is `source × cumulative × multiplier` through
    `scaled_line_quantity`, quantized **once**, here, at this column (RCP-073,
    ADR-006). Quantizing the per-recipe quantity first would round a gram of
    saffron twice before anybody scaled it.

    The **cumulative** multiplier is the one figure taken at its stored
    precision rather than the walk's: twelve places is its column, the walk can
    compose thirty-six, and scaling by a value the row will not contain is what
    made a rescale to an unchanged multiplier move the planned quantities.
    """
    rows = [
        ProductionBatchLine(
            batch=batch,
            line_order=number,
            source_version=leaf.version,
            source_line=leaf.line,
            source_line_public_id=leaf.line.public_id,
            source_version_public_id=leaf.version.public_id,
            component_path=leaf.path_display,
            component_label_path=leaf.label_path,
            source_kind=_KIND_FROM_LEAF[leaf.kind],
            item=leaf.line.item,
            item_code=leaf.line.item.code,
            item_name=leaf.line.item.name_ar,
            base_unit_code=leaf.line.item.base_unit.code,
            source_base_quantity=leaf.line.base_quantity,
            cumulative_multiplier=quantize_factor(leaf.cumulative_multiplier),
            planned_base_quantity=scaled_line_quantity(
                source_base_quantity=leaf.line.base_quantity,
                cumulative_multiplier=quantize_factor(leaf.cumulative_multiplier),
                multiplier=multiplier,
            ),
            cost_class=str(leaf.line.cost_class),
            is_optional=bool(leaf.line.is_optional),
            preparation_stage=str(leaf.line.preparation_stage or ""),
        )
        for number, leaf in enumerate(leaves, start=1)
    ]
    ProductionBatchLine.objects.bulk_create(rows)
    return list(
        ProductionBatchLine.objects.filter(batch=batch)
        .select_related("item", "item__base_unit")
        .order_by("line_order")
    )


def _write_default_actuals(*, lines: list[ProductionBatchLine], actor: User | None) -> None:
    """
    One actual row per requirement, pre-filled with the plan.

    So the common case — the kitchen used what the recipe said — needs no
    typing at all, and a variance is a deliberate edit rather than an omission
    somebody has to notice.

    **Always in the item's own base unit, at a factor of 1.** Never in the
    package the recipe line happened to be entered in, and never with a
    measurement: a generated row must not claim somebody observed two sacks or
    weighed a variable container. The base unit is the one measure that needs no
    observation to be true, so the default states the plan in it and nothing
    more.
    """
    ProductionBatchActualLine.objects.bulk_create(
        [
            ProductionBatchActualLine(
                line=line,
                entry_order=1,
                kind=ActualLineKind.PRIMARY,
                item=line.item,
                substitute=None,
                entered_quantity=line.planned_base_quantity,
                entered_unit=line.item.base_unit,
                package_unit=None,
                # One base unit is one base unit. A real factor, not a filler.
                conversion_factor=ONE_FACTOR,
                conversion_version=None,
                measured_base_quantity=None,
                base_quantity=line.planned_base_quantity,
                recorded_by=actor,
            )
            for line in lines
        ]
    )


@transaction.atomic
def create_production_batch(
    *,
    recipe: Recipe,
    branch: Branch,
    warehouse: Warehouse,
    planned_business_date: datetime.date,
    multiplier: object,
    actor: User | None,
    idempotency_key: str,
    notes: str = "",
) -> ProductionBatch:
    """
    Draft one batch from the version in force at this branch on this date.

    `planned_business_date` has no default. A batch drafted on Monday for
    Sunday's production must use Sunday's recipe, and a service that quietly
    meant *today* would be right during development and wrong the first time
    somebody planned ahead.

    Atomic: the header, every requirement and every default actual row commit
    together or none do. A batch with half its requirements is worse than no
    batch, because it looks like a plan.
    """
    if not idempotency_key.strip():
        raise _refuse(_("مفتاح التكرار مطلوب."), "idempotency_key_required", "idempotency_key")
    _validate_shape(recipe=recipe, branch=branch, warehouse=warehouse)
    # Quantized here, once, and used everywhere below. The row must be scaled by
    # the multiplier it stores, not by the figure the caller happened to send.
    scale = _scale(multiplier)

    fingerprint = batch_fingerprint(
        recipe=recipe,
        branch=branch,
        warehouse=warehouse,
        planned_business_date=planned_business_date,
        multiplier=scale,
    )
    replayed = _replay(
        organization=recipe.organization,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
    )
    if replayed is not None:
        return replayed

    # The one resolution there is, and the only time it runs for this batch.
    version = resolve_recipe_version(recipe=recipe, branch=branch, on_date=planned_business_date)
    leaves = expand_recipe_version(version)
    if not leaves:
        raise _refuse(
            _("النسخة السارية بلا مكوّنات، فلا يوجد ما يُنتج."),
            "production_batch_version_has_no_lines",
        )

    batch = ProductionBatch(
        organization=recipe.organization,
        branch=branch,
        warehouse=warehouse,
        recipe=recipe,
        recipe_version=version,
        planned_business_date=planned_business_date,
        multiplier=scale,
        expected_output_quantity=scaled_expected_output(
            version_output=version.expected_output_quantity, multiplier=scale
        ),
        expected_output_unit_code=version.output_unit.code,
        status=ProductionBatchStatus.DRAFT,
        number="",
        notes=notes,
        created_by=actor,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    batch.full_clean(exclude=["created_at", "updated_at"])
    batch.save()

    lines = _write_lines(batch=batch, leaves=leaves, multiplier=scale)
    _write_default_actuals(lines=lines, actor=actor)

    record_audit_event(
        action=AuditAction.CREATED,
        target=batch,
        branch=branch,
        previous_state=None,
        new_state=snapshot(batch),
        reason=notes,
    )
    return ProductionBatch.objects.get(pk=batch.pk)


# ---------------------------------------------------------------------------
# Actual consumption
# ---------------------------------------------------------------------------


def _unit_factor(*, item: InventoryItem, unit: UnitOfMeasure) -> Decimal:
    """
    How many base units one `unit` is, for this item.

    `1` for the item's own base unit, `0.001` for grams against a KG item.
    Derived through `apps.units.convert`, which refuses a unit outside the
    item's dimension — so a millilitre against a KG item is caught here rather
    than becoming a factor nobody can interpret.
    """
    from apps.units.services import convert

    return quantize_factor(convert(Decimal("1"), from_unit=unit, to_unit=item.base_unit))


def _actual_basis(
    *,
    item: InventoryItem,
    quantity: Decimal,
    entered_unit: UnitOfMeasure | None,
    package_unit: PackageUnit | None,
    measured_base_quantity: object | None,
    on_date: datetime.date,
) -> tuple[Decimal, Decimal, int | None]:
    """
    Convert an entered actual into the item's base unit, once, at entry.

    Delegates to `services._line_basis` for the arithmetic — the **certified**
    conversion the recipe lines already use. Writing a second one here would be
    a second interpretation of what 350 g of a KG-based item means, and the two
    would agree right up until somebody fixed one of them.

    What this adds is a **uniform factor snapshot**. `_line_basis` returns no
    factor for a unit entry, because a recipe line does not need one; an actual
    row does, so readiness can ask one question instead of three. The factor is
    always the real "base units per entered unit" — never fabricated, and never
    a placeholder.

    Zero is permitted here where a recipe line's quantity may not be: an
    optional requirement the kitchen skipped is a real fact about a real batch.
    A zero **package** entry still resolves its package's factor by date, so the
    snapshot records which sack size was in force — but it never invents a
    VARIABLE measurement, because there is nothing to measure.
    """
    from apps.kitchen.services import _line_basis

    if (entered_unit is None) == (package_unit is None):
        raise _refuse(
            _("أدخل الكمية بوحدة قياس أو بعبوة، وليس بالاثنين معاً."),
            "production_actual_one_entry_mode",
            "entered_unit",
        )

    if entered_unit is not None:
        factor = _unit_factor(item=item, unit=entered_unit)
        if quantity == ZERO:
            return ZERO, factor, None
        base, _unused, _version = _line_basis(
            item=item,
            quantity=quantity,
            entered_unit=entered_unit,
            package_unit=None,
            measured_base_quantity=None,
            on_date=on_date,
        )
        return base, factor, None

    # A package. Resolve the effective conversion even at zero, so the row
    # records which sack size was in force — but a VARIABLE package at zero has
    # nothing to measure, and inventing a measurement would be inventing a fact.
    from apps.inventory.models import ConversionType
    from apps.inventory.selectors import effective_conversion

    # The exclusivity check above leaves exactly one possibility here.
    package = package_unit
    if package is None:  # pragma: no cover - unreachable past the check above
        raise _refuse(_("العبوة مطلوبة."), "production_actual_one_entry_mode", "package_unit")
    conversion = effective_conversion(item=item, package_unit=package, on_date=on_date)
    if conversion is None:
        raise _refuse(
            _("لا يوجد تحويل فعّال لهذه العبوة في هذا التاريخ."),
            "production_actual_no_effective_conversion",
            "package_unit",
        )
    factor = quantize_factor(conversion.factor_to_base)

    if quantity == ZERO:
        return ZERO, factor, conversion.version
    if conversion.conversion_type == ConversionType.VARIABLE and measured_base_quantity is None:
        raise _refuse(
            _("العبوة متغيرة الوزن — أدخل الكمية الأساس المقاسة."),
            "production_actual_variable_package_needs_a_measurement",
            "measured_base_quantity",
        )
    base, snapshot_factor, version = _line_basis(
        item=item,
        quantity=quantity,
        entered_unit=None,
        package_unit=package,
        measured_base_quantity=measured_base_quantity,
        on_date=on_date,
    )
    return base, snapshot_factor or factor, version


def _approved_item(
    *, line: ProductionBatchLine, item: InventoryItem
) -> tuple[str, RecipeLineSubstitute | None]:
    """
    Whether this item may stand in for this requirement, and on what authority.

    Three refusals, and each is a different mistake:

    * an item that is neither the requirement's own nor an approved substitute
      **of this source line** — a substitute approved for the rice line is not
      approved for the oil line;
    * an archived substitute — somebody withdrew that approval;
    * an item from another organization — refused before it is even looked up,
      because a foreign id must never widen scope.
    """
    if item.organization_id != line.batch.organization_id:
        raise _refuse(_("الصنف يتبع مؤسسة أخرى."), "production_actual_foreign_item", "item")
    if item.pk == line.item_id:
        return ActualLineKind.PRIMARY, None

    substitute = RecipeLineSubstitute.objects.filter(
        line=line.source_line, substitute_item=item, is_active=True
    ).first()
    if substitute is None:
        raise _refuse(
            _("هذا الصنف ليس الصنف الأصلي ولا بديلاً معتمداً لهذا السطر."),
            "production_actual_item_not_approved",
            "item",
        )
    return ActualLineKind.SUBSTITUTE, substitute


def _touched(batch: ProductionBatch) -> bool:
    """
    Whether an operator has edited anything a rescale would overwrite.

    True when any actual row differs from the plan it was created with, when a
    substitute exists, or when an output has been entered. That is the whole
    test `rescale_production_batch` refuses on: rescaling an untouched batch
    changes numbers nobody typed, and rescaling a touched one silently discards
    numbers somebody did.
    """
    if batch.actual_output_base_quantity is not None:
        return True
    rows = ProductionBatchActualLine.objects.filter(line__batch=batch).select_related("line")
    for row in rows:
        if row.kind != ActualLineKind.PRIMARY:
            return True
        if row.item_id != row.line.item_id:
            return True
        if row.base_quantity != row.line.planned_base_quantity:
            return True
    if rows.count() != ProductionBatchLine.objects.filter(batch=batch).count():
        return True
    return False


@transaction.atomic
def update_production_batch_actuals(
    *,
    actual: ProductionBatchActualLine,
    entered_quantity: object,
    entered_unit: UnitOfMeasure | None = None,
    package_unit: PackageUnit | None = None,
    measured_base_quantity: object | None = None,
    note: str = "",
    actor: User | None = None,
) -> ProductionBatchActualLine:
    """
    Record what was actually consumed on one row.

    More than planned, less, or zero — all accepted. A variance is the batch
    variance report's business, never a refusal here (RCP-030): refusing would
    teach kitchens to falsify quantities to match the recipe, which is the one
    outcome that makes the whole module useless.
    """
    batch, locked = _lock_actual_row(actual.pk)
    previous = snapshot(locked)

    amount = _not_negative(entered_quantity, "entered_quantity")
    base, factor, version = _actual_basis(
        item=locked.item,
        quantity=amount,
        entered_unit=entered_unit or (None if package_unit else locked.item.base_unit),
        package_unit=package_unit,
        measured_base_quantity=measured_base_quantity,
        on_date=batch.planned_business_date,
    )

    locked.entered_quantity = quantize_calculation(amount)
    locked.entered_unit = entered_unit or (None if package_unit else locked.item.base_unit)
    locked.package_unit = package_unit
    locked.conversion_factor = factor
    locked.conversion_version = version
    locked.measured_base_quantity = (
        quantize_calculation(measured_base_quantity) if measured_base_quantity is not None else None
    )
    locked.base_quantity = quantize_calculation(base)
    locked.note = note
    locked.recorded_by = actor or locked.recorded_by
    locked.full_clean(exclude=["created_at", "updated_at"])
    locked.save()

    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=batch.branch,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return ProductionBatchActualLine.objects.get(pk=locked.pk)


@transaction.atomic
def add_production_batch_substitute(
    *,
    line: ProductionBatchLine,
    item: InventoryItem,
    entered_quantity: object,
    entered_unit: UnitOfMeasure | None = None,
    package_unit: PackageUnit | None = None,
    measured_base_quantity: object | None = None,
    reason: str = "",
    actor: User | None = None,
) -> ProductionBatchActualLine:
    """
    Record an approved stand-in beside — not instead of — the primary row.

    A split is the case this exists for: 3 kg of the primary plus 1 kg of a
    substitute is two facts about one requirement, and the caller decides what
    the primary row becomes. Nothing here reduces it automatically, because
    "the rest was substituted" is an assumption and the operator knows.

    **This never edits the `RecipeVersion`.** Choosing a substitute records
    what a batch consumed; the recipe still says what it says.
    """
    batch, locked_line = _lock_requirement(line.pk)

    kind, substitute = _approved_item(line=locked_line, item=item)
    if kind == ActualLineKind.PRIMARY:
        raise _refuse(
            _("هذا هو الصنف الأصلي — عدّل سطره بدل إضافة بديل."),
            "production_actual_item_is_the_primary",
            "item",
        )

    amount = _not_negative(entered_quantity, "entered_quantity")
    base, factor, conversion_version = _actual_basis(
        item=item,
        quantity=amount,
        entered_unit=entered_unit or (None if package_unit else item.base_unit),
        package_unit=package_unit,
        measured_base_quantity=measured_base_quantity,
        on_date=batch.planned_business_date,
    )

    highest = (
        ProductionBatchActualLine.objects.filter(line=locked_line)
        .order_by("-entry_order")
        .values_list("entry_order", flat=True)
        .first()
        or 0
    )
    row = ProductionBatchActualLine(
        line=locked_line,
        entry_order=highest + 1,
        kind=ActualLineKind.SUBSTITUTE,
        item=item,
        substitute=substitute,
        entered_quantity=quantize_calculation(amount),
        entered_unit=entered_unit or (None if package_unit else item.base_unit),
        package_unit=package_unit,
        conversion_factor=factor,
        conversion_version=conversion_version,
        measured_base_quantity=(
            quantize_calculation(measured_base_quantity)
            if measured_base_quantity is not None
            else None
        ),
        base_quantity=quantize_calculation(base),
        reason=reason,
        recorded_by=actor,
    )
    row.full_clean(exclude=["created_at", "updated_at"])
    row.save()

    record_audit_event(
        action=AuditAction.CREATED,
        target=row,
        branch=batch.branch,
        previous_state=None,
        new_state=snapshot(row),
        reason=reason,
    )
    return ProductionBatchActualLine.objects.get(pk=row.pk)


@transaction.atomic
def remove_production_batch_substitute(
    *, actual: ProductionBatchActualLine, actor: User | None = None, reason: str = ""
) -> None:
    """
    Withdraw one actual row, substitute or primary.

    The primary row may go when a substitution was **complete** — the kitchen
    used none of the planned item — because forcing a zero row to remain would
    be forcing a statement about an item that never entered the pot. What may
    not happen is a requirement left with no actual row at all: that is not "no
    consumption", it is "nobody said", and readiness would refuse it anyway.
    """
    batch, locked = _lock_actual_row(actual.pk)

    # A complete substitution is a real thing: the kitchen used none of the
    # primary item at all. Removing the primary row is therefore permitted —
    # but only while something is left to say what *was* consumed, because a
    # requirement with no actual row at all is a requirement nobody answered,
    # and readiness would rightly refuse it. Setting the primary to zero is the
    # other, equally valid, way to say the same thing.
    #
    # Counted **under the batch lock**, which is what makes the count true rather
    # than a guess two simultaneous removals could each make. Both threads seeing
    # "one other row remains" and both deleting is precisely how a requirement
    # ends with none, and it is the batch lock — not this query — that prevents it.
    remaining = ProductionBatchActualLine.objects.filter(line=locked.line).exclude(pk=locked.pk)
    if not remaining.exists():
        raise _refuse(
            _("لا يمكن حذف آخر سطر استهلاك — اجعل كميته صفراً بدلاً من ذلك."),
            "production_actual_last_row_is_not_removable",
        )
    previous = snapshot(locked)
    locked.delete()
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.ProductionBatchActualLine",
        target_id=str(actual.pk),
        branch=batch.branch,
        previous_state=previous,
        new_state=None,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Rescaling
# ---------------------------------------------------------------------------


@transaction.atomic
def rescale_production_batch(
    *,
    batch: ProductionBatch,
    multiplier: object,
    actor: User | None = None,
    reset_actuals: bool = False,
    reason: str = "",
) -> ProductionBatch:
    """
    Change how many recipe batches this run is.

    Two modes, and the difference between them is whose numbers get discarded.

    **Ordinary rescale** is refused the moment an operator has entered
    anything — an edited quantity, a substitute, an output. Silently
    recomputing over their figures would be the system overwriting a
    measurement with an assumption, and there is no way to tell afterwards that
    it happened.

    **Reset-and-rescale** does exactly that, and requires the caller to say so:
    a deliberate flag *and* a non-empty reason. The consequence is the point of
    the command, so it is stated rather than discovered.

    Never re-flattens. The requirements keep their frozen source basis and
    exact paths, and only `planned_base_quantity` is recomputed — from
    `source × cumulative × new multiplier`, so scaling twice cannot compound a
    rounding error the way scaling a scaled figure would.

    **The header and every requirement move together or not at all.** Migration
    0015 checks the three derived figures against each other at COMMIT, so this
    is not merely tidy: a rescale that updated the multiplier and missed a line
    would be refused by the database rather than leaving a batch whose plan is
    half one scale and half another.
    """
    locked = _lock_batch(batch.pk)
    lines = _lock_lines(locked)
    _lock_actuals(lines)
    previous = snapshot(locked)

    scale = _scale(multiplier)
    touched = _touched(locked)

    if touched and not reset_actuals:
        raise _refuse(
            _(
                "الدفعة تحتوي كميات فعلية أو بدائل مسجّلة. "
                "استخدم إعادة الضبط مع سبب إن أردت تجاهلها."
            ),
            "production_batch_has_operator_edits",
            "multiplier",
        )
    if reset_actuals and not reason.strip():
        raise _refuse(
            _("إعادة الضبط تتطلب سبباً."), "production_batch_reset_requires_reason", "reason"
        )

    locked.multiplier = scale
    locked.expected_output_quantity = scaled_expected_output(
        version_output=locked.recipe_version.expected_output_quantity, multiplier=scale
    )
    if reset_actuals:
        locked.actual_output_entered_quantity = None
        locked.actual_output_unit = None
        locked.actual_output_base_quantity = None
    locked.save(
        update_fields=[
            "multiplier",
            "expected_output_quantity",
            "actual_output_entered_quantity",
            "actual_output_unit",
            "actual_output_base_quantity",
            "updated_at",
        ]
    )

    for line in lines:
        line.planned_base_quantity = scaled_line_quantity(
            source_base_quantity=line.source_base_quantity,
            cumulative_multiplier=line.cumulative_multiplier,
            multiplier=scale,
        )
        line.save(update_fields=["planned_base_quantity", "updated_at"])

    if reset_actuals:
        ProductionBatchActualLine.objects.filter(line__in=lines).delete()
        _write_default_actuals(
            lines=list(
                ProductionBatchLine.objects.filter(batch=locked)
                .select_related("item", "item__base_unit")
                .order_by("line_order")
            ),
            actor=actor,
        )
    else:
        # Untouched by definition, so the defaults follow the plan they mirror.
        for row in ProductionBatchActualLine.objects.select_related("line").filter(line__in=lines):
            row.entered_quantity = row.line.planned_base_quantity
            row.base_quantity = row.line.planned_base_quantity
            row.save(update_fields=["entered_quantity", "base_quantity", "updated_at"])

    refreshed = ProductionBatch.objects.get(pk=locked.pk)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=refreshed,
        branch=refreshed.branch,
        previous_state=previous,
        new_state=snapshot(refreshed),
        reason=reason,
    )
    return refreshed


# ---------------------------------------------------------------------------
# Output actuals
# ---------------------------------------------------------------------------


@transaction.atomic
def record_production_output(
    *,
    batch: ProductionBatch,
    entered_quantity: object,
    entered_unit: UnitOfMeasure,
    actor: User | None = None,
) -> ProductionBatch:
    """
    What actually came out. Entered by the operator, never derived (RCP-031).

    Deriving it from the inputs would assume a yield the kitchen did not
    measure, and the difference between expected and actual output is precisely
    what the yield report exists to show.
    """
    locked = _lock_batch(batch.pk)
    previous = snapshot(locked)
    output_item = locked.recipe.output_item
    if output_item is None:  # pragma: no cover — refused at creation
        raise _refuse(_("هذه الوصفة بلا صنف ناتج."), "production_batch_recipe_has_no_output_item")

    from apps.units.services import convert

    amount = _not_negative(entered_quantity, "entered_quantity")
    base = convert(amount, from_unit=entered_unit, to_unit=output_item.base_unit)

    locked.actual_output_entered_quantity = quantize_calculation(amount)
    locked.actual_output_unit = entered_unit
    locked.actual_output_base_quantity = quantize_calculation(base)
    locked.save(
        update_fields=[
            "actual_output_entered_quantity",
            "actual_output_unit",
            "actual_output_base_quantity",
            "updated_at",
        ]
    )
    refreshed = ProductionBatch.objects.get(pk=locked.pk)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=refreshed,
        branch=refreshed.branch,
        previous_state=previous,
        new_state=snapshot(refreshed),
    )
    return refreshed


@transaction.atomic
def update_production_batch_notes(
    *, batch: ProductionBatch, notes: str, actor: User | None = None
) -> ProductionBatch:
    """
    Correct the operator's own note on a draft.

    A service rather than a `save()` in a view or a route, for the reason every
    other mutation here is: the note is what an operator writes to explain a
    variance somebody will read next month, and an edit that left no audit trail
    would let the explanation change after the fact with nothing to say it had.

    Clearing the note is a real edit and is permitted. An empty string that meant
    "leave it alone" would make deleting a wrong note impossible through the only
    screen that offers it.
    """
    locked = _lock_batch(batch.pk)
    previous = snapshot(locked)
    locked.notes = notes
    locked.save(update_fields=["notes", "updated_at"])
    refreshed = ProductionBatch.objects.get(pk=locked.pk)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=refreshed,
        branch=refreshed.branch,
        previous_state=previous,
        new_state=snapshot(refreshed),
    )
    return refreshed


# ---------------------------------------------------------------------------
# Comparing an actual against its plan
# ---------------------------------------------------------------------------


def comparable_consumption(line: ProductionBatchLine) -> Decimal | None:
    """
    The actual quantity that may honestly be compared with this plan, or `None`.

    **Quantities in different dimensions are never added together.** A
    requirement for 4 KG of rice, met with 3 KG of rice and 2 litres of an
    approved oil substitute, has not been met with "5" of anything: kilograms
    and litres do not sum, and a system that printed 5 there would be inventing
    a number the kitchen never measured.

    So this sums only the rows whose item shares the requirement item's
    **dimension**, and returns `None` when no row does. A screen shows every
    actual row separately either way; this is only for the one place a single
    figure is genuinely meaningful — comparing like with like against the plan.

    The alternative — summing `base_quantity` because every row happens to have
    one — is the shape of bug that looks right in every test where the recipe
    and its substitutes share a unit, and is silently wrong the first time a
    kitchen substitutes across dimensions.
    """
    from apps.units.models import UnitOfMeasure

    target = UnitOfMeasure.objects.filter(pk=line.item.base_unit_id).first()
    if target is None:  # pragma: no cover - an item always has a base unit
        return None

    total: Decimal | None = None
    for row in line.actuals.all():
        unit = row.item.base_unit
        if unit.dimension != target.dimension:
            continue
        total = (total or ZERO) + row.base_quantity
    return total


def has_recorded_consumption(line: ProductionBatchLine) -> bool:
    """
    Whether **anything** was recorded against this requirement, in any dimension.

    Deliberately weaker than `comparable_consumption`: a requirement met
    entirely by a cross-dimension substitute *was* met, and readiness must not
    call it empty merely because the figure cannot be compared with the plan.
    """
    return any(row.base_quantity > ZERO for row in line.actuals.all())


@dataclass(frozen=True)
class ConsumptionComparison:
    """
    One requirement's plan beside the actual figure it may honestly be compared
    with — and, when there is none, the statement that there is none.

    A variance is only a number when the dimensions agree. A requirement for 4 KG
    of rice met with 2 litres of an approved oil substitute has a real, recorded
    consumption and **no comparable quantity**, and the screen must say so in
    words rather than print a figure that would be arithmetic on unlike things.
    Task 3.5 values every row separately, which is the answer to "then how is it
    costed"; it is not this module's answer, and inventing a physical conversion
    ratio here would put a number in the database that no scale ever produced.
    """

    line: ProductionBatchLine
    comparable_quantity: Decimal | None
    recorded: bool

    @property
    def is_comparable(self) -> bool:
        return self.comparable_quantity is not None

    @property
    def variance(self) -> Decimal | None:
        """Actual less plan, or `None` when the two are not comparable."""
        if self.comparable_quantity is None:
            return None
        return quantize_calculation(self.comparable_quantity - self.line.planned_base_quantity)

    @property
    def variance_display(self) -> str:
        variance = self.variance
        return "" if variance is None else f"{variance:f}"

    @property
    def statement(self) -> str:
        """What may be said about this requirement, in Arabic, without arithmetic."""
        if self.is_comparable:
            return ""
        if self.recorded:
            return str(_("سُجّل استهلاك ببُعد قياس مختلف — غير قابل للمقارنة الكمية."))
        return str(_("لا يوجد استهلاك مسجّل."))


def consumption_comparisons(batch: ProductionBatch) -> list[ConsumptionComparison]:
    """
    Every requirement of one batch, with its comparable actual or the reason none.

    One place, so a screen, the API and the verifier cannot each decide for
    themselves when a variance is a number.
    """
    lines = (
        ProductionBatchLine.objects.filter(batch=batch)
        .prefetch_related("actuals__item__base_unit")
        .select_related("item", "item__base_unit")
        .order_by("line_order")
    )
    return [
        ConsumptionComparison(
            line=line,
            comparable_quantity=comparable_consumption(line),
            recorded=has_recorded_consumption(line),
        )
        for line in lines
    ]


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadinessProblem:
    """One reason a draft is not yet ready for Task 3.5."""

    code: str
    message: str
    line_order: int | None = None


def validate_production_batch_ready(batch: ProductionBatch) -> list[ReadinessProblem]:
    """
    Everything standing between this draft and a posting, reported together.

    **Derived, never stored.** There is no `READY` status and no readiness
    column: a stored flag goes stale the moment somebody edits a quantity, and
    the only way to trust it would be to recompute it — which is this function.

    **All problems at once** where practical, because an operator fixing one
    thing at a time and resubmitting is an operator the system is wasting.

    Checks **no stock**. Availability, lots, expiry, locations, negative-stock
    refusal and open periods are Task 3.5's, at posting. A draft that reserved
    or checked stock would make drafting a thing that can fail for reasons
    nothing about the draft can fix.
    """
    problems: list[ReadinessProblem] = []

    def add(code: str, message: str, line_order: int | None = None) -> None:
        problems.append(ReadinessProblem(code=code, message=message, line_order=line_order))

    if not batch.is_draft:
        add("production_ready_not_draft", "الدفعة لم تعد مسودة.")
    if batch.recipe.output_item_id is None:
        add("production_ready_recipe_has_no_output", "الوصفة بلا صنف ناتج.")
    if batch.recipe_version.recipe_id != batch.recipe_id:
        add("production_ready_version_is_not_of_this_recipe", "النسخة لا تتبع هذه الوصفة.")
    if batch.multiplier <= ZERO:
        add("production_ready_multiplier_not_positive", "المعامل يجب أن يكون موجباً.")
    if batch.warehouse.branch_id != batch.branch_id:
        add("production_ready_warehouse_outside_branch", "المخزن لا يتبع فرع الدفعة.")
    if batch.expected_output_quantity <= ZERO:
        add("production_ready_expected_output_not_positive", "الناتج المتوقع غير موجب.")

    if batch.actual_output_base_quantity is None:
        add("production_ready_no_actual_output", "لم تُدخل كمية الناتج الفعلي.")
    elif batch.actual_output_base_quantity <= ZERO:
        add("production_ready_actual_output_not_positive", "الناتج الفعلي يجب أن يكون موجباً.")
    elif batch.actual_output_unit_id is None:
        add("production_ready_output_conversion_incomplete", "وحدة الناتج الفعلي ناقصة.")

    lines = list(
        ProductionBatchLine.objects.filter(batch=batch)
        .prefetch_related("actuals__item", "actuals__substitute")
        .select_related("item", "source_line")
        .order_by("line_order")
    )
    if not lines:
        add("production_ready_no_requirements", "لا توجد متطلبات في هذه الدفعة.")

    seen_paths: set[tuple[str, int]] = set()
    for line in lines:
        key = (line.component_path, line.source_line_id)
        if key in seen_paths:
            add(
                "production_ready_duplicate_path",
                f"المسار {line.component_path or '—'} مكرر.",
                line.line_order,
            )
        seen_paths.add(key)

        actuals = list(line.actuals.all())
        if not actuals:
            add(
                "production_ready_requirement_has_no_actual",
                f"السطر {line.line_order}: لا يوجد سطر استهلاك فعلي.",
                line.line_order,
            )
            continue

        # Never a cross-dimension sum. A requirement met entirely by a
        # substitute in another dimension has been met; what cannot be done is
        # adding litres to kilograms and calling the result one quantity.
        if not has_recorded_consumption(line) and not line.is_optional:
            add(
                "production_ready_required_line_is_zero",
                f"السطر {line.line_order}: متطلب إلزامي بلا استهلاك مسجّل.",
                line.line_order,
            )

        for row in actuals:
            if row.base_quantity < ZERO:
                add(
                    "production_ready_negative_actual",
                    f"السطر {line.line_order}: كمية سالبة.",
                    line.line_order,
                )
            if row.item_id != line.item_id and row.substitute_id is None:
                add(
                    "production_ready_item_not_approved",
                    f"السطر {line.line_order}: {row.item_id} ليس بديلاً معتمداً.",
                    line.line_order,
                )
            substitute = row.substitute
            if substitute is not None and substitute.line_id != line.source_line_id:
                add(
                    "production_ready_substitute_of_another_line",
                    f"السطر {line.line_order}: البديل معتمد لسطر آخر.",
                    line.line_order,
                )
            if (row.entered_unit_id is None) == (row.package_unit_id is None):
                add(
                    "production_ready_conversion_incomplete",
                    f"السطر {line.line_order}: طريقة الإدخال غير مكتملة.",
                    line.line_order,
                )
            if row.package_unit_id is not None and row.conversion_factor is None:
                add(
                    "production_ready_conversion_incomplete",
                    f"السطر {line.line_order}: معامل تحويل العبوة ناقص.",
                    line.line_order,
                )

    if batch.number:
        add("production_ready_draft_carries_a_number", "مسودة تحمل رقم مستند.")

    return problems


def production_batch_is_ready(batch: ProductionBatch) -> bool:
    """A convenience for a template. The list is the answer; this is its emptiness."""
    return not validate_production_batch_ready(batch)


@dataclass(frozen=True)
class ReadinessObservation:
    """
    Something an operator must be told that does **not** block a posting.

    Kept apart from `ReadinessProblem` on purpose. A cross-dimension substitution
    is legitimate — the kitchen used an approved stand-in measured in its own unit
    — so reporting it as a problem would refuse a batch for being correct. But
    saying nothing would leave a variance column blank with no explanation, and a
    blank that means "not comparable" is indistinguishable from a blank that means
    "nobody looked".
    """

    code: str
    message: str
    line_order: int | None = None


@dataclass(frozen=True)
class ProductionReadiness:
    """Everything the readiness panel shows: what blocks, and what must be said."""

    problems: list[ReadinessProblem]
    observations: list[ReadinessObservation]

    @property
    def is_ready(self) -> bool:
        return not self.problems

    @property
    def findings(self) -> list[ReadinessProblem | ReadinessObservation]:
        """Both lists, problems first, for a screen that renders one table."""
        return [*self.problems, *self.observations]


def production_batch_readiness(batch: ProductionBatch) -> ProductionReadiness:
    """
    Readiness with its non-blocking findings, computed once for the whole screen.

    `validate_production_batch_ready` remains the answer to "may this be posted",
    because that question has to stay a list of blockers and nothing else.
    """
    observations = [
        ReadinessObservation(
            code="production_ready_not_quantitatively_comparable",
            message=f"السطر {comparison.line.line_order}: {comparison.statement}",
            line_order=comparison.line.line_order,
        )
        for comparison in consumption_comparisons(batch)
        if comparison.recorded and not comparison.is_comparable
    ]
    return ProductionReadiness(
        problems=validate_production_batch_ready(batch), observations=observations
    )


# ---------------------------------------------------------------------------
# Discarding
# ---------------------------------------------------------------------------


@transaction.atomic
def discard_production_batch(
    *, batch: ProductionBatch, actor: User | None = None, reason: str = ""
) -> None:
    """
    Throw a draft away.

    Cascades to its own requirement and actual rows and to nothing else: no
    stock moved, no journal exists, and the recipe it was drafted from is
    untouched. A reason is required once an operator has entered anything,
    because discarding somebody's measurements without a word is the kind of
    thing a system should make you say out loud.
    """
    locked = _lock_batch(batch.pk)
    if _touched(locked) and not reason.strip():
        raise _refuse(
            _("هذه المسودة تحتوي كميات فعلية — أدخل سبب الحذف."),
            "production_batch_discard_requires_reason",
            "reason",
        )
    previous = snapshot(locked)
    branch = locked.branch
    identity = str(locked.pk)
    locked.delete()
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="kitchen.ProductionBatch",
        target_id=identity,
        branch=branch,
        previous_state=previous,
        new_state=None,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionPreview:
    """
    What a batch *would* contain, without creating one.

    The screen's answer to "show me before I commit". Reads the same expansion
    the real command writes, so a preview that looked right and a batch that
    came out different is not a state this can reach.
    """

    version: RecipeVersion
    multiplier: Decimal
    expected_output_quantity: Decimal
    output_unit_code: str
    leaves: list[ExpandedLeaf]

    @property
    def planned(self) -> list[tuple[ExpandedLeaf, Decimal]]:
        """
        Each leaf with the quantity it would be written at.

        Through `scaled_line_quantity`, on the **stored** cumulative precision,
        because the promise this screen makes is that the batch will contain
        these figures — not figures near them.
        """
        return [
            (
                leaf,
                scaled_line_quantity(
                    source_base_quantity=leaf.line.base_quantity,
                    cumulative_multiplier=quantize_factor(leaf.cumulative_multiplier),
                    multiplier=self.multiplier,
                ),
            )
            for leaf in self.leaves
        ]


def preview_production_batch(
    *,
    recipe: Recipe,
    branch: Branch,
    planned_business_date: datetime.date,
    multiplier: object,
) -> ProductionPreview:
    """
    Resolve and expand without writing anything.

    Same resolver, same expansion, same arithmetic as `create_production_batch`
    — deliberately, because a preview computed a second way is a preview that
    can disagree with the thing it previews.
    """
    scale = _scale(multiplier)
    version = resolve_recipe_version(recipe=recipe, branch=branch, on_date=planned_business_date)
    return ProductionPreview(
        version=version,
        multiplier=scale,
        expected_output_quantity=scaled_expected_output(
            version_output=version.expected_output_quantity, multiplier=scale
        ),
        output_unit_code=version.output_unit.code,
        leaves=expand_recipe_version(version),
    )
