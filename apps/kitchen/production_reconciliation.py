"""
Verifying production drafts. Report-only, always.

A third verifier rather than a third section of an existing one, because the
three answer different questions and a reader chasing one of them should not
have to read the other two: `reconciliation` checks the version lifecycle,
`cost_reconciliation` checks stored costing records, and this checks whether a
production draft still says what it was drafted to say.

## What it checks, and what it deliberately cannot

Everything here is answerable from the draft's own rows plus the recipe it
names. The interesting checks are the ones that catch a **snapshot that has
drifted from its source**: a requirement whose planned quantity no longer
equals `source × cumulative × multiplier` is a row somebody scaled by hand, and
a requirement whose stored path no longer matches the version's actual graph is
a row that will reconstruct the wrong tree in two years.

It checks **no stock**. Availability, lots and expiry are Task 3.5's, at
posting. A verifier that reported "not enough rice" would be reporting a fact
about Tuesday afternoon, not a defect in a document.

## No repair

Nothing here writes. There is no `--fix`, and the frozen columns refuse an
`UPDATE` at the database anyway (migration 0011). A draft that disagrees with
itself is evidence that something wrote it wrongly or reached behind a trigger,
and smoothing it over would erase the evidence that the question ever existed.
The answer is to discard the draft and draft again, which is one command and
leaves an audit trail.

Impossible states are planted only inside rolled-back tests, or handed to the
pure finding functions as in-memory copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apps.kitchen.expansion import expand_recipe_version
from apps.kitchen.models import (
    ActualLineKind,
    ProductionBatch,
    ProductionBatchLine,
    ProductionBatchStatus,
    RecipeLineSubstitute,
)
from apps.kitchen.production import scaled_expected_output, scaled_line_quantity
from apps.organizations.models import Organization

ZERO = Decimal("0")


@dataclass(frozen=True)
class DraftFinding:
    """
    One thing worth saying about one production draft.

    `code` is stable and machine-readable; `message` is the Arabic sentence an
    operator reads. Both, because a code with no sentence needs a lookup table
    nobody maintains and a sentence with no code cannot be counted.

    `is_blocking` separates a **defect** from an **observation**, and the
    distinction earns its keep the first time a kitchen substitutes across
    dimensions. That is a legitimate thing to do — RCP-022 approves items, not
    conversions — so reporting it as a defect would mean the verifier exits
    non-zero forever on a correct database. A red list nobody can clear stops
    being read within a week, and then the real defects go unread with it.
    """

    code: str
    organization_code: str
    batch_id: int
    recipe_code: str
    message: str
    line_order: int | None = None
    is_blocking: bool = True


def _finding(
    code: str,
    batch: ProductionBatch,
    message: str,
    line_order: int | None = None,
    *,
    is_blocking: bool = True,
) -> DraftFinding:
    return DraftFinding(
        code=code,
        organization_code=batch.organization.code,
        batch_id=batch.pk,
        recipe_code=batch.recipe.code,
        message=message,
        line_order=line_order,
        is_blocking=is_blocking,
    )


def verify_production_drafts(organization: Organization) -> list[DraftFinding]:
    """Every defect in one organization's production drafts."""
    findings: list[DraftFinding] = []
    batches = (
        ProductionBatch.objects.filter(organization=organization)
        .select_related("organization", "branch", "warehouse", "recipe", "recipe_version")
        .prefetch_related("lines__actuals", "lines__source_line")
        .order_by("pk")
    )
    for batch in batches:
        findings.extend(batch_findings(batch))
    return findings


