"""
Printed sheets: one paper layout for every statement in the system.

A statement that can be exported to Excel can be printed, and the two must
agree. So a printed sheet is built from the **same rows the export builds** —
`csv_rows` for the accounting reports, `columns` + `rows` for the inventory and
kitchen ones — rather than from a second query written for paper. A number that
differs between the file and the page is worse than no page at all.

The renderer is the reader's own browser: the print view is HTML, and
"حفظ بصيغة PDF" in the print dialog produces the file. That is what the
architecture charter (Step 8) asks for — a browser-rendered HTML/CSS
approach — and it is the only renderer that shapes Arabic, joins its letters
and lays out a right-to-left table without a font pipeline of our own. This
module is the service interface the charter asks to keep: a view hands over a
`PrintSheet`, and what turns that into paper can be replaced (a headless
Chromium on the server, say) without any report changing.

What appears in the header is the reader's choice, not ours. Every part —
the logo, the organization and branch, the period and filters, who printed it
and when, the signature block — is rendered and then shown or hidden by the
toolbar on screen, remembered per reader. A sheet printed for the owner's file
and a sheet faxed to a supplier want different headings.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.templatetags.report_tags import render_value

#: Where a deployment drops its letterhead. Absent, the sheet prints the
#: organization's name instead — a missing image must never be a broken sheet.
LOGO_RELATIVE_PATH = Path("img") / "jadwa-rms-logo.svg"


def logo_static_path() -> str | None:
    """The logo's static path if a deployment has provided one."""
    for directory in [*settings.STATICFILES_DIRS, getattr(settings, "STATIC_ROOT", None)]:
        if directory and (Path(directory) / LOGO_RELATIVE_PATH).is_file():
            return LOGO_RELATIVE_PATH.as_posix()
    return None


#: An identifier reads as one token: digits, Latin letters and the separators
#: codes are built from, with no space anywhere. `1-01-01-001` and
#: `P-MANDI-DAJAJ-HALF` match; an Arabic name or a sentence does not.
_CODE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]{2,}$")


def _is_code(value: Any) -> bool:
    return bool(_CODE.match(str(value)))


@dataclass(frozen=True)
class SheetFilter:
    """One applied filter, as the reader chose it rather than as a query string."""

    label: str
    value: str


@dataclass(frozen=True)
class SheetColumn:
    """A column of the printed table."""

    key: str
    label: str
    numeric: bool = False


@dataclass(frozen=True)
class SheetCell:
    """
    One cell, carrying its own alignment.

    The cell knows whether it is a number so the template needs no lookup: a
    Django template cannot index a list by a loop counter, and inventing a
    filter to do it would put layout logic in a place tests cannot reach.
    """

    value: Any
    numeric: bool = False
    #: An identifier — an account code, an item code, a document number. It is
    #: read and re-typed as one token, so it is never broken across two lines.
    code: bool = False

    @classmethod
    def of(
        cls,
        value: Any,
        *,
        numeric: bool | None = None,
        renderer: Callable[[Any], Any] = render_value,
    ) -> SheetCell:
        """
        Render one value the way the screen it was printed from renders it.

        The renderer is the screen's own: the ledger screens write money
        through `money_audit`, the operational reports through `render_value`,
        and a figure that changed shape on its way to paper would be read as a
        different figure. `render_value` is the default because it is what a
        data-driven report table already uses.
        """
        is_number = isinstance(value, Decimal | int) and not isinstance(value, bool)
        rendered = renderer(value)
        return cls(
            rendered,
            is_number if numeric is None else numeric,
            code=not is_number and _is_code(rendered),
        )


