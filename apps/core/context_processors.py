"""
Template context for the application shell.

Runs on every render, including the anonymous login page, so it must stay
cheap and must never assume an authenticated user.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from apps.core.navigation import DEFAULT_MODULE_KEY, MODULES, MODULES_BY_KEY
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
    if requested in MODULES_BY_KEY:
        active_key = requested
    else:
        active_key = getattr(request, "active_module", None) or DEFAULT_MODULE_KEY

    active_module = MODULES_BY_KEY.get(active_key, MODULES_BY_KEY[DEFAULT_MODULE_KEY])

    return {
        "nav_modules": MODULES,
        "active_module": active_module,
        # Evaluated lazily by the template; an unrendered branch picker costs
        # no query.
        "user_branches": accessible_branches(user),
        "filter_query": _filter_query(request),
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
    encoded = parameters.urlencode()
    return f"{encoded}&" if encoded else ""
