"""
Writing an immutable cost snapshot, and reading one back.

The one place in Task 3.3 that creates a business record. Everything else is a
read: `apps/kitchen/costing.py` derives a card and forgets it. This turns one
card into a row somebody can be shown in September and asked to explain.

## What may become a snapshot

A **complete authoritative** card, and nothing else:

| Card | Snapshot |
|---|---|
| `ACTIVE` / `APPROVED` / `SUPERSEDED`, every leaf valued | written |
| `DRAFT` / `SUBMITTED` preview | `recipe_cost_version_not_authoritative` |
| `REJECTED` | `recipe_cost_version_not_authoritative` |
| any status, an unvalued leaf | `recipe_cost_snapshot_requires_complete_cost` |
| no primary serving, so no plate basis | `recipe_cost_snapshot_requires_plate_basis` |
| a warehouse in another organization | `recipe_cost_wrong_warehouse` |

There is no `force`, no partial mode and no "snapshot what we have". A costing
record with a hole in it is worse than no record: it looks like a total.

## Idempotency

Organization-scoped, on a key **and** a fingerprint of the request, never on
the key alone (`CLAUDE.md`). The key answers "have I seen this request id
before"; the fingerprint answers "and was it the same request". A caller
retrying after a timeout gets their original snapshot back with no second set
of lines; a caller reusing a key for a different version, warehouse, date or
calculation gets `idempotency_key_conflict`, not a silent hand-back of somebody
else's answer.

The fingerprint deliberately does **not** include the resulting figures. Two
identical requests a week apart legitimately produce different totals — stock
moved — and hashing the answer would turn every honest re-run into a conflict.
It hashes what was *asked for*.

Two intentional snapshots of the same version, warehouse and date are allowed
and expected: a menu is repriced more than once, and the second decision is a
real one. They simply need different keys. There is deliberately no uniqueness
constraint on `(version, warehouse, as_of_date)` — one would forbid the second
decision in the name of preventing a duplicate that the key already prevents.

## Append-only

Written once, inside one transaction, and never touched again. Database
triggers refuse UPDATE and DELETE on all three tables (migration 0009), the
admin is read-only, and there is no service, command or endpoint here that
edits or removes one. A correction is a new snapshot.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event
from apps.kitchen.costing import (
    RecipeCostCard,
    ServingAllocationState,
)
from apps.kitchen.models import (
    CostLineSource,
    CostValuationMode,
    RecipeCostSnapshot,
    RecipeCostSnapshotLine,
    RecipeCostSnapshotServing,
    ServingAllocationOutcome,
)
from apps.organizations.models import Organization
from apps.users.models import User

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise

ZERO = Decimal("0")

_KIND_TO_SOURCE = {
    "DIRECT": CostLineSource.DIRECT,
    "COMPONENT": CostLineSource.COMPONENT,
}

_STATE_TO_OUTCOME = {
    ServingAllocationState.ALLOCATED: ServingAllocationOutcome.ALLOCATED,
    ServingAllocationState.NO_WHOLE_SERVING: ServingAllocationOutcome.NO_WHOLE_SERVING,
}


def _refuse(
    message: str | _StrPromise, code: str, field_name: str | None = None
) -> ValidationError:
    if field_name is None:
        return ValidationError(message, code=code)
    return ValidationError({field_name: ValidationError(message, code=code)})


def snapshot_fingerprint(
    *,
    card: RecipeCostCard,
    reference: str,
    reason: str,
) -> str:
    """
    A digest of what this snapshot command asked for.

    The **exact version's public id**, not its primary key: the public id is
    the stable identity across a restore, and a fingerprint that changed with a
    sequence would call the same request different on a rebuilt database.

    The purpose inputs — `reference` and `reason` — are part of the request.
    Snapshotting the same card "for the July menu review" and "for the audit
    query" are two decisions, and a fingerprint blind to the purpose would call
    the second a retry of the first if somebody reused a key.

    The resulting figures are deliberately absent. See the module docstring.
    """
    payload = {
        "command": "create_recipe_cost_snapshot",
        "version": str(card.version.public_id),
        "warehouse": card.warehouse.pk,
        "as_of_date": card.as_of_date.isoformat(),
        "valuation_mode": str(card.cutoff.mode),
        "calculation_version": card.calculation_version,
        "reference": reference,
        "reason": reason,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _replay(
    *, organization: Organization, idempotency_key: str, fingerprint: str
) -> RecipeCostSnapshot | None:
    """
    The snapshot this key already produced **in this organization**, if any.

    Scoped in the query itself. A lookup on the key alone would hand another
    organization's costing record to whoever guessed their key — and a costing
    record is exactly the sort of thing a competitor would guess for.
    """
    existing = RecipeCostSnapshot.objects.filter(
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


@transaction.atomic
def create_recipe_cost_snapshot(
    *,
    card: RecipeCostCard,
    actor: User | None,
    idempotency_key: str,
    reference: str = "",
    reason: str = "",
    note: str = "",
) -> RecipeCostSnapshot:
    """
    Freeze one complete authoritative card into an append-only record.

    Atomic: the header, every line and every serving row commit together or
    none do. A half-written snapshot would be a document whose total disagreed
    with its own lines, which is the one state the verifier exists to catch and
    the one state this must never create.

    Every figure comes from the card that was already computed — nothing is
    recalculated here against a second read of inventory, which would let the
    stored lines observe a different ledger state from the stored total.
    """
    if not idempotency_key.strip():
        raise _refuse(
            _("مفتاح التكرار مطلوب."), "idempotency_key_required", field_name="idempotency_key"
        )
    if not card.is_authoritative:
        raise _refuse(
            _("لا يمكن حفظ لقطة كلفة من معاينة غير معتمدة."),
            "recipe_cost_version_not_authoritative",
            field_name="version",
        )
    if card.plate is None:
        # A record that could not explain its own plate cost later is a record
        # with a hole in it. Refused with its own code, so the operator is told
        # which half is missing rather than "incomplete".
        raise _refuse(
            _("لا يمكن حفظ لقطة كلفة بلا أساس لكلفة الطبق."),
            "recipe_cost_snapshot_requires_plate_basis",
        )
    if not card.is_complete:
        raise _refuse(
            _("لا يمكن حفظ لقطة كلفة قبل تسعير كل المكوّنات."),
            "recipe_cost_snapshot_requires_complete_cost",
        )

    organization = card.recipe.organization
    fingerprint = snapshot_fingerprint(card=card, reference=reference, reason=reason)
    replayed = _replay(
        organization=organization, idempotency_key=idempotency_key, fingerprint=fingerprint
    )
    if replayed is not None:
        return replayed

    snapshot = RecipeCostSnapshot(
        organization=organization,
        recipe=card.recipe,
        version=card.version,
        branch=card.branch,
        warehouse=card.warehouse,
        as_of_date=card.as_of_date,
        valuation_mode=CostValuationMode.POSTED_AS_OF,
        ledger_cutoff_sequence=card.cutoff.posted_sequence,
        calculation_version=card.calculation_version,
        is_authoritative=True,
        version_status=card.version_status,
        version_number=card.version.version_number,
        recipe_code=card.recipe.code,
        recipe_name=card.recipe.name_ar,
        warehouse_code=card.warehouse.code,
        output_quantity=card.output_quantity,
        output_unit_code=card.output_unit_code,
        food_total=card.food_total,
        packaging_total=card.packaging_total,
        accompaniment_total=card.accompaniment_total,
        total_material_cost=card.total_material_cost,
        cost_per_output_unit=card.cost_per_output_unit,
        portions_per_batch=card.plate.portions_per_batch,
        plate_cost=card.plate.plate_cost,
        primary_serving_code=card.plate.serving.code,
        created_by=actor,
        reason=reason,
        reference=reference,
        note=note,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    snapshot.full_clean(exclude=["created_at"])
    snapshot.save()

    RecipeCostSnapshotLine.objects.bulk_create(
        [
            RecipeCostSnapshotLine(
                snapshot=snapshot,
                line_number=line.line_number,
                component_path=line.path_display,
                source_kind=_KIND_TO_SOURCE[str(line.kind)],
                source_version=line.source_version,
                source_version_number=line.source_version.version_number,
                source_recipe_code=line.source_recipe.code,
                source_version_public_id=line.source_version.public_id,
                recipe_line=line.recipe_line,
                recipe_line_public_id=line.recipe_line.public_id,
                recipe_line_order=line.recipe_line.line_order,
                item=line.item,
                item_code=line.item.code,
                item_name=line.item.name_ar,
                item_unit_code=line.item.base_unit.code,
                cost_class=line.cost_class,
                cumulative_multiplier=line.cumulative_multiplier,
                effective_quantity=line.effective_quantity,
                valuation_quantity=line.valuation.quantity,
                valuation_value=line.valuation.value,
                valuation_lot_count=line.valuation.lot_count,
                unit_cost=line.unit_cost,
                raw_extension=line.raw_extension,
                allocated_extension=line.allocated_extension,
            )
            for line in card.lines
        ]
    )

    RecipeCostSnapshotServing.objects.bulk_create(
        [
            RecipeCostSnapshotServing(
                snapshot=snapshot,
                display_order=serving.serving.display_order,
                serving=serving.serving,
                serving_public_id=serving.serving.public_id,
                code=serving.serving.code,
                name_ar=serving.serving.name_ar,
                name_en=serving.serving.name_en,
                is_primary=serving.serving.is_primary,
                serving_quantity=serving.serving.serving_quantity,
                serving_unit_code=serving.serving.serving_unit.code,
                base_quantity=serving.serving.base_quantity,
                factor_of_batch=serving.factor_of_batch,
                whole_serving_count=max(serving.whole_count, 0),
                remainder_quantity=serving.remainder_quantity,
                cost_per_serving=serving.cost_per_serving,
                allocation_state=_STATE_TO_OUTCOME[serving.state],
                allocated_total=serving.allocated_total,
                minimum_allocated=serving.normal_cost_per_serving,
                maximum_allocated=serving.elevated_cost_per_serving,
                normal_serving_count=serving.normal_serving_count,
                elevated_serving_count=serving.elevated_serving_count,
                remainder_cost=serving.remainder_cost,
            )
            for serving in card.servings
        ]
    )

    record_audit_event(
        # `AuditAction.CREATED`, not a bespoke string: the column is a closed
        # enum, and a module inventing its own verb would be a trail nobody can
        # filter. What kind of thing was created is `target_type`'s job.
        action=AuditAction.CREATED,
        target=snapshot,
        branch=card.branch,
        previous_state=None,
        new_state={
            "recipe": snapshot.recipe_code,
            "version": snapshot.version_number,
            "warehouse": snapshot.warehouse_code,
            "as_of_date": snapshot.as_of_date.isoformat(),
            "total_material_cost": str(snapshot.total_material_cost),
            "plate_cost": str(snapshot.plate_cost),
            "portions_per_batch": str(snapshot.portions_per_batch),
            "primary_serving": snapshot.primary_serving_code,
            "ledger_cutoff_sequence": snapshot.ledger_cutoff_sequence,
            "calculation_version": snapshot.calculation_version,
        },
        reason=reason or note,
    )
    return RecipeCostSnapshot.objects.get(pk=snapshot.pk)
