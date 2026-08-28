"""
Task 2.16 — the twelve reports and the module's GL tie-out.

The reports are reads over the same documents the verifiers prove, so the
tests hold them to the verifiers' standard: exact Decimals, scope before
filters, cost columns *omitted* — never blanked — without
`procurement.view_supplier_cost`, and a CSV that shows precisely what its
screen shows. `verify_procurement_accounting` (PRC-058) is tested the way
every verifier here is tested: green on a scenario built through the real
services, loud on a journal planted behind the services' back, and
repair-free either way.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
    PURCHASE_PRICE_VARIANCE,
    SUPPLIER_ADVANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_PAYMENT_BANK,
    SUPPLIER_RETURN_CLEARING,
    Account,
    AccountRole,
    CostCenter,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.models import InventoryItem, ItemType, Warehouse
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_organization_access
from apps.procurement import reports
from apps.procurement.credit_notes import (
    add_return_allocation,
    create_supplier_credit_note,
    post_supplier_credit_note,
)
from apps.procurement.invoices import (
    add_account_line,
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    post_supplier_invoice,
    supplier_outstanding,
)
from apps.procurement.matching import add_allocation, create_purchase_match, mark_match_ready
from apps.procurement.models import (
    GoodsReceipt,
    PurchaseOrder,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
    SupplierReturn,
)
from apps.procurement.payments import (
    add_payment_allocation,
    create_supplier_payment,
    post_supplier_payment,
)
from apps.procurement.posting import post_goods_receipt
from apps.procurement.reconciliation import verify_procurement_accounting
from apps.procurement.reports import ProcurementReportFilters
from apps.procurement.returns import (
    add_return_line,
    create_supplier_return,
    post_supplier_return,
)
from apps.procurement.services import (
    add_order_line,
    add_receipt_line,
    approve_purchase_order,
    create_goods_receipt,
    create_purchase_order,
    create_supplier,
    inspect_receipt_line,
    issue_purchase_order,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
PASSWORD = "pw-not-real-1234"

PAYABLE_CODE = "2-01-01-001"
INVENTORY_CODE = "1-03-01-001"
GRNI_CODE = "2-01-02-001"
BANK_CODE = "1-01-02-001"
ADVANCE_CODE = "1-04-01-001"
PPV_CODE = "8-01-03-001"
CLEARING_CODE = "8-01-04-001"
EXPENSE_CODE = "5-01-02-003"

ZERO = Decimal("0.000")

ROUTE_NAMES = (
    "report_supplier_aging",
    "report_supplier_statement",
    "report_open_purchase_orders",
    "report_outstanding_receipts",
    "report_grni_exceptions",
    "report_invoice_without_receipt",
    "report_matching_exceptions",
    "report_purchase_spend",
    "report_price_variance",
    "report_return_credit_status",
    "report_payment_allocations",
    "report_procurement_to_gl",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def units() -> None:
    call_command("seed_units", verbosity=0)


@pytest.fixture
def kilogram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="KG")


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    """Every role any document in these scenarios resolves."""
    for code, account_code in (
        (INVENTORY_CONTROL, INVENTORY_CODE),
        (GOODS_RECEIVED_NOT_INVOICED, GRNI_CODE),
        (SUPPLIER_PAYABLE, PAYABLE_CODE),
        (SUPPLIER_PAYMENT_BANK, BANK_CODE),
        (SUPPLIER_ADVANCE, ADVANCE_CODE),
        (PURCHASE_PRICE_VARIANCE, PPV_CODE),
        (SUPPLIER_RETURN_CLEARING, CLEARING_CODE),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=JAN_1,
        )


@pytest.fixture
def rice(organization: Organization, kilogram: UnitOfMeasure) -> InventoryItem:
    from apps.inventory.services import create_item, create_item_category

    return create_item(
        organization=organization,
        code="RICE",
        name="رز",
        category=create_item_category(organization=organization, code="GRAINS", name="حبوب"),
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )


@pytest.fixture
def store(branch: Branch) -> Warehouse:
    from apps.inventory.services import create_warehouse

    return create_warehouse(branch=branch, code="MAIN", name="مخزن")


@pytest.fixture
def grocery(organization: Organization, mapped: None) -> Supplier:
    return create_supplier(organization=organization, code="GROC-01", name="مورد")


@pytest.fixture
def org_manager(organization: Organization) -> User:
    user = User.objects.create_user(username="org-manager", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def controller(organization: Organization) -> User:
    user = User.objects.create_user(username="controller", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


def _today() -> datetime.date:
    return timezone.localdate()


def _posted_expense_invoice(
    *,
    organization: Organization,
    supplier: Supplier,
    branch: Branch,
    actor: User,
    amount: str,
    reference: str,
    invoice_date: datetime.date,
) -> SupplierInvoice:
    invoice = create_supplier_invoice(
        supplier=supplier,
        branch=branch,
        created_by=actor,
        supplier_invoice_number=reference,
        invoice_date=invoice_date,
    )
    add_account_line(
        invoice=invoice,
        account=Account.objects.get(organization=organization, code=EXPENSE_CODE),
        cost_center=CostCenter.objects.filter(organization=organization).first(),
        description="أجور نقل",
        quantity=Decimal("1.000"),
        unit_price=Decimal(amount),
    )
    approve_supplier_invoice(invoice=invoice, actor=actor)
    return post_supplier_invoice(invoice=invoice, actor=actor)


@pytest.fixture
def money_docs(
    organization: Organization,
    grocery: Supplier,
    branch: Branch,
    keeper: User,
) -> dict[str, object]:
    """
    Three posted invoices in three aging buckets and one partly allocated
    payment: 20,000 due today, 50,000 due 15 days ago and fully paid,
    30,000 due 100 days ago — plus a 60,000 payment leaving 10,000 standing
    as an advance.
    """
    today = _today()
    current = _posted_expense_invoice(
        organization=organization,
        supplier=grocery,
        branch=branch,
        actor=keeper,
        amount="20000.000000",
        reference="INV-CURRENT",
        invoice_date=today,
    )
    paid = _posted_expense_invoice(
        organization=organization,
        supplier=grocery,
        branch=branch,
        actor=keeper,
        amount="50000.000000",
        reference="INV-PAID",
        invoice_date=today - datetime.timedelta(days=15),
    )
    old = _posted_expense_invoice(
        organization=organization,
        supplier=grocery,
        branch=branch,
        actor=keeper,
        amount="30000.000000",
        reference="INV-OLD",
        invoice_date=today - datetime.timedelta(days=100),
    )
    payment = create_supplier_payment(
        supplier=grocery,
        branch=branch,
        created_by=keeper,
        paid_at=today,
        method="BANK",
        amount=Decimal("60000.000"),
        reference="TRF-1",
    )
    add_payment_allocation(payment=payment, invoice=paid, allocated_amount=Decimal("50000.000"))
    payment = post_supplier_payment(payment=payment, actor=keeper)
    return {"current": current, "paid": paid, "old": old, "payment": payment}


@pytest.fixture
def issued_order(
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    org_manager: User,
    rice: InventoryItem,
) -> PurchaseOrder:
    """Fifty kilograms of rice at 1,400, sent to the supplier."""
    order = create_purchase_order(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=keeper,
        ordered_on=_today(),
    )
    add_order_line(
        order=order,
        item=rice,
        ordered_quantity=Decimal("50.000"),
        unit_price=Decimal("1400.000000"),
    )
    approve_purchase_order(order=order, actor=org_manager)
    return issue_purchase_order(order=order, actor=keeper)


@pytest.fixture
def partial_receipt(
    issued_order: PurchaseOrder,
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    rice: InventoryItem,
) -> GoodsReceipt:
    """Thirty of the fifty kilograms arrive and post: 42,000 into GRNI."""
    receipt = create_goods_receipt(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=keeper,
        received_at=_today() - datetime.timedelta(days=5),
        order=issued_order,
        delivery_reference="DN-1",
        evidence_reference="إشعار",
    )
    line = add_receipt_line(
        receipt=receipt,
        item=rice,
        delivered_quantity=Decimal("30.000"),
        order_line=issued_order.lines.get(),
    )
    inspect_receipt_line(line=line, accepted_base_quantity=Decimal("30.000"), actor=keeper)
    return post_goods_receipt(receipt=receipt, actor=keeper)


@pytest.fixture
def posted_return(
    partial_receipt: GoodsReceipt,
    keeper: User,
    org_manager: User,
) -> SupplierReturn:
    """Ten kilograms go back: a 14,000 claim standing against the supplier."""
    supplier_return = create_supplier_return(
        organization=partial_receipt.organization,
        branch=partial_receipt.branch,
        supplier=partial_receipt.supplier,
        warehouse=partial_receipt.warehouse,
        location=partial_receipt.location,
        created_by=keeper,
        returned_at=_today() - datetime.timedelta(days=3),
        reason="بضاعة تالفة",
        evidence_reference="وصل السائق",
    )
    add_return_line(
        supplier_return=supplier_return,
        item=partial_receipt.lines.get().item,
        lot=partial_receipt.lines.get().lot,
        returned_base_quantity=Decimal("10.000"),
    )
    return post_supplier_return(supplier_return=supplier_return, actor=org_manager)


def _filters(**kwargs: object) -> ProcurementReportFilters:
    return ProcurementReportFilters(**kwargs)  # type: ignore[arg-type]


def _by_key(rows: list[dict[str, object]], key: str) -> dict[object, dict[str, object]]:
    return {row[key]: row for row in rows}


# ---------------------------------------------------------------------------
# Access: entry permission, scope, redaction
# ---------------------------------------------------------------------------


class TestAccess:
    def test_every_report_route_answers_the_manager_and_refuses_the_floor(
        self,
        organization: Organization,
        grocery: Supplier,
        org_manager: User,
        keeper: User,
        cashier: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        The entry permission is `view_procurement_report`, held by the roles
        that reason about money — and by neither the storekeeper nor the
        cashier, whose screens are the documents they act on, not the
        module-wide totals.
        """
        allowed = client_for(org_manager)
        keeper_client = client_for(keeper)
        cashier_client = client_for(cashier)
        for name in ROUTE_NAMES:
            url = reverse(f"procurement:{name}")
            assert allowed.get(url).status_code == 200, name
            assert keeper_client.get(url).status_code == 403, name
            assert cashier_client.get(url).status_code == 403, name

    def test_a_hand_granted_permission_names_no_post_and_reaches_no_rows(
        self,
        organization: Organization,
        money_docs: dict[str, object],
        client_for: Callable[[User], Client],
    ) -> None:
        """
        ADR-016's two halves, separately visible. A permission attached
        directly to a user passes the global gate (200, not 403) but names
        no post over any organization, so the scope arm answers nothing:
        the same screen that shows a manager the supplier shows this caller
        an empty report. Meanwhile a *branch* manager sees the rows — the
        same reach `visible_supplier_invoices` certifies (PRC-060), because
        their post carries the permission and names the organization.
        """
        from django.contrib.auth.models import Permission

        loner = User.objects.create_user(username="loner", password=PASSWORD)
        loner.user_permissions.add(
            Permission.objects.get(
                codename="view_procurement_report",
                content_type__app_label="procurement",
            )
        )
        loner = User.objects.get(pk=loner.pk)

        assert reports.supplier_aging(loner, _filters(), include_cost=True) == []
        response = client_for(loner).get(reverse("procurement:report_supplier_aging"))
        assert response.status_code == 200
        assert response.context["total_rows"] == 0

    def test_a_branch_manager_sees_the_organization_rows(
        self,
        organization: Organization,
        money_docs: dict[str, object],
        manager: User,
    ) -> None:
        rows = reports.supplier_aging(manager, _filters(), include_cost=True)
        assert [row["supplier_code"] for row in rows] == ["GROC-01"]

    def test_naming_a_foreign_organization_returns_nothing_not_its_rows(
        self,
        money_docs: dict[str, object],
        other_organization: Organization,
        org_manager: User,
    ) -> None:
        """Scope first, filters second: a filter can narrow, never widen."""
        rows = reports.supplier_aging(
            org_manager,
            _filters(organization_id=other_organization.pk),
            include_cost=True,
        )
        assert rows == []

    def test_without_the_cost_permission_the_money_keys_are_absent(
        self,
        money_docs: dict[str, object],
        org_manager: User,
    ) -> None:
        """Omitted, not blanked — a key that is absent cannot leak."""
        rows = reports.supplier_aging(org_manager, _filters(), include_cost=False)
        assert rows, "the supplier has open documents and must appear"
        assert set(rows[0]) == {"supplier_code", "supplier_name"}

    def test_the_csv_of_a_costless_viewer_has_no_cost_columns(
        self,
        money_docs: dict[str, object],
        org_manager: User,
        client_for: Callable[[User], Client],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The screen and the export are the same call, so proving the export
        clean proves the screen clean. The cost permission is withheld by
        patching the view's own switch: every role that reads reports today
        also reads costs, and the redaction must not quietly rot if that
        pairing ever changes.
        """
        from apps.procurement import report_views

        monkeypatch.setattr(
            report_views.ProcurementReportView,
            "include_valuation",
            property(lambda self: False),
        )
        response = client_for(org_manager).get(
            reverse("procurement:report_supplier_aging") + "?export=csv"
        )
        body = response.getvalue().decode("utf-8-sig")
        assert "GROC-01" in body
        assert "إجمالي المفتوح" not in body
        assert "20000" not in body


# ---------------------------------------------------------------------------
# The money reports
# ---------------------------------------------------------------------------


class TestMoneyReports:
    def test_aging_buckets_credit_and_advances_in_one_row(
        self,
        organization: Organization,
        money_docs: dict[str, object],
        org_manager: User,
    ) -> None:
        rows = reports.supplier_aging(org_manager, _filters(), include_cost=True)
        row = _by_key(rows, "supplier_code")["GROC-01"]
        assert row["current"] == Decimal("20000.000")
        assert row["d30"] == ZERO  # fully paid, so nothing stands in its bucket
        assert row["older"] == Decimal("30000.000")
        assert row["open_total"] == Decimal("50000.000")
        assert row["standing_credit"] == ZERO
        assert row["advances"] == Decimal("10000.000")
        assert row["net_position"] == Decimal("50000.000")

    def test_the_statement_runs_the_balance_the_way_the_ledger_would(
        self,
        organization: Organization,
        money_docs: dict[str, object],
        org_manager: User,
    ) -> None:
        """
        All four documents share one business date, so this is the journal's
        chronology doing the ordering: the three invoices in the order they
        posted, then the payment that settled one of them.
        """
        rows = reports.supplier_statement(org_manager, _filters(), include_cost=True)
        assert [row["balance"] for row in rows] == [
            Decimal("20000.000"),
            Decimal("70000.000"),
            Decimal("100000.000"),
            Decimal("50000.000"),
        ]
        payment_row = rows[-1]
        assert payment_row["document_kind"] == "دفعة"
        assert payment_row["settled"] == Decimal("50000.000")
        assert payment_row["advance"] == Decimal("10000.000")

    def test_payment_allocations_show_what_was_covered_and_what_stands(
        self,
        organization: Organization,
        money_docs: dict[str, object],
        org_manager: User,
    ) -> None:
        rows = reports.payment_allocations(org_manager, _filters(), include_cost=True)
        assert len(rows) == 1
        row = rows[0]
        paid_invoice = money_docs["paid"]
        assert isinstance(paid_invoice, SupplierInvoice)
        assert row["amount"] == Decimal("60000.000")
        assert row["allocated"] == Decimal("50000.000")
        assert row["advance"] == Decimal("10000.000")
        assert row["covered_invoices"] == paid_invoice.number

    def test_spend_sums_posted_invoices_by_supplier_and_month(
        self,
        organization: Organization,
        money_docs: dict[str, object],
        org_manager: User,
    ) -> None:
        rows = reports.purchase_spend(org_manager, _filters(), include_cost=True)
        assert sum((row["spend"] for row in rows), start=ZERO) == Decimal("100000.000")
        assert sum(int(row["invoice_count"]) for row in rows) == 3
        assert all(row["supplier_code"] == "GROC-01" for row in rows)


# ---------------------------------------------------------------------------
# Statement chronology
# ---------------------------------------------------------------------------


class TestStatementOrdering:
    """
    A statement is a chronology, and within one business date the journal is
    the only record of what happened first.

    These all post on ONE business date, so nothing but the ordering rule can
    separate the rows. Each names the answer an ordering by document *kind*
    (charges before settlements) would have given, because that was the first
    cut and it was wrong: the ledger is the authority, not a preference about
    how a statement reads.
    """

    ONE_DAY = datetime.date(TEST_YEAR, 5, 4)

    def _invoice(
        self,
        *,
        organization: Organization,
        supplier: Supplier,
        branch: Branch,
        actor: User,
        amount: str,
        reference: str,
    ) -> SupplierInvoice:
        invoice = create_supplier_invoice(
            supplier=supplier,
            branch=branch,
            created_by=actor,
            supplier_invoice_number=reference,
            invoice_date=self.ONE_DAY,
            business_date=self.ONE_DAY,
        )
        add_account_line(
            invoice=invoice,
            account=Account.objects.get(organization=organization, code=EXPENSE_CODE),
            cost_center=CostCenter.objects.filter(organization=organization).first(),
            description="أجور نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal(amount),
        )
        approve_supplier_invoice(invoice=invoice, actor=actor)
        return invoice

    def _pay(
        self,
        *,
        supplier: Supplier,
        branch: Branch,
        actor: User,
        amount: str,
        reference: str,
        invoice: SupplierInvoice | None = None,
        allocated: str | None = None,
    ) -> SupplierPayment:
        payment = create_supplier_payment(
            supplier=supplier,
            branch=branch,
            created_by=actor,
            paid_at=self.ONE_DAY,
            business_date=self.ONE_DAY,
            method="BANK",
            amount=Decimal(amount),
            reference=reference,
        )
        if invoice is not None and allocated is not None:
            add_payment_allocation(
                payment=payment, invoice=invoice, allocated_amount=Decimal(allocated)
            )
        return post_supplier_payment(payment=payment, actor=actor)

    def test_an_invoice_then_a_payment_reads_in_that_order(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        org_manager: User,
    ) -> None:
        invoice = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="20000.000000",
            reference="INV-A",
        )
        post_supplier_invoice(invoice=invoice, actor=keeper)
        self._pay(
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="20000.000",
            reference="TRF-A",
            invoice=invoice,
            allocated="20000.000",
        )
        rows = reports.supplier_statement(org_manager, _filters(), include_cost=True)
        assert [row["document_kind"] for row in rows] == ["فاتورة", "دفعة"]
        assert [row["balance"] for row in rows] == [Decimal("20000.000"), ZERO]

    def test_a_payment_before_an_invoice_reads_before_it(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        org_manager: User,
    ) -> None:
        """
        The case kind-precedence got wrong. Money left on the same day the
        bill arrived, and it left *first* — so the statement shows the
        advance standing before the charge that would later absorb it.
        Ordering by kind would have printed the invoice first and told a
        reader the payment settled a debt that did not yet exist.
        """
        self._pay(
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="15000.000",
            reference="TRF-EARLY",
        )
        invoice = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="15000.000000",
            reference="INV-LATE",
        )
        post_supplier_invoice(invoice=invoice, actor=keeper)

        rows = reports.supplier_statement(org_manager, _filters(), include_cost=True)
        assert [row["document_kind"] for row in rows] == ["دفعة", "فاتورة"]
        # Wholly unallocated, so it settles nothing and stands as an advance.
        assert rows[0]["settled"] == ZERO
        assert rows[0]["advance"] == Decimal("15000.000")
        assert [row["balance"] for row in rows] == [ZERO, Decimal("15000.000")]

    def test_a_payment_between_two_invoices_stays_between_them(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        org_manager: User,
    ) -> None:
        """
        Three same-day documents whose two orderings disagree at every row.
        Chronology: 20,000 charged, settled, then 30,000 charged — balances
        20,000 / 0 / 30,000. Kind precedence would print both invoices first
        and give 20,000 / 50,000 / 0, which is a debt the supplier never
        held.
        """
        first = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="20000.000000",
            reference="INV-FIRST",
        )
        post_supplier_invoice(invoice=first, actor=keeper)
        self._pay(
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="20000.000",
            reference="TRF-MID",
            invoice=first,
            allocated="20000.000",
        )
        second = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="30000.000000",
            reference="INV-SECOND",
        )
        post_supplier_invoice(invoice=second, actor=keeper)

        rows = reports.supplier_statement(org_manager, _filters(), include_cost=True)
        assert [row["document_kind"] for row in rows] == ["فاتورة", "دفعة", "فاتورة"]
        assert [row["balance"] for row in rows] == [
            Decimal("20000.000"),
            ZERO,
            Decimal("30000.000"),
        ]

    def test_a_credit_note_reads_where_it_posted_between_two_events(
        self,
        organization: Organization,
        posted_return: SupplierReturn,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        controller: User,
        org_manager: User,
    ) -> None:
        """A note posted second reads second, not sorted to the credit block."""
        first = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="40000.000000",
            reference="INV-BEFORE-NOTE",
        )
        post_supplier_invoice(invoice=first, actor=keeper)

        note = create_supplier_credit_note(
            supplier_return=posted_return,
            created_by=keeper,
            supplier_document_number="SCN-MID",
            credit_date=self.ONE_DAY,
            business_date=self.ONE_DAY,
            amount=Decimal("14000.000"),
        )
        add_return_allocation(
            credit_note=note,
            return_line=posted_return.lines.get(),
            credited_base_quantity=Decimal("10.000"),
            allocated_credit_amount=Decimal("14000.000"),
        )
        post_supplier_credit_note(credit_note=note, actor=controller)

        second = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="5000.000000",
            reference="INV-AFTER-NOTE",
        )
        post_supplier_invoice(invoice=second, actor=keeper)

        rows = reports.supplier_statement(org_manager, _filters(), include_cost=True)
        assert [row["document_kind"] for row in rows] == ["فاتورة", "إشعار دائن", "فاتورة"]
        assert [row["balance"] for row in rows] == [
            Decimal("40000.000"),
            Decimal("26000.000"),
            Decimal("31000.000"),
        ]

    def test_the_journal_orders_the_rows_even_when_the_primary_keys_disagree(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        org_manager: User,
    ) -> None:
        """
        Two drafts created in one order and posted in the other. The row with
        the *lower* primary key posted second and must read second — a sort
        key reading `pk` (or a per-document number sequence, which is drawn
        at posting) would put them the other way round.
        """
        earlier_pk = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="11000.000000",
            reference="INV-LOW-PK",
        )
        later_pk = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="22000.000000",
            reference="INV-HIGH-PK",
        )
        assert earlier_pk.pk < later_pk.pk

        post_supplier_invoice(invoice=later_pk, actor=keeper)
        post_supplier_invoice(invoice=earlier_pk, actor=keeper)

        rows = reports.supplier_statement(org_manager, _filters(), include_cost=True)
        assert [row["charged"] for row in rows] == [Decimal("22000.000"), Decimal("11000.000")]
        assert [row["balance"] for row in rows] == [Decimal("22000.000"), Decimal("33000.000")]

    def test_the_closing_balance_is_the_supplier_position_however_rows_are_read(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        org_manager: User,
    ) -> None:
        """
        Ordering decides how the balance *travels*, never where it ends. The
        closing figure equals `supplier_outstanding` — the same derivation
        `verify_procurement_accounting` proves against the payable account —
        and equals the sum of the row movements in any order.
        """
        invoice = self._invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="80000.000000",
            reference="INV-CLOSE",
        )
        post_supplier_invoice(invoice=invoice, actor=keeper)
        self._pay(
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="30000.000",
            reference="TRF-CLOSE",
            invoice=invoice,
            allocated="30000.000",
        )
        rows = reports.supplier_statement(org_manager, _filters(), include_cost=True)
        movements = sum((row["charged"] - row["settled"] for row in rows), start=ZERO)

        assert rows[-1]["balance"] == Decimal("50000.000")
        assert rows[-1]["balance"] == movements
        assert rows[-1]["balance"] == supplier_outstanding(grocery)
        assert verify_procurement_accounting(organization) == []

        # Reading the same movements in any other order reaches the same
        # place: a running balance is a presentation of a sum, not the sum.
        for permutation in (list(reversed(rows)), sorted(rows, key=lambda row: row["number"])):
            assert (
                sum((row["charged"] - row["settled"] for row in permutation), start=ZERO)
                == movements
            )


# ---------------------------------------------------------------------------
# The goods reports
# ---------------------------------------------------------------------------


class TestGoodsReports:
    def test_open_orders_show_the_undelivered_remainder(
        self,
        organization: Organization,
        partial_receipt: GoodsReceipt,
        org_manager: User,
    ) -> None:
        rows = reports.open_purchase_orders(org_manager, _filters(), include_cost=True)
        assert len(rows) == 1
        row = rows[0]
        assert row["ordered"] == Decimal("50.000")
        assert row["received"] == Decimal("30.000")
        assert row["outstanding"] == Decimal("20.000")
        assert row["unit_price"] == Decimal("1400.000000")

        totals = reports.outstanding_receipt_quantity(org_manager, _filters(), include_cost=True)
        assert _by_key(totals, "item_code")["RICE"]["outstanding"] == Decimal("20.000")

    def test_grni_exceptions_age_the_uninvoiced_delivery(
        self,
        organization: Organization,
        partial_receipt: GoodsReceipt,
        org_manager: User,
    ) -> None:
        rows = reports.grni_exceptions(org_manager, _filters(), include_cost=True)
        assert len(rows) == 1
        row = rows[0]
        assert row["accepted_quantity"] == Decimal("30.000")
        assert row["uninvoiced_value"] == Decimal("42000.000")
        assert row["age_days"] == 5

    def test_the_matching_lifecycle_moves_a_variance_between_three_reports(
        self,
        organization: Organization,
        partial_receipt: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        rice: InventoryItem,
        keeper: User,
        controller: User,
        org_manager: User,
    ) -> None:
        """
        One 3,000 difference, three homes. Unmatched, the invoice sits in
        "billed, never delivered". Matched but unposted, the difference is a
        pending decision in matching exceptions. Posted, it leaves both and
        becomes a line in the posted price-variance report — and the GRNI
        exception the delivery had been raising is gone, because the
        delivery is now invoiced.
        """
        invoice = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=keeper,
            supplier_invoice_number="INV-RICE",
            invoice_date=_today(),
        )
        add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("30.000"),
            unit_price=Decimal("1500.000000"),
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)

        unmatched = reports.invoice_without_receipt(org_manager, _filters(), include_cost=True)
        assert [row["invoice_number"] for row in unmatched] == ["INV-RICE"]
        assert unmatched[0]["line_amount"] == Decimal("45000.000")

        match = create_purchase_match(invoice=invoice, created_by=controller)
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=partial_receipt.lines.get(),
            matched_base_quantity=Decimal("30.000"),
            created_by=controller,
        )
        mark_match_ready(match=match, actor=controller)

        assert reports.invoice_without_receipt(org_manager, _filters(), include_cost=True) == []
        pending = reports.matching_exceptions(org_manager, _filters(), include_cost=True)
        assert len(pending) == 1
        assert pending[0]["price_variance"] == Decimal("3000.000")
        assert reports.price_variance(org_manager, _filters(), include_cost=True) == []

        post_supplier_invoice(invoice=invoice, actor=controller)

        assert reports.matching_exceptions(org_manager, _filters(), include_cost=True) == []
        posted = reports.price_variance(org_manager, _filters(), include_cost=True)
        assert len(posted) == 1
        assert posted[0]["receipt_value"] == Decimal("42000.000")
        assert posted[0]["invoice_value"] == Decimal("45000.000")
        assert posted[0]["price_variance"] == Decimal("3000.000")
        assert reports.grni_exceptions(org_manager, _filters(), include_cost=True) == []

    def test_return_credit_status_walks_from_standing_to_partial(
        self,
        organization: Organization,
        posted_return: SupplierReturn,
        keeper: User,
        controller: User,
        org_manager: User,
    ) -> None:
        rows = reports.return_credit_status(org_manager, _filters(), include_cost=True)
        assert len(rows) == 1
        assert rows[0]["state"] == "قائم"
        assert rows[0]["book_value"] == Decimal("14000.000")
        assert rows[0]["open_claim"] == Decimal("14000.000")

        note = create_supplier_credit_note(
            supplier_return=posted_return,
            created_by=keeper,
            supplier_document_number="SCN-1",
            credit_date=_today(),
            amount=Decimal("5600.000"),
        )
        add_return_allocation(
            credit_note=note,
            return_line=posted_return.lines.get(),
            credited_base_quantity=Decimal("4.000"),
            allocated_credit_amount=Decimal("5600.000"),
        )
        post_supplier_credit_note(credit_note=note, actor=controller)

        rows = reports.return_credit_status(org_manager, _filters(), include_cost=True)
        assert rows[0]["state"] == "جزئي"
        assert rows[0]["settled_value"] == Decimal("5600.000")
        assert rows[0]["open_claim"] == Decimal("8400.000")


# ---------------------------------------------------------------------------
# The GL tie-out — report and verifier together (PRC-058)
# ---------------------------------------------------------------------------


class TestProcurementToGl:
    def test_a_clean_scenario_reconciles_and_says_so(
        self,
        organization: Organization,
        money_docs: dict[str, object],
        org_manager: User,
    ) -> None:
        assert verify_procurement_accounting(organization) == []
        rows = reports.procurement_to_gl(org_manager, _filters(), include_cost=True)
        assert len(rows) == 3
        assert all(row["state"] == "مطابق" for row in rows)
        assert all(row["organization"] == "KM" for row in rows)

    def test_a_planted_journal_is_reported_by_verifier_and_report_alike(
        self,
        organization: Organization,
        branch: Branch,
        money_docs: dict[str, object],
        org_manager: User,
    ) -> None:
        """
        No repair mode. A journal claiming to be a supplier payment that does
        not exist moves the payable and cites a ghost; the verifier names
        both, the report shows both, and the row is left exactly where it is.
        """
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine

        post_entry(
            organization=organization,
            accounting_date=_today(),
            lines=[
                PostingLine(
                    account=Account.objects.get(organization=organization, code=PAYABLE_CODE),
                    branch=branch,
                    debit=Decimal("1.000"),
                ),
                PostingLine(
                    account=Account.objects.get(organization=organization, code=BANK_CODE),
                    branch=branch,
                    credit=Decimal("1.000"),
                ),
            ],
            idempotency_key="planted-payment-entry",
            source_document_type="PROCUREMENT_SUPPLIER_PAYMENT",
            source_document_id="00000000-0000-0000-0000-000000000000",
            source_event="POSTED",
        )
        problems = verify_procurement_accounting(organization)
        fields = {problem.field for problem in problems}
        assert "journal_cites_unknown_document" in fields
        assert "open_balances_vs_payable_account" in fields

        rows = reports.procurement_to_gl(org_manager, _filters(), include_cost=True)
        states = {row["check"]: row["state"] for row in rows}
        assert states["أرصدة الموردين المفتوحة مقابل حساب الذمم"] == "غير مطابق"
        assert states["كل قيد شراء يقتفي مستنداً واحداً"] == "غير مطابق"


# ---------------------------------------------------------------------------
# The export contract
# ---------------------------------------------------------------------------


class TestExports:
    def test_the_csv_shows_the_screen_and_neutralises_a_hostile_name(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        org_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        §E's contract, inherited intact: the same call builds the screen and
        the file, and a supplier whose name begins with a formula trigger
        reaches the spreadsheet as text, not as code on the reader's machine.
        """
        hostile = create_supplier(organization=organization, code="EVIL-01", name="=cmd|احتيال")
        _posted_expense_invoice(
            organization=organization,
            supplier=hostile,
            branch=branch,
            actor=keeper,
            amount="1000.000000",
            reference="INV-EVIL",
            invoice_date=_today(),
        )
        response = client_for(org_manager).get(
            reverse("procurement:report_supplier_aging") + "?export=csv"
        )
        assert response.status_code == 200
        assert response["Content-Disposition"].startswith('attachment; filename="supplier-aging-')
        body = response.getvalue().decode("utf-8-sig")
        assert "'=cmd|احتيال" in body
        assert "\n=cmd" not in body and ",=cmd" not in body

    def test_the_htmx_request_gets_the_fragment_not_the_shell(
        self,
        organization: Organization,
        grocery: Supplier,
        org_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        client = client_for(org_manager)
        url = reverse("procurement:report_supplier_aging")
        full = client.get(url)
        fragment = client.get(url, HX_REQUEST="true", headers={"HX-Request": "true"})
        assert b"<html" in full.content
        assert b"<html" not in fragment.content

    def test_the_supplier_filter_narrows_every_figure_to_that_supplier(
        self,
        organization: Organization,
        money_docs: dict[str, object],
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        org_manager: User,
    ) -> None:
        other = create_supplier(organization=organization, code="MEAT-01", name="مورد لحم")
        _posted_expense_invoice(
            organization=organization,
            supplier=other,
            branch=branch,
            actor=keeper,
            amount="7000.000000",
            reference="INV-MEAT",
            invoice_date=_today(),
        )
        rows = reports.supplier_aging(
            org_manager, _filters(supplier_id=grocery.pk), include_cost=True
        )
        assert [row["supplier_code"] for row in rows] == ["GROC-01"]
        statement = reports.supplier_statement(
            org_manager, _filters(supplier_id=other.pk), include_cost=True
        )
        assert [row["supplier_code"] for row in statement] == ["MEAT-01"]
        assert statement[0]["balance"] == Decimal("7000.000")
