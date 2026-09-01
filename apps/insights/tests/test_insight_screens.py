"""
The screens, and the boundaries they must not leak across.

Most of these are authorization tests, and they are written from the position
that the interesting failure is never "a stranger saw everything". It is the
quieter one: a count that includes a finding the list cannot show, a 403 that
confirms a record exists, a filter that widens rather than narrows. Each of
those tells a reader something true about data they may not have.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from django.urls import reverse

from apps.insights.detectors import base as registry
from apps.insights.detectors.base import (
    STATEFUL,
    Candidate,
    DetectorContext,
    DetectorCoverage,
    DetectorResult,
    DetectorSpec,
    InsightConfidence,
    InsightDomain,
    InsightSensitivity,
    InsightSeverity,
)
from apps.insights.models import Insight, InsightEvent, InsightEventType
from apps.organizations.models import Organization
from apps.users.models import User

from .conftest import WINDOW_END, WINDOW_START

pytestmark = pytest.mark.django_db


def _latest_event(insight: Insight) -> str:
    """The current status, with the `None` case made explicit for the checker."""
    event = insight.events.order_by("-occurred_at", "-id").first()
    assert event is not None, "an insight always has at least one event"
    return str(event.event_type)


def _register_stub(
    code: str = "screen-stub", sensitivity: str = InsightSensitivity.OPERATIONAL
) -> DetectorSpec:
    def detect(context: DetectorContext) -> DetectorResult:
        return DetectorResult(
            candidates=[
                Candidate(
                    fingerprint="screen:item=1",
                    scope_key="MAIN",
                    branch=None,
                    severity=InsightSeverity.HIGH,
                    confidence=InsightConfidence.HIGH,
                    title_ar="ملاحظة على الشاشة",
                    narrative_ar="نص الملاحظة",
                    recommendation_ar="التوصية",
                    evidence={"measures": {"ratio": "0"}, "counts": {"rows": 2}},
                )
            ],
            evaluated_fingerprints=["screen:item=1"],
            coverage=DetectorCoverage.COMPLETE,
        )

    return registry.register(
        DetectorSpec(
            code=code,
            domain=InsightDomain.DATA_QUALITY,
            sensitivity=sensitivity,
            lifecycle=STATEFUL,
            version="test-1",
            required_permission="insights.view_insight",
            default_settings={},
            detect=detect,
        )
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    saved = dict(registry._REGISTRY)  # noqa: SLF001
    registry._REGISTRY.clear()  # noqa: SLF001
    yield
    registry._REGISTRY.clear()  # noqa: SLF001
    registry._REGISTRY.update(saved)  # noqa: SLF001


@pytest.fixture
def finding(organization: Organization, owner: User) -> Insight:
    from apps.insights.services import run_insights

    _register_stub()
    run_insights(
        organization=organization,
        actor=owner,
        period_start=WINDOW_START,
        period_end=WINDOW_END,
    )
    return Insight.objects.get()


class TestDashboardAccess:
    def test_a_reader_sees_the_findings(
        self, client_for: Any, owner: User, finding: Insight
    ) -> None:
        response = client_for(owner).get(reverse("insights:dashboard"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "ملاحظة على الشاشة" in body
        assert "آخر تحليل" in body

    def test_without_the_permission_the_screen_is_refused(
        self, client_for: Any, storekeeper: User, finding: Insight
    ) -> None:
        response = client_for(storekeeper).get(reverse("insights:dashboard"))
        assert response.status_code in (302, 403)

    def test_another_organization_sees_nothing(
        self, client_for: Any, rival_owner: User, finding: Insight
    ) -> None:
        """
        Not "sees it greyed out" — sees nothing, and the count agrees.

        A count that exceeded the list would disclose the finding's existence
        as surely as showing it.
        """
        response = client_for(rival_owner).get(reverse("insights:dashboard"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "ملاحظة على الشاشة" not in body

    def test_the_detail_of_another_organization_is_404_not_403(
        self, client_for: Any, rival_owner: User, finding: Insight
    ) -> None:
        """403 would confirm the finding is real. Absent and foreign must match."""
        response = client_for(rival_owner).get(reverse("insights:detail", args=[finding.public_id]))
        assert response.status_code == 404

    def test_the_counts_come_from_the_same_filtered_queryset(
        self, client_for: Any, rival_owner: User, owner: User, finding: Insight
    ) -> None:
        from apps.insights.selectors import headline_counts

        assert headline_counts(owner)["active"] == 1
        assert headline_counts(rival_owner)["active"] == 0


class TestDetailAndEvidence:
    def test_the_detail_shows_the_figures_behind_the_claim(
        self, client_for: Any, owner: User, finding: Insight
    ) -> None:
        response = client_for(owner).get(reverse("insights:detail", args=[finding.public_id]))
        assert response.status_code == 200
        body = response.content.decode()
        assert "الأدلة والاحتساب" in body
        assert "التوصية" in body
        assert "المسار الزمني" in body

    def test_evidence_is_rendered_as_text_not_markup(
        self, client_for: Any, owner: User, finding: Insight
    ) -> None:
        """
        Evidence is data a detector wrote, and next year another detector will
        write it. The template must never use `|safe` on it: one detector that
        copied a source record's free-text field would then be rendering
        whatever somebody typed into an item name.
        """
        source = Path("templates/insights/detail.html").read_text(encoding="utf-8")
        # Comments stripped first. The template's own documentation says the
        # words `|safe` in explaining why it must never appear, and a check
        # that could not tell the warning from the offence would fail on a
        # correct file — which is how a test teaches people to delete it.
        code = re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)
        assert "|safe" not in code
        assert "autoescape off" not in code

        response = client_for(owner).get(reverse("insights:detail", args=[finding.public_id]))
        assert response.status_code == 200


class TestLifecycleOverHttp:
    def test_acknowledge_requires_post(
        self, client_for: Any, owner: User, finding: Insight
    ) -> None:
        """A state change reachable by GET is one a prefetch can make."""
        response = client_for(owner).get(reverse("insights:acknowledge", args=[finding.public_id]))
        assert response.status_code == 405

    def test_acknowledge_records_an_event(
        self, client_for: Any, owner: User, finding: Insight
    ) -> None:
        client_for(owner).post(reverse("insights:acknowledge", args=[finding.public_id]))
        assert _latest_event(finding) == (InsightEventType.ACKNOWLEDGED)

    def test_dismiss_without_a_reason_changes_nothing(
        self, client_for: Any, owner: User, finding: Insight
    ) -> None:
        client_for(owner).post(
            reverse("insights:dismiss", args=[finding.public_id]), {"reason": ""}
        )
        assert not InsightEvent.objects.filter(
            insight=finding, event_type=InsightEventType.DISMISSED
        ).exists()

    def test_a_reader_without_manage_cannot_act(
        self, client_for: Any, accountant: User, finding: Insight
    ) -> None:
        """
        Reading and deciding are separate grants.

        The accountant sees every finding and may not acknowledge one — the
        button is absent *and* the POST is refused, because a hidden button is
        not a permission check.
        """
        client = client_for(accountant)
        assert client.get(reverse("insights:dashboard")).status_code == 200

        response = client.post(reverse("insights:acknowledge", args=[finding.public_id]))
        assert response.status_code in (302, 403)
        assert not InsightEvent.objects.filter(
            insight=finding, event_type=InsightEventType.ACKNOWLEDGED
        ).exists()

    def test_the_manage_buttons_are_hidden_from_a_reader(
        self, client_for: Any, accountant: User, finding: Insight
    ) -> None:
        body = (
            client_for(accountant)
            .get(reverse("insights:detail", args=[finding.public_id]))
            .content.decode()
        )
        assert "سجّل اطّلاعي" not in body


class TestHtmxParity:
    def test_the_fragment_carries_no_second_document(
        self, client_for: Any, owner: User, finding: Insight
    ) -> None:
        response = client_for(owner).get(reverse("insights:dashboard"), HTTP_HX_REQUEST="true")
        body = response.content.decode()
        assert response.status_code == 200
        assert "<!doctype html>" not in body.lower()
        assert "ملاحظة على الشاشة" in body

    def test_the_full_page_and_the_fragment_show_the_same_findings(
        self, client_for: Any, owner: User, finding: Insight
    ) -> None:
        client = client_for(owner)
        full = client.get(reverse("insights:dashboard")).content.decode()
        fragment = client.get(
            reverse("insights:dashboard"), HTTP_HX_REQUEST="true"
        ).content.decode()
        assert ("ملاحظة على الشاشة" in full) == ("ملاحظة على الشاشة" in fragment)


class TestNavigation:
    def test_the_module_is_visible_to_a_reader(self, client_for: Any, owner: User) -> None:
        from apps.core.navigation_access import visible_modules_for

        keys = {module.key for module in visible_modules_for(owner)}
        assert "insights" in keys

    def test_the_module_is_hidden_from_someone_without_the_permission(
        self, storekeeper: User
    ) -> None:
        from apps.core.navigation_access import visible_modules_for

        modules = {m.key: m for m in visible_modules_for(storekeeper)}
        assert "insights" not in modules
