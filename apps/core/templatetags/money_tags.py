"""
Template filters for rendering money.

Templates must never reach for Django's `floatformat`: it converts through
float, which is exactly what the whole policy forbids. These filters go
through `apps.core.money`, which is Decimal end to end.

    {{ line.amount|money }}          normal UI      -> 1,250
    {{ line.amount|money_full }}     ledger/audit   -> 1,250.001
    {{ line.amount|iqd }}            normal UI      -> 1,250 د.ع
    {{ line.amount|iqd_full }}       ledger/audit   -> 1,250.001 د.ع
"""

from __future__ import annotations

from django import template

from apps.core import money

register = template.Library()


@register.filter(name="money")
def money_filter(value: object) -> str:
    """Whole IQD for operational screens and printed documents."""
    return money.money_display(value)


@register.filter(name="money_full")
def money_full_filter(value: object) -> str:
    """Stored precision, for ledger, audit, and reconciliation screens."""
    return money.money_audit(value)


@register.filter(name="iqd")
def iqd_filter(value: object) -> str:
    """Whole Iraqi dinars with grouping and an explicit currency label."""
    return f"{money.money_display(value)} د.ع"


@register.filter(name="iqd_full")
def iqd_full_filter(value: object) -> str:
    """Stored precision with grouping and an explicit Iraqi-dinar label."""
    return f"{money.money_audit(value)} د.ع"
