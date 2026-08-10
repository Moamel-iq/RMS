"""
Source identity and idempotency.

`organization + source_document_type + source_document_id + source_event` names
one upstream economic event. The guarantee it buys is narrow and important: a
purchase invoice that is posted twice — by a retry, a duplicated message, a
second click — produces one journal, not two.

Tested at both levels on purpose. The service tests show the behaviour a caller
sees; the constraint tests show that the guarantee survives a caller who never
goes through the service at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounting.models import (
    Account,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    SourceEvent,
)
from apps.accounting.services import post_entry, reverse_entry
from apps.accounting.validators import PostingLine, validate_source_identity
from apps.organizations.models import Branch, Organization

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db

INVOICE = "PURCHASE_INVOICE"


def _lines(
    cash: Account, sales: Account, branch: Branch, hall: CostCenter, amount: str = "100000"
) -> list[PostingLine]:
    return [
        PostingLine(account=cash, branch=branch, debit=Decimal(amount)),
        PostingLine(account=sales, branch=branch, credit=Decimal(amount), cost_center=hall),
    ]


def _post_invoice(
    organization: Organization,
    cash: Account,
    sales: Account,
    branch: Branch,
    hall: CostCenter,
    *,
    source_id: str = "145",
    key: str = "invoice-145",
    event: str = SourceEvent.POSTED,
) -> JournalEntry:
    return post_entry(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=_lines(cash, sales, branch, hall),
        idempotency_key=key,
        source_document_type=INVOICE,
        source_document_id=source_id,
        source_event=event,
    )


class TestNoDuplicatePosting:
    def test_a_retried_command_returns_the_same_journal(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The same command twice — same idempotency key. A network timeout on
        the first attempt must not cost the ledger a second entry.
        """
        first = _post_invoice(organization, cash, sales, branch, hall)
        second = _post_invoice(organization, cash, sales, branch, hall)

        assert first.pk == second.pk
        assert JournalEntry.objects.filter(source_document_id="145").count() == 1

    def test_the_same_event_under_a_different_key_is_a_conflict(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Not a retry: the caller composed a new command for an event already in
        the ledger. Returning the existing entry would hand back something
        they did not ask for, so it is reported instead — and still no second
        journal is created.
        """
        _post_invoice(organization, cash, sales, branch, hall)

        with pytest.raises(ValidationError) as caught:
            _post_invoice(organization, cash, sales, branch, hall, key="invoice-145-again")

        assert caught.value.code == "source_event_already_posted"
        assert JournalEntry.objects.filter(source_document_id="145").count() == 1

    def test_a_conflict_names_the_entry_that_already_holds_the_event(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """An operator has to be able to go and look at it."""
        first = _post_invoice(organization, cash, sales, branch, hall)

        with pytest.raises(ValidationError) as caught:
            _post_invoice(organization, cash, sales, branch, hall, key="different")

        assert first.entry_number in "; ".join(caught.value.messages)

    def test_a_different_document_of_the_same_type_is_unaffected(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        _post_invoice(organization, cash, sales, branch, hall, source_id="145")
        _post_invoice(organization, cash, sales, branch, hall, source_id="146", key="invoice-146")

        assert JournalEntry.objects.filter(source_document_type=INVOICE).count() == 2


class TestScopedToOneOrganization:
    def test_the_same_source_id_is_allowed_in_another_organization(
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
        Two organizations each number their own purchase invoices from 1. The
        uniqueness is per organization for exactly that reason; a global one
        would make the second organization's invoice 145 unpostable.
        """
        other_sales = Account.objects.get(organization=other_organization, code="4-01-01-001")

        _post_invoice(organization, cash, sales, branch, hall, source_id="145")
        rival = post_entry(
            organization=other_organization,
            accounting_date=POSTING_DATE,
            lines=_lines(other_cash, other_sales, other_branch, other_hall),
            idempotency_key="rival-invoice-145",
            source_document_type=INVOICE,
            source_document_id="145",
            source_event=SourceEvent.POSTED,
        )

        assert rival.pk is not None
        assert JournalEntry.objects.filter(source_document_id="145").count() == 2


class TestPostedAndReversedCoexist:
    def test_a_reversal_claims_the_same_document_under_a_different_event(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        `PURCHASE_INVOICE / 145 / POSTED` and `.../ REVERSED` are two distinct
        economic events about one document, so both exist and each is unique
        in its own right.
        """
        original = _post_invoice(organization, cash, sales, branch, hall)

        reversal = reverse_entry(
            entry=original,
            idempotency_key="reversal-of-145",
            reason="invoice cancelled by supplier",
        )

        assert reversal.source_document_type == INVOICE
        assert reversal.source_document_id == "145"
        assert reversal.source_event == SourceEvent.REVERSED

        original.refresh_from_db()
        assert original.source_event == SourceEvent.POSTED
        assert original.status == JournalEntryStatus.REVERSED

    def test_a_manual_journal_reversal_carries_no_source_identity(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """A journal with no upstream document does not acquire one by being reversed."""
        manual = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key="manual-1",
        )

        reversal = reverse_entry(
            entry=manual, idempotency_key="manual-1-rev", reason="keyed in error"
        )

        assert reversal.source_event == ""
        assert reversal.source_document_type == ""

    def test_a_reversal_cannot_itself_be_reversed(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        It would need to claim `.../ REVERSED` a second time. Reported as a
        domain error rather than left to collide on the unique index, and the
        accounting answer is a replacement entry anyway.
        """
        original = _post_invoice(organization, cash, sales, branch, hall)
        reversal = reverse_entry(entry=original, idempotency_key="rev-1", reason="cancelled")

        with pytest.raises(ValidationError) as caught:
            reverse_entry(entry=reversal, idempotency_key="rev-2", reason="changed my mind")

        assert caught.value.code == "cannot_reverse_a_reversal"


class TestTheEnumIsClosed:
    @pytest.mark.parametrize("event", ["POSTEED", "VOIDED", "PAID", "SETTLED"])
    def test_an_unknown_source_event_is_refused(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
        event: str,
    ) -> None:
        """
        The typo case is the whole point. `POSTEED` would sit outside the
        uniqueness guarantee while looking, to a reader, exactly like an entry
        inside it.
        """
        with pytest.raises(ValidationError) as caught:
            _post_invoice(organization, cash, sales, branch, hall, event=event)

        assert caught.value.code == "unknown_source_event"

    @pytest.mark.parametrize("event", ["posted", " Posted ", "REVERSED "])
    def test_but_case_and_padding_are_canonicalised_rather_than_refused(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
        event: str,
    ) -> None:
        """
        Changed by the Task 1.2 source-identity amendment, deliberately.

        `posted` used to be refused as an unknown event. It is now canonicalised
        to `POSTED` before validation, because `source_event` is **our**
        vocabulary and folding its case loses nothing — while leaving it
        unfolded meant `"POSTED"` and `"posted"` were two different economic
        events for the same document, and the uniqueness guarantee missed the
        second one.

        `source_document_id` is deliberately *not* folded the same way: that is
        the supplier's vocabulary, and `AB-1042` and `ab-1042` can be two real
        invoices.
        """
        entry = _post_invoice(organization, cash, sales, branch, hall, event=event)
        assert entry.source_event == event.strip().upper()

    def test_the_database_refuses_one_too(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """The check constraint, reached by a writer that skipped the service."""
        entry = _post_invoice(organization, cash, sales, branch, hall)

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).update(source_event="VOIDED")


class TestIdentityIsCompleteOrAbsent:
    @pytest.mark.parametrize(
        ("source_type", "source_id", "event"),
        [
            (INVOICE, "145", ""),
            (INVOICE, "", SourceEvent.POSTED),
            ("", "145", SourceEvent.POSTED),
            (INVOICE, "", ""),
            ("", "", SourceEvent.POSTED),
        ],
    )
    def test_a_partial_identity_is_refused(
        self, source_type: str, source_id: str, event: str
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            validate_source_identity(
                source_document_type=source_type,
                source_document_id=source_id,
                source_event=event,
            )
        assert caught.value.code == "incomplete_source_identity"

    def test_none_of_the_three_is_fine(self) -> None:
        validate_source_identity(source_document_type="", source_document_id="", source_event="")

    def test_a_partial_identity_is_refused_by_post_entry(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=_lines(cash, sales, branch, hall),
                idempotency_key="partial",
                source_document_type=INVOICE,
                source_document_id="145",
            )
        assert caught.value.code == "incomplete_source_identity"

    def test_the_database_refuses_a_partial_identity_too(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        manual = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=_lines(cash, sales, branch, hall),
            idempotency_key="manual-partial",
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=manual.pk).update(
                source_document_type=INVOICE, source_document_id="999"
            )


class TestSourceIdentityIsImmutable:
    def test_the_source_event_of_a_posted_entry_cannot_be_changed(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        A guarantee that can be edited afterwards is not a guarantee.
        Repointing this entry would free `145 / POSTED` to be claimed again,
        and the unique index would raise no objection because nothing would be
        duplicated at that instant.
        """
        entry = _post_invoice(organization, cash, sales, branch, hall)

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).update(source_event=SourceEvent.REVERSED)

    def test_the_source_document_id_cannot_be_repointed(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = _post_invoice(organization, cash, sales, branch, hall)

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).update(source_document_id="999")

    def test_the_organization_cannot_be_moved(
        self,
        organization: Organization,
        other_organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = _post_invoice(organization, cash, sales, branch, hall)

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).update(organization=other_organization)

    def test_a_posted_narration_cannot_be_rewritten(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Regression for the blocklist hole fixed in migration 0005. The
        immutability trigger used to name the columns that must not change,
        and `narration` was not among them — so a posted journal's description
        could be rewritten years later with no error and no history row.
        """
        entry = _post_invoice(organization, cash, sales, branch, hall)

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).update(narration="rewritten")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("document_date", POSTING_DATE.replace(day=1)),
            ("is_adjustment", True),
            ("posting_rule_version", "v99"),
            ("posted_at", None),
        ],
    )
    def test_no_other_posted_column_can_be_rewritten_either(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
        field: str,
        value: object,
    ) -> None:
        """The allowlist covers every column, not a remembered subset."""
        entry = _post_invoice(organization, cash, sales, branch, hall)

        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).update(**{field: value})


