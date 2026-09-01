"""
What one reader may see, and nothing beyond it.

Every screen, count, fragment, lifecycle action and drill-through goes through
`visible_insights`. One filtered queryset for all of them, because the day a
dashboard count and its list are built from two different querysets is the day
a reader learns that eleven findings exist and can only open nine — which
leaks the existence of the other two just as surely as showing them would.

## Two gates, not one

**Scope.** The organizations this actor holds `view_insight` in. An
organization-wide finding (`branch IS NULL`) requires organization-wide
authority: a branch manager reaching it would be reading a conclusion drawn
over branches they cannot see.

**Sensitivity.** Stage 1's detector is `OPERATIONAL`, so the base permission
covers it. `HR_RESTRICTED` findings are filtered out here for everyone without
the additional grant — filtered out *entirely*, never blanked. A blanked
column tells the reader a fact exists and that they are not trusted with it,
which is a different disclosure from silence and often the more interesting
one.

Out of scope is 404 at the view, per ADR-016: a 403 about another
organization's finding confirms the finding is real, and ids are sequential.
"""

from __future__ import annotations

from typing import Any

from django.db.models import OuterRef, QuerySet, Subquery

from apps.insights.models import (
    Insight,
    InsightEvent,
    InsightEventType,
    InsightObservation,
    InsightRun,
    InsightSensitivity,
)
from apps.insights.permissions import VIEW_INSIGHT
from apps.organizations.authorization import organizations_with_permission

#: Statuses a reader thinks of as "still my problem".
ACTIVE_STATUSES = (
    InsightEventType.OPENED,
    InsightEventType.ACKNOWLEDGED,
    InsightEventType.REOPENED,
)


def readable_sensitivities(user: Any) -> list[str]:
    """
    Which sensitivities this reader may see at all.

    Stage 1 ships only operational findings. The restricted tiers are listed
    here rather than assumed absent, so the later detectors that carry them
    inherit a gate that already exists instead of inventing one.
    """
    allowed: list[str] = [str(InsightSensitivity.OPERATIONAL)]
    if user.has_perm("procurement.view_supplier_cost") or user.is_superuser:
        allowed.append(str(InsightSensitivity.FINANCIAL))
    if user.is_superuser or user.has_perm("insights.view_conduct_insight"):
        allowed.append(str(InsightSensitivity.HR_RESTRICTED))
    return allowed


def _latest_event_type() -> Subquery:
    return Subquery(
        InsightEvent.objects.filter(insight=OuterRef("pk"))
        .order_by("-occurred_at", "-id")
        .values("event_type")[:1]
    )


def _latest_observation(field: str) -> Subquery:
    return Subquery(
        InsightObservation.objects.filter(insight=OuterRef("pk"))
        .order_by("-observed_at", "-id")
        .values(field)[:1]
    )


def visible_insights(user: Any) -> QuerySet[Insight]:
    """
    The findings this reader may open — the single authority for every surface.

    Status, severity, confidence and last-seen are annotated from the latest
    event and the latest observation rather than stored, so nothing on the
    screen can drift from the append-only history behind it. Annotating in one
    place is also what keeps the list off an N+1: a per-row lookup of "current
    status" over fifty findings is a hundred queries.
    """
    organizations = organizations_with_permission(user, VIEW_INSIGHT)
    queryset = Insight.objects.filter(organization__in=organizations)
    if not user.is_superuser:
        queryset = queryset.filter(sensitivity__in=readable_sensitivities(user))
    # `django-stubs` cannot reconcile a queryset annotation with an attribute
    # declared on the model for the checker's benefit, and it cannot see an
    # annotated name as filterable. Both are limitations of the stubs, not of
    # Django: the annotations below are what every surface reads, and the
    # tests exercise them against a real database.
    return (
        queryset.select_related("organization", "branch")
        .annotate(  # type: ignore[misc, no-redef]
            status=_latest_event_type(),
            latest_severity=_latest_observation("severity"),
            latest_confidence=_latest_observation("confidence"),
            latest_coverage=_latest_observation("coverage"),
            latest_title=_latest_observation("title_ar"),
            last_seen_at=_latest_observation("observed_at"),
        )
        .order_by("-last_seen_at", "-id")
    )


def active_insights(user: Any) -> QuerySet[Insight]:
    """Open, acknowledged or reopened — what a reader still has to deal with."""
    return visible_insights(user).filter(status__in=list(ACTIVE_STATUSES))  # type: ignore[misc]


def resolve_insight(user: Any, public_id: Any) -> Insight:
    """
    One finding, resolved **with** the caller.

    Never fetch-then-check: there must be no moment where an out-of-scope
    object exists in a local variable, because the next edit is always the one
    that forgets to check it.
    """
    from django.http import Http404

    insight = visible_insights(user).filter(public_id=public_id).first()
    if insight is None:
        raise Http404("insight")
    return insight


def latest_run(user: Any) -> InsightRun | None:
    """The most recent completed run in a scope this reader may see."""
    organizations = organizations_with_permission(user, VIEW_INSIGHT)
    return (
        InsightRun.objects.filter(organization__in=organizations, finished_at__isnull=False)
        .select_related("organization", "branch")
        .prefetch_related("outcomes")
        .order_by("-finished_at")
        .first()
    )


def observation_history(insight: Insight) -> QuerySet[InsightObservation]:
    """Every observation about one case, newest first."""
    return insight.observations.select_related("run").order_by("-observed_at", "-id")


def lifecycle_history(insight: Insight) -> QuerySet[InsightEvent]:
    """Every transition, oldest first — a timeline reads forwards."""
    return insight.events.select_related("actor", "run").order_by("occurred_at", "id")


def headline_counts(user: Any) -> dict[str, int]:
    """
    The dashboard's numbers, from the same filtered queryset as its list.

    Deliberately one function rather than counts computed beside each panel:
    two count expressions over one concept eventually disagree, and a count
    that exceeds what the list can show is a leak.
    """
    active = active_insights(user)
    return {
        "active": active.count(),
        "high": active.filter(latest_severity__in=["HIGH", "CRITICAL"]).count(),  # type: ignore[misc]
        "acknowledged": active.filter(status=InsightEventType.ACKNOWLEDGED).count(),  # type: ignore[misc]
        "partial_confidence": active.filter(latest_coverage="PARTIAL").count(),  # type: ignore[misc]
    }


__all__ = [
    "ACTIVE_STATUSES",
    "active_insights",
    "headline_counts",
    "latest_run",
    "lifecycle_history",
    "observation_history",
    "readable_sensitivities",
    "resolve_insight",
    "visible_insights",
]
