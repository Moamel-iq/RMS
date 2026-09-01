"""
`inventory-issue-coverage-gap` — the arithmetic, and where it refuses to speak.

The comparison itself is one division, so most of these tests are about the
boundaries around it: the exact threshold, the zero denominator, the mismatched
unit, the unreliable identity. Those are where an analytics layer either earns
trust or loses it, because a wrong finding costs more than a missing one — the
reader who is sent to look at a clean item twice stops looking the third time.

The engine is stubbed rather than exercised through posted movements. That is
deliberate and it is the *whole* point of the seam: the detector's contract is
"given these flows and these contributions, produce these candidates", and
proving that needs control over the inputs, not a fixture that posts fifty
movements to arrive at one ratio. The engines have their own tests for whether
those flows are right.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from apps.insights.detectors import inventory_issue_coverage as detector
from apps.insights.detectors.base import DetectorContext, DetectorCoverage, InsightSeverity

pytestmark = pytest.mark.django_db

WINDOW_START = datetime.date(2026, 3, 1)
WINDOW_END = datetime.date(2026, 4, 1)
ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Stubs shaped exactly like the engines' real return values
# ---------------------------------------------------------------------------


@dataclass
class FakeItemFlow:
    """
    Mirrors `apps.kitchen.consumption.ItemFlow` where this detector touches it.

    `total_consumption` is a positive magnitude here because it is one there:
    the engine negates the ledger's signed outbound quantities inside its own
    properties. A stub that returned a negative would be testing a convention
    the engine does not have.
    """

    item_id: int
    item_code: str
    item_name: str
    warehouse_id: int
    warehouse_code: str
    base_unit_code: str
    total_consumption: Decimal
    receipts: Decimal
    identity_difference: Decimal = ZERO
    movement_count: int = 5

    def quantity_of(self, bucket: Any) -> Decimal:
        from apps.kitchen.consumption import MovementBucket

        return self.receipts if bucket == MovementBucket.SUPPLY_RECEIPT else ZERO


@dataclass
class FakeFlow:
    items: list[FakeItemFlow] = field(default_factory=list)


@dataclass
class FakeTotal:
    leaf_item_id: int
    leaf_item_code: str
    leaf_item_name: str
    base_unit_code: str
    effective_base_quantity: Decimal
    contribution_count: int = 1
    source_type: str = "SALES"


class FakeSource:
    source_type = "SALES"

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def contributions(self, **kwargs: Any) -> list[Any]:
        return self._rows


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the detector at controlled flows and contributions."""

    def _apply(*, items: list[FakeItemFlow], totals: list[FakeTotal]) -> None:
        import apps.kitchen.consumption as consumption
        import apps.kitchen.consumption_sources as sources

        monkeypatch.setattr(
            consumption, "kitchen_warehouse_flow", lambda user, filters: FakeFlow(items=items)
        )
        monkeypatch.setattr(sources, "REGISTERED_SOURCES", (FakeSource([object()]),))
        monkeypatch.setattr(sources, "totals_by_item", lambda contributions: totals)

    return _apply


@pytest.fixture
def context(organization: Any, owner: Any) -> DetectorContext:
    return DetectorContext(
        organization=organization,
        branch=None,
        period_start=WINDOW_START,
        period_end=WINDOW_END,
        settings={"minimum_item_issue_ratio": Decimal("0.05")},
        settings_version=0,
        source_cutoffs={"inventory.stock_movement.posted_sequence": "100"},
        actor=owner,
    )


def _item(**overrides: Any) -> FakeItemFlow:
    base = {
        "item_id": 1,
        "item_code": "STK-0001",
        "item_name": "لحم",
        "warehouse_id": 7,
        "warehouse_code": "MAIN",
        "base_unit_code": "KG",
        "total_consumption": ZERO,
        "receipts": Decimal("500"),
    }
    base.update(overrides)
    return FakeItemFlow(**base)  # type: ignore[arg-type]


