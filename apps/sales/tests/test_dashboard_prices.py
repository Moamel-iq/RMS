"""
Contracts for the أسعار التطبيقات card.

The card reads price lists, not sales lines, so it has an answer on a day with
no posted sale — which is exactly when the owner wants to see it. Two rules
hold it together: only a channel price *above* the hall price is a premium,
and the pair is the unit, so items sharing one pair share one tile.

Built on the master-data fixtures alone, deliberately not on `scenario`: a
price list needs no posted day, and the posted-day fixture currently trips a
direct-stock guard at teardown that has nothing to do with prices.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest

from apps.accounting.models import CostCenter
from apps.organizations.models import Branch, Organization
from apps.sales.dashboard import DashboardScope, price_premiums
from apps.sales.models import PriceScope, SalesChannel, SalesChannelCategory, TenderDestination
from apps.sales.services import (
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_branch_availability,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

JANUARY = datetime.date(2026, 1, 1)
JUNE = datetime.date(2026, 6, 1)


@pytest.fixture
def apps_channel(organization: Organization, delivery_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="PRC-APPS",
        name="التطبيقات",
        category=SalesChannelCategory.DELIVERY_APPLICATION,
        cost_center=delivery_cost_center,
        default_tender=TenderDestination.APPLICATION_RECEIVABLE,
    )


def _item(organization: Organization, branch: Branch, recipe: Any, code: str, name: str) -> Any:
    item = create_menu_item(
        organization=organization, code=code, name=name, recipe=recipe, serving_code="WHOLE"
    )
    set_branch_availability(item=item, branch=branch)
    return item


def _price(item: Any, branch: Branch, amount: str, **scope: Any) -> None:
    create_menu_price(
        menu_item=item,
        branch=branch,
        unit_price=Decimal(amount),
        effective_from=JANUARY,
        **scope,
    )


def _scope(organization: Organization, on: datetime.date) -> DashboardScope:
    return DashboardScope(organization_id=organization.pk, date_from=on, date_to=on)


def test_a_dearer_channel_price_is_a_premium_and_an_equal_one_is_not(
    organization: Organization,
    branch: Branch,
    scenario_recipe: Any,
    apps_channel: SalesChannel,
    manager: User,
) -> None:
    mandi = _item(organization, branch, scenario_recipe, "PRC-MANDI", "مندي")
    _price(mandi, branch, "10000")
    _price(mandi, branch, "12000", scope=PriceScope.CHANNEL, channel=apps_channel)

    # A second item whose channel price equals its hall price: not a premium.
    water = _item(organization, branch, scenario_recipe, "PRC-WATER", "ماء")
    _price(water, branch, "500")
    _price(water, branch, "500", scope=PriceScope.CHANNEL, channel=apps_channel)

    rows = price_premiums(manager, _scope(organization, JUNE))

    assert [(row.base_price, row.channel_price, row.items) for row in rows] == [
        (Decimal("10000.000000"), Decimal("12000.000000"), ("مندي",))
    ]
    assert rows[0].premium_share == Decimal("20.0")


def test_items_on_the_same_pair_share_one_tile(
    organization: Organization,
    branch: Branch,
    scenario_recipe: Any,
    apps_channel: SalesChannel,
    manager: User,
) -> None:
    for code, name in (("PRC-A", "مندي لحم"), ("PRC-B", "مدفون لحم")):
        item = _item(organization, branch, scenario_recipe, code, name)
        _price(item, branch, "23000")
        _price(item, branch, "25000", scope=PriceScope.CHANNEL, channel=apps_channel)

    rows = price_premiums(manager, _scope(organization, JUNE))

    assert len(rows) == 1
    assert rows[0].items == ("مندي لحم", "مدفون لحم")


def test_a_channel_price_not_yet_in_force_is_not_counted(
    organization: Organization,
    branch: Branch,
    scenario_recipe: Any,
    apps_channel: SalesChannel,
    manager: User,
) -> None:
    mandi = _item(organization, branch, scenario_recipe, "PRC-LATER", "مندي")
    _price(mandi, branch, "10000")
    create_menu_price(
        menu_item=mandi,
        branch=branch,
        unit_price=Decimal("12000"),
        effective_from=datetime.date(2026, 9, 1),
        scope=PriceScope.CHANNEL,
        channel=apps_channel,
    )

    # Asked about June, a September price is a plan, not a premium.
    assert price_premiums(manager, _scope(organization, JUNE)) == []
    assert len(price_premiums(manager, _scope(organization, datetime.date(2026, 9, 1)))) == 1
