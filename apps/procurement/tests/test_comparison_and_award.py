"""
Comparison and award: the ranking inversion, and who is allowed to resolve it.

The centre of this file is `TestTheRankingInversion`. Two suppliers quote the
same rice — one by the 30 kg sack, one by the kilogram — and the cheaper of
the two changes depending on whether delivery is counted. Both readings are
arithmetically correct, and a comparison that showed only one of them would be
choosing for the buyer. So both flags are asserted to land on *different*
suppliers, which is the only way to prove the screen is showing a disagreement
rather than a preference.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import JournalEntry
from apps.core.models import AuditEvent
from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    ItemType,
    PackageUnit,
    StockLedgerEntry,
    StockMovement,
    Warehouse,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.procurement.comparison import (
    award_quotation,
    compare_quotations,
    comparison_for_request,
)
from apps.procurement.models import (
    PurchaseRequest,
    Supplier,
    SupplierQuotation,
    SupplierQuotationStatus,
)
from apps.procurement.permissions import AWARD_QUOTATION, permissions_for_role
from apps.procurement.services import (
    add_quotation_line,
    add_request_line,
    approve_purchase_request,
    create_purchase_request,
    create_supplier,
    create_supplier_quotation,
    decline_supplier_quotation,
    submit_purchase_request,
    submit_supplier_quotation,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}
QUOTED = datetime.date(2026, 2, 1)
REQUIRED = datetime.date(2026, 3, 15)


@pytest.fixture
def units() -> None:
    from django.core.management import call_command

    call_command("seed_units", verbosity=0)


@pytest.fixture
def kilogram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="KG")


@pytest.fixture
def rice(organization: Organization, kilogram: UnitOfMeasure) -> InventoryItem:
    from apps.inventory.services import create_item, create_item_category

    return create_item(
        organization=organization,
        code="RICE",
        name="رز",
        category=create_item_category(organization=organization, code="GRAINS", name="حبوب"),
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )


@pytest.fixture
def sack(organization: Organization, rice: InventoryItem) -> PackageUnit:
    from apps.inventory.services import create_item_conversion, create_package_unit

    package = create_package_unit(organization=organization, code="SACK", name="كيس")
    create_item_conversion(
        item=rice,
        package_unit=package,
        factor_to_base=Decimal("30.000000000000"),
        conversion_type=ConversionType.FIXED,
        effective_from=datetime.date(2026, 1, 1),
    )
    return package


@pytest.fixture
def store(branch: Branch) -> Warehouse:
    from apps.inventory.services import create_warehouse

    return create_warehouse(branch=branch, code="MAIN", name="مخزن")


@pytest.fixture
def buyer(branch: Branch) -> User:
    user = User.objects.create_user(username="buyer", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.PURCHASING)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def approver(branch: Branch) -> User:
    user = User.objects.create_user(username="approver", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def approved(
    branch: Branch, keeper: User, approver: User, store: Warehouse, rice: InventoryItem
) -> PurchaseRequest:
    document = create_purchase_request(
        branch=branch,
        requested_by=keeper,
        warehouse=store,
        required_date=REQUIRED,
        purpose="رز الشهر",
    )
    add_request_line(request=document, item=rice, entered_quantity=Decimal("120.000"))
    submit_purchase_request(request=document, actor=keeper)
    return approve_purchase_request(request=document, actor=approver, reason="ok")


@pytest.fixture
def grocery(organization: Organization) -> Supplier:
    return create_supplier(organization=organization, code="GROC-01", name="مورد المواد")


@pytest.fixture
def meat(organization: Organization) -> Supplier:
    return create_supplier(organization=organization, code="MEAT-01", name="مورد اللحوم")


@pytest.fixture
def by_the_sack(
    grocery: Supplier,
    buyer: User,
    approved: PurchaseRequest,
    rice: InventoryItem,
    sack: PackageUnit,
) -> SupplierQuotation:
    """4 sacks at 42,000 = 168,000 for 120 kg, plus 15,000 delivery."""
    quotation = create_supplier_quotation(
        supplier=grocery,
        recorded_by=buyer,
        request=approved,
        quoted_at=QUOTED,
        # No stated expiry: a real case, and one that keeps this fixture
        # meaningful whatever the calendar says when the suite runs.
        supplier_reference="G-1",
        freight_amount=Decimal("15000.000"),
        evidence_reference="بريد",
    )
    add_quotation_line(
        quotation=quotation,
        item=rice,
        package_unit=sack,
        quantity=Decimal("4.000"),
        unit_price=Decimal("42000.000000"),
    )
    return submit_supplier_quotation(quotation=quotation, actor=buyer)


@pytest.fixture
def by_the_kilo(
    meat: Supplier, buyer: User, approved: PurchaseRequest, rice: InventoryItem
) -> SupplierQuotation:
    """120 kg at 1,450 = 174,000, delivered free."""
    quotation = create_supplier_quotation(
        supplier=meat,
        recorded_by=buyer,
        request=approved,
        quoted_at=QUOTED,
        supplier_reference="M-1",
        evidence_reference="ورقة",
    )
    add_quotation_line(
        quotation=quotation,
        item=rice,
        quantity=Decimal("120.000"),
        unit_price=Decimal("1450.000000"),
    )
    return submit_supplier_quotation(quotation=quotation, actor=buyer)


# ---------------------------------------------------------------------------
# The inversion
# ---------------------------------------------------------------------------


class TestTheRankingInversion:
    def test_both_cheapest_flags_land_on_different_suppliers(
        self,
        approved: PurchaseRequest,
        by_the_sack: SupplierQuotation,
        by_the_kilo: SupplierQuotation,
    ) -> None:
        """
        The whole reason the comparison exists.

        Grocery is cheaper per kilogram (1,400 against 1,450) and dearer once
        its 15,000 delivery is counted (1,525 against 1,450). A screen showing
        only one of those two facts would be answering a different question
        from the one the buyer is asking.
        """
        rows = {
            row.supplier_code: row for row in comparison_for_request(request=approved, on=QUOTED)
        }

        assert rows["GROC-01"].base_unit_price == Decimal("1400.000000")
        assert rows["MEAT-01"].base_unit_price == Decimal("1450.000000")
        assert rows["GROC-01"].is_cheapest_base_unit is True
        assert rows["MEAT-01"].is_cheapest_base_unit is False

        # 168,000 + 15,000 = 183,000 over 120 kg = 1,525 per kg.
        assert rows["GROC-01"].landed_base_unit_price == Decimal("1525.000000")
        assert rows["MEAT-01"].landed_base_unit_price == Decimal("1450.000000")
        assert rows["GROC-01"].is_cheapest_landed is False
        assert rows["MEAT-01"].is_cheapest_landed is True

    def test_the_landed_total_is_the_line_plus_its_charge_share(
        self, approved: PurchaseRequest, by_the_sack: SupplierQuotation
    ) -> None:
        row = comparison_for_request(request=approved, on=QUOTED)[0]
        assert row.line_total == Decimal("168000.000")
        assert row.charge_share == Decimal("15000.000")
        assert row.landed_total == Decimal("183000.000")

    def test_rows_are_ordered_by_landed_price(
        self,
        approved: PurchaseRequest,
        by_the_sack: SupplierQuotation,
        by_the_kilo: SupplierQuotation,
    ) -> None:
        codes = [row.supplier_code for row in comparison_for_request(request=approved, on=QUOTED)]
        assert codes == ["MEAT-01", "GROC-01"]

    def test_normalisation_survives_a_package_the_other_supplier_never_used(
        self, approved: PurchaseRequest, by_the_sack: SupplierQuotation
    ) -> None:
        """A sack and a kilogram are the same thing once both reach base units."""
        row = comparison_for_request(request=approved, on=QUOTED)[0]
        assert row.package_code == "SACK"
        assert row.quoted_quantity == Decimal("4.000")
        assert row.base_quantity == Decimal("120.000")
        assert row.base_unit_code == "KG"


# ---------------------------------------------------------------------------
# Charge allocation
# ---------------------------------------------------------------------------


class TestChargeAllocation:
    def test_freight_splits_across_lines_and_sums_exactly(
        self,
        grocery: Supplier,
        buyer: User,
        approved: PurchaseRequest,
        rice: InventoryItem,
        organization: Organization,
        kilogram: UnitOfMeasure,
    ) -> None:
        """
        A charge that does not divide evenly still adds up.

        10,000 over lines weighted 1 and 2 is 3,333.333 and 6,666.667 — and the
        two must sum to exactly 10,000, which is what `allocate` guarantees and
        rating-then-rounding does not.
        """
        from apps.inventory.services import create_item, create_item_category

        oil = create_item(
            organization=organization,
            code="OIL",
            name="زيت",
            category=create_item_category(organization=organization, code="OILS", name="زيوت"),
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        quotation = create_supplier_quotation(
            supplier=grocery,
            recorded_by=buyer,
            request=approved,
            quoted_at=QUOTED,
            freight_amount=Decimal("10000.000"),
            evidence_reference="بريد",
        )
        add_quotation_line(
            quotation=quotation,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.000000"),
        )
        add_quotation_line(
            quotation=quotation,
            item=oil,
            quantity=Decimal("1.000"),
            unit_price=Decimal("2000.000000"),
        )
        submit_supplier_quotation(quotation=quotation, actor=buyer)

        rows = comparison_for_request(request=approved, on=QUOTED)
        shares = sorted(row.charge_share for row in rows)
        assert sum(shares) == Decimal("10000.000")
        assert shares == [Decimal("3333.333"), Decimal("6666.667")]

    def test_a_quotation_of_free_samples_leaves_its_charges_unallocated(
        self, grocery: Supplier, buyer: User, approved: PurchaseRequest, rice: InventoryItem
    ) -> None:
        """
        Nobody has said how to attribute freight on a zero-value line, so the
        comparison does not invent a basis.
        """
        quotation = create_supplier_quotation(
            supplier=grocery,
            recorded_by=buyer,
            request=approved,
            quoted_at=QUOTED,
            freight_amount=Decimal("5000.000"),
            evidence_reference="عينة",
        )
        add_quotation_line(
            quotation=quotation,
            item=rice,
            quantity=Decimal("2.000"),
            unit_price=Decimal("0.000000"),
        )
        submit_supplier_quotation(quotation=quotation, actor=buyer)

        row = comparison_for_request(request=approved, on=QUOTED)[0]
        assert row.charge_share == Decimal("0.000")
        assert row.landed_total == Decimal("0.000")

    def test_a_quotation_with_no_charges_allocates_nothing(
        self, approved: PurchaseRequest, by_the_kilo: SupplierQuotation
    ) -> None:
        row = comparison_for_request(request=approved, on=QUOTED)[0]
        assert row.charge_share == Decimal("0.000")
        assert row.landed_total == row.line_total


# ---------------------------------------------------------------------------
# Context a price alone does not carry
# ---------------------------------------------------------------------------


class TestContextColumns:
    def test_an_expired_offer_is_shown_and_never_flagged_cheapest(
        self, grocery: Supplier, buyer: User, approved: PurchaseRequest, rice: InventoryItem
    ) -> None:
        """Flagging it would recommend something the award service refuses."""
        stale = create_supplier_quotation(
            supplier=grocery,
            recorded_by=buyer,
            request=approved,
            quoted_at=QUOTED,
            valid_until=QUOTED + datetime.timedelta(days=1),
            evidence_reference="قديم",
        )
        add_quotation_line(
            quotation=stale,
            item=rice,
            quantity=Decimal("120.000"),
            unit_price=Decimal("1.000000"),
        )
        submit_supplier_quotation(quotation=stale, actor=buyer)

        rows = compare_quotations(quotations=[stale], on=QUOTED + datetime.timedelta(days=30))
        assert rows[0].is_expired is True
        assert rows[0].is_cheapest_base_unit is False
        assert rows[0].is_cheapest_landed is False

    def test_a_draft_quotation_is_not_compared(
        self, grocery: Supplier, buyer: User, approved: PurchaseRequest, rice: InventoryItem
    ) -> None:
        drafted = create_supplier_quotation(
            supplier=grocery, recorded_by=buyer, request=approved, quoted_at=QUOTED
        )
        add_quotation_line(
            quotation=drafted,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        assert comparison_for_request(request=approved, on=QUOTED) == []

    def test_minimum_order_is_flagged_when_the_units_are_comparable(
        self,
        approved: PurchaseRequest,
        grocery: Supplier,
        buyer: User,
        rice: InventoryItem,
        sack: PackageUnit,
    ) -> None:
        from apps.procurement.services import create_supplier_item

        catalogue = create_supplier_item(
            supplier=grocery,
            item=rice,
            package_unit=sack,
            effective_from=datetime.date(2026, 1, 1),
            minimum_order_quantity=Decimal("10.000"),
            lead_time_days=3,
        )
        quotation = create_supplier_quotation(
            supplier=grocery,
            recorded_by=buyer,
            request=approved,
            quoted_at=QUOTED,
            evidence_reference="بريد",
        )
        add_quotation_line(
            quotation=quotation,
            item=rice,
            package_unit=sack,
            quantity=Decimal("4.000"),
            unit_price=Decimal("42000.000000"),
            supplier_item=catalogue,
        )
        submit_supplier_quotation(quotation=quotation, actor=buyer)

        row = comparison_for_request(request=approved, on=QUOTED)[0]
        assert row.minimum_order_quantity == Decimal("10.000")
        assert row.meets_minimum_order is False
        assert row.lead_time_days == 3

    def test_a_minimum_in_a_different_unit_is_not_pretended_to_be_comparable(
        self,
        approved: PurchaseRequest,
        grocery: Supplier,
        buyer: User,
        rice: InventoryItem,
        sack: PackageUnit,
    ) -> None:
        from apps.procurement.services import create_supplier_item

        catalogue = create_supplier_item(
            supplier=grocery,
            item=rice,
            package_unit=sack,
            effective_from=datetime.date(2026, 1, 1),
            minimum_order_quantity=Decimal("10.000"),
        )
        quotation = create_supplier_quotation(
            supplier=grocery,
            recorded_by=buyer,
            request=approved,
            quoted_at=QUOTED,
            evidence_reference="بريد",
        )
        # Quoted in kilograms; the catalogue minimum is in sacks.
        add_quotation_line(
            quotation=quotation,
            item=rice,
            quantity=Decimal("120.000"),
            unit_price=Decimal("1400.000000"),
            supplier_item=catalogue,
        )
        submit_supplier_quotation(quotation=quotation, actor=buyer)

        row = comparison_for_request(request=approved, on=QUOTED)[0]
        assert row.minimum_order_quantity is None
        assert row.meets_minimum_order is True


# ---------------------------------------------------------------------------
# The award
# ---------------------------------------------------------------------------


class TestTheAward:
    def test_nothing_selects_a_winner_by_itself(
        self,
        approved: PurchaseRequest,
        by_the_sack: SupplierQuotation,
        by_the_kilo: SupplierQuotation,
    ) -> None:
        """
        The comparison ranks and flags. Reading it changes nothing — PRC-016.
        """
        comparison_for_request(request=approved, on=QUOTED)
        approved.refresh_from_db()
        assert approved.awarded_quotation_id is None
        assert approved.award_reason == ""

    def test_the_dearer_per_unit_offer_may_be_awarded_with_a_reason(
        self,
        approved: PurchaseRequest,
        by_the_sack: SupplierQuotation,
        by_the_kilo: SupplierQuotation,
        buyer: User,
    ) -> None:
        awarded = award_quotation(
            request=approved,
            quotation=by_the_kilo,
            actor=buyer,
            reason="أرخص بعد النقل والتسليم أسرع",
            on=QUOTED,
        )
        assert awarded.awarded_quotation_id == by_the_kilo.pk
        assert awarded.awarded_by_id == buyer.pk
        assert awarded.awarded_at is not None
        assert awarded.award_reason == "أرخص بعد النقل والتسليم أسرع"

        by_the_kilo.refresh_from_db()
        assert by_the_kilo.status == SupplierQuotationStatus.AWARDED

    def test_an_award_without_a_reason_is_refused(
        self, approved: PurchaseRequest, by_the_kilo: SupplierQuotation, buyer: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            award_quotation(request=approved, quotation=by_the_kilo, actor=buyer, reason="   ")
        assert refused.value.code == "reason_required"

    def test_the_database_refuses_half_an_award(
        self, approved: PurchaseRequest, by_the_kilo: SupplierQuotation
    ) -> None:
        """Three of the four columns with the fourth missing is not a decision."""
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseRequest.objects.filter(pk=approved.pk).update(
                awarded_quotation=by_the_kilo, awarded_at=timezone.now()
            )

    def test_an_expired_quotation_cannot_be_awarded(
        self, grocery: Supplier, buyer: User, approved: PurchaseRequest, rice: InventoryItem
    ) -> None:
        stale = create_supplier_quotation(
            supplier=grocery,
            recorded_by=buyer,
            request=approved,
            quoted_at=QUOTED,
            valid_until=QUOTED,
            evidence_reference="قديم",
        )
        add_quotation_line(
            quotation=stale,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        submit_supplier_quotation(quotation=stale, actor=buyer)

        with pytest.raises(ValidationError) as refused:
            award_quotation(
                request=approved,
                quotation=stale,
                actor=buyer,
                reason="رخيص",
                on=QUOTED + datetime.timedelta(days=1),
            )
        assert refused.value.code == "quotation_expired"

    def test_a_declined_quotation_cannot_be_awarded(
        self, approved: PurchaseRequest, by_the_kilo: SupplierQuotation, buyer: User
    ) -> None:
        decline_supplier_quotation(quotation=by_the_kilo, actor=buyer, reason="لا")
        with pytest.raises(ValidationError) as refused:
            award_quotation(request=approved, quotation=by_the_kilo, actor=buyer, reason="ok")
        assert refused.value.code == "quotation_not_awardable"

    def test_a_quotation_offered_against_another_request_is_refused(
        self,
        approved: PurchaseRequest,
        grocery: Supplier,
        buyer: User,
        rice: InventoryItem,
    ) -> None:
        loose = create_supplier_quotation(
            supplier=grocery, recorded_by=buyer, quoted_at=QUOTED, evidence_reference="ورقة"
        )
        add_quotation_line(
            quotation=loose,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        submit_supplier_quotation(quotation=loose, actor=buyer)
        with pytest.raises(ValidationError) as refused:
            award_quotation(request=approved, quotation=loose, actor=buyer, reason="ok", on=QUOTED)
        assert refused.value.code == "quotation_not_for_request"

    def test_an_unapproved_request_cannot_be_awarded(
        self,
        branch: Branch,
        keeper: User,
        store: Warehouse,
        rice: InventoryItem,
        grocery: Supplier,
        buyer: User,
    ) -> None:
        drafted = create_purchase_request(
            branch=branch,
            requested_by=keeper,
            warehouse=store,
            required_date=REQUIRED,
            purpose="مسودة",
        )
        add_request_line(request=drafted, item=rice, entered_quantity=Decimal("1.000"))
        quotation = create_supplier_quotation(
            supplier=grocery,
            recorded_by=buyer,
            request=drafted,
            quoted_at=QUOTED,
            evidence_reference="ورقة",
        )
        add_quotation_line(
            quotation=quotation,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        submit_supplier_quotation(quotation=quotation, actor=buyer)
        with pytest.raises(ValidationError) as refused:
            award_quotation(
                request=drafted, quotation=quotation, actor=buyer, reason="ok", on=QUOTED
            )
        assert refused.value.code == "request_not_approved"

    def test_a_request_cannot_be_awarded_twice(
        self,
        approved: PurchaseRequest,
        by_the_sack: SupplierQuotation,
        by_the_kilo: SupplierQuotation,
        buyer: User,
    ) -> None:
        award_quotation(
            request=approved,
            quotation=by_the_kilo,
            actor=buyer,
            reason="أرخص بعد النقل",
            on=QUOTED,
        )
        with pytest.raises(ValidationError) as refused:
            award_quotation(
                request=approved,
                quotation=by_the_sack,
                actor=buyer,
                reason="غيّرت رأيي",
                on=QUOTED,
            )
        assert refused.value.code == "already_awarded"

    def test_the_award_is_audited_on_both_documents(
        self, approved: PurchaseRequest, by_the_kilo: SupplierQuotation, buyer: User
    ) -> None:
        award_quotation(
            request=approved,
            quotation=by_the_kilo,
            actor=buyer,
            reason="أرخص بعد النقل",
            on=QUOTED,
        )
        assert AuditEvent.objects.filter(
            target_type="procurement.PurchaseRequest",
            target_id=str(approved.pk),
            action="APPROVED",
        ).exists()
        assert AuditEvent.objects.filter(
            target_type="procurement.SupplierQuotation",
            target_id=str(by_the_kilo.pk),
            action="APPROVED",
        ).exists()


# ---------------------------------------------------------------------------
# Nothing moves
# ---------------------------------------------------------------------------


class TestNoLedgerEffect:
    def test_awarding_creates_no_stock_and_no_journal(
        self, approved: PurchaseRequest, by_the_kilo: SupplierQuotation, buyer: User
    ) -> None:
        award_quotation(
            request=approved,
            quotation=by_the_kilo,
            actor=buyer,
            reason="أرخص بعد النقل",
            on=QUOTED,
        )
        assert StockMovement.objects.count() == 0
        assert StockLedgerEntry.objects.count() == 0
        assert JournalEntry.objects.count() == 0


# ---------------------------------------------------------------------------
# Permissions and screens
# ---------------------------------------------------------------------------


class TestPermissionsAndScreens:
    def test_only_purchasing_and_manager_hold_the_award_permission(self) -> None:
        assert AWARD_QUOTATION in permissions_for_role(Role.PURCHASING)
        assert AWARD_QUOTATION in permissions_for_role(Role.MANAGER)
        assert AWARD_QUOTATION not in permissions_for_role(Role.ACCOUNTING_MANAGER)
        assert AWARD_QUOTATION not in permissions_for_role(Role.STOREKEEPER)

    def test_the_comparison_screen_shows_both_cheapest_flags(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        approved: PurchaseRequest,
        by_the_sack: SupplierQuotation,
        by_the_kilo: SupplierQuotation,
    ) -> None:
        body = (
            client_for(buyer)
            .get(reverse("procurement:quotation_comparison", args=[approved.pk]))
            .content.decode()
        )
        assert "1400.000000" in body
        assert "1525.000000" in body
        assert "الأرخص للوحدة" in body
        assert "الأرخص بعد النقل" in body

    def test_an_hx_request_returns_only_the_table(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        approved: PurchaseRequest,
        by_the_kilo: SupplierQuotation,
    ) -> None:
        body = (
            client_for(buyer)
            .get(reverse("procurement:quotation_comparison", args=[approved.pk]), headers=HX)
            .content.decode()
        )
        assert "<html" not in body.lower()
        assert "comparison-results" in body

    def test_awarding_through_the_screen_records_the_reason(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        approved: PurchaseRequest,
        by_the_kilo: SupplierQuotation,
    ) -> None:
        response = client_for(buyer).post(
            reverse("procurement:quotation_award", args=[approved.pk]),
            {"quotation": str(by_the_kilo.pk), "reason": "التسليم أسرع"},
        )
        assert response.status_code == 302
        approved.refresh_from_db()
        assert approved.awarded_quotation_id == by_the_kilo.pk
        assert approved.award_reason == "التسليم أسرع"

    def test_a_storekeeper_cannot_award_through_the_route(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        approved: PurchaseRequest,
        by_the_kilo: SupplierQuotation,
    ) -> None:
        """Hiding the button is presentation; this is the protection."""
        response = client_for(keeper).post(
            reverse("procurement:quotation_award", args=[approved.pk]),
            {"quotation": str(by_the_kilo.pk), "reason": "أريد"},
        )
        assert response.status_code in {302, 403}
        approved.refresh_from_db()
        assert approved.awarded_quotation_id is None

    def test_awarding_is_post_only(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        approved: PurchaseRequest,
        by_the_kilo: SupplierQuotation,
    ) -> None:
        response = client_for(buyer).get(reverse("procurement:quotation_award", args=[approved.pk]))
        assert response.status_code == 405
