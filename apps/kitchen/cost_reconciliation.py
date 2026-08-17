"""
Verifying cost snapshots. Report-only, always.

Kept out of `apps/kitchen/reconciliation.py` because the two answer different
questions and their findings would be unreadable interleaved: that module checks
whether the *version lifecycle* is coherent, this one checks whether a *stored
costing record* still says what it said when it was written.

## The one comparison this must never make

**A snapshot is not checked against today's inventory.** Stock moved; that is
what stock does. A March snapshot whose items are worth more in September is
correct in every particular, and a verifier that called the difference a defect
would produce a red list nobody could act on and therefore nobody would read.

What is checked instead is **internal coherence** — that the row still agrees
with itself — plus a small number of relationships to the recipe structure that
cannot legitimately change. Every check below is answerable from the snapshot's
own columns, and that is the design rather than a limitation.

Exact recomputation from the recorded cutoff is a different and heavier job:
it re-reads the ledger at `ledger_cutoff_sequence` and re-derives every unit
cost. It is available as an explicit second mode — `recompute_findings` — and
never runs by default, because it is only meaningful while the movements behind
that cutoff are still present and it costs a query per item.

## No repair

Nothing here writes. There is no `--fix`, no `--repair` and no reconciliation
side effect, and the snapshot tables refuse UPDATE and DELETE at the database
anyway (migration 0009). A verifier that could change a figure it is verifying
would be the one place a discrepancy could be made to disappear.

Impossible states are planted only inside rolled-back tests, never seeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_calculation
from apps.inventory.reports import ReportMode
from apps.inventory.valuation import ValuationCutoff, valuation_at_cutoff
from apps.kitchen.costing import CALCULATION_VERSION, COSTABLE_VERSION_STATUSES
from apps.kitchen.models import (
    CostValuationMode,
    RecipeCostSnapshot,
    RecipeLineCostClass,
    ServingAllocationOutcome,
)
from apps.organizations.models import Organization

ZERO = Decimal("0")


@dataclass(frozen=True)
class CostFinding:
    """
    One thing wrong with one stored snapshot.

    `code` is stable and machine-readable; `message` is the Arabic sentence an
    operator reads. Both, because a code with no sentence needs a lookup table
    nobody maintains and a sentence with no code cannot be counted.
    """

    code: str
    organization_code: str
    snapshot_id: int
    recipe_code: str
    version: str
    message: str


def _finding(code: str, snapshot: RecipeCostSnapshot, message: str) -> CostFinding:
    return CostFinding(
        code=code,
        organization_code=snapshot.organization.code,
        snapshot_id=snapshot.pk,
        recipe_code=snapshot.recipe_code,
        version=f"v{snapshot.version_number}",
        message=message,
    )


def verify_cost_snapshots(organization: Organization) -> list[CostFinding]:
    """Every internal-coherence defect in one organization's snapshots."""
    findings: list[CostFinding] = []
    snapshots = (
        RecipeCostSnapshot.objects.filter(organization=organization)
        .select_related("organization", "recipe", "version", "branch", "warehouse")
        .prefetch_related("lines", "servings")
        .order_by("pk")
    )
    for snapshot in snapshots:
        findings.extend(snapshot_findings(snapshot))
    return findings


