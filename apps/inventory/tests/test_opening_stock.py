"""
The opening-stock document: lifecycle, maker-checker, atomic posting, and
reversal (Task 1.3 §V 16–52).

The convention throughout: documents are built through the command layer with
a real actor, exactly as a view or the API would, so every test also exercises
the authorization that protects the act it tests.
"""

from __future__ import annotations

import datetime
import zoneinfo
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_OPENING_EQUITY,
    Account,
    AccountingPeriod,
    AccountRole,
    JournalEntry,
    JournalEntryStatus,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.core.context import audit_context
from apps.core.models import AuditEvent
from apps.inventory.commands import (
    add_opening_line,
    create_opening,
    delete_opening,
    post_opening,
    remove_opening_line,
    return_opening_to_draft,
    reverse_opening,
    submit_opening,
    update_opening,
)
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    ItemCategory,
    ItemType,
    MovementType,
    OpeningStockDocument,
    OpeningStockStatus,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
    ValuationAllocation,
    ValuationLayer,
    Warehouse,
)
from apps.inventory.opening import OpeningLineInput
from apps.inventory.services import create_item, create_warehouse
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_organization_access
from apps.units.models import UnitOfMeasure
from apps.users.models import User

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
CUTOFF = datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def accounting(organization: Organization) -> None:
    """A fiscal year of OPEN periods and the seeded chart."""
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def control_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-01-001")


@pytest.fixture
def equity_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="3-02-01-001")


@pytest.fixture
def second_control_account(organization: Organization, accounting: None) -> Account:
    """A second postable asset account, for the grouped-debit test."""
    return Account.objects.get(organization=organization, code="1-03-02-001")


@pytest.fixture
def mapped(organization: Organization, control_account: Account, equity_account: Account) -> None:
    """The two mappings every opening needs."""
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_CONTROL),
        account=control_account,
        effective_from=datetime.date(TEST_YEAR, 1, 1),
    )
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_OPENING_EQUITY),
        account=equity_account,
        effective_from=datetime.date(TEST_YEAR, 1, 1),
    )


@pytest.fixture
def second_accounting_manager(organization: Organization) -> User:
    """A second organization-level approver, so maker-checker can be satisfied."""
    user = User.objects.create_user(username="second-approver", password="pw-not-real-1234")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def draft(
    manager: User,
    organization: Organization,
    branch: Branch,
    accounting: None,
) -> OpeningStockDocument:
    return create_opening(
        actor=manager,
        organization=organization,
        branch=branch,
        cutoff_at=CUTOFF,
        evidence_reference="COUNT-SHEET-001",
        narration="جرد افتتاحي",
    )


