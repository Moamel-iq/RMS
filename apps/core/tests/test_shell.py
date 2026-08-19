"""
Application shell.

The rail shows every module in the approved build order, including the ones
that do not exist yet. Those must be visibly inert rather than links to
nowhere, and the navigation must not reshape itself as phases land.
"""

from __future__ import annotations

from datetime import time

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.navigation import MODULES, MODULES_BY_KEY
from apps.organizations.models import Role
from apps.organizations.services import create_branch, create_organization, grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def user() -> User:
    return User.objects.create_user(username="manager", password=PASSWORD)


@pytest.fixture
def member(user: User) -> User:
    organization = create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return user


class TestNavigationDefinition:
    def test_every_module_key_is_unique(self) -> None:
        keys = [module.key for module in MODULES]
        assert len(keys) == len(set(keys))

    def test_available_modules_declare_a_url(self) -> None:
        """An available module without a URL would raise at render time."""
        for module in MODULES:
            if module.available:
                assert module.url_name, module.key

    def test_available_sections_declare_a_url(self) -> None:
        for module in MODULES:
            for section in module.sections:
                if section.available:
                    assert section.url_name, f"{module.key}:{section.label}"

    def test_unavailable_items_declare_no_url(self) -> None:
        """A URL on an inert item invites someone to wire it up by accident."""
        for module in MODULES:
            for section in module.sections:
                if not section.available:
                    assert section.url_name is None

    def test_build_order_modules_are_present(self) -> None:
        expected = {
            "home",
            "inventory",
            "procurement",
            "kitchen",
            "sales",
            "accounting",
            "hr",
            "reports",
            "settings",
        }
        assert set(MODULES_BY_KEY) == expected


class TestShellRendering:
    def test_home_renders_the_shell(self, client: Client, user: User) -> None:
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        assert 'class="shell"' in body
        assert 'class="rail"' in body
        assert 'class="subnav"' in body
        assert 'class="topbar"' in body

    def test_every_module_appears_in_the_rail(self, client: Client, user: User) -> None:
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        for module in MODULES:
            assert str(module.label) in body, module.key

    def test_unbuilt_modules_are_marked_as_such(self, client: Client, user: User) -> None:
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        assert "is-unavailable" in body

    def test_unbuilt_sections_are_inert(self, client: Client, user: User) -> None:
        """
        Sections without an implementation must not be clickable.

        Asked of a module that **currently** has unbuilt sections rather than of
        a fixed one. This test used to name Sales; Phase 4 finished it, and a
        test naming a specific module goes stale the day that module lands —
        which is exactly what happened here. Reading the module out of the
        navigation data keeps the assertion about the *rule* rather than about
        one phase's progress.
        """
        client.force_login(user)
        unfinished = next(
            module
            for module in MODULES
            if any(not section.available for section in module.sections)
        )
        body = client.get(reverse("users:home"), {"module": unfinished.key}).content.decode()
        assert 'aria-disabled="true"' in body
        assert "قريباً" in body

    def test_a_finished_module_has_no_inert_section_left(self, client: Client, user: User) -> None:
        """
        Sales is complete: twelve sections, twelve routes, no قريباً badge.

        The other half of the rule above, and the half that would otherwise go
        untested — an entry that stays muted after its screen exists is as
        misleading as one that 404s, and nothing else would notice.
        """
        client.force_login(user)
        sales = next(module for module in MODULES if module.key == "sales")
        assert len(sales.sections) == 12
        assert all(section.available for section in sales.sections)
        assert all(section.url_name for section in sales.sections)

        body = client.get(reverse("users:home"), {"module": "sales"}).content.decode()
        assert "قريباً" not in body

    def test_active_module_is_marked(self, client: Client, user: User) -> None:
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        assert "is-active" in body
        assert 'aria-current="page"' in body

    def test_rail_can_preview_a_module_that_has_no_pages(self, client: Client, user: User) -> None:
        """
        The point of showing unbuilt modules is being able to see what each
        phase will contain. A module you cannot open shows nothing.
        """
        client.force_login(user)
        body = client.get(reverse("users:home"), {"module": "inventory"}).content.decode()
        assert "الأصناف" in body
        assert "تقييم المخزون" in body

    def test_every_module_sidebar_can_be_previewed(self, client: Client, user: User) -> None:
        client.force_login(user)
        for module in MODULES:
            response = client.get(reverse("users:home"), {"module": module.key})
            assert response.status_code == 200, module.key
            body = response.content.decode()
            for section in module.sections:
                assert str(section.label) in body, f"{module.key}:{section.label}"

    def test_the_content_area_follows_the_selected_module(self, client: Client, user: User) -> None:
        """
        The content panel must say what the sidebar says. Showing the home
        page under an "المخزون" sidebar reads as the module having failed to
        open.
        """
        client.force_login(user)
        body = client.get(reverse("users:home"), {"module": "inventory"}).content.decode()
        assert "هذه الوحدة لم تُبنَ بعد" in body
        assert "المرحلة ١" in body
        # The home page's own content must not still be sitting there.
        assert "الفروع المتاحة لك" not in body

    def test_the_content_area_lists_what_the_module_will_contain(
        self, client: Client, user: User
    ) -> None:
        client.force_login(user)
        body = client.get(reverse("users:home"), {"module": "sales"}).content.decode()
        assert "تسويات التطبيقات" in body
        assert "إقفال الكاشير" in body

    def test_home_still_shows_the_home_content(self, client: Client, user: User) -> None:
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        assert "الفروع المتاحة لك" in body
        assert "هذه الوحدة لم تُبنَ بعد" not in body

    def test_unknown_module_falls_back_instead_of_erroring(
        self, client: Client, user: User
    ) -> None:
        client.force_login(user)
        response = client.get(reverse("users:home"), {"module": "../../etc/passwd"})
        assert response.status_code == 200
        assert response.context["active_module"].key == "home"

    def test_settings_sections_resolve(self, client: Client, user: User) -> None:
        """These are the only sections with an implementation behind them."""
        settings_module = MODULES_BY_KEY["settings"]
        available = [s for s in settings_module.sections if s.available]
        assert len(available) == 7
        for section in available:
            assert section.url_name is not None
            assert reverse(section.url_name)

    def test_foundation_screens_are_native_not_django_admin(self) -> None:
        """
        Django admin stays available as a developer tool, but the five
        foundation screens must be the application's own.
        """
        settings_module = MODULES_BY_KEY["settings"]
        native = [
            s
            for s in settings_module.sections
            if s.available and not str(s.url_name).startswith("admin:")
        ]
        assert len(native) == 6
        for section in native:
            assert reverse(str(section.url_name)).startswith("/settings/")


class TestShellShowsBranchAccess:
    def test_member_sees_their_branch(self, client: Client, member: User) -> None:
        client.force_login(member)
        body = client.get(reverse("users:home")).content.decode()
        assert "البنوك" in body

    def test_user_without_branches_is_told_so(self, client: Client, user: User) -> None:
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        assert "لا توجد فروع مسندة إليك" in body

    def test_shell_context_is_absent_for_anonymous_requests(self, client: Client) -> None:
        """
        The context processor runs on the login page too. It must not touch
        the database or assume a user.
        """
        response = client.get(reverse("users:login"))
        assert response.status_code == 200
        assert "nav_modules" not in response.context
