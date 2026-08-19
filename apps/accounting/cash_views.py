"""
الصناديق and الحسابات البنكية — the cash and bank screens.

Both pages answer the same question in two shapes: what is in this account, how
did it get there, and does that agree with what somebody counted. The balance
is `account_balance` over posted lines, computed when the page is requested —
there is no stored figure anywhere in this module that could disagree with the
ledger (ADR-030 §1).

The bank page shows supplier payments and application settlements because those
are what actually move a restaurant's bank balance, and it shows them
**read-only**: they belong to Procurement and Sales, and this module reads their
selectors and never calls `save()` on one of their rows (ADR-029 §6). That is
the first place `apps.accounting` imports either module, and the boundary is
one-way by construction — neither imports this back.

There is deliberately **no bank-statement import** and no disabled button
offering one. A greyed-out control is a promise the system does not keep, and
an operator who plans a month's reconciliation around it finds out at the worst
moment (ADR-030 §2).
"""

from __future__ import annotations

import datetime
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.cash_forms import (
    BankAccountForm,
    BankAccountMetadataForm,
    CashboxForm,
    CashboxMetadataForm,
)
from apps.accounting.commands import (
    amend_bank_account,
    amend_cashbox,
    mark_cash_record_reconciled,
    register_bank_account,
    register_cashbox,
    restore_bank_account,
    restore_cashbox,
    withdraw_bank_account,
    withdraw_cashbox,
)
from apps.accounting.models import BankAccount, Cashbox
from apps.accounting.permissions import MANAGE_BANK_ACCOUNTS, MANAGE_CASHBOXES
from apps.accounting.selectors import account_balance
from apps.accounting.statements import account_statement, parse_window
from apps.accounting.views import (
    AccountingDetailView,
    AccountingListView,
    AccountingViewMixin,
    AccountingWriteView,
)
from apps.organizations.authorization import (
    OutOfScope,
    has_organization_permission,
    organizations_with_permission,
    require_organization_permission,
)

#: How many source documents each read-only panel on the bank page shows.
#: Enough to recognise the traffic, few enough that the page stays a summary.
SOURCE_PANEL_LIMIT = 10


def _visible_cashboxes(actor: Any) -> QuerySet[Cashbox]:
    return Cashbox.objects.filter(
        organization__in=organizations_with_permission(actor, MANAGE_CASHBOXES)
    ).select_related("organization", "branch", "account")


def _visible_banks(actor: Any) -> QuerySet[BankAccount]:
    return BankAccount.objects.filter(
        organization__in=organizations_with_permission(actor, MANAGE_BANK_ACCOUNTS)
    ).select_related("organization", "branch", "account")


# ---------------------------------------------------------------------------
# الصناديق
# ---------------------------------------------------------------------------