def batch_findings(batch: ProductionBatch) -> list[DraftFinding]:
    """
    Everything checkable about one draft.

    Split out from `verify_production_drafts` so a screen can verify the one
    batch a reader is looking at without walking an organization, and so a test
    can hand it a hand-built in-memory batch without planting anything.
    """
    findings: list[DraftFinding] = []
    lines = list(batch.lines.all())

    # --- The document's own identities ------------------------------------
    if batch.branch.organization_id != batch.organization_id:
        findings.append(
            _finding(
                "production_branch_organization_disagrees",
                batch,
                "الفرع يتبع مؤسسة غير مؤسسة الدفعة.",
            )
        )
    if batch.warehouse.branch_id != batch.branch_id:
        findings.append(
            _finding(
                "production_warehouse_branch_disagrees",
                batch,
                "المخزن لا يتبع فرع الدفعة.",
            )
        )
    if batch.recipe.organization_id != batch.organization_id:
        findings.append(
            _finding(
                "production_recipe_organization_disagrees",
                batch,
                "الوصفة تتبع مؤسسة غير مؤسسة الدفعة.",
            )
        )
    if batch.recipe.output_item_id is None:
        findings.append(
            _finding(
                "production_recipe_shape_is_ineligible",
                batch,
                "الوصفة بلا صنف ناتج، فلا يمكن إنتاجها.",
            )
        )
    if batch.recipe_version.recipe_id != batch.recipe_id:
        findings.append(
            _finding(
                "production_version_is_not_of_this_recipe",
                batch,
                "النسخة المخزّنة لا تتبع وصفة الدفعة.",
            )
        )
    if batch.expected_output_quantity <= ZERO:
        findings.append(
            _finding(
                "production_expected_output_is_not_positive",
                batch,
                "الناتج المتوقع غير موجب.",
            )
        )
    if batch.multiplier <= ZERO:
        findings.append(
            _finding(
                "production_multiplier_is_not_positive",
                batch,
                "معامل الدفعة غير موجب.",
            )
        )

    # The header half of the invariant migration 0015 enforces at COMMIT. The
    # trigger refuses a *new* disagreement; this reports one that got in before
    # the trigger existed, or through a database restored from elsewhere.
    required_output = scaled_expected_output(
        version_output=batch.recipe_version.expected_output_quantity,
        multiplier=batch.multiplier,
    )
    if batch.expected_output_quantity != required_output:
        findings.append(
            _finding(
                "production_expected_output_disagrees",
                batch,
                f"الناتج المتوقع {batch.expected_output_quantity} لا يساوي ناتج النسخة "
                f"× معامل الدفعة ({required_output}).",
            )
        )

    # --- Nothing Task 3.5 owns may exist yet ------------------------------
    if batch.status != ProductionBatchStatus.DRAFT:
        findings.append(
            _finding(
                "production_batch_is_not_draft",
                batch,
                f"الحالة {batch.status} خارج حدود المهمة 3.4.",
            )
        )
    if batch.number:
        findings.append(
            _finding(
                "production_draft_carries_a_number",
                batch,
                "مسودة تحمل رقم مستند، والترقيم يحدث عند الترحيل.",
            )
        )

    # --- The requirements still match the recipe they claim ---------------
    #
    # Re-expanding is the check that matters: a stored path that no longer
    # appears in the version's own graph is a row that will reconstruct the
    # wrong tree in two years, and nothing else here would notice.
    try:
        leaves = expand_recipe_version(batch.recipe_version)
    except Exception:  # noqa: BLE001 - a corrupt graph is itself the finding
        findings.append(
            _finding(
                "production_source_graph_is_corrupt",
                batch,
                "لا يمكن إعادة توسيع نسخة الوصفة — الرسم معطوب.",
            )
        )
        leaves = []

    expected = {(leaf.path_display, leaf.line.pk): leaf for leaf in leaves}
    seen: set[tuple[str, int]] = set()

    for line in lines:
        key = (line.component_path, line.source_line_id)
        if key in seen:
            findings.append(
                _finding(
                    "production_duplicate_source_path",
                    batch,
                    f"المسار {line.component_path or '—'} مكرر.",
                    line.line_order,
                )
            )
        seen.add(key)

        leaf = expected.get(key)
        if leaves and leaf is None:
            findings.append(
                _finding(
                    "production_source_path_no_longer_matches",
                    batch,
                    f"السطر {line.line_order}: المسار المخزّن لم يعد موجوداً في النسخة.",
                    line.line_order,
                )
            )
        elif leaf is not None:
            if leaf.line.item_id != line.item_id:
                findings.append(
                    _finding(
                        "production_source_item_disagrees",
                        batch,
                        f"السطر {line.line_order}: الصنف لا يطابق سطر الوصفة.",
                        line.line_order,
                    )
                )
            if leaf.cumulative_multiplier != line.cumulative_multiplier:
                findings.append(
                    _finding(
                        "production_cumulative_multiplier_disagrees",
                        batch,
                        f"السطر {line.line_order}: المعامل التراكمي لا يطابق الرسم.",
                        line.line_order,
                    )
                )

        # Through the same function the services and the trigger use. A verifier
        # with its own copy of the arithmetic would agree with them until
        # somebody fixed one of the two, and then report every draft as broken.
        planned = scaled_line_quantity(
            source_base_quantity=line.source_base_quantity,
            cumulative_multiplier=line.cumulative_multiplier,
            multiplier=batch.multiplier,
        )
        if planned != line.planned_base_quantity:
            findings.append(
                _finding(
                    "production_planned_quantity_disagrees",
                    batch,
                    f"السطر {line.line_order}: الكمية المخططة {line.planned_base_quantity} "
                    f"لا تساوي الأساس × المعامل × معامل الدفعة ({planned}).",
                    line.line_order,
                )
            )
        if line.source_line.version_id != line.source_version_id:
            findings.append(
                _finding(
                    "production_source_line_version_disagrees",
                    batch,
                    f"السطر {line.line_order}: سطر الوصفة لا يتبع النسخة المصدر.",
                    line.line_order,
                )
            )

    # A non-stocked component whose leaves were dropped, or a stocked one that
    # was expanded, both show up here: the two sets simply differ.
    if leaves and len(seen) != len(expected):
        findings.append(
            _finding(
                "production_expansion_is_incomplete",
                batch,
                f"عدد المتطلبات {len(seen)} لا يساوي عدد أوراق النسخة {len(expected)}.",
            )
        )

    findings.extend(_actual_findings(batch, lines))
    findings.extend(_task_3_5_link_findings(batch))
    return findings


