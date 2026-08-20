"""
Inventory master-data screens, inside the Khan Mandi shell.

Reuses the foundation list/form scaffolding rather than inventing a second
one, so the inventory screens look and behave exactly like the settings
screens an operator already knows.

Three differences from the foundation screens, all deliberate:

* Access is by **inventory permission**, not by staff flag. A storekeeper is
  not staff and must still see the item master.
* Every queryset is scoped through `apps/inventory/selectors.py`, so an
  organization's items are invisible to anyone outside it — never filtered in
  the template.
* No view calls `form.save()`. Every mutation goes through
  `apps/inventory/services.py`, which re-reads the authoritative row, checks
  the invariants, and records the audit event. A bound `ModelForm` mutates its
  instance during validation, so a "before" snapshot taken from it would
  already hold the new values.

Hiding a button is presentation, never protection. Each write view checks the
same authorization the service does, so a hand-made POST to a hidden action is
refused on its merits and not on whether the operator saw a link.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Q, QuerySet, Sum
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import ListView

if TYPE_CHECKING:
    # django-stubs types ListView generically; the runtime class is not
    # subscriptable. Same arrangement as `apps/accounting/admin.py`.
    _ListView = ListView[Any]
else:
    _ListView = ListView

from apps.accounting.permissions import MANAGE_ACCOUNT_MAPPINGS
from apps.accounting.permissions import VIEW_JOURNAL as ACCOUNTING_VIEW_JOURNAL
from apps.core.views import ModuleViewMixin
from apps.inventory.adjustments import AdjustmentLineInput
from apps.inventory.commands import (
    DOCUMENT_PERMISSION,
    add_adjustment_line,
    add_document_line,
    add_opening_line,
    add_transfer_line,
    add_unexpected_count_line,
    approve_stock_count,
    archive_inventory_role_mapping,
    blind_count_sheet,
    cancel_stock_count,
    close_inventory_role_mapping,
    create_adjustment,
    create_document,
    create_opening,
    create_reason_code,
    create_stock_count,
    create_transfer,
    create_transfer_receipt,
    create_transfer_shortage,
    delete_adjustment,
    delete_document,
    delete_opening,
    delete_stock_count,
    delete_transfer,
    delete_transfer_receipt,
    dispatch_transfer,
    map_inventory_role,
    may_see_cost,
    post_adjustment,
    post_document,
    post_opening,
    post_transfer_receipt,
    post_transfer_shortage,
    record_stock_counts,
    remove_document_line,
    remove_opening_line,
    remove_transfer_line,
    replace_transfer_receipt_lines,
    resolve_adjustment,
    resolve_count,
    resolve_count_line,
    resolve_document,
    resolve_movement,
    resolve_opening_document,
    resolve_reason_code,
    resolve_receipt,
    resolve_shortage,
    resolve_transfer,
    return_opening_to_draft,
    reverse_adjustment,
    reverse_dispatch,
    reverse_document,
    reverse_opening,
    reverse_stock_count,
    reverse_transfer_receipt,
    reverse_transfer_shortage,
    start_stock_count,
    submit_opening,
    submit_stock_count,
    update_opening,
    update_reason_code,
    visible_adjustments,
    visible_counts,
    visible_documents,
    visible_in_transit,
    visible_movements,
    visible_opening_documents,
    visible_reason_codes,
    visible_stock,
    visible_transfers,
)
from apps.inventory.counts import ApprovedCost, CountEntry
from apps.inventory.forms import (
    AdjustmentForm,
    AdjustmentLineForm,
    InventoryItemForm,
    InventoryMappingForm,
    ItemCategoryForm,
    ItemConversionForm,
    OpeningDocumentForm,
    OpeningLineForm,
    OperationalDocumentForm,
    OperationalLineForm,
    PackageUnitForm,
    ReasonCodeForm,
    StockCountForm,
    SupersedeConversionForm,
    TransferForm,
    TransferLineForm,
    TransferReceiptForm,
    TransferReceiptLineForm,
    TransferShortageForm,
    UnexpectedCountLineForm,
    WarehouseForm,
)
from apps.inventory.models import (
    ACTIVE_COUNT_STATUSES,
    OPEN_TRANSFER_STATUSES,
    InventoryDocumentStatus,
    InventoryDocumentType,
    ItemType,
    MovementType,
    StockCountStatus,
    StockTransferStatus,
)
from apps.inventory.opening import OpeningLineInput, ensure_opening_lot
from apps.inventory.operations import DocumentLineInput
from apps.inventory.permissions import (
    APPROVE_STOCK_COUNT,
    CLOSE_TRANSFER_SHORTAGE,
    CONDUCT_STOCK_COUNT,
    CREATE_DRAFT_MOVEMENT,
    CREATE_OPENING_STOCK,
    MANAGE_CATEGORIES,
    MANAGE_CONVERSIONS,
    MANAGE_ITEMS,
    MANAGE_PACKAGE_UNITS,
    MANAGE_REASON_CODES,
    MANAGE_WAREHOUSES,
    POST_ADJUSTMENT,
    POST_OPENING_STOCK,
    POST_TRANSFER,
    REVERSE_MOVEMENT,
    VIEW_ITEM,
    VIEW_STOCK,
    VIEW_VALUATION,
)
from apps.inventory.selectors import (
    readable_warehouses,
    resolve_category,
    resolve_conversion,
    resolve_item,
    resolve_manageable_warehouse,
    resolve_package_unit,
    visible_categories,
    visible_conversions,
    visible_items,
    visible_package_units,
)
from apps.inventory.services import (
    create_item,
    create_item_category,
    create_item_conversion,
    create_package_unit,
    create_warehouse,
    supersede_item_conversion,
    update_item,
    update_item_category,
    update_item_conversion,
    update_package_unit,
    update_warehouse,
)
from apps.inventory.transfers import ReceiptLineInput, TransferLineInput
from apps.organizations.authorization import (
    branches_with_permission,
    has_branch_permission,
    has_organization_permission,
    has_warehouse_permission,
    organizations_with_permission,
    require_branch_permission,
    require_organization_permission,
    require_reachable_organization_permission,
)
from apps.users.models import User


class InventoryViewMixin(LoginRequiredMixin, UserPassesTestMixin, ModuleViewMixin):
    """
    Signed in, and holding the permission this screen needs.

    `required_permission` is checked globally here; the *scope* is enforced by
    the selector that builds the queryset, which is the only place that can
    know which rows the caller reaches.
    """

    module_key = "inventory"
    required_permission: str = VIEW_ITEM
    #: Declared for the type checker: the mixin is always combined with a view.
    request: HttpRequest

    def test_func(self) -> bool:
        user = self.request.user
        return bool(user.is_authenticated and user.has_perm(self.required_permission))

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        """
        Out of scope answers **404**, not 403.

        `OutOfScope` is an `ObjectDoesNotExist`, and a 403 about another
        organization's record would confirm that the record is real — which
        turns an id-guessing loop into a census of their item master. Absent
        and foreign must be indistinguishable.
        """
        try:
            return super().dispatch(request, *args, **kwargs)
        except ObjectDoesNotExist as missing:
            raise Http404(str(missing)) from missing

    @property
    def actor(self) -> User:
        """The signed-in caller. `test_func` has already refused anonymity."""
        user: User = self.request.user  # type: ignore[assignment]
        return user

    def is_htmx(self) -> bool:
        """
        Whether htmx made this request, rather than the browser navigating.

        On the mixin rather than on the list view, because the **write** views
        need the same answer: a form rendered into an HTMX target must not carry
        the shell. Defined once so a list and a form cannot disagree about what
        an HTMX request is.
        """
        return self.request.headers.get("HX-Request") == "true"


class InventoryListView(InventoryViewMixin, _ListView):
    """
    A searchable, paged list of one master-data kind, with its actions.

    The action buttons are decided **per row**, from the organization or
    branch that row belongs to, because a caller can legitimately manage the
    item master of one organization while only reading another's. A single
    page-level "can manage" flag would be wrong in exactly that case, and
    wrong in the permissive direction.
    """

    paginate_by = 25
    page_title: Any = ""
    page_hint: Any = ""
    search_placeholder: Any = _("ابحث بالرمز أو الاسم…")
    result_label: Any = _("سجل")
    search_fields: tuple[str, ...] = ()
    create_url_name: str | None = None
    create_label: Any = ""
    #: The permission that gates create, edit, archive, and reactivate here.
    manage_permission: str = ""
    #: Whether that permission is answered per organization or per branch.
    manage_scope: str = "organization"

    def get_queryset(self) -> QuerySet[Any]:
        queryset = self.scoped_queryset()
        search = self.request.GET.get("q", "").strip()
        if search and self.search_fields:
            matches = Q()
            for field in self.search_fields:
                matches |= Q(**{f"{field}__icontains": search})
            queryset = queryset.filter(matches)
        return queryset

    def scoped_queryset(self) -> QuerySet[Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def manageable_ids(self) -> list[int]:
        """Organizations, or branches, where this caller may write."""
        if not self.manage_permission:
            return []
        if self.manage_scope in {"branch", "warehouse"}:
            return list(
                branches_with_permission(self.actor, self.manage_permission).values_list(
                    "id", flat=True
                )
            )
        return list(
            organizations_with_permission(self.actor, self.manage_permission).values_list(
                "id", flat=True
            )
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        manageable = self.manageable_ids()
        context["page_title"] = self.page_title
        context["page_hint"] = self.page_hint
        context["search"] = self.request.GET.get("q", "")
        context["search_placeholder"] = self.search_placeholder
        context["result_label"] = self.result_label
        context["create_label"] = self.create_label
        context["manageable_ids"] = manageable
        # `filter_query` comes from `apps.core.context_processors.shell`: it is
        # derived from the request alone, and every list template needs it.
        # Filtering and paging swap the results table alone. The flag drives
        # the hx-* attributes, and it is set only where this view answers an
        # HX-Request with the partial — see `_list_fragment.html`.
        context["htmx_list"] = True
        context["inventory_ui"] = True
        context["list_base_template"] = (
            "settings/_list_fragment.html" if self.is_htmx() else "shell.html"
        )
        # No place to create it means no button. The create view refuses the
        # same request anyway; this only avoids offering a dead end.
        context["create_url"] = (
            reverse(self.create_url_name) if self.create_url_name and manageable else None
        )
        return context


class ItemCategoryListView(InventoryListView):
    template_name = "inventory/category_list.html"
    context_object_name = "categories"
    page_title = _("مجموعات الأصناف")
    page_hint = _(
        "ثلاثة مستويات كحد أقصى. الأصناف تُربط بالمستوى الأخير فقط، حتى تبقى تقارير المجموعات صحيحة."
    )
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_CATEGORIES
    create_url_name = "inventory:category_create"
    create_label = _("مجموعة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_categories(self.actor).order_by("depth", "code")


class PackageUnitListView(InventoryListView):
    template_name = "inventory/package_unit_list.html"
    context_object_name = "package_units"
    page_title = _("وحدات التعبئة")
    page_hint = _(
        "الكرتونة والكيس والعلبة. لا تحمل معامل تحويل عام — كرتونة الدجاج "
        "ليست كرتونة الزيت، والمعامل يُسجَّل لكل صنف على حدة."
    )
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_PACKAGE_UNITS
    create_url_name = "inventory:package_unit_create"
    create_label = _("وحدة تعبئة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_package_units(self.actor).order_by("code")


class ItemListView(InventoryListView):
    template_name = "inventory/item_list.html"
    context_object_name = "items"
    page_title = _("الأصناف")
    page_hint = _("سجل الأصناف على مستوى المؤسسة، مشترك بين الفروع.")
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_ITEMS
    create_url_name = "inventory:item_create"
    create_label = _("صنف جديد")
    search_placeholder = _("ابحث عن صنف بالرمز أو الاسم العربي أو الإنجليزي…")
    result_label = _("صنف")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_items(self.actor)
        item_type = self.request.GET.get("item_type", "").strip()
        if item_type in ItemType.values:
            queryset = queryset.filter(item_type=item_type)
        return queryset.order_by("code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["item_types"] = ItemType.choices
        context["selected_item_type"] = self.request.GET.get("item_type", "")
        return context


class ItemConversionListView(InventoryListView):
    template_name = "inventory/conversion_list.html"
    context_object_name = "conversions"
    page_title = _("تحويلات وحدات الصنف")
    page_hint = _(
        "كم وحدة أساس في العبوة الواحدة، لكل صنف. التحويل يصل مباشرة إلى وحدة "
        "أساس الصنف — بلا سلاسل."
    )
    search_fields = ("item__code", "item__name_ar", "package_unit__code")
    manage_permission = MANAGE_CONVERSIONS
    create_url_name = "inventory:conversion_create"
    create_label = _("تحويل جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_conversions(self.actor).order_by(
            "item__code", "package_unit__code", "-effective_from"
        )


class WarehouseListView(InventoryListView):
    template_name = "inventory/warehouse_list.html"
    context_object_name = "warehouses"
    page_title = _("المخازن")
    page_hint = _("المخزن يتبع فرعاً واحداً، وهو الذي يملك قيمة المخزون.")
    required_permission = MANAGE_WAREHOUSES
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_WAREHOUSES
    manage_scope = "branch"
    create_url_name = "inventory:warehouse_create"
    create_label = _("مخزن جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        # Archived warehouses stay readable — their codes are still reserved
        # and their history is still referenced. `visible_warehouses` answers
        # custody, which is only ever about active ones.
        return readable_warehouses(self.actor).order_by("branch__code", "code")


# ---------------------------------------------------------------------------
# Write screens
# ---------------------------------------------------------------------------


class InventoryWriteView(InventoryViewMixin, View):
    """
    Base for the create and edit screens.

    `test_func` is the coarse gate — it answers "may this person manage this
    kind of thing at all". It is never the authorization: the scoped check
    against the specific organization or branch happens in `authorize`, after
    the target is known, and it is what the service would enforce anyway.

    Nothing here calls `form.save()`.
    """

    template_name = "inventory/master_form.html"
    form_class: Any = None
    page_title: Any = ""
    page_hint: Any = ""
    success_message: Any = _("تم الحفظ.")
    success_url_name: str = ""
    required_permission: str = ""

    def get_success_url(self) -> str:
        return reverse(self.success_url_name)

    # -- overridden per screen ---------------------------------------------

    def load(self) -> Any:
        """The row being edited, or None when creating."""
        return None

    def initial_for(self, instance: Any) -> dict[str, Any]:  # pragma: no cover - overridden
        return {}

    def authorize(self, instance: Any, form: Any) -> None:  # pragma: no cover - overridden
        """Raise unless the caller may write here. Runs before the service."""
        raise NotImplementedError

    def perform(self, instance: Any, form: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- request handling ---------------------------------------------------

    def build_form(self, instance: Any, data: Any = None) -> Any:
        kwargs: dict[str, Any] = {"actor": self.actor, "instance": instance}
        if instance is not None:
            kwargs["initial"] = self.initial_for(instance)
        if data is not None:
            kwargs["data"] = data
        return self.form_class(**kwargs)

    def context(self, instance: Any, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": self.page_title,
            "page_hint": self.page_hint,
            "cancel_url": self.get_success_url(),
            "instance": instance,
            # The write-screen half of the contract `list_base_template`
            # already gives every list. Without it an HTMX GET rendered the
            # whole shell into the target: two <html> elements, two navigation
            # rails, and a page that looks correct until somebody swaps it into
            # a panel. Defaults to the shell, so no existing caller changes.
            "form_base_template": (
                "settings/_form_fragment.html" if self.is_htmx() else "shell.html"
            ),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        return render(
            request, self.template_name, self.context(instance, self.build_form(instance))
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        form = self.build_form(instance, data=request.POST)
        if form.is_valid():
            try:
                self.authorize(instance, form)
                self.perform(instance, form)
            except ValidationError as error:
                # A domain rule the form could not know about — a cycle, a
                # period overlap, a protected system row. Rendered as a form
                # error in Arabic rather than as a 500.
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, self.success_message)
                if self.is_htmx():
                    response = HttpResponse(status=200)
                    response["HX-Redirect"] = self.get_success_url()
                    return response
                return HttpResponseRedirect(self.get_success_url())
        return render(request, self.template_name, self.context(instance, form))


class InventoryActionView(InventoryViewMixin, View):
    """
    A POST-only archive or reactivate action.

    POST-only because it changes state: a GET that archived a warehouse would
    be triggered by a link prefetch. There is no delete anywhere in this
    module — a code that has been used stays reserved, and a row that has been
    referenced stays readable.
    """

    #: True archives, False brings the row back.
    activate: bool = False
    success_url_name: str = ""
    required_permission: str = ""

    def get_success_url(self) -> str:
        return reverse(self.success_url_name)

    def load(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def authorize(self, instance: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def perform(self, instance: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        self.authorize(instance)
        try:
            self.perform(instance)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(
                request, _("تمت الإعادة إلى الخدمة.") if self.activate else _("تمت الأرشفة.")
            )
        return HttpResponseRedirect(self.get_success_url())


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class ItemCategoryCreateView(InventoryWriteView):
    form_class = ItemCategoryForm
    required_permission = MANAGE_CATEGORIES
    success_url_name = "inventory:category_list"
    page_title = _("مجموعة أصناف جديدة")
    page_hint = _("ثلاثة مستويات كحد أقصى. الأصناف تُربط بالمستوى الأخير فقط.")
    success_message = _("تمت إضافة المجموعة.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CATEGORIES, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_item_category(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            parent=form.cleaned_data["parent"],
        )


class ItemCategoryUpdateView(InventoryWriteView):
    form_class = ItemCategoryForm
    required_permission = MANAGE_CATEGORIES
    success_url_name = "inventory:category_list"
    page_title = _("تعديل المجموعة")
    success_message = _("تم حفظ المجموعة.")

    def load(self) -> Any:
        return resolve_category(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "organization": instance.organization,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "parent": instance.parent,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CATEGORIES, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_item_category(
            category=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            parent=form.cleaned_data["parent"],
            is_active=form.cleaned_data["is_active"],
        )


class ItemCategoryActionView(InventoryActionView):
    required_permission = MANAGE_CATEGORIES
    success_url_name = "inventory:category_list"

    def load(self) -> Any:
        return resolve_category(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CATEGORIES, instance.organization
        )

    def perform(self, instance: Any) -> None:
        update_item_category(
            category=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            parent=instance.parent,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Package units
# ---------------------------------------------------------------------------


class PackageUnitCreateView(InventoryWriteView):
    form_class = PackageUnitForm
    required_permission = MANAGE_PACKAGE_UNITS
    success_url_name = "inventory:package_unit_list"
    page_title = _("وحدة تعبئة جديدة")
    page_hint = _(
        "لا معامل هنا. كرتونة الدجاج ليست كرتونة الزيت — المعامل يُسجَّل لكل صنف "
        "في شاشة تحويلات وحدات الصنف."
    )
    success_message = _("تمت إضافة وحدة التعبئة.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_PACKAGE_UNITS, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_package_unit(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
        )


class PackageUnitQuickCreateView(InventoryViewMixin, View):
    """Small HTMX form for adding a package name without leaving its list."""

    required_permission = MANAGE_PACKAGE_UNITS
    template_name = "inventory/_package_unit_quick_form.html"

    def form(self, data: Any = None) -> PackageUnitForm:
        return PackageUnitForm(actor=self.actor, data=data)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.headers.get("HX-Request") != "true":
            return HttpResponseRedirect(reverse("inventory:package_unit_create"))
        return render(request, self.template_name, {"form": self.form()})

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = self.form(request.POST)
        if not form.is_valid():
            # htmx swaps successful responses by default. Validation is a
            # renderable form state, not a transport failure (ADR-011).
            return render(request, self.template_name, {"form": form}, status=200)
        require_reachable_organization_permission(
            self.actor, MANAGE_PACKAGE_UNITS, form.cleaned_data["organization"]
        )
        create_package_unit(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
        )
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=204)
            response["HX-Trigger"] = "packageUnitCreated"
            return response
        messages.success(request, _("تمت إضافة وحدة التعبئة."))
        return HttpResponseRedirect(reverse("inventory:package_unit_list"))


class PackageUnitUpdateView(InventoryWriteView):
    form_class = PackageUnitForm
    required_permission = MANAGE_PACKAGE_UNITS
    success_url_name = "inventory:package_unit_list"
    page_title = _("تعديل وحدة التعبئة")
    success_message = _("تم حفظ وحدة التعبئة.")

    def load(self) -> Any:
        return resolve_package_unit(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "organization": instance.organization,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_PACKAGE_UNITS, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_package_unit(
            package_unit=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            is_active=form.cleaned_data["is_active"],
        )


class PackageUnitActionView(InventoryActionView):
    required_permission = MANAGE_PACKAGE_UNITS
    success_url_name = "inventory:package_unit_list"

    def load(self) -> Any:
        return resolve_package_unit(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_PACKAGE_UNITS, instance.organization
        )

    def perform(self, instance: Any) -> None:
        update_package_unit(
            package_unit=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


class ItemCreateView(InventoryWriteView):
    template_name = "inventory/item_form.html"
    form_class = InventoryItemForm
    required_permission = MANAGE_ITEMS
    success_url_name = "inventory:item_list"
    page_title = _("صنف جديد")
    page_hint = _("وحدة الأساس تُثبَّت عند الإنشاء — كل كمية مخزنية تُقاس بها.")
    success_message = _("تمت إضافة الصنف.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_ITEMS, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_item(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            category=form.cleaned_data["category"],
            item_type=form.cleaned_data["item_type"],
            base_unit=form.cleaned_data["base_unit"],
            tracks_lots=form.cleaned_data["tracks_lots"],
            tracks_expiry=form.cleaned_data["tracks_expiry"],
            shelf_life_days=form.cleaned_data["shelf_life_days"],
            notes=form.cleaned_data["notes"],
        )


class ItemUpdateView(InventoryWriteView):
    template_name = "inventory/item_form.html"
    form_class = InventoryItemForm
    required_permission = MANAGE_ITEMS
    success_url_name = "inventory:item_list"
    page_title = _("تعديل الصنف")
    success_message = _("تم حفظ الصنف.")

    def load(self) -> Any:
        return resolve_item(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "organization": instance.organization,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "category": instance.category,
            "item_type": instance.item_type,
            "base_unit": instance.base_unit,
            "tracks_lots": instance.tracks_lots,
            "tracks_expiry": instance.tracks_expiry,
            "shelf_life_days": instance.shelf_life_days,
            "notes": instance.notes,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_ITEMS, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        update_item(
            item=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            category=form.cleaned_data["category"],
            item_type=form.cleaned_data["item_type"],
            tracks_lots=form.cleaned_data["tracks_lots"],
            tracks_expiry=form.cleaned_data["tracks_expiry"],
            shelf_life_days=form.cleaned_data["shelf_life_days"],
            notes=form.cleaned_data["notes"],
            is_active=form.cleaned_data["is_active"],
        )


class ItemActionView(InventoryActionView):
    required_permission = MANAGE_ITEMS
    success_url_name = "inventory:item_list"

    def load(self) -> Any:
        return resolve_item(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_ITEMS, instance.organization)

    def perform(self, instance: Any) -> None:
        update_item(
            item=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            category=instance.category,
            item_type=instance.item_type,
            shelf_life_days=instance.shelf_life_days,
            notes=instance.notes,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Item package conversions
# ---------------------------------------------------------------------------


class ItemConversionCreateView(InventoryWriteView):
    form_class = ItemConversionForm
    required_permission = MANAGE_CONVERSIONS
    success_url_name = "inventory:conversion_list"
    page_title = _("تحويل وحدة صنف جديد")
    page_hint = _("المعامل يصل مباشرة إلى وحدة أساس الصنف — بلا سلاسل تحويل.")
    success_message = _("تمت إضافة التحويل.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CONVERSIONS, form.cleaned_data["item"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_item_conversion(
            item=form.cleaned_data["item"],
            package_unit=form.cleaned_data["package_unit"],
            factor_to_base=form.cleaned_data["factor_to_base"],
            effective_from=form.cleaned_data["effective_from"],
            conversion_type=form.cleaned_data["conversion_type"],
            effective_to=form.cleaned_data["effective_to"],
            allows_fractional=form.cleaned_data["allows_fractional"],
            minimum_increment=form.cleaned_data["minimum_increment"],
            is_default_purchase_package=form.cleaned_data["is_default_purchase_package"],
        )


class ItemConversionUpdateView(InventoryWriteView):
    form_class = ItemConversionForm
    required_permission = MANAGE_CONVERSIONS
    success_url_name = "inventory:conversion_list"
    page_title = _("تصحيح التحويل")
    page_hint = _(
        "التصحيح لمعامل لم يُقيَّم عليه شيء بعد. إن تغيّر حجم العبوة فعلياً "
        "فاستخدم إصداراً جديداً بدل التصحيح."
    )
    success_message = _("تم حفظ التحويل.")

    def load(self) -> Any:
        return resolve_conversion(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "item": instance.item,
            "package_unit": instance.package_unit,
            "conversion_type": instance.conversion_type,
            # The stored precision, with a period. Re-entering what is shown
            # must reproduce the same factor exactly.
            "factor_to_base": instance.factor_display,
            "effective_from": instance.effective_from,
            "effective_to": instance.effective_to,
            "allows_fractional": instance.allows_fractional,
            "minimum_increment": (
                f"{instance.minimum_increment:f}" if instance.minimum_increment is not None else ""
            ),
            "is_default_purchase_package": instance.is_default_purchase_package,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CONVERSIONS, instance.item.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_item_conversion(
            conversion=instance,
            factor_to_base=form.cleaned_data["factor_to_base"],
            effective_from=form.cleaned_data["effective_from"],
            effective_to=form.cleaned_data["effective_to"],
            conversion_type=form.cleaned_data["conversion_type"],
            allows_fractional=form.cleaned_data["allows_fractional"],
            minimum_increment=form.cleaned_data["minimum_increment"],
            is_default_purchase_package=form.cleaned_data["is_default_purchase_package"],
            is_active=form.cleaned_data["is_active"],
        )


class ItemConversionSupersedeView(InventoryWriteView):
    """Close this version and open its successor — a new packaging fact."""

    form_class = SupersedeConversionForm
    required_permission = MANAGE_CONVERSIONS
    success_url_name = "inventory:conversion_list"
    page_title = _("إصدار جديد من التحويل")
    page_hint = _(
        "صار الكيس ٢٥ كغم بدل ٣٠؟ هذه حقيقة تعبئة جديدة، لا تصحيح. الإصدار "
        "الحالي يُغلق في اليوم السابق وتبقى الحركات المرحّلة كما قُيّمت."
    )
    success_message = _("تم إصدار المعامل الجديد.")

    def load(self) -> Any:
        return resolve_conversion(self.actor, self.kwargs["pk"])

    def build_form(self, instance: Any, data: Any = None) -> Any:
        # No `instance` kwarg: this form describes the successor, not the row
        # it replaces, so there is nothing of the old one to prefill.
        kwargs: dict[str, Any] = {"actor": self.actor}
        if data is not None:
            kwargs["data"] = data
        return self.form_class(**kwargs)

    def context(self, instance: Any, form: Any) -> dict[str, Any]:
        context = super().context(instance, form)
        context["page_title"] = _("إصدار جديد") + f" — {instance}"
        return context

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CONVERSIONS, instance.item.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        supersede_item_conversion(
            conversion=instance,
            factor_to_base=form.cleaned_data["factor_to_base"],
            effective_from=form.cleaned_data["effective_from"],
            reason=form.cleaned_data["reason"],
        )


class ItemConversionActionView(InventoryActionView):
    required_permission = MANAGE_CONVERSIONS
    success_url_name = "inventory:conversion_list"

    def load(self) -> Any:
        return resolve_conversion(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CONVERSIONS, instance.item.organization
        )

    def perform(self, instance: Any) -> None:
        update_item_conversion(
            conversion=instance,
            factor_to_base=instance.factor_to_base,
            effective_from=instance.effective_from,
            effective_to=instance.effective_to,
            conversion_type=instance.conversion_type,
            allows_fractional=instance.allows_fractional,
            minimum_increment=instance.minimum_increment,
            is_default_purchase_package=instance.is_default_purchase_package,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------


class WarehouseCreateView(InventoryWriteView):
    form_class = WarehouseForm
    required_permission = MANAGE_WAREHOUSES
    success_url_name = "inventory:warehouse_list"
    page_title = _("مخزن جديد")
    page_hint = _("المخزن يتبع فرعاً واحداً وهو الذي يملك قيمة المخزون.")
    success_message = _("تمت إضافة المخزن.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(self.actor, MANAGE_WAREHOUSES, form.cleaned_data["branch"])

    def perform(self, instance: Any, form: Any) -> None:
        create_warehouse(
            branch=form.cleaned_data["branch"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            warehouse_type=form.cleaned_data["warehouse_type"],
        )


class WarehouseUpdateView(InventoryWriteView):
    form_class = WarehouseForm
    required_permission = MANAGE_WAREHOUSES
    success_url_name = "inventory:warehouse_list"
    page_title = _("تعديل المخزن")
    success_message = _("تم حفظ المخزن.")

    def load(self) -> Any:
        return resolve_manageable_warehouse(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "branch": instance.branch,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "warehouse_type": instance.warehouse_type,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(self.actor, MANAGE_WAREHOUSES, instance.branch)

    def perform(self, instance: Any, form: Any) -> None:
        update_warehouse(
            warehouse=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            is_active=form.cleaned_data["is_active"],
        )


class WarehouseActionView(InventoryActionView):
    required_permission = MANAGE_WAREHOUSES
    success_url_name = "inventory:warehouse_list"

    def load(self) -> Any:
        return resolve_manageable_warehouse(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_branch_permission(self.actor, MANAGE_WAREHOUSES, instance.branch)

    def perform(self, instance: Any) -> None:
        update_warehouse(
            warehouse=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Stock and movements — read only
# ---------------------------------------------------------------------------
#
# There is no form on either screen. Stock moves through a posting service and
# nowhere else, so a page that offered to edit a balance would be offering
# something that cannot be done.
#
# Both may show an empty table until Tasks 1.3 and 1.4 create real business
# postings. That is the honest state, and it is why the production seed
# deliberately contains no demonstration movements: a stock figure nobody
# posted is worse than no figure at all.


class StockOnHandView(InventoryListView):
    """What is held, where, and — for those who may see it — what it is worth."""

    template_name = "inventory/stock_list.html"
    context_object_name = "balances"
    page_title = _("المخزون المتوفر")
    page_hint = _("الرصيد لكل مخزن وصنف ودفعة. يُحتسب من حركات المخزون ولا يُحرَّر يدوياً.")
    required_permission = VIEW_STOCK
    search_fields = ("item__code", "item__name_ar", "warehouse__code")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_stock(self.actor).order_by("warehouse__code", "item__code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # A storekeeper sees quantities and not money. The columns are absent
        # rather than blank: a blank cell still says a number belongs there.
        context["show_cost"] = may_see_cost(self.actor)
        return context


class MovementHistoryView(InventoryListView):
    """The ledger, newest first, in the order valuation was computed."""

    template_name = "inventory/movement_list.html"
    context_object_name = "movements"
    page_title = _("حركة المخزون")
    page_hint = _(
        "سجل غير قابل للتعديل. التصحيح يكون بعكس الحركة لا بتحريرها، "
        "والترتيب هو ترتيب الترحيل الذي احتُسب عليه التقييم."
    )
    required_permission = VIEW_STOCK
    search_fields = ("item__code", "item__name_ar", "warehouse__code", "effect_key")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_movements(self.actor)
        movement_type = self.request.GET.get("movement_type", "").strip()
        if movement_type in MovementType.values:
            queryset = queryset.filter(movement_type=movement_type)
        return queryset.order_by("-posted_sequence")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["show_cost"] = may_see_cost(self.actor)
        context["movement_types"] = MovementType.choices
        context["selected_movement_type"] = self.request.GET.get("movement_type", "")
        return context


class MovementDetailView(InventoryViewMixin, View):
    """
    One movement, with the arithmetic it was posted with and the document
    behind it.

    Shows `before` and `after` on both quantity and value, because that is
    what makes a disputed figure answerable: the movement says what it found,
    what it did, and what it left.
    """

    template_name = "inventory/movement_detail.html"
    required_permission = VIEW_STOCK

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        movement = resolve_movement(self.actor, self.kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "movement": movement,
                "entry": movement.entry,
                "show_cost": may_see_cost(self.actor),
                "page_title": _("تفاصيل الحركة"),
                "back_url": reverse("inventory:movement_list"),
            },
        )


# ---------------------------------------------------------------------------
# Task 1.3 — inventory account-mapping overrides
# ---------------------------------------------------------------------------


class InventoryMappingListView(InventoryListView):
    """Item/category overrides. The organization defaults live in accounting."""

    template_name = "inventory/inventory_mapping_list.html"
    context_object_name = "mappings"
    page_title = _("ربط حسابات المخزون")
    page_hint = _(
        "تخصيص حساب المراقبة لصنف أو مجموعة. الافتراضي على مستوى المؤسسة يُدار في شاشة "
        "المحاسبة، والربط المستعمَل يُغلق نطاقه ولا يُعدَّل."
    )
    required_permission = MANAGE_ACCOUNT_MAPPINGS
    search_fields = ("item__code", "category__code", "account__code")
    create_url_name = "inventory:mapping_create"
    create_label = _("تخصيص جديد")
    manage_permission = MANAGE_ACCOUNT_MAPPINGS

    def scoped_queryset(self) -> QuerySet[Any]:
        from apps.inventory.models import InventoryAccountMapping

        return (
            InventoryAccountMapping.objects.filter(
                organization__in=organizations_with_permission(self.actor, MANAGE_ACCOUNT_MAPPINGS)
            )
            .select_related("organization", "account_role", "account", "item", "category")
            .order_by("organization__code", "account_role__code", "-version")
        )


class InventoryMappingCreateView(InventoryWriteView):
    form_class = InventoryMappingForm
    required_permission = MANAGE_ACCOUNT_MAPPINGS
    success_url_name = "inventory:mapping_list"
    page_title = _("تخصيص حساب لصنف أو مجموعة")
    page_hint = _("يتقدم التخصيص على افتراضي المؤسسة: الصنف أولاً، ثم أقرب مجموعة أعلى.")
    success_message = _("تم التخصيص.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, MANAGE_ACCOUNT_MAPPINGS, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        map_inventory_role(
            actor=self.actor,
            organization=form.cleaned_data["organization"],
            role=form.cleaned_data["account_role"],
            account=form.cleaned_data["account"],
            item=form.cleaned_data["item"],
            category=form.cleaned_data["category"],
            effective_from=form.cleaned_data["effective_from"],
            effective_to=form.cleaned_data["effective_to"],
        )


class InventoryMappingCloseView(InventoryViewMixin, View):
    """End an override's effective range, with a stated reason."""

    template_name = "inventory/mapping_close.html"
    required_permission = MANAGE_ACCOUNT_MAPPINGS

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.accounting.forms import CloseMappingForm

        return render(
            request,
            self.template_name,
            {
                "form": CloseMappingForm(),
                "page_title": _("إغلاق نطاق التخصيص"),
                "cancel_url": reverse("inventory:mapping_list"),
            },
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.accounting.forms import CloseMappingForm

        form = CloseMappingForm(data=request.POST)
        if form.is_valid():
            try:
                close_inventory_role_mapping(
                    actor=self.actor,
                    mapping_id=self.kwargs["pk"],
                    effective_to=form.cleaned_data["effective_to"],
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as error:
                messages.error(request, "؛ ".join(str(message) for message in error.messages))
            else:
                messages.success(request, _("أُغلق نطاق التخصيص."))
            return HttpResponseRedirect(reverse("inventory:mapping_list"))
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "page_title": _("إغلاق نطاق التخصيص"),
                "cancel_url": reverse("inventory:mapping_list"),
            },
        )


