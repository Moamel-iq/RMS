"""
Template filters for rendering quantities.

The counterpart to `money_tags`, and it exists for the same reason. Neither
`floatformat` nor `stringformat:"f"` is safe here: both go through float, and
`stringformat:"f"` is printf, so it prints six decimals whatever the column
stores — `60.000` comes out `60.000000`, which reads as a precision the system
does not have.

    {{ line.accepted_base_quantity|quantity }}   -> 60.0

Ungrouped and locale-independent. A quantity in a table sits beside a
conversion factor and an item code, and Django would localise a Decimal under
Arabic to `60,000` — a comma that is ambiguous and invites a mis-typed
re-entry (see the locale rule in CLAUDE.md).
"""

from __future__ import annotations

from decimal import Decimal

from django import template

from apps.core.quantity import QUANTITY_ROUNDING, ensure_decimal

register = template.Library()


@register.filter(name="quantity")
def quantity_filter(value: object) -> str:
    """Operational quantity display — one decimal place, without grouping."""
    if value is None or value == "":
        return ""
    amount = ensure_decimal(value).quantize(Decimal("0.1"), rounding=QUANTITY_ROUNDING)
    return f"{amount:.1f}"
