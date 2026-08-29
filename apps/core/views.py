"""Shared view helpers and the audit log screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, View

from apps.core.automation import (
    acknowledge_task,
    outbox_metrics,
    replay_dead_letter,
    tasks_for_actor,
)
from apps.core.models import (
    AuditAction,
    AuditEvent,
    AutomationOutboxEvent,
    AutomationTaskStatus,
    OutboxEventStatus,
)
from apps.core.permissions import VIEW_AUTOMATION_OUTBOX, VIEW_AUTOMATION_TASK
from apps.core.selectors import AUDIT_PAGE_SIZE, audit_events
from apps.organizations.authorization import organizations_with_organization_permission
from apps.organizations.security_permissions import VIEW_AUDIT
from config import __version__

if TYPE_CHECKING:
    from apps.organizations.models import Organization


class AboutView(LoginRequiredMixin, View):
    """
    حول النظام — what this software is, and who it belongs to.

    Signed in, but no permission beyond that: every user is entitled to know
    what they are using and which version, and a support call starts by
    quoting it. It reads nothing sensitive — the organizations listed are the
    ones the caller can already reach.
    """

    module_key = "home"
    template_name = "core/about.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        import sys

        import django

        from apps.organizations.selectors import accessible_branches
        from apps.users.models import User

        # `LoginRequiredMixin` has already refused anonymity; the annotation
        # is for the type checker, which cannot see that from here.
        actor: User = request.user  # type: ignore[assignment]
        organizations = {
            branch.organization_id: branch.organization
            for branch in accessible_branches(actor).select_related("organization")
        }
        return render(
            request,
            self.template_name,
            {
                "page_title": _("حول النظام"),
                "version": __version__,
                "django_version": django.get_version(),
                "python_version": (
                    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                ),
                "time_zone": settings.TIME_ZONE,
                "organizations": sorted(organizations.values(), key=lambda row: row.code),
            },
        )


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
    Base for the settings screens.

    Administrative acts are organization-scoped.  ``is_staff`` only permits a
    user into Django's administration site; it must never turn a cashier into
    an ERP security administrator.  Each concrete screen therefore declares
    its named permission and filters every object from the matching
    organization query set.  Superusers remain an explicit break-glass path.
    """

    module_key = "settings"
    required_permission: str | None = None
    superuser_only = False

    def authorized_organizations(self) -> QuerySet[Organization]:
        """The organizations this view may operate on for this request."""
        if not self.required_permission:
            from apps.organizations.models import Organization

            return Organization.objects.none()
        return organizations_with_organization_permission(
            self.request.user,  # type: ignore[attr-defined]
            self.required_permission,
        )

    def test_func(self) -> bool:
        user = self.request.user  # type: ignore[attr-defined]
        if not user.is_authenticated or not user.is_active:
            return False
        if user.is_superuser:
            return True
        if self.superuser_only:
            return False
        return bool(self.required_permission and self.authorized_organizations().exists())


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
    required_permission = VIEW_AUDIT

    def get_queryset(self) -> QuerySet[AuditEvent]:
        queryset = audit_events()
        # An event without a provable tenant scope must never be shown to an
        # organization user.  The migration backfills branch-scoped history;
        # new events always carry organization explicitly or by inference.
        if not self.request.user.is_superuser:
            queryset = queryset.filter(organization__in=self.authorized_organizations())
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


class AutomationTaskInboxView(LoginRequiredMixin, UserPassesTestMixin, ModuleViewMixin, View):
    """Role-scoped inbox. A task does not grant access to its source record."""

    module_key = "settings"
    required_permission = VIEW_AUTOMATION_TASK
    template_name = "core/task_inbox.html"

    def test_func(self) -> bool:
        user = self.request.user
        if not user.is_authenticated or not user.is_active:
            return False
        # Django's role groups decide whether the reader may open the inbox;
        # `tasks_for_actor` still applies the stricter organization, branch,
        # assigned-role and sensitivity filters to every row.  A user who has
        # the role only in another organization sees an empty inbox, never a
        # foreign task, which is safer than treating a zero-task day as 403.
        return user.is_superuser or user.has_perm(self.required_permission)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        tasks = tasks_for_actor(actor=request.user)
        status = request.GET.get("status", "").strip()
        if status in AutomationTaskStatus.values:
            tasks = tasks.filter(status=status)
        return render(
            request,
            self.template_name,
            {
                "page_title": _("صندوق المهام"),
                "page_hint": _(
                    "تظهر هنا الاستثناءات التي تقع ضمن دورك ونطاقك فقط. استلام المهمة لا يعتمد ولا يلغي أي عملية مالية."
                ),
                "tasks": tasks,
                "selected_status": status,
                "statuses": AutomationTaskStatus.choices,
            },
        )


class AutomationTaskAcknowledgeView(LoginRequiredMixin, UserPassesTestMixin, ModuleViewMixin, View):
    module_key = "settings"
    required_permission = VIEW_AUTOMATION_TASK

    def test_func(self) -> bool:
        return self.request.user.is_authenticated and self.request.user.is_active

    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        task = get_object_or_404(tasks_for_actor(actor=request.user), pk=pk)
        try:
            task = acknowledge_task(task=task, actor=request.user)
        except Exception as error:  # authorization and lifecycle errors are rendered safely
            messages.error(request, str(error))
            return redirect("core:task_inbox")
        if request.headers.get("HX-Request") == "true":
            return render(request, "core/_task_row.html", {"task": task})
        messages.success(request, _("تم تسجيل استلام المهمة."))
        return redirect("core:task_inbox")


class AutomationMonitoringView(FoundationViewMixin, View):
    """Queue health and dead letters, limited to organization-control roles."""

    required_permission = VIEW_AUTOMATION_OUTBOX
    template_name = "core/automation_monitoring.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations = self.authorized_organizations()
        events = (
            AutomationOutboxEvent.objects.filter(organization__in=organizations)
            .select_related("organization", "branch", "created_by")
            .order_by("-created_at")[:100]
        )
        return render(
            request,
            self.template_name,
            {
                "page_title": _("مراقبة الأتمتة"),
                "page_hint": _(
                    "الرسائل تنفّذ جمع البيانات والتنبيهات فقط؛ لا تعتمد أو ترحّل معاملات مالية."
                ),
                "metrics": outbox_metrics(organizations=organizations),
                "events": events,
                "dead_letter_status": OutboxEventStatus.DEAD_LETTER,
                "may_replay": request.user.has_perm("core.replay_automation_outbox"),
            },
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        event = get_object_or_404(
            AutomationOutboxEvent.objects.filter(organization__in=self.authorized_organizations()),
            pk=request.POST.get("event_id"),
        )
        try:
            replay_dead_letter(event=event, actor=request.user)
        except Exception as error:  # service remains the permission and lifecycle gate
            messages.error(request, str(error))
        else:
            messages.success(request, _("أُعيدت الرسالة إلى طابور المعالجة."))
        return redirect("core:automation_monitoring")
