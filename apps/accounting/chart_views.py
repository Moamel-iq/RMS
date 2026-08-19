"""
دليل الحسابات — the chart of accounts screens.

Kept in their own module rather than added to `views.py`, which is the role and
mapping surface. These screens are about the *shape* of the chart, and the two
have little in common beyond the shell they render in.

The tree loads **lazily**: each group renders as a row with `hx-get` at its
children, and the children arrive as a fragment. Rendering the whole chart and
hiding it with CSS would ship several hundred rows to draw four, and the cost
grows with the chart rather than with what anybody looked at.

Every rule the forms enforce is enforced again by the service, because hiding a
control is presentation and a hand-made POST has to be refused on its merits.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.commands import (
    archive_chart_account,
    clear_account_report_mapping,
    create_chart_account,
    reactivate_chart_account,
    read_chart_account,
    set_account_report_mapping,
    update_chart_account,
)
from apps.accounting.forms import AccountForm, AccountMetadataForm, ReportMappingForm
from apps.accounting.models import (
    Account,
    AccountClass,
    AccountReportMapping,
    JournalEntryStatus,
    JournalLine,
    ManualPostingPolicy,
    OrganizationAccountMapping,
    PresentationSection,
    StatementGroup,
)
from apps.accounting.permissions import (
    MANAGE_CHART_OF_ACCOUNTS,
    MANAGE_REPORT_MAPPINGS,
    VIEW_CHART_OF_ACCOUNTS,
)
from apps.accounting.selectors import account_balance, account_balances, chart_tree
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

#: How many recent journal lines an account's detail page shows before it sends
#: the reader to the ledger. Enough to recognise the account's traffic, few
#: enough that the page stays a summary.
RECENT_LINE_LIMIT = 25


def _rows(accounts: Any, balances: dict[int, Decimal]) -> list[dict[str, Any]]:
    """
    Accounts paired with their balances, ready to render.

    The pairing happens here rather than in the template because a template
    cannot index a dict by a variable key without a custom filter, and the
    filter would be one more place for the lookup to go quietly wrong. An
    account with no posted line is absent from `balances`; it renders as an
    explicit zero rather than as a blank, because blank and zero are different
    claims about an account.
    """
    return [
        {"account": account, "balance": balances.get(account.pk, Decimal("0"))}
        for account in accounts
    ]


def _visible_accounts(actor: Any) -> QuerySet[Account]:
    """
    Every account in a chart this caller may read.

    The scope, expressed as a queryset. An account outside it is not filtered
    out later — it was never in the set, so a guessed primary key finds nothing
    and the view answers 404 rather than confirming the row exists.
    """
    return Account.objects.filter(
        organization__in=organizations_with_permission(actor, VIEW_CHART_OF_ACCOUNTS)
    ).select_related("organization", "parent")


class ChartTreeView(AccountingViewMixin, View):
    """
    The chart as a hierarchy, one organization at a time.

    Roots render eagerly; everything below arrives on demand. A chart is four
    levels deep and a few hundred rows wide, and the reader opens two of them.
    """

    required_permission = VIEW_CHART_OF_ACCOUNTS
    template_name = "accounting/chart_tree.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations = organizations_with_permission(self.actor, VIEW_CHART_OF_ACCOUNTS).order_by(
            "code"
        )
        chosen = request.GET.get("organization", "").strip()
        organization = (
            organizations.filter(pk=int(chosen)).first()
            if chosen.isdigit()
            else organizations.first()
        )
        if chosen.isdigit() and organization is None:
            raise Http404(_("Organization does not exist."))

        include_archived = request.GET.get("archived") == "1"
        balances = account_balances(organization=organization) if organization is not None else {}
        roots = (
            _rows(
                [
                    node.account
                    for node in chart_tree(
                        organization=organization, include_archived=include_archived
                    )
                ],
                balances,
            )
            if organization is not None
            else []
        )

        context = {
            "organization": organization,
            "organizations": organizations,
            "roots": roots,
            "include_archived": include_archived,
            "page_title": _("دليل الحسابات"),
            "page_hint": _(
                "الرمز يحمل المستوى: صنف، مجموعة، مجموعة فرعية، ثم حساب تفصيلي. "
                "الحساب التفصيلي وحده يقبل القيود، ولا يُحذف حساب بعد إنشائه."
            ),
            "may_manage": bool(
                organization is not None
                and has_organization_permission(self.actor, MANAGE_CHART_OF_ACCOUNTS, organization)
            ),
            "list_base_template": (
                "settings/_form_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
            "inventory_ui": False,
        }
        return render(request, self.template_name, context)


class ChartChildrenView(AccountingViewMixin, View):
    """One node's children, as an htmx fragment. The lazy half of the tree."""

    required_permission = VIEW_CHART_OF_ACCOUNTS
    template_name = "accounting/_chart_children.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        parent = _visible_accounts(self.actor).filter(pk=kwargs["pk"]).first()
        if parent is None:
            raise OutOfScope(_("Account does not exist."))

        include_archived = request.GET.get("archived") == "1"
        children = Account.objects.filter(parent=parent)
        if not include_archived:
            children = children.filter(is_active=True)
        children = children.order_by("code")

        return render(
            request,
            self.template_name,
            {
                "parent": parent,
                "children": _rows(children, account_balances(organization=parent.organization)),
                "include_archived": include_archived,
            },
        )


