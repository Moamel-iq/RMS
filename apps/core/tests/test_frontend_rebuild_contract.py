"""Filesystem contracts for the authoritative ``ui-*`` frontend rebuild.

These checks deliberately inspect source files.  They catch a partial rollback
before it can ship: loading both design systems, restoring a retired class in a
template, or losing progressive enhancement while rearranging navigation.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"

LEGACY_ASSETS = (
    STATIC / "css" / "app.css",
    STATIC / "css" / "inventory.css",
    STATIC / "js" / "app-shell.js",
)

LEGACY_CLASSES = {
    "shell",
    "rail",
    "subnav",
    "topbar",
    "pagehead",
    "panel",
    "formcard",
    "btn",
    "table",
    "table-wrap",
    "toolbar",
    "filterbar",
    "formrow",
    "chip",
    "status-badge",
}

LEGACY_CLASS_PREFIXES = (
    "shell__",
    "rail__",
    "subnav__",
    "topbar__",
    "pagehead__",
    "panel__",
    "formcard__",
    "btn--",
    "table__",
    "toolbar__",
    "filterbar__",
    "formrow__",
    "chip--",
    "status-badge--",
)


def _template_class_tokens(path: Path) -> set[str]:
    source = path.read_text(encoding="utf-8")
    values = re.findall(r"\bclass\s*=\s*(['\"])(.*?)\1", source, flags=re.S)
    return {token for _quote, value in values for token in value.split()}


def test_base_loads_one_authoritative_design_system_and_local_arabic_fonts() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert "css/erp-design-system.css" in base
    assert "js/ui-shell.js" in base
    assert "fonts/alexandria-arabic.woff2" in base
    assert "fonts/alexandria-latin.woff2" in base
    assert "css/app.css" not in base
    assert "css/inventory.css" not in base
    assert "js/app-shell.js" not in base

    for font in ("alexandria-arabic.woff2", "alexandria-latin.woff2"):
        path = STATIC / "fonts" / font
        assert path.is_file() and path.stat().st_size > 0, path


def test_retired_legacy_frontend_assets_are_physically_removed() -> None:
    remaining = [str(path.relative_to(ROOT)) for path in LEGACY_ASSETS if path.exists()]
    assert not remaining, "retired frontend assets still present:\n  " + "\n  ".join(remaining)


def test_templates_do_not_restore_common_legacy_presentation_classes() -> None:
    violations: list[str] = []
    for path in TEMPLATES.rglob("*.html"):
        for token in _template_class_tokens(path):
            if token in LEGACY_CLASSES or token.startswith(LEGACY_CLASS_PREFIXES):
                violations.append(f"{path.relative_to(ROOT)}: {token}")

    assert not violations, "legacy presentation classes found:\n  " + "\n  ".join(violations)


def test_shell_uses_the_new_primary_secondary_and_header_contract() -> None:
    shell = (TEMPLATES / "shell.html").read_text(encoding="utf-8")
    primary = (TEMPLATES / "layouts" / "_primary_navigation.html").read_text(encoding="utf-8")
    secondary = (TEMPLATES / "layouts" / "_secondary_navigation_panel.html").read_text(
        encoding="utf-8"
    )

    assert 'class="ui-app-shell ui-app-shell--compact"' in shell
    assert 'class="ui-app-header"' in shell
    assert 'class="ui-primary-nav"' in primary
    assert 'class="ui-secondary-nav"' in secondary
    assert 'id="main-content"' in shell
    assert 'class="ui-skip-link"' in shell


def test_navigation_keeps_full_page_fallback_history_and_small_htmx_swaps() -> None:
    primary = (TEMPLATES / "layouts" / "_primary_navigation.html").read_text(encoding="utf-8")
    secondary_item = (TEMPLATES / "layouts" / "_secondary_navigation_item.html").read_text(
        encoding="utf-8"
    )
    fragment = (TEMPLATES / "settings" / "_form_fragment.html").read_text(encoding="utf-8")

    for navigation in (primary, secondary_item):
        assert "href=" in navigation
        assert "hx-get=" in navigation
        assert 'hx-target="#main-content"' in navigation
        assert 'hx-push-url="true"' in navigation
        assert 'hx-indicator="#app-loading"' in navigation
        assert 'hx-sync="#main-content:replace"' in navigation

    assert "shell_navigation_oob" in fragment
    assert "with navigation_oob=True" in fragment


def test_screen_templates_choose_full_shell_or_fragment_at_request_time() -> None:
    direct_shell_extends: list[str] = []
    dynamic_shell_extends = 0
    for path in TEMPLATES.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        if re.search(r'{%\s*extends\s+["\']shell\.html["\']\s*%}', source):
            direct_shell_extends.append(str(path.relative_to(ROOT)))
        if 'extends shell_base_template|default:"shell.html"' in source:
            dynamic_shell_extends += 1

    assert not direct_shell_extends, (
        "templates bypassing the HTMX shell contract:\n  " + "\n  ".join(direct_shell_extends)
    )
    assert dynamic_shell_extends > 0


def test_live_filter_forms_bind_every_control_without_first_match_selectors() -> None:
    purchase = (TEMPLATES / "procurement" / "supplier_invoice_list.html").read_text(
        encoding="utf-8"
    )
    shared_list = (TEMPLATES / "settings" / "base_list.html").read_text(encoding="utf-8")

    assert "from:find" not in "\n".join(
        path.read_text(encoding="utf-8") for path in TEMPLATES.rglob("*.html")
    )
    assert "keyup changed delay:350ms from:#purchase-invoice-search" in purchase
    assert "keyup changed delay:350ms from:#list-search" in shared_list
    assert ', change"' in purchase
    assert ', change"' in shared_list
    assert 'hx-sync="closest form:replace"' in purchase
    assert 'hx-sync="closest form:replace"' in shared_list


def test_approved_desktop_density_keeps_mobile_and_htmx_layout_safe() -> None:
    design_system = (STATIC / "css" / "erp-design-system.css").read_text(encoding="utf-8")
    purchase_css = (STATIC / "css" / "procurement-invoices.css").read_text(encoding="utf-8")
    purchase = (TEMPLATES / "procurement" / "supplier_invoice_list.html").read_text(
        encoding="utf-8"
    )

    assert "Approved desktop density" in design_system
    assert "@media screen and (min-width: 721px)" in design_system
    assert ".ui-app-shell--compact .ui-content" in design_system
    assert "padding: 19px 21px" in design_system
    assert "@media (max-width: 720px)" in purchase_css
    assert "display: flex" in purchase_css
    assert "ui-loading--inline ui-purchase-loading htmx-indicator" in purchase
    assert "ui-purchase-result-count ui-sr-only" in purchase
