"""
Recording and cancelling staff and complimentary meals.

Two commands and nothing else. There is no `update_meal`, and its absence is
the design: a recorded meal is a statement about a day that has already
happened, and correcting it means saying the first statement was wrong and
making another. An edited row would quietly restate a variance report somebody
has already read and acted on.

## What these commands deliberately do not do

They move **no stock and write no journal**. Not "not yet" — not at all, and
not by oversight. The ingredients of a staff meal already left stock through
the batch that cooked them or the issue that took them out of the store. Both
are posted economic events with movements behind them. Recording the meal as a
second stock issue would take the same kilogram out twice, and the resulting
variance would be a permanent structural overage nobody could explain.

What the record contributes is the **explanation**: it enters the theoretical
side of consumption so that fed-but-not-sold portions stop surfacing as
unexplained usage variance (RCP-043).

The accounting reclassification — moving staff-meal cost into a staff-benefit
expense account — is real practice and is **deferred, recorded** (RCP-044). It
needs an approved journal shape, an expense role and a theoretical-cost basis,
and none of the three exists in any approved document. The records accumulate
from day one so that the task, when approved, starts with its data already
there.

## The version is resolved once

A meal eaten on the 3rd is a portion of whatever the recipe said on the 3rd.
`resolve_recipe_version` answers that once, at recording, and the answer is
stored. Nothing re-resolves it — not a newer version activating, not the recipe
being superseded, not the report being rerun next year. That is the charter's
absolute rule, and re-resolving would let a recipe changed in June restate what
March consumed.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.quantity import quantize_calculation
from apps.core.services import record_audit_event, snapshot
from apps.kitchen.lifecycle import resolve_recipe_version
from apps.kitchen.models import (
    MealRecord,
    MealRecordStatus,
    MealType,
    Recipe,
    RecipeServing,
)

if TYPE_CHECKING:
    from apps.organizations.models import Branch
    from apps.users.models import User

ZERO = Decimal("0")


def _refuse(message: Any, code: str) -> ValidationError:
    """A domain refusal with a stable code — 422 at the API, never a 500."""
    return ValidationError(message, code=code)


def _positive(value: object, code: str) -> Decimal:
    """A Decimal above zero, or a named refusal. Never a float."""
    if isinstance(value, float):
        raise _refuse(_("استخدم Decimal وليس عدداً عشرياً ثنائياً."), "float_not_permitted")
    amount = Decimal(str(value))
    if amount <= ZERO:
        raise _refuse(_("الكمية يجب أن تكون أكبر من صفر."), code)
    return quantize_calculation(amount)


def meal_fingerprint(
    *,
    branch_id: int,
    recipe_id: int,
    meal_type: str,
    consumed_on: datetime.date,
    serving_id: int | None,
    quantity: Decimal,
    beneficiary: str,
    reason: str,
) -> str:
    """
    What "the same meal request" means.

    Everything that defines the request, so a retry after a timeout returns the
    record already made and a **changed** request under the same key is a
    conflict rather than a silent no-op returning the wrong row.
    """
    payload = {
        "command": "record_meal",
        "branch": branch_id,
        "recipe": recipe_id,
        "meal_type": meal_type,
        "consumed_on": consumed_on.isoformat(),
        "serving": serving_id,
        "quantity": str(quantity),
        "beneficiary": beneficiary.strip(),
        "reason": reason.strip(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _serving_for(
    *, version: Any, serving: RecipeServing | None
) -> tuple[RecipeServing | None, Decimal, str]:
    """
    The serving basis a portion is measured in, and what one portion is worth.

    Where the version defines servings, one is **required**: "three portions"
    of a recipe with a full and a half serving is not a quantity until somebody
    says which. Where it defines none, the version's own output unit is the
    basis and a portion is one output unit.

    Nothing here invents a conversion. The serving carries its own quantity and
    unit, approved on the version, and this reads them.
    """
    available = list(version.servings.select_related("serving_unit").order_by("code"))
    if not available:
        if serving is not None:
            raise _refuse(_("هذه النسخة لا تعرّف حصصاً."), "meal_version_has_no_servings")
        return None, Decimal("1"), version.output_unit.code

    if serving is None:
        raise _refuse(
            _("اختر الحصة: هذه النسخة تعرّف أكثر من أساس للحصة."),
            "meal_serving_required",
        )
    if serving.version_id != version.pk:
        raise _refuse(
            _("الحصة المختارة لا تخص النسخة السارية في هذا التاريخ."),
            "meal_serving_version_mismatch",
        )
    return serving, quantize_calculation(serving.serving_quantity), serving.serving_unit.code


@transaction.atomic
def record_meal(
    *,
    branch: Branch,
    recipe: Recipe,
    meal_type: str,
    consumed_on: datetime.date,
    quantity: Decimal,
    idempotency_key: str,
    serving: RecipeServing | None = None,
    beneficiary: str = "",
    reason: str = "",
    notes: str = "",
    replaces: MealRecord | None = None,
    actor: User | None = None,
) -> MealRecord:
    """
    Record one meal. Resolves the exact version once, and moves nothing.

    Every input is explicit. There is no default date, because "today" is the
    wrong answer for a meal recorded on Monday for Sunday's evening shift; and
    no default recipe version, because the whole point is that the date decides
    which one applies.
    """
    if not idempotency_key.strip():
        raise _refuse(_("التسجيل يحتاج مفتاح تكرار."), "idempotency_key_required")
    if meal_type not in MealType.values:
        raise _refuse(_("نوع وجبة غير معروف."), "meal_type_unknown")
    if branch.organization_id != recipe.organization_id:
        raise _refuse(_("الفرع لا يتبع منظمة هذه الوصفة."), "meal_branch_organization_mismatch")
    portions = _positive(quantity, "meal_quantity_not_positive")

    # The exact version, once. A refusal here is a real answer: a recipe with
    # no version in force on that date has nothing to record a portion of.
    version = resolve_recipe_version(recipe=recipe, branch=branch, on_date=consumed_on)
    resolved_serving, serving_size, unit_code = _serving_for(version=version, serving=serving)

    fingerprint = meal_fingerprint(
        branch_id=branch.pk,
        recipe_id=recipe.pk,
        meal_type=meal_type,
        consumed_on=consumed_on,
        serving_id=resolved_serving.pk if resolved_serving else None,
        quantity=portions,
        beneficiary=beneficiary,
        reason=reason,
    )
    existing = MealRecord.objects.filter(
        organization=recipe.organization, idempotency_key=idempotency_key.strip()
    ).first()
    if existing is not None:
        # Matched on the key **and** the fingerprint, never the key alone: a
        # caller who reused a key with a corrected quantity and received the
        # uncorrected record would believe the correction had gone through.
        if existing.request_fingerprint != fingerprint:
            raise _refuse(_("نفس المفتاح مع طلب مختلف."), "idempotency_key_conflict")
        return existing

    if replaces is not None and replaces.status != MealRecordStatus.CANCELLED:
        raise _refuse(_("لا يمكن استبدال سجل لم يُلغَ بعد."), "meal_replaced_record_is_not_cancelled")

    record = MealRecord.objects.create(
        organization=recipe.organization,
        branch=branch,
        meal_type=meal_type,
        recipe=recipe,
        recipe_version=version,
        serving=resolved_serving,
        quantity=portions,
        output_base_quantity=quantize_calculation(portions * serving_size),
        output_unit_code=unit_code,
        consumed_on=consumed_on,
        beneficiary=beneficiary.strip(),
        reason=reason.strip(),
        notes=notes.strip(),
        recorded_by=actor,
        replaces=replaces,
        idempotency_key=idempotency_key.strip(),
        request_fingerprint=fingerprint,
    )
    record_audit_event(
        action=AuditAction.CREATED,
        target=record,
        branch=branch,
        new_state=snapshot(record),
        reason=reason or "meal recorded",
        metadata={
            "meal_type": meal_type,
            "recipe": recipe.code,
            "version": version.version_number,
            "consumed_on": consumed_on.isoformat(),
            # Stated on the audit event as well as in the docstring, because
            # this is the record somebody reads when they ask why no stock
            # moved.
            "stock_effect": "none",
            "journal_effect": "none",
        },
    )
    return record


@transaction.atomic
def cancel_meal(*, record: MealRecord, reason: str, actor: User | None = None) -> MealRecord:
    """
    Cancel a recorded meal, with a reason that is kept forever.

    Cancellation moves no stock and writes no journal either — there was
    nothing to undo, because recording moved nothing. What changes is that the
    row stops contributing to theoretical consumption; it stays visible in
    history, because a correction that hides what it corrected is not a
    correction.
    """
    if not reason.strip():
        raise _refuse(_("الإلغاء يحتاج سبباً."), "reason_required")

    locked = MealRecord.objects.select_for_update().filter(pk=record.pk).first()
    if locked is None:
        raise _refuse(_("هذا السجل لم يعد موجوداً."), "meal_record_no_longer_exists")
    if locked.status == MealRecordStatus.CANCELLED:
        raise _refuse(_("هذا السجل ملغى بالفعل."), "meal_record_already_cancelled")

    previous = snapshot(locked)
    locked.status = MealRecordStatus.CANCELLED
    locked.cancelled_by = actor
    locked.cancelled_at = timezone.now()
    locked.cancellation_reason = reason.strip()
    locked.save(
        update_fields=[
            "status",
            "cancelled_by",
            "cancelled_at",
            "cancellation_reason",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.CANCELLED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
        metadata={"stock_effect": "none", "journal_effect": "none"},
    )
    return locked


__all__ = ["cancel_meal", "meal_fingerprint", "record_meal"]
