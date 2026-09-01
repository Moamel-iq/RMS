"""
The detector contract: pure functions from a context to candidates.

A detector reads and returns. It writes nothing, opens no transaction, sends
no notification and touches no ledger — persistence, deduplication, lifecycle
and delivery are the orchestrator's business. That separation is what makes a
detector testable as arithmetic: give it the same window, settings and source
snapshot and it must produce the same fingerprints, the same evidence values
and the same order, forever.

## Why a `DetectorResult` and not just a list

Three of the four things the orchestrator needs are not candidates.

**The evaluated fingerprints.** Auto-resolution may only close a case that a
run *explicitly looked at*. A detector that returns "no candidates" has not
said "everything I know about is clean" — it may simply never have reached
those rows. So it states, positively, which identities it evaluated.

**The coverage.** Whether the population could be seen at all is independent
of whether the run finished. `SUCCEEDED` + `PARTIAL` is the ordinary case for
analytics over incomplete operational data, and it is precisely the
combination that may show findings but may never resolve one.

**The data-quality flags.** What the detector could not see, in structured
form, so the reader is told what the silence is worth.

## Decimals

Every threshold, ratio and quantity here is a `Decimal` parsed from an exact
string. `float` never appears — not in a setting, not in a comparison, not in
evidence. `0.05` as a binary float is not five percent, and a threshold that is
not exactly the number the owner agreed to is a threshold nobody agreed to.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from apps.insights.models import (
    DetectorCoverage,
    InsightConfidence,
    InsightDomain,
    InsightSensitivity,
    InsightSeverity,
)


@dataclass(frozen=True)
class DetectorContext:
    """
    Everything a detector may read, resolved once by the orchestrator.

    The window is half-open business dates: `[period_start, period_end)`. The
    exclusive end is not pedantry — an inclusive end silently doubles the
    boundary day when two consecutive windows are compared, and the day it
    matters is the day somebody is reconciling a month.

    `frames` carries expensive authoritative reads that several detectors would
    otherwise each fetch. A detector takes what it needs from here rather than
    querying per item; the alternative is one queryset per item per detector,
    which is the shape that makes analytics unusable at a hundred items.
    """

    organization: Any
    branch: Any | None
    period_start: datetime.date
    period_end: datetime.date
    settings: Mapping[str, Decimal]
    settings_version: int
    source_cutoffs: Mapping[str, str]
    #: The signed-in caller, because the authoritative engines this reads
    #: through are themselves permission-scoped and take a user.
    actor: Any
    frames: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """
    One finding a detector proposes. Not yet a case, not yet stored.

    `fingerprint` is the identity the orchestrator deduplicates on, and it may
    contain only stable technical facts. A period, a severity, a measured
    value or a translated label in here would make the same condition a new
    case on every run — and nothing anybody acknowledged would ever stay
    acknowledged.
    """

    fingerprint: str
    scope_key: str
    branch: Any | None
    severity: str
    confidence: str
    title_ar: str
    narrative_ar: str
    recommendation_ar: str
    evidence: dict[str, Any]
    #: Deterministic tie-break for display, so two runs over identical data
    #: never present the same findings in a different order.
    sort_key: tuple[Any, ...] = ()


@dataclass(frozen=True)
class DetectorResult:
    """What one detector concluded, and how much it could see while concluding."""

    candidates: list[Candidate]
    #: Positively stated: these identities were looked at. Auto-resolution
    #: reads this and refuses to act on anything absent from it.
    evaluated_fingerprints: list[str]
    coverage: str
    evaluated_scope_count: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.coverage == DetectorCoverage.COMPLETE


class DetectorSkippedError(Exception):
    """
    A declared prerequisite is absent, so the detector did not run.

    Distinct from failure: skipping is an expected, named condition — a
    consumption adapter that is not registered, an organization with no
    warehouses — and it records `SKIPPED`, never `FAILED`, and never resolves
    anything.
    """

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


@dataclass(frozen=True)
class DetectorSpec:
    """
    What a detector declares about itself, before it runs.

    `lifecycle` is the one field with teeth. `STATEFUL` means the condition
    persists and can genuinely be observed to have ended, so a clean complete
    evaluation may resolve it. `EVENT` means the finding records something that
    *happened*; it does not stop being true because it left a rolling window,
    and it is never auto-resolved.
    """

    code: str
    domain: str
    sensitivity: str
    lifecycle: str
    version: str
    required_permission: str
    #: Defaults, as exact Decimal strings. An organization override is
    #: validated against these keys and never introduces new ones.
    default_settings: Mapping[str, str]
    detect: Callable[[DetectorContext], DetectorResult]
    minimum_sample: int = 1


class Detector(Protocol):  # pragma: no cover - structural typing only
    def __call__(self, context: DetectorContext) -> DetectorResult: ...


STATEFUL = "STATEFUL"
EVENT = "EVENT"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, DetectorSpec] = {}


def register(spec: DetectorSpec) -> DetectorSpec:
    """
    Add a detector to the registry.

    Refuses a duplicate code outright. Two detectors under one code would
    share a fingerprint namespace, and one would silently overwrite the
    other's outcome row for every run.
    """
    if spec.code in _REGISTRY:
        raise ValueError(f"detector {spec.code!r} is already registered")
    _REGISTRY[spec.code] = spec
    return spec


def registered(codes: Sequence[str] | None = None) -> list[DetectorSpec]:
    """The registered detectors, in a stable order, optionally narrowed."""
    wanted = set(codes) if codes else None
    return [spec for code, spec in sorted(_REGISTRY.items()) if wanted is None or code in wanted]


def get(code: str) -> DetectorSpec | None:
    return _REGISTRY.get(code)


def known_codes() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "EVENT",
    "STATEFUL",
    "Candidate",
    "Detector",
    "DetectorContext",
    "DetectorCoverage",
    "DetectorResult",
    "DetectorSkippedError",
    "DetectorSpec",
    "InsightConfidence",
    "InsightDomain",
    "InsightSensitivity",
    "InsightSeverity",
    "get",
    "known_codes",
    "register",
    "registered",
]
