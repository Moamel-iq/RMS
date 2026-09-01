"""
The two screens: what needs attention, and why one finding says what it says.

Both read; neither computes. An analysis is a scan over ledgers and never runs
inside an HTTP request — the dashboard reads rows a management command wrote,
which is why it stays fast whatever the data does.

Every state-changing action is POST-only, CSRF-protected, permission- and
scope-gated, and audited. They work without htmx; htmx only saves a page load.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.core.views import FoundationViewMixin
from apps.insights.models import InsightEventType, InsightSeverity
from apps.insights.permissions import MANAGE_INSIGHT, VIEW_INSIGHT
from apps.insights.selectors import (
    active_insights,
    headline_counts,
    latest_run,
    lifecycle_history,
    observation_history,
    resolve_insight,
    visible_insights,
)
from apps.insights.services import (
    acknowledge_insight,
    dismiss_insight,
    reopen_insight,
)
from apps.organizations.authorization import (
    has_organization_permission,
    require_organization_permission,
)
from apps.users.models import User

#: Worst first. A severity-ordered list is the only ordering a person reading
#: it at seven in the morning can act on without reading all of it.
SEVERITY_RANK: dict[str, int] = {
    str(InsightSeverity.CRITICAL): 0,
    str(InsightSeverity.HIGH): 1,
    str(InsightSeverity.MEDIUM): 2,
    str(InsightSeverity.LOW): 3,
}


def _actor(request: HttpRequest) -> User:
    user: User = request.user  # type: ignore[assignment]
    return user


def _is_htmx(request: HttpRequest) -> bool:
    """
    Whether htmx made this request rather than the browser navigating.

    Defined here rather than inherited: `FoundationViewMixin` — the settings
    base these screens use — does not carry the helper that the inventory
    mixin does, and reaching across module bases for one method would couple
    two view hierarchies that have stayed independent on purpose.
    """
    return request.headers.get("HX-Request") == "true"


class InsightDashboardView(FoundationViewMixin, View):
    """
    التحليل الذكي — what the last analysis found, ranked by how much it matters.

    Shows the run's own state as prominently as its findings. "Nothing was
    found" and "nothing has been analysed since Tuesday" look identical on a
    dashboard that only lists findings, and they are opposite facts.
    """

    module_key = "insights"
    required_permission = VIEW_INSIGHT
    template_name = "insights/dashboard.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        actor = _actor(request)
        rows = list(active_insights(actor))

        severity = request.GET.get("severity", "").strip().upper()
        status = request.GET.get("status", "").strip().upper()
        if severity in SEVERITY_RANK:
            rows = [row for row in rows if row.latest_severity == severity]
        if status in InsightEventType.values:
            rows = [row for row in rows if row.status == status]
        rows.sort(
            key=lambda row: (
                SEVERITY_RANK.get(row.latest_severity, 9),
                -(row.last_seen_at.timestamp() if row.last_seen_at else 0),
            )
        )

        run = latest_run(actor)
        context = {
            "page_title": _("التحليل الذكي"),
            "insights": rows,
            "counts": headline_counts(actor),
            "latest_run": run,
            "outcomes": list(run.outcomes.all()) if run else [],
            "severities": InsightSeverity.choices,
            "statuses": [
                (value, label)
                for value, label in InsightEventType.choices
                if value in {"OPENED", "ACKNOWLEDGED", "REOPENED"}
            ],
            "selected_severity": severity,
            "selected_status": status,
            # The panel and the whole page render from one template; an HTMX
            # request gets the fragment alone so a filter can never nest a
            # second document inside the shell.
            "shell_base_template": (
                "settings/_form_fragment.html" if _is_htmx(request) else "shell.html"
            ),
        }
        return render(request, self.template_name, context)


class InsightDetailView(FoundationViewMixin, View):
    """One finding: what was seen, what it means, and everything behind it."""

    module_key = "insights"
    required_permission = VIEW_INSIGHT
    template_name = "insights/detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        actor = _actor(request)
        insight = resolve_insight(actor, self.kwargs["public_id"])
        observations = list(observation_history(insight))
        latest = observations[0] if observations else None
        if latest is None:  # pragma: no cover - an insight always has one
            raise Http404("insight has no observation")

        may_manage = has_organization_permission(actor, MANAGE_INSIGHT, insight.organization)
        return render(
            request,
            self.template_name,
            {
                "page_title": latest.title_ar,
                "insight": insight,
                "latest": latest,
                "observations": observations,
                "timeline": list(lifecycle_history(insight)),
                "status": insight.status,
                "evidence": _readable_evidence(latest.evidence),
                "may_manage": may_manage,
                "is_dismissed": insight.status == InsightEventType.DISMISSED,
            },
        )


def _readable_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Flatten the evidence into rows a person can read.

    Rendered as escaped text, never with `|safe`: evidence is data written by
    a detector today and by another detector next year, and a template that
    trusts it is one detector away from rendering whatever a source record
    happened to contain.
    """
    sections: list[dict[str, Any]] = []
    for key in ("measures", "counts", "period", "scope", "item"):
        block = evidence.get(key)
        if isinstance(block, dict) and block:
            sections.append(
                {
                    "key": key,
                    "rows": [(name, value) for name, value in block.items()],
                }
            )
    return sections


class InsightLifecycleView(FoundationViewMixin, View):
    """
    POST-only: acknowledge, dismiss, reopen.

    No GET. A state change reachable by a link is a state change a crawler,
    a prefetch or a mistyped URL can make on somebody's behalf.
    """

    module_key = "insights"
    required_permission = MANAGE_INSIGHT
    action = "acknowledge"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        actor = _actor(request)
        insight = resolve_insight(actor, self.kwargs["public_id"])
        require_organization_permission(actor, MANAGE_INSIGHT, insight.organization)
        reason = request.POST.get("reason", "")

        try:
            if self.action == "acknowledge":
                acknowledge_insight(insight=insight, actor=actor, reason=reason)
                messages.success(request, _("سُجّل اطّلاعك. تبقى الملاحظة نشطة."))
            elif self.action == "dismiss":
                dismiss_insight(insight=insight, actor=actor, reason=reason)
                messages.success(request, _("صُرف النظر عن الملاحظة. لن تُعاد تلقائياً."))
            else:
                reopen_insight(insight=insight, actor=actor, reason=reason)
                messages.success(request, _("أُعيد فتح الملاحظة."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))

        return HttpResponseRedirect(reverse("insights:detail", args=[insight.public_id]))


__all__ = [
    "InsightDashboardView",
    "InsightDetailView",
    "InsightLifecycleView",
    "visible_insights",
]