class CashboxListView(AccountingListView):
    template_name = "accounting/cashbox_list.html"
    context_object_name = "cashboxes"
    required_permission = MANAGE_CASHBOXES
    page_title = _("الصناديق")
    page_hint = _(
        "كل صندوق مرتبط بحساب نقدية واحد، والرصيد مشتق من القيود المُرحَّلة — "
        "لا يوجد رصيد مخزّن يمكن أن يخالف دفتر الأستاذ."
    )
    search_fields = ("code", "name_ar", "name_en", "account__code")
    search_placeholder = _("ابحث برمز الصندوق أو اسمه…")
    result_label = _("صندوق")
    create_url_name = "accounting:cashbox_create"
    create_label = _("صندوق جديد")
    manage_permission = MANAGE_CASHBOXES
    manage_scope = "organization"

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = _visible_cashboxes(self.actor)
        branch = self.request.GET.get("branch", "").strip()
        if branch.isdigit():
            queryset = queryset.filter(branch_id=int(branch))
        state = self.request.GET.get("state", "").strip()
        if state == "archived":
            queryset = queryset.filter(is_active=False)
        elif state != "all":
            queryset = queryset.filter(is_active=True)
        return queryset.order_by("organization__code", "code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        rows = list(context.get(self.context_object_name) or [])
        # One balance query per row is acceptable at this scale — a chart has
        # hundreds of accounts, an organization has a handful of drawers — and
        # it keeps the figure identical to the one the detail page derives.
        for cashbox in rows:
            cashbox.derived_balance = account_balance(
                account=cashbox.account, branch=cashbox.branch
            )
        context["branches"] = sorted(
            {cashbox.branch for cashbox in _visible_cashboxes(self.actor)},
            key=lambda branch: branch.code,
        )
        context["selected_branch"] = self.request.GET.get("branch", "")
        context["selected_state"] = self.request.GET.get("state", "")
        return context


class CashboxCreateView(AccountingWriteView):
    form_class = CashboxForm
    required_permission = MANAGE_CASHBOXES
    success_url_name = "accounting:cashbox_list"
    page_title = _("صندوق جديد")
    page_hint = _(
        "الحساب المختار يصبح دفتر الصندوق. لا يمكن ربط حسابين بصندوق واحد ولا "
        "صندوقين فعّالين بحساب واحد."
    )
    success_message = _("سُجِّل الصندوق.")
    submit_label = _("تسجيل")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        return self.form_class(data=data, actor=self.actor)

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, MANAGE_CASHBOXES, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = register_cashbox(
            actor=self.actor,
            organization_id=data["organization"].pk,
            branch_id=data["branch"].pk,
            account_id=data["account"].pk,
            code=data["code"],
            name_ar=data["name_ar"],
            name_en=data["name_en"],
            opened_on=data["opened_on"],
            responsible_note=data.get("responsible_note", ""),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("accounting:cashbox_detail", args=[created.pk])
        return reverse(self.success_url_name)


class CashboxUpdateView(AccountingWriteView):
    form_class = CashboxMetadataForm
    required_permission = MANAGE_CASHBOXES
    success_url_name = "accounting:cashbox_list"
    page_title = _("تعديل بيانات الصندوق")
    page_hint = _(
        "الحساب والفرع لا يتغيّران: صندوق غيّر حسابه يعيد نسبة كل كشف عرضه من قبل. "
        "التغيير الحقيقي أرشفةٌ وتسجيلُ صندوق جديد."
    )
    success_message = _("عُدِّلت بيانات الصندوق.")

    def load(self) -> Any:
        row = _visible_cashboxes(self.actor).filter(pk=self.kwargs["pk"]).first()
        if row is None:
            raise OutOfScope(_("Cashbox does not exist."))
        return row

    def build_form(self, instance: Any, data: Any = None) -> Any:
        if data is not None:
            return self.form_class(data=data, actor=self.actor, instance=instance)
        return self.form_class(
            actor=self.actor, instance=instance, initial=self.initial_for(instance)
        )

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "responsible_note": instance.responsible_note,
            "notes": instance.notes,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(self.actor, MANAGE_CASHBOXES, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        amend_cashbox(
            actor=self.actor,
            cashbox_id=instance.pk,
            name_ar=data["name_ar"],
            name_en=data["name_en"],
            responsible_note=data.get("responsible_note", ""),
            notes=data.get("notes", ""),
            reason=data.get("reason", ""),
        )

    def get_success_url(self) -> str:
        return reverse("accounting:cashbox_detail", args=[self.kwargs["pk"]])


class CashboxActionView(AccountingViewMixin, View):
    """POST-only: archive, reactivate, or stamp a reconciliation date."""

    required_permission = MANAGE_CASHBOXES
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        pk = kwargs["pk"]
        try:
            if self.action == "archive":
                withdraw_cashbox(
                    actor=self.actor, cashbox_id=pk, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("أُرشف الصندوق."))
            elif self.action == "reactivate":
                restore_cashbox(
                    actor=self.actor, cashbox_id=pk, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("أُعيد تفعيل الصندوق."))
            elif self.action == "reconcile":
                raw = request.POST.get("on_date", "").strip()
                on_date = datetime.date.fromisoformat(raw) if raw else timezone.localdate()
                mark_cash_record_reconciled(
                    actor=self.actor,
                    kind="cashbox",
                    record_id=pk,
                    on_date=on_date,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(request, _("سُجِّلت المطابقة."))
        except (ValidationError, ValueError) as error:
            detail = getattr(error, "messages", [str(error)])
            messages.error(request, "؛ ".join(str(message) for message in detail))
        return HttpResponseRedirect(reverse("accounting:cashbox_detail", args=[pk]))


class CashboxDetailView(AccountingDetailView):
    """One drawer: its balance, its movement, and the till closings behind it."""

    template_name = "accounting/cashbox_detail.html"
    required_permission = MANAGE_CASHBOXES

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        cashbox = _visible_cashboxes(self.actor).filter(pk=kwargs["pk"]).first()
        if cashbox is None:
            raise OutOfScope(_("Cashbox does not exist."))

        date_from, date_to = parse_window(
            request.GET.get("from", ""), request.GET.get("to", ""), today=timezone.localdate()
        )
        statement = account_statement(
            account=cashbox.account,
            date_from=date_from,
            date_to=date_to,
            branch=cashbox.branch,
        )

        # Cashier closings for this branch, read-only. Sales owns them; this
        # page links out rather than offering any action over them.
        from apps.sales.models import CashierShift

        shifts = list(
            CashierShift.objects.filter(branch=cashbox.branch)
            .select_related("branch")
            .order_by("-business_date", "-id")[:SOURCE_PANEL_LIMIT]
        )

        return self.render_detail(
            request,
            {
                "cashbox": cashbox,
                "statement": statement,
                "current_balance": account_balance(account=cashbox.account, branch=cashbox.branch),
                "shifts": shifts,
                "may_manage": has_organization_permission(
                    self.actor, MANAGE_CASHBOXES, cashbox.organization
                ),
                "today": timezone.localdate(),
                "page_title": _("صندوق %(code)s") % {"code": cashbox.code},
                "page_hint": _(
                    "الترتيب: تاريخ العملية ثم وقت الترحيل ثم رقم القيد ثم رقم السطر — "
                    "وهو ما يجعل الرصيد المتحرك صحيحاً."
                ),
            },
        )


# ---------------------------------------------------------------------------
# الحسابات البنكية
# ---------------------------------------------------------------------------


class BankAccountListView(AccountingListView):
    template_name = "accounting/bank_account_list.html"
    context_object_name = "banks"
    required_permission = MANAGE_BANK_ACCOUNTS
    page_title = _("الحسابات البنكية")
    page_hint = _(
        "رقم الحساب يُخزَّن مقنَّعاً. الرصيد مشتق من القيود المُرحَّلة، ولا يوجد "
        "استيراد كشوف في الإصدار الأول."
    )
    search_fields = ("code", "name_ar", "name_en", "bank_name", "account__code")
    search_placeholder = _("ابحث بالاسم أو المصرف…")
    result_label = _("حساب")
    create_url_name = "accounting:bank_account_create"
    create_label = _("حساب بنكي جديد")
    manage_permission = MANAGE_BANK_ACCOUNTS
    manage_scope = "organization"

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = _visible_banks(self.actor)
        state = self.request.GET.get("state", "").strip()
        if state == "archived":
            queryset = queryset.filter(is_active=False)
        elif state != "all":
            queryset = queryset.filter(is_active=True)
        return queryset.order_by("organization__code", "code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        for bank in context.get(self.context_object_name) or []:
            bank.derived_balance = account_balance(account=bank.account)
        context["selected_state"] = self.request.GET.get("state", "")
        return context


class BankAccountCreateView(AccountingWriteView):
    form_class = BankAccountForm
    required_permission = MANAGE_BANK_ACCOUNTS
    success_url_name = "accounting:bank_account_list"
    page_title = _("حساب بنكي جديد")
    page_hint = _("رقم الحساب يُقنَّع تلقائياً: يُحفظ آخر أربعة أرقام فقط.")
    success_message = _("سُجِّل الحساب البنكي.")
    submit_label = _("تسجيل")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        return self.form_class(data=data, actor=self.actor)

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, MANAGE_BANK_ACCOUNTS, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        branch = data.get("branch")
        self.created = register_bank_account(
            actor=self.actor,
            organization_id=data["organization"].pk,
            branch_id=branch.pk if branch else None,
            account_id=data["account"].pk,
            code=data["code"],
            bank_name=data["bank_name"],
            name_ar=data["name_ar"],
            name_en=data["name_en"],
            masked_account_number=data["masked_account_number"],
            iban=data.get("iban", ""),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("accounting:bank_account_detail", args=[created.pk])
        return reverse(self.success_url_name)


class BankAccountUpdateView(AccountingWriteView):
    form_class = BankAccountMetadataForm
    required_permission = MANAGE_BANK_ACCOUNTS
    success_url_name = "accounting:bank_account_list"
    page_title = _("تعديل الحساب البنكي")
    page_hint = _("الحساب المحاسبي لا يتغيّر — الأرشفة والتسجيل من جديد هو المسار.")
    success_message = _("عُدِّل الحساب البنكي.")

    def load(self) -> Any:
        row = _visible_banks(self.actor).filter(pk=self.kwargs["pk"]).first()
        if row is None:
            raise OutOfScope(_("Bank account does not exist."))
        return row

    def build_form(self, instance: Any, data: Any = None) -> Any:
        if data is not None:
            return self.form_class(data=data, actor=self.actor, instance=instance)
        return self.form_class(
            actor=self.actor, instance=instance, initial=self.initial_for(instance)
        )

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "bank_name": instance.bank_name,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "masked_account_number": instance.masked_account_number,
            "iban": instance.iban,
            "notes": instance.notes,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(self.actor, MANAGE_BANK_ACCOUNTS, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        amend_bank_account(
            actor=self.actor,
            bank_id=instance.pk,
            bank_name=data["bank_name"],
            name_ar=data["name_ar"],
            name_en=data["name_en"],
            masked_account_number=data["masked_account_number"],
            iban=data.get("iban", ""),
            notes=data.get("notes", ""),
            reason=data.get("reason", ""),
        )

    def get_success_url(self) -> str:
        return reverse("accounting:bank_account_detail", args=[self.kwargs["pk"]])


class BankAccountActionView(AccountingViewMixin, View):
    """POST-only: archive, reactivate, or stamp a reconciliation date."""

    required_permission = MANAGE_BANK_ACCOUNTS
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        pk = kwargs["pk"]
        try:
            if self.action == "archive":
                withdraw_bank_account(
                    actor=self.actor, bank_id=pk, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("أُرشف الحساب البنكي."))
            elif self.action == "reactivate":
                restore_bank_account(
                    actor=self.actor, bank_id=pk, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("أُعيد تفعيل الحساب البنكي."))
            elif self.action == "reconcile":
                raw = request.POST.get("on_date", "").strip()
                on_date = datetime.date.fromisoformat(raw) if raw else timezone.localdate()
                mark_cash_record_reconciled(
                    actor=self.actor,
                    kind="bank",
                    record_id=pk,
                    on_date=on_date,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(request, _("سُجِّلت المطابقة."))
        except (ValidationError, ValueError) as error:
            detail = getattr(error, "messages", [str(error)])
            messages.error(request, "؛ ".join(str(message) for message in detail))
        return HttpResponseRedirect(reverse("accounting:bank_account_detail", args=[pk]))


class BankAccountDetailView(AccountingDetailView):
    """
    One bank account: its movement, and the documents that caused it.

    Supplier payments and application settlements are shown because those are
    what actually move the balance. Both panels are read-only and link to the
    owning module — Accounting reports what is there and never edits it.
    """

    template_name = "accounting/bank_account_detail.html"
    required_permission = MANAGE_BANK_ACCOUNTS

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        bank = _visible_banks(self.actor).filter(pk=kwargs["pk"]).first()
        if bank is None:
            raise OutOfScope(_("Bank account does not exist."))

        date_from, date_to = parse_window(
            request.GET.get("from", ""), request.GET.get("to", ""), today=timezone.localdate()
        )
        statement = account_statement(account=bank.account, date_from=date_from, date_to=date_to)

        from apps.procurement.models import SupplierPayment
        from apps.sales.models import DeliveryApplicationSettlement

        payments = list(
            SupplierPayment.objects.filter(organization=bank.organization)
            .select_related("supplier")
            .order_by("-business_date", "-id")[:SOURCE_PANEL_LIMIT]
        )
        settlements = list(
            DeliveryApplicationSettlement.objects.filter(organization=bank.organization)
            .select_related("delivery_application")
            .order_by("-period_end", "-id")[:SOURCE_PANEL_LIMIT]
        )

        # An "unreconciled item" here is a posted line dated after the last
        # reconciliation. Not a stored flag on the line — the ledger is
        # append-only and a per-line tick would be a second mutable state to
        # keep in step with it.
        unreconciled = [
            row
            for row in statement.rows
            if bank.last_reconciled_on is None
            or row.line.entry.accounting_date > bank.last_reconciled_on
        ]

        return self.render_detail(
            request,
            {
                "bank": bank,
                "statement": statement,
                "current_balance": account_balance(account=bank.account),
                "payments": payments,
                "settlements": settlements,
                "unreconciled": unreconciled,
                "may_manage": has_organization_permission(
                    self.actor, MANAGE_BANK_ACCOUNTS, bank.organization
                ),
                "today": timezone.localdate(),
                "page_title": _("حساب %(code)s") % {"code": bank.code},
                "page_hint": _(
                    "البنود غير المطابقة هي السطور المُرحَّلة بعد تاريخ آخر مطابقة — "
                    "مشتقّة، لا علامة مخزّنة على كل سطر."
                ),
            },
        )


__all__ = [
    "BankAccountActionView",
    "BankAccountCreateView",
    "BankAccountDetailView",
    "BankAccountListView",
    "BankAccountUpdateView",
    "CashboxActionView",
    "CashboxCreateView",
    "CashboxDetailView",
    "CashboxListView",
    "CashboxUpdateView",
]
