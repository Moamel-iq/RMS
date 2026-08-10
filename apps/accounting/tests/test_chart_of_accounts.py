"""Chart of accounts and cost centres (ADR-014, ADR-015)."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from apps.accounting.management.commands.seed_chart_of_accounts import (
    CASH_ROUNDING_ACCOUNT_CODE,
)
from apps.accounting.models import Account, AccountClass, CostCenter
from apps.accounting.services import archive_account, create_account, create_cost_center
from apps.organizations.models import Branch, Organization

pytestmark = pytest.mark.django_db


class TestSeed:
    def test_the_chart_is_seeded(self, organization: Organization, chart: None) -> None:
        # 63: the original 46, plus Task 1.3's inventory and opening-equity
        # branches (eight accounts), plus Task 1.4's goods-received-not-invoiced
        # liability and the three consumption leaves with their parents (six),
        # plus Task 1.5's transfer-shortage loss leaf with its parents (three).
        assert Account.objects.filter(organization=organization).count() == 63

    def test_the_seed_is_idempotent(self, organization: Organization, chart: None) -> None:
        call_command("seed_chart_of_accounts", organization="KM", verbosity=0)
        assert Account.objects.filter(organization=organization).count() == 63

    def test_the_cash_rounding_account_exists_though_the_policy_is_off(
        self, organization: Organization, chart: None
    ) -> None:
        """
        ADR-012: enabling cash rounding later must fail loudly if the account
        is missing, not discover it mid-settlement.
        """
        from apps.core.money import CASH_ROUNDING_ENABLED

        assert CASH_ROUNDING_ENABLED is False
        account = Account.objects.get(organization=organization, code=CASH_ROUNDING_ACCOUNT_CODE)
        assert account.is_postable
        assert account.account_class == AccountClass.OTHER

    def test_the_six_cost_centers_are_seeded(self, organization: Organization, chart: None) -> None:
        codes = set(
            CostCenter.objects.filter(organization=organization).values_list("code", flat=True)
        )
        assert codes == {"KITCHEN", "HALL", "WAREHOUSE", "DELIVERY", "ADMIN", "HR"}

    def test_default_cost_center_policy_follows_the_account_class(
        self, organization: Organization, chart: None
    ) -> None:
        requires = {
            account.code: account.requires_cost_center
            for account in Account.objects.filter(organization=organization, is_postable=True)
        }
        assert requires["4-01-01-001"] is True  # revenue
        assert requires["5-01-01-001"] is True  # COGS
        assert requires["6-01-01-001"] is True  # operating expense
        assert requires["1-01-01-001"] is False  # cash
        assert requires["1-02-01-001"] is False  # receivable
        assert requires["2-01-01-001"] is False  # payable
        assert requires["3-01-01-001"] is False  # equity
        assert requires["8-01-01-001"] is False  # clearing


class TestCodeStructure:
    def test_only_detail_codes_are_postable(self, organization: Organization, chart: None) -> None:
        for account in Account.objects.filter(organization=organization):
            assert account.is_postable == (account.code.count("-") == 3), account.code

    def test_codes_are_strings_not_numbers(self, organization: Organization, chart: None) -> None:
        """Leading zeros are significant; arithmetic on a code is always a bug."""
        account = Account.objects.get(organization=organization, code="1-01-01-001")
        assert isinstance(account.code, str)
        assert account.code == "1-01-01-001"

    def test_a_child_sits_under_its_code_prefix(
        self, organization: Organization, chart: None
    ) -> None:
        account = Account.objects.get(organization=organization, code="1-01-01-001")
        assert account.parent is not None
        assert account.parent.code == "1-01-01"
        assert account.parent.parent is not None
        assert account.parent.parent.code == "1-01"

    def test_a_malformed_code_is_refused(self, organization: Organization) -> None:
        with pytest.raises(ValidationError):
            create_account(organization=organization, code="1-1-1-1", name_ar="س", name_en="S")

    def test_an_unknown_class_is_refused(self, organization: Organization) -> None:
        with pytest.raises(ValidationError) as exc:
            create_account(organization=organization, code="0", name_ar="س", name_en="S")
        assert exc.value.code in {"unknown_account_class", "invalid"}

    def test_a_missing_parent_is_refused(self, organization: Organization, chart: None) -> None:
        with pytest.raises(ValidationError) as exc:
            create_account(organization=organization, code="1-99-99-001", name_ar="س", name_en="S")
        assert exc.value.code == "missing_parent"

    def test_the_database_refuses_a_postable_flag_that_disagrees_with_the_code(
        self, organization: Organization, chart: None
    ) -> None:
        """The flag and the code's level can never drift apart."""
        group = Account.objects.get(organization=organization, code="1-01-01")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounting_account SET is_postable = TRUE WHERE id = %s",
                    [group.pk],
                )


