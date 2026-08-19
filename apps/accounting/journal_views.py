"""
قيود اليومية — the journal screens.

Two kinds of journal appear here and they behave differently on purpose.

A **system journal** was written by a document — a goods receipt, a supplier
invoice, a production batch, a sales day — and Accounting displays it and links
back to its source. It offers no edit, no post, no discard: not disabled
controls, no controls. The document owns its own lifecycle, and a second edit
path would be a second set of validation rules over one economic event, of
which the weaker one is the one that matters because it is the one an operator
finds.

A **manual journal** is the exception: a correction, a reclassification, an
opening balance, an adjustment an auditor asked for. It is the only thing these
screens can create, and it carries the two Phase 5 controls — the creator may
not post it, and a line landing on a controlled account needs its own authority
(ADR-029 §2).

The draft line editor is an inline sub-form on the detail page rather than a
spreadsheet widget, for the reason `apps/sales/day_views.py` records: a line
that can fail validation needs somewhere to say why, and a grid that accepted
twenty rows and rejected the eighteenth would lose the other nineteen.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.commands import (
    LineInput,
    amend_draft_entry,
    create_draft_entry,
    discard_draft_entry,
    post_journal_entry,
    reverse_journal_entry,
)
from apps.accounting.forms import JournalDraftForm, JournalLineForm, ReasonForm
from apps.accounting.models import (
    Account,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
    SourceEvent,
)
from apps.accounting.permissions import (
    CREATE_DRAFT,
    EDIT_DRAFT,
    POST_JOURNAL,
    REVERSE_JOURNAL,
    VIEW_JOURNAL,
)
from apps.accounting.selectors import account_balance
from apps.accounting.views import AccountingDetailView, AccountingListView, AccountingViewMixin
from apps.core.models import AuditEvent
from apps.organizations.authorization import (
    OutOfScope,
    has_branch_permission,
    organizations_with_permission,
)
from apps.organizations.models import Branch
from apps.organizations.selectors import accessible_branches

#: Where a system journal's source document lives, keyed by the **stored**
#: source-document type.
#:
#: Upper case, every one of them. `canonical_source_identity` case-folds the
#: type before persisting it, so a key spelled `sales.SalesDay` would never
#: match anything and every sales journal would silently lose its link — the
#: same trap that cost a Phase 4 reversal its own journal.
#:
#: One registry rather than a chain of `if` statements in the template, so a
#: module that adds a source type adds one line here and the detail page picks
#: it up. A type this does not know renders its identity as text; a broken link
#: is worse than a plain string, because it looks like it should work.
SOURCE_DOCUMENT_ROUTES: dict[str, str] = {
    "SALES.SALESDAY": "sales:day_detail",
    "SALES.SALESADJUSTMENT": "sales:adjustment_detail",
    "SALES.DELIVERYAPPLICATIONSETTLEMENT": "sales:settlement_detail",
    "SALES.CASHIERSHIFT": "sales:shift_detail",
    "PROCUREMENT.GOODSRECEIPT": "procurement:goods_receipt_detail",
    "PROCUREMENT.SUPPLIERINVOICE": "procurement:supplier_invoice_detail",
    "PROCUREMENT.SUPPLIERCREDITNOTE": "procurement:credit_note_detail",
    "PROCUREMENT.SUPPLIERPAYMENT": "procurement:payment_detail",
    "PROCUREMENT.SUPPLIERRETURN": "procurement:supplier_return_detail",
    "KITCHEN.PRODUCTIONBATCH": "kitchen:batch_detail",
}


def source_document_url(entry: JournalEntry) -> str | None:
    """The screen that owns this journal's source document, if there is one."""
    route = SOURCE_DOCUMENT_ROUTES.get(entry.source_document_type)
    if route is None or not entry.source_document_id:
        return None
    raw_id = entry.source_document_id.split(":", 1)[0]
    if not raw_id.isdigit():
        return None
    try:
        return reverse(route, args=[int(raw_id)])
    except NoReverseMatch:
        # The owning module's route was renamed. A missing link is recoverable;
        # a 500 on the journal detail page is not.
        return None


