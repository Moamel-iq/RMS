"""
Procurement screens, mounted inside the shell.

The list, write and action machinery is reused from `apps.inventory.views`
rather than copied. It is generic — a scoped queryset, a per-row action
decision, an htmx partial swap, a POST-only archive — and it lives in
inventory only because inventory needed it first. A second copy here would
drift from the original within two tasks, and the drift would be in the
authorization behaviour, which is the part that must not vary.

Procurement depends on inventory already (a receipt posts through its kernel),
so the import direction is the one that was always going to hold. Extracting
these bases into `apps.core` is worth doing when a third module needs them; it
is a refactor of certified code and does not belong inside a feature task.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.inventory.views import (
    InventoryActionView,
    InventoryListView,
    InventoryViewMixin,
    InventoryWriteView,
)
from apps.organizations.authorization import (
    has_branch_permission,
    has_organization_master_data_permission,
    has_organization_permission,
    has_warehouse_permission,
    organizations_with_permission,
    require_branch_permission,
    require_organization_permission,
    require_reachable_organization_permission,
    require_warehouse_permission,
)
from apps.procurement.comparison import award_quotation, comparison_for_request
from apps.procurement.credit_notes import (
    add_credit_allocation,
    add_return_allocation,
    create_supplier_credit_note,
    note_timeline,
    post_supplier_credit_note,
    remaining_book_value,
    remaining_credit_quantity,
    remove_credit_allocation,
    remove_return_allocation,
    reverse_supplier_credit_note,
    unallocated_credit,
)
from apps.procurement.forms import (
    CreditAllocationForm,
    CreditReturnAllocationForm,
    GoodsReceiptForm,
    GoodsReceiptLineForm,
    InspectLineForm,
    InvoiceAccountLineForm,
    InvoiceInventoryLineForm,
    MatchAllocationForm,
    PaymentAllocationForm,
    PurchaseOrderForm,
    PurchaseOrderLineForm,
    PurchaseRequestForm,
    PurchaseRequestLineForm,
    SupplierCreditNoteForm,
    SupplierForm,
    SupplierInvoiceForm,
    SupplierItemForm,
    SupplierPaymentForm,
    SupplierQuotationForm,
    SupplierQuotationLineForm,
    SupplierReturnForm,
    SupplierReturnLineForm,
)
from apps.procurement.invoices import (
    add_account_line,
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    invoice_timeline,
    outstanding_amount,
    post_supplier_invoice,
    remove_invoice_line,
    return_supplier_invoice_to_draft,
    reverse_supplier_invoice,
)
from apps.procurement.matching import (
    add_allocation,
    cancel_purchase_match,
    coverage_for_invoice,
    create_purchase_match,
    live_posting_for,
    mark_match_ready,
    remove_allocation,
)
from apps.procurement.models import (
    GoodsReceiptStatus,
    PurchaseMatchStatus,
    PurchaseOrderStatus,
    PurchaseRequestStatus,
    SupplierCreditNoteStatus,
    SupplierInvoiceStatus,
    SupplierPaymentStatus,
    SupplierQuotationStatus,
    SupplierReturnStatus,
)
from apps.procurement.payments import (
    add_payment_allocation,
    advance_remainder,
    create_supplier_payment,
    payment_timeline,
    post_supplier_payment,
    remove_payment_allocation,
    reverse_supplier_payment,
)
from apps.procurement.payments import allocated_total as payment_allocated_total
from apps.procurement.permissions import (
    APPROVE_PURCHASE_ORDER,
    APPROVE_PURCHASE_REQUEST,
    APPROVE_SUPPLIER_INVOICE,
    AWARD_QUOTATION,
    CANCEL_PURCHASE_MATCH,
    CANCEL_PURCHASE_ORDER,
    CREATE_GOODS_RECEIPT,
    CREATE_PURCHASE_ORDER,
    CREATE_PURCHASE_REQUEST,
    CREATE_SUPPLIER_CREDIT_NOTE,
    CREATE_SUPPLIER_INVOICE,
    CREATE_SUPPLIER_PAYMENT,
    CREATE_SUPPLIER_RETURN,
    INSPECT_GOODS_RECEIPT,
    ISSUE_PURCHASE_ORDER,
    MANAGE_QUOTATIONS,
    MANAGE_SUPPLIER_ITEMS,
    MANAGE_SUPPLIERS,
    MATCH_SUPPLIER_INVOICE,
    POST_GOODS_RECEIPT,
    POST_SUPPLIER_CREDIT_NOTE,
    POST_SUPPLIER_INVOICE,
    POST_SUPPLIER_PAYMENT,
    POST_SUPPLIER_RETURN,
    REVERSE_GOODS_RECEIPT,
    REVERSE_SUPPLIER_CREDIT_NOTE,
    REVERSE_SUPPLIER_INVOICE,
    REVERSE_SUPPLIER_PAYMENT,
    REVERSE_SUPPLIER_RETURN,
    VIEW_GOODS_RECEIPT,
    VIEW_PURCHASE_MATCH,
    VIEW_PURCHASE_ORDER,
    VIEW_PURCHASE_REQUEST,
    VIEW_QUOTATION,
    VIEW_SUPPLIER,
    VIEW_SUPPLIER_COST,
    VIEW_SUPPLIER_CREDIT_NOTE,
    VIEW_SUPPLIER_INVOICE,
    VIEW_SUPPLIER_ITEM,
    VIEW_SUPPLIER_PAYMENT,
    VIEW_SUPPLIER_RETURN,
)
from apps.procurement.posting import (
    post_goods_receipt,
    receipt_timeline,
    reverse_goods_receipt,
)
from apps.procurement.returns import (
    add_return_line,
    create_supplier_return,
    post_supplier_return,
    remove_return_line,
    return_timeline,
    reverse_supplier_return,
)
from apps.procurement.selectors import (
    matching_queue,
    order_version_history,
    outstanding_order_lines,
    resolve_credit_allocation,
    resolve_credit_return_allocation,
    resolve_goods_receipt,
    resolve_invoice_line,
    resolve_match_allocation,
    resolve_order_line,
    resolve_payment_allocation,
    resolve_purchase_match,
    resolve_purchase_order,
    resolve_purchase_request,
    resolve_quotation,
    resolve_quotation_line,
    resolve_receipt_line,
    resolve_request_line,
    resolve_return_line,
    resolve_supplier,
    resolve_supplier_credit_note,
    resolve_supplier_invoice,
    resolve_supplier_item,
    resolve_supplier_payment,
    resolve_supplier_return,
    returnable_receipt_lines,
    visible_goods_receipts,
    visible_purchase_matches,
    visible_purchase_orders,
    visible_purchase_requests,
    visible_quotations,
    visible_supplier_credit_notes,
    visible_supplier_invoices,
    visible_supplier_items,
    visible_supplier_payments,
    visible_supplier_returns,
    visible_suppliers,
)
from apps.procurement.services import (
    add_order_line,
    add_quotation_line,
    add_receipt_line,
    add_request_line,
    approve_purchase_order,
    approve_purchase_request,
    cancel_purchase_order,
    cancel_purchase_request,
    create_goods_receipt,
    create_purchase_order,
    create_purchase_request,
    create_supplier,
    create_supplier_item,
    create_supplier_quotation,
    decline_supplier_quotation,
    inspect_receipt_line,
    issue_purchase_order,
    reject_purchase_request,
    remove_order_line,
    remove_quotation_line,
    remove_receipt_line,
    remove_request_line,
    revise_purchase_order,
    submit_purchase_request,
    submit_supplier_quotation,
    update_supplier,
    update_supplier_item,
)


class SupplierListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_SUPPLIER
    template_name = "procurement/supplier_list.html"
    context_object_name = "suppliers"
    page_title = _("الموردون")
    page_hint = _(
        "سجل الموردين على مستوى المؤسسة، مشترك بين الفروع. الرصيد المستحق "
        "يُحتسب من الفواتير والدفعات المرحّلة، ولا يُخزَّن في هذا السجل."
    )
    search_fields = ("code", "name_ar", "name_en", "contact_name", "phone")
    manage_permission = MANAGE_SUPPLIERS
    create_url_name = "procurement:supplier_create"
    create_label = _("مورد جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_suppliers(self.actor).order_by("code")


class SupplierWriteView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/supplier_form.html"
    form_class = SupplierForm
    required_permission = MANAGE_SUPPLIERS
    success_url_name = "procurement:supplier_list"

    def _fields(self, form: Any) -> dict[str, Any]:
        data = form.cleaned_data
        return {
            "name_ar": data["name_ar"],
            "name_en": data.get("name_en", ""),
            "contact_name": data.get("contact_name", ""),
            "phone": data.get("phone", ""),
            "email": data.get("email", ""),
            "address": data.get("address", ""),
            "payment_terms_days": data["payment_terms_days"],
            "credit_limit": data.get("credit_limit"),
            "notes": data.get("notes", ""),
        }


class SupplierCreateView(SupplierWriteView):
    page_title = _("مورد جديد")
    page_hint = _("الرمز يُخزَّن بأحرف كبيرة ولا يمكن تغييره بعد الحفظ.")
    success_message = _("تم إنشاء المورد.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SUPPLIERS, form.selected_organization()
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_supplier(
            organization=form.selected_organization(),
            code=form.cleaned_data["code"],
            **self._fields(form),
        )


class SupplierUpdateView(SupplierWriteView):
    page_title = _("تعديل مورد")
    page_hint = _(
        "تغيير مهلة السداد يسري على المستندات الجديدة فقط. المستندات المرحّلة "
        "تحمل نسختها الخاصة من الشروط."
    )
    success_message = _("تم حفظ التعديل.")

    def load(self) -> Any:
        return resolve_supplier(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "contact_name": instance.contact_name,
            "phone": instance.phone,
            "email": instance.email,
            "address": instance.address,
            "payment_terms_days": instance.payment_terms_days,
            "credit_limit": instance.credit_limit,
            "notes": instance.notes,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SUPPLIERS, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_supplier(supplier=instance, is_active=instance.is_active, **self._fields(form))


class SupplierActionView(InventoryActionView):
    module_key = "procurement"
    required_permission = MANAGE_SUPPLIERS
    success_url_name = "procurement:supplier_list"

    def load(self) -> Any:
        return resolve_supplier(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SUPPLIERS, instance.organization
        )

    def perform(self, instance: Any) -> None:
        update_supplier(
            supplier=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            contact_name=instance.contact_name,
            phone=instance.phone,
            email=instance.email,
            address=instance.address,
            payment_terms_days=instance.payment_terms_days,
            credit_limit=instance.credit_limit,
            notes=instance.notes,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Supplier item catalogue
# ---------------------------------------------------------------------------


class SupplierItemListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_ITEM
    template_name = "procurement/supplier_item_list.html"
    context_object_name = "supplier_items"
    page_title = _("كتالوج الموردين")
    page_hint = _(
        "من يورّد أي صنف، بأي عبوة، وبأي مهلة. السعر هنا للتخطيط فقط — "
        "تقييم المخزون يأتي من سعر الاستلام نفسه ولا يقرأ هذا الجدول."
    )
    search_fields = (
        "supplier__code",
        "supplier__name_ar",
        "item__code",
        "item__name_ar",
        "supplier_sku",
    )
    manage_permission = MANAGE_SUPPLIER_ITEMS
    create_url_name = "procurement:supplier_item_create"
    create_label = _("سطر كتالوج جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_supplier_items(self.actor).order_by(
            "supplier__code", "item__code", "-effective_from"
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Prices are a separate permission, and the column is omitted rather
        # than blanked — an empty cell still says a number belongs there.
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        return context


class SupplierItemWriteView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/supplier_item_form.html"
    form_class = SupplierItemForm
    required_permission = MANAGE_SUPPLIER_ITEMS
    success_url_name = "procurement:supplier_item_list"

    def _terms(self, form: Any) -> dict[str, Any]:
        data = form.cleaned_data
        return {
            "supplier_sku": data.get("supplier_sku", ""),
            "supplier_description": data.get("supplier_description", ""),
            "last_quoted_price": data.get("last_quoted_price"),
            "lead_time_days": data.get("lead_time_days"),
            "minimum_order_quantity": data.get("minimum_order_quantity"),
            "is_preferred": data.get("is_preferred", False),
            "notes": data.get("notes", ""),
        }


class SupplierItemCreateView(SupplierItemWriteView):
    page_title = _("سطر كتالوج جديد")
    page_hint = _(
        "العبوة يجب أن تكون عبوة يعرف الصنف تحويلها إلى وحدته الأساسية، "
        "وإلا لن يجد الاستلام معاملاً يثبته."
    )
    success_message = _("تمت إضافة سطر الكتالوج.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SUPPLIER_ITEMS, form.cleaned_data["supplier"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_supplier_item(
            supplier=form.cleaned_data["supplier"],
            item=form.cleaned_data["item"],
            package_unit=form.cleaned_data.get("package_unit"),
            effective_from=form.cleaned_data["effective_from"],
            effective_to=form.cleaned_data.get("effective_to"),
            **self._terms(form),
        )


class SupplierItemUpdateView(SupplierItemWriteView):
    page_title = _("تعديل سطر كتالوج")
    page_hint = _(
        "المورد والصنف والعبوة وتاريخ البداية تُعرّف السطر ولا تُعدَّل. "
        "تغييرها يعني سطراً آخر، وهذا ما يفعله الإصدار الجديد."
    )
    success_message = _("تم حفظ التعديل.")

    def load(self) -> Any:
        return resolve_supplier_item(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "supplier": instance.supplier,
            "item": instance.item,
            "package_unit": instance.package_unit,
            "supplier_sku": instance.supplier_sku,
            "supplier_description": instance.supplier_description,
            "last_quoted_price": instance.last_quoted_price,
            "lead_time_days": instance.lead_time_days,
            "minimum_order_quantity": instance.minimum_order_quantity,
            "is_preferred": instance.is_preferred,
            "effective_from": instance.effective_from,
            "effective_to": instance.effective_to,
            "notes": instance.notes,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SUPPLIER_ITEMS, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_supplier_item(
            supplier_item=instance,
            effective_to=form.cleaned_data.get("effective_to"),
            is_active=instance.is_active,
            **self._terms(form),
        )


class SupplierItemActionView(InventoryActionView):
    module_key = "procurement"
    required_permission = MANAGE_SUPPLIER_ITEMS
    success_url_name = "procurement:supplier_item_list"

    def load(self) -> Any:
        return resolve_supplier_item(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SUPPLIER_ITEMS, instance.organization
        )

    def perform(self, instance: Any) -> None:
        update_supplier_item(
            supplier_item=instance,
            supplier_sku=instance.supplier_sku,
            supplier_description=instance.supplier_description,
            last_quoted_price=instance.last_quoted_price,
            lead_time_days=instance.lead_time_days,
            minimum_order_quantity=instance.minimum_order_quantity,
            # An archived row is not where anything is normally bought.
            is_preferred=instance.is_preferred and self.activate,
            effective_to=instance.effective_to,
            notes=instance.notes,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Purchase requests
# ---------------------------------------------------------------------------


class PurchaseRequestListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_PURCHASE_REQUEST
    template_name = "procurement/purchase_request_list.html"
    context_object_name = "requests"
    page_title = _("طلبات الشراء")
    page_hint = _(
        "ما يطلبه الفرع. لا يحرّك مخزوناً ولا يُنشئ التزاماً في أي حالة — "
        "الاعتماد اتفاق على الحاجة، وأمر الشراء وحده هو الالتزام التجاري."
    )
    search_fields = ("number", "purpose", "warehouse__code", "requested_by__username")
    manage_permission = CREATE_PURCHASE_REQUEST
    manage_scope = "branch"
    create_url_name = "procurement:purchase_request_create"
    create_label = _("طلب شراء جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_purchase_requests(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in PurchaseRequestStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = PurchaseRequestStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        return context


class PurchaseRequestCreateView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/purchase_request_form.html"
    form_class = PurchaseRequestForm
    required_permission = CREATE_PURCHASE_REQUEST
    success_url_name = "procurement:purchase_request_list"
    page_title = _("طلب شراء جديد")
    page_hint = _("يُفتح كمسودة بلا رقم. الرقم يُسحب عند الإرسال حتى لا تحرق مسودةٌ رقماً.")
    success_message = _("تم إنشاء المسودة. أضف الأصناف ثم أرسلها.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(
            self.actor, CREATE_PURCHASE_REQUEST, form.cleaned_data["warehouse"].branch
        )

    def perform(self, instance: Any, form: Any) -> None:
        warehouse = form.cleaned_data["warehouse"]
        self.created = create_purchase_request(
            branch=warehouse.branch,
            requested_by=self.actor,
            warehouse=warehouse,
            location=form.cleaned_data.get("location"),
            required_date=form.cleaned_data["required_date"],
            purpose=form.cleaned_data["purpose"],
            notes=form.cleaned_data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is None:
            return reverse(self.success_url_name)
        # Straight to the detail screen: a header with no lines is not yet a
        # request, and sending the user back to the list would hide that.
        return reverse("procurement:purchase_request_detail", args=[created.pk])


class PurchaseRequestDetailView(InventoryViewMixin, View):
    """The header, its lines, the add-line form, and whatever may happen next."""

    module_key = "procurement"
    required_permission = VIEW_PURCHASE_REQUEST
    template_name = "procurement/purchase_request_detail.html"

    def load(self) -> Any:
        return resolve_purchase_request(self.actor, self.kwargs["pk"])

    def context(self, request_document: Any, form: Any) -> dict[str, Any]:
        may_edit = self.actor.has_perm(CREATE_PURCHASE_REQUEST) and has_branch_permission(
            self.actor, CREATE_PURCHASE_REQUEST, request_document.branch
        )
        may_decide = has_branch_permission(
            self.actor, APPROVE_PURCHASE_REQUEST, request_document.branch
        )
        return {
            "request_document": request_document,
            "lines": request_document.lines.select_related(
                "item", "item__base_unit", "package_unit", "preferred_supplier"
            ).order_by("sequence"),
            "form": form,
            "page_title": request_document.number or _("مسودة طلب شراء"),
            "may_edit": may_edit and request_document.is_editable,
            "may_decide": may_decide,
            "is_submitted": request_document.status == PurchaseRequestStatus.SUBMITTED,
            "may_cancel": (may_edit or may_decide)
            and request_document.status
            in {
                PurchaseRequestStatus.DRAFT,
                PurchaseRequestStatus.SUBMITTED,
                PurchaseRequestStatus.APPROVED,
            },
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = self.load()
        form = PurchaseRequestLineForm(actor=self.actor, request_document=document)
        return render(request, self.template_name, self.context(document, form))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = self.load()
        require_branch_permission(self.actor, CREATE_PURCHASE_REQUEST, document.branch)
        form = PurchaseRequestLineForm(
            actor=self.actor, request_document=document, data=request.POST
        )
        if form.is_valid():
            try:
                add_request_line(
                    request=document,
                    item=form.cleaned_data["item"],
                    package_unit=form.cleaned_data.get("package_unit"),
                    entered_quantity=form.cleaned_data["entered_quantity"],
                    preferred_supplier=form.cleaned_data.get("preferred_supplier"),
                    note=form.cleaned_data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة السطر."))
                return HttpResponseRedirect(
                    reverse("procurement:purchase_request_detail", args=[document.pk])
                )
        return render(request, self.template_name, self.context(document, form))


class PurchaseRequestLineDeleteView(InventoryViewMixin, View):
    """POST-only. A GET that deleted a line would fire on a link prefetch."""

    module_key = "procurement"
    required_permission = CREATE_PURCHASE_REQUEST

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = resolve_purchase_request(self.actor, self.kwargs["pk"])
        require_branch_permission(self.actor, CREATE_PURCHASE_REQUEST, document.branch)
        line = resolve_request_line(self.actor, request=document, line_id=self.kwargs["line_id"])
        try:
            remove_request_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف السطر."))
        return HttpResponseRedirect(
            reverse("procurement:purchase_request_detail", args=[document.pk])
        )


class PurchaseRequestTransitionView(InventoryViewMixin, View):
    """
    Submit, approve, reject or cancel — one POST-only route per act.

    The permission differs per transition and is checked against the request's
    own branch, never globally: holding `approve_purchase_request` somewhere is
    not holding it here.
    """

    module_key = "procurement"
    required_permission = VIEW_PURCHASE_REQUEST
    #: One of "submit", "approve", "reject", "cancel".
    transition: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = resolve_purchase_request(self.actor, self.kwargs["pk"])
        reason = request.POST.get("reason", "").strip()

        needed = (
            CREATE_PURCHASE_REQUEST
            if self.transition in {"submit", "cancel"}
            else APPROVE_PURCHASE_REQUEST
        )
        require_branch_permission(self.actor, needed, document.branch)

        try:
            if self.transition == "submit":
                submit_purchase_request(request=document, actor=self.actor)
                messages.success(request, _("تم إرسال الطلب."))
            elif self.transition == "approve":
                approve_purchase_request(request=document, actor=self.actor, reason=reason)
                messages.success(request, _("تم اعتماد الطلب."))
            elif self.transition == "reject":
                reject_purchase_request(request=document, actor=self.actor, reason=reason)
                messages.success(request, _("تم رفض الطلب."))
            else:
                cancel_purchase_request(request=document, actor=self.actor, reason=reason)
                messages.success(request, _("تم إلغاء الطلب."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))

        return HttpResponseRedirect(
            reverse("procurement:purchase_request_detail", args=[document.pk])
        )


# ---------------------------------------------------------------------------
# Supplier quotations
# ---------------------------------------------------------------------------


class SupplierQuotationListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_QUOTATION
    template_name = "procurement/quotation_list.html"
    context_object_name = "quotations"
    page_title = _("عروض الموردين")
    page_hint = _(
        "ما يقوله المورد إن السعر سيكون. إثبات لا التزام — لا مخزون ولا قيد "
        "ولا ذمة في أي حالة، حتى بعد الإرساء."
    )
    search_fields = ("number", "supplier_reference", "supplier__code", "supplier__name_ar")
    manage_permission = MANAGE_QUOTATIONS
    create_url_name = "procurement:quotation_create"
    create_label = _("عرض جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_quotations(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in SupplierQuotationStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = SupplierQuotationStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        return context


class SupplierQuotationCreateView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/quotation_form.html"
    form_class = SupplierQuotationForm
    required_permission = MANAGE_QUOTATIONS
    success_url_name = "procurement:quotation_list"
    page_title = _("عرض مورد جديد")
    page_hint = _("يُفتح كمسودة. الرقم يُسحب عند الاستلام، ومرجع الإثبات مطلوب عندها.")
    success_message = _("تم إنشاء المسودة. أضف الأسطر ثم سجّل الاستلام.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_QUOTATIONS, form.cleaned_data["supplier"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = create_supplier_quotation(
            supplier=data["supplier"],
            recorded_by=self.actor,
            request=data.get("request"),
            quoted_at=data["quoted_at"],
            valid_until=data.get("valid_until"),
            supplier_reference=data.get("supplier_reference", ""),
            freight_amount=data.get("freight_amount") or Decimal("0.000"),
            other_charges=data.get("other_charges") or Decimal("0.000"),
            evidence_reference=data.get("evidence_reference", ""),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is None:
            return reverse(self.success_url_name)
        return reverse("procurement:quotation_detail", args=[created.pk])


class SupplierQuotationDetailView(InventoryViewMixin, View):
    """The header, its priced lines, and the base unit price each implies."""

    module_key = "procurement"
    required_permission = VIEW_QUOTATION
    template_name = "procurement/quotation_detail.html"

    def load(self) -> Any:
        return resolve_quotation(self.actor, self.kwargs["pk"])

    def context(self, quotation: Any, form: Any) -> dict[str, Any]:
        may_manage = has_organization_master_data_permission(
            self.actor, MANAGE_QUOTATIONS, quotation.organization
        )
        return {
            "quotation": quotation,
            "lines": quotation.lines.select_related(
                "item", "item__base_unit", "package_unit"
            ).order_by("sequence"),
            "form": form,
            "page_title": quotation.number or _("مسودة عرض"),
            "may_edit": may_manage and quotation.is_editable,
            "may_manage": may_manage,
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        quotation = self.load()
        form = SupplierQuotationLineForm(actor=self.actor, quotation=quotation)
        return render(request, self.template_name, self.context(quotation, form))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        quotation = self.load()
        require_reachable_organization_permission(
            self.actor, MANAGE_QUOTATIONS, quotation.organization
        )
        form = SupplierQuotationLineForm(actor=self.actor, quotation=quotation, data=request.POST)
        if form.is_valid():
            try:
                add_quotation_line(
                    quotation=quotation,
                    item=form.cleaned_data["item"],
                    package_unit=form.cleaned_data.get("package_unit"),
                    quantity=form.cleaned_data["quantity"],
                    unit_price=form.cleaned_data["unit_price"],
                    note=form.cleaned_data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة السطر."))
                return HttpResponseRedirect(
                    reverse("procurement:quotation_detail", args=[quotation.pk])
                )
        return render(request, self.template_name, self.context(quotation, form))


class SupplierQuotationLineDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = MANAGE_QUOTATIONS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        quotation = resolve_quotation(self.actor, self.kwargs["pk"])
        require_reachable_organization_permission(
            self.actor, MANAGE_QUOTATIONS, quotation.organization
        )
        line = resolve_quotation_line(
            self.actor, quotation=quotation, line_id=self.kwargs["line_id"]
        )
        try:
            remove_quotation_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف السطر."))
        return HttpResponseRedirect(reverse("procurement:quotation_detail", args=[quotation.pk]))


class SupplierQuotationTransitionView(InventoryViewMixin, View):
    """Record the offer as received, or set it aside. POST-only."""

    module_key = "procurement"
    required_permission = MANAGE_QUOTATIONS
    #: "submit" or "decline".
    transition: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        quotation = resolve_quotation(self.actor, self.kwargs["pk"])
        require_reachable_organization_permission(
            self.actor, MANAGE_QUOTATIONS, quotation.organization
        )
        reason = request.POST.get("reason", "").strip()
        try:
            if self.transition == "submit":
                submit_supplier_quotation(quotation=quotation, actor=self.actor)
                messages.success(request, _("تم تسجيل استلام العرض."))
            else:
                decline_supplier_quotation(quotation=quotation, actor=self.actor, reason=reason)
                messages.success(request, _("تم استبعاد العرض."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        return HttpResponseRedirect(reverse("procurement:quotation_detail", args=[quotation.pk]))


# ---------------------------------------------------------------------------
# Comparison and award
# ---------------------------------------------------------------------------


class QuotationComparisonView(InventoryViewMixin, View):
    """
    Every offer against one request, normalised and ranked.

    Read-only. The award is a separate POST route, because a page that both
    showed a comparison and could award from a GET would be a page a link
    prefetch could commit the business to.
    """

    module_key = "procurement"
    required_permission = VIEW_QUOTATION
    template_name = "procurement/quotation_comparison.html"

    def load(self) -> Any:
        return resolve_purchase_request(self.actor, self.kwargs["pk"])

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = self.load()
        item_filter = request.GET.get("item", "").strip()
        rows = comparison_for_request(request=document)
        if item_filter:
            rows = [row for row in rows if row.item_code == item_filter]

        context = {
            "request_document": document,
            "rows": rows,
            "items": sorted({row.item_code for row in comparison_for_request(request=document)}),
            "selected_item": item_filter,
            "page_title": _("مقارنة العروض"),
            "may_award": has_organization_master_data_permission(
                self.actor, AWARD_QUOTATION, document.organization
            ),
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
            "is_awardable": document.status == PurchaseRequestStatus.APPROVED
            and document.awarded_quotation_id is None,
        }
        # The comparison table is the only thing that changes when the item
        # filter moves, so htmx swaps it alone and a full page is the fallback.
        template = (
            "procurement/_comparison_rows.html"
            if request.headers.get("HX-Request") == "true"
            else self.template_name
        )
        return render(request, template, context)


class QuotationAwardView(InventoryViewMixin, View):
    """POST-only. Awarding is a decision, never the side effect of a GET."""

    module_key = "procurement"
    required_permission = AWARD_QUOTATION

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        document = resolve_purchase_request(self.actor, self.kwargs["pk"])
        require_reachable_organization_permission(
            self.actor, AWARD_QUOTATION, document.organization
        )
        quotation = resolve_quotation(self.actor, int(request.POST.get("quotation", "0") or 0))
        try:
            award_quotation(
                request=document,
                quotation=quotation,
                actor=self.actor,
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم إرساء العرض."))
        return HttpResponseRedirect(reverse("procurement:quotation_comparison", args=[document.pk]))


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


class PurchaseOrderListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_PURCHASE_ORDER
    template_name = "procurement/purchase_order_list.html"
    context_object_name = "orders"
    page_title = _("أوامر الشراء")
    page_hint = _(
        "الالتزام التجاري. لا يزيد مخزوناً ولا يُنشئ ذمة في أي حالة — حتى بعد "
        "الإرسال للمورد. لا شيء مستحق قبل وصول البضاعة وفاتورة تذكر مبلغاً."
    )
    search_fields = ("number", "supplier__code", "supplier__name_ar", "supplier_reference")
    manage_permission = CREATE_PURCHASE_ORDER
    manage_scope = "branch"
    create_url_name = "procurement:purchase_order_create"
    create_label = _("أمر شراء جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_purchase_orders(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in PurchaseOrderStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = PurchaseOrderStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        return context


class PurchaseOrderCreateView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/purchase_order_form.html"
    form_class = PurchaseOrderForm
    required_permission = CREATE_PURCHASE_ORDER
    success_url_name = "procurement:purchase_order_list"
    page_title = _("أمر شراء جديد")
    page_hint = _(
        "يُفتح كمسودة بلا رقم. شروط السداد تُنسخ من المورد الآن ولا تُقرأ لاحقاً، "
        "حتى لا يتغيّر تاريخ الاستحقاق إذا أُعيد التفاوض عليها."
    )
    success_message = _("تم إنشاء المسودة. أضف الأصناف ثم اعتمدها.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(
            self.actor, CREATE_PURCHASE_ORDER, form.cleaned_data["warehouse"].branch
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        warehouse = data["warehouse"]
        self.created = create_purchase_order(
            supplier=data["supplier"],
            branch=warehouse.branch,
            warehouse=warehouse,
            location=data.get("location"),
            created_by=self.actor,
            ordered_on=data["ordered_on"],
            expected_on=data.get("expected_on"),
            request=data.get("request"),
            quotation=data.get("quotation"),
            supplier_reference=data.get("supplier_reference", ""),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is None:
            return reverse(self.success_url_name)
        return reverse("procurement:purchase_order_detail", args=[created.pk])


class PurchaseOrderDetailView(InventoryViewMixin, View):
    """The header, its agreed lines, and whatever may happen next."""

    module_key = "procurement"
    required_permission = VIEW_PURCHASE_ORDER
    template_name = "procurement/purchase_order_detail.html"

    def load(self) -> Any:
        return resolve_purchase_order(self.actor, self.kwargs["pk"])

    def context(self, order: Any, form: Any) -> dict[str, Any]:
        may_edit = has_branch_permission(self.actor, CREATE_PURCHASE_ORDER, order.branch)
        return {
            "order": order,
            "lines": order.lines.select_related("item", "item__base_unit", "package_unit").order_by(
                "sequence"
            ),
            "form": form,
            "page_title": order.number or _("مسودة أمر شراء"),
            "may_edit": may_edit and order.is_editable,
            "may_approve": has_branch_permission(self.actor, APPROVE_PURCHASE_ORDER, order.branch)
            and order.status == PurchaseOrderStatus.DRAFT,
            "may_issue": has_branch_permission(self.actor, ISSUE_PURCHASE_ORDER, order.branch)
            and order.status == PurchaseOrderStatus.APPROVED,
            "may_cancel": has_branch_permission(self.actor, CANCEL_PURCHASE_ORDER, order.branch)
            and order.status != PurchaseOrderStatus.CANCELLED,
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
            "may_revise": may_edit
            and order.status in {PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.ISSUED},
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = self.load()
        return render(
            request,
            self.template_name,
            self.context(order, PurchaseOrderLineForm(actor=self.actor, order=order)),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = self.load()
        require_branch_permission(self.actor, CREATE_PURCHASE_ORDER, order.branch)
        form = PurchaseOrderLineForm(actor=self.actor, order=order, data=request.POST)
        if form.is_valid():
            try:
                add_order_line(
                    order=order,
                    item=form.cleaned_data["item"],
                    package_unit=form.cleaned_data.get("package_unit"),
                    ordered_quantity=form.cleaned_data["ordered_quantity"],
                    unit_price=form.cleaned_data["unit_price"],
                    note=form.cleaned_data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة السطر."))
                return HttpResponseRedirect(
                    reverse("procurement:purchase_order_detail", args=[order.pk])
                )
        return render(request, self.template_name, self.context(order, form))


class PurchaseOrderLineDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = CREATE_PURCHASE_ORDER

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = resolve_purchase_order(self.actor, self.kwargs["pk"])
        require_branch_permission(self.actor, CREATE_PURCHASE_ORDER, order.branch)
        line = resolve_order_line(self.actor, order=order, line_id=self.kwargs["line_id"])
        try:
            remove_order_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف السطر."))
        return HttpResponseRedirect(reverse("procurement:purchase_order_detail", args=[order.pk]))


class PurchaseOrderTransitionView(InventoryViewMixin, View):
    """Approve, issue or cancel — one POST-only route each."""

    module_key = "procurement"
    required_permission = VIEW_PURCHASE_ORDER
    #: "approve", "issue" or "cancel".
    transition: str = ""

    NEEDED = {
        "approve": APPROVE_PURCHASE_ORDER,
        "issue": ISSUE_PURCHASE_ORDER,
        "cancel": CANCEL_PURCHASE_ORDER,
    }

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = resolve_purchase_order(self.actor, self.kwargs["pk"])
        require_branch_permission(self.actor, self.NEEDED[self.transition], order.branch)
        reason = request.POST.get("reason", "").strip()
        try:
            if self.transition == "approve":
                approve_purchase_order(order=order, actor=self.actor, reason=reason)
                messages.success(request, _("تم اعتماد أمر الشراء."))
            elif self.transition == "issue":
                issue_purchase_order(order=order, actor=self.actor)
                messages.success(request, _("تم إرسال الأمر للمورد."))
            else:
                cancel_purchase_order(order=order, actor=self.actor, reason=reason)
                messages.success(request, _("تم إلغاء الأمر."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        return HttpResponseRedirect(reverse("procurement:purchase_order_detail", args=[order.pk]))


class PurchaseOrderHistoryView(InventoryViewMixin, View):
    """Every version of one order, with what changed between each pair."""

    module_key = "procurement"
    required_permission = VIEW_PURCHASE_ORDER
    template_name = "procurement/purchase_order_history.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = resolve_purchase_order(self.actor, self.kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "order": order,
                "entries": order_version_history(self.actor, order=order),
                "page_title": _("سجل إصدارات أمر الشراء"),
                "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
            },
        )


class PurchaseOrderReviseView(InventoryViewMixin, View):
    """POST-only. A revision changes what a supplier was told."""

    module_key = "procurement"
    required_permission = CREATE_PURCHASE_ORDER

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = resolve_purchase_order(self.actor, self.kwargs["pk"])
        require_branch_permission(self.actor, CREATE_PURCHASE_ORDER, order.branch)

        quantities: dict[str, Decimal] = {}
        prices: dict[str, Decimal] = {}
        # `request.POST.items()` types values as `str | list`; `dict.items()`
        # on the underlying QueryDict gives the last value per key, which is
        # what a single form field means anyway.
        for key in request.POST:
            value = request.POST.get(key, "").strip()
            if not value:
                continue
            try:
                if key.startswith("quantity-"):
                    quantities[key.removeprefix("quantity-")] = Decimal(value)
                elif key.startswith("price-"):
                    prices[key.removeprefix("price-")] = Decimal(value)
            except InvalidOperation:
                messages.error(request, _("قيمة عددية غير صالحة."))
                return HttpResponseRedirect(
                    reverse("procurement:purchase_order_detail", args=[order.pk])
                )

        expected = request.POST.get("expected_on", "").strip()
        try:
            revise_purchase_order(
                order=order,
                actor=self.actor,
                reason=request.POST.get("reason", ""),
                expected_on=datetime.date.fromisoformat(expected) if expected else None,
                supplier_reference=request.POST.get("supplier_reference") or None,
                line_quantities=quantities or None,
                line_prices=prices or None,
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        except ValueError:
            messages.error(request, _("تاريخ غير صالح."))
        else:
            messages.success(request, _("تم إصدار نسخة جديدة من الأمر."))
        return HttpResponseRedirect(reverse("procurement:purchase_order_history", args=[order.pk]))


# ---------------------------------------------------------------------------
# Goods receipts
# ---------------------------------------------------------------------------


class GoodsReceiptListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_GOODS_RECEIPT
    template_name = "procurement/goods_receipt_list.html"
    context_object_name = "receipts"
    page_title = _("استلام البضاعة")
    page_hint = _(
        "ما وصل فعلاً، وما قرّره الفحص. الكمية المقبولة وحدها تدخل المخزون؛ "
        "المرفوضة تُسجَّل للمطالبة ولا ترحّل شيئاً. الترحيل يُنشئ الحركة "
        "المخزنية والقيد المحاسبي معاً في معاملة واحدة."
    )
    search_fields = ("number", "delivery_reference", "supplier__code", "supplier__name_ar")
    manage_permission = CREATE_GOODS_RECEIPT
    manage_scope = "branch"
    create_url_name = "procurement:goods_receipt_create"
    create_label = _("استلام جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_goods_receipts(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in GoodsReceiptStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = GoodsReceiptStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        return context


class GoodsReceiptCreateView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/goods_receipt_form.html"
    form_class = GoodsReceiptForm
    required_permission = CREATE_GOODS_RECEIPT
    success_url_name = "procurement:goods_receipt_list"
    page_title = _("استلام بضاعة جديد")
    page_hint = _("يُفتح كمسودة. لا يتحرك مخزون ولا يُنشأ قيد حتى الترحيل.")
    success_message = _("تم إنشاء المسودة. أضف الأصناف ثم افحصها.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_warehouse_permission(
            self.actor, CREATE_GOODS_RECEIPT, form.cleaned_data["warehouse"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        warehouse = data["warehouse"]
        self.created = create_goods_receipt(
            supplier=data["supplier"],
            branch=warehouse.branch,
            warehouse=warehouse,
            location=data.get("location"),
            created_by=self.actor,
            received_at=data["received_at"],
            order=data.get("order"),
            delivery_reference=data.get("delivery_reference", ""),
            evidence_reference=data.get("evidence_reference", ""),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is None:
            return reverse(self.success_url_name)
        return reverse("procurement:goods_receipt_detail", args=[created.pk])


class GoodsReceiptDetailView(InventoryViewMixin, View):
    """Header, delivered lines, inspection state and what is still outstanding."""

    module_key = "procurement"
    required_permission = VIEW_GOODS_RECEIPT
    template_name = "procurement/goods_receipt_detail.html"

    def load(self) -> Any:
        return resolve_goods_receipt(self.actor, self.kwargs["pk"])

    def context(self, receipt: Any, form: Any) -> dict[str, Any]:
        may_edit = has_warehouse_permission(self.actor, CREATE_GOODS_RECEIPT, receipt.warehouse)
        return {
            "receipt": receipt,
            "lines": receipt.lines.select_related(
                "item",
                "item__base_unit",
                "package_unit",
                "lot",
                "rejection_reason",
                "movement",
                "inventory_account",
                "contra_account",
                "journal_line",
            ).order_by("sequence"),
            "form": form,
            "page_title": receipt.number or _("مسودة استلام"),
            "may_edit": may_edit and receipt.is_editable,
            "may_inspect": has_warehouse_permission(
                self.actor, INSPECT_GOODS_RECEIPT, receipt.warehouse
            )
            and receipt.is_editable,
            # The button appears only where the act is actually available:
            # the permission, the warehouse, and the receipt's own state all
            # have to agree. The service re-checks every one of them.
            "may_post": has_warehouse_permission(self.actor, POST_GOODS_RECEIPT, receipt.warehouse)
            and receipt.is_ready_to_post,
            "may_reverse": has_warehouse_permission(
                self.actor, REVERSE_GOODS_RECEIPT, receipt.warehouse
            )
            and receipt.status == GoodsReceiptStatus.POSTED,
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
            "outstanding": outstanding_order_lines(receipt.order) if receipt.order else [],
            "timeline": receipt_timeline(receipt),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = self.load()
        return render(
            request,
            self.template_name,
            self.context(receipt, GoodsReceiptLineForm(actor=self.actor, receipt=receipt)),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = self.load()
        require_warehouse_permission(self.actor, CREATE_GOODS_RECEIPT, receipt.warehouse)
        form = GoodsReceiptLineForm(actor=self.actor, receipt=receipt, data=request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_receipt_line(
                    receipt=receipt,
                    item=data["item"],
                    package_unit=data.get("package_unit"),
                    delivered_quantity=data["delivered_quantity"],
                    measured_base_quantity=data.get("measured_base_quantity"),
                    unit_price=data.get("unit_price"),
                    lot=data.get("lot"),
                    expiry_date=data.get("expiry_date"),
                    note=data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة السطر."))
                return HttpResponseRedirect(
                    reverse("procurement:goods_receipt_detail", args=[receipt.pk])
                )
        return render(request, self.template_name, self.context(receipt, form))


class GoodsReceiptLineDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = CREATE_GOODS_RECEIPT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = resolve_goods_receipt(self.actor, self.kwargs["pk"])
        require_warehouse_permission(self.actor, CREATE_GOODS_RECEIPT, receipt.warehouse)
        line = resolve_receipt_line(self.actor, receipt=receipt, line_id=self.kwargs["line_id"])
        try:
            remove_receipt_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف السطر."))
        return HttpResponseRedirect(reverse("procurement:goods_receipt_detail", args=[receipt.pk]))


class GoodsReceiptInspectView(InventoryViewMixin, View):
    """POST-only. Accepting goods is a decision about what enters stock."""

    module_key = "procurement"
    required_permission = INSPECT_GOODS_RECEIPT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = resolve_goods_receipt(self.actor, self.kwargs["pk"])
        require_warehouse_permission(self.actor, INSPECT_GOODS_RECEIPT, receipt.warehouse)
        line = resolve_receipt_line(self.actor, receipt=receipt, line_id=self.kwargs["line_id"])
        form = InspectLineForm(actor=self.actor, receipt=receipt, data=request.POST)
        if not form.is_valid():
            messages.error(request, _("كمية مقبولة غير صالحة."))
        else:
            try:
                inspect_receipt_line(
                    line=line,
                    accepted_base_quantity=form.cleaned_data["accepted_base_quantity"],
                    actor=self.actor,
                    rejection_reason=form.cleaned_data.get("rejection_reason"),
                    note=form.cleaned_data.get("note", ""),
                )
            except ValidationError as error:
                messages.error(request, "؛ ".join(str(m) for m in error.messages))
            else:
                messages.success(request, _("تم تسجيل نتيجة الفحص."))
        return HttpResponseRedirect(reverse("procurement:goods_receipt_detail", args=[receipt.pk]))


class GoodsReceiptPostView(InventoryViewMixin, View):
    """
    POST-only. This is the act that puts goods into stock and money into the
    ledger, and a GET that did it would be a link a crawler could follow.
    """

    module_key = "procurement"
    required_permission = POST_GOODS_RECEIPT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = resolve_goods_receipt(self.actor, self.kwargs["pk"])
        require_warehouse_permission(self.actor, POST_GOODS_RECEIPT, receipt.warehouse)
        try:
            posted = post_goods_receipt(receipt=receipt, actor=self.actor)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(
                request,
                _("تم ترحيل الاستلام %(number)s. دخل المخزون وأُنشئ القيد معاً.")
                % {"number": posted.number},
            )
        return HttpResponseRedirect(reverse("procurement:goods_receipt_detail", args=[receipt.pk]))


class GoodsReceiptReverseView(InventoryViewMixin, View):
    """POST-only, and a reason is required — an unexplained reversal is a hole."""

    module_key = "procurement"
    required_permission = REVERSE_GOODS_RECEIPT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        receipt = resolve_goods_receipt(self.actor, self.kwargs["pk"])
        require_warehouse_permission(self.actor, REVERSE_GOODS_RECEIPT, receipt.warehouse)
        try:
            reverse_goods_receipt(
                receipt=receipt,
                actor=self.actor,
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم عكس الاستلام. خرج المخزون وعُكس القيد معاً."))
        return HttpResponseRedirect(reverse("procurement:goods_receipt_detail", args=[receipt.pk]))


# ---------------------------------------------------------------------------
# Supplier invoices (Task 2.10)
# ---------------------------------------------------------------------------


class SupplierInvoiceListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_INVOICE
    template_name = "procurement/supplier_invoice_list.html"
    context_object_name = "invoices"
    page_title = _("فواتير الموردين")
    page_hint = _(
        "ما يقوله المورد إنه مستحق. الفاتورة لا تحرّك مخزوناً إطلاقاً؛ تُنشئ ذمة "
        "دائنة فقط. أسطر البضاعة تنتظر المطابقة الثلاثية قبل الترحيل، وأسطر "
        "المصروف المباشر تُرحّل فوراً."
    )
    search_fields = ("number", "supplier_invoice_number", "supplier__code", "supplier__name_ar")
    manage_permission = CREATE_SUPPLIER_INVOICE
    manage_scope = "organization"
    create_url_name = "procurement:supplier_invoice_create"
    create_label = _("فاتورة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_supplier_invoices(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in SupplierInvoiceStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = SupplierInvoiceStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        return context


class SupplierInvoiceCreateView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/supplier_invoice_form.html"
    form_class = SupplierInvoiceForm
    required_permission = CREATE_SUPPLIER_INVOICE
    success_url_name = "procurement:supplier_invoice_list"
    page_title = _("فاتورة مورد جديدة")
    page_hint = _("تُفتح كمسودة. لا ذمة ولا قيد حتى الاعتماد ثم الترحيل.")
    success_message = _("تم إنشاء المسودة. أضف الأسطر ثم اعتمدها.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, CREATE_SUPPLIER_INVOICE, form.cleaned_data["branch"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = create_supplier_invoice(
            supplier=data["supplier"],
            branch=data["branch"],
            created_by=self.actor,
            supplier_invoice_number=data["supplier_invoice_number"],
            invoice_date=data["invoice_date"],
            business_date=data.get("business_date"),
            supplier_reference=data.get("supplier_reference", ""),
            freight_amount=data.get("freight_amount"),
            discount_amount=data.get("discount_amount"),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is None:
            return reverse(self.success_url_name)
        return reverse("procurement:supplier_invoice_detail", args=[created.pk])


class SupplierInvoiceDetailView(InventoryViewMixin, View):
    """Header, lines, what each line costs, and what still blocks posting."""

    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_INVOICE
    template_name = "procurement/supplier_invoice_detail.html"

    def load(self) -> Any:
        return resolve_supplier_invoice(self.actor, self.kwargs["pk"])

    def context(
        self, invoice: Any, item_form: Any = None, account_form: Any = None
    ) -> dict[str, Any]:
        may_edit = has_organization_permission(
            self.actor, CREATE_SUPPLIER_INVOICE, invoice.organization
        )
        return {
            # One hook, set once here, so all three render paths on this view —
            # the GET, the inventory-line POST and the account-line POST — answer
            # an HTMX request with a fragment rather than a nested document.
            "form_base_template": (
                "settings/_form_fragment.html" if self.is_htmx() else "shell.html"
            ),
            "invoice": invoice,
            "lines": invoice.lines.select_related(
                "item", "account", "cost_center", "receipt_line", "receipt_line__receipt"
            ).order_by("sequence"),
            "item_form": item_form or InvoiceInventoryLineForm(actor=self.actor, invoice=invoice),
            "account_form": account_form
            or InvoiceAccountLineForm(actor=self.actor, invoice=invoice),
            "page_title": invoice.number or invoice.supplier_invoice_number,
            "may_edit": may_edit and invoice.is_editable,
            "may_approve": has_organization_permission(
                self.actor, APPROVE_SUPPLIER_INVOICE, invoice.organization
            )
            and invoice.status == SupplierInvoiceStatus.DRAFT,
            "may_post": has_organization_permission(
                self.actor, POST_SUPPLIER_INVOICE, invoice.organization
            )
            and invoice.is_ready_to_post,
            "may_reverse": has_organization_permission(
                self.actor, REVERSE_SUPPLIER_INVOICE, invoice.organization
            )
            and invoice.status == SupplierInvoiceStatus.POSTED,
            "blocking": invoice.blocking_lines,
            "timeline": invoice_timeline(invoice),
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
            "outstanding": outstanding_amount(invoice),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, self.template_name, self.context(self.load()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        invoice = self.load()
        require_organization_permission(self.actor, CREATE_SUPPLIER_INVOICE, invoice.organization)
        if request.POST.get("line_type", "") == "ACCOUNT":
            return self._add_account_line(request, invoice)
        return self._add_inventory_line(request, invoice)

    def _add_inventory_line(self, request: HttpRequest, invoice: Any) -> HttpResponse:
        form = InvoiceInventoryLineForm(actor=self.actor, invoice=invoice, data=request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_inventory_line(
                    invoice=invoice,
                    item=data["item"],
                    base_quantity=data["base_quantity"],
                    unit_price=data["unit_price"],
                    receipt_line=data.get("receipt_line"),
                    description=data.get("description", ""),
                    note=data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة سطر البضاعة."))
                return HttpResponseRedirect(
                    reverse("procurement:supplier_invoice_detail", args=[invoice.pk])
                )
        return render(request, self.template_name, self.context(invoice, item_form=form))

    def _add_account_line(self, request: HttpRequest, invoice: Any) -> HttpResponse:
        form = InvoiceAccountLineForm(actor=self.actor, invoice=invoice, data=request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_account_line(
                    invoice=invoice,
                    account=data["account"],
                    cost_center=data.get("cost_center"),
                    description=data["description"],
                    quantity=data["quantity"],
                    unit_price=data["unit_price"],
                    note=data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة سطر المصروف."))
                return HttpResponseRedirect(
                    reverse("procurement:supplier_invoice_detail", args=[invoice.pk])
                )
        return render(request, self.template_name, self.context(invoice, account_form=form))


class SupplierInvoiceLineDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = CREATE_SUPPLIER_INVOICE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        invoice = resolve_supplier_invoice(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, CREATE_SUPPLIER_INVOICE, invoice.organization)
        line = resolve_invoice_line(self.actor, invoice=invoice, line_id=self.kwargs["line_id"])
        try:
            remove_invoice_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف السطر."))
        return HttpResponseRedirect(
            reverse("procurement:supplier_invoice_detail", args=[invoice.pk])
        )


class SupplierInvoiceTransitionView(InventoryViewMixin, View):
    """
    POST-only: approve, return to draft, post, reverse.

    One view rather than four, because they differ only in the permission they
    demand and the service they call — and a shared shape is what keeps the
    authorization identical across all of them.
    """

    module_key = "procurement"
    transition = "approve"

    PERMISSIONS = {
        "approve": APPROVE_SUPPLIER_INVOICE,
        "return_to_draft": APPROVE_SUPPLIER_INVOICE,
        "post": POST_SUPPLIER_INVOICE,
        "reverse": REVERSE_SUPPLIER_INVOICE,
    }

    @property
    def required_permission(self) -> str:  # type: ignore[override]
        return self.PERMISSIONS[self.transition]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        invoice = resolve_supplier_invoice(self.actor, self.kwargs["pk"])
        require_organization_permission(
            self.actor, self.PERMISSIONS[self.transition], invoice.organization
        )
        reason = request.POST.get("reason", "")
        try:
            self._apply(request, invoice, reason)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        return HttpResponseRedirect(
            reverse("procurement:supplier_invoice_detail", args=[invoice.pk])
        )

    def _apply(self, request: HttpRequest, invoice: Any, reason: str) -> None:
        if self.transition == "approve":
            approve_supplier_invoice(invoice=invoice, actor=self.actor)
            messages.success(request, _("تم اعتماد الفاتورة."))
        elif self.transition == "return_to_draft":
            return_supplier_invoice_to_draft(invoice=invoice, actor=self.actor, reason=reason)
            messages.success(request, _("أُعيدت الفاتورة إلى المسودة."))
        elif self.transition == "post":
            posted = post_supplier_invoice(invoice=invoice, actor=self.actor)
            messages.success(
                request,
                _("تم ترحيل الفاتورة %(number)s وأُنشئت ذمة المورد.") % {"number": posted.number},
            )
        else:
            reverse_supplier_invoice(invoice=invoice, actor=self.actor, reason=reason)
            messages.success(request, _("تم عكس الفاتورة وعُكست الذمة معها."))


# ---------------------------------------------------------------------------
# Three-way matching (Task 2.11)
# ---------------------------------------------------------------------------


class PurchaseMatchListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_PURCHASE_MATCH
    template_name = "procurement/purchase_match_list.html"
    context_object_name = "matches"
    page_title = _("مطابقة المشتريات")
    page_hint = _(
        "ما الذي يُغطّي ماذا: أي جزء من أي تسليم يقابل أي جزء من أي فاتورة، وما الفرق "
        "بينهما. المطابقة لا تُنشئ قيداً ولا تُسوّي ذمة ولا تُحرّك مخزوناً؛ الترحيل "
        "المالي يأتي في مهمة لاحقة."
    )
    search_fields = (
        "number",
        "supplier__code",
        "supplier__name_ar",
        "supplier_invoice__supplier_invoice_number",
    )
    manage_permission = MATCH_SUPPLIER_INVOICE
    manage_scope = "organization"

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_purchase_matches(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in PurchaseMatchStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = PurchaseMatchStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        for row in context.get("matches", []):
            row.live_posting = live_posting_for(row)
        return context


class MatchingQueueView(InventoryViewMixin, View):
    """
    What still needs reconciling, in the two directions it can be missing.

    A delivery nobody has billed and an invoice nobody has covered are
    different problems, so the queue keeps them in separate columns rather
    than merging them into one count that answers neither question.
    """

    module_key = "procurement"
    required_permission = VIEW_PURCHASE_MATCH
    template_name = "procurement/matching_queue.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        queue = matching_queue(self.actor)
        receipts = next((row for row in queue if row["kind"] == "RECEIPT"), {})
        invoices = next((row for row in queue if row["kind"] == "INVOICE"), {})
        return render(
            request,
            self.template_name,
            {
                "page_title": _("قائمة المطابقة"),
                "receipt_lines": receipts.get("receipt_lines", []),
                "invoice_lines": invoices.get("invoice_lines", []),
                "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
                "may_match": has_organization_permission(
                    self.actor, MATCH_SUPPLIER_INVOICE, self.actor_organization()
                )
                if self.actor_organization()
                else False,
            },
        )

    def actor_organization(self) -> Any:
        """Any organization the caller may view matches in, for the create gate."""
        return organizations_with_permission(self.actor, VIEW_PURCHASE_MATCH).first()


class PurchaseMatchCreateView(InventoryViewMixin, View):
    """POST-only. Opens a draft match against one approved supplier invoice."""

    module_key = "procurement"
    required_permission = MATCH_SUPPLIER_INVOICE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        invoice = resolve_supplier_invoice(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, MATCH_SUPPLIER_INVOICE, invoice.organization)
        try:
            match = create_purchase_match(invoice=invoice, created_by=self.actor)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
            return HttpResponseRedirect(
                reverse("procurement:supplier_invoice_detail", args=[invoice.pk])
            )
        messages.success(request, _("فُتحت مسودة مطابقة."))
        return HttpResponseRedirect(reverse("procurement:purchase_match_detail", args=[match.pk]))


class PurchaseMatchDetailView(InventoryViewMixin, View):
    """Allocations, coverage, variance, and what may still be done."""

    module_key = "procurement"
    required_permission = VIEW_PURCHASE_MATCH
    template_name = "procurement/purchase_match_detail.html"

    def load(self) -> Any:
        return resolve_purchase_match(self.actor, self.kwargs["pk"])

    def context(self, match: Any, form: Any = None) -> dict[str, Any]:
        invoice = match.supplier_invoice
        may_edit = (
            has_organization_permission(self.actor, MATCH_SUPPLIER_INVOICE, match.organization)
            and match.is_editable
        )
        return {
            "match": match,
            "invoice": invoice,
            "allocations": match.allocations.select_related(
                "supplier_invoice_line",
                "supplier_invoice_line__item",
                "goods_receipt_line",
                "goods_receipt_line__receipt",
                "purchase_order_line",
                "purchase_order_line__order",
            ).order_by("sequence"),
            "coverage": coverage_for_invoice(invoice),
            # Derived, never stored: whether a generation currently stands on
            # this match. `None` after a reversal, because the generation is
            # history and history holds nothing.
            "posting": live_posting_for(match),
            "form": form or MatchAllocationForm(actor=self.actor, match=match),
            "page_title": match.number or _("مسودة مطابقة"),
            "may_edit": may_edit,
            "may_ready": may_edit and match.allocations.exists(),
            "may_cancel": has_organization_permission(
                self.actor, CANCEL_PURCHASE_MATCH, match.organization
            )
            and match.status != PurchaseMatchStatus.CANCELLED,
            # Price and variance are money. Omitted, not blanked, without the
            # cost permission — a blanked column says a number exists and you
            # are not trusted with it, which is a different statement.
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, self.template_name, self.context(self.load()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        match = self.load()
        require_organization_permission(self.actor, MATCH_SUPPLIER_INVOICE, match.organization)
        form = MatchAllocationForm(actor=self.actor, match=match, data=request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_allocation(
                    match=match,
                    invoice_line=data["invoice_line"],
                    receipt_line=data["receipt_line"],
                    matched_base_quantity=data["matched_base_quantity"],
                    created_by=self.actor,
                    note=data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة التخصيص."))
                return HttpResponseRedirect(
                    reverse("procurement:purchase_match_detail", args=[match.pk])
                )
        return render(request, self.template_name, self.context(match, form=form))


class MatchAllocationDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = MATCH_SUPPLIER_INVOICE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        match = resolve_purchase_match(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, MATCH_SUPPLIER_INVOICE, match.organization)
        allocation = resolve_match_allocation(
            self.actor, match=match, allocation_id=self.kwargs["allocation_id"]
        )
        try:
            remove_allocation(allocation=allocation)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف التخصيص."))
        return HttpResponseRedirect(reverse("procurement:purchase_match_detail", args=[match.pk]))


class PurchaseMatchTransitionView(InventoryViewMixin, View):
    """POST-only: freeze the evidence, or withdraw it."""

    module_key = "procurement"
    transition = "ready"

    PERMISSIONS = {
        "ready": MATCH_SUPPLIER_INVOICE,
        "cancel": CANCEL_PURCHASE_MATCH,
    }

    @property
    def required_permission(self) -> str:  # type: ignore[override]
        permission: str = self.PERMISSIONS[self.transition]
        return permission

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        match = resolve_purchase_match(self.actor, self.kwargs["pk"])
        require_organization_permission(
            self.actor, self.PERMISSIONS[self.transition], match.organization
        )
        try:
            if self.transition == "ready":
                mark_match_ready(match=match, actor=self.actor)
                messages.success(
                    request,
                    _(
                        "المطابقة جاهزة. الأدلة مُجمّدة — ولم يُرحّل أي مبلغ بعد: "
                        "الترحيل المالي مهمة لاحقة."
                    ),
                )
            else:
                cancel_purchase_match(
                    match=match, actor=self.actor, reason=request.POST.get("reason", "")
                )
                messages.success(request, _("أُلغيت المطابقة وأُفرجت عن الكميات."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        return HttpResponseRedirect(reverse("procurement:purchase_match_detail", args=[match.pk]))


# ---------------------------------------------------------------------------
# Supplier returns (Task 2.13)
# ---------------------------------------------------------------------------


class SupplierReturnListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_RETURN
    template_name = "procurement/supplier_return_list.html"
    context_object_name = "supplier_returns"
    page_title = _("مرتجعات الموردين")
    page_hint = _(
        "بضاعة تعود إلى موردها. الإرجاع يُخرج المخزون بالمتوسط المتحرك القائم "
        "ويقيّد المطالبة في حساب تسوية المرتجعات — لا ذمة تُمسّ ولا فرق يُسجَّل "
        "حتى يصل إشعار الدائن."
    )
    search_fields = (
        "number",
        "evidence_reference",
        "supplier__code",
        "supplier__name_ar",
        "receipt__number",
    )
    manage_permission = CREATE_SUPPLIER_RETURN
    manage_scope = "branch"
    create_url_name = "procurement:supplier_return_create"
    create_label = _("مرتجع جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_supplier_returns(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in SupplierReturnStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = SupplierReturnStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        return context


class SupplierReturnCreateView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/supplier_return_form.html"
    form_class = SupplierReturnForm
    required_permission = CREATE_SUPPLIER_RETURN
    success_url_name = "procurement:supplier_return_list"
    page_title = _("مرتجع مورد جديد")
    page_hint = _("يُفتح كمسودة على تسليم مرحّل. لا يتحرك مخزون ولا يُنشأ قيد حتى الترحيل.")
    success_message = _("تم إنشاء المسودة. أضف الأسطر ثم رحّلها.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_warehouse_permission(
            self.actor, CREATE_SUPPLIER_RETURN, form.cleaned_data["receipt"].warehouse
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = create_supplier_return(
            receipt=data["receipt"],
            created_by=self.actor,
            returned_at=data["returned_at"],
            reason_code=data.get("reason_code"),
            reason=data.get("reason", ""),
            evidence_reference=data.get("evidence_reference", ""),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is None:
            return reverse(self.success_url_name)
        return reverse("procurement:supplier_return_detail", args=[created.pk])


class SupplierReturnDetailView(InventoryViewMixin, View):
    """Header, lines, and what the cited delivery still has left to send back."""

    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_RETURN
    template_name = "procurement/supplier_return_detail.html"

    def load(self) -> Any:
        return resolve_supplier_return(self.actor, self.kwargs["pk"])

    def context(self, supplier_return: Any, form: Any) -> dict[str, Any]:
        may_edit = has_warehouse_permission(
            self.actor, CREATE_SUPPLIER_RETURN, supplier_return.warehouse
        )
        return {
            "supplier_return": supplier_return,
            "lines": supplier_return.lines.select_related(
                "item",
                "item__base_unit",
                "lot",
                "goods_receipt_line",
                "movement",
                "inventory_account",
                "contra_account",
            ).order_by("sequence"),
            "availability": returnable_receipt_lines(supplier_return),
            "form": form,
            "page_title": supplier_return.number or _("مسودة مرتجع"),
            "may_edit": may_edit and supplier_return.is_editable,
            # The button appears only where the act is actually available: the
            # permission, the warehouse and the document's own state all have
            # to agree. The service re-checks every one of them.
            "may_post": has_warehouse_permission(
                self.actor, POST_SUPPLIER_RETURN, supplier_return.warehouse
            )
            and supplier_return.status == SupplierReturnStatus.DRAFT,
            "may_reverse": has_warehouse_permission(
                self.actor, REVERSE_SUPPLIER_RETURN, supplier_return.warehouse
            )
            and supplier_return.status == SupplierReturnStatus.POSTED,
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
            "timeline": return_timeline(supplier_return),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        supplier_return = self.load()
        return render(
            request,
            self.template_name,
            self.context(
                supplier_return,
                SupplierReturnLineForm(actor=self.actor, supplier_return=supplier_return),
            ),
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        supplier_return = self.load()
        require_warehouse_permission(self.actor, CREATE_SUPPLIER_RETURN, supplier_return.warehouse)
        form = SupplierReturnLineForm(
            actor=self.actor, supplier_return=supplier_return, data=request.POST
        )
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_return_line(
                    supplier_return=supplier_return,
                    receipt_line=data["receipt_line"],
                    returned_base_quantity=data["returned_base_quantity"],
                    expected_credit_value=data.get("expected_credit_value"),
                    note=data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة السطر."))
                return HttpResponseRedirect(
                    reverse("procurement:supplier_return_detail", args=[supplier_return.pk])
                )
        return render(request, self.template_name, self.context(supplier_return, form))


class SupplierReturnLineDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = CREATE_SUPPLIER_RETURN

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        supplier_return = resolve_supplier_return(self.actor, self.kwargs["pk"])
        require_warehouse_permission(self.actor, CREATE_SUPPLIER_RETURN, supplier_return.warehouse)
        line = resolve_return_line(
            self.actor, supplier_return=supplier_return, line_id=self.kwargs["line_id"]
        )
        try:
            remove_return_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف السطر."))
        return HttpResponseRedirect(
            reverse("procurement:supplier_return_detail", args=[supplier_return.pk])
        )


class SupplierReturnTransitionView(InventoryViewMixin, View):
    """
    POST-only: post, or reverse. This is the act that takes goods out of stock
    and money out of the inventory account, and a GET that did it would be a
    link a crawler could follow.
    """

    module_key = "procurement"
    transition = "post"

    PERMISSIONS = {
        "post": POST_SUPPLIER_RETURN,
        "reverse": REVERSE_SUPPLIER_RETURN,
    }

    @property
    def required_permission(self) -> str:  # type: ignore[override]
        permission: str = self.PERMISSIONS[self.transition]
        return permission

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        supplier_return = resolve_supplier_return(self.actor, self.kwargs["pk"])
        require_warehouse_permission(
            self.actor, self.PERMISSIONS[self.transition], supplier_return.warehouse
        )
        try:
            if self.transition == "post":
                posted = post_supplier_return(supplier_return=supplier_return, actor=self.actor)
                messages.success(
                    request,
                    _("تم ترحيل المرتجع %(number)s. خرج المخزون وقُيّدت المطالبة معاً.")
                    % {"number": posted.number},
                )
            else:
                reverse_supplier_return(
                    supplier_return=supplier_return,
                    actor=self.actor,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(request, _("تم عكس المرتجع. عادت البضاعة وعاد الحساب معاً."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        return HttpResponseRedirect(
            reverse("procurement:supplier_return_detail", args=[supplier_return.pk])
        )


# ---------------------------------------------------------------------------
# Supplier credit notes (Task 2.14)
# ---------------------------------------------------------------------------


class SupplierCreditNoteListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_CREDIT_NOTE
    template_name = "procurement/supplier_credit_note_list.html"
    context_object_name = "credit_notes"
    page_title = _("إشعارات الموردين الدائنة")
    page_hint = _(
        "جواب المورد على المرتجع. الترحيل يقفل مطالبة المرتجع بقيمتها الدفترية، "
        "يخفّض ذمة المورد بالمبلغ المعتمد، ويعترف بالفرق في حساب فروقات "
        "المرتجعات — لا يتحرك مخزون إطلاقاً."
    )
    search_fields = (
        "number",
        "supplier_document_number",
        "supplier__code",
        "supplier__name_ar",
        "supplier_return__number",
    )
    manage_permission = CREATE_SUPPLIER_CREDIT_NOTE
    manage_scope = "organization"
    create_url_name = "procurement:supplier_credit_note_create"
    create_label = _("إشعار جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_supplier_credit_notes(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in SupplierCreditNoteStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = SupplierCreditNoteStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        return context


class SupplierCreditNoteCreateView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/supplier_credit_note_form.html"
    form_class = SupplierCreditNoteForm
    required_permission = CREATE_SUPPLIER_CREDIT_NOTE
    success_url_name = "procurement:supplier_credit_note_list"
    page_title = _("إشعار دائن جديد")
    page_hint = _("يُفتح كمسودة على مرتجع مرحّل. لا قيد حتى الترحيل.")
    success_message = _("تم إنشاء المسودة. خصّصها على الفواتير إن شئت ثم رحّلها.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor,
            CREATE_SUPPLIER_CREDIT_NOTE,
            form.cleaned_data["supplier_return"].organization,
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = create_supplier_credit_note(
            supplier_return=data["supplier_return"],
            created_by=self.actor,
            supplier_document_number=data["supplier_document_number"],
            credit_date=data["credit_date"],
            amount=data["amount"],
            reason=data.get("reason", ""),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is None:
            return reverse(self.success_url_name)
        return reverse("procurement:supplier_credit_note_detail", args=[created.pk])


class SupplierCreditNoteDetailView(InventoryViewMixin, View):
    """Header, the claim it settles, allocations, and the figures side by side."""

    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_CREDIT_NOTE
    template_name = "procurement/supplier_credit_note_detail.html"

    def load(self) -> Any:
        return resolve_supplier_credit_note(self.actor, self.kwargs["pk"])

    def context(
        self, credit_note: Any, form: Any = None, return_form: Any = None
    ) -> dict[str, Any]:
        may_edit = has_organization_permission(
            self.actor, CREATE_SUPPLIER_CREDIT_NOTE, credit_note.organization
        )
        settlement_rows = [
            {
                "allocation": row,
                "line": row.supplier_return_line,
                "remaining_quantity": remaining_credit_quantity(row.supplier_return_line),
                "remaining_value": remaining_book_value(row.supplier_return_line),
            }
            for row in credit_note.return_allocations.select_related(
                "supplier_return_line", "supplier_return_line__item"
            ).order_by("sequence")
        ]
        return {
            "credit_note": credit_note,
            "settlements": settlement_rows,
            "allocations": credit_note.allocations.select_related("invoice").order_by("sequence"),
            "form": form or CreditAllocationForm(actor=self.actor, credit_note=credit_note),
            "return_form": return_form
            or CreditReturnAllocationForm(actor=self.actor, credit_note=credit_note),
            "page_title": credit_note.number or _("مسودة إشعار"),
            "book_value": credit_note.supplier_return.posted_value,
            "unallocated": unallocated_credit(credit_note),
            "may_edit": may_edit and credit_note.is_editable,
            "may_post": has_organization_permission(
                self.actor, POST_SUPPLIER_CREDIT_NOTE, credit_note.organization
            )
            and credit_note.status == SupplierCreditNoteStatus.DRAFT,
            "may_reverse": has_organization_permission(
                self.actor, REVERSE_SUPPLIER_CREDIT_NOTE, credit_note.organization
            )
            and credit_note.status == SupplierCreditNoteStatus.POSTED,
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
            "timeline": note_timeline(credit_note),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, self.template_name, self.context(self.load()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        credit_note = self.load()
        require_organization_permission(
            self.actor, CREATE_SUPPLIER_CREDIT_NOTE, credit_note.organization
        )
        if request.POST.get("allocation_type", "") == "RETURN":
            return self._add_return_allocation(request, credit_note)
        return self._add_invoice_allocation(request, credit_note)

    def _add_return_allocation(self, request: HttpRequest, credit_note: Any) -> HttpResponse:
        form = CreditReturnAllocationForm(
            actor=self.actor, credit_note=credit_note, data=request.POST
        )
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_return_allocation(
                    credit_note=credit_note,
                    return_line=data["return_line"],
                    credited_base_quantity=data["credited_base_quantity"],
                    allocated_credit_amount=data["allocated_credit_amount"],
                    note=data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة التسوية."))
                return HttpResponseRedirect(
                    reverse("procurement:supplier_credit_note_detail", args=[credit_note.pk])
                )
        return render(request, self.template_name, self.context(credit_note, return_form=form))

    def _add_invoice_allocation(self, request: HttpRequest, credit_note: Any) -> HttpResponse:
        form = CreditAllocationForm(actor=self.actor, credit_note=credit_note, data=request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_credit_allocation(
                    credit_note=credit_note,
                    invoice=data["invoice"],
                    allocated_amount=data["allocated_amount"],
                    note=data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة التخصيص."))
                return HttpResponseRedirect(
                    reverse("procurement:supplier_credit_note_detail", args=[credit_note.pk])
                )
        return render(request, self.template_name, self.context(credit_note, form=form))


class SupplierPaymentListView(InventoryListView):
    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_PAYMENT
    template_name = "procurement/supplier_payment_list.html"
    context_object_name = "payments"
    page_title = _("دفعات الموردين")
    page_hint = _(
        "المال الخارج للموردين. المخصص على الفواتير يخفض الذمة، والباقي يقف "
        "سلفة للمورد — أصلاً لا ذمة سالبة — حتى يُستهلك لاحقاً."
    )
    search_fields = ("number", "reference", "supplier__code", "supplier__name_ar")
    manage_permission = CREATE_SUPPLIER_PAYMENT
    manage_scope = "organization"
    create_url_name = "procurement:supplier_payment_create"
    create_label = _("دفعة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_supplier_payments(self.actor)
        status = self.request.GET.get("status", "").strip().upper()
        if status in SupplierPaymentStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = SupplierPaymentStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["may_see_cost"] = self.actor.has_perm(VIEW_SUPPLIER_COST)
        return context


class SupplierPaymentCreateView(InventoryWriteView):
    module_key = "procurement"
    template_name = "procurement/supplier_payment_form.html"
    form_class = SupplierPaymentForm
    required_permission = CREATE_SUPPLIER_PAYMENT
    success_url_name = "procurement:supplier_payment_list"
    page_title = _("دفعة مورد جديدة")
    page_hint = _("تُفتح كمسودة. لا يخرج مال حتى الترحيل.")
    success_message = _("تم إنشاء المسودة. خصّصها على الفواتير ثم رحّلها.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, CREATE_SUPPLIER_PAYMENT, form.cleaned_data["branch"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = create_supplier_payment(
            supplier=data["supplier"],
            branch=data["branch"],
            created_by=self.actor,
            paid_at=data["paid_at"],
            method=data["method"],
            amount=data["amount"],
            reference=data.get("reference", ""),
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is None:
            return reverse(self.success_url_name)
        return reverse("procurement:supplier_payment_detail", args=[created.pk])


class SupplierPaymentDetailView(InventoryViewMixin, View):
    """Header, allocations, and what stands as an advance."""

    module_key = "procurement"
    required_permission = VIEW_SUPPLIER_PAYMENT
    template_name = "procurement/supplier_payment_detail.html"

    def load(self) -> Any:
        return resolve_supplier_payment(self.actor, self.kwargs["pk"])

    def context(self, payment: Any, form: Any = None) -> dict[str, Any]:
        may_edit = has_organization_permission(
            self.actor, CREATE_SUPPLIER_PAYMENT, payment.organization
        )
        return {
            "payment": payment,
            "allocations": payment.allocations.select_related("invoice").order_by("sequence"),
            "form": form or PaymentAllocationForm(actor=self.actor, payment=payment),
            "page_title": payment.number or _("مسودة دفعة"),
            "allocated": payment_allocated_total(payment),
            "advance": advance_remainder(payment),
            "may_edit": may_edit and payment.is_editable,
            "may_post": has_organization_permission(
                self.actor, POST_SUPPLIER_PAYMENT, payment.organization
            )
            and payment.status == SupplierPaymentStatus.DRAFT,
            "may_reverse": has_organization_permission(
                self.actor, REVERSE_SUPPLIER_PAYMENT, payment.organization
            )
            and payment.status == SupplierPaymentStatus.POSTED,
            "may_see_cost": self.actor.has_perm(VIEW_SUPPLIER_COST),
            "timeline": payment_timeline(payment),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, self.template_name, self.context(self.load()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payment = self.load()
        require_organization_permission(self.actor, CREATE_SUPPLIER_PAYMENT, payment.organization)
        form = PaymentAllocationForm(actor=self.actor, payment=payment, data=request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_payment_allocation(
                    payment=payment,
                    invoice=data["invoice"],
                    allocated_amount=data["allocated_amount"],
                    note=data.get("note", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة التخصيص."))
                return HttpResponseRedirect(
                    reverse("procurement:supplier_payment_detail", args=[payment.pk])
                )
        return render(request, self.template_name, self.context(payment, form))


class PaymentAllocationDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = CREATE_SUPPLIER_PAYMENT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payment = resolve_supplier_payment(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, CREATE_SUPPLIER_PAYMENT, payment.organization)
        allocation = resolve_payment_allocation(
            self.actor, payment=payment, allocation_id=self.kwargs["allocation_id"]
        )
        try:
            remove_payment_allocation(allocation=allocation)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف التخصيص."))
        return HttpResponseRedirect(
            reverse("procurement:supplier_payment_detail", args=[payment.pk])
        )


class SupplierPaymentTransitionView(InventoryViewMixin, View):
    """POST-only: post, or reverse. Money leaves; a GET must not send it."""

    module_key = "procurement"
    transition = "post"

    PERMISSIONS = {
        "post": POST_SUPPLIER_PAYMENT,
        "reverse": REVERSE_SUPPLIER_PAYMENT,
    }

    @property
    def required_permission(self) -> str:  # type: ignore[override]
        permission: str = self.PERMISSIONS[self.transition]
        return permission

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payment = resolve_supplier_payment(self.actor, self.kwargs["pk"])
        require_organization_permission(
            self.actor, self.PERMISSIONS[self.transition], payment.organization
        )
        try:
            if self.transition == "post":
                posted = post_supplier_payment(payment=payment, actor=self.actor)
                messages.success(
                    request,
                    _("تم ترحيل الدفعة %(number)s. خرج المال وخُفّضت الذمة بالمخصص.")
                    % {"number": posted.number},
                )
            else:
                reverse_supplier_payment(
                    payment=payment,
                    actor=self.actor,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(request, _("تم عكس الدفعة. عادت الذمة والسلفة كما كانتا."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        return HttpResponseRedirect(
            reverse("procurement:supplier_payment_detail", args=[payment.pk])
        )


class CreditReturnAllocationDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = CREATE_SUPPLIER_CREDIT_NOTE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        credit_note = resolve_supplier_credit_note(self.actor, self.kwargs["pk"])
        require_organization_permission(
            self.actor, CREATE_SUPPLIER_CREDIT_NOTE, credit_note.organization
        )
        allocation = resolve_credit_return_allocation(
            self.actor, credit_note=credit_note, allocation_id=self.kwargs["allocation_id"]
        )
        try:
            remove_return_allocation(allocation=allocation)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف التسوية."))
        return HttpResponseRedirect(
            reverse("procurement:supplier_credit_note_detail", args=[credit_note.pk])
        )


class CreditAllocationDeleteView(InventoryViewMixin, View):
    module_key = "procurement"
    required_permission = CREATE_SUPPLIER_CREDIT_NOTE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        credit_note = resolve_supplier_credit_note(self.actor, self.kwargs["pk"])
        require_organization_permission(
            self.actor, CREATE_SUPPLIER_CREDIT_NOTE, credit_note.organization
        )
        allocation = resolve_credit_allocation(
            self.actor, credit_note=credit_note, allocation_id=self.kwargs["allocation_id"]
        )
        try:
            remove_credit_allocation(allocation=allocation)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف التخصيص."))
        return HttpResponseRedirect(
            reverse("procurement:supplier_credit_note_detail", args=[credit_note.pk])
        )


class SupplierCreditNoteTransitionView(InventoryViewMixin, View):
    """POST-only: post, or reverse. Money moves; a GET must not."""

    module_key = "procurement"
    transition = "post"

    PERMISSIONS = {
        "post": POST_SUPPLIER_CREDIT_NOTE,
        "reverse": REVERSE_SUPPLIER_CREDIT_NOTE,
    }

    @property
    def required_permission(self) -> str:  # type: ignore[override]
        permission: str = self.PERMISSIONS[self.transition]
        return permission

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        credit_note = resolve_supplier_credit_note(self.actor, self.kwargs["pk"])
        require_organization_permission(
            self.actor, self.PERMISSIONS[self.transition], credit_note.organization
        )
        try:
            if self.transition == "post":
                posted = post_supplier_credit_note(credit_note=credit_note, actor=self.actor)
                messages.success(
                    request,
                    _("تم ترحيل الإشعار %(number)s. أُقفلت المطالبة وخُفّضت الذمة معاً.")
                    % {"number": posted.number},
                )
            else:
                reverse_supplier_credit_note(
                    credit_note=credit_note,
                    actor=self.actor,
                    reason=request.POST.get("reason", ""),
                )
                messages.success(request, _("تم عكس الإشعار. عادت المطالبة قائمة كما كانت."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        return HttpResponseRedirect(
            reverse("procurement:supplier_credit_note_detail", args=[credit_note.pk])
        )
