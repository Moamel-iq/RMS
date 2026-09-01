"""
Application shell.

The primary navigation shows every module in the approved build order, including the ones
that do not exist yet. Those must be visibly inert rather than links to
nowhere, and the navigation must not reshape itself as phases land.
"""

from __future__ import annotations

import re
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
    organization = create_organization(code="KM", name="خان مندي")
    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name="البنوك",
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

    def test_reports_center_contains_only_available_destinations(self) -> None:
        reports = MODULES_BY_KEY["reports"]

        assert reports.available
        assert reports.url_name
        assert all(section.available and section.url_name for section in reports.sections)

    def test_unavailable_items_declare_no_url(self) -> None:
        """A URL on an inert item invites someone to wire it up by accident."""
        for module in MODULES:
            for section in module.sections:
                if not section.available:
                    assert section.url_name is None

    def test_build_order_modules_are_present(self) -> None:
        """
        The rail carries exactly the modules the build order names.

        An exact set rather than a subset, so a module added without a
        decision shows up here as a failing test rather than as a new icon
        somebody notices a week later.
        """
        expected = {
            "home",
            "sales",
            "inventory",
            "procurement",
            "kitchen",
            "accounting",
            # Phase 8's read-only analysis layer. Sits between accounting and
            # the reports it complements, per the owner's ordering.
            "insights",
            "reports",
            "hr",
            "settings",
        }
        assert set(MODULES_BY_KEY) == expected


class TestShellRendering:
    def test_home_renders_the_shell(self, client: Client, user: User) -> None:
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        assert 'class="ui-app-shell"' in body
        assert 'class="ui-primary-nav"' in body
        assert 'class="ui-secondary-nav"' in body
        assert 'class="ui-app-header"' in body

    def test_html_responses_vary_on_htmx_shape(self, client: Client, user: User) -> None:
        """A shared URL can never cache a fragment as a complete document."""
        client.force_login(user)
        response = client.get(reverse("users:home"))
        vary = {value.strip().lower() for value in response.headers["Vary"].split(",")}
        assert "hx-request" in vary
        assert "hx-target" in vary

    def test_every_module_appears_in_primary_navigation_for_someone_who_may_open_them(
        self, client: Client
    ) -> None:
        """
        The primary navigation shows the whole system to a reader who may open all of it.

        Since ADR-034 navigation is cut down to what the reader may do, so the
        "every module" claim is made about a superuser; the cut itself is
        pinned by `test_navigation_visibility`.
        """
        root = User.objects.create_superuser(username="root", password=PASSWORD)
        client.force_login(root)
        body = client.get(reverse("users:home")).content.decode()
        for module in MODULES:
            assert str(module.label) in body, module.key

    def test_a_reader_with_no_post_sees_only_home_and_the_unbuilt(
        self, client: Client, user: User
    ) -> None:
        """
        No membership, no permission — and therefore no module to open.

        Asserted on primary navigation itself: the home page's build-status panel names
        every module as a statement about the system, and is meant to.
        """
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        primary = body.split('class="ui-primary-nav__list"', 1)[1].split("</ul>", 1)[0]
        for module in MODULES:
            expected = module.key == "home" or not module.available
            assert (str(module.label) in primary) == expected, module.key

    def test_unbuilt_modules_are_marked_as_such(self, client: Client, user: User) -> None:
        """
        Exactly the modules the registry marks unavailable are muted in primary navigation.

        Read out of the navigation data rather than asserted as "at least one",
        because the day the last module lands the old assertion turns into a
        demand that something be left unbuilt. The rule is about the marking
        matching the registry — in both directions — not about progress.
        """
        client.force_login(user)
        body = client.get(reverse("users:home")).content.decode()
        unbuilt = [module for module in MODULES if not module.available]
        assert len(re.findall(r'ui-primary-nav__item[^"]*\bis-unavailable\b', body)) == len(unbuilt)

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
        # Seven foundation screens, the roles screen (ADR-034) and financial
        # periods — which Settings used to advertise as "coming soon" while the
        # Accounting module opened the built screen.
        assert len(available) == 9
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
        # Six foundation screens, the roles screen (ADR-034) and periods.
        assert len(native) == 8
        for section in native:
            url = reverse(str(section.url_name))
            if section.url_name == "accounting:period_list":
                # Deliberately not a settings screen: financial periods are an
                # accounting act, and Settings links to the built screen rather
                # than claiming a second one is coming.
                assert url.startswith("/accounting/")
                continue
            assert url.startswith("/settings/")


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
