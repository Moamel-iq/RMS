"""Shared HTTP performance, compression, and HTMX cache contracts."""

from __future__ import annotations

import gzip
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import path

from config.middleware import HtmxVaryMiddleware, RequestPerformanceMiddleware


def _large_page(_request: HttpRequest) -> HttpResponse:
    return HttpResponse("<main>" + ("safe-compressible-content " * 80) + "</main>")


urlpatterns = [
    path("large-page/", _large_page, name="large_page"),
]


class _FakeDatabase:
    """Expose Django's execute-wrapper contract without opening PostgreSQL."""

    wrapper: Any = None

    @contextmanager
    def execute_wrapper(self, wrapper: Any) -> Any:
        self.wrapper = wrapper
        try:
            yield
        finally:
            self.wrapper = None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"HX-Request": "true", "HX-Target": "main-content"},
    ],
)
def test_html_cache_always_varies_by_htmx_headers(headers: dict[str, str]) -> None:
    request = RequestFactory().get("/screen/", headers=headers)
    middleware = HtmxVaryMiddleware(
        lambda _request: HttpResponse("<main>screen</main>", headers={"Vary": "Accept-Language"})
    )

    response = middleware(request)

    vary = {value.strip() for value in response["Vary"].split(",")}
    assert vary == {"Accept-Language", "HX-Request", "HX-Target"}


def test_non_html_response_does_not_gain_irrelevant_htmx_variation() -> None:
    request = RequestFactory().get("/api/")
    middleware = HtmxVaryMiddleware(lambda _request: JsonResponse({"status": "ok"}))

    response = middleware(request)

    assert "Vary" not in response


@override_settings(
    PERFORMANCE_MONITORING_ENABLED=True,
    PERFORMANCE_LOG_ALL_REQUESTS=True,
    PERFORMANCE_SLOW_REQUEST_MS=0,
    PERFORMANCE_SLOW_DB_QUERY_MS=0,
)
def test_performance_log_contains_timings_but_no_sql_or_request_data(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_marker = "PRIVATE-SUPPLIER-PRICE-918273"
    request = RequestFactory().get(
        "/performance-probe/",
        {"marker": sensitive_marker},
        headers={"HX-Request": "true"},
    )
    request.resolver_match = cast(Any, SimpleNamespace(view_name="performance_probe"))
    database = _FakeDatabase()
    monkeypatch.setattr("config.middleware.connections.all", lambda: [database])

    def view(_request: HttpRequest) -> HttpResponse:
        database.wrapper(
            lambda sql, params, many, context: "ok",
            "SELECT %s::text",
            [sensitive_marker],
            False,
            {},
        )
        return HttpResponse(
            "<main>ok</main>",
            headers={"X-Correlation-ID": sensitive_marker},
        )

    middleware = RequestPerformanceMiddleware(view)
    with caplog.at_level(logging.WARNING, logger="khan_mandi.performance"):
        response = middleware(request)

    assert response.status_code == 200
    records = [record for record in caplog.records if record.name == "khan_mandi.performance"]
    assert len(records) == 1
    record = records[0]
    rendered = record.getMessage()
    assert "route=performance_probe" in rendered
    assert "htmx=true" in rendered
    assert record.__dict__["db_query_count"] >= 1
    assert record.__dict__["slow_db_query_count"] >= 1
    assert sensitive_marker not in rendered
    assert sensitive_marker not in record.__dict__.values()
    assert "SELECT" not in rendered.upper()
    assert not hasattr(record, "sql")
    assert not hasattr(record, "params")


@override_settings(
    ROOT_URLCONF=__name__,
    PERFORMANCE_MONITORING_ENABLED=False,
    STATIC_ROOT="",
)
def test_dynamic_responses_are_gzipped_and_preserve_htmx_vary(client: Client) -> None:
    response = client.get(
        "/large-page/",
        headers={"Accept-Encoding": "gzip", "HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response["Content-Encoding"] == "gzip"
    assert b"safe-compressible-content" in gzip.decompress(response.content)
    vary = {value.strip() for value in response["Vary"].split(",")}
    assert {"Accept-Encoding", "HX-Request", "HX-Target"} <= vary


def test_performance_and_gzip_middleware_order() -> None:
    order = list(settings.MIDDLEWARE)
    whitenoise = order.index("whitenoise.middleware.WhiteNoiseMiddleware")
    performance = order.index("config.middleware.RequestPerformanceMiddleware")
    gzip_middleware = order.index("django.middleware.gzip.GZipMiddleware")
    sessions = order.index("django.contrib.sessions.middleware.SessionMiddleware")

    assert whitenoise < performance < gzip_middleware < sessions
