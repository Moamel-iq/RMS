"""
المستحقات والمقدمات — accruals and prepayments.

Two workflows that look alike and are not. An **accrual** recognises a cost
before the paperwork arrives and has to stop recognising it when the paperwork
does. A **prepayment** recognises a payment before the cost is consumed and has
to release it over time, exactly.

Both resolve their accounts through `resolve_default_account` and the role
indirection. No account id and no account code is written into this module.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    ACCRUED_EXPENSES_PAYABLE,
    Account,
    AccountClass,
    AccrualDocument,
    AccrualLine,
    AmortizationFrequency,
    CostCenter,
    FinancialDocumentStatus,
    Prepayment,
    PrepaymentScheduleLine,
    ScheduleLineStatus,
)
from apps.accounting.services import post_entry, resolve_default_account, reverse_entry
from apps.accounting.validators import PostingLine
from apps.core.allocation import AllocationItem, allocate
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.users.models import User

ZERO = Decimal("0")

#: Stored, upper-case. `canonical_source_identity` case-folds the type before
#: persisting it, so a constant in the natural spelling would write one string
#: and then look for another — and a reversal would not find its own journal.
ACCRUAL_SOURCE_TYPE = "ACCOUNTING.ACCRUALDOCUMENT"
PREPAYMENT_SOURCE_TYPE = "ACCOUNTING.PREPAYMENT"
PREPAYMENT_LINE_SOURCE_TYPE = "ACCOUNTING.PREPAYMENTSCHEDULELINE"

#: Which classes an expense side may name. The same set the expense voucher
#: uses, and for the same reason: an accrual that credited revenue would be a
#: sale nobody recorded as one.
EXPENSE_CLASSES = frozenset(
    {AccountClass.COST_OF_SALES, AccountClass.OPERATING_EXPENSE, AccountClass.OTHER}
)


def _next_number(manager: Any, *, organization_id: int, prefix: str, year: int) -> str:
    """
    A per-organization, per-year document number, taken when the draft is left.

    The manager is passed in rather than the model class: `type` has no
    `objects` as far as a type checker is concerned, and reaching through the
    class here would be the one place this file lied about what it does.
    """
    stem = f"{prefix}-{year}-"
    last = (
        manager.filter(organization_id=organization_id, number__startswith=stem).aggregate(
            highest=Max("number")
        )["highest"]
        or ""
    )
    sequence = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{stem}{sequence:05d}"


# ---------------------------------------------------------------------------
# Accruals (ADR-030 §4)
# ---------------------------------------------------------------------------


@transaction.atomic
def add_accrual_line(
    *,
    accrual: AccrualDocument,
    account: Account,
    amount: Decimal,
    cost_center: CostCenter | None = None,
    description: str = "",
) -> AccrualLine:
    if not accrual.is_editable:
        raise ValidationError(_("Only a draft accrual may be changed."), code="not_a_draft")
    if account.organization_id != accrual.organization_id:
        raise ValidationError(
            _("The account belongs to another organization."), code="account_organization_mismatch"
        )
    if not account.is_postable:
        raise ValidationError(
            _("Only a detail account accepts journal lines."), code="account_not_postable"
        )
    if account.account_class not in EXPENSE_CLASSES:
        raise ValidationError(
            _("An accrual line must name an expense account."), code="account_not_an_expense"
        )
    if account.requires_cost_center and cost_center is None:
        raise ValidationError(
            _("This account requires a cost center."), code="cost_center_required"
        )

    quantized = quantize_money(amount, field="amount")
    if quantized <= ZERO:
        raise ValidationError(_("A line amount must be positive."), code="amount_not_positive")

    sequence = (accrual.lines.aggregate(highest=Max("sequence"))["highest"] or 0) + 1
    line = AccrualLine(
        accrual=accrual,
        sequence=sequence,
        account=account,
        cost_center=cost_center,
        description=description.strip(),
        amount=quantized,
    )
    line.full_clean()
    line.save()
    _recompute_accrual_total(accrual)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=accrual,
        branch=accrual.branch,
        new_state=snapshot(accrual),
        reason=str(_("accrual line added")),
    )
    return line


def _recompute_accrual_total(accrual: AccrualDocument) -> AccrualDocument:
    """The header is the sum of its lines, never rounded independently."""
    accrual.total_amount = sum((line.amount for line in accrual.lines.all()), ZERO)
    accrual.save(update_fields=["total_amount", "updated_at"])
    return accrual


@transaction.atomic
def remove_accrual_line(*, line: AccrualLine) -> None:
    accrual = line.accrual
    if not accrual.is_editable:
        raise ValidationError(_("Only a draft accrual may be changed."), code="not_a_draft")
    line.delete()
    _recompute_accrual_total(accrual)


@transaction.atomic
def approve_accrual(
    *, accrual: AccrualDocument, approver: User, reason: str = ""
) -> AccrualDocument:
    """Release a draft. The creator may not approve their own."""
    if accrual.status != FinancialDocumentStatus.DRAFT:
        raise ValidationError(_("Only a draft may be approved."), code="not_a_draft")
    if not accrual.lines.exists():
        raise ValidationError(_("An accrual needs at least one line."), code="no_lines")
    if accrual.created_by_id is None:
        raise ValidationError(
            _("This accrual records no author, so a different approver cannot be shown."),
            code="author_unknown",
        )
    if accrual.created_by_id == approver.pk:
        raise ValidationError(
            _("An accrual must be approved by somebody other than its author."),
            code="self_approved",
        )

    _recompute_accrual_total(accrual)
    before = snapshot(accrual)
    accrual.status = FinancialDocumentStatus.APPROVED
    accrual.approved_by = approver
    accrual.approved_at = timezone.now()
    accrual.number = _next_number(
        AccrualDocument.objects,
        organization_id=accrual.organization_id,
        prefix="ACR",
        year=accrual.business_date.year,
    )
    accrual.full_clean()
    accrual.save(update_fields=["status", "approved_by", "approved_at", "number", "updated_at"])
    record_audit_event(
        action=AuditAction.APPROVED,
        target=accrual,
        branch=accrual.branch,
        previous_state=before,
        new_state=snapshot(accrual),
        reason=reason,
    )
    return accrual


@transaction.atomic
def post_accrual(*, accrual: AccrualDocument, poster: User, reason: str = "") -> AccrualDocument:
    """`Dr Expense · Cr ACCRUED_EXPENSES_PAYABLE`, the liability resolved by role."""
    if accrual.status != FinancialDocumentStatus.APPROVED:
        raise ValidationError(_("Only an approved accrual may be posted."), code="not_approved")

    liability = resolve_default_account(
        organization=accrual.organization,
        account_role=ACCRUED_EXPENSES_PAYABLE,
        on_date=accrual.business_date,
    ).account

    lines = list(accrual.lines.select_related("account", "cost_center").order_by("sequence"))
    posting = [
        PostingLine(
            account=line.account,
            branch=accrual.branch,
            cost_center=line.cost_center,
            debit=line.amount,
            credit=ZERO,
            narration=line.description or accrual.description,
        )
        for line in lines
    ]
    posting.append(
        PostingLine(
            account=liability,
            branch=accrual.branch,
            cost_center=None,
            debit=ZERO,
            credit=sum((line.amount for line in lines), ZERO),
            narration=accrual.description,
        )
    )

    entry = post_entry(
        organization=accrual.organization,
        accounting_date=accrual.business_date,
        document_date=accrual.business_date,
        lines=posting,
        narration=f"{accrual.number} — {accrual.description}",
        source_document_type=ACCRUAL_SOURCE_TYPE,
        source_document_id=str(accrual.public_id),
        source_event="POSTED",
        idempotency_key=f"accrual:{accrual.public_id}:post",
        posting_rule_version="accrual-v1",
    )

    before = snapshot(accrual)
    accrual.status = FinancialDocumentStatus.POSTED
    accrual.posted_by = poster
    accrual.posted_at = timezone.now()
    accrual.journal_entry = entry
    accrual.full_clean()
    accrual.save(update_fields=["status", "posted_by", "posted_at", "journal_entry", "updated_at"])
    record_audit_event(
        action=AuditAction.POSTED,
        target=accrual,
        branch=accrual.branch,
        previous_state=before,
        new_state=snapshot(accrual),
        reason=reason,
        metadata={"entry_number": entry.entry_number},
    )
    return accrual


@transaction.atomic
def reverse_accrual(
    *, accrual: AccrualDocument, reason: str, invoice: object | None = None
) -> AccrualDocument:
    """
    Unwind a posted accrual, and optionally record which invoice replaced it.

    **Linking is not creating.** Accounting never writes a supplier invoice —
    that document belongs to Procurement and arrives through Procurement. This
    records which one superseded the accrual and reverses the accrual's own
    journal, so the expense stands recognised exactly once (ADR-030 §4).
    """
    if accrual.status != FinancialDocumentStatus.POSTED:
        raise ValidationError(_("Only a posted accrual may be reversed."), code="not_posted")
    if not reason.strip():
        raise ValidationError(_("Reversing an accrual requires a reason."), code="reason_required")
    if accrual.journal_entry is None:  # pragma: no cover - constraint forbids it
        raise ValidationError(_("The accrual carries no journal."), code="no_journal")

    if invoice is not None and getattr(invoice, "organization_id", None) != accrual.organization_id:
        raise ValidationError(
            _("The invoice belongs to another organization."), code="invoice_organization_mismatch"
        )

    reversal = reverse_entry(
        entry=accrual.journal_entry,
        idempotency_key=f"accrual:{accrual.public_id}:reverse",
        reason=reason.strip(),
    )

    before = snapshot(accrual)
    accrual.status = FinancialDocumentStatus.REVERSED
    accrual.reversal_entry = reversal
    if invoice is not None:
        accrual.settled_by_invoice = invoice  # type: ignore[assignment]
    accrual.full_clean()
    accrual.save(update_fields=["status", "reversal_entry", "settled_by_invoice", "updated_at"])
    record_audit_event(
        action=AuditAction.REVERSED,
        target=accrual,
        branch=accrual.branch,
        previous_state=before,
        new_state=snapshot(accrual),
        reason=reason.strip(),
    )
    return accrual


# ---------------------------------------------------------------------------
# Prepayments (ADR-030 §5)
# ---------------------------------------------------------------------------


def _period_bounds(
    *, start: datetime.date, frequency: str, index: int
) -> tuple[datetime.date, datetime.date]:
    """The nth period's window, counting from `start`."""
    months = 1 if frequency == AmortizationFrequency.MONTHLY else 3
    begin = _add_months(start, months * index)
    end = _add_months(start, months * (index + 1)) - datetime.timedelta(days=1)
    return begin, end


