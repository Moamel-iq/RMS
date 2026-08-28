"""
The two supplier terms the reports read: a settlement floor and a reset date.

`minimum_settlement_percent` is how much of an invoice must be paid by the day
it falls due. `balance_reset_date` is the day the account starts again. Both
are nullable, and the sharpest tests here are about what each *refuses* and
what neither destroys — a floor with no due date to be tested at, and a reset
that quietly dropped the documents before it, are the two ways these fields
could look like they worked while lying.
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

from apps.organizations.models import Organization
from apps.procurement.models import Supplier
from apps.procurement.services import create_supplier, update_supplier
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def meat(organization: Organization) -> Supplier:
    return create_supplier(
        organization=organization,
        code="meat-01",
        name="مورد اللحوم",
        payment_terms_days=30,
    )


# ---------------------------------------------------------------------------
# The settlement floor
# ---------------------------------------------------------------------------


class TestTheSettlementFloor:
    def test_it_is_absent_until_somebody_agrees_one(self, meat: Supplier) -> None:
        """
        NULL rather than zero, because they are different statements.

        Zero would mean "we agreed you may pay nothing by the due date", which
        is a term somebody negotiated. NULL means nobody negotiated anything,
        and the breach report has nothing to test.
        """
        assert meat.minimum_settlement_percent is None

    def test_it_is_stored_and_read_back_exactly(self, organization: Organization) -> None:
        supplier = create_supplier(
            organization=organization,
            code="veg-01",
            name="مورد الخضار",
            payment_terms_days=45,
            minimum_settlement_percent=Decimal("50"),
        )
        assert Supplier.objects.get(pk=supplier.pk).minimum_settlement_percent == Decimal("50")

    @pytest.mark.parametrize("percent", ["-1", "100.01", "250"])
    def test_a_share_outside_zero_to_one_hundred_is_refused(
        self, organization: Organization, percent: str
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_supplier(
                organization=organization,
                code="oil-01",
                name="مورد الزيت",
                payment_terms_days=30,
                minimum_settlement_percent=Decimal(percent),
            )
        assert caught.value.code == "settlement_percent_out_of_range"

    def test_a_floor_without_a_payment_term_is_refused(self, organization: Organization) -> None:
        """
        With terms of zero the invoice is due the day it is raised, so "half of
        it by the due date" is a demand for half on delivery dressed as a
        concession. Refused rather than stored as a rule nothing could test.
        """
        with pytest.raises(ValidationError) as caught:
            create_supplier(
                organization=organization,
                code="ice-01",
                name="مورد الثلج",
                payment_terms_days=0,
                minimum_settlement_percent=Decimal("50"),
            )
        assert caught.value.code == "settlement_percent_needs_terms"

    def test_the_database_refuses_it_too(self, meat: Supplier) -> None:
        """The service is not the only guard: the constraint is the real one."""
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Supplier.objects.filter(pk=meat.pk).update(
                    minimum_settlement_percent=Decimal("150")
                )

    def test_the_database_refuses_a_floor_with_no_term(self, meat: Supplier) -> None:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Supplier.objects.filter(pk=meat.pk).update(
                    payment_terms_days=0, minimum_settlement_percent=Decimal("50")
                )

    def test_it_can_be_corrected_after_the_fact(self, meat: Supplier) -> None:
        """
        Unlike the payment term, which is versioned and approved, the floor is
        read *forward* — it tests today's open invoices — so correcting it
        restates no posted document and needs no version.
        """
        update_supplier(supplier=meat, name=meat.name, minimum_settlement_percent=Decimal("60"))
        meat.refresh_from_db()
        assert meat.minimum_settlement_percent == Decimal("60")

        update_supplier(supplier=meat, name=meat.name, minimum_settlement_percent=None)
        meat.refresh_from_db()
        assert meat.minimum_settlement_percent is None


# ---------------------------------------------------------------------------
# The reset date
# ---------------------------------------------------------------------------


class TestTheResetDate:
    def test_it_is_optional_and_absent_by_default(self, meat: Supplier) -> None:
        assert meat.balance_reset_date is None

    def test_it_is_stored_and_cleared(self, meat: Supplier) -> None:
        reset_on = datetime.date(2026, 1, 1)
        update_supplier(supplier=meat, name=meat.name, balance_reset_date=reset_on)
        meat.refresh_from_db()
        assert meat.balance_reset_date == reset_on

        update_supplier(supplier=meat, name=meat.name, balance_reset_date=None)
        meat.refresh_from_db()
        assert meat.balance_reset_date is None

    def test_it_destroys_no_document(self, meat: Supplier) -> None:
        """
        The field changes what a statement *shows*, never what the ledger
        *holds*. Nothing in the service touches a posted row, and this is the
        assertion that would fail first if somebody ever made it.
        """
        from apps.procurement.models import SupplierInvoice

        before = SupplierInvoice.objects.filter(supplier=meat).count()
        update_supplier(supplier=meat, name=meat.name, balance_reset_date=datetime.date(2026, 6, 1))
        assert SupplierInvoice.objects.filter(supplier=meat).count() == before


# ---------------------------------------------------------------------------
# Existing suppliers
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_a_supplier_created_without_either_field_still_saves(
        self, organization: Organization
    ) -> None:
        supplier = create_supplier(organization=organization, code="old-01", name="مورد قديم")
        assert supplier.minimum_settlement_percent is None
        assert supplier.balance_reset_date is None
        assert supplier.payment_terms_days == 0

    def test_updating_an_old_supplier_leaves_both_absent(self, meat: Supplier) -> None:
        update_supplier(supplier=meat, name="اسم مصحَّح")
        meat.refresh_from_db()
        assert meat.name == "اسم مصحَّح"
        assert meat.minimum_settlement_percent is None
        assert meat.balance_reset_date is None


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


class TestTheSupplierScreen:
    def test_both_fields_are_on_the_create_form(
        self, manager: User, client_for: Callable[[User], Client]
    ) -> None:
        body = client_for(manager).get(reverse("procurement:supplier_create")).content.decode()
        assert "الحد الأدنى لنسبة السداد عند الاستحقاق" in body
        assert "تاريخ تصفير الحساب" in body

    def test_both_fields_are_on_the_edit_form(
        self, manager: User, meat: Supplier, client_for: Callable[[User], Client]
    ) -> None:
        body = (
            client_for(manager)
            .get(reverse("procurement:supplier_update", args=[meat.pk]))
            .content.decode()
        )
        assert "الحد الأدنى لنسبة السداد عند الاستحقاق" in body
        assert "تاريخ تصفير الحساب" in body

    def test_the_form_refuses_a_floor_with_no_term(
        self, manager: User, organization: Organization, client_for: Callable[[User], Client]
    ) -> None:
        response = client_for(manager).post(
            reverse("procurement:supplier_create"),
            {
                "organization": organization.pk,
                "code": "NOTERM-01",
                "name": "بلا مهلة",
                "payment_terms_days": "0",
                "minimum_settlement_percent": "50",
            },
        )
        assert response.status_code == 200
        assert (
            "لا يمكن تحديد حد أدنى للسداد بلا مهلة سداد أكبر من صفر." in response.content.decode()
        )
        assert not Supplier.objects.filter(code="NOTERM-01").exists()

    def test_the_list_shows_the_floor_beside_the_term(
        self, manager: User, meat: Supplier, client_for: Callable[[User], Client]
    ) -> None:
        update_supplier(supplier=meat, name=meat.name, minimum_settlement_percent=Decimal("50"))
        body = client_for(manager).get(reverse("procurement:supplier_list")).content.decode()
        assert "30 يوم" in body
        assert "الحد الأدنى 50٪" in body