class ChartListView(AccountingListView):
    """The chart flat, searchable and filtered — the view for finding one account."""

    template_name = "accounting/chart_list.html"
    context_object_name = "accounts"
    required_permission = VIEW_CHART_OF_ACCOUNTS
    page_title = _("دليل الحسابات — قائمة")
    page_hint = _(
        "كل الحسابات في المؤسسات التي تصل إليها. الرمز رقم تقني: يُقرأ من اليسار "
        "إلى اليمين ولا تُجرى عليه عمليات حسابية."
    )
    search_fields = ("code", "name_ar", "name_en")
    search_placeholder = _("ابحث برمز الحساب أو اسمه…")
    result_label = _("حساب")
    create_url_name = "accounting:account_create"
    create_label = _("حساب جديد")
    manage_permission = MANAGE_CHART_OF_ACCOUNTS
    manage_scope = "organization"
    paginate_by = 50

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = _visible_accounts(self.actor)

        organization = self.request.GET.get("organization", "").strip()
        if organization.isdigit():
            queryset = queryset.filter(organization_id=int(organization))

        account_class = self.request.GET.get("account_class", "").strip()
        if account_class in AccountClass.values:
            queryset = queryset.filter(account_class=account_class)

        postable = self.request.GET.get("postable", "").strip()
        if postable == "yes":
            queryset = queryset.filter(is_postable=True)
        elif postable == "no":
            queryset = queryset.filter(is_postable=False)

        state = self.request.GET.get("state", "").strip()
        if state == "archived":
            queryset = queryset.filter(is_active=False)
        elif state != "all":
            queryset = queryset.filter(is_active=True)

        cost_center = self.request.GET.get("cost_center", "").strip()
        if cost_center == "required":
            queryset = queryset.filter(requires_cost_center=True)
        elif cost_center == "not_required":
            queryset = queryset.filter(requires_cost_center=False)

        policy = self.request.GET.get("policy", "").strip()
        if policy in ManualPostingPolicy.values:
            queryset = queryset.filter(manual_posting_policy=policy)

        group = self.request.GET.get("group", "").strip()
        if group in StatementGroup.values:
            queryset = queryset.filter(report_mappings__statement_group=group)
        elif group == "unmapped":
            queryset = queryset.filter(is_postable=True).filter(
                Q(report_mappings__isnull=True) | Q(report_mappings__is_active=False)
            )

        return queryset.order_by("organization__code", "code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["organizations"] = organizations_with_permission(
            self.actor, VIEW_CHART_OF_ACCOUNTS
        ).order_by("code")
        context["account_classes"] = AccountClass.choices
        context["policies"] = ManualPostingPolicy.choices
        context["statement_groups"] = StatementGroup.choices
        for key in (
            "organization",
            "account_class",
            "postable",
            "state",
            "cost_center",
            "policy",
            "group",
        ):
            context[f"selected_{key}"] = self.request.GET.get(key, "")

        # The statement group is attached to each row here rather than looked
        # up in the template: a Django template cannot index a dict by a
        # variable key without a custom filter, and the filter would be one
        # more place for the lookup to fail silently and read as "unmapped".
        rows = list(context.get(self.context_object_name) or [])
        mappings = {
            mapping.account_id: mapping
            for mapping in AccountReportMapping.objects.filter(account__in=rows, is_active=True)
        }
        for account in rows:
            account.report_mapping = mappings.get(account.pk)
        return context


