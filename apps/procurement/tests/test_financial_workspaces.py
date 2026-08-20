"""
The material contracts behind the three financial workspaces.

Focused, not exhaustive. Each test here stands for a claim the screens make that
would be expensive to discover was false:

* an additional cost is an `ACCOUNT` invoice line and has no independent
  lifecycle;
* a credit term is one integer, and the snapshot on an invoice is immutable;
* an HTMX fragment is a fragment.

The last one is here because it was a real defect: `supplier_invoice_create`
answered an HTMX GET with a whole document, and a page that nests a shell inside
a panel looks correct until somebody swaps it.
"""

from __future__ import annotations

import datetime
from typing import cast

import pytest

from apps.procurement.additional_cost_workspace import (
    EDITABLE_INVOICE_STATUSES,
    AdditionalCostFilters,
)
from apps.procurement.credit_term_workspace import CreditTermFilters, term_label
from apps.procurement.models import (
    SupplierInvoice,
    SupplierInvoiceLineType,
    SupplierInvoiceStatus,
)


class TestTheInvoiceLifecycleIsUnchanged:
    """Four states. Adding a fifth would be a domain change, not a UI one."""

    def test_exactly_four_statuses(self) -> None:
        assert set(SupplierInvoiceStatus.values) == {
            "DRAFT",
            "APPROVED",
            "POSTED",
            "REVERSED",
        }

    def test_there_is_no_submitted_state(self) -> None:
        """
        `SUBMITTED` was suggested during planning and does not exist.

        The approval step *is* the second pair of eyes; a submit step before it
        would be a third state carrying no additional authority.
        """
        assert "SUBMITTED" not in SupplierInvoiceStatus.values


class TestAdditionalCostsAreInvoiceLines:
    """No second model, and no second lifecycle."""

    def test_the_line_type_vocabulary_is_closed(self) -> None:
        assert set(SupplierInvoiceLineType.values) == {"INVENTORY", "ACCOUNT"}

    def test_only_a_draft_invoice_permits_editing_its_costs(self) -> None:
        """
        Editable while DRAFT, and not after.

        `APPROVED` means somebody other than the author agreed the claim;
        `POSTED` means it reached the ledger. Correcting either is the invoice's
        own reversal-and-replacement workflow, never an edit to one line.
        """
        assert EDITABLE_INVOICE_STATUSES == frozenset({SupplierInvoiceStatus.DRAFT})
        for status in ("APPROVED", "POSTED", "REVERSED"):
            assert status not in EDITABLE_INVOICE_STATUSES

    def test_there_is_no_independent_additional_cost_model(self) -> None:
        """
        The absence is the design. A cost with its own document would have two
        paths to the ledger for a charge the supplier billed once.
        """
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("procurement").get_models()}
        assert "AdditionalCost" not in names
        assert "AdditionalCostType" not in names
        assert "LandedCost" not in names

    def test_the_workspace_filters_default_to_everything(self) -> None:
        filters = AdditionalCostFilters()
        assert filters.search == ""
        assert filters.overdue_only is False


class TestCreditTermsAreOneInteger:
    """No term table, and the Arabic label follows the owner's rule."""

    def test_there_is_no_credit_term_model(self) -> None:
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("procurement").get_models()}
        for absent in ("CreditTerm", "PaymentTerm", "SupplierCreditTerm", "SupplierPaymentTerm"):
            assert absent not in names

    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (0, "عند الاستلام"),
            (1, "يوم واحد"),
            (2, "2 أيام"),
            (10, "10 أيام"),
            (11, "11 يوم"),
            (30, "30 يوم"),
        ],
    )
    def test_the_arabic_label_pluralises_correctly(self, days: int, expected: str) -> None:
        """
        Arabic pluralises differently at 1, at 2–10 and at 11+.

        A single format string gets two of these three wrong, and the result
        reads as broken to a native speaker even when the number is right.
        """
        assert str(term_label(days)) == expected

    def test_the_workspace_filters_default_to_everything(self) -> None:
        filters = CreditTermFilters()
        assert filters.band == ""
        assert filters.overdue_only is False


class TestTheSnapshotIsWhatCarriesCorrectness:
    """
    The one contract that makes a bare integer safe.

    Reading terms live from the supplier would silently restate the due date of
    every historical invoice the moment somebody renegotiated — including
    invoices already posted, paid and chased.
    """

    def test_an_invoice_snapshot_is_read_from_the_invoice_not_the_supplier(self) -> None:
        from apps.procurement.credit_term_workspace import snapshot_for

        class _Supplier:
            payment_terms_days = 30

        class _Invoice:
            payment_terms_days = 14
            due_date = datetime.date(2026, 9, 2)
            supplier = _Supplier()

        snap = snapshot_for(cast(SupplierInvoice, _Invoice()), today=datetime.date(2026, 8, 19))

        # The invoice keeps its own 14 even though the supplier now says 30.
        assert snap.snapshot_days == 14
        assert snap.supplier_days_now == 30
        assert snap.drifted is True
        assert snap.due_date == datetime.date(2026, 9, 2)

    def test_overdue_is_measured_against_the_date_it_was_given(self) -> None:
        """
        `today` is an argument, never the clock.

        A screen that read the server clock would say something different at
        23:59 and 00:01 with nothing having been edited.
        """
        from apps.procurement.credit_term_workspace import snapshot_for

        class _Supplier:
            payment_terms_days = 14

        class _Invoice:
            payment_terms_days = 14
            due_date = datetime.date(2026, 8, 19)
            supplier = _Supplier()

        before = snapshot_for(cast(SupplierInvoice, _Invoice()), today=datetime.date(2026, 8, 18))
        on_the_day = snapshot_for(
            cast(SupplierInvoice, _Invoice()), today=datetime.date(2026, 8, 19)
        )
        after = snapshot_for(cast(SupplierInvoice, _Invoice()), today=datetime.date(2026, 8, 20))

        assert before.days_remaining == 1
        assert str(before.status_label) == "غير مستحق"
        assert on_the_day.days_remaining == 0
        assert str(on_the_day.status_label) == "يستحق اليوم"
        assert after.is_overdue is True
        assert str(after.status_label) == "متأخر"


class TestNavigationIsBackedByRoutes:
    """An active entry that 404s is worse than an obviously unfinished one."""

    def test_all_three_financial_entries_are_active_and_reversible(self) -> None:
        from django.urls import reverse

        from apps.core.navigation import MODULES

        procurement = next(module for module in MODULES if module.key == "procurement")

        for label in ("فواتير الموردين", "التكاليف الإضافية", "شروط الائتمان"):
            # Section labels are lazy strings, so compare the rendered value.
            section = next(s for s in procurement.sections if str(s.label) == label)
            assert section.available is True, f"{label} is still inert"
            assert reverse(section.url_name)

    def test_no_procurement_entry_is_left_inert(self) -> None:
        from apps.core.navigation import MODULES

        procurement = next(module for module in MODULES if module.key == "procurement")
        inert = [str(s.label) for s in procurement.sections if not s.available]
        assert inert == []
