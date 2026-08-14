"""
The stock ledger kernel: arithmetic, identity, immutability, and refusal.

These tests are the specification of what the numbers mean. A failure here is
not a broken test — it is stock that would be valued wrongly, or a retry that
would post twice.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounting.models import AccountingPeriod, PeriodState, SourceEvent
from apps.core.source_identity import canonical_source_identity
from apps.inventory.ledger import (
    MovementInput,
    apply_inbound,
    apply_outbound,
    post_stock_entry,
    request_fingerprint,
    reverse_stock_entry,
)
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
    ValuationAllocation,
    ValuationLayer,
    Warehouse,
)
from apps.inventory.reconciliation import verify_organization
from apps.organizations.models import Branch, Organization

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _period_covering_now(organization: Organization) -> AccountingPeriod:
    """
    The period today falls in, opened through the real service.

    `open_fiscal_year` writes all twelve months, so this uses it rather than
    hand-building one period: a test that constructed periods its own way
    could pass against a year no deployment would ever have.
    """
    from apps.accounting.services import open_fiscal_year

    today = timezone.localdate()
    open_fiscal_year(organization=organization, year=today.year)
    return AccountingPeriod.objects.get(
        fiscal_year__organization=organization,
        start_date__lte=today,
        end_date__gte=today,
    )


@pytest.fixture
def open_period(organization: Organization) -> AccountingPeriod:
    """Stock postings need an OPEN period, so every posting test needs one."""
    return _period_covering_now(organization)


@pytest.fixture
def other_open_period(other_organization: Organization) -> AccountingPeriod:
    return _period_covering_now(other_organization)


@pytest.fixture
def lot_item(organization: Organization, leaf_category: Any, kilogram: Any) -> InventoryItem:
    from apps.inventory.services import create_item

    return create_item(
        organization=organization,
        code="CHK-FRESH",
        name_ar="دجاج طازج",
        category=leaf_category,
        item_type="RAW_MATERIAL",
        base_unit=kilogram,
        tracks_lots=True,
    )


@pytest.fixture
def lot(organization: Organization, lot_item: InventoryItem) -> InventoryLot:
    return InventoryLot.objects.create(organization=organization, item=lot_item, code="L-2026-01")


def _receipt(
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    unit_cost: str,
    *,
    key: str = "line:1",
    lot: InventoryLot | None = None,
) -> MovementInput:
    return MovementInput(
        warehouse=warehouse,
        item=item,
        movement_type=MovementType.RECEIPT,
        quantity=Decimal(quantity),
        unit_cost=Decimal(unit_cost),
        effect_key=key,
        lot=lot,
    )


def _issue(
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    *,
    key: str = "line:1",
    lot: InventoryLot | None = None,
) -> MovementInput:
    return MovementInput(
        warehouse=warehouse,
        item=item,
        movement_type=MovementType.ISSUE,
        quantity=Decimal(quantity),
        effect_key=key,
        lot=lot,
    )


def _post(
    organization: Organization, effects: list[MovementInput], key: str, **kwargs: Any
) -> StockLedgerEntry:
    return post_stock_entry(
        organization=organization, effects=effects, idempotency_key=key, **kwargs
    )


def _balance(
    warehouse: Warehouse, item: InventoryItem, lot: InventoryLot | None = None
) -> StockBalance:
    return StockBalance.objects.get(warehouse=warehouse, item=item, lot=lot)


# ---------------------------------------------------------------------------
# Source identity canonicalisation
# ---------------------------------------------------------------------------


class TestSourceIdentityCanonicalisation:
    def test_surrounding_space_names_the_same_economic_source(self) -> None:
        """`"145"`, `"145 "`, and `" 145"` are one invoice."""
        forms = ["145", "145 ", " 145", "  145  "]
        canonical = {
            canonical_source_identity(
                source_document_type="goods_receipt",
                source_document_id=value,
                source_event="posted",
            ).as_tuple()
            for value in forms
        }
        assert canonical == {("GOODS_RECEIPT", "145", "POSTED")}

    def test_case_is_folded_on_our_vocabulary_and_not_on_theirs(self) -> None:
        """
        `document_type` and `event` are names this system chose. A supplier's
        invoice number is not: `AB-1042` and `ab-1042` can be two different
        documents, and merging them would suppress a real posting.
        """
        upper = canonical_source_identity(
            source_document_type="GOODS_RECEIPT", source_document_id="ABC", source_event="POSTED"
        )
        lower = canonical_source_identity(
            source_document_type="goods_receipt", source_document_id="abc", source_event="posted"
        )
        assert upper.document_type == lower.document_type
        assert upper.event == lower.event
        assert upper.document_id != lower.document_id

    def test_a_whitespace_only_value_is_refused_not_swallowed(self) -> None:
        with pytest.raises(ValidationError) as refused:
            canonical_source_identity(
                source_document_type="GOODS_RECEIPT",
                source_document_id="   ",
                source_event="POSTED",
            )
        assert refused.value.code == "blank_source_identity"

    def test_a_posting_stores_the_canonical_form(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        entry = _post(
            organization,
            [_receipt(main_store, rice, "10", "1000")],
            "k-1",
            source_document_type=" goods_receipt ",
            source_document_id=" 145 ",
            source_event="posted",
        )
        assert entry.source_document_type == "GOODS_RECEIPT"
        assert entry.source_document_id == "145"
        assert entry.source_event == SourceEvent.POSTED

    def test_and_the_padded_retry_is_refused_as_the_same_event(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """The whole reason canonicalisation exists, checked end to end."""
        _post(
            organization,
            [_receipt(main_store, rice, "10", "1000")],
            "k-1",
            source_document_type="GOODS_RECEIPT",
            source_document_id="145",
        )
        with pytest.raises(ValidationError) as refused:
            _post(
                organization,
                [_receipt(main_store, rice, "10", "1000")],
                "k-2",
                source_document_type="goods_receipt",
                source_document_id="145 ",
            )
        assert refused.value.code == "source_event_already_posted"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_the_same_payload_returns_the_original(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        first = _post(organization, [_receipt(main_store, rice, "10", "1000")], "retry-me")
        again = _post(organization, [_receipt(main_store, rice, "10", "1000")], "retry-me")

        assert again.pk == first.pk
        assert StockMovement.objects.count() == 1
        assert _balance(main_store, rice).quantity == Decimal("10.000")

    def test_a_changed_payload_is_a_conflict(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "same-key")
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, rice, "12", "1000")], "same-key")
        assert refused.value.code == "idempotency_key_conflict"

    def test_effect_order_is_not_part_of_the_request(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        lot_item: InventoryItem,
        lot: InventoryLot,
        open_period: Any,
    ) -> None:
        """
        The same lines in a different order are the same request. A
        fingerprint over an unordered collection in caller order would call a
        legitimate retry a conflict.
        """
        a = _receipt(main_store, rice, "10", "1000", key="line:1")
        b = _receipt(main_store, lot_item, "5", "2000", key="line:2", lot=lot)

        first = _post(organization, [a, b], "order-key")
        again = _post(organization, [b, a], "order-key")
        assert again.pk == first.pk

    def test_the_same_key_in_another_organization_is_independent(
        self,
        organization: Organization,
        other_organization: Organization,
        main_store: Warehouse,
        other_warehouse: Warehouse,
        rice: InventoryItem,
        other_category: Any,
        kilogram: Any,
        open_period: Any,
        other_open_period: Any,
    ) -> None:
        from apps.inventory.services import create_item

        theirs = create_item(
            organization=other_organization,
            code="RICE-272",
            name_ar="رز",
            category=other_category,
            item_type="RAW_MATERIAL",
            base_unit=kilogram,
        )

        ours = _post(organization, [_receipt(main_store, rice, "10", "1000")], "shared-key")
        theirs_entry = _post(
            other_organization, [_receipt(other_warehouse, theirs, "7", "500")], "shared-key"
        )

        assert ours.pk != theirs_entry.pk
        assert _balance(main_store, rice).quantity == Decimal("10.000")
        assert _balance(other_warehouse, theirs).quantity == Decimal("7.000")

    def test_a_duplicate_effect_key_in_one_posting_is_refused(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            _post(
                organization,
                [
                    _receipt(main_store, rice, "10", "1000", key="line:1"),
                    _receipt(main_store, rice, "5", "1000", key="line:1"),
                ],
                "dupe",
            )
        assert refused.value.code == "duplicate_effect_key"

    def test_the_database_refuses_a_duplicate_effect_key_too(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """The service refuses it; so does the constraint behind the service."""
        entry = _post(organization, [_receipt(main_store, rice, "10", "1000")], "k")
        original = entry.movements.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(
                entry=entry,
                organization=original.organization,
                branch=original.branch,
                warehouse=original.warehouse,
                item=original.item,
                movement_type=MovementType.RECEIPT,
                effect_key=original.effect_key,
                base_quantity=Decimal("1"),
                inventory_value=Decimal("1"),
                unit_cost=Decimal("1"),
                quantity_before=Decimal("0"),
                quantity_after=Decimal("1"),
                value_before=Decimal("0"),
                value_after=Decimal("1"),
                average_before=Decimal("0"),
                average_after=Decimal("1"),
                posted_sequence=9999,
                effective_at=timezone.now(),
            )


# ---------------------------------------------------------------------------
# Moving weighted average
# ---------------------------------------------------------------------------


class TestMovingWeightedAverage:
    def test_a_receipt_at_zero_balance_sets_the_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "100", "1500")], "k1")
        balance = _balance(main_store, rice)

        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("150000.000")
        assert balance.average_cost == Decimal("1500.000000")

    def test_a_second_receipt_blends_the_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        _post(organization, [_receipt(main_store, rice, "100", "2000")], "k2")
        balance = _balance(main_store, rice)

        assert balance.quantity == Decimal("200.000")
        assert balance.value == Decimal("300000.000")
        assert balance.average_cost == Decimal("1500.000000")

    def test_an_issue_is_valued_at_the_current_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        _post(organization, [_receipt(main_store, rice, "100", "2000")], "k2")
        _post(organization, [_issue(main_store, rice, "50")], "k3")

        movement = StockMovement.objects.order_by("-posted_sequence").first()
        assert movement is not None
        assert movement.unit_cost == Decimal("1500.000000")
        assert movement.inventory_value == Decimal("-75000.000")

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("150.000")
        assert balance.value == Decimal("225000.000")
        assert balance.average_cost == Decimal("1500.000000")

    def test_an_issue_does_not_move_the_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "3", "1000")], "k1")
        before = _balance(main_store, rice).average_cost
        _post(organization, [_issue(main_store, rice, "1")], "k2")
        assert _balance(main_store, rice).average_cost == before

    def test_full_depletion_absorbs_the_exact_remaining_value(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        The case ADR-018 §4 exists for. 3 units at a cost that does not divide
        evenly: issuing all three must leave exactly zero value, not a residual
        that no later movement can ever clear.
        """
        _post(organization, [_receipt(main_store, rice, "3", "1000.0005")], "k1")
        balance = _balance(main_store, rice)
        assert balance.value == Decimal("3000.002")  # 3 x 1000.0005, rounded once

        _post(organization, [_issue(main_store, rice, "1")], "k2")
        _post(organization, [_issue(main_store, rice, "1")], "k3")
        _post(organization, [_issue(main_store, rice, "1")], "k4")

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")
        assert balance.average_cost == Decimal("0.000000")

        last = StockMovement.objects.order_by("-posted_sequence").first()
        assert last is not None
        # The residual went into the goods that actually left, and no earlier
        # movement was touched to make it balance.
        assert last.value_after == Decimal("0.000")

    def test_zero_quantity_implies_zero_value_as_an_invariant(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "7", "333.333")], "k1")
        _post(organization, [_issue(main_store, rice, "7")], "k2")
        balance = _balance(main_store, rice)
        assert (balance.quantity, balance.value, balance.average_cost) == (
            Decimal("0.000"),
            Decimal("0.000"),
            Decimal("0.000000"),
        )

    def test_a_zero_cost_inbound_is_allowed_and_explicit(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        A free sample genuinely costs nothing. Permitted, and it does pull the
        average down — which is correct: the goods are on the books and they
        cost nothing.
        """
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_receipt(main_store, rice, "10", "0")], "k2")
        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("20.000")
        assert balance.value == Decimal("10000.000")
        assert balance.average_cost == Decimal("500.000000")

    def test_a_negative_unit_cost_is_refused(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        effect = MovementInput(
            warehouse=main_store,
            item=rice,
            movement_type=MovementType.RECEIPT,
            quantity=Decimal("1"),
            unit_cost=Decimal("-5"),
            effect_key="line:1",
        )
        with pytest.raises(ValidationError) as refused:
            _post(organization, [effect], "k1")
        assert refused.value.code == "unit_cost_negative"


class TestTheArithmeticInIsolation:
    """The pure functions, without a database in the way."""

    def test_inbound_derives_the_average_from_the_totals(self) -> None:
        step = apply_inbound(
            quantity=Decimal("3"),
            unit_cost=Decimal("1000.0005"),
            before_quantity=Decimal("0"),
            before_value=Decimal("0"),
        )
        assert step.value_after == Decimal("3000.002")
        assert step.average_after == Decimal("1000.000667")

    def test_outbound_to_zero_takes_everything(self) -> None:
        step = apply_outbound(
            quantity=Decimal("3"),
            before_quantity=Decimal("3"),
            before_value=Decimal("3000.002"),
        )
        assert step.value_delta == Decimal("-3000.002")
        assert step.value_after == Decimal("0.000")
        assert step.average_after == Decimal("0")

    def test_outbound_short_of_zero_uses_the_average(self) -> None:
        step = apply_outbound(
            quantity=Decimal("1"),
            before_quantity=Decimal("3"),
            before_value=Decimal("3000.000"),
        )
        assert step.value_delta == Decimal("-1000.000")
        assert step.quantity_after == Decimal("2.000")


# ---------------------------------------------------------------------------
# Negative stock
# ---------------------------------------------------------------------------


class TestNegativeStockIsRefused:
    def test_an_issue_beyond_the_balance_is_refused(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_issue(main_store, rice, "11")], "k2")
        assert refused.value.code == "insufficient_stock"
        assert _balance(main_store, rice).quantity == Decimal("10.000")

    def test_an_issue_from_nothing_is_refused(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_issue(main_store, rice, "1")], "k1")
        assert refused.value.code == "insufficient_stock"

    def test_even_the_owner_is_refused_and_no_role_holds_the_override(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        `override_negative_stock` is reserved vocabulary and deliberately **not
        operational**.

        Two layers, both asserted. While `NEGATIVE_STOCK_OVERRIDE_ENABLED` is
        False no default role — OWNER included — is granted the permission, so
        a role cannot even *look* like it can do something the kernel refuses
        (Task 1.3 §B.2). And the kernel consults no permission at all, so a
        deliberate direct grant would change nothing either. Activating the
        override is an architectural decision with its own approval, tests,
        audit, and a migration relaxing the database constraint.
        """
        from apps.inventory.commands import post_stock_movements
        from apps.inventory.permissions import NEGATIVE_STOCK_OVERRIDE_ENABLED
        from apps.organizations.authorization import roles_granting
        from apps.organizations.models import Role
        from apps.organizations.services import grant_branch_access
        from apps.users.models import User

        from .conftest import PASSWORD

        assert NEGATIVE_STOCK_OVERRIDE_ENABLED is False
        assert roles_granting("inventory.override_negative_stock") == set()

        owner = User.objects.create_user(username="owner", password=PASSWORD)
        grant_branch_access(user=owner, branch=branch, role=Role.OWNER)
        owner = User.objects.get(pk=owner.pk)
        assert not owner.has_perm("inventory.override_negative_stock")

        with pytest.raises(ValidationError) as refused:
            post_stock_movements(
                actor=owner,
                organization=organization,
                effects=[_issue(main_store, rice, "5")],
                idempotency_key="k1",
            )
        assert refused.value.code == "insufficient_stock"

    def test_the_database_refuses_a_negative_balance(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        balance = _balance(main_store, rice)
        with pytest.raises(IntegrityError), transaction.atomic():
            StockBalance.objects.filter(pk=balance.pk).update(quantity=Decimal("-1"))


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


class TestReversal:
    def test_a_reversal_mirrors_the_original_exactly(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        **Not today's average.** The receipt cost 1,000; by the time it is
        reversed the average is 1,500, and the reversal must still remove
        exactly what the receipt added.
        """
        first = _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        _post(organization, [_receipt(main_store, rice, "100", "2000")], "k2")
        assert _balance(main_store, rice).average_cost == Decimal("1500.000000")

        reversal = reverse_stock_entry(entry=first, idempotency_key="rev-1", reason="استلام خاطئ")

        mirrored = reversal.movements.get()
        assert mirrored.base_quantity == Decimal("-100.000")
        assert mirrored.inventory_value == Decimal("-100000.000")

        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("100.000")
        assert balance.value == Decimal("200000.000")
        assert balance.average_cost == Decimal("2000.000000")

    def test_a_reversal_that_would_go_negative_is_refused(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        Receipt +100, issue -80, reverse the receipt. The goods are no longer
        there to take back. Exempting reversals would make "reverse the
        receipt" the standard way to drive a balance negative — the one thing
        the check exists to prevent.
        """
        receipt = _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        _post(organization, [_issue(main_store, rice, "80")], "k2")

        with pytest.raises(ValidationError) as refused:
            reverse_stock_entry(entry=receipt, idempotency_key="rev", reason="خطأ")
        assert refused.value.code == "insufficient_stock"
        assert _balance(main_store, rice).quantity == Decimal("20.000")

    def test_reversing_an_untouched_receipt_is_fine(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        receipt = _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        reverse_stock_entry(entry=receipt, idempotency_key="rev", reason="خطأ")
        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")

    def test_a_second_reversal_is_refused(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        receipt = _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        reverse_stock_entry(entry=receipt, idempotency_key="rev-1", reason="خطأ")
        with pytest.raises(ValidationError) as refused:
            reverse_stock_entry(entry=receipt, idempotency_key="rev-2", reason="مرة أخرى")
        assert refused.value.code == "already_reversed"

    def test_a_reversal_cannot_itself_be_reversed(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        receipt = _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        reversal = reverse_stock_entry(entry=receipt, idempotency_key="rev-1", reason="خطأ")
        with pytest.raises(ValidationError) as refused:
            reverse_stock_entry(entry=reversal, idempotency_key="rev-2", reason="تراجع")
        assert refused.value.code == "cannot_reverse_a_reversal"

    def test_a_reversal_needs_a_reason(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        receipt = _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        with pytest.raises(ValidationError) as refused:
            reverse_stock_entry(entry=receipt, idempotency_key="rev", reason="  ")
        assert refused.value.code == "reason_required"

    def test_the_reversal_carries_the_reversed_source_event(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        receipt = _post(
            organization,
            [_receipt(main_store, rice, "10", "1000")],
            "k1",
            source_document_type="GOODS_RECEIPT",
            source_document_id="145",
        )
        reversal = reverse_stock_entry(entry=receipt, idempotency_key="rev", reason="خطأ")

        assert reversal.source_document_type == "GOODS_RECEIPT"
        assert reversal.source_document_id == "145"
        assert reversal.source_event == SourceEvent.REVERSED


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------


class TestLots:
    def test_a_lot_tracked_item_requires_a_lot(
        self,
        organization: Organization,
        main_store: Warehouse,
        lot_item: InventoryItem,
        open_period: Any,
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, lot_item, "10", "1000")], "k1")
        assert refused.value.code == "lot_required"

    def test_an_untracked_item_refuses_one(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        lot: InventoryLot,
        open_period: Any,
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, rice, "10", "1000", lot=lot)], "k1")
        assert refused.value.code == "lot_not_allowed"

    def test_a_lot_from_another_item_is_refused(
        self,
        organization: Organization,
        main_store: Warehouse,
        lot_item: InventoryItem,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        wrong = InventoryLot.objects.create(organization=organization, item=rice, code="X")
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, lot_item, "1", "1", lot=wrong)], "k1")
        assert refused.value.code == "lot_item_mismatch"

    def test_each_lot_holds_its_own_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        lot_item: InventoryItem,
        lot: InventoryLot,
        open_period: Any,
    ) -> None:
        """The valuation key includes the lot, so two batches never blend."""
        second = InventoryLot.objects.create(
            organization=organization, item=lot_item, code="L-2026-02"
        )
        _post(organization, [_receipt(main_store, lot_item, "10", "1000", lot=lot)], "k1")
        _post(
            organization,
            [_receipt(main_store, lot_item, "10", "3000", key="line:1", lot=second)],
            "k2",
        )

        assert _balance(main_store, lot_item, lot).average_cost == Decimal("1000.000000")
        assert _balance(main_store, lot_item, second).average_cost == Decimal("3000.000000")

    def test_an_expired_lot_cannot_be_issued(
        self,
        organization: Organization,
        main_store: Warehouse,
        leaf_category: Any,
        kilogram: Any,
        open_period: Any,
    ) -> None:
        from apps.inventory.services import create_item

        perishable = create_item(
            organization=organization,
            code="MILK",
            name_ar="حليب",
            category=leaf_category,
            item_type="RAW_MATERIAL",
            base_unit=kilogram,
            tracks_lots=True,
            tracks_expiry=True,
        )
        expired = InventoryLot.objects.create(
            organization=organization,
            item=perishable,
            code="OLD",
            expiry_date=datetime.date(2020, 1, 1),
        )
        _post(organization, [_receipt(main_store, perishable, "10", "1000", lot=expired)], "k1")

        with pytest.raises(ValidationError) as refused:
            _post(organization, [_issue(main_store, perishable, "1", lot=expired)], "k2")
        assert refused.value.code == "lot_expired"

    def test_but_expired_stock_can_still_be_wasted(
        self,
        organization: Organization,
        main_store: Warehouse,
        leaf_category: Any,
        kilogram: Any,
        open_period: Any,
    ) -> None:
        """
        Writing off expired goods is what should happen to them. Blocking
        `WASTE` as well would leave them on the books forever.
        """
        from apps.inventory.services import create_item

        perishable = create_item(
            organization=organization,
            code="MILK",
            name_ar="حليب",
            category=leaf_category,
            item_type="RAW_MATERIAL",
            base_unit=kilogram,
            tracks_lots=True,
            tracks_expiry=True,
        )
        expired = InventoryLot.objects.create(
            organization=organization,
            item=perishable,
            code="OLD",
            expiry_date=datetime.date(2020, 1, 1),
        )
        _post(organization, [_receipt(main_store, perishable, "10", "1000", lot=expired)], "k1")

        waste = MovementInput(
            warehouse=main_store,
            item=perishable,
            movement_type=MovementType.WASTE,
            quantity=Decimal("10"),
            effect_key="line:1",
            lot=expired,
        )
        _post(organization, [waste], "k2")
        assert _balance(main_store, perishable, expired).quantity == Decimal("0.000")

    def test_the_null_lot_balance_is_unique(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        In standard SQL every NULL differs from every other, so a plain unique
        constraint would permit unlimited rows for a non-lot-tracked item.
        `nulls_distinct=False` is what closes that.
        """
        _post(organization, [_receipt(main_store, rice, "1", "1")], "k1")
        with pytest.raises(IntegrityError), transaction.atomic():
            StockBalance.objects.create(
                organization=organization,
                branch=main_store.branch,
                warehouse=main_store,
                item=rice,
                lot=None,
            )


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestTheLedgerIsAppendOnly:
    def test_a_movement_cannot_be_updated(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        movement = StockMovement.objects.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            StockMovement.objects.filter(pk=movement.pk).update(base_quantity=Decimal("999"))

    def test_a_movement_cannot_be_deleted(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        movement = StockMovement.objects.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            movement.delete()

    def test_an_entry_cannot_be_edited(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        entry = _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        with pytest.raises(IntegrityError), transaction.atomic():
            StockLedgerEntry.objects.filter(pk=entry.pk).update(reason="rewritten")

    def test_the_source_identity_of_an_entry_is_immutable(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        entry = _post(
            organization,
            [_receipt(main_store, rice, "10", "1000")],
            "k1",
            source_document_type="GOODS_RECEIPT",
            source_document_id="145",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            StockLedgerEntry.objects.filter(pk=entry.pk).update(source_document_id="146")

    def test_a_valuation_layer_cost_is_immutable(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        layer = ValuationLayer.objects.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            ValuationLayer.objects.filter(pk=layer.pk).update(unit_cost=Decimal("1"))


# ---------------------------------------------------------------------------
# Valuation layers and allocations
# ---------------------------------------------------------------------------


class TestValuationLayers:
    def test_every_inbound_writes_a_layer(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_receipt(main_store, rice, "5", "2000")], "k2")

        layers = list(ValuationLayer.objects.order_by("posted_sequence"))
        assert [layer.unit_cost for layer in layers] == [
            Decimal("1000.000000"),
            Decimal("2000.000000"),
        ]

    def test_an_outbound_writes_none(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_issue(main_store, rice, "4")], "k2")
        assert ValuationLayer.objects.count() == 1

    def test_no_allocation_is_fabricated_under_moving_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        A moving average does not consume a layer — it charges the blended
        cost of everything on hand. Recording that an issue "took 10 kg from
        the layer received on the 3rd" would be a fabrication that looks like
        evidence.
        """
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_issue(main_store, rice, "4")], "k2")
        assert ValuationAllocation.objects.count() == 0

    def test_the_layer_remaining_quantity_is_not_a_stock_claim(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """Untouched by an issue, because nothing consumed it."""
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_issue(main_store, rice, "10")], "k2")
        layer = ValuationLayer.objects.get()
        assert layer.remaining_quantity == Decimal("10.000")
        assert _balance(main_store, rice).quantity == Decimal("0.000")


