from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.db.models import Prefetch, QuerySet
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from apps.core.models import AuditAction
from apps.core.services import record_audit_event
from apps.organizations.authorization import organizations_with_permission
from apps.supplier_quotes.forms import (
    SupplierQuoteAttachmentForm,
    SupplierQuoteForm,
    SupplierQuoteLineForm,
)
from apps.supplier_quotes.models import (
    SupplierQuote,
    SupplierQuoteAttachment,
    SupplierQuoteLine,
)
from apps.supplier_quotes.permissions import ADD, CHANGE, DELETE, DOWNLOAD, VIEW
from apps.supplier_quotes.services import (
    add_line,
    remove_attachment,
    remove_line,
    replace_attachment,
    update_line,
)
from apps.users.models import User


def _actor(request: HttpRequest) -> User:
    """LoginRequiredMixin has already refused anonymous callers."""
    user: User = request.user  # type: ignore[assignment]
    return user


class QuoteView(LoginRequiredMixin, PermissionRequiredMixin, View):
    raise_exception = True
    permission_required = VIEW
    template_name = ""

    def render(self, request: HttpRequest, context: dict[str, Any]) -> HttpResponse:
        return render(request, self.template_name, context)

    def quote(self, request: HttpRequest, pk: int) -> SupplierQuote:
        orgs = organizations_with_permission(_actor(request), VIEW)
        return get_object_or_404(SupplierQuote.objects.filter(organization__in=orgs), pk=pk)


