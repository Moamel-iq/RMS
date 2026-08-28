"""Forms for the unit of measure screens."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.units.models import UnitOfMeasure


class UnitCreateForm(forms.ModelForm):
    class Meta:
        model = UnitOfMeasure
        fields = ("code", "name", "dimension", "factor_to_base")
        labels = {
            "code": _("الرمز"),
            "name": _("الاسم بالعربية"),
            "dimension": _("البُعد"),
            "factor_to_base": _("المعامل إلى الوحدة الأساس"),
        }
        help_texts = {
            "factor_to_base": _("كم وحدة أساس في وحدة واحدة من هذه. الغرام = ٠٫٠٠١ كغم."),
            "dimension": _("لا يمكن تغييره لاحقاً؛ تغييره يُبطل كل تحويل تم عبره."),
        }


class UnitUpdateForm(forms.ModelForm):
    """
    Code, dimension, and base status are absent on purpose.

    Changing which unit is the base, or moving a unit to another dimension,
    would invalidate conversions already computed under the old rules with no
    way for them to know.
    """

    class Meta:
        model = UnitOfMeasure
        fields = ("name", "factor_to_base", "is_active")
        labels = {
            "name": _("الاسم بالعربية"),
            "factor_to_base": _("المعامل إلى الوحدة الأساس"),
            "is_active": _("فعّال"),
        }
        help_texts = {
            "factor_to_base": _(
                "تغيير المعامل يعيد حساب كل كمية حُوّلت عبر هذه الوحدة. يُسجَّل في سجل التدقيق."
            ),
        }

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if self.instance.pk and self.instance.is_base:
            # A base unit's factor is fixed at 1 by a database constraint;
            # offering the field would only produce an error on save.
            self.fields["factor_to_base"].disabled = True
            self.fields["factor_to_base"].help_text = _("الوحدة الأساس معاملها ١ دائماً.")
