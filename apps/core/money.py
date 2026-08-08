"""
Monetary precision and rounding policy — IQD.

Separate from `apps.core.quantity` on purpose. A monetary amount must never
inherit quantity rounding, so the two policies share no constants, no
functions, and no module. Every public name here says "money", "price", or
"rate".

The policy, as approved:

    MONEY_PLACES         3   posted accounting and document line amounts
    MONEY_DISPLAY_PLACES 0   IQD shown in the UI and on printed documents
    UNIT_PRICE_PLACES    6   unit costs and unit prices
    RATE_PLACES          6   commission, discount, and allocation rates
    ROUNDING             ROUND_HALF_UP

The shape of a monetary calculation:

    1. Calculate at full Decimal precision. Never round mid-calculation.
    2. Quantize to 0.001 IQD at the moment the amount becomes a posted
       accounting or document line.
    3. A document total is the SUM of its posted lines. It is never rounded
       independently, because that is how SUM(lines) != total happens.

Where a total must be split across lines — discounts, commissions, shared
expenses, cost allocations — use `apps.core.allocation`, which guarantees the
parts sum exactly to the whole.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.quantity import ensure_decimal

# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

#: Decimal places for a posted accounting or document line amount.
MONEY_PLACES = 3

#: Decimal places shown to a user or printed. IQD has no circulating
#: subdivision, so an operator reads whole dinars.
MONEY_DISPLAY_PLACES = 0

#: Decimal places for a unit cost or unit price. Higher than MONEY_PLACES
#: because a per-gram cost multiplied by a recipe quantity must not have been
#: pre-rounded into the answer.
UNIT_PRICE_PLACES = 6

#: Decimal places for a commission percentage, discount rate, or allocation
#: rate.
RATE_PLACES = 6

#: Same direction as quantities: ties away from zero. Required so a reversal
#: cancels its original exactly — a credit of -1.0005 must mirror a debit of
#: 1.0005, and both must land on the same magnitude.
MONEY_ROUNDING = ROUND_HALF_UP

#: The trading currency. Multi-currency is not modelled; if it ever is, the
#: exchange rate is a rate and uses RATE_PLACES.
CURRENCY_CODE = "IQD"

_MONEY_EXPONENT = Decimal(1).scaleb(-MONEY_PLACES)
_DISPLAY_EXPONENT = Decimal(1).scaleb(-MONEY_DISPLAY_PLACES)
_UNIT_PRICE_EXPONENT = Decimal(1).scaleb(-UNIT_PRICE_PLACES)
_RATE_EXPONENT = Decimal(1).scaleb(-RATE_PLACES)

#: The smallest posted amount. Residual allocation works in whole units of it.
MONEY_QUANTUM = _MONEY_EXPONENT


# ---------------------------------------------------------------------------
# Cash settlement rounding — designed, disabled
# ---------------------------------------------------------------------------
#
# OFF by default and deliberately so. Nearest-250 rounding must never touch
# sales values, invoices, supplier balances, receivables, commissions,
# payroll, inventory valuation, recipe costing, COGS, journal entries, taxes,
# or discounts.
#
# When it is switched on it applies to ONE thing: the final amount physically
# payable in cash, computed after every other calculation is complete. The
# difference posts explicitly to a cash rounding gain/loss account — never
# buried in revenue, COGS, or inventory.

CASH_ROUNDING_ENABLED = False

#: The increment cash settlement would round to, if enabled.
CASH_ROUNDING_INCREMENT = Decimal("250")


# ---------------------------------------------------------------------------
# Guards and quantization
# ---------------------------------------------------------------------------


def ensure_money_decimal(value: object, *, field: str = "amount") -> Decimal:
    """
    Return `value` as a Decimal, refusing floats and non-finite values.

    Shares the input guard with quantities — rejecting floats is universal —
    but nothing about the rounding policy is shared.
    """
    return ensure_decimal(value, field=field)


def _quantize(value: Decimal, exponent: Decimal, *, field: str) -> Decimal:
    try:
        return value.quantize(exponent, rounding=MONEY_ROUNDING)
    except InvalidOperation as exc:
        raise ValidationError(
            _("%(field)s is too large to store: %(value)s"),
            code="amount_out_of_range",
            params={"field": field, "value": str(value)},
        ) from exc


def quantize_money(value: object, *, field: str = "amount") -> Decimal:
    """
    Reduce an amount to posted precision: 0.001 IQD, ties away from zero.

    Call this ONCE, at the point the amount becomes a posted accounting or
    document line. Calling it on an intermediate double-rounds.
    """
    return _quantize(ensure_money_decimal(value, field=field), _MONEY_EXPONENT, field=field)


def quantize_unit_price(value: object, *, field: str = "unit price") -> Decimal:
    """Reduce a unit cost or price to 6 decimal places."""
    return _quantize(ensure_money_decimal(value, field=field), _UNIT_PRICE_EXPONENT, field=field)


def quantize_rate(value: object, *, field: str = "rate") -> Decimal:
    """Reduce a commission, discount, or allocation rate to 6 decimal places."""
    return _quantize(ensure_money_decimal(value, field=field), _RATE_EXPONENT, field=field)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
#
# Every renderer returns a STRING, never a Decimal. That is the enforcement,
# not a convention: a rendered amount cannot be summed, compared numerically,
# or written back to a column, so a report physically cannot derive a
# discrepancy from display rounding.
#
# Reconciliation, audit, and ledger diagnostics must compare the stored
# Decimal values via quantize_money — never these.


def _render(value: object, places: int, *, grouped: bool, field: str) -> str:
    amount = _quantize(
        ensure_money_decimal(value, field=field),
        Decimal(1).scaleb(-places),
        field=field,
    )
    return f"{amount:,.{places}f}" if grouped else f"{amount:.{places}f}"


def money_display(value: object, *, field: str = "amount") -> str:
    """
    Whole IQD with thousands separators, for normal operational screens and
    printed documents. `1250.001` renders as `1,250`.

    NEVER for reconciliation, audit, ledger diagnostics, or exports — those
    must see the stored third decimal. Use `money_audit` or `money_export`.
    """
    return _render(value, MONEY_DISPLAY_PLACES, grouped=True, field=field)


def money_audit(value: object, *, field: str = "amount") -> str:
    """
    Full stored precision with thousands separators, for ledger, audit, and
    reconciliation screens. `1250.001` renders as `1,250.001`.

    These views must expose the third decimal, or an operator reconciling by
    eye will chase a difference that only exists in the rounding.
    """
    return _render(value, MONEY_PLACES, grouped=True, field=field)


def money_export(value: object, *, field: str = "amount") -> str:
    """
    Full stored precision, ungrouped, for CSV and accounting exports.
    `1250.001` renders as `1250.001`.

    Ungrouped on purpose: a thousands separator inside a CSV field either
    breaks the delimiter or is re-parsed as a different number.
    """
    return _render(value, MONEY_PLACES, grouped=False, field=field)


# ---------------------------------------------------------------------------
# Cash settlement rounding
# ---------------------------------------------------------------------------


def apply_cash_settlement_rounding(
    payable: object, *, field: str = "payable"
) -> tuple[Decimal, Decimal]:
    """
    Round the cash-payable amount, returning (rounded, adjustment).

    `adjustment` is what must be posted to the cash rounding gain/loss
    account, so that:

        rounded == payable + adjustment

    Disabled by default, in which case this is an identity and the adjustment
    is zero. The signature exists now so that callers are written against the
    final shape and enabling the policy later is a configuration change rather
    than a rewrite of every settlement path.
    """
    amount = quantize_money(payable, field=field)

    if not CASH_ROUNDING_ENABLED:
        return amount, Decimal("0").quantize(_MONEY_EXPONENT)

    increment = CASH_ROUNDING_INCREMENT
    # Round the magnitude, then restore the sign, so a refund rounds the same
    # distance as the payment it reverses.
    sign = -1 if amount < 0 else 1
    magnitude = abs(amount)
    steps = (magnitude / increment).quantize(Decimal("1"), rounding=MONEY_ROUNDING)
    rounded = quantize_money(sign * steps * increment)
    return rounded, quantize_money(rounded - amount)