def _add_months(day: datetime.date, months: int) -> datetime.date:
    """
    Shift a date by whole months, clamping the day.

    Clamped rather than rolled forward: a schedule starting on the 31st must
    produce February's period inside February, not on the 3rd of March where it
    would fall in the wrong accounting period.
    """
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    last_day = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    return datetime.date(year, month, min(day.day, last_day))


def build_schedule(*, prepayment: Prepayment) -> list[PrepaymentScheduleLine]:
    """
    Split the total across the periods **exactly**.

    `apps/core/allocation.py`, never `total / periods` rounded per period. This
    is the ADR-006 counterexample in another costume: 1,000,000 over three
    months at three decimals is 333,333.333 each, summing to 999,999.999 — one
    thousandth of a dinar the prepaid account can never shed, so it never
    reaches zero and the year cannot close without a plug.
    """
    if prepayment.schedule_lines.exclude(status=ScheduleLineStatus.PLANNED).exists():
        raise ValidationError(
            _("A posted schedule line is never re-planned."), code="schedule_has_posted_lines"
        )
    prepayment.schedule_lines.filter(status=ScheduleLineStatus.PLANNED).delete()

    items = [
        AllocationItem(sequence=index + 1, weight=Decimal("1"))
        for index in range(prepayment.period_count)
    ]
    shares = {row.sequence: row.amount for row in allocate(prepayment.total_amount, items)}

    created: list[PrepaymentScheduleLine] = []
    for index in range(prepayment.period_count):
        begin, end = _period_bounds(
            start=prepayment.start_date, frequency=prepayment.frequency, index=index
        )
        line = PrepaymentScheduleLine(
            prepayment=prepayment,
            sequence=index + 1,
            period_start=begin,
            period_end=end,
            amount=shares[index + 1],
            status=ScheduleLineStatus.PLANNED,
        )
        line.full_clean()
        line.save()
        created.append(line)
    return created


