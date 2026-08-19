"""
The accounting API. Commands, not CRUD.

The ledger is not a table to be edited, so the endpoints are named after
accounting acts — post, reverse, close, reopen — and there is no generic
writable resource for posted state anywhere in this file. A journal that could
be PUT would be a journal that could be rewritten.

This layer does five things and nothing else:

    authenticate -> authorize -> validate the input shape -> call the
    application service -> serialize the result

Every write goes through `apps.accounting.commands`. Nothing here calls
`Model.objects.create` or `.update` on ledger state, and nothing here re-states
an accounting rule — a rule duplicated in a view is a rule that will disagree
with the kernel the first time one of them is changed.

**Money crosses this boundary as strings.** Amounts arrive as strings and are
parsed with `quantize_money`; they leave as strings from `money_export`. JSON
has one numeric type and it is binary floating point, so a bare `1250.001` in
a request body would already have been through a float before any Python code
saw it. A string cannot be rounded by a parser.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.accounting.api_documents import router as document_router
from apps.accounting.api_master import router as master_router
from apps.accounting.api_reports import router as report_router
from apps.accounting.commands import (
    LineInput,
    amend_draft_entry,
    close_accounting_period,
    create_draft_entry,
    discard_draft_entry,
    list_journal_entries,
    post_journal_entry,
    read_journal_entry,
    reopen_accounting_period,
    reverse_journal_entry,
    soft_close_accounting_period,
)
from apps.accounting.models import AccountingPeriod, JournalEntry
from apps.core.money import money_export, quantize_money
from apps.users.models import User

router = Router(tags=["accounting"])

# The rest of the module's surface, split by subject rather than piled into one
# file: journals and periods here, master data, documents and reports in their
# own modules. Mounted on this router so `config/api.py` keeps naming one
# accounting entry point.
router.add_router("", master_router)
router.add_router("", document_router)
router.add_router("", report_router)


def _actor(request: HttpRequest) -> User:
    """The authenticated caller. Auth is enforced by the API's auth backend."""
    user: User = request.user  # type: ignore[assignment]
    return user


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class LineIn(Schema):
    """
    One requested journal line.

    `debit` and `credit` are strings. See the module docstring: a JSON number
    is a float before it is anything else.
    """

    account_id: int
    branch_id: int
    debit: str = "0"
    credit: str = "0"
    cost_center_id: int | None = None
    narration: str = ""

    def to_input(self) -> LineInput:
        return LineInput(
            account_id=self.account_id,
            branch_id=self.branch_id,
            # Parsed here rather than deeper down so a malformed amount is a
            # 422 about the request, not a failure inside the kernel.
            debit=quantize_money(self.debit, field="debit"),
            credit=quantize_money(self.credit, field="credit"),
            cost_center_id=self.cost_center_id,
            narration=self.narration,
        )


class DraftIn(Schema):
    organization_id: int
    accounting_date: datetime.date
    lines: list[LineIn]
    document_date: datetime.date | None = None
    narration: str = ""
    is_adjustment: bool = False
    idempotency_key: str | None = None


class DraftPatchIn(Schema):
    """Every field optional: absent means "leave it alone", not "clear it"."""

    accounting_date: datetime.date | None = None
    lines: list[LineIn] | None = None
    document_date: datetime.date | None = None
    narration: str | None = None
    is_adjustment: bool | None = None


class PostIn(Schema):
    #: Required only when the period is soft-closed, where the override needs
    #: a stated reason. Checked by the command, not here.
    reason: str = ""


class ReverseIn(Schema):
    reason: str
    accounting_date: datetime.date | None = None
    idempotency_key: str | None = None


class PeriodActionIn(Schema):
    reason: str = ""


class ReopenIn(Schema):
    #: Not optional. Reopening a closed period without saying why is the one
    #: thing an auditor cannot be asked to accept.
    reason: str


class DiscardIn(Schema):
    reason: str = ""


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------


