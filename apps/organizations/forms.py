"""
Forms for the organization settings screens.

Thin: every field maps to a service argument. The services own the rules and
the audit events; these only collect input and render errors.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import AccessChangeAction, Branch, Organization, Role
from apps.organizations.security_permissions import MANAGE_ACCESS, MANAGE_ORG_SETTINGS, MANAGE_ROLES
from apps.users.models import User


class OrganizationCreateForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ("code", "name_ar", "name_en")
        labels = {
            "code": _("الرمز"),
            "name_ar": _("الاسم بالعربية"),
            "name_en": _("الاسم بالإنجليزية"),
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
        fields = ("name_ar", "name_en", "is_active")
        labels = {
            "name_ar": _("الاسم بالعربية"),
            "name_en": _("الاسم بالإنجليزية"),
            "is_active": _("فعّال"),
        }


class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = (
            "organization",
            "code",
            "name_ar",
            "name_en",
            "timezone",
            "business_day_start_time",
            "is_active",
        )
        labels = {
            "organization": _("المؤسسة"),
            "code": _("الرمز"),
            "name_ar": _("الاسم بالعربية"),
            "name_en": _("الاسم بالإنجليزية"),
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


class BranchMembershipForm(forms.Form):
    """Grant one user access to one branch in one role."""

    user = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by("username"),
        label=_("المستخدم"),
    )
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.filter(is_active=True).order_by("code"),
        label=_("الفرع"),
    )
    # The built-in posts and every organization's active custom posts
    # (ADR-034). Computed per request: a post defined a minute ago must be
    # grantable now, and a class-level tuple would be frozen at import.
    role = forms.ChoiceField(choices=(), label=_("الدور"))

    def __init__(self, *args: Any, actor: User | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        user_queryset = User.objects.filter(
            is_active=True,
            is_staff=False,
            is_superuser=False,
        ).order_by("username")
        self.fields["user"].queryset = user_queryset  # type: ignore[attr-defined]
        if actor is not None and not actor.is_superuser:
            from apps.organizations.authorization import organizations_with_organization_permission

            organizations = organizations_with_organization_permission(actor, MANAGE_ACCESS)
            self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
                is_active=True, organization__in=organizations
            ).order_by("organization__code", "code")
            # A target must already be visible in the manager's organization.
            # Privileged accounts are never candidates for an ERP role grant.
            self.fields["user"].queryset = (  # type: ignore[attr-defined]
                user_queryset.filter(
                    Q(organization_memberships__organization__in=organizations)
                    | Q(branch_memberships__branch__organization__in=organizations)
                )
                .distinct()
                .order_by("username")
            )
        from apps.organizations.roles import role_choices

        choices = role_choices(
            organizations if actor is not None and not actor.is_superuser else None
        )
        # OWNER is a security-sensitive post.  It is never granted through a
        # direct form; the maker-checker workflow will own it in a follow-up
        # migration.
        self.fields["role"].choices = [choice for choice in choices if choice[0] != Role.OWNER]  # type: ignore[attr-defined]


class AccessChangeRequestForm(forms.Form):
    """Collect an access proposal; the service applies it only after review."""

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.none(),
        required=False,
        label=_("الفرع"),
        help_text=_("اتركه فارغاً للصلاحية على مستوى المؤسسة كلها."),
    )
    target_user = forms.ModelChoiceField(queryset=User.objects.none(), label=_("المستخدم"))
    action = forms.ChoiceField(choices=AccessChangeAction.choices, label=_("الإجراء"))
    requested_role = forms.ChoiceField(
        choices=(),
        required=False,
        label=_("الدور المطلوب"),
        help_text=_("يُستخدم عند المنح فقط. طلب المالك يكون على مستوى المؤسسة."),
    )
    reason = forms.CharField(label=_("سبب الطلب"), widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args: Any, actor: User | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        organizations = Organization.objects.none()
        if actor is not None:
            from apps.organizations.authorization import organizations_with_organization_permission

            organizations = organizations_with_organization_permission(actor, MANAGE_ACCESS)
        self.fields["organization"].queryset = organizations  # type: ignore[attr-defined]
        self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
            is_active=True, organization__in=organizations
        ).order_by("organization__code", "code")
        self.fields["target_user"].queryset = User.objects.filter(  # type: ignore[attr-defined]
            is_active=True, is_staff=False, is_superuser=False
        ).order_by("username")
        from apps.organizations.roles import role_choices

        self.fields["requested_role"].choices = [  # type: ignore[attr-defined]
            ("", _("— اختر الدور —")),
            *role_choices(organizations),
        ]

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        organization = cleaned.get("organization")
        branch = cleaned.get("branch")
        action = cleaned.get("action")
        role = cleaned.get("requested_role")
        if (
            organization is not None
            and branch is not None
            and branch.organization_id != organization.pk
        ):
            self.add_error("branch", _("الفرع لا يتبع المؤسسة المحددة."))
        if action == AccessChangeAction.GRANT and not role:
            self.add_error("requested_role", _("اختر الدور المطلوب للمنح."))
        if action == AccessChangeAction.REVOKE:
            cleaned["requested_role"] = ""
        return cleaned


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
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنجليزية"), max_length=200, required=False)
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