class InventoryMappingArchiveView(InventoryViewMixin, View):
    required_permission = MANAGE_ACCOUNT_MAPPINGS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            archive_inventory_role_mapping(
                actor=self.actor,
                mapping_id=self.kwargs["pk"],
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("أُرشف التخصيص."))
        return HttpResponseRedirect(reverse("inventory:mapping_list"))


# ---------------------------------------------------------------------------
# Task 1.3 — opening stock documents
# ---------------------------------------------------------------------------


class OpeningListView(InventoryListView):
    template_name = "inventory/opening_list.html"
    context_object_name = "documents"
    page_title = _("الأرصدة الافتتاحية")
    page_hint = _("مستند لكل فرع بلحظة جرد واحدة. من قدّم المستند لا يرحّله — مبدأ المُعِدّ والمُعتمِد.")
    required_permission = VIEW_STOCK
    search_fields = ("document_number", "evidence_reference", "branch__code")
    manage_permission = CREATE_OPENING_STOCK
    manage_scope = "branch"
    create_url_name = "inventory:opening_create"
    create_label = _("رصيد افتتاحي جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_opening_documents(self.actor)


class OpeningCreateView(InventoryViewMixin, View):
    """The header first; the lines are added on the document's own page."""

    template_name = "inventory/master_form.html"
    required_permission = CREATE_OPENING_STOCK

    def _context(self, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("رصيد افتتاحي جديد"),
            "page_hint": _("لحظة جرد واحدة للمستند كله، ومرجع إثبات إلزامي."),
            "cancel_url": reverse("inventory:opening_list"),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(
            request, self.template_name, self._context(OpeningDocumentForm(actor=self.actor))
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = OpeningDocumentForm(data=request.POST, actor=self.actor)
        if form.is_valid():
            branch = form.cleaned_data["branch"]
            try:
                require_branch_permission(self.actor, CREATE_OPENING_STOCK, branch)
                document = create_opening(
                    actor=self.actor,
                    organization=branch.organization,
                    branch=branch,
                    cutoff_at=form.cleaned_data["cutoff_at"],
                    evidence_reference=form.cleaned_data["evidence_reference"],
                    narration=form.cleaned_data["narration"],
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُنشئ المستند. أضف السطور ثم قدّمه."))
                return HttpResponseRedirect(reverse("inventory:opening_detail", args=[document.pk]))
        return render(request, self.template_name, self._context(form))


class OpeningDetailView(InventoryViewMixin, View):
    """
    The document, its lines, its totals, and the actions its status allows.

    Buttons are decided by the same checks the commands enforce; hiding one is
    presentation, and a hand-made POST to a hidden action is refused on its
    merits by the command layer.
    """

    template_name = "inventory/opening_detail.html"
    required_permission = VIEW_STOCK

    def _document(self) -> Any:
        return resolve_opening_document(self.actor, self.kwargs["pk"])

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = self._document()
        return render(
            request, self.template_name, self._context(document, self._line_form(document))
        )

    def _line_form(self, document: Any, data: Any = None) -> Any:
        return OpeningLineForm(data=data, actor=self.actor, branch=document.branch)

    def _context(self, document: Any, line_form: Any) -> dict[str, Any]:
        from apps.inventory.models import OpeningStockStatus

        lines = list(
            document.lines.select_related(
                "warehouse", "item", "item__base_unit", "lot", "inventory_account", "movement"
            ).order_by("sequence")
        )
        show_cost = may_see_cost(self.actor)
        can_prepare = has_branch_permission(self.actor, CREATE_OPENING_STOCK, document.branch)
        can_post = has_organization_permission(
            self.actor, POST_OPENING_STOCK, document.organization
        )
        return {
            "document": document,
            "lines": lines,
            "show_cost": show_cost,
            "total_value": (
                sum((line.total_value for line in lines), Decimal("0")) if show_cost else None
            ),
            "line_form": line_form,
            "is_draft": document.status == OpeningStockStatus.DRAFT,
            "is_submitted": document.status == OpeningStockStatus.SUBMITTED,
            "is_posted": document.status == OpeningStockStatus.POSTED,
            "can_prepare": can_prepare,
            "can_post": (
                can_post
                and document.submitted_by_id is not None
                and document.submitted_by_id != self.actor.pk
            ),
            "can_reverse": has_organization_permission(
                self.actor, REVERSE_MOVEMENT, document.organization
            ),
            "page_title": _("رصيد افتتاحي") + f" — {document}",
            "back_url": reverse("inventory:opening_list"),
        }

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Adding one line, from the embedded form."""
        document = self._document()
        form = self._line_form(document, data=request.POST)
        if form.is_valid():
            item = form.cleaned_data["item"]
            lot = None
            try:
                if form.cleaned_data["lot_code"]:
                    lot = ensure_opening_lot(
                        item=item,
                        code=form.cleaned_data["lot_code"],
                        expiry_date=form.cleaned_data["lot_expiry"],
                    )
                add_opening_line(
                    actor=self.actor,
                    document=document,
                    line=OpeningLineInput(
                        warehouse=form.cleaned_data["warehouse"],
                        item=item,
                        lot=lot,
                        package_conversion=form.cleaned_data["package_conversion"],
                        entered_package_quantity=form.cleaned_data["entered_package_quantity"],
                        measured_base_quantity=form.cleaned_data["measured_base_quantity"],
                        base_quantity=form.cleaned_data["base_quantity"],
                        unit_cost=form.cleaned_data["unit_cost"] or Decimal("0"),
                    ),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُضيف السطر."))
                return HttpResponseRedirect(reverse("inventory:opening_detail", args=[document.pk]))
        return render(request, self.template_name, self._context(document, form))


class OpeningUpdateView(InventoryViewMixin, View):
    """Edit a draft's header."""

    template_name = "inventory/master_form.html"
    required_permission = CREATE_OPENING_STOCK

    def _context(self, form: Any, document: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("تعديل الرصيد الافتتاحي"),
            "cancel_url": reverse("inventory:opening_detail", args=[document.pk]),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = resolve_opening_document(self.actor, self.kwargs["pk"])
        form = OpeningDocumentForm(
            actor=self.actor,
            initial={
                "branch": document.branch,
                "cutoff_at": document.cutoff_at,
                "evidence_reference": document.evidence_reference,
                "narration": document.narration,
            },
        )
        form.fields["branch"].disabled = True
        form.fields["branch"].queryset = type(document.branch).objects.filter(  # type: ignore[attr-defined]
            pk=document.branch_id
        )
        return render(request, self.template_name, self._context(form, document))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = resolve_opening_document(self.actor, self.kwargs["pk"])
        form = OpeningDocumentForm(data=request.POST, actor=self.actor)
        form.fields["branch"].disabled = True
        form.fields["branch"].queryset = type(document.branch).objects.filter(  # type: ignore[attr-defined]
            pk=document.branch_id
        )
        form.initial["branch"] = document.branch
        if form.is_valid():
            try:
                update_opening(
                    actor=self.actor,
                    document=document,
                    cutoff_at=form.cleaned_data["cutoff_at"],
                    evidence_reference=form.cleaned_data["evidence_reference"],
                    narration=form.cleaned_data["narration"],
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("حُفظ المستند."))
                return HttpResponseRedirect(reverse("inventory:opening_detail", args=[document.pk]))
        return render(request, self.template_name, self._context(form, document))


class OpeningActionView(InventoryViewMixin, View):
    """
    A POST-only lifecycle action on one document.

    `action` names the command; reason-bearing actions read `reason` from the
    POST body. Every command re-checks authorization — this view only routes.
    """

    required_permission = VIEW_STOCK
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = resolve_opening_document(self.actor, self.kwargs["pk"])
        detail = reverse("inventory:opening_detail", args=[document.pk])
        try:
            if self.action == "submit":
                submit_opening(actor=self.actor, document=document)
                messages.success(request, _("قُدّم المستند للاعتماد."))
            elif self.action == "return":
                return_opening_to_draft(
                    actor=self.actor, document=document, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("أُعيد المستند إلى المسودة."))
            elif self.action == "post":
                post_opening(actor=self.actor, document=document)
                messages.success(request, _("رُحّل الرصيد الافتتاحي إلى الدفترين."))
            elif self.action == "reverse":
                reverse_opening(
                    actor=self.actor, document=document, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("عُكس المستند بالكامل."))
            elif self.action == "delete":
                delete_opening(
                    actor=self.actor, document=document, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("حُذفت المسودة."))
                return HttpResponseRedirect(reverse("inventory:opening_list"))
            elif self.action == "delete_line":
                line = document.lines.filter(pk=self.kwargs["line_pk"]).first()
                if line is None:
                    raise Http404("line does not exist")
                remove_opening_line(actor=self.actor, line=line)
                messages.success(request, _("حُذف السطر."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(detail)


# ---------------------------------------------------------------------------
# Task 1.4 — operational documents
# ---------------------------------------------------------------------------
#
# One set of views, parameterised by document type, mounted three times. The
# type comes from the URL and never from the request body, so a receipt screen
# cannot be talked into creating an issue.


class OperationalListView(InventoryListView):
    """Receipts, issues, or returns — whichever this route names."""

    template_name = "inventory/operational_list.html"
    context_object_name = "documents"
    required_permission = VIEW_STOCK
    search_fields = ("document_number", "evidence_reference", "warehouse__code")
    manage_permission = CREATE_DRAFT_MOVEMENT
    manage_scope = "branch"
    document_type: str = ""

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_documents(self.actor, document_type=self.document_type)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["show_cost"] = may_see_cost(self.actor)
        context["document_type"] = self.document_type
        context["detail_url_name"] = f"inventory:{self.document_type.lower()}_detail"
        return context


class OperationalCreateView(InventoryViewMixin, View):
    """The header first; lines are added on the document's own page."""

    template_name = "inventory/master_form.html"
    required_permission = CREATE_DRAFT_MOVEMENT
    document_type: str = ""
    page_title: Any = ""
    page_hint: Any = ""

    def _branches(self) -> QuerySet[Any]:
        return branches_with_permission(self.actor, CREATE_DRAFT_MOVEMENT)

    def _form(self, branch: Any, data: Any = None) -> Any:
        return OperationalDocumentForm(
            data=data, actor=self.actor, document_type=self.document_type, branch=branch
        )

    def _context(self, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": self.page_title,
            "page_hint": self.page_hint,
            "cancel_url": reverse(f"inventory:{self.document_type.lower()}_list"),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        branch = self._branches().first()
        if branch is None:
            raise Http404("no branch is reachable for this action")
        return render(request, self.template_name, self._context(self._form(branch)))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        branch = self._branches().first()
        if branch is None:
            raise Http404("no branch is reachable for this action")
        form = self._form(branch, data=request.POST)
        if form.is_valid():
            try:
                document = create_document(
                    actor=self.actor,
                    organization=branch.organization,
                    branch=branch,
                    warehouse=form.cleaned_data["warehouse"],
                    document_type=self.document_type,
                    effective_at=form.cleaned_data["effective_at"],
                    evidence_reference=form.cleaned_data["evidence_reference"],
                    narration=form.cleaned_data["narration"],
                    cost_center=form.cleaned_data.get("cost_center"),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُنشئ المستند. أضف السطور ثم رحّله."))
                return HttpResponseRedirect(
                    reverse(f"inventory:{self.document_type.lower()}_detail", args=[document.pk])
                )
        return render(request, self.template_name, self._context(form))


class OperationalDetailView(InventoryViewMixin, View):
    """The document, its lines, its totals, and the actions its status allows."""

    template_name = "inventory/operational_detail.html"
    required_permission = VIEW_STOCK
    document_type: str = ""

    def _document(self) -> Any:
        return resolve_document(self.actor, self.kwargs["pk"], document_type=self.document_type)

    def _line_form(self, document: Any, data: Any = None, selected_item: Any = None) -> Any:
        return OperationalLineForm(
            data=data, actor=self.actor, document=document, selected_item=selected_item
        )

    def _context(self, document: Any, line_form: Any) -> dict[str, Any]:
        lines = list(
            document.lines.select_related(
                "item",
                "item__base_unit",
                "lot",
                "inventory_account",
                "contra_account",
                "movement",
                "source_issue_line",
                "source_issue_line__document",
                "reason_code",
            ).order_by("sequence")
        )
        show_cost = may_see_cost(self.actor)
        post_permission = DOCUMENT_PERMISSION[document.document_type]
        return {
            "document": document,
            "lines": lines,
            "show_cost": show_cost,
            "total_value": (
                sum((line.total_value or Decimal("0") for line in lines), Decimal("0"))
                if show_cost
                else None
            ),
            "line_form": line_form,
            "is_draft": document.status == InventoryDocumentStatus.DRAFT,
            "is_posted": document.status == InventoryDocumentStatus.POSTED,
            "is_waste": document.document_type == InventoryDocumentType.WASTE,
            "can_prepare": has_warehouse_permission(
                self.actor, CREATE_DRAFT_MOVEMENT, document.warehouse
            ),
            "can_post": has_warehouse_permission(self.actor, post_permission, document.warehouse),
            "can_reverse": has_warehouse_permission(
                self.actor, REVERSE_MOVEMENT, document.warehouse
            ),
            "page_title": f"{document.get_document_type_display()} — {document}",
            "back_url": reverse(f"inventory:{self.document_type.lower()}_list"),
            "detail_url_name": f"inventory:{self.document_type.lower()}_detail",
            "line_delete_url_name": f"inventory:{self.document_type.lower()}_line_delete",
            "action_url_names": {
                action: f"inventory:{self.document_type.lower()}_{action}"
                for action in ("post", "reverse", "delete")
            },
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = self._document()
        # An item chosen on a previous submit narrows the conversion and
        # source-issue selectors to what actually applies to it.
        selected = None
        raw_item = request.GET.get("item", "").strip()
        if raw_item.isdigit():
            selected = resolve_item(self.actor, int(raw_item))
        return render(
            request,
            self.template_name,
            self._context(document, self._line_form(document, selected_item=selected)),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = self._document()
        form = self._line_form(document, data=request.POST)
        if form.is_valid():
            item = form.cleaned_data["item"]
            lot = None
            try:
                if form.cleaned_data["lot_code"]:
                    lot = ensure_opening_lot(
                        item=item,
                        code=form.cleaned_data["lot_code"],
                        expiry_date=form.cleaned_data["lot_expiry"],
                    )
                add_document_line(
                    actor=self.actor,
                    document=document,
                    line=DocumentLineInput(
                        item=item,
                        lot=lot,
                        package_conversion=form.cleaned_data["package_conversion"],
                        entered_package_quantity=form.cleaned_data["entered_package_quantity"],
                        measured_base_quantity=form.cleaned_data["measured_base_quantity"],
                        base_quantity=form.cleaned_data["base_quantity"],
                        unit_cost=form.cleaned_data.get("unit_cost"),
                        source_issue_line=form.cleaned_data.get("source_issue_line"),
                        reason_code=form.cleaned_data.get("reason_code"),
                        line_comment=form.cleaned_data.get("line_comment", ""),
                    ),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُضيف السطر."))
                return HttpResponseRedirect(
                    reverse(f"inventory:{self.document_type.lower()}_detail", args=[document.pk])
                )
        return render(request, self.template_name, self._context(document, form))


class OperationalActionView(InventoryViewMixin, View):
    """A POST-only lifecycle action. Every command re-checks authorization."""

    required_permission = VIEW_STOCK
    document_type: str = ""
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = resolve_document(self.actor, self.kwargs["pk"], document_type=self.document_type)
        listing = reverse(f"inventory:{self.document_type.lower()}_list")
        detail = reverse(f"inventory:{self.document_type.lower()}_detail", args=[document.pk])
        try:
            if self.action == "post":
                post_document(actor=self.actor, document=document)
                messages.success(request, _("رُحّل المستند إلى الدفترين."))
            elif self.action == "reverse":
                reverse_document(
                    actor=self.actor, document=document, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("عُكس المستند بالكامل."))
            elif self.action == "delete":
                delete_document(
                    actor=self.actor, document=document, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("حُذفت المسودة."))
                return HttpResponseRedirect(listing)
            elif self.action == "delete_line":
                line = document.lines.filter(pk=self.kwargs["line_pk"]).first()
                if line is None:
                    raise Http404("line does not exist")
                remove_document_line(actor=self.actor, line=line)
                messages.success(request, _("حُذف السطر."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(detail)


class ReconciliationView(InventoryViewMixin, View):
    """
    Inventory against the general ledger, read-only.

    Requires both halves of the story: `inventory.view_valuation` for the
    stock values and `accounting.view_journal` for the GL. There is no repair
    button because there is no repair — a mismatch is investigated.
    """

    template_name = "inventory/reconciliation.html"
    required_permission = VIEW_VALUATION

    def test_func(self) -> bool:
        user = self.request.user
        return bool(
            user.is_authenticated
            and user.has_perm(VIEW_VALUATION)
            and user.has_perm(ACCOUNTING_VIEW_JOURNAL)
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.inventory.reconciliation import verify_inventory_accounting
        from apps.organizations.authorization import organization_scope, resolve_organization
        from apps.organizations.models import Organization
        from apps.organizations.selectors import accessible_branches

        reachable_ids = set(organization_scope(self.actor))
        reachable_ids.update(
            accessible_branches(self.actor).values_list("organization_id", flat=True)
        )
        organizations = Organization.objects.filter(pk__in=reachable_ids, is_active=True).order_by(
            "code"
        )

        selected = None
        mismatches: list[str] | None = None
        raw = request.GET.get("organization", "").strip()
        if raw.isdigit():
            selected = resolve_organization(self.actor, int(raw))
            mismatches = verify_inventory_accounting(selected)

        return render(
            request,
            self.template_name,
            {
                "page_title": _("مطابقة المخزون مع الأستاذ"),
                "page_hint": _(
                    "ثلاث مقارنات للقراءة فقط: كل مستند مع آثاره، والأرصدة مع إعادة "
                    "بناء الدفتر، وقيمة المخزون مع حسابات المراقبة. الاختلاف عيب "
                    "يُحقَّق فيه ولا يُصلَّح تلقائياً."
                ),
                "organizations": organizations,
                "selected": selected,
                "mismatches": mismatches,
            },
        )


# ---------------------------------------------------------------------------
# Transfers (Task 1.5 §W)
# ---------------------------------------------------------------------------
#
# A transfer is a multi-event aggregate, so its screens are too: one page for
# the agreement, one for each arrival, one for the closure, and a timeline on
# the detail page that shows the events in the order they were posted.
#
# Every action button is decided from the same authorization the command layer
# checks. Hiding a button is presentation, never protection — a hand-made POST
# to a hidden action is refused on its merits.


class TransferListView(InventoryListView):
    """Transfers the caller can see from either end."""

    template_name = "inventory/transfer_list.html"
    context_object_name = "transfers"
    required_permission = VIEW_STOCK
    page_title = _("التحويلات المخزنية")
    page_hint = _("بضاعة تنتقل بين مخزنين داخل المؤسسة. تبقى بعهدة الفرع المُرسِل حتى الاستلام.")
    create_url_name = "inventory:transfer_create"
    create_label = _("تحويل جديد")
    search_fields = (
        "transfer_number",
        "evidence_reference",
        "source_warehouse__code",
        "destination_warehouse__code",
    )
    manage_permission = CREATE_DRAFT_MOVEMENT
    manage_scope = "branch"

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_transfers(self.actor)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["show_cost"] = may_see_cost(self.actor)
        return context


class InTransitView(InventoryListView):
    """Goods that have left one place and not arrived at another."""

    template_name = "inventory/in_transit_list.html"
    context_object_name = "rows"
    required_permission = VIEW_STOCK
    page_title = _("بضاعة بالطريق")
    page_hint = _("الكميات المُرسلة التي لم تُستلم ولم تُقفل بعجز. ملك الفرع المُرسِل حتى الآن.")
    search_fields = ("item__code", "item__name_ar", "transfer__transfer_number")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_in_transit(self.actor)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["show_cost"] = may_see_cost(self.actor)
        return context


class TransferCreateView(InventoryViewMixin, View):
    """The header first; lines are added on the transfer's own page."""

    template_name = "inventory/master_form.html"
    required_permission = CREATE_DRAFT_MOVEMENT

    def _context(self, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("تحويل جديد"),
            "page_hint": _("اختر المخزن المُرسِل والمستلم. المخزن الوسيط يختاره النظام."),
            "cancel_url": reverse("inventory:transfer_list"),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(request, TransferForm(actor=self.actor))

    def _render(self, request: HttpRequest, form: Any) -> HttpResponse:
        return render(request, self.template_name, self._context(form))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = TransferForm(data=request.POST, actor=self.actor)
        if form.is_valid():
            source = form.cleaned_data["source_warehouse"]
            try:
                transfer = create_transfer(
                    actor=self.actor,
                    organization=source.branch.organization,
                    source_warehouse=source,
                    destination_warehouse=form.cleaned_data["destination_warehouse"],
                    effective_at=form.cleaned_data["effective_at"],
                    evidence_reference=form.cleaned_data["evidence_reference"],
                    narration=form.cleaned_data["narration"],
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُنشئ التحويل. أضف السطور ثم أرسل البضاعة."))
                return HttpResponseRedirect(
                    reverse("inventory:transfer_detail", args=[transfer.pk])
                )
        return self._render(request, form)


class TransferDetailView(InventoryViewMixin, View):
    """
    The transfer, its lines, its event timeline, and the actions its status
    allows.

    The timeline is the point of this page: dispatched, then each arrival,
    then any closure, in the order they were posted — including the reversed
    ones, greyed but never hidden. A reversal that vanishes from the history
    is a reversal nobody can audit.
    """

    template_name = "inventory/transfer_detail.html"
    required_permission = VIEW_STOCK

    def _transfer(self) -> Any:
        return resolve_transfer(self.actor, self.kwargs["pk"])

    def _context(self, transfer: Any, line_form: Any) -> dict[str, Any]:
        lines = list(
            transfer.lines.select_related("item", "item__base_unit", "lot").order_by("sequence")
        )
        # Per line in Python, not as two annotations. Aggregating over two
        # different multi-valued relations in one queryset joins them against
        # each other, and both sums come back multiplied by the other's row
        # count — a wrong number that looks entirely plausible.
        #
        # Only *active* children count: a reversed receipt put its quantity
        # back on the transfer, so still showing it as received would leave
        # the columns failing to add up to the dispatch.
        for line in lines:
            line.received_quantity = line.receipt_lines.filter(
                receipt__status=InventoryDocumentStatus.POSTED
            ).aggregate(total=Sum("base_quantity"))["total"] or Decimal("0")
            line.shortage_quantity = line.shortage_lines.filter(
                shortage__status=InventoryDocumentStatus.POSTED
            ).aggregate(total=Sum("base_quantity"))["total"] or Decimal("0")
        receipts = list(
            transfer.receipts.select_related("received_by", "reversed_by").order_by(
                "created_at", "id"
            )
        )
        shortages = list(
            transfer.shortages.select_related("cost_center", "closed_by", "reversed_by").order_by(
                "created_at", "id"
            )
        )
        show_cost = may_see_cost(self.actor)
        source_branch = transfer.source_warehouse.branch
        return {
            "transfer": transfer,
            "lines": lines,
            "receipts": receipts,
            "shortages": shortages,
            "show_cost": show_cost,
            "dispatched_value": (
                sum((line.total_value or Decimal("0") for line in lines), Decimal("0"))
                if show_cost
                else None
            ),
            "remaining_value": (
                sum((line.remaining_value for line in lines), Decimal("0")) if show_cost else None
            ),
            "dispatched_quantity": sum((line.base_quantity for line in lines), Decimal("0")),
            "remaining_quantity": sum((line.remaining_quantity for line in lines), Decimal("0")),
            "line_form": line_form,
            "is_draft": transfer.status == StockTransferStatus.DRAFT,
            "is_open": transfer.status in OPEN_TRANSFER_STATUSES,
            "can_prepare": has_warehouse_permission(
                self.actor, CREATE_DRAFT_MOVEMENT, transfer.source_warehouse
            ),
            "can_dispatch": has_warehouse_permission(
                self.actor, POST_TRANSFER, transfer.source_warehouse
            ),
            "can_receive": has_warehouse_permission(
                self.actor, POST_TRANSFER, transfer.destination_warehouse
            ),
            "can_close_shortage": has_branch_permission(
                self.actor, CLOSE_TRANSFER_SHORTAGE, source_branch
            ),
            "can_reverse": has_warehouse_permission(
                self.actor, REVERSE_MOVEMENT, transfer.source_warehouse
            ),
            "page_title": _("تحويل مخزني") if not transfer.transfer_number else str(transfer),
            "back_url": reverse("inventory:transfer_list"),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = self._transfer()
        selected = None
        raw_item = request.GET.get("item", "").strip()
        if raw_item.isdigit():
            selected = resolve_item(self.actor, int(raw_item))
        form = TransferLineForm(actor=self.actor, transfer=transfer, selected_item=selected)
        return render(request, self.template_name, self._context(transfer, form))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = self._transfer()
        form = TransferLineForm(data=request.POST, actor=self.actor, transfer=transfer)
        if form.is_valid():
            item = form.cleaned_data["item"]
            lot = None
            try:
                if form.cleaned_data["lot_code"]:
                    lot = ensure_opening_lot(
                        item=item,
                        code=form.cleaned_data["lot_code"],
                        expiry_date=form.cleaned_data["lot_expiry"],
                    )
                add_transfer_line(
                    actor=self.actor,
                    transfer=transfer,
                    line=TransferLineInput(
                        item=item,
                        lot=lot,
                        package_conversion=form.cleaned_data["package_conversion"],
                        entered_package_quantity=form.cleaned_data["entered_package_quantity"],
                        measured_base_quantity=form.cleaned_data["measured_base_quantity"],
                        base_quantity=form.cleaned_data["base_quantity"],
                    ),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُضيف السطر."))
                return HttpResponseRedirect(
                    reverse("inventory:transfer_detail", args=[transfer.pk])
                )
        return render(request, self.template_name, self._context(transfer, form))


class TransferDispatchView(InventoryViewMixin, View):
    """
    A confirmation before the goods leave.

    Its own page rather than a button, because dispatch is the moment value
    leaves a shelf and enters a state nobody can sell from: worth reading the
    list once more before committing to it.
    """

    template_name = "inventory/transfer_dispatch.html"
    required_permission = VIEW_STOCK

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = resolve_transfer(self.actor, self.kwargs["pk"])
        lines = list(
            transfer.lines.select_related("item", "item__base_unit", "lot").order_by("sequence")
        )
        return render(
            request,
            self.template_name,
            {
                "transfer": transfer,
                "lines": lines,
                "page_title": _("تأكيد الإرسال"),
                "page_hint": _(
                    "بعد الإرسال تصبح البضاعة بالطريق على حساب الفرع المُرسِل حتى الاستلام."
                ),
                "back_url": reverse("inventory:transfer_detail", args=[transfer.pk]),
                "can_dispatch": has_warehouse_permission(
                    self.actor, POST_TRANSFER, transfer.source_warehouse
                ),
            },
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = resolve_transfer(self.actor, self.kwargs["pk"])
        try:
            dispatch_transfer(actor=self.actor, transfer=transfer)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("أُرسلت البضاعة وسُجّلت بالطريق."))
        return HttpResponseRedirect(reverse("inventory:transfer_detail", args=[transfer.pk]))


class TransferActionView(InventoryViewMixin, View):
    """A POST-only transfer action. Every command re-checks authorization."""

    required_permission = VIEW_STOCK
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = resolve_transfer(self.actor, self.kwargs["pk"])
        listing = reverse("inventory:transfer_list")
        detail = reverse("inventory:transfer_detail", args=[transfer.pk])
        try:
            if self.action == "reverse":
                reverse_dispatch(
                    actor=self.actor,
                    transfer=transfer,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(request, _("عُكس الإرسال وعادت البضاعة إلى مخزنها."))
            elif self.action == "delete":
                delete_transfer(
                    actor=self.actor, transfer=transfer, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("حُذفت المسودة."))
                return HttpResponseRedirect(listing)
            elif self.action == "delete_line":
                line = transfer.lines.filter(pk=self.kwargs["line_pk"]).first()
                if line is None:
                    raise Http404("line does not exist")
                remove_transfer_line(actor=self.actor, line=line)
                messages.success(request, _("حُذف السطر."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(detail)


class TransferReceiptCreateView(InventoryViewMixin, View):
    """Start an arrival against an open transfer."""

    template_name = "inventory/master_form.html"
    required_permission = CREATE_DRAFT_MOVEMENT

    def _context(self, transfer: Any, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("استلام تحويل"),
            "page_hint": _("سجّل ما وصل فعلاً. يمكن أن يصل التحويل على دفعات."),
            "cancel_url": reverse("inventory:transfer_detail", args=[transfer.pk]),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = resolve_transfer(self.actor, self.kwargs["pk"])
        return render(
            request,
            self.template_name,
            self._context(transfer, TransferReceiptForm(actor=self.actor)),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = resolve_transfer(self.actor, self.kwargs["pk"])
        form = TransferReceiptForm(data=request.POST, actor=self.actor)
        if form.is_valid():
            try:
                receipt = create_transfer_receipt(
                    actor=self.actor,
                    transfer=transfer,
                    effective_at=form.cleaned_data["effective_at"],
                    evidence_reference=form.cleaned_data["evidence_reference"],
                    narration=form.cleaned_data["narration"],
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُنشئت مسودة الاستلام. أضف الكميات ثم رحّلها."))
                return HttpResponseRedirect(
                    reverse("inventory:transfer_receipt_detail", args=[receipt.pk])
                )
        return render(request, self.template_name, self._context(transfer, form))


class TransferReceiptDetailView(InventoryViewMixin, View):
    """One arrival: its lines, its two business dates, and its journals."""

    template_name = "inventory/transfer_receipt_detail.html"
    required_permission = VIEW_STOCK

    def _receipt(self) -> Any:
        return resolve_receipt(self.actor, self.kwargs["pk"])

    def _context(self, receipt: Any, form: Any) -> dict[str, Any]:
        lines = list(
            receipt.lines.select_related(
                "transfer_line", "transfer_line__item", "transfer_line__item__base_unit"
            ).order_by("sequence")
        )
        show_cost = may_see_cost(self.actor)
        transfer = receipt.transfer
        return {
            "receipt": receipt,
            "transfer": transfer,
            "lines": lines,
            "line_form": form,
            "show_cost": show_cost,
            "total_value": (
                sum((line.allocated_value or Decimal("0") for line in lines), Decimal("0"))
                if show_cost
                else None
            ),
            "is_draft": receipt.status == InventoryDocumentStatus.DRAFT,
            "is_posted": receipt.status == InventoryDocumentStatus.POSTED,
            "can_prepare": has_warehouse_permission(
                self.actor, CREATE_DRAFT_MOVEMENT, transfer.destination_warehouse
            ),
            "can_post": has_warehouse_permission(
                self.actor, POST_TRANSFER, transfer.destination_warehouse
            ),
            "can_reverse": has_warehouse_permission(
                self.actor, REVERSE_MOVEMENT, transfer.destination_warehouse
            ),
            "page_title": _("استلام تحويل"),
            "back_url": reverse("inventory:transfer_detail", args=[transfer.pk]),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = self._receipt()
        form = TransferReceiptLineForm(actor=self.actor, transfer=receipt.transfer)
        return render(request, self.template_name, self._context(receipt, form))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = self._receipt()
        form = TransferReceiptLineForm(
            data=request.POST, actor=self.actor, transfer=receipt.transfer
        )
        if form.is_valid():
            existing = [
                ReceiptLineInput(
                    transfer_line=line.transfer_line,
                    base_quantity=line.base_quantity,
                )
                for line in receipt.lines.select_related("transfer_line").order_by("sequence")
            ]
            try:
                replace_transfer_receipt_lines(
                    actor=self.actor,
                    receipt=receipt,
                    lines=[
                        *existing,
                        ReceiptLineInput(
                            transfer_line=form.cleaned_data["transfer_line"],
                            entered_package_quantity=form.cleaned_data["entered_package_quantity"],
                            measured_base_quantity=form.cleaned_data["measured_base_quantity"],
                            base_quantity=form.cleaned_data["base_quantity"],
                        ),
                    ],
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُضيف السطر."))
                return HttpResponseRedirect(
                    reverse("inventory:transfer_receipt_detail", args=[receipt.pk])
                )
        return render(request, self.template_name, self._context(receipt, form))


class TransferReceiptActionView(InventoryViewMixin, View):
    """A POST-only receipt action."""

    required_permission = VIEW_STOCK
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = resolve_receipt(self.actor, self.kwargs["pk"])
        transfer_url = reverse("inventory:transfer_detail", args=[receipt.transfer_id])
        detail = reverse("inventory:transfer_receipt_detail", args=[receipt.pk])
        try:
            if self.action == "post":
                post_transfer_receipt(actor=self.actor, receipt=receipt)
                messages.success(request, _("رُحّل الاستلام إلى الدفترين."))
            elif self.action == "reverse":
                reverse_transfer_receipt(
                    actor=self.actor, receipt=receipt, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("عُكس الاستلام وعادت الكمية إلى الطريق."))
            elif self.action == "delete":
                delete_transfer_receipt(
                    actor=self.actor, receipt=receipt, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("حُذفت المسودة."))
                return HttpResponseRedirect(transfer_url)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(detail)


class TransferShortageCreateView(InventoryViewMixin, View):
    """
    Close what will never arrive.

    Its own screen and its own permission. This is the one inventory act that
    turns missing stock into an expense, and it asks for a reason, a cost
    centre and evidence before it will do so.
    """

    template_name = "inventory/master_form.html"
    required_permission = VIEW_STOCK

    def _context(self, transfer: Any, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("إقفال بعجز"),
            "page_hint": _("يُقفل كامل الكمية المتبقية بالطريق ويحمّلها على حساب عجز التحويلات."),
            "cancel_url": reverse("inventory:transfer_detail", args=[transfer.pk]),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = resolve_transfer(self.actor, self.kwargs["pk"])
        form = TransferShortageForm(actor=self.actor, transfer=transfer)
        return render(request, self.template_name, self._context(transfer, form))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        transfer = resolve_transfer(self.actor, self.kwargs["pk"])
        form = TransferShortageForm(data=request.POST, actor=self.actor, transfer=transfer)
        if form.is_valid():
            try:
                shortage = create_transfer_shortage(
                    actor=self.actor,
                    transfer=transfer,
                    effective_at=form.cleaned_data["effective_at"],
                    reason=form.cleaned_data["reason"],
                    evidence_reference=form.cleaned_data["evidence_reference"],
                    cost_center=form.cleaned_data["cost_center"],
                )
                post_transfer_shortage(actor=self.actor, shortage=shortage)
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُقفل التحويل بعجز وسُجّلت الخسارة."))
                return HttpResponseRedirect(
                    reverse("inventory:transfer_detail", args=[transfer.pk])
                )
        return render(request, self.template_name, self._context(transfer, form))


class TransferShortageActionView(InventoryViewMixin, View):
    """A POST-only shortage action."""

    required_permission = VIEW_STOCK
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        shortage = resolve_shortage(self.actor, self.kwargs["pk"])
        detail = reverse("inventory:transfer_detail", args=[shortage.transfer_id])
        try:
            if self.action == "reverse":
                reverse_transfer_shortage(
                    actor=self.actor, shortage=shortage, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("عُكس الإقفال وعادت الكمية إلى الطريق."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(detail)


# ---------------------------------------------------------------------------
# Task 1.6 — reason codes, counts and adjustments
# ---------------------------------------------------------------------------
#
# The counting screen is the one that needs care. `BlindCountView` renders
# `blind_count_sheet`, which returns dictionaries that have never held a book
# quantity — so there is no value in the template context for a stray
# `{{ line.book_quantity }}`, a hidden input or a data attribute to leak. The
# review screen is a different view with a different template, reached only
# after submission and only by somebody who may approve.


class ReasonCodeListView(InventoryListView):
    """The organization's vocabulary for why stock went."""

    template_name = "inventory/reason_code_list.html"
    context_object_name = "reason_codes"
    required_permission = VIEW_ITEM
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_REASON_CODES
    manage_scope = "organization"
    page_title = _("أسباب الحركات المخزنية")
    page_hint = _("أسبابك أنت. الرمز ومجاله لا يتغيّران بعد الإنشاء، والمؤرشف يبقى محجوزاً.")
    create_url_name = "inventory:reason_code_create"
    create_label = _("سبب جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_reason_codes(self.actor)


class ReasonCodeCreateView(InventoryWriteView):
    form_class = ReasonCodeForm
    required_permission = MANAGE_REASON_CODES
    page_title = _("سبب جديد")
    page_hint = _("الرمز ومجال الاستخدام يُثبّتان عند الإنشاء ولا يتغيّران بعده.")
    success_message = _("أُضيف السبب.")
    success_url_name = "inventory:reason_code_list"

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_REASON_CODES, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_reason_code(
            actor=self.actor,
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            applies_to=form.cleaned_data["applies_to"],
            name_en=form.cleaned_data["name_en"],
            requires_comment=form.cleaned_data["requires_comment"],
            requires_evidence=form.cleaned_data["requires_evidence"],
        )


class ReasonCodeUpdateView(InventoryWriteView):
    form_class = ReasonCodeForm
    required_permission = MANAGE_REASON_CODES
    page_title = _("تعديل سبب")
    success_message = _("حُفظ السبب.")
    success_url_name = "inventory:reason_code_list"

    def load(self) -> Any:
        return resolve_reason_code(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "organization": instance.organization,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "applies_to": instance.applies_to,
            "requires_comment": instance.requires_comment,
            "requires_evidence": instance.requires_evidence,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_REASON_CODES, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_reason_code(
            actor=self.actor,
            reason_code=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            requires_comment=form.cleaned_data["requires_comment"],
            requires_evidence=form.cleaned_data["requires_evidence"],
            is_active=form.cleaned_data["is_active"],
        )


class StockCountListView(InventoryListView):
    """Counts, with the freeze state visible on every row."""

    template_name = "inventory/count_list.html"
    context_object_name = "counts"
    required_permission = VIEW_STOCK
    search_fields = ("count_number", "reference", "warehouse__code")
    manage_permission = CONDUCT_STOCK_COUNT
    manage_scope = "warehouse"
    page_title = _("الجرد الفعلي")
    page_hint = _("الجرد يُجمّد المخزن من البدء حتى الاعتماد أو الإلغاء. من يعدّ لا يعتمد.")
    create_url_name = "inventory:count_create"
    create_label = _("جرد جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_counts(self.actor)


class StockCountCreateView(InventoryViewMixin, View):
    """Prepare a count. Nothing is frozen until it is started."""

    template_name = "inventory/master_form.html"
    required_permission = CONDUCT_STOCK_COUNT

    def _branches(self) -> QuerySet[Any]:
        return branches_with_permission(self.actor, CONDUCT_STOCK_COUNT)

    def _context(self, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("جرد جديد"),
            "page_hint": _("حدّد المخزن أولاً. التجميد ولقطة الدفاتر يحدثان عند البدء."),
            "cancel_url": reverse("inventory:count_list"),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        branch = self._branches().first()
        if branch is None:
            raise Http404("no branch is reachable for this action")
        return render(
            request,
            self.template_name,
            self._context(StockCountForm(actor=self.actor, branch=branch)),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        branch = self._branches().first()
        if branch is None:
            raise Http404("no branch is reachable for this action")
        form = StockCountForm(data=request.POST, actor=self.actor, branch=branch)
        if form.is_valid():
            try:
                count = create_stock_count(
                    actor=self.actor,
                    organization=branch.organization,
                    branch=branch,
                    warehouse=form.cleaned_data["warehouse"],
                    reference=form.cleaned_data["reference"],
                    reason=form.cleaned_data["reason"],
                    cost_center=form.cleaned_data.get("cost_center"),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُنشئ الجرد. ابدأه لتجميد المخزن وأخذ لقطة الدفاتر."))
                return HttpResponseRedirect(reverse("inventory:count_detail", args=[count.pk]))
        return render(request, self.template_name, self._context(form))


class StockCountDetailView(InventoryViewMixin, View):
    """
    The count as an approver sees it: book, counted, variance.

    Only reachable once the sheet is out of the conductor's hands, and the
    figures are shown only from `SUBMITTED` onwards — before that there is
    nothing to review, and showing the book quantity to whoever opens the page
    would be the leak the blind sheet exists to prevent.
    """

    template_name = "inventory/count_detail.html"
    required_permission = VIEW_STOCK

    def _count(self) -> Any:
        return resolve_count(self.actor, self.kwargs["pk"])

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        count = self._count()
        reviewable = count.status not in (StockCountStatus.DRAFT, StockCountStatus.IN_PROGRESS)
        lines = (
            list(
                count.lines.select_related("item", "base_unit", "lot", "movement").order_by(
                    "sequence"
                )
            )
            if reviewable
            else []
        )
        needs_cost = [
            line
            for line in lines
            if (line.variance_quantity or Decimal("0")) > Decimal("0")
            and line.book_quantity == Decimal("0")
            and line.approved_unit_cost is None
        ]
        return render(
            request,
            self.template_name,
            {
                "count": count,
                "lines": lines,
                "reviewable": reviewable,
                "needs_cost": needs_cost,
                "show_cost": may_see_cost(self.actor),
                "is_draft": count.status == StockCountStatus.DRAFT,
                "is_in_progress": count.status == StockCountStatus.IN_PROGRESS,
                "is_submitted": count.status == StockCountStatus.SUBMITTED,
                "is_posted": count.status == StockCountStatus.POSTED,
                "is_active": count.status in ACTIVE_COUNT_STATUSES,
                "can_conduct": has_warehouse_permission(
                    self.actor, CONDUCT_STOCK_COUNT, count.warehouse
                ),
                "can_approve": has_branch_permission(self.actor, APPROVE_STOCK_COUNT, count.branch),
                "can_reverse": has_branch_permission(self.actor, REVERSE_MOVEMENT, count.branch),
                "is_own_count": count.conducted_by_id == self.actor.pk,
                "page_title": f"{_('جرد')} — {count}",
                "back_url": reverse("inventory:count_list"),
            },
        )


class BlindCountView(InventoryViewMixin, View):
    """
    The counting sheet. Genuinely blind, by construction rather than by CSS.

    The context holds `blind_count_sheet`'s dictionaries and nothing else. No
    book quantity is fetched, so none can be rendered, hidden in an input, or
    read out of the page source by a curious counter.
    """

    template_name = "inventory/count_sheet.html"
    required_permission = CONDUCT_STOCK_COUNT

    def _count(self) -> Any:
        return resolve_count(self.actor, self.kwargs["pk"])

    def _context(self, count: Any, form: Any) -> dict[str, Any]:
        return {
            "count": count,
            "rows": blind_count_sheet(actor=self.actor, count=count),
            "unexpected_form": form,
            "is_in_progress": count.status == StockCountStatus.IN_PROGRESS,
            "page_title": f"{_('كشف الجرد')} — {count.count_number}",
            "back_url": reverse("inventory:count_detail", args=[count.pk]),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        count = self._count()
        return render(
            request,
            self.template_name,
            self._context(count, UnexpectedCountLineForm(actor=self.actor, count=count)),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Record every counted quantity the sheet came back with."""
        count = self._count()
        entries: list[CountEntry] = []
        errors: list[str] = []
        for key in request.POST:
            if not key.startswith("counted-"):
                continue
            text = request.POST.get(key, "").strip()
            if not text:
                continue
            if "," in text:
                errors.append(str(_("استخدم النقطة العشرية لا الفاصلة.")))
                continue
            try:
                quantity = Decimal(text)
            except ArithmeticError, InvalidOperation, ValueError:
                errors.append(str(_("قيمة عشرية غير صالحة.")))
                continue
            line_id = key.removeprefix("counted-")
            if not line_id.isdigit():
                continue
            entries.append(
                CountEntry(
                    # Constrained to this count: a line id from another sheet
                    # is a 404, not a write through the wrong document.
                    line=resolve_count_line(self.actor, int(line_id), count=count),
                    base_quantity=quantity,
                    note=request.POST.get(f"note-{line_id}", "").strip(),
                )
            )
        if not errors:
            try:
                record_stock_counts(actor=self.actor, count=count, entries=entries)
            except ValidationError as error:
                errors.extend(error.messages)
            else:
                messages.success(request, _("حُفظت الكميات المعدودة."))
                return HttpResponseRedirect(reverse("inventory:count_sheet", args=[count.pk]))
        for message in errors:
            messages.error(request, message)
        return render(
            request,
            self.template_name,
            self._context(count, UnexpectedCountLineForm(actor=self.actor, count=count)),
        )


class UnexpectedCountLineView(InventoryViewMixin, View):
    """Add stock the books do not have. Quantity only — never a cost."""

    required_permission = CONDUCT_STOCK_COUNT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        count = resolve_count(self.actor, self.kwargs["pk"])
        form = UnexpectedCountLineForm(data=request.POST, actor=self.actor, count=count)
        if form.is_valid():
            try:
                lot = None
                item = form.cleaned_data["item"]
                if form.cleaned_data["lot_code"]:
                    lot = ensure_opening_lot(
                        item=item,
                        code=form.cleaned_data["lot_code"],
                        expiry_date=form.cleaned_data["lot_expiry"],
                    )
                add_unexpected_count_line(
                    actor=self.actor,
                    count=count,
                    item=item,
                    lot=lot,
                    base_quantity=form.cleaned_data["base_quantity"],
                    note=form.cleaned_data["note"],
                )
            except ValidationError as error:
                for message in error.messages:
                    messages.error(request, message)
            else:
                messages.success(request, _("سُجّل صنف غير متوقّع."))
        else:
            for field_errors in form.errors.values():
                for problem in field_errors:
                    messages.error(request, str(problem))
        return HttpResponseRedirect(reverse("inventory:count_sheet", args=[count.pk]))


class StockCountActionView(InventoryViewMixin, View):
    """Start, submit, approve, cancel or reverse — one POST each."""

    required_permission = VIEW_STOCK
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        count = resolve_count(self.actor, self.kwargs["pk"])
        reason = request.POST.get("reason", "").strip()
        try:
            if self.action == "start":
                start_stock_count(actor=self.actor, count=count)
                messages.success(request, _("بدأ الجرد وجُمّد المخزن."))
                return HttpResponseRedirect(reverse("inventory:count_sheet", args=[count.pk]))
            if self.action == "submit":
                submit_stock_count(actor=self.actor, count=count)
                messages.success(request, _("قُدّم الجرد للاعتماد."))
            elif self.action == "approve":
                approve_stock_count(
                    actor=self.actor, count=count, costs=self._approved_costs(request, count)
                )
                messages.success(request, _("اعتُمد الجرد ورُحّلت الفروقات وفُكّ التجميد."))
            elif self.action == "cancel":
                cancel_stock_count(actor=self.actor, count=count, reason=reason)
                messages.success(request, _("أُلغي الجرد وفُكّ التجميد."))
            elif self.action == "reverse":
                reverse_stock_count(actor=self.actor, count=count, reason=reason)
                messages.success(request, _("عُكست فروقات الجرد."))
            elif self.action == "delete":
                delete_stock_count(actor=self.actor, count=count)
                messages.success(request, _("حُذفت مسودة الجرد."))
                return HttpResponseRedirect(reverse("inventory:count_list"))
        except ValidationError as error:
            for message in error.messages:
                messages.error(request, message)
        return HttpResponseRedirect(reverse("inventory:count_detail", args=[count.pk]))

    def _approved_costs(self, request: HttpRequest, count: Any) -> list[ApprovedCost]:
        """The unit costs the approver typed for gains the books cannot price."""
        costs: list[ApprovedCost] = []
        for key in request.POST:
            if not key.startswith("cost-"):
                continue
            text = request.POST.get(key, "").strip()
            if not text:
                continue
            line_id = key.removeprefix("cost-")
            if not line_id.isdigit():
                continue
            if "," in text:
                raise ValidationError(_("استخدم النقطة العشرية لا الفاصلة."))
            try:
                unit_cost = Decimal(text)
            except (ArithmeticError, InvalidOperation, ValueError) as error:
                raise ValidationError(_("قيمة عشرية غير صالحة.")) from error
            costs.append(
                ApprovedCost(
                    line=resolve_count_line(self.actor, int(line_id), count=count),
                    unit_cost=unit_cost,
                    zero_confirmed=request.POST.get(f"zero-{line_id}") == "on",
                )
            )
        return costs


class AdjustmentListView(InventoryListView):
    """Manual adjustments — the exception, and visibly labelled as one."""

    template_name = "inventory/adjustment_list.html"
    context_object_name = "adjustments"
    required_permission = VIEW_STOCK
    search_fields = ("document_number", "evidence_reference", "warehouse__code")
    manage_permission = POST_ADJUSTMENT
    manage_scope = "branch"
    page_title = _("التسويات المخزنية اليدوية")
    page_hint = _("لتصحيح دفاتر خاطئة فقط. ليست بديلاً عن استلام أو صرف أو تحويل أو جرد.")
    create_url_name = "inventory:adjustment_create"
    create_label = _("تسوية جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_adjustments(self.actor)


class AdjustmentCreateView(InventoryViewMixin, View):
    template_name = "inventory/master_form.html"
    required_permission = POST_ADJUSTMENT

    def _branches(self) -> QuerySet[Any]:
        return branches_with_permission(self.actor, POST_ADJUSTMENT)

    def _context(self, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("تسوية جديدة"),
            "page_hint": _("التسوية تُثبت أن الدفاتر كانت خاطئة، لا أن البضاعة تحرّكت."),
            "cancel_url": reverse("inventory:adjustment_list"),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        branch = self._branches().first()
        if branch is None:
            raise Http404("no branch is reachable for this action")
        return render(
            request,
            self.template_name,
            self._context(AdjustmentForm(actor=self.actor, branch=branch)),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        branch = self._branches().first()
        if branch is None:
            raise Http404("no branch is reachable for this action")
        form = AdjustmentForm(data=request.POST, actor=self.actor, branch=branch)
        if form.is_valid():
            try:
                document = create_adjustment(
                    actor=self.actor,
                    organization=branch.organization,
                    branch=branch,
                    warehouse=form.cleaned_data["warehouse"],
                    effective_at=form.cleaned_data["effective_at"],
                    evidence_reference=form.cleaned_data["evidence_reference"],
                    reason=form.cleaned_data["reason"],
                    cost_center=form.cleaned_data.get("cost_center"),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُنشئت التسوية. أضف السطور ثم رحّلها."))
                return HttpResponseRedirect(
                    reverse("inventory:adjustment_detail", args=[document.pk])
                )
        return render(request, self.template_name, self._context(form))


class AdjustmentDetailView(InventoryViewMixin, View):
    template_name = "inventory/adjustment_detail.html"
    required_permission = VIEW_STOCK

    def _document(self) -> Any:
        return resolve_adjustment(self.actor, self.kwargs["pk"])

    def _context(self, document: Any, form: Any) -> dict[str, Any]:
        lines = list(
            document.lines.select_related(
                "item", "item__base_unit", "lot", "reason_code", "movement", "control_account"
            ).order_by("sequence")
        )
        return {
            "document": document,
            "lines": lines,
            "line_form": form,
            "show_cost": may_see_cost(self.actor),
            "is_draft": document.status == InventoryDocumentStatus.DRAFT,
            "is_posted": document.status == InventoryDocumentStatus.POSTED,
            "can_post": has_branch_permission(self.actor, POST_ADJUSTMENT, document.branch),
            "can_reverse": has_branch_permission(self.actor, REVERSE_MOVEMENT, document.branch),
            "page_title": f"{_('تسوية مخزنية')} — {document}",
            "back_url": reverse("inventory:adjustment_list"),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = self._document()
        return render(
            request,
            self.template_name,
            self._context(document, AdjustmentLineForm(actor=self.actor, document=document)),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = self._document()
        form = AdjustmentLineForm(data=request.POST, actor=self.actor, document=document)
        if form.is_valid():
            try:
                item = form.cleaned_data["item"]
                lot = None
                if form.cleaned_data["lot_code"]:
                    lot = ensure_opening_lot(
                        item=item,
                        code=form.cleaned_data["lot_code"],
                        expiry_date=form.cleaned_data["lot_expiry"],
                    )
                add_adjustment_line(
                    actor=self.actor,
                    document=document,
                    line=AdjustmentLineInput(
                        kind=form.cleaned_data["kind"],
                        item=item,
                        lot=lot,
                        reason_code=form.cleaned_data["reason_code"],
                        base_quantity=form.cleaned_data["base_quantity"],
                        unit_cost=form.cleaned_data["unit_cost"],
                        zero_cost_confirmed=form.cleaned_data["zero_cost_confirmed"],
                        value_adjustment=form.cleaned_data["value_adjustment"],
                        line_comment=form.cleaned_data["line_comment"],
                    ),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُضيف السطر."))
                return HttpResponseRedirect(
                    reverse("inventory:adjustment_detail", args=[document.pk])
                )
        return render(request, self.template_name, self._context(document, form))


class AdjustmentActionView(InventoryViewMixin, View):
    required_permission = VIEW_STOCK
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = resolve_adjustment(self.actor, self.kwargs["pk"])
        reason = request.POST.get("reason", "").strip()
        try:
            if self.action == "post":
                post_adjustment(actor=self.actor, document=document)
                messages.success(request, _("رُحّلت التسوية."))
            elif self.action == "reverse":
                reverse_adjustment(actor=self.actor, document=document, reason=reason)
                messages.success(request, _("عُكست التسوية."))
            elif self.action == "delete":
                delete_adjustment(actor=self.actor, document=document)
                messages.success(request, _("حُذفت مسودة التسوية."))
                return HttpResponseRedirect(reverse("inventory:adjustment_list"))
        except ValidationError as error:
            for message in error.messages:
                messages.error(request, message)
        return HttpResponseRedirect(reverse("inventory:adjustment_detail", args=[document.pk]))
