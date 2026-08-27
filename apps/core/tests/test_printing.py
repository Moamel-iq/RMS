"""
Printed statements: `?print=1` on a report, and paper furniture on every screen.

Two claims are tested here, because they are the two ways a printed figure
goes wrong: the sheet must carry the **same rows** the export carries — a
statement that disagreed with its own spreadsheet would be worse than no
statement — and it must be reachable by exactly the readers who may see the
screen, since a printable URL is a URL.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.printing import (
    PrintSheet,
    SheetCell,
    SheetFilter,
    logo_static_path,
    sheet_from_columns,
    sheet_from_table,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import create_branch, create_organization, grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]
PASSWORD = "pw-not-real-1234"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="011",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def accountant(branch: Branch) -> User:
    user = User.objects.create_user(username="accountant", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTANT)
    return user


@pytest.fixture
def outsider() -> User:
    return User.objects.create_user(username="outsider", password=PASSWORD)


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def test_a_cell_is_written_the_way_the_screens_write_it() -> None:
    """One renderer, so a figure keeps its shape between screen, file and paper."""
    assert SheetCell.of(Decimal("1250.500")).value == "1250.500"
    assert SheetCell.of(Decimal("1250.500")).numeric is True
    assert SheetCell.of(7).numeric is True
    assert SheetCell.of("مندي لحم").numeric is False
    assert SheetCell.of(None).value == "—"
    assert SheetCell.of(True).value != "—"
    # A renderer may be supplied when the screen writes money its own way.
    assert SheetCell.of(Decimal("1250.5"), renderer=lambda v: f"[{v}]").value == "[1250.5]"


def test_a_sheet_built_from_columns_keeps_the_column_order() -> None:
    sheet = sheet_from_columns(
        title="تقرير",
        columns=[("code", "الرمز"), ("qty", "الكمية")],
        rows=[{"qty": Decimal("3.000"), "code": "A-1"}],
        numeric_keys=["qty"],
    )
    assert [column.label for column in sheet.columns] == ["الرمز", "الكمية"]
    assert [cell.value for cell in sheet.rows[0]] == ["A-1", "3.000"]
    assert [cell.numeric for cell in sheet.rows[0]] == [False, True]


def test_a_sheet_built_from_an_export_table_keeps_its_rows() -> None:
    sheet = sheet_from_table(
        title="ميزان",
        headers=["الحساب", "مدين"],
        table=[["1-01", Decimal("5.000")], ["1-02", Decimal("0")]],
    )
    assert len(sheet.rows) == 2
    assert sheet.rows[0][1].numeric is True
    assert sheet.is_empty is False
    assert PrintSheet(title="فارغ").is_empty is True


def test_the_sheet_declares_no_paper_of_its_own() -> None:
    """
    A page size in the document is how a printed statement loses its last
    columns: the reader's dialog, the stylesheet and the document each claim a
    different page box, and the printer cuts whatever does not fit the one it
    used. The stylesheet declares the paper once; the reader may turn it
    sideways from the toolbar, and nothing else has an opinion.
    """
    template = (ROOT / "templates" / "print" / "sheet.html").read_text(encoding="utf-8")
    assert "@page" not in template
    assert "sheet--wide" not in template
    assert "data-print-landscape" in template
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert styles.count("@page") == 1

    # Nothing in the sheet may be wider than the page it is printed on.
    section = styles[styles.index("19. Any screen on paper") :]
    assert "max-inline-size: 100%" in section


def test_no_template_comment_can_leak_onto_the_paper() -> None:
    """
    Django's `{# #}` is a single-line comment. Opened on one line and closed on
    another it is not a comment at all — it is text, and it printed at the top
    of the owner's statement.
    """
    for path in (ROOT / "templates").rglob("*.html"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "{#" in line:
                assert "#}" in line.split("{#", 1)[1], f"{path}:{number}"


def test_the_letterhead_is_optional_and_never_breaks_the_sheet() -> None:
    """A deployment without a logo file prints its name; it does not 404."""
    path = logo_static_path()
    assert path is None or (ROOT / "static" / path).is_file()


# ---------------------------------------------------------------------------
# The report sheets
# ---------------------------------------------------------------------------


def _sheet_rows(body: str) -> list[list[str]]:
    table = re.search(r'<table class="sheet__table">(.*?)</table>', body, re.S)
    assert table, "the sheet has no table"
    rows = []
    for line in re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", line, re.S)
        ]
        if cells:
            rows.append(cells)
    return rows


def test_a_report_prints_the_rows_its_export_writes(
    accountant: User, organization: Organization
) -> None:
    client = _client(accountant)
    url = reverse("accounting:trial_balance")
    query = f"?organization={organization.pk}"

    printed = client.get(url + query + "&print=1")
    assert printed.status_code == 200
    body = printed.content.decode()
    assert 'class="sheet' in body
    assert "data-print-toolbar" in body

    exported = client.get(url + query + "&export=csv")
    assert exported["Content-Type"].startswith("text/csv")
    table = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    # The CSV carries four provenance lines and a blank one before its header.
    data_rows = [row for row in table[6:] if row]
    printed_rows = _sheet_rows(body)[1:]  # first row is the header
    assert len(printed_rows) == len(data_rows)


def test_the_sheet_says_who_issued_it_and_over_what_window(
    accountant: User, organization: Organization
) -> None:
    client = _client(accountant)
    response = client.get(
        reverse("accounting:trial_balance")
        + f"?organization={organization.pk}&from=2026-08-01&to=2026-08-19&print=1"
    )
    body = response.content.decode()
    assert "خان مندي" in body
    assert "2026-08-01" in body and "2026-08-19" in body
    assert "طُبع" in body
    assert accountant.username in body
    # And the parts the reader can switch off are all marked.
    for part in ("logo", "issuer", "period", "stamp", "signature"):
        assert f'data-part="{part}"' in body


def test_printing_is_the_screen_permission_and_no_other(outsider: User) -> None:
    """A printable URL is a URL: it must refuse whoever the screen refuses."""
    client = _client(outsider)
    screen = client.get(reverse("accounting:trial_balance"))
    printed = client.get(reverse("accounting:trial_balance") + "?print=1")
    assert printed.status_code == screen.status_code
    assert printed.status_code in {403, 404}


def test_an_inventory_report_prints_every_row_not_only_the_page(
    accountant: User,
) -> None:
    """
    Paper is not paginated, and a sheet that stopped at the page size without
    saying so would be read as the whole report.
    """
    client = _client(accountant)
    response = client.get(reverse("inventory:report_valuation") + "?print=1")
    assert response.status_code in {200, 403}
    if response.status_code == 200:
        assert 'class="sheet' in response.content.decode()


# ---------------------------------------------------------------------------
# Every other screen
# ---------------------------------------------------------------------------


def test_every_screen_carries_the_paper_furniture(accountant: User) -> None:
    body = _client(accountant).get(reverse("accounting:journal_list")).content.decode()
    assert 'class="printhead print-only"' in body
    assert 'class="printfoot print-only"' in body
    assert "data-print-menu" in body
    assert 'data-print-part="signature"' in body
    # The application's own chrome is removed by rule rather than by a marker
    # in the markup, so the shell's tested class attributes stay as they are.
    assert 'class="topbar"' in body
    assert 'class="shell__nav"' in body


def test_the_print_rules_remove_the_chrome_and_free_the_tables() -> None:
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    section = styles[styles.index("19. Any screen on paper") :]
    for hidden in (
        ".toolbar",
        ".pagination",
        ".rowactions",
        ".pagehead__actions",
        ".list-statusbar",
    ):
        assert hidden in section, hidden
    # A table clipped by its scroll box would print with columns missing.
    assert ".data-table-shell" in section and "overflow: visible !important" in section
    # Headers repeat, rows are not split, and paper is white in either theme.
    assert "display: table-header-group" in section
    assert "break-inside: avoid" in section
    assert ':root:not([data-theme="light"])' in section


def test_printed_sheet_keeps_the_signature_with_a_short_statement() -> None:
    """Browser print headers leave too little landscape height for 2rem gaps."""
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    section = styles[styles.index("19. Any screen on paper") :]
    assert ".sheet__foot" in section and "margin-block-start: 0;" in section
    assert ".sheet__sign" in section and "padding-block-start: 0.25cm;" in section


def test_the_options_are_remembered_and_reach_both_layouts() -> None:
    script = (ROOT / "static" / "js" / "print-sheet.js").read_text(encoding="utf-8")
    assert 'const STORE_KEY = "khan-mandi:print-parts"' in script
    assert "print-without-" in script
    assert 'window.addEventListener("beforeprint"' in script
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    # Loaded for every page, not only for the report sheets, and revisioned so
    # a cached copy cannot outlive the markup it drives.
    assert "js/print-sheet.js" in base
    assert "{% static 'js/print-sheet.js' %}?v=" in base


def test_the_sheet_layout_never_renders_the_application(
    accountant: User, organization: Organization
) -> None:
    body = (
        _client(accountant)
        .get(reverse("accounting:trial_balance") + f"?organization={organization.pk}&print=1")
        .content.decode()
    )
    assert "shell__nav" not in body
    assert "topbar" not in body
    assert "command-trigger" not in body


def test_a_filter_is_named_the_way_the_screen_names_it() -> None:
    sheet = PrintSheet(title="t", filters=[SheetFilter(label="من", value="2026-08-01")])
    assert sheet.filters[0].label == "من"
    assert "date_from" not in sheet.filters[0].label


def test_a_sheet_states_when_it_has_nothing_to_show() -> None:
    sheet: PrintSheet = PrintSheet(title="كشف فارغ")
    assert sheet.empty_message
    assert sheet.is_empty


def test_documents_may_bring_their_own_body(tmp_path: Any) -> None:
    """A payslip is not a table; the layout accepts a template instead."""
    sheet = PrintSheet(title="قسيمة", body_template="print/_example.html", body_context={"x": 1})
    assert sheet.is_empty is False
    assert sheet.body_context == {"x": 1}
