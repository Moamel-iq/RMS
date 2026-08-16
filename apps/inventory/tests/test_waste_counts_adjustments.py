"""
Waste, physical counts and manual adjustments (Task 1.6 §AF).

Every act goes through the command layer with a real actor, so each test also
exercises the authorization protecting the act it tests.
"""

from __future__ import annotations

import datetime
import zoneinfo
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from apps.accounting.models import (
    INVENTORY_ADJUSTMENT,
    INVENTORY_CONTROL,
    INVENTORY_COUNT_VARIANCE,
    INVENTORY_WASTE_EXPENSE,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
    PeriodState,
)
from apps.accounting.services import (
    archive_account_mapping,
    close_period,
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
    resolve_period,
    soft_close_period,
)
from apps.inventory.adjustments import AdjustmentLineInput
from apps.inventory.commands import (
    add_adjustment_line,
    add_document_line,
    add_unexpected_count_line,
    approve_stock_count,
    blind_count_sheet,
    cancel_stock_count,
    create_adjustment,
    create_document,
    create_reason_code,
    create_stock_count,
    post_adjustment,
    post_document,
    record_stock_counts,
    resolve_count,
    resolve_count_line,
    reverse_adjustment,
    reverse_document,
    reverse_stock_count,
    start_stock_count,
    submit_stock_count,
    update_reason_code,
    visible_counts,
    visible_reason_codes,
)
from apps.inventory.counts import ApprovedCost, CountEntry
from apps.inventory.models import (
    ConversionType,
    InventoryAdjustmentDocument,
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    InventoryMovementDocument,
    InventoryReasonCode,
    ItemCategory,
    MovementType,
    PackageUnit,
    ReasonCodeApplication,
    StockBalance,
    StockCount,
    StockCountLine,
    StockCountStatus,
    StockMovement,
    Warehouse,
)
from apps.inventory.operations import DocumentLineInput
from apps.inventory.services import create_item_conversion
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")


def _moment(day: int = 15, hour: int = 10) -> datetime.datetime:
    return datetime.datetime(TEST_YEAR, 3, day, hour, 0, tzinfo=BAGHDAD)


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
def waste_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="6-02-01-002")


@pytest.fixture
def count_variance_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="7-09-02-001")


@pytest.fixture
def adjustment_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="7-09-03-001")


@pytest.fixture
def grni_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="2-01-02-001")


@pytest.fixture
def kitchen(organization: Organization, accounting: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="KITCHEN")


@pytest.fixture
def mapped(
    organization: Organization,
    control_account: Account,
    waste_account: Account,
    count_variance_account: Account,
    adjustment_account: Account,
    grni_account: Account,
) -> None:
    from apps.accounting.models import GOODS_RECEIVED_NOT_INVOICED, INVENTORY_CONSUMPTION

    consumption = Account.objects.get(organization=organization, code="5-01-02-001")
    for code, account in (
        (INVENTORY_CONTROL, control_account),
        (INVENTORY_WASTE_EXPENSE, waste_account),
        (INVENTORY_COUNT_VARIANCE, count_variance_account),
        (INVENTORY_ADJUSTMENT, adjustment_account),
        (GOODS_RECEIVED_NOT_INVOICED, grni_account),
        (INVENTORY_CONSUMPTION, consumption),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=account,
            effective_from=datetime.date(TEST_YEAR, 1, 1),
        )


