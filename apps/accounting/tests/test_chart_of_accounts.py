"""Chart of accounts and cost centres (ADR-014, ADR-015)."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from apps.accounting.management.commands.seed_chart_of_accounts import (
    CASH_ROUNDING_ACCOUNT_CODE,
    CHART,
)
from apps.accounting.models import Account, AccountClass, CostCenter, ManualPostingPolicy
from apps.accounting.services import (
    archive_account,
    create_account,
    create_cost_center,
    reactivate_account,
    update_account_metadata,
)
from apps.organizations.models import Branch, Organization

pytestmark = pytest.mark.django_db


class TestSeed:
    def test_the_chart_is_seeded(self, organization: Organization, chart: None) -> None:
        """
        Every declared account, and exactly those.

        Asserted against `len(CHART)` rather than a literal. The literal had
        drifted: Task 4.0 added seventeen sales accounts without touching this
        number, so the test was asserting 77 against a chart of 94 and failing
        before Phase 5 touched anything. A hand-maintained count is a second
        source of truth for a fact the list already states, and this is what
        happens to one.
        """
        assert Account.objects.filter(organization=organization).count() == len(CHART)

    def test_the_seed_is_idempotent(self, organization: Organization, chart: None) -> None:
        call_command("seed_chart_of_accounts", organization="KM", verbosity=0)
        assert Account.objects.filter(organization=organization).count() == len(CHART)

    def test_the_chart_declares_each_code_once(self) -> None:
        """
        A duplicated code would be created once and silently skipped the second
        time, so the seed would still look idempotent while the second entry's
        Arabic name never reached the database.
        """
        codes = [code for code, *_ in CHART]
        assert len(codes) == len(set(codes))

    def test_a_second_run_changes_nothing(self, organization: Organization, chart: None) -> None:
        """
        Idempotent means *unchanged*, not merely "creates no duplicates".

        The flags reconciliation pass exists so a chart seeded before Phase 5
        gets `is_system` and the control-account policies. That pass has to
        converge: a second run must find every row already correct and write
        none of them, or every deployment's `updated_at` column moves on every
        deploy and the history table grows a row per account per run.
        """
        before = {
            account.code: (account.is_system, account.manual_posting_policy, account.updated_at)
            for account in Account.objects.filter(organization=organization)
        }

        call_command("seed_chart_of_accounts", organization="KM", verbosity=0)

        after = {
            account.code: (account.is_system, account.manual_posting_policy, account.updated_at)
            for account in Account.objects.filter(organization=organization)
        }
        assert after == before

    def test_the_phase_five_accounts_are_seeded(
        self, organization: Organization, chart: None
    ) -> None:
        """
        The four accounts the new roles need, each in the class its economics
        put it in rather than the one that was convenient.
        """
        expected = {
            # A cost incurred that no invoice has stated: a liability, and
            # deliberately not the supplier payable.
            "2-02-01-001": AccountClass.LIABILITY,
            # Paid before the period it covers: an asset, not a January cost.
            "1-04-02-001": AccountClass.ASSET,
            "3-03-01-001": AccountClass.EQUITY,
            "3-04-01-001": AccountClass.EQUITY,
        }
        for code, account_class in expected.items():
            account = Account.objects.get(organization=organization, code=code)
            assert account.account_class == account_class, code
            assert account.is_postable, code
            # None of the four is revenue, COGS or an operating expense, so
            # none of them requires a cost centre — and the seed did not decide
            # that, the account class did.
            assert account.requires_cost_center is False, code

    def test_every_seeded_account_is_a_system_account(
        self, organization: Organization, chart: None
    ) -> None:
        """
        Reference data a user may not repurpose. Renaming `2-01-01-001` does
        not move the postings already sitting in it — it relabels them, which
        is the one correction nobody can spot in a report.
        """
        assert not Account.objects.filter(organization=organization, is_system=False).exists()

    def test_the_subledger_control_accounts_are_restricted(
        self, organization: Organization, chart: None
    ) -> None:
        """
        ADR-029 §2. A manual credit to supplier payable balances and posts, so
        the ledger never objects; what it breaks is the equality the supplier
        workspace exists to prove, and the workspace then reports a difference
        with no document behind it.
        """
        restricted = set(
            Account.objects.filter(
                organization=organization, manual_posting_policy=ManualPostingPolicy.RESTRICTED
            ).values_list("code", flat=True)
        )
        assert restricted == {
            "2-01-01-001",  # supplier payable
            "2-01-02-001",  # goods received not invoiced
            "1-03-01-001",  # inventory control
            "1-02-01-001",  # delivery application receivables, every leaf
            "1-02-01-002",
            "1-02-01-003",
            "1-02-01-009",
            "3-03-01-001",  # retained earnings
            "3-04-01-001",  # current year earnings
        }

    def test_no_rollup_carries_a_posting_policy(
        self, organization: Organization, chart: None
    ) -> None:
        """A policy on an account nothing can post to is a claim about nothing."""
        for account in Account.objects.filter(organization=organization, is_postable=False):
            assert account.manual_posting_policy == ManualPostingPolicy.ALLOWED, account.code

    def test_the_purchase_variance_account_is_a_clearing_account(
        self, organization: Organization, chart: None
    ) -> None:
        """
        Task 2.0 §15 proposed a cost-of-sales code and it is superseded
        (ADR-022, amended at Task 2.12).

        Class 5 would have set `requires_cost_center`, and a supplier invoice
        has no cost centre to give — the document belongs to a branch, not a
        department. ADR-022 independently rejects booking a purchasing outcome
        as cost of sales. So the difference is parked in a clearing account
        until a later period-end process splits it between stock still on hand
        and what has been consumed.
        """
        account = Account.objects.get(organization=organization, code="8-01-03-001")
        assert account.account_class == AccountClass.CLEARING
        assert account.is_postable
        assert account.requires_cost_center is False

    def test_the_supplier_return_accounts_split_the_two_facts(
        self, organization: Organization, chart: None
    ) -> None:
        """
        Task 2.13 seeds two, and keeping them apart is the point.

        The clearing account holds the book value of goods that have left while
        the supplier has not yet said what they are worth — a claim in flight,
        so class 8. The variance account holds the difference once the credit
        note settles it, and that is a gain or a loss somebody should be able
        to see, so class 7 beside the other bidirectional difference accounts.

        Neither is the purchase *price* variance. That figure is
        invoice-versus-receipt on goods coming in; these are about goods going
        out, and merging them would hide a supplier's pricing behaviour inside
        the arithmetic of unwinding an average.
        """
        clearing = Account.objects.get(organization=organization, code="8-01-04-001")
        assert clearing.account_class == AccountClass.CLEARING
        assert clearing.is_postable
        assert clearing.requires_cost_center is False

        variance = Account.objects.get(organization=organization, code="7-09-04-001")
        assert variance.account_class == AccountClass.OTHER
        assert variance.is_postable
        assert variance.requires_cost_center is False

        assert clearing.pk != variance.pk
        price = Account.objects.get(organization=organization, code="8-01-03-001")
        assert {clearing.pk, variance.pk}.isdisjoint({price.pk})

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
            create_account(organization=organization, code="1-1-1-1", name="س")

    def test_an_unknown_class_is_refused(self, organization: Organization) -> None:
        with pytest.raises(ValidationError) as exc:
            create_account(organization=organization, code="0", name="س")
        assert exc.value.code in {"unknown_account_class", "invalid"}

    def test_a_missing_parent_is_refused(self, organization: Organization, chart: None) -> None:
        with pytest.raises(ValidationError) as exc:
            create_account(organization=organization, code="1-99-99-001", name="س")
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
            create_account(organization=organization, code="1-01-01-001", name="مكرر")

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
            create_account(organization=organization, code="6-01-02-001", name="جديد")

    def test_cost_center_codes_are_unique_per_organization(
        self, organization: Organization, other_organization: Organization, chart: None
    ) -> None:
        with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
            create_cost_center(organization=organization, code="HALL", name="صالة")
        twin = create_cost_center(organization=other_organization, code="HALL", name="صالة")
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
            name="صندوق فرعي",
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
                name="ناقص",
                external_accounting_system="IQ_UNIFIED",
            )

    def test_the_mapping_does_not_affect_posting(
        self, organization: Organization, chart: None
    ) -> None:
        account = Account.objects.get(organization=organization, code="1-01-01-001")
        assert account.external_accounting_system == ""
        assert account.is_postable is True


class TestManualPostingPolicy:
    """ADR-029 §2. Whether a hand-written journal line may name this account."""

    def test_a_new_account_is_allowed_by_default(
        self, organization: Organization, chart: None
    ) -> None:
        """The value every account meant before the column existed."""
        account = create_account(organization=organization, code="6-01-02-002", name="كهرباء")
        assert account.manual_posting_policy == ManualPostingPolicy.ALLOWED
        assert account.is_system is False

    def test_a_policy_on_a_rollup_is_refused(self, organization: Organization, chart: None) -> None:
        """
        A group account never receives a line, so a policy on it is a claim
        about nothing — and worse than silence: a reader who sees FORBIDDEN on
        `2-01` concludes the payable branch is protected when every leaf under
        it is still ALLOWED.
        """
        with pytest.raises(ValidationError) as exc:
            create_account(
                organization=organization,
                code="6-04",
                name="مصروفات أخرى",
                manual_posting_policy=ManualPostingPolicy.FORBIDDEN,
            )
        assert exc.value.code == "policy_on_rollup"

    def test_the_database_refuses_a_policy_on_a_rollup_too(
        self, organization: Organization, chart: None
    ) -> None:
        """The service says it readably; the constraint makes it true."""
        group = Account.objects.get(organization=organization, code="2-01-01")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounting_account SET manual_posting_policy = 'FORBIDDEN' "
                    "WHERE id = %s",
                    [group.pk],
                )

    def test_an_unknown_policy_is_refused(self, organization: Organization, chart: None) -> None:
        with pytest.raises(ValidationError) as exc:
            create_account(
                organization=organization,
                code="6-01-02-003",
                name="ماء",
                manual_posting_policy="MAYBE",
            )
        assert exc.value.code == "unknown_manual_posting_policy"


class TestArchiveDate:
    def test_archiving_stamps_the_date(self, organization: Organization, chart: None) -> None:
        account = Account.objects.get(organization=organization, code="6-01-01-001")
        archive_account(account=account, reason="restructured")
        account.refresh_from_db()
        assert account.is_active is False
        assert account.archived_at is not None

    def test_reactivating_clears_it(self, organization: Organization, chart: None) -> None:
        account = Account.objects.get(organization=organization, code="6-01-01-001")
        archive_account(account=account, reason="restructured")
        reactivate_account(account=account, reason="needed again")
        account.refresh_from_db()
        assert account.is_active is True
        assert account.archived_at is None

    def test_reactivating_under_an_archived_parent_is_refused(
        self, organization: Organization, chart: None
    ) -> None:
        """
        An active leaf under a dead group is detached from the branch that
        gives its code meaning, and any report grouping by parent drops it.
        """
        leaf = Account.objects.get(organization=organization, code="6-01-01-001")
        parent = Account.objects.get(organization=organization, code="6-01-01")
        archive_account(account=leaf, reason="restructured")
        archive_account(account=parent, reason="restructured")

        with pytest.raises(ValidationError) as exc:
            reactivate_account(account=leaf, reason="needed again")
        assert exc.value.code == "parent_archived"

    def test_the_database_refuses_an_archived_account_with_no_date(
        self, organization: Organization, chart: None
    ) -> None:
        """If and only if. An archive with no date loses when it happened."""
        account = Account.objects.get(organization=organization, code="6-01-01-001")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounting_account SET is_active = FALSE WHERE id = %s",
                    [account.pk],
                )

    def test_the_database_refuses_an_active_account_with_a_date(
        self, organization: Organization, chart: None
    ) -> None:
        """
        The other direction, and the dangerous one: an active row carrying an
        archive date says it is archived while the flag every query filters on
        says it is not, and the flag is the one that wins silently.
        """
        account = Account.objects.get(organization=organization, code="6-01-01-001")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounting_account SET archived_at = NOW() WHERE id = %s",
                    [account.pk],
                )


class TestUpdateAccountMetadata:
    """The four fields an account with journal history may safely change."""

    def test_the_names_and_the_policy_change(self, organization: Organization, chart: None) -> None:
        """A user-created account: not seeded, so nothing here is protected."""
        account = create_account(
            organization=organization,
            code="6-01-02-002",
            name="إيجار المستودع",
        )
        updated = update_account_metadata(
            account=account,
            name="إيجار المخزن",
            requires_cost_center=True,
            manual_posting_policy=ManualPostingPolicy.FORBIDDEN,
            reason="clearer name",
        )
        assert updated.name == "إيجار المخزن"
        assert updated.name == "Store rent"
        assert updated.manual_posting_policy == ManualPostingPolicy.FORBIDDEN

    def test_a_financial_meaning_change_is_refused_on_an_account_with_history(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        A posted line names the account, not the code, so renumbering does not
        move the posting — it relabels it, and every report that groups by code
        silently restates. The path for a genuinely wrong code is a new account
        and an archive, which keeps the old code reserved.
        """
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
            idempotency_key="metadata-history",
        )

        cash.code = "1-01-01-009"
        with pytest.raises(ValidationError) as exc:
            update_account_metadata(
                account=cash,
                name=cash.name,
                requires_cost_center=cash.requires_cost_center,
                manual_posting_policy=cash.manual_posting_policy,
            )
        assert exc.value.code == "account_financial_meaning_immutable"

        cash.refresh_from_db()
        assert cash.code == "1-01-01-001"

    def test_turning_a_rollup_postable_is_refused(
        self, organization: Organization, chart: None
    ) -> None:
        group = Account.objects.get(organization=organization, code="1-01-01")
        group.is_postable = True
        with pytest.raises(ValidationError) as exc:
            update_account_metadata(
                account=group,
                name=group.name,
                requires_cost_center=False,
                manual_posting_policy=ManualPostingPolicy.ALLOWED,
            )
        assert exc.value.code == "account_financial_meaning_immutable"

    def test_a_system_account_policy_is_protected(
        self, organization: Organization, chart: None
    ) -> None:
        """
        Opening `2-01-01-001` to hand-written entries is exactly what seeding
        it RESTRICTED was for, so it takes more than the chart screen's own
        permission — the command layer gates `allow_system` separately.
        """
        payable = Account.objects.get(organization=organization, code="2-01-01-001")
        with pytest.raises(ValidationError) as exc:
            update_account_metadata(
                account=payable,
                name=payable.name,
                requires_cost_center=False,
                manual_posting_policy=ManualPostingPolicy.ALLOWED,
            )
        assert exc.value.code == "system_account_policy_protected"

        payable.refresh_from_db()
        assert payable.manual_posting_policy == ManualPostingPolicy.RESTRICTED

    def test_a_system_account_may_be_renamed_without_the_flag(
        self, organization: Organization, chart: None
    ) -> None:
        """
        Only the *policy* is protected. A better Arabic name restates nothing
        that was posted, and refusing it would push somebody into the shell.
        """
        payable = Account.objects.get(organization=organization, code="2-01-01-001")
        updated = update_account_metadata(
            account=payable,
            name="ذمم الموردين والمقاولين",
            requires_cost_center=False,
            manual_posting_policy=ManualPostingPolicy.RESTRICTED,
        )
        assert updated.name == "ذمم الموردين والمقاولين"
        assert updated.manual_posting_policy == ManualPostingPolicy.RESTRICTED

    def test_the_flag_opens_the_system_account(
        self, organization: Organization, chart: None
    ) -> None:
        payable = Account.objects.get(organization=organization, code="2-01-01-001")
        updated = update_account_metadata(
            account=payable,
            name=payable.name,
            requires_cost_center=False,
            manual_posting_policy=ManualPostingPolicy.FORBIDDEN,
            allow_system=True,
            reason="subledger is now the only path",
        )
        assert updated.manual_posting_policy == ManualPostingPolicy.FORBIDDEN

    def test_a_cost_center_requirement_on_a_rollup_is_refused(
        self, organization: Organization, chart: None
    ) -> None:
        group = Account.objects.get(organization=organization, code="6-01-01")
        with pytest.raises(ValidationError) as exc:
            update_account_metadata(
                account=group,
                name=group.name,
                requires_cost_center=True,
                manual_posting_policy=ManualPostingPolicy.ALLOWED,
            )
        assert exc.value.code == "cost_center_on_rollup"
