"""
Business-date semantics for stock postings (Task 1.4 §B, §S 1–4).

Two timestamps, one authority. `effective_at` is the physical moment;
`business_date` is the operational and accounting date, derived through the
branch's timezone and operating-day cutoff. Everything that *dates* a posting
uses the business date — and requiring the calendar month of `effective_at`
as well would demand two open periods for an event that happened on one
business day.
"""

from __future__ import annotations

import datetime
import zoneinfo
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_OPENING_EQUITY,
    Account,
    AccountingPeriod,
    AccountRole,
    PeriodState,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.core.context import audit_context
from apps.inventory.commands import (
    add_opening_line,
    create_opening,
    post_opening,
    return_opening_to_draft,
    submit_opening,
)
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import (
    InventoryItem,
    MovementType,
    OpeningStockDocument,
    StockLedgerEntry,
    Warehouse,
)
from apps.inventory.opening import OpeningLineInput
from apps.organizations.business_dates import business_date_for, resolve_business_day
from apps.organizations.models import Branch, Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026

#: 01:30 on 1 August, physically. Under an 03:00 cutoff this belongs to the
#: 31 July business day — the case the whole section exists for.
AFTER_MIDNIGHT_AUGUST = datetime.datetime(TEST_YEAR, 8, 1, 1, 30, tzinfo=BAGHDAD)


@pytest.fixture
def late_branch(branch: Branch) -> Branch:
    """The Al-Bunook branch with a 03:00 operating-day start."""
    branch.business_day_start_time = datetime.time(3, 0)
    branch.save(update_fields=["business_day_start_time"])
    return Branch.objects.get(pk=branch.pk)


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_CONTROL),
        account=Account.objects.get(organization=organization, code="1-03-01-001"),
        effective_from=datetime.date(TEST_YEAR, 1, 1),
    )
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_OPENING_EQUITY),
        account=Account.objects.get(organization=organization, code="3-02-01-001"),
        effective_from=datetime.date(TEST_YEAR, 1, 1),
    )


def _period(organization: Organization, month: int) -> AccountingPeriod:
    return AccountingPeriod.objects.get(fiscal_year__organization=organization, period_number=month)


def _set_state(organization: Organization, month: int, state: str) -> None:
    period = _period(organization, month)
    period.state = state
    period.save(update_fields=["state"])


def _receipt(warehouse: Warehouse, item: InventoryItem) -> MovementInput:
    return MovementInput(
        warehouse=warehouse,
        item=item,
        movement_type=MovementType.RECEIPT,
        quantity=Decimal("10"),
        unit_cost=Decimal("1000"),
        effect_key="line:1",
    )


class TestDerivation:
    def test_a_moment_before_the_cutoff_belongs_to_the_previous_day(
        self, late_branch: Branch
    ) -> None:
        assert business_date_for(late_branch, AFTER_MIDNIGHT_AUGUST) == datetime.date(
            TEST_YEAR, 7, 31
        )

    def test_a_moment_after_the_cutoff_belongs_to_its_own_day(self, late_branch: Branch) -> None:
        morning = datetime.datetime(TEST_YEAR, 8, 1, 9, 0, tzinfo=BAGHDAD)
        assert business_date_for(late_branch, morning) == datetime.date(TEST_YEAR, 8, 1)

    def test_midnight_itself_belongs_to_the_previous_day(self, late_branch: Branch) -> None:
        midnight = datetime.datetime(TEST_YEAR, 8, 1, 0, 0, tzinfo=BAGHDAD)
        assert business_date_for(late_branch, midnight) == datetime.date(TEST_YEAR, 7, 31)

    def test_a_snapshot_carries_the_settings_that_produced_it(self, late_branch: Branch) -> None:
        day = resolve_business_day(late_branch, AFTER_MIDNIGHT_AUGUST)
        assert day.business_date == datetime.date(TEST_YEAR, 7, 31)
        assert day.timezone_name == late_branch.timezone
        assert day.day_start == datetime.time(3, 0)

    def test_a_naive_moment_is_refused(self, late_branch: Branch) -> None:
        with pytest.raises(ValueError, match="aware"):
            business_date_for(late_branch, datetime.datetime(TEST_YEAR, 8, 1, 1, 30))


