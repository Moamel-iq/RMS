"""
The shared-shell contracts this release fixed, pinned so they stay fixed.

Every assertion here answers a finding from the 2026-08-25 control-and-
experience audit: a control the reader could not name, a page that grew wider
than the window, a column nobody could identify, a confirmation that asked the
browser instead of the system. They are cheap checks of markup and stylesheet
text — the pixels were verified in a browser, but a browser is not what stops
these from coming back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import create_branch, create_organization, grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name="خان مندي")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    from datetime import time

    return create_branch(
        organization=organization,
        code="011",
        name="البنوك",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def reader(branch: Branch) -> Client:
    user = User.objects.create_user(username="ux-reader", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.VIEWER)
    client = Client()
    client.force_login(user)
    return client


def test_the_command_search_is_named_and_states_its_shortcut(reader: Client) -> None:
    """
    Below 1100px the label is hidden and the button is an icon. Unnamed, it is
    the fastest control on the screen and the only one a screen-reader or
    voice-control user cannot ask for.
    """
    body = reader.get(reverse("inventory:item_list")).content.decode()
    trigger = body[body.index("command-trigger") - 60 : body.index("command-trigger") + 500]
    assert "aria-label=" in trigger
    assert 'aria-keyshortcuts="Control+K"' in trigger
    assert "title=" in trigger
    assert "Ctrl K" in trigger


def test_no_table_header_is_left_unnamed() -> None:
    """
    Thirty-six action columns rendered `<th></th>`. Sighted readers infer the
    column from the buttons in it; a screen reader announces nothing at all.
    """
    empty = []
    for path in TEMPLATES.rglob("*.html"):
        for match in re.finditer(r"<th\b[^>]*>\s*</th>", path.read_text(encoding="utf-8")):
            line = path.read_text(encoding="utf-8")[: match.start()].count("\n") + 1
            empty.append(f"{path.relative_to(ROOT)}:{line}")
    assert not empty, "unnamed table headers:\n  " + "\n  ".join(empty)


def test_dashboard_grids_may_be_narrower_than_their_contents() -> None:
    """
    A `1fr` track will not go below its own min-content, so one wide table made
    the Sales dashboard 1,677px wide inside a 1,250px window — the reader
    panned the whole page, navigation included, instead of one card.
    """
    css = (STATIC / "css" / "erp-design-system.css").read_text(encoding="utf-8")
    grid = css[css.index(".ui-dashboard-grid {") : css.index(".ui-dashboard-grid--even {") + 200]
    assert "minmax(0, 1.7fr)" in grid
    assert "minmax(18rem, 1fr)" in grid
    assert re.search(r"grid-template-columns:\s*1\.7fr", grid) is None


def test_wide_tables_are_contained_named_and_reachable() -> None:
    """
    A table that overflows must scroll inside its own region, and that region
    must be a named, focusable one — hidden columns otherwise belong to mouse
    users alone (WCAG 2.1.1).
    """
    script = (STATIC / "js" / "inventory-htmx.js").read_text(encoding="utf-8")
    assert "const containWideTables" in script
    assert 'shell.className = "ui-table-scroll"' in script
    assert "const regionName" in script
    assert 'shell.setAttribute("role", "region")' in script
    assert 'shell.setAttribute("tabindex", "0")' in script
    # And it stops being a tab stop when it no longer overflows.
    assert 'shell.dataset.scrollRegion === "auto"' in script
    assert 'shell.removeAttribute("tabindex")' in script


def test_an_identity_table_keeps_its_progressive_enhancement_hook() -> None:
    attendance = (TEMPLATES / "hr" / "attendance_list.html").read_text(encoding="utf-8")
    assert "data-sticky-identity" in attendance
    assert 'class="ui-table ui-table--responsive"' in attendance
    assert 'class="ui-table-scroll"' in attendance


def test_every_confirmation_goes_through_the_shared_dialog() -> None:
    """
    `hx-confirm` alone is the browser's prompt: one line, no document identity,
    no amount, no severity, and a button that says OK. Approvals, postings,
    terminations and deletions are confirmed in the system's own dialog.
    """
    shell = (STATIC / "js" / "ui-shell.js").read_text(encoding="utf-8")
    assert 'document.addEventListener("htmx:confirm"' in shell
    assert "event.detail.issueRequest(true)" in shell
    assert "const dressConfirm" in shell
    assert "const focusConfirmDefault" in shell

    bare = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"hx-confirm=", text):
            window = text[match.start() : match.start() + 700]
            if "data-confirm-title" not in window:
                line = text[: match.start()].count("\n") + 1
                bare.append(f"{path.relative_to(ROOT)}:{line}")
    assert not bare, "confirmations without a stated contract:\n  " + "\n  ".join(bare)


def test_a_reason_that_is_kept_forever_is_asked_for_with_a_label() -> None:
    """A placeholder disappears at the first keystroke, so a reason that lands
    in an append-only audit log is asked for with a persistent label."""
    employee = (TEMPLATES / "hr" / "employee_detail.html").read_text(encoding="utf-8")
    assert "placeholder=\"{% translate 'سبب الأرشفة' %}\"" not in employee
    assert "placeholder=\"{% translate 'سبب إعادة التفعيل' %}\"" not in employee
    assert "سجل التدقيق" in employee


def test_the_command_palette_folds_arabic_and_lists_each_screen_once() -> None:
    shell_js = (STATIC / "js" / "ui-shell.js").read_text(encoding="utf-8")
    select_js = (STATIC / "js" / "searchable-select.js").read_text(encoding="utf-8")
    assert "window.KhanMandiText" in select_js
    assert "window.KhanMandiText" in shell_js
    assert 'toLocaleLowerCase("ar")' in shell_js  # kept as the no-script fallback only
    palette = (TEMPLATES / "layouts" / "_command_palette.html").read_text(encoding="utf-8")
    assert "section.url_name != module.url_name" in palette


def test_settings_does_not_call_a_built_screen_unbuilt(reader: Client) -> None:
    from apps.core.navigation import MODULES_BY_KEY

    periods = [
        section
        for section in MODULES_BY_KEY["settings"].sections
        if str(section.label) == "الفترات المالية"
    ]
    assert periods, "the settings module no longer lists financial periods"
    assert periods[0].available is True
    assert periods[0].url_name == "accounting:period_list"


def test_printed_type_never_shrinks_below_seven_points() -> None:
    """
    Fitting a wide register by scaling took an 8.5pt table to about 5pt —
    smaller than contract footnotes. Past the floor it continues on another
    page instead, because type nobody can read has lost the column anyway.
    """
    script = (STATIC / "js" / "print-sheet.js").read_text(encoding="utf-8")
    assert "const MIN_SCALE = 7 / 8.5" in script
    assert "Math.max(MIN_SCALE," in script
    assert "Math.max(0.6," not in script
