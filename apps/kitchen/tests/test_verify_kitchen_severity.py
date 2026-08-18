"""
Proof that `verify_kitchen` can actually fail.

A verifier that has only ever been run against clean data is a verifier nobody
has evidence for. It exits 0, which is exactly what a verifier that checks
nothing at all would also do — and the difference between those two is the whole
value of the command.

So this file plants contradictions and asserts the command notices, and asserts
the two severities that must **not** fail it do not.

## Why these are pure-function tests

`verify_kitchen`'s severity contract lives in two places: the finding functions
that classify a defect, and the command's exit rule. Both are reachable without
a database, and driving them directly is what makes this a focused proof rather
than the beginning of a suite.

The alternative — planting a real defect in the development database inside a
rolled-back transaction — was considered and rejected here for a reason worth
recording: the guards Task 3.8 added are *good*, so most contradictions cannot be
written at all. The trigger refuses them, which is the right behaviour and the
wrong tool for proving the reporting layer reacts. The database guards have their
own direct probe; this proves the severity and exit logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from apps.kitchen.consumption import FlowFilters
from apps.kitchen.consumption_reconciliation import (
    ADVISORY,
    COVERAGE_LIMITATION,
    ERROR,
    Finding,
)
from apps.kitchen.management.commands.verify_kitchen import Section

if TYPE_CHECKING:
    from apps.users.models import User


class TestTheSeverityContract:
    """`ERROR` fails the run. The other two never do."""

    def test_an_error_finding_is_an_error(self) -> None:
        finding = Finding(severity=ERROR, code="planted", message="a planted contradiction")
        assert finding.is_error is True

    def test_an_advisory_is_not_an_error(self) -> None:
        assert Finding(severity=ADVISORY, code="c", message="m").is_error is False

    def test_a_coverage_limitation_is_not_an_error(self) -> None:
        """
        The one that matters most.

        `SALES_NOT_INCLUDED_PHASE_4` is reported on every single run, because
        Phase 4 has not happened. If it counted as an error the command would be
        permanently red, and a permanently red gate is a gate everybody learns to
        ignore.
        """
        limitation = Finding(
            severity=COVERAGE_LIMITATION,
            code="SALES_NOT_INCLUDED_PHASE_4",
            message="approved sales quantities arrive in Phase 4",
        )
        assert limitation.is_error is False


class TestWhatASectionCounts:
    """A section's `errors` is what the exit code is built from."""

    def test_a_clean_section_has_no_errors(self) -> None:
        section = Section(title="t", checked="0 rows", findings=[])
        assert section.errors == []

    def test_advisories_and_limitations_alone_leave_a_section_clean(self) -> None:
        """
        A section carrying only the two non-failing severities must report zero
        errors. This is the shape every real run has: nine advisories and five
        coverage limitations, and an exit code of 0.
        """
        section = Section(
            title="t",
            checked="many",
            findings=[
                Finding(severity=ADVISORY, code="a", message="m"),
                Finding(
                    severity=COVERAGE_LIMITATION, code="SALES_NOT_INCLUDED_PHASE_4", message="m"
                ),
                Finding(severity=ADVISORY, code="b", message="m"),
            ],
        )
        assert section.errors == []

    def test_one_error_among_many_findings_is_still_found(self) -> None:
        """
        The planted defect. Buried among advisories, because that is how a real
        one arrives — never alone and never first.
        """
        planted = Finding(severity=ERROR, code="planted_defect", message="stock identity broken")
        section = Section(
            title="t",
            checked="many",
            findings=[
                Finding(severity=ADVISORY, code="a", message="m"),
                Finding(severity=COVERAGE_LIMITATION, code="c", message="m"),
                planted,
                Finding(severity=ADVISORY, code="b", message="m"),
            ],
        )
        assert section.errors == [planted]


class TestThePartitionVerifierReactsToAContradiction:
    """
    `verify_movement_partition` must report an unclassifiable movement as ERROR.

    The classifier raises `ValueError` on a `MovementType` it does not know —
    that is the design, and it is only useful if the layer above turns it into a
    finding rather than a traceback.
    """

    def test_an_unclassifiable_movement_becomes_an_error_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apps.kitchen import consumption_reconciliation

        def explode(_user: object, _filters: object) -> None:
            raise ValueError("unclassified movement type 'INVENTED' on movement 1")

        monkeypatch.setattr(consumption_reconciliation, "kitchen_warehouse_flow", explode)
        # The scoped read is monkeypatched away, so no real caller is needed;
        # the cast states that rather than leaving `object()` to be puzzled over.
        findings = consumption_reconciliation.verify_movement_partition(
            cast("User", object()), FlowFilters()
        )

        assert len(findings) == 1
        assert findings[0].severity == ERROR
        assert findings[0].code == "kitchen_movement_unclassified"
        assert "INVENTED" in findings[0].message

    def test_a_broken_stock_identity_becomes_an_error_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A flow whose buckets do not reconstruct the ledger's own movement is the
        one failure the partition exists to surface. Built here as a contradiction
        rather than found in data, because correct data cannot produce it.
        """
        from decimal import Decimal

        from apps.kitchen import consumption_reconciliation
        from apps.kitchen.consumption import ItemFlow, MovementBucket, WarehouseFlow

        broken = ItemFlow(
            warehouse_id=1,
            warehouse_code="W",
            item_id=1,
            item_code="ITEM",
            item_name="item",
            base_unit_code="KG",
            opening=Decimal("0"),
            # Closing says 10 arrived; the buckets account for only 4. The
            # missing 6 is a movement that reached the ledger and no bucket.
            closing=Decimal("10"),
            quantities={MovementBucket.SUPPLY_RECEIPT: Decimal("4")},
        )
        flow = WarehouseFlow(
            warehouse=None, date_from=None, date_to=None, items=[broken], classified_count=1
        )
        monkeypatch.setattr(
            consumption_reconciliation, "kitchen_warehouse_flow", lambda *_a, **_k: flow
        )

        # The scoped read is monkeypatched away, so no real caller is needed;
        # the cast states that rather than leaving `object()` to be puzzled over.
        findings = consumption_reconciliation.verify_movement_partition(
            cast("User", object()), FlowFilters()
        )
        errors = [row for row in findings if row.severity == ERROR]

        assert len(errors) == 1
        assert errors[0].code == "kitchen_stock_identity_broken"
        assert "ITEM" in errors[0].message
        # The difference is reported, not merely the fact of one.
        assert "6" in errors[0].message
