"""
لوحة المبيعات — the module's landing page, and the eight cards under it.

**Two views, not nine.** `SalesDashboardView` renders the shell and the
headline; `SalesDashboardCardView` renders exactly one card, chosen by slug from
`dashboard.CARDS`. Nine view classes would be nine places to check the wrong
permission, and the registry is what keeps the route, the template and the
fetch in step.

## Why the cards load separately

The headline is a handful of aggregates over indexed columns and it renders with
the page. Everything else — the reconciliation status, which rebuilds one
`DailyReconciliation` per branch per day, and the cost card, which walks the
snapshot evidence — is real work. Each card is fetched by its own `hx-get` on
load, so a slow card delays itself and nothing else, and a card that fails
leaves the rest of the screen standing.

## Cost is omitted, never blanked

`view_sales_cost` decides whether the cost card exists at all. Without it the
card is absent from `visible_cards`, its route answers 403, and no placeholder,
no dash and no empty figure appears anywhere — the rule inventory applies to
valuation and procurement to supplier cost. A blank card would tell the reader a
number exists and that they are not trusted with it, which is a different
statement from the one intended.

The permission is answered from the **organization**, not from the rows on the
page: a window with no sales must not silently grant the column.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.inventory.views import InventoryViewMixin
from apps.organizations.authorization import (
    OutOfScope,
    PermissionMissing,
    has_organization_master_data_permission,
    organizations_with_permission,
)
from apps.organizations.models import Organization
from apps.organizations.selectors import accessible_branches
from apps.sales.dashboard import (
    CARDS,
    CARDS_BY_SLUG,
    DashboardScope,
    card_context,
    default_window,
    headline_for,
)
from apps.sales.permissions import VIEW_SALES_COST, VIEW_SALES_REPORTS


def _date(request: HttpRequest, key: str, fallback: datetime.date) -> datetime.date:
    """
    One date out of the query string, falling back rather than erroring.

    A mistyped date must not take the dashboard away from somebody trying to
    read yesterday's takings — the same choice `report_views._date` and
    `receivable_views._as_of` make.
    """
    raw = request.GET.get(key, "").strip()
    if raw:
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            return fallback
    return fallback


class SalesDashboardMixin(InventoryViewMixin):
    """
    Everything the page and its cards agree about: scope, dates, and cost.

    Shared rather than duplicated because a card that resolved its scope
    differently from the page above it would render figures for a period nobody
    asked for, and the difference would be invisible.
    """

    module_key = "sales"
    required_permission = VIEW_SALES_REPORTS

    def readable_organizations(self) -> QuerySet[Organization]:
        return organizations_with_permission(self.actor, VIEW_SALES_REPORTS).order_by("code")

    def resolve_organization(self, request: HttpRequest) -> Organization:
        """
        The organization this dashboard is about, resolved **with** the caller.

        An id the caller cannot read reports 404 rather than falling back to one
        they can — a filter that silently answered about a different
        organization would be worse than an error, because the figures would
        look perfectly plausible.
        """
        organizations = self.readable_organizations()
        raw = request.GET.get("organization", "").strip()
        if raw.isdigit():
            chosen = organizations.filter(pk=int(raw)).first()
            if chosen is None:
                raise OutOfScope(_("Organization %(id)s does not exist.") % {"id": raw})
            return chosen
        first = organizations.first()
        if first is None:
            raise PermissionMissing(_("view_sales_reports is not held anywhere."))
        return first

    def resolve_scope(
        self, request: HttpRequest, organization: Organization
    ) -> tuple[DashboardScope, Any, str]:
        today = timezone.localdate()
        window_from, window_to = default_window(today)
        date_to = _date(request, "to", window_to)
        date_from = _date(request, "from", window_from)
        if date_from > date_to:
            date_from = date_to

        branches = (
            accessible_branches(self.actor).filter(organization=organization).order_by("code")
        )
        raw_branch = request.GET.get("branch", "").strip()
        branch_ids: tuple[int, ...] | None = None
        if raw_branch.isdigit():
            selected = branches.filter(pk=int(raw_branch)).first()
            # An id the caller cannot reach narrows to nothing rather than
            # widening to everything. A filter must fail closed.
            branch_ids = (selected.pk,) if selected is not None else ()

        scope = DashboardScope(
            organization_id=organization.pk,
            date_from=date_from,
            date_to=date_to,
            branch_ids=branch_ids,
        )
        return scope, branches, raw_branch

    def may_read_cost(self, organization: Organization) -> bool:
        return has_organization_master_data_permission(self.actor, VIEW_SALES_COST, organization)

    def visible_cards(self, organization: Organization) -> list[Any]:
        cost = self.may_read_cost(organization)
        return [card for card in CARDS if cost or not card.needs_cost]

    def query_string(self, scope: DashboardScope, organization: Organization, branch: str) -> str:
        """The filter as a query string, so every card fetches the same period."""
        parts = [
            f"organization={organization.pk}",
            f"from={scope.date_from.isoformat()}",
            f"to={scope.date_to.isoformat()}",
        ]
        if branch:
            parts.append(f"branch={branch}")
        return "&".join(parts)


class SalesDashboardView(SalesDashboardMixin, View):
    """The module's landing page: the filter, the headline, and eight slots."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organization = self.resolve_organization(request)
        scope, branches, raw_branch = self.resolve_scope(request, organization)

        context = {
            "organization": organization,
            "organizations": self.readable_organizations(),
            "branches": branches,
            "selected_branch": raw_branch,
            "scope": scope,
            "headline": headline_for(self.actor, scope),
            "cards": self.visible_cards(organization),
            "card_query": self.query_string(scope, organization, raw_branch),
            "may_read_cost": self.may_read_cost(organization),
            "page_title": _("لوحة المبيعات"),
            "page_hint": _(
                "كل رقم هنا مقروء من مستندات مرحّلة، ولا شيء في هذه الشاشة يكتب. "
                "صافي الإيراد = الإجمالي − خصم المطعم − المرتجعات + خصم المرتجعات، "
                "وهو نفس ما تحمله الحسابات الأربعة في الأستاذ العام. الخصم المموّل "
                "من التطبيق يُعرض ولا يُطرح: التطبيق يعوّضه."
            ),
            "list_base_template": (
                "settings/_list_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }
        return render(request, "sales/dashboard.html", context)


class SalesDashboardCardView(SalesDashboardMixin, View):
    """
    One card, fetched on its own.

    Answers a bare fragment in both directions — there is no full-page form of a
    card, and wrapping one in the shell would put a second navigation rail
    inside a swap target. A direct visit therefore renders the card alone, which
    is honest about what the URL is.
    """

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        slug = str(kwargs.get("slug", ""))
        card = CARDS_BY_SLUG.get(slug)
        if card is None:
            raise OutOfScope(_("Card %(slug)s does not exist.") % {"slug": slug})

        organization = self.resolve_organization(request)
        if card.needs_cost and not self.may_read_cost(organization):
            # 403 rather than an empty card. The card is omitted from the page
            # above, so reaching this at all means the URL was typed, and an
            # empty 200 would look like "no cost this period".
            raise PermissionMissing(_("view_sales_cost is not held in this organization."))

        scope, _branches, _raw = self.resolve_scope(request, organization)
        context: dict[str, Any] = {
            "card": card,
            "scope": scope,
            "organization": organization,
        }
        context.update(card_context(slug, self.actor, scope))
        return render(request, card.template, context)


__all__ = ["SalesDashboardCardView", "SalesDashboardMixin", "SalesDashboardView"]