def _line(
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    cost: str,
    **extra: object,
) -> OpeningLineInput:
    return OpeningLineInput(
        warehouse=warehouse,
        item=item,
        base_quantity=Decimal(quantity),
        unit_cost=Decimal(cost),
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def rice_line(
    draft: OpeningStockDocument, manager: User, main_store: Warehouse, rice: InventoryItem
) -> object:
    return add_opening_line(
        actor=manager, document=draft, line=_line(main_store, rice, "100.000", "1500")
    )


def _submitted(
    draft: OpeningStockDocument, manager: User, *lines: OpeningLineInput
) -> OpeningStockDocument:
    for line in lines:
        add_opening_line(actor=manager, document=draft, line=line)
    return submit_opening(actor=manager, document=draft)


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


class TestDraftLifecycle:
    def test_a_draft_is_editable(self, draft: OpeningStockDocument, manager: User) -> None:
        updated = update_opening(
            actor=manager, document=draft, evidence_reference="COUNT-SHEET-002"
        )
        assert updated.evidence_reference == "COUNT-SHEET-002"

    def test_the_cutoff_must_be_timezone_aware(
        self, manager: User, organization: Organization, branch: Branch, accounting: None
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_opening(
                actor=manager,
                organization=organization,
                branch=branch,
                cutoff_at=datetime.datetime(TEST_YEAR, 3, 15, 10, 0),
                evidence_reference="X",
            )
        assert caught.value.code == "cutoff_must_be_aware"

    def test_the_evidence_reference_is_required(
        self, manager: User, organization: Organization, branch: Branch, accounting: None
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_opening(
                actor=manager,
                organization=organization,
                branch=branch,
                cutoff_at=CUTOFF,
                evidence_reference="   ",
            )
        assert caught.value.code == "evidence_reference_required"

    def test_the_business_date_is_derived_through_the_branch_cutoff(
        self, manager: User, organization: Organization, branch: Branch, accounting: None
    ) -> None:
        """
        02:00 Baghdad is before the 09:00 operating-day start, so the moment
        belongs to the PREVIOUS business day — never `date(timestamp)`.
        """
        small_hours = datetime.datetime(TEST_YEAR, 3, 15, 2, 0, tzinfo=BAGHDAD)
        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=small_hours,
            evidence_reference="NIGHT-COUNT",
        )
        assert document.business_date == datetime.date(TEST_YEAR, 3, 14)

    def test_a_draft_may_be_deleted_and_leaves_an_audit_trail(
        self, draft: OpeningStockDocument, manager: User, rice_line: Any
    ) -> None:
        pk = draft.pk
        delete_opening(actor=manager, document=draft, reason="duplicate")
        assert not OpeningStockDocument.objects.filter(pk=pk).exists()
        assert AuditEvent.objects.filter(
            target_type="inventory.OpeningStockDocument", target_id=str(pk), action="DELETED"
        ).exists()

    def test_a_submitted_document_refuses_ordinary_editing(
        self, draft: OpeningStockDocument, manager: User, rice_line: Any
    ) -> None:
        submit_opening(actor=manager, document=draft)
        with pytest.raises(ValidationError) as caught:
            update_opening(actor=manager, document=draft, narration="quietly changed")
        assert caught.value.code == "not_a_draft"
        with pytest.raises(ValidationError):
            add_opening_line(
                actor=manager,
                document=draft,
                line=_line(rice_line.warehouse, rice_line.item, "1", "1"),
            )
        with pytest.raises(ValidationError):
            delete_opening(actor=manager, document=draft)

    def test_return_to_draft_needs_a_reason_and_is_audited(
        self, draft: OpeningStockDocument, manager: User, rice_line: Any
    ) -> None:
        submit_opening(actor=manager, document=draft)
        with pytest.raises(ValidationError) as caught:
            return_opening_to_draft(actor=manager, document=draft, reason="  ")
        assert caught.value.code == "reason_required"

        returned = return_opening_to_draft(
            actor=manager, document=draft, reason="wrong warehouse counted"
        )
        assert returned.status == OpeningStockStatus.DRAFT
        assert returned.submitted_by is None
        event = AuditEvent.objects.filter(
            target_type="inventory.OpeningStockDocument",
            target_id=str(draft.pk),
            action="REJECTED",
        ).first()
        assert event is not None
        assert event.reason == "wrong warehouse counted"
        # ...and the returned draft is editable again.
        update_opening(actor=manager, document=returned, narration="corrected")

    def test_an_empty_document_cannot_be_submitted(
        self, draft: OpeningStockDocument, manager: User
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            submit_opening(actor=manager, document=draft)
        assert caught.value.code == "no_lines"


class TestLineRules:
    def test_a_warehouse_of_another_branch_is_rejected(
        self, draft: OpeningStockDocument, manager: User, second_branch: Branch, rice: InventoryItem
    ) -> None:
        elsewhere = create_warehouse(branch=second_branch, code="KAR", name="مخزن الكرادة")
        with pytest.raises(ValidationError) as caught:
            add_opening_line(actor=manager, document=draft, line=_line(elsewhere, rice, "1", "1"))
        assert caught.value.code == "warehouse_branch_mismatch"

    def test_the_system_in_transit_warehouse_takes_no_opening(
        self, draft: OpeningStockDocument, manager: User, branch: Branch, rice: InventoryItem
    ) -> None:
        from apps.inventory.services import ensure_in_transit_warehouse

        transit = ensure_in_transit_warehouse(branch=branch)
        with pytest.raises(ValidationError) as caught:
            add_opening_line(actor=manager, document=draft, line=_line(transit, rice, "1", "1"))
        assert caught.value.code == "opening_into_system_warehouse"

    def test_quantity_and_cost_must_be_positive(
        self, draft: OpeningStockDocument, manager: User, main_store: Warehouse, rice: InventoryItem
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            add_opening_line(
                actor=manager, document=draft, line=_line(main_store, rice, "0", "1500")
            )
        assert caught.value.code == "quantity_not_positive"
        with pytest.raises(ValidationError) as caught:
            add_opening_line(actor=manager, document=draft, line=_line(main_store, rice, "5", "0"))
        assert caught.value.code == "unit_cost_not_positive"

    def test_a_positive_quantity_with_a_value_rounding_to_zero_is_refused(
        self, draft: OpeningStockDocument, manager: User, main_store: Warehouse, rice: InventoryItem
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            add_opening_line(
                actor=manager, document=draft, line=_line(main_store, rice, "0.001", "0.000001")
            )
        assert caught.value.code == "line_value_not_positive"

    def test_one_valuation_key_appears_once_per_document(
        self,
        draft: OpeningStockDocument,
        manager: User,
        main_store: Warehouse,
        rice: InventoryItem,
        rice_line: Any,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            add_opening_line(
                actor=manager, document=draft, line=_line(main_store, rice, "5", "1400")
            )
        assert caught.value.code == "duplicate_valuation_key"

    def test_a_lot_tracked_item_requires_its_lot(
        self,
        draft: OpeningStockDocument,
        manager: User,
        main_store: Warehouse,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        chicken = create_item(
            organization=organization,
            code="CHICKEN",
            name="دجاج",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
            tracks_lots=True,
        )
        with pytest.raises(ValidationError) as caught:
            add_opening_line(
                actor=manager, document=draft, line=_line(main_store, chicken, "10", "4000")
            )
        assert caught.value.code == "lot_required"

        lot = InventoryLot.objects.create(organization=organization, item=chicken, code="LOT-A")
        line = add_opening_line(
            actor=manager,
            document=draft,
            line=_line(main_store, chicken, "10", "4000", lot=lot),
        )
        assert line.lot == lot

    def test_an_untracked_item_refuses_a_lot(
        self,
        draft: OpeningStockDocument,
        manager: User,
        main_store: Warehouse,
        rice: InventoryItem,
        organization: Organization,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        chicken = create_item(
            organization=organization,
            code="CHICKEN2",
            name="دجاج ٢",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
            tracks_lots=True,
        )
        lot = InventoryLot.objects.create(organization=organization, item=chicken, code="LOT-B")
        with pytest.raises(ValidationError) as caught:
            add_opening_line(
                actor=manager, document=draft, line=_line(main_store, rice, "5", "1500", lot=lot)
            )
        assert caught.value.code == "lot_not_allowed"

    def test_a_line_can_be_removed_from_a_draft(
        self, draft: OpeningStockDocument, manager: User, rice_line: Any
    ) -> None:
        remove_opening_line(actor=manager, line=rice_line, reason="typo")
        assert draft.lines.count() == 0


# ---------------------------------------------------------------------------
# Maker-checker
# ---------------------------------------------------------------------------


class TestMakerChecker:
    def test_the_submitter_cannot_post_even_with_both_permissions(
        self,
        organization: Organization,
        branch: Branch,
        accounting: None,
        mapped: None,
        accounting_manager: User,
        main_store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        document = create_opening(
            actor=accounting_manager,
            organization=organization,
            branch=branch,
            cutoff_at=CUTOFF,
            evidence_reference="SELF-DEAL",
        )
        add_opening_line(
            actor=accounting_manager, document=document, line=_line(main_store, rice, "10", "1500")
        )
        submit_opening(actor=accounting_manager, document=document)
        with pytest.raises(ValidationError) as caught:
            post_opening(actor=accounting_manager, document=document)
        assert caught.value.code == "submitter_cannot_post"

    def test_a_different_authorized_actor_posts(
        self,
        draft: OpeningStockDocument,
        manager: User,
        rice_line: Any,
        mapped: None,
        accounting_manager: User,
    ) -> None:
        submit_opening(actor=manager, document=draft)
        posted = post_opening(actor=accounting_manager, document=draft)
        assert posted.status == OpeningStockStatus.POSTED
        assert posted.posted_by == accounting_manager
        assert posted.submitted_by == manager

    def test_the_database_also_refuses_submitter_equals_poster(
        self, draft: OpeningStockDocument, manager: User, rice_line: Any
    ) -> None:
        submit_opening(actor=manager, document=draft)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                OpeningStockDocument.objects.filter(pk=draft.pk).update(
                    posted_by=manager, posted_at=timezone.now()
                )

    def test_a_manager_cannot_post_at_all(
        self, draft: OpeningStockDocument, manager: User, rice_line: Any, second_branch: Branch
    ) -> None:
        """Branch authority prepares; posting is organization authority."""
        submit_opening(actor=manager, document=draft)
        with pytest.raises(PermissionDenied):
            post_opening(actor=manager, document=draft)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


class TestPosting:
    @pytest.fixture
    def posted(
        self,
        draft: OpeningStockDocument,
        manager: User,
        rice_line: Any,
        mapped: None,
        accounting_manager: User,
    ) -> OpeningStockDocument:
        submit_opening(actor=manager, document=draft)
        return post_opening(actor=accounting_manager, document=draft)

    def test_the_opening_creates_movement_balance_layer_and_journal(
        self, posted: OpeningStockDocument, rice: InventoryItem
    ) -> None:
        movement = StockMovement.objects.get(item=rice)
        assert movement.movement_type == MovementType.OPENING
        assert movement.base_quantity == Decimal("100.000")
        assert movement.inventory_value == Decimal("150000.000")

        balance = StockBalance.objects.get(item=rice)
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("150000.000")
        assert balance.average_cost == Decimal("1500.000000")

        assert ValuationLayer.objects.filter(item=rice).count() == 1
        assert posted.journal_entry is not None
        assert posted.journal_entry.status == JournalEntryStatus.POSTED

    def test_moving_average_creates_no_valuation_allocation(
        self, posted: OpeningStockDocument
    ) -> None:
        assert ValuationAllocation.objects.count() == 0

    def test_the_journal_balances_and_mirrors_the_stored_line_values(
        self, posted: OpeningStockDocument, control_account: Account, equity_account: Account
    ) -> None:
        assert posted.journal_entry is not None
        lines = list(posted.journal_entry.lines.order_by("line_number"))
        assert len(lines) == 2
        debit = lines[0]
        credit = lines[1]
        assert debit.account == control_account
        assert debit.debit == Decimal("150000.000")
        assert credit.account == equity_account
        assert credit.credit == Decimal("150000.000")
        assert debit.branch_id == posted.branch_id

    def test_line_movement_and_journal_all_carry_the_same_value(
        self, posted: OpeningStockDocument
    ) -> None:
        line = posted.lines.get()
        assert line.total_value == Decimal("150000.000")
        assert line.movement is not None
        assert line.movement.inventory_value == line.total_value
        assert line.journal_line is not None
        assert line.journal_line.debit == line.total_value
        assert line.inventory_account_id is not None
        assert line.resolved_organization_mapping is not None

    def test_source_identity_uses_the_immutable_public_id(
        self, posted: OpeningStockDocument
    ) -> None:
        entry = posted.stock_entry
        assert entry is not None
        assert entry.source_document_type == "INVENTORY_OPENING"
        assert entry.source_document_id == str(posted.public_id)
        assert entry.source_event == "POSTED"
        journal = posted.journal_entry
        assert journal is not None
        assert journal.source_document_id == str(posted.public_id)
        # The human number is display metadata, assigned at posting.
        assert posted.document_number.startswith(f"OPN-{TEST_YEAR}-")
        assert posted.document_number != str(posted.public_id)

    def test_the_effect_key_is_the_stable_line_identity(self, posted: OpeningStockDocument) -> None:
        line = posted.lines.get()
        assert line.movement is not None
        assert line.movement.effect_key == f"opening-line:{line.line_uid}"

    def test_a_posted_document_is_immutable_at_the_database(
        self, posted: OpeningStockDocument
    ) -> None:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                OpeningStockDocument.objects.filter(pk=posted.pk).update(
                    evidence_reference="rewritten"
                )

    def test_posted_lines_are_frozen_at_the_database(self, posted: OpeningStockDocument) -> None:
        line = posted.lines.get()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                type(line).objects.filter(pk=line.pk).update(base_quantity=Decimal("999"))
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                line.delete()

    def test_a_second_post_attempt_is_refused(
        self, posted: OpeningStockDocument, accounting_manager: User
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            post_opening(actor=accounting_manager, document=posted)
        assert caught.value.code == "already_posted"

    def test_an_open_period_is_required_soft_closed_refused(
        self,
        draft: OpeningStockDocument,
        manager: User,
        rice_line: Any,
        mapped: None,
        accounting_manager: User,
        organization: Organization,
    ) -> None:
        submit_opening(actor=manager, document=draft)
        period = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, period_number=3
        )
        period.state = "SOFT_CLOSED"
        period.save(update_fields=["state"])
        with pytest.raises(ValidationError) as caught:
            post_opening(actor=accounting_manager, document=draft)
        assert caught.value.code in {"period_soft_closed", "period_not_open"}

        period.state = "CLOSED"
        period.save(update_fields=["state"])
        with pytest.raises(ValidationError) as caught:
            post_opening(actor=accounting_manager, document=draft)
        assert caught.value.code in {"period_closed", "period_not_open"}

        period.state = "OPEN"
        period.save(update_fields=["state"])
        posted = post_opening(actor=accounting_manager, document=draft)
        assert posted.status == OpeningStockStatus.POSTED

    def test_a_missing_mapping_rolls_the_whole_posting_back(
        self,
        draft: OpeningStockDocument,
        manager: User,
        rice_line: Any,
        accounting_manager: User,
        organization: Organization,
    ) -> None:
        """No mapping fixture here — resolution fails AFTER the stock entry
        would have been written, and everything must vanish with it."""
        submit_opening(actor=manager, document=draft)
        with pytest.raises(ValidationError) as caught:
            post_opening(actor=accounting_manager, document=draft)
        assert caught.value.code == "account_role_unmapped"

        draft.refresh_from_db()
        assert draft.status == OpeningStockStatus.SUBMITTED
        assert draft.document_number == ""
        assert StockMovement.objects.count() == 0
        assert StockBalance.objects.count() == 0
        assert JournalEntry.objects.count() == 0
        assert StockLedgerEntry.objects.count() == 0

    def test_document_numbering_is_gapless_across_a_failed_attempt(
        self,
        organization: Organization,
        branch: Branch,
        accounting: None,
        mapped: None,
        manager: User,
        accounting_manager: User,
        main_store: Warehouse,
        rice: InventoryItem,
        second_branch: Branch,
    ) -> None:
        """A failed posting burns no number; the next success takes 000002."""
        first = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=CUTOFF,
            evidence_reference="A",
        )
        add_opening_line(actor=manager, document=first, line=_line(main_store, rice, "10", "100"))
        submit_opening(actor=manager, document=first)
        first = post_opening(actor=accounting_manager, document=first)
        assert first.document_number == f"OPN-{TEST_YEAR}-000001"

        # A second document for the SAME key fails (history exists) — and must
        # not consume a number.
        second = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=CUTOFF,
            evidence_reference="B",
        )
        add_opening_line(actor=manager, document=second, line=_line(main_store, rice, "5", "100"))
        submit_opening(actor=manager, document=second)
        with pytest.raises(ValidationError) as caught:
            post_opening(actor=accounting_manager, document=second)
        assert caught.value.code == "opening_key_already_has_history"

        # A third, for a different warehouse, succeeds with the next number.
        other_store = create_warehouse(branch=branch, code="COLD", name="المبردات")
        third = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=CUTOFF,
            evidence_reference="C",
        )
        add_opening_line(actor=manager, document=third, line=_line(other_store, rice, "5", "100"))
        submit_opening(actor=manager, document=third)
        third = post_opening(actor=accounting_manager, document=third)
        assert third.document_number == f"OPN-{TEST_YEAR}-000002"

    def test_a_key_with_prior_movement_history_is_refused(
        self,
        organization: Organization,
        branch: Branch,
        accounting: None,
        mapped: None,
        manager: User,
        accounting_manager: User,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting_manager_posts_first_receipt: None,
    ) -> None:
        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=CUTOFF,
            evidence_reference="LATE",
        )
        add_opening_line(actor=manager, document=document, line=_line(main_store, rice, "5", "100"))
        submit_opening(actor=manager, document=document)
        with pytest.raises(ValidationError) as caught:
            post_opening(actor=accounting_manager, document=document)
        assert caught.value.code == "opening_key_already_has_history"

    @pytest.fixture
    def accounting_manager_posts_first_receipt(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
        superuser: User,
    ) -> None:
        """A RECEIPT that predates the opening, via the kernel directly."""
        with audit_context(actor=superuser):
            post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("1"),
                        effect_key="receipt:1",
                        unit_cost=Decimal("100"),
                    )
                ],
                idempotency_key="receipt-before-opening",
                effective_at=CUTOFF,
            )

    def test_grouped_debits_when_items_resolve_to_different_accounts(
        self,
        organization: Organization,
        branch: Branch,
        accounting: None,
        mapped: None,
        manager: User,
        accounting_manager: User,
        main_store: Warehouse,
        rice: InventoryItem,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        second_control_account: Account,
        equity_account: Account,
        control_account: Account,
    ) -> None:
        """An item override sends oil to a second control account; the journal
        carries one debit per account, each the exact sum of its lines."""
        from apps.inventory.accounts import create_inventory_mapping

        oil = create_item(
            organization=organization,
            code="OIL",
            name="زيت",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        create_inventory_mapping(
            organization=organization,
            role=INVENTORY_CONTROL,
            account=second_control_account,
            item=oil,
            effective_from=datetime.date(TEST_YEAR, 1, 1),
        )

        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=CUTOFF,
            evidence_reference="TWO-ACCOUNTS",
        )
        add_opening_line(
            actor=manager, document=document, line=_line(main_store, rice, "10", "1500")
        )
        add_opening_line(actor=manager, document=document, line=_line(main_store, oil, "4", "2500"))
        submit_opening(actor=manager, document=document)
        posted = post_opening(actor=accounting_manager, document=document)

        assert posted.journal_entry is not None
        journal_lines = list(posted.journal_entry.lines.order_by("line_number"))
        debits = {line.account.code: line.debit for line in journal_lines if line.debit > 0}
        credits = [line for line in journal_lines if line.credit > 0]
        assert debits == {
            control_account.code: Decimal("15000.000"),
            second_control_account.code: Decimal("10000.000"),
        }
        assert len(credits) == 1
        assert credits[0].account == equity_account
        assert credits[0].credit == Decimal("25000.000")

        oil_line = posted.lines.get(item=oil)
        assert oil_line.resolved_mapping is not None
        assert oil_line.inventory_account == second_control_account


# ---------------------------------------------------------------------------
# Idempotency, exercised on the exact keys the opening service uses
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.fixture
    def posted(
        self,
        draft: OpeningStockDocument,
        manager: User,
        rice_line: Any,
        mapped: None,
        accounting_manager: User,
    ) -> OpeningStockDocument:
        submit_opening(actor=manager, document=draft)
        return post_opening(actor=accounting_manager, document=draft)

    def test_the_same_key_and_payload_returns_the_original_posting(
        self,
        posted: OpeningStockDocument,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        superuser: User,
    ) -> None:
        original = posted.stock_entry
        assert original is not None
        with audit_context(actor=superuser):
            replayed = post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.OPENING,
                        quantity=Decimal("100.000"),
                        effect_key=f"opening-line:{posted.lines.get().line_uid}",
                        unit_cost=Decimal("1500.000000"),
                        source_conversion=None,
                    )
                ],
                idempotency_key=f"inventory-opening:{posted.public_id}",
                effective_at=posted.cutoff_at,
                source_document_type="INVENTORY_OPENING",
                source_document_id=str(posted.public_id),
                source_event="POSTED",
                reference=posted.evidence_reference,
                reason=posted.narration or "opening stock",
            )
        assert replayed.pk == original.pk
        assert StockMovement.objects.count() == 1

    def test_the_same_key_with_a_changed_payload_conflicts(
        self,
        posted: OpeningStockDocument,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        superuser: User,
    ) -> None:
        with audit_context(actor=superuser), pytest.raises(ValidationError) as caught:
            post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.OPENING,
                        quantity=Decimal("999.000"),
                        effect_key="opening-line:changed",
                        unit_cost=Decimal("1500.000000"),
                    )
                ],
                idempotency_key=f"inventory-opening:{posted.public_id}",
                effective_at=posted.cutoff_at,
            )
        assert caught.value.code == "idempotency_key_conflict"


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