def snapshot_findings(snapshot: RecipeCostSnapshot) -> list[CostFinding]:
    """
    Everything checkable about one snapshot from its own columns.

    Split out from `verify_cost_snapshots` so a screen can verify the one
    snapshot a reader is looking at without walking an organization.
    """
    findings: list[CostFinding] = []
    lines = list(snapshot.lines.all())
    servings = list(snapshot.servings.all())

    # --- The document total is the sum of its lines (CLAUDE.md, ADR-012) ----
    line_total = sum((line.allocated_extension for line in lines), ZERO)
    if quantize_money(line_total) != snapshot.total_material_cost:
        findings.append(
            _finding(
                "cost_snapshot_lines_do_not_sum_to_total",
                snapshot,
                f"مجموع الأسطر {line_total} لا يساوي إجمالي اللقطة {snapshot.total_material_cost}.",
            )
        )

    # --- The class split is the same total, cut three ways (RCP-092) --------
    by_class: dict[str, Decimal] = dict.fromkeys(RecipeLineCostClass.values, ZERO)
    for line in lines:
        by_class[line.cost_class] = by_class.get(line.cost_class, ZERO) + line.allocated_extension
    stored = {
        RecipeLineCostClass.FOOD: snapshot.food_total,
        RecipeLineCostClass.PACKAGING: snapshot.packaging_total,
        RecipeLineCostClass.ACCOMPANIMENT: snapshot.accompaniment_total,
    }
    for cost_class, amount in stored.items():
        if quantize_money(by_class.get(cost_class, ZERO)) != amount:
            findings.append(
                _finding(
                    "cost_snapshot_class_total_disagrees_with_lines",
                    snapshot,
                    f"إجمالي فئة {cost_class} المخزّن {amount} لا يساوي مجموع أسطرها "
                    f"{by_class.get(cost_class, ZERO)}.",
                )
            )
    if sum(stored.values(), ZERO) != snapshot.total_material_cost:
        findings.append(
            _finding(
                "cost_snapshot_class_totals_do_not_sum_to_total",
                snapshot,
                "مجموع إجماليات الفئات لا يساوي إجمالي كلفة المواد.",
            )
        )

    # --- The line order is 1..n with no gaps and no duplicate path ---------
    numbers = sorted(line.line_number for line in lines)
    if numbers != list(range(1, len(lines) + 1)):
        findings.append(
            _finding(
                "cost_snapshot_line_numbers_have_gaps",
                snapshot,
                f"ترقيم الأسطر غير متصل: {numbers}.",
            )
        )
    paths = [(line.component_path, line.recipe_line_public_id) for line in lines]
    if len(set(paths)) != len(paths):
        findings.append(
            _finding(
                "cost_snapshot_duplicate_path",
                snapshot,
                "المسار نفسه مكرر في أكثر من سطر، فالبطاقة غير قابلة للتتبع.",
            )
        )

    # --- Every stored figure is internally consistent ----------------------
    for line in lines:
        expected_raw = line.effective_quantity * line.unit_cost
        if line.raw_extension != expected_raw:
            findings.append(
                _finding(
                    "cost_snapshot_line_extension_disagrees",
                    snapshot,
                    f"السطر {line.line_number}: الامتداد المخزّن {line.raw_extension} "
                    f"لا يساوي الكمية × كلفة الوحدة ({expected_raw}).",
                )
            )
        if line.valuation_quantity > ZERO:
            derived = quantize_unit_price(line.valuation_value / line.valuation_quantity)
            if derived != line.unit_cost:
                findings.append(
                    _finding(
                        "cost_snapshot_unit_cost_disagrees_with_valuation",
                        snapshot,
                        f"السطر {line.line_number}: كلفة الوحدة {line.unit_cost} "
                        f"لا تساوي القيمة ÷ الكمية ({derived}).",
                    )
                )
        elif line.unit_cost != ZERO:
            findings.append(
                _finding(
                    "cost_snapshot_incomplete_valuation_evidence",
                    snapshot,
                    f"السطر {line.line_number}: كلفة وحدة بلا رصيد مخزني يفسّرها.",
                )
            )

    # --- The identities the snapshot claims still hold ---------------------
    if snapshot.recipe.organization_id != snapshot.organization_id:
        findings.append(
            _finding(
                "cost_snapshot_source_organization_disagrees",
                snapshot,
                "الوصفة تتبع مؤسسة غير مؤسسة اللقطة.",
            )
        )
    if snapshot.version.recipe_id != snapshot.recipe_id:
        findings.append(
            _finding(
                "cost_snapshot_version_is_not_of_this_recipe",
                snapshot,
                "النسخة المخزّنة لا تتبع وصفة اللقطة.",
            )
        )
    if snapshot.warehouse.branch_id != snapshot.branch_id:
        findings.append(
            _finding(
                "cost_snapshot_warehouse_branch_disagrees",
                snapshot,
                "المخزن لا يتبع فرع اللقطة.",
            )
        )
    if snapshot.branch.organization_id != snapshot.organization_id:
        findings.append(
            _finding(
                "cost_snapshot_branch_organization_disagrees",
                snapshot,
                "الفرع يتبع مؤسسة غير مؤسسة اللقطة.",
            )
        )
    for line in lines:
        if line.recipe_line.version_id != line.source_version_id:
            findings.append(
                _finding(
                    "cost_snapshot_line_source_version_disagrees",
                    snapshot,
                    f"السطر {line.line_number}: سطر الوصفة لا يتبع النسخة المصدر المخزّنة.",
                )
            )

    # --- Authority and calculation version ---------------------------------
    if snapshot.version_status not in COSTABLE_VERSION_STATUSES:
        findings.append(
            _finding(
                "cost_snapshot_built_from_non_authoritative_version",
                snapshot,
                f"اللقطة مبنية على نسخة بحالة {snapshot.version_status}.",
            )
        )
    if snapshot.calculation_version != CALCULATION_VERSION:
        findings.append(
            _finding(
                "cost_snapshot_unsupported_calculation_version",
                snapshot,
                f"إصدار الحساب {snapshot.calculation_version} غير مدعوم من هذا التحقق.",
            )
        )
    if snapshot.valuation_mode != CostValuationMode.POSTED_AS_OF:
        findings.append(
            _finding(
                "cost_snapshot_unsupported_valuation_mode",
                snapshot,
                f"وضع التقييم {snapshot.valuation_mode} ليس الوضع المعتمد للكلفة.",
            )
        )

    # --- The serving scenarios each allocate the whole total (RCP-087) ------
    #
    # Three checks, and the first is the one the compact representation exists
    # to survive: five stored numbers must still add up to the distribution
    # they claim to describe. A summary that stopped reconstructing would be
    # unverifiable, and unverifiable is the one thing a costing record may not
    # be.
    for serving in servings:
        if serving.allocation_state != ServingAllocationOutcome.ALLOCATED:
            continue
        if serving.reconstructs_to() != serving.allocated_total:
            findings.append(
                _finding(
                    "cost_snapshot_serving_summary_does_not_reconstruct",
                    snapshot,
                    f"سيناريو الحصة {serving.code}: ملخّص التوزيع "
                    f"({serving.normal_serving_count}×{serving.minimum_allocated} + "
                    f"{serving.elevated_serving_count}×{serving.maximum_allocated} + "
                    f"{serving.remainder_cost}) لا يعيد بناء "
                    f"{serving.allocated_total}.",
                )
            )
        if serving.allocated_total != snapshot.total_material_cost:
            findings.append(
                _finding(
                    "cost_snapshot_serving_allocation_disagrees_with_total",
                    snapshot,
                    f"سيناريو الحصة {serving.code}: مجموع التوزيع "
                    f"{serving.allocated_total} لا يساوي إجمالي اللقطة "
                    f"{snapshot.total_material_cost}.",
                )
            )
        if serving.maximum_allocated - serving.minimum_allocated > quantize_money("0.001"):
            findings.append(
                _finding(
                    "cost_snapshot_serving_allocation_is_uneven",
                    snapshot,
                    f"سيناريو الحصة {serving.code}: الفارق بين أعلى وأدنى حصة يتجاوز فلساً.",
                )
            )

    # --- The plate basis is present and is one of this snapshot's servings ---
    if not snapshot.primary_serving_code:
        findings.append(
            _finding(
                "cost_snapshot_has_no_plate_basis",
                snapshot,
                "اللقطة بلا حصة أساسية تفسّر كلفة الطبق.",
            )
        )
    else:
        primary = [row for row in servings if row.is_primary]
        if len(primary) != 1 or primary[0].code != snapshot.primary_serving_code:
            findings.append(
                _finding(
                    "cost_snapshot_plate_basis_is_not_its_primary_serving",
                    snapshot,
                    f"أساس كلفة الطبق {snapshot.primary_serving_code} لا يطابق "
                    "الحصة الأساسية المخزّنة.",
                )
            )
        else:
            expected = quantize_unit_price(
                snapshot.total_material_cost * primary[0].factor_of_batch
            )
            if expected != snapshot.plate_cost:
                findings.append(
                    _finding(
                        "cost_snapshot_plate_cost_disagrees_with_its_basis",
                        snapshot,
                        f"كلفة الطبق {snapshot.plate_cost} لا تساوي الإجمالي × معامل "
                        f"الحصة الأساسية ({expected}).",
                    )
                )
            if primary[0].base_quantity > Decimal("0"):
                portions = quantize_calculation(snapshot.output_quantity / primary[0].base_quantity)
                if portions != snapshot.portions_per_batch:
                    findings.append(
                        _finding(
                            "cost_snapshot_portions_disagree_with_its_basis",
                            snapshot,
                            f"عدد الأطباق {snapshot.portions_per_batch} لا يساوي الناتج ÷ "
                            f"كمية الحصة الأساسية ({portions}).",
                        )
                    )

    # --- Idempotency evidence ----------------------------------------------
    if not snapshot.idempotency_key or not snapshot.request_fingerprint:
        findings.append(
            _finding(
                "cost_snapshot_idempotency_evidence_missing",
                snapshot,
                "اللقطة بلا مفتاح تكرار أو بصمة طلب.",
            )
        )
    elif _stored_fingerprint(snapshot) != snapshot.request_fingerprint:
        # Recomputed from the stored request inputs, never from the figures. A
        # fingerprint that no longer matches its own row means the columns it
        # was taken over were changed behind the trigger's back.
        findings.append(
            _finding(
                "cost_snapshot_fingerprint_does_not_match_request",
                snapshot,
                "بصمة الطلب لا تطابق بيانات اللقطة المخزّنة.",
            )
        )

    return findings


