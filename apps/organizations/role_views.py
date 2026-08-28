"""
The roles screen (ADR-034): the posts an organization defines, and what each may do.

Staff-only like every settings screen. The built-in posts are shown beside the
custom ones, read-only, so the owner can see what "accountant" already means
before defining a variant of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import FormView

from apps.core.views import FoundationFormViewMixin, FoundationListView, FoundationViewMixin
from apps.organizations.authorization import organizations_with_organization_permission
from apps.organizations.forms import RoleDefinitionForm, RoleLifecycleForm
from apps.organizations.models import Role, RoleDefinition
from apps.organizations.permission_catalog import matrix
from apps.organizations.permissions import role_group_name
from apps.organizations.roles import CONFIGURABLE_APP_LABELS
from apps.organizations.security_permissions import MANAGE_ROLES
from apps.organizations.services import (
    archive_role_definition,
    create_role_definition,
    reactivate_role_definition,
    role_definition_member_count,
    update_role_definition,
)
from apps.users.models import User


def _builtin_permission_codes(role: Role) -> set[str]:
    group = Group.objects.filter(name=role_group_name(role)).first()
    if group is None:
        return set()
    return {
        f"{permission.content_type.app_label}.{permission.codename}"
        for permission in group.permissions.filter(
            content_type__app_label__in=CONFIGURABLE_APP_LABELS
        ).select_related("content_type")
    }


def _actor(request: HttpRequest) -> User:
    """The signed-in caller. Every screen here refuses an anonymous request first."""
    user: User = request.user  # type: ignore[assignment]
    return user


class RoleListView(FoundationListView):
    model = RoleDefinition
    template_name = "settings/role_list.html"
    context_object_name = "definitions"
    page_title = _("الأدوار والصلاحيات")
    page_hint = _(
        "الدور مجموعة صلاحيات تُمنح للمستخدم في فرع أو مؤسسة. "
        "الأدوار المبنية ثابتة؛ الأدوار المخصصة تعرّفها أنت وتتحكم بما يراه حاملها وما يفعله."
    )
    create_url_name = "organizations:role_create"
    create_label = _("دور جديد")
    search_fields = ("code", "name", "organization__code")
    required_permission = MANAGE_ROLES

    def get_queryset(self) -> QuerySet[RoleDefinition]:
        return (
            super()
            .get_queryset()
            .filter(organization__in=self.authorized_organizations())
            .select_related("organization")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["rows"] = [
            {
                "definition": definition,
                "members": role_definition_member_count(definition),
                "permissions": definition.permissions.count(),
            }
            for definition in context["definitions"]
        ]
        context["builtins"] = [
            {"role": role, "label": role.label, "permissions": len(_builtin_permission_codes(role))}
            for role in Role
        ]
        return context


if TYPE_CHECKING:
    _RoleFormView = FormView[RoleDefinitionForm]
else:
    # django-stubs makes FormView generic; Django's runtime class is not
    # subscriptable, and this repository does not monkeypatch it.
    _RoleFormView = FormView


class RoleFormView(FoundationFormViewMixin, _RoleFormView):
    """Create and edit share one screen: a name and a matrix of acts."""

    template_name = "settings/role_form.html"
    form_class = RoleDefinitionForm
    success_url = reverse_lazy("organizations:role_list")
    definition: RoleDefinition | None = None
    page_title: Any = _("دور جديد")
    required_permission = MANAGE_ROLES

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["definition"] = self.definition
        kwargs["actor"] = self.request.user
        return kwargs

    def selected_codes(self, form: RoleDefinitionForm) -> set[str]:
        if form.is_bound:
            return set(self.request.POST.getlist("permissions"))
        return set(form.initial.get("permissions", []))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form = context["form"]
        context["page_title"] = self.page_title
        context["cancel_url"] = self.success_url
        context["definition"] = self.definition
        context["matrix"] = matrix(self.selected_codes(form))
        context["templates"] = [(role.value, role.label) for role in Role]
        return context

    def refuse(self, form: RoleDefinitionForm, error: ValidationError) -> HttpResponse:
        for message in error.messages:
            form.add_error(None, message)
        return self.form_invalid(form)


class RoleCreateView(RoleFormView):
    page_title = _("دور جديد")

    def get_initial(self) -> dict[str, Any]:
        initial = super().get_initial()
        # "ابدأ من": a built-in post's permissions, prefilled and editable.
        template = self.request.GET.get("based_on", "")
        if template in Role.values:
            initial["based_on"] = template
            initial["permissions"] = sorted(_builtin_permission_codes(Role(template)))
        return initial

    def form_valid(self, form: RoleDefinitionForm) -> HttpResponse:
        data = form.cleaned_data
        try:
            create_role_definition(
                organization=data["organization"],
                code=data["code"],
                name=data["name"],
                description=data["description"],
                based_on=data["based_on"],
                permissions=data["permissions"],
                actor=_actor(self.request),
            )
        except ValidationError as error:
            return self.refuse(form, error)
        return HttpResponseRedirect(self.get_success_url())


class RoleUpdateView(RoleFormView):
    page_title = _("تعديل الدور")

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        self.definition = get_object_or_404(
            RoleDefinition.objects.select_related("organization").filter(
                organization__in=organizations_with_organization_permission(
                    _actor(request), MANAGE_ROLES
                )
            ),
            pk=kwargs["pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self) -> dict[str, Any]:
        initial = super().get_initial()
        assert self.definition is not None  # noqa: S101 - set in dispatch
        definition = self.definition
        initial.update(
            organization=definition.organization,
            code=definition.code,
            name=definition.name,
            description=definition.description,
            based_on=definition.based_on,
            permissions=sorted(
                f"{p.content_type.app_label}.{p.codename}"
                for p in definition.permissions.select_related("content_type")
            ),
        )
        return initial

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        assert self.definition is not None  # noqa: S101 - set in dispatch
        context["members"] = role_definition_member_count(self.definition)
        context["lifecycle_form"] = RoleLifecycleForm()
        return context

    def form_valid(self, form: RoleDefinitionForm) -> HttpResponse:
        assert self.definition is not None  # noqa: S101 - set in dispatch
        data = form.cleaned_data
        try:
            update_role_definition(
                definition=self.definition,
                name=data["name"],
                description=data["description"],
                permissions=data["permissions"],
                actor=_actor(self.request),
            )
        except ValidationError as error:
            return self.refuse(form, error)
        return HttpResponseRedirect(self.get_success_url())


class RoleLifecycleView(FoundationViewMixin, View):
    """Archive or reactivate, with a reason; refused while anyone holds the post."""

    action: str = "archive"
    required_permission = MANAGE_ROLES

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        definition = get_object_or_404(
            RoleDefinition.objects.filter(
                organization__in=organizations_with_organization_permission(
                    _actor(request), MANAGE_ROLES
                )
            ),
            pk=kwargs["pk"],
        )
        form = RoleLifecycleForm(request.POST)
        target = reverse("organizations:role_update", args=[definition.pk])
        if not form.is_valid():
            return redirect(target)
        try:
            if self.action == "archive":
                archive_role_definition(
                    definition=definition, reason=form.cleaned_data["reason"], actor=_actor(request)
                )
            else:
                reactivate_role_definition(
                    definition=definition, reason=form.cleaned_data["reason"], actor=_actor(request)
                )
        except ValidationError as error:
            from django.contrib import messages

            for message in error.messages:
                messages.error(request, message)
            return redirect(target)
        return redirect(reverse("organizations:role_list"))


__all__ = [
    "RoleCreateView",
    "RoleLifecycleView",
    "RoleListView",
    "RoleUpdateView",
]
