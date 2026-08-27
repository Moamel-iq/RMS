"""Unit of measure settings screens."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import CreateView, UpdateView

from apps.core.views import FoundationFormViewMixin, FoundationListView
from apps.units.forms import UnitCreateForm, UnitUpdateForm
from apps.units.models import Dimension, UnitOfMeasure
from apps.units.services import create_unit, update_unit


class UnitListView(FoundationListView):
    model = UnitOfMeasure
    template_name = "settings/unit_list.html"
    context_object_name = "units"
    page_title = _("وحدات القياس")
    page_hint = _(
        "الوحدات التي معاملها ثابت بالتعريف. الكرتونة والكيس تعبئة خاصة بالصنف "
        "وتأتي مع المخزون في المرحلة ١."
    )
    create_url_name = "units:unit_create"
    create_label = _("وحدة جديدة")
    search_fields = ("code", "name_ar", "name_en")
    superuser_only = True

    def get_queryset(self) -> QuerySet[UnitOfMeasure]:
        queryset = super().get_queryset()
        dimension = self.request.GET.get("dimension", "").strip()
        if dimension in Dimension.values:
            queryset = queryset.filter(dimension=dimension)
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["dimensions"] = Dimension.choices
        context["selected_dimension"] = self.request.GET.get("dimension", "")
        return context


class UnitCreateView(FoundationFormViewMixin, CreateView):
    model = UnitOfMeasure
    form_class = UnitCreateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("units:unit_list")
    superuser_only = True

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("وحدة قياس جديدة")
        context["page_hint"] = _(
            "الوحدات التي معاملها ثابت بالتعريف فقط. الكرتونة والكيس تعبئة خاصة بالصنف "
            "وتأتي مع المخزون."
        )
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: UnitCreateForm) -> HttpResponse:
        create_unit(
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            dimension=form.cleaned_data["dimension"],
            factor_to_base=form.cleaned_data["factor_to_base"],
        )
        return HttpResponseRedirect(self.get_success_url())


class UnitUpdateView(FoundationFormViewMixin, UpdateView):
    model = UnitOfMeasure
    form_class = UnitUpdateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("units:unit_list")
    superuser_only = True

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("تعديل الوحدة") + f" — {self.object.code}"
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: UnitUpdateForm) -> HttpResponse:
        update_unit(
            unit=self.object,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            factor_to_base=form.cleaned_data.get("factor_to_base", self.object.factor_to_base),
            is_active=form.cleaned_data["is_active"],
        )
        return HttpResponseRedirect(self.get_success_url())