def _total(**overrides: Any) -> FakeTotal:
    base = {
        "leaf_item_id": 1,
        "leaf_item_code": "STK-0001",
        "leaf_item_name": "لحم",
        "base_unit_code": "KG",
        "effective_base_quantity": Decimal("100"),
    }
    base.update(overrides)
    return FakeTotal(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


class TestTheRatio:
    def test_zero_recorded_consumption_is_a_high_finding(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """The `STK-0102` case: received, sold, and never issued."""
        patched(items=[_item(total_consumption=ZERO)], totals=[_total()])
        result = detector.detect(context)

        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.severity == InsightSeverity.HIGH
        assert candidate.evidence["measures"]["item_issue_ratio"] == "0"
        assert candidate.evidence["measures"]["theoretical_consumption"] == "100"

    def test_a_small_but_non_zero_share_is_medium(
        self, patched: Any, context: DetectorContext
    ) -> None:
        patched(
            items=[_item(total_consumption=Decimal("1"))],
            totals=[_total(effective_base_quantity=Decimal("100"))],
        )
        candidate = detector.detect(context).candidates[0]
        assert candidate.severity == InsightSeverity.MEDIUM
        assert candidate.evidence["measures"]["item_issue_ratio"] == "0.01"

    def test_exactly_at_the_threshold_is_not_a_finding(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """
        `<` and not `<=`, exactly as specified.

        Five percent of a hundred is five. The boundary is stated as "equality
        is not a finding", and a Decimal comparison is the only way to keep
        that promise — `5/100` as a binary float is not `0.05`.
        """
        patched(
            items=[_item(total_consumption=Decimal("5"))],
            totals=[_total(effective_base_quantity=Decimal("100"))],
        )
        assert detector.detect(context).candidates == []

    def test_just_below_the_threshold_is_a_finding(
        self, patched: Any, context: DetectorContext
    ) -> None:
        patched(
            items=[_item(total_consumption=Decimal("4.999"))],
            totals=[_total(effective_base_quantity=Decimal("100"))],
        )
        assert len(detector.detect(context).candidates) == 1

    def test_well_above_the_threshold_is_silent(
        self, patched: Any, context: DetectorContext
    ) -> None:
        patched(
            items=[_item(total_consumption=Decimal("95"))],
            totals=[_total(effective_base_quantity=Decimal("100"))],
        )
        result = detector.detect(context)
        assert result.candidates == []
        assert result.coverage == DetectorCoverage.COMPLETE
        assert result.evaluated_fingerprints, "silence must still say what it looked at"

    def test_one_eligible_item_is_enough(self, patched: Any, context: DetectorContext) -> None:
        """No artificial multi-item minimum: one item is a population of one."""
        patched(items=[_item()], totals=[_total()])
        assert len(detector.detect(context).candidates) == 1


class TestWhenItRefusesToSpeak:
    def test_no_theoretical_consumption_means_no_denominator(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """Never divides by zero; records the exclusion instead."""
        patched(items=[_item()], totals=[])
        result = detector.detect(context)
        assert result.candidates == []
        assert result.notes["skipped_no_theoretical_consumption"] == 1
        assert result.coverage == DetectorCoverage.INSUFFICIENT

    def test_no_posted_receipt_means_no_evidence_the_item_was_handled_here(
        self, patched: Any, context: DetectorContext
    ) -> None:
        patched(items=[_item(receipts=ZERO)], totals=[_total()])
        result = detector.detect(context)
        assert result.candidates == []
        assert result.notes["skipped_no_posted_receipt"] == 1

    def test_a_unit_mismatch_is_excluded_not_compared(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """
        Kilograms against pieces is not an imprecise ratio; it is nonsense.

        Excluding it also degrades coverage to PARTIAL, so the run cannot
        later resolve anything on the strength of what it could not compare.
        """
        patched(
            items=[_item(base_unit_code="KG")],
            totals=[_total(base_unit_code="PIECE")],
        )
        result = detector.detect(context)
        assert result.candidates == []
        assert result.notes["skipped_unit_mismatch"] == 1
        assert result.coverage == DetectorCoverage.INSUFFICIENT

    def test_a_broken_stock_identity_suppresses_the_item(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """
        A movement reached the ledger and reached no bucket.

        The actual figure is then not trustworthy, and dividing by it would
        produce a confident number built on an unexplained gap.
        """
        patched(
            items=[_item(identity_difference=Decimal("3"))],
            totals=[_total()],
        )
        result = detector.detect(context)
        assert result.candidates == []
        assert result.notes["skipped_identity_unreliable"] == 1

    def test_partial_coverage_when_some_items_are_excluded(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """One comparable item and one unreliable one is PARTIAL, not COMPLETE."""
        patched(
            items=[
                _item(item_id=1, total_consumption=ZERO),
                _item(item_id=2, item_code="STK-0002", identity_difference=Decimal("1")),
            ],
            totals=[_total(leaf_item_id=1), _total(leaf_item_id=2, leaf_item_code="STK-0002")],
        )
        result = detector.detect(context)
        assert len(result.candidates) == 1
        assert result.coverage == DetectorCoverage.PARTIAL


class TestDeterminism:
    def test_the_fingerprint_carries_no_rolling_value(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """
        Identity must survive the period, the severity and the measurement.

        Any of those inside it would make the same condition a new case every
        week, and nothing anybody acknowledged would stay acknowledged.
        """
        patched(items=[_item()], totals=[_total()])
        fingerprint = detector.detect(context).candidates[0].fingerprint
        assert fingerprint == "inventory-issue-coverage-gap:item=1:branch=org:warehouse=7"
        for forbidden in ("2026-03", "HIGH", "0.05", "لحم"):
            assert forbidden not in fingerprint

    def test_the_same_inputs_produce_the_same_output(
        self, patched: Any, context: DetectorContext
    ) -> None:
        patched(items=[_item()], totals=[_total()])
        first = detector.detect(context)
        second = detector.detect(context)
        assert [c.fingerprint for c in first.candidates] == [
            c.fingerprint for c in second.candidates
        ]
        assert first.candidates[0].evidence == second.candidates[0].evidence

    def test_worst_ratio_sorts_first(self, patched: Any, context: DetectorContext) -> None:
        patched(
            items=[
                _item(item_id=1, item_code="STK-0001", total_consumption=Decimal("4")),
                _item(item_id=2, item_code="STK-0002", warehouse_id=8, total_consumption=ZERO),
            ],
            totals=[_total(leaf_item_id=1), _total(leaf_item_id=2, leaf_item_code="STK-0002")],
        )
        candidates = detector.detect(context).candidates
        assert candidates[0].evidence["item"]["code"] == "STK-0002", "zero is worse than 4%"


class TestEvidenceAndWording:
    def test_every_decimal_in_the_evidence_is_an_exact_string(
        self, patched: Any, context: DetectorContext
    ) -> None:
        from apps.insights.services import assert_no_floats

        patched(items=[_item(total_consumption=Decimal("1.5"))], totals=[_total()])
        evidence = detector.detect(context).candidates[0].evidence
        assert_no_floats(evidence)
        assert isinstance(evidence["measures"]["item_issue_ratio"], str)

    def test_the_evidence_carries_what_reproduces_the_claim(
        self, patched: Any, context: DetectorContext
    ) -> None:
        patched(items=[_item()], totals=[_total()])
        evidence = detector.detect(context).candidates[0].evidence
        assert evidence["period"]["start"] == "2026-03-01"
        assert evidence["period"]["end_exclusive"] == "2026-04-01"
        assert evidence["source_cutoffs"]["inventory.stock_movement.posted_sequence"] == "100"
        assert evidence["detector_version"] == detector.VERSION
        assert evidence["formula"]

    def test_source_references_are_typed_never_urls(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """
        A URL inside evidence JSON is a link the template would have to trust.

        Typed references are resolved by the template through each source
        screen's own permission boundary instead.
        """
        patched(items=[_item()], totals=[_total()])
        references = detector.detect(context).candidates[0].evidence["source_references"]
        assert {row["type"] for row in references} == {
            "inventory.item",
            "inventory.warehouse",
        }
        for row in references:
            assert "http" not in str(row).lower()

    def test_the_narrative_reports_a_recording_gap_not_an_accusation(
        self, patched: Any, context: DetectorContext
    ) -> None:
        """
        The sentence a manager will actually read.

        It must describe incomplete recording and must never assert waste,
        loss or theft — the quantity is missing, so any claim about where it
        went would be invented.
        """
        patched(items=[_item()], totals=[_total()])
        candidate = detector.detect(context).candidates[0]
        assert "تسجيل" in candidate.narrative_ar
        for accusation in ("سرقة", "اختلاس", "إهمال", "تلاعب"):
            assert accusation not in candidate.narrative_ar
            assert accusation not in candidate.recommendation_ar
        assert "لا تفترض مسؤولية" in candidate.recommendation_ar
