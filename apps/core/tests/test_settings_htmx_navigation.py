"""Database-free coverage of the settings full-page/fragment contract."""

from typing import Any, cast

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import resolve, reverse

from apps.core.navigation import MODULES_BY_KEY
from apps.core.views import FoundationListView
from apps.users.models import User

LIST_URLS = (
    "organizations:organization_list",
    "organizations:branch_list",
    "organizations:access_list",
    "organizations:access_request_list",
    "organizations:role_list",
    "users:user_list",
    "units:unit_list",
    "core:audit_list",
)


def render_settings(url_name: str, target: str | None) -> tuple[str, dict[str, Any]]:
    """Use the real shared view and templates, without querying any tenant."""
    headers = {"HX-Request": "true", "HX-Target": target} if target else {}
    request = RequestFactory().get(reverse(url_name), headers=headers)
    request.user = User(username="settings-test")
    view_class = cast(Any, resolve(request.path).func).view_class
    assert issubclass(view_class, FoundationListView)
    view = FoundationListView()
    view.setup(request)
    view.object_list = []
    context = view.get_context_data()
    context.update(
        request=request,
        user=request.user,
        active_module=MODULES_BY_KEY["settings"],
        nav_modules=[MODULES_BY_KEY["settings"]],
        shell_navigation_oob=target == "main-content",
        page_title="Settings",
    )
    return render_to_string(view_class.template_name, context), context


@pytest.mark.parametrize("url_name", LIST_URLS)
def test_direct_page_has_one_shell(url_name: str) -> None:
    body, context = render_settings(url_name, None)
    assert context["list_base_template"] == "shell.html"
    assert body.count('class="ui-app-header"') == 1
    assert body.count('id="main-content"') == 1
    assert 'hx-target="#list-results"' in body


@pytest.mark.parametrize("url_name", LIST_URLS)
def test_navigation_returns_page_and_oob_navigation(url_name: str) -> None:
    body, context = render_settings(url_name, "main-content")
    assert context["list_base_template"] == "settings/_form_fragment.html"
    assert 'class="ui-page ui-page--list"' in body
    assert "<html" not in body and "ui-app-shell" not in body
    assert 'class="ui-app-header"' not in body
    assert 'hx-swap-oob="outerHTML:#primary-navigation"' in body
    assert 'hx-swap-oob="outerHTML:#secondary-navigation"' in body


@pytest.mark.parametrize("url_name", LIST_URLS)
def test_filter_returns_results_without_the_page(url_name: str) -> None:
    body, context = render_settings(url_name, "list-results")
    assert context["list_base_template"] == "settings/_list_fragment.html"
    assert body.count('id="list-results"') == 1
    assert 'class="ui-page-header"' not in body
    assert "<html" not in body and "ui-app-shell" not in body
    assert 'id="primary-navigation"' not in body