def _task_3_5_link_findings(batch: ProductionBatch) -> list[DraftFinding]:
    """
    Whether anything Task 3.5 owns has attached itself to this draft.

    Structural rather than field-based, because Task 3.4 deliberately added no
    column linking a batch to a movement or a journal — so the only way such a
    link can exist is through the generic source identity every posted document
    carries (ADR-017). Looking there is what makes "zero Inventory, zero GL" a
    thing the verifier can *check* rather than a thing the schema merely happens
    not to offer today.

    A finding here is serious: it means something posted against a draft.
    """
    from apps.accounting.models import JournalEntry
    from apps.core.models import AuditEvent

    findings: list[DraftFinding] = []
    identity = "kitchen.ProductionBatch"

    journals = JournalEntry.objects.filter(
        source_document_type=identity, source_document_id=str(batch.pk)
    ).count()
    if journals:
        findings.append(
            _finding(
                "production_draft_has_a_journal_link",
                batch,
                f"مسودة مرتبطة بـ {journals} قيد محاسبي، والترحيل ليس ضمن المهمة 3.4.",
            )
        )

    # Audit events are expected and correct; a *posting* audit event is not.
    posted = AuditEvent.objects.filter(
        target_type=identity, target_id=str(batch.pk), action__in=["POSTED", "REVERSED"]
    ).count()
    if posted:
        findings.append(
            _finding(
                "production_draft_has_a_posting_event",
                batch,
                f"مسودة تحمل {posted} حدث ترحيل أو عكس.",
            )
        )
    return findings