@pytest.fixture
def boss(organization: Organization, branch: Branch) -> User:
    """Organization authority: conducts, approves, posts, reverses."""
    user = User.objects.create_user(username="boss", password="pw-not-real-1234")
    grant_organization_access(user=user, organization=organization, role=Role.OWNER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def checker(organization: Organization) -> User:
    """An accounting manager: approves counts, never conducts one."""
    user = User.objects.create_user(username="checker", password="pw-not-real-1234")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def keeper(branch: Branch) -> User:
    """A storekeeper: counts, never approves, never wastes or adjusts."""
    user = User.objects.create_user(username="keeper", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def spoilage(organization: Organization) -> InventoryReasonCode:
    return create_reason_code(
        actor=User.objects.get(username="boss")
        if User.objects.filter(username="boss").exists()
        else User.objects.create_superuser(username="seed", password="pw-not-real-1234"),
        organization=organization,
        code="spoil",
        name_ar="تلف طبيعي",
        applies_to=ReasonCodeApplication.WASTE,
    )


@pytest.fixture
def correction(boss: User, organization: Organization) -> InventoryReasonCode:
    return create_reason_code(
        actor=boss,
        organization=organization,
        code="CORRECT",
        name_ar="تصحيح قيد",
        applies_to=ReasonCodeApplication.MANUAL_ADJUSTMENT,
    )


def _seed_stock(
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    cost: str,
    *,
    lot: InventoryLot | None = None,
    at: datetime.datetime | None = None,
) -> None:
    """Put stock on a shelf through a real posted receipt."""
    document = create_document(
        actor=actor,
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=InventoryDocumentType.RECEIPT,
        effective_at=at or _moment(1),
        evidence_reference="DN-SEED",
    )
    add_document_line(
        actor=actor,
        document=document,
        line=DocumentLineInput(
            item=item, lot=lot, base_quantity=Decimal(quantity), unit_cost=Decimal(cost)
        ),
    )
    post_document(actor=actor, document=document)


@pytest.fixture
def stocked(
    boss: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    rice: InventoryItem,
    mapped: None,
) -> None:
    """100 kg of rice at 1500 — 150,000 on the shelf."""
    _seed_stock(boss, organization, branch, main_store, rice, "100", "1500")


def _balance(
    warehouse: Warehouse, item: InventoryItem, lot: InventoryLot | None = None
) -> StockBalance:
    balance = StockBalance.objects.filter(warehouse=warehouse, item=item, lot=lot).first()
    assert balance is not None
    return balance


def _journal(
    document: InventoryMovementDocument | InventoryAdjustmentDocument | StockCount,
) -> JournalEntry:
    """The journal, insisting it exists — these assertions are about figures."""
    entry = document.journal_entry
    assert entry is not None
    return entry


def _value(amount: Decimal | None) -> Decimal:
    assert amount is not None
    return amount


def _waste(
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    reason_code: InventoryReasonCode,
    *,
    cost_center: CostCenter | None = None,
    lot: InventoryLot | None = None,
    comment: str = "",
    at: datetime.datetime | None = None,
) -> InventoryMovementDocument:
    document = create_document(
        actor=actor,
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        document_type=InventoryDocumentType.WASTE,
        effective_at=at or _moment(),
        evidence_reference="WASTE-NOTE-1",
        cost_center=cost_center,
    )
    add_document_line(
        actor=actor,
        document=document,
        line=DocumentLineInput(
            item=item,
            lot=lot,
            base_quantity=Decimal(quantity),
            reason_code=reason_code,
            line_comment=comment,
        ),
    )
    return document


# ---------------------------------------------------------------------------
# §D Reason codes
# ---------------------------------------------------------------------------


class TestReasonCodes:
    def test_a_code_is_canonicalised_to_upper_case(
        self, boss: User, organization: Organization
    ) -> None:
        code = create_reason_code(
            actor=boss,
            organization=organization,
            code="  spoil  ",
            name_ar="تلف",
            applies_to=ReasonCodeApplication.WASTE,
        )
        assert code.code == "SPOIL"

    def test_a_code_is_unique_per_organization(
        self, boss: User, organization: Organization
    ) -> None:
        create_reason_code(
            actor=boss,
            organization=organization,
            code="SPOIL",
            name_ar="تلف",
            applies_to=ReasonCodeApplication.WASTE,
        )
        with pytest.raises(ValidationError) as refused:
            create_reason_code(
                actor=boss,
                organization=organization,
                code="spoil",
                name_ar="تلف آخر",
                applies_to=ReasonCodeApplication.COUNT_VARIANCE,
            )
        assert refused.value.code == "reason_code_taken"

    def test_an_archived_code_stays_reserved(self, boss: User, organization: Organization) -> None:
        code = create_reason_code(
            actor=boss,
            organization=organization,
            code="BREAK",
            name_ar="كسر",
            applies_to=ReasonCodeApplication.WASTE,
        )
        update_reason_code(actor=boss, reason_code=code, name_ar="كسر", is_active=False)
        with pytest.raises(ValidationError) as refused:
            create_reason_code(
                actor=boss,
                organization=organization,
                code="BREAK",
                name_ar="شيء مختلف تماماً",
                applies_to=ReasonCodeApplication.MANUAL_ADJUSTMENT,
            )
        assert refused.value.code == "reason_code_taken"

    def test_the_code_and_its_application_are_immutable_at_the_database(
        self, boss: User, organization: Organization
    ) -> None:
        code = create_reason_code(
            actor=boss,
            organization=organization,
            code="SPOIL",
            name_ar="تلف",
            applies_to=ReasonCodeApplication.WASTE,
        )
        with pytest.raises(IntegrityError, match="repurposing it"), transaction.atomic():
            InventoryReasonCode.objects.filter(pk=code.pk).update(
                applies_to=ReasonCodeApplication.COUNT_VARIANCE
            )

    def test_the_name_and_its_requirements_may_change(
        self, boss: User, organization: Organization
    ) -> None:
        code = create_reason_code(
            actor=boss,
            organization=organization,
            code="OTHER",
            name_ar="أخرى",
            applies_to=ReasonCodeApplication.WASTE,
        )
        updated = update_reason_code(
            actor=boss, reason_code=code, name_ar="أسباب أخرى", requires_comment=True
        )
        assert updated.name_ar == "أسباب أخرى"
        assert updated.requires_comment is True

    def test_another_organizations_codes_are_invisible(
        self,
        boss: User,
        organization: Organization,
        other_organization: Organization,
        rival_manager: User,
    ) -> None:
        create_reason_code(
            actor=boss,
            organization=organization,
            code="SPOIL",
            name_ar="تلف",
            applies_to=ReasonCodeApplication.WASTE,
        )
        assert visible_reason_codes(rival_manager).count() == 0

    def test_a_storekeeper_cannot_invent_a_reason(
        self, keeper: User, organization: Organization
    ) -> None:
        with pytest.raises(PermissionDenied):
            create_reason_code(
                actor=keeper,
                organization=organization,
                code="CONVENIENT",
                name_ar="سبب مريح",
                applies_to=ReasonCodeApplication.WASTE,
            )


# ---------------------------------------------------------------------------
# §E/§F Waste
# ---------------------------------------------------------------------------


class TestWaste:
    def test_waste_leaves_at_the_current_moving_average(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
        control_account: Account,
        waste_account: Account,
    ) -> None:
        document = _waste(
            boss, organization, branch, main_store, rice, "10", spoilage, cost_center=kitchen
        )
        posted = post_document(actor=boss, document=document)

        assert posted.status == InventoryDocumentStatus.POSTED
        assert posted.document_number.startswith("WST-2026-")
        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("90.000")
        assert balance.value == Decimal("135000.000")

        movement = StockMovement.objects.get(entry=posted.stock_entry)
        assert movement.movement_type == MovementType.WASTE
        assert movement.inventory_value == Decimal("-15000.000")

        lines = list(_journal(posted).lines.order_by("account__code"))
        assert [(line.account.code, line.debit, line.credit) for line in lines] == [
            ("1-03-01-001", Decimal("0.000"), Decimal("15000.000")),
            ("6-02-01-002", Decimal("15000.000"), Decimal("0.000")),
        ]
        assert lines[1].cost_center == kitchen

    def test_the_last_waste_out_takes_the_entire_remaining_value(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
    ) -> None:
        # Three receipts whose average does not divide evenly.
        _seed_stock(boss, organization, branch, main_store, rice, "3", "1000")
        _seed_stock(boss, organization, branch, main_store, rice, "3", "1001")
        _seed_stock(boss, organization, branch, main_store, rice, "1", "1002")
        before = _balance(main_store, rice).value

        document = _waste(
            boss, organization, branch, main_store, rice, "7", spoilage, cost_center=kitchen
        )
        posted = post_document(actor=boss, document=document)

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")
        movement = StockMovement.objects.get(entry=posted.stock_entry)
        assert -movement.inventory_value == before

    def test_an_expired_lot_may_be_wasted_but_never_issued(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        mapped: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        milk = InventoryItem.objects.create(
            organization=organization,
            category=leaf_category,
            code="MILK",
            name_ar="حليب",
            base_unit=kilogram,
            tracks_lots=True,
            tracks_expiry=True,
        )
        lot = InventoryLot.objects.create(
            organization=organization,
            item=milk,
            code="L-OLD",
            expiry_date=datetime.date(TEST_YEAR, 3, 1),
        )
        _seed_stock(
            boss, organization, branch, main_store, milk, "10", "2000", lot=lot, at=_moment(1)
        )

        issue = create_document(
            actor=boss,
            organization=organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.ISSUE,
            effective_at=_moment(),
            evidence_reference="REQ-1",
            cost_center=kitchen,
        )
        add_document_line(
            actor=boss,
            document=issue,
            line=DocumentLineInput(item=milk, lot=lot, base_quantity=Decimal("1")),
        )
        with pytest.raises(ValidationError) as refused:
            post_document(actor=boss, document=issue)
        assert refused.value.code == "lot_expired"

        waste = _waste(
            boss,
            organization,
            branch,
            main_store,
            milk,
            "10",
            spoilage,
            cost_center=kitchen,
            lot=lot,
        )
        posted = post_document(actor=boss, document=waste)
        assert posted.status == InventoryDocumentStatus.POSTED
        assert _balance(main_store, milk, lot).quantity == Decimal("0.000")

    def test_waste_beyond_the_shelf_is_refused(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
    ) -> None:
        document = _waste(
            boss, organization, branch, main_store, rice, "500", spoilage, cost_center=kitchen
        )
        with pytest.raises(ValidationError) as refused:
            post_document(actor=boss, document=document)
        assert refused.value.code == "insufficient_stock"

    def test_a_waste_line_needs_a_reason_code(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        document = create_document(
            actor=boss,
            organization=organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.WASTE,
            effective_at=_moment(),
            evidence_reference="WASTE-NOTE-1",
            cost_center=kitchen,
        )
        with pytest.raises(ValidationError) as refused:
            add_document_line(
                actor=boss,
                document=document,
                line=DocumentLineInput(item=rice, base_quantity=Decimal("1")),
            )
        assert refused.value.code == "waste_reason_code_required"

    def test_a_count_reason_cannot_be_used_on_a_waste_line(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        wrong = create_reason_code(
            actor=boss,
            organization=organization,
            code="RECOUNT",
            name_ar="خطأ عدّ",
            applies_to=ReasonCodeApplication.COUNT_VARIANCE,
        )
        document = create_document(
            actor=boss,
            organization=organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.WASTE,
            effective_at=_moment(),
            evidence_reference="W-1",
            cost_center=kitchen,
        )
        with pytest.raises(ValidationError) as refused:
            add_document_line(
                actor=boss,
                document=document,
                line=DocumentLineInput(item=rice, base_quantity=Decimal("1"), reason_code=wrong),
            )
        assert refused.value.code == "reason_code_wrong_application"

    def test_a_reason_that_demands_a_comment_gets_one(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        other = create_reason_code(
            actor=boss,
            organization=organization,
            code="OTHER",
            name_ar="أخرى",
            applies_to=ReasonCodeApplication.WASTE,
            requires_comment=True,
        )
        document = create_document(
            actor=boss,
            organization=organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.WASTE,
            effective_at=_moment(),
            evidence_reference="W-1",
            cost_center=kitchen,
        )
        with pytest.raises(ValidationError) as refused:
            add_document_line(
                actor=boss,
                document=document,
                line=DocumentLineInput(item=rice, base_quantity=Decimal("1"), reason_code=other),
            )
        assert refused.value.code == "reason_code_comment_required"

    def test_waste_needs_a_cost_centre_because_its_account_demands_one(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
    ) -> None:
        document = _waste(boss, organization, branch, main_store, rice, "1", spoilage)
        with pytest.raises(ValidationError) as refused:
            post_document(actor=boss, document=document)
        assert refused.value.code == "cost_center_required"

    def test_an_unmapped_waste_account_rolls_everything_back(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
    ) -> None:
        mapping = organization.account_mappings.get(account_role__code=INVENTORY_WASTE_EXPENSE)
        archive_account_mapping(mapping=mapping, reason="test")

        document = _waste(
            boss, organization, branch, main_store, rice, "10", spoilage, cost_center=kitchen
        )
        with pytest.raises(ValidationError) as refused:
            post_document(actor=boss, document=document)
        assert refused.value.code == "account_role_unmapped"

        document.refresh_from_db()
        assert document.status == InventoryDocumentStatus.DRAFT
        assert _balance(main_store, rice).quantity == Decimal("100.000")

    def test_a_storekeeper_cannot_post_waste(
        self,
        boss: User,
        keeper: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
    ) -> None:
        document = _waste(
            boss, organization, branch, main_store, rice, "1", spoilage, cost_center=kitchen
        )
        with pytest.raises(PermissionDenied):
            post_document(actor=keeper, document=document)

    def test_reversing_waste_restores_the_exact_quantity_and_value(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
    ) -> None:
        document = _waste(
            boss, organization, branch, main_store, rice, "10", spoilage, cost_center=kitchen
        )
        posted = post_document(actor=boss, document=document)
        reversed_document = reverse_document(
            actor=boss, document=posted, reason="wrong shelf counted"
        )

        assert reversed_document.status == InventoryDocumentStatus.REVERSED
        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("150000.000")

    def test_an_identical_retry_posts_once(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
    ) -> None:
        document = _waste(
            boss, organization, branch, main_store, rice, "10", spoilage, cost_center=kitchen
        )
        post_document(actor=boss, document=document)
        with pytest.raises(ValidationError) as refused:
            post_document(actor=boss, document=document)
        assert refused.value.code == "already_posted"
        assert StockMovement.objects.filter(movement_type=MovementType.WASTE).count() == 1


# ---------------------------------------------------------------------------
# §G–§R Physical counts
# ---------------------------------------------------------------------------


def _start(actor: User, count: StockCount, at: datetime.datetime | None = None) -> StockCount:
    return start_stock_count(actor=actor, count=count, effective_at=at or _moment())


def _count_of(
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    *,
    cost_center: CostCenter | None = None,
) -> StockCount:
    return create_stock_count(
        actor=actor,
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        reference="COUNT-SHEET-1",
        reason="جرد شهري",
        cost_center=cost_center,
    )


class TestCountStartAndFreeze:
    def test_starting_freezes_the_warehouse_and_snapshots_the_book(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))

        assert count.status == StockCountStatus.IN_PROGRESS
        assert count.count_number.startswith("CNT-2026-")
        assert count.business_date == datetime.date(TEST_YEAR, 3, 15)
        main_store.refresh_from_db()
        assert main_store.frozen_by_count_id == count.pk

        line = StockCountLine.objects.get(count=count)
        assert line.item_id == rice.pk
        assert line.book_quantity == Decimal("100.000")
        assert line.book_value == Decimal("150000.000")
        assert line.book_average == Decimal("1500.000000")
        assert line.counted_quantity is None

    def test_a_frozen_warehouse_refuses_every_posting(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
    ) -> None:
        _start(boss, _count_of(boss, organization, branch, main_store))
        document = _waste(
            boss, organization, branch, main_store, rice, "1", spoilage, cost_center=kitchen
        )
        with pytest.raises(ValidationError) as refused:
            post_document(actor=boss, document=document)
        assert refused.value.code == "warehouse_frozen"

    def test_only_one_count_may_be_active_in_a_warehouse(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        _start(boss, _count_of(boss, organization, branch, main_store))
        second = _count_of(boss, organization, branch, main_store)
        with pytest.raises(ValidationError) as refused:
            _start(boss, second)
        assert refused.value.code == "warehouse_already_frozen"

    def test_the_in_transit_warehouse_cannot_be_counted(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        mapped: None,
    ) -> None:
        from apps.inventory.services import ensure_in_transit_warehouse

        transit = ensure_in_transit_warehouse(branch=branch)
        with pytest.raises(ValidationError) as refused:
            _count_of(boss, organization, branch, transit)
        assert refused.value.code == "warehouse_not_countable"

    def test_a_frozen_warehouse_names_an_active_count_at_the_database(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        cancel_stock_count(actor=boss, count=count, reason="abandoned")
        with pytest.raises(IntegrityError, match="because that count is"), transaction.atomic():
            Warehouse.objects.filter(pk=main_store.pk).update(frozen_by_count=count)
            connection.cursor().execute("SET CONSTRAINTS ALL IMMEDIATE")


class TestBlindEntry:
    def test_the_sheet_carries_no_book_quantity_at_all(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        sheet = blind_count_sheet(actor=boss, count=count)

        assert len(sheet) == 1
        blind = {
            "book_quantity",
            "book_value",
            "book_average",
            "book_control_account",
            "variance_quantity",
            "variance_value",
        }
        assert blind.isdisjoint(sheet[0].keys())
        # And nothing that merely renames them either.
        serialised = str(sheet[0])
        assert "150000" not in serialised
        assert "1500" not in serialised

    def test_the_sheet_stays_blind_even_for_a_valuation_holder(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        from apps.inventory.permissions import VIEW_VALUATION

        assert boss.has_perm(VIEW_VALUATION)
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        assert "book_quantity" not in blind_count_sheet(actor=boss, count=count)[0]

    def test_counting_records_a_quantity_and_zero_is_a_real_answer(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        line = StockCountLine.objects.get(count=count)
        [stored] = record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("0"))]
        )
        assert stored.counted_quantity == Decimal("0.000")

    def test_a_negative_count_is_refused(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        line = StockCountLine.objects.get(count=count)
        with pytest.raises(ValidationError) as refused:
            record_stock_counts(
                actor=boss,
                count=count,
                entries=[CountEntry(line=line, base_quantity=Decimal("-1"))],
            )
        assert refused.value.code == "counted_quantity_negative"

    def test_a_fixed_conversion_computes_its_own_base_quantity(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        sack: PackageUnit,
        stocked: None,
    ) -> None:
        conversion = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("25"),
            conversion_type=ConversionType.FIXED,
            effective_from=datetime.date(TEST_YEAR, 1, 1),
        )
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        line = StockCountLine.objects.get(count=count)
        [stored] = record_stock_counts(
            actor=boss,
            count=count,
            entries=[
                CountEntry(
                    line=line,
                    package_conversion=conversion,
                    entered_package_quantity=Decimal("4"),
                )
            ],
        )
        assert stored.counted_quantity == Decimal("100.000")

    def test_a_variable_conversion_must_be_weighed(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        sack: PackageUnit,
        stocked: None,
    ) -> None:
        conversion = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("25"),
            conversion_type=ConversionType.VARIABLE,
            effective_from=datetime.date(TEST_YEAR, 1, 1),
        )
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        line = StockCountLine.objects.get(count=count)
        with pytest.raises(ValidationError) as refused:
            record_stock_counts(
                actor=boss,
                count=count,
                entries=[
                    CountEntry(
                        line=line,
                        package_conversion=conversion,
                        entered_package_quantity=Decimal("4"),
                    )
                ],
            )
        assert refused.value.code == "measured_quantity_required"

    def test_unexpected_stock_is_added_with_a_zero_book(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        salt = InventoryItem.objects.create(
            organization=organization,
            category=leaf_category,
            code="SALT",
            name_ar="ملح",
            base_unit=kilogram,
        )
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        line = add_unexpected_count_line(
            actor=boss, count=count, item=salt, base_quantity=Decimal("5")
        )
        assert line.is_unexpected is True
        assert line.book_quantity == Decimal("0.000")
        assert line.counted_quantity == Decimal("5.000")

    def test_the_same_item_cannot_appear_twice(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        with pytest.raises(ValidationError) as refused:
            add_unexpected_count_line(
                actor=boss, count=count, item=rice, base_quantity=Decimal("5")
            )
        assert refused.value.code == "duplicate_count_key"


class TestSubmissionAndApproval:
    def test_submission_needs_every_line_counted(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        with pytest.raises(ValidationError) as refused:
            submit_stock_count(actor=boss, count=count)
        assert refused.value.code == "count_incomplete"

    def test_submission_computes_the_variance_and_freezes_the_figures(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("95"))]
        )
        submitted = submit_stock_count(actor=boss, count=count)

        assert submitted.status == StockCountStatus.SUBMITTED
        line.refresh_from_db()
        assert line.variance_quantity == Decimal("-5.000")
        main_store.refresh_from_db()
        assert main_store.frozen_by_count_id == count.pk

        with pytest.raises(ValidationError) as refused:
            record_stock_counts(
                actor=boss,
                count=count,
                entries=[CountEntry(line=line, base_quantity=Decimal("100"))],
            )
        assert refused.value.code == "count_not_in_progress"

    def test_the_conductor_cannot_approve_their_own_count(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("95"))]
        )
        submit_stock_count(actor=boss, count=count)
        with pytest.raises(ValidationError) as refused:
            approve_stock_count(actor=boss, count=count)
        assert refused.value.code == "approver_is_the_conductor"

    def test_maker_checker_is_a_database_constraint_too(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        with (
            pytest.raises(IntegrityError, match="approver_is_not_the_conductor"),
            transaction.atomic(),
        ):
            StockCount.objects.filter(pk=count.pk).update(approved_by=boss)

    def test_a_storekeeper_cannot_approve(
        self,
        boss: User,
        keeper: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(keeper, _count_of(keeper, organization, branch, main_store))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=keeper, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("95"))]
        )
        submit_stock_count(actor=keeper, count=count)
        with pytest.raises(PermissionDenied):
            approve_stock_count(actor=keeper, count=count)

    def test_a_loss_posts_at_the_standing_average_and_unfreezes(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
        control_account: Account,
        count_variance_account: Account,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("95"))]
        )
        submit_stock_count(actor=boss, count=count)
        posted = approve_stock_count(actor=checker, count=count)

        assert posted.status == StockCountStatus.POSTED
        main_store.refresh_from_db()
        assert main_store.frozen_by_count_id is None

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("95.000")
        assert balance.value == Decimal("142500.000")

        line.refresh_from_db()
        assert line.variance_value == Decimal("-7500.000")
        entries = list(_journal(posted).lines.order_by("account__code"))
        assert [(e.account.code, e.debit, e.credit) for e in entries] == [
            ("1-03-01-001", Decimal("0.000"), Decimal("7500.000")),
            ("7-09-02-001", Decimal("7500.000"), Decimal("0.000")),
        ]

    def test_a_gain_into_standing_stock_uses_the_standing_average(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("110"))]
        )
        submit_stock_count(actor=boss, count=count)
        approve_stock_count(actor=checker, count=count)

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("110.000")
        assert balance.value == Decimal("165000.000")
        # The average is unchanged: finding more of something says nothing
        # about what the rest of it cost.
        assert balance.average_cost == Decimal("1500.000000")

    def test_a_zero_book_gain_needs_an_approved_unit_cost(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
        kitchen: CostCenter,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        salt = InventoryItem.objects.create(
            organization=organization,
            category=leaf_category,
            code="SALT",
            name_ar="ملح",
            base_unit=kilogram,
        )
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        rice_line = StockCountLine.objects.get(count=count, is_unexpected=False)
        record_stock_counts(
            actor=boss,
            count=count,
            entries=[CountEntry(line=rice_line, base_quantity=Decimal("100"))],
        )
        found = add_unexpected_count_line(
            actor=boss, count=count, item=salt, base_quantity=Decimal("5")
        )
        submit_stock_count(actor=boss, count=count)

        with pytest.raises(ValidationError) as refused:
            approve_stock_count(actor=checker, count=count)
        assert refused.value.code == "approved_unit_cost_required"

        approve_stock_count(
            actor=checker,
            count=count,
            costs=[ApprovedCost(line=found, unit_cost=Decimal("800"))],
        )
        balance = _balance(main_store, salt)
        assert balance.quantity == Decimal("5.000")
        assert balance.value == Decimal("4000.000")

    def test_an_omitted_cost_and_a_confirmed_zero_are_different_answers(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
        kitchen: CostCenter,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        salt = InventoryItem.objects.create(
            organization=organization,
            category=leaf_category,
            code="SALT",
            name_ar="ملح",
            base_unit=kilogram,
        )
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        rice_line = StockCountLine.objects.get(count=count, is_unexpected=False)
        record_stock_counts(
            actor=boss,
            count=count,
            entries=[CountEntry(line=rice_line, base_quantity=Decimal("100"))],
        )
        found = add_unexpected_count_line(
            actor=boss, count=count, item=salt, base_quantity=Decimal("5")
        )
        submit_stock_count(actor=boss, count=count)

        with pytest.raises(ValidationError) as refused:
            approve_stock_count(
                actor=checker,
                count=count,
                costs=[ApprovedCost(line=found, unit_cost=Decimal("0"))],
            )
        assert refused.value.code == "zero_cost_not_confirmed"

        approve_stock_count(
            actor=checker,
            count=count,
            costs=[ApprovedCost(line=found, unit_cost=Decimal("0"), zero_confirmed=True)],
        )
        found.refresh_from_db()
        assert found.zero_cost_confirmed is True
        assert _balance(main_store, salt).quantity == Decimal("5.000")

    def test_a_full_loss_takes_the_exact_remaining_value(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
        kitchen: CostCenter,
    ) -> None:
        _seed_stock(boss, organization, branch, main_store, rice, "3", "1000")
        _seed_stock(boss, organization, branch, main_store, rice, "3", "1001")
        _seed_stock(boss, organization, branch, main_store, rice, "1", "1002")
        before = _balance(main_store, rice).value

        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("0"))]
        )
        submit_stock_count(actor=boss, count=count)
        approve_stock_count(actor=checker, count=count)

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")
        line.refresh_from_db()
        assert -_value(line.variance_value) == before

    def test_a_zero_variance_count_posts_nothing_and_still_completes(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("100"))]
        )
        submit_stock_count(actor=boss, count=count)
        posted = approve_stock_count(actor=checker, count=count)

        assert posted.status == StockCountStatus.POSTED
        assert posted.stock_entry is None
        assert posted.journal_entry is None
        main_store.refresh_from_db()
        assert main_store.frozen_by_count_id is None

    def test_a_changed_book_position_refuses_to_post(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("95"))]
        )
        submit_stock_count(actor=boss, count=count)

        # Something bypassed the freeze. The count must not paper over it.
        StockBalance.objects.filter(warehouse=main_store, item=rice).update(
            quantity=Decimal("90.000")
        )
        with pytest.raises(ValidationError) as refused:
            approve_stock_count(actor=checker, count=count)
        assert refused.value.code == "count_snapshot_mismatch"

    def test_a_missing_mapping_rolls_back_stock_journal_status_and_freeze(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("95"))]
        )
        submit_stock_count(actor=boss, count=count)

        mapping = organization.account_mappings.get(account_role__code=INVENTORY_COUNT_VARIANCE)
        archive_account_mapping(mapping=mapping, reason="test")

        with pytest.raises(ValidationError) as refused:
            approve_stock_count(actor=checker, count=count)
        assert refused.value.code == "account_role_unmapped"

        count.refresh_from_db()
        main_store.refresh_from_db()
        assert count.status == StockCountStatus.SUBMITTED
        assert main_store.frozen_by_count_id == count.pk
        assert _balance(main_store, rice).quantity == Decimal("100.000")


