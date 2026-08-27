"""
Template filters for rendering money.

Templates must never reach for Django's `floatformat`: it converts through
float, which is exactly what the whole policy forbids. These filters go
through `apps.core.money`, which is Decimal end to end.

    {{ line.amount|money }}          normal UI      -> 1,250 دينار عراقي
    {{ line.amount|money_full }}     ledger/audit   -> 1,250.001 دينار عراقي
    {{ line.amount|iqd }}            normal UI      -> 1,250 دينار عراقي
    {{ line.amount|iqd_full }}       ledger/audit   -> 1,250.001 دينار عراقي
"""

from __future__ import annotations

from django import template

from apps.core import money

register = template.Library()


@register.filter(name="money")
def money_filter(value: object) -> str:
    """Whole Iraqi dinars with an explicit currency label."""
    return money.money_with_currency(value)


@register.filter(name="money_full")
def money_full_filter(value: object) -> str:
    """Stored precision with an explicit Iraqi-dinar label."""
    return money.money_audit_with_currency(value)


@register.filter(name="iqd")
def iqd_filter(value: object) -> str:
    """Whole Iraqi dinars with grouping and an explicit currency label."""
    return money.money_with_currency(value)


@register.filter(name="iqd_full")
def iqd_full_filter(value: object) -> str:
    """Stored precision with grouping and an explicit Iraqi-dinar label."""
    return money.money_audit_with_currency(value)
