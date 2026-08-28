"""
The financial-statement mapping, and the ledger-first read that keeps it honest
(ADR-031).

The decision under test is §2 of that ADR, and it is the one the natural
implementation gets backwards. A statement built by iterating mappings produces
a balanced, beautiful, **wrong** report when an account is unmapped: the
balance is simply not there, nothing on the page indicates its absence, and the
arithmetic still ties because every line that is present is internally
consistent. So the account set is resolved from posted lines and the mapping
table is used only to subtract what is already classified.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.accounting.models import (
    Account,
    AccountReportMapping,
    CostCenter,
    PresentationSection,
    StatementGroup,
)
from apps.accounting.selectors import (
    account_balance,
    account_balances,
    chart_tree,
    report_mapping_for,
)
from apps.accounting.services import (
    archive_account,
    clear_report_mapping,
    post_entry,
    set_report_mapping,
    unmapped_accounts,
)
from apps.accounting.tests.conftest import POSTING_DATE
from apps.accounting.validators import PostingLine
from apps.organizations.authorization import OutOfScope, PermissionMissing
from apps.organizations.models import Branch, Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


def _post_ten(
    organization: Organization,
    cash: Account,
    sales: Account,
    branch: Branch,
    hall: CostCenter,
    *,
    key: str,
    amount: str = "10",
    on: datetime.date = POSTING_DATE,
) -> None:
    post_entry(
        organization=organization,
        accounting_date=on,
        lines=[
            PostingLine(account=cash, branch=branch, debit=Decimal(amount)),
            PostingLine(account=sales, branch=branch, credit=Decimal(amount), cost_center=hall),
        ],
        idempotency_key=key,
    )


class TestSettingAMapping:
    def test_a_postable_account_is_classified(
        self, organization: Organization, cash: Account
    ) -> None:
        mapping = set_report_mapping(
            organization=organization,
            account=cash,
            statement_group=StatementGroup.ASSET,
            presentation_section=PresentationSection.CURRENT,
            display_order=10,
        )
        assert mapping.statement_group == StatementGroup.ASSET
        assert mapping.presentation_section == PresentationSection.CURRENT
        assert mapping.display_order == 10
        assert mapping.is_active is True

    def test_reclassifying_updates_the_same_row(
        self, organization: Organization, cash: Account
    ) -> None:
        """
        Two active classifications would put one balance in two sections of the
        same statement, and both sections would still add up internally.
        """
        first = set_report_mapping(
            organization=organization,
            account=cash,
            statement_group=StatementGroup.ASSET,
            presentation_section=PresentationSection.CURRENT,
        )
        second = set_report_mapping(
            organization=organization,
            account=cash,
            statement_group=StatementGroup.ASSET,
            presentation_section=PresentationSection.NON_CURRENT,
        )
        assert first.pk == second.pk
        assert AccountReportMapping.objects.filter(account=cash).count() == 1

    def test_a_cleared_mapping_is_revived_rather_than_duplicated(
        self, organization: Organization, cash: Account
    ) -> None:
        mapping = set_report_mapping(
            organization=organization, account=cash, statement_group=StatementGroup.ASSET
        )
        clear_report_mapping(mapping=mapping, reason="restructuring")

        revived = set_report_mapping(
            organization=organization, account=cash, statement_group=StatementGroup.ASSET
        )
        assert revived.pk == mapping.pk
        assert revived.is_active is True
        assert AccountReportMapping.objects.filter(account=cash).count() == 1

    def test_a_group_account_cannot_be_classified(
        self, organization: Organization, group_account: Account
    ) -> None:
        """
        A rollup's figure is the sum of its children, so classifying it as well
        as the leaves under it would put the same money on the balance sheet
        twice — and the statement would still add up.

        Enforced in the service rather than by a check constraint because it is
        a fact about other rows.
        """
        with pytest.raises(ValidationError) as exc:
            set_report_mapping(
                organization=organization,
                account=group_account,
                statement_group=StatementGroup.ASSET,
            )
        assert exc.value.code == "account_not_postable"

    def test_another_organizations_account_is_refused(
        self, organization: Organization, other_cash: Account
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            set_report_mapping(
                organization=organization,
                account=other_cash,
                statement_group=StatementGroup.ASSET,
            )
        assert exc.value.code == "account_organization_mismatch"

    def test_an_archived_account_may_still_be_classified(
        self, organization: Organization, cash: Account
    ) -> None:
        """
        Archiving stops new postings; it does not remove the balance already
        sitting there. An archived account with a non-zero balance is exactly
        the row ADR-031 §2 refuses to let a statement omit, so it has to be
        classifiable.
        """
        archive_account(account=cash, reason="replaced")
        cash.refresh_from_db()
        mapping = set_report_mapping(
            organization=organization,
            account=cash,
            statement_group=StatementGroup.ASSET,
            presentation_section=PresentationSection.CURRENT,
        )
        assert mapping.account_id == cash.pk

    def test_an_unknown_group_is_refused(self, organization: Organization, cash: Account) -> None:
        with pytest.raises(ValidationError) as exc:
            set_report_mapping(organization=organization, account=cash, statement_group="PROFIT")
        assert exc.value.code == "unknown_statement_group"


class TestPresentationSection:
    def test_a_current_split_on_revenue_is_refused(
        self, organization: Organization, sales: Account
    ) -> None:
        """
        A current / non-current split is a balance-sheet question. On revenue
        it is not merely unused, it is false — revenue has no maturity, and a
        reader would conclude somebody had decided something they had not.
        """
        with pytest.raises(ValidationError) as exc:
            set_report_mapping(
                organization=organization,
                account=sales,
                statement_group=StatementGroup.REVENUE,
                presentation_section=PresentationSection.CURRENT,
            )
        assert exc.value.code == "section_not_on_balance_sheet"

    def test_the_database_refuses_it_too(self, organization: Organization, sales: Account) -> None:
        mapping = set_report_mapping(
            organization=organization, account=sales, statement_group=StatementGroup.REVENUE
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounting_accountreportmapping "
                    "SET presentation_section = 'CURRENT' WHERE id = %s",
                    [mapping.pk],
                )

    def test_not_applicable_is_the_default(
        self, organization: Organization, sales: Account
    ) -> None:
        """A real answer, not a missing one: revenue has no maturity."""
        mapping = set_report_mapping(
            organization=organization, account=sales, statement_group=StatementGroup.REVENUE
        )
        assert mapping.presentation_section == PresentationSection.NOT_APPLICABLE


class TestOneClassificationPerAccount:
    def test_the_database_refuses_a_second_row(
        self, organization: Organization, cash: Account
    ) -> None:
        set_report_mapping(
            organization=organization, account=cash, statement_group=StatementGroup.ASSET
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            AccountReportMapping.objects.create(
                organization=organization,
                account=cash,
                statement_group=StatementGroup.LIABILITY,
            )

    def test_two_organizations_may_classify_their_own_accounts(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        other_cash: Account,
    ) -> None:
        mine = set_report_mapping(
            organization=organization, account=cash, statement_group=StatementGroup.ASSET
        )
        theirs = set_report_mapping(
            organization=other_organization,
            account=other_cash,
            statement_group=StatementGroup.ASSET,
        )
        assert mine.pk != theirs.pk


class TestClearing:
    def test_clearing_keeps_the_row(self, organization: Organization, cash: Account) -> None:
        """
        A statement produced under the old classification has to stay
        explicable. A deleted row leaves "why was this in operating expenses
        last year" with no answer at all.
        """
        mapping = set_report_mapping(
            organization=organization, account=cash, statement_group=StatementGroup.ASSET
        )
        clear_report_mapping(mapping=mapping, reason="reclassified")
        mapping.refresh_from_db()
        assert mapping.is_active is False
        assert AccountReportMapping.objects.filter(pk=mapping.pk).exists()

    def test_a_cleared_mapping_classifies_nothing(
        self, organization: Organization, cash: Account
    ) -> None:
        mapping = set_report_mapping(
            organization=organization, account=cash, statement_group=StatementGroup.ASSET
        )
        clear_report_mapping(mapping=mapping, reason="reclassified")
        assert report_mapping_for(organization=organization) == {}


class TestUnmappedAccounts:
    """
    ADR-031 §2 — the account set comes from the ledger, never from the mapping
    table.
    """

    def test_an_unmapped_non_zero_account_is_returned(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        _post_ten(organization, cash, sales, branch, hall, key="unmapped-1")

        found = unmapped_accounts(organization=organization)

        assert {account.code for account in found} == {cash.code, sales.code}

    def test_a_mapped_account_is_not_returned(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        _post_ten(organization, cash, sales, branch, hall, key="unmapped-2")
        set_report_mapping(
            organization=organization, account=cash, statement_group=StatementGroup.ASSET
        )

        found = unmapped_accounts(organization=organization)

        assert {account.code for account in found} == {sales.code}

    def test_clearing_a_mapping_makes_the_account_unmapped_again(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        _post_ten(organization, cash, sales, branch, hall, key="unmapped-3")
        mapping = set_report_mapping(
            organization=organization, account=cash, statement_group=StatementGroup.ASSET
        )
        clear_report_mapping(mapping=mapping, reason="reclassified")

        found = unmapped_accounts(organization=organization)

        assert cash.code in {account.code for account in found}

    def test_an_account_with_no_movement_is_not_reported(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        rent: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Classifying a zero-balance account is tidiness. Reporting it as a
        finding would bury the ones that actually change a figure.
        """
        _post_ten(organization, cash, sales, branch, hall, key="unmapped-4")

        found = unmapped_accounts(organization=organization)

        assert rent.code not in {account.code for account in found}

    def test_an_archived_account_with_a_balance_is_still_reported(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The failure mode this whole function exists for. Archiving hides the
        account from the chart screen; it does not move the money, and a
        statement that dropped it would balance and be wrong.
        """
        _post_ten(organization, cash, sales, branch, hall, key="unmapped-5")
        archive_account(account=cash, reason="replaced")

        found = unmapped_accounts(organization=organization)

        assert cash.code in {account.code for account in found}

    def test_an_as_of_date_before_the_posting_reports_nothing(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        _post_ten(organization, cash, sales, branch, hall, key="unmapped-6")

        found = unmapped_accounts(
            organization=organization, up_to=POSTING_DATE - datetime.timedelta(days=1)
        )

        assert found == []

    def test_another_organizations_movement_is_not_reported(
        self,
        organization: Organization,
        other_organization: Organization,
        other_cash: Account,
        branch: Branch,
        cash: Account,
        sales: Account,
        hall: CostCenter,
    ) -> None:
        _post_ten(organization, cash, sales, branch, hall, key="unmapped-7")

        found = unmapped_accounts(organization=other_organization)

        assert found == []


class TestAccountBalances:
    def test_it_agrees_with_the_single_account_selector(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Two selectors answering the same question have to agree, or a report
        and a detail page will disagree about the same account.
        """
        _post_ten(organization, cash, sales, branch, hall, key="balances-1", amount="1250.001")

        balances = account_balances(organization=organization)

        assert balances[cash.pk] == account_balance(account=cash)
        assert balances[sales.pk] == account_balance(account=sales)
        assert balances[cash.pk] == Decimal("1250.001")
        assert balances[sales.pk] == Decimal("-1250.001")

    def test_it_is_one_query(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
        django_assert_num_queries: object,
    ) -> None:
        """
        The whole reason it exists. A per-account loop is one round trip per
        account, so the cost of the balance sheet grows with the chart.
        """
        _post_ten(organization, cash, sales, branch, hall, key="balances-2")

        with django_assert_num_queries(1):  # type: ignore[operator]
            account_balances(organization=organization)

    def test_an_account_with_no_movement_is_absent(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        rent: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        _post_ten(organization, cash, sales, branch, hall, key="balances-3")

        balances = account_balances(organization=organization)

        assert rent.pk not in balances

    def test_a_branch_filter_narrows_it(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        second_branch: Branch,
        hall: CostCenter,
    ) -> None:
        _post_ten(organization, cash, sales, branch, hall, key="balances-4")

        assert account_balances(organization=organization, branch=second_branch) == {}
        assert account_balances(organization=organization, branch=branch)[cash.pk] == Decimal("10")


class TestChartTree:
    def test_the_roots_are_the_classes(self, organization: Organization, chart: None) -> None:
        roots = chart_tree(organization=organization)
        assert {node.account.code for node in roots} == {"1", "2", "3", "4", "5", "6", "7", "8"}

    def test_a_leaf_sits_under_its_code_prefix(
        self, organization: Organization, chart: None
    ) -> None:
        roots = {node.account.code: node for node in chart_tree(organization=organization)}
        assets = roots["1"]
        cash_group = next(node for node in assets.children if node.account.code == "1-01")
        boxes = next(node for node in cash_group.children if node.account.code == "1-01-01")
        assert "1-01-01-001" in {node.account.code for node in boxes.children}

    def test_it_is_one_query(
        self,
        organization: Organization,
        chart: None,
        django_assert_num_queries: object,
    ) -> None:
        with django_assert_num_queries(1):  # type: ignore[operator]
            chart_tree(organization=organization)

    def test_archived_accounts_are_excluded_unless_asked_for(
        self, organization: Organization, chart: None
    ) -> None:
        account = Account.objects.get(organization=organization, code="6-01-01-001")
        archive_account(account=account, reason="restructured")

        def codes(include_archived: bool) -> set[str]:
            found: set[str] = set()
            stack = list(chart_tree(organization=organization, include_archived=include_archived))
            while stack:
                node = stack.pop()
                found.add(node.account.code)
                stack.extend(node.children)
            return found

        assert "6-01-01-001" not in codes(False)
        assert "6-01-01-001" in codes(True)

    def test_an_orphaned_leaf_is_returned_as_a_root(
        self, organization: Organization, chart: None
    ) -> None:
        """
        An active leaf under an archived group. No constraint forbids it, and
        dropping it because of a filter applied to a *different* row is exactly
        the silent omission this module exists to prevent.
        """
        parent = Account.objects.get(organization=organization, code="6-01-01")
        archive_account(account=parent, reason="restructured")

        roots = {node.account.code for node in chart_tree(organization=organization)}

        assert "6-01-01-001" in roots


class TestReportMappingCommands:
    """Permission plus scope, at the command layer (ADR-016, ADR-029 §7)."""

    def test_the_accounting_manager_may_classify(
        self, organization: Organization, cash: Account, accounting_manager: User
    ) -> None:
        from apps.accounting.commands import set_account_report_mapping

        mapping = set_account_report_mapping(
            actor=accounting_manager,
            organization_id=organization.pk,
            account_id=cash.pk,
            statement_group=StatementGroup.ASSET,
            presentation_section=PresentationSection.CURRENT,
        )
        assert mapping.account_id == cash.pk

    def test_a_branch_accountant_may_not(
        self, organization: Organization, cash: Account, accountant: User
    ) -> None:
        """In scope, without the authority: 403, and the record is not touched."""
        from apps.accounting.commands import set_account_report_mapping

        with pytest.raises(PermissionMissing):
            set_account_report_mapping(
                actor=accountant,
                organization_id=organization.pk,
                account_id=cash.pk,
                statement_group=StatementGroup.ASSET,
            )
        assert not AccountReportMapping.objects.filter(account=cash).exists()

    def test_another_organizations_caller_gets_a_404(
        self, organization: Organization, cash: Account, rival_accountant: User
    ) -> None:
        """
        Out of scope is 404, never 403: a 403 about another organization's
        record confirms it exists, and ids are sequential.
        """
        from apps.accounting.commands import set_account_report_mapping

        with pytest.raises(OutOfScope):
            set_account_report_mapping(
                actor=rival_accountant,
                organization_id=organization.pk,
                account_id=cash.pk,
                statement_group=StatementGroup.ASSET,
            )

    def test_an_account_from_another_organization_is_not_found(
        self, organization: Organization, other_cash: Account, accounting_manager: User
    ) -> None:
        """
        The id is resolved *inside* the named organization, so a foreign
        account selects nothing rather than being fetched and then checked.
        """
        from apps.accounting.commands import set_account_report_mapping

        with pytest.raises(OutOfScope):
            set_account_report_mapping(
                actor=accounting_manager,
                organization_id=organization.pk,
                account_id=other_cash.pk,
                statement_group=StatementGroup.ASSET,
            )

    def test_clearing_needs_the_same_authority(
        self, organization: Organization, cash: Account, accounting_manager: User, cashier: User
    ) -> None:
        from apps.accounting.commands import (
            clear_account_report_mapping,
            set_account_report_mapping,
        )

        mapping = set_account_report_mapping(
            actor=accounting_manager,
            organization_id=organization.pk,
            account_id=cash.pk,
            statement_group=StatementGroup.ASSET,
        )

        with pytest.raises(PermissionMissing):
            clear_account_report_mapping(actor=cashier, mapping_id=mapping.pk)

        cleared = clear_account_report_mapping(
            actor=accounting_manager, mapping_id=mapping.pk, reason="reclassified"
        )
        assert cleared.is_active is False


class TestChartCommands:
    def test_a_branch_accountant_reads_the_chart(
        self, organization: Organization, chart: None, accountant: User, cash: Account
    ) -> None:
        """
        Coding a journal line means choosing an account. An accountant who
        cannot see the chart cannot do the job their posting permissions exist
        for, so the read is reachable-organization authority rather than
        organization membership (ADR-016).
        """
        from apps.accounting.commands import list_chart_accounts, read_chart_account

        assert read_chart_account(actor=accountant, account_id=cash.pk).pk == cash.pk
        assert list_chart_accounts(actor=accountant, organization_id=organization.pk).exists()

    def test_a_cashier_reads_nothing(
        self, organization: Organization, chart: None, cashier: User, cash: Account
    ) -> None:
        from apps.accounting.commands import list_chart_accounts, read_chart_account

        with pytest.raises(PermissionMissing):
            read_chart_account(actor=cashier, account_id=cash.pk)
        assert not list_chart_accounts(actor=cashier).exists()

    def test_a_rival_reads_nothing_and_is_told_it_does_not_exist(
        self, organization: Organization, chart: None, rival_accountant: User, cash: Account
    ) -> None:
        from apps.accounting.commands import read_chart_account

        with pytest.raises(OutOfScope):
            read_chart_account(actor=rival_accountant, account_id=cash.pk)

    def test_only_organization_authority_creates_an_account(
        self, organization: Organization, chart: None, accountant: User, accounting_manager: User
    ) -> None:
        from apps.accounting.commands import create_chart_account

        with pytest.raises(PermissionMissing):
            create_chart_account(
                actor=accountant,
                organization_id=organization.pk,
                code="6-01-02-002",
                name="إيجار المخزن",
            )

        account = create_chart_account(
            actor=accounting_manager,
            organization_id=organization.pk,
            code="6-01-02-002",
            name="إيجار المخزن",
        )
        assert account.is_system is False

    def test_archiving_and_reactivating_round_trip(
        self, organization: Organization, chart: None, accounting_manager: User
    ) -> None:
        from apps.accounting.commands import archive_chart_account, reactivate_chart_account

        account = Account.objects.get(organization=organization, code="6-01-01-001")

        archived = archive_chart_account(
            actor=accounting_manager, account_id=account.pk, reason="restructured"
        )
        assert archived.is_active is False
        assert archived.archived_at is not None

        restored = reactivate_chart_account(
            actor=accounting_manager, account_id=account.pk, reason="needed again"
        )
        assert restored.is_active is True
        assert restored.archived_at is None

    def test_opening_a_system_account_needs_the_elevated_permission(
        self, organization: Organization, chart: None, accounting_manager: User, accountant: User
    ) -> None:
        """
        `allow_system` is not authorization and grants nothing on its own. The
        command layer gates it on `accounting.manage_accounts` — the structural
        authority from Task 0.7 — so the ordinary chart screen cannot open the
        seeded control accounts.
        """
        from apps.accounting.commands import update_chart_account
        from apps.accounting.models import ManualPostingPolicy

        payable = Account.objects.get(organization=organization, code="2-01-01-001")

        with pytest.raises(PermissionMissing):
            update_chart_account(
                actor=accountant,
                account_id=payable.pk,
                name=payable.name,
                requires_cost_center=False,
                manual_posting_policy=ManualPostingPolicy.ALLOWED,
                allow_system=True,
            )

        updated = update_chart_account(
            actor=accounting_manager,
            account_id=payable.pk,
            name=payable.name,
            requires_cost_center=False,
            manual_posting_policy=ManualPostingPolicy.FORBIDDEN,
            allow_system=True,
            reason="subledger is now the only path",
        )
        assert updated.manual_posting_policy == ManualPostingPolicy.FORBIDDEN
