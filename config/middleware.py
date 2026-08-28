"""Project middleware."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

from django.conf import settings
from django.db import connections
from django.http import HttpRequest, HttpResponse
from django.utils import translation
from django.utils.cache import patch_vary_headers

logger = logging.getLogger("khan_mandi.performance")


class _DatabaseTimingRecorder:
    """Count database calls and timings without retaining SQL or parameters."""

    def __init__(self, *, slow_query_ms: int) -> None:
        self.slow_query_ms = slow_query_ms
        self.query_count = 0
        self.slow_query_count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0

    def __call__(
        self,
        execute: Callable[..., Any],
        sql: str,
        params: Any,
        many: bool,
        context: dict[str, Any],
    ) -> Any:
        # The arguments are passed through exactly once and are never retained
        # or logged: they can contain salaries, supplier prices, or tokens.
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            self.query_count += 1
            self.total_ms += duration_ms
            self.max_ms = max(self.max_ms, duration_ms)
            if duration_ms >= self.slow_query_ms:
                self.slow_query_count += 1


class RequestPerformanceMiddleware:
    """Emit safe request and database timing metrics.

    The log identifies the named Django route, never the raw path, query
    string, SQL, parameters, request body, user, or response body. This keeps
    the timing useful in production without copying financial or HR data into
    the platform log stream.
    """

    #: Platform health probes are frequent and add no useful page-latency
    #: signal. They are still logged when slow, failed, or backed by a slow DB
    #: call.
    QUIET_ROUTES = frozenset({"healthz", "api_v1:health"})

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not getattr(settings, "PERFORMANCE_MONITORING_ENABLED", True):
            return self.get_response(request)

        slow_query_ms = int(getattr(settings, "PERFORMANCE_SLOW_DB_QUERY_MS", 100))
        recorder = _DatabaseTimingRecorder(slow_query_ms=max(slow_query_ms, 0))
        started = time.perf_counter()
        response: HttpResponse | None = None
        try:
            # `execute_wrapper` observes timings without Django's DEBUG query
            # capture, which stores complete SQL and bound parameters.
            with ExitStack() as stack:
                for database in connections.all():
                    stack.enter_context(database.execute_wrapper(recorder))
                response = self.get_response(request)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            self._record(
                request=request,
                response=response,
                duration_ms=duration_ms,
                database=recorder,
            )
        if response is None:  # pragma: no cover - Django's contract forbids it
            raise RuntimeError("The downstream middleware returned no response.")
        return response

    @staticmethod
    def _route_name(request: HttpRequest) -> str:
        match = getattr(request, "resolver_match", None)
        name = getattr(match, "view_name", "") if match is not None else ""
        return str(name or "unresolved")

    @staticmethod
    def _correlation_id(response: HttpResponse | None) -> str:
        if response is None:
            return "-"
        supplied = response.get("X-Correlation-ID", "")
        try:
            return str(uuid.UUID(supplied))
        except ValueError, AttributeError, TypeError:
            return "-"

    def _record(
        self,
        *,
        request: HttpRequest,
        response: HttpResponse | None,
        duration_ms: float,
        database: _DatabaseTimingRecorder,
    ) -> None:
        route = self._route_name(request)
        status = response.status_code if response is not None else 500
        known_methods = {"GET", "POST", "PUT", "PATCH", "DELETE"}
        method = request.method if request.method in known_methods else "OTHER"
        slow_request_ms = int(getattr(settings, "PERFORMANCE_SLOW_REQUEST_MS", 500))
        is_slow = duration_ms >= max(slow_request_ms, 0) or database.slow_query_count > 0
        is_failure = status >= 500
        log_all = bool(getattr(settings, "PERFORMANCE_LOG_ALL_REQUESTS", True))
        if not (is_slow or is_failure or (log_all and route not in self.QUIET_ROUTES)):
            return

        level = logging.WARNING if is_slow or is_failure else logging.INFO
        htmx = request.headers.get("HX-Request") == "true"
        correlation_id = self._correlation_id(response)
        # The formatted message is deliberately self-contained because the
        # default console formatter does not serialize `extra` fields.
        logger.log(
            level,
            "request_performance method=%s route=%s status=%d duration_ms=%.2f "
            "db_ms=%.2f db_queries=%d slow_db_queries=%d max_db_ms=%.2f "
            "htmx=%s correlation_id=%s",
            method,
            route,
            status,
            duration_ms,
            database.total_ms,
            database.query_count,
            database.slow_query_count,
            database.max_ms,
            str(htmx).lower(),
            correlation_id,
            extra={
                "http_method": method,
                "route_name": route,
                "status_code": status,
                "duration_ms": duration_ms,
                "db_duration_ms": database.total_ms,
                "db_query_count": database.query_count,
                "slow_db_query_count": database.slow_query_count,
                "max_db_query_ms": database.max_ms,
                "is_htmx": htmx,
                "correlation_id": correlation_id,
            },
        )


class HtmxVaryMiddleware:
    """Keep full-page and HTMX fragments distinct in every HTTP cache.

    Several views choose a smaller template from ``HX-Request`` and, during
    the gradual navigation migration, from ``HX-Target``.  The Vary header
    must be present on the ordinary response as well as the fragment response;
    adding it only when the request is HTMX still lets a cache serve a stored
    full document into a partial target.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        content_type = response.get("Content-Type", "")
        if content_type.startswith("text/html"):
            patch_vary_headers(response, ("HX-Request", "HX-Target"))
        return response


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
