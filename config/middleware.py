"""Project middleware."""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import translation


class ExplicitLocaleMiddleware:
    """
    Activate the language the user chose, or the site default.

    Django's LocaleMiddleware falls back to the browser's Accept-Language
    header. That is wrong for this system: the interface is Arabic and
    right-to-left, and a manager whose Windows is set to English would
    otherwise be served Arabic text inside a left-to-right layout — icons and
    labels on the wrong side of every field.

    Language therefore changes only by explicit choice, stored in the language
    cookie. The browser does not get a vote.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        language = self._language_for(request)
        translation.activate(language)
        request.LANGUAGE_CODE = translation.get_language()
        try:
            response = self.get_response(request)
        finally:
            translation.deactivate()
        response.setdefault("Content-Language", language)
        return response

    @staticmethod
    def _language_for(request: HttpRequest) -> str:
        supported = {code for code, _ in settings.LANGUAGES}
        chosen = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if chosen in supported:
            return chosen
        return settings.LANGUAGE_CODE
