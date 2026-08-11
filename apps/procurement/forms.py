"""
Procurement forms. They validate and normalise; they never save.

Every form takes the `actor` and narrows its own choices from that caller's
scope, so a submitted id cannot widen access. `save()` is deliberately absent:
the view calls a service, which is the only place a write happens.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.organizations.authorization import organizations_with_permission
from apps.organizations.models import Organization
from apps.procurement.models import Supplier
from apps.procurement.permissions import MANAGE_SUPPLIERS
from apps.procurement.selectors import visible_suppliers
from apps.procurement.services import canonical_code
from apps.users.models import User
from apps.users.phone import normalize_iraqi_mobile


class SupplierForm(forms.Form):
    """
    Create or correct a supplier.

    The organization field appears only when creating. Moving an existing
    supplier between organizations would carry its whole document history
    across a tenancy boundary, so the field is simply absent on edit rather
    than present and disabled — a disabled field is still submitted, and the
    view would have to remember to ignore it.
    """

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنكليزية"), max_length=200, required=False)
    contact_name = forms.CharField(label=_("جهة الاتصال"), max_length=200, required=False)
    phone = forms.CharField(label=_("الهاتف"), max_length=20, required=False)
    email = forms.EmailField(label=_("البريد الإلكتروني"), required=False)
    address = forms.CharField(label=_("العنوان"), required=False, widget=forms.Textarea)
    payment_terms_days = forms.IntegerField(
        label=_("مهلة السداد (يوم)"),
        min_value=0,
        max_value=365,
        initial=0,
        help_text=_("صفر يعني الدفع عند الاستلام."),
    )
    credit_limit = forms.DecimalField(
        label=_("سقف الائتمان"),
        min_value=0,
        required=False,
        help_text=_("اتركه فارغاً إذا لم يكن هناك سقف متفق عليه."),
    )
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(
        self,
        *args: Any,
        actor: User,
        instance: Supplier | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        if instance is not None:
            # The code identifies a supplier every posted document already
            # points at. Correcting a typo is a data migration, not an edit.
            del self.fields["organization"]
            self.fields["code"].disabled = True
            self.fields["code"].initial = instance.code
            return

        self.fields["organization"].queryset = organizations_with_permission(  # type: ignore[attr-defined]
            actor, MANAGE_SUPPLIERS
        ).order_by("code")

    def clean_code(self) -> str:
        code = canonical_code(self.cleaned_data["code"])
        if not code:
            raise forms.ValidationError(_("الرمز مطلوب."), code="code_required")
        if self.instance is not None:
            return self.instance.code

        organization_id = self.data.get("organization")
        if (
            organization_id
            and Supplier.objects.filter(organization_id=organization_id, code=code).exists()
        ):
            raise forms.ValidationError(
                _("الرمز %(code)s مستخدم في هذه المؤسسة.") % {"code": code},
                code="code_taken",
            )
        return code

    def clean_phone(self) -> str:
        value = self.cleaned_data.get("phone", "").strip()
        if not value:
            return ""
        # Raises a ValidationError in Arabic, which the form renders inline.
        return normalize_iraqi_mobile(value)

    def selected_organization(self) -> Organization:
        organization: Organization = self.cleaned_data["organization"]
        return organization


class SupplierActionForm(forms.Form):
    """Archive or reactivate. A reason is required and is audited."""

    reason = forms.CharField(label=_("السبب"), max_length=500)

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor

    def visible(self) -> Any:
        return visible_suppliers(self.actor)
