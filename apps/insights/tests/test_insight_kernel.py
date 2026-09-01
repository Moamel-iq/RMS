"""
The kernel: identity, immutable observations, append-only lifecycle.

These tests are about the promises the *storage* makes, independent of any
detector. They use a stub detector rather than the real one, because the
questions here — can a case be duplicated, can history be rewritten, may a
partial run resolve anything — are about the orchestrator and would be
answered identically whatever the arithmetic was.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.insights.detectors import base as registry
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
)
from apps.insights.models import (
    DetectorOutcome,
    Insight,
    InsightEvent,
    InsightEventType,
    InsightObservation,
    InsightRun,
)
from apps.insights.services import (
    acknowledge_insight,
    assert_no_floats,
    dismiss_insight,
    reopen_insight,
    run_insights,
)
from apps.organizations.models import Organization
from apps.users.models import User

from .conftest import WINDOW_END, WINDOW_START

pytestmark = pytest.mark.django_db


def _latest_event(insight: Insight) -> str:
    """The current status, with the `None` case made explicit for the checker."""
    event = insight.events.order_by("-occurred_at", "-id").first()
    assert event is not None, "an insight always has at least one event"
    return str(event.event_type)


FINGERPRINT = "stub:item=1"


def _candidate(fingerprint: str = FINGERPRINT, severity: str = InsightSeverity.MEDIUM) -> Candidate:
    return Candidate(
        fingerprint=fingerprint,
        scope_key="MAIN",
        branch=None,
        severity=severity,
        confidence=InsightConfidence.HIGH,
        title_ar="ملاحظة تجريبية",
        narrative_ar="نص",
        recommendation_ar="توصية",
        evidence={"measures": {"ratio": "0"}},
        sort_key=(fingerprint,),
    )


def _stub(
    *,
    code: str,
    candidates: list[Candidate] | None = None,
    evaluated: list[str] | None = None,
    coverage: str = DetectorCoverage.COMPLETE,
    raises: Exception | None = None,
) -> DetectorSpec:
    """Register a throwaway detector with a controlled result."""

    def detect(context: DetectorContext) -> DetectorResult:
        if raises is not None:
            raise raises
        return DetectorResult(
            candidates=list(candidates or []),
            evaluated_fingerprints=list(evaluated or []),
            coverage=coverage,
        )

    spec = DetectorSpec(
        code=code,
        domain=InsightDomain.DATA_QUALITY,
        sensitivity=InsightSensitivity.OPERATIONAL,
        lifecycle=STATEFUL,
        version="test-1",
        required_permission="insights.view_insight",
        default_settings={"threshold": "0.05"},
        detect=detect,
    )
    registry.register(spec)
    return spec


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Keep each test's stub detectors out of every other test."""
    saved = dict(registry._REGISTRY)  # noqa: SLF001 - the test owns the registry
    registry._REGISTRY.clear()  # noqa: SLF001
    yield
    registry._REGISTRY.clear()  # noqa: SLF001
    registry._REGISTRY.update(saved)  # noqa: SLF001


def _run(organization: Organization, owner: User, **kwargs: Any) -> InsightRun:
    return run_insights(
        organization=organization,
        actor=owner,
        period_start=WINDOW_START,
        period_end=WINDOW_END,
        **kwargs,
    )


class TestIdentityAndObservations:
    def test_first_detection_opens_a_case_with_one_observation_and_one_event(
        self, organization: Organization, owner: User
    ) -> None:
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)

        insight = Insight.objects.get()
        assert insight.fingerprint == FINGERPRINT
        assert insight.observations.count() == 1
        assert [e.event_type for e in insight.events.all()] == [InsightEventType.OPENED]

    def test_re_detection_appends_and_never_rewrites(
        self, organization: Organization, owner: User
    ) -> None:
        """
        The whole reason observations are separate rows.

        A second run sees the same condition at a worse severity. The first
        observation must still say what it said — "it was MEDIUM in March" is
        a claim about March, not about the current row.
        """
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        first = InsightObservation.objects.get()

        registry._REGISTRY.clear()  # noqa: SLF001
        _stub(
            code="stub-a",
            candidates=[_candidate(severity=InsightSeverity.HIGH)],
            evaluated=[FINGERPRINT],
        )
        _run(organization, owner)

        assert Insight.objects.count() == 1, "the same condition is the same case"
        assert InsightObservation.objects.count() == 2
        first.refresh_from_db()
        assert first.severity == InsightSeverity.MEDIUM, "history was not rewritten"

    def test_the_same_run_cannot_observe_a_case_twice(
        self, organization: Organization, owner: User
    ) -> None:
        """Storage idempotency, enforced by the database and not by a flag."""
        spec = _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        run = _run(organization, owner)
        observation = InsightObservation.objects.get()

        with pytest.raises(IntegrityError), transaction.atomic():
            InsightObservation.objects.create(
                insight=observation.insight,
                run=run,
                period_start=run.period_start,
                period_end=run.period_end,
                detector_version=spec.version,
                severity=InsightSeverity.LOW,
                confidence=InsightConfidence.LOW,
                coverage=DetectorCoverage.COMPLETE,
                title_ar="ثانية",
                narrative_ar="ثانية",
                recommendation_ar="ثانية",
                observed_at=run.started_at,
            )

    def test_two_organizations_may_share_a_fingerprint(
        self,
        organization: Organization,
        other_organization: Organization,
        owner: User,
        rival_owner: User,
    ) -> None:
        """Identity is unique per organization, not globally."""
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        _run(other_organization, rival_owner)
        assert Insight.objects.count() == 2