class LineOut(Schema):
    line_number: int
    account_id: int
    account_code: str
    branch_id: int
    cost_center_id: int | None
    debit: str
    credit: str
    narration: str


class EntryOut(Schema):
    id: int
    organization_id: int
    period_id: int
    entry_number: str
    status: str
    accounting_date: datetime.date
    document_date: datetime.date
    is_adjustment: bool
    narration: str
    source_document_type: str
    source_document_id: str
    source_event: str
    idempotency_key: str
    reverses_id: int | None
    debit_total: str
    credit_total: str
    lines: list[LineOut]


class EntrySummaryOut(Schema):
    id: int
    organization_id: int
    entry_number: str
    status: str
    accounting_date: datetime.date
    source_document_type: str
    source_document_id: str
    source_event: str


class PeriodOut(Schema):
    id: int
    fiscal_year: int
    period_number: int
    start_date: datetime.date
    end_date: datetime.date
    state: str


def _serialize_entry(entry: JournalEntry) -> dict[str, Any]:
    """
    An entry as exact decimal strings.

    Totals are summed from the stored line Decimals and rendered once, at the
    end. Rendering each line and adding the strings would be the mistake the
    money policy exists to prevent.
    """
    lines = list(entry.lines.select_related("account").order_by("line_number"))
    debit_total = sum((line.debit for line in lines), start=quantize_money("0"))
    credit_total = sum((line.credit for line in lines), start=quantize_money("0"))

    return {
        "id": entry.pk,
        "organization_id": entry.organization_id,
        "period_id": entry.period_id,
        "entry_number": entry.entry_number,
        "status": entry.status,
        "accounting_date": entry.accounting_date,
        "document_date": entry.document_date,
        "is_adjustment": entry.is_adjustment,
        "narration": entry.narration,
        "source_document_type": entry.source_document_type,
        "source_document_id": entry.source_document_id,
        "source_event": entry.source_event,
        "idempotency_key": entry.idempotency_key,
        "reverses_id": entry.reverses_id,
        "debit_total": money_export(debit_total),
        "credit_total": money_export(credit_total),
        "lines": [
            {
                "line_number": line.line_number,
                "account_id": line.account_id,
                "account_code": line.account.code,
                "branch_id": line.branch_id,
                "cost_center_id": line.cost_center_id,
                "debit": money_export(line.debit),
                "credit": money_export(line.credit),
                "narration": line.narration,
            }
            for line in lines
        ],
    }


def _serialize_period(period: AccountingPeriod) -> dict[str, Any]:
    return {
        "id": period.pk,
        "fiscal_year": period.fiscal_year.year,
        "period_number": period.period_number,
        "start_date": period.start_date,
        "end_date": period.end_date,
        "state": period.state,
    }


# ---------------------------------------------------------------------------
# Journal entry commands
# ---------------------------------------------------------------------------


