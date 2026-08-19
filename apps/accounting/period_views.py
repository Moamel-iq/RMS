"""
الفترات المحاسبية — fiscal years, periods, and the pre-close check.

The transitions call the kernel commands unchanged. What this module adds is
the **preview**: `فحص ما قبل الإغلاق` runs every blocker check independently and
reports the whole list, because the kernel's own guard runner stops at the
first veto and an accountant told one thing at a time closes a month in six
attempts instead of one.

There is no repair control anywhere on these screens. Every blocker belongs to
some other document, and clearing it from here would be deciding something that
is not this module's to decide.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.commands import (
    close_accounting_period,
    reopen_accounting_period,
    soft_close_accounting_period,
)
from apps.accounting.models import AccountingPeriod, PeriodState
from apps.accounting.period_services import (
    fiscal_year_summary,
    period_activity,
    period_close_blockers,
)
from apps.accounting.permissions import (
    CLOSE_PERIOD,
    REOPEN_PERIOD,
    SOFT_CLOSE_PERIOD,
    VIEW_JOURNAL,
)
from apps.accounting.views import AccountingDetailView, AccountingViewMixin
from apps.core.models import AuditEvent
from apps.organizations.authorization import (
    OutOfScope,
    has_organization_permission,
    organizations_with_permission,
)
from apps.organizations.models import Organization


def _visible_periods(actor: Any) -> QuerySet[AccountingPeriod]:
    return AccountingPeriod.objects.filter(
        fiscal_year__organization__in=organizations_with_permission(actor, VIEW_JOURNAL)
    ).select_related("fiscal_year", "fiscal_year__organization")


def _chosen_organization(actor: Any, request: HttpRequest) -> Organization | None:
    organizations = organizations_with_permission(actor, VIEW_JOURNAL).order_by("code")
    raw = request.GET.get("organization", "").strip()
    if raw.isdigit():
        organization = organizations.filter(pk=int(raw)).first()
        if organization is None:
            raise OutOfScope(_("Organization does not exist."))
        return organization
    return organizations.first()


class PeriodListView(AccountingViewMixin, View):
    """Every fiscal year and its twelve periods, with what each one carries."""

    required_permission = VIEW_JOURNAL
    template_name = "accounting/period_list.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations = organizations_with_permission(self.actor, VIEW_JOURNAL).order_by("code")
        organization = _chosen_organization(self.actor, request)
        years = fiscal_year_summary(organization=organization) if organization else []

        state = request.GET.get("state", "").strip()
        if state in PeriodState.values:
            for row in years:
                row["periods"] = [p for p in row["periods"] if p.state == state]

        return render(
            request,
            self.template_name,
            {
                "organizations": organizations,
                "organization": organization,
                "years": years,
                "states": PeriodState.choices,
                "selected_state": state,
                "may_close": bool(
                    organization
                    and has_organization_permission(self.actor, CLOSE_PERIOD, organization)
                ),
                "page_title": _("الفترات المحاسبية"),
                "page_hint": _(
                    "اثنتا عشرة فترة شهرية في السنة، ولا فترة ثالثة عشرة. الإقفال "
                    "بالترتيب، وإعادة الفتح بالعكس — ولكلٍّ سببه المسجَّل."
                ),
                "list_base_template": (
                    "settings/_form_fragment.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
                "inventory_ui": False,
            },
        )


class PeriodDetailView(AccountingDetailView):
    """One period: its state, what it carries, and what stands in the way."""

    required_permission = VIEW_JOURNAL
    template_name = "accounting/period_detail.html"

    def period(self) -> AccountingPeriod:
        row = _visible_periods(self.actor).filter(pk=self.kwargs["pk"]).first()
        if row is None:
            raise OutOfScope(_("Accounting period does not exist."))
        return row

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        period = self.period()
        organization = period.fiscal_year.organization
        return self.render_detail(
            request,
            {
                "period": period,
                "activity": period_activity(period=period),
                "may_soft_close": has_organization_permission(
                    self.actor, SOFT_CLOSE_PERIOD, organization
                ),
                "may_close": has_organization_permission(self.actor, CLOSE_PERIOD, organization),
                "may_reopen": has_organization_permission(self.actor, REOPEN_PERIOD, organization),
                "timeline": AuditEvent.objects.filter(
                    target_type="accounting.AccountingPeriod", target_id=str(period.pk)
                ).order_by("-occurred_at")[:20],
                "page_title": _("الفترة %(period)s") % {"period": str(period)},
                "page_hint": _(
                    "فحص ما قبل الإغلاق يجمع كل المعوّقات دفعة واحدة، لا واحداً "
                    "بعد الآخر — وإلا صار إقفال شهر ستّ محاولات."
                ),
            },
        )


class PeriodPrecheckView(AccountingViewMixin, View):
    """
    فحص ما قبل الإغلاق — every blocker at once, as an htmx panel.

    Its own request because it is the expensive part of the page: it runs each
    registered domain guard and counts documents across four modules, and the
    period's own state should render instantly whether or not this does.
    """

    required_permission = VIEW_JOURNAL
    template_name = "accounting/_period_blockers.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        period = _visible_periods(self.actor).filter(pk=kwargs["pk"]).first()
        if period is None:
            raise OutOfScope(_("Accounting period does not exist."))
        blockers = period_close_blockers(period=period)
        return render(
            request,
            self.template_name,
            {
                "period": period,
                "blockers": blockers,
                "blocking_count": sum(1 for row in blockers if row.is_blocking),
                "advisory_count": sum(1 for row in blockers if not row.is_blocking),
            },
        )


class PeriodTransitionView(AccountingViewMixin, View):
    """
    Soft close, close and reopen — the kernel commands, unchanged.

    The command layer checks the permission and the kernel enforces the
    ordering; this view only turns a `ValidationError` into a sentence.
    """

    required_permission = VIEW_JOURNAL
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        period = _visible_periods(self.actor).filter(pk=kwargs["pk"]).first()
        if period is None:
            raise OutOfScope(_("Accounting period does not exist."))
        reason = request.POST.get("reason", "").strip()

        try:
            if self.action == "soft_close":
                soft_close_accounting_period(actor=self.actor, period_id=period.pk, reason=reason)
                messages.success(request, _("أُغلقت الفترة مبدئياً."))
            elif self.action == "close":
                close_accounting_period(actor=self.actor, period_id=period.pk, reason=reason)
                messages.success(request, _("أُقفلت الفترة."))
            elif self.action == "reopen":
                # The kernel requires a reason and says so; passed through
                # rather than defaulted, because "reopened for no stated
                # reason" is exactly what the audit event must not say.
                reopen_accounting_period(actor=self.actor, period_id=period.pk, reason=reason)
                messages.success(request, _("أُعيد فتح الفترة."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(reverse("accounting:period_detail", args=[period.pk]))


__all__ = [
    "PeriodDetailView",
    "PeriodListView",
    "PeriodPrecheckView",
    "PeriodTransitionView",
]