class TestImmutability:
    def test_an_observation_cannot_be_updated(
        self, organization: Organization, owner: User
    ) -> None:
        """The database refuses it, not a service that could be bypassed."""
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        observation = InsightObservation.objects.get()

        with pytest.raises(IntegrityError), transaction.atomic():
            InsightObservation.objects.filter(pk=observation.pk).update(
                severity=InsightSeverity.LOW
            )

    def test_a_lifecycle_event_cannot_be_deleted(
        self, organization: Organization, owner: User
    ) -> None:
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        event = InsightEvent.objects.get()

        with pytest.raises(IntegrityError), transaction.atomic():
            InsightEvent.objects.filter(pk=event.pk).delete()

    def test_a_run_may_only_record_its_completion(
        self, organization: Organization, owner: User
    ) -> None:
        """
        The allowlist trigger. `finished_at` may change; the window may not.

        A run whose period could be edited afterwards would let somebody
        restate what every observation in it was about.
        """
        _stub(code="stub-a", evaluated=[])
        run = _run(organization, owner)

        with pytest.raises(IntegrityError), transaction.atomic():
            InsightRun.objects.filter(pk=run.pk).update(period_start=datetime.date(2020, 1, 1))


class TestAutoResolution:
    def test_a_complete_clean_run_resolves_a_case_it_evaluated(
        self, organization: Organization, owner: User
    ) -> None:
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)

        registry._REGISTRY.clear()  # noqa: SLF001
        _stub(code="stub-a", candidates=[], evaluated=[FINGERPRINT])
        _run(organization, owner)

        insight = Insight.objects.get()
        assert _latest_event(insight) == (InsightEventType.RESOLVED)

    def test_a_partial_run_resolves_nothing(self, organization: Organization, owner: User) -> None:
        """
        The rule that makes silence safe.

        A run that saw half the population and found nothing has said nothing
        about the half it could not see. Closing a case on that would make
        findings vanish exactly when the data got worse.
        """
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)

        registry._REGISTRY.clear()  # noqa: SLF001
        _stub(
            code="stub-a",
            candidates=[],
            evaluated=[FINGERPRINT],
            coverage=DetectorCoverage.PARTIAL,
        )
        _run(organization, owner)

        assert _latest_event(Insight.objects.get()) == InsightEventType.OPENED

    def test_a_case_the_run_never_evaluated_is_not_resolved(
        self, organization: Organization, owner: User
    ) -> None:
        """Absence from the candidate list is not evidence of absence."""
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)

        registry._REGISTRY.clear()  # noqa: SLF001
        _stub(code="stub-a", candidates=[], evaluated=["stub:item=999"])
        _run(organization, owner)

        assert _latest_event(Insight.objects.get()) == InsightEventType.OPENED

    def test_a_dismissed_case_is_not_resolved_and_not_reopened_by_a_run(
        self, organization: Organization, owner: User
    ) -> None:
        """
        A dismissal is a human decision, and only a human undoes it.

        Neither a clean run (which would "resolve" it) nor a recurrence
        (which would reopen it) may overrule the person who dismissed it.
        """
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        insight = Insight.objects.get()
        dismiss_insight(insight=insight, actor=owner, reason="ستُدخل الصرفيات")

        registry._REGISTRY.clear()  # noqa: SLF001
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)

        assert _latest_event(insight) == (InsightEventType.DISMISSED)

    def test_a_resolved_case_reopens_on_recurrence(
        self, organization: Organization, owner: User
    ) -> None:
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        registry._REGISTRY.clear()  # noqa: SLF001
        _stub(code="stub-a", candidates=[], evaluated=[FINGERPRINT])
        _run(organization, owner)
        registry._REGISTRY.clear()  # noqa: SLF001
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)

        insight = Insight.objects.get()
        assert _latest_event(insight) == (InsightEventType.REOPENED)
        assert Insight.objects.count() == 1, "a recurrence is the same case"


