"""Shared view helpers and the audit log screen."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponseBase
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView

from apps.core.models import AuditAction, AuditEvent
from apps.core.selectors import AUDIT_PAGE_SIZE, audit_events


class ModuleViewMixin:
    """
    Declares which module a view belongs to, so the rail highlights it.

    Set on the request rather than passed through context, because the shell
    context processor runs for every template — including ones rendered
    outside a view that knows about modules.
    """

    module_key: str = "home"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        # HttpResponseBase, not HttpResponse: it has to match Django's
        # AccessMixin, which this mixin is combined with on every settings
        # screen. A narrower return type makes the two definitions conflict.
        request.active_module = self.module_key  # type: ignore[attr-defined]
        response: HttpResponseBase = super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
        return response


class FoundationViewMixin(LoginRequiredMixin, UserPassesTestMixin, ModuleViewMixin):
    """
    Base for the Phase 0 settings screens.

    Staff-only for now. These screens create users, branches, and the units
    every quantity is measured in — not something any signed-in cashier should
    reach. Granular per-role permissions are Task 0.7 proper; until they
    exist, the conservative gate is the correct one.
    """

    module_key = "settings"

    def test_func(self) -> bool:
        user = self.request.user  # type: ignore[attr-defined]
        return bool(user.is_authenticated and user.is_staff)


class FoundationFormViewMixin(FoundationViewMixin):
    """
    Base for the Phase 0 create and edit screens.

    Every mutation goes through a service, so the generic view never calls
    `form.save()` and `self.object` stays unset on create. Django's default
    `get_success_url` formats the URL against `self.object.__dict__`, which
    would raise; the destination here is always a fixed list page.
    """

    success_url: Any = None

    def get_success_url(self) -> str:
        return str(self.success_url)


class FoundationListView(FoundationViewMixin, ListView):
    """
    Shared behaviour for the Phase 0 settings lists: page furniture, search,
    and paging. Each subclass declares what it is and which fields it searches.
    """

    page_title: Any = ""
    page_hint: Any = ""
    create_url_name: str | None = None
    create_label: Any = _("إضافة")
    search_fields: tuple[str, ...] = ()
    paginate_by = 50

    def get_queryset(self) -> QuerySet[Any]:
        queryset = super().get_queryset()
        search = self.request.GET.get("q", "").strip()
        if search and self.search_fields:
            matches = Q()
            for field in self.search_fields:
                matches |= Q(**{f"{field}__icontains": search})
            queryset = queryset.filter(matches)
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["page_hint"] = self.page_hint
        context["search"] = self.request.GET.get("q", "")
        context["create_label"] = self.create_label
        context["create_url"] = reverse(self.create_url_name) if self.create_url_name else None
        return context


class AuditEventListView(FoundationListView):
    """
    The audit trail, read-only.

    No create, edit, or delete: the database trigger refuses them, so offering
    the action would only produce an error.
    """

    model = AuditEvent
    template_name = "settings/audit_list.html"
    context_object_name = "events"
    paginate_by = AUDIT_PAGE_SIZE
    page_title = _("سجل التدقيق")
    page_hint = _("سجل غير قابل للتعديل أو الحذف — تمنع ذلك قاعدة البيانات نفسها، لا التطبيق.")
    search_fields = ("actor_label", "target_type", "target_id", "reason")

    def get_queryset(self) -> QuerySet[AuditEvent]:
        queryset = audit_events()
        action = self.request.GET.get("action", "").strip()
        if action in AuditAction.values:
            queryset = queryset.filter(action=action)

        search = self.request.GET.get("q", "").strip()
        if search:
            matches = Q()
            for field in self.search_fields:
                matches |= Q(**{f"{field}__icontains": search})
            queryset = queryset.filter(matches)
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["actions"] = AuditAction.choices
        context["selected_action"] = self.request.GET.get("action", "")
        return context
