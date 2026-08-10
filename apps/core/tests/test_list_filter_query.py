"""
`filter_query`: the pagination carry, now shared by every list screen.

It lives in `apps.core.context_processors` rather than in a list view because
`settings/base_list.html` serves the settings, accounting and inventory lists
alike. A version supplied by only one of them would silently drop the others'
filters the moment somebody paged — which is exactly the bug it replaced:
pagination used to carry `q` and nothing else, so page two of a filtered list
was page two of everything while the toolbar still showed the filter.

These tests live in `apps.core` for the same reason. Testing shared
infrastructure only through the module that happened to need it first is how
the next module inherits the bug.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import parse_qs

import pytest
from django.core.management import call_command
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.core.context_processors import _filter_query
from apps.users.models import User

pytestmark = pytest.mark.django_db

#: Every paginated list, and a filter its own screen accepts.
PAGED_LISTS: list[tuple[str, dict[str, str]]] = [
    ("core:audit_list", {"action": "CREATED"}),
    ("users:user_list", {"q": "demo"}),
    ("units:unit_list", {"dimension": "MASS"}),
]


def page_links(body: str) -> list[str]:
    """The query strings of every pagination link in the rendered page."""
    return [match.replace("&amp;", "&") for match in re.findall(r'href="\?([^"]*page=\d+)"', body)]


# ---------------------------------------------------------------------------
# The function itself
# ---------------------------------------------------------------------------


class TestFilterQuery:
    def test_no_parameters_produces_an_empty_carry(self) -> None:
        """`?page=2` is the right link when there is nothing to carry."""
        assert _filter_query(RequestFactory().get("/anything/")) == ""

    def test_it_ends_in_an_ampersand_so_page_can_follow(self) -> None:
        carry = _filter_query(RequestFactory().get("/anything/?q=rice"))
        assert carry == "q=rice&"
        assert f"?{carry}page=2" == "?q=rice&page=2"

    def test_the_page_parameter_is_never_carried_forward(self) -> None:
        """
        Otherwise `?page=1&page=2` reaches the view, and Django reads the last
        value — so the link would work by accident and break the moment the
        parameter order changed.
        """
        carry = _filter_query(RequestFactory().get("/anything/?q=rice&page=7"))
        assert "page" not in carry
        assert parse_qs(f"{carry}page=2")["page"] == ["2"]

    def test_unrelated_parameters_survive(self) -> None:
        """A later screen's filter must not need this function to be edited."""
        carry = _filter_query(
            RequestFactory().get("/anything/?q=rice&category=FOOD&is_active=true")
        )
        parsed = parse_qs(carry.rstrip("&"))
        assert parsed == {"q": ["rice"], "category": ["FOOD"], "is_active": ["true"]}

    def test_values_are_url_encoded(self) -> None:
        """
        Arabic is the source language, so a search term is usually non-ASCII.

        An unencoded value would break the link, and a space would truncate the
        href at the first gap.
        """
        carry = _filter_query(RequestFactory().get("/anything/", {"q": "رز تجريبي"}))
        assert " " not in carry
        assert parse_qs(carry.rstrip("&"))["q"] == ["رز تجريبي"]

    def test_a_repeated_parameter_keeps_both_values(self) -> None:
        carry = _filter_query(RequestFactory().get("/anything/?tag=a&tag=b"))
        assert parse_qs(carry.rstrip("&"))["tag"] == ["a", "b"]

    def test_the_module_preview_parameter_is_dropped(self) -> None:
        """
        `?module=` only tells the rail which sidebar to preview.

        Carrying it into pagination would pin the sidebar to whatever the user
        was previewing when they first landed, which is not a list filter.
        """
        assert _filter_query(RequestFactory().get("/anything/?module=kitchen")) == ""


# ---------------------------------------------------------------------------
# The screens that use it
# ---------------------------------------------------------------------------


@pytest.fixture
def units() -> None:
    """The standard units, so the unit list has rows to page through."""
    call_command("seed_units", verbosity=0)


@pytest.fixture
def owner(units: None) -> User:
    """Someone who can see every list under test."""
    user = User.objects.create_superuser(username="list-owner", password="pw-not-real-1234")
    return User.objects.get(pk=user.pk)


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login


