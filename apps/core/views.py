"""Shared view helpers."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse


class ModuleViewMixin:
    """
    Declares which module a view belongs to, so the rail highlights it.

    Set on the request rather than passed through context, because the shell
    context processor runs for every template — including ones rendered
    outside a view that knows about modules.
    """

    module_key: str = "home"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        request.active_module = self.module_key  # type: ignore[attr-defined]
        response: HttpResponse = super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
        return response
