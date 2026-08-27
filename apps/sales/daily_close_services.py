"""
Persist and review the financial close of one sales day.

The close is intentionally an *evidence snapshot*, not a second sales ledger:
the sales lines, tender declarations and cashier shift remain authoritative.
What must survive is the comparison a reviewer saw at a particular time.  A
correction therefore creates a new close attempt instead of mutating an old
one, so an exception can never disappear by being overwritten.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import AccountingSettings
from apps.core.automation import enqueue_event
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.sales.models import (
    CashierShift,
    CashierShiftStatus,
    DailyFinancialClose,
    DailyFinancialCloseStatus,
    SalesDay,
    SalesDayStatus,
    SalesTenderSummary,
    TenderDestination,
)

ZERO = Decimal("0")
_TENDERS: tuple[str, ...] = (
    TenderDestination.CASH,
    TenderDestination.CARD,
    TenderDestination.APPLICATION_RECEIVABLE,
)


def daily_close_is_required(*, sales_day: SalesDay) -> bool:
    """
    Whether this day was entered after the organisation's control start date.

    An organisation without an AccountingSettings row is treated as starting
    today.  That is deliberately fail-closed for new activity while still not
    reclassifying a historical day as a failed close merely because the system
    was upgraded later.
    """

    enforced_from = (
        AccountingSettings.objects.filter(organization_id=sales_day.organization_id)
        .values_list("daily_close_enforced_from", flat=True)
        .first()
        or timezone.localdate()
    )
    return sales_day.business_date >= enforced_from


def _derived_by_tender(*, sales_day: SalesDay) -> dict[str, Decimal]:
    derived: dict[str, Decimal] = dict.fromkeys(_TENDERS, ZERO)
    for line in sales_day.lines.select_related("channel"):
        if line.is_application_sale:
            tender = TenderDestination.APPLICATION_RECEIVABLE
        elif line.channel.default_tender == TenderDestination.CARD:
            tender = TenderDestination.CARD
        else:
            tender = TenderDestination.CASH
        derived[tender] = derived[tender] + line.net_amount
    return {key: quantize_money(value) for key, value in derived.items()}


def _declared_by_tender(*, sales_day: SalesDay) -> dict[str, Decimal]:
    declared: dict[str, Decimal] = dict.fromkeys(_TENDERS, ZERO)
    for row in SalesTenderSummary.objects.filter(sales_day=sales_day):
        if row.tender in declared:
            declared[row.tender] = quantize_money(row.declared_amount)
    return declared


def _money(value: Decimal | None) -> str:
    """Serialise fixed-decimal evidence without JSON's binary float loss."""

    return f"{quantize_money(value or ZERO):f}"


def _capture(*, sales_day: SalesDay) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    declared = _declared_by_tender(sales_day=sales_day)
    derived = _derived_by_tender(sales_day=sales_day)
    tender_rows: list[dict[str, str]] = []
    exceptions: list[dict[str, Any]] = []
    for tender in _TENDERS:
        difference = quantize_money(declared[tender] - derived[tender])
        tender_rows.append(
            {
                "tender": tender,
                "declared": _money(declared[tender]),
                "derived": _money(derived[tender]),
                "difference": _money(difference),
            }
        )
        if difference:
            exceptions.append(
                {
                    "code": "tender_declaration_mismatch",
                    "tender": tender,
                    "declared": _money(declared[tender]),
                    "derived": _money(derived[tender]),
                    "difference": _money(difference),
                }
            )

    shift = (
        CashierShift.objects.filter(
            sales_day_id=sales_day.pk,
            branch_id=sales_day.branch_id,
            business_date=sales_day.business_date,
        )
        .prefetch_related("tender_counts")
        .first()
    )
    shift_snapshot: dict[str, Any] | None = None
    if shift is None:
        exceptions.append({"code": "cashier_shift_missing"})
    elif shift.status not in {CashierShiftStatus.CLOSED, CashierShiftStatus.APPROVED}:
        exceptions.append({"code": "cashier_shift_not_closed", "status": str(shift.status)})
        shift_snapshot = {"status": str(shift.status)}
    else:
        card_count = next(
            (row for row in shift.tender_counts.all() if row.tender == TenderDestination.CARD),
            None,
        )
        card_difference = quantize_money(
            (card_count.counted_amount if card_count is not None else ZERO)
            - (card_count.expected_amount if card_count is not None else ZERO)
        )
        shift_snapshot = {
            "status": str(shift.status),
            "expected_cash": _money(shift.expected_cash),
            "counted_cash": _money(shift.counted_cash),
            "cash_variance": _money(shift.variance_amount),
            "card_expected": _money(card_count.expected_amount if card_count else ZERO),
            "card_counted": _money(card_count.counted_amount if card_count else ZERO),
            "card_variance": _money(card_difference),
        }
        if shift.variance_amount:
            exceptions.append(
                {
                    "code": "cash_count_variance",
                    "difference": _money(shift.variance_amount),
                }
            )
        if card_difference:
            exceptions.append(
                {
                    "code": "card_count_variance",
                    "difference": _money(card_difference),
                }
            )

    return (
        {
            "captured_at": timezone.now().isoformat(),
            "sales_day": str(sales_day.public_id),
            "tenders": tender_rows,
            "cashier_shift": shift_snapshot,
            "exceptions": exceptions,
        },
        exceptions,
    )


