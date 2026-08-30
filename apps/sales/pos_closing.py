"""Controlled cashier/accountant workflow for imported external-POS evidence."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.sales.models import PosSalesImportBatch, PosSalesImportStatus, SalesDayStatus

ZERO = Decimal("0")
REVIEW_STEPS = (1, 2, 3, 4, 5)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.001"))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError("وجدت قيمة مالية غير صالحة.") from error


def channel_summary(batch: PosSalesImportBatch) -> list[dict[str, Any]]:
    data = batch.report_data.get("item_sales_by_type", {})
    return [
        {"name": name or "غير مصنف", "amount": _decimal(amount)}
        for name, amount in data.get("channel_totals", {}).items()
    ]


def application_summary(batch: PosSalesImportBatch) -> list[dict[str, Any]]:
    from apps.sales.agreements import commission_for, inputs_from, resolve_agreement
    from apps.sales.models import DeliveryApplication

    applications = {
        app.name.strip(): app
        for app in DeliveryApplication.objects.filter(
            organization=batch.organization, is_active=True
        )
    }
    rows: list[dict[str, Any]] = []
    for line in batch.report_data.get("expenses", {}).get("lines", []):
        name = str(line.get("application") or "").strip()
        if not name:
            continue
        amount = _decimal(line.get("amount"))
        app = applications.get(name)
        agreement = (
            resolve_agreement(
                branch_id=batch.branch_id,
                delivery_application_id=app.pk,
                on_date=batch.business_date,
            )
            if app is not None
            else None
        )
        commission = ZERO
        if agreement is not None:
            commission = commission_for(
                inputs_from(
                    agreement,
                    gross_amount=amount,
                    restaurant_discount=ZERO,
                    application_discount=ZERO,
                    order_count=0,
                )
            ).total
        rows.append(
            {
                "name": name,
                "application": app,
                "gross": amount,
                "commission": commission,
                "net": amount - commission,
                "agreement": agreement,
            }
        )
    return rows


def operational_expenses(batch: PosSalesImportBatch) -> list[dict[str, Any]]:
    return [
        line
        for line in batch.report_data.get("expenses", {}).get("lines", [])
        if not line.get("application")
    ]


def review_summary(batch: PosSalesImportBatch) -> dict[str, Any]:
    data = batch.review_data or {}
    cash = data.get("step_4", {})
    qi = _decimal(cash.get("qi_card_amount"))
    withdrawals = _decimal(cash.get("withdrawals"))
    deposits = _decimal(cash.get("deposits"))
    non_application = batch.total_sales - batch.application_sales
    expected_cash = non_application - qi - batch.operational_expenses - withdrawals + deposits
    actual_cash = _decimal(cash.get("actual_cash"))
    return {
        "non_application_sales": non_application,
        "qi_card_amount": qi,
        "expected_cash": expected_cash,
        "actual_cash": actual_cash,
        "cash_variance": actual_cash - expected_cash,
        "withdrawals": withdrawals,
        "deposits": deposits,
    }


def posting_blockers(batch: PosSalesImportBatch) -> list[str]:
    blockers: list[str] = []
    if batch.warnings:
        blockers.append("توجد أصناف أو تطبيقات غير مربوطة.")
    if batch.files.count() != 6:
        blockers.append("حزمة الأدلة لا تحتوي التقارير الستة كاملة.")
    if batch.review_step < 5:
        blockers.append("خطوات مراجعة المحاسب غير مكتملة.")
    if batch.cashier_confirmed_by_id == batch.accountant_started_by_id and not getattr(
        batch.accountant_started_by, "is_superuser", False
    ):
        blockers.append("يجب أن يكون المحاسب المراجع شخصاً مختلفاً عن الكاشير المؤكد.")
    existing_day = batch.linked_sales_day
    if existing_day is None:
        from apps.sales.models import SalesDay

        existing_day = SalesDay.objects.filter(
            branch=batch.branch, business_date=batch.business_date
        ).first()
    if (
        existing_day is not None
        and existing_day.status
        in {
            SalesDayStatus.POSTED,
            SalesDayStatus.REVERSED,
        }
        and existing_day != batch.linked_sales_day
    ):
        blockers.append("يوجد يوم مبيعات مرحّل أو معكوس لنفس الفرع والتاريخ.")
    step4 = (batch.review_data or {}).get("step_4", {})
    if not step4.get("cashbox_id"):
        blockers.append("لم يُحدد صندوق النقد.")
    if _decimal(step4.get("qi_card_amount")) and not step4.get("qi_cashbox_id"):
        blockers.append("لم يُحدد صندوق كي كارد.")
    if batch.review_step >= 4:
        blockers.extend(_sales_configuration_blockers(batch))
    return list(dict.fromkeys(blockers))


def _sales_configuration_blockers(batch: PosSalesImportBatch) -> list[str]:
    """Read-only preflight for item/channel/recipe/price/application resolution."""

    from apps.sales.day_services import resolve_line
    from apps.sales.models import SalesDay

    day = (
        batch.linked_sales_day
        or SalesDay.objects.filter(branch=batch.branch, business_date=batch.business_date).first()
    )
    if day is None:
        day = SalesDay(
            organization=batch.organization,
            branch=batch.branch,
            business_date=batch.business_date,
        )
    menu = _menu_mapping(batch)
    applications = application_summary(batch)
    messages: list[str] = []
    if any(row["application"] is None or row["agreement"] is None for row in applications):
        messages.append("أكمل ربط التطبيقات وعقود العمولات الفعالة.")
    for source in batch.report_data.get("item_sales_by_type", {}).get("items", []):
        from apps.sales.pos_imports import normalize_name

        item = menu.get(normalize_name(source.get("name")))
        if item is None:
            messages.append(f"صنف POS غير مربوط: {source.get('name')}.")
            continue
        try:
            channel = _channel_for(batch, str(source.get("channel") or ""))
            application = (
                next((row["application"] for row in applications if row["application"]), None)
                if channel.requires_delivery_application
                else None
            )
            resolved = resolve_line(
                day=day,
                menu_item=item,
                channel=channel,
                quantity=_decimal(source.get("quantity")),
                delivery_application=application,
            )
            imported = _decimal(source.get("amount"))
            if imported > resolved.gross_amount:
                messages.append(f"سعر POS للصنف «{item.name}» أعلى من السعر الفعال.")
        except ValidationError as error:
            messages.append(f"{item.name}: {' '.join(error.messages)}")
        if len(set(messages)) >= 8:
            messages.append("توجد متطلبات أصناف إضافية؛ أصلح الظاهر ثم أعد الفحص.")
            break
    return list(dict.fromkeys(messages))


@transaction.atomic
def confirm_by_cashier(*, batch: PosSalesImportBatch, actor: Any) -> PosSalesImportBatch:
    locked = PosSalesImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status not in {
        PosSalesImportStatus.AWAITING_CASHIER,
        PosSalesImportStatus.RETURNED_TO_CASHIER,
    }:
        raise ValidationError("هذه الدفعة ليست بانتظار تأكيد الكاشير.")
    if locked.warnings or not all(check.get("ok") for check in locked.checks):
        raise ValidationError("عالج تنبيهات الربط والمطابقة قبل تأكيد المبيعات.")
    if locked.files.count() != 6:
        raise ValidationError("يجب أن تبقى التقارير الستة مرفقة قبل التأكيد.")
    before = snapshot(locked)
    locked.status = PosSalesImportStatus.AWAITING_ACCOUNTANT
    locked.cashier_confirmed_by = actor
    locked.cashier_confirmed_at = timezone.now()
    locked.review_step = 0
    locked.review_data = {}
    locked.return_reason = ""
    locked.save()
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
        metadata={"file_checksums": list(locked.files.values_list("checksum", flat=True))},
    )
    return locked


@transaction.atomic
def start_accountant_review(*, batch: PosSalesImportBatch, actor: Any) -> PosSalesImportBatch:
    locked = PosSalesImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status not in {
        PosSalesImportStatus.AWAITING_ACCOUNTANT,
        PosSalesImportStatus.ACCOUNTANT_REVIEW,
    }:
        raise ValidationError("هذه الدفعة ليست بانتظار مراجعة المحاسب.")
    if locked.cashier_confirmed_by_id == actor.pk and not actor.is_superuser:
        raise ValidationError("لا يستطيع مؤكد المبيعات مراجعتها محاسبياً.")
    if locked.accountant_started_by_id and locked.accountant_started_by_id != actor.pk:
        raise ValidationError("بدأ محاسب آخر مراجعة هذه الدفعة.")
    if locked.status == PosSalesImportStatus.AWAITING_ACCOUNTANT:
        before = snapshot(locked)
        locked.status = PosSalesImportStatus.ACCOUNTANT_REVIEW
        locked.accountant_started_by = actor
        locked.accountant_started_at = timezone.now()
        locked.save()
        record_audit_event(
            action=(
                AuditAction.PERMISSION_OVERRIDE
                if locked.cashier_confirmed_by_id == actor.pk
                else AuditAction.UPDATED
            ),
            target=locked,
            branch=locked.branch,
            previous_state=before,
            new_state=snapshot(locked),
            reason=(
                "بدء مراجعة استيراد مبيعات POS — تجاوز موثق لمدير النظام"
                if locked.cashier_confirmed_by_id == actor.pk
                else "بدء مراجعة استيراد مبيعات POS"
            ),
            metadata=(
                {"override": "superuser_self_review"}
                if locked.cashier_confirmed_by_id == actor.pk
                else {}
            ),
        )
    return locked


@transaction.atomic
def save_review_step(
    *, batch: PosSalesImportBatch, actor: Any, step: int, evidence: dict[str, Any]
) -> PosSalesImportBatch:
    if step not in REVIEW_STEPS:
        raise ValidationError("خطوة المراجعة غير صالحة.")
    locked = PosSalesImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status != PosSalesImportStatus.ACCOUNTANT_REVIEW:
        raise ValidationError("ابدأ مراجعة المحاسب أولاً.")
    if locked.accountant_started_by_id != actor.pk:
        raise ValidationError("هذه المراجعة مسندة إلى محاسب آخر.")
    expected = min(locked.review_step + 1, 5)
    if step != expected:
        raise ValidationError("يجب إكمال خطوات المراجعة بالترتيب.")
    before = snapshot(locked)
    data = dict(locked.review_data or {})
    data[f"step_{step}"] = {**evidence, "reviewed_at": timezone.now().isoformat()}
    locked.review_data = data
    locked.review_step = step
    if step == 5:
        blockers = posting_blockers(locked)
        if blockers:
            raise ValidationError(blockers)
        locked.status = PosSalesImportStatus.READY_TO_POST
    locked.save()
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
        reason=f"اعتماد خطوة مراجعة POS رقم {step}",
        metadata={"step": step, "evidence": evidence},
    )
    return locked


@transaction.atomic
def return_to_cashier(
    *, batch: PosSalesImportBatch, actor: Any, reason: str
) -> PosSalesImportBatch:
    if not reason.strip():
        raise ValidationError("سبب الإعادة إلى الكاشير مطلوب.")
    locked = PosSalesImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status not in {
        PosSalesImportStatus.AWAITING_ACCOUNTANT,
        PosSalesImportStatus.ACCOUNTANT_REVIEW,
        PosSalesImportStatus.READY_TO_POST,
    }:
        raise ValidationError("لا يمكن إعادة هذه الدفعة في حالتها الحالية.")
    before = snapshot(locked)
    locked.status = PosSalesImportStatus.RETURNED_TO_CASHIER
    locked.returned_by = actor
    locked.returned_at = timezone.now()
    locked.return_reason = reason.strip()
    locked.review_step = 0
    locked.review_data = {}
    locked.accountant_started_by = None
    locked.accountant_started_at = None
    locked.save()
    record_audit_event(
        action=AuditAction.REJECTED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


@transaction.atomic
def mark_posted_from_sales_day(*, batch: PosSalesImportBatch, actor: Any) -> PosSalesImportBatch:
    """Close the import only after its canonical SalesDay has actually posted."""

    locked = (
        PosSalesImportBatch.objects.select_for_update()
        .select_related("linked_sales_day")
        .get(pk=batch.pk)
    )
    if locked.status != PosSalesImportStatus.READY_TO_POST:
        raise ValidationError("الدفعة ليست جاهزة للترحيل.")
    if locked.linked_sales_day is None or locked.linked_sales_day.status != SalesDayStatus.POSTED:
        raise ValidationError(
            "لم يُرحّل مستند يوم المبيعات بعد. أُبقيت الدفعة جاهزة من دون إنشاء قيد مكرر."
        )
    before = snapshot(locked)
    locked.status = PosSalesImportStatus.POSTED
    locked.posted_by = actor
    locked.posted_at = timezone.now()
    locked.posting_reference = locked.linked_sales_day.number
    locked.save()
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
        source_document_type="SALES.SALESDAY",
        source_document_id=str(locked.linked_sales_day.public_id),
    )
    return locked


def _allocate(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Deterministic 0.001 allocation whose rows add back to ``total``."""

    if not weights or sum(weights, ZERO) <= ZERO:
        return []
    remaining = _decimal(total)
    denominator = sum(weights, ZERO)
    result: list[Decimal] = []
    for index, weight in enumerate(weights):
        amount = remaining if index == len(weights) - 1 else _decimal(total * weight / denominator)
        result.append(amount)
        remaining -= amount
    return result


