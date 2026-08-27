"""
The searchable select: every select with enough options becomes a combobox.

The behaviour lives in `static/js/searchable-select.js` and runs in the
browser, so these tests pin the contract the rest of the system relies on —
the asset ships with the shell, the native select stays the form control, the
combobox follows the ARIA pattern, Arabic spelling variants are folded, and the
stylesheet keeps to logical properties — rather than the pixels.
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
SCRIPT = ROOT / "static" / "js" / "searchable-select.js"
STYLES = ROOT / "static" / "css" / "app.css"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    from datetime import time

    return create_branch(
        organization=organization,
        code="011",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def reader(branch: Branch) -> Client:
    user = User.objects.create_user(username="reader", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.VIEWER)
    client = Client()
    client.force_login(user)
    return client


def test_the_script_ships_with_the_shell_and_is_revisioned(reader: Client) -> None:
    body = reader.get(reverse("inventory:item_list")).content.decode()
    assert "js/searchable-select.js?v=" in body
    # It follows the shell script, which wires field descriptions first.
    assert body.index("js/app-shell.js?v=") < body.index("js/searchable-select.js?v=")


def test_the_login_page_does_not_load_it() -> None:
    body = Client().get(reverse("users:login")).content.decode()
    assert "searchable-select.js" not in body


def test_the_native_select_remains_the_form_control() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    # The select is wrapped, never replaced or renamed: the posted value is unchanged.
    assert "wrapper.append(input, select)" in script
    assert 'select.removeAttribute("name")' not in script
    assert "select.remove()" not in script
    # Choosing an option writes the native select and tells its listeners.
    assert "select.value = row.option.value" in script
    assert 'select.dispatchEvent(new Event("change", { bubbles: true }))' in script
    # Only selects worth searching are enhanced; the author can force or forbid it.
    assert "const ENHANCE_FROM = 6" in script
    assert 'mode === "off"' in script and 'mode !== "always"' in script
    assert "select.multiple || select.size > 1" in script


def test_requiredness_is_reported_on_a_control_the_reader_can_see() -> None:
    """
    The native select is one pixel wide and transparent. Asked to report a
    missing value on it, the browser focuses something invisible and refuses the
    submit without a message — so the attribute moves to the text box.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    mirror = script[script.index("const mirrorState") : script.index("mirrorState();")]
    assert "input.required = true;" in mirror
    assert "select.required = false;" in mirror
    assert "input.disabled = select.disabled;" in mirror
    # The move is the script's alone: nothing in the shell markup carries it, so
    # Django's widget still renders `required` and a page without JavaScript
    # keeps it on the select.
    assert "mirrorState();" in script
    assert "required" not in (ROOT / "templates" / "base.html").read_text(encoding="utf-8")


def test_the_combobox_follows_the_aria_pattern() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    for attribute in (
        'role", "combobox',
        "aria-autocomplete",
        "aria-expanded",
        "aria-controls",
        "aria-activedescendant",
        'role", "listbox',
        'role", "option',
        "aria-selected",
    ):
        assert attribute in script, attribute
    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Enter", "Escape", "Tab"):
        assert f'case "{key}"' in script, key
    # The label reaches the text box; the native select leaves the tree.
    assert "label.htmlFor = input.id" in script
    assert 'select.setAttribute("aria-hidden", "true")' in script
    assert "select.tabIndex = -1" in script


def test_arabic_spelling_variants_are_folded() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    fold = script[script.index("const fold") : script.index("const matches")]
    assert "\\u064B-\\u0652" in fold  # tashkeel
    assert "\\u0640" in fold  # tatweel
    assert "[أإآٱ]" in fold and '"ا"' in fold
    assert "/ة/g" in fold and '"ه"' in fold
    assert "/ى/g" in fold and '"ي"' in fold
    assert "[٠-٩۰-۹]" in fold
    # Persian and Central-Kurdish keyboards are common in Iraq: their yeh and
    # kaf must reach an Arabic-typed label.
    assert "[یيے]" in fold and "[کڪ]" in fold
    # Invisible marks (ZWNJ from those layouts, ZWJ, ZWSP, ALM) are stripped.
    assert "\\u200B-\\u200F" in fold and "\\u061C" in fold
    # A numeric value (a database id) is not searchable text.
    assert "/^\\d+$/.test(child.value)" in script