def visible_entries(actor: Any) -> QuerySet[JournalEntry]:
    """
    Every journal this caller may read, as a queryset.

    Organization authority sees the whole organization. Branch authority sees
    only entries whose lines all fall inside the branches they reach — an entry
    that also touches a branch they do not reach is hidden entirely, because a
    half-visible balanced entry reads as unbalanced.
    """
    organizations = organizations_with_permission(actor, VIEW_JOURNAL)
    branch_ids = set(accessible_branches(actor).values_list("id", flat=True))
    organization_ids = set(organizations.values_list("id", flat=True))
    organization_ids.update(
        Branch.objects.filter(pk__in=branch_ids).values_list("organization_id", flat=True)
    )

    entries = JournalEntry.objects.filter(organization_id__in=organization_ids)
    foreign = JournalLine.objects.exclude(branch_id__in=branch_ids).values("entry_id")
    return entries.exclude(
        Q(pk__in=foreign) & ~Q(organization_id__in=organizations.values_list("id", flat=True))
    ).select_related("organization", "period", "period__fiscal_year", "created_by", "posted_by")


class JournalListView(AccountingListView):
    """Every journal in view, with the filters an accountant actually reaches for."""

    template_name = "accounting/journal_list.html"
    context_object_name = "entries"
    required_permission = VIEW_JOURNAL
    page_title = _("قيود اليومية")
    page_hint = _(
        "القيد المُرحَّل لا يُعدَّل ولا يُحذف — يُعكس. القيود التي أنشأتها المستندات "
        "تُقرأ من هنا وتُدار من مستنداتها."
    )
    search_fields = ("entry_number", "narration", "source_document_id")
    search_placeholder = _("ابحث برقم القيد أو الشرح…")
    result_label = _("قيد")
    create_url_name = "accounting:journal_create"
    create_label = _("قيد يدوي جديد")
    manage_permission = CREATE_DRAFT
    manage_scope = "branch"
    paginate_by = 30

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_entries(self.actor)
        get = self.request.GET

        organization = get.get("organization", "").strip()
        if organization.isdigit():
            queryset = queryset.filter(organization_id=int(organization))

        branch = get.get("branch", "").strip()
        if branch.isdigit():
            queryset = queryset.filter(lines__branch_id=int(branch)).distinct()

        status = get.get("status", "").strip()
        if status in JournalEntryStatus.values:
            queryset = queryset.filter(status=status)

        source_type = get.get("source_type", "").strip()
        if source_type:
            queryset = queryset.filter(source_document_type=source_type)

        source_event = get.get("source_event", "").strip()
        if source_event in SourceEvent.values:
            queryset = queryset.filter(source_event=source_event)

        origin = get.get("origin", "").strip()
        if origin == "manual":
            queryset = queryset.filter(source_event="")
        elif origin == "system":
            queryset = queryset.exclude(source_event="")

        account = get.get("account", "").strip()
        if account.isdigit():
            queryset = queryset.filter(lines__account_id=int(account)).distinct()

        cost_center = get.get("cost_center", "").strip()
        if cost_center.isdigit():
            queryset = queryset.filter(lines__cost_center_id=int(cost_center)).distinct()

        if get.get("adjustment") == "1":
            queryset = queryset.filter(is_adjustment=True)

        reversed_filter = get.get("reversed", "").strip()
        if reversed_filter == "yes":
            queryset = queryset.filter(status=JournalEntryStatus.REVERSED)
        elif reversed_filter == "no":
            queryset = queryset.exclude(status=JournalEntryStatus.REVERSED)

        for key, lookup in (("from", "gte"), ("to", "lte")):
            raw = get.get(key, "").strip()
            if raw:
                try:
                    parsed = datetime.date.fromisoformat(raw)
                except ValueError:
                    continue
                queryset = queryset.filter(**{f"accounting_date__{lookup}": parsed})

        return queryset.order_by("-accounting_date", "-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        organizations = organizations_with_permission(self.actor, VIEW_JOURNAL).order_by("code")
        context["organizations"] = organizations
        context["branches"] = accessible_branches(self.actor).order_by("code")
        context["statuses"] = JournalEntryStatus.choices
        context["source_events"] = SourceEvent.choices
        context["source_types"] = sorted(
            JournalEntry.objects.filter(organization__in=organizations)
            .exclude(source_document_type="")
            .values_list("source_document_type", flat=True)
            .distinct()
        )
        context["cost_centers"] = CostCenter.objects.filter(
            organization__in=organizations, is_active=True
        ).order_by("code")
        for key in (
            "organization",
            "branch",
            "status",
            "source_type",
            "source_event",
            "origin",
            "account",
            "cost_center",
            "reversed",
            "from",
            "to",
        ):
            context[f"selected_{key}"] = self.request.GET.get(key, "")
        context["selected_adjustment"] = self.request.GET.get("adjustment", "")
        return context


class JournalCreateView(AccountingViewMixin, View):
    """
    Open a manual draft: organization, date, narration.

    Lines are added on the detail page rather than here. A create form that
    also collected lines would have to resolve accounts and branches before it
    knew which organization it was in, and would lose every line it had if one
    of them failed.
    """

    required_permission = CREATE_DRAFT
    template_name = "accounting/master_form.html"

    def _context(self, request: HttpRequest, form: JournalDraftForm) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("قيد يدوي جديد"),
            "page_hint": _(
                "القيد اليدوي لما لا مستند له: تصحيح، إعادة تصنيف، رصيد افتتاحي. "
                "المشتريات والمبيعات والإنتاج تُرحَّل من مستنداتها."
            ),
            "submit_label": _("فتح المسودة"),
            "cancel_url": reverse("accounting:journal_list"),
            "form_base_template": (
                "settings/_form_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(
            request, self.template_name, self._context(request, JournalDraftForm(actor=self.actor))
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = JournalDraftForm(data=request.POST, actor=self.actor)
        if form.is_valid():
            data = form.cleaned_data
            branch = data["branch"]
            try:
                entry = create_draft_entry(
                    actor=self.actor,
                    organization_id=branch.organization_id,
                    accounting_date=data["accounting_date"],
                    document_date=data.get("document_date") or data["accounting_date"],
                    narration=data["narration"],
                    is_adjustment=data.get("is_adjustment", False),
                    # One placeholder line, because the kernel refuses an entry
                    # with none and a draft has to exist before lines can be
                    # attached to it. Replaced entirely by the first real line.
                    lines=[
                        LineInput(
                            account_id=data["opening_account"].pk,
                            branch_id=branch.pk,
                            debit=data["opening_debit"],
                            credit=data["opening_credit"],
                            cost_center_id=(
                                data["opening_cost_center"].pk
                                if data.get("opening_cost_center")
                                else None
                            ),
                            narration=data["narration"],
                        )
                    ],
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("فُتحت المسودة. أضف بقية السطور."))
                return HttpResponseRedirect(reverse("accounting:journal_detail", args=[entry.pk]))
        return render(request, self.template_name, self._context(request, form))


class JournalDetailView(AccountingDetailView):
    """
    One journal: its lines, its provenance, and whatever it may do next.

    The action buttons are decided from the entry's own status **and** the
    caller's authority at the branches it touches, so a bookkeeper sees the
    line editor and not the post button. Hiding a button is presentation — the
    transition views refuse the same request either way — but offering a dead
    end is its own kind of wrong.
    """

    template_name = "accounting/journal_detail.html"
    required_permission = VIEW_JOURNAL

    def entry(self) -> JournalEntry:
        row = visible_entries(self.actor).filter(pk=self.kwargs["pk"]).first()
        if row is None:
            raise OutOfScope(_("Journal entry does not exist."))
        return row

    def _context(self, entry: JournalEntry, request: HttpRequest, **extra: Any) -> dict[str, Any]:
        lines = list(
            entry.lines.select_related("account", "branch", "cost_center").order_by("line_number")
        )
        branches = {line.branch for line in lines}
        may_edit = entry.is_editable and all(
            has_branch_permission(self.actor, EDIT_DRAFT, branch) for branch in branches
        )

        # The trial-balance impact preview: what each account's balance is now,
        # and what it becomes if this draft posts. Read-only, computed from the
        # ledger, and shown only for a draft — for a posted entry the "after"
        # is simply the balance.
        impact: list[dict[str, Any]] = []
        if entry.status == JournalEntryStatus.DRAFT:
            movement: dict[int, Decimal] = {}
            accounts: dict[int, Account] = {}
            for line in lines:
                accounts[line.account_id] = line.account
                movement[line.account_id] = (
                    movement.get(line.account_id, Decimal("0")) + line.debit - line.credit
                )
            for account_id, delta in movement.items():
                before = account_balance(account=accounts[account_id])
                impact.append(
                    {
                        "account": accounts[account_id],
                        "before": before,
                        "movement": delta,
                        "after": before + delta,
                    }
                )
            impact.sort(key=lambda row: row["account"].code)

        context: dict[str, Any] = {
            "entry": entry,
            "lines": lines,
            "total_debit": sum((line.debit for line in lines), Decimal("0")),
            "total_credit": sum((line.credit for line in lines), Decimal("0")),
            "impact": impact,
            "source_url": source_document_url(entry),
            "may_edit": may_edit,
            "may_post": entry.status == JournalEntryStatus.DRAFT
            and all(has_branch_permission(self.actor, POST_JOURNAL, branch) for branch in branches),
            "may_reverse": entry.status == JournalEntryStatus.POSTED
            and all(
                has_branch_permission(self.actor, REVERSE_JOURNAL, branch) for branch in branches
            ),
            "line_form": JournalLineForm(actor=self.actor, entry=entry) if may_edit else None,
            "reason_form": ReasonForm(),
            # `target_type` is stored as `app_label.ModelName` by
            # `record_audit_event`, with the model name in its original case.
            "timeline": AuditEvent.objects.filter(
                target_type="accounting.JournalEntry", target_id=str(entry.pk)
            )
            .select_related("actor")
            .order_by("-occurred_at")[:20],
            "page_title": (
                entry.entry_number
                if entry.entry_number
                else _("مسودة قيد #%(id)s") % {"id": entry.pk}
            ),
            "page_hint": _("القيد المُرحَّل لا يُعدَّل. التصحيح عكسٌ ثم قيد جديد."),
        }
        context.update(extra)
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self.render_detail(request, self._context(self.entry(), request))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Add a line to a draft. Every other mutation has its own route."""
        entry = self.entry()
        if not entry.is_editable:
            messages.error(request, _("هذا القيد ليس مسودة يدوية."))
            return HttpResponseRedirect(reverse("accounting:journal_detail", args=[entry.pk]))

        form = JournalLineForm(data=request.POST, actor=self.actor, entry=entry)
        if form.is_valid():
            data = form.cleaned_data
            existing = [
                LineInput(
                    account_id=line.account_id,
                    branch_id=line.branch_id,
                    debit=line.debit,
                    credit=line.credit,
                    cost_center_id=line.cost_center_id,
                    narration=line.narration,
                )
                for line in entry.lines.order_by("line_number")
            ]
            existing.append(
                LineInput(
                    account_id=data["account"].pk,
                    branch_id=data["branch"].pk,
                    debit=data["debit"],
                    credit=data["credit"],
                    cost_center_id=data["cost_center"].pk if data.get("cost_center") else None,
                    narration=data.get("narration", ""),
                )
            )
            try:
                amend_draft_entry(actor=self.actor, entry_id=entry.pk, lines=existing)
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُضيف السطر."))
                return HttpResponseRedirect(reverse("accounting:journal_detail", args=[entry.pk]))
        return self.render_detail(request, self._context(entry, request, line_form=form))


class JournalLineDeleteView(AccountingViewMixin, View):
    """POST-only: drop one line from a draft. A GET would fire on a link prefetch."""

    required_permission = EDIT_DRAFT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        line = JournalLine.objects.filter(pk=kwargs["pk"]).select_related("entry", "branch").first()
        if line is None or not visible_entries(self.actor).filter(pk=line.entry_id).exists():
            raise OutOfScope(_("Journal line does not exist."))

        entry = line.entry
        if not entry.is_editable:
            messages.error(request, _("هذا القيد ليس مسودة يدوية."))
            return HttpResponseRedirect(reverse("accounting:journal_detail", args=[entry.pk]))

        remaining = [
            LineInput(
                account_id=row.account_id,
                branch_id=row.branch_id,
                debit=row.debit,
                credit=row.credit,
                cost_center_id=row.cost_center_id,
                narration=row.narration,
            )
            for row in entry.lines.exclude(pk=line.pk).order_by("line_number")
        ]
        if not remaining:
            messages.error(
                request,
                _("لا يمكن حذف السطر الأخير. احذف المسودة كلها إن كانت خطأً."),
            )
            return HttpResponseRedirect(reverse("accounting:journal_detail", args=[entry.pk]))

        try:
            amend_draft_entry(actor=self.actor, entry_id=entry.pk, lines=remaining)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("حُذف السطر."))
        return HttpResponseRedirect(reverse("accounting:journal_detail", args=[entry.pk]))


class JournalTransitionView(AccountingViewMixin, View):
    """
    Post, reverse and discard — one view, three transitions.

    One view because the shape is identical: resolve the entry with the caller,
    check the authority that specific transition needs, call the command, turn a
    `ValidationError` into a readable message. Three views would be three copies
    of that with three chances to check the wrong permission.

    Every one of them refuses a **system** journal outright. That check lives
    here as well as in the command layer: a hand-made POST must be refused on
    its merits, not on whether the operator saw a button.
    """

    required_permission = VIEW_JOURNAL
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        entry = visible_entries(self.actor).filter(pk=kwargs["pk"]).first()
        if entry is None:
            raise OutOfScope(_("Journal entry does not exist."))

        reason = request.POST.get("reason", "").strip()
        detail = reverse("accounting:journal_detail", args=[entry.pk])

        if self.action in {"discard"} and not entry.is_manual:
            messages.error(
                request,
                _("هذا القيد أنشأه مستند. يُدار من مستنده، لا من المحاسبة."),
            )
            return HttpResponseRedirect(detail)

        try:
            if self.action == "post":
                post_journal_entry(actor=self.actor, entry_id=entry.pk, reason=reason)
                messages.success(request, _("رُحّل القيد."))
            elif self.action == "reverse":
                reversal = reverse_journal_entry(
                    actor=self.actor, entry_id=entry.pk, reason=reason or str(_("عكس يدوي"))
                )
                messages.success(request, _("عُكس القيد."))
                return HttpResponseRedirect(
                    reverse("accounting:journal_detail", args=[reversal.pk])
                )
            elif self.action == "discard":
                discard_draft_entry(actor=self.actor, entry_id=entry.pk, reason=reason)
                messages.success(request, _("حُذفت المسودة."))
                return HttpResponseRedirect(reverse("accounting:journal_list"))
            else:  # pragma: no cover - a routing mistake, not a state
                raise ValidationError(_("Unknown transition."), code="unknown_action")
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(detail)


__all__ = [
    "JournalCreateView",
    "JournalDetailView",
    "JournalLineDeleteView",
    "JournalListView",
    "JournalTransitionView",
    "source_document_url",
    "visible_entries",
]
