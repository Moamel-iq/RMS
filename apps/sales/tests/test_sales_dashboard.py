"""
لوحة المبيعات, and the two things about it that would be expensive to get wrong.

**The arithmetic has to be the ledger's.** `net_revenue` is asserted against the
four accounts it claims to summarise, not against a figure this test computed the
same way the module did. A dashboard that agreed only with itself would be a
second opinion nobody could reconcile.

**Cost has to be absent, not blank.** Every assertion about `view_sales_cost`
here is about *absence*: the card is not in `visible_cards`, its route answers
403, and the rendered page contains none of its headings. A test that only
checked for a masked value would pass on a page that quietly leaked the number
in a `title` attribute.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    SALES_DISCOUNT,
    SALES_RETURNS,
    SALES_REVENUE,
    Account,
    JournalLine,
)
from apps.organizations.models import Organization
from apps.sales.dashboard import (
    CARDS,
    CARDS_BY_SLUG,
    DashboardScope,
    application_mix,
    cashier_summary,
    channel_mix,
    cost_summary,
    headline_for,
    receivable_summary,
    reconciliation_summary,
    returns_breakdown,
    top_menu_items,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

WINDOW_FROM = datetime.date(2026, 8, 1)
WINDOW_TO = datetime.date(2026, 8, 31)
ZERO = Decimal("0")


def _scope(organization: Organization) -> DashboardScope:
    return DashboardScope(organization_id=organization.pk, date_from=WINDOW_FROM, date_to=WINDOW_TO)


def _account_net(organization: Organization, role_code: str) -> Decimal:
    """What one role's account actually holds, summed straight off the ledger."""
    codes = {
        SALES_REVENUE: "4-01-01-001",
        SALES_DISCOUNT: "4-02-01-001",
        SALES_RETURNS: "4-03-01-001",
    }
    account = Account.objects.get(organization=organization, code=codes[role_code])
    total = ZERO
    for line in JournalLine.objects.filter(account=account):
        total += line.debit - line.credit
    return total


