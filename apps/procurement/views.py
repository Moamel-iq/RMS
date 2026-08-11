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

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.inventory.views import InventoryActionView, InventoryListView, InventoryWriteView
from apps.organizations.authorization import require_reachable_organization_permission
from apps.procurement.forms import SupplierForm
from apps.procurement.permissions import MANAGE_SUPPLIERS, VIEW_SUPPLIER
from apps.procurement.selectors import resolve_supplier, visible_suppliers
from apps.procurement.services import create_supplier, update_supplier


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
