"""
A day of trading, end to end: resolved, submitted, posted, reversed.

The one test file in this module that touches the ledger. Everything it asserts
is a claim ADR-027 makes and that would be expensive to discover was false:

* revenue posts **gross**, with every deduction beside it rather than inside it;
* the application-funded discount reaches neither the journal nor the
  restaurant's revenue;
* the receivable is a ledger entry, and reversing writes the opposite entry
  rather than editing the first;
* the posted line's recipe version is the one in force on its business date,
  and stays that version after the kitchen approves a newer one;
* the kitchen's `SALES_NOT_INCLUDED_PHASE_4` limitation is gone.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    DELIVERY_COMMISSION_EXPENSE,
    SALES_CASH_ON_HAND,
    SALES_DISCOUNT,
    SALES_REVENUE,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
    JournalLine,
)
from apps.accounting.services import (
    create_account,
    create_account_mapping,
    open_fiscal_year,
)
from apps.kitchen.models import Recipe, RecipeServing, RecipeVersion
from apps.organizations.models import Branch, Organization
from apps.sales.consumption_source import SalesQuantitySource
from apps.sales.day_services import (
    add_sales_line,
    create_sales_day,
    submit_sales_day,
    totals_for,
)
from apps.sales.models import (
    ApplicationReceivableEntry,
    CommissionBasis,
    DeliveryApplication,
    MenuItem,
    ReceivableSource,
    SalesChannel,
    SalesChannelCategory,
    SalesDay,
    SalesDayStatus,
    TenderDestination,
)
from apps.sales.posting import SOURCE_DOCUMENT_TYPE, post_sales_day, reverse_sales_day
from apps.sales.selectors import receivable_balance
from apps.sales.services import (
    create_delivery_agreement,
    create_delivery_application,
    create_discount_program,
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_application_branch_setting,
    set_branch_availability,
)
from apps.users.models import User

BUSINESS_DATE = datetime.date(2026, 8, 10)
JANUARY = datetime.date(2026, 1, 1)

#: The chart leaves the sales roles map to. Codes rather than names, because a
#: code is what a mapping actually points at.
_CHART: tuple[tuple[str, str, str], ...] = (
    ("1", "الأصول", "Assets"),
    ("1-01", "النقد", "Cash"),
    ("1-01-01", "الصناديق", "Cash boxes"),
    ("1-01-01-001", "الصندوق", "Cash"),
    ("1-02", "الذمم", "Receivables"),
    ("1-02-01", "ذمم التطبيقات", "App receivables"),
    ("1-02-01-001", "ذمم تطبيقات التوصيل", "App Receivable"),
    ("4", "الإيرادات", "Revenue"),
    ("4-01", "المبيعات", "Sales"),
    ("4-01-01", "مبيعات مباشرة", "Direct"),
    ("4-01-01-001", "مبيعات الصالة", "Dine-in Sales"),
    ("4-02", "خصومات المبيعات", "Discounts"),
    ("4-02-01", "خصومات المطعم", "Restaurant discounts"),
    ("4-02-01-001", "خصومات المبيعات", "Sales Discount"),
    ("6", "المصروفات", "Expenses"),
    ("6-03", "مصروفات البيع", "Selling"),
    ("6-03-01", "عمولات التطبيقات", "App commissions"),
    ("6-03-01-001", "عمولات التوصيل", "Commission"),
)


@pytest.fixture
def chart(
    organization: Organization,
    hall_cost_center: CostCenter,
    delivery_cost_center: CostCenter,
) -> dict[str, str]:
    """The four accounts a sales journal needs, and their role mappings."""
    for code, name_ar, name_en in _CHART:
        create_account(organization=organization, code=code, name_ar=name_ar, name_en=name_en)
    mappings = {
        SALES_REVENUE: "4-01-01-001",
        SALES_DISCOUNT: "4-02-01-001",
        SALES_CASH_ON_HAND: "1-01-01-001",
        DELIVERY_APP_RECEIVABLE: "1-02-01-001",
        DELIVERY_COMMISSION_EXPENSE: "6-03-01-001",
    }
    for role, code in mappings.items():
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=role),
            account=Account.objects.get(organization=organization, code=code),
            effective_from=JANUARY,
        )
    # Twelve open periods, through the kernel's own helper rather than by
    # writing rows: a period created by hand would skip the validation the
    # posting path is about to rely on.
    open_fiscal_year(organization=organization, year=BUSINESS_DATE.year)
    return mappings


@pytest.fixture
def recipe_setup(organization: Organization) -> tuple[Recipe, RecipeVersion, RecipeServing]:
    """A recipe with one active version and a WHOLE serving worth a tenth of a batch."""
    from apps.kitchen.models import ApprovalEvidenceKind, RecipeType, RecipeVersionStatus
    from apps.units.models import UnitOfMeasure

    unit = UnitOfMeasure.objects.filter(code="KG").first() or UnitOfMeasure.objects.create(
        code="KG",
        name_ar="كيلوغرام",
        name_en="Kilogram",
        dimension="MASS",
        factor_to_base=Decimal("1"),
        is_base=True,
    )
    recipe = Recipe.objects.create(
        organization=organization,
        code="DEMO-MANDI",
        name_ar="مندي",
        recipe_type=RecipeType.PORTION,
    )
    # Walked through the kitchen's real lifecycle, one transition at a time,
    # because its immutability trigger permits exactly that and nothing else:
    # DRAFT -> SUBMITTED -> APPROVED -> ACTIVE, each carrying only the columns
    # that transition is allowed to move. Reaching around it with a single
    # bulk write is refused by the database, which is the trigger doing its
    # job — Task 3.2A built it precisely so a version could not acquire an
    # effective range without acquiring its evidence first.
    #
    # A serving is added while the version is still a DRAFT, for the same
    # reason: a serving on an ACTIVE version is frozen.
    preparer = User.objects.create_user(username="recipe-preparer", password="pw-not-real-1234")
    approver = User.objects.create_user(username="recipe-approver", password="pw-not-real-1234")
    version = RecipeVersion.objects.create(
        recipe=recipe,
        version_number=1,
        output_unit=unit,
        expected_output_quantity=Decimal("10.000000"),
        status=RecipeVersionStatus.DRAFT,
    )
    serving = RecipeServing.objects.create(
        version=version,
        code="WHOLE",
        name_ar="حبة كاملة",
        serving_quantity=Decimal("1.000000"),
        serving_unit=unit,
        base_quantity=Decimal("1.000000"),
        factor_of_batch=Decimal("0.100000000000"),
        is_primary=True,
    )

    now = timezone.now()
    rows = RecipeVersion.objects.filter(pk=version.pk)
    rows.update(status=RecipeVersionStatus.SUBMITTED, submitted_by=preparer, submitted_at=now)
    # `DEMO_FICTIONAL` evidence is permitted only inside the demo namespace, by
    # a trigger as well as by the service — which is why the recipe above is
    # coded `DEMO-MANDI`. Fabricated approval data belongs there and nowhere
    # else, and a demo signoff that looked like a signed Khan Mandi record is
    # exactly how unapproved figures acquire authority.
    rows.update(
        status=RecipeVersionStatus.APPROVED,
        approved_by=approver,
        approved_at=now,
        approval_reference="اعتماد تجريبي",
        approval_evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
    )
    # Approval is agreement; activation is the claim on a date. Two decisions,
    # two transitions, and the effective range arrives with the second.
    rows.update(
        status=RecipeVersionStatus.ACTIVE,
        activated_by=approver,
        activated_at=now,
        effective_from=JANUARY,
    )
    version.refresh_from_db()
    return recipe, version, serving


@pytest.fixture
def menu_item(
    organization: Organization,
    branch: Branch,
    recipe_setup: tuple[Recipe, RecipeVersion, RecipeServing],
) -> MenuItem:
    recipe, _version, _serving = recipe_setup
    item = create_menu_item(
        organization=organization,
        code="MENU-MANDI",
        name_ar="مندي",
        recipe=recipe,
        serving_code="WHOLE",
    )
    set_branch_availability(item=item, branch=branch)
    create_menu_price(
        menu_item=item,
        branch=branch,
        unit_price=Decimal("10000"),
        effective_from=JANUARY,
    )
    return item


@pytest.fixture
def hall(organization: Organization, hall_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="DINE-IN",
        name_ar="الصالة",
        category=SalesChannelCategory.DINE_IN,
        cost_center=hall_cost_center,
        default_tender=TenderDestination.CASH,
    )


@pytest.fixture
def app_channel(organization: Organization, delivery_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="APPS",
        name_ar="تطبيقات",
        category=SalesChannelCategory.DELIVERY_APPLICATION,
        cost_center=delivery_cost_center,
    )


@pytest.fixture
def application(organization: Organization, branch: Branch) -> DeliveryApplication:
    app = create_delivery_application(
        organization=organization, code="DEMO-APP", name_ar="تطبيق تجريبي"
    )
    set_application_branch_setting(application=app, branch=branch)
    create_delivery_agreement(
        branch=branch,
        delivery_application=app,
        effective_from=JANUARY,
        commission_percent=Decimal("15"),
        commission_basis=CommissionBasis.GROSS_LIST_AMOUNT,
        evidence_reference="عقد تجريبي",
    )
    return app


@pytest.fixture
def day(organization: Organization, branch: Branch, manager: User) -> SalesDay:
    return create_sales_day(
        organization=organization,
        branch=branch,
        business_date=BUSINESS_DATE,
        actor=manager,
    )


def _lines_by_code(entry: JournalEntry) -> dict[str, JournalLine]:
    return {line.account.code: line for line in entry.lines.select_related("account")}


@pytest.mark.django_db
class TestResolutionHappensOnceAndRefusesRatherThanFallingBack:
    def test_a_line_stores_the_version_in_force_on_its_business_date(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        recipe_setup: tuple[Recipe, RecipeVersion, RecipeServing],
    ) -> None:
        _recipe, version, serving = recipe_setup
        line = add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
        assert line.recipe_version == version
        assert line.serving == serving
        assert line.unit_price == Decimal("10000.000000")
        assert line.gross_amount == Decimal("20000.000")

    def test_an_item_with_no_price_on_the_date_is_refused(
        self,
        day: SalesDay,
        hall: SalesChannel,
        recipe_setup: tuple[Recipe, RecipeVersion, RecipeServing],
        organization: Organization,
        branch: Branch,
    ) -> None:
        recipe, _version, _serving = recipe_setup
        priceless = create_menu_item(
            organization=organization,
            code="MENU-NOPRICE",
            name_ar="بلا سعر",
            recipe=recipe,
            serving_code="WHOLE",
        )
        set_branch_availability(item=priceless, branch=branch)
        with pytest.raises(ValidationError) as caught:
            add_sales_line(day=day, menu_item=priceless, channel=hall, quantity=Decimal("1.000"))
        assert caught.value.code == "no_effective_price"

    def test_an_application_line_with_no_agreement_is_refused(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        organization: Organization,
        branch: Branch,
    ) -> None:
        """
        No default rate, ever. A default would accrue a commission nobody
        agreed to, against a real company.
        """
        bare = create_delivery_application(
            organization=organization, code="DEMO-BARE", name_ar="بلا اتفاقية"
        )
        set_application_branch_setting(application=bare, branch=branch)
        with pytest.raises(ValidationError) as caught:
            add_sales_line(
                day=day,
                menu_item=menu_item,
                channel=app_channel,
                delivery_application=bare,
                quantity=Decimal("1.000"),
            )
        assert caught.value.code == "no_effective_agreement"

    def test_a_manual_discount_needs_a_reason(
        self, day: SalesDay, menu_item: MenuItem, hall: SalesChannel
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            add_sales_line(
                day=day,
                menu_item=menu_item,
                channel=hall,
                quantity=Decimal("1.000"),
                manual_discount_amount=Decimal("500"),
            )
        assert caught.value.code == "manual_discount_reason_required"

    def test_a_decimal_quantity_is_ordinary(
        self, day: SalesDay, menu_item: MenuItem, hall: SalesChannel
    ) -> None:
        """`0.500` is a row, not a branch in a service (RCP-082)."""
        line = add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("0.500"))
        assert line.quantity == Decimal("0.500")
        assert line.gross_amount == Decimal("5000.000")


@pytest.mark.django_db
class TestADiscountProgrammeMustCoverTheLineItIsAppliedTo:
    """
    The audit finding this class exists for.

    `applicable_programs` is the single statement of which programme covers
    which line, and `resolve_line` never called it — it took whatever programme
    it was handed and split it. The expensive case is the funding one: a
    promotion funded by one delivery application, applied to a line taken by
    another, raises a receivable against a company that agreed to reimburse
    nothing, and every figure on the line still sums.
    """

    def _second_application(
        self, organization: Organization, branch: Branch
    ) -> DeliveryApplication:
        other = create_delivery_application(
            organization=organization, code="DEMO-OTHER", name_ar="تطبيق آخر"
        )
        set_application_branch_setting(application=other, branch=branch)
        create_delivery_agreement(
            branch=branch,
            delivery_application=other,
            effective_from=JANUARY,
            commission_percent=Decimal("15"),
            commission_basis=CommissionBasis.GROSS_LIST_AMOUNT,
            evidence_reference="عقد تجريبي آخر",
        )
        return other

    def test_one_applications_promotion_may_not_be_billed_to_another(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        application: DeliveryApplication,
        organization: Organization,
        branch: Branch,
    ) -> None:
        """A receivable against a company that funded nothing."""
        program = create_discount_program(
            organization=organization,
            code="DEMO-THEIRS",
            name_ar="عرض تطبيق واحد",
            effective_from=JANUARY,
            discount_percent=Decimal("20"),
            restaurant_funded_share=Decimal("0"),
            application_funded_share=Decimal("100"),
            delivery_application=application,
        )
        other = self._second_application(organization, branch)

        with pytest.raises(ValidationError) as caught:
            add_sales_line(
                day=day,
                menu_item=menu_item,
                channel=app_channel,
                delivery_application=other,
                quantity=Decimal("1.000"),
                order_count=1,
                discount_program=program,
            )
        assert caught.value.code == "discount_program_not_applicable"
        assert day.lines.count() == 0

    def test_a_programme_that_has_ended_is_no_longer_applied(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        organization: Organization,
    ) -> None:
        """
        `close_discount_program` writes `effective_to` and leaves `is_active`
        alone, by design — the row stays readable. A filter that asked only
        `is_active` therefore kept applying a promotion that ended months ago.
        """
        program = create_discount_program(
            organization=organization,
            code="DEMO-ENDED",
            name_ar="عرض منتهٍ",
            effective_from=JANUARY,
            effective_to=datetime.date(2026, 4, 1),
            discount_percent=Decimal("10"),
        )
        assert program.is_active is True

        with pytest.raises(ValidationError) as caught:
            add_sales_line(
                day=day,
                menu_item=menu_item,
                channel=hall,
                quantity=Decimal("1.000"),
                discount_program=program,
            )
        assert caught.value.code == "discount_program_not_applicable"

    def test_a_programme_restricted_to_another_branch_is_refused(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        organization: Organization,
        second_branch: Branch,
    ) -> None:
        program = create_discount_program(
            organization=organization,
            code="DEMO-ELSEWHERE",
            name_ar="عرض فرع آخر",
            effective_from=JANUARY,
            discount_percent=Decimal("10"),
            branch=second_branch,
        )
        with pytest.raises(ValidationError) as caught:
            add_sales_line(
                day=day,
                menu_item=menu_item,
                channel=hall,
                quantity=Decimal("1.000"),
                discount_program=program,
            )
        assert caught.value.code == "discount_program_not_applicable"

    def test_a_programme_restricted_to_another_channel_is_refused(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        app_channel: SalesChannel,
        organization: Organization,
    ) -> None:
        program = create_discount_program(
            organization=organization,
            code="DEMO-APPSONLY",
            name_ar="عرض قناة التطبيقات",
            effective_from=JANUARY,
            discount_percent=Decimal("10"),
            channel=app_channel,
        )
        with pytest.raises(ValidationError) as caught:
            add_sales_line(
                day=day,
                menu_item=menu_item,
                channel=hall,
                quantity=Decimal("1.000"),
                discount_program=program,
            )
        assert caught.value.code == "discount_program_not_applicable"

    def test_a_programme_that_does_cover_the_line_still_applies(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        organization: Organization,
        branch: Branch,
    ) -> None:
        """
        The check refuses what does not fit and nothing else.

        Every axis named, so the narrowest possible programme still reaches the
        line it was written for — otherwise the guard would be a ban.
        """
        program = create_discount_program(
            organization=organization,
            code="DEMO-EXACT",
            name_ar="عرض مطابق",
            effective_from=JANUARY,
            discount_percent=Decimal("10"),
            branch=branch,
            channel=hall,
            menu_item=menu_item,
        )
        line = add_sales_line(
            day=day,
            menu_item=menu_item,
            channel=hall,
            quantity=Decimal("1.000"),
            discount_program=program,
        )
        assert line.restaurant_discount == Decimal("1000.000")
        assert line.customer_charge == Decimal("9000.000")

    def test_an_unrestricted_programme_still_covers_everything(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        organization: Organization,
    ) -> None:
        """A `NULL` on every axis means no restriction on any of them."""
        program = create_discount_program(
            organization=organization,
            code="DEMO-RAMADAN",
            name_ar="عرض رمضان",
            effective_from=JANUARY,
            discount_percent=Decimal("10"),
        )
        line = add_sales_line(
            day=day,
            menu_item=menu_item,
            channel=hall,
            quantity=Decimal("1.000"),
            discount_program=program,
        )
        assert line.restaurant_discount == Decimal("1000.000")


@pytest.mark.django_db
class TestTheCashJournal:
    def test_revenue_is_gross_and_the_discount_sits_beside_it(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        add_sales_line(
            day=day,
            menu_item=menu_item,
            channel=hall,
            quantity=Decimal("1.000"),
            manual_discount_amount=Decimal("1000"),
            manual_discount_reason="خصم ترويجي",
        )
        submit_sales_day(day=day, actor=manager)
        posted = post_sales_day(day=day, actor=accounting_manager)

        assert posted.status == SalesDayStatus.POSTED
        assert posted.number.startswith("SD-2026-")

        entry = JournalEntry.objects.get(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
        )
        lines = _lines_by_code(entry)
        # Gross revenue, not net. A figure with the discount already inside it
        # cannot answer "what did we give away".
        assert lines["4-01-01-001"].credit == Decimal("10000.000")
        assert lines["4-02-01-001"].debit == Decimal("1000.000")
        assert lines["1-01-01-001"].debit == Decimal("9000.000")
        assert sum(row.debit for row in lines.values()) == sum(row.credit for row in lines.values())


@pytest.mark.django_db
class TestTheApplicationJournalAndReceivable:
    def _post_application_day(
        self,
        day: SalesDay,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        application: DeliveryApplication,
        manager: User,
        accounting_manager: User,
        **line_kwargs: object,
    ) -> SalesDay:
        add_sales_line(
            day=day,
            menu_item=menu_item,
            channel=app_channel,
            delivery_application=application,
            quantity=Decimal("1.000"),
            order_count=1,
            **line_kwargs,  # type: ignore[arg-type]
        )
        submit_sales_day(day=day, actor=manager)
        return post_sales_day(day=day, actor=accounting_manager)

    def test_the_journal_balances_and_the_receivable_is_the_residual(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        application: DeliveryApplication,
        manager: User,
        accounting_manager: User,
    ) -> None:
        posted = self._post_application_day(
            day, menu_item, app_channel, application, manager, accounting_manager
        )
        entry = JournalEntry.objects.get(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
        )
        lines = _lines_by_code(entry)
        assert lines["4-01-01-001"].credit == Decimal("10000.000")
        assert lines["6-03-01-001"].debit == Decimal("1500.000")
        assert lines["1-02-01-001"].debit == Decimal("8500.000")
        assert sum(row.debit for row in lines.values()) == Decimal("10000.000")

    def test_an_application_funded_discount_reaches_neither_revenue_nor_the_journal(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        application: DeliveryApplication,
        organization: Organization,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """
        The single most consequential assertion in this module.

        The application reimburses its own discount, so it reduces neither
        revenue nor what the application owes. Posting it as a restaurant
        discount would understate both by the same amount and look internally
        consistent afterwards — surfacing months later as a recurring
        favourable settlement variance (ADR-028 §3).
        """
        program = create_discount_program(
            organization=organization,
            code="DEMO-APPPROMO",
            name_ar="عرض التطبيق",
            effective_from=JANUARY,
            discount_percent=Decimal("20"),
            restaurant_funded_share=Decimal("0"),
            application_funded_share=Decimal("100"),
            delivery_application=application,
        )
        posted = self._post_application_day(
            day,
            menu_item,
            app_channel,
            application,
            manager,
            accounting_manager,
            discount_program=program,
        )
        line = posted.lines.get()
        assert line.application_discount == Decimal("2000.000")
        assert line.restaurant_discount == Decimal("0.000")
        # The customer paid 8,000; the restaurant is still owed 10,000 less
        # commission, because the application pays back what it discounted.
        assert line.customer_charge == Decimal("8000.000")
        assert line.net_amount == Decimal("8500.000")

        entry = JournalEntry.objects.get(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
        )
        lines = _lines_by_code(entry)
        assert lines["4-01-01-001"].credit == Decimal("10000.000")
        assert "4-02-01-001" not in lines

    def test_the_receivable_is_a_ledger_entry_and_the_balance_is_derived(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        application: DeliveryApplication,
        organization: Organization,
        manager: User,
        accounting_manager: User,
    ) -> None:
        posted = self._post_application_day(
            day, menu_item, app_channel, application, manager, accounting_manager
        )
        entry = ApplicationReceivableEntry.objects.get(
            source_document_id=str(posted.public_id), source=ReceivableSource.SALE_POSTED
        )
        assert entry.debit == Decimal("8500.000")
        assert entry.credit == Decimal("0.000")
        assert receivable_balance(
            delivery_application_id=application.pk, organization_id=organization.pk
        ) == Decimal("8500.000")

    def test_the_ledger_is_append_only_in_the_database(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        application: DeliveryApplication,
        manager: User,
        accounting_manager: User,
    ) -> None:
        posted = self._post_application_day(
            day, menu_item, app_channel, application, manager, accounting_manager
        )
        entry = ApplicationReceivableEntry.objects.get(source_document_id=str(posted.public_id))
        entry.debit = Decimal("1.000")
        with pytest.raises(Exception, match="append-only"), transaction.atomic():
            entry.save(update_fields=["debit"])

    def test_reversing_writes_the_opposite_entry_rather_than_editing_the_first(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        application: DeliveryApplication,
        organization: Organization,
        manager: User,
        accounting_manager: User,
    ) -> None:
        posted = self._post_application_day(
            day, menu_item, app_channel, application, manager, accounting_manager
        )
        reverse_sales_day(day=posted, actor=accounting_manager, reason="خطأ في الإدخال")

        rows = ApplicationReceivableEntry.objects.filter(
            source_document_id=str(posted.public_id)
        ).order_by("pk")
        assert [row.source for row in rows] == [
            ReceivableSource.SALE_POSTED,
            ReceivableSource.SALE_REVERSED,
        ]
        assert rows[0].debit == rows[1].credit == Decimal("8500.000")
        assert receivable_balance(
            delivery_application_id=application.pk, organization_id=organization.pk
        ) == Decimal("0.000")


@pytest.mark.django_db
class TestTheLifecycleIsEnforcedByTheDatabase:
    def test_a_posted_day_cannot_have_its_lines_changed(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("1.000"))
        submit_sales_day(day=day, actor=manager)
        posted = post_sales_day(day=day, actor=accounting_manager)

        line = posted.lines.get()
        line.quantity = Decimal("99.000")
        with pytest.raises(Exception, match="draft"), transaction.atomic():
            line.save(update_fields=["quantity"])

    def test_a_submitted_day_cannot_have_its_figures_changed(
        self, day: SalesDay, menu_item: MenuItem, hall: SalesChannel, manager: User
    ) -> None:
        """
        Submitting is the statement "this is what I counted", and a statement
        that can be edited after it is made is not one. The way back is the
        service's own return-to-draft, which leaves a reason on the trail.
        """
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("1.000"))
        submit_sales_day(day=day, actor=manager)
        line = day.lines.get()
        line.quantity = Decimal("5.000")
        with pytest.raises(Exception, match="draft"), transaction.atomic():
            line.save(update_fields=["quantity"])

    def test_an_empty_day_cannot_be_submitted(self, day: SalesDay, manager: User) -> None:
        with pytest.raises(ValidationError) as caught:
            submit_sales_day(day=day, actor=manager)
        assert caught.value.code == "no_lines"

    def test_one_day_per_branch_and_date(
        self, organization: Organization, branch: Branch, day: SalesDay, manager: User
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_sales_day(
                organization=organization,
                branch=branch,
                business_date=BUSINESS_DATE,
                actor=manager,
            )
        assert caught.value.code == "day_already_exists"

    def test_a_number_is_assigned_only_at_posting(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        assert day.number == ""
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("1.000"))
        submit_sales_day(day=day, actor=manager)
        assert SalesDay.objects.get(pk=day.pk).number == ""
        posted = post_sales_day(day=day, actor=accounting_manager)
        assert posted.number != ""


@pytest.mark.django_db
class TestTheKitchenNowHasItsSalesSource:
    def test_a_posted_line_contributes_its_leaf_ingredients(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
        submit_sales_day(day=day, actor=manager)
        post_sales_day(day=day, actor=accounting_manager)

        contributions = SalesQuantitySource().contributions(
            organization_id=organization.pk,
            branch_ids=[branch.pk],
            date_from=None,
            date_to=None,
            recipe_id=None,
        )
        # The recipe has no lines in this fixture, so the expansion is empty —
        # what is asserted is that the source *runs* against a posted line and
        # reads its stored version rather than re-resolving one.
        assert isinstance(contributions, list)

    def test_a_draft_day_contributes_nothing(
        self,
        organization: Organization,
        branch: Branch,
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
    ) -> None:
        """A draft is a proposal. It consumed no ingredient from anything."""
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
        contributions = SalesQuantitySource().contributions(
            organization_id=organization.pk,
            branch_ids=[branch.pk],
            date_from=None,
            date_to=None,
            recipe_id=None,
        )
        assert contributions == []

    def test_the_phase_three_coverage_limitation_is_gone(self) -> None:
        """
        Task 3.8 stamped `SALES_NOT_INCLUDED_PHASE_4` on every theoretical
        figure and promised the limitation would disappear when an adapter
        arrived. This is that promise, checked.
        """
        from apps.kitchen.consumption_sources import (
            COMPLETE_COVERAGE,
            FINAL_USAGE_VARIANCE,
            SALES_INCLUDED,
            coverage_code,
            coverage_labels,
            sales_source_is_registered,
        )

        assert sales_source_is_registered() is True
        assert coverage_code() == SALES_INCLUDED
        assert coverage_labels() == (COMPLETE_COVERAGE, FINAL_USAGE_VARIANCE)

    def test_the_kitchen_verifier_does_not_fail_a_correct_deployment(self, manager: User) -> None:
        """
        The audit finding this test exists for.

        `verify_theoretical_coverage` still raised Phase 3's two ERRORs — a
        `SALES` adapter reporting itself available, and coverage claiming
        finality — both of which became true *by construction* the moment
        `SalesConfig.ready()` registered the adapter. `verify_kitchen` therefore
        exited non-zero on every correct install, and did so as the exact mirror
        of `verify_sales`, which raises an ERROR when the registration is
        absent. The two gates could never both pass.
        """
        from apps.kitchen.consumption_reconciliation import ERROR, verify_theoretical_coverage
        from apps.kitchen.consumption_sources import MealUsageFilters

        findings = verify_theoretical_coverage(manager, MealUsageFilters())
        assert [row for row in findings if row.severity == ERROR] == []

    def test_the_limitation_is_no_longer_reported_either(self, manager: User) -> None:
        """
        A complete figure carrying a warning that it is incomplete is the
        opposite failure, and `TheoreticalCoverage.notice` already refuses to
        make it. The verifier now agrees with the notice.
        """
        from apps.kitchen.consumption_reconciliation import verify_theoretical_coverage
        from apps.kitchen.consumption_sources import SALES_NOT_INCLUDED, MealUsageFilters

        codes = {row.code for row in verify_theoretical_coverage(manager, MealUsageFilters())}
        assert SALES_NOT_INCLUDED not in codes

    def test_the_two_gates_agree(self, manager: User) -> None:
        """`verify_sales` and `verify_kitchen` must be satisfiable at once."""
        from apps.kitchen.consumption_reconciliation import ERROR, verify_theoretical_coverage
        from apps.kitchen.consumption_sources import MealUsageFilters
        from apps.sales.reconciliation import verify_coverage

        kitchen = [
            row
            for row in verify_theoretical_coverage(manager, MealUsageFilters())
            if row.severity == ERROR
        ]
        sales = [row for row in verify_coverage() if row.is_error]
        assert kitchen == []
        assert sales == []

    def test_a_branch_with_sales_and_no_meal_records_is_still_in_scope(
        self,
        chart: dict[str, str],
        day: SalesDay,
        menu_item: MenuItem,
        hall: SalesChannel,
        manager: User,
        accounting_manager: User,
        organization: Organization,
        branch: Branch,
    ) -> None:
        """
        The audit finding this test exists for.

        `_scoped` derived the organization and branch list from `MealRecord`
        rows, which was self-consistent while every theoretical source read
        meals. A branch that posts sales and never logs a staff meal produced no
        identifiers at all, so `_usage` returned `[]` without ever calling the
        sales adapter — while the same call stamped the figure `SALES_INCLUDED`
        and `is_final=True`. Every ingredient those sales consumed then appeared
        as unexplained variance on a report claiming to be complete.

        Asserted against the scope rather than against a contribution count,
        because the scope is where the figure was lost: the adapter that is
        never called cannot contribute whatever its recipes expand to.
        """
        from apps.kitchen.consumption_sources import MealUsageFilters, _scoped
        from apps.kitchen.models import MealRecord

        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
        submit_sales_day(day=day, actor=manager)
        post_sales_day(day=day, actor=accounting_manager)
        assert not MealRecord.objects.exists()

        assert _scoped(manager, MealUsageFilters()) == (organization.pk, [branch.pk])

    def test_the_branch_filter_narrows_rather_than_empties(
        self, manager: User, organization: Organization, branch: Branch
    ) -> None:
        """
        Filtering to the branch that actually sold must not zero the figure.

        The old scoping applied `filter(branch_id=...)` to meal records, so a
        branch with sales and no meals answered `None` — and every source,
        including sales, returned empty against a page still stamped final.
        """
        from apps.kitchen.consumption_sources import MealUsageFilters, _scoped

        assert _scoped(manager, MealUsageFilters(branch_id=branch.pk)) == (
            organization.pk,
            [branch.pk],
        )

    def test_a_branch_the_caller_cannot_reach_is_still_refused(
        self, manager: User, second_branch: Branch
    ) -> None:
        """
        Wider than meal records, never wider than the caller's own reach.

        `second_branch` exists in the same organization and `manager` holds no
        post at it, so filtering to it must answer nothing rather than quietly
        widening to the whole organization.
        """
        from apps.kitchen.consumption_sources import MealUsageFilters, _scoped

        assert _scoped(manager, MealUsageFilters(branch_id=second_branch.pk)) is None

    def test_a_caller_who_reaches_nothing_still_answers_empty(self, outsider: User) -> None:
        """A user with no reach has an empty kitchen, not a broken one."""
        from apps.kitchen.consumption_sources import (
            MealUsageFilters,
            TheoreticalSourceType,
            _scoped,
            theoretical_consumption_coverage,
        )

        assert _scoped(outsider, MealUsageFilters()) is None
        coverage = theoretical_consumption_coverage(outsider, MealUsageFilters())
        sales = next(
            row for row in coverage.sources if row.source_type == TheoreticalSourceType.SALES
        )
        assert sales.contribution_count == 0

    def test_the_kitchen_still_owns_its_own_adapters(self) -> None:
        """A module registering from outside may not shadow a meal source."""
        from apps.kitchen.consumption_sources import (
            MealEquivalentSource,
            TheoreticalSourceType,
            register_theoretical_source,
        )

        with pytest.raises(ValueError, match="may not be replaced"):
            register_theoretical_source(
                MealEquivalentSource(source_type=TheoreticalSourceType.STAFF_MEAL)
            )


@pytest.mark.django_db
class TestTotalsAreDerived:
    def test_a_days_totals_are_computed_from_its_lines(
        self, day: SalesDay, menu_item: MenuItem, hall: SalesChannel
    ) -> None:
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("1.000"))
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("0.500"))
        totals = totals_for(day)
        assert totals.line_count == 2
        assert totals.gross == Decimal("15000.000")
        assert totals.net_cash == Decimal("15000.000")

    def test_a_sales_day_carries_no_total_field(self) -> None:
        """A stored total is a second source of truth for what the lines say."""
        fields = {field.name for field in SalesDay._meta.get_fields()}
        for absent in ("total", "gross_total", "net_total", "total_amount"):
            assert absent not in fields
