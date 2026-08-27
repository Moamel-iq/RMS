"""
The navigation shows what the reader may do (ADR-034 §3).

A section's permission is read off the view it links to, so these tests pin
the *rule* — shown if and only if held — rather than any one module's list,
and then check the one concrete case the owner asked for: a person given only
the item master sees the inventory module and its item screens, and does not
see the stock balances, the postings, or any other module.
"""

from __future__ import annotations

from datetime import time

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.navigation import MODULES_BY_KEY
from apps.core.navigation_access import may_open, permission_for, visible_modules_for
from apps.inventory.permissions import VIEW_ITEM, VIEW_STOCK
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    create_role_definition,
    grant_branch_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="011",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def item_reader(organization: Organization, branch: Branch) -> User:
    """Holds one permission: reading the item master. Not stock, not value."""
    user = User.objects.create_user(username="reader", password=PASSWORD)
    post = create_role_definition(
        organization=organization,
        code="item-reader",
        name_ar="قارئ الأصناف",
        permissions=[VIEW_ITEM],
    )
    grant_branch_access(user=user, branch=branch, role=post.key)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def manager(branch: Branch) -> User:
    user = User.objects.create_user(username="manager", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


def _login(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_every_visible_section_is_one_the_reader_may_open_and_no_hidden_one_is(
    item_reader: User,
) -> None:
    visible = {m.key: m for m in visible_modules_for(item_reader)}
    for module in MODULES_BY_KEY.values():
        if module.key in {"home", "settings"} or not module.available:
            continue
        shown = visible.get(module.key)
        for section in module.sections:
            if not section.available:
                continue
            is_shown = shown is not None and section in shown.sections
            assert is_shown == may_open(item_reader, section.url_name), section.label


def test_an_item_reader_sees_the_item_master_and_not_the_stock(item_reader: User) -> None:
    assert permission_for("inventory:item_list") == VIEW_ITEM
    assert permission_for("inventory:stock_list") == VIEW_STOCK
    assert permission_for("inventory:overview") == VIEW_STOCK

    visible = {m.key: m for m in visible_modules_for(item_reader)}
    inventory = visible["inventory"]
    urls = {section.url_name for section in inventory.sections}
    assert "inventory:item_list" in urls
    assert "inventory:stock_list" not in urls
    # The landing page needs stock authority; the module opens where it can.
    assert inventory.url_name != "inventory:overview"
    assert inventory.url_name in urls
    # Modules with nothing to show are gone, not muted.
    assert "accounting" not in visible and "hr" not in visible
    # Settings are staff business; the home module is everyone's.
    assert "settings" not in visible and "home" in visible


def test_the_rendered_shell_follows_the_same_cut(item_reader: User, manager: User) -> None:
    reader_body = _login(item_reader).get(reverse("inventory:item_list")).content.decode()
    assert reverse("inventory:item_list") in reader_body
    assert reverse("inventory:stock_list") not in reader_body
    assert reverse("accounting:dashboard") not in reader_body

    manager_body = _login(manager).get(reverse("inventory:item_list")).content.decode()
    assert reverse("inventory:stock_list") in manager_body


def test_a_superuser_and_staff_see_everything_built() -> None:
    root = User.objects.create_superuser(username="root", password=PASSWORD)
    visible = {m.key: m for m in visible_modules_for(root)}
    for module in MODULES_BY_KEY.values():
        assert module.key in visible, module.key
        if module.available:
            assert visible[module.key].sections == module.sections
    # `visible_modules_for` returns a filtered copy of every available module,
    # so identity is not the contract — the sections a superuser sees are.
    assert visible["settings"].sections == MODULES_BY_KEY["settings"].sections


def test_a_screen_without_a_declared_permission_stays_visible() -> None:
    """A screen nobody gated is a screen nobody meant to hide."""
    user = User.objects.create_user(username="plain", password=PASSWORD)
    assert permission_for("users:home") is None
    assert may_open(user, "users:home")
    assert not may_open(user, "admin:index")
