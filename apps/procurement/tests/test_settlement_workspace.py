"""
تسديد الموردين — the screen that decides what to pay, and the draft it produces.

The plans' arithmetic is tested next door, over plain `(invoice, amount)`
pairs, because where a plan stops is a question about numbers. This module
tests the two things that only exist once real invoices do: that confirming a
plan produces the **ordinary** supplier payment with FIFO allocations already
on it, and that the screen refuses to act on figures that have moved since it
was read.

What is deliberately not tested here is posting. Confirming a plan drafts;
somebody holding `post_supplier_payment` posts, on the payment screen, and
that path already has its own tests. The split is the point — a settlement
screen that posted its own money would have collapsed maker-checker into one
click.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
    SUPPLIER_ADVANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_PAYMENT_BANK,
    SUPPLIER_PAYMENT_CASH,
    Account,
    AccountRole,
    CostCenter,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_organization_access
from apps.procurement.invoices import (
    add_account_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    outstanding_amount,
    post_supplier_invoice,
)
from apps.procurement.models import (
    Supplier,
    SupplierInvoice,
    SupplierPayment,
    SupplierPaymentStatus,
)
from apps.procurement.payments import (
    add_payment_allocation,
    draft_settlement,
    post_supplier_payment,
)
from apps.procurement.services import create_supplier
from apps.procurement.settlement import PlanKind, workspace_for
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
PAID = datetime.date(TEST_YEAR, 3, 20)
PASSWORD = "pw-not-real-1234"

EXPENSE_CODE = "5-01-02-003"


@pytest.fixture
def units() -> None:
    call_command("seed_units", verbosity=0)


@pytest.fixture
def accounting(organization: Organization, units: None) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    for role_code, account_code in (
        (INVENTORY_CONTROL, "1-03-01-001"),
        (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
        (SUPPLIER_PAYABLE, "2-01-01-001"),
        (SUPPLIER_PAYMENT_CASH, "1-01-01-001"),
        (SUPPLIER_PAYMENT_BANK, "1-01-02-001"),
        (SUPPLIER_ADVANCE, "1-04-01-001"),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=role_code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=JAN_1,
        )


@pytest.fixture
def grocery(organization: Organization, mapped: None) -> Supplier:
    """Thirty-day terms, and half of it promised by the due date."""
    return create_supplier(
        organization=organization,
        code="GROC-01",
        name="مورد البقالة",
        payment_terms_days=30,
        minimum_settlement_percent=Decimal("50"),
    )


@pytest.fixture
def buyer(organization: Organization) -> User:
    user = User.objects.create_user(username="buyer", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


def _invoice(
    *,
    organization: Organization,
    supplier: Supplier,
    branch: Branch,
    actor: User,
    amount: str,
    reference: str,
    on: datetime.date,
) -> SupplierInvoice:
    invoice = create_supplier_invoice(
        supplier=supplier,
        branch=branch,
        created_by=actor,
        supplier_invoice_number=reference,
        invoice_date=on,
    )
    add_account_line(
        invoice=invoice,
        account=Account.objects.get(organization=organization, code=EXPENSE_CODE),
        cost_center=CostCenter.objects.filter(organization=organization).first(),
        description="بضاعة",
        quantity=Decimal("1.000"),
        unit_price=Decimal(amount),
    )
    approve_supplier_invoice(invoice=invoice, actor=actor)
    return post_supplier_invoice(invoice=invoice, actor=actor)


@pytest.fixture
def ledger(
    organization: Organization, grocery: Supplier, branch: Branch, buyer: User
) -> list[SupplierInvoice]:
    """
    Five invoices in one cycle: 10, 6, 8, 4 and 7 million.

    Chosen so the three plans genuinely differ against a 50% floor. The open
    balance is 35 million and the target 17.5, which no run of whole invoices
    reaches exactly: 10+6 is 16, 10+6+8 is 24.
    """
    amounts = ["10000000", "6000000", "8000000", "4000000", "7000000"]
    return [
        _invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=buyer,
            amount=amount,
            reference=f"SUP-{index}",
            on=datetime.date(TEST_YEAR, 3, 1 + index),
        )
        for index, amount in enumerate(amounts)
    ]


# ---------------------------------------------------------------------------
# The workspace over real invoices
# ---------------------------------------------------------------------------


class TestTheWorkspace:
    def test_it_reads_the_open_balance_and_the_agreed_share(
        self, grocery: Supplier, ledger: list[SupplierInvoice]
    ) -> None:
        workspace = workspace_for(grocery)
        assert workspace.open_total == Decimal("35000000.000")
        assert workspace.minimum_percent == Decimal("50.000000")
        assert workspace.target == Decimal("17500000.000")
        assert len(workspace.invoices) == 5

    def test_the_invoices_come_oldest_first(
        self, grocery: Supplier, ledger: list[SupplierInvoice]
    ) -> None:
        """FIFO is the order the screen shows, not only the order it pays."""
        workspace = workspace_for(grocery)
        assert [row.invoice.pk for row in workspace.invoice_rows] == [
            invoice.pk for invoice in ledger
        ]
        assert [row.outstanding for row in workspace.invoice_rows] == [
            Decimal("10000000.000"),
            Decimal("6000000.000"),
            Decimal("8000000.000"),
            Decimal("4000000.000"),
            Decimal("7000000.000"),
        ]

    def test_the_three_plans_bracket_the_target(
        self, grocery: Supplier, ledger: list[SupplierInvoice]
    ) -> None:
        """Under it, over it, and exactly on it — and every one says which."""
        plans = {row.plan.kind: row for row in workspace_for(grocery).plan_rows}

        under = plans[PlanKind.UNDER]
        assert under.plan.invoice_count == 2
        assert under.plan.total == Decimal("16000000.000")
        assert under.difference == Decimal("-1500000.000")
        assert not under.plan.splits_an_invoice

        over = plans[PlanKind.OVER]
        assert over.plan.invoice_count == 3
        assert over.plan.total == Decimal("24000000.000")
        assert over.difference == Decimal("6500000.000")
        assert not over.plan.splits_an_invoice

        exact = plans[PlanKind.EXACT]
        assert exact.plan.total == Decimal("17500000.000")
        assert exact.difference == Decimal("0.000")
        assert exact.plan.splits_an_invoice, "the third invoice is part-paid"

    def test_the_exact_amounts_post_back_without_a_float(
        self, grocery: Supplier, ledger: list[SupplierInvoice]
    ) -> None:
        """
        The hidden fields carry `Decimal.__format__`, not `%f`.

        `stringformat:"f"` converts through a binary float, and both of these
        figures are re-entered and compared rather than merely read.
        """
        workspace = workspace_for(grocery)
        assert workspace.open_total_exact == "35000000.000"
        assert workspace.target_exact == "17500000.000"


# ---------------------------------------------------------------------------
# Drafting from a plan
# ---------------------------------------------------------------------------


class TestDraftingASettlement:
    def test_it_produces_an_ordinary_draft_payment(
        self, grocery: Supplier, branch: Branch, buyer: User, ledger: list[SupplierInvoice]
    ) -> None:
        """
        No second document, no second set of numbers.

        The settlement is a `SupplierPayment` in `DRAFT`, which is what makes
        posting it the same act — and the same permission — as posting one
        keyed by hand.
        """
        plan = next(
            row.plan for row in workspace_for(grocery).plan_rows if row.plan.kind == PlanKind.UNDER
        )
        payment = draft_settlement(
            supplier=grocery,
            branch=branch,
            created_by=buyer,
            paid_at=PAID,
            method="CASH",
            allocations=[(line.invoice, line.amount) for line in plan.allocations],
        )
        assert payment.status == SupplierPaymentStatus.DRAFT
        assert payment.number == "", "a draft has no document number until it posts"
        assert payment.amount == Decimal("16000000.000")
        assert payment.allocations.count() == 2

    def test_the_amount_is_the_sum_of_its_allocations(
        self, grocery: Supplier, branch: Branch, buyer: User, ledger: list[SupplierInvoice]
    ) -> None:
        """
        Never a figure of its own — so a settlement cannot leave an advance.

        Paying more than is owed stays possible and stays where it was: a
        payment keyed by hand, whose remainder the operator meant.
        """
        plan = next(
            row.plan for row in workspace_for(grocery).plan_rows if row.plan.kind == PlanKind.OVER
        )
        payment = draft_settlement(
            supplier=grocery,
            branch=branch,
            created_by=buyer,
            paid_at=PAID,
            method="CASH",
            allocations=[(line.invoice, line.amount) for line in plan.allocations],
        )
        allocated = sum(row.allocated_amount for row in payment.allocations.all())
        assert payment.amount == allocated == Decimal("24000000.000")

    def test_the_allocations_are_written_oldest_first(
        self, grocery: Supplier, branch: Branch, buyer: User, ledger: list[SupplierInvoice]
    ) -> None:
        """The sequence is the order the payment reads in, and FIFO is claimed."""
        plan = next(
            row.plan for row in workspace_for(grocery).plan_rows if row.plan.kind == PlanKind.EXACT
        )
        payment = draft_settlement(
            supplier=grocery,
            branch=branch,
            created_by=buyer,
            paid_at=PAID,
            method="CASH",
            allocations=list(reversed([(line.invoice, line.amount) for line in plan.allocations])),
        )
        rows = list(payment.allocations.select_related("invoice").order_by("sequence"))
        assert [row.invoice.invoice_date for row in rows] == sorted(
            row.invoice.invoice_date for row in rows
        ), "given newest-first, it still writes oldest-first"

    def test_the_partial_plan_leaves_the_rest_of_that_invoice_owed(
        self, grocery: Supplier, branch: Branch, buyer: User, ledger: list[SupplierInvoice]
    ) -> None:
        """Part-paid is a real state, and the remainder stays a debt."""
        plan = next(
            row.plan for row in workspace_for(grocery).plan_rows if row.plan.kind == PlanKind.EXACT
        )
        payment = draft_settlement(
            supplier=grocery,
            branch=branch,
            created_by=buyer,
            paid_at=PAID,
            method="CASH",
            allocations=[(line.invoice, line.amount) for line in plan.allocations],
        )
        post_supplier_payment(payment=payment, actor=buyer)

        split = ledger[2]  # the eight-million invoice the plan cut into
        assert outstanding_amount(split) == Decimal("6500000.000")
        assert outstanding_amount(ledger[0]) == Decimal("0.000")
        assert outstanding_amount(ledger[1]) == Decimal("0.000")

    def test_an_empty_plan_is_refused(self, grocery: Supplier, branch: Branch, buyer: User) -> None:
        with pytest.raises(ValidationError) as error:
            draft_settlement(
                supplier=grocery,
                branch=branch,
                created_by=buyer,
                paid_at=PAID,
                method="CASH",
                allocations=[],
            )
        assert error.value.code == "settlement_plan_is_empty"

    def test_it_cannot_pay_more_than_an_invoice_owes(
        self, grocery: Supplier, branch: Branch, buyer: User, ledger: list[SupplierInvoice]
    ) -> None:
        """
        The guard is the payment service's, not this screen's.

        Worth asserting anyway: it is what makes the settlement path safe
        without a bound of its own — a plan is built from outstanding
        balances, so it cannot exceed them, and if it ever did the ordinary
        allocation check would still refuse it.
        """
        with pytest.raises(ValidationError) as error:
            draft_settlement(
                supplier=grocery,
                branch=branch,
                created_by=buyer,
                paid_at=PAID,
                method="CASH",
                allocations=[(ledger[0], Decimal("99000000.000"))],
            )
        assert error.value.code == "allocation_over_invoice"


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


class TestTheScreen:
    def _confirm(
        self,
        client: Client,
        *,
        supplier: Supplier,
        branch: Branch,
        plan: str,
        target: str,
        shown: str,
    ) -> Any:
        return client.post(
            reverse("procurement:settlement_workspace"),
            {
                "supplier": supplier.pk,
                "branch": branch.pk,
                "paid_at": PAID.isoformat(),
                "method": "CASH",
                "plan": plan,
                "target": target,
                "shown_open_total": shown,
            },
        )

    def test_it_renders_the_cycle_the_balance_and_the_plans(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        grocery: Supplier,
        ledger: list[SupplierInvoice],
    ) -> None:
        client = client_for(buyer)
        response = client.get(reverse("procurement:settlement_workspace"), {"supplier": grocery.pk})
        assert response.status_code == 200
        body = response.content.decode("utf-8")
        for phrase in (
            "تسديد الموردين",
            "دورة السداد الجارية",
            "المبلغ المطلوب تسديده",
            "الفواتير المفتوحة",
            "التسديد الأقل",
            "التسديد الأعلى",
            "مطابقة المبلغ",
        ):
            assert phrase in body, phrase

    def test_confirming_a_plan_drafts_a_payment_and_goes_to_it(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        grocery: Supplier,
        branch: Branch,
        ledger: list[SupplierInvoice],
    ) -> None:
        client = client_for(buyer)
        response = self._confirm(
            client,
            supplier=grocery,
            branch=branch,
            plan="UNDER",
            target="17500000.000",
            shown="35000000.000",
        )
        payment = SupplierPayment.objects.get(supplier=grocery)
        assert response.status_code == 302
        assert response["Location"] == reverse(
            "procurement:supplier_payment_detail", args=[payment.pk]
        )
        assert payment.status == SupplierPaymentStatus.DRAFT
        assert payment.amount == Decimal("16000000.000")

    def test_a_balance_that_moved_is_refused_rather_than_paid(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        grocery: Supplier,
        branch: Branch,
        ledger: list[SupplierInvoice],
    ) -> None:
        """
        The whole reason the screen carries the balance it displayed.

        A colleague settles the oldest invoice while this screen stands open.
        The plans are a function of the outstanding balances, so the plan being
        confirmed is no longer the plan that was read — and drafting it would
        settle invoices nobody agreed to.
        """
        rival = draft_settlement(
            supplier=grocery,
            branch=branch,
            created_by=buyer,
            paid_at=PAID,
            method="CASH",
            allocations=[(ledger[0], Decimal("10000000.000"))],
        )
        post_supplier_payment(payment=rival, actor=buyer)

        client = client_for(buyer)
        response = self._confirm(
            client,
            supplier=grocery,
            branch=branch,
            plan="UNDER",
            target="17500000.000",
            shown="35000000.000",  # the figure from before the colleague paid
        )
        assert response.status_code == 200, "re-rendered, not redirected"
        assert "تغيّر رصيد المورد" in response.content.decode("utf-8")
        assert SupplierPayment.objects.filter(status=SupplierPaymentStatus.DRAFT).count() == 0

    def test_the_checker_may_read_the_screen_but_not_draft_from_it(
        self,
        client_for: Callable[[User], Client],
        grocery: Supplier,
        branch: Branch,
        ledger: list[SupplierInvoice],
    ) -> None:
        """
        Maker-checker, kept: an accounting manager posts payments and cannot
        type one in. `view_supplierpayment` opens this screen;
        `create_supplier_payment` is what turns a plan into a document. So
        they see every figure and not one confirm button.
        """
        checker = User.objects.create_user(username="checker", password=PASSWORD)
        grant_organization_access(
            user=checker, organization=grocery.organization, role=Role.ACCOUNTING_MANAGER
        )
        checker = User.objects.get(pk=checker.pk)
        assert checker.has_perm("procurement.view_supplierpayment")
        assert not checker.has_perm("procurement.create_supplier_payment")

        response = client_for(checker).get(
            reverse("procurement:settlement_workspace"), {"supplier": grocery.pk}
        )
        assert response.status_code == 200
        body = response.content.decode("utf-8")
        assert "الفواتير المفتوحة" in body, "the figures are readable"
        assert 'name="plan"' not in body, "and there is nothing to press"
        assert "ليس لديك صلاحية تسجيل دفعات" in body

    def test_the_checker_is_refused_even_posting_the_form_directly(
        self,
        client_for: Callable[[User], Client],
        grocery: Supplier,
        branch: Branch,
        ledger: list[SupplierInvoice],
    ) -> None:
        """A hidden button is not a permission check. The POST is."""
        checker = User.objects.create_user(username="checker-2", password=PASSWORD)
        grant_organization_access(
            user=checker, organization=grocery.organization, role=Role.ACCOUNTING_MANAGER
        )
        checker = User.objects.get(pk=checker.pk)

        response = self._confirm(
            client_for(checker),
            supplier=grocery,
            branch=branch,
            plan="UNDER",
            target="17500000.000",
            shown="35000000.000",
        )
        assert response.status_code == 403
        assert not SupplierPayment.objects.exists()

    def test_a_supplier_in_another_organization_is_absent_not_forbidden(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        other_organization: Organization,
    ) -> None:
        """Out of scope is 404: a 403 would confirm the record exists."""
        stranger = create_supplier(
            organization=other_organization, code="OTHER-01", name="مورد آخر"
        )
        client = client_for(buyer)
        response = client.get(
            reverse("procurement:settlement_workspace"), {"supplier": stranger.pk}
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Where the money lands
# ---------------------------------------------------------------------------


class TestPostingASettlement:
    def test_a_settled_cycle_closes_and_the_next_invoice_opens_a_new_one(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        buyer: User,
        ledger: list[SupplierInvoice],
    ) -> None:
        """
        The window is over the moment the last dinar lands, not on a schedule.

        Settling every invoice in the cycle closes it, and the invoice keyed
        afterwards opens a fresh window with its own due date.
        """
        from apps.procurement.cycles import collecting_cycle
        from apps.procurement.models import SupplierPaymentCycleStatus

        cycle = ledger[0].cycle
        assert cycle is not None

        payment = draft_settlement(
            supplier=grocery,
            branch=branch,
            created_by=buyer,
            paid_at=PAID,
            method="CASH",
            allocations=[(invoice, outstanding_amount(invoice)) for invoice in ledger],
        )
        post_supplier_payment(payment=payment, actor=buyer)

        cycle.refresh_from_db()
        assert cycle.status == SupplierPaymentCycleStatus.SETTLED
        assert cycle.settled_on is not None

        later = _invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=buyer,
            amount="3000000",
            reference="SUP-LATER",
            on=datetime.date(TEST_YEAR, 4, 10),
        )
        assert later.cycle_id != cycle.pk
        opened = collecting_cycle(grocery)
        assert opened is not None
        assert opened.pk == later.cycle_id
        assert opened.due_date == datetime.date(TEST_YEAR, 5, 10)

    def test_the_ordinary_allocation_path_and_this_one_agree(
        self,
        grocery: Supplier,
        branch: Branch,
        buyer: User,
        ledger: list[SupplierInvoice],
    ) -> None:
        """
        A settlement drafted from a plan is the same record as one keyed by
        hand — the point of building on the payment document rather than
        beside it.
        """
        from apps.procurement.payments import create_supplier_payment

        drafted = draft_settlement(
            supplier=grocery,
            branch=branch,
            created_by=buyer,
            paid_at=PAID,
            method="CASH",
            allocations=[(ledger[0], Decimal("10000000.000"))],
        )
        by_hand = create_supplier_payment(
            supplier=grocery,
            branch=branch,
            created_by=buyer,
            paid_at=PAID,
            method="CASH",
            amount=Decimal("6000000.000"),
        )
        add_payment_allocation(
            payment=by_hand, invoice=ledger[1], allocated_amount=Decimal("6000000.000")
        )

        assert type(drafted) is type(by_hand)
        assert drafted.status == by_hand.status == SupplierPaymentStatus.DRAFT
        for payment in (drafted, by_hand):
            posted = post_supplier_payment(payment=payment, actor=buyer)
            assert posted.journal_entry is not None
            assert posted.number