@dataclass(frozen=True)
class PrintSheet:
    """
    Everything a printed statement says, and nothing about how it looks.

    `rows` are `SheetCell`s in column order, already rendered by whatever built
    them — money through `money_display`, quantities through the quantity
    helpers. Formatting a Decimal here would be a second place where rounding
    happens, and the charter allows exactly one.
    """

    title: str
    organization_label: str = ""
    branch_label: str = ""
    period_label: str = ""
    subtitle: str = ""
    filters: Sequence[SheetFilter] = ()
    columns: Sequence[SheetColumn] = ()
    rows: Sequence[Sequence[SheetCell]] = ()
    totals: Sequence[SheetCell] | None = None
    totals_label: str = str(_("الإجمالي"))
    note: str = ""
    #: Rendered instead of the table when a statement is a document rather than
    #: a list — a payslip, an invoice. The template is included as-is.
    body_template: str = ""
    body_context: dict[str, Any] = field(default_factory=dict)
    empty_message: str = str(_("لا سطور في هذا الكشف."))

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.body_template


def sheet_from_columns(
    *,
    title: str,
    columns: Iterable[tuple[str, Any]],
    rows: Iterable[dict[str, Any]],
    numeric_keys: Iterable[str] = (),
    renderer: Callable[[Any], Any] = render_value,
    **extra: Any,
) -> PrintSheet:
    """Build a sheet from the `(key, label)` pairs a report already declares."""
    numeric = set(numeric_keys)
    ordered = [SheetColumn(key, str(label), key in numeric) for key, label in columns]
    return PrintSheet(
        title=title,
        columns=ordered,
        rows=[
            [
                SheetCell.of(
                    row.get(column.key, ""),
                    numeric=column.numeric or None,
                    renderer=renderer,
                )
                for column in ordered
            ]
            for row in rows
        ],
        **extra,
    )


def sheet_from_table(
    *,
    title: str,
    headers: Sequence[Any],
    table: Iterable[Sequence[Any]],
    renderer: Callable[[Any], Any] = render_value,
    **extra: Any,
) -> PrintSheet:
    """Build a sheet from the flat `(headers, rows)` an export already produces."""
    columns = [SheetColumn(f"c{index}", str(header)) for index, header in enumerate(headers)]
    return PrintSheet(
        title=title,
        columns=columns,
        rows=[[SheetCell.of(cell, renderer=renderer) for cell in line] for line in table],
        **extra,
    )


def render_sheet(request: HttpRequest, sheet: PrintSheet) -> HttpResponse:
    """Render one sheet in the paper layout."""
    printed_at: datetime.datetime = timezone.localtime()
    user = request.user
    printed_by = getattr(user, "get_full_name", lambda: "")() or getattr(user, "username", "")
    return render(
        request,
        "print/sheet.html",
        {
            "sheet": sheet,
            "printed_at": printed_at,
            "printed_by": printed_by,
            "logo_path": logo_static_path(),
        },
    )


class PrintableReportMixin:
    """
    Mixed into a report view, `?print=1` answers with the paper layout.

    It sits beside `?export=csv` deliberately: same URL, same filters, same
    permission check, same rows — one more representation of a screen the
    reader is already allowed to see, never a second way in.
    """

    #: Column keys whose cells are numbers and belong on the number side.
    print_numeric_keys: tuple[str, ...] = ()

    def wants_print(self, request: HttpRequest) -> bool:
        return request.GET.get("print") == "1"

    def print_sheet(self, context: dict[str, Any], filters: Any) -> PrintSheet:
        raise NotImplementedError  # pragma: no cover

    def render_print(
        self, request: HttpRequest, context: dict[str, Any], filters: Any
    ) -> HttpResponse:
        return render_sheet(request, self.print_sheet(context, filters))


def filters_from_query(
    pairs: Iterable[tuple[str, Any]], labels: dict[str, Any]
) -> list[SheetFilter]:
    """
    Name the filters the way the screen names them.

    A sheet that says `date_from=2026-08-01` has told the reader the shape of a
    URL, not the window of the report.
    """
    out: list[SheetFilter] = []
    for key, value in pairs:
        if value in (None, "", False):
            continue
        out.append(SheetFilter(label=str(labels.get(key, key)), value=str(value)))
    return out
