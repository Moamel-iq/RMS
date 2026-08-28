"""
Inventory account-mapping overrides: precedence, immutability, and the
reclassification guard (Task 1.3 §V 5–9, 13–15).

The resolver walks item → nearest category ancestor → organization default →
`account_role_unmapped`. The guard refuses any mapping change that would
silently re-home the standing value of an item with stock.
"""

from __future__ import annotations

import datetime
import zoneinfo
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_OPENING_EQUITY,
    Account,
    AccountRole,
    OrganizationAccountMapping,
)
from apps.accounting.services import (
    close_account_mapping,
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.core.context import audit_context
from apps.inventory.accounts import (
    archive_inventory_mapping,
    close_inventory_mapping,
    create_inventory_mapping,
    resolve_inventory_account,
)
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import (
    InventoryAccountMapping,
    InventoryItem,
    ItemCategory,
    MovementType,
    Warehouse,
)
from apps.inventory.services import create_item_category, update_item
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
MAR_15 = datetime.date(TEST_YEAR, 3, 15)


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def control_role() -> AccountRole:
    return AccountRole.objects.get(code=INVENTORY_CONTROL)


@pytest.fixture
def default_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-01-001")


@pytest.fixture
def override_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-02-001")


@pytest.fixture
def third_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-01-02-001")


@pytest.fixture
def default_mapping(
    organization: Organization, control_role: AccountRole, default_account: Account
) -> object:
    return create_account_mapping(
        organization=organization,
        account_role=control_role,
        account=default_account,
        effective_from=JAN_1,
    )


@pytest.fixture
def equity_mapped(organization: Organization, accounting: None) -> None:
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_OPENING_EQUITY),
        account=Account.objects.get(organization=organization, code="3-02-01-001"),
        effective_from=JAN_1,
    )


def _resolve(organization: Organization, item: InventoryItem) -> Account:
    return resolve_inventory_account(
        organization=organization, role=INVENTORY_CONTROL, item=item, on_date=MAR_15
    ).account


