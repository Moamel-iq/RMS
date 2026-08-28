"""
Consumption issues (Task 1.4 §S 9–49).

Every document is built and posted through the command layer with a real
actor, so each test also exercises the authorization protecting the act it
tests.

The un-invoiced receipt and the return-from-issue that this file also covered
were withdrawn from the product; what they tested went with them. The stock an
issue needs is put on the shelf through the kernel instead — the same call
procurement's goods receipt makes.
"""

from __future__ import annotations

import datetime
import zoneinfo
from dataclasses import fields
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
    AccountRole,
    CostCenter,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.commands import (
    add_document_line,
    create_document,
    post_document,
    post_stock_movements,
    reverse_document,
)
from apps.inventory.ledger import MovementInput
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
    StockLedgerEntry,
    StockMovement,
    Warehouse,
)
from apps.inventory.operations import DocumentLineInput
from apps.inventory.services import create_item, create_item_conversion
from apps.inventory.tests.stock_seed import seed_stock
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
    """
    One requested line.

    `cost` survives as a parameter although no surviving document accepts an
    entered cost: two tests exist precisely to prove that entering one is
    refused, and they need a way to try. It is smuggled past the dataclass
    with `setattr`, which is what a caller sending the field over the API
    effectively does.
    """
    line = DocumentLineInput(item=item, base_quantity=Decimal(quantity), **extra)  # type: ignore[arg-type]
    if cost is not None:
        object.__setattr__(line, "unit_cost", Decimal(cost))
    return line


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
def stocked(
    manager: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    control_account: Account,
    rice: InventoryItem,
    mapped: None,
) -> None:
    """100 kg of rice at 1500 on the shelf, so 150,000 to issue against."""
    seed_stock(
        actor=manager,
        organization=organization,
        warehouse=main_store,
        item=rice,
        quantity="100.000",
        unit_cost="1500.000000",
        control_account=control_account,
        effective_at=WHEN - datetime.timedelta(hours=1),
    )


@pytest.fixture
def issued(
    manager: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    rice: InventoryItem,
    kitchen: CostCenter,
    stocked: None,
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


class TestControlAccountContinuity:
    """§D: a receipt into standing stock keeps the account the stock is in."""

    def test_a_conflicting_account_is_refused(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
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
        stocked: None,
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


class TestLineConversions:
    """
    How a package count becomes a base quantity.

    A rule of `_derive_base_quantity`, never of one document type. It was
    demonstrated on the un-invoiced receipt because that was the document
    operators keyed packages into; the issue keys them the same way.
    """

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
        kitchen: CostCenter,
        stocked: None,
    ) -> None:
        document = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        line = add_document_line(
            actor=manager,
            document=document,
            line=DocumentLineInput(
                item=rice,
                package_conversion=sack_conversion,
                entered_package_quantity=Decimal("4"),
            ),
        )
        assert line.base_quantity == Decimal("100.000")
        posted = post_document(actor=manager, document=document)
        movement = StockMovement.objects.get(movement_type=MovementType.ISSUE)
        assert movement.base_quantity == Decimal("-100.000")
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
        kitchen: CostCenter,
        stocked: None,
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
            add_document_line(
                actor=manager,
                document=document,
                line=DocumentLineInput(
                    item=rice,
                    package_conversion=carton_conversion,
                    entered_package_quantity=Decimal("2"),
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
        kitchen: CostCenter,
        stocked: None,
    ) -> None:
        future = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("25"),
            effective_from=datetime.date(TEST_YEAR, 6, 1),
        )
        document = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        with pytest.raises(ValidationError) as caught:
            add_document_line(
                actor=manager,
                document=document,
                line=DocumentLineInput(
                    item=rice,
                    package_conversion=future,
                    entered_package_quantity=Decimal("1"),
                ),
            )
        assert caught.value.code == "conversion_not_effective"


class TestIssue:
    def test_a_duplicate_valuation_key_is_rejected(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        stocked: None,
    ) -> None:
        """One item and lot may hold one line: two would be one position twice."""
        document = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "10"))
        with pytest.raises(ValidationError) as caught:
            add_document_line(actor=manager, document=document, line=_line(rice, "5"))
        assert caught.value.code == "duplicate_valuation_key"

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

    def test_a_line_cannot_carry_an_entered_cost(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        stocked: None,
    ) -> None:
        """
        Nothing left in this family prices itself.

        The service used to refuse an entered cost at `add_line`. It no longer
        needs to: `DocumentLineInput` has no `unit_cost` field to carry one,
        which is a stronger guarantee than a check, and the database refuses a
        cost smuggled past the dataclass anyway.
        """
        assert "unit_cost" not in {field.name for field in fields(DocumentLineInput)}

        document = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=document, line=_line(rice, "10"))
        line = document.lines.get()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                InventoryMovementDocumentLine.objects.filter(pk=line.pk).delete()
                InventoryMovementDocumentLine.objects.create(
                    document=document,
                    sequence=2,
                    item=rice,
                    base_quantity=Decimal("1.000"),
                    unit_cost=Decimal("999.000000"),
                )

    def test_insufficient_stock_is_rejected(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        stocked: None,
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
        stocked: None,
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
        stocked: None,
    ) -> None:
        """The seeded consumption accounts are COGS, which ADR-015 says needs
        a cost centre. Posting without one fails before any effect."""
        document = _document(manager, organization, branch, main_store, InventoryDocumentType.ISSUE)
        add_document_line(actor=manager, document=document, line=_line(rice, "10"))
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=document)
        assert caught.value.code == "cost_center_required"
        assert StockMovement.objects.count() == 1  # only the receipt

    def test_an_expired_lot_cannot_be_issued(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
        kitchen: CostCenter,
        control_account: Account,
        mapped: None,
    ) -> None:
        chicken = create_item(
            organization=organization,
            code="CHK",
            name="دجاج",
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
        post_stock_movements(
            actor=manager,
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=main_store,
                    item=chicken,
                    movement_type=MovementType.RECEIPT,
                    quantity=Decimal("10.000"),
                    effect_key="seed:chicken",
                    lot=lot,
                    unit_cost=Decimal("4000.000000"),
                    control_account=control_account,
                )
            ],
            idempotency_key="test-seed-chicken",
            effective_at=WHEN - datetime.timedelta(hours=1),
        )

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
        stocked: None,
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


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


