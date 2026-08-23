"""
Contracts for the inventory overview screen.

The interesting case is not that the numbers are right; it is that the *value*
figures disappear for a caller without `inventory.view_valuation`, and that
they disappear rather than becoming zero. A dashboard that renders `0` for a
redacted total is worse than one that errors: the storekeeper reads it as
"there is nothing here" and nobody finds out until they say so out loud.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounting.models import AccountingPeriod
from apps.accounting.services import open_fiscal_year
from apps.inventory.dashboard import inventory_overview
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import InventoryItem, MovementType, Warehouse
from apps.organizations.models import Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def open_period(organization: Organization) -> AccountingPeriod:
    """Posting stock needs an OPEN period, opened through the real service."""
    from django.utils import timezone

    today = timezone.localdate()
    open_fiscal_year(organization=organization, year=today.year)
    return AccountingPeriod.objects.get(
        fiscal_year__organization=organization,
        start_date__lte=today,
        end_date__gte=today,
    )


def _stock(
    organization: Organization,
    warehouse: Warehouse,
    item: InventoryItem,
    *,
    quantity: str = "10",
    unit_cost: str = "2500",
) -> None:
    post_stock_entry(
        organization=organization,
        effects=[
            MovementInput(
                warehouse=warehouse,
                item=item,
                movement_type=MovementType.RECEIPT,
                quantity=Decimal(quantity),
                unit_cost=Decimal(unit_cost),
                effect_key="overview:1",
            )
        ],
        idempotency_key="overview-seed",
    )


def test_the_overview_counts_only_what_the_caller_can_reach(
    organization: Organization,
    main_store: Warehouse,
    rice: InventoryItem,
    manager: User,
    rival_manager: User,
    open_period: AccountingPeriod,
) -> None:
    _stock(organization, main_store, rice)

    mine = inventory_overview(manager, include_valuation=True)
    theirs = inventory_overview(rival_manager, include_valuation=True)

    assert mine.stocked_item_count == 1
    assert mine.movement_count == 1
    assert mine.total_value == Decimal("25000.000")

    # Another organization's manager reaches no warehouse here, so the screen
    # is empty rather than showing a total they are not entitled to.
    assert theirs.stocked_item_count == 0
    assert theirs.movement_count == 0
    assert theirs.rows == []


def test_without_valuation_the_value_is_absent_not_zero(
    organization: Organization,
    main_store: Warehouse,
    rice: InventoryItem,
    open_period: AccountingPeriod,
    manager: User,
) -> None:
    _stock(organization, main_store, rice)

    redacted = inventory_overview(manager, include_valuation=False)

    # None, not Decimal("0") — the template tests `is not None` and drops the
    # card, and a zero would have rendered as a real figure.
    assert redacted.total_value is None
    assert redacted.type_slices == []
    assert [row.value for row in redacted.rows] == [None]
    assert [row.unit_cost for row in redacted.rows] == [None]
    # The quantity is not a cost and stays visible: a storekeeper still counts.
    assert redacted.rows[0].quantity == Decimal("10.000")
    assert redacted.stocked_item_count == 1


def test_the_storekeeper_screen_carries_no_cost_column(
    organization: Organization,
    main_store: Warehouse,
    rice: InventoryItem,
    storekeeper: User,
    manager: User,
    client_for: Callable[[User], Client],
    open_period: AccountingPeriod,
) -> None:
    _stock(organization, main_store, rice)
    url = reverse("inventory:overview")

    keeper_body = client_for(storekeeper).get(url).content.decode()
    manager_body = client_for(manager).get(url).content.decode()

    assert "قيمة المخزون" not in keeper_body
    assert "كلفة الوحدة" not in keeper_body
    # The redacted screen must not print a stand-in figure for the total.
    assert "25,000" not in keeper_body
    # The same screen, for someone entitled to the figure, does show it.
    assert "قيمة المخزون" in manager_body
    assert "25,000" in manager_body


def test_the_unstocked_alert_appears_only_when_it_is_true(
    organization: Organization,
    main_store: Warehouse,
    rice: InventoryItem,
    open_period: AccountingPeriod,
    manager: User,
) -> None:
    # One active item, no movement yet: every active item is unstocked.
    before = inventory_overview(manager, include_valuation=True)
    assert before.unstocked_item_count == before.active_item_count == 1

    _stock(organization, main_store, rice)

    after = inventory_overview(manager, include_valuation=True)
    assert after.unstocked_item_count == 0
    assert after.stocked_share == Decimal("100")


def test_a_balance_drained_to_zero_is_not_stock_on_hand(
    organization: Organization,
    main_store: Warehouse,
    rice: InventoryItem,
    open_period: AccountingPeriod,
    manager: User,
) -> None:
    _stock(organization, main_store, rice, quantity="4")
    post_stock_entry(
        organization=organization,
        effects=[
            MovementInput(
                warehouse=main_store,
                item=rice,
                movement_type=MovementType.ISSUE,
                quantity=Decimal("4"),
                effect_key="overview:drain",
            )
        ],
        idempotency_key="overview-drain",
    )

    overview = inventory_overview(manager, include_valuation=True)

    # The row still exists in the ledger; it is not stock, and it must not
    # count toward "items with a balance" or appear in the table.
    assert overview.stocked_item_count == 0
    assert overview.rows == []
    assert overview.movement_count == 2


def test_the_supplier_mix_is_borrowed_on_procurements_terms(
    organization: Organization,
    main_store: Warehouse,
    rice: InventoryItem,
    storekeeper: User,
    manager: User,
    client_for: Callable[[User], Client],
    open_period: AccountingPeriod,
) -> None:
    """
    The stock screen may show where stock came from, but only to a caller
    Procurement itself would show it to. With no posted invoice the panel is
    simply absent for everyone; the gate is still the caller's post.
    """
    _stock(organization, main_store, rice)
    url = reverse("inventory:overview")

    keeper_body = client_for(storekeeper).get(url).content.decode()
    manager_body = client_for(manager).get(url).content.decode()

    # No invoice is posted here, so the panel has no rows and renders for
    # nobody — and the storekeeper would not see it even if it had rows.
    assert "المشتريات حسب المورد" not in keeper_body
    assert "المشتريات حسب المورد" not in manager_body
