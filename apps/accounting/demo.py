"""
The Accounting demo dataset.

Built through the real services, never by writing rows directly, so what the
screens show is what the domain actually produces. See
`docs/development/demo-data-policy.md`.

Idempotent by construction: every step looks before it creates, keyed on
something the database already makes unique — a code for master data, and the
GL account for a cash record. A second run reports `0 created, N reused` and
adds no second cashbox, no second bank account and no second journal.

**No `--reset`.** Nothing this seeds may be deleted to make a reseed
convenient: a cashbox that has carried movement is history, and an accounting
demo that could erase it would teach the wrong lesson about what this module
is for.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.utils import timezone

from apps.accounting.cash_services import create_bank_account, create_cashbox
from apps.accounting.expense_services import add_expense_line
from apps.accounting.models import (
    Account,
    AccountClass,
    BankAccount,
    Cashbox,
    CostCenter,
    ExpenseVoucher,
    PaymentSource,
)
from apps.core.context import get_actor
from apps.organizations.models import Branch, Organization

#: The demo organization every Phase 1–5 seed shares.
DEMO_ORGANIZATION_CODE = "DEMO-KHAN-MANDI"

#: Stamped on every demo record's notes so nothing seeded here can be mistaken
#: for a real one on a screen, in an export, or in a screenshot.
DEMO_BANNER = "تجريبي — غير معتمد للإنتاج"


@dataclass
class AccountingDemo:
    """What one run created, and what it found already there."""

    organization: Organization | None = None
    created: int = 0
    reused: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, *, made: bool, what: str) -> None:
        if made:
            self.created += 1
        else:
            self.reused += 1
        self.notes.append(f"{'+' if made else '='} {what}")


class DemoPreconditionError(RuntimeError):
    """The demo organization or its chart is missing. Seed those first."""


def _account(organization: Organization, code: str) -> Account:
    account = Account.objects.filter(organization=organization, code=code).first()
    if account is None:
        raise DemoPreconditionError(f"account {code} is missing — run seed_chart_of_accounts first")
    return account


def seed_cash_records(result: AccountingDemo) -> None:
    """
    One drawer and one bank account, on the accounts the chart already seeds.

    Keyed on the **code**, not on a count: a run that found the cashbox already
    there must not create a second one on a different account, which is exactly
    what "create if none exist" would do after somebody archived the first.
    """
    organization = result.organization
    if organization is None:  # pragma: no cover - the caller always sets it
        raise DemoPreconditionError("the demo run has no organization")
    branch = Branch.objects.filter(organization=organization).order_by("code").first()
    if branch is None:
        raise DemoPreconditionError("the demo organization has no branch")

    cashbox = Cashbox.objects.filter(organization=organization, code="DEMO-CASH-1").first()
    if cashbox is None:
        cashbox = create_cashbox(
            organization=organization,
            branch=branch,
            account=_account(organization, "1-01-01-001"),
            code="DEMO-CASH-1",
            name_ar="صندوق الفرع الرئيسي",
            name_en="Main branch cashbox",
            opened_on=_start_of_year(),
            responsible_note="أمين الصندوق التجريبي",
            notes=DEMO_BANNER,
        )
        result.note(made=True, what="cashbox DEMO-CASH-1")
    else:
        result.note(made=False, what="cashbox DEMO-CASH-1")

    bank = BankAccount.objects.filter(organization=organization, code="DEMO-BANK-1").first()
    if bank is None:
        create_bank_account(
            organization=organization,
            account=_account(organization, "1-01-02-001"),
            code="DEMO-BANK-1",
            bank_name="مصرف تجريبي",
            name_ar="الحساب الجاري التجريبي",
            name_en="Demo current account",
            # Already a mask. The service masks again on the way in, so a full
            # number typed here by mistake would still never land in the column.
            masked_account_number="****4417",
            branch=None,
            iban="",
            notes=DEMO_BANNER,
        )
        result.note(made=True, what="bank account DEMO-BANK-1")
    else:
        result.note(made=False, what="bank account DEMO-BANK-1")


def seed_expense_vouchers(result: AccountingDemo) -> None:
    """
    One draft expense voucher with a line, so المصروفات opens with something on it.

    Deliberately **not** approved or posted here. Approval is maker-checker —
    the creator may not approve — so a posted demo voucher needs two demo
    actors, and inventing a second one silently would model the control as
    though it were a formality. The lifecycle is exercised by the tests; the
    seed shows the screen.
    """
    organization = result.organization
    if organization is None:  # pragma: no cover - the caller always sets it
        raise DemoPreconditionError("the demo run has no organization")
    branch = Branch.objects.filter(organization=organization).order_by("code").first()
    if branch is None:
        raise DemoPreconditionError("the demo organization has no branch")

    cashbox = Cashbox.objects.filter(organization=organization, code="DEMO-CASH-1").first()
    if cashbox is None:
        raise DemoPreconditionError("seed the demo cashbox first")

    voucher = ExpenseVoucher.objects.filter(
        organization=organization, evidence_reference="DEMO-EXP-0001"
    ).first()
    if voucher is not None:
        result.note(made=False, what="expense voucher DEMO-EXP-0001")
        return

    today = timezone.localdate()
    voucher = ExpenseVoucher(
        organization=organization,
        branch=branch,
        business_date=today,
        expense_date=today,
        payment_source=PaymentSource.CASHBOX,
        cashbox=cashbox,
        beneficiary="شركة الكهرباء التجريبية",
        reason="فاتورة كهرباء الفرع",
        evidence_reference="DEMO-EXP-0001",
        notes=DEMO_BANNER,
        created_by=get_actor(),
    )
    voucher.full_clean()
    voucher.save()

    account = _expense_account(organization)
    add_expense_line(
        voucher=voucher,
        account=account,
        amount=Decimal("125000"),
        cost_center=_cost_center(organization),
        description="كهرباء — شهر تجريبي",
    )
    result.note(made=True, what="expense voucher DEMO-EXP-0001")


def _expense_account(organization: Organization) -> Account:
    """The first postable operating-expense account in the seeded chart."""
    account = (
        Account.objects.filter(
            organization=organization,
            is_postable=True,
            is_active=True,
            account_class=AccountClass.OPERATING_EXPENSE,
        )
        .order_by("code")
        .first()
    )
    if account is None:
        raise DemoPreconditionError("no operating-expense account in the chart")
    return account


def _cost_center(organization: Organization) -> CostCenter | None:
    return (
        CostCenter.objects.filter(organization=organization, is_active=True)
        .order_by("code")
        .first()
    )


def _start_of_year() -> datetime.date:
    today = timezone.localdate()
    return datetime.date(today.year, 1, 1)


def seed_accounting_demo(**_options: Any) -> AccountingDemo:
    """
    Build the Accounting demo, or report what is already there.

    Callers must have checked `settings.DEBUG` before reaching this — the
    management command does, before it reads a single argument.
    """
    organization = Organization.objects.filter(code=DEMO_ORGANIZATION_CODE).first()
    if organization is None:
        raise DemoPreconditionError(
            f"organization {DEMO_ORGANIZATION_CODE} is missing — run an earlier phase's demo first"
        )

    result = AccountingDemo(organization=organization)
    seed_cash_records(result)
    seed_expense_vouchers(result)
    return result


__all__ = [
    "DEMO_BANNER",
    "DEMO_ORGANIZATION_CODE",
    "AccountingDemo",
    "DemoPreconditionError",
    "seed_accounting_demo",
    "seed_cash_records",
    "seed_expense_vouchers",
]