class TestOutcomes:
    def test_a_failing_detector_is_isolated_and_recorded(
        self, organization: Organization, owner: User
    ) -> None:
        """One detector's exception must not erase a sibling's success."""
        _stub(code="stub-ok", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _stub(code="stub-bad", raises=RuntimeError("boom"))
        run = _run(organization, owner)

        outcomes = {o.detector_code: o for o in run.outcomes.all()}
        assert outcomes["stub-ok"].outcome == DetectorOutcome.SUCCEEDED
        assert outcomes["stub-bad"].outcome == DetectorOutcome.FAILED
        assert Insight.objects.count() == 1

    def test_a_failure_never_leaks_its_traceback(
        self, organization: Organization, owner: User
    ) -> None:
        _stub(code="stub-bad", raises=RuntimeError("secret path /etc/passwd"))
        run = _run(organization, owner)
        outcome = run.outcomes.get()
        assert "secret" not in outcome.error_summary
        assert "/etc" not in outcome.error_summary

    def test_a_skipped_prerequisite_is_not_a_failure(
        self, organization: Organization, owner: User
    ) -> None:
        _stub(code="stub-skip", raises=DetectorSkippedError("no_source", "لا مصدر"))
        run = _run(organization, owner)
        outcome = run.outcomes.get()
        assert outcome.outcome == DetectorOutcome.SKIPPED
        assert outcome.error_code == "no_source"

    def test_an_empty_successful_run_is_recorded_as_success(
        self, organization: Organization, owner: User
    ) -> None:
        """Finding nothing is an answer. It is the only one that may resolve."""
        _stub(code="stub-a", candidates=[], evaluated=[FINGERPRINT])
        run = _run(organization, owner)
        assert run.outcomes.get().outcome == DetectorOutcome.SUCCEEDED
        assert run.outcomes.get().candidate_count == 0


class TestLifecycleActions:
    def test_acknowledging_keeps_the_case_active(
        self, organization: Organization, owner: User
    ) -> None:
        """Acknowledgement is not resolution: later runs keep observing."""
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        insight = Insight.objects.get()
        acknowledge_insight(insight=insight, actor=owner)

        registry._REGISTRY.clear()  # noqa: SLF001
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        assert insight.observations.count() == 2

    def test_dismissal_requires_a_reason(self, organization: Organization, owner: User) -> None:
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        with pytest.raises(ValidationError):
            dismiss_insight(insight=Insight.objects.get(), actor=owner, reason="  ")

    def test_reopening_requires_a_reason(self, organization: Organization, owner: User) -> None:
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        insight = Insight.objects.get()
        dismiss_insight(insight=insight, actor=owner, reason="مؤقتاً")
        with pytest.raises(ValidationError):
            reopen_insight(insight=insight, actor=owner, reason="")

    def test_an_explicit_reopen_lifts_a_dismissal(
        self, organization: Organization, owner: User
    ) -> None:
        _stub(code="stub-a", candidates=[_candidate()], evaluated=[FINGERPRINT])
        _run(organization, owner)
        insight = Insight.objects.get()
        dismiss_insight(insight=insight, actor=owner, reason="مؤقتاً")
        reopen_insight(insight=insight, actor=owner, reason="عاد الوضع")
        assert _latest_event(insight) == (InsightEventType.REOPENED)


class TestEvidenceHygiene:
    def test_a_float_anywhere_in_the_evidence_is_refused(self) -> None:
        """
        Recursive, because the one that hurts is always nested.

        A float here would be stored, re-read, and silently disagree with the
        Decimal it came from.
        """
        with pytest.raises(ValidationError):
            assert_no_floats({"measures": {"ratio": 0.05}})
        with pytest.raises(ValidationError):
            assert_no_floats({"rows": [{"deep": [1, 2, 3.5]}]})

    def test_exact_strings_and_integers_are_accepted(
        self, zero_ratio_evidence: dict[str, Any]
    ) -> None:
        assert_no_floats(zero_ratio_evidence)

    def test_a_boolean_is_not_mistaken_for_a_float(self) -> None:
        assert_no_floats({"is_final": True})


class TestRunValidation:
    def test_a_backwards_window_is_refused(self, organization: Organization, owner: User) -> None:
        with pytest.raises(ValidationError):
            run_insights(
                organization=organization,
                actor=owner,
                period_start=WINDOW_END,
                period_end=WINDOW_START,
            )

    def test_an_unknown_detector_code_is_refused(
        self, organization: Organization, owner: User
    ) -> None:
        _stub(code="stub-a", evaluated=[])
        with pytest.raises(ValidationError):
            _run(organization, owner, detector_codes=["not-a-detector"])