class TestCancellationReversalAndPeriods:
    def test_cancelling_unfreezes_and_keeps_the_history(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        cancelled = cancel_stock_count(actor=boss, count=count, reason="power cut")

        assert cancelled.status == StockCountStatus.CANCELLED
        assert cancelled.cancellation_reason == "power cut"
        main_store.refresh_from_db()
        assert main_store.frozen_by_count_id is None
        assert StockCountLine.objects.filter(count=cancelled).exists()
        assert StockCount.objects.filter(pk=count.pk).exists()

    def test_cancelling_needs_a_reason(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        with pytest.raises(ValidationError) as refused:
            cancel_stock_count(actor=boss, count=count, reason="   ")
        assert refused.value.code == "reason_required"

    def test_a_posted_count_reverses_exactly_and_does_not_refreeze(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("95"))]
        )
        submit_stock_count(actor=boss, count=count)
        approve_stock_count(actor=checker, count=count)

        reversed_count = reverse_stock_count(
            actor=boss, count=count, reason="counted the wrong aisle"
        )
        assert reversed_count.status == StockCountStatus.REVERSED

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("150000.000")
        main_store.refresh_from_db()
        assert main_store.frozen_by_count_id is None

    def test_reversing_a_gain_whose_stock_was_consumed_is_refused(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("110"))]
        )
        submit_stock_count(actor=boss, count=count)
        approve_stock_count(actor=checker, count=count)

        issue = create_document(
            actor=boss,
            organization=organization,
            branch=branch,
            warehouse=main_store,
            document_type=InventoryDocumentType.ISSUE,
            effective_at=_moment(16),
            evidence_reference="REQ-9",
            cost_center=kitchen,
        )
        add_document_line(
            actor=boss,
            document=issue,
            line=DocumentLineInput(item=rice, base_quantity=Decimal("105")),
        )
        post_document(actor=boss, document=issue)

        with pytest.raises(ValidationError) as refused:
            reverse_stock_count(actor=boss, count=count, reason="mistake")
        assert refused.value.code == "insufficient_stock"

    def test_an_active_count_blocks_closing_its_period(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        period = resolve_period(
            organization=organization, accounting_date=datetime.date(TEST_YEAR, 3, 15)
        )
        with pytest.raises(ValidationError) as refused:
            soft_close_period(period=period, reason="month end")
        assert refused.value.code == "active_inventory_count"
        assert count.count_number in str(refused.value)

        # And the hard close, once the ordering rule is out of the way.
        for month in (1, 2):
            close_period(
                period=resolve_period(
                    organization=organization,
                    accounting_date=datetime.date(TEST_YEAR, month, 1),
                ),
                reason="month end",
            )
        with pytest.raises(ValidationError) as refused:
            close_period(period=period, reason="month end")
        assert refused.value.code == "active_inventory_count"

    def test_a_cancelled_count_does_not_block_the_close(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        cancel_stock_count(actor=boss, count=count, reason="abandoned")
        period = resolve_period(
            organization=organization, accounting_date=datetime.date(TEST_YEAR, 3, 15)
        )
        assert soft_close_period(period=period, reason="month end").state == (
            PeriodState.SOFT_CLOSED
        )

    def test_a_business_date_follows_the_branch_cutoff_not_the_calendar(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        branch.business_day_start_time = datetime.time(3, 0)
        branch.save(update_fields=["business_day_start_time"])
        count = _start(
            boss,
            _count_of(boss, organization, branch, main_store),
            at=datetime.datetime(TEST_YEAR, 3, 16, 1, 30, tzinfo=BAGHDAD),
        )
        assert count.business_date == datetime.date(TEST_YEAR, 3, 15)


class TestCountGuards:
    def test_a_started_count_cannot_be_deleted(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        # Three guards, in the order they fire. While the count holds the
        # freeze, PROTECT on `Warehouse.frozen_by_count` refuses first.
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        with pytest.raises(IntegrityError, match="protected foreign keys"), transaction.atomic():
            StockCount.objects.filter(pk=count.pk).delete()

        # Once the warehouse is released, the cascade to its lines is refused.
        cancel_stock_count(actor=boss, count=count, reason="abandoned")
        with pytest.raises(IntegrityError, match="lines cannot be removed"), transaction.atomic():
            StockCount.objects.filter(pk=count.pk).delete()
        assert StockCount.objects.filter(pk=count.pk).exists()

    def test_a_cancelled_count_with_no_lines_is_still_undeletable(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        kitchen_store: Warehouse,
        mapped: None,
    ) -> None:
        """The count-level trigger, reached where no line trigger fires first."""
        count = _start(boss, _count_of(boss, organization, branch, kitchen_store))
        assert not StockCountLine.objects.filter(count=count).exists()
        cancel_stock_count(actor=boss, count=count, reason="empty store")
        with pytest.raises(IntegrityError, match="cancel it instead"), transaction.atomic():
            StockCount.objects.filter(pk=count.pk).delete()

    def test_the_book_snapshot_is_immutable(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        line = StockCountLine.objects.get(count=count)
        with pytest.raises(IntegrityError, match="fixed at the cutoff"), transaction.atomic():
            StockCountLine.objects.filter(pk=line.pk).update(book_quantity=Decimal("999"))

    def test_the_cutoff_is_immutable(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        with pytest.raises(IntegrityError, match="frozen once it starts"), transaction.atomic():
            StockCount.objects.filter(pk=count.pk).update(
                business_date=datetime.date(TEST_YEAR, 4, 1)
            )

    def test_a_foreign_count_is_a_404(
        self,
        boss: User,
        rival_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        count = _start(boss, _count_of(boss, organization, branch, main_store))
        with pytest.raises(OutOfScope):
            resolve_count(rival_manager, count.pk)
        assert visible_counts(rival_manager).count() == 0

    def test_a_line_from_another_count_is_a_404_on_this_route(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        _seed_stock(boss, organization, branch, kitchen_store, rice, "5", "1500")
        first = _start(boss, _count_of(boss, organization, branch, main_store))
        second = _start(boss, _count_of(boss, organization, branch, kitchen_store))
        other_line = StockCountLine.objects.get(count=second)
        with pytest.raises(OutOfScope):
            resolve_count_line(boss, other_line.pk, count=first)


# ---------------------------------------------------------------------------
# §V/§W Manual adjustments
# ---------------------------------------------------------------------------


def _adjustment(
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    *,
    cost_center: CostCenter | None = None,
) -> InventoryAdjustmentDocument:
    return create_adjustment(
        actor=actor,
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        effective_at=_moment(),
        evidence_reference="MEMO-1",
        reason="تصحيح خطأ إدخال",
        cost_center=cost_center,
    )


class TestManualAdjustments:
    def test_a_quantity_gain_needs_an_explicit_unit_cost(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        with pytest.raises(ValidationError) as refused:
            add_adjustment_line(
                actor=boss,
                document=document,
                line=AdjustmentLineInput(
                    kind="QUANTITY_GAIN",
                    item=rice,
                    reason_code=correction,
                    base_quantity=Decimal("10"),
                ),
            )
        assert refused.value.code == "unit_cost_required"

    def test_a_quantity_gain_posts_at_the_stated_cost(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
        control_account: Account,
        adjustment_account: Account,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=document,
            line=AdjustmentLineInput(
                kind="QUANTITY_GAIN",
                item=rice,
                reason_code=correction,
                base_quantity=Decimal("10"),
                unit_cost=Decimal("2000"),
            ),
        )
        posted = post_adjustment(actor=boss, document=document)

        assert posted.document_number.startswith("ADJ-2026-")
        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("110.000")
        assert balance.value == Decimal("170000.000")

        lines = list(_journal(posted).lines.order_by("account__code"))
        assert [(e.account.code, e.debit, e.credit) for e in lines] == [
            ("1-03-01-001", Decimal("20000.000"), Decimal("0.000")),
            ("7-09-03-001", Decimal("0.000"), Decimal("20000.000")),
        ]

    def test_a_quantity_loss_posts_at_the_standing_average(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=document,
            line=AdjustmentLineInput(
                kind="QUANTITY_LOSS",
                item=rice,
                reason_code=correction,
                base_quantity=Decimal("20"),
            ),
        )
        posted = post_adjustment(actor=boss, document=document)

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("80.000")
        assert balance.value == Decimal("120000.000")
        lines = list(_journal(posted).lines.order_by("account__code"))
        assert [(e.account.code, e.debit, e.credit) for e in lines] == [
            ("1-03-01-001", Decimal("0.000"), Decimal("30000.000")),
            ("7-09-03-001", Decimal("30000.000"), Decimal("0.000")),
        ]

    def test_a_value_only_write_up_moves_no_quantity(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=document,
            line=AdjustmentLineInput(
                kind="VALUE_ONLY",
                item=rice,
                reason_code=correction,
                value_adjustment=Decimal("30000"),
            ),
        )
        posted = post_adjustment(actor=boss, document=document)

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("180000.000")
        assert balance.average_cost == Decimal("1800.000000")

        movement = StockMovement.objects.get(entry=posted.stock_entry)
        assert movement.base_quantity == Decimal("0.000")
        assert movement.inventory_value == Decimal("30000.000")

    def test_a_value_only_write_down_moves_no_quantity(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=document,
            line=AdjustmentLineInput(
                kind="VALUE_ONLY",
                item=rice,
                reason_code=correction,
                value_adjustment=Decimal("-30000"),
            ),
        )
        posted = post_adjustment(actor=boss, document=document)

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("120000.000")
        lines = list(_journal(posted).lines.order_by("account__code"))
        assert [(e.account.code, e.debit, e.credit) for e in lines] == [
            ("1-03-01-001", Decimal("0.000"), Decimal("30000.000")),
            ("7-09-03-001", Decimal("30000.000"), Decimal("0.000")),
        ]

    def test_a_value_only_line_against_no_quantity_is_refused(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        mapped: None,
        correction: InventoryReasonCode,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        salt = InventoryItem.objects.create(
            organization=organization,
            category=leaf_category,
            code="SALT",
            name_ar="ملح",
            base_unit=kilogram,
        )
        document = _adjustment(boss, organization, branch, main_store)
        with pytest.raises(ValidationError) as refused:
            add_adjustment_line(
                actor=boss,
                document=document,
                line=AdjustmentLineInput(
                    kind="VALUE_ONLY",
                    item=salt,
                    reason_code=correction,
                    value_adjustment=Decimal("1000"),
                ),
            )
        assert refused.value.code == "value_only_needs_quantity"

    def test_a_write_down_below_zero_is_refused(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        with pytest.raises(ValidationError) as refused:
            add_adjustment_line(
                actor=boss,
                document=document,
                line=AdjustmentLineInput(
                    kind="VALUE_ONLY",
                    item=rice,
                    reason_code=correction,
                    value_adjustment=Decimal("-200000"),
                ),
            )
        assert refused.value.code == "value_only_would_go_negative"

    def test_a_wrong_application_reason_is_refused(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        with pytest.raises(ValidationError) as refused:
            add_adjustment_line(
                actor=boss,
                document=document,
                line=AdjustmentLineInput(
                    kind="QUANTITY_LOSS",
                    item=rice,
                    reason_code=spoilage,
                    base_quantity=Decimal("1"),
                ),
            )
        assert refused.value.code == "reason_code_wrong_application"

    def test_a_storekeeper_cannot_post_an_adjustment(
        self,
        keeper: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        with pytest.raises(PermissionDenied):
            _adjustment(keeper, organization, branch, main_store)

    def test_reversal_mirrors_the_adjustment_exactly(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=document,
            line=AdjustmentLineInput(
                kind="VALUE_ONLY",
                item=rice,
                reason_code=correction,
                value_adjustment=Decimal("30000"),
            ),
        )
        posted = post_adjustment(actor=boss, document=document)
        reversed_document = reverse_adjustment(actor=boss, document=posted, reason="wrong memo")

        assert reversed_document.status == InventoryDocumentStatus.REVERSED
        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("150000.000")

    def test_a_posted_adjustment_is_immutable_at_the_database(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
    ) -> None:
        from apps.inventory.models import InventoryAdjustmentDocument

        document = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=document,
            line=AdjustmentLineInput(
                kind="QUANTITY_LOSS",
                item=rice,
                reason_code=correction,
                base_quantity=Decimal("1"),
            ),
        )
        posted = post_adjustment(actor=boss, document=document)
        with pytest.raises(IntegrityError, match="posted and is immutable"), transaction.atomic():
            InventoryAdjustmentDocument.objects.filter(pk=posted.pk).update(
                evidence_reference="MEMO-CHANGED"
            )

    def test_a_frozen_warehouse_refuses_an_adjustment(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        correction: InventoryReasonCode,
    ) -> None:
        document = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=document,
            line=AdjustmentLineInput(
                kind="QUANTITY_LOSS",
                item=rice,
                reason_code=correction,
                base_quantity=Decimal("1"),
            ),
        )
        _start(boss, _count_of(boss, organization, branch, main_store))
        with pytest.raises(ValidationError) as refused:
            post_adjustment(actor=boss, document=document)
        assert refused.value.code == "warehouse_frozen"

    def test_an_expired_lot_may_be_adjusted_away(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        mapped: None,
        correction: InventoryReasonCode,
        leaf_category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        milk = InventoryItem.objects.create(
            organization=organization,
            category=leaf_category,
            code="MILK",
            name_ar="حليب",
            base_unit=kilogram,
            tracks_lots=True,
            tracks_expiry=True,
        )
        lot = InventoryLot.objects.create(
            organization=organization,
            item=milk,
            code="L-OLD",
            expiry_date=datetime.date(TEST_YEAR, 3, 1),
        )
        _seed_stock(
            boss, organization, branch, main_store, milk, "10", "2000", lot=lot, at=_moment(1)
        )
        document = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=document,
            line=AdjustmentLineInput(
                kind="QUANTITY_LOSS",
                item=milk,
                lot=lot,
                reason_code=correction,
                base_quantity=Decimal("10"),
            ),
        )
        post_adjustment(actor=boss, document=document)
        assert _balance(main_store, milk, lot).quantity == Decimal("0.000")


# ---------------------------------------------------------------------------
# §AC Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_a_clean_organization_reports_nothing(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        correction: InventoryReasonCode,
        kitchen: CostCenter,
    ) -> None:
        """One of each document, then every comparison at once."""
        from apps.inventory.reconciliation import verify_inventory_accounting

        post_document(
            actor=boss,
            document=_waste(
                boss, organization, branch, main_store, rice, "10", spoilage, cost_center=kitchen
            ),
        )

        adjustment = _adjustment(boss, organization, branch, main_store)
        add_adjustment_line(
            actor=boss,
            document=adjustment,
            line=AdjustmentLineInput(
                kind="VALUE_ONLY",
                item=rice,
                reason_code=correction,
                value_adjustment=Decimal("1000"),
            ),
        )
        post_adjustment(actor=boss, document=adjustment)

        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("85"))]
        )
        submit_stock_count(actor=boss, count=count)
        approve_stock_count(actor=checker, count=count)

        assert verify_inventory_accounting(organization) == []

    def test_a_tampered_count_variance_is_reported(
        self,
        boss: User,
        checker: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
        kitchen: CostCenter,
    ) -> None:
        from apps.inventory.reconciliation import verify_stock_count

        count = _start(boss, _count_of(boss, organization, branch, main_store, cost_center=kitchen))
        line = StockCountLine.objects.get(count=count)
        record_stock_counts(
            actor=boss, count=count, entries=[CountEntry(line=line, base_quantity=Decimal("95"))]
        )
        submit_stock_count(actor=boss, count=count)
        approve_stock_count(actor=checker, count=count)
        count.refresh_from_db()
        assert verify_stock_count(count) == []

        # Somebody edited the retained variance value directly.
        StockCountLine.objects.filter(pk=line.pk).update(variance_value=Decimal("-1.000"))
        problems = verify_stock_count(count)
        fields = {problem.field for problem in problems}
        assert "variance_value" in fields
        assert "movement_total" in fields

    def test_a_frozen_warehouse_with_no_active_count_is_reported(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        """
        Unreachable through the services and refused by two triggers — so this
        plants it with the triggers disabled, which is the only way the state
        can arise and exactly the state reconciliation must still name.
        """
        from apps.inventory.reconciliation import verify_warehouse_freezes

        count = _start(boss, _count_of(boss, organization, branch, main_store))
        assert verify_warehouse_freezes(organization) == []

        cancel_stock_count(actor=boss, count=count, reason="abandoned")
        with connection.cursor() as cursor:
            # The seeding receipt armed the deferred control-account triggers,
            # and PostgreSQL refuses `ALTER TABLE` on a table with pending
            # trigger events. Forcing them to fire now empties the queue; the
            # postings are sound, so nothing is refused.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("ALTER TABLE inventory_warehouse DISABLE TRIGGER USER")
            cursor.execute(
                "UPDATE inventory_warehouse SET frozen_by_count_id = %s WHERE id = %s",
                [count.pk, main_store.pk],
            )
            cursor.execute("ALTER TABLE inventory_warehouse ENABLE TRIGGER USER")

        problems = verify_warehouse_freezes(organization)
        assert [problem.field for problem in problems] == ["freeze_owner_status"]
        assert problems[0].actual == StockCountStatus.CANCELLED

    def test_a_planted_journal_stays_visible_as_drift(
        self,
        boss: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        spoilage: InventoryReasonCode,
        kitchen: CostCenter,
        control_account: Account,
        waste_account: Account,
    ) -> None:
        """No repair mode anywhere: a manual journal on a control account shows."""
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine
        from apps.inventory.reconciliation import verify_inventory_accounting

        post_document(
            actor=boss,
            document=_waste(
                boss, organization, branch, main_store, rice, "10", spoilage, cost_center=kitchen
            ),
        )
        assert verify_inventory_accounting(organization) == []

        post_entry(
            organization=organization,
            accounting_date=datetime.date(TEST_YEAR, 3, 15),
            lines=[
                PostingLine(
                    account=control_account,
                    branch=branch,
                    debit=Decimal("5000"),
                    credit=Decimal("0"),
                ),
                PostingLine(
                    account=waste_account,
                    branch=branch,
                    cost_center=kitchen,
                    debit=Decimal("0"),
                    credit=Decimal("5000"),
                ),
            ],
            idempotency_key="planted-by-hand",
            narration="قيد يدوي",
        )
        problems = verify_inventory_accounting(organization)
        assert problems, "a manual journal on the control account must remain visible"
        assert any("5000" in problem for problem in problems)


class TestTheFreezeBypassIsNotAGeneralEscape:
    def test_only_the_count_service_may_post_into_a_frozen_warehouse(self) -> None:
        """
        `owned_freezes` is a narrow allowance for the one caller that owns the
        freeze it is posting through. A test holds that line, because a second
        caller would be a way to post into somebody else's count.

        `ledger.py` is excluded because it *defines* and forwards the parameter
        — it is the mechanism, not a user of it — and this file is excluded
        because naming the thing is how the test asks the question.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3]
        mechanism = {
            "apps/inventory/ledger.py",
            "apps/inventory/tests/" + pathlib.Path(__file__).name,
        }
        callers = sorted(
            relative
            for path in (root / "apps").rglob("*.py")
            if "owned_freezes" in path.read_text(encoding="utf-8")
            and (relative := path.relative_to(root).as_posix()) not in mechanism
        )
        assert callers == ["apps/inventory/counts.py"]

    def test_no_permission_grants_the_bypass(self) -> None:
        from apps.inventory.permissions import ALL_PERMISSIONS

        assert not any("freeze" in permission for permission in ALL_PERMISSIONS)
