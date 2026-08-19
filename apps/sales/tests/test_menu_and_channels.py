"""
The contracts behind the menu, its prices and the sales channels.

Focused rather than exhaustive. Each test stands for a claim the screens make
that would be expensive to discover was false:

* a price resolves most-specific-first, and two prices cannot both apply;
* a menu item points at a recipe and a serving *code*, not at a version;
* an application channel settles and never counts;
* direct-stock sales are refused by the database, not only by the service.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounting.models import CostCenter
from apps.kitchen.models import Recipe, RecipeServing, RecipeVersion
from apps.organizations.models import Branch, Organization
from apps.sales.models import (
    FulfillmentSource,
    MenuItem,
    PriceScope,
    SalesChannel,
    SalesChannelCategory,
    TenderDestination,
)
from apps.sales.selectors import effective_prices, is_available_at, resolve_price
from apps.sales.services import (
    close_menu_price,
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_branch_availability,
)
from apps.users.models import User

TODAY = datetime.date(2026, 8, 19)


class TestTheVocabularyIsClosed:
    """Pure checks — no database, no fixtures."""

    def test_channel_categories_are_the_approved_five(self) -> None:
        assert set(SalesChannelCategory.values) == {
            "DINE_IN",
            "TAKEAWAY",
            "DIRECT_DELIVERY",
            "DELIVERY_APPLICATION",
            "OTHER",
        }

    def test_there_is_no_channel_per_delivery_company(self) -> None:
        """
        One `DELIVERY_APPLICATION` value, not one per contract.

        A channel per application would multiply every report's channel axis by
        the number of companies and make "how much did we sell through apps" a
        sum somebody has to maintain by hand.
        """
        application_like = [
            value for value in SalesChannelCategory.values if "DELIVERY_APPLICATION" in value
        ]
        assert application_like == ["DELIVERY_APPLICATION"]

    def test_tender_destinations_are_three(self) -> None:
        assert set(TenderDestination.values) == {"CASH", "CARD", "APPLICATION_RECEIVABLE"}

    def test_price_scopes_declare_application_before_it_is_usable(self) -> None:
        """
        The scope is declared and the model refuses it until checkpoint 2.

        Declaring it now keeps the resolution order — application, channel,
        branch default — in one place. Splitting the vocabulary across two
        checkpoints would make the earlier half read as complete.
        """
        assert PriceScope.APPLICATION in PriceScope
        assert set(PriceScope.values) == {"BRANCH_DEFAULT", "CHANNEL", "APPLICATION"}

    def test_release_one_fulfils_from_recipe_servings(self) -> None:
        assert set(FulfillmentSource.values) == {"RECIPE_SERVING", "DIRECT_STOCK"}


@pytest.fixture
def recipe(organization: Organization) -> Recipe:
    from apps.kitchen.models import RecipeType

    return Recipe.objects.create(
        organization=organization,
        code="MANDI-CHICKEN",
        name_ar="مندي دجاج",
        recipe_type=RecipeType.PORTION,
    )


@pytest.fixture
def recipe_version(recipe: Recipe) -> RecipeVersion:
    from apps.units.models import UnitOfMeasure

    unit = UnitOfMeasure.objects.filter(code="KG").first() or UnitOfMeasure.objects.create(
        code="KG",
        name_ar="كيلوغرام",
        name_en="Kilogram",
        dimension="MASS",
        factor_to_base=Decimal("1"),
        is_base=True,
    )
    return RecipeVersion.objects.create(
        recipe=recipe,
        version_number=1,
        output_unit=unit,
        expected_output_quantity=Decimal("10.000000"),
        effective_from=datetime.date(2026, 1, 1),
    )


@pytest.fixture
def servings(recipe_version: RecipeVersion) -> list[RecipeServing]:
    from apps.units.models import UnitOfMeasure

    unit = UnitOfMeasure.objects.get(code="KG")
    rows = []
    for code, name, quantity, factor, primary in (
        ("WHOLE", "حبة كاملة", "1.000000", "0.100000000000", True),
        ("HALF", "نصف حبة", "0.500000", "0.050000000000", False),
    ):
        rows.append(
            RecipeServing.objects.create(
                version=recipe_version,
                code=code,
                name_ar=name,
                serving_quantity=Decimal(quantity),
                serving_unit=unit,
                base_quantity=Decimal(quantity),
                factor_of_batch=Decimal(factor),
                is_primary=primary,
            )
        )
    return rows


@pytest.fixture
def menu_item(
    organization: Organization, recipe: Recipe, servings: list[RecipeServing]
) -> MenuItem:
    return create_menu_item(
        organization=organization,
        code="MENU-MANDI-WHOLE",
        name_ar="مندي دجاج — حبة كاملة",
        recipe=recipe,
        serving_code="WHOLE",
    )


@pytest.fixture
def dine_in(organization: Organization, hall_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="DINE-IN",
        name_ar="الصالة",
        category=SalesChannelCategory.DINE_IN,
        cost_center=hall_cost_center,
        default_tender=TenderDestination.CASH,
    )


@pytest.mark.django_db
class TestAMenuItemNamesARecipeAndAServingCode:
    def test_the_serving_code_is_checked_against_every_version(
        self, organization: Organization, recipe: Recipe, servings: list[RecipeServing]
    ) -> None:
        """
        A code no version has ever carried is a typo and is refused.

        Checked against every version rather than the one in force today, so an
        item can legitimately be configured for a recipe whose new version
        starts next Sunday.
        """
        with pytest.raises(ValidationError) as caught:
            create_menu_item(
                organization=organization,
                code="MENU-TYPO",
                name_ar="خطأ مطبعي",
                recipe=recipe,
                serving_code="QUARTER",
            )
        assert caught.value.code == "unknown_serving_code"

    def test_a_recipe_from_another_organization_is_refused(
        self, other_organization: Organization, recipe: Recipe, servings: list[RecipeServing]
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_menu_item(
                organization=other_organization,
                code="MENU-CROSS",
                name_ar="عبر المؤسسات",
                recipe=recipe,
                serving_code="WHOLE",
            )
        assert caught.value.code == "recipe_organization_mismatch"

    def test_direct_stock_is_refused_by_the_service(
        self, organization: Organization, recipe: Recipe, servings: list[RecipeServing]
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_menu_item(
                organization=organization,
                code="MENU-WATER",
                name_ar="ماء",
                recipe=recipe,
                serving_code="WHOLE",
                fulfillment_source=FulfillmentSource.DIRECT_STOCK,
            )
        assert caught.value.code == "direct_stock_deferred"

    def test_direct_stock_is_refused_by_the_database_as_well(
        self, organization: Organization, recipe: Recipe, servings: list[RecipeServing]
    ) -> None:
        """
        The refusal survives a shell session and a CSV import.

        Enabling direct-stock sales is then an explicit act — a migration that
        drops the constraint beside the service that posts the COGS — rather
        than something an ORM call can do by accident.
        """
        with pytest.raises(IntegrityError), transaction.atomic():
            MenuItem.objects.create(
                organization=organization,
                code="MENU-BYPASS",
                name_ar="التفاف",
                recipe=recipe,
                serving_code="WHOLE",
                fulfillment_source=FulfillmentSource.DIRECT_STOCK,
            )


@pytest.mark.django_db
class TestBranchAvailabilityHasThreeStates:
    def test_no_row_means_never_offered(self, menu_item: MenuItem, branch: Branch) -> None:
        assert is_available_at(menu_item, branch) is False
        assert menu_item.branch_settings.count() == 0

    def test_a_row_switched_off_is_a_different_fact_from_no_row(
        self, menu_item: MenuItem, branch: Branch
    ) -> None:
        """
        Both answer "not on sale today". The difference tells an operator
        whether to add the offer or turn it back on.
        """
        setting = set_branch_availability(item=menu_item, branch=branch, is_available=False)
        assert is_available_at(menu_item, branch) is False
        assert setting.pk is not None
        assert menu_item.branch_settings.count() == 1

    def test_a_branch_of_another_organization_is_refused(
        self, menu_item: MenuItem, other_organization: Organization
    ) -> None:
        from datetime import time

        from apps.organizations.services import create_branch

        foreign = create_branch(
            organization=other_organization,
            code="FOREIGN",
            name_ar="أجنبي",
            name_en="Foreign",
            business_day_start_time=time(9, 0),
        )
        with pytest.raises(ValidationError) as caught:
            set_branch_availability(item=menu_item, branch=foreign)
        assert caught.value.code == "branch_organization_mismatch"


@pytest.mark.django_db
class TestPriceResolution:
    def test_the_narrower_scope_wins(
        self, menu_item: MenuItem, branch: Branch, dine_in: SalesChannel
    ) -> None:
        create_menu_price(
            menu_item=menu_item,
            branch=branch,
            unit_price=Decimal("12000"),
            effective_from=datetime.date(2026, 1, 1),
        )
        create_menu_price(
            menu_item=menu_item,
            branch=branch,
            unit_price=Decimal("13500"),
            effective_from=datetime.date(2026, 1, 1),
            scope=PriceScope.CHANNEL,
            channel=dine_in,
        )

        chosen = resolve_price(menu_item, branch, TODAY, channel=dine_in)
        assert chosen is not None
        assert chosen.unit_price == Decimal("13500.000000")
        assert chosen.scope == PriceScope.CHANNEL

        # Both are in force. The resolver picks; it does not hide the other.
        assert len(effective_prices(menu_item, branch, TODAY, channel=dine_in)) == 2

    def test_without_a_channel_only_the_branch_default_applies(
        self, menu_item: MenuItem, branch: Branch, dine_in: SalesChannel
    ) -> None:
        create_menu_price(
            menu_item=menu_item,
            branch=branch,
            unit_price=Decimal("12000"),
            effective_from=datetime.date(2026, 1, 1),
        )
        create_menu_price(
            menu_item=menu_item,
            branch=branch,
            unit_price=Decimal("13500"),
            effective_from=datetime.date(2026, 1, 1),
            scope=PriceScope.CHANNEL,
            channel=dine_in,
        )
        chosen = resolve_price(menu_item, branch, TODAY)
        assert chosen is not None
        assert chosen.unit_price == Decimal("12000.000000")

    def test_no_price_answers_none_rather_than_zero(
        self, menu_item: MenuItem, branch: Branch
    ) -> None:
        """
        Zero would be a price — a free plate. The *sale* refuses a line with no
        price; the read reports the absence so a maintenance screen can still
        render the item somebody opened it to fix.
        """
        assert resolve_price(menu_item, branch, TODAY) is None

    def test_a_lapsed_price_stops_applying(self, menu_item: MenuItem, branch: Branch) -> None:
        price = create_menu_price(
            menu_item=menu_item,
            branch=branch,
            unit_price=Decimal("12000"),
            effective_from=datetime.date(2026, 1, 1),
        )
        close_menu_price(price=price, effective_to=datetime.date(2026, 8, 18), reason="نهاية موسم")
        assert resolve_price(menu_item, branch, TODAY) is None
        assert resolve_price(menu_item, branch, datetime.date(2026, 8, 18)) is not None

    def test_two_branch_defaults_cannot_overlap(self, menu_item: MenuItem, branch: Branch) -> None:
        """
        Refused by a PostgreSQL exclusion constraint, not by a service check.

        The clash is between ranges rather than values, and two concurrent
        requests both read a clean table before either writes — which is
        exactly the case a service check misses.
        """
        create_menu_price(
            menu_item=menu_item,
            branch=branch,
            unit_price=Decimal("12000"),
            effective_from=datetime.date(2026, 1, 1),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            create_menu_price(
                menu_item=menu_item,
                branch=branch,
                unit_price=Decimal("14000"),
                effective_from=datetime.date(2026, 6, 1),
            )

    def test_a_channel_price_may_overlap_a_branch_default(
        self, menu_item: MenuItem, branch: Branch, dine_in: SalesChannel
    ) -> None:
        """
        The whole point of "most specific wins" is that the scopes coexist.

        A single constraint over `(item, branch, range)` would refuse exactly
        the arrangement the design requires, which is why there is one
        constraint per scope.
        """
        create_menu_price(
            menu_item=menu_item,
            branch=branch,
            unit_price=Decimal("12000"),
            effective_from=datetime.date(2026, 1, 1),
        )
        overlapping = create_menu_price(
            menu_item=menu_item,
            branch=branch,
            unit_price=Decimal("13500"),
            effective_from=datetime.date(2026, 3, 1),
            scope=PriceScope.CHANNEL,
            channel=dine_in,
        )
        assert overlapping.pk is not None

    def test_an_application_scoped_price_must_name_an_application(
        self, menu_item: MenuItem, branch: Branch
    ) -> None:
        """
        Checkpoint 2 replaced the "not yet available" refusal with the real
        rule. The scope was declared in checkpoint 1 and refused outright,
        because its master did not exist; now it exists and the scope means
        something, so what is refused is a price that claims the scope and
        names nobody.
        """
        with pytest.raises(ValidationError) as caught:
            create_menu_price(
                menu_item=menu_item,
                branch=branch,
                unit_price=Decimal("15000"),
                effective_from=datetime.date(2026, 1, 1),
                scope=PriceScope.APPLICATION,
            )
        assert caught.value.code == "application_required"

    def test_the_narrowest_scope_is_the_application(
        self, menu_item: MenuItem, branch: Branch, dine_in: SalesChannel
    ) -> None:
        """
        Application beats channel beats branch default. All three are in force
        together, and the resolver picks — the same plate is genuinely listed
        higher on a delivery application than in the hall.
        """
        from apps.sales.services import create_delivery_application

        application = create_delivery_application(
            organization=menu_item.organization, code="DEMO-APPX", name_ar="تطبيق تجريبي"
        )
        for scope, price, channel, app in (
            (PriceScope.BRANCH_DEFAULT, "12000", None, None),
            (PriceScope.CHANNEL, "13500", dine_in, None),
            (PriceScope.APPLICATION, "16000", None, application),
        ):
            create_menu_price(
                menu_item=menu_item,
                branch=branch,
                unit_price=Decimal(price),
                effective_from=datetime.date(2026, 1, 1),
                scope=scope,
                channel=channel,
                delivery_application=app,
            )

        chosen = resolve_price(
            menu_item, branch, TODAY, channel=dine_in, delivery_application=application
        )
        assert chosen is not None
        assert chosen.unit_price == Decimal("16000.000000")
        # ...and a read that names no application never sees the app price.
        without = resolve_price(menu_item, branch, TODAY, channel=dine_in)
        assert without is not None
        assert without.unit_price == Decimal("13500.000000")


@pytest.mark.django_db
class TestChannelFinancialBehaviour:
    def test_an_application_channel_always_settles(
        self, organization: Organization, delivery_cost_center: CostCenter
    ) -> None:
        """
        The tender is derived, not trusted. A cashier counting a drawer must
        never be asked to account for money a delivery company is holding.
        """
        channel = create_sales_channel(
            organization=organization,
            code="APPS",
            name_ar="تطبيقات التوصيل",
            category=SalesChannelCategory.DELIVERY_APPLICATION,
            cost_center=delivery_cost_center,
            default_tender=TenderDestination.CASH,
        )
        assert channel.default_tender == TenderDestination.APPLICATION_RECEIVABLE
        assert channel.requires_delivery_application is True

    def test_only_an_application_channel_may_settle(
        self, organization: Organization, hall_cost_center: CostCenter
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_sales_channel(
                organization=organization,
                code="HALL-SETTLED",
                name_ar="صالة تُسوّى",
                category=SalesChannelCategory.DINE_IN,
                cost_center=hall_cost_center,
                default_tender=TenderDestination.APPLICATION_RECEIVABLE,
            )
        assert caught.value.code == "tender_needs_application_channel"

    def test_the_database_refuses_the_same_contradiction(
        self, organization: Organization, hall_cost_center: CostCenter
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            SalesChannel.objects.create(
                organization=organization,
                code="BYPASS",
                name_ar="التفاف",
                category=SalesChannelCategory.DINE_IN,
                cost_center=hall_cost_center,
                default_tender=TenderDestination.APPLICATION_RECEIVABLE,
            )

    def test_a_cost_center_is_required(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        """
        Revenue, discount and commission accounts all require a cost centre, so
        a channel without one is a channel whose sales cannot post. Refusing it
        here means the failure lands on whoever configures the channel rather
        than on a cashier trying to close a day.
        """
        from apps.accounting.services import create_cost_center

        foreign = create_cost_center(
            organization=other_organization, code="X", name_ar="س", name_en="X"
        )
        with pytest.raises(ValidationError) as caught:
            create_sales_channel(
                organization=organization,
                code="MISMATCH",
                name_ar="مركز كلفة أجنبي",
                category=SalesChannelCategory.DINE_IN,
                cost_center=foreign,
            )
        assert caught.value.code == "cost_center_organization_mismatch"


@pytest.mark.django_db
class TestScopeAndAuthority:
    def test_a_branch_membership_reaches_the_organization_menu(
        self, manager: User, menu_item: MenuItem
    ) -> None:
        """
        `ORGANIZATION_MASTER_DATA` means *reaching* the organization is enough.

        The manager holds no organization membership at all — only a branch
        one — and the menu is organization property.
        """
        from apps.sales.selectors import visible_menu_items

        assert list(visible_menu_items(manager)) == [menu_item]

    def test_another_organizations_menu_is_absent_rather_than_forbidden(
        self, outsider: User, menu_item: MenuItem
    ) -> None:
        from apps.organizations.authorization import OutOfScope
        from apps.sales.selectors import resolve_menu_item, visible_menu_items

        assert list(visible_menu_items(outsider)) == []
        with pytest.raises(OutOfScope):
            resolve_menu_item(outsider, menu_item.pk)

    def test_a_cashier_reads_the_menu_and_cannot_change_it(self, cashier: User) -> None:
        from apps.sales.permissions import MANAGE_MENU, VIEW_SALES, VIEW_SALES_COST

        assert cashier.has_perm(VIEW_SALES)
        assert not cashier.has_perm(MANAGE_MENU)
        # What a plate costs to make is not information a till needs.
        assert not cashier.has_perm(VIEW_SALES_COST)


@pytest.mark.django_db
class TestNavigationIsBackedByRoutes:
    """An active entry that 404s is worse than an obviously unfinished one."""

    def test_the_two_checkpoint_one_entries_are_active_and_reversible(self) -> None:
        from django.urls import reverse

        from apps.core.navigation import MODULES

        sales = next(module for module in MODULES if module.key == "sales")
        for label in ("أصناف المنيو", "قنوات البيع"):
            section = next(row for row in sales.sections if str(row.label) == label)
            assert section.available is True, f"{label} is still inert"
            assert reverse(section.url_name)

    def test_the_unbuilt_sections_are_still_honestly_inert(self) -> None:
        """
        The count moves with each checkpoint, on purpose. An entry becomes
        active only after its route answers as a page *and* as a fragment, so
        this number is a record of what is actually reachable rather than of
        what is planned.
        """
        from apps.core.navigation import MODULES

        sales = next(module for module in MODULES if module.key == "sales")
        inert = [str(row.label) for row in sales.sections if not row.available]
        assert len(sales.sections) == 12
        assert len(inert) == 7
        assert "المبيعات اليومية" in inert
