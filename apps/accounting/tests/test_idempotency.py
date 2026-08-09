"""
Idempotency keys: organization-scoped, and verified against the request.

Two things have to be true at once. A retry must not double-post, and a key
must not be a way to reach into another organization or to receive a journal
other than the one the caller asked for. The second is easy to lose: matching
on the key alone looks like idempotency and is actually a lookup by
attacker-supplied string.

Keys are frequently predictable — upstream modules build them from document
numbers — so "guess a key" is a realistic attack, not a theoretical one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounting.models import Account, CostCenter, JournalEntry, SourceEvent
from apps.accounting.selectors import entry_by_idempotency_key
from apps.accounting.services import create_draft, post_entry, reverse_entry
from apps.accounting.validators import PostingLine
from apps.organizations.models import Branch, Organization

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db

KEY = "invoice-145"


def _lines(
    cash: Account,
    sales: Account,
    branch: Branch,
    hall: CostCenter,
    amount: str = "100000",
) -> list[PostingLine]:
    return [
        PostingLine(account=cash, branch=branch, debit=Decimal(amount)),
        PostingLine(account=sales, branch=branch, credit=Decimal(amount), cost_center=hall),
    ]


class TestSameKeySameRequest:
    def test_a_retry_returns_the_original_and_creates_nothing(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        first = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )
        second = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )

        assert first.pk == second.pk
        assert JournalEntry.objects.count() == 1

    def test_a_differing_narration_is_still_the_same_request(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Narration describes the entry without changing what it does to the
        ledger. A client that regenerates descriptive text on retry — a
        timestamp, a hostname — must not be told its retry is a conflict.
        """
        first = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
            narration="cash sale",
        )
        second = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
            narration="cash sale (retry at 09:41)",
        )

        assert first.pk == second.pk

    def test_a_retry_after_a_successful_posting_never_duplicates(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """Five retries of a timed-out request. One journal."""
        for _attempt in range(5):
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=_lines(cash, sales, branch, hall),
                idempotency_key=KEY,
                source_document_type="PURCHASE_INVOICE",
                source_document_id="145",
                source_event=SourceEvent.POSTED,
            )

        assert JournalEntry.objects.count() == 1


class TestSameKeyDifferentRequest:
    def test_changed_lines_are_a_conflict(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The failure this exists to prevent: a caller corrects an amount, keeps
        the key, and would otherwise receive the *uncorrected* journal while
        believing the correction had posted.
        """
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall, amount="100000"),
            idempotency_key=KEY,
        )

        with pytest.raises(ValidationError) as caught:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=_lines(cash, sales, branch, hall, amount="250000"),
                idempotency_key=KEY,
            )

        assert caught.value.code == "idempotency_key_conflict"
        assert JournalEntry.objects.count() == 1

    def test_a_changed_account_is_a_conflict(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        rent: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )

        with pytest.raises(ValidationError) as caught:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(
                        account=rent, branch=branch, debit=Decimal("100000"), cost_center=hall
                    ),
                    PostingLine(account=cash, branch=branch, credit=Decimal("100000")),
                ],
                idempotency_key=KEY,
            )

        assert caught.value.code == "idempotency_key_conflict"

    def test_a_changed_source_identity_is_a_conflict(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
            source_document_type="PURCHASE_INVOICE",
            source_document_id="145",
            source_event=SourceEvent.POSTED,
        )

        with pytest.raises(ValidationError) as caught:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=_lines(cash, sales, branch, hall),
                idempotency_key=KEY,
                source_document_type="PURCHASE_INVOICE",
                source_document_id="146",
                source_event=SourceEvent.POSTED,
            )

        assert caught.value.code == "idempotency_key_conflict"

    def test_a_changed_accounting_date_is_a_conflict(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """The date decides the period, so it is never incidental."""
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )

        with pytest.raises(ValidationError) as caught:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE.replace(month=4),
                lines=_lines(cash, sales, branch, hall),
                idempotency_key=KEY,
            )

        assert caught.value.code == "idempotency_key_conflict"

    def test_a_different_command_under_the_same_key_is_a_conflict(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """`create_draft` and `post_entry` are not the same operation."""
        create_draft(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )

        with pytest.raises(ValidationError) as caught:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=_lines(cash, sales, branch, hall),
                idempotency_key=KEY,
            )

        assert caught.value.code == "idempotency_key_conflict"


class TestKeysAreScopedToTheOrganization:
    def test_the_same_key_in_another_organization_is_independent(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
        other_cash: Account,
        other_branch: Branch,
        other_hall: CostCenter,
    ) -> None:
        """
        Two organizations both number their invoices from 1 and both compose
        the key `invoice-145`. Neither may block the other.
        """
        other_sales = Account.objects.get(organization=other_organization, code="4-01-01-001")

        mine = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )
        theirs = post_entry(
            organization=other_organization,
            accounting_date=POSTING_DATE,
            lines=_lines(other_cash, other_sales, other_branch, other_hall),
            idempotency_key=KEY,
        )

        assert mine.pk != theirs.pk
        assert mine.organization_id == organization.pk
        assert theirs.organization_id == other_organization.pk
        assert JournalEntry.objects.filter(idempotency_key=KEY).count() == 2

    def test_a_key_cannot_be_used_to_discover_another_organizations_journal(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
        other_cash: Account,
        other_branch: Branch,
        other_hall: CostCenter,
    ) -> None:
        """
        The tenancy leak this scoping closes. Before it, posting into the
        rival organization with a key Khan Mandi had already used returned
        *Khan Mandi's journal* — a cross-tenant read through a guessed string.
        """
        other_sales = Account.objects.get(organization=other_organization, code="4-01-01-001")

        mine = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )
        theirs = post_entry(
            organization=other_organization,
            accounting_date=POSTING_DATE,
            lines=_lines(other_cash, other_sales, other_branch, other_hall),
            idempotency_key=KEY,
        )

        assert theirs.pk != mine.pk
        assert theirs.organization_id == other_organization.pk
        # Both are JE-2026-000001: numbering is gapless *per organization*, so
        # the number is not what distinguishes them. The organization is.
        assert theirs.entry_number == mine.entry_number
        assert theirs.lines.first().branch.organization_id == other_organization.pk  # type: ignore[union-attr]

    def test_the_selector_requires_an_organization(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )

        assert entry_by_idempotency_key(organization=organization, key=KEY) is not None
        assert entry_by_idempotency_key(organization=other_organization, key=KEY) is None

    def test_the_database_enforces_uniqueness_per_organization(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """The constraint, reached by a writer that skipped the service."""
        original = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.create(
                organization=organization,
                period=original.period,
                entry_number="JE-2026-999998",
                accounting_date=POSTING_DATE,
                document_date=POSTING_DATE,
                idempotency_key=KEY,
            )


class TestReversalIdempotency:
    def test_a_reversal_retry_never_creates_a_second_mirror(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key=KEY,
        )
        reverse_entry(entry=entry, idempotency_key="rev-145", reason="cancelled")

        with pytest.raises(ValidationError) as caught:
            reverse_entry(entry=entry, idempotency_key="rev-145", reason="cancelled")

        assert caught.value.code == "already_reversed"
        assert JournalEntry.objects.filter(reverses=entry).count() == 1
