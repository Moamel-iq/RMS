"""
Which parts of the navigation a signed-in person may see (ADR-034 §3).

The registry in `navigation.py` describes the whole system. This module cuts
it down to one reader: a section is shown when the reader holds the permission
its screen requires, a module when it has one such section, and the module
opens on its first visible section when the reader may not open its own
landing page.

The permission a section requires is **derived from the view it links to**
rather than declared a second time here. The views are the truth about what a
screen needs; a copy in the registry would drift from them the first time a
view changed its mind. Derivation is cached for the life of the process,
because the URL configuration does not change while it runs.

Hidden, not muted. ADR-016 already answers 403 for a URL typed by hand; the
navigation is a courtesy to the reader, not the gate. Unbuilt sections keep
their muted rendering — that is a statement about the system, not about the
reader — and so does a whole module that has no screens yet.
"""

from __future__ import annotations

from dataclasses import replace
from functools import cache
from typing import TYPE_CHECKING

from django.urls import NoReverseMatch, Resolver404, resolve, reverse

from apps.core.navigation import DEFAULT_MODULE_KEY, MODULES, Module, Section

if TYPE_CHECKING:
    from apps.users.models import User

#: The developer tool is not an ERP permission.  It remains a break-glass
#: superuser tool and is deliberately absent from ordinary role groups.
ADMIN_URL_NAME = "admin:index"


@cache
def permission_for(url_name: str) -> str | None:
    """
    The permission the view behind `url_name` requires, or None when it
    declares none — or when the name does not resolve at all, which is a
    registry entry for a screen that does not exist yet.
    """
    try:
        match = resolve(reverse(url_name))
    except NoReverseMatch, Resolver404:
        return None
    view_class = getattr(match.func, "view_class", None)
    required = getattr(view_class, "required_permission", None)
    if required:
        return str(required)
    # Django's own `PermissionRequiredMixin` spells it differently, and a view
    # using it is every bit as gated. Reading only this project's name made
    # those screens look ungated to the navigation, so their sections were
    # offered to readers the view itself would refuse — a cashier was shown
    # عروض الموردين المستقلة and got a 403 for taking the invitation.
    #
    # A view may name several; the first is enough here, because this is the
    # courtesy filter and the view still enforces all of them.
    declared = getattr(view_class, "permission_required", None)
    if isinstance(declared, str):
        return declared
    if declared:
        return str(next(iter(declared), "")) or None
    return None


@cache
def superuser_only_for(url_name: str) -> bool:
    """Whether the view is an explicit break-glass screen."""
    try:
        match = resolve(reverse(url_name))
    except NoReverseMatch, Resolver404:
        return False
    view_class = getattr(match.func, "view_class", None)
    return bool(getattr(view_class, "superuser_only", False))


def may_open(user: User, url_name: str | None) -> bool:
    """Whether this reader may open the screen behind a URL name."""
    if not url_name:
        return False
    if url_name == ADMIN_URL_NAME:
        return bool(user.is_superuser)
    if user.is_superuser:
        return True
    if superuser_only_for(url_name):
        return False
    permission = permission_for(url_name)
    return True if permission is None else bool(user.has_perm(permission))


def may_see(user: User, section: Section) -> bool:
    """An unbuilt section is shown muted to everyone; a built one needs its permission."""
    if not section.available:
        return True
    return may_open(user, section.url_name)


def visible_modules_for(user: User) -> tuple[Module, ...]:
    """
    The registry, cut down to what this reader may open.

    Returns new `Module` values whose `sections` are the visible subset and
    whose `url_name` is reachable; the registry itself is never mutated.
    """
    visible: list[Module] = []
    for module in MODULES:
        if module.key == DEFAULT_MODULE_KEY or not module.available:
            visible.append(module)
            continue
        sections = tuple(section for section in module.sections if may_see(user, section))
        reachable = [section for section in sections if section.available and section.url_name]
        if not reachable:
            continue
        url_name = module.url_name if may_open(user, module.url_name) else reachable[0].url_name
        visible.append(replace(module, sections=sections, url_name=url_name))
    return tuple(visible)


__all__ = ["may_open", "may_see", "permission_for", "superuser_only_for", "visible_modules_for"]