def _menu_mapping(batch: PosSalesImportBatch) -> dict[str, Any]:
    from apps.sales.models import MenuItem, PosMenuItemMapping
    from apps.sales.pos_imports import normalize_name

    result = {
        normalize_name(item.name): item
        for item in MenuItem.objects.filter(organization=batch.organization, is_active=True)
    }
    for mapping in PosMenuItemMapping.objects.filter(
        organization=batch.organization
    ).select_related("menu_item"):
        result[mapping.normalized_source_name] = mapping.menu_item
    return result


def _channel_for(batch: PosSalesImportBatch, source_name: str) -> Any:
    from apps.sales.models import SalesChannel, SalesChannelCategory
    from apps.sales.pos_imports import normalize_name

    normalized = normalize_name(source_name)
    channels = list(
        SalesChannel.objects.filter(organization=batch.organization, is_active=True).order_by(
            "display_order", "code"
        )
    )
    direct = next((row for row in channels if normalize_name(row.name) == normalized), None)
    if direct is not None:
        return direct
    categories = {
        "صالة": SalesChannelCategory.DINE_IN,
        "سفري": SalesChannelCategory.TAKEAWAY,
        "ديليفري": SalesChannelCategory.DIRECT_DELIVERY,
        "دليفري": SalesChannelCategory.DIRECT_DELIVERY,
        "ديليفري تطبيق": SalesChannelCategory.DELIVERY_APPLICATION,
        "دليفري تطبيق": SalesChannelCategory.DELIVERY_APPLICATION,
    }
    category = categories.get(normalized)
    channel = next((row for row in channels if row.category == category), None)
    if channel is None:
        raise ValidationError(f"لا توجد قناة مبيعات فعالة تقابل «{source_name}».")
    return channel