class TestReversal:
    def test_an_issue_reversal_adds_the_original_quantity_and_value(
        self, manager: User, issued: InventoryMovementDocument
    ) -> None:
        reverse_document(actor=manager, document=issued, reason="issued in error")
        balance = StockBalance.objects.get()
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("150000.000")

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
        stocked: None,
    ) -> None:
        first = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=first, line=_line(rice, "10"))
        assert post_document(actor=manager, document=first).document_number == (
            f"ISS-{TEST_YEAR}-000001"
        )

        second = _document(
            manager,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=manager, document=second, line=_line(rice, "5"))
        assert post_document(actor=manager, document=second).document_number == (
            f"ISS-{TEST_YEAR}-000002"
        )

    def test_a_failed_posting_burns_no_number(
        self,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        kitchen: CostCenter,
        stocked: None,
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
        self, manager: User, issued: InventoryMovementDocument
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            post_document(actor=manager, document=issued)
        assert caught.value.code == "already_posted"

    def test_a_retry_of_the_same_economic_event_returns_the_original(
        self,
        manager: User,
        organization: Organization,
        main_store: Warehouse,
        control_account: Account,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """The same key with the same payload is a retry, not a second event."""
        from apps.core.context import audit_context
        from apps.inventory.ledger import MovementInput, post_stock_entry

        def post() -> Any:
            return post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("100.000"),
                        unit_cost=Decimal("1500.000000"),
                        effect_key="retry-line",
                        control_account=control_account,
                    )
                ],
                idempotency_key="retry",
                effective_at=WHEN,
                business_date=WHEN.date(),
            )

        with audit_context(actor=manager):
            original = post()
            replayed = post()
        assert replayed.pk == original.pk
        assert StockMovement.objects.count() == 1

    def test_a_changed_retry_payload_conflicts(
        self,
        manager: User,
        organization: Organization,
        main_store: Warehouse,
        control_account: Account,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        from apps.core.context import audit_context
        from apps.inventory.ledger import MovementInput, post_stock_entry

        original = StockLedgerEntry.objects.order_by("id").first()
        assert original is not None
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
                idempotency_key=original.idempotency_key,
                effective_at=original.effective_at,
                business_date=original.business_date,
            )
        assert caught.value.code == "idempotency_key_conflict"