class TestOverrideShape:
    def test_exactly_one_target_is_required(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        rice: InventoryItem,
        leaf_category: ItemCategory,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_inventory_mapping(
                organization=organization,
                role=control_role,
                account=override_account,
                effective_from=JAN_1,
            )
        assert caught.value.code == "override_target_required"
        with pytest.raises(ValidationError) as caught:
            create_inventory_mapping(
                organization=organization,
                role=control_role,
                account=override_account,
                item=rice,
                category=leaf_category,
                effective_from=JAN_1,
            )
        assert caught.value.code == "override_target_required"

    def test_a_non_overridable_role_is_refused(
        self,
        organization: Organization,
        override_account: Account,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_inventory_mapping(
                organization=organization,
                role=INVENTORY_OPENING_EQUITY,
                account=override_account,
                item=rice,
                effective_from=JAN_1,
            )
        assert caught.value.code == "role_not_overridable"

    def test_item_override_ranges_cannot_overlap(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        third_account: Account,
        rice: InventoryItem,
    ) -> None:
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            item=rice,
            effective_from=JAN_1,
        )
        with pytest.raises(ValidationError) as caught:
            create_inventory_mapping(
                organization=organization,
                role=control_role,
                account=third_account,
                item=rice,
                effective_from=MAR_15,
            )
        assert caught.value.code == "mapping_period_overlaps"

    def test_category_override_ranges_cannot_overlap_even_at_the_database(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        third_account: Account,
        leaf_category: ItemCategory,
    ) -> None:
        """The EXCLUDE constraint must catch two category rows despite the
        NULL item column — the COALESCE is what makes NULL comparable."""
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            category=leaf_category,
            effective_from=JAN_1,
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                InventoryAccountMapping.objects.create(
                    organization=organization,
                    account_role=control_role,
                    account=third_account,
                    category=leaf_category,
                    effective_from=MAR_15,
                    version=99,
                )


class TestResolverPrecedence:
    def test_the_item_mapping_wins_over_everything(
        self,
        organization: Organization,
        control_role: AccountRole,
        default_mapping: OrganizationAccountMapping,
        override_account: Account,
        third_account: Account,
        rice: InventoryItem,
        leaf_category: ItemCategory,
    ) -> None:
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=third_account,
            category=leaf_category,
            effective_from=JAN_1,
        )
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            item=rice,
            effective_from=JAN_1,
        )
        assert _resolve(organization, rice) == override_account

    def test_the_nearest_category_ancestor_wins(
        self,
        organization: Organization,
        control_role: AccountRole,
        default_mapping: OrganizationAccountMapping,
        override_account: Account,
        third_account: Account,
        rice: InventoryItem,
        category: ItemCategory,
        leaf_category: ItemCategory,
    ) -> None:
        """Rice sits in MEAT under FOOD. A mapping on both must resolve to
        MEAT — the nearest — not FOOD, and not the organization default."""
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=third_account,
            category=category,
            effective_from=JAN_1,
        )
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            category=leaf_category,
            effective_from=JAN_1,
        )
        assert _resolve(organization, rice) == override_account

    def test_an_ancestor_category_covers_grandchildren(
        self,
        organization: Organization,
        control_role: AccountRole,
        default_mapping: OrganizationAccountMapping,
        override_account: Account,
        rice: InventoryItem,
        category: ItemCategory,
    ) -> None:
        """A mapping on FOOD reaches an item filed under FOOD > MEAT."""
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            category=category,
            effective_from=JAN_1,
        )
        assert _resolve(organization, rice) == override_account

    def test_the_category_wins_over_the_organization_default(
        self,
        organization: Organization,
        control_role: AccountRole,
        default_mapping: OrganizationAccountMapping,
        default_account: Account,
        override_account: Account,
        rice: InventoryItem,
        leaf_category: ItemCategory,
    ) -> None:
        assert _resolve(organization, rice) == default_account
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            category=leaf_category,
            effective_from=JAN_1,
        )
        assert _resolve(organization, rice) == override_account

    def test_a_missing_mapping_fails_explicitly(
        self, organization: Organization, rice: InventoryItem, accounting: None
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            _resolve(organization, rice)
        assert caught.value.code == "account_role_unmapped"


class TestUsedOverridesAreImmutable:
    def test_a_used_override_cannot_be_archived(
        self,
        organization: Organization,
        branch: Branch,
        control_role: AccountRole,
        default_mapping: OrganizationAccountMapping,
        equity_mapped: None,
        override_account: Account,
        rice: InventoryItem,
        main_store: Warehouse,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """Posting an opening through an override freezes it; the correction
        is closing its range, which stays permitted."""
        from apps.inventory.commands import (
            add_opening_line,
            create_opening,
            post_opening,
            submit_opening,
        )
        from apps.inventory.opening import OpeningLineInput

        override = create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            item=rice,
            effective_from=JAN_1,
        )
        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD),
            evidence_reference="SHEET",
        )
        add_opening_line(
            actor=manager,
            document=document,
            line=OpeningLineInput(
                warehouse=main_store,
                item=rice,
                base_quantity=Decimal("10"),
                unit_cost=Decimal("1000"),
            ),
        )
        submit_opening(actor=manager, document=document)
        post_opening(actor=accounting_manager, document=document)

        with pytest.raises(ValidationError) as caught:
            archive_inventory_mapping(mapping=override, reason="tidy up")
        assert caught.value.code == "mapping_in_use"


