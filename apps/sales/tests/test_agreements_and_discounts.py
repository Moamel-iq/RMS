"""
The two pieces of arithmetic Phase 4 turns on, and the constraints behind them.

* **A commission is charged on one of four contractual bases**, and the two
  that happen to compute identically today are still separate values.
* **A discount divides into two shares that must close**, and the second is a
  residual so it closes at every amount rather than at most of them.

The rest of this file is the database refusing the contradictions a service
check alone would let through under concurrency.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounting.models import CostCenter
from apps.organizations.models import Branch, Organization
from apps.sales.agreements import (
    CommissionInputs,
    basis_amount_for,
    commission_for,
    inputs_from,
    preview,
    resolve_agreement,
)
from apps.sales.discounts import applicable_programs, split_for
from apps.sales.models import (
    CommissionBasis,
    DeliveryAgreement,
    DeliveryApplication,
    DiscountProgram,
    SalesChannelCategory,
    TenderDestination,
)
from apps.sales.services import (
    close_delivery_agreement,
    create_delivery_agreement,
    create_delivery_application,
    create_discount_program,
    create_sales_channel,
    set_application_branch_setting,
)
from apps.users.models import User

TODAY = datetime.date(2026, 8, 19)
JANUARY = datetime.date(2026, 1, 1)


def _inputs(basis: str, **overrides: object) -> CommissionInputs:
    base = {
        "gross_amount": Decimal("10000"),
        "restaurant_discount": Decimal("1000"),
        "application_discount": Decimal("500"),
        "order_count": 1,
        "commission_percent": Decimal("15"),
        "fixed_fee_per_order": Decimal("0"),
        "commission_basis": basis,
    }
    base.update(overrides)
    return CommissionInputs(**base)  # type: ignore[arg-type]


class TestTheCommissionBasis:
    """Pure arithmetic — no database."""

    def test_gross_list_amount_ignores_every_discount(self) -> None:
        assert basis_amount_for(_inputs(CommissionBasis.GROSS_LIST_AMOUNT)) == Decimal("10000")

    def test_after_restaurant_discount_subtracts_only_the_restaurant_share(self) -> None:
        assert basis_amount_for(_inputs(CommissionBasis.AFTER_RESTAURANT_DISCOUNT)) == Decimal(
            "9000"
        )

    def test_after_all_discounts_subtracts_both(self) -> None:
        assert basis_amount_for(_inputs(CommissionBasis.AFTER_ALL_DISCOUNTS)) == Decimal("8500")

    def test_customer_paid_amount_computes_the_same_and_is_still_its_own_value(self) -> None:
        """
        Identical arithmetic in Release 1, deliberately separate values.

        They are different contractual claims and they diverge the moment a
        delivery fee or a tip enters the model; collapsing them now would make
        that later divergence a silent restatement of every historical
        agreement.
        """
        both = basis_amount_for(_inputs(CommissionBasis.AFTER_ALL_DISCOUNTS))
        paid = basis_amount_for(_inputs(CommissionBasis.CUSTOMER_PAID_AMOUNT))
        assert both == paid
        assert (
            CommissionBasis.AFTER_ALL_DISCOUNTS.value != CommissionBasis.CUSTOMER_PAID_AMOUNT.value
        )
        # Both survive in the vocabulary; neither is an alias for the other.
        assert {"AFTER_ALL_DISCOUNTS", "CUSTOMER_PAID_AMOUNT"} <= set(CommissionBasis.values)

    def test_an_unknown_basis_raises_rather_than_defaulting(self) -> None:
        """
        No fallback. Answering anyway would accrue a commission nobody agreed
        to, against a real company, silently.
        """
        with pytest.raises(ValueError, match="unknown commission basis"):
            basis_amount_for(_inputs("AFTER_ALL_DISCOUNT"))

    def test_the_fixed_fee_multiplies_by_the_order_count(self) -> None:
        result = commission_for(
            _inputs(
                CommissionBasis.GROSS_LIST_AMOUNT,
                commission_percent=Decimal("10"),
                fixed_fee_per_order=Decimal("500"),
                order_count=4,
            )
        )
        assert result.percentage_component == Decimal("1000")
        assert result.fixed_component == Decimal("2000")
        assert result.total == Decimal("3000.000")

    def test_the_total_is_quantized_once_rather_than_per_component(self) -> None:
        """
        Rounding each component and adding is the error ADR-006 names. Here the
        two halves are each a third of a fils; rounded separately they would
        sum to a different figure from the one rounded once.
        """
        result = commission_for(
            _inputs(
                CommissionBasis.GROSS_LIST_AMOUNT,
                gross_amount=Decimal("1000"),
                commission_percent=Decimal("0.0555"),
                fixed_fee_per_order=Decimal("0.0004"),
            )
        )
        assert result.percentage_component == Decimal("0.555000")
        assert result.total == Decimal("0.555")


class TestTheDiscountSplit:
    """Pure arithmetic — no database."""

    def _program(self, **overrides: Any) -> DiscountProgram:
        """
        An unsaved programme. Unsaved on purpose: the split arithmetic reads
        fields and touches no database, and giving it a real row would make a
        pure-function test depend on migrations.
        """
        defaults: dict[str, Any] = {
            "discount_percent": Decimal("10"),
            "discount_amount": None,
            "maximum_amount": None,
            "restaurant_funded_share": Decimal("100"),
            "application_funded_share": Decimal("0"),
        }
        defaults.update(overrides)
        return DiscountProgram(**defaults)

    def test_no_program_is_a_zero_split_rather_than_a_missing_one(self) -> None:
        split = split_for(None, gross_amount=Decimal("10000"))
        assert split.gross_discount == Decimal("0")
        assert split.restaurant_funded == Decimal("0")
        assert split.application_funded == Decimal("0")

    def test_a_restaurant_funded_percentage(self) -> None:
        split = split_for(self._program(), gross_amount=Decimal("10000"))
        assert split.gross_discount == Decimal("1000.000")
        assert split.restaurant_funded == Decimal("1000.000")
        assert split.application_funded == Decimal("0.000")

    def test_an_application_funded_discount_costs_the_restaurant_nothing(self) -> None:
        split = split_for(
            self._program(
                restaurant_funded_share=Decimal("0"),
                application_funded_share=Decimal("100"),
            ),
            gross_amount=Decimal("5000"),
        )
        assert split.restaurant_funded == Decimal("0.000")
        assert split.application_funded == Decimal("500.000")

    def test_the_second_share_is_a_residual_so_the_split_always_closes(self) -> None:
        """
        The load-bearing arithmetic decision.

        A third and two thirds of 1,000 rated independently round to 333.333
        and 666.667, which happens to close — but 333.333 and 666.666 is what
        truncation gives, and a fils funded by nobody is a fils that surfaces
        as a settlement variance. Taking one share and subtracting makes the
        split close by construction, at every amount.
        """
        split = split_for(
            self._program(
                discount_percent=None,
                discount_amount=Decimal("1000"),
                restaurant_funded_share=Decimal("33.333333"),
                application_funded_share=Decimal("66.666667"),
            ),
            gross_amount=Decimal("10000"),
        )
        assert split.restaurant_funded + split.application_funded == split.gross_discount

    def test_a_maximum_caps_a_percentage(self) -> None:
        split = split_for(
            self._program(discount_percent=Decimal("50"), maximum_amount=Decimal("2000")),
            gross_amount=Decimal("10000"),
        )
        assert split.gross_discount == Decimal("2000.000")

    def test_a_discount_cannot_exceed_what_is_being_discounted(self) -> None:
        """
        Capped rather than refused: a fixed-amount promotion meeting a small
        order is ordinary, and a negative sale is not.
        """
        split = split_for(
            self._program(discount_percent=None, discount_amount=Decimal("5000")),
            gross_amount=Decimal("3000"),
        )
        assert split.gross_discount == Decimal("3000.000")

    def test_a_manual_discount_is_entirely_restaurant_funded(self) -> None:
        """
        A delivery company does not reimburse a discount it never agreed to.
        Allowing a manual application-funded discount would let a cashier
        create a receivable out of a keystroke.
        """
        split = split_for(None, gross_amount=Decimal("10000"), manual_amount=Decimal("750"))
        assert split.restaurant_funded == Decimal("750.000")
        assert split.application_funded == Decimal("0")


@pytest.fixture
def application(organization: Organization) -> DeliveryApplication:
    return create_delivery_application(
        organization=organization,
        code="DEMO-APPONE",
        name_ar="تطبيق تجريبي أول",
        settlement_cycle_days=14,
    )


@pytest.fixture
def agreement(branch: Branch, application: DeliveryApplication) -> DeliveryAgreement:
    return create_delivery_agreement(
        branch=branch,
        delivery_application=application,
        effective_from=JANUARY,
        commission_percent=Decimal("15"),
        commission_basis=CommissionBasis.GROSS_LIST_AMOUNT,
        evidence_reference="عقد تجريبي ١",
    )


@pytest.mark.django_db
class TestAgreementResolution:
    def test_the_agreement_in_force_on_the_date_is_the_one_returned(
        self, branch: Branch, application: DeliveryApplication, agreement: DeliveryAgreement
    ) -> None:
        found = resolve_agreement(
            branch_id=branch.pk, delivery_application_id=application.pk, on_date=TODAY
        )
        assert found == agreement

    def test_a_date_before_the_agreement_resolves_to_nothing(
        self, branch: Branch, application: DeliveryApplication, agreement: DeliveryAgreement
    ) -> None:
        """
        `None`, never a default rate. A default would be a commission nobody
        agreed to; the sale refuses to post a line it cannot price, which puts
        the failure in front of whoever can fix it.
        """
        assert (
            resolve_agreement(
                branch_id=branch.pk,
                delivery_application_id=application.pk,
                on_date=datetime.date(2025, 12, 31),
            )
            is None
        )

    def test_two_agreements_cannot_overlap_for_one_branch_and_application(
        self, branch: Branch, application: DeliveryApplication, agreement: DeliveryAgreement
    ) -> None:
        """
        Refused by the exclusion constraint. If two could overlap, resolution
        would have to pick — and whatever it picked would decide an expense on
        every order, differently depending on insertion order.
        """
        with pytest.raises(IntegrityError), transaction.atomic():
            create_delivery_agreement(
                branch=branch,
                delivery_application=application,
                effective_from=datetime.date(2026, 6, 1),
                commission_percent=Decimal("18"),
                evidence_reference="عقد تجريبي ٢",
            )

    def test_a_replacement_may_start_the_day_after_the_first_ends(
        self, branch: Branch, application: DeliveryApplication, agreement: DeliveryAgreement
    ) -> None:
        close_delivery_agreement(
            agreement=agreement, effective_to=datetime.date(2026, 5, 31), reason="إعادة تفاوض"
        )
        replacement = create_delivery_agreement(
            branch=branch,
            delivery_application=application,
            effective_from=datetime.date(2026, 6, 1),
            commission_percent=Decimal("18"),
            evidence_reference="عقد تجريبي ٢",
        )
        assert (
            resolve_agreement(
                branch_id=branch.pk, delivery_application_id=application.pk, on_date=TODAY
            )
            == replacement
        )
        # The old one still answers for its own dates. That is the point.
        assert (
            resolve_agreement(
                branch_id=branch.pk,
                delivery_application_id=application.pk,
                on_date=datetime.date(2026, 3, 1),
            )
            == agreement
        )

    def test_an_agreement_needs_its_evidence(
        self, branch: Branch, application: DeliveryApplication
    ) -> None:
        """
        The only master in this module that insists on evidence, because it is
        the only one that charges an expense on every order from the day it
        starts.
        """
        with pytest.raises(ValidationError) as caught:
            create_delivery_agreement(
                branch=branch,
                delivery_application=application,
                effective_from=JANUARY,
                commission_percent=Decimal("15"),
            )
        assert caught.value.code == "evidence_required"

    def test_the_snapshot_replays_without_the_agreement_row(
        self, agreement: DeliveryAgreement
    ) -> None:
        """
        A posted commission is re-derivable from the line's own stored values.

        `inputs_from` is what a sales line snapshots; `commission_for` replays
        it. Neither needs the agreement table, which is what makes a
        three-year-old accrual arguable.
        """
        snapshot = inputs_from(
            agreement,
            gross_amount=Decimal("20000"),
            restaurant_discount=Decimal("0"),
            application_discount=Decimal("0"),
            order_count=1,
        )
        assert commission_for(snapshot).total == Decimal("3000.000")

    def test_the_preview_shows_the_money_rather_than_the_rate(
        self, agreement: DeliveryAgreement
    ) -> None:
        shown = preview(agreement, gross_amount=Decimal("25000"))
        assert shown.result.total == Decimal("3750.000")
        assert shown.net_to_restaurant == Decimal("21250.000")


@pytest.mark.django_db
class TestApplicationMaster:
    def test_an_application_carries_no_balance_field(self) -> None:
        """
        The absence is the design. A stored balance can disagree with the
        entries that produced it, and the disagreement surfaces during a
        settlement argument.
        """
        fields = {field.name for field in DeliveryApplication._meta.get_fields()}
        for absent in ("balance", "current_balance", "outstanding", "receivable_balance"):
            assert absent not in fields

    def test_an_application_carries_no_commission_rate(self) -> None:
        fields = {field.name for field in DeliveryApplication._meta.get_fields()}
        for absent in ("commission_percent", "commission_rate", "commission"):
            assert absent not in fields

    def test_a_branch_of_another_organization_cannot_be_activated(
        self, application: DeliveryApplication, other_organization: Organization
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
            set_application_branch_setting(application=application, branch=foreign)
        assert caught.value.code == "branch_organization_mismatch"


@pytest.mark.django_db
class TestDiscountFunding:
    def test_shares_that_do_not_close_are_refused_by_the_service(
        self, organization: Organization
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_discount_program(
                organization=organization,
                code="DEMO-BADSPLIT",
                name_ar="تقسيم ناقص",
                effective_from=JANUARY,
                discount_percent=Decimal("10"),
                restaurant_funded_share=Decimal("60"),
                application_funded_share=Decimal("30"),
            )
        assert caught.value.code == "funding_does_not_close"

    def test_shares_that_do_not_close_are_refused_by_the_database_as_well(
        self, organization: Organization
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            DiscountProgram.objects.create(
                organization=organization,
                code="DEMO-BYPASS",
                name_ar="التفاف",
                effective_from=JANUARY,
                discount_percent=Decimal("10"),
                restaurant_funded_share=Decimal("60"),
                application_funded_share=Decimal("30"),
            )

    def test_application_funding_must_name_the_application(
        self, organization: Organization
    ) -> None:
        """
        A "50% funded by the app" promotion with no application attached could
        be applied to a cash sale in the hall, and the receivable it implies
        would be owed by nobody.
        """
        with pytest.raises(ValidationError) as caught:
            create_discount_program(
                organization=organization,
                code="DEMO-ORPHAN",
                name_ar="ممول من لا أحد",
                effective_from=JANUARY,
                discount_percent=Decimal("20"),
                restaurant_funded_share=Decimal("50"),
                application_funded_share=Decimal("50"),
            )
        assert caught.value.code == "application_funding_needs_an_application"

    def test_a_percentage_and_an_amount_are_mutually_exclusive(
        self, organization: Organization
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_discount_program(
                organization=organization,
                code="DEMO-BOTH",
                name_ar="نسبة ومبلغ",
                effective_from=JANUARY,
                discount_percent=Decimal("10"),
                discount_amount=Decimal("500"),
            )
        assert caught.value.code == "discount_value_ambiguous"

    def test_a_shared_discount_is_accepted_when_it_names_its_application(
        self, organization: Organization, application: DeliveryApplication
    ) -> None:
        program = create_discount_program(
            organization=organization,
            code="DEMO-SHARED",
            name_ar="خصم مشترك",
            effective_from=JANUARY,
            discount_percent=Decimal("20"),
            restaurant_funded_share=Decimal("50"),
            application_funded_share=Decimal("50"),
            delivery_application=application,
        )
        assert program.is_shared is True

        split = split_for(program, gross_amount=Decimal("10000"))
        assert split.gross_discount == Decimal("2000.000")
        assert split.restaurant_funded == Decimal("1000.000")
        assert split.application_funded == Decimal("1000.000")


@pytest.mark.django_db
class TestDiscountApplicability:
    def test_an_application_funded_program_cannot_reach_a_line_with_no_application(
        self,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        hall_cost_center: CostCenter,
    ) -> None:
        """
        No application on the line means nobody to reimburse the discount, so
        an application-scoped programme is not even a candidate.
        """
        create_discount_program(
            organization=organization,
            code="DEMO-APPONLY",
            name_ar="عرض التطبيق",
            effective_from=JANUARY,
            discount_percent=Decimal("15"),
            restaurant_funded_share=Decimal("0"),
            application_funded_share=Decimal("100"),
            delivery_application=application,
        )
        hall = create_sales_channel(
            organization=organization,
            code="DINE-IN",
            name_ar="الصالة",
            category=SalesChannelCategory.DINE_IN,
            cost_center=hall_cost_center,
            default_tender=TenderDestination.CASH,
        )
        cash_line = applicable_programs(
            organization_id=organization.pk,
            branch_id=branch.pk,
            on_date=TODAY,
            channel_id=hall.pk,
        )
        assert list(cash_line) == []

        application_line = applicable_programs(
            organization_id=organization.pk,
            branch_id=branch.pk,
            on_date=TODAY,
            delivery_application_id=application.pk,
        )
        assert [row.code for row in application_line] == ["DEMO-APPONLY"]

    def test_an_unrestricted_program_covers_everything(
        self, organization: Organization, branch: Branch, second_branch: Branch
    ) -> None:
        """
        A `NULL` on an axis means no restriction on that axis. Forcing an
        operator to enumerate every channel to express "everywhere" would
        guarantee that a channel added later silently fell outside it.
        """
        create_discount_program(
            organization=organization,
            code="DEMO-RAMADAN",
            name_ar="عرض رمضان",
            effective_from=JANUARY,
            discount_percent=Decimal("10"),
        )
        for target in (branch, second_branch):
            found = applicable_programs(
                organization_id=organization.pk, branch_id=target.pk, on_date=TODAY
            )
            assert [row.code for row in found] == ["DEMO-RAMADAN"]


@pytest.mark.django_db
class TestNavigationAndAuthority:
    def test_the_three_checkpoint_two_entries_are_active(self) -> None:
        from django.urls import reverse

        from apps.core.navigation import MODULES

        sales = next(module for module in MODULES if module.key == "sales")
        for label in ("تطبيقات التوصيل", "العمولات والاتفاقيات", "الخصومات"):
            section = next(row for row in sales.sections if str(row.label) == label)
            assert section.available is True, f"{label} is still inert"
            assert reverse(section.url_name)

    def test_the_module_still_has_exactly_twelve_sections(self) -> None:
        """
        The shape of the finished module, which does not move.

        How many are *active* is asserted as an invariant rather than a count
        in `test_menu_and_channels.py`: a count is true for one checkpoint and
        has to be edited by the next, which teaches nobody anything.
        """
        from apps.core.navigation import MODULES

        sales = next(module for module in MODULES if module.key == "sales")
        assert len(sales.sections) == 12

    def test_a_cashier_may_not_touch_agreements_or_discounts(self, cashier: User) -> None:
        """A discount a till can invent is a discount nobody approved."""
        from apps.sales.permissions import MANAGE_SALES_AGREEMENTS, MANAGE_SALES_DISCOUNTS

        assert not cashier.has_perm(MANAGE_SALES_AGREEMENTS)
        assert not cashier.has_perm(MANAGE_SALES_DISCOUNTS)

    def test_an_outsider_reaches_no_application(
        self, outsider: User, application: DeliveryApplication
    ) -> None:
        from apps.organizations.authorization import OutOfScope
        from apps.sales.selectors import resolve_delivery_application, visible_delivery_applications

        assert list(visible_delivery_applications(outsider)) == []
        with pytest.raises(OutOfScope):
            resolve_delivery_application(outsider, application.pk)