@pytest.mark.django_db(transaction=True)
class TestTheGuaranteeSurvivesACommit:
    """
    The unique index, reached through a real COMMIT.

    The service tests above prove the *application* refuses a duplicate. These
    prove the duplicate is refused even when nothing checks it in Python —
    which is the case that matters, because two concurrent workers can both
    pass a `SELECT ... first()` before either has inserted.
    """

    def test_two_journals_cannot_claim_one_economic_event(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        original = _post_invoice(organization, cash, sales, branch, hall)

        # Exactly what a second worker's INSERT would look like, having read
        # the table before the first worker committed.
        with pytest.raises(IntegrityError), transaction.atomic():
            JournalEntry.objects.create(
                organization=organization,
                period=original.period,
                entry_number="JE-2026-999999",
                accounting_date=POSTING_DATE,
                document_date=POSTING_DATE,
                status=JournalEntryStatus.DRAFT,
                idempotency_key="a-different-key-entirely",
                source_document_type=INVOICE,
                source_document_id="145",
                source_event=SourceEvent.POSTED,
            )

        assert JournalEntry.objects.filter(source_document_id="145").count() == 1

    def test_manual_journals_do_not_collide_with_each_other(
        self,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The index is partial for this reason. Without the condition every
        manual journal would collide with every other on three empty strings,
        and the second one anybody keyed in would fail.
        """
        for number in range(3):
            post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=_lines(cash, sales, branch, hall),
                idempotency_key=f"manual-{number}",
            )

        assert JournalEntry.objects.filter(source_event="").count() == 3