@transaction.atomic
def approve_prepayment(*, prepayment: Prepayment, approver: User, reason: str = "") -> Prepayment:
    if prepayment.status != FinancialDocumentStatus.DRAFT:
        raise ValidationError(_("Only a draft may be approved."), code="not_a_draft")
    if prepayment.created_by_id is None:
        raise ValidationError(
            _("This prepayment records no author, so a different approver cannot be shown."),
            code="author_unknown",
        )
    if prepayment.created_by_id == approver.pk:
        raise ValidationError(
            _("A prepayment must be approved by somebody other than its author."),
            code="self_approved",
        )

    total = sum((line.amount for line in prepayment.schedule_lines.all()), ZERO)
    if total != prepayment.total_amount:
        # The equality this document exists to keep. Refused rather than
        # silently corrected: a schedule that does not sum to its total means
        # something upstream is wrong, and adjusting a line here would hide it.
        raise ValidationError(
            _("The schedule sums to %(sum)s, not %(total)s."),
            code="schedule_total_mismatch",
            params={"sum": total, "total": prepayment.total_amount},
        )

    before = snapshot(prepayment)
    prepayment.status = FinancialDocumentStatus.APPROVED
    prepayment.approved_by = approver
    prepayment.approved_at = timezone.now()
    prepayment.number = _next_number(
        Prepayment.objects,
        organization_id=prepayment.organization_id,
        prefix="PRE",
        year=prepayment.business_date.year,
    )
    prepayment.full_clean()
    prepayment.save(update_fields=["status", "approved_by", "approved_at", "number", "updated_at"])
    record_audit_event(
        action=AuditAction.APPROVED,
        target=prepayment,
        branch=prepayment.branch,
        previous_state=before,
        new_state=snapshot(prepayment),
        reason=reason,
    )
    return prepayment


