"""
`inventory-issue-coverage-gap` — the system saying where it is blind.

An item was received. Recipes and sales say it must have been consumed. The
stock ledger records almost none of that consumption. That gap is what this
detector reports, and it is the first detector deliberately: every later
analysis — usage variance, waste trends, theoretical-versus-actual, margin by
plate — is computed *from the same actual-consumption inputs*. If those inputs
are missing, those analyses are not wrong so much as uninformed, and an
analytics layer that does not know that will state its conclusions with a
confidence nothing supports.

## What this finding is not

It is **not** evidence of waste, theft, loss, negligence, or financial damage.
The overwhelmingly likely explanation is the ordinary one: issues are not being
keyed. The narrative says the recording is incomplete and the recommendation
asks somebody to look at the workflow. It never names a person, never infers a
motive, and never converts a quantity gap into a money loss — the quantity is
missing, so any money figure derived from it would be invented.

## The comparison, exactly

For one item at one warehouse over `[period_start, period_end)`:

    item_issue_ratio = actual_consumption / theoretical_consumption

`actual_consumption` is `ItemFlow.total_consumption`, which the kitchen engine
already defines as `net_production_consumption + direct_economic_consumption`
and already returns as a **positive magnitude** — the engine negates the
ledger's signed outbound quantities inside those properties. So no `abs()`
here: taking the absolute value of a number that is already a magnitude would
be a no-op that hides the day the engine's convention changes.

`theoretical_consumption` is the sum of `EquivalentTotal.effective_base_quantity`
across the registered theoretical sources for that leaf item. Summing *across*
sources is correct here and is not the double count `totals_by_item` refuses:
that function refuses a grand total because two sources may describe the same
plate; these sources are disjoint populations — a sold dish, a staff meal, a
complimentary meal — and a kilo consumed by any of them is a kilo the ledger
should show leaving.

## Eligibility, and why it is strict

An item is compared only when all four hold in the same window and scope:

1. at least one posted receipt (`SUPPLY_RECEIPT`) — without it there is no
   evidence the item was even handled here;
2. theoretical consumption strictly greater than zero — the denominator;
3. the same base unit on both sides — comparing kilograms to pieces produces a
   ratio that is arithmetic nonsense;
4. the warehouse's stock identity holds (`identity_difference == 0`) — if a
   movement reached the ledger and reached no bucket, the actual figure is
   not trustworthy and the honest act is to say so rather than to divide by it.

Anything that fails these is **not a finding**. It is a limitation, recorded on
the run's outcome. Inventing a finding out of an unreliable comparison is how
an analytics layer loses the reader for good.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from decimal import Decimal
from typing import Any

from apps.insights.detectors.base import (
    STATEFUL,
    Candidate,
    DetectorContext,
    DetectorCoverage,
    DetectorResult,
    DetectorSkippedError,
    DetectorSpec,
    InsightConfidence,
    InsightDomain,
    InsightSensitivity,
    InsightSeverity,
    register,
)

CODE = "inventory-issue-coverage-gap"
VERSION = "1.0.0"
EVIDENCE_SCHEMA = "insights.inventory-issue-coverage-gap.evidence/1"

#: The only setting, as an exact Decimal string. Below this share of its
#: theoretical consumption, an item's recorded issues are treated as materially
#: incomplete rather than merely imprecise.
DEFAULT_SETTINGS = {"minimum_item_issue_ratio": "0.05"}

ZERO = Decimal("0")

#: The kitchen engines take inclusive date windows; ours is half-open. One
#: day is the whole of the translation between them.
_ONE_DAY = datetime.timedelta(days=1)

#: Evidence lists are bounded. A warehouse with nine hundred movements would
#: otherwise put nine hundred ids in a JSON column that a person then has to
#: scroll past to reach the number that matters.
MAX_SAMPLE = 20


def _fingerprint(*, item_id: int, branch: Any | None, warehouse_id: int) -> str:
    """
    The stable identity of this condition.

    Item, scope, warehouse — and nothing that moves. No period (it rolls every
    run), no severity, no measured ratio (those are the observations), and no
    translated label (it changes with the locale and would fork the case).
    """
    branch_part = f"branch={branch.pk}" if branch is not None else "branch=org"
    return f"{CODE}:item={item_id}:{branch_part}:warehouse={warehouse_id}"


def _exact(value: Decimal) -> str:
    """A Decimal as an exact, locale-independent string for evidence JSON."""
    return format(value, "f")


def detect(context: DetectorContext) -> DetectorResult:
    """Compare recorded consumption against what the recipes imply was used."""
    # Imported here rather than at module scope: the registry is imported by
    # `apps.insights.apps` at startup, and the kitchen engines pull in the
    # sales adapter, which is registered from another app's `ready()`.
    from apps.kitchen.consumption import (
        FlowFilters,
        MovementBucket,
        kitchen_warehouse_flow,
    )
    from apps.kitchen.consumption_sources import REGISTERED_SOURCES, totals_by_item

    threshold = context.settings["minimum_item_issue_ratio"]

    # --- theoretical side ------------------------------------------------
    #
    # The registry is asked directly with an explicit organization rather than
    # through the permission-scoped helper, because the run's scope is already
    # decided by the orchestrator and the helper would silently re-derive a
    # different organization from the actor's memberships.
    branch_ids = [context.branch.pk] if context.branch is not None else None
    contributions = [
        row
        for source in REGISTERED_SOURCES
        for row in source.contributions(
            organization_id=context.organization.pk,
            branch_ids=branch_ids,
            date_from=context.period_start,
            # The engines' windows are inclusive on both ends while ours is
            # half-open, so the last day is excluded here by asking for the
            # day before it. Passing `period_end` straight through would count
            # one extra business day of sales against the same stock.
            date_to=context.period_end - _ONE_DAY,
            recipe_id=None,
        )
    ]
    if not REGISTERED_SOURCES:
        raise DetectorSkippedError(
            "no_theoretical_sources",
            "لا يوجد مصدر استهلاك نظري مسجَّل، فلا مقام للمقارنة.",
        )

    theoretical: dict[int, Decimal] = defaultdict(lambda: ZERO)
    theoretical_units: dict[int, str] = {}
    theoretical_sources: dict[int, set[str]] = defaultdict(set)
    contribution_counts: dict[int, int] = defaultdict(int)
    for total in totals_by_item(contributions):
        theoretical[total.leaf_item_id] += total.effective_base_quantity
        theoretical_units.setdefault(total.leaf_item_id, total.base_unit_code)
        theoretical_sources[total.leaf_item_id].add(str(total.source_type))
        contribution_counts[total.leaf_item_id] += total.contribution_count

    # --- actual side -----------------------------------------------------
    flow = kitchen_warehouse_flow(
        context.actor,
        FlowFilters(date_from=context.period_start, date_to=context.period_end - _ONE_DAY),
    )

    candidates: list[Candidate] = []
    evaluated: list[str] = []
    skipped_unreliable = 0
    skipped_no_receipt = 0
    skipped_unit_mismatch = 0
    skipped_no_theoretical = 0
    identity_failures: list[str] = []

    for item in flow.items:
        implied = theoretical.get(item.item_id, ZERO)
        if implied <= ZERO:
            skipped_no_theoretical += 1
            continue

        receipts = item.quantity_of(MovementBucket.SUPPLY_RECEIPT)
        if receipts == ZERO:
            skipped_no_receipt += 1
            continue

        expected_unit = theoretical_units.get(item.item_id, "")
        if expected_unit and expected_unit != item.base_unit_code:
            # Two different units on the two sides of a division is not a
            # small imprecision; it is a meaningless number.
            skipped_unit_mismatch += 1
            continue

        if item.identity_difference != ZERO:
            # The partition did not account for every movement at this
            # warehouse, so the actual figure is not trustworthy here.
            skipped_unreliable += 1
            identity_failures.append(f"{item.warehouse_code}:{item.item_code}")
            continue

        fingerprint = _fingerprint(
            item_id=item.item_id, branch=context.branch, warehouse_id=item.warehouse_id
        )
        evaluated.append(fingerprint)

        actual = item.total_consumption
        ratio = actual / implied
        if ratio >= threshold:
            continue

        severity = InsightSeverity.HIGH if actual == ZERO else InsightSeverity.MEDIUM
        scope_key = (
            f"{context.branch.code} · {item.warehouse_code}"
            if context.branch is not None
            else item.warehouse_code
        )
        candidates.append(
            Candidate(
                fingerprint=fingerprint,
                scope_key=scope_key,
                branch=context.branch,
                severity=severity,
                confidence=InsightConfidence.HIGH,
                title_ar=_title(item),
                narrative_ar=_narrative(
                    item=item, actual=actual, implied=implied, ratio=ratio, threshold=threshold
                ),
                recommendation_ar=_recommendation(item),
                evidence=_evidence(
                    context=context,
                    item=item,
                    actual=actual,
                    implied=implied,
                    ratio=ratio,
                    threshold=threshold,
                    receipts=receipts,
                    sources=sorted(theoretical_sources.get(item.item_id, set())),
                    contribution_count=contribution_counts.get(item.item_id, 0),
                ),
                # Worst first, then a stable technical tie-break so two runs
                # over identical data never reorder the list.
                sort_key=(ratio, item.item_code, item.warehouse_code),
            )
        )

    coverage = _coverage(
        evaluated=evaluated,
        unreliable=skipped_unreliable,
        unit_mismatch=skipped_unit_mismatch,
    )
    return DetectorResult(
        candidates=sorted(candidates, key=lambda row: row.sort_key),
        evaluated_fingerprints=evaluated,
        coverage=coverage,
        evaluated_scope_count=len({row.split(":warehouse=")[-1] for row in evaluated}),
        notes={
            "items_seen": len(flow.items),
            "items_evaluated": len(evaluated),
            "skipped_no_theoretical_consumption": skipped_no_theoretical,
            "skipped_no_posted_receipt": skipped_no_receipt,
            "skipped_unit_mismatch": skipped_unit_mismatch,
            "skipped_identity_unreliable": skipped_unreliable,
            "identity_failures_sample": identity_failures[:MAX_SAMPLE],
            "theoretical_sources": sorted({str(s.source_type) for s in REGISTERED_SOURCES}),
        },
    )


def _coverage(*, evaluated: list[str], unreliable: int, unit_mismatch: int) -> str:
    """
    How much of the intended population could actually be compared.

    Nothing evaluated is `INSUFFICIENT` — not `COMPLETE` with an empty result.
    The difference is the whole point: one says "everything is fine", the other
    says "I could not see anything", and only the first may close a case.
    """
    if not evaluated:
        return DetectorCoverage.INSUFFICIENT
    if unreliable or unit_mismatch:
        return DetectorCoverage.PARTIAL
    return DetectorCoverage.COMPLETE


# ---------------------------------------------------------------------------
# Deterministic Arabic wording, generated from the evidence and nothing else
# ---------------------------------------------------------------------------


def _title(item: Any) -> str:
    return f"استهلاك غير مسجَّل: {item.item_name} في {item.warehouse_code}"


def _narrative(
    *, item: Any, actual: Decimal, implied: Decimal, ratio: Decimal, threshold: Decimal
) -> str:
    unit = item.base_unit_code
    percent = _exact((ratio * 100).quantize(Decimal("0.01")))
    limit = _exact((threshold * 100).quantize(Decimal("0.01")))
    if actual == ZERO:
        opening = (
            f"لا يوجد استهلاك مسجَّل إطلاقاً للصنف «{item.item_name}» "
            f"({item.item_code}) في مخزن {item.warehouse_code} خلال الفترة، "
            f"رغم وجود استلامات مرحّلة."
        )
    else:
        opening = (
            f"الاستهلاك المسجَّل للصنف «{item.item_name}» ({item.item_code}) في مخزن "
            f"{item.warehouse_code} بلغ {_exact(actual)} {unit} فقط، أي {percent}٪ "
            f"من المتوقع، وهو دون الحد المعتمد {limit}٪."
        )
    return (
        f"{opening} تشير الوصفات والمبيعات إلى استهلاك متوقع قدره "
        f"{_exact(implied)} {unit} في الفترة نفسها. "
        "هذا فرق في تسجيل البيانات لا دليل على هدر أو فقدان: الحركات المخزنية "
        "لم تُسجَّل، أو سُجّلت ناقصة. "
        "والأثر أن كل تحليل يعتمد على الاستهلاك الفعلي — كفروقات الاستهلاك "
        "النظري مقابل الفعلي — يبقى ناقص التغطية لهذا الصنف حتى تُسجَّل حركاته."
    )


def _recommendation(item: Any) -> str:
    return (
        f"راجع آلية تسجيل صرف المخزون لمخزن {item.warehouse_code}، وتحقّق من أن "
        f"صرفيات «{item.item_name}» تُدخل عند حدوثها. "
        "المراجعة إدارية للتحقق من اكتمال التسجيل، ولا تفترض مسؤولية أحد."
    )


def _evidence(
    *,
    context: DetectorContext,
    item: Any,
    actual: Decimal,
    implied: Decimal,
    ratio: Decimal,
    threshold: Decimal,
    receipts: Decimal,
    sources: list[str],
    contribution_count: int,
) -> dict[str, Any]:
    """
    Everything needed to reproduce and audit the claim.

    Decimals are exact strings; counts and ids stay integers. A reader with
    this dictionary and the source ledgers can recompute the ratio and reach
    the same number, which is the only definition of "evidence" worth the word.
    """
    return {
        "schema": EVIDENCE_SCHEMA,
        "formula": "item_issue_ratio = actual_consumption / theoretical_consumption",
        "detector_version": VERSION,
        "settings_version": context.settings_version,
        "period": {
            "start": context.period_start.isoformat(),
            "end_exclusive": context.period_end.isoformat(),
            "basis": "business_date",
        },
        "source_cutoffs": dict(context.source_cutoffs),
        "scope": {
            "organization_id": context.organization.pk,
            "organization_code": context.organization.code,
            "branch_id": context.branch.pk if context.branch is not None else None,
            "warehouse_id": item.warehouse_id,
            "warehouse_code": item.warehouse_code,
        },
        "item": {
            "id": item.item_id,
            "code": item.item_code,
            "name": item.item_name,
            "base_unit": item.base_unit_code,
        },
        "measures": {
            "actual_consumption": _exact(actual),
            "theoretical_consumption": _exact(implied),
            "item_issue_ratio": _exact(ratio),
            "threshold": _exact(threshold),
            "unit": item.base_unit_code,
        },
        "counts": {
            "posted_receipt_quantity": _exact(receipts),
            "movements_at_warehouse": item.movement_count,
            "theoretical_contributions": contribution_count,
        },
        "theoretical_sources": sources,
        "identity_difference": _exact(item.identity_difference),
        # A typed reference the template resolves through the item screen's own
        # permission boundary. Never a URL built inside JSON.
        "source_references": [
            {"type": "inventory.item", "id": item.item_id, "label": item.item_code},
            {"type": "inventory.warehouse", "id": item.warehouse_id, "label": item.warehouse_code},
        ],
    }


SPEC = register(
    DetectorSpec(
        code=CODE,
        domain=InsightDomain.DATA_QUALITY,
        sensitivity=InsightSensitivity.OPERATIONAL,
        lifecycle=STATEFUL,
        version=VERSION,
        required_permission="insights.view_insight",
        default_settings=DEFAULT_SETTINGS,
        detect=detect,
        minimum_sample=1,
    )
)