class TestPeriodValidationUsesTheBusinessDate:
    """§S 1–2, and the four period combinations §B names by hand."""

    def test_the_entry_records_the_business_date_not_the_calendar_date(
        self,
        organization: Organization,
        late_branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
        superuser: User,
    ) -> None:
        with audit_context(actor=superuser):
            entry = post_stock_entry(
                organization=organization,
                effects=[_receipt(main_store, rice)],
                idempotency_key="k1",
                effective_at=AFTER_MIDNIGHT_AUGUST,
            )
        assert entry.effective_at == AFTER_MIDNIGHT_AUGUST
        assert entry.business_date == datetime.date(TEST_YEAR, 7, 31)

    def test_july_open_and_august_closed_still_accepts_a_july_business_date(
        self,
        organization: Organization,
        late_branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
        superuser: User,
    ) -> None:
        """
        The month-end case. The physical timestamp is in August and August is
        closed — but the event belongs to the 31st of July, which is open, and
        that is the only period it may be asked about.
        """
        _set_state(organization, 8, PeriodState.CLOSED)
        with audit_context(actor=superuser):
            entry = post_stock_entry(
                organization=organization,
                effects=[_receipt(main_store, rice)],
                idempotency_key="k1",
                effective_at=AFTER_MIDNIGHT_AUGUST,
            )
        assert entry.business_date == datetime.date(TEST_YEAR, 7, 31)

    def test_july_closed_and_august_open_refuses_a_july_business_date(
        self,
        organization: Organization,
        late_branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
        superuser: User,
    ) -> None:
        """The mirror image: the calendar month being open saves nothing."""
        _set_state(organization, 7, PeriodState.CLOSED)
        with audit_context(actor=superuser), pytest.raises(ValidationError) as caught:
            post_stock_entry(
                organization=organization,
                effects=[_receipt(main_store, rice)],
                idempotency_key="k1",
                effective_at=AFTER_MIDNIGHT_AUGUST,
            )
        assert caught.value.code == "period_not_open"
        assert StockLedgerEntry.objects.count() == 0

    def test_a_soft_closed_business_period_is_refused_too(
        self,
        organization: Organization,
        late_branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
        superuser: User,
    ) -> None:
        _set_state(organization, 7, PeriodState.SOFT_CLOSED)
        with audit_context(actor=superuser), pytest.raises(ValidationError) as caught:
            post_stock_entry(
                organization=organization,
                effects=[_receipt(main_store, rice)],
                idempotency_key="k1",
                effective_at=AFTER_MIDNIGHT_AUGUST,
            )
        assert caught.value.code == "period_not_open"

    def test_an_explicit_business_date_overrides_the_derivation(
        self,
        organization: Organization,
        late_branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
        superuser: User,
    ) -> None:
        """A document passes its stored snapshot; the kernel honours it."""
        with audit_context(actor=superuser):
            entry = post_stock_entry(
                organization=organization,
                effects=[_receipt(main_store, rice)],
                idempotency_key="k1",
                effective_at=AFTER_MIDNIGHT_AUGUST,
                business_date=datetime.date(TEST_YEAR, 8, 1),
            )
        assert entry.business_date == datetime.date(TEST_YEAR, 8, 1)


