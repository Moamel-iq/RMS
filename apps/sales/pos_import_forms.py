from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django import forms

from apps.organizations.authorization import branches_with_permission
from apps.organizations.models import Branch
from apps.sales.permissions import CONFIRM_POS_SALES_IMPORT
from apps.sales.pos_imports import EXPECTED_REPORTS

if TYPE_CHECKING:
    from apps.users.models import User


class PosSalesImportForm(forms.Form):
    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label="الفرع")
    sales_items = forms.FileField(label="تقرير مبيعات الأصناف")
    sales_final = forms.FileField(label="التقرير الشامل")
    item_sales_by_type = forms.FileField(label="مبيعات الأصناف حسب نوع الطلب")
    sales_by_type = forms.FileField(label="المبيعات حسب نوع الطلب")
    sales_by_category = forms.FileField(label="المبيعات حسب المجموعة")
    expenses = forms.FileField(label="المصاريف وحركة التطبيقات")

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        branch_field = self.fields["branch"]
        if not isinstance(branch_field, forms.ModelChoiceField):
            raise TypeError("branch must be a ModelChoiceField")
        branch_field.queryset = branches_with_permission(actor, CONFIRM_POS_SALES_IMPORT).order_by(
            "code"
        )
        for key in EXPECTED_REPORTS:
            self.fields[key].widget.attrs.update({"accept": ".xlsx", "data-pos-report": key})

    def uploads(self) -> list[Any]:
        return [self.cleaned_data[key] for key in EXPECTED_REPORTS]


class CashierSalesConfirmationForm(forms.Form):
    confirmation = forms.BooleanField(
        label="أؤكد أن تاريخ المبيعات والأرقام والتقارير المرفوعة صحيحة",
        required=True,
    )


class PosImportReturnForm(forms.Form):
    reason = forms.CharField(
        label="سبب الإعادة إلى الكاشير",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class PosImportReviewStepForm(forms.Form):
    approved = forms.BooleanField(label="راجعت هذه الخطوة وأعتمد بياناتها", required=True)
    variance_reason = forms.CharField(
        label="سبب الفرق أو الملاحظة المحاسبية",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args: Any, batch: Any, step: int, **kwargs: Any) -> None:
        from apps.accounting.expense_services import EXPENSE_LINE_CLASSES
        from apps.accounting.models import Account, Cashbox, CostCenter
        from apps.sales.pos_closing import operational_expenses

        self.batch = batch
        self.step = step
        super().__init__(*args, **kwargs)
        if step == 2:
            self.fields["allocation_acknowledged"] = forms.BooleanField(
                label="أعتمد توزيع إجمالي قناة التطبيقات على التطبيقات وفق تقرير الذمم",
                required=True,
            )
        elif step == 3:
            accounts = Account.objects.filter(
                organization=batch.organization,
                is_active=True,
                is_postable=True,
                account_class__in=[value.value for value in EXPENSE_LINE_CLASSES],
            ).order_by("code")
            centers = CostCenter.objects.filter(
                organization=batch.organization, is_active=True
            ).order_by("code")
            for line in operational_expenses(batch):
                key = str(line.get("row"))
                self.fields[f"account_{key}"] = forms.ModelChoiceField(
                    label=f"حساب: {line.get('type')}", queryset=accounts, required=True
                )
                self.fields[f"cost_center_{key}"] = forms.ModelChoiceField(
                    label=f"مركز كلفة: {line.get('type')}", queryset=centers, required=True
                )
                self.fields[f"notes_{key}"] = forms.CharField(
                    label=f"ملاحظات: {line.get('type')}", required=False, max_length=300
                )
        elif step == 4:
            boxes = Cashbox.objects.filter(
                organization=batch.organization,
                branch=batch.branch,
                is_active=True,
            ).order_by("code")
            self.fields["cashbox"] = forms.ModelChoiceField(
                label="صندوق النقد", queryset=boxes, required=True
            )
            self.fields["qi_card_amount"] = forms.DecimalField(
                label="مبلغ كي كارد",
                min_value=0,
                max_digits=21,
                decimal_places=3,
                initial=0,
            )
            self.fields["qi_cashbox"] = forms.ModelChoiceField(
                label="صندوق كي كارد", queryset=boxes, required=False
            )
            self.fields["withdrawals"] = forms.DecimalField(
                label="السحوبات", min_value=0, max_digits=21, decimal_places=3, initial=0
            )
            self.fields["deposits"] = forms.DecimalField(
                label="الإيداعات", min_value=0, max_digits=21, decimal_places=3, initial=0
            )
            self.fields["actual_cash"] = forms.DecimalField(
                label="صافي الصندوق الفعلي", min_value=0, max_digits=21, decimal_places=3
            )

    def clean(self) -> dict[str, Any]:
        from decimal import Decimal

        cleaned = super().clean() or {}
        if self.step == 4:
            qi = cleaned.get("qi_card_amount") or Decimal("0")
            if qi and not cleaned.get("qi_cashbox"):
                self.add_error("qi_cashbox", "حدد الصندوق المخصص لكي كارد.")
            expected = (
                self.batch.total_sales
                - self.batch.application_sales
                - qi
                - self.batch.operational_expenses
                - (cleaned.get("withdrawals") or Decimal("0"))
                + (cleaned.get("deposits") or Decimal("0"))
            )
            actual = cleaned.get("actual_cash")
            if (
                actual is not None
                and actual != expected
                and not str(cleaned.get("variance_reason") or "").strip()
            ):
                self.add_error("variance_reason", "سبب فرق الصندوق مطلوب قبل المتابعة.")
            cleaned["expected_cash"] = expected
            cleaned["cash_variance"] = (actual - expected) if actual is not None else Decimal("0")
        return cleaned

    def evidence(self) -> dict[str, Any]:
        from decimal import Decimal

        if self.step == 3:
            from apps.sales.pos_closing import operational_expenses

            routes = {}
            for line in operational_expenses(self.batch):
                key = str(line.get("row"))
                routes[key] = {
                    "account_id": self.cleaned_data[f"account_{key}"].pk,
                    "cost_center_id": self.cleaned_data[f"cost_center_{key}"].pk,
                    "notes": self.cleaned_data.get(f"notes_{key}", ""),
                    "amount": str(line.get("amount")),
                }
            return {"approved": True, "routes": routes}
        evidence = {}
        for key, value in self.cleaned_data.items():
            if hasattr(value, "pk"):
                evidence[f"{key}_id"] = value.pk
            elif isinstance(value, Decimal):
                evidence[key] = str(value)
            else:
                evidence[key] = value
        return evidence


__all__ = [
    "CashierSalesConfirmationForm",
    "PosImportReturnForm",
    "PosImportReviewStepForm",
    "PosSalesImportForm",
]
