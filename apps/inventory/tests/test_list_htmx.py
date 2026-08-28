"""
The one htmx interaction on the inventory lists, and the audit that justifies it.

Two jobs. The first is evidence: htmx is vendored, loaded once, and *used* —
claims that would otherwise rest on a file existing on disk. The second is the
contract of the interaction itself, where the failure mode is specific and
nasty: a view that answers an HX-Request with a whole page swaps a second
`<html>` document inside a table cell, and the page keeps working well enough
that nobody notices until the third nesting.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.inventory.models import ItemCategory, ItemType
from apps.inventory.services import create_item
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}

#: Where the vendored copy lives, and the version pinned in ADR-011.
HTMX_PATH = Path(settings.BASE_DIR) / "static" / "vendor" / "htmx.min.js"
HTMX_VERSION = "2.0.4"

#: Every list screen that answers an HX-Request with the results partial.
HTMX_LISTS = [
    "inventory:item_list",
    "inventory:category_list",
    "inventory:package_unit_list",
    "inventory:conversion_list",
    "inventory:warehouse_list",
    "inventory:stock_list",
    "inventory:movement_list",
]


# ---------------------------------------------------------------------------
# §O — the audit, as assertions rather than prose
# ---------------------------------------------------------------------------


class TestHtmxIsVendoredAndLoadedOnce:
    def test_the_vendored_file_is_the_pinned_version(self) -> None:
        """
        Read from the file, not from a comment about it.

        Vendored JS is upgraded deliberately (ADR-011); a version that drifted
        without anyone deciding is exactly what this catches.
        """
        source = HTMX_PATH.read_text(encoding="utf-8", errors="replace")
        assert f'version:"{HTMX_VERSION}"' in source

    def test_no_template_loads_htmx_from_a_cdn(self) -> None:
        """A login page must make no third-party request."""
        for template in Path(settings.BASE_DIR).joinpath("templates").rglob("*.html"):
            body = template.read_text(encoding="utf-8")
            assert "unpkg.com" not in body, template
            assert "cdn.jsdelivr.net" not in body, template
            assert "//cdn." not in body, template

    def test_htmx_is_included_exactly_once(
        self, manager: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        Twice would load two copies and double every request htmx makes.

        Counted in the rendered page rather than in the templates, because
        inheritance is what would cause the duplicate.
        """
        page = client_for(manager).get(reverse("inventory:item_list")).content.decode()
        assert page.count("htmx.min.js") == 1
        assert "vendor/htmx.min.js" in page