def _stored_fingerprint(snapshot: RecipeCostSnapshot) -> str:
    """
    Rebuild the fingerprint from the snapshot's own stored request inputs.

    Mirrors `snapshots.snapshot_fingerprint` exactly, which is a duplication
    with a purpose: this one reads *columns*, that one reads a live card, and a
    verifier that called the writer's helper with the writer's objects would
    prove only that the helper is deterministic.
    """
    import hashlib
    import json

    payload = {
        "command": "create_recipe_cost_snapshot",
        "version": str(snapshot.version.public_id),
        "warehouse": snapshot.warehouse_id,
        "as_of_date": snapshot.as_of_date.isoformat(),
        "valuation_mode": str(ReportMode.POSTED_AS_OF),
        "calculation_version": snapshot.calculation_version,
        "reference": snapshot.reference,
        "reason": snapshot.reason,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def recompute_findings(snapshot: RecipeCostSnapshot) -> list[CostFinding]:
    """
    The **explicit** second mode: re-read the ledger at the recorded cutoff.

    Off by default and never part of `verify_cost_snapshots`, because it is a
    query per item and it is only meaningful while the movements behind the
    cutoff are still present. What it proves is stronger than internal
    coherence: that the unit costs stored on the lines are the ones the ledger
    actually held at `ledger_cutoff_sequence`.

    Still report-only. Still no repair. And still not a comparison against
    today — the cutoff is the snapshot's own, so a later posting changes
    nothing about what this returns.
    """
    lines = list(snapshot.lines.select_related("item").all())
    if not lines:
        return []
    cutoff = ValuationCutoff(
        organization_id=snapshot.organization_id,
        as_of_date=snapshot.as_of_date,
        posted_sequence=snapshot.ledger_cutoff_sequence,
    )
    valuations = valuation_at_cutoff(
        warehouse=snapshot.warehouse,
        item_ids=[line.item_id for line in lines],
        cutoff=cutoff,
    )
    findings: list[CostFinding] = []
    for line in lines:
        valuation = valuations[line.item_id]
        if not valuation.is_available:
            findings.append(
                _finding(
                    "cost_snapshot_recompute_valuation_unavailable",
                    snapshot,
                    f"السطر {line.line_number}: لا يوجد تقييم لهذا الصنف عند نقطة القطع "
                    f"{snapshot.ledger_cutoff_sequence}.",
                )
            )
            continue
        if valuation.unit_cost != line.unit_cost:
            findings.append(
                _finding(
                    "cost_snapshot_recompute_unit_cost_differs",
                    snapshot,
                    f"السطر {line.line_number}: كلفة الوحدة المخزّنة {line.unit_cost} "
                    f"وإعادة الحساب عند نقطة القطع تعطي {valuation.unit_cost}.",
                )
            )
        if quantize_calculation(valuation.quantity) != quantize_calculation(
            line.valuation_quantity
        ):
            findings.append(
                _finding(
                    "cost_snapshot_recompute_quantity_differs",
                    snapshot,
                    f"السطر {line.line_number}: الرصيد المخزّن {line.valuation_quantity} "
                    f"وإعادة الحساب تعطي {valuation.quantity}.",
                )
            )
    return findings


def snapshots_checked(organization: Organization) -> int:
    """How many snapshots the verifier walked — the denominator for its report."""
    return RecipeCostSnapshot.objects.filter(organization=organization).count()
