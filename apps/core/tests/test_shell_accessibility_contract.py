"""Accessibility and information-architecture contracts for the app shell."""

from __future__ import annotations

from datetime import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.navigation import MODULES_BY_KEY
from apps.organizations.models import Role
from apps.organizations.services import create_branch, create_organization, grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db


class _StartTags(HTMLParser):
    """Collect start-tag attributes without depending on an HTML test library."""

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


@pytest.fixture
def inventory_manager() -> User:
    organization = create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )
    user = User.objects.create_user(username="inventory-ui-manager", password="not-real-123")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


def test_shell_has_a_keyboard_skip_link_and_focusable_main(
    client: Client, inventory_manager: User
) -> None:
    client.force_login(inventory_manager)
    response = client.get(reverse("inventory:item_list"))

    assert response.status_code == 200
    body = response.content.decode()
    skip_links = [
        tag
        for tag in _tags(body, "a")
        if tag.get("href") == "#main-content" and "skip-link" in _class_tokens(tag)
    ]
    main = [tag for tag in _tags(body, "main") if tag.get("id") == "main-content"]

    assert len(skip_links) == 1
    assert len(main) == 1
    assert main[0].get("tabindex") == "-1"


def test_shell_assets_are_revisioned_and_icons_have_intrinsic_sizes(
    client: Client, inventory_manager: User
) -> None:
    """A stale pre-redesign stylesheet must not break the redesigned shell."""
    client.force_login(inventory_manager)
    body = client.get(reverse("inventory:item_list")).content.decode()

    assert "css/app.css?v=" in body
    assert "css/inventory.css?v=" in body
    assert "js/app-shell.js?v=" in body
    assert "js/inventory-htmx.js?v=" in body

    shell_svgs = [tag for tag in _tags(body, "svg") if tag.get("viewbox")]
    assert shell_svgs
    assert all(tag.get("width") and tag.get("height") for tag in shell_svgs)


def test_mobile_drawer_controls_name_and_control_the_navigation(
    client: Client, inventory_manager: User
) -> None:
    client.force_login(inventory_manager)
    body = client.get(reverse("inventory:item_list")).content.decode()

    navigation = [
        tag
        for tag in _tags(body, "div")
        if tag.get("id") == "application-navigation" and "data-shell-nav" in tag
    ]
    toggles = [tag for tag in _tags(body, "button") if "data-nav-toggle" in tag]

    assert len(navigation) == 1
    assert len(toggles) >= 2
    assert all(tag.get("aria-controls") == "application-navigation" for tag in toggles)
    assert all(tag.get("aria-expanded") in {"true", "false"} for tag in toggles)
    assert all(tag.get("aria-label") for tag in toggles)


def test_inventory_page_marks_both_module_and_section_as_current(
    client: Client, inventory_manager: User
) -> None:
    client.force_login(inventory_manager)
    body = client.get(reverse("inventory:item_list")).content.decode()
    # The rail marks the *module* current and links to its landing page — the
    # overview, as Sales and Accounting already do. The subnav marks the
    # *section* current and links to the screen itself. Two links, two hrefs.
    module_url = reverse("inventory:overview")
    item_url = reverse("inventory:item_list")

    current_links = [tag for tag in _tags(body, "a") if tag.get("aria-current") == "page"]

    assert any(
        "rail__item" in _class_tokens(tag) and tag.get("href") == module_url
        for tag in current_links
    )
    assert any(
        "subnav__item" in _class_tokens(tag) and tag.get("href") == item_url
        for tag in current_links
    )


def test_confirmation_dialog_is_named_described_and_keyboard_operable(
    client: Client, inventory_manager: User
) -> None:
    client.force_login(inventory_manager)
    body = client.get(reverse("inventory:item_list")).content.decode()

    dialogs = [tag for tag in _tags(body, "dialog") if "data-confirm-dialog" in tag]
    assert len(dialogs) == 1
    assert dialogs[0].get("aria-labelledby") == "confirm-dialog-title"
    assert dialogs[0].get("aria-describedby") == "confirm-dialog-message"
    assert any(tag.get("id") == "confirm-dialog-title" for tag in _tags(body, "h2"))
    assert any(tag.get("id") == "confirm-dialog-message" for tag in _tags(body, "p"))
    assert any("data-confirm-cancel" in tag for tag in _tags(body, "button"))
    assert any("data-confirm-accept" in tag for tag in _tags(body, "button"))


def test_confirmation_dialog_preserves_the_clicked_submit_action() -> None:
    """Named submit buttons must survive the confirm-and-resubmit round trip."""
    script = (Path(__file__).resolve().parents[3] / "static" / "js" / "app-shell.js").read_text(
        encoding="utf-8"
    )

    assert "event.submitter || null" in script
    assert "form.requestSubmit(submitter)" in script


def test_inventory_navigation_is_grouped_and_all_workflow_destinations_resolve() -> None:
    inventory = MODULES_BY_KEY["inventory"]
    sections_by_url = {section.url_name: section for section in inventory.sections}
    expected_destinations = {
        "inventory:overview",
        "inventory:item_list",
        "inventory:category_list",
        "inventory:package_unit_list",
        "inventory:conversion_list",
        "inventory:warehouse_list",
        "inventory:stock_list",
        "inventory:movement_list",
        "inventory:opening_list",
        "inventory:inventory_receipt_list",
        "inventory:inventory_issue_list",
        "inventory:inventory_return_in_list",
        "inventory:transfer_list",
        "inventory:in_transit",
        "inventory:inventory_waste_list",
        "inventory:count_list",
        "inventory:adjustment_list",
        "inventory:reason_code_list",
        "inventory:mapping_list",
        "inventory:reconciliation",
        "inventory:import_list",
        "inventory:report_valuation",
        "inventory:report_stock_card",
        "inventory:report_in_transit",
        "inventory:report_expiry",
        "inventory:report_reorder",
        "inventory:report_waste",
        "inventory:report_count_variance",
        "inventory:report_adjustments",
        "inventory:report_locations",
    }
    expected_groups = {
        "نظرة عامة",
        "البيانات الأساسية",
        "الرصيد والحركة",
        "الحركات المخزنية",
        "الجرد والتسويات",
        "الضبط والمطابقة",
        "التقارير",
    }

    assert set(sections_by_url) == expected_destinations
    assert all(section.available for section in inventory.sections)
    assert {str(section.group) for section in inventory.sections} == expected_groups
    assert all(reverse(route_name) for route_name in expected_destinations)
    assert sections_by_url["inventory:import_list"].active_prefixes == ("inventory:import_",)
