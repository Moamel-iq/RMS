"""
المصروفات — the expense-voucher lifecycle and its posting.

`DRAFT → APPROVED → POSTED → REVERSED`, with the creator barred from approving
and from posting. The document exists for what Procurement is not for: a
non-supplier operational expense paid immediately (ADR-030 §3).

The journal is `Dr expense/asset lines · Cr the pay-from account`, resolved
through the `Cashbox` or `BankAccount` master record rather than by naming a GL
account directly — so the voucher's cash effect appears on that record's
statement automatically, with no second place to keep in step.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    Account,
    AccountClass,
    BankAccount,
    Cashbox,
    CostCenter,
    ExpenseVoucher,
    ExpenseVoucherLine,
    FinancialDocumentStatus,
    PaymentSource,
)
from apps.accounting.services import post_entry, reverse_entry
from apps.accounting.validators import PostingLine
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.organizations.models import Branch
from apps.users.models import User

ZERO = Decimal("0")

#: **Written in the stored, upper-case form.** `canonical_source_identity`
#: case-folds `source_document_type` before persisting it, so a constant
#: spelled `accounting.ExpenseVoucher` would write `ACCOUNTING.EXPENSEVOUCHER`
#: and then fail to find itself again — a reversal that cannot locate its own
#: journal. Phase 4 paid for this lesson once.
SOURCE_DOCUMENT_TYPE = "ACCOUNTING.EXPENSEVOUCHER"

#: Which account classes an expense line may name. Expense, cost of sales and
#: "other expense" are the point of the document; an asset is allowed because a
#: small purchase capitalised on payment is a real case. Revenue, liability and
#: equity are refused: a voucher that credited revenue would be a sale nobody
#: recorded as one.
EXPENSE_LINE_CLASSES = frozenset(
    {
        AccountClass.ASSET,
        AccountClass.COST_OF_SALES,
        AccountClass.OPERATING_EXPENSE,
        AccountClass.OTHER,
    }
)


def _next_number(*, organization_id: int, on_date: datetime.date) -> str:
    """
    A per-organization, per-year voucher number.

    Taken when the voucher leaves DRAFT, not when it is created: an abandoned
    draft must not burn a number, for the same reason a journal's number is
    taken at posting.
    """
    year = on_date.year
    prefix = f"EXP-{year}-"
    last = (
        ExpenseVoucher.objects.filter(
            organization_id=organization_id, number__startswith=prefix
        ).aggregate(highest=Max("number"))["highest"]
        or ""
    )
    sequence = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{sequence:05d}"


def recompute_total(voucher: ExpenseVoucher) -> ExpenseVoucher:
    """
    The header total is the **sum of its lines**, never rounded on its own.

    A total rounded independently would differ from the journal it produces by
    a rounding unit, and the journal would then refuse to balance — which is
    the good outcome. The bad one is a total that merely *displays* differently
    from the sum of what is under it.
    """
    total = sum((line.amount for line in voucher.lines.all()), ZERO)
    voucher.total_amount = total
    voucher.save(update_fields=["total_amount", "updated_at"])
    return voucher


def _validate_line_account(voucher: ExpenseVoucher, account: Account) -> None:
    if account.organization_id != voucher.organization_id:
        raise ValidationError(
            _("The account belongs to another organization."),
            code="account_organization_mismatch",
        )
    if not account.is_postable:
        raise ValidationError(
            _("Only a detail account accepts journal lines."), code="account_not_postable"
        )
    if account.account_class not in EXPENSE_LINE_CLASSES:
        raise ValidationError(
            _("An expense line cannot name a revenue, liability or equity account."),
            code="account_class_not_an_expense",
        )
    payment_account = voucher.payment_account
    if payment_account is not None and payment_account.pk == account.pk:
        # Dr and Cr on the same account nets to nothing and posts a journal that
        # balances while recording no expense at all.
        raise ValidationError(
            _("An expense line cannot name the account the voucher is paid from."),
            code="account_is_the_payment_source",
        )


@transaction.atomic
def open_expense_voucher(
    *,
    branch: Branch,
    business_date: datetime.date,
    expense_date: datetime.date,
    beneficiary: str,
    reason: str,
    created_by: User,
    cashbox: Cashbox | None = None,
    bank_account: BankAccount | None = None,
    evidence_reference: str = "",
    notes: str = "",
) -> ExpenseVoucher:
    """
    Open a draft voucher header. Lines are added afterwards.

    The pay-from source is given as **the record itself**, never as a
    `payment_source` string alongside it. Passing both would let a caller
    declare `BANK` and hand over a cashbox, and the two would then have to be
    checked against each other in every caller that exists — so the kind is
    derived here from which record arrived, and "exactly one source" is decided
    once instead of restated per surface.
    """
    if (cashbox is None) == (bank_account is None):
        raise ValidationError(
            _("A voucher is paid from exactly one source."),
            code="payment_source_not_exactly_one",
        )

    source: Cashbox | BankAccount = cashbox if cashbox is not None else bank_account  # type: ignore[assignment]
    if source.organization_id != branch.organization_id:
        raise ValidationError(
            _("The pay-from record belongs to another organization."),
            code="payment_source_organization_mismatch",
        )
    if not source.is_active:
        raise ValidationError(
            _("The pay-from record is withdrawn."), code="payment_source_inactive"
        )
    if expense_date > business_date:
        raise ValidationError(
            _("The expense date is after the business date."), code="expense_date_after_business"
        )

    voucher = ExpenseVoucher(
        organization=branch.organization,
        branch=branch,
        business_date=business_date,
        expense_date=expense_date,
        payment_source=PaymentSource.CASHBOX if cashbox is not None else PaymentSource.BANK,
        cashbox=cashbox,
        bank_account=bank_account,
        beneficiary=beneficiary,
        reason=reason,
        evidence_reference=evidence_reference,
        notes=notes,
        created_by=created_by,
    )
    voucher.full_clean()
    voucher.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=voucher,
        branch=branch,
        new_state=snapshot(voucher),
        reason="",
    )
    return voucher


@transaction.atomic
def add_expense_line(
    *,
    voucher: ExpenseVoucher,
    account: Account,
    amount: Decimal,
    cost_center: CostCenter | None = None,
    description: str = "",
) -> ExpenseVoucherLine:
    """Append a line to a draft."""
    if not voucher.is_editable:
        raise ValidationError(_("Only a draft voucher may be changed."), code="voucher_not_a_draft")
    _validate_line_account(voucher, account)
    if account.requires_cost_center and cost_center is None:
        raise ValidationError(
            _("This account requires a cost center."), code="cost_center_required"
        )

    quantized = quantize_money(amount, field="amount")
    if quantized <= ZERO:
        raise ValidationError(_("A line amount must be positive."), code="amount_not_positive")

    next_sequence = (voucher.lines.aggregate(highest=Max("sequence"))["highest"] or 0) + 1
    line = ExpenseVoucherLine(
        voucher=voucher,
        sequence=next_sequence,
        account=account,
        cost_center=cost_center,
        description=description.strip(),
        amount=quantized,
    )
    line.full_clean()
    line.save()
    recompute_total(voucher)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=voucher,
        branch=voucher.branch,
        new_state=snapshot(voucher),
        reason=str(_("expense line added")),
        metadata={"account": account.code, "amount": str(quantized)},
    )
    return line


@transaction.atomic
def remove_expense_line(*, line: ExpenseVoucherLine) -> None:
    voucher = line.voucher
    if not voucher.is_editable:
        raise ValidationError(_("Only a draft voucher may be changed."), code="voucher_not_a_draft")
    line.delete()
    recompute_total(voucher)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=voucher,
        branch=voucher.branch,
        new_state=snapshot(voucher),
        reason=str(_("expense line removed")),
    )


@transaction.atomic
def approve_expense_voucher(
    *, voucher: ExpenseVoucher, approver: User, reason: str = ""
) -> ExpenseVoucher:
    """
    Release a draft for posting.

    **The creator may not approve.** The whole point of the document is that
    somebody spends the organization's cash without a supplier invoice behind
    it; one person doing both halves is the control that matters most here, and
    a null creator is refused rather than waved through because nothing can
    prove the two differ.
    """
    if voucher.status != FinancialDocumentStatus.DRAFT:
        raise ValidationError(_("Only a draft may be approved."), code="voucher_not_a_draft")
    if not voucher.lines.exists():
        raise ValidationError(_("A voucher needs at least one line."), code="voucher_has_no_lines")
    if voucher.created_by_id is None:
        raise ValidationError(
            _(
                "This voucher records no author, so it cannot be shown that the approver "
                "is a different person."
            ),
            code="voucher_author_unknown",
        )
    if voucher.created_by_id == approver.pk:
        raise ValidationError(
            _("A voucher must be approved by somebody other than the person who wrote it."),
            code="voucher_self_approved",
        )

    recompute_total(voucher)
    if voucher.total_amount <= ZERO:
        raise ValidationError(_("A voucher must carry a value."), code="voucher_has_no_value")

    before = snapshot(voucher)
    voucher.status = FinancialDocumentStatus.APPROVED
    voucher.approved_by = approver
    voucher.approved_at = timezone.now()
    voucher.number = _next_number(
        organization_id=voucher.organization_id, on_date=voucher.business_date
    )
    voucher.full_clean()
    voucher.save(update_fields=["status", "approved_by", "approved_at", "number", "updated_at"])
    record_audit_event(
        action=AuditAction.APPROVED,
        target=voucher,
        branch=voucher.branch,
        previous_state=before,
        new_state=snapshot(voucher),
        reason=reason,
    )
    return voucher


@transaction.atomic
def post_expense_voucher(
    *, voucher: ExpenseVoucher, poster: User, reason: str = ""
) -> ExpenseVoucher:
    """
    Move an approved voucher into the ledger.

    `Dr expense lines · Cr the pay-from account`. The credit side comes from the
    cashbox or bank record, so the movement lands on that record's statement
    without anything else being told about it.
    """
    if voucher.status != FinancialDocumentStatus.APPROVED:
        raise ValidationError(
            _("Only an approved voucher may be posted."), code="voucher_not_approved"
        )
    if voucher.created_by_id == poster.pk:
        raise ValidationError(
            _("A voucher must be posted by somebody other than the person who wrote it."),
            code="voucher_self_posted",
        )

    payment_account = voucher.payment_account
    if payment_account is None:  # pragma: no cover - the check constraint forbids it
        raise ValidationError(
            _("The voucher names no payment source."), code="voucher_has_no_payment_source"
        )

    lines = list(voucher.lines.select_related("account", "cost_center").order_by("sequence"))
    if not lines:
        raise ValidationError(_("A voucher needs at least one line."), code="voucher_has_no_lines")

    posting_lines = [
        PostingLine(
            account=line.account,
            branch=voucher.branch,
            cost_center=line.cost_center,
            debit=line.amount,
            credit=ZERO,
            narration=line.description or voucher.reason[:255],
        )
        for line in lines
    ]
    posting_lines.append(
        PostingLine(
            account=payment_account,
            branch=voucher.branch,
            cost_center=None,
            debit=ZERO,
            # The sum of the lines, not a separately rounded total: a document
            # total is the SUM of its posted lines (CLAUDE.md).
            credit=sum((line.amount for line in lines), ZERO),
            narration=voucher.beneficiary,
        )
    )

    entry = post_entry(
        organization=voucher.organization,
        accounting_date=voucher.business_date,
        document_date=voucher.expense_date,
        lines=posting_lines,
        narration=f"{voucher.number} — {voucher.beneficiary}",
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(voucher.public_id),
        source_event="POSTED",
        idempotency_key=f"expense:{voucher.public_id}:post",
        posting_rule_version="expense-v1",
    )

    before = snapshot(voucher)
    voucher.status = FinancialDocumentStatus.POSTED
    voucher.posted_by = poster
    voucher.posted_at = timezone.now()
    voucher.journal_entry = entry
    voucher.full_clean()
    voucher.save(update_fields=["status", "posted_by", "posted_at", "journal_entry", "updated_at"])
    record_audit_event(
        action=AuditAction.POSTED,
        target=voucher,
        branch=voucher.branch,
        previous_state=before,
        new_state=snapshot(voucher),
        reason=reason,
        metadata={"entry_number": entry.entry_number, "total": str(voucher.total_amount)},
    )
    return voucher


@transaction.atomic
def reverse_expense_voucher(*, voucher: ExpenseVoucher, actor: User, reason: str) -> ExpenseVoucher:
    """
    Reverse a posted voucher, exactly.

    The original stays in the ledger and a mirrored entry is appended. A
    correction is a reversal plus a new voucher, never an edit — the posted
    voucher and its lines are immutable from here.
    """
    if voucher.status != FinancialDocumentStatus.POSTED:
        raise ValidationError(
            _("Only a posted voucher may be reversed."), code="voucher_not_posted"
        )
    if not reason.strip():
        raise ValidationError(
            _("Reversing a voucher requires a reason."), code="reversal_reason_required"
        )
    if voucher.journal_entry is None:  # pragma: no cover - the constraint forbids it
        raise ValidationError(
            _("The voucher carries no journal to reverse."), code="voucher_has_no_journal"
        )

    reversal = reverse_entry(
        entry=voucher.journal_entry,
        # Derived from the voucher, not generated: a retried reversal must find
        # the entry it already made rather than append a second mirror.
        idempotency_key=f"expense:{voucher.public_id}:reverse",
        reason=reason.strip(),
    )

    before = snapshot(voucher)
    voucher.status = FinancialDocumentStatus.REVERSED
    voucher.reversal_entry = reversal
    voucher.full_clean()
    voucher.save(update_fields=["status", "reversal_entry", "updated_at"])
    record_audit_event(
        action=AuditAction.REVERSED,
        target=voucher,
        branch=voucher.branch,
        previous_state=before,
        new_state=snapshot(voucher),
        reason=reason.strip(),
        metadata={"reversal_entry": reversal.entry_number},
    )
    return voucher


@transaction.atomic
def discard_expense_voucher(*, voucher: ExpenseVoucher, reason: str = "") -> None:
    """Abandon a draft. Nothing that reached the ledger can take this path."""
    if not voucher.is_editable:
        raise ValidationError(
            _("Only a draft voucher may be discarded."), code="voucher_not_a_draft"
        )
    before = snapshot(voucher)
    record_audit_event(
        action=AuditAction.DELETED,
        target=voucher,
        branch=voucher.branch,
        previous_state=before,
        reason=reason,
    )
    voucher.delete()


__all__ = [
    "EXPENSE_LINE_CLASSES",
    "SOURCE_DOCUMENT_TYPE",
    "add_expense_line",
    "approve_expense_voucher",
    "discard_expense_voucher",
    "open_expense_voucher",
    "post_expense_voucher",
    "recompute_total",
    "remove_expense_line",
    "reverse_expense_voucher",
]