class TestScoping:
    def test_codes_are_unique_within_an_organization(
        self, organization: Organization, chart: None
    ) -> None:
        with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
            create_account(
                organization=organization, code="1-01-01-001", name_ar="مكرر", name_en="Dup"
            )

    def test_two_organizations_may_use_the_same_code(
        self, organization: Organization, other_organization: Organization, chart: None
    ) -> None:
        call_command("seed_chart_of_accounts", organization="RIVAL", verbosity=0)
        km = Account.objects.get(organization=organization, code="1-01-01-001")
        rival = Account.objects.get(organization=other_organization, code="1-01-01-001")
        assert km.pk != rival.pk
        assert km.code == rival.code

    def test_codes_stay_reserved_after_archiving(
        self, organization: Organization, chart: None
    ) -> None:
        """
        An archived code must never be reissued to mean something else, or a
        historic report would silently change meaning.
        """
        account = Account.objects.get(organization=organization, code="6-01-02-001")
        archive_account(account=account, reason="no longer used")
        with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
            create_account(
                organization=organization, code="6-01-02-001", name_ar="جديد", name_en="New"
            )

    def test_cost_center_codes_are_unique_per_organization(
        self, organization: Organization, other_organization: Organization, chart: None
    ) -> None:
        with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
            create_cost_center(
                organization=organization, code="HALL", name_ar="صالة", name_en="Hall"
            )
        twin = create_cost_center(
            organization=other_organization, code="HALL", name_ar="صالة", name_en="Hall"
        )
        assert twin.code == "HALL"

    def test_a_cost_center_belongs_to_the_organization_not_a_branch(
        self, organization: Organization, chart: None
    ) -> None:
        """
        ADR-015: Kitchen and Delivery exist at every branch, so branch
        ownership would force a duplicate per branch and make cross-branch
        analysis impossible.
        """
        center = CostCenter.objects.get(organization=organization, code="KITCHEN")
        assert center.organization_id == organization.pk
        assert not hasattr(center, "branch")


class TestArchivingNotDeleting:
    def test_an_account_with_postings_cannot_be_deleted(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        from decimal import Decimal

        from apps.accounting.services import post_entry
        from apps.accounting.tests.conftest import POSTING_DATE
        from apps.accounting.validators import PostingLine

        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("10")),
                PostingLine(account=sales, branch=branch, credit=Decimal("10"), cost_center=hall),
            ],
            idempotency_key="protect-account",
        )
        with pytest.raises(ProtectedError):
            cash.delete()

    def test_a_parent_cannot_be_deleted_while_it_has_children(
        self, organization: Organization, chart: None
    ) -> None:
        parent = Account.objects.get(organization=organization, code="1-01-01")
        with pytest.raises(ProtectedError):
            parent.delete()

    def test_archiving_keeps_the_row(self, organization: Organization, chart: None) -> None:
        account = Account.objects.get(organization=organization, code="6-01-01-001")
        archive_account(account=account, reason="restructured")
        account.refresh_from_db()
        assert account.is_active is False
        assert Account.objects.filter(pk=account.pk).exists()


class TestExternalMapping:
    def test_a_statutory_code_can_be_attached(
        self, organization: Organization, chart: None
    ) -> None:
        account = create_account(
            organization=organization,
            code="1-01-01-002",
            name_ar="صندوق فرعي",
            name_en="Petty Cash",
            external_accounting_system="IQ_UNIFIED",
            external_account_code="1101",
        )
        assert account.external_accounting_system == "IQ_UNIFIED"
        assert account.external_account_code == "1101"

    def test_the_mapping_must_be_complete_or_absent(
        self, organization: Organization, chart: None
    ) -> None:
        """A system with no code, or a code with no system, means nothing."""
        with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
            create_account(
                organization=organization,
                code="1-01-01-003",
                name_ar="ناقص",
                name_en="Incomplete",
                external_accounting_system="IQ_UNIFIED",
            )

    def test_the_mapping_does_not_affect_posting(
        self, organization: Organization, chart: None
    ) -> None:
        account = Account.objects.get(organization=organization, code="1-01-01-001")
        assert account.external_accounting_system == ""
        assert account.is_postable is True