@transaction.atomic
def submit_daily_financial_close(*, sales_day: SalesDay, actor: Any) -> DailyFinancialClose:
    """Freeze a new close attempt from a submitted day and its cashier evidence."""

    locked_day = SalesDay.objects.select_for_update().get(pk=sales_day.pk)
    if locked_day.status != SalesDayStatus.SUBMITTED:
        raise ValidationError(
            _("Only a submitted sales day can be sent for financial close."),
            code="sales_day_not_submitted",
        )

    payload, exceptions = _capture(sales_day=locked_day)
    attempt_number = (
        DailyFinancialClose.objects.filter(sales_day=locked_day).aggregate(
            maximum=Max("attempt_number")
        )["maximum"]
        or 0
    ) + 1
    close = DailyFinancialClose(
        organization=locked_day.organization,
        branch=locked_day.branch,
        sales_day=locked_day,
        business_date=locked_day.business_date,
        attempt_number=attempt_number,
        status=(
            DailyFinancialCloseStatus.BLOCKED if exceptions else DailyFinancialCloseStatus.SUBMITTED
        ),
        reconciliation_snapshot=payload,
        exception_count=len(exceptions),
        submitted_by=actor,
        submitted_at=timezone.now(),
    )
    close.full_clean()
    close.save()
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=close,
        new_state=snapshot(close),
        branch=locked_day.branch,
        metadata={"exception_count": len(exceptions), "exceptions": exceptions},
    )
    # This insert is in the same transaction as the immutable close.  A
    # rolled-back submission cannot later create a task for a close that never
    # existed; a retry of the worker is harmless because its handler dedupes
    # current exceptions by organization, SalesDay, and code.
    enqueue_event(
        organization=locked_day.organization,
        branch=locked_day.branch,
        event_type="sales.daily_financial_close.captured",
        idempotency_key=f"{locked_day.public_id}:daily-close:{close.attempt_number}",
        payload={"daily_financial_close_id": close.pk},
        source=close,
    )
    return close


@transaction.atomic
def approve_daily_financial_close(*, close: DailyFinancialClose, actor: Any) -> DailyFinancialClose:
    """A second person approves a clean, captured daily close."""

    locked = DailyFinancialClose.objects.select_for_update().get(pk=close.pk)
    if locked.status == DailyFinancialCloseStatus.BLOCKED:
        raise ValidationError(
            _("This daily close has exceptions and cannot be approved."),
            code="daily_close_blocked",
        )
    if locked.status != DailyFinancialCloseStatus.SUBMITTED:
        raise ValidationError(
            _("Only a submitted daily close can be approved."),
            code="daily_close_not_submitted",
        )
    if locked.submitted_by_id == actor.pk:
        raise ValidationError(
            _("The person who submitted the daily close cannot approve it."),
            code="daily_close_reviewer_is_submitter",
        )

    previous = snapshot(locked)
    locked.status = DailyFinancialCloseStatus.APPROVED
    locked.reviewed_by = actor
    locked.reviewed_at = timezone.now()
    locked.full_clean()
    locked.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        branch=locked.branch,
    )
    return locked


def require_approved_daily_financial_close(*, sales_day: SalesDay) -> None:
    """Refuse a controlled day unless its latest recorded close is approved."""

    if not daily_close_is_required(sales_day=sales_day):
        return
    latest = (
        DailyFinancialClose.objects.filter(sales_day=sales_day).order_by("-attempt_number").first()
    )
    if latest is None:
        raise ValidationError(
            _("A submitted daily financial close is required before posting this sales day."),
            code="daily_financial_close_required",
        )
    if latest.status != DailyFinancialCloseStatus.APPROVED:
        raise ValidationError(
            _("The latest daily financial close is not approved."),
            code="daily_financial_close_not_approved",
        )


__all__ = [
    "approve_daily_financial_close",
    "daily_close_is_required",
    "require_approved_daily_financial_close",
    "submit_daily_financial_close",
]
