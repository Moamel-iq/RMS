"""
Orchestration and lifecycle: turning candidates into cases, and cases into
decisions.

Detectors do arithmetic. This module does everything that touches the
database, and it is where the three hard problems live.

## 1. Deduplication is the database's job

Two runs racing over one organization will both find the same condition and
both try to open it. The service does not check-then-insert — that is the
race, not the fix. It inserts and lets the unique index arbitrate, catching
the loser's `IntegrityError` and refetching the winner's row. The index is the
authority; the Python is a convenience.

## 2. Determinism and idempotency are different promises

A detector is *deterministic*: same inputs, same candidates. Storage is
*idempotent*: reprocessing one run cannot double its observations. They are
separate because a new cutoff legitimately produces a new run with different
numbers, while replaying an existing run must change nothing. The unique
constraint on `(insight, run)` is what makes the second true regardless of how
many times a writer is called.

## 3. Only a run that looked may close a case

Auto-resolution is the most dangerous thing here, because it makes a finding
disappear. Five conditions must all hold, and the fifth is the one that is
easy to forget: the fingerprint must appear in the detector's **explicitly
evaluated** list. A run that skipped an item, failed halfway, or saw only part
of the population has said nothing about the cases it did not reach — and
silence must never be read as "resolved".
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event
from apps.insights.detectors import base as registry
from apps.insights.detectors.base import (
    Candidate,
    DetectorContext,
    DetectorResult,
    DetectorSkippedError,
    DetectorSpec,
)
from apps.insights.locks import lock_insights_run
from apps.insights.models import (
    DetectorCoverage,
    DetectorOutcome,
    DetectorSetting,
    Insight,
    InsightDetectorOutcome,
    InsightEvent,
    InsightEventType,
    InsightObservation,
    InsightRun,
    InsightSeverity,
    RunTrigger,
)

logger = logging.getLogger(__name__)

#: The default analysis window: four completed weeks. Completed, because a
#: partial current day compared against whole past days makes every trend look
#: like a collapse on the morning it is read.
DEFAULT_WINDOW_DAYS = 28

ACTIVE_STATUSES = frozenset(
    {InsightEventType.OPENED, InsightEventType.ACKNOWLEDGED, InsightEventType.REOPENED}
)


# ---------------------------------------------------------------------------
# Evidence hygiene
# ---------------------------------------------------------------------------


def assert_no_floats(payload: Any, *, path: str = "evidence") -> None:
    """
    Refuse a float anywhere in an evidence tree, at any depth.

    Recursive rather than top-level because the one that hurts is always
    nested — a ratio inside a measures dict inside a list. A float here would
    be stored, re-read, and silently disagree with the Decimal it came from.
    """
    if isinstance(payload, bool):
        return
    if isinstance(payload, float):
        raise ValidationError(
            _("الأدلة تحتوي عدداً عشرياً ثنائياً في %(path)s؛ استعمل نصاً دقيقاً."),
            code="evidence_contains_float",
            params={"path": path},
        )
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert_no_floats(value, path=f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_no_floats(value, path=f"{path}[{index}]")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def effective_settings(*, organization: Any, spec: DetectorSpec) -> tuple[dict[str, Decimal], int]:
    """
    The thresholds this detector runs under, and the version they came from.

    Defaults live in code; an organization override is the highest-versioned
    `DetectorSetting` row. The version travels with every observation so a
    reader can tell whether a finding that vanished did so because the world
    changed or because somebody moved the line.

    An override may only replace a **declared** key. A payload naming an
    unknown setting is a typo that would otherwise be silently ignored, and a
    silently ignored threshold is the worst kind: it looks configured.
    """
    values = {key: Decimal(value) for key, value in spec.default_settings.items()}
    version = 0
    row = (
        DetectorSetting.objects.filter(organization=organization, detector_code=spec.code)
        .order_by("-version")
        .first()
    )
    if row is not None:
        for key, raw in (row.payload or {}).items():
            if key not in values:
                raise ValidationError(
                    _("إعداد غير معروف للكاشف: %(key)s"),
                    code="unknown_detector_setting",
                    params={"key": key},
                )
            try:
                values[key] = Decimal(str(raw))
            except InvalidOperation as error:
                raise ValidationError(
                    _("قيمة غير رقمية للإعداد %(key)s"),
                    code="invalid_detector_setting",
                    params={"key": key},
                ) from error
        version = row.version
    return values, version


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def default_window(*, today: datetime.date | None = None) -> tuple[datetime.date, datetime.date]:
    """
    The last `DEFAULT_WINDOW_DAYS` **completed** business days.

    The end is exclusive and is today, so today itself — still being lived —
    is outside the window. Including a half-finished day would make every
    morning's run report a consumption collapse that is only the clock.
    """
    end = today or timezone.localdate()
    return end - datetime.timedelta(days=DEFAULT_WINDOW_DAYS), end


@transaction.atomic
def run_insights(
    *,
    organization: Any,
    actor: Any,
    branch: Any | None = None,
    period_start: datetime.date | None = None,
    period_end: datetime.date | None = None,
    detector_codes: list[str] | None = None,
    trigger: str = RunTrigger.MANUAL,
) -> InsightRun:
    """
    Execute the registered detectors once, and record everything that happened.

    One transaction and one organization-wide lock. Scheduled and manual
    execution both arrive here — two entry points into analytics would mean two
    definitions of a window, and the day they disagree is the day two findings
    about the same week cite different numbers.
    """
    if period_start is None or period_end is None:
        period_start, period_end = default_window()
    if period_end <= period_start:
        raise ValidationError(_("نهاية الفترة يجب أن تكون بعد بدايتها."), code="invalid_period")
    if branch is not None and branch.organization_id != organization.pk:
        raise ValidationError(_("الفرع يتبع مؤسسة أخرى."), code="branch_organization_mismatch")

    lock_insights_run(organization.pk)

    specs = registry.registered(detector_codes)
    if detector_codes:
        unknown = sorted(set(detector_codes) - {spec.code for spec in specs})
        if unknown:
            raise ValidationError(
                _("كاشف غير معروف: %(codes)s"),
                code="unknown_detector",
                params={"codes": "، ".join(unknown)},
            )

    started = timezone.now()
    cutoffs = _source_cutoffs()
    run = InsightRun.objects.create(
        organization=organization,
        branch=branch,
        requested_detectors=[spec.code for spec in specs],
        period_start=period_start,
        period_end=period_end,
        trigger=trigger,
        actor=actor if trigger == RunTrigger.MANUAL else None,
        source_cutoffs=cutoffs,
        started_at=started,
    )

    for spec in specs:
        _run_one_detector(
            run=run,
            spec=spec,
            organization=organization,
            branch=branch,
            actor=actor,
            period_start=period_start,
            period_end=period_end,
            cutoffs=cutoffs,
        )

    run.finished_at = timezone.now()
    run.save(update_fields=["finished_at", "updated_at"])
    return run


def _source_cutoffs() -> dict[str, str]:
    """
    The high-watermark of each authoritative ledger this analysis reads.

    Recorded so two detectors in one run cannot silently combine different
    views of the same ledger, and so a reader can ask what the data looked
    like when a claim was made.
    """
    from apps.inventory.models import StockMovement

    latest = (
        StockMovement.objects.order_by("-posted_sequence")
        .values_list("posted_sequence", flat=True)
        .first()
    )
    return {
        "inventory.stock_movement.posted_sequence": str(latest or 0),
        "read_at": timezone.now().isoformat(),
    }


def _run_one_detector(
    *,
    run: InsightRun,
    spec: DetectorSpec,
    organization: Any,
    branch: Any | None,
    actor: Any,
    period_start: datetime.date,
    period_end: datetime.date,
    cutoffs: dict[str, str],
) -> InsightDetectorOutcome:
    """
    Execute one detector and persist its outcome, whatever happens.

    A failure here is isolated: it is recorded against this detector and the
    run continues. One detector's exception must never erase its siblings'
    recorded successes, which is why the outcome row is written in every
    branch of this function rather than once at the end.
    """
    settings_values, settings_version = effective_settings(organization=organization, spec=spec)
    context = DetectorContext(
        organization=organization,
        branch=branch,
        period_start=period_start,
        period_end=period_end,
        settings=settings_values,
        settings_version=settings_version,
        source_cutoffs=cutoffs,
        actor=actor,
    )

    try:
        result = spec.detect(context)
    except DetectorSkippedError as skipped:
        return InsightDetectorOutcome.objects.create(
            run=run,
            detector_code=spec.code,
            outcome=DetectorOutcome.SKIPPED,
            coverage=DetectorCoverage.INSUFFICIENT,
            error_code=skipped.code,
            error_summary=skipped.summary[:300],
            finished_at=timezone.now(),
        )
    except Exception as error:  # noqa: BLE001 - isolation is the whole point
        # The technical detail goes to the log, never to a screen. A traceback
        # in the UI is an information leak wearing a diagnostic hat.
        logger.exception("insights detector %s failed", spec.code)
        return InsightDetectorOutcome.objects.create(
            run=run,
            detector_code=spec.code,
            outcome=DetectorOutcome.FAILED,
            coverage=DetectorCoverage.INSUFFICIENT,
            error_code=type(error).__name__,
            error_summary=_("تعذّر تنفيذ الكاشف. راجع السجل الفني.")[:300],
            finished_at=timezone.now(),
        )

    for candidate in result.candidates:
        _persist_candidate(
            run=run,
            spec=spec,
            candidate=candidate,
            result=result,
            organization=organization,
            settings_version=settings_version,
        )

    _auto_resolve(run=run, spec=spec, result=result, organization=organization)

    return InsightDetectorOutcome.objects.create(
        run=run,
        detector_code=spec.code,
        outcome=DetectorOutcome.SUCCEEDED,
        coverage=result.coverage,
        evaluated_fingerprints=result.evaluated_fingerprints,
        evaluated_scope_count=result.evaluated_scope_count,
        candidate_count=len(result.candidates),
        notes=result.notes,
        finished_at=timezone.now(),
    )


def _persist_candidate(
    *,
    run: InsightRun,
    spec: DetectorSpec,
    candidate: Candidate,
    result: DetectorResult,
    organization: Any,
    settings_version: int,
) -> InsightObservation:
    """Open or continue one case, and record what this run saw about it."""
    assert_no_floats(candidate.evidence)

    insight, created = _identity(organization=organization, spec=spec, candidate=candidate, run=run)
    if created:
        InsightEvent.objects.create(
            insight=insight,
            event_type=InsightEventType.OPENED,
            run=run,
            occurred_at=timezone.now(),
        )
    elif _status_of(insight) == InsightEventType.RESOLVED:
        # It came back. A resolved case that recurs is reopened rather than
        # duplicated: the history of the first occurrence is the context for
        # the second.
        InsightEvent.objects.create(
            insight=insight,
            event_type=InsightEventType.REOPENED,
            run=run,
            reason=str(_("عاد الشرط للظهور في تحليل لاحق.")),
            occurred_at=timezone.now(),
        )

    observation, _made = InsightObservation.objects.get_or_create(
        insight=insight,
        run=run,
        defaults={
            "period_start": run.period_start,
            "period_end": run.period_end,
            "source_cutoffs": run.source_cutoffs,
            "detector_version": spec.version,
            "settings_version": settings_version,
            "severity": candidate.severity,
            "confidence": candidate.confidence,
            "coverage": result.coverage,
            "title_ar": candidate.title_ar,
            "narrative_ar": candidate.narrative_ar,
            "recommendation_ar": candidate.recommendation_ar,
            "evidence": candidate.evidence,
            "observed_at": timezone.now(),
        },
    )

    if candidate.severity in (InsightSeverity.HIGH, InsightSeverity.CRITICAL):
        _bridge_to_inbox(insight=insight, candidate=candidate, spec=spec)
    return observation


def _identity(
    *, organization: Any, spec: DetectorSpec, candidate: Candidate, run: InsightRun
) -> tuple[Insight, bool]:
    """
    The case this candidate belongs to, creating it exactly once.

    Insert-then-recover rather than check-then-insert: two concurrent runs
    would both pass a check and both insert. The unique index is the arbiter.
    """
    lookup = {
        "organization": organization,
        "detector_code": spec.code,
        "fingerprint": candidate.fingerprint,
    }
    existing = Insight.objects.filter(**lookup).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            return (
                Insight.objects.create(
                    **lookup,
                    branch=candidate.branch,
                    scope_key=candidate.scope_key,
                    domain=spec.domain,
                    sensitivity=spec.sensitivity,
                    first_run=run,
                ),
                True,
            )
    except IntegrityError:
        # The other run won the race. Its row is the one true identity.
        return Insight.objects.get(**lookup), False


def _auto_resolve(
    *, run: InsightRun, spec: DetectorSpec, result: DetectorResult, organization: Any
) -> None:
    """
    Close the cases this run looked at and found clean. Nothing else.

    Five conditions, all required. The last two are the ones that make this
    safe: the fingerprint must have been **explicitly evaluated**, and the
    coverage must be COMPLETE. A partial run has said nothing about what it
    could not see, and reading its silence as "resolved" would make findings
    vanish exactly when the data got worse.
    """
    if spec.lifecycle != registry.STATEFUL:
        return
    if result.coverage != DetectorCoverage.COMPLETE:
        return
    evaluated = set(result.evaluated_fingerprints)
    if not evaluated:
        return
    still_present = {candidate.fingerprint for candidate in result.candidates}
    gone = evaluated - still_present
    if not gone:
        return

    for insight in Insight.objects.filter(
        organization=organization, detector_code=spec.code, fingerprint__in=gone
    ):
        status = _status_of(insight)
        # A dismissed case stays dismissed. Somebody decided it was not worth
        # tracking, and a clean run is not a reason to overrule them.
        if status not in ACTIVE_STATUSES:
            continue
        InsightEvent.objects.create(
            insight=insight,
            event_type=InsightEventType.RESOLVED,
            run=run,
            reason=str(_("لم يعد الشرط قائماً في تحليل كامل التغطية.")),
            occurred_at=timezone.now(),
        )


def _bridge_to_inbox(*, insight: Insight, candidate: Candidate, spec: DetectorSpec) -> None:
    """
    Put an urgent finding in the task inbox people already read.

    Targets the stable `Insight`, never the narrative, so the existing
    deduplication recognises a recurrence as the same condition. Sensitivity
    is carried across unchanged — the inbox already hides `HR_RESTRICTED`
    tasks from everyone but the owner and accounting manager, and a second
    notification system with its own idea of who may read what is how a
    restricted finding eventually reaches the wrong person.
    """
    from apps.core.automation import open_exception
    from apps.core.models import AutomationSeverity

    severity = (
        AutomationSeverity.CRITICAL
        if candidate.severity == InsightSeverity.CRITICAL
        else AutomationSeverity.HIGH
    )
    open_exception(
        organization=insight.organization,
        branch=insight.branch,
        code=f"insight:{spec.code}",
        target=insight,
        severity=severity,
        is_blocking=False,
        sensitivity=spec.sensitivity,
        title=candidate.title_ar[:240],
        summary=candidate.recommendation_ar,
        details={"fingerprint": candidate.fingerprint, "scope": candidate.scope_key},
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _status_of(insight: Insight) -> str:
    """The current status: the latest event's type, derived and never stored."""
    latest = insight.events.order_by("-occurred_at", "-id").first()
    return latest.event_type if latest else InsightEventType.OPENED


