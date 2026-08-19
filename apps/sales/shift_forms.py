"""
Forms for the cashier shift.

Separate from `day_forms.py`, `adjustment_forms.py` and `settlement_forms.py`
for the reason each of those is separate: this document establishes one fact —
what was physically in a drawer — and the rules for asking that question have
nothing in common with the rules for entering a day's sales.

None of them computes a variance. `shift_services.close_cashier_shift` does,
because it has to stamp the expectation in the same transaction and under the
same row lock, and a form that computed one would be a second implementation of
the arithmetic the whole document exists to produce.

The count is asked for **without** the expected figure beside it in the input.
That is deliberate: a field pre-filled with what the drawer *should* contain is
an invitation to confirm rather than to count, and the number this document
exists to produce would then be zero every time. The expectation is shown on the
page after the count is recorded, which is where it belongs.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.organizations.authorization import branches_with_permission
from apps.organizations.models import Branch
from apps.sales.models import CashierShift, SalesDay, TenderDestination
from apps.sales.permissions import CLOSE_CASHIER_SHIFT
from apps.sales.shift_services import COUNTABLE_TENDERS, candidate_days
from apps.users.models import User


class CashierShiftForm(forms.Form):
    """
    Open a till.

    The branch choices come from where the caller holds `close_cashier_shift`,
    which is `BRANCH` — offering a branch they cannot close for would be
    offering a dead end two clicks later.

    `cashier` is asked for rather than assumed to be the caller. A supervisor
    legitimately opens a drawer for somebody else, and the person answerable for
    the count is the one whose name is on it, not the one who happened to be at
    the terminal.
    """

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    business_date = forms.DateField(
        label=_("تاريخ العمل"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("يُدخَل ولا يُشتق من الوقت: بداية يوم العمل تخص الفرع نفسه."),
    )
    cashier = forms.ModelChoiceField(queryset=User.objects.none(), label=_("الكاشير"))
    opening_float = forms.DecimalField(
        label=_("العهدة الافتتاحية"),
        min_value=Decimal("0"),
        decimal_places=3,
        initial=Decimal("0"),
        help_text=_(
            "مال المطعم نُقل من الخزنة إلى الدرج: لا يُرحَّل ولا يُعدّ إيراداً. "
            "يرفع ما يُتوقَّع عدّه، ولا شيء غير ذلك."
        ),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        branches = branches_with_permission(actor, CLOSE_CASHIER_SHIFT)
        self.fields["branch"].queryset = branches.select_related(  # type: ignore[attr-defined]
            "organization"
        ).order_by("organization__code", "code")
        # Anybody with a post at one of those branches, or at the organization
        # above it, can be named as the cashier. Narrower than "every user" and
        # wider than "the caller", which is the honest middle: the till belongs
        # to somebody who works there, and an organization post reaches every
        # branch it owns (ADR-016).
        organization_ids = list(branches.values_list("organization_id", flat=True))
        self.fields["cashier"].queryset = (  # type: ignore[attr-defined]
            User.objects.filter(
                Q(branch_memberships__branch__in=branches, branch_memberships__is_active=True)
                | Q(
                    organization_memberships__organization_id__in=organization_ids,
                    organization_memberships__is_active=True,
                ),
                is_active=True,
            )
            .distinct()
            .order_by("username")
        )
        self.fields["business_date"].initial = timezone.localdate()

    def clean_business_date(self) -> datetime.date:
        value: datetime.date = self.cleaned_data["business_date"]
        if value > timezone.localdate():
            raise forms.ValidationError(
                _("لا يمكن فتح صندوق ليوم لم يأتِ بعد."), code="future_business_date"
            )
        return value

    def selected_organization(self) -> Any:
        branch = self.cleaned_data.get("branch")
        return branch.organization if branch is not None else None


class TenderCountForm(forms.Form):
    """
    Record what was counted in one tender.

    `APPLICATION_RECEIVABLE` is absent from the choices and refused by a check
    constraint on the row. A delivery application's debt is not in a drawer — it
    is cleared by a settlement — and offering a box to type it into would invite
    somebody to, which is how a receivable ends up counted as cash.

    Card is offered even though it never reaches this document's journal. A card
    total that disagrees with the terminal is a real finding on the same day,
    and recording it costs nothing; recognising a difference in it would be
    recognising a variance against money the acquirer has not remitted, which is
    why the journal ignores it.
    """

    tender = forms.ChoiceField(
        label=_("وجهة التحصيل"),
        choices=[(value, TenderDestination(value).label) for value in COUNTABLE_TENDERS],
    )
    counted_amount = forms.DecimalField(
        label=_("المبلغ المعدود"),
        min_value=Decimal("0"),
        decimal_places=3,
        help_text=_("ما في الدرج فعلاً. لا يُملأ مسبقاً بالمتوقَّع — العدّ هو الغاية."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, shift: CashierShift, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shift = shift


class CashierShiftCloseForm(forms.Form):
    """
    Name the posted day this drawer is reconciled against, and declare the count.

    The choices are **posted** days only, which is the same refusal
    `close_cashier_shift` makes with code `day_not_posted`: an expectation
    derived from a draft can move after the count, and the variance would then
    be a difference between a count and a target still being edited.

    A branch that has not posted its day sees an empty selector rather than a
    disabled button, so the reason the shift cannot close is on the screen.
    """

    sales_day = forms.ModelChoiceField(
        queryset=SalesDay.objects.none(),
        label=_("يوم المبيعات"),
        help_text=_("اليوم المرحّل الذي تُطابَق عليه الدرج. المسودة لا تصلح مرجعاً."),
    )
    notes = forms.CharField(
        label=_("ملاحظات الإقفال"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, shift: CashierShift, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shift = shift
        days = candidate_days(shift)
        self.fields["sales_day"].queryset = SalesDay.objects.filter(  # type: ignore[attr-defined]
            pk__in=[day.pk for day in days]
        )
        if len(days) == 1:
            self.fields["sales_day"].initial = days[0].pk


class ShiftReasonForm(forms.Form):
    """
    A typed reason, for the two transitions that need one.

    Reopening a closed shift and reversing an approved one are different acts
    with different consequences, and both need somebody's words: a hidden field
    holding a constant would satisfy the constraint while telling the next
    reader nothing about why a count was taken back.
    """

    reason = forms.CharField(label=_("السبب"), max_length=300)


__all__ = [
    "CashierShiftCloseForm",
    "CashierShiftForm",
    "ShiftReasonForm",
    "TenderCountForm",
]
