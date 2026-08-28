"""Focused contracts for the redesigned inventory list and item form."""

from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from apps.users.models import User

pytestmark = pytest.mark.django_db


class _StartTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _tags(body: str, name: str) -> list[dict[str, str | None]]:
    parser = _StartTags()
    parser.feed(body)
    return [attributes for tag, attributes in parser.tags if tag == name]


def _class_tokens(attributes: dict[str, str | None]) -> set[str]:
    return set((attributes.get("class") or "").split())


def test_item_list_search_is_debounced_shareable_and_has_a_stable_target(
    manager: User, client_for: Callable[[User], Client]
) -> None:
    body = client_for(manager).get(reverse("inventory:item_list")).content.decode()

    toolbars = [tag for tag in _tags(body, "form") if "ui-filter-bar" in _class_tokens(tag)]
    results = [tag for tag in _tags(body, "section") if tag.get("id") == "list-results"]

    assert len(toolbars) == 1
    assert toolbars[0].get("method") == "get"
    assert toolbars[0].get("hx-target") == "#list-results"
    assert toolbars[0].get("hx-push-url") == "true"
    assert toolbars[0].get("hx-indicator") == "#list-loading"
    assert "delay:350ms" in (toolbars[0].get("hx-trigger") or "")
    assert len(results) == 1


def test_item_list_search_loading_and_table_region_have_accessible_names(
    manager: User, client_for: Callable[[User], Client]
) -> None:
    body = client_for(manager).get(reverse("inventory:item_list")).content.decode()

    search = [tag for tag in _tags(body, "input") if tag.get("id") == "list-search"]
    search_labels = [tag for tag in _tags(body, "label") if tag.get("for") == "list-search"]
    loading = [tag for tag in _tags(body, "span") if tag.get("id") == "list-loading"]
    table_regions = [
        tag
        for tag in _tags(body, "div")
        if tag.get("role") == "region" and "ui-table-scroll" in _class_tokens(tag)
    ]

    assert len(search) == len(search_labels) == 1
    assert search[0].get("type") == "search"
    assert search[0].get("name") == "q"
    assert search[0].get("placeholder")
    assert len(loading) == 1
    assert loading[0].get("role") == "status"
    assert loading[0].get("aria-live") == "polite"
    assert len(table_regions) == 1
    assert table_regions[0].get("tabindex") == "0"
    assert table_regions[0].get("aria-label")


def test_item_list_announces_swapped_results_and_marks_empty_params_for_pruning(
    manager: User, client_for: Callable[[User], Client]
) -> None:
    body = client_for(manager).get(reverse("inventory:item_list")).content.decode()

    toolbars = [tag for tag in _tags(body, "form") if "ui-filter-bar" in _class_tokens(tag)]
    counters = [tag for tag in _tags(body, "span") if tag.get("id") == "list-result-count"]

    assert len(toolbars) == 1
    assert "data-prune-empty-params" in toolbars[0]
    assert len(counters) == 1
    assert counters[0].get("role") == "status"
    assert counters[0].get("aria-live") == "polite"
    assert counters[0].get("aria-atomic") == "true"


def test_search_does_not_force_the_additional_filter_panel_open(
    manager: User, client_for: Callable[[User], Client]
) -> None:
    body = client_for(manager).get(reverse("inventory:item_list"), {"q": "RICE"}).content.decode()
    disclosures = [
        tag for tag in _tags(body, "details") if "ui-filter-disclosure" in _class_tokens(tag)
    ]

    assert len(disclosures) == 1
    assert "open" not in disclosures[0]


def test_item_list_exposes_the_golden_screen_table_and_compact_card_hooks(
    manager: User, client_for: Callable[[User], Client], rice: object
) -> None:
    body = client_for(manager).get(reverse("inventory:item_list")).content.decode()
    tables = [tag for tag in _tags(body, "table") if "ui-items-table" in _class_tokens(tag)]
    rows = [tag for tag in _tags(body, "tr") if "ui-item-row" in _class_tokens(tag)]
    filter_labels = [tag for tag in _tags(body, "span") if "ui-field__label" in _class_tokens(tag)]

    assert len(tables) == 1
    assert rows
    assert filter_labels


def test_item_list_exposes_active_filters_and_a_reset_destination(
    manager: User, client_for: Callable[[User], Client]
) -> None:
    body = (
        client_for(manager)
        .get(reverse("inventory:item_list"), {"q": "RICE", "item_type": "RAW_MATERIAL"})
        .content.decode()
    )

    active_filters = [
        tag for tag in _tags(body, "div") if "ui-active-filters" in _class_tokens(tag)
    ]
    reset_links = [
        tag
        for tag in _tags(body, "a")
        if "data-reset-filters" in tag and tag.get("href") == reverse("inventory:item_list")
    ]

    assert len(active_filters) == 1
    assert active_filters[0].get("aria-label")
    assert "RICE" in body
    assert len(reset_links) == 1


def test_invalid_item_form_has_a_linked_error_summary_and_required_cues(
    manager: User, client_for: Callable[[User], Client]
) -> None:
    response = client_for(manager).post(reverse("inventory:item_create"), data={})
    body = response.content.decode()

    summaries = [tag for tag in _tags(body, "div") if "data-error-summary" in tag]
    code_inputs = [tag for tag in _tags(body, "input") if tag.get("id") == "id_code"]
    code_labels = [tag for tag in _tags(body, "label") if tag.get("for") == "id_code"]
    error_links = [tag for tag in _tags(body, "a") if tag.get("href") == "#id_code"]

    assert response.status_code == 200
    assert len(summaries) == 1
    assert summaries[0].get("role") == "alert"
    assert summaries[0].get("tabindex") == "-1"
    assert len(code_inputs) == len(code_labels) == 1
    assert "required" in code_inputs[0]
    assert any("ui-field__required" in _class_tokens(tag) for tag in _tags(body, "span"))
    assert error_links


def test_inventory_forms_prevent_duplicates_and_warn_about_unsaved_work() -> None:
    script = (
        Path(__file__).resolve().parents[3] / "static" / "js" / "inventory-htmx.js"
    ).read_text(encoding="utf-8")

    assert "body[data-module='inventory'] form[method='post']" in script
    assert 'form.dataset.submitting === "true"' in script
    assert "(!form.noValidate && !form.checkValidity())" in script
    assert 'window.addEventListener("beforeunload"' in script


def test_inventory_htmx_prunes_empty_get_parameters_before_push_url() -> None:
    script = (
        Path(__file__).resolve().parents[3] / "static" / "js" / "inventory-htmx.js"
    ).read_text(encoding="utf-8")

    assert 'document.addEventListener("htmx:configRequest"' in script
    assert "[data-prune-empty-params]" in script
    assert "delete event.detail.parameters[name]" in script
