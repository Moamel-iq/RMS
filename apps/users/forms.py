"""
User forms.

The manager normalises phone numbers on `create_user`, but the admin saves
through a ModelForm and never touches the manager. Without these forms an
admin typing `07701234567` would hit the database CHECK constraint and see an
IntegrityError instead of a field error.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserChangeForm, UserCreationForm
from django.utils.translation import gettext_lazy as _

from apps.users.models import User
from apps.users.phone import normalize_iraqi_mobile


class PhoneNormalizingMixin:
    """Canonicalise the phone field, or raise a field-level validation error."""

    def clean_phone(self) -> str | None:
        phone = self.cleaned_data.get("phone")  # type: ignore[attr-defined]
        if not phone:
            # Empty string would defeat the unique index; store NULL.
            return None
        return normalize_iraqi_mobile(phone)


# These base classes are generic in django-stubs but NOT subscriptable at
# runtime, so `UserCreationForm[User]` raises TypeError when Django imports
# this module. `disallow_any_generics` is therefore switched off for this
# module in pyproject.toml. The alternative — django_stubs_ext.monkeypatch() —
# would make production depend on a type-checking package.
class UserAdminCreationForm(PhoneNormalizingMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "phone")


class UserAdminChangeForm(PhoneNormalizingMixin, UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


class UserAccountCreateForm(PhoneNormalizingMixin, forms.ModelForm):
    """Create an account from the settings screens."""

    password1 = forms.CharField(label=_("كلمة المرور"), widget=forms.PasswordInput, strip=False)
    password2 = forms.CharField(
        label=_("تأكيد كلمة المرور"), widget=forms.PasswordInput, strip=False
    )

    class Meta:
        model = User
        fields = ("username", "phone", "first_name", "last_name", "is_staff")
        labels = {
            "username": _("اسم المستخدم"),
            "phone": _("رقم الهاتف"),
            "first_name": _("الاسم الأول"),
            "last_name": _("الاسم الأخير"),
            "is_staff": _("يدخل إلى شاشات الإعدادات"),
        }
        help_texts = {"phone": _("رقم موبايل عراقي. يُخزَّن بصيغة ‎+9647XXXXXXXXX.")}

    def clean(self) -> dict[str, object]:
        # super().clean() is typed as possibly None; cleaned_data is the
        # authoritative dict either way.
        super().clean()
        cleaned: dict[str, object] = self.cleaned_data
        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", _("كلمتا المرور غير متطابقتين."))
        return cleaned


class UserAccountUpdateForm(PhoneNormalizingMixin, forms.ModelForm):
    """
    Username is absent on purpose: it is what the audit trail shows as the
    actor, and changing it makes historic events harder to attribute.
    """

    class Meta:
        model = User
        fields = ("phone", "first_name", "last_name", "is_active", "is_staff")
        labels = {
            "phone": _("رقم الهاتف"),
            "first_name": _("الاسم الأول"),
            "last_name": _("الاسم الأخير"),
            "is_active": _("فعّال"),
            "is_staff": _("يدخل إلى شاشات الإعدادات"),
        }


class LoginForm(AuthenticationForm):
    """
    Sign-in form accepting a phone number or a username in one field.

    The failure message is deliberately identical for "no such account" and
    "wrong password". Telling the two apart would let anyone enumerate which
    staff phone numbers hold accounts.
    """

    error_messages = {
        "invalid_login": _(
            "بيانات الدخول غير صحيحة. تحقّق من اسم المستخدم أو رقم الهاتف وكلمة المرور."
        ),
        "inactive": _("هذا الحساب غير مُفعَّل. راجع مسؤول النظام."),
    }

    username = forms.CharField(
        label=_("رقم الهاتف أو اسم المستخدم"),
        widget=forms.TextInput(
            attrs={
                "class": "field__input",
                "placeholder": _("رقم الهاتف أو اسم المستخدم"),
                "autocomplete": "username",
                "autofocus": True,
                # No dir="auto" here. An empty field has no strong characters,
                # so it would resolve to LTR and put padding-inline-start on
                # the left while the icon sits on the right. The field
                # inherits the page direction instead; the browser's bidi
                # algorithm still renders a typed +964 number left-to-right.
            }
        ),
    )

    password = forms.CharField(
        label=_("كلمة المرور"),
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "field__input",
                "autocomplete": "current-password",
            }
        ),
    )