@transaction.atomic
def post_prepayment(*, prepayment: Prepayment, poster: User, reason: str = "") -> Prepayment:
    """`Dr PREPAID_EXPENSE · Cr cash/bank` — the payment itself, not its consumption."""
    if prepayment.status != FinancialDocumentStatus.APPROVED:
        raise ValidationError(_("Only an approved prepayment may be posted."), code="not_approved")
    payment_account = prepayment.payment_account
    if payment_account is None:  # pragma: no cover - constraint forbids it
        raise ValidationError(_("No payment source."), code="no_payment_source")

    entry = post_entry(
        organization=prepayment.organization,
        accounting_date=prepayment.business_date,
        document_date=prepayment.business_date,
        lines=[
            PostingLine(
                account=prepayment.prepaid_account,
                branch=prepayment.branch,
                cost_center=None,
                debit=prepayment.total_amount,
                credit=ZERO,
                narration=prepayment.description,
            ),
            PostingLine(
                account=payment_account,
                branch=prepayment.branch,
                cost_center=None,
                debit=ZERO,
                credit=prepayment.total_amount,
                narration=prepayment.description,
            ),
        ],
        narration=f"{prepayment.number} — {prepayment.description}",
        source_document_type=PREPAYMENT_SOURCE_TYPE,
        source_document_id=str(prepayment.public_id),
        source_event="POSTED",
        idempotency_key=f"prepayment:{prepayment.public_id}:post",
        posting_rule_version="prepayment-v1",
    )

    before = snapshot(prepayment)
    prepayment.status = FinancialDocumentStatus.POSTED
    prepayment.posted_by = poster
    prepayment.posted_at = timezone.now()
    prepayment.journal_entry = entry
    prepayment.full_clean()
    prepayment.save(
        update_fields=["status", "posted_by", "posted_at", "journal_entry", "updated_at"]
    )
    record_audit_event(
        action=AuditAction.POSTED,
        target=prepayment,
        branch=prepayment.branch,
        previous_state=before,
        new_state=snapshot(prepayment),
        reason=reason,
    )
    return prepayment