class SupplierQuoteRawMediaBlockView(View):
    """Prevent the DEBUG media helper from exposing supplier quote files."""

    def get(self, request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        raise Http404


class QuoteListView(QuoteView):
    template_name = "supplier_quotes/list.html"

    def get_queryset(self, request: HttpRequest) -> QuerySet[SupplierQuote]:
        rows = SupplierQuote.objects.filter(
            organization__in=organizations_with_permission(_actor(request), VIEW)
        )
        query = request.GET.get("q", "").strip()
        date_from = request.GET.get("date_from", "")
        date_to = request.GET.get("date_to", "")
        if query:
            rows = rows.filter(supplier_name__icontains=query) | rows.filter(phone__icontains=query)
        if date_from:
            rows = rows.filter(quote_date__gte=date_from)
        if date_to:
            rows = rows.filter(quote_date__lte=date_to)
        return rows.prefetch_related(
            Prefetch(
                "lines",
                queryset=SupplierQuoteLine.objects.only("quote_id", "line_total"),
            ),
            Prefetch(
                "attachments",
                queryset=SupplierQuoteAttachment.objects.only("id", "quote_id"),
            ),
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        query, date_from, date_to = (
            request.GET.get("q", "").strip(),
            request.GET.get("date_from", ""),
            request.GET.get("date_to", ""),
        )
        rows = self.get_queryset(request)
        return self.render(
            request, {"quotes": rows, "q": query, "date_from": date_from, "date_to": date_to}
        )


class QuoteCreateView(QuoteView):
    permission_required = ADD
    template_name = "supplier_quotes/form.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return self.render(
            request,
            {
                "form": SupplierQuoteForm(actor=_actor(request)),
                "attachment_form": SupplierQuoteAttachmentForm(),
                "page_title": "إضافة عرض مورد",
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        form, attachment_form = (
            SupplierQuoteForm(request.POST, actor=_actor(request)),
            SupplierQuoteAttachmentForm(request.POST, request.FILES),
        )
        has_file = bool(request.FILES.get("file"))
        if form.is_valid() and (not has_file or attachment_form.is_valid()):
            quote = form.save(commit=False)
            quote.organization, quote.created_by = (
                form.cleaned_data["organization"],
                _actor(request),
            )
            quote.save()
            if has_file:
                replace_attachment(
                    quote=quote,
                    uploaded=attachment_form.cleaned_data["file"],
                    actor=_actor(request),
                )
            return HttpResponseRedirect(reverse("supplier_quotes:detail", args=[quote.pk]))
        return self.render(
            request,
            {"form": form, "attachment_form": attachment_form, "page_title": "إضافة عرض مورد"},
        )


class QuoteDetailView(QuoteView):
    template_name = "supplier_quotes/detail.html"

    def detail_quote(self, request: HttpRequest, pk: int) -> SupplierQuote:
        orgs = organizations_with_permission(_actor(request), VIEW)
        rows = SupplierQuote.objects.filter(organization__in=orgs).prefetch_related(
            Prefetch(
                "lines",
                queryset=SupplierQuoteLine.objects.select_related("item", "unit"),
            ),
            "attachments",
        )
        return get_object_or_404(rows, pk=pk)

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        quote = self.detail_quote(request, pk)
        return self.render(
            request,
            {
                "quote": quote,
                "line_form": SupplierQuoteLineForm(quote=quote),
                "attachment_form": SupplierQuoteAttachmentForm(),
                "can_change": request.user.has_perm(CHANGE),
                "can_delete": request.user.has_perm(DELETE),
                "can_download": request.user.has_perm(DOWNLOAD),
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        if not request.user.has_perm(CHANGE):
            self.handle_no_permission()
        form = SupplierQuoteLineForm(request.POST, quote=quote)
        if form.is_valid():
            add_line(quote=quote, data=form.cleaned_data)
        return HttpResponseRedirect(reverse("supplier_quotes:detail", args=[pk]))


class QuoteEditView(QuoteView):
    permission_required = CHANGE
    template_name = "supplier_quotes/form.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        return self.render(
            request,
            {
                "form": SupplierQuoteForm(instance=quote, actor=_actor(request)),
                "attachment_form": SupplierQuoteAttachmentForm(),
                "quote": quote,
                "page_title": "تعديل عرض المورد",
            },
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        form, attachment_form = (
            SupplierQuoteForm(request.POST, instance=quote, actor=_actor(request)),
            SupplierQuoteAttachmentForm(request.POST, request.FILES),
        )
        has_file = bool(request.FILES.get("file"))
        if form.is_valid() and (not has_file or attachment_form.is_valid()):
            form.save()
            if has_file:
                replace_attachment(
                    quote=quote,
                    uploaded=attachment_form.cleaned_data["file"],
                    actor=_actor(request),
                )
            return HttpResponseRedirect(reverse("supplier_quotes:detail", args=[pk]))
        return self.render(
            request,
            {
                "form": form,
                "attachment_form": attachment_form,
                "quote": quote,
                "page_title": "تعديل عرض المورد",
            },
        )


class QuoteLineDeleteView(QuoteView):
    permission_required = CHANGE

    def post(self, request: HttpRequest, pk: int, line_pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        remove_line(line=get_object_or_404(quote.lines, pk=line_pk))
        return HttpResponseRedirect(reverse("supplier_quotes:detail", args=[pk]))


class QuoteLineEditView(QuoteView):
    permission_required = CHANGE
    template_name = "supplier_quotes/line_form.html"

    def get(self, request: HttpRequest, pk: int, line_pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        line = get_object_or_404(quote.lines, pk=line_pk)
        return self.render(
            request,
            {
                "quote": quote,
                "line": line,
                "form": SupplierQuoteLineForm(instance=line, quote=quote),
            },
        )

    def post(self, request: HttpRequest, pk: int, line_pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        line = get_object_or_404(quote.lines, pk=line_pk)
        form = SupplierQuoteLineForm(request.POST, instance=line, quote=quote)
        if form.is_valid():
            update_line(line=line, data=form.cleaned_data)
            return HttpResponseRedirect(reverse("supplier_quotes:detail", args=[pk]))
        return self.render(request, {"quote": quote, "line": line, "form": form})


class QuoteAttachmentUploadView(QuoteView):
    permission_required = CHANGE

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        form = SupplierQuoteAttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            replace_attachment(
                quote=quote, uploaded=form.cleaned_data["file"], actor=_actor(request)
            )
            messages.success(request, "تم رفع المرفق واستبدال المرفق السابق إن وُجد.")
        else:
            messages.error(request, form.errors.as_text())
        return HttpResponseRedirect(reverse("supplier_quotes:detail", args=[pk]))


class QuoteAttachmentDeleteView(QuoteView):
    permission_required = CHANGE

    def post(self, request: HttpRequest, pk: int, attachment_pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        remove_attachment(attachment=get_object_or_404(quote.attachments, pk=attachment_pk))
        return HttpResponseRedirect(reverse("supplier_quotes:detail", args=[pk]))


class QuoteAttachmentDownloadView(QuoteView):
    permission_required = DOWNLOAD

    def get(self, request: HttpRequest, pk: int, attachment_pk: int) -> FileResponse:
        quote = self.quote(request, pk)
        attachment = get_object_or_404(quote.attachments, pk=attachment_pk)
        record_audit_event(
            action=AuditAction.DOCUMENT_DOWNLOADED,
            target=attachment,
            metadata={"quote_id": quote.pk},
        )
        return FileResponse(
            attachment.file.open("rb"), as_attachment=True, filename=attachment.original_name
        )


class QuoteDeleteView(QuoteView):
    permission_required = DELETE

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        quote = self.quote(request, pk)
        for attachment in quote.attachments.all():
            remove_attachment(attachment=attachment)
        quote.delete()
        messages.success(request, "تم حذف عرض المورد.")
        return HttpResponseRedirect(reverse("supplier_quotes:list"))