class TestTheOpeningSnapshotIsStable:
    """§S 3–4: a submitted document's business date is committed, not live."""

    @pytest.fixture
    def submitted(
        self,
        manager: User,
        organization: Organization,
        late_branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> OpeningStockDocument:
        document = create_opening(
            actor=manager,
            organization=organization,
            branch=late_branch,
            cutoff_at=AFTER_MIDNIGHT_AUGUST,
            evidence_reference="NIGHT-COUNT",
        )
        add_opening_line(
            actor=manager,
            document=document,
            line=OpeningLineInput(
                warehouse=main_store,
                item=rice,
                base_quantity=Decimal("10"),
                unit_cost=Decimal("1000"),
            ),
        )
        return submit_opening(actor=manager, document=document)

    def test_submission_stores_the_date_and_the_settings_that_produced_it(
        self, submitted: OpeningStockDocument
    ) -> None:
        assert submitted.business_date == datetime.date(TEST_YEAR, 7, 31)
        assert submitted.business_date_timezone == "Asia/Baghdad"
        assert submitted.business_day_start == datetime.time(3, 0)

    def test_changing_the_branch_cutoff_afterwards_does_not_move_the_document(
        self,
        submitted: OpeningStockDocument,
        late_branch: Branch,
        accounting_manager: User,
        mapped: None,
    ) -> None:
        """
        The defect this exists to prevent: an approver reads "31 July", the
        cutoff is changed to midnight, and posting silently lands the document
        in August — a different period, a different month's figures, and
        nobody decided it.
        """
        late_branch.business_day_start_time = datetime.time(0, 0)
        late_branch.save(update_fields=["business_day_start_time"])

        posted = post_opening(actor=accounting_manager, document=submitted)
        assert posted.business_date == datetime.date(TEST_YEAR, 7, 31)
        assert posted.stock_entry is not None
        assert posted.stock_entry.business_date == datetime.date(TEST_YEAR, 7, 31)
        assert posted.journal_entry is not None
        assert posted.journal_entry.accounting_date == datetime.date(TEST_YEAR, 7, 31)

    def test_return_to_draft_releases_the_snapshot_and_resubmission_recalculates(
        self,
        submitted: OpeningStockDocument,
        late_branch: Branch,
        manager: User,
    ) -> None:
        returned = return_opening_to_draft(
            actor=manager, document=submitted, reason="cutoff was wrong"
        )
        assert returned.business_date_timezone == ""
        assert returned.business_day_start is None

        late_branch.business_day_start_time = datetime.time(0, 0)
        late_branch.save(update_fields=["business_day_start_time"])

        resubmitted = submit_opening(actor=manager, document=returned)
        # Under a midnight cutoff the same moment is now an August day, and
        # the deliberate resubmission is what makes that change legitimate.
        assert resubmitted.business_date == datetime.date(TEST_YEAR, 8, 1)
        assert resubmitted.business_day_start == datetime.time(0, 0)

    def test_a_draft_previews_the_date_but_commits_to_nothing(
        self,
        manager: User,
        organization: Organization,
        late_branch: Branch,
        accounting: None,
    ) -> None:
        draft = create_opening(
            actor=manager,
            organization=organization,
            branch=late_branch,
            cutoff_at=AFTER_MIDNIGHT_AUGUST,
            evidence_reference="DRAFT",
        )
        assert draft.business_date == datetime.date(TEST_YEAR, 7, 31)
        assert draft.business_date_timezone == ""
        assert draft.business_day_start is None


class TestReversalTakesTodaysBusinessDate:
    def test_a_reversal_is_dated_when_it_is_made(
        self,
        manager: User,
        accounting_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        A reversal is a new economic event in the current period, not a
        retroactive edit of the original's month.
        """
        from apps.inventory.commands import reverse_opening

        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=timezone.now(),
            evidence_reference="SHEET",
        )
        add_opening_line(
            actor=manager,
            document=document,
            line=OpeningLineInput(
                warehouse=main_store,
                item=rice,
                base_quantity=Decimal("10"),
                unit_cost=Decimal("1000"),
            ),
        )
        submit_opening(actor=manager, document=document)
        posted = post_opening(actor=accounting_manager, document=document)
        reversed_document = reverse_opening(
            actor=accounting_manager, document=posted, reason="restated"
        )

        today = business_date_for(branch, timezone.now())
        reversal_entry = StockLedgerEntry.objects.get(reverses=posted.stock_entry)
        assert reversal_entry.business_date == today
        assert reversed_document.reversal_journal_entry is not None
        assert reversed_document.reversal_journal_entry.accounting_date == today


class TestAmbiguousBusinessDates:
    def test_effects_falling_on_two_business_dates_are_refused(
        self,
        organization: Organization,
        branch: Branch,
        second_branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
        superuser: User,
    ) -> None:
        """
        Two branches with different cutoffs put one moment on two operational
        days. A single posting cannot belong to both, and picking the first
        silently would date half the effects wrongly.
        """
        from apps.inventory.services import create_warehouse

        branch.business_day_start_time = datetime.time(3, 0)
        branch.save(update_fields=["business_day_start_time"])
        second_branch.business_day_start_time = datetime.time(0, 0)
        second_branch.save(update_fields=["business_day_start_time"])
        other_store = create_warehouse(
            branch=Branch.objects.get(pk=second_branch.pk), code="KAR", name="مخزن الكرادة"
        )

        with audit_context(actor=superuser), pytest.raises(ValidationError) as caught:
            post_stock_entry(
                organization=organization,
                effects=[
                    _receipt(Warehouse.objects.get(pk=main_store.pk), rice),
                    MovementInput(
                        warehouse=other_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("5"),
                        unit_cost=Decimal("1000"),
                        effect_key="line:2",
                    ),
                ],
                idempotency_key="k1",
                effective_at=AFTER_MIDNIGHT_AUGUST,
            )
        assert caught.value.code == "ambiguous_business_date"


class TestConversionAndMappingUseTheBusinessDate:
    def test_the_opening_resolves_its_mapping_on_the_business_date(
        self,
        manager: User,
        accounting_manager: User,
        organization: Organization,
        late_branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        """
        A mapping effective only up to 31 July still covers an event that is
        physically in August but operationally on the 31st.
        """
        control = Account.objects.get(organization=organization, code="1-03-01-001")
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=INVENTORY_CONTROL),
            account=control,
            effective_from=datetime.date(TEST_YEAR, 1, 1),
            effective_to=datetime.date(TEST_YEAR, 7, 31),
        )
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=INVENTORY_OPENING_EQUITY),
            account=Account.objects.get(organization=organization, code="3-02-01-001"),
            effective_from=datetime.date(TEST_YEAR, 1, 1),
        )

        document = create_opening(
            actor=manager,
            organization=organization,
            branch=late_branch,
            cutoff_at=AFTER_MIDNIGHT_AUGUST,
            evidence_reference="NIGHT",
        )
        add_opening_line(
            actor=manager,
            document=document,
            line=OpeningLineInput(
                warehouse=main_store,
                item=rice,
                base_quantity=Decimal("10"),
                unit_cost=Decimal("1000"),
            ),
        )
        submit_opening(actor=manager, document=document)
        posted = post_opening(actor=accounting_manager, document=document)

        assert posted.lines.get().inventory_account == control


class TestTheMovementCarriesItsControlAccount:
    """§D: the account is on the movement, not re-resolved from today's chart."""

    def test_a_posted_opening_stamps_the_account_on_movement_and_balance(
        self,
        manager: User,
        accounting_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        from apps.inventory.models import StockBalance, StockMovement

        document = create_opening(
            actor=manager,
            organization=organization,
            branch=branch,
            cutoff_at=timezone.now(),
            evidence_reference="SHEET",
        )
        add_opening_line(
            actor=manager,
            document=document,
            line=OpeningLineInput(
                warehouse=main_store,
                item=rice,
                base_quantity=Decimal("10"),
                unit_cost=Decimal("1000"),
            ),
        )
        submit_opening(actor=manager, document=document)
        post_opening(actor=accounting_manager, document=document)

        control = Account.objects.get(organization=organization, code="1-03-01-001")
        assert StockMovement.objects.get().control_account == control
        assert StockBalance.objects.get().control_account == control

    def test_a_bare_kernel_posting_records_no_account_rather_than_inventing_one(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
        superuser: User,
    ) -> None:
        """
        No mapping resolved, so no account entered — and NULL says exactly
        that. A default would claim the value sits somewhere it does not.
        """
        from apps.inventory.models import StockBalance, StockMovement

        with audit_context(actor=superuser):
            post_stock_entry(
                organization=organization,
                effects=[_receipt(main_store, rice)],
                idempotency_key="k1",
            )
        assert StockMovement.objects.get().control_account is None
        assert StockBalance.objects.get().control_account is None
