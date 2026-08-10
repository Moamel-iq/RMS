"""
Template helpers for data-driven report tables.

A report declares its columns as `(key, header)` pairs and its rows as dicts,
so one template renders all nine. Django cannot index a dict by a variable key
in a template, which is the only reason this module exists.

`cell` also decides how a value is *written*, and that decision matters more
than it looks. Technical and re-enterable values — quantities, costs, codes —
must render locale-independently: Django localises `Decimal`, so under Arabic
a quantity would come out `29,500` and a reader re-typing it would enter
twenty-nine thousand five hundred.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django import template
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

register = template.Library()

#: Rendered for a column a row does not carry. Distinct from an empty string,
#: which is a value the row genuinely holds.
MISSING = "—"

_YES = _("نعم")
_NO = _("لا")


@register.filter(name="cell")
def cell(row: dict[str, Any], key: str) -> Any:
    """
    One report cell, by column key.

    A key the row does not carry renders as a dash rather than blank. That is
    the valuation-redaction case: a storekeeper's row has no `value` key at
    all, and a blank cell would suggest the number is zero.
    """
    if not isinstance(row, dict) or key not in row:
        return MISSING

    value = row[key]
    if value is None or value == "":
        return MISSING
    if isinstance(value, bool):
        return _YES if value else _NO
    if isinstance(value, Decimal):
        # Never localised: a comma here would be re-typed as a thousands
        # separator, and `format` keeps the trailing zeros that say how many
        # decimal places the figure actually carries.
        return format(value, "f")
    if isinstance(value, datetime.datetime):
        return date_format(value, "SHORT_DATETIME_FORMAT")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


@register.filter(name="is_numeric_cell")
def is_numeric_cell(row: dict[str, Any], key: str) -> bool:
    """Whether this cell should be rendered LTR in an RTL table."""
    return isinstance(row, dict) and isinstance(row.get(key), Decimal | int)
