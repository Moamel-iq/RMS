"""
The authorized way into the expense, accrual and prepayment services.

Same shape as `apps/accounting/commands.py` and for the same reasons: resolve
every submitted identifier **with** the caller, check the permission at the
scope that owns the decision, bind the audit actor, then call the service. No
accounting rule lives here. If a rule appears necessary in this file it belongs
in `expense_services` or `deferral_services`, where the screens would get it
too — a rule enforced only on the API path is a rule the UI does not have.

The scopes are not uniform, and the asymmetry is deliberate:

* an **expense voucher** is written at a branch, so authoring is branch-scoped,
  while approving and posting it are organization decisions;
* an **accrual** and a **prepayment** are month-end judgements about the
  organization's own position, so both are organization-scoped throughout.

Maker-checker is enforced by the services, never here. Hiding a button is
presentation; refusing the second signature is the control.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounting.commands import _acting_as, _scoped_account, _scoped_cost_center
from apps.accounting.deferral_services import (
    add_accrual_line,
    approve_accrual,
    approve_prepayment,
    open_accrual,
    open_prepayment,
    post_accrual,
    post_prepayment,
    post_schedule_line,
    remove_accrual_line,
    reverse_accrual,
    reverse_schedule_line,
)
from apps.accounting.expense_services import (
    add_expense_line,
    approve_expense_voucher,
    discard_expense_voucher,
    open_expense_voucher,
    post_expense_voucher,
    remove_expense_line,
    reverse_expense_voucher,
)
from apps.accounting.models import (
    AccrualDocument,
    AccrualLine,
    AmortizationFrequency,
    BankAccount,
    Cashbox,
    ExpenseVoucher,
    ExpenseVoucherLine,
    FinancialDocumentStatus,
    Prepayment,
    PrepaymentScheduleLine,
)
from apps.accounting.permissions import (
    APPROVE_EXPENSE_VOUCHERS,
    MANAGE_ACCRUALS,
    MANAGE_EXPENSE_VOUCHERS,
    MANAGE_PREPAYMENTS,
)
from apps.organizations.authorization import (
    OutOfScope,
    organization_scope,
    organizations_with_permission,
    require_branch_permission,
    require_organization_permission,
    resolve_branch,
)
from apps.organizations.models import Branch
from apps.users.models import User


def _payment_records(
    *, branch: Branch, cashbox_id: int | None, bank_account_id: int | None
) -> tuple[Cashbox | None, BankAccount | None]:
    """
    Resolve the pay-from record inside the branch's own organization.

    Filtered by organization rather than fetched and compared: another
    organization's cashbox is simply not in the queryset, so there is no moment
    at which it exists in a local variable. "Exactly one of the two" is decided
    by the service, not here — this only turns ids into records.
    """
    cashbox: Cashbox | None = None
    bank: BankAccount | None = None
    if cashbox_id is not None:
        cashbox = Cashbox.objects.filter(
            organization_id=branch.organization_id, pk=cashbox_id
        ).first()
        if cashbox is None:
            raise OutOfScope(_("Cashbox %(id)s does not exist.") % {"id": cashbox_id})
    if bank_account_id is not None:
        bank = BankAccount.objects.filter(
            organization_id=branch.organization_id, pk=bank_account_id
        ).first()
        if bank is None:
            raise OutOfScope(_("Bank account %(id)s does not exist.") % {"id": bank_account_id})
    return cashbox, bank


# ---------------------------------------------------------------------------
# المصروفات — expense vouchers
# ---------------------------------------------------------------------------


def visible_expense_vouchers(actor: User) -> QuerySet[ExpenseVoucher]:
    """Every voucher at a branch this caller may write vouchers at."""
    from apps.organizations.authorization import branches_with_permission

    return ExpenseVoucher.objects.filter(
        branch__in=branches_with_permission(actor, MANAGE_EXPENSE_VOUCHERS)
    ).select_related("organization", "branch", "cashbox", "bank_account", "created_by")


def _resolve_voucher(actor: User, voucher_id: int) -> ExpenseVoucher:
    row = visible_expense_vouchers(actor).filter(pk=voucher_id).first()
    if row is None:
        raise OutOfScope(_("Expense voucher %(id)s does not exist.") % {"id": voucher_id})
    return row


def list_expense_vouchers(
    *,
    actor: User,
    organization_id: int | None = None,
    branch_id: int | None = None,
    status: str = "",
) -> QuerySet[ExpenseVoucher]:
    vouchers = visible_expense_vouchers(actor)
    if organization_id is not None:
        vouchers = vouchers.filter(organization_id=organization_id)
    if branch_id is not None:
        vouchers = vouchers.filter(branch_id=branch_id)
    if status:
        if status not in FinancialDocumentStatus.values:
            raise OutOfScope(_("Unknown status %(status)s.") % {"status": status})
        vouchers = vouchers.filter(status=status)
    return vouchers.order_by("-business_date", "-id")


def read_expense_voucher(*, actor: User, voucher_id: int) -> ExpenseVoucher:
    return _resolve_voucher(actor, voucher_id)


@transaction.atomic
def open_expense(
    *,
    actor: User,
    branch_id: int,
    business_date: datetime.date,
    expense_date: datetime.date,
    beneficiary: str,
    reason: str,
    cashbox_id: int | None = None,
    bank_account_id: int | None = None,
    evidence_reference: str = "",
    notes: str = "",
) -> ExpenseVoucher:
    branch = resolve_branch(actor, branch_id)
    require_branch_permission(actor, MANAGE_EXPENSE_VOUCHERS, branch)
    cashbox, bank = _payment_records(
        branch=branch, cashbox_id=cashbox_id, bank_account_id=bank_account_id
    )
    with _acting_as(actor):
        return open_expense_voucher(
            branch=branch,
            business_date=business_date,
            expense_date=expense_date,
            beneficiary=beneficiary,
            reason=reason,
            cashbox=cashbox,
            bank_account=bank,
            evidence_reference=evidence_reference,
            notes=notes,
            created_by=actor,
        )


@transaction.atomic
def add_expense_voucher_line(
    *,
    actor: User,
    voucher_id: int,
    account_id: int,
    amount: Decimal,
    cost_center_id: int | None = None,
    description: str = "",
) -> ExpenseVoucherLine:
    voucher = _resolve_voucher(actor, voucher_id)
    require_branch_permission(actor, MANAGE_EXPENSE_VOUCHERS, voucher.branch)
    account = _scoped_account(voucher.organization, account_id)
    cost_center = (
        _scoped_cost_center(voucher.organization, cost_center_id)
        if cost_center_id is not None
        else None
    )
    with _acting_as(actor):
        return add_expense_line(
            voucher=voucher,
            account=account,
            amount=amount,
            cost_center=cost_center,
            description=description,
        )


@transaction.atomic
def remove_expense_voucher_line(*, actor: User, voucher_id: int, line_id: int) -> None:
    voucher = _resolve_voucher(actor, voucher_id)
    require_branch_permission(actor, MANAGE_EXPENSE_VOUCHERS, voucher.branch)
    line = ExpenseVoucherLine.objects.filter(voucher=voucher, pk=line_id).first()
    if line is None:
        raise OutOfScope(_("Line %(id)s does not exist.") % {"id": line_id})
    with _acting_as(actor):
        remove_expense_line(line=line)


@transaction.atomic
def approve_expense(*, actor: User, voucher_id: int, reason: str = "") -> ExpenseVoucher:
    voucher = _resolve_voucher(actor, voucher_id)
    require_organization_permission(actor, APPROVE_EXPENSE_VOUCHERS, voucher.organization)
    with _acting_as(actor):
        return approve_expense_voucher(voucher=voucher, approver=actor, reason=reason)


@transaction.atomic
def post_expense(*, actor: User, voucher_id: int, reason: str = "") -> ExpenseVoucher:
    voucher = _resolve_voucher(actor, voucher_id)
    require_organization_permission(actor, APPROVE_EXPENSE_VOUCHERS, voucher.organization)
    with _acting_as(actor):
        return post_expense_voucher(voucher=voucher, poster=actor, reason=reason)


@transaction.atomic
def reverse_expense(*, actor: User, voucher_id: int, reason: str) -> ExpenseVoucher:
    voucher = _resolve_voucher(actor, voucher_id)
    require_organization_permission(actor, APPROVE_EXPENSE_VOUCHERS, voucher.organization)
    with _acting_as(actor):
        return reverse_expense_voucher(voucher=voucher, actor=actor, reason=reason)


@transaction.atomic
def discard_expense(*, actor: User, voucher_id: int, reason: str = "") -> None:
    voucher = _resolve_voucher(actor, voucher_id)
    require_branch_permission(actor, MANAGE_EXPENSE_VOUCHERS, voucher.branch)
    with _acting_as(actor):
        discard_expense_voucher(voucher=voucher, reason=reason)


# ---------------------------------------------------------------------------
# المستحقات — accruals
# ---------------------------------------------------------------------------


def visible_accrual_documents(actor: User) -> QuerySet[AccrualDocument]:
    return AccrualDocument.objects.filter(
        organization__in=organizations_with_permission(actor, MANAGE_ACCRUALS)
    ).select_related("organization", "branch", "created_by", "approved_by")


def _resolve_accrual(actor: User, accrual_id: int) -> AccrualDocument:
    row = visible_accrual_documents(actor).filter(pk=accrual_id).first()
    if row is None:
        raise OutOfScope(_("Accrual %(id)s does not exist.") % {"id": accrual_id})
    return row


def list_accruals(
    *, actor: User, organization_id: int | None = None, status: str = ""
) -> QuerySet[AccrualDocument]:
    accruals = visible_accrual_documents(actor)
    if organization_id is not None:
        accruals = accruals.filter(organization_id=organization_id)
    if status:
        if status not in FinancialDocumentStatus.values:
            raise OutOfScope(_("Unknown status %(status)s.") % {"status": status})
        accruals = accruals.filter(status=status)
    return accruals.order_by("-business_date", "-id")


def read_accrual(*, actor: User, accrual_id: int) -> AccrualDocument:
    return _resolve_accrual(actor, accrual_id)


@transaction.atomic
def open_accrual_document(
    *,
    actor: User,
    branch_id: int,
    business_date: datetime.date,
    description: str,
    reason: str = "",
    auto_reverse_on: datetime.date | None = None,
    evidence_reference: str = "",
) -> AccrualDocument:
    branch = resolve_branch(actor, branch_id)
    require_organization_permission(actor, MANAGE_ACCRUALS, branch.organization)
    with _acting_as(actor):
        return open_accrual(
            branch=branch,
            business_date=business_date,
            description=description,
            reason=reason,
            auto_reverse_on=auto_reverse_on,
            evidence_reference=evidence_reference,
            created_by=actor,
        )


@transaction.atomic
def add_accrual_document_line(
    *,
    actor: User,
    accrual_id: int,
    account_id: int,
    amount: Decimal,
    cost_center_id: int | None = None,
    description: str = "",
) -> AccrualLine:
    accrual = _resolve_accrual(actor, accrual_id)
    require_organization_permission(actor, MANAGE_ACCRUALS, accrual.organization)
    account = _scoped_account(accrual.organization, account_id)
    cost_center = (
        _scoped_cost_center(accrual.organization, cost_center_id)
        if cost_center_id is not None
        else None
    )
    with _acting_as(actor):
        return add_accrual_line(
            accrual=accrual,
            account=account,
            amount=amount,
            cost_center=cost_center,
            description=description,
        )


@transaction.atomic
def remove_accrual_document_line(*, actor: User, accrual_id: int, line_id: int) -> None:
    accrual = _resolve_accrual(actor, accrual_id)
    require_organization_permission(actor, MANAGE_ACCRUALS, accrual.organization)
    line = AccrualLine.objects.filter(accrual=accrual, pk=line_id).first()
    if line is None:
        raise OutOfScope(_("Line %(id)s does not exist.") % {"id": line_id})
    with _acting_as(actor):
        remove_accrual_line(line=line)


@transaction.atomic
def approve_accrual_document(*, actor: User, accrual_id: int, reason: str = "") -> AccrualDocument:
    accrual = _resolve_accrual(actor, accrual_id)
    require_organization_permission(actor, MANAGE_ACCRUALS, accrual.organization)
    with _acting_as(actor):
        return approve_accrual(accrual=accrual, approver=actor, reason=reason)


@transaction.atomic
def post_accrual_document(*, actor: User, accrual_id: int, reason: str = "") -> AccrualDocument:
    accrual = _resolve_accrual(actor, accrual_id)
    require_organization_permission(actor, MANAGE_ACCRUALS, accrual.organization)
    with _acting_as(actor):
        return post_accrual(accrual=accrual, poster=actor, reason=reason)


@transaction.atomic
def reverse_accrual_document(*, actor: User, accrual_id: int, reason: str) -> AccrualDocument:
    accrual = _resolve_accrual(actor, accrual_id)
    require_organization_permission(actor, MANAGE_ACCRUALS, accrual.organization)
    with _acting_as(actor):
        return reverse_accrual(accrual=accrual, reason=reason)


# ---------------------------------------------------------------------------
# المقدمات — prepayments
# ---------------------------------------------------------------------------


def visible_prepayment_documents(actor: User) -> QuerySet[Prepayment]:
    return Prepayment.objects.filter(
        organization__in=organizations_with_permission(actor, MANAGE_PREPAYMENTS)
    ).select_related("organization", "branch", "expense_account", "prepaid_account")


def _resolve_prepayment(actor: User, prepayment_id: int) -> Prepayment:
    row = visible_prepayment_documents(actor).filter(pk=prepayment_id).first()
    if row is None:
        raise OutOfScope(_("Prepayment %(id)s does not exist.") % {"id": prepayment_id})
    return row


def list_prepayments(
    *, actor: User, organization_id: int | None = None, status: str = ""
) -> QuerySet[Prepayment]:
    prepayments = visible_prepayment_documents(actor)
    if organization_id is not None:
        prepayments = prepayments.filter(organization_id=organization_id)
    if status:
        if status not in FinancialDocumentStatus.values:
            raise OutOfScope(_("Unknown status %(status)s.") % {"status": status})
        prepayments = prepayments.filter(status=status)
    return prepayments.order_by("-business_date", "-id")


def read_prepayment(*, actor: User, prepayment_id: int) -> Prepayment:
    return _resolve_prepayment(actor, prepayment_id)


@transaction.atomic
def open_prepayment_document(
    *,
    actor: User,
    branch_id: int,
    business_date: datetime.date,
    description: str,
    total_amount: Decimal,
    start_date: datetime.date,
    frequency: str,
    period_count: int,
    expense_account_id: int,
    prepaid_account_id: int,
    cost_center_id: int | None = None,
    cashbox_id: int | None = None,
    bank_account_id: int | None = None,
    source_reference: str = "",
    evidence_reference: str = "",
) -> Prepayment:
    branch = resolve_branch(actor, branch_id)
    require_organization_permission(actor, MANAGE_PREPAYMENTS, branch.organization)
    if frequency not in AmortizationFrequency.values:
        raise OutOfScope(_("Unknown frequency %(value)s.") % {"value": frequency})
    cashbox, bank = _payment_records(
        branch=branch, cashbox_id=cashbox_id, bank_account_id=bank_account_id
    )
    with _acting_as(actor):
        return open_prepayment(
            branch=branch,
            business_date=business_date,
            description=description,
            total_amount=total_amount,
            start_date=start_date,
            frequency=frequency,
            period_count=period_count,
            expense_account=_scoped_account(branch.organization, expense_account_id),
            prepaid_account=_scoped_account(branch.organization, prepaid_account_id),
            cost_center=(
                _scoped_cost_center(branch.organization, cost_center_id)
                if cost_center_id is not None
                else None
            ),
            cashbox=cashbox,
            bank_account=bank,
            source_reference=source_reference,
            evidence_reference=evidence_reference,
            created_by=actor,
        )


@transaction.atomic
def approve_prepayment_document(*, actor: User, prepayment_id: int, reason: str = "") -> Prepayment:
    prepayment = _resolve_prepayment(actor, prepayment_id)
    require_organization_permission(actor, MANAGE_PREPAYMENTS, prepayment.organization)
    with _acting_as(actor):
        return approve_prepayment(prepayment=prepayment, approver=actor, reason=reason)


@transaction.atomic
def post_prepayment_document(*, actor: User, prepayment_id: int, reason: str = "") -> Prepayment:
    prepayment = _resolve_prepayment(actor, prepayment_id)
    require_organization_permission(actor, MANAGE_PREPAYMENTS, prepayment.organization)
    with _acting_as(actor):
        return post_prepayment(prepayment=prepayment, poster=actor, reason=reason)


def _resolve_schedule_line(actor: User, line_id: int) -> PrepaymentScheduleLine:
    row = (
        PrepaymentScheduleLine.objects.filter(
            prepayment__organization_id__in=organization_scope(actor)
        )
        .select_related("prepayment", "prepayment__organization")
        .filter(pk=line_id)
        .first()
    )
    if row is None:
        raise OutOfScope(_("Schedule line %(id)s does not exist.") % {"id": line_id})
    return row


@transaction.atomic
def post_prepayment_schedule_line(
    *, actor: User, line_id: int, reason: str = ""
) -> PrepaymentScheduleLine:
    line = _resolve_schedule_line(actor, line_id)
    require_organization_permission(actor, MANAGE_PREPAYMENTS, line.prepayment.organization)
    with _acting_as(actor):
        return post_schedule_line(line=line, reason=reason)


@transaction.atomic
def reverse_prepayment_schedule_line(
    *, actor: User, line_id: int, reason: str
) -> PrepaymentScheduleLine:
    line = _resolve_schedule_line(actor, line_id)
    require_organization_permission(actor, MANAGE_PREPAYMENTS, line.prepayment.organization)
    with _acting_as(actor):
        return reverse_schedule_line(line=line, reason=reason)


__all__ = [
    "add_accrual_document_line",
    "add_expense_voucher_line",
    "approve_accrual_document",
    "approve_expense",
    "approve_prepayment_document",
    "discard_expense",
    "list_accruals",
    "list_expense_vouchers",
    "list_prepayments",
    "open_accrual_document",
    "open_expense",
    "open_prepayment_document",
    "post_accrual_document",
    "post_expense",
    "post_prepayment_document",
    "post_prepayment_schedule_line",
    "read_accrual",
    "read_expense_voucher",
    "read_prepayment",
    "remove_accrual_document_line",
    "remove_expense_voucher_line",
    "reverse_accrual_document",
    "reverse_expense",
    "reverse_prepayment_schedule_line",
    "visible_accrual_documents",
    "visible_expense_vouchers",
    "visible_prepayment_documents",
]