class AccountCreateView(AccountingWriteView):
    """Add an account. The code decides the level; the service derives the rest."""

    form_class = AccountForm
    required_permission = MANAGE_CHART_OF_ACCOUNTS
    success_url_name = "accounting:chart_list"
    page_title = _("حساب جديد")
    page_hint = _(
        "الرمز يحدد المستوى والأب تلقائياً. لا يمكن إنشاء حساب تحت حساب تفصيلي، "
        "ولا جعل حساب أب قابلاً للترحيل."
    )
    success_message = _("أُضيف الحساب.")
    submit_label = _("إضافة")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        return self.form_class(data=data, actor=self.actor)

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, MANAGE_CHART_OF_ACCOUNTS, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = create_chart_account(
            actor=self.actor,
            organization_id=data["organization"].pk,
            code=data["code"],
            name_ar=data["name_ar"],
            name_en=data["name_en"],
            requires_cost_center=data["requires_cost_center"] or None,
            manual_posting_policy=data["manual_posting_policy"],
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("accounting:account_detail", args=[created.pk])
        return reverse(self.success_url_name)


class AccountUpdateView(AccountingWriteView):
    """
    Amend the metadata an account may safely change.

    Four fields, and the omissions are the design. Code, class, parent and
    postability decide what the account *means*; an account somebody has
    posted to cannot change its meaning without restating history, so those
    are not on the form and the service refuses them too.
    """

    form_class = AccountMetadataForm
    required_permission = MANAGE_CHART_OF_ACCOUNTS
    success_url_name = "accounting:chart_list"
    page_title = _("تعديل بيانات الحساب")
    page_hint = _(
        "الرمز والصنف والأب وقابلية الترحيل لا تتغيّر بعد الإنشاء: هي معنى الحساب، "
        "وتغييرها يعيد كتابة تاريخ مُرحَّل."
    )
    success_message = _("عُدِّلت بيانات الحساب.")

    def load(self) -> Any:
        account = _visible_accounts(self.actor).filter(pk=self.kwargs["pk"]).first()
        if account is None:
            raise OutOfScope(_("Account does not exist."))
        return account

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
            "requires_cost_center": instance.requires_cost_center,
            "manual_posting_policy": instance.manual_posting_policy,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(self.actor, MANAGE_CHART_OF_ACCOUNTS, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        update_chart_account(
            actor=self.actor,
            account_id=instance.pk,
            name_ar=data["name_ar"],
            name_en=data["name_en"],
            requires_cost_center=data["requires_cost_center"],
            manual_posting_policy=data["manual_posting_policy"],
            reason=data.get("reason", ""),
            # A seeded control account needs the structural authority as well.
            # Resolved here rather than assumed, so the command can refuse.
            allow_system=has_organization_permission(
                self.actor, MANAGE_CHART_OF_ACCOUNTS, instance.organization
            )
            and instance.is_system,
        )

    def get_success_url(self) -> str:
        return reverse("accounting:account_detail", args=[self.kwargs["pk"]])


class AccountArchiveView(AccountingViewMixin, View):
    """
    POST-only. An account is withdrawn from use, never deleted.

    Its code stays reserved: reusing `6-01-02-001` for something else would
    make every historical journal line that named it mean something new.
    """

    required_permission = MANAGE_CHART_OF_ACCOUNTS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            archive_chart_account(
                actor=self.actor,
                account_id=kwargs["pk"],
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("أُرشف الحساب."))
        return HttpResponseRedirect(reverse("accounting:account_detail", args=[kwargs["pk"]]))


class AccountReactivateView(AccountingViewMixin, View):
    """POST-only: return an archived account to use."""

    required_permission = MANAGE_CHART_OF_ACCOUNTS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            reactivate_chart_account(
                actor=self.actor,
                account_id=kwargs["pk"],
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("أُعيد تفعيل الحساب."))
        return HttpResponseRedirect(reverse("accounting:account_detail", args=[kwargs["pk"]]))


class AccountDetailView(AccountingDetailView):
    """
    One account: what it means, what it carries, and where that came from.

    The balance is derived here and not stored anywhere — `account_balance`
    over posted lines. There is no cached figure to disagree with the ledger,
    which is the whole reason this module has no balance column in any table.
    """

    template_name = "accounting/account_detail.html"
    required_permission = VIEW_CHART_OF_ACCOUNTS

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        account = read_chart_account(actor=self.actor, account_id=kwargs["pk"])

        parents: list[Account] = []
        node = account.parent
        while node is not None:
            parents.append(node)
            node = node.parent
        parents.reverse()

        recent = list(
            JournalLine.objects.filter(
                account=account,
                entry__status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
            )
            .select_related("entry", "branch", "cost_center")
            .order_by("-entry__accounting_date", "-entry__posted_at", "-entry__id", "line_number")[
                :RECENT_LINE_LIMIT
            ]
        )

        may_manage = has_organization_permission(
            self.actor, MANAGE_CHART_OF_ACCOUNTS, account.organization
        )
        return self.render_detail(
            request,
            {
                "account": account,
                "parents": parents,
                "children": list(account.children.order_by("code")),
                "balance": account_balance(account=account),
                "recent_lines": recent,
                "recent_limit": RECENT_LINE_LIMIT,
                "role_mappings": list(
                    OrganizationAccountMapping.objects.filter(account=account)
                    .select_related("account_role")
                    .order_by("account_role__code", "-version")
                ),
                "report_mapping": AccountReportMapping.objects.filter(
                    account=account, is_active=True
                ).first(),
                "mapping_form": ReportMappingForm(
                    initial=_report_mapping_initial(account),
                ),
                "may_manage": may_manage,
                "may_map_report": has_organization_permission(
                    self.actor, MANAGE_REPORT_MAPPINGS, account.organization
                ),
                "page_title": f"{account.code} — {account.name_ar}",
                "page_hint": _("الرصيد مشتق من القيود المُرحَّلة عند الطلب، وليس رقماً مخزّناً."),
                "as_of": timezone.localdate(),
            },
        )


def _report_mapping_initial(account: Account) -> dict[str, Any]:
    existing = AccountReportMapping.objects.filter(account=account, is_active=True).first()
    if existing is not None:
        return {
            "statement_group": existing.statement_group,
            "presentation_section": existing.presentation_section,
            "display_order": existing.display_order,
        }
    return {"presentation_section": PresentationSection.NOT_APPLICABLE, "display_order": 0}


class AccountReportMappingView(AccountingViewMixin, View):
    """
    Set or clear which statement group an account presents under (ADR-031).

    POST-only and rendered inside the account detail page, because it is one
    decision about one account rather than a screen of its own.
    """

    required_permission = MANAGE_REPORT_MAPPINGS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        account = _visible_accounts(self.actor).filter(pk=kwargs["pk"]).first()
        if account is None:
            raise OutOfScope(_("Account does not exist."))

        if request.POST.get("action") == "clear":
            existing = AccountReportMapping.objects.filter(account=account, is_active=True).first()
            if existing is not None:
                try:
                    clear_account_report_mapping(actor=self.actor, mapping_id=existing.pk)
                except ValidationError as error:
                    messages.error(request, "؛ ".join(str(message) for message in error.messages))
                else:
                    messages.success(request, _("أُلغي التصنيف."))
            return HttpResponseRedirect(reverse("accounting:account_detail", args=[account.pk]))

        form = ReportMappingForm(data=request.POST)
        if form.is_valid():
            try:
                set_account_report_mapping(
                    actor=self.actor,
                    organization_id=account.organization_id,
                    account_id=account.pk,
                    statement_group=form.cleaned_data["statement_group"],
                    presentation_section=form.cleaned_data["presentation_section"],
                    display_order=form.cleaned_data["display_order"],
                )
            except ValidationError as error:
                messages.error(request, "؛ ".join(str(message) for message in error.messages))
            else:
                messages.success(request, _("حُفظ التصنيف."))
        else:
            messages.error(request, _("تحقّق من قيم التصنيف."))
        return HttpResponseRedirect(reverse("accounting:account_detail", args=[account.pk]))


class AccountActivityView(AccountingDetailView):
    """
    One account's posted lines, dated, with a running balance.

    A summary rather than the ledger: the full report with its filters, its
    CSV and its source drill-down is `دفتر الأستاذ`, and this page links there.
    The running balance is accumulated **here**, in Python, because the order
    it accumulates in is the whole content of the column.
    """

    template_name = "accounting/account_activity.html"
    required_permission = VIEW_CHART_OF_ACCOUNTS

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        account = read_chart_account(actor=self.actor, account_id=kwargs["pk"])

        raw_from = request.GET.get("from", "").strip()
        raw_to = request.GET.get("to", "").strip()
        today = timezone.localdate()
        try:
            date_to = datetime.date.fromisoformat(raw_to) if raw_to else today
        except ValueError:
            date_to = today
        try:
            date_from = (
                datetime.date.fromisoformat(raw_from) if raw_from else date_to.replace(day=1)
            )
        except ValueError:
            date_from = date_to.replace(day=1)

        opening = account_balance(account=account, up_to=date_from - datetime.timedelta(days=1))
        lines = (
            JournalLine.objects.filter(
                account=account,
                entry__status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
                entry__accounting_date__gte=date_from,
                entry__accounting_date__lte=date_to,
            )
            .select_related("entry", "branch", "cost_center")
            .order_by(
                "entry__accounting_date", "entry__posted_at", "entry__entry_number", "line_number"
            )
        )

        running = opening
        rows = []
        for line in lines:
            running = running + line.debit - line.credit
            rows.append({"line": line, "running": running})

        return self.render_detail(
            request,
            {
                "account": account,
                "rows": rows,
                "opening": opening,
                "closing": running,
                "date_from": date_from,
                "date_to": date_to,
                "page_title": _("حركة الحساب %(code)s") % {"code": account.code},
                "page_hint": _(
                    "الترتيب: تاريخ العملية ثم وقت الترحيل ثم رقم القيد ثم رقم السطر — "
                    "وهو ما يجعل الرصيد المتحرك صحيحاً."
                ),
            },
        )


__all__ = [
    "AccountActivityView",
    "AccountArchiveView",
    "AccountCreateView",
    "AccountDetailView",
    "AccountReactivateView",
    "AccountReportMappingView",
    "AccountUpdateView",
    "ChartChildrenView",
    "ChartListView",
    "ChartTreeView",
]
