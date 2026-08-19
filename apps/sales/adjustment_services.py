"""
Drafting a return, a cancellation or a financial correction.

Kept apart from `adjustment_posting.py` for the reason `day_services.py` is kept
apart from `posting.py`: nothing here writes a journal, a receivable entry or a
number. This module decides *what the correction is worth*; the other one is the
only place that value moves.

## The arithmetic every agent shares

`proportional_amounts` is the whole of it, and it has one non-obvious rule.
Six of the seven figures are the original's figure times a ratio, each quantized
**once** with `quantize_money`. The seventh — `net_amount` — is then
**recomputed as the residual** of the others rather than quantized from its own
product:

    application line:  net = gross − restaurant discount − commission − fees
    cash or card line: net = gross − restaurant discount

That is not a tidiness preference. Rating all seven independently and rounding
each would let a 1,000-dinar return split into figures that sum to 999.999,
and the journal would then be out by a fils for reasons nobody could
reconstruct. Taking the residual makes the credits sum to the debit *by
construction*, at every amount, forever — the same device ADR-027 §6 uses to
make the application journal balance by definition rather than by luck, and the
same one `discounts.DiscountSplit` uses for the funding shares.

## Which ratio

A cancellation or a return names a **quantity**, and the ratio is
`adjusted_quantity / original.quantity`. A `FINANCIAL_CORRECTION` may not touch
quantity at all (a database trigger says so), so it names a **gross amount** and
the ratio is `adjusted_gross / original.gross_amount`. Which of the two is in
force is decided by whether the caller supplied `adjusted_gross`, not by
re-reading the header — so the value object stays usable for a preview before an
adjustment exists.

Nothing here re-resolves a price, a recipe version or an agreement. The price
that sold the plate is the price that un-sells it; looking the table up again
would let a Monday price change restate a Sunday return.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.sales.models import (
    SalesAdjustment,
    SalesAdjustmentLine,
    SalesAdjustmentReasonKind,
    SalesAdjustmentStatus,
    SalesDay,
    SalesDayLine,
    SalesDayStatus,
    TenderDestination,
)

if TYPE_CHECKING:
    from apps.users.models import User

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdjustedAmounts:
    """
    What one adjustment line is worth, before anything is stored.

    A value object rather than a row, so a screen can show an operator what
    taking back two of the five plates would cost before they commit to it.
    """

    gross: Decimal
    restaurant_discount: Decimal
    application_discount: Decimal
    commission: Decimal
    other_fees: Decimal
    customer_charge: Decimal
    net_amount: Decimal


def proportional_amounts(
    original_line: SalesDayLine,
    *,
    adjusted_quantity: Decimal,
    adjusted_gross: Decimal | None = None,
) -> AdjustedAmounts:
    """
    Scale every figure on a posted line by the share being taken back.

    See the module docstring for why `net_amount` is the residual and not the
    seventh product. The two divisors are refused rather than defaulted: a line
    that sold nothing has no share to take back, and dividing by it would
    produce a plausible-looking zero instead of a refusal.
    """
    if adjusted_gross is not None:
        if original_line.gross_amount <= ZERO:
            raise ValidationError(
                _("A line with no value cannot be corrected by amount."),
                code="original_has_no_value",
            )
        ratio = adjusted_gross / original_line.gross_amount
    else:
        if original_line.quantity <= ZERO:  # pragma: no cover - a check constraint forbids it
            raise ValidationError(
                _("A line with no quantity cannot be adjusted by quantity."),
                code="original_has_no_quantity",
            )
        ratio = adjusted_quantity / original_line.quantity

    if ratio <= ZERO:
        raise ValidationError(
            _("An adjustment must take something back."), code="adjustment_is_empty"
        )
    if ratio > Decimal("1"):
        raise ValidationError(
            _("An adjustment cannot exceed the line it corrects."), code="adjustment_exceeds_line"
        )

    gross = quantize_money(original_line.gross_amount * ratio)
    restaurant_discount = quantize_money(original_line.restaurant_discount * ratio)
    application_discount = quantize_money(original_line.application_discount * ratio)
    commission = quantize_money(original_line.commission_amount * ratio)
    other_fees = quantize_money(original_line.other_fee_amount * ratio)
    customer_charge = quantize_money(original_line.customer_charge * ratio)

    # The residual. Never a product of its own — see the module docstring.
    if original_line.is_application_sale:
        net_amount = gross - restaurant_discount - commission - other_fees
    else:
        net_amount = gross - restaurant_discount

    if net_amount < ZERO:
        raise ValidationError(
            _(
                "This correction would credit back more in fees than it takes off the "
                "sale. Check the original line."
            ),
            code="adjusted_net_is_negative",
        )

    return AdjustedAmounts(
        gross=gross,
        restaurant_discount=restaurant_discount,
        application_discount=application_discount,
        commission=commission,
        other_fees=other_fees,
        customer_charge=customer_charge,
        net_amount=net_amount,
    )


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _require_draft(adjustment: SalesAdjustment) -> None:
    if adjustment.status != SalesAdjustmentStatus.DRAFT:
        raise ValidationError(
            _("Only a draft adjustment can be changed."), code="adjustment_not_draft"
        )


@transaction.atomic
def create_sales_adjustment(
    *,
    sales_day: SalesDay,
    reason_kind: str,
    business_date: datetime.date,
    reason: str,
    evidence_reference: str,
    actor: User,
    notes: str = "",
) -> SalesAdjustment:
    """
    Open a correction against a posted day.

    Every refusal here is also a database guarantee — the posted-day rule and
    the date ordering live in `0008`'s containment trigger as well — and both
    are wanted. The service is the sentence an operator can act on; the trigger
    is what survives a data fix applied at two in the morning through a shell.
    """
    if sales_day.status != SalesDayStatus.POSTED:
        raise ValidationError(_("Only a posted sales day can be adjusted."), code="day_not_posted")
    if reason_kind not in SalesAdjustmentReasonKind.values:
        raise ValidationError(_("Unknown adjustment reason."), code="unknown_reason_kind")
    if not reason.strip():
        raise ValidationError(_("An adjustment needs a reason."), code="reason_required")
    if not evidence_reference.strip():
        raise ValidationError(
            _("An adjustment needs an evidence reference."), code="evidence_required"
        )
    if business_date < sales_day.business_date:
        raise ValidationError(
            _("An adjustment cannot be dated before the day it corrects."),
            code="business_date_precedes_the_day",
        )

    adjustment = SalesAdjustment(
        organization=sales_day.organization,
        branch=sales_day.branch,
        sales_day=sales_day,
        business_date=business_date,
        reason_kind=reason_kind,
        reason=reason.strip(),
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
        created_by=actor,
    )
    adjustment.full_clean()
    adjustment.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=adjustment,
        new_state=snapshot(adjustment),
        branch=sales_day.branch,
    )
    return adjustment


@transaction.atomic
def add_adjustment_line(
    *,
    adjustment: SalesAdjustment,
    original_line: SalesDayLine,
    adjusted_quantity: Decimal,
    adjusted_gross: Decimal | None = None,
    line_reason: str = "",
    actor: User,
) -> SalesAdjustmentLine:
    """
    Take back part of one posted line.

    `unit_price` is copied off the original rather than resolved again, for the
    reason the module docstring gives. The seven money figures come from
    `proportional_amounts` and are stored, so the journal this eventually posts
    reads its own numbers rather than recomputing them against a world that has
    since moved.
    """
    _require_draft(adjustment)

    if original_line.sales_day_id != adjustment.sales_day_id:
        raise ValidationError(
            _("An adjustment line must correct a line of its own sales day."),
            code="line_belongs_to_another_day",
        )

    is_correction = adjustment.reason_kind == SalesAdjustmentReasonKind.FINANCIAL_CORRECTION
    if is_correction:
        if adjusted_gross is None:
            raise ValidationError(
                _("A financial correction names an amount, not a quantity."),
                code="correction_needs_an_amount",
            )
        adjusted_quantity = ZERO
    else:
        if adjusted_gross is not None:
            raise ValidationError(
                _("A cancellation or a return names a quantity, not an amount."),
                code="quantity_adjustment_takes_no_amount",
            )
        adjusted_quantity = quantize_quantity(adjusted_quantity)
        if adjusted_quantity <= ZERO:
            raise ValidationError(
                _("A cancellation or a return needs a quantity."), code="quantity_required"
            )

    amounts = proportional_amounts(
        original_line,
        adjusted_quantity=adjusted_quantity,
        adjusted_gross=quantize_money(adjusted_gross) if adjusted_gross is not None else None,
    )
    _refuse_over_adjustment(
        original_line=original_line,
        adjusted_quantity=adjusted_quantity,
        adjusted_gross=amounts.gross,
    )

    sequence = adjustment.lines.count() + 1
    line = SalesAdjustmentLine(
        adjustment=adjustment,
        sequence=sequence,
        original_line=original_line,
        adjusted_quantity=adjusted_quantity,
        unit_price=original_line.unit_price,
        adjusted_gross=amounts.gross,
        adjusted_restaurant_discount=amounts.restaurant_discount,
        adjusted_application_discount=amounts.application_discount,
        adjusted_commission=amounts.commission,
        adjusted_other_fees=amounts.other_fees,
        adjusted_customer_charge=amounts.customer_charge,
        adjusted_net_amount=amounts.net_amount,
        line_reason=line_reason.strip(),
    )
    line.full_clean()
    line.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=line,
        new_state=snapshot(line),
        branch=adjustment.branch,
    )
    return line


def _refuse_over_adjustment(
    *,
    original_line: SalesDayLine,
    adjusted_quantity: Decimal,
    adjusted_gross: Decimal,
    exclude_line_id: int | None = None,
) -> None:
    """
    Refuse a correction that would take back more than the line sold.

    The same rule as `0008`'s containment trigger, and both exist on purpose:
    this one produces a sentence a screen can render, and the trigger is the one
    a raw `INSERT` cannot walk past. Only **posted** adjustments count — two
    drafts may each propose the full amount, and only one of them can go on to
    post.
    """
    claimed = _claimed_against(original_line, exclude_line_id=exclude_line_id)
    if claimed.quantity + adjusted_quantity > original_line.quantity:
        raise ValidationError(
            _("Adjustments would take back more than the %(sold)s this line sold.")
            % {"sold": f"{original_line.quantity:f}"},
            code="quantity_over_adjusted",
        )
    if claimed.gross + adjusted_gross > original_line.gross_amount:
        raise ValidationError(
            _("Adjustments would take back more value than this line sold."),
            code="gross_over_adjusted",
        )


@dataclass(frozen=True)
class _Claimed:
    quantity: Decimal
    gross: Decimal


def _claimed_against(
    original_line: SalesDayLine, *, exclude_line_id: int | None = None
) -> _Claimed:
    """How much of this line posted adjustments have already taken back."""
    rows = SalesAdjustmentLine.objects.filter(
        original_line=original_line, adjustment__status=SalesAdjustmentStatus.POSTED
    )
    if exclude_line_id is not None:
        rows = rows.exclude(pk=exclude_line_id)
    totals = rows.aggregate(quantity=Sum("adjusted_quantity"), gross=Sum("adjusted_gross"))
    return _Claimed(
        quantity=totals["quantity"] or ZERO,
        gross=totals["gross"] or ZERO,
    )


@transaction.atomic
def remove_adjustment_line(*, line: SalesAdjustmentLine, actor: User) -> None:
    """
    Take a line off a draft adjustment. The database refuses it otherwise.

    Sequences are **not** renumbered, exactly as they are not on a sales day: a
    gap records that a line was entered and removed, and renumbering would
    rewrite the identity of every line after it.
    """
    adjustment = line.adjustment
    _require_draft(adjustment)
    previous = snapshot(line)
    line.delete()
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="sales.SalesAdjustmentLine",
        target_id=str(previous.get("public_id", "")),
        previous_state=previous,
        branch=adjustment.branch,
    )


def adjustable_lines(sales_day: SalesDay) -> list[SalesDayLine]:
    """
    The lines of a posted day that still have something left to take back.

    A line already fully returned is left out rather than shown greyed: offering
    it would be offering a dead end, and the trigger would refuse the row
    anyway. A line partly returned stays, because the rest of it is still real.
    """
    if sales_day.status != SalesDayStatus.POSTED:
        return []
    rows = (
        sales_day.lines.select_related(
            "menu_item", "channel", "delivery_application", "recipe_version", "serving"
        )
        .order_by("sequence")
        .all()
    )
    return [line for line in rows if _claimed_against(line).gross < line.gross_amount]


# ---------------------------------------------------------------------------
# Reads a screen needs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdjustmentTotals:
    """What an adjustment adds up to, derived from its lines every time."""

    gross: Decimal
    restaurant_discount: Decimal
    application_discount: Decimal
    commission: Decimal
    other_fees: Decimal
    customer_charge: Decimal
    net_cash: Decimal
    net_card: Decimal
    net_application: Decimal
    line_count: int


def totals_for(adjustment: SalesAdjustment) -> AdjustmentTotals:
    """
    Add an adjustment up. Derived every time, never stored.

    Split three ways by tender for the reason a day's totals are: what comes out
    of a drawer, what comes off a card clearing balance and what comes off an
    application's debt are three different economic claims, and one net figure
    would let a cash refund and a receivable credit cancel each other out on the
    screen that is supposed to distinguish them.
    """
    gross = restaurant = application = commission = fees = charge = ZERO
    cash = card = receivable = ZERO
    count = 0
    for line in adjustment.lines.select_related(
        "original_line", "original_line__channel", "original_line__delivery_application"
    ).all():
        count += 1
        gross += line.adjusted_gross
        restaurant += line.adjusted_restaurant_discount
        application += line.adjusted_application_discount
        commission += line.adjusted_commission
        fees += line.adjusted_other_fees
        charge += line.adjusted_customer_charge
        original = line.original_line
        if original.is_application_sale:
            receivable += line.adjusted_net_amount
        elif original.channel.default_tender == TenderDestination.CARD:
            card += line.adjusted_net_amount
        else:
            cash += line.adjusted_net_amount
    return AdjustmentTotals(
        gross=quantize_money(gross),
        restaurant_discount=quantize_money(restaurant),
        application_discount=quantize_money(application),
        commission=quantize_money(commission),
        other_fees=quantize_money(fees),
        customer_charge=quantize_money(charge),
        net_cash=quantize_money(cash),
        net_card=quantize_money(card),
        net_application=quantize_money(receivable),
        line_count=count,
    )


__all__ = [
    "AdjustedAmounts",
    "AdjustmentTotals",
    "add_adjustment_line",
    "adjustable_lines",
    "create_sales_adjustment",
    "proportional_amounts",
    "remove_adjustment_line",
    "totals_for",
]