class TestEveryListFamilyKeepsItsFilters:
    def test_settings_and_reference_lists_carry_their_filter_through_paging(
        self, owner: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        The regression guard.

        These lists are not inventory, and their views set nothing — they get
        the carry from the context processor or not at all. Collected rather
        than asserted one at a time so a failure names every screen that broke.
        """
        client = client_for(owner)
        problems: list[str] = []
        for route, filters in PAGED_LISTS:
            body = client.get(reverse(route), filters).content.decode()
            links = page_links(body)
            if not links:
                continue  # not enough rows to page; nothing to carry
            for key, value in filters.items():
                if not all(f"{key}={value}" in link for link in links):
                    problems.append(f"{route}: {links} dropped {key}={value}")
        assert not problems, "\n".join(problems)

    def test_no_pagination_link_carries_two_page_parameters(
        self, owner: User, client_for: Callable[[User], Client]
    ) -> None:
        """`?page=1&page=2` would work by accident until the order changed."""
        client = client_for(owner)
        for route, filters in [*PAGED_LISTS, ("inventory:movement_list", {})]:
            body = client.get(reverse(route), {**filters, "page": "1"}).content.decode()
            for link in page_links(body):
                assert parse_qs(link)["page"] == [parse_qs(link)["page"][0]]
                assert link.count("page=") == 1, f"{route}: {link}"

    def test_an_accounting_list_carries_its_filter_too(
        self, owner: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        Accounting shares `base_list.html` and sets no carry of its own.

        Asserted on the rendered template rather than on rows, because the
        mapping list may legitimately be short: what matters is that the
        pagination markup is fed from the shared carry.
        """
        response = client_for(owner).get(reverse("accounting:role_list"), {"q": "INVENTORY"})
        assert response.status_code == 200
        assert response.context["filter_query"] == "q=INVENTORY&"

    def test_the_carry_reaches_every_authenticated_render(
        self, owner: User, client_for: Callable[[User], Client]
    ) -> None:
        """A context processor that returned early for some pages would be worse
        than none: the bug would come back on exactly the screens nobody tested."""
        response = client_for(owner).get(reverse("inventory:item_list"), {"q": "DEMO"})
        assert response.context["filter_query"] == "q=DEMO&"

    def test_an_anonymous_render_needs_no_carry(self, client: Client) -> None:
        """The login page has no list, and the processor stays cheap there."""
        response = client.get(reverse("users:login"))
        assert response.status_code == 200
        assert "filter_query" not in response.context or not response.context["filter_query"]


class TestFollowingTheLinkActuallyStaysFiltered:
    """
    End to end, not just the href.

    A carry that reached the link but not the queryset would look completely
    correct in the markup and be wrong in the browser, which is the version of
    this bug that survives review.
    """

    #: `apps.core.views` pages settings lists at 50, so 60 forces a second page.
    MATCHING = 60

    @pytest.fixture
    def crowd(self) -> None:
        """
        Enough matching rows to page, plus some that must not appear.

        Built with `bulk_create` and an unusable password: these are list rows,
        never sign-ins, and hashing sixty-five real passwords costs seconds per
        test for nothing this test asserts.
        """
        User.objects.bulk_create(
            [
                User(username=f"demo-person-{index:03d}", password="!", is_active=True)
                for index in range(self.MATCHING)
            ]
            + [
                User(username=f"other-person-{index:03d}", password="!", is_active=True)
                for index in range(5)
            ]
        )

    def test_page_two_of_a_filtered_list_is_still_filtered(
        self, owner: User, client_for: Callable[[User], Client], crowd: None
    ) -> None:
        client = client_for(owner)
        url = reverse("users:user_list")

        first = client.get(url, {"q": "demo-person"})
        links = page_links(first.content.decode())
        assert links, f"{self.MATCHING} matching rows must produce a second page"
        assert all("q=demo-person" in link for link in links)

        second = client.get(f"{url}?{links[0]}")
        body = second.content.decode()
        assert second.status_code == 200
        assert second.context["search"] == "demo-person"
        # The filter is applied, not merely echoed back into the toolbar.
        assert "other-person-000" not in body
        assert "demo-person-0" in body

    def test_the_unfiltered_list_pages_further_than_the_filtered_one(
        self, owner: User, client_for: Callable[[User], Client], crowd: None
    ) -> None:
        """
        The control.

        Without this, a filter that silently did nothing would still pass the
        test above — both pages would contain everything.
        """
        client = client_for(owner)
        url = reverse("users:user_list")
        everything = client.get(url).context["page_obj"].paginator.count
        filtered = client.get(url, {"q": "demo-person"}).context["page_obj"].paginator.count
        assert filtered == self.MATCHING
        assert everything > filtered
