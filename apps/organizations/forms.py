"""
Forms for the organization settings screens.

Thin: every field maps to a service argument. The services own the rules and
the audit events; these only collect input and render errors.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import Branch, Organization, Role
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

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
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
    role = forms.ChoiceField(choices=Role.choices, label=_("الدور"))