class TestReversal:
    @pytest.fixture
    def posted(
        self,
        draft: OpeningStockDocument,
        manager: User,
        rice_line: Any,
        mapped: None,
        accounting_manager: User,
    ) -> OpeningStockDocument:
        submit_opening(actor=manager, document=draft)
        return post_opening(actor=accounting_manager, document=draft)

    def test_reversal_mirrors_quantities_values_and_the_journal_exactly(
        self,
        posted: OpeningStockDocument,
        accounting_manager: User,
        rice: InventoryItem,
        control_account: Account,
        equity_account: Account,
    ) -> None:
        reversed_document = reverse_opening(
            actor=accounting_manager, document=posted, reason="wrong count sheet"
        )
        assert reversed_document.status == OpeningStockStatus.REVERSED

        movements = StockMovement.objects.filter(item=rice).order_by("posted_sequence")
        assert [m.movement_type for m in movements] == [
            MovementType.OPENING,
            MovementType.REVERSAL,
        ]
        original, mirror = movements
        assert mirror.base_quantity == -original.base_quantity
        assert mirror.inventory_value == -original.inventory_value
        assert mirror.reverses_id == original.pk

        balance = StockBalance.objects.get(item=rice)
        assert balance.quantity == Decimal("0")
        assert balance.value == Decimal("0")

        reversal_journal = reversed_document.reversal_journal_entry
        assert reversal_journal is not None
        mirrored = {
            (line.account.code, line.debit, line.credit) for line in reversal_journal.lines.all()
        }
        assert mirrored == {
            (control_account.code, Decimal("0.000"), Decimal("150000.000")),
            (equity_account.code, Decimal("150000.000"), Decimal("0.000")),
        }
        assert posted.journal_entry_id is not None
        original_journal = JournalEntry.objects.get(pk=posted.journal_entry_id)
        assert original_journal.status == JournalEntryStatus.REVERSED

    def test_a_reason_is_required(
        self, posted: OpeningStockDocument, accounting_manager: User
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            reverse_opening(actor=accounting_manager, document=posted, reason="   ")
        assert caught.value.code == "reason_required"

    def test_already_reversed_is_reported_as_such(
        self, posted: OpeningStockDocument, accounting_manager: User
    ) -> None:
        reverse_opening(actor=accounting_manager, document=posted, reason="first")
        with pytest.raises(ValidationError) as caught:
            reverse_opening(actor=accounting_manager, document=posted, reason="second")
        assert caught.value.code == "already_reversed"

    def test_a_consumed_opening_cannot_be_reversed(
        self,
        posted: OpeningStockDocument,
        accounting_manager: User,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        superuser: User,
    ) -> None:
        """Opening +100, issue -80: the goods are gone, the mirror is refused,
        and nothing partial is left behind."""
        with audit_context(actor=superuser):
            post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.ISSUE,
                        quantity=Decimal("80"),
                        effect_key="issue:1",
                    )
                ],
                idempotency_key="consume-most-of-it",
            )
        with pytest.raises(ValidationError) as caught:
            reverse_opening(actor=accounting_manager, document=posted, reason="undo")
        assert caught.value.code == "insufficient_stock"

        posted.refresh_from_db()
        assert posted.status == OpeningStockStatus.POSTED
        assert posted.reversal_journal_entry is None
        # The original journal was not touched either.
        assert JournalEntry.objects.filter(status=JournalEntryStatus.REVERSED).count() == 0

    def test_a_successful_reversal_restores_stock_and_gl_exactly(
        self,
        posted: OpeningStockDocument,
        accounting_manager: User,
        rice: InventoryItem,
        control_account: Account,
    ) -> None:
        from apps.inventory.reconciliation import verify_inventory_accounting

        reverse_opening(actor=accounting_manager, document=posted, reason="restated")
        assert StockBalance.objects.get(item=rice).quantity == Decimal("0")
        # Inventory and GL agree at zero — the reconciliation proves it.
        assert verify_inventory_accounting(posted.organization) == []

    def test_a_reversed_document_is_immutable_at_the_database(
        self, posted: OpeningStockDocument, accounting_manager: User
    ) -> None:
        reverse_opening(actor=accounting_manager, document=posted, reason="done")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                OpeningStockDocument.objects.filter(pk=posted.pk).update(reversal_reason="edited")


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    @pytest.fixture
    def posted(
        self,
        draft: OpeningStockDocument,
        manager: User,
        rice_line: Any,
        mapped: None,
        accounting_manager: User,
    ) -> OpeningStockDocument:
        submit_opening(actor=manager, document=draft)
        return post_opening(actor=accounting_manager, document=draft)

    def test_a_clean_posting_reconciles_in_every_direction(
        self, posted: OpeningStockDocument, organization: Organization
    ) -> None:
        from apps.inventory.reconciliation import (
            verify_inventory_accounting,
            verify_inventory_against_gl,
            verify_opening_document,
            verify_organization,
        )

        assert verify_opening_document(posted) == []
        assert verify_organization(organization) == []
        assert verify_inventory_against_gl(organization) == []
        assert verify_inventory_accounting(organization) == []

    def test_a_manual_journal_against_the_control_account_is_reported(
        self,
        posted: OpeningStockDocument,
        organization: Organization,
        branch: Branch,
        control_account: Account,
        equity_account: Account,
        superuser: User,
    ) -> None:
        """GL drift the inventory ledger never caused — exactly what the
        report exists to surface, and never to repair."""
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine
        from apps.inventory.reconciliation import verify_inventory_against_gl

        with audit_context(actor=superuser):
            post_entry(
                organization=organization,
                accounting_date=datetime.date(TEST_YEAR, 3, 20),
                lines=[
                    PostingLine(account=control_account, branch=branch, debit=Decimal("999.000")),
                    PostingLine(account=equity_account, branch=branch, credit=Decimal("999.000")),
                ],
                idempotency_key="manual-drift",
            )
        problems = verify_inventory_against_gl(organization)
        assert len(problems) == 1
        assert problems[0].field == "inventory_vs_gl"
        # The report changed nothing: the drift is still there to investigate.
        assert verify_inventory_against_gl(organization) == problems

    def test_the_management_command_reports_and_exits_nonzero_on_mismatch(
        self,
        posted: OpeningStockDocument,
        organization: Organization,
        branch: Branch,
        control_account: Account,
        equity_account: Account,
        superuser: User,
        capsys: Any,
    ) -> None:
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine

        call_command("verify_inventory_accounting", organization=organization.code)

        with audit_context(actor=superuser):
            post_entry(
                organization=organization,
                accounting_date=datetime.date(TEST_YEAR, 3, 20),
                lines=[
                    PostingLine(account=control_account, branch=branch, debit=Decimal("1.000")),
                    PostingLine(account=equity_account, branch=branch, credit=Decimal("1.000")),
                ],
                idempotency_key="drift-for-command",
            )
        with pytest.raises(SystemExit):
            call_command("verify_inventory_accounting", organization=organization.code)