def test_leaving_the_box_never_silently_changes_the_selection() -> None:
    """
    Two options may read the same ("الفرع الرئيسي" in two organizations), and a
    half-typed search must not become a selection on the way out — on a filter
    form that would reload the screen for a record nobody chose.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    # Text that still reads as the current selection settles on it, whatever else matches.
    assert 'if (typed === fold(selectedLabel()) && select.value !== "")' in script
    # A zero-width mark is noise in a label and a word break in a query.
    assert "const foldQuery = (value) =>" in script
    assert "const folded = foldQuery(query);" in script
    assert "candidates.find((row) => row.option.selected && fold(row.label) === typed)" in script
    # A unique partial match is taken only when the reader commits: Enter or Tab.
    assert "const settle = ({ acceptUnique = false } = {}) => {" in script
    assert "else if (acceptUnique && candidates.length === 1) commit(candidates[0]);" in script
    assert script.count("settle({ acceptUnique: true })") == 2
    body = script[script.index('input.addEventListener("blur"') :]
    assert "acceptUnique" not in body  # blur settles without committing a guess


def test_enter_leaves_no_stale_popup() -> None:
    """Reverting the text must not leave "no results" standing under it."""
    script = SCRIPT.read_text(encoding="utf-8")
    enter = script[script.index('case "Enter"') : script.index('case "Escape"')]
    assert "settle({ acceptUnique: true });" in enter and "closeList();" in enter


def test_swapped_fragments_are_enhanced() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'document.addEventListener("htmx:afterSwap"' in script
    assert 'document.addEventListener("htmx:load"' in script
    assert "new MutationObserver" in script


def test_a_page_restored_from_history_is_enhanced_again() -> None:
    """
    htmx caches `body.innerHTML`. Pressing Back therefore restores the wrapper
    and the text box as markup with no listeners, over a select that is one
    pixel wide and untabbable — so readiness may not be read from the DOM.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert "const live = new WeakSet()" in script
    assert "if (live.has(select)) return;" in script
    assert "const unwrapStale = (select) =>" in script
    assert "stale.replaceWith(select);" in script
    assert 'document.addEventListener("htmx:historyRestore"' in script
    assert "if (event.persisted) restore();" in script


def test_enhancement_waits_for_htmx_to_bind_its_triggers() -> None:
    """
    `hx-trigger="change from:find input"` binds to the first input in the form.
    Injected before htmx processes the page, the combobox would be that input.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'if (document.readyState === "complete") {' in script
    assert 'document.addEventListener("DOMContentLoaded", () => enhanceWithin(document));' in script


def test_the_box_keeps_its_own_events_and_the_popup_leaves_the_label() -> None:
    """
    Searching is not editing, and a <ul> inside a <label> forwards its clicks to
    the label's control — which would re-open the list the moment it was used.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    typing = script[
        script.index('input.addEventListener("input"') : script.index(
            'input.addEventListener("change"'
        )
    ]
    assert "event.stopPropagation();" in typing
    assert "document.body.append(list);" in script
    click = script[script.index('list.addEventListener("click"') :]
    assert "event.preventDefault();" in click.split("});")[0]
    # The popup is outside the wrapper, so both are asked before closing.
    assert "!wrapper.contains(event.target) && !list.contains(event.target)" in script
    assert "list.remove();" in script


def test_the_accessible_name_follows_the_reader_to_the_text_box() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'input.setAttribute("aria-labelledby", labelledBy);' in script
    assert 'input.setAttribute("aria-label", labelText);' in script


def test_the_stylesheet_section_uses_logical_properties_only() -> None:
    styles = STYLES.read_text(encoding="utf-8")
    start = styles.index("17. Searchable select")
    section = styles[start:]
    for rule in (
        ".combo {",
        ".combo > input.combo__input {",
        ".combo > .combo__native {",
        ".combo__list {",
        ".combo__option {",
    ):
        assert rule in section, rule
    physical = re.findall(
        r"^\s*(left|right|margin-left|margin-right|padding-left|padding-right|text-align: (?:left|right))\s*:",
        section,
        re.M,
    )
    assert physical == []
    # The caret's borders are the one deliberate exception: a rotation angle is
    # physical, so a logical border pair would point sideways in RTL.
    assert "border-right: 2px solid var(--ink-muted);" in section
    assert "border-bottom: 2px solid var(--ink-muted);" in section
    # The list's coordinates come from the script; the stylesheet sets none.
    assert "inset-inline-end" in section
