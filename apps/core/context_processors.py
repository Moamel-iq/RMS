"""
Template context for the application shell.

Runs on every render, including the anonymous login page, so it must stay
cheap and must never assume an authenticated user.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from apps.core.navigation import DEFAULT_MODULE_KEY, MODULES_BY_KEY
from apps.core.navigation_access import visible_modules_for
from apps.core.printing import logo_static_path
from apps.organizations.selectors import accessible_branches


def shell(request: HttpRequest) -> dict[str, Any]:
    """Provide the module rail, the active module, and the user's branches."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    # ?module= lets the rail preview a module that has no pages yet, so the
    # sidebar can show what that phase will contain. Only known keys are
    # honoured; anything else falls back rather than erroring.
    requested = request.GET.get("module")
    actual_key = getattr(request, "active_module", None) or DEFAULT_MODULE_KEY
    # Module previewing belongs only to the home screen. A query string must
    # never make a real Inventory URL render without its Inventory assets or
    # highlight the wrong permission/workflow context.
    if actual_key == DEFAULT_MODULE_KEY and requested in MODULES_BY_KEY:
        active_key = requested
    else:
        active_key = actual_key

    # The registry cut down to what this reader may open (ADR-034 §3). The
    # active module is taken from the same cut, so its sidebar hides the
    # same sections the rail would; a screen reached by a typed URL the reader
    # may not open still renders its shell from the full registry, because the
    # view is what answers 403, not the navigation.
    modules = visible_modules_for(user)
    visible_by_key = {module.key: module for module in modules}
    active_module = visible_by_key.get(active_key) or MODULES_BY_KEY.get(
        active_key, MODULES_BY_KEY[DEFAULT_MODULE_KEY]
    )
    resolver_match = getattr(request, "resolver_match", None)
    current_url_name = getattr(resolver_match, "view_name", "") or ""
    active_section = next(
        (
            section
            for section in active_module.sections
            if section.url_name == current_url_name
            or any(current_url_name.startswith(prefix) for prefix in section.active_prefixes)
        ),
        None,
    )

    return {
        "nav_modules": modules,
        "active_module": active_module,
        "active_section": active_section,
        "active_nav_group": active_section.group if active_section else "",
        "current_url_name": current_url_name,
        # Evaluated lazily by the template; an unrendered branch picker costs
        # no query.
        "user_branches": accessible_branches(user).select_related("organization"),
        "filter_query": _filter_query(request),
        # Paper needs to say who issued the page and under what letterhead.
        # The screen already knows both; only the printed heading uses them.
        **_print_identity(user),
    }


def _print_identity(user: Any) -> dict[str, Any]:
    """
    The letterhead for a printed screen.

    One branch is named outright; several are named by their organization,
    because a sheet that claimed one branch while showing another's rows would
    be worse than a sheet that claims neither.
    """
    branches = list(accessible_branches(user).select_related("organization")[:2])
    organization = branches[0].organization if branches else None
    return {
        "print_logo": logo_static_path(),
        "print_organization": f"{organization.code} — {organization.name_ar}"
        if organization
        else "",
        "print_branch": f"{branches[0].code} — {branches[0].name_ar}" if len(branches) == 1 else "",
    }


def _filter_query(request: HttpRequest) -> str:
    """
    Every query parameter except the page number, ready to prefix `page=`.

    Here rather than in a list view because it is derived from the request and
    nothing else, and because `settings/base_list.html` serves the settings,
    accounting and inventory lists alike. A version that only one of those
    supplied would silently drop the others' filters the moment somebody paged
    — which is the bug this replaced: pagination used to carry `q` and nothing
    else, so page two of a filtered list was page two of everything while the
    toolbar still showed the filter.
    """
    parameters = request.GET.copy()
    parameters.pop("page", None)
    parameters.pop("module", None)
    # Browser GET forms submit empty search/select controls too. They are not
    # active filters and should not keep a reset badge visible or pollute the
    # shareable URL. Repeated non-empty values are preserved in order.
    for key in list(parameters):
        values = [value for value in parameters.getlist(key) if value.strip()]
        if values:
            parameters.setlist(key, values)
        else:
            parameters.pop(key, None)
    encoded = parameters.urlencode()
    return f"{encoded}&" if encoded else ""