@transaction.atomic
def acknowledge_insight(*, insight: Insight, actor: Any, reason: str = "") -> InsightEvent:
    """
    Record that somebody has seen it. The case stays active.

    Acknowledgement is not resolution and does not suppress anything: later
    runs keep appending observations, so a condition that gets worse after
    somebody looked at it still says so.
    """
    event = InsightEvent.objects.create(
        insight=insight,
        event_type=InsightEventType.ACKNOWLEDGED,
        actor=actor,
        reason=reason.strip(),
        occurred_at=timezone.now(),
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=insight,
        organization=insight.organization,
        branch=insight.branch,
        reason=reason.strip() or str(_("اطّلاع على ملاحظة تحليلية")),
        metadata={"event": InsightEventType.ACKNOWLEDGED, "fingerprint": insight.fingerprint},
    )
    return event


@transaction.atomic
def dismiss_insight(*, insight: Insight, actor: Any, reason: str) -> InsightEvent:
    """
    Decide this one is not worth tracking, and say why.

    The reason is required and the constraint enforces it: a dismissal
    suppresses automatic reopening, so an unexplained one is a permanent
    silence nobody can review.
    """
    if not reason.strip():
        raise ValidationError(_("صرف النظر يحتاج سبباً."), code="reason_required")
    event = InsightEvent.objects.create(
        insight=insight,
        event_type=InsightEventType.DISMISSED,
        actor=actor,
        reason=reason.strip(),
        occurred_at=timezone.now(),
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=insight,
        organization=insight.organization,
        branch=insight.branch,
        reason=reason.strip(),
        metadata={"event": InsightEventType.DISMISSED, "fingerprint": insight.fingerprint},
    )
    return event


@transaction.atomic
def reopen_insight(*, insight: Insight, actor: Any, reason: str) -> InsightEvent:
    """
    Undo a dismissal, deliberately and by a person.

    The only way out of `DISMISSED`. No run may do it — that is what makes a
    dismissal mean something.
    """
    if not reason.strip():
        raise ValidationError(_("إعادة الفتح تحتاج سبباً."), code="reason_required")
    event = InsightEvent.objects.create(
        insight=insight,
        event_type=InsightEventType.REOPENED,
        actor=actor,
        reason=reason.strip(),
        occurred_at=timezone.now(),
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=insight,
        organization=insight.organization,
        branch=insight.branch,
        reason=reason.strip(),
        metadata={"event": InsightEventType.REOPENED, "fingerprint": insight.fingerprint},
    )
    return event


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "acknowledge_insight",
    "assert_no_floats",
    "default_window",
    "dismiss_insight",
    "effective_settings",
    "reopen_insight",
    "run_insights",
]
