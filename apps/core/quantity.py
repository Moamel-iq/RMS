"""
Quantity precision and rounding policy.

This module governs QUANTITIES ONLY — stock, recipe amounts, and the results
of unit conversion. It must never be used for money.

Money is a separate policy with its own precision, deliberately kept in a
separate module so that a monetary amount cannot inherit quantity rounding by
accident. Every public name here says "quantity" for that reason. See
docs/adr/ADR-006.

The policy, as decided by the product owner:

    Inventory quantities and unit-conversion results are stored and displayed
    at 3 decimal places, rounded ROUND_HALF_UP. Recipe calculations and
    intermediate conversions carry at least 6 decimal places internally before
    being reduced to 3 for storage or display.

One clarification the implementation makes explicit, because reading the
policy literally would produce wrong numbers:

    "Carry 6 decimal places internally, then round to 3" must NOT be
    implemented as quantize(6) followed by quantize(3). That is double
    rounding and it is demonstrably wrong:

        Decimal("1.00049999")
            .quantize(6dp) -> 1.000500   .quantize(3dp) -> 1.001   WRONG
            .quantize(3dp) directly      ->                1.000   CORRECT

    So a calculation carries full precision end to end and is quantized
    exactly once, at the boundary where it is stored or shown. This matches
    the house rule "never round mid-calculation; round once, at the boundary".
    CALCULATION_PLACES is the storage precision for values a later phase must
    persist mid-computation (a recipe line, say) — a floor on representation,
    not an instruction to round.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

#: Decimal places for a stored or displayed inventory quantity.
QUANTITY_PLACES = 3

#: Decimal places for persisted intermediate values (recipes, conversions).
#: A floor on representation, never a rounding step inside a calculation.
CALCULATION_PLACES = 6

#: Decimal places for a unit conversion factor.
#:
#: NOT covered by the stated policy, and inferred rather than decided. Twelve
#: is the smallest value that stores every plausible factor exactly:
#: one ounce is 0.028349523125 kg — twelve decimals. Ten would silently
#: truncate it, and a truncated factor is wrong in every conversion that uses
#: it, forever. Flagged in ADR-006 as awaiting confirmation.
FACTOR_PLACES = 12

#: Ties round away from zero. VERIFIED, not assumed:
#:     Decimal("-1.0005").quantize(0.001, ROUND_HALF_UP) == -1.001
#: This is symmetric in magnitude, so a quantity and its reversal round to the
#: same absolute value — which is what a reversal must do. It is NOT
#: "round toward positive infinity"; that would make a correction fail to
#: cancel its original.
QUANTITY_ROUNDING = ROUND_HALF_UP

_QUANTITY_EXPONENT = Decimal(1).scaleb(-QUANTITY_PLACES)
_CALCULATION_EXPONENT = Decimal(1).scaleb(-CALCULATION_PLACES)
_FACTOR_EXPONENT = Decimal(1).scaleb(-FACTOR_PLACES)


# ---------------------------------------------------------------------------
# Digit scripts
# ---------------------------------------------------------------------------
#
# Python's Decimal() silently accepts any Unicode decimal digit, so
# Decimal("١٢٣") is 123 — and so is Decimal("1٢3"), mixing scripts.
#
# The first is genuinely useful here: operators in Baghdad may type
# Arabic-Indic numerals. The second is not. A mixed-script number is almost
# always damage — a mis-segmented OCR read of a supplier invoice, or a
# corrupted import — and reading it as a valid quantity would post a number
# nobody typed.
#
# So the conversion is done explicitly rather than left to CPython: this
# module decides which scripts are accepted, and mixing them is refused.

ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"  # U+0660..U+0669
EXTENDED_ARABIC_INDIC_DIGITS = "۰۱۲۳۴۵۶۷۸۹"  # U+06F0..U+06F9 (Persian/Urdu)
ASCII_DIGITS = "0123456789"

_ARABIC_INDIC_TO_ASCII = str.maketrans(ARABIC_INDIC_DIGITS, ASCII_DIGITS)
_EXTENDED_TO_ASCII = str.maketrans(EXTENDED_ARABIC_INDIC_DIGITS, ASCII_DIGITS)

#: Arabic decimal separator U+066B, as used on Arabic keyboards.
ARABIC_DECIMAL_SEPARATOR = "٫"

#: What a numeric string may look like once digits are ASCII.
_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def normalize_digits(text: str) -> str:
    """
    Convert Arabic-Indic or Persian digits to ASCII, refusing a mix.

    Accepts one script consistently. `١٢٣` and `۱۲۳` both become `123`;
    `1٢3` is rejected.
    """
    scripts_present = {
        "arabic_indic": any(ch in ARABIC_INDIC_DIGITS for ch in text),
        "extended": any(ch in EXTENDED_ARABIC_INDIC_DIGITS for ch in text),
        "ascii": any(ch in ASCII_DIGITS for ch in text),
    }
    if sum(scripts_present.values()) > 1:
        raise ValidationError(
            _(
                "%(value)s mixes more than one set of numerals. A number must use "
                "one script throughout."
            ),
            code="mixed_digit_scripts",
            params={"value": text},
        )

    return (
        text.translate(_ARABIC_INDIC_TO_ASCII)
        .translate(_EXTENDED_TO_ASCII)
        .replace(ARABIC_DECIMAL_SEPARATOR, ".")
    )


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def ensure_decimal(value: object, *, field: str = "quantity") -> Decimal:
    """
    Return `value` as a Decimal, refusing anything that could lose precision.

    Floats are rejected outright rather than converted. `Decimal(0.1)` is
    0.1000000000000000055511151231257827021181583404541015625 — a float that
    reaches a quantity path has already lost the exactness the whole policy
    exists to preserve, and silently accepting it would hide the bug.

    bool is rejected too: it is a subclass of int, and `True` arriving where a
    quantity belongs is a programming error, not the number one.
    """
    if isinstance(value, bool):
        raise ValidationError(
            _("%(field)s must be a number, not a boolean."),
            code="invalid_quantity_type",
            params={"field": field},
        )

    if isinstance(value, float):
        raise ValidationError(
            _(
                "%(field)s was given a float. Quantities must be Decimal or str; "
                "a float cannot represent 0.1 exactly and would corrupt the value."
            ),
            code="float_in_quantity_path",
            params={"field": field},
        )

    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, str):
        # normalize_digits raises on mixed scripts; let that propagate.
        candidate = normalize_digits(value.strip())
        # Validate the shape ourselves rather than relying on Decimal(), whose
        # Unicode digit handling is broader than this system wants.
        if not _NUMERIC_RE.match(candidate):
            raise ValidationError(
                _("%(field)s is not a valid number: %(value)s"),
                code="invalid_quantity",
                params={"field": field, "value": value},
            )
        try:
            decimal_value = Decimal(candidate)
        except InvalidOperation as exc:
            raise ValidationError(
                _("%(field)s is not a valid number: %(value)s"),
                code="invalid_quantity",
                params={"field": field, "value": value},
            ) from exc
    else:
        raise ValidationError(
            _("%(field)s must be a Decimal, int, or str."),
            code="invalid_quantity_type",
            params={"field": field},
        )

    # NaN and Infinity survive Decimal() construction and would poison every
    # comparison and sum downstream. NaN != NaN, so a stock balance containing
    # one can never be reconciled.
    if not decimal_value.is_finite():
        raise ValidationError(
            _("%(field)s must be a finite number, not %(value)s."),
            code="non_finite_quantity",
            params={"field": field, "value": str(decimal_value)},
        )

    return decimal_value


# ---------------------------------------------------------------------------
# Quantization
# ---------------------------------------------------------------------------


def _quantize(value: Decimal, exponent: Decimal, *, field: str) -> Decimal:
    try:
        return value.quantize(exponent, rounding=QUANTITY_ROUNDING)
    except InvalidOperation as exc:
        # quantize() raises when the result would need more digits than the
        # context allows — Decimal("1E+30") at 3dp, for instance. Surfacing
        # the raw InvalidOperation would give an operator nothing to act on.
        raise ValidationError(
            _("%(field)s is too large to store: %(value)s"),
            code="quantity_out_of_range",
            params={"field": field, "value": str(value)},
        ) from exc


def quantize_quantity(value: object, *, field: str = "quantity") -> Decimal:
    """
    Reduce a value to the stored quantity precision (3dp, ties away from zero).

    Call this ONCE, at the boundary where the number is stored or displayed.
    Calling it on an intermediate result introduces double rounding.
    """
    return _quantize(ensure_decimal(value, field=field), _QUANTITY_EXPONENT, field=field)


def quantize_calculation(value: object, *, field: str = "quantity") -> Decimal:
    """
    Reduce a value to the persisted intermediate precision (6dp).

    For values a later phase must store part-way through a computation, such
    as a recipe line. Not a rounding step inside a single calculation.
    """
    return _quantize(ensure_decimal(value, field=field), _CALCULATION_EXPONENT, field=field)


def quantize_factor(value: object, *, field: str = "factor") -> Decimal:
    """Reduce a conversion factor to its stored precision (12dp)."""
    return _quantize(ensure_decimal(value, field=field), _FACTOR_EXPONENT, field=field)


def decimal_places_of(value: Decimal) -> int:
    """
    How many decimal places `value` carries.

    Decimal.as_tuple().exponent is an int for finite values but the literal
    'n', 'N', or 'F' for NaN and Infinity, so it is narrowed rather than
    assumed.
    """
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValidationError(
            _("Cannot measure the precision of %(value)s."),
            code="non_finite_quantity",
            params={"value": str(value)},
        )
    return max(0, -exponent)


def is_quantized_to_quantity(value: Decimal) -> bool:
    """Whether `value` already sits at or within the stored quantity precision."""
    return decimal_places_of(value) <= QUANTITY_PLACES
