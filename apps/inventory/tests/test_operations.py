"""
Goods receipts, consumption issues, and returns-in (Task 1.4 §S 9–49).

Every document is built and posted through the command layer with a real
actor, so each test also exercises the authorization protecting the act it
tests.
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

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONSUMPTION,
    INVENTORY_CONTROL,
    Account,
    AccountingPeriod,
    AccountRole,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    PeriodState,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.accounts import create_inventory_mapping
from apps.inventory.commands import (
    add_document_line,
    create_document,
    delete_document,
    post_document,
    remove_document_line,
    reverse_document,
    update_document,
)
from apps.inventory.models import (
    ConversionType,
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    InventoryMovementDocument,
    InventoryMovementDocumentLine,
    ItemCategory,
    ItemPackageConversion,
    ItemType,
    MovementType,
    PackageUnit,
    StockBalance,
    StockMovement,
    ValuationAllocation,
    ValuationLayer,
    Warehouse,
)
from apps.inventory.operations import DocumentLineInput, returnable
from apps.inventory.services import create_item, create_item_conversion
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
WHEN = datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD)
JAN_1 = datetime.date(TEST_YEAR, 1, 1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def control_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-01-001")


@pytest.fixture
def grni_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="2-01-02-001")


@pytest.fixture
def consumption_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="5-01-02-001")


@pytest.fixture
def packaging_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="5-01-02-002")


@pytest.fixture
def kitchen(organization: Organization, accounting: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="KITCHEN")


@pytest.fixture
def hall(organization: Organization, accounting: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="HALL")


@pytest.fixture
def mapped(
    organization: Organization,
    control_account: Account,
    grni_account: Account,
    consumption_account: Account,
) -> None:
    """The three mappings the operational documents need."""
    for code, account in (
        (INVENTORY_CONTROL, control_account),
        (GOODS_RECEIVED_NOT_INVOICED, grni_account),
        (INVENTORY_CONSUMPTION, consumption_account),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=account,
            effective_from=JAN_1,
        )


@pytest.fixture
def keeper(branch: Branch, main_store: Warehouse) -> User:
    """A storekeeper: receives, issues, returns — and never sees cost."""
    user = User.objects.create_user(username="keeper", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


def _line(
    item: InventoryItem, quantity: str, cost: str | None = None, **extra: object
) -> DocumentLineInput:
    return DocumentLineInput(
        item=item,
        base_quantity=Decimal(quantity),
        unit_cost=Decimal(cost) if cost is not None else None,
        **extra,  # type: ignore[arg-type]
    )


def _document(
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    document_type: str,
    **extra: object,
) -> InventoryMovementDocument:
    return create_document(
        actor=actor,
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=document_type,
        effective_at=WHEN,
        evidence_reference="DN-001",
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def receipt(
    manager: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    rice: InventoryItem,
    mapped: None,
) -> InventoryMovementDocument:
    """A posted receipt: 100 kg of rice at 1500, so 150,000 on the shelf."""
    document = _document(manager, organization, branch, main_store, InventoryDocumentType.RECEIPT)
    add_document_line(actor=manager, document=document, line=_line(rice, "100", "1500"))
    return post_document(actor=manager, document=document)


@pytest.fixture
def issued(
    manager: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    rice: InventoryItem,
    kitchen: CostCenter,
    receipt: InventoryMovementDocument,
) -> InventoryMovementDocument:
    """40 kg of that rice issued to the kitchen, at 1500 → 60,000."""
    document = _document(
        manager,
        organization,
        branch,
        main_store,
        InventoryDocumentType.ISSUE,
        cost_center=kitchen,
    )
    add_document_line(actor=manager, document=document, line=_line(rice, "40"))
    return post_document(actor=manager, document=document)


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


class TestReceiptDraft:
    def test_a_draft_is_editable_and_discardable(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        updated = update_document(actor=manager, document=document, evidence_reference="DN-002")
        assert updated.evidence_reference == "DN-002"
        assert updated.document_number == ""

        line = add_document_line(actor=manager, document=document, line=_line(rice, "10", "1000"))
        remove_document_line(actor=manager, line=line)
        assert document.lines.count() == 0

        pk = document.pk
        delete_document(actor=manager, document=document, reason="duplicate")
        assert not InventoryMovementDocument.objects.filter(pk=pk).exists()

    def test_quantity_and_cost_must_be_positive(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        with pytest.raises(ValidationError) as caught:
            add_document_line(actor=manager, document=document, line=_line(rice, "0", "1000"))
        assert caught.value.code == "quantity_not_positive"

        with pytest.raises(ValidationError) as caught:
            add_document_line(actor=manager, document=document, line=_line(rice, "5", "0"))
        assert caught.value.code == "unit_cost_not_positive"

    def test_a_value_rounding_to_zero_is_refused(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager, document=document, line=_line(rice, "0.001", "0.000001")
            )
        assert caught.value.code == "line_value_not_positive"

    def test_a_duplicate_valuation_key_is_rejected(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "10", "1000"))
        with pytest.raises(ValidationError) as caught:
            add_document_line(actor=manager, document=document, line=_line(rice, "5", "1200"))
        assert caught.value.code == "duplicate_valuation_key"

    def test_a_receipt_line_cannot_be_added_without_a_cost(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        with pytest.raises(ValidationError) as caught:
            add_document_line(actor=manager, document=document, line=_line(rice, "10"))
        assert caught.value.code == "unit_cost_required"


class TestReceiptConversions:
    @pytest.fixture
    def sack_conversion(self, rice: InventoryItem, sack: PackageUnit) -> ItemPackageConversion:
        return create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("25"),
            effective_from=JAN_1,
            conversion_type=ConversionType.FIXED,
        )

    @pytest.fixture
    def carton_conversion(self, rice: InventoryItem, carton: PackageUnit) -> ItemPackageConversion:
        return create_item_conversion(
            item=rice,
            package_unit=carton,
            factor_to_base=Decimal("18"),
            effective_from=JAN_1,
            conversion_type=ConversionType.VARIABLE,
        )

    def test_a_fixed_package_converts_arithmetically(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        sack_conversion: ItemPackageConversion,
        mapped: None,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        line = add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice,
                package_conversion=sack_conversion,
                entered_package_quantity=Decimal("4"),
                unit_cost=Decimal("1500"),
            ),
        )
        assert line.base_quantity == Decimal("100.000")
        posted = post_document(actor=manager, document=document)
        movement = StockMovement.objects.get()
        assert movement.base_quantity == Decimal("100.000")
        # The conversion is snapshotted, so a later version cannot restate it.
        assert movement.source_conversion == sack_conversion
        assert posted.lines.get().package_conversion == sack_conversion

    def test_a_variable_package_requires_the_measured_quantity(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        carton_conversion: ItemPackageConversion,
        mapped: None,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager,
                document=document,
                line=DocumentLineInput(
                    item=rice,
                    package_conversion=carton_conversion,
                    entered_package_quantity=Decimal("2"),
                    unit_cost=Decimal("1500"),
                ),
            )
        assert caught.value.code == "measured_quantity_required"

        # The scale is the truth: 2 cartons weighed 35.4, not the 36 the
        # planning factor would have implied.
        line = add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice,
                package_conversion=carton_conversion,
                entered_package_quantity=Decimal("2"),
                measured_base_quantity=Decimal("35.4"),
                unit_cost=Decimal("1500"),
            ),
        )
        assert line.base_quantity == Decimal("35.400")

    def test_a_conversion_not_effective_on_the_business_date_is_refused(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        sack: PackageUnit,
        accounting: None,
    ) -> None:
        future = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("25"),
            effective_from=datetime.date(TEST_YEAR, 6, 1),
        )
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager,
                document=document,
                line=DocumentLineInput(
                    item=rice,
                    package_conversion=future,
                    entered_package_quantity=Decimal("1"),
                    unit_cost=Decimal("1500"),
                ),
            )
        assert caught.value.code == "conversion_not_effective"


class TestReceiptPosting:
    def test_it_creates_movement_balance_layer_and_journal(
        self,
        receipt: InventoryMovementDocument,
        rice: InventoryItem,
        control_account: Account,
        grni_account: Account,
    ) -> None:
        assert receipt.status == InventoryDocumentStatus.POSTED
        assert receipt.document_number.startswith(f"RCV-{TEST_YEAR}-")

        movement = StockMovement.objects.get()
        assert movement.movement_type == MovementType.RECEIPT
        assert movement.base_quantity == Decimal("100.000")
        assert movement.inventory_value == Decimal("150000.000")
        assert movement.control_account == control_account

        balance = StockBalance.objects.get()
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("150000.000")
        assert balance.control_account == control_account

        assert ValuationLayer.objects.filter(item=rice).count() == 1
        assert ValuationAllocation.objects.count() == 0

        journal = receipt.journal_entry
        assert journal is not None
        lines = list(journal.lines.order_by("line_number"))
        assert [(line.account.code, line.debit, line.credit) for line in lines] == [
            (control_account.code, Decimal("150000.000"), Decimal("0.000")),
            (grni_account.code, Decimal("0.000"), Decimal("150000.000")),
        ]

    def test_source_identity_uses_the_immutable_public_id(
        self, receipt: InventoryMovementDocument
    ) -> None:
        entry = receipt.stock_entry
        assert entry is not None
        assert entry.source_document_type == "INVENTORY_RECEIPT"
        assert entry.source_document_id == str(receipt.public_id)
        assert entry.source_event == "POSTED"
        line = receipt.lines.get()
        assert line.movement is not None
        assert line.movement.effect_key == f"inventory_receipt-line:{line.line_uid}"

    def test_stored_line_movement_and_journal_carry_one_figure(
        self, receipt: InventoryMovementDocument
    ) -> None:
        line = receipt.lines.get()
        assert line.total_value == Decimal("150000.000")
        assert line.movement is not None
        assert line.movement.inventory_value == line.total_value
        assert line.journal_line is not None
        assert line.journal_line.debit == line.total_value

    def test_a_posted_receipt_is_immutable_at_the_database(
        self, receipt: InventoryMovementDocument
    ) -> None:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                InventoryMovementDocument.objects.filter(pk=receipt.pk).update(
                    evidence_reference="rewritten"
                )

    def test_posted_lines_are_frozen_at_the_database(
        self, receipt: InventoryMovementDocument
    ) -> None:
        line = receipt.lines.get()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                InventoryMovementDocumentLine.objects.filter(pk=line.pk).update(
                    base_quantity=Decimal("999")
                )

    def test_grouped_debits_when_items_resolve_to_different_accounts(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        control_account: Account,
        grni_account: Account,
        mapped: None,
    ) -> None:
        other_control = Account.objects.get(organization=organization, code="1-03-02-001")
        oil = create_item(
            organization=organization,
            code="OIL",
            name_ar="زيت",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        create_inventory_mapping(
            organization=organization,
            role=INVENTORY_CONTROL,
            account=other_control,
            item=oil,
            effective_from=JAN_1,
        )

        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "10", "1500"))
        add_document_line(actor=manager, document=document, line=_line(oil, "4", "2500"))
        posted = post_document(actor=manager, document=document)

        journal = posted.journal_entry
        assert journal is not None
        debits = {line.account.code: line.debit for line in journal.lines.all() if line.debit > 0}
        credits = [line for line in journal.lines.all() if line.credit > 0]
        assert debits == {
            control_account.code: Decimal("15000.000"),
            other_control.code: Decimal("10000.000"),
        }
        assert len(credits) == 1
        assert credits[0].account == grni_account
        assert credits[0].credit == Decimal("25000.000")

    def test_a_missing_grni_mapping_rolls_everything_back(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        control_account: Account,
    ) -> None:
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=INVENTORY_CONTROL),
            account=control_account,
            effective_from=JAN_1,
        )
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "10", "1500"))
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=document)
        assert caught.value.code == "account_role_unmapped"

        document.refresh_from_db()
        assert document.status == InventoryDocumentStatus.DRAFT
        assert document.document_number == ""
        assert StockMovement.objects.count() == 0
        assert StockBalance.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_a_missing_control_mapping_rolls_everything_back(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        grni_account: Account,
    ) -> None:
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=GOODS_RECEIVED_NOT_INVOICED),
            account=grni_account,
            effective_from=JAN_1,
        )
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "10", "1500"))
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=document)
        assert caught.value.code == "account_role_unmapped"
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_a_soft_closed_or_closed_period_is_refused(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        period = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, period_number=3
        )
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "10", "1500"))

        for state in (PeriodState.SOFT_CLOSED, PeriodState.CLOSED):
            period.state = state
            period.save(update_fields=["state"])
            with pytest.raises(ValidationError) as caught:
                post_document(actor=manager, document=document)
            assert caught.value.code in {"period_soft_closed", "period_closed", "period_not_open"}

        period.state = PeriodState.OPEN
        period.save(update_fields=["state"])
        assert post_document(actor=manager, document=document).status == (
            InventoryDocumentStatus.POSTED
        )


class TestControlAccountContinuity:
    """§D: a receipt into standing stock keeps the account the stock is in."""

    def test_a_second_receipt_preserves_the_standing_account(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        control_account: Account,
        receipt: InventoryMovementDocument,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "50", "1600"))
        post_document(actor=manager, document=document)

        assert StockBalance.objects.get().control_account == control_account
        assert {movement.control_account for movement in StockMovement.objects.all()} == {
            control_account
        }

    def test_a_conflicting_account_is_refused(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        receipt: InventoryMovementDocument,
    ) -> None:
        """
        The mapping is re-homed while stock stands. The reclassification guard
        refuses the mapping change itself; this proves the kernel refuses the
        posting too, so no path can blend two accounts into one position.
        """
        from apps.core.context import audit_context
        from apps.inventory.ledger import MovementInput, post_stock_entry

        other = Account.objects.get(organization=organization, code="1-03-02-001")
        with audit_context(actor=manager), pytest.raises(ValidationError) as caught:
            post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("5"),
                        unit_cost=Decimal("1000"),
                        effect_key="rogue:1",
                        control_account=other,
                    )
                ],
                idempotency_key="rogue",
                effective_at=WHEN,
            )
        assert caught.value.code == "inventory_account_reclassification_required"

    def test_emptying_the_position_releases_the_account(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        receipt: InventoryMovementDocument,
    ) -> None:
        document = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "100"))
        post_document(actor=manager, document=document)

        balance = StockBalance.objects.get()
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")
        assert balance.control_account is None


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


class TestIssue:
    def test_it_uses_the_current_average_and_needs_no_entered_cost(
        self,
        issued: InventoryMovementDocument,
        consumption_account: Account,
        control_account: Account,
        kitchen: CostCenter,
    ) -> None:
        line = issued.lines.get()
        assert line.unit_cost == Decimal("1500.000000")
        assert line.total_value == Decimal("60000.000")

        journal = issued.journal_entry
        assert journal is not None
        rows = {
            (row.account.code, row.debit, row.credit, row.cost_center)
            for row in journal.lines.all()
        }
        assert rows == {
            (consumption_account.code, Decimal("60000.000"), Decimal("0.000"), kitchen),
            (control_account.code, Decimal("0.000"), Decimal("60000.000"), None),
        }

    def test_a_user_supplied_unit_cost_is_refused(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        receipt: InventoryMovementDocument,
    ) -> None:
        document = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        with pytest.raises(ValidationError) as caught:
            add_document_line(actor=manager, document=document, line=_line(rice, "10", "999"))
        assert caught.value.code == "unit_cost_not_accepted"

    def test_insufficient_stock_is_rejected(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        receipt: InventoryMovementDocument,
    ) -> None:
        document = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "500"))
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=document)
        assert caught.value.code == "insufficient_stock"
        assert StockBalance.objects.get().quantity == Decimal("100.000")

    def test_full_depletion_leaves_zero_quantity_and_zero_value(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        receipt: InventoryMovementDocument,
    ) -> None:
        document = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "100"))
        posted = post_document(actor=manager, document=document)

        balance = StockBalance.objects.get()
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")
        assert balance.average_cost == Decimal("0.000000")
        assert posted.lines.get().total_value == Decimal("150000.000")

    def test_a_cost_center_is_required_when_the_account_demands_one(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        receipt: InventoryMovementDocument,
    ) -> None:
        """The seeded consumption accounts are COGS, which ADR-015 says needs
        a cost centre. Posting without one fails before any effect."""
        document = _document(manager, organization, branch, main_store, InventoryDocumentType.ISSUE)
        add_document_line(actor=manager, document=document, line=_line(rice, "10"))
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=document)
        assert caught.value.code == "cost_center_required"
        assert StockMovement.objects.count() == 1  # only the receipt

    def test_a_receipt_refuses_a_cost_center(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen: CostCenter,
        accounting: None,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            _document(
                manager,
                organization,
                branch,
                main_store,
                InventoryDocumentType.RECEIPT,
                cost_center=kitchen,
            )
        assert caught.value.code == "cost_center_not_applicable"

    def test_an_expired_lot_cannot_be_issued(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        kitchen: CostCenter,
        mapped: None,
    ) -> None:
        chicken = create_item(
            organization=organization,
            code="CHK",
            name_ar="دجاج",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
            tracks_lots=True,
            tracks_expiry=True,
        )
        lot = InventoryLot.objects.create(
            organization=organization,
            item=chicken,
            code="L-1",
            expiry_date=datetime.date(TEST_YEAR, 3, 1),
        )
        receipt_document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(
            actor=manager,
            document=receipt_document,
            line=DocumentLineInput(
                item=chicken, lot=lot, base_quantity=Decimal("10"), unit_cost=Decimal("4000")
            ),
        )
        post_document(actor=manager, document=receipt_document)

        issue = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(
            actor=manager,
            document=issue,
            line=DocumentLineInput(item=chicken, lot=lot, base_quantity=Decimal("5")),
        )
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=issue)
        assert caught.value.code == "lot_expired"

    def test_an_issue_is_not_a_transfer(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        receipt: InventoryMovementDocument,
    ) -> None:
        """
        An issue leaves custody for consumption; it names one warehouse and
        cannot name a destination. Moving stock between warehouses is a
        transfer, and there is no way to spell one here.
        """
        issue = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=issue, line=_line(rice, "10"))
        post_document(actor=manager, document=issue)

        # The kitchen store gained nothing: the goods were consumed, not moved.
        assert not StockBalance.objects.filter(warehouse=kitchen_store).exists()
        assert StockBalance.objects.get(warehouse=main_store).quantity == Decimal("90.000")


# ---------------------------------------------------------------------------
# Return-in
# ---------------------------------------------------------------------------


class TestReturnIn:
    def _return_document(
        self,
        actor: User,
        organization: Organization,
        branch: Branch,
        warehouse: Warehouse,
    ) -> InventoryMovementDocument:
        return _document(actor, organization, branch, warehouse, InventoryDocumentType.RETURN_IN)

    def test_a_partial_return_uses_the_original_issue_cost(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        issued: InventoryMovementDocument,
        control_account: Account,
        consumption_account: Account,
        kitchen: CostCenter,
    ) -> None:
        source = issued.lines.get()
        document = self._return_document(manager, organization, branch, main_store)
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("10"), source_issue_line=source
            ),
        )
        posted = post_document(actor=manager, document=document)

        line = posted.lines.get()
        assert line.unit_cost == Decimal("1500.000000")
        assert line.total_value == Decimal("15000.000")

        journal = posted.journal_entry
        assert journal is not None
        rows = {
            (row.account.code, row.debit, row.credit, row.cost_center)
            for row in journal.lines.all()
        }
        # The mirror of the returned part, in the original's accounts and the
        # original's cost centre.
        assert rows == {
            (control_account.code, Decimal("15000.000"), Decimal("0.000"), None),
            (consumption_account.code, Decimal("0.000"), Decimal("15000.000"), kitchen),
        }
        assert StockBalance.objects.get().quantity == Decimal("70.000")

    def test_the_final_return_takes_the_exact_remaining_value(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        kitchen: CostCenter,
        mapped: None,
    ) -> None:
        """
        An issue whose unit cost does not divide evenly. Cumulative returns
        must still equal the issue to the dinar, with no residual left behind.
        """
        spice = create_item(
            organization=organization,
            code="SPICE",
            name_ar="بهارات",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        receipt_document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(
            actor=manager, document=receipt_document, line=_line(spice, "3", "1000.0003")
        )
        post_document(actor=manager, document=receipt_document)

        issue = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=issue, line=_line(spice, "3"))
        posted_issue = post_document(actor=manager, document=issue)
        source = posted_issue.lines.get()
        issued_value = source.total_value
        assert issued_value is not None

        first = self._return_document(manager, organization, branch, main_store)
        add_document_line(
            actor=manager,
            document=first,
            line=DocumentLineInput(
                item=spice, base_quantity=Decimal("1"), source_issue_line=source
            ),
        )
        posted_first = post_document(actor=manager, document=first)

        second = self._return_document(manager, organization, branch, main_store)
        add_document_line(
            actor=manager,
            document=second,
            line=DocumentLineInput(
                item=spice, base_quantity=Decimal("2"), source_issue_line=source
            ),
        )
        posted_second = post_document(actor=manager, document=second)

        returned = (posted_first.lines.get().total_value or Decimal("0")) + (
            posted_second.lines.get().total_value or Decimal("0")
        )
        assert returned == issued_value
        remaining_quantity, remaining_value = returnable(source)
        assert remaining_quantity == Decimal("0.000")
        assert remaining_value == Decimal("0.000")

    def test_cumulative_returns_cannot_exceed_the_issue(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        issued: InventoryMovementDocument,
    ) -> None:
        source = issued.lines.get()
        document = self._return_document(manager, organization, branch, main_store)
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager,
                document=document,
                line=DocumentLineInput(
                    item=rice, base_quantity=Decimal("41"), source_issue_line=source
                ),
            )
        assert caught.value.code == "return_exceeds_issue"

    def test_a_wrong_item_warehouse_or_lot_is_rejected(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        issued: InventoryMovementDocument,
    ) -> None:
        source = issued.lines.get()
        other_item = create_item(
            organization=organization,
            code="OTHER",
            name_ar="آخر",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )

        document = self._return_document(manager, organization, branch, main_store)
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager,
                document=document,
                line=DocumentLineInput(
                    item=other_item, base_quantity=Decimal("1"), source_issue_line=source
                ),
            )
        assert caught.value.code == "return_item_mismatch"

        elsewhere = self._return_document(manager, organization, branch, kitchen_store)
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager,
                document=elsewhere,
                line=DocumentLineInput(
                    item=rice, base_quantity=Decimal("1"), source_issue_line=source
                ),
            )
        assert caught.value.code == "return_warehouse_mismatch"

    def test_a_return_needs_a_posted_issue(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        receipt: InventoryMovementDocument,
    ) -> None:
        document = self._return_document(manager, organization, branch, main_store)
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager,
                document=document,
                line=DocumentLineInput(item=rice, base_quantity=Decimal("1")),
            )
        assert caught.value.code == "source_issue_required"

        # ...and a receipt line is not an issue line.
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager,
                document=document,
                line=DocumentLineInput(
                    item=rice,
                    base_quantity=Decimal("1"),
                    source_issue_line=receipt.lines.get(),
                ),
            )
        assert caught.value.code == "source_is_not_an_issue"

    def test_todays_mapping_is_not_used_for_the_return(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        issued: InventoryMovementDocument,
        consumption_account: Account,
        packaging_account: Account,
    ) -> None:
        """
        The consumption mapping is re-pointed after the issue. The return must
        still credit where the expense actually landed, or the pair never nets
        to zero in the account that carries it.
        """
        create_inventory_mapping(
            organization=organization,
            role=INVENTORY_CONSUMPTION,
            account=packaging_account,
            item=rice,
            effective_from=JAN_1,
        )

        source = issued.lines.get()
        document = self._return_document(manager, organization, branch, main_store)
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("10"), source_issue_line=source
            ),
        )
        posted = post_document(actor=manager, document=document)

        assert posted.journal_entry is not None
        credits = [row for row in posted.journal_entry.lines.all() if row.credit > 0]
        assert len(credits) == 1
        assert credits[0].account == consumption_account
        assert posted.lines.get().contra_account == consumption_account

    def test_returning_an_expired_lot_is_allowed_but_it_stays_expired(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        kitchen: CostCenter,
        mapped: None,
    ) -> None:
        """
        Goods that expired in the kitchen still physically come back, so the
        return is accepted. What must not happen is the return laundering
        them: issuing them again is still refused.
        """
        chicken = create_item(
            organization=organization,
            code="CHK2",
            name_ar="دجاج ٢",
            category=leaf_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
            tracks_lots=True,
            tracks_expiry=True,
        )
        lot = InventoryLot.objects.create(
            organization=organization,
            item=chicken,
            code="L-2",
            expiry_date=datetime.date(TEST_YEAR, 4, 1),
        )
        receipt_document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(
            actor=manager,
            document=receipt_document,
            line=DocumentLineInput(
                item=chicken, lot=lot, base_quantity=Decimal("10"), unit_cost=Decimal("4000")
            ),
        )
        post_document(actor=manager, document=receipt_document)

        issue = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(
            actor=manager,
            document=issue,
            line=DocumentLineInput(item=chicken, lot=lot, base_quantity=Decimal("6")),
        )
        posted_issue = post_document(actor=manager, document=issue)

        # The lot expires while it is out of the store.
        lot.expiry_date = datetime.date(TEST_YEAR, 3, 10)
        lot.save(update_fields=["expiry_date"])

        document = self._return_document(manager, organization, branch, main_store)
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=chicken,
                lot=lot,
                base_quantity=Decimal("6"),
                source_issue_line=posted_issue.lines.get(),
            ),
        )
        returned = post_document(actor=manager, document=document)
        assert returned.status == InventoryDocumentStatus.POSTED

        # ...and it is still expired for any normal issue.
        again = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(
            actor=manager,
            document=again,
            line=DocumentLineInput(item=chicken, lot=lot, base_quantity=Decimal("1")),
        )
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=again)
        assert caught.value.code == "lot_expired"


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


class TestReversal:
    def test_a_receipt_reversal_restores_stock_and_gl(
        self, manager: User, receipt: InventoryMovementDocument, rice: InventoryItem
    ) -> None:
        reversed_document = reverse_document(
            actor=manager, document=receipt, reason="wrong delivery note"
        )
        assert reversed_document.status == InventoryDocumentStatus.REVERSED

        balance = StockBalance.objects.get()
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")

        assert receipt.journal_entry_id is not None
        original = JournalEntry.objects.get(pk=receipt.journal_entry_id)
        assert original.status == JournalEntryStatus.REVERSED
        mirror = reversed_document.reversal_journal_entry
        assert mirror is not None
        assert sum(row.debit for row in mirror.lines.all()) == Decimal("150000.000")

    def test_a_receipt_reversal_respects_availability(
        self, manager: User, receipt: InventoryMovementDocument, issued: InventoryMovementDocument
    ) -> None:
        """40 of the 100 are gone; the receipt of 100 cannot be un-received."""
        with pytest.raises(ValidationError) as caught:
            reverse_document(actor=manager, document=receipt, reason="undo")
        assert caught.value.code == "insufficient_stock"

        receipt.refresh_from_db()
        assert receipt.status == InventoryDocumentStatus.POSTED

    def test_an_issue_reversal_adds_the_original_quantity_and_value(
        self, manager: User, issued: InventoryMovementDocument
    ) -> None:
        reverse_document(actor=manager, document=issued, reason="issued in error")
        balance = StockBalance.objects.get()
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("150000.000")

    def test_an_issue_with_active_returns_cannot_be_reversed(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        issued: InventoryMovementDocument,
    ) -> None:
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RETURN_IN
        )
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("10"), source_issue_line=issued.lines.get()
            ),
        )
        returned = post_document(actor=manager, document=document)

        with pytest.raises(ValidationError) as caught:
            reverse_document(actor=manager, document=issued, reason="undo")
        assert caught.value.code == "issue_has_active_returns"

        # Reverse the return first, and the issue becomes reversible.
        reverse_document(actor=manager, document=returned, reason="return was wrong")
        reverse_document(actor=manager, document=issued, reason="undo")
        issued.refresh_from_db()
        assert issued.status == InventoryDocumentStatus.REVERSED

    def test_a_return_reversal_restores_the_returnable_amount(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        issued: InventoryMovementDocument,
    ) -> None:
        source = issued.lines.get()
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RETURN_IN
        )
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("10"), source_issue_line=source
            ),
        )
        returned = post_document(actor=manager, document=document)
        assert returnable(source)[0] == Decimal("30.000")

        reverse_document(actor=manager, document=returned, reason="mistaken return")
        assert returnable(source)[0] == Decimal("40.000")

    def test_already_reversed_and_reason_required(
        self, manager: User, issued: InventoryMovementDocument
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            reverse_document(actor=manager, document=issued, reason="   ")
        assert caught.value.code == "reason_required"

        reverse_document(actor=manager, document=issued, reason="first")
        with pytest.raises(ValidationError) as caught:
            reverse_document(actor=manager, document=issued, reason="second")
        assert caught.value.code == "already_reversed"

    def test_a_return_is_not_a_reversal(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        issued: InventoryMovementDocument,
    ) -> None:
        """
        Both put stock back, and they mean different things. A return is a new
        posted document with its own number and its own movement; the issue
        stays POSTED and keeps its history.
        """
        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RETURN_IN
        )
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("10"), source_issue_line=issued.lines.get()
            ),
        )
        returned = post_document(actor=manager, document=document)

        issued.refresh_from_db()
        assert issued.status == InventoryDocumentStatus.POSTED
        assert returned.document_number.startswith("RTN-")
        return_movement = returned.lines.get().movement
        assert return_movement is not None
        assert return_movement.movement_type == MovementType.RETURN_IN
        # ...and the ledger holds three separate movements, not two with one
        # cancelled out.
        assert StockMovement.objects.count() == 3


# ---------------------------------------------------------------------------
# Numbering, idempotency, authorization
# ---------------------------------------------------------------------------


class TestNumberingAndIdempotency:
    def test_numbers_are_gapless_per_type_and_year(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        mapped: None,
    ) -> None:
        first = _document(manager, organization, branch, main_store, InventoryDocumentType.RECEIPT)
        add_document_line(actor=manager, document=first, line=_line(rice, "10", "1000"))
        assert post_document(actor=manager, document=first).document_number == (
            f"RCV-{TEST_YEAR}-000001"
        )

        second = _document(manager, organization, branch, main_store, InventoryDocumentType.RECEIPT)
        add_document_line(actor=manager, document=second, line=_line(rice, "5", "1000"))
        assert post_document(actor=manager, document=second).document_number == (
            f"RCV-{TEST_YEAR}-000002"
        )

        issue = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=issue, line=_line(rice, "1"))
        # A separate series per type.
        assert post_document(actor=manager, document=issue).document_number == (
            f"ISS-{TEST_YEAR}-000001"
        )

    def test_a_failed_posting_burns_no_number(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        receipt: InventoryMovementDocument,
    ) -> None:
        doomed = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=doomed, line=_line(rice, "5000"))
        with pytest.raises(ValidationError):
            post_document(actor=manager, document=doomed)

        good = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=good, line=_line(rice, "5"))
        assert post_document(actor=manager, document=good).document_number == (
            f"ISS-{TEST_YEAR}-000001"
        )

    def test_posting_twice_is_refused(
        self, manager: User, receipt: InventoryMovementDocument
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=receipt)
        assert caught.value.code == "already_posted"

    def test_a_retry_of_the_same_economic_event_returns_the_original(
        self,
        manager: User,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        receipt: InventoryMovementDocument,
    ) -> None:
        """
        The kernel's idempotency, exercised on the exact key the receipt used.
        """
        from apps.core.context import audit_context
        from apps.inventory.ledger import MovementInput, post_stock_entry

        line = receipt.lines.get()
        original = receipt.stock_entry
        assert original is not None
        with audit_context(actor=manager):
            replayed = post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("100"),
                        unit_cost=Decimal("1500.000000"),
                        effect_key=f"inventory_receipt-line:{line.line_uid}",
                        control_account=line.inventory_account,
                    )
                ],
                idempotency_key=f"inventory_receipt:{receipt.public_id}",
                effective_at=receipt.effective_at,
                business_date=receipt.business_date,
                source_document_type="INVENTORY_RECEIPT",
                source_document_id=str(receipt.public_id),
                source_event="POSTED",
                reference=receipt.evidence_reference,
                reason=receipt.narration or str(receipt.get_document_type_display()),
            )
        assert replayed.pk == original.pk
        assert StockMovement.objects.count() == 1

    def test_a_changed_retry_payload_conflicts(
        self,
        manager: User,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        receipt: InventoryMovementDocument,
    ) -> None:
        from apps.core.context import audit_context
        from apps.inventory.ledger import MovementInput, post_stock_entry

        with audit_context(actor=manager), pytest.raises(ValidationError) as caught:
            post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("999"),
                        unit_cost=Decimal("1500"),
                        effect_key="changed",
                    )
                ],
                idempotency_key=f"inventory_receipt:{receipt.public_id}",
                effective_at=receipt.effective_at,
                business_date=receipt.business_date,
            )
        assert caught.value.code == "idempotency_key_conflict"


class TestAuthorization:
    def test_a_storekeeper_may_receive_issue_and_return(
        self,
        keeper: User,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: Any,
        mapped: None,
    ) -> None:
        receipt_document = _document(
            keeper, organization, branch, main_store, InventoryDocumentType.RECEIPT
        )
        add_document_line(actor=keeper, document=receipt_document, line=_line(rice, "20", "1000"))
        post_document(actor=keeper, document=receipt_document)

        issue = _document(
            keeper,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=keeper, document=issue, line=_line(rice, "5"))
        posted_issue = post_document(actor=keeper, document=issue)

        document = _document(
            keeper, organization, branch, main_store, InventoryDocumentType.RETURN_IN
        )
        add_document_line(
            actor=keeper,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("2"), source_issue_line=posted_issue.lines.get()
            ),
        )
        assert post_document(actor=keeper, document=document).status == (
            InventoryDocumentStatus.POSTED
        )

    def test_a_storekeeper_cannot_reverse(
        self, keeper: User, receipt: InventoryMovementDocument
    ) -> None:
        with pytest.raises(PermissionDenied):
            reverse_document(actor=keeper, document=receipt, reason="no")

    def test_a_rival_cannot_reach_the_document(
        self, rival_manager: User, receipt: InventoryMovementDocument
    ) -> None:
        from apps.inventory.commands import resolve_document
        from apps.organizations.authorization import OutOfScope

        with pytest.raises(OutOfScope):
            resolve_document(rival_manager, receipt.pk)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_a_receipt_issue_and_return_all_reconcile(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        issued: InventoryMovementDocument,
    ) -> None:
        """
        Every document equals its own effects, the projection equals the
        ledger replay, and the inventory book value equals the GL control
        account — after a receipt, an issue, and a return.
        """
        from apps.inventory.reconciliation import (
            verify_inventory_accounting,
            verify_inventory_against_gl,
            verify_operational_document,
            verify_organization,
        )

        document = _document(
            manager, organization, branch, main_store, InventoryDocumentType.RETURN_IN
        )
        add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice, base_quantity=Decimal("15"), source_issue_line=issued.lines.get()
            ),
        )
        returned = post_document(actor=manager, document=document)

        for posted in (returned, issued):
            assert verify_operational_document(posted) == []
        assert verify_organization(organization) == []
        assert verify_inventory_against_gl(organization) == []
        assert verify_inventory_accounting(organization) == []

    def test_a_manual_journal_against_the_control_account_is_reported_not_repaired(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        control_account: Account,
        grni_account: Account,
        superuser: User,
        receipt: InventoryMovementDocument,
    ) -> None:
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine
        from apps.core.context import audit_context
        from apps.inventory.reconciliation import verify_inventory_against_gl

        with audit_context(actor=superuser):
            post_entry(
                organization=organization,
                accounting_date=receipt.business_date,
                lines=[
                    PostingLine(account=control_account, branch=branch, debit=Decimal("999.000")),
                    PostingLine(account=grni_account, branch=branch, credit=Decimal("999.000")),
                ],
                idempotency_key="manual-drift",
            )
        problems = verify_inventory_against_gl(organization)
        assert len(problems) == 1
        assert problems[0].field == "inventory_vs_gl"
        # Reported, and left exactly where it was: reading the report changes
        # nothing, so the evidence survives for whoever investigates.
        assert verify_inventory_against_gl(organization) == problems

    def test_the_command_reports_and_exits_nonzero(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        control_account: Account,
        grni_account: Account,
        superuser: User,
        receipt: InventoryMovementDocument,
    ) -> None:
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine
        from apps.core.context import audit_context

        call_command("verify_inventory_accounting", organization=organization.code)

        with audit_context(actor=superuser):
            post_entry(
                organization=organization,
                accounting_date=receipt.business_date,
                lines=[
                    PostingLine(account=control_account, branch=branch, debit=Decimal("1.000")),
                    PostingLine(account=grni_account, branch=branch, credit=Decimal("1.000")),
                ],
                idempotency_key="drift-for-command",
            )
        with pytest.raises(SystemExit):
            call_command("verify_inventory_accounting", organization=organization.code)
