"""
المطابقة اليومية — the sales reconciliation screen.

**Read-only.** No form, no POST handler and no resolve action live on this
report. `DailyFinancialClose` persists a separate immutable snapshot for the
pre-posting control; this screen intentionally continues to derive its figures
from live source documents, so a later reversal or correction is never hidden
by a stored verdict.

That includes the absence of an "acknowledge" or "mark reviewed" control. A
finding here says two documents disagree; a button that merely recorded someone
having seen it would let a real shortage be closed by clicking. The same refusal
`verify_kitchen` makes by having no `--fix` (RCP-050).

Guarded by `VIEW_SALES_REPORTS` and by nothing narrower. It records nothing, and
a permission of its own would be a grant that protects no state; the rows it can
reach are already narrowed by `visible_sales_days`, which is branch-scoped, so
scope and selector answer two different questions and both are asked (ADR-016).
"""

from __future__ import annotations

import datetime
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.inventory.views import InventoryViewMixin
from apps.organizations.selectors import accessible_branches
from apps.sales.daily_reconciliation import reconcile_range
from apps.sales.permissions import VIEW_SALES_REPORTS

#: A fortnight. Long enough that a Monday morning covers the previous week and
#: short enough that the default never rebuilds a quarter of reconciliations to
#: answer a question about yesterday.
DEFAULT_WINDOW_DAYS = 14


def _date(request: HttpRequest, key: str, fallback: datetime.date) -> datetime.date:
    """
    One date out of the query string, falling back rather than erroring.

    A mistyped date should not take the screen away from somebody trying to read
    a variance — the same choice `receivable_views._as_of` makes, for the same
    reason.
    """
    raw = request.GET.get(key, "").strip()
    if raw:
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            return fallback
    return fallback


class DailyReconciliationView(InventoryViewMixin, View):
    """One row per branch per business date, with every stream kept apart."""

    module_key = "sales"
    required_permission = VIEW_SALES_REPORTS

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        today = timezone.localdate()
        date_to = _date(request, "to", today)
        date_from = _date(request, "from", date_to - datetime.timedelta(days=DEFAULT_WINDOW_DAYS))
        if date_from > date_to:
            date_from = date_to

        branches = accessible_branches(self.actor).select_related("organization")
        raw_branch = request.GET.get("branch", "").strip()
        branch_ids: list[int] | None = None
        if raw_branch.isdigit():
            selected = branches.filter(pk=int(raw_branch)).first()
            # Resolved *with* the caller. An id they cannot reach narrows to
            # nothing rather than widening to everything, which is the direction
            # a filter must fail in.
            branch_ids = [selected.pk] if selected is not None else []

        rows = reconcile_range(
            self.actor, branch_ids=branch_ids, date_from=date_from, date_to=date_to
        )
        only_dirty = request.GET.get("dirty", "") == "1"
        if only_dirty:
            rows = [row for row in rows if not row.is_clean]

        context = {
            "rows": rows,
            "date_from": date_from,
            "date_to": date_to,
            "branches": branches.order_by("organization__code", "code"),
            "selected_branch": raw_branch,
            "only_dirty": only_dirty,
            "error_count": sum(len(row.errors) for row in rows),
            "advisory_count": sum(len(row.advisories) for row in rows),
            "limitation_count": sum(len(row.limitations) for row in rows),
            "page_title": _("المطابقة اليومية"),
            "page_hint": _(
                "تقرير فقط: لا يُخزَّن ولا يُعتمد ولا يُغلَق. الفرق هنا يعني أن "
                "مستندين لا يتفقان، وعلاجه تغيير مستند بخدمته وصلاحيته — لا زرّ "
                "يشطبه من الشاشة. المُعلن والمشتق يُعرضان معاً لأن أيّهما الخطأ هو "
                "ما يقرّر من يصلحه."
            ),
            # `_form_fragment.html`, not `_list_fragment.html`. This template
            # extends `list_base_template` **directly** rather than through
            # `settings/base_list.html`, so the block it defines is `page`;
            # `_list_fragment.html` contains only `results`, Django silently
            # drops a child block the parent does not declare, and the htmx
            # form of this screen answered 200 with an empty body.
            "list_base_template": (
                "settings/_form_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }
        return render(request, "sales/daily_reconciliation.html", context)


__all__ = ["DEFAULT_WINDOW_DAYS", "DailyReconciliationView"]