def _actual_findings(
    batch: ProductionBatch, lines: list[ProductionBatchLine]
) -> list[DraftFinding]:
    """Whether every recorded consumption is one somebody approved."""
    findings: list[DraftFinding] = []
    for line in lines:
        rows = list(line.actuals.all())

        # A requirement nobody answered. Not the same as a requirement answered
        # with zero: the second is a fact about the batch, the first is a gap.
        if not rows:
            findings.append(
                _finding(
                    "production_requirement_has_no_actual",
                    batch,
                    f"السطر {line.line_order}: لا يوجد سطر استهلاك فعلي.",
                    line.line_order,
                )
            )

        # Two rows for one item are one quantity written twice, and the second
        # is a correction masquerading as a second consumption. Refused by
        # `production_actual_item_unique_per_line`; reported here for a database
        # that predates it or was restored from one.
        counted: dict[int, int] = {}
        for row in rows:
            counted[row.item_id] = counted.get(row.item_id, 0) + 1
        for item_id, count in counted.items():
            if count > 1:
                findings.append(
                    _finding(
                        "production_duplicate_actual_item",
                        batch,
                        f"السطر {line.line_order}: الصنف {item_id} مسجّل في {count} سطور.",
                        line.line_order,
                    )
                )

        # Rows in more than one dimension. **Not a defect** — an approved
        # stand-in may legitimately be measured in another dimension — but the
        # reader must be told, because the plan-versus-actual figure for this
        # requirement is not one number and anything that printed it as one
        # would have added litres to kilograms.
        dimensions = {row.item.base_unit.dimension for row in rows}
        if len(dimensions) > 1:
            findings.append(
                _finding(
                    "production_actual_rows_span_dimensions",
                    batch,
                    f"السطر {line.line_order}: سطور الاستهلاك بأبعاد قياس مختلفة "
                    f"({', '.join(sorted(dimensions))}) — تُقرأ منفصلة ولا تُجمع.",
                    line.line_order,
                    is_blocking=False,
                )
            )

        for row in rows:
            if row.base_quantity < ZERO:
                findings.append(
                    _finding(
                        "production_actual_quantity_is_negative",
                        batch,
                        f"السطر {line.line_order}: كمية فعلية سالبة.",
                        line.line_order,
                    )
                )
            if (row.entered_unit_id is None) == (row.package_unit_id is None):
                findings.append(
                    _finding(
                        "production_actual_conversion_incomplete",
                        batch,
                        f"السطر {line.line_order}: طريقة الإدخال غير مكتملة.",
                        line.line_order,
                    )
                )
            if row.package_unit_id is not None and row.conversion_factor is None:
                findings.append(
                    _finding(
                        "production_actual_conversion_incomplete",
                        batch,
                        f"السطر {line.line_order}: معامل تحويل العبوة ناقص.",
                        line.line_order,
                    )
                )
            if row.kind == ActualLineKind.PRIMARY and row.item_id != line.item_id:
                findings.append(
                    _finding(
                        "production_actual_item_not_approved",
                        batch,
                        f"السطر {line.line_order}: سطر أصلي بصنف مختلف عن المتطلب.",
                        line.line_order,
                    )
                )
            if row.kind == ActualLineKind.SUBSTITUTE:
                approval = row.substitute
                if approval is None:
                    findings.append(
                        _finding(
                            "production_actual_item_not_approved",
                            batch,
                            f"السطر {line.line_order}: بديل بلا اعتماد مسجّل.",
                            line.line_order,
                        )
                    )
                elif approval.line_id != line.source_line_id:
                    findings.append(
                        _finding(
                            "production_substitute_belongs_to_another_line",
                            batch,
                            f"السطر {line.line_order}: البديل معتمد لسطر وصفة آخر.",
                            line.line_order,
                        )
                    )
                elif not RecipeLineSubstitute.objects.filter(
                    pk=approval.pk, substitute_item_id=row.item_id
                ).exists():
                    findings.append(
                        _finding(
                            "production_substitute_item_disagrees",
                            batch,
                            f"السطر {line.line_order}: الصنف لا يطابق البديل المعتمد.",
                            line.line_order,
                        )
                    )
            if row.measured_base_quantity is None and row.package_unit_id is not None:
                # A VARIABLE package with no measurement. Fixed packages carry
                # a factor and need none, so this is only a finding when the
                # snapshot says the package had no arithmetic answer.
                if row.conversion_version is None:
                    findings.append(
                        _finding(
                            "production_variable_package_has_no_measurement",
                            batch,
                            f"السطر {line.line_order}: عبوة متغيرة بلا وزن مقاس.",
                            line.line_order,
                        )
                    )
    return findings


def drafts_checked(organization: Organization) -> int:
    """How many drafts the verifier walked — the denominator for its report."""
    return ProductionBatch.objects.filter(organization=organization).count()