@router.get(
    "/journal-entries/",
    response=list[EntrySummaryOut],
    summary="List journal entries within the caller's scope",
)
def list_entries(
    request: HttpRequest,
    organization_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    entries = list_journal_entries(
        actor=_actor(request), organization_id=organization_id, status=status
    )
    window = entries[offset : offset + min(limit, 200)]
    return [
        {
            "id": entry.pk,
            "organization_id": entry.organization_id,
            "entry_number": entry.entry_number,
            "status": entry.status,
            "accounting_date": entry.accounting_date,
            "source_document_type": entry.source_document_type,
            "source_document_id": entry.source_document_id,
            "source_event": entry.source_event,
        }
        for entry in window
    ]


@router.get(
    "/journal-entries/{entry_id}/",
    response=EntryOut,
    summary="Read one journal entry",
)
def read_entry(request: HttpRequest, entry_id: int) -> dict[str, Any]:
    entry = read_journal_entry(actor=_actor(request), entry_id=entry_id)
    return _serialize_entry(entry)


@router.post(
    "/journal-entries/",
    response={201: EntryOut},
    summary="Create a draft journal entry",
)
def create_entry(request: HttpRequest, payload: DraftIn) -> Status[dict[str, Any]]:
    entry = create_draft_entry(
        actor=_actor(request),
        organization_id=payload.organization_id,
        accounting_date=payload.accounting_date,
        lines=[line.to_input() for line in payload.lines],
        document_date=payload.document_date,
        narration=payload.narration,
        is_adjustment=payload.is_adjustment,
        idempotency_key=payload.idempotency_key,
    )
    return Status(201, _serialize_entry(entry))


@router.patch(
    "/journal-entries/{entry_id}/",
    response=EntryOut,
    summary="Amend a draft journal entry — drafts only",
)
def amend_entry(request: HttpRequest, entry_id: int, payload: DraftPatchIn) -> dict[str, Any]:
    entry = amend_draft_entry(
        actor=_actor(request),
        entry_id=entry_id,
        accounting_date=payload.accounting_date,
        lines=([line.to_input() for line in payload.lines] if payload.lines is not None else None),
        document_date=payload.document_date,
        narration=payload.narration,
        is_adjustment=payload.is_adjustment,
    )
    return _serialize_entry(entry)


@router.delete(
    "/journal-entries/{entry_id}/",
    response={204: None},
    summary="Discard a draft journal entry — drafts only",
)
def discard_entry(
    request: HttpRequest, entry_id: int, payload: DiscardIn | None = None
) -> Status[None]:
    discard_draft_entry(
        actor=_actor(request),
        entry_id=entry_id,
        reason=payload.reason if payload else "",
    )
    return Status(204, None)


@router.post(
    "/journal-entries/{entry_id}/post/",
    response=EntryOut,
    summary="Post a draft to the ledger",
)
def post_entry_command(
    request: HttpRequest, entry_id: int, payload: PostIn | None = None
) -> dict[str, Any]:
    entry = post_journal_entry(
        actor=_actor(request),
        entry_id=entry_id,
        reason=payload.reason if payload else "",
    )
    return _serialize_entry(entry)


@router.post(
    "/journal-entries/{entry_id}/reverse/",
    response={201: EntryOut},
    summary="Reverse a posted entry by appending its mirror",
)
def reverse_entry_command(
    request: HttpRequest, entry_id: int, payload: ReverseIn
) -> Status[dict[str, Any]]:
    reversal = reverse_journal_entry(
        actor=_actor(request),
        entry_id=entry_id,
        reason=payload.reason,
        accounting_date=payload.accounting_date,
        idempotency_key=payload.idempotency_key,
    )
    return Status(201, _serialize_entry(reversal))


# ---------------------------------------------------------------------------
# Period commands
# ---------------------------------------------------------------------------


@router.post(
    "/periods/{period_id}/soft-close/",
    response=PeriodOut,
    summary="Stop routine posting into a period",
)
def soft_close(
    request: HttpRequest, period_id: int, payload: PeriodActionIn | None = None
) -> dict[str, Any]:
    period = soft_close_accounting_period(
        actor=_actor(request),
        period_id=period_id,
        reason=payload.reason if payload else "",
    )
    return _serialize_period(period)


@router.post(
    "/periods/{period_id}/close/",
    response=PeriodOut,
    summary="Close a period — chronological order enforced",
)
def close(
    request: HttpRequest, period_id: int, payload: PeriodActionIn | None = None
) -> dict[str, Any]:
    period = close_accounting_period(
        actor=_actor(request),
        period_id=period_id,
        reason=payload.reason if payload else "",
    )
    return _serialize_period(period)


@router.post(
    "/periods/{period_id}/reopen/",
    response=PeriodOut,
    summary="Reopen a closed period — organization authority, reason required",
)
def reopen(request: HttpRequest, period_id: int, payload: ReopenIn) -> dict[str, Any]:
    period = reopen_accounting_period(
        actor=_actor(request), period_id=period_id, reason=payload.reason
    )
    return _serialize_period(period)