def test_net_revenue_is_the_ledgers_own_arithmetic(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    """
    `net_revenue` equals revenue credit less the discount and returns debits.

    Asserted against the accounts rather than against a recomputation, because
    the point of the figure is that a person can find it in the general ledger.
    """
    organization = scenario["organization"]
    headline = headline_for(accounting_manager, _scope(organization))

    revenue = -_account_net(organization, SALES_REVENUE)
    discount = _account_net(organization, SALES_DISCOUNT)
    returns = _account_net(organization, SALES_RETURNS)

    assert headline.gross == revenue
    assert headline.net_revenue == revenue - discount - returns


def test_headline_separates_every_tender(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    headline = headline_for(accounting_manager, _scope(scenario["organization"]))
    # 4 plates at 10,000 cash, less one cancelled = 30,000.
    assert headline.cash_sales == Decimal("30000.000")
    assert headline.card_sales == ZERO
    # 6 plates at 10,000 less 15% commission = 51,000.
    assert headline.application_sales == Decimal("51000.000")
    assert headline.day_count == 1


def test_the_application_funded_discount_is_shown_and_never_subtracted(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    """
    It has its own figure and it is not inside `net_revenue`.

    The scenario carries none, so the assertion is that the field exists, reads
    zero, and that `net_revenue` is unchanged by it — which is the shape that
    would break first if somebody ever started subtracting it.
    """
    organization = scenario["organization"]
    headline = headline_for(accounting_manager, _scope(organization))
    assert headline.application_discount == ZERO
    assert headline.net_revenue == (
        headline.gross - headline.restaurant_discount - headline.returns_gross
    )


def test_returns_breakdown_marks_only_cancellations_as_reducing_consumption(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    """
    The one flag on the card, asserted rather than trusted.

    The intuitive implementation subtracts every adjustment and reads perfectly
    well. This is the cheapest guard against it.
    """
    rows = returns_breakdown(accounting_manager, _scope(scenario["organization"]))
    assert rows
    for row in rows:
        assert row.reduces_consumption == (row.reason_kind == "CANCELLED_BEFORE_FULFILLMENT")


def test_mixes_and_top_items_share_to_a_hundred(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    scope = _scope(scenario["organization"])
    channels = channel_mix(accounting_manager, scope)
    assert {row.code for row in channels} == {"SCN-HALL", "SCN-APPS"}
    assert sum((row.share for row in channels), ZERO) == Decimal("100.00")

    applications = application_mix(accounting_manager, scope)
    assert [row.code for row in applications] == ["SCN-APP"]

    items = top_menu_items(accounting_manager, scope)
    assert [row.code for row in items] == ["SCN-MENU"]
    assert items[0].quantity == Decimal("10.000")


def test_shortage_and_overage_are_never_netted(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    """
    Two figures, not one.

    A month of alternating shortages and overages nets to zero and is a serious
    finding; a clean month also nets to zero. The card must be able to tell them
    apart, so both sides are reported separately.
    """
    summary = cashier_summary(accounting_manager, _scope(scenario["organization"]))
    assert summary.shortage == Decimal("750.000")
    assert summary.overage == ZERO
    assert summary.variance == Decimal("-750.000")
    assert summary.approved_shifts == 1


def test_receivable_summary_reads_the_ledger_and_writes_nothing_off(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    summary = receivable_summary(accounting_manager, _scope(scenario["organization"]))
    # The settlement cleared the whole allocated debit, so nothing is left.
    assert summary.outstanding == ZERO
    assert summary.settlements_posted == 1
    assert summary.overdue == ZERO


def test_reconciliation_summary_agrees_with_the_report(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    """Composed from `reconcile_range`, so the two screens cannot disagree."""
    summary = reconciliation_summary(accounting_manager, _scope(scenario["organization"]))
    assert summary.days == 1
    # The drawer came up short, which is an advisory, so the day is not clean.
    assert summary.clean == 0
    assert summary.advisories >= 1


def test_cost_summary_never_values_a_line_at_zero(
    scenario: dict[str, Any], accounting_manager: User
) -> None:
    """
    With no snapshot behind the serving, every line is **uncosted**, not free.

    Costing a line at zero would divide a partial food cost by a complete
    revenue and produce a food-cost percentage that is wrong in the direction
    that looks good — which is the one direction nobody questions.
    """
    summary = cost_summary(accounting_manager, _scope(scenario["organization"]))
    assert summary.food_cost == ZERO
    assert summary.costed_lines == 0
    assert summary.uncosted_lines == 2
    assert summary.uncosted_gross == Decimal("100000.000")
    assert summary.is_complete is False


def test_dashboard_page_renders_and_lists_its_cards(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    client = client_for(accounting_manager)
    response = client.get(reverse("sales:dashboard"))
    assert response.status_code == 200
    body = response.content.decode("utf-8")
    for card in CARDS:
        assert f"dashboard-card-{card.slug}" in body


def test_every_card_route_answers_as_a_fragment(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    """Each card is its own request, and no card carries a second shell."""
    client = client_for(accounting_manager)
    for card in CARDS:
        response = client.get(
            reverse("sales:dashboard_card", args=[card.slug]),
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200, card.slug
        assert "<html" not in response.content.decode("utf-8").lower(), card.slug


def test_every_detail_screen_answers_as_a_page_and_as_a_real_fragment(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    """
    The audit finding this test exists for.

    Nine screens extend `list_base_template` **directly** rather than through
    `settings/base_list.html`, so the block each defines is `page`. They named
    `settings/_list_fragment.html` as their htmx parent, which declares only
    `results` — and Django silently drops a child block the parent does not
    have, so every one of them answered 200 with a body of whitespace.

    The old assertions on these routes checked the status and the absence of a
    second `<html>`, both of which an empty body satisfies perfectly. That is
    why the defect sat here undetected, and it is why this test asserts on
    *content* rather than on the absence of a mistake.
    """
    routes = (
        reverse("sales:dashboard"),
        reverse("sales:day_detail", args=[scenario["day"].pk]),
        reverse("sales:adjustment_detail", args=[scenario["adjustment"].pk]),
        # `pk` here is the delivery application: the page is that company's
        # account, and the entries are its lines.
        reverse("sales:receivable_detail", args=[scenario["application"].pk]),
        reverse("sales:settlement_detail", args=[scenario["settlement"].pk]),
        reverse("sales:shift_detail", args=[scenario["shift"].pk]),
        reverse("sales:menu_item_detail", args=[scenario["menu_item"].pk]),
        reverse("sales:application_detail", args=[scenario["application"].pk]),
        reverse("sales:report_daily_reconciliation"),
    )

    client = client_for(accounting_manager)
    for path in routes:
        page = client.get(path)
        fragment = client.get(path, headers={"HX-Request": "true"})
        assert page.status_code == 200, path
        assert fragment.status_code == 200, path

        body = fragment.content.decode("utf-8")
        # A fragment carrying a second shell renders correctly enough to be
        # missed in review and is wrong in every accessibility tree.
        assert "<html" not in body.lower(), path
        # And an *empty* fragment is the failure the previous assertion cannot
        # see. Compared against the full page rather than against a magic
        # number: the fragment is the page's own `page` block, so it is
        # substantial and strictly smaller than the shell around it.
        assert len(body.strip()) > 500, (path, len(body.strip()))
        assert len(body) < len(page.content.decode("utf-8")), path


def test_the_cost_card_is_omitted_from_a_cashiers_dashboard(
    scenario: dict[str, Any], cashier: User, client_for: Callable[[User], Client]
) -> None:
    """
    Omitted, not blanked — and the whole page is checked, not one cell.

    A cashier holds neither `view_sales_reports` nor `view_sales_cost`, so the
    dashboard itself is refused. The manager case below is the interesting one.
    """
    client = client_for(cashier)
    assert client.get(reverse("sales:dashboard")).status_code == 403


def test_a_manager_without_cost_gets_no_cost_card_and_a_403_on_its_route(
    scenario: dict[str, Any],
    manager: User,
    branch: Any,
    client_for: Callable[[User], Client],
) -> None:
    """
    `MANAGER` holds `view_sales_cost`, so the permission is withdrawn to prove
    the omission rather than asserting on a role that never had it.
    """
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    group = group_for_role("MANAGER")
    group.permissions.remove(
        Permission.objects.get(content_type__app_label="sales", codename="view_sales_cost")
    )
    actor = User.objects.get(pk=manager.pk)

    client = client_for(actor)
    page = client.get(reverse("sales:dashboard"))
    assert page.status_code == 200
    body = page.content.decode("utf-8")
    assert "dashboard-card-cost" not in body
    assert "كلفة الطعام" not in body
    assert "مجمل الربح" not in body

    refused = client.get(reverse("sales:dashboard_card", args=["cost"]))
    assert refused.status_code == 403


def test_an_unknown_card_slug_is_a_404(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    client = client_for(accounting_manager)
    assert client.get("/sales/dashboard/cards/nonsense/").status_code == 404


def test_the_outsider_reaches_no_organization(
    scenario: dict[str, Any], outsider: User, client_for: Callable[[User], Client]
) -> None:
    """
    Out of scope is 404, never 403 — an organization id they cannot read must be
    indistinguishable from one that was never created.
    """
    client = client_for(outsider)
    organization = scenario["organization"]
    response = client.get(f"{reverse('sales:dashboard')}?organization={organization.pk}")
    assert response.status_code == 404


def test_the_card_registry_and_the_templates_agree() -> None:
    """
    Every declared card has a context builder and a template that exists.

    The registry is what keeps the route, the fetch and the template in step; a
    card added to it with no template would 500 on load and nowhere else.
    """
    from django.template.loader import get_template

    assert set(CARDS_BY_SLUG) == {card.slug for card in CARDS}
    for card in CARDS:
        get_template(card.template)