@transaction.atomic
def post_schedule_line(*, line: PrepaymentScheduleLine, reason: str = "") -> PrepaymentScheduleLine:
    """
    Amortize one period: `Dr Expense · Cr PREPAID_EXPENSE`.

    Refuses a closed period and says which one, rather than posting quietly into
    the current month. The accountant reopens it or posts a catch-up
    deliberately; what must not happen is the system choosing for them.
    """
    prepayment = line.prepayment
    if prepayment.status != FinancialDocumentStatus.POSTED:
        raise ValidationError(
            _("The prepayment itself is not posted yet."), code="prepayment_not_posted"
        )
    if line.status != ScheduleLineStatus.PLANNED:
        raise ValidationError(
            _("Only a planned schedule line may be posted."), code="line_not_planned"
        )

    entry = post_entry(
        organization=prepayment.organization,
        accounting_date=line.period_end,
        document_date=line.period_end,
        lines=[
            PostingLine(
                account=prepayment.expense_account,
                branch=prepayment.branch,
                cost_center=prepayment.cost_center,
                debit=line.amount,
                credit=ZERO,
                narration=prepayment.description,
            ),
            PostingLine(
                account=prepayment.prepaid_account,
                branch=prepayment.branch,
                cost_center=None,
                debit=ZERO,
                credit=line.amount,
                narration=prepayment.description,
            ),
        ],
        narration=f"{prepayment.number} #{line.sequence} — {prepayment.description}",
        source_document_type=PREPAYMENT_LINE_SOURCE_TYPE,
        source_document_id=f"{prepayment.public_id}:{line.sequence}",
        source_event="POSTED",
        idempotency_key=f"prepayment:{prepayment.public_id}:line:{line.sequence}",
        posting_rule_version="prepayment-v1",
    )

    line.status = ScheduleLineStatus.POSTED
    line.journal_entry = entry
    line.posted_at = timezone.now()
    line.full_clean()
    line.save(update_fields=["status", "journal_entry", "posted_at"])
    record_audit_event(
        action=AuditAction.POSTED,
        target=prepayment,
        branch=prepayment.branch,
        new_state=snapshot(prepayment),
        reason=reason or str(_("schedule line posted")),
        metadata={"sequence": line.sequence, "entry_number": entry.entry_number},
    )
    return line


@transaction.atomic
def reverse_schedule_line(*, line: PrepaymentScheduleLine, reason: str) -> PrepaymentScheduleLine:
    if line.status != ScheduleLineStatus.POSTED:
        raise ValidationError(_("Only a posted line may be reversed."), code="line_not_posted")
    if not reason.strip():
        raise ValidationError(_("Reversing requires a reason."), code="reason_required")
    if line.journal_entry is None:  # pragma: no cover - constraint forbids it
        raise ValidationError(_("The line carries no journal."), code="no_journal")

    reversal = reverse_entry(
        entry=line.journal_entry,
        idempotency_key=f"prepayment:{line.prepayment.public_id}:line:{line.sequence}:reverse",
        reason=reason.strip(),
    )
    line.status = ScheduleLineStatus.REVERSED
    line.reversal_entry = reversal
    line.full_clean()
    line.save(update_fields=["status", "reversal_entry"])
    record_audit_event(
        action=AuditAction.REVERSED,
        target=line.prepayment,
        branch=line.prepayment.branch,
        new_state=snapshot(line.prepayment),
        reason=reason.strip(),
        metadata={"sequence": line.sequence},
    )
    return line


__all__ = [
    "ACCRUAL_SOURCE_TYPE",
    "PREPAYMENT_LINE_SOURCE_TYPE",
    "PREPAYMENT_SOURCE_TYPE",
    "add_accrual_line",
    "approve_accrual",
    "approve_prepayment",
    "build_schedule",
    "post_accrual",
    "post_prepayment",
    "post_schedule_line",
    "remove_accrual_line",
    "reverse_accrual",
    "reverse_schedule_line",
]
