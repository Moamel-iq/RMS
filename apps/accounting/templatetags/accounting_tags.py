"""Presentation-only helpers for accounting templates."""

from __future__ import annotations

from django import template

register = template.Library()


@register.filter(name="simple_account_code")
def simple_account_code(value: object) -> str:
    """Render structural account codes like ``1-01-01-001`` as ``1111``.

    The stored account code remains unchanged; this filter only makes the code
    match the compact numbering used by the imported chart of accounts.
    """

    code = str(value or "").strip()
    if not code:
        return ""

    segments = code.split("-")
    if not all(segment.isdigit() for segment in segments):
        return code

    return "".join(str(int(segment)) for segment in segments)