class TestTheReclassificationGuard:
    """§G: no mapping change may silently re-home standing stock value."""

    @pytest.fixture
    def stocked_rice(
        self,
        organization: Organization,
        rice: InventoryItem,
        main_store: Warehouse,
        default_mapping: OrganizationAccountMapping,
        accounting: None,
        superuser: User,
    ) -> InventoryItem:
        """Rice with a real posted balance, resolved through the default."""
        with audit_context(actor=superuser):
            post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("50"),
                        effect_key="stock:1",
                        unit_cost=Decimal("1200"),
                    )
                ],
                idempotency_key="stock-for-guard",
                effective_at=datetime.datetime(TEST_YEAR, 3, 10, 12, 0, tzinfo=BAGHDAD),
            )
        return rice

    def test_an_item_override_that_would_move_stock_is_rejected(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        stocked_rice: InventoryItem,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_inventory_mapping(
                organization=organization,
                role=control_role,
                account=override_account,
                item=stocked_rice,
                effective_from=JAN_1,
            )
        assert caught.value.code == "inventory_account_reclassification_required"
        # Rolled back: the refused mapping does not exist.
        assert InventoryAccountMapping.objects.count() == 0

    def test_a_category_override_that_would_move_stock_is_rejected(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        stocked_rice: InventoryItem,
        leaf_category: ItemCategory,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_inventory_mapping(
                organization=organization,
                role=control_role,
                account=override_account,
                category=leaf_category,
                effective_from=JAN_1,
            )
        assert caught.value.code == "inventory_account_reclassification_required"

    def test_an_override_matching_the_current_account_is_permitted(
        self,
        organization: Organization,
        control_role: AccountRole,
        default_account: Account,
        stocked_rice: InventoryItem,
    ) -> None:
        """Same resolved account, so nothing is re-homed: allowed."""
        mapping = create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=default_account,
            item=stocked_rice,
            effective_from=JAN_1,
        )
        assert mapping.pk is not None

    def test_closing_an_override_that_stock_relies_on_is_rejected(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        rice: InventoryItem,
        main_store: Warehouse,
        default_mapping: OrganizationAccountMapping,
        superuser: User,
    ) -> None:
        """The override exists BEFORE stock arrives; closing it later would
        drop resolution back to the default — a re-homing, refused."""
        override = create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            item=rice,
            effective_from=JAN_1,
        )
        # Now stock arrives, valued under the override.
        with audit_context(actor=superuser):
            post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("5"),
                        effect_key="stock:2",
                        unit_cost=Decimal("900"),
                    )
                ],
                idempotency_key="stock-after-override",
                effective_at=datetime.datetime(TEST_YEAR, 3, 12, 12, 0, tzinfo=BAGHDAD),
            )
        with pytest.raises(ValidationError) as caught:
            close_inventory_mapping(mapping=override, effective_to=datetime.date(TEST_YEAR, 3, 13))
        assert caught.value.code == "inventory_account_reclassification_required"

    def test_the_organization_default_cannot_change_under_standing_stock(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        stocked_rice: InventoryItem,
        default_mapping: OrganizationAccountMapping,
    ) -> None:
        """The §G rule holds through the accounting service too — the guard
        hook fires no matter which module changes the mapping."""
        with pytest.raises(ValidationError) as caught:
            close_account_mapping(
                mapping=default_mapping, effective_to=datetime.date(TEST_YEAR, 3, 13)
            )
        assert caught.value.code == "inventory_account_reclassification_required"

    def test_a_non_control_default_may_change_freely(
        self, organization: Organization, stocked_rice: InventoryItem, accounting: None
    ) -> None:
        """Only INVENTORY_CONTROL carries standing value; other roles move."""
        equity = create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=INVENTORY_OPENING_EQUITY),
            account=Account.objects.get(organization=organization, code="3-02-01-001"),
            effective_from=JAN_1,
        )
        closed = close_account_mapping(mapping=equity, effective_to=MAR_15)
        assert closed.effective_to == MAR_15

    def test_an_item_with_stock_cannot_move_to_a_category_resolving_differently(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        stocked_rice: InventoryItem,
        category: ItemCategory,
    ) -> None:
        """§V 15: the category move is the third door into the same room."""
        dry_goods = create_item_category(
            organization=organization, code="DRY", name="جافة", parent=category
        )
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            category=dry_goods,
            effective_from=JAN_1,
        )
        with pytest.raises(ValidationError) as caught:
            update_item(
                item=stocked_rice,
                name=stocked_rice.name,
                category=dry_goods,
                item_type=stocked_rice.item_type,
            )
        assert caught.value.code == "inventory_account_reclassification_required"
        stocked_rice.refresh_from_db()
        assert stocked_rice.category.code == "MEAT"

    def test_an_item_without_stock_moves_between_categories_freely(
        self,
        organization: Organization,
        control_role: AccountRole,
        override_account: Account,
        rice: InventoryItem,
        category: ItemCategory,
        default_mapping: OrganizationAccountMapping,
        kilogram: UnitOfMeasure,
    ) -> None:
        dry_goods = create_item_category(
            organization=organization, code="DRY2", name="جافة ٢", parent=category
        )
        create_inventory_mapping(
            organization=organization,
            role=control_role,
            account=override_account,
            category=dry_goods,
            effective_from=JAN_1,
        )
        moved = update_item(
            item=rice,
            name=rice.name,
            category=dry_goods,
            item_type=rice.item_type,
        )
        assert moved.category == dry_goods
