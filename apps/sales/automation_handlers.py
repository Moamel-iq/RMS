"""Safe, retryable automation handlers owned by the Sales domain."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from apps.core.automation import open_exception, register_handler, resolve_exception
from apps.core.models import AutomationDataSensitivity, AutomationOutboxEvent, AutomationSeverity
from apps.organizations.models import Role
from apps.sales.models import DailyFinancialClose

DAILY_CLOSE_CAPTURED_EVENT = "sales.daily_financial_close.captured"

_TITLE = {
    "tender_declaration_mismatch": "فرق بين إقرار وسجل وسيلة الدفع",
    "cashier_shift_missing": "إقفال الكاشير مفقود",
    "cashier_shift_not_closed": "إقفال الكاشير لم يكتمل",
    "cash_count_variance": "فرق في عدّ النقد",
    "card_count_variance": "فرق في عدّ البطاقة",
}


def _amount(row: dict[str, object]) -> Decimal | None:
    raw = row.get("difference")
    if raw in (None, ""):
        return None
    try:
        return abs(Decimal(str(raw)))
    except (InvalidOperation, ValueError) as error:
        raise ValidationError(
            "Daily-close snapshot contains an invalid monetary difference.",
            code="daily_close_snapshot_invalid_difference",
        ) from error


@register_handler(DAILY_CLOSE_CAPTURED_EVENT)
def materialize_daily_close_exceptions(event: AutomationOutboxEvent) -> None:
    """
    Persist the current close findings as owned, blocking exceptions.

    The immutable DailyFinancialClose retains the evidence for *this* attempt.
    These rows instead model the current operational condition of the SalesDay:
    a clean later capture resolves an earlier condition but never changes the
    blocked attempt that proved it existed.
    """

    close_id = event.payload.get("daily_financial_close_id")
    try:
        close = DailyFinancialClose.objects.select_related(
            "sales_day", "organization", "branch"
        ).get(pk=int(str(close_id)))
    except (DailyFinancialClose.DoesNotExist, TypeError, ValueError) as error:
        raise ValidationError(
            "The daily-close automation event does not name a valid close.",
            code="daily_close_event_invalid",
        ) from error
    if close.organization_id != event.organization_id or close.branch_id != event.branch_id:
        raise ValidationError(
            "Daily-close automation event has an invalid organization or branch scope.",
            code="daily_close_event_scope_invalid",
        )

    snapshot = close.reconciliation_snapshot or {}
    findings = snapshot.get("exceptions", [])
    if not isinstance(findings, list):
        raise ValidationError(
            "Daily-close snapshot exceptions must be a list.",
            code="daily_close_snapshot_invalid",
        )
    seen_codes: set[str] = set()
    for raw in findings:
        if not isinstance(raw, dict):
            raise ValidationError(
                "Daily-close snapshot exception is invalid.",
                code="daily_close_snapshot_invalid",
            )
        code = str(raw.get("code", "")).strip()
        if code not in _TITLE:
            raise ValidationError(
                "Daily-close snapshot exception code is unknown.",
                code="daily_close_snapshot_unknown_exception",
            )
        seen_codes.add(code)
        open_exception(
            organization=close.organization,
            branch=close.branch,
            code=code,
            target=close.sales_day,
            severity=AutomationSeverity.HIGH,
            is_blocking=True,
            sensitivity=AutomationDataSensitivity.FINANCIAL,
            amount=_amount(raw),
            owner_role=Role.ACCOUNTING_MANAGER,
            details=raw,
            source_event=event,
            title=_TITLE[code],
            summary="استثناء إقفال مالي يومي يحتاج تصحيح المصدر ثم مراجعة مستقلة.",
        )

    for code in _TITLE:
        if code not in seen_codes:
            resolve_exception(
                organization=close.organization,
                branch=close.branch,
                code=code,
                target=close.sales_day,
                resolution="لم يظهر هذا الاستثناء في أحدث لقطة إقفال يومي.",
            )