# ---------------------------------------------------------------------------
# Period and warehouse state
# ---------------------------------------------------------------------------


class TestPeriodAndWarehouseState:
    @pytest.mark.parametrize("state", [PeriodState.SOFT_CLOSED, PeriodState.CLOSED])
    def test_a_closed_period_refuses_postings(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: AccountingPeriod,
        state: str,
    ) -> None:
        AccountingPeriod.objects.filter(pk=open_period.pk).update(state=state)
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        assert refused.value.code == "period_not_open"

    def test_a_date_with_no_period_is_refused(
        self, organization: Organization, main_store: Warehouse, rice: InventoryItem
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        assert refused.value.code == "no_period"

    def test_a_frozen_position_refuses_postings(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        StockBalance.objects.filter(warehouse=main_store, item=rice).update(is_frozen=True)

        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, rice, "5", "1000")], "k2")
        assert refused.value.code == "stock_position_frozen"

    def test_an_archived_warehouse_refuses_postings(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        Warehouse.objects.filter(pk=main_store.pk).update(is_active=False)
        main_store.refresh_from_db()
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        assert refused.value.code == "warehouse_inactive"


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestTenancy:
    def test_a_foreign_warehouse_is_refused(
        self,
        organization: Organization,
        other_warehouse: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(other_warehouse, rice, "1", "1")], "k1")
        assert refused.value.code == "warehouse_organization_mismatch"

    def test_a_foreign_item_is_refused(
        self,
        organization: Organization,
        other_organization: Organization,
        main_store: Warehouse,
        other_category: Any,
        kilogram: Any,
        open_period: Any,
    ) -> None:
        from apps.inventory.services import create_item

        theirs = create_item(
            organization=other_organization,
            code="THEIRS",
            name_ar="لهم",
            category=other_category,
            item_type="RAW_MATERIAL",
            base_unit=kilogram,
        )
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_receipt(main_store, theirs, "1", "1")], "k1")
        assert refused.value.code == "item_organization_mismatch"


