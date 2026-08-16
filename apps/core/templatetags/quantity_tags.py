"""
Template filters for rendering quantities.

The counterpart to `money_tags`, and it exists for the same reason. Neither
`floatformat` nor `stringformat:"f"` is safe here: both go through float, and
`stringformat:"f"` is printf, so it prints six decimals whatever the column
stores — `60.000` comes out `60.000000`, which reads as a precision the system
does not have.

    {{ line.accepted_base_quantity|quantity }}   -> 60.000

Ungrouped and locale-independent. A quantity in a table sits beside a
conversion factor and an item code, and Django would localise a Decimal under
Arabic to `60,000` — a comma that is ambiguous and invites a mis-typed
re-entry (see the locale rule in CLAUDE.md).
"""

from __future__ import annotations

from django import template

from apps.core.quantity import QUANTITY_PLACES, ensure_decimal, quantize_quantity

register = template.Library()


@register.filter(name="quantity")
def quantity_filter(value: object) -> str:
    """Stored quantity precision — three places, no thousands separator."""
    if value is None or value == "":
        return ""
    amount = quantize_quantity(ensure_decimal(value))
    return f"{amount:.{QUANTITY_PLACES}f}"
