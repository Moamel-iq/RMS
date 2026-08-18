"""
What a **posted** production batch must be able to prove, checked and reported.

`production_reconciliation.py` verifies drafts — what still has to be entered.
This verifies postings — what already happened, and whether the three records
of it agree: the batch, the stock ledger, and the general ledger.

It **composes** the draft verifier rather than replacing it. A posted batch is
still a batch, and the structural claims about its requirements and consumption
rows stay true after posting; only the readiness ones stop applying, and those
are filtered rather than re-implemented.

## The proof that matters most

A journal that is **rightly** absent and one that is **wrongly** missing look
identical from the outside. Both are a batch with `journal_entry_id IS NULL`.
So `no_journal_is_unexplained` does not read that column and conclude anything:
it recomputes the per-account nets from the movements the posting actually
wrote and asserts each is exactly zero (RCP-112 proof 5). That is the only
check that can tell the two apart, and it is why the legitimate silence is safe
to allow at all.

## Reports, never repairs

Every function here reads. Nothing writes, nothing corrects, and there is no
repair mode — a verifier that could change a figure it verifies is the one
place a discrepancy could be made to disappear.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from apps.accounting.models import JournalEntry
from apps.core.money import quantize_money
from apps.inventory.models import MovementType, StockMovement
from apps.kitchen.models import (
    ProductionBatch,
    ProductionBatchStatus,
)
from apps.kitchen.production_posting import SOURCE_DOCUMENT_TYPE
from apps.kitchen.production_reconciliation import (  # noqa: PLC2701
    DraftFinding,
    _finding,
    verify_production_drafts,
)

if TYPE_CHECKING:
    from apps.organizations.models import Organization

ZERO = Decimal("0")

#: Draft findings that stop meaning anything once a batch is posted. A posted
#: batch is by definition ready — it was checked at posting — so re-reporting
#: readiness against it would fill the list with noise that can never be
#: cleared.
_DRAFT_ONLY_CODES = frozenset(
    {
        "production_batch_has_no_actual",
        "production_requirement_has_no_actual",
        "production_output_not_recorded",
    }
)


def verify_production(organization: Organization) -> list[DraftFinding]:
    """
    Drafts and postings together, which is what a reader actually wants.

    Composed rather than merged: the draft verifier still owns every structural
    claim about requirements and consumption rows, and those claims stay true
    after posting. What stops applying is **readiness** — a posted batch was
    checked at posting and cannot become unready afterwards — so those codes are
    filtered out for non-draft batches rather than re-implemented with a
    condition bolted on.
    """
    drafts = {
        batch.pk: batch.status
        for batch in ProductionBatch.objects.filter(organization=organization).only("pk", "status")
    }
    findings = [
        finding
        for finding in verify_production_drafts(organization)
        if drafts.get(finding.batch_id) == ProductionBatchStatus.DRAFT
        or finding.code not in _DRAFT_ONLY_CODES
    ]
    findings.extend(verify_posted_production(organization))
    return findings


def verify_posted_production(organization: Organization) -> list[DraftFinding]:
    """Every disagreement between a posted batch and the ledgers it wrote."""
    findings: list[DraftFinding] = []
    batches = (
        ProductionBatch.objects.filter(organization=organization)
        .exclude(status=ProductionBatchStatus.DRAFT)
        .select_related(
            "organization",
            "branch",
            "warehouse",
            "recipe",
            "recipe_version",
            "stock_entry",
            "journal_entry",
            "output_item",
            "output_lot",
            "output_movement",
            "reversal_stock_entry",
        )
        .prefetch_related("lines__actuals__allocations")
        .order_by("pk")
    )
    for batch in batches:
        findings.extend(posted_batch_findings(batch))
    return findings


def posted_batch_findings(batch: ProductionBatch) -> list[DraftFinding]:
    """Everything one posted batch is asked to prove, reported together."""
    findings: list[DraftFinding] = []
    findings.extend(_evidence_findings(batch))
    entry = batch.stock_entry
    if entry is None:
        # Nothing below can be checked without the posting it is checked
        # against, and saying so once beats twenty consequential findings.
        return findings

    movements = list(entry.movements.select_related("item", "lot", "control_account").all())
    findings.extend(_movement_findings(batch, movements))
    findings.extend(_output_findings(batch, movements))
    findings.extend(_journal_findings(batch, movements))
    findings.extend(_identity_findings(batch))
    findings.extend(_reversal_findings(batch))
    return findings


# ---------------------------------------------------------------------------
# The header
# ---------------------------------------------------------------------------


def _evidence_findings(batch: ProductionBatch) -> list[DraftFinding]:
    """Posting evidence, all present or the row is a half-posted document."""
    found: list[DraftFinding] = []
    if not batch.number:
        found.append(_finding("posted_batch_has_no_number", batch, _("دفعة مرحّلة بلا رقم مستند.")))
    if batch.posted_at is None or batch.posted_by_id is None:
        found.append(
            _finding(
                "posted_batch_has_no_actor",
                batch,
                _("دفعة مرحّلة بلا وقت ترحيل أو بلا من رحّلها."),
            )
        )
    if batch.stock_entry_id is None:
        found.append(
            _finding("posted_batch_has_no_stock_entry", batch, _("دفعة مرحّلة بلا ترحيل مخزني."))
        )
    if batch.input_value is None or batch.output_value is None:
        found.append(_finding("posted_batch_has_no_value", batch, _("دفعة مرحّلة بلا قيمة مسجلة.")))
    elif batch.input_value != batch.output_value:
        found.append(
            _finding(
                "posted_batch_value_not_conserved",
                batch,
                _("قيمة المدخلات لا تساوي قيمة الناتج."),
            )
        )
    if batch.output_item_id is None:
        found.append(
            _finding("posted_batch_has_no_output_item", batch, _("دفعة مرحّلة بلا صنف ناتج."))
        )
    return found


# ---------------------------------------------------------------------------
# The stock ledger
# ---------------------------------------------------------------------------


def _movement_findings(
    batch: ProductionBatch, movements: list[StockMovement]
) -> list[DraftFinding]:
    """Every positive consumption exactly once, and nothing extra."""
    found: list[DraftFinding] = []
    by_key = {movement.effect_key: movement for movement in movements}
    consumed_total = ZERO

    for line in batch.lines.all():
        for actual in line.actuals.all():
            allocations = list(actual.allocations.all())
            positive = actual.base_quantity > ZERO

            if not positive:
                # A zero row creates no movement. One that did would be a
                # movement saying nothing, reconciling against nothing.
                if allocations or f"production-actual:{actual.public_id}" in by_key:
                    found.append(
                        _finding(
                            "posted_zero_actual_moved_stock",
                            batch,
                            _("سطر استهلاك بكمية صفر أنتج حركة مخزنية."),
                            line.line_order,
                        )
                    )
                continue

            keys = (
                [f"production-allocation:{row.public_id}" for row in allocations]
                if allocations
                else [f"production-actual:{actual.public_id}"]
            )
            missing = [key for key in keys if key not in by_key]
            if missing:
                found.append(
                    _finding(
                        "posted_actual_has_no_movement",
                        batch,
                        _("سطر استهلاك موجب بلا حركة صرف للإنتاج."),
                        line.line_order,
                    )
                )
                continue

            if allocations:
                allocated = quantize_money(sum((row.base_quantity for row in allocations), ZERO))
                if allocated != quantize_money(actual.base_quantity):
                    found.append(
                        _finding(
                            "posted_allocation_total_mismatch",
                            batch,
                            _("مجموع التخصيصات لا يساوي الكمية المستهلكة."),
                            line.line_order,
                        )
                    )

            for row in allocations:
                movement = by_key[f"production-allocation:{row.public_id}"]
                if movement.lot_id != row.lot_id:
                    found.append(
                        _finding(
                            "posted_allocation_lot_mismatch",
                            batch,
                            _("لوط التخصيص لا يطابق لوط الحركة."),
                            line.line_order,
                        )
                    )
                if abs(movement.base_quantity) != row.base_quantity:
                    found.append(
                        _finding(
                            "posted_allocation_quantity_mismatch",
                            batch,
                            _("كمية التخصيص لا تطابق كمية الحركة."),
                            line.line_order,
                        )
                    )
                if movement.item_id != actual.item_id:
                    found.append(
                        _finding(
                            "posted_movement_item_mismatch",
                            batch,
                            _("صنف الحركة لا يطابق صنف الاستهلاك."),
                            line.line_order,
                        )
                    )

            for key in keys:
                movement = by_key[key]
                if movement.movement_type != MovementType.PRODUCTION_OUT:
                    found.append(
                        _finding(
                            "posted_consumption_is_not_production_out",
                            batch,
                            _("حركة الاستهلاك ليست من نوع صرف للإنتاج."),
                            line.line_order,
                        )
                    )
                consumed_total += -movement.inventory_value

    if batch.input_value is not None and quantize_money(consumed_total) != batch.input_value:
        found.append(
            _finding(
                "posted_consumption_value_mismatch",
                batch,
                _("مجموع قيم الحركات لا يساوي قيمة المدخلات المسجلة."),
            )
        )
    return found


def _output_findings(batch: ProductionBatch, movements: list[StockMovement]) -> list[DraftFinding]:
    """One `PRODUCTION_IN`, for the entered quantity, at the consumed value."""
    found: list[DraftFinding] = []
    inbound = [
        movement for movement in movements if movement.movement_type == MovementType.PRODUCTION_IN
    ]
    if len(inbound) != 1:
        found.append(
            _finding(
                "posted_batch_output_movement_count",
                batch,
                _("دفعة مرحّلة يجب أن تحمل حركة إنتاج واردة واحدة بالضبط."),
            )
        )
        return found

    movement = inbound[0]
    if batch.output_item_id is not None and movement.item_id != batch.output_item_id:
        found.append(
            _finding("posted_output_item_mismatch", batch, _("صنف الحركة الواردة ليس صنف الناتج."))
        )
    if (
        batch.actual_output_base_quantity is not None
        and movement.base_quantity != batch.actual_output_base_quantity
    ):
        found.append(
            _finding(
                "posted_output_quantity_mismatch",
                batch,
                _("كمية الحركة الواردة لا تساوي الناتج الفعلي المسجل."),
            )
        )
    if batch.output_value is not None and movement.inventory_value != batch.output_value:
        found.append(
            _finding(
                "posted_output_value_mismatch",
                batch,
                _("قيمة الحركة الواردة لا تساوي قيمة الناتج المسجلة."),
            )
        )

    # The lot, and the provenance Phase 1 reserved the columns for.
    item = batch.output_item
    if item is not None and item.tracks_lots:
        if batch.output_lot_id is None:
            found.append(
                _finding(
                    "posted_output_lot_missing",
                    batch,
                    _("الصنف الناتج يتتبع اللوطات ولا يوجد لوط للدفعة."),
                )
            )
        else:
            lot = batch.output_lot
            assert lot is not None  # noqa: S101 - guarded above
            if (
                lot.produced_by_document_type != SOURCE_DOCUMENT_TYPE
                or lot.produced_by_document_id != str(batch.public_id)
            ):
                found.append(
                    _finding(
                        "posted_output_lot_provenance_mismatch",
                        batch,
                        _("لوط الناتج لا يشير إلى الدفعة التي أنتجته."),
                    )
                )
    elif batch.output_lot_id is not None:
        found.append(
            _finding(
                "posted_output_lot_not_required",
                batch,
                _("أُنشئ لوط ناتج لصنف لا يتتبع اللوطات."),
            )
        )
    return found


# ---------------------------------------------------------------------------
# The general ledger, and its legitimate silence
# ---------------------------------------------------------------------------


def account_nets(movements: list[StockMovement]) -> dict[int, Decimal]:
    """
    Per control account, what this posting moved. Signed.

    Positive is value entering the account, negative is value leaving it. The
    sum over all accounts is zero by value conservation, which is exactly why a
    single-account batch nets to zero everywhere.
    """
    nets: dict[int, Decimal] = {}
    for movement in movements:
        if movement.control_account_id is None:
            continue
        nets[movement.control_account_id] = (
            nets.get(movement.control_account_id, ZERO) + movement.inventory_value
        )
    return nets


def _journal_findings(batch: ProductionBatch, movements: list[StockMovement]) -> list[DraftFinding]:
    """
    The proof that tells a correct silence from a missing journal.

    Recomputed from the movements rather than read from a column, because the
    column is `NULL` in both cases and only the recomputation distinguishes
    them (RCP-112 proof 5).
    """
    found: list[DraftFinding] = []
    nets = {
        account_id: quantize_money(net)
        for account_id, net in account_nets(movements).items()
        if quantize_money(net) != ZERO
    }

    if batch.journal_entry_id is None:
        if nets:
            found.append(
                _finding(
                    "posted_batch_journal_is_missing",
                    batch,
                    _("صافي حسابات المخزون غير صفري ولا يوجد قيد محاسبي."),
                )
            )
        return found

    if not nets:
        found.append(
            _finding(
                "posted_batch_journal_should_be_silent",
                batch,
                _("صافي حسابات المخزون صفر ومع ذلك كُتب قيد محاسبي."),
            )
        )
        return found

    journal = batch.journal_entry
    assert journal is not None  # noqa: S101 - guarded above
    posted: dict[int, Decimal] = {}
    for line in journal.lines.all():
        posted[line.account_id] = posted.get(line.account_id, ZERO) + line.debit - line.credit
    if {key: value for key, value in posted.items() if value != ZERO} != nets:
        found.append(
            _finding(
                "posted_batch_journal_disagrees_with_movements",
                batch,
                _("سطور القيد لا تساوي صوافي الحسابات من الحركات."),
            )
        )
    return found


def _identity_findings(batch: ProductionBatch) -> list[DraftFinding]:
    """One posting per batch, named the same way everywhere."""
    found: list[DraftFinding] = []
    entry = batch.stock_entry
    assert entry is not None  # noqa: S101 - guarded by the caller
    if (
        entry.source_document_type != SOURCE_DOCUMENT_TYPE
        or entry.source_document_id != str(batch.public_id)
        or entry.source_event != "POSTED"
    ):
        found.append(
            _finding(
                "posted_batch_source_identity_mismatch",
                batch,
                _("هوية المستند على الترحيل المخزني لا تطابق الدفعة."),
            )
        )
    duplicates = (
        ProductionBatch.objects.filter(organization=batch.organization, stock_entry__isnull=False)
        .exclude(pk=batch.pk)
        .filter(stock_entry__source_document_id=str(batch.public_id))
        .count()
    )
    if duplicates:
        found.append(
            _finding(
                "posted_batch_source_identity_duplicated",
                batch,
                _("أكثر من دفعة تحمل نفس هوية المستند."),
            )
        )
    if batch.journal_entry_id is not None:
        others = (
            JournalEntry.objects.filter(
                organization=batch.organization,
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=str(batch.public_id),
                source_event="POSTED",
            )
            .exclude(pk=batch.journal_entry_id)
            .count()
        )
        if others:
            found.append(
                _finding(
                    "posted_batch_journal_duplicated",
                    batch,
                    _("أكثر من قيد محاسبي يحمل هوية هذه الدفعة."),
                )
            )
    return found


def _reversal_findings(batch: ProductionBatch) -> list[DraftFinding]:
    """A reversal mirrors its original exactly, or it is not a reversal."""
    found: list[DraftFinding] = []
    if batch.status != ProductionBatchStatus.REVERSED:
        if batch.reversal_stock_entry_id is not None:
            found.append(
                _finding(
                    "posted_batch_has_unexpected_reversal",
                    batch,
                    _("دفعة غير معكوسة تحمل ترحيل عكس."),
                )
            )
        return found

    if batch.reversal_stock_entry_id is None:
        found.append(
            _finding("reversed_batch_has_no_reversal", batch, _("دفعة معكوسة بلا ترحيل عكس."))
        )
        return found
    if not batch.reversal_reason.strip():
        found.append(_finding("reversed_batch_has_no_reason", batch, _("دفعة معكوسة بلا سبب.")))

    entry = batch.stock_entry
    reversal = batch.reversal_stock_entry
    assert entry is not None and reversal is not None  # noqa: S101 - guarded above
    original = {
        (movement.item_id, movement.lot_id, movement.effect_key): (
            movement.base_quantity,
            movement.inventory_value,
        )
        for movement in entry.movements.all()
    }
    mirrored = {
        (movement.item_id, movement.lot_id, movement.effect_key.removeprefix("reverse:")): (
            -movement.base_quantity,
            -movement.inventory_value,
        )
        for movement in reversal.movements.all()
    }
    if original != mirrored:
        found.append(
            _finding(
                "reversal_does_not_mirror_the_posting",
                batch,
                _("حركات العكس لا تطابق حركات الترحيل الأصلية بالضبط."),
            )
        )

    if batch.journal_entry_id is None and batch.reversal_journal_entry_id is not None:
        found.append(
            _finding(
                "reversal_journal_without_an_original",
                batch,
                _("كُتب قيد عكس لدفعة لم يكن لها قيد أصلاً."),
            )
        )
    if batch.journal_entry_id is not None and batch.reversal_journal_entry_id is None:
        found.append(
            _finding(
                "reversal_journal_is_missing",
                batch,
                _("دفعة لها قيد أصلي ولا يوجد لها قيد عكس."),
            )
        )
    return found


def posted_batches_checked(organization: Organization) -> int:
    """How many postings the verifier looked at, for the summary line."""
    return (
        ProductionBatch.objects.filter(organization=organization)
        .exclude(status=ProductionBatchStatus.DRAFT)
        .count()
    )


__all__ = [
    "account_nets",
    "verify_production",
    "posted_batch_findings",
    "posted_batches_checked",
    "verify_posted_production",
]