# ---------------------------------------------------------------------------
# Posted sequence
# ---------------------------------------------------------------------------


class TestPostedSequence:
    def test_it_increases_and_is_unique_within_the_organization(
        self,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_receipt(kitchen_store, rice, "5", "1000")], "k2")

        sequences = list(
            StockMovement.objects.order_by("posted_sequence").values_list(
                "posted_sequence", flat=True
            )
        )
        assert sequences == [1, 2]

    def test_two_organizations_count_independently(
        self,
        organization: Organization,
        other_organization: Organization,
        main_store: Warehouse,
        other_warehouse: Warehouse,
        rice: InventoryItem,
        other_category: Any,
        kilogram: Any,
        open_period: Any,
        other_open_period: Any,
    ) -> None:
        from apps.inventory.services import create_item

        theirs = create_item(
            organization=other_organization,
            code="THEIRS",
            name_ar="لهم",
            category=other_category,
            item_type="RAW_MATERIAL",
            base_unit=kilogram,
        )
        _post(organization, [_receipt(main_store, rice, "1", "1")], "a")
        _post(other_organization, [_receipt(other_warehouse, theirs, "1", "1")], "b")

        ours = StockMovement.objects.get(organization=organization)
        theirs_movement = StockMovement.objects.get(organization=other_organization)
        assert ours.posted_sequence == 1
        assert theirs_movement.posted_sequence == 1

    def test_a_rolled_back_posting_leaves_no_movement(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        A failed multi-effect posting must leave nothing at all — not the first
        effect, not a balance, not a half-used sequence row with a movement
        behind it.
        """
        good = _receipt(main_store, rice, "10", "1000", key="line:1")
        bad = _issue(main_store, rice, "999", key="line:2")

        with pytest.raises(ValidationError):
            _post(organization, [good, bad], "k1")

        assert StockMovement.objects.count() == 0
        assert StockLedgerEntry.objects.count() == 0
        assert not StockBalance.objects.filter(quantity__gt=0).exists()


# ---------------------------------------------------------------------------
# Rebuild and reconciliation
# ---------------------------------------------------------------------------


class TestRebuild:
    def test_the_rebuild_agrees_with_the_projection(
        self,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        _post(organization, [_receipt(main_store, rice, "100", "2000")], "k2")
        _post(organization, [_issue(main_store, rice, "50")], "k3")
        _post(organization, [_receipt(kitchen_store, rice, "7", "1234.567")], "k4")

        assert verify_organization(organization) == []

    def test_a_corrupted_projection_is_detected(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        The point of the whole exercise. Note the balance is corrupted through
        a raw `update()` — the only way to do it, because nothing in the
        application will.
        """
        _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        StockBalance.objects.filter(warehouse=main_store, item=rice).update(quantity=Decimal("99"))

        mismatches = verify_organization(organization)
        assert [mismatch.field for mismatch in mismatches] == ["quantity"]
        assert "RICE-272" in str(mismatches[0])

    def test_a_full_depletion_replays_to_exactly_zero(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "3", "1000.0005")], "k1")
        _post(organization, [_issue(main_store, rice, "3")], "k2")
        assert verify_organization(organization) == []

    def test_the_verify_command_reports_and_refuses_to_repair(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        from django.core.management import call_command

        _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        StockBalance.objects.filter(warehouse=main_store, item=rice).update(quantity=Decimal("99"))

        with pytest.raises(SystemExit) as exited:
            call_command("verify_stock_ledger", verbosity=0)
        assert exited.value.code == 1

        # Reported, not repaired.
        balance = _balance(main_store, rice)
        assert balance.quantity == Decimal("99.000")


# ---------------------------------------------------------------------------
# Master-data freezing, now that movements exist
# ---------------------------------------------------------------------------


class TestPostedHistoryFreezesMasterData:
    def test_lot_tracking_cannot_change_once_movements_exist(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        Task 1.1 wrote this guard against a model that did not exist. It is
        live now, and the reason is arithmetic: enabling lot tracking splits
        one balance into many, and there is no rule that says which lot the
        existing quantity belonged to.
        """
        from apps.inventory.services import update_item

        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")

        with pytest.raises(ValidationError) as refused:
            update_item(
                item=rice,
                name_ar=rice.name_ar,
                category=rice.category,
                item_type=rice.item_type,
                tracks_lots=True,
            )
        assert refused.value.code == "item_locked_by_movements"

    def test_a_used_conversion_cannot_be_edited_in_place(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        sack: Any,
        open_period: Any,
    ) -> None:
        from apps.inventory.services import create_item_conversion, update_item_conversion

        conversion = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=datetime.date(2026, 1, 1),
        )
        effect = MovementInput(
            warehouse=main_store,
            item=rice,
            movement_type=MovementType.RECEIPT,
            quantity=Decimal("30"),
            unit_cost=Decimal("1000"),
            effect_key="line:1",
            source_conversion=conversion,
        )
        _post(organization, [effect], "k1")

        with pytest.raises(ValidationError) as refused:
            update_item_conversion(
                conversion=conversion,
                factor_to_base=Decimal("25"),
                effective_from=datetime.date(2026, 1, 1),
            )
        assert refused.value.code == "conversion_locked_by_movements"

    def test_but_it_can_still_be_superseded(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        sack: Any,
        open_period: Any,
    ) -> None:
        from apps.inventory.services import create_item_conversion, supersede_item_conversion

        conversion = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=datetime.date(2026, 1, 1),
        )
        effect = MovementInput(
            warehouse=main_store,
            item=rice,
            movement_type=MovementType.RECEIPT,
            quantity=Decimal("30"),
            unit_cost=Decimal("1000"),
            effect_key="line:1",
            source_conversion=conversion,
        )
        _post(organization, [effect], "k1")

        successor = supersede_item_conversion(
            conversion=conversion,
            factor_to_base=Decimal("25"),
            effective_from=datetime.date(2026, 6, 1),
        )
        assert successor.version == 2
        conversion.refresh_from_db()
        assert conversion.factor_to_base == Decimal("30.000000000000")

    def test_an_unused_conversion_may_still_be_corrected(
        self, organization: Organization, rice: InventoryItem, sack: Any
    ) -> None:
        from apps.inventory.services import create_item_conversion, update_item_conversion

        conversion = create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("30"),
            effective_from=datetime.date(2026, 1, 1),
        )
        update_item_conversion(
            conversion=conversion,
            factor_to_base=Decimal("25"),
            effective_from=datetime.date(2026, 1, 1),
        )
        conversion.refresh_from_db()
        assert conversion.factor_to_base == Decimal("25.000000000000")
        assert conversion.version == 1


# ---------------------------------------------------------------------------
# Fingerprint shape
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_it_is_stable_across_effect_order(
        self, main_store: Warehouse, kitchen_store: Warehouse, rice: InventoryItem
    ) -> None:
        moment = timezone.now()
        a = _receipt(main_store, rice, "1", "1", key="a")
        b = _receipt(kitchen_store, rice, "2", "2", key="b")

        one = request_fingerprint(
            command="post", effective_at=moment, source=("", "", ""), effects=[a, b]
        )
        two = request_fingerprint(
            command="post", effective_at=moment, source=("", "", ""), effects=[b, a]
        )
        assert one == two

    def test_it_changes_with_the_quantity(self, main_store: Warehouse, rice: InventoryItem) -> None:
        moment = timezone.now()
        one = request_fingerprint(
            command="post",
            effective_at=moment,
            source=("", "", ""),
            effects=[_receipt(main_store, rice, "1", "1")],
        )
        two = request_fingerprint(
            command="post",
            effective_at=moment,
            source=("", "", ""),
            effects=[_receipt(main_store, rice, "2", "1")],
        )
        assert one != two


# ---------------------------------------------------------------------------
# The eighteen valuation cases from the Task 1.0 specification, §9
# ---------------------------------------------------------------------------


class TestTheEighteenValuationCases:
    """
    Each case named by its number, so the exit gate can be read against the
    specification rather than inferred from test names.

    Four are **not** covered here and cannot be: cases 6, 7, 11, and 12
    (`RETURN_IN`, `RETURN_OUT`, transfer, transfer shortage) are properties of
    documents that Tasks 1.3 to 1.6 create. Their movement types exist and the
    kernel values them correctly; what does not exist is the document that
    would name the original issue to return against, or the in-transit leg to
    dispatch into. Testing them now would test a fiction.
    """

    def test_case_1_receipt_into_positive_stock(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "100", "1000")], "k1")
        _post(organization, [_receipt(main_store, rice, "100", "2000")], "k2")
        assert _balance(main_store, rice).average_cost == Decimal("1500.000000")

    def test_case_2_receipt_at_zero_quantity(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1234.5")], "k1")
        assert _balance(main_store, rice).average_cost == Decimal("1234.500000")

    def test_case_3_issue_at_current_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_issue(main_store, rice, "4")], "k2")
        movement = StockMovement.objects.order_by("-posted_sequence").first()
        assert movement is not None
        assert movement.inventory_value == Decimal("-4000.000")

    def test_case_4_full_depletion_takes_the_whole_remaining_value(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "3", "1000.000333")], "k1")
        held = _balance(main_store, rice).value
        _post(organization, [_issue(main_store, rice, "3")], "k2")

        movement = StockMovement.objects.order_by("-posted_sequence").first()
        assert movement is not None
        assert -movement.inventory_value == held
        assert _balance(main_store, rice).value == Decimal("0.000")

    def test_case_5_a_residual_at_zero_quantity_is_unreachable_and_still_guarded(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        Case 4 makes this state unreachable through the service, and the
        database refuses to hold it at all — an `update()` that tried to force
        value against no quantity is rejected by
        `stock_balance_zero_quantity_has_zero_value`.

        So the state cannot be reached to test the service guard through a
        posting. The guard is checked directly instead: it is defence in depth
        for a future in which the constraint is relaxed, and a guard nobody
        ever exercises is a guard nobody knows is broken.
        """
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_issue(main_store, rice, "10")], "k2")

        with pytest.raises(IntegrityError), transaction.atomic():
            StockBalance.objects.filter(warehouse=main_store, item=rice).update(
                value=Decimal("0.500")
            )

        from apps.inventory.ledger import _assert_position_is_coherent, _Position, _StockKey

        impossible = _Position(
            key=_StockKey(warehouse_id=main_store.pk, item_id=rice.pk, lot_id=None),
            balance=_balance(main_store, rice),
            quantity=Decimal("0"),
            value=Decimal("0.500"),
        )
        with pytest.raises(ValidationError) as refused:
            _assert_position_is_coherent(
                effect=_receipt(main_store, rice, "1", "1000"), position=impossible
            )
        assert refused.value.code == "residual_value_at_zero_quantity"

    def test_negative_value_against_positive_quantity_is_guarded_too(
        self, main_store: Warehouse, rice: InventoryItem
    ) -> None:
        """The other impossible shape, guarded for the same reason."""
        from apps.inventory.ledger import _assert_position_is_coherent, _Position, _StockKey

        impossible = _Position(
            key=_StockKey(warehouse_id=main_store.pk, item_id=rice.pk, lot_id=None),
            balance=StockBalance(warehouse=main_store, item=rice),
            quantity=Decimal("5"),
            value=Decimal("-1"),
        )
        with pytest.raises(ValidationError) as refused:
            _assert_position_is_coherent(
                effect=_receipt(main_store, rice, "1", "1000"), position=impossible
            )
        assert refused.value.code == "negative_value_with_positive_quantity"

    def test_case_8_a_positive_adjustment_needs_an_explicit_unit_cost(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        A count gain adds quantity the ledger did not know about. It must
        arrive with a real cost: creating quantity at zero value would put
        free stock on the books and understate cost of sales for as long as it
        lasted (ADR-018 §8).
        """
        without_cost = MovementInput(
            warehouse=main_store,
            item=rice,
            movement_type=MovementType.COUNT_GAIN,
            quantity=Decimal("5"),
            effect_key="line:1",
        )
        with pytest.raises(ValidationError) as refused:
            _post(organization, [without_cost], "k1")
        assert refused.value.code == "unit_cost_required"

        with_cost = MovementInput(
            warehouse=main_store,
            item=rice,
            movement_type=MovementType.COUNT_GAIN,
            quantity=Decimal("5"),
            unit_cost=Decimal("900"),
            effect_key="line:1",
        )
        _post(organization, [with_cost], "k2")
        assert _balance(main_store, rice).value == Decimal("4500.000")

    def test_case_9_a_negative_adjustment_uses_the_current_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        loss = MovementInput(
            warehouse=main_store,
            item=rice,
            movement_type=MovementType.COUNT_LOSS,
            quantity=Decimal("2"),
            effect_key="line:1",
        )
        _post(organization, [loss], "k2")

        movement = StockMovement.objects.order_by("-posted_sequence").first()
        assert movement is not None
        assert movement.inventory_value == Decimal("-2000.000")
        assert _balance(main_store, rice).average_cost == Decimal("1000.000000")

    def test_case_9_a_negative_adjustment_still_respects_availability(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "1", "1000")], "k1")
        loss = MovementInput(
            warehouse=main_store,
            item=rice,
            movement_type=MovementType.COUNT_LOSS,
            quantity=Decimal("5"),
            effect_key="line:1",
        )
        with pytest.raises(ValidationError) as refused:
            _post(organization, [loss], "k2")
        assert refused.value.code == "insufficient_stock"

    def test_case_10_waste_is_valued_at_the_current_average(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_receipt(main_store, rice, "10", "3000")], "k2")
        waste = MovementInput(
            warehouse=main_store,
            item=rice,
            movement_type=MovementType.WASTE,
            quantity=Decimal("4"),
            effect_key="line:1",
        )
        _post(organization, [waste], "k3")

        movement = StockMovement.objects.order_by("-posted_sequence").first()
        assert movement is not None
        assert movement.unit_cost == Decimal("2000.000000")
        assert movement.inventory_value == Decimal("-8000.000")

    def test_case_13_a_backdated_posting_does_not_reprice_what_came_before(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        """
        Valuation follows **posting order**. A receipt backdated behind an
        issue affects the average from the moment it posts, and leaves the
        issue valued as it was reported and reconciled.
        """
        now = timezone.now()
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_issue(main_store, rice, "5")], "k2")

        issue = StockMovement.objects.order_by("-posted_sequence").first()
        assert issue is not None
        assert issue.inventory_value == Decimal("-5000.000")

        _post(
            organization,
            [_receipt(main_store, rice, "10", "3000")],
            "k3",
            effective_at=now - datetime.timedelta(days=2),
        )

        issue.refresh_from_db()
        assert issue.inventory_value == Decimal("-5000.000")  # untouched
        # And the ledger order is posting order, not effective-date order.
        sequences = list(
            StockMovement.objects.order_by("posted_sequence").values_list(
                "posted_sequence", flat=True
            )
        )
        assert sequences == [1, 2, 3]
        assert _balance(main_store, rice).average_cost == Decimal("2333.333333")

    def test_case_15_reversal_of_an_issue_mirrors_it_exactly(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        issue = _post(organization, [_issue(main_store, rice, "4")], "k2")
        # Move the average, so "mirrors the original" and "uses today's
        # average" would give different answers.
        _post(organization, [_receipt(main_store, rice, "6", "5000")], "k3")

        reverse_stock_entry(entry=issue, idempotency_key="rev", reason="صرف خاطئ")

        mirrored = StockMovement.objects.order_by("-posted_sequence").first()
        assert mirrored is not None
        assert mirrored.base_quantity == Decimal("4.000")
        assert mirrored.inventory_value == Decimal("4000.000")

    def test_case_16_every_rule_applies_per_lot(
        self,
        organization: Organization,
        main_store: Warehouse,
        lot_item: InventoryItem,
        lot: InventoryLot,
        open_period: Any,
    ) -> None:
        _post(organization, [_receipt(main_store, lot_item, "10", "1000", lot=lot)], "k1")
        with pytest.raises(ValidationError) as refused:
            _post(organization, [_issue(main_store, lot_item, "11", lot=lot)], "k2")
        assert refused.value.code == "insufficient_stock"


class TestArchivingAWarehouseThatHoldsStock:
    """
    A gap found while reviewing the read screens against the services.

    Custody scope covers **active** warehouses, so archiving one that still
    holds goods made real stock vanish from every screen and every reorder
    report while its value stayed on the books and on the general ledger.
    """

    def test_it_is_refused_while_stock_remains(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        from apps.inventory.services import update_warehouse

        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")

        with pytest.raises(ValidationError) as refused:
            update_warehouse(warehouse=main_store, name_ar=main_store.name_ar, is_active=False)
        assert refused.value.code == "warehouse_still_holds_stock"

        main_store.refresh_from_db()
        assert main_store.is_active is True

    def test_and_permitted_once_the_stock_has_gone(
        self,
        organization: Organization,
        main_store: Warehouse,
        rice: InventoryItem,
        open_period: Any,
    ) -> None:
        from apps.inventory.services import update_warehouse

        _post(organization, [_receipt(main_store, rice, "10", "1000")], "k1")
        _post(organization, [_issue(main_store, rice, "10")], "k2")

        update_warehouse(warehouse=main_store, name_ar=main_store.name_ar, is_active=False)
        main_store.refresh_from_db()
        assert main_store.is_active is False

    def test_a_warehouse_that_never_held_anything_archives_freely(
        self, main_store: Warehouse
    ) -> None:
        from apps.inventory.services import update_warehouse

        update_warehouse(warehouse=main_store, name_ar=main_store.name_ar, is_active=False)
        main_store.refresh_from_db()
        assert main_store.is_active is False


class TestTheSupplierReturnMovementType:
    """
    `RETURN_OUT`, reserved by Task 1.0's movement table and delivered at Task
    2.13 with the document that produces it.

    The kernel owns the movement and nothing else: there is no supplier-return
    document in this module, and `apps.procurement` is the only caller. These
    tests are what say the value is wired correctly rather than merely present.
    """

    def test_it_is_outbound_and_is_not_return_in(self) -> None:
        """
        PRC-047, the whole of it. `RETURN_IN` is stock coming back from a
        kitchen to a store at the cost it was issued at; `RETURN_OUT` is stock
        leaving the business at the standing average. Opposite directions, two
        different reports, and one shared value would make each report wrong
        about the other.
        """
        from apps.inventory.models import INBOUND_MOVEMENT_TYPES, OUTBOUND_MOVEMENT_TYPES

        # Compared as stored values: mypy rightly refuses `A != B` on two
        # distinct enum literals, and the claim worth making is about the
        # closed set the database holds anyway.
        assert {MovementType.RETURN_OUT.value, MovementType.RETURN_IN.value} <= set(
            MovementType.values
        )
        assert MovementType.RETURN_OUT.value != MovementType.RETURN_IN.value
        assert MovementType.RETURN_OUT in OUTBOUND_MOVEMENT_TYPES
        assert MovementType.RETURN_OUT not in INBOUND_MOVEMENT_TYPES
        assert MovementType.RETURN_IN in INBOUND_MOVEMENT_TYPES

    def test_it_carries_its_own_sign_and_needs_no_direction(self) -> None:
        """An outbound type states its direction; only a signless one asks."""
        from apps.inventory.models import SIGNLESS_MOVEMENT_TYPES

        assert MovementType.RETURN_OUT not in SIGNLESS_MOVEMENT_TYPES

    def test_it_may_take_an_expired_lot_off_the_shelf(self) -> None:
        """
        A decision recorded rather than assumed — Task 2.0 §10 is silent.

        Goods that arrived spoiled or too near their date are among the
        commonest things a restaurant sends back. Refusing to move an expired
        lot would force the storekeeper to waste it instead, destroying a
        legitimate claim against the supplier in order to obey a rule written
        to keep expired food out of a kitchen. Nothing here reaches a kitchen.
        """
        from apps.inventory.ledger import EXPIRED_LOT_REMOVAL_TYPES

        assert MovementType.RETURN_OUT in EXPIRED_LOT_REMOVAL_TYPES
        assert MovementType.ISSUE not in EXPIRED_LOT_REMOVAL_TYPES

    def test_this_module_defines_no_supplier_return_document(self) -> None:
        """
        The movement lives here; the document that causes it does not. A
        supplier return needs a supplier, a delivery and a payable, none of
        which inventory knows about — Task 1.7 recorded exactly that when it
        moved this work to Phase 2.
        """
        from django.apps import apps as django_apps

        names = {model.__name__ for model in django_apps.get_app_config("inventory").get_models()}
        assert "SupplierReturn" not in names
        assert "SupplierReturnLine" not in names

    def test_inventory_never_imports_procurement(self) -> None:
        """
        The dependency runs one way. Procurement orchestrates inventory through
        its public services; inventory knowing about suppliers would make the
        kernel unusable by any other module.
        """
        import ast
        from pathlib import Path

        import apps.inventory as inventory_package

        assert inventory_package.__file__ is not None
        root = Path(inventory_package.__file__).parent
        offenders: list[str] = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "apps.procurement"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.Import) and any(
                    alias.name.startswith("apps.procurement") for alias in node.names
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        # Parsed rather than grepped: a prose mention of the module in a
        # docstring is not a dependency, and a test that could not tell the
        # difference would fail on its own explanation.
        assert offenders == []