class TestPostedDocumentsAreFrozen:
    """
    Four contracts every posted operational document owes.

    They were written against the un-invoiced receipt because it was the first
    document this module had; none of them was ever about a receipt. The issue
    carries them now.
    """

    def test_source_identity_uses_the_immutable_public_id(
        self, issued: InventoryMovementDocument
    ) -> None:
        entry = issued.stock_entry
        assert entry is not None
        assert entry.source_document_type == "INVENTORY_ISSUE"
        assert entry.source_document_id == str(issued.public_id)
        assert entry.source_event == "POSTED"
        line = issued.lines.get()
        assert line.movement is not None
        assert line.movement.effect_key == f"inventory_issue-line:{line.line_uid}"

    def test_stored_line_movement_and_journal_carry_one_figure(
        self, issued: InventoryMovementDocument
    ) -> None:
        line = issued.lines.get()
        assert line.total_value == Decimal("60000.000")
        assert line.movement is not None
        assert line.movement.inventory_value == -line.total_value
        assert line.journal_line is not None
        assert line.journal_line.credit == line.total_value

    def test_a_posted_document_is_immutable_at_the_database(
        self, issued: InventoryMovementDocument
    ) -> None:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                InventoryMovementDocument.objects.filter(pk=issued.pk).update(
                    evidence_reference="rewritten"
                )

    def test_posted_lines_are_frozen_at_the_database(
        self, issued: InventoryMovementDocument
    ) -> None:
        line = issued.lines.get()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                InventoryMovementDocumentLine.objects.filter(pk=line.pk).update(
                    base_quantity=Decimal("1")
                )


class TestAuthorization:
    def test_a_storekeeper_may_issue(
        self,
        keeper: User,
        manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        control_account: Account,
        rice: InventoryItem,
        kitchen: Any,
        mapped: None,
    ) -> None:
        post_stock_movements(
            actor=keeper,
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=main_store,
                    item=rice,
                    movement_type=MovementType.RECEIPT,
                    quantity=Decimal("20.000"),
                    effect_key="seed:rice-keeper",
                    unit_cost=Decimal("1000.000000"),
                    control_account=control_account,
                )
            ],
            idempotency_key="test-seed-rice-keeper",
            effective_at=WHEN - datetime.timedelta(hours=1),
        )

        issue = _document(
            keeper,
            organization,
            branch,
            main_store,
            InventoryDocumentType.ISSUE,
            cost_center=kitchen,
        )
        add_document_line(actor=keeper, document=issue, line=_line(rice, "5"))
        assert post_document(actor=keeper, document=issue).status == (
            InventoryDocumentStatus.POSTED
        )

    def test_a_storekeeper_cannot_reverse(
        self, keeper: User, issued: InventoryMovementDocument
    ) -> None:
        with pytest.raises(PermissionDenied):
            reverse_document(actor=keeper, document=issued, reason="no")

    def test_a_rival_cannot_reach_the_document(
        self, rival_manager: User, issued: InventoryMovementDocument
    ) -> None:
        from apps.inventory.commands import resolve_document
        from apps.organizations.authorization import OutOfScope

        with pytest.raises(OutOfScope):
            resolve_document(rival_manager, issued.pk)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_stock_in_and_an_issue_reconcile(
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
        account — after stock came in and an issue took some out.
        """
        from apps.inventory.reconciliation import (
            verify_inventory_accounting,
            verify_inventory_against_gl,
            verify_operational_document,
            verify_organization,
        )

        assert verify_operational_document(issued) == []
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
        issued: InventoryMovementDocument,
    ) -> None:
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine
        from apps.core.context import audit_context
        from apps.inventory.reconciliation import verify_inventory_against_gl

        with audit_context(actor=superuser):
            post_entry(
                organization=organization,
                accounting_date=issued.business_date,
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
        issued: InventoryMovementDocument,
    ) -> None:
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine
        from apps.core.context import audit_context

        call_command("verify_inventory_accounting", organization=organization.code)

        with audit_context(actor=superuser):
            post_entry(
                organization=organization,
                accounting_date=issued.business_date,
                lines=[
                    PostingLine(account=control_account, branch=branch, debit=Decimal("1.000")),
                    PostingLine(account=grni_account, branch=branch, credit=Decimal("1.000")),
                ],
                idempotency_key="drift-for-command",
            )
        with pytest.raises(SystemExit):
            call_command("verify_inventory_accounting", organization=organization.code)
