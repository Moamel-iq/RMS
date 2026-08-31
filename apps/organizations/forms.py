"""
Forms for the organization settings screens.

Thin: every field maps to a service argument. The services own the rules and
the audit events; these only collect input and render errors.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import Branch, Organization, Role
from apps.organizations.security_permissions import MANAGE_ACCESS, MANAGE_ORG_SETTINGS, MANAGE_ROLES
from apps.users.models import User


class OrganizationCreateForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ("code", "name")
        labels = {
            "code": _("الرمز"),
            "name": _("الاسم بالعربية"),
        }
        help_texts = {
            "code": _("حروف إنجليزية كبيرة وأرقام. لا يمكن تغييره لاحقاً."),
        }


class OrganizationUpdateForm(forms.ModelForm):
    """
    Code is deliberately absent.

    It appears in document numbering and reports; editing it would rewrite
    what historic documents claim to belong to.
    """

    class Meta:
        model = Organization
        fields = ("name", "is_active")
        labels = {
            "name": _("الاسم بالعربية"),
            "is_active": _("فعّال"),
        }


class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = (
            "organization",
            "code",
            "name",
            "name",
            "timezone",
            "business_day_start_time",
            "is_active",
        )
        labels = {
            "organization": _("المؤسسة"),
            "code": _("الرمز"),
            "name": _("الاسم بالعربية"),
            "timezone": _("المنطقة الزمنية"),
            "business_day_start_time": _("بداية يوم العمل"),
            "is_active": _("فعّال"),
        }
        help_texts = {
            "business_day_start_time": _(
                "يمتد يوم العمل ٢٤ ساعة من هذا الوقت. المبيعات بعد منتصف الليل "
                "تُنسب إلى اليوم الذي بدأ صباح أمس."
            ),
        }
        widgets = {
            "business_day_start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }

    def __init__(self, *args: object, actor: User | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if actor is not None and not actor.is_superuser:
            from apps.organizations.authorization import organizations_with_organization_permission

            self.fields["organization"].queryset = organizations_with_organization_permission(  # type: ignore[attr-defined]
                actor, MANAGE_ORG_SETTINGS
            )
        # The code identifies the branch in documents; fix it once created.
        if self.instance.pk:
            self.fields["code"].disabled = True
            self.fields["organization"].disabled = True


class EmployeeAccessForm(forms.Form):
    """
    Put one employee in one post, at one branch or across the organization.

    The target is not a field: this form lives on that employee's own screen,
    so the person is already chosen and offering a picker would invite the
    manager to change somebody else by accident.

    `scope` collapses "which organization" and "which branch" into one choice
    because they are one question to the person answering it — *where does
    this employee work* — and a two-field version lets you submit an
    organization and a branch that disagree.
    """

    scope = forms.ChoiceField(choices=(), label=_("النطاق"))
    role = forms.ChoiceField(choices=(), label=_("الدور"))

    #: `scope` values are `org:<pk>` or `branch:<pk>`.
    ORGANIZATION = "org"
    BRANCH = "branch"

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        from apps.organizations.authorization import organizations_with_organization_permission
        from apps.organizations.roles import role_choices

        organizations = organizations_with_organization_permission(actor, MANAGE_ACCESS)
        if actor.is_superuser:
            organizations = Organization.objects.filter(is_active=True)
        self.organizations = {org.pk: org for org in organizations}

        branches = (
            Branch.objects.filter(is_active=True, organization__in=organizations)
            .select_related("organization")
            .order_by("organization__code", "code")
        )
        self.branches = {branch.pk: branch for branch in branches}

        scopes: list[tuple[str, Any]] = []
        for org in organizations.order_by("code"):
            scopes.append(
                (f"{self.ORGANIZATION}:{org.pk}", _("%(name)s — كل الفروع") % {"name": org.name})
            )
        for branch in branches:
            scopes.append(
                (f"{self.BRANCH}:{branch.pk}", f"{branch.organization.name} — {branch.name}")
            )
        self.fields["scope"].choices = scopes  # type: ignore[attr-defined]

        # OWNER is absent from the list rather than refused after the fact.
        # `_require_access_administrator` refuses it anyway; leaving it out
        # means the manager is never offered a post they cannot grant.
        choices = role_choices(None if actor.is_superuser else organizations)
        self.fields["role"].choices = [row for row in choices if row[0] != Role.OWNER]  # type: ignore[attr-defined]

    def clean_scope(self) -> str:
        raw = str(self.cleaned_data["scope"])
        kind, _sep, key = raw.partition(":")
        if not key.isdigit():
            raise forms.ValidationError(_("نطاق غير صالح."), code="bad_scope")
        pk = int(key)
        # Resolved against the caller's own scope, never fetched then checked:
        # a submitted id must not be able to widen what this manager reaches.
        if kind == self.ORGANIZATION and pk in self.organizations:
            self.organization = self.organizations[pk]
            self.branch = None
        elif kind == self.BRANCH and pk in self.branches:
            self.branch = self.branches[pk]
            self.organization = self.branch.organization
        else:
            raise forms.ValidationError(_("نطاق غير صالح."), code="bad_scope")
        return raw


class RoleDefinitionForm(forms.Form):
    """
    A post the organization defines (ADR-034): a name and a set of acts.

    The acts are validated against the configurable permissions, so a code
    typed into the request that names nothing — or names something outside
    the modules — is refused here before the service sees it.
    """

    organization = forms.ModelChoiceField(
        queryset=Organization.objects.filter(is_active=True).order_by("code"),
        label=_("المؤسسة"),
        help_text=_("الدور يُمنح داخل هذه المؤسسة وحدها."),
    )
    code = forms.CharField(
        label=_("الرمز"),
        max_length=24,
        help_text=_("حروف لاتينية صغيرة وأرقام وشرطات. لا يتغيّر بعد الإنشاء."),
    )
    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    description = forms.CharField(
        label=_("الوصف"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    based_on = forms.ChoiceField(
        label=_("ابدأ من دور مبني"),
        required=False,
        choices=[("", _("— من الصفر —")), *Role.choices],
        help_text=_("تُنسخ صلاحيات الدور المبني كبداية؛ عدّلها كما تشاء."),
    )
    permissions = forms.MultipleChoiceField(
        label=_("الصلاحيات"),
        required=False,
        choices=(),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(
        self,
        *args: Any,
        definition: Any | None = None,
        actor: User | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if actor is not None and not actor.is_superuser:
            from apps.organizations.authorization import organizations_with_organization_permission

            self.fields["organization"].queryset = organizations_with_organization_permission(  # type: ignore[attr-defined]
                actor, MANAGE_ROLES
            )
        from apps.organizations.roles import configurable_permissions

        self.fields["permissions"].choices = [  # type: ignore[attr-defined]
            (f"{p.content_type.app_label}.{p.codename}", p.name) for p in configurable_permissions()
        ]
        if definition is not None:
            # The organization and code are part of every key that already
            # names this post; they are shown, not offered.
            self.fields["organization"].disabled = True
            self.fields["code"].disabled = True
            self.fields["based_on"].disabled = True

    def clean_code(self) -> str:
        return str(self.cleaned_data["code"]).strip().lower()


class RoleLifecycleForm(forms.Form):
    """Archive or reactivate a post: the reason is the record."""

    reason = forms.CharField(label=_("السبب"), max_length=200)
