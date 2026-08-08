"""
Scope and authority, tested against objects rather than permission names.

The failure these guard against is not "the permission check was missing". It
is subtler and far more common: the permission check was present, passed, and
was asked about the wrong place. So every test here names a concrete
organization, branch, account, or entry, and asserts what happens when a real
user submits an identifier belonging to someone else.

Numbered to match the approved security-test list.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounting.commands import (
    LineInput,
    close_accounting_period,
    create_draft_entry,
    post_journal_entry,
    read_journal_entry,
    reopen_accounting_period,
    reverse_journal_entry,
    soft_close_accounting_period,
)
from apps.accounting.models import (
    Account,
    AccountingPeriod,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    PeriodState,
)
from apps.accounting.services import (
    close_period,
    create_draft,
    post_entry,
    resolve_period,
)
from apps.accounting.validators import PostingLine
from apps.organizations.models import Branch, Organization
from apps.users.models import User

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db


def _lines(cash: Account, sales: Account, branch: Branch, hall: CostCenter) -> list[LineInput]:
    return [
        LineInput(account_id=cash.pk, branch_id=branch.pk, debit=Decimal("100000")),
        LineInput(
            account_id=sales.pk,
            branch_id=branch.pk,
            credit=Decimal("100000"),
            cost_center_id=hall.pk,
        ),
    ]


def _posted_entry(
    organization: Organization,
    cash: Account,
    sales: Account,
    branch: Branch,
    hall: CostCenter,
    key: str = "sec-1",
) -> JournalEntry:
    return post_entry(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
            PostingLine(account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall),
        ],
        idempotency_key=key,
    )


def _draft_entry(
    organization: Organization,
    cash: Account,
    sales: Account,
    branch: Branch,
    hall: CostCenter,
) -> JournalEntry:
    """
    A genuine draft.

    Built through `create_draft` rather than by flipping a posted entry back:
    the immutability trigger refuses that, correctly, so a test that did it
    would be exercising a state the system cannot reach.
    """
    return create_draft(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
            PostingLine(account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall),
        ],
    )


def _close_through(organization: Organization, period: AccountingPeriod) -> None:
    """Close every earlier period so the target may close chronologically."""
    earlier = AccountingPeriod.objects.filter(
        fiscal_year=period.fiscal_year, period_number__lte=period.period_number
    ).order_by("period_number")
    for each in earlier:
        close_period(period=each, reason="year end")
    period.refresh_from_db()


# --- 1. Another organization by changing organization_id -------------------


class TestCrossOrganization:
    def test_1_submitting_a_foreign_organization_id_is_refused(
        self,
        rival_accountant: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The rival's accountant holds every permission a branch accountant
        holds. What they do not hold is Khan Mandi, and no identifier in the
        request body can supply it.
        """
        with pytest.raises(PermissionDenied):
            create_draft_entry(
                actor=rival_accountant,
                organization_id=organization.pk,
                accounting_date=POSTING_DATE,
                lines=_lines(cash, sales, branch, hall),
            )

    def test_3_a_foreign_organizations_entry_is_inaccessible(
        self,
        rival_accountant: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = _posted_entry(organization, cash, sales, branch, hall)

        with pytest.raises(PermissionDenied):
            read_journal_entry(actor=rival_accountant, entry_id=entry.pk)

    def test_a_foreign_organizations_period_is_inaccessible(
        self, rival_accountant: User, organization: Organization
    ) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)

        with pytest.raises(PermissionDenied):
            soft_close_accounting_period(
                actor=rival_accountant, period_id=period.pk, reason="not mine"
            )


# --- 2 & 4. Another branch by changing branch_id ---------------------------


class TestCrossBranch:
    def test_2_submitting_a_foreign_branch_id_is_refused(
        self,
        branch_accountant_elsewhere: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Same organization, same permissions, different branch. The caller can
        reach the organization, so this is the case a naive organization-only
        check would let through.
        """
        with pytest.raises(PermissionDenied):
            create_draft_entry(
                actor=branch_accountant_elsewhere,
                organization_id=organization.pk,
                accounting_date=POSTING_DATE,
                lines=_lines(cash, sales, branch, hall),
            )

    def test_4_an_entry_at_another_branch_is_inaccessible(
        self,
        branch_accountant_elsewhere: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = _posted_entry(organization, cash, sales, branch, hall)

        with pytest.raises(PermissionDenied):
            read_journal_entry(actor=branch_accountant_elsewhere, entry_id=entry.pk)

    def test_an_entry_spanning_two_branches_needs_authority_at_both(
        self,
        accountant: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        second_branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Authority over half an entry authorizes nothing. Our accountant works
        at Bunook only, and this entry moves value at Karrada too.
        """
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales,
                    branch=second_branch,
                    credit=Decimal("100000"),
                    cost_center=hall,
                ),
            ],
            idempotency_key="two-branch",
        )

        with pytest.raises(PermissionDenied):
            read_journal_entry(actor=accountant, entry_id=entry.pk)


