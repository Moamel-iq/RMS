"""
Accounting screens, inside the Khan Mandi shell.

The list, write and action machinery is reused from `apps.inventory.views`
rather than copied, for the reason `apps.procurement.views` and
`apps.sales.views` both record: it is generic — a scoped queryset, a per-row
action decision, an htmx partial swap, a POST-only archive — and a second copy
would drift within two tasks, in the authorization behaviour, which is the part
that must not vary.

**Accounting gets its own form template** (`accounting/master_form.html`) rather
than reusing inventory's. Inventory's write template extends `shell.html`
directly, which is correct for inventory and wrong for a module whose forms open
inside htmx panels: a fragment carrying a second shell looks right until
somebody swaps it.

## The scope tightening

The Task 1.3 screens gated themselves with `user.has_perm(...)` alone. That is a
**global** answer — Django recomputes role groups from every membership a user
holds — to a question that is always local, so a viewer in one organization who
held accounting authority in another satisfied it everywhere. Phase 5 moves
every accounting view onto `apps/organizations/authorization.py`, where the
permission must be carried by a role held *inside the target organization*.

That is a tightening, not a refactor. Somebody who could previously reach the
mapping screen through authority held elsewhere now cannot. See ADR-029 §7.

Same discipline as the other modules: no view calls `form.save()`, every
mutation goes through `apps/accounting/commands.py`, and hiding a button is
presentation, never protection.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

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
    amend_account_role_mapping,
    archive_account_role_mapping,
    close_account_role_mapping,
    map_account_role,
)
from apps.accounting.forms import (
    AccountMappingForm,
    AmendMappingForm,
    CloseMappingForm,
    manageable_organizations,
)
from apps.accounting.models import (
    AccountRole,
    AccountRoleDomain,
    AccountRoleMappingScope,
    OrganizationAccountMapping,
)
from apps.accounting.permissions import (
    MANAGE_ACCOUNT_MAPPINGS,
    VIEW_CHART_OF_ACCOUNTS,
    VIEW_JOURNAL,
)
from apps.accounting.selectors import (
    mapping_continuity_gaps,
    mapping_history,
    role_usage,
)
from apps.accounting.services import mapping_is_used, resolve_default_account
from apps.inventory.views import (
    InventoryActionView,
    InventoryListView,
    InventoryViewMixin,
    InventoryWriteView,
)
from apps.organizations.authorization import (
    OutOfScope,
    has_organization_permission,
    organizations_with_permission,
    require_organization_permission,
)
from apps.organizations.models import Organization

if TYPE_CHECKING:
    from apps.users.models import User


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


class AccountingViewMixin(InventoryViewMixin):
    """Signed in, holding the permission this screen needs; 404 for foreign scope."""

    module_key = "accounting"
    required_permission: str = VIEW_JOURNAL

    def visible_organizations(self, permission: str | None = None) -> QuerySet[Organization]:
        """
        The organizations this caller may read through, as a queryset.

        A queryset rather than a list because it is the *scope*: every
        accounting selector filters through it, so an id from a request can
        only ever select something already inside it. Fetching first and
        checking afterwards is the shape of the bug this avoids.
        """
        return organizations_with_permission(
            self.actor, permission or self.required_permission
        ).order_by("code")


class AccountingListView(InventoryListView):
    """Every accounting list: same scoping, same htmx contract, same row actions."""

    module_key = "accounting"
    required_permission = VIEW_JOURNAL

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Accounting keeps the shared semantic structure and its own styling,
        # exactly as Settings, Procurement, Kitchen and Sales do. Inventory's
        # direction and density rules are inventory's.
        context["inventory_ui"] = False
        return context

    def visible_organizations(self, permission: str | None = None) -> QuerySet[Organization]:
        return organizations_with_permission(
            self.actor, permission or self.required_permission
        ).order_by("code")


class AccountingWriteView(InventoryWriteView):
    """
    Every accounting create and edit screen.

    Supplies `form_base_template` so an htmx GET answers with the form alone.
    Without it a panel swap receives a whole document — two `<html>` elements,
    two navigation rails — which renders correctly enough to be missed in review
    and is wrong in every accessibility tree.
    """

    module_key = "accounting"
    template_name = "accounting/master_form.html"
    submit_label: Any = _("حفظ")

    def is_htmx(self) -> bool:
        return self.request.headers.get("HX-Request") == "true"

    def context(self, instance: Any, form: Any) -> dict[str, Any]:
        context = super().context(instance, form)
        context["submit_label"] = self.submit_label
        context["form_base_template"] = (
            "settings/_form_fragment.html" if self.is_htmx() else "shell.html"
        )
        return context


class AccountingActionView(InventoryActionView):
    module_key = "accounting"


class AccountingDetailView(AccountingViewMixin, View):
    """
    Base for every accounting detail page.

    A detail template extends its base **directly** rather than through
    `settings/base_list.html`, so the block it defines is `page` — and the
    fragment it must be paired with is `settings/_form_fragment.html`, which
    declares `page`. `_list_fragment.html` declares only `results`; Django
    silently drops a child block the parent does not declare, so pairing a
    detail page with it answers 200 with a whitespace body.

    Which fragment a screen needs is decided by the block it defines, never by
    whether the screen is conceptually a list or a form. Centralised here so
    no individual detail screen has to remember it.
    """

    template_name: str = ""

    def base_template(self) -> str:
        if self.request.headers.get("HX-Request") == "true":
            return "settings/_form_fragment.html"
        return "shell.html"

    def render_detail(self, request: HttpRequest, context: dict[str, Any]) -> HttpResponse:
        context.setdefault("list_base_template", self.base_template())
        context.setdefault("inventory_ui", False)
        return render(request, self.template_name, context)


# ---------------------------------------------------------------------------
# الأدوار المحاسبية — the system vocabulary
# ---------------------------------------------------------------------------


class AccountRoleListView(AccountingListView):
    """
    Every role posting rules can name, and how it is mapped where this caller
    can see.

    Read-only by construction. `AccountRole` is system-controlled vocabulary:
    `INVENTORY_CONTROL` means the same thing in every organization, and which
    *account* carries it is the organization's decision, recorded in
    `OrganizationAccountMapping`. So this screen offers no create, no rename,
    no delete — not disabled buttons, no buttons — and the only thing it links
    to is the mapping that is genuinely the organization's to change.

    The column that earns the screen is **unresolved organizations**. A role
    with no mapping in effect is not visibly broken anywhere: the failure
    arrives the first time somebody posts a document that resolves it, which is
    days later and looks like a posting bug.
    """

    template_name = "accounting/role_list.html"
    context_object_name = "roles"
    page_title = _("الأدوار المحاسبية")
    page_hint = _(
        "مفردات النظام: قواعد الترحيل تشير إلى الدور، والمؤسسة تقرر أي حساب يحمله. "
        "الأدوار محمية ولا تُنشأ ولا تُحذف من هنا — الذي يتغيّر هو الربط."
    )
    search_placeholder = _("ابحث برمز الدور أو اسمه…")
    result_label = _("دور")
    paginate_by = 40

    def as_of(self) -> datetime.date:
        raw = self.request.GET.get("as_of", "").strip()
        if raw:
            try:
                return datetime.date.fromisoformat(raw)
            except ValueError:
                pass
        return timezone.localdate()

    def _organizations(self) -> list[Organization]:
        organizations = list(self.visible_organizations(VIEW_CHART_OF_ACCOUNTS))
        chosen = self.request.GET.get("organization", "").strip()
        if chosen.isdigit():
            organizations = [
                organization for organization in organizations if organization.pk == int(chosen)
            ]
        return organizations

    def get_queryset(self) -> Any:
        rows = role_usage(organizations=self._organizations(), on_date=self.as_of())

        search = self.request.GET.get("q", "").strip().lower()
        domain = self.request.GET.get("domain", "").strip()
        scope = self.request.GET.get("scope", "").strip()
        mapped = self.request.GET.get("mapped", "").strip()

        if search:
            rows = [
                row
                for row in rows
                if search in row.role.code.lower()
                or search in row.role.name_ar
                or search in row.role.name_en.lower()
            ]
        if domain in AccountRoleDomain.values:
            rows = [row for row in rows if row.role.domain == domain]
        if scope in AccountRoleMappingScope.values:
            rows = [row for row in rows if row.role.mapping_scope == scope]
        if mapped == "mapped":
            rows = [row for row in rows if row.is_mapped]
        elif mapped == "unmapped":
            rows = [row for row in rows if not row.is_mapped]
        elif mapped == "incomplete":
            rows = [row for row in rows if row.unresolved]
        return rows

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["domains"] = AccountRoleDomain.choices
        context["scopes"] = AccountRoleMappingScope.choices
        context["organizations"] = self.visible_organizations(VIEW_CHART_OF_ACCOUNTS)
        context["selected_domain"] = self.request.GET.get("domain", "")
        context["selected_scope"] = self.request.GET.get("scope", "")
        context["selected_mapped"] = self.request.GET.get("mapped", "")
        context["selected_organization"] = self.request.GET.get("organization", "")
        context["as_of"] = self.as_of()
        return context


class AccountRoleDetailView(AccountingDetailView):
    """One role: where it resolves today, every version behind that, and the gaps."""

    template_name = "accounting/role_detail.html"
    required_permission = VIEW_CHART_OF_ACCOUNTS

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        role = AccountRole.objects.filter(pk=kwargs["pk"]).first()
        if role is None:
            raise Http404(_("Account role does not exist."))

        organizations = list(self.visible_organizations(VIEW_CHART_OF_ACCOUNTS))
        as_of = timezone.localdate()

        resolutions = []
        for organization in organizations:
            try:
                # The resolver hands back the **mapping**; the account is one
                # hop further in. Rendering the mapping as though it were the
                # account gives a blank code and a link to the wrong id, and
                # both look like data rather than like a defect.
                account = resolve_default_account(
                    organization=organization, account_role=role.code, on_date=as_of
                ).account
            except ValidationError:
                # Unmapped on this date. Reported as a row rather than skipped:
                # an organization missing from the table reads as "fine".
                account = None
            resolutions.append(
                {
                    "organization": organization,
                    "account": account,
                    "history": mapping_history(organization=organization, role=role),
                }
            )

        return self.render_detail(
            request,
            {
                "role": role,
                "resolutions": resolutions,
                "as_of": as_of,
                "unresolved": [
                    row["organization"] for row in resolutions if row["account"] is None
                ],
                "page_title": role.code,
                "page_hint": _(
                    "الدور مفردة نظام: الرمز والنطاق لا يتغيّران. ما يتغيّر هو الحساب "
                    "الذي تحمله كل مؤسسة، ومن أي تاريخ."
                ),
                "may_manage": bool(
                    organizations_with_permission(self.actor, MANAGE_ACCOUNT_MAPPINGS).exists()
                ),
            },
        )


# ---------------------------------------------------------------------------
# ربط الحسابات — the effective-dated defaults
# ---------------------------------------------------------------------------


class AccountMappingListView(AccountingListView):
    """
    Every mapping in the organizations this caller manages, newest version first.

    Scoped through `organizations_with_permission`, not through
    `user.has_perm`. The list is the boundary: a mapping outside the caller's
    organizations is not in the queryset, so a guessed id on any of the row
    actions below finds nothing rather than being fetched and then refused.
    """

    template_name = "accounting/mapping_list.html"
    context_object_name = "mappings"
    required_permission = MANAGE_ACCOUNT_MAPPINGS
    page_title = _("ربط الحسابات")
    page_hint = _(
        "أي حساب يحمل كل دور، ومن أي تاريخ. الربط المستعمَل لا يُعدَّل — يُغلق نطاقه ويُنشأ إصدار جديد."
    )
    search_fields = ("account_role__code", "account__code", "account__name_ar")
    search_placeholder = _("ابحث برمز الدور أو الحساب…")
    result_label = _("ربط")
    create_url_name = "accounting:mapping_create"
    create_label = _("ربط جديد")
    manage_permission = MANAGE_ACCOUNT_MAPPINGS
    manage_scope = "organization"

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = OrganizationAccountMapping.objects.filter(
            organization__in=manageable_organizations(self.actor)
        ).select_related("organization", "account_role", "account")

        organization = self.request.GET.get("organization", "").strip()
        if organization.isdigit():
            queryset = queryset.filter(organization_id=int(organization))

        role = self.request.GET.get("role", "").strip()
        if role.isdigit():
            queryset = queryset.filter(account_role_id=int(role))

        state = self.request.GET.get("state", "").strip()
        today = timezone.localdate()
        if state == "active":
            queryset = queryset.filter(is_active=True, effective_from__lte=today).filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=today)
            )
        elif state == "closed":
            queryset = queryset.filter(effective_to__lt=today)
        elif state == "archived":
            queryset = queryset.filter(is_active=False)

        return queryset.order_by("organization__code", "account_role__code", "-version")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        organizations = manageable_organizations(self.actor)
        context["organizations"] = organizations
        context["roles"] = AccountRole.objects.filter(is_active=True).order_by("domain", "code")
        context["selected_organization"] = self.request.GET.get("organization", "")
        context["selected_role"] = self.request.GET.get("role", "")
        context["selected_state"] = self.request.GET.get("state", "")
        # Which rows may still be amended, decided per row and in bulk rather
        # than once per template render: `mapping_is_used` is a query, and a
        # page of forty rows would otherwise be forty of them.
        context["used_mapping_ids"] = {
            mapping.pk
            for mapping in context.get(self.context_object_name, [])
            if mapping_is_used(mapping)
        }
        gaps: list[Any] = []
        for organization in organizations:
            gaps.extend(mapping_continuity_gaps(organization=organization))
        context["continuity_gaps"] = gaps
        return context


class MappingPreviewView(AccountingViewMixin, View):
    """
    "On this date, this role resolves to this account" — for every role at once.

    An htmx panel rather than a page, because it answers a question somebody
    asks *while* editing a mapping: what will actually be in force. The
    resolution runs through `resolve_default_account`, the same function the
    posting services call, so the preview cannot disagree with the posting.
    """

    required_permission = MANAGE_ACCOUNT_MAPPINGS
    template_name = "accounting/_mapping_preview.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        organizations = manageable_organizations(self.actor)
        chosen = request.GET.get("organization", "").strip()
        organization = None
        if chosen.isdigit():
            organization = organizations.filter(pk=int(chosen)).first()
            if organization is None:
                raise Http404(_("Organization does not exist."))
        else:
            organization = organizations.first()

        raw_date = request.GET.get("as_of", "").strip()
        try:
            as_of = datetime.date.fromisoformat(raw_date) if raw_date else timezone.localdate()
        except ValueError:
            as_of = timezone.localdate()

        rows: list[dict[str, Any]] = []
        if organization is not None:
            for role in AccountRole.objects.filter(is_active=True).order_by("domain", "code"):
                try:
                    account = resolve_default_account(
                        organization=organization, account_role=role.code, on_date=as_of
                    ).account
                except ValidationError:
                    account = None
                rows.append({"role": role, "account": account})

        return render(
            request,
            self.template_name,
            {
                "organization": organization,
                "organizations": organizations,
                "as_of": as_of,
                "rows": rows,
                "unresolved_count": sum(1 for row in rows if row["account"] is None),
            },
        )


class AccountMappingCreateView(AccountingWriteView):
    """Map a role to an account, from a date."""

    form_class = AccountMappingForm
    required_permission = MANAGE_ACCOUNT_MAPPINGS
    success_url_name = "accounting:mapping_list"
    page_title = _("ربط دور بحساب")
    page_hint = _("الحساب يجب أن يكون حساباً تفصيلياً فعّالاً في المؤسسة نفسها.")
    success_message = _("تم الربط.")
    submit_label = _("ربط")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        # This form takes `actor` and nothing else; the inventory base passes
        # `instance` too, which it does not accept.
        return (
            self.form_class(data=data, actor=self.actor)
            if data
            else self.form_class(actor=self.actor)
        )

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, MANAGE_ACCOUNT_MAPPINGS, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        map_account_role(
            actor=self.actor,
            organization_id=form.cleaned_data["organization"].pk,
            account_role_id=form.cleaned_data["account_role"].pk,
            account_id=form.cleaned_data["account"].pk,
            effective_from=form.cleaned_data["effective_from"],
            effective_to=form.cleaned_data["effective_to"],
        )


class _ScopedMappingMixin(AccountingViewMixin):
    """Resolve a mapping **with** the caller, never fetch-then-check."""

    required_permission = MANAGE_ACCOUNT_MAPPINGS
    #: Declared for the type checker: the mixin is always combined with a view,
    #: which is where `kwargs` comes from. Same arrangement as
    #: `InventoryViewMixin.request`.
    kwargs: dict[str, Any]

    def mapping(self) -> OrganizationAccountMapping:
        row = (
            OrganizationAccountMapping.objects.filter(
                organization__in=manageable_organizations(self.actor)
            )
            .select_related("organization", "account", "account_role")
            .filter(pk=self.kwargs["pk"])
            .first()
        )
        if row is None:
            raise OutOfScope(_("Account mapping does not exist."))
        return row


class AccountMappingAmendView(_ScopedMappingMixin, View):
    """
    Correct a mapping nothing has posted through yet.

    Deliberately *not* a way to edit history: the service refuses a mapping any
    posting has snapshotted, and the path for one of those is to close it and
    create the next version.
    """

    template_name = "accounting/mapping_amend.html"

    def _render(self, request: HttpRequest, form: AmendMappingForm) -> HttpResponse:
        mapping = self.mapping()
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "mapping": mapping,
                "is_used": mapping_is_used(mapping),
                "page_title": _("تعديل الربط"),
                "page_hint": _(
                    "التعديل متاح ما دام لم يُرحَّل عبر هذا الربط شيء. بعد الترحيل "
                    "يُغلق النطاق وتُنشأ نسخة جديدة."
                ),
                "cancel_url": reverse("accounting:mapping_list"),
                "form_base_template": (
                    "settings/_form_fragment.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
            },
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(request, AmendMappingForm(mapping=self.mapping()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        mapping = self.mapping()
        form = AmendMappingForm(data=request.POST, mapping=mapping)
        if form.is_valid():
            try:
                amend_account_role_mapping(
                    actor=self.actor,
                    mapping_id=mapping.pk,
                    account_id=(
                        form.cleaned_data["account"].pk if form.cleaned_data["account"] else None
                    ),
                    effective_from=form.cleaned_data["effective_from"],
                    effective_to=form.cleaned_data["effective_to"],
                    clear_effective_to=form.cleaned_data["clear_effective_to"],
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("عُدِّل الربط."))
                return HttpResponseRedirect(reverse("accounting:mapping_list"))
        return self._render(request, form)


class AccountMappingCloseView(_ScopedMappingMixin, View):
    """End a mapping's range, with a stated reason. The correction path for a used one."""

    template_name = "accounting/mapping_close.html"

    def _render(self, request: HttpRequest, form: CloseMappingForm) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "mapping": self.mapping(),
                "page_title": _("إغلاق نطاق الربط"),
                "page_hint": _(
                    "إغلاق النطاق لا يمسّ أي قيد مُرحَّل: الإصدار يبقى مقروءاً، "
                    "والإصدار التالي يبدأ من اليوم التالي."
                ),
                "cancel_url": reverse("accounting:mapping_list"),
                "form_base_template": (
                    "settings/_form_fragment.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
            },
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(request, CloseMappingForm())

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        mapping = self.mapping()
        form = CloseMappingForm(data=request.POST)
        if form.is_valid():
            try:
                close_account_role_mapping(
                    actor=self.actor,
                    mapping_id=mapping.pk,
                    effective_to=form.cleaned_data["effective_to"],
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as error:
                messages.error(request, "؛ ".join(str(message) for message in error.messages))
            else:
                messages.success(request, _("أُغلق نطاق الربط."))
            return HttpResponseRedirect(reverse("accounting:mapping_list"))
        return self._render(request, form)


class AccountMappingArchiveView(_ScopedMappingMixin, View):
    """POST-only: withdraw an unused mapping recorded in error."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        mapping = self.mapping()
        try:
            archive_account_role_mapping(
                actor=self.actor,
                mapping_id=mapping.pk,
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("أُرشف الربط."))
        return HttpResponseRedirect(reverse("accounting:mapping_list"))


def may_manage_mappings(actor: User, organization: Organization) -> bool:
    """Shared by the templates that decide whether to offer a mapping action."""
    return has_organization_permission(actor, MANAGE_ACCOUNT_MAPPINGS, organization)


__all__ = [
    "AccountMappingAmendView",
    "AccountMappingArchiveView",
    "AccountMappingCloseView",
    "AccountMappingCreateView",
    "AccountMappingListView",
    "AccountRoleDetailView",
    "AccountRoleListView",
    "AccountingActionView",
    "AccountingDetailView",
    "AccountingListView",
    "AccountingViewMixin",
    "AccountingWriteView",
    "MappingPreviewView",
]