class TestHtmxIsActuallyUsed:
    def test_the_list_toolbar_carries_the_hx_attributes(
        self, manager: User, client_for: Callable[[User], Client]
    ) -> None:
        """Loaded is not used. These are the attributes that make it used."""
        page = client_for(manager).get(reverse("inventory:item_list")).content.decode()
        for attribute in ("hx-get", "hx-target", "hx-swap", "hx-trigger", "hx-push-url"):
            assert attribute in page, attribute

    def test_the_swap_target_exists_in_the_page_it_targets(
        self, manager: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        `hx-target="#list-results"` pointing at nothing swaps into the body.

        Asserted together, because the attribute and the element are only
        correct with respect to each other.
        """
        page = client_for(manager).get(reverse("inventory:item_list")).content.decode()
        assert 'hx-target="#list-results"' in page
        assert 'id="list-results"' in page


# ---------------------------------------------------------------------------
# §P — the partial contract
# ---------------------------------------------------------------------------


class TestPartialVersusFullPage:
    @pytest.mark.parametrize("route", HTMX_LISTS)
    def test_a_normal_request_returns_the_whole_page(
        self, manager: User, client_for: Callable[[User], Client], route: str
    ) -> None:
        response = client_for(manager).get(reverse(route))
        body = response.content.decode()
        assert response.status_code == 200
        assert "<html" in body
        assert 'class="ui-app-shell"' in body

    @pytest.mark.parametrize("route", HTMX_LISTS)
    def test_an_hx_request_returns_only_the_results(
        self, manager: User, client_for: Callable[[User], Client], route: str
    ) -> None:
        """
        The partial is the table and nothing else.

        `<html>` in a swap response is the bug this whole test class exists
        for: htmx would put a second document inside the first one's table.
        """
        response = client_for(manager).get(reverse(route), headers=HX)
        body = response.content.decode()
        assert response.status_code == 200
        assert "<html" not in body
        assert "<head" not in body
        assert 'class="ui-app-shell"' not in body
        assert 'class="ui-secondary-nav"' not in body
        assert body.strip().startswith('<section class="ui-data-card" id="list-results"')

    def test_the_partial_is_much_smaller_than_the_page(
        self, manager: User, client_for: Callable[[User], Client]
    ) -> None:
        """The reason for doing this at all: the shell is most of the bytes."""
        client = client_for(manager)
        full = client.get(reverse("inventory:item_list")).content
        partial = client.get(reverse("inventory:item_list"), headers=HX).content
        assert len(partial) * 2 < len(full)

    def test_the_partial_keeps_the_id_the_swap_replaces(
        self, manager: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        `hx-swap="outerHTML"` replaces the target with the response.

        A response that dropped the id would work once and then have nothing
        to swap into: the second filter would silently do nothing.
        """
        body = client_for(manager).get(reverse("inventory:item_list"), headers=HX).content.decode()
        assert 'id="list-results"' in body

    def test_a_settings_list_is_untouched_by_any_of_this(
        self, superuser: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        The shared base also serves lists whose views do not answer result
        partials. The global shell may use htmx navigation, but this form must
        not target ``#list-results`` or swap a whole page into the register.
        """
        body = (
            client_for(superuser).get(reverse("organizations:organization_list")).content.decode()
        )
        assert "<html" in body
        assert 'hx-target="#list-results"' not in body


class TestAuthorizationIsIdentical:
    @pytest.fixture
    def cashier(self, branch: Branch) -> User:
        """Holds a post at the branch, but no inventory permission at all."""
        user = User.objects.create_user(username="till", password="pw-not-real-1234")
        grant_branch_access(user=user, branch=branch, role=Role.CASHIER)
        return User.objects.get(pk=user.pk)

    def test_an_anonymous_hx_request_is_redirected_like_any_other(self, client: Client) -> None:
        """
        A header does not authenticate anything.

        The interesting failure is a view that skips the login mixin on the
        partial path because "it is only a fragment".
        """
        response = client.get(reverse("inventory:item_list"), headers=HX)
        assert response.status_code == 302
        assert reverse("users:login") in response["Location"]

    def test_a_user_without_the_permission_is_refused_on_both_paths(
        self, cashier: User, client_for: Callable[[User], Client]
    ) -> None:
        client = client_for(cashier)
        normal = client.get(reverse("inventory:item_list"))
        partial = client.get(reverse("inventory:item_list"), headers=HX)
        assert normal.status_code == partial.status_code == 403

    def test_out_of_scope_rows_are_absent_from_the_partial_too(
        self,
        manager: User,
        rival_manager: User,
        client_for: Callable[[User], Client],
        rice: object,
    ) -> None:
        """Scope is enforced by the selector, which both paths share."""
        body = (
            client_for(rival_manager)
            .get(reverse("inventory:item_list"), headers=HX)
            .content.decode()
        )
        assert "RICE-272" not in body


class TestValuationRedactionSurvivesTheSwap:
    """
    §P's sharpest requirement.

    A partial rendered by a different code path is exactly where a redaction
    gets forgotten — the cost columns are conditional on `show_cost`, and a
    hand-written fragment that hard-coded the table would leak them.
    """

    def test_a_caller_with_view_valuation_sees_cost_in_the_partial(
        self, accounting_manager: User, client_for: Callable[[User], Client]
    ) -> None:
        body = (
            client_for(accounting_manager)
            .get(reverse("inventory:stock_list"), headers=HX)
            .content.decode()
        )
        assert "متوسط الكلفة" in body

    def test_a_caller_without_it_gets_no_cost_column_in_the_partial(
        self, storekeeper: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        Omitted, not blanked: an empty cell still says a number belongs there.
        """
        body = (
            client_for(storekeeper)
            .get(reverse("inventory:stock_list"), headers=HX)
            .content.decode()
        )
        assert "متوسط الكلفة" not in body
        assert "القيمة" not in body

    def test_the_partial_redacts_exactly_as_the_full_page_does(
        self, storekeeper: User, client_for: Callable[[User], Client]
    ) -> None:
        """The two paths must not disagree about what is secret."""
        client = client_for(storekeeper)
        full = client.get(reverse("inventory:stock_list")).content.decode()
        partial = client.get(reverse("inventory:stock_list"), headers=HX).content.decode()
        assert ("متوسط الكلفة" in full) == ("متوسط الكلفة" in partial)


class TestFilteringAndPaging:
    @pytest.fixture
    def many_items(
        self,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        """Enough rows to page, with two distinguishable names."""
        for index in range(30):
            create_item(
                organization=organization,
                code=f"BULK-{index:03d}",
                name=f"صنف {index}",
                category=leaf_category,
                item_type=ItemType.RAW_MATERIAL,
                base_unit=kilogram,
            )

    def test_the_search_narrows_the_partial(
        self, manager: User, client_for: Callable[[User], Client], rice: object, many_items: None
    ) -> None:
        body = (
            client_for(manager)
            .get(reverse("inventory:item_list"), {"q": "RICE"}, headers=HX)
            .content.decode()
        )
        assert "RICE-272" in body
        assert "BULK-000" not in body

    def test_paging_keeps_the_filter(
        self, manager: User, client_for: Callable[[User], Client], many_items: None
    ) -> None:
        """
        The bug this fixes: paging used to carry only `q`.

        Page two of a filtered list silently became page two of everything,
        and the toolbar still showed the filter that was no longer applied.
        """
        response = client_for(manager).get(
            reverse("inventory:item_list"), {"q": "BULK", "page": "1"}, headers=HX
        )
        body = response.content.decode()
        links = re.findall(r'hx-get="\?([^"]+)"', body)
        assert links, "a paged list must offer a next page"
        assert all("q=BULK" in link for link in links), links

    def test_the_pagination_links_target_the_results_only(
        self, manager: User, client_for: Callable[[User], Client], many_items: None
    ) -> None:
        body = client_for(manager).get(reverse("inventory:item_list"), headers=HX).content.decode()
        assert 'hx-target="#list-results"' in body

    def test_the_form_still_works_without_javascript(
        self, manager: User, client_for: Callable[[User], Client], rice: object, many_items: None
    ) -> None:
        """
        Progressive enhancement, not dependence.

        The toolbar is still a GET form with a submit button; htmx intercepts
        it when present and the browser submits it when not.
        """
        body = client_for(manager).get(reverse("inventory:item_list")).content.decode()
        assert '<form class="ui-filter-bar" method="get"' in body

        plain = client_for(manager).get(reverse("inventory:item_list"), {"q": "RICE"})
        assert plain.status_code == 200
        assert "RICE-272" in plain.content.decode()
        assert "BULK-000" not in plain.content.decode()