# --- 5 & 6. Injecting a foreign account or cost centre ---------------------


class TestForeignObjectInjection:
    def test_5_a_foreign_account_id_cannot_be_injected(
        self,
        accountant: User,
        organization: Organization,
        other_cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        A real account, a real id, belonging to the rival's chart. Refused at
        resolution — it is never even in the queryset the command looks in.
        """
        with pytest.raises(PermissionDenied):
            create_draft_entry(
                actor=accountant,
                organization_id=organization.pk,
                accounting_date=POSTING_DATE,
                lines=[
                    LineInput(
                        account_id=other_cash.pk,
                        branch_id=branch.pk,
                        debit=Decimal("100000"),
                    ),
                    LineInput(
                        account_id=sales.pk,
                        branch_id=branch.pk,
                        credit=Decimal("100000"),
                        cost_center_id=hall.pk,
                    ),
                ],
            )

    def test_6_a_foreign_cost_center_id_cannot_be_injected(
        self,
        accountant: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        other_hall: CostCenter,
    ) -> None:
        with pytest.raises(PermissionDenied):
            create_draft_entry(
                actor=accountant,
                organization_id=organization.pk,
                accounting_date=POSTING_DATE,
                lines=[
                    LineInput(account_id=cash.pk, branch_id=branch.pk, debit=Decimal("100000")),
                    LineInput(
                        account_id=sales.pk,
                        branch_id=branch.pk,
                        credit=Decimal("100000"),
                        cost_center_id=other_hall.pk,
                    ),
                ],
            )

    def test_a_foreign_branch_id_cannot_be_injected_into_a_line(
        self,
        accountant: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        other_branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(PermissionDenied):
            create_draft_entry(
                actor=accountant,
                organization_id=organization.pk,
                accounting_date=POSTING_DATE,
                lines=[
                    LineInput(
                        account_id=cash.pk,
                        branch_id=other_branch.pk,
                        debit=Decimal("100000"),
                    ),
                    LineInput(
                        account_id=sales.pk,
                        branch_id=branch.pk,
                        credit=Decimal("100000"),
                        cost_center_id=hall.pk,
                    ),
                ],
            )


# --- 7 to 12. Authority for each act ---------------------------------------


class TestUnauthorizedActs:
    def test_7_a_cashier_cannot_post(
        self,
        cashier: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = _draft_entry(organization, cash, sales, branch, hall)

        with pytest.raises(PermissionDenied):
            post_journal_entry(actor=cashier, entry_id=entry.pk)

        entry.refresh_from_db()
        assert entry.status == JournalEntryStatus.DRAFT

    def test_7b_a_branch_manager_cannot_post(
        self,
        branch_manager: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """Read authority is not write authority."""
        entry = _draft_entry(organization, cash, sales, branch, hall)
        assert branch_manager.has_perm("accounting.view_journal")

        with pytest.raises(PermissionDenied):
            post_journal_entry(actor=branch_manager, entry_id=entry.pk)

    def test_8_a_cashier_cannot_reverse(
        self,
        cashier: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = _posted_entry(organization, cash, sales, branch, hall)

        with pytest.raises(PermissionDenied):
            reverse_journal_entry(actor=cashier, entry_id=entry.pk, reason="not authorized")

    def test_9_a_branch_accountant_cannot_close_a_period(
        self, accountant: User, organization: Organization
    ) -> None:
        """
        They hold `accounting.close_period` — closing is accountant work — but
        they hold it nowhere, because a period is organization state.
        """
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        assert accountant.has_perm("accounting.close_period")

        with pytest.raises(PermissionDenied):
            close_accounting_period(actor=accountant, period_id=period.pk, reason="month end")

    def test_10_a_cashier_cannot_reopen_a_period(
        self, cashier: User, organization: Organization
    ) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        _close_through(organization, period)

        with pytest.raises(PermissionDenied):
            reopen_accounting_period(actor=cashier, period_id=period.pk, reason="let me in")

    def test_11_a_branch_role_cannot_reopen_an_organization_period(
        self, accountant: User, organization: Organization
    ) -> None:
        """
        The requirement stated in its own terms: branch-scoped authority is
        insufficient to reopen. This accountant does not even hold the
        permission, and would still be refused on scope if they did.
        """
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        _close_through(organization, period)

        assert not accountant.has_perm("accounting.reopen_period")
        with pytest.raises(PermissionDenied):
            reopen_accounting_period(
                actor=accountant, period_id=period.pk, reason="reopening March"
            )

    def test_12_the_accounting_manager_can_reopen(
        self, accounting_manager: User, organization: Organization
    ) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        _close_through(organization, period)
        assert period.state == PeriodState.CLOSED

        reopened = reopen_accounting_period(
            actor=accounting_manager,
            period_id=period.pk,
            reason="supplier invoice arrived after close",
        )

        assert reopened.state == PeriodState.OPEN

    def test_a_manager_from_another_organization_cannot_reopen_here(
        self, organization: Organization, other_organization: Organization
    ) -> None:
        """Organization scope narrows to one organization, not to all of them."""
        from apps.organizations.models import Role
        from apps.organizations.services import grant_organization_access

        rival_manager = User.objects.create_user(
            username="rival-chief", password="pw-not-real-1234"
        )
        grant_organization_access(
            user=rival_manager,
            organization=other_organization,
            role=Role.ACCOUNTING_MANAGER,
        )
        rival_manager = User.objects.get(pk=rival_manager.pk)

        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        _close_through(organization, period)

        with pytest.raises(PermissionDenied):
            reopen_accounting_period(
                actor=rival_manager, period_id=period.pk, reason="reaching across"
            )


# --- 13. The reason ---------------------------------------------------------


class TestReopenReason:
    def test_13_a_whitespace_only_reason_is_refused(
        self, accounting_manager: User, organization: Organization
    ) -> None:
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        _close_through(organization, period)

        with pytest.raises(ValidationError) as caught:
            reopen_accounting_period(
                actor=accounting_manager, period_id=period.pk, reason="   \t\n  "
            )

        assert caught.value.code == "reopen_reason_required"
        period.refresh_from_db()
        assert period.state == PeriodState.CLOSED

    def test_the_reopening_records_actor_organization_period_states_and_reason(
        self, accounting_manager: User, organization: Organization
    ) -> None:
        from apps.core.models import AuditAction, AuditEvent

        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        _close_through(organization, period)

        reopen_accounting_period(
            actor=accounting_manager,
            period_id=period.pk,
            reason="credit note received after close",
        )

        reopened = AuditEvent.objects.filter(action=AuditAction.PERIOD_REOPENED).latest(
            "occurred_at"
        )
        assert reopened.actor_id == accounting_manager.pk
        assert reopened.reason == "credit note received after close"
        assert reopened.occurred_at is not None
        # Both snapshots must be present: an audit row that recorded only the
        # new state would say a period is open without saying it was sealed.
        assert reopened.previous_state is not None
        assert reopened.new_state is not None
        assert reopened.previous_state["state"] == PeriodState.CLOSED
        assert reopened.new_state["state"] == PeriodState.OPEN

        override = AuditEvent.objects.filter(action=AuditAction.PERMISSION_OVERRIDE).latest(
            "occurred_at"
        )
        assert override.metadata["permission"] == "accounting.reopen_period"
        assert override.metadata["organization"] == organization.code

    def test_the_superuser_emergency_path_still_requires_a_reason(
        self, superuser: User, organization: Organization
    ) -> None:
        """
        Emergency authority reaches the same service, so it inherits every
        guard that service applies. It is not a way around them.
        """
        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        _close_through(organization, period)

        with pytest.raises(ValidationError) as caught:
            reopen_accounting_period(actor=superuser, period_id=period.pk, reason="  ")
        assert caught.value.code == "reopen_reason_required"

    def test_the_superuser_emergency_path_still_obeys_reopen_ordering(
        self, superuser: User, organization: Organization
    ) -> None:
        march = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        _close_through(organization, march)
        april = AccountingPeriod.objects.get(fiscal_year=march.fiscal_year, period_number=4)
        close_period(period=april, reason="year end")

        with pytest.raises(ValidationError) as caught:
            reopen_accounting_period(actor=superuser, period_id=march.pk, reason="out of order")
        assert caught.value.code == "reopen_out_of_order"


# --- 14. No raw edit path ---------------------------------------------------


class TestNoRawLedgerEdits:
    def test_14_holding_every_permission_does_not_permit_a_raw_posted_edit(
        self,
        superuser: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Authority is about which *commands* may be issued. It never becomes a
        licence to write the table, and the database enforces that whoever is
        asking.
        """
        from django.db import IntegrityError, transaction

        entry = _posted_entry(organization, cash, sales, branch, hall)

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).update(narration="rewritten")

    def test_14b_a_posted_line_cannot_be_rewritten_either(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        from django.db import IntegrityError, transaction

        from apps.accounting.models import JournalLine

        entry = _posted_entry(organization, cash, sales, branch, hall)

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalLine.objects.filter(entry=entry).update(debit=Decimal("1"))

    def test_14c_the_api_layer_never_imports_the_kernel_directly(self) -> None:
        """
        An architectural test, because the rule is architectural: the API
        reaches the kernel through `commands`, which is where authorization
        lives. A view importing `services` would be a posting path with no
        permission check, and it would look entirely reasonable in review.
        """
        import ast
        import pathlib

        source = pathlib.Path(__file__).resolve().parents[1] / "api.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "apps.accounting.services" not in imported
        assert "apps.accounting.commands" in imported
