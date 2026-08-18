"""
The import screens: history, upload, preview, apply.

Three views. The list is the audit trail, gated on `view_import_history`; the
detail screen is the preview and the two commands that act on it, each gated on
the permission for the *kind* being imported rather than on one blanket import
right — reshaping the item master and preparing an opening draft are different
authorities and §G keeps them apart.

Authorization is permission **plus** membership in the batch's own
organization, resolved through the same selector the rest of inventory uses. A
global Django permission reaches nothing on its own.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from apps.inventory import imports
from apps.inventory.models import (
    OPENING_IMPORT_KINDS,
    ImportBatch,
    ImportBatchStatus,
    ImportKind,
)
from apps.inventory.permissions import (
    IMPORT_MASTER_DATA,
    IMPORT_OPENING_DRAFT,
    VIEW_IMPORT_HISTORY,
)
from apps.inventory.views import InventoryViewMixin
from apps.organizations.authorization import (
    require_organization_permission,
    resolve_organization,
)
from apps.organizations.selectors import accessible_organizations

#: kind -> the permission it needs, for kinds another module registered.
#: Filled the same way `VALIDATORS` is — from the owning module at app
#: ready — so an unregistered kind still falls back to this module's own
#: master-data permission and can never arrive permissionless.
KIND_PERMISSIONS: dict[str, str] = {}


def permission_for_kind(kind: str) -> str:
    """Which right this kind needs. Data, so the two cannot drift apart."""
    registered = KIND_PERMISSIONS.get(kind)
    if registered is not None:
        return registered
    return IMPORT_OPENING_DRAFT if kind in OPENING_IMPORT_KINDS else IMPORT_MASTER_DATA


class ImportBatchListView(InventoryViewMixin, View):
    """
    What has been uploaded, by whom, and what it changed.

    Gated on `view_import_history` alone: an accountant who may apply nothing
    still has to be able to see what was applied, which is the point of the
    permission being separate.
    """

    required_permission = VIEW_IMPORT_HISTORY
    template_name = "inventory/import_list.html"
    paginate_by = 50

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batches = (
            ImportBatch.objects.filter(organization__in=accessible_organizations(self.actor))
            .select_related("organization", "branch", "uploaded_by", "applied_by")
            .order_by("-created_at")
        )
        status = request.GET.get("status", "").strip()
        if status:
            batches = batches.filter(status=status)
        search = request.GET.get("q", "").strip()
        if search:
            batches = batches.filter(original_filename__icontains=search)

        paginator = Paginator(batches, self.paginate_by)
        page = paginator.get_page(request.GET.get("page"))
        return render(
            request,
            self.template_name,
            {
                "page_title": _("سجل الاستيراد"),
                "page_hint": _(
                    "كل ملف رُفع، وحكمه، وما غيّره. الرفع والتدقيق لا يكتبان شيئاً — "
                    "التطبيق وحده يكتب، وكاملاً أو لا شيء."
                ),
                "batches": page.object_list,
                "page_obj": page,
                "is_paginated": page.has_other_pages(),
                "paginator": paginator,
                "statuses": ImportBatchStatus.choices,
                "selected_status": status,
                "search": search,
                "kinds": [
                    (kind, label)
                    for kind, label in ImportKind.choices
                    if kind in imports.VALIDATORS
                ],
                "htmx_list": True,
                "inventory_ui": True,
                "list_base_template": (
                    "settings/_list_fragment.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
            },
        )


class ImportBatchDetailView(InventoryViewMixin, View):
    """
    One batch: its verdict, its rows, and the commands that act on it.

    The row table is the preview. It shows valid and invalid rows together
    because knowing *which* row is wrong is what makes it fixable, and it is
    rendered from stored verdicts rather than by re-judging on each render —
    a preview that recomputed could disagree with the apply that follows it.
    """

    required_permission = VIEW_IMPORT_HISTORY
    template_name = "inventory/import_detail.html"

    def batch(self, pk: int) -> ImportBatch:
        return get_object_or_404(
            ImportBatch.objects.filter(
                organization__in=accessible_organizations(self.actor)
            ).select_related("organization", "branch", "uploaded_by", "applied_by"),
            pk=pk,
        )

    def get(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = self.batch(pk)
        rows = batch.rows.order_by("row_number")
        only = request.GET.get("rows", "")
        if only == "errors":
            rows = rows.filter(is_valid=False)
        elif only == "valid":
            rows = rows.filter(is_valid=True)

        may_act = request.user.has_perm(permission_for_kind(batch.kind))
        return render(
            request,
            self.template_name,
            {
                "batch": batch,
                "rows": rows,
                "row_filter": only,
                "page_title": _("دفعة استيراد"),
                "may_act": may_act,
                "can_validate": may_act and not batch.is_terminal,
                "can_apply": may_act and batch.status == ImportBatchStatus.VALIDATED,
                "can_cancel": may_act and not batch.is_terminal,
                "list_base_template": (
                    "settings/_list_fragment.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
            },
        )

    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = self.batch(pk)
        # Permission **and** membership in this batch's organization. A hidden
        # button is not a control; this is the control.
        require_organization_permission(
            self.actor, permission_for_kind(batch.kind), batch.organization
        )
        action = request.POST.get("action", "")
        try:
            if action == "validate":
                imports.validate_batch(batch=batch)
                messages.success(request, _("تم التدقيق."))
            elif action == "apply":
                applied = imports.apply_batch(batch=batch)
                messages.success(
                    request,
                    _("تم التطبيق: %(count)s سطر مُغيَّر.") % {"count": applied.applied_row_count},
                )
            elif action == "cancel":
                imports.cancel_batch(batch=batch, reason=request.POST.get("reason", ""))
                messages.success(request, _("أُلغيت الدفعة."))
            else:
                messages.error(request, _("أمر غير معروف."))
        except ValidationError as problem:
            messages.error(request, "؛ ".join(problem.messages))
        return redirect(reverse("inventory:import_detail", args=[batch.pk]))


class ImportUploadView(InventoryViewMixin, View):
    """
    Upload and parse. Writes the batch and its rows, and nothing else.

    The kind decides the permission, so the form cannot be used to reach an
    authority the caller does not hold by picking a different dropdown value.
    """

    required_permission = VIEW_IMPORT_HISTORY
    template_name = "inventory/import_upload.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "page_title": _("استيراد جديد"),
                "organizations": accessible_organizations(self.actor),
                "kinds": [
                    (kind, label)
                    for kind, label in ImportKind.choices
                    if kind in imports.VALIDATORS
                ],
                "max_bytes": imports.MAX_UPLOAD_BYTES,
                "allowed": ", ".join(imports.ALLOWED_EXTENSIONS),
            },
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        kind = request.POST.get("kind", "")
        if kind not in imports.VALIDATORS:
            messages.error(request, _("نوع الاستيراد غير مدعوم."))
            return redirect(reverse("inventory:import_upload"))

        organization = resolve_organization(
            self.actor, int(request.POST.get("organization", 0) or 0)
        )
        require_organization_permission(self.actor, permission_for_kind(kind), organization)

        branch = None
        branch_id = request.POST.get("branch", "")
        if branch_id.isdigit():
            branch = organization.branches.filter(pk=int(branch_id)).first()

        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, _("اختر ملفاً."))
            return redirect(reverse("inventory:import_upload"))
        if upload.size is None or upload.name is None:
            messages.error(request, _("الملف غير صالح."))
            return redirect(reverse("inventory:import_upload"))
        if upload.size > imports.MAX_UPLOAD_BYTES:
            # Refused before the bytes are read into memory.
            messages.error(request, _("حجم الملف يتجاوز الحد المسموح."))
            return redirect(reverse("inventory:import_upload"))

        try:
            batch = imports.create_batch(
                organization=organization,
                branch=branch,
                kind=kind,
                raw=upload.read(),
                filename=upload.name,
            )
            imports.validate_batch(batch=batch)
        except ValidationError as problem:
            messages.error(request, "؛ ".join(problem.messages))
            return redirect(reverse("inventory:import_upload"))
        return redirect(reverse("inventory:import_detail", args=[batch.pk]))