@transaction.atomic
def post_and_close_import(*, batch: PosSalesImportBatch, actor: Any) -> PosSalesImportBatch:
    """Create/post the canonical SalesDay and routed expenses as one transaction."""

    from apps.accounting.expense_services import (
        add_expense_line,
        approve_expense_voucher,
        open_expense_voucher,
        post_expense_voucher,
    )
    from apps.accounting.models import Account, Cashbox, CostCenter
    from apps.sales.day_services import (
        add_sales_line,
        create_sales_day,
        resolve_line,
        set_tender_summary,
        submit_sales_day,
    )
    from apps.sales.models import (
        SalesChannel,
        SalesDay,
        TenderDestination,
    )
    from apps.sales.pos_imports import normalize_name
    from apps.sales.posting import post_sales_day

    locked = PosSalesImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status != PosSalesImportStatus.READY_TO_POST:
        raise ValidationError("الدفعة ليست جاهزة للترحيل.")
    blockers = posting_blockers(locked)
    if blockers:
        raise ValidationError(blockers)
    if locked.cashier_confirmed_by_id == actor.pk and not actor.is_superuser:
        raise ValidationError("لا يستطيع الكاشير المؤكد ترحيل الدفعة نفسها.")
    cashier = locked.cashier_confirmed_by
    if cashier is None:
        raise ValidationError("الدفعة لا تحتوي مؤكد مبيعات موثقاً.")

    day = (
        SalesDay.objects.select_for_update()
        .filter(branch=locked.branch, business_date=locked.business_date)
        .first()
    )
    if day is None:
        day = create_sales_day(
            organization=locked.organization,
            branch=locked.branch,
            business_date=locked.business_date,
            actor=cashier,
            notes=f"مستورد من POS: {locked.public_id}",
        )
    elif day.status != SalesDayStatus.DRAFT or day.lines.exists():
        raise ValidationError("يوجد مستند يوم مبيعات غير فارغ لنفس الفرع والتاريخ.")

    menu = _menu_mapping(locked)
    applications = application_summary(locked)
    if any(row["application"] is None or row["agreement"] is None for row in applications):
        raise ValidationError("أكمل ربط التطبيقات وعقود العمولات الفعالة قبل الترحيل.")
    app_weights = [row["gross"] for row in applications]
    data_lines = locked.report_data.get("item_sales_by_type", {}).get("items", [])
    qi_amount = _decimal((locked.review_data or {}).get("step_4", {}).get("qi_card_amount"))
    direct_total = locked.total_sales - locked.application_sales
    card_channel = None
    if qi_amount:
        card_channel = SalesChannel.objects.filter(
            organization=locked.organization,
            is_active=True,
            default_tender=TenderDestination.CARD,
            requires_delivery_application=False,
        ).first()
        if card_channel is None:
            raise ValidationError("أنشئ قناة بيع مخصصة لكي كارد قبل ترحيل مبلغ البطاقة.")

    for source in data_lines:
        source_name = normalize_name(source.get("name"))
        menu_item = menu.get(source_name)
        if menu_item is None:
            raise ValidationError(f"صنف POS غير مربوط: {source.get('name')}.")
        original_channel = _channel_for(locked, str(source.get("channel") or ""))
        quantity = _decimal(source.get("quantity"))
        imported_amount = _decimal(source.get("amount"))
        if original_channel.requires_delivery_application:
            amount_parts = _allocate(imported_amount, app_weights)
            quantity_parts = _allocate(quantity, app_weights)
            allocations = [
                (original_channel, row["application"], part_qty, part_amount)
                for row, part_qty, part_amount in zip(
                    applications, quantity_parts, amount_parts, strict=True
                )
                if part_qty > ZERO
            ]
        elif qi_amount and direct_total > ZERO:
            card_part_amount = _decimal(imported_amount * qi_amount / direct_total)
            card_part_qty = _decimal(quantity * qi_amount / direct_total)
            allocations = []
            if quantity - card_part_qty > ZERO:
                allocations.append(
                    (
                        original_channel,
                        None,
                        quantity - card_part_qty,
                        imported_amount - card_part_amount,
                    )
                )
            if card_part_qty > ZERO:
                allocations.append((card_channel, None, card_part_qty, card_part_amount))
        else:
            allocations = [(original_channel, None, quantity, imported_amount)]

        for channel, application, part_quantity, part_amount in allocations:
            resolved = resolve_line(
                day=day,
                menu_item=menu_item,
                channel=channel,
                quantity=part_quantity,
                delivery_application=application,
            )
            discount = resolved.gross_amount - part_amount
            if discount < ZERO:
                raise ValidationError(
                    f"سعر POS للصنف «{menu_item.name}» أعلى من السعر الفعال بمبلغ {-discount}."
                )
            add_sales_line(
                day=day,
                menu_item=menu_item,
                channel=channel,
                quantity=part_quantity,
                delivery_application=application,
                manual_discount_amount=discount if discount else None,
                manual_discount_reason="فرق سعر موثق في تقرير POS" if discount else "",
                notes=f"POS row {source.get('row')}",
            )

    declared_cash = direct_total - qi_amount
    set_tender_summary(day=day, tender=TenderDestination.CASH, declared_amount=declared_cash)
    set_tender_summary(day=day, tender=TenderDestination.CARD, declared_amount=qi_amount)
    set_tender_summary(
        day=day,
        tender=TenderDestination.APPLICATION_RECEIVABLE,
        declared_amount=locked.application_sales,
    )
    day = submit_sales_day(day=day, actor=cashier)
    day = post_sales_day(day=day, actor=actor)

    step3 = (locked.review_data or {}).get("step_3", {}).get("routes", {})
    step4 = (locked.review_data or {}).get("step_4", {})
    cashbox = Cashbox.objects.filter(
        pk=step4.get("cashbox_id"),
        organization=locked.organization,
        branch=locked.branch,
        is_active=True,
    ).first()
    if cashbox is None:
        raise ValidationError("صندوق دفع المصروفات غير صالح.")
    expense_ids: list[int] = []
    skipped_expenses: list[dict[str, Any]] = []
    for expense in operational_expenses(locked):
        route = step3.get(str(expense.get("row")), {})
        account_id = route.get("account_id")
        center_id = route.get("cost_center_id")
        if not account_id:
            skipped_expenses.append(
                {
                    "row": expense.get("row"),
                    "type": expense.get("type"),
                    "amount": str(expense.get("amount")),
                    "reason": "no_account_selected",
                }
            )
            continue
        account = Account.objects.filter(
            pk=account_id, organization=locked.organization, is_active=True
        ).first()
        center = (
            CostCenter.objects.filter(
                pk=center_id, organization=locked.organization, is_active=True
            ).first()
            if center_id
            else None
        )
        if account is None or (center_id and center is None):
            raise ValidationError("أحد توجيهات المصروفات لم يعد صالحاً.")
        if account.requires_cost_center and center is None:
            skipped_expenses.append(
                {
                    "row": expense.get("row"),
                    "type": expense.get("type"),
                    "amount": str(expense.get("amount")),
                    "reason": "selected_account_requires_cost_center",
                }
            )
            continue
        voucher = open_expense_voucher(
            branch=locked.branch,
            business_date=locked.business_date,
            expense_date=locked.business_date,
            beneficiary=str(expense.get("type") or "مصروف POS"),
            reason=str(expense.get("details") or expense.get("type") or "مصروف تشغيلي"),
            created_by=cashier,
            cashbox=cashbox,
            evidence_reference=f"POS {locked.public_id} row {expense.get('row')}",
            notes=str(route.get("notes") or ""),
        )
        add_expense_line(
            voucher=voucher,
            account=account,
            cost_center=center,
            amount=_decimal(expense.get("amount")),
            description=str(expense.get("details") or expense.get("type") or ""),
        )
        voucher = approve_expense_voucher(voucher=voucher, approver=actor)
        voucher = post_expense_voucher(voucher=voucher, poster=actor)
        expense_ids.append(voucher.pk)

    before = snapshot(locked)
    review_data = dict(locked.review_data or {})
    review_data["posting"] = {
        "sales_day_id": day.pk,
        "expense_voucher_ids": expense_ids,
        "skipped_expenses": skipped_expenses,
        "posted_at": timezone.now().isoformat(),
    }
    locked.review_data = review_data
    locked.linked_sales_day = day
    locked.status = PosSalesImportStatus.POSTED
    locked.posted_by = actor
    locked.posted_at = timezone.now()
    locked.posting_reference = day.number
    locked.save()
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
        source_document_type="SALES.SALESDAY",
        source_document_id=str(day.public_id),
        metadata={
            "expense_voucher_ids": expense_ids,
            "skipped_expenses": skipped_expenses,
        },
    )
    return locked


__all__ = [
    "application_summary",
    "channel_summary",
    "confirm_by_cashier",
    "mark_posted_from_sales_day",
    "operational_expenses",
    "post_and_close_import",
    "posting_blockers",
    "return_to_cashier",
    "review_summary",
    "save_review_step",
    "start_accountant_review",
]
