"""
المصروفات · المستحقات · المقدمات — the accounting documents, over HTTP.

Every one of these is `DRAFT → APPROVED → POSTED → REVERSED`, and the API says
so: `approve`, `post` and `reverse` are separate endpoints because they are
separate decisions with separate authority, and there is no PUT that could
carry a document from one state to another as a side effect of an edit.

**Money crosses this boundary as a string**, in both directions. `"1250.001"`
arrives as text and goes through `quantize_money`; a bare JSON `1250.001` would
already have passed through a binary float before any Python code saw it, and
no amount of care further in could recover the thousandth.

**Maker-checker is not enforced here.** The services refuse a self-approval and
a self-post whoever is asking, so an API caller holding both permissions is
refused for the same reason and with the same error the screen gets. That is
the point of putting the rule in the service: this file could not weaken it
even by accident.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.accounting.document_commands import (
    add_accrual_document_line,
    add_expense_voucher_line,
    approve_accrual_document,
    approve_expense,
    approve_prepayment_document,
    discard_expense,
    list_accruals,
    list_expense_vouchers,
    list_prepayments,
    open_accrual_document,
    open_expense,
    open_prepayment_document,
    post_accrual_document,
    post_expense,
    post_prepayment_document,
    post_prepayment_schedule_line,
    read_accrual,
    read_expense_voucher,
    read_prepayment,
    remove_accrual_document_line,
    remove_expense_voucher_line,
    reverse_accrual_document,
    reverse_expense,
    reverse_prepayment_schedule_line,
)
from apps.accounting.models import (
    AccrualDocument,
    AmortizationFrequency,
    ExpenseVoucher,
    Prepayment,
)
from apps.core.money import money_export, quantize_money

router = Router(tags=["accounting-documents"])


def _actor(request: HttpRequest) -> Any:
    return request.user


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class ReasonIn(Schema):
    reason: str = ""


class RequiredReasonIn(Schema):
    reason: str


class ExpenseIn(Schema):
    branch_id: int
    business_date: datetime.date
    expense_date: datetime.date
    beneficiary: str
    reason: str
    #: Exactly one of the two. The service decides that, not this schema — a
    #: schema-level rule here would still have to be repeated for the screen.
    cashbox_id: int | None = None
    bank_account_id: int | None = None
    evidence_reference: str = ""
    notes: str = ""


class DocumentLineIn(Schema):
    account_id: int
    amount: str
    cost_center_id: int | None = None
    description: str = ""


class AccrualIn(Schema):
    branch_id: int
    business_date: datetime.date
    description: str
    reason: str = ""
    auto_reverse_on: datetime.date | None = None
    evidence_reference: str = ""


class PrepaymentIn(Schema):
    branch_id: int
    business_date: datetime.date
    description: str
    total_amount: str
    start_date: datetime.date
    frequency: str = AmortizationFrequency.MONTHLY
    period_count: int
    expense_account_id: int
    prepaid_account_id: int
    cost_center_id: int | None = None
    cashbox_id: int | None = None
    bank_account_id: int | None = None
    source_reference: str = ""
    evidence_reference: str = ""


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------


class ExpenseLineOut(Schema):
    id: int
    sequence: int
    account_id: int
    account_code: str
    cost_center_id: int | None
    description: str
    amount: str


class ExpenseOut(Schema):
    id: int
    organization_id: int
    branch_id: int
    number: str
    status: str
    business_date: datetime.date
    expense_date: datetime.date
    payment_source: str
    cashbox_id: int | None
    bank_account_id: int | None
    beneficiary: str
    reason: str
    total_amount: str
    created_by_id: int | None
    approved_by_id: int | None
    posted_by_id: int | None
    journal_entry_id: int | None
    reversal_entry_id: int | None
    lines: list[ExpenseLineOut]


class ExpenseSummaryOut(Schema):
    id: int
    organization_id: int
    branch_id: int
    number: str
    status: str
    business_date: datetime.date
    beneficiary: str
    total_amount: str


class AccrualLineOut(Schema):
    id: int
    sequence: int
    account_id: int
    account_code: str
    cost_center_id: int | None
    description: str
    amount: str


class AccrualOut(Schema):
    id: int
    organization_id: int
    branch_id: int
    number: str
    status: str
    business_date: datetime.date
    description: str
    auto_reverse_on: datetime.date | None
    total_amount: str
    journal_entry_id: int | None
    reversal_entry_id: int | None
    lines: list[AccrualLineOut]


class AccrualSummaryOut(Schema):
    id: int
    organization_id: int
    number: str
    status: str
    business_date: datetime.date
    description: str
    total_amount: str


class ScheduleLineOut(Schema):
    id: int
    sequence: int
    period_start: datetime.date
    period_end: datetime.date
    amount: str
    status: str
    journal_entry_id: int | None


class PrepaymentOut(Schema):
    id: int
    organization_id: int
    branch_id: int
    number: str
    status: str
    business_date: datetime.date
    description: str
    total_amount: str
    schedule_total: str
    start_date: datetime.date
    end_date: datetime.date
    frequency: str
    period_count: int
    expense_account_id: int
    prepaid_account_id: int
    journal_entry_id: int | None
    schedule_lines: list[ScheduleLineOut]


class PrepaymentSummaryOut(Schema):
    id: int
    organization_id: int
    number: str
    status: str
    business_date: datetime.date
    description: str
    total_amount: str


def _expense(voucher: ExpenseVoucher) -> dict[str, Any]:
    return {
        "id": voucher.pk,
        "organization_id": voucher.organization_id,
        "branch_id": voucher.branch_id,
        "number": voucher.number,
        "status": voucher.status,
        "business_date": voucher.business_date,
        "expense_date": voucher.expense_date,
        "payment_source": voucher.payment_source,
        "cashbox_id": voucher.cashbox_id,
        "bank_account_id": voucher.bank_account_id,
        "beneficiary": voucher.beneficiary,
        "reason": voucher.reason,
        "total_amount": money_export(voucher.total_amount),
        "created_by_id": voucher.created_by_id,
        "approved_by_id": voucher.approved_by_id,
        "posted_by_id": voucher.posted_by_id,
        "journal_entry_id": voucher.journal_entry_id,
        "reversal_entry_id": voucher.reversal_entry_id,
        "lines": [
            {
                "id": line.pk,
                "sequence": line.sequence,
                "account_id": line.account_id,
                "account_code": line.account.code,
                "cost_center_id": line.cost_center_id,
                "description": line.description,
                "amount": money_export(line.amount),
            }
            for line in voucher.lines.select_related("account").order_by("sequence")
        ],
    }


def _expense_summary(voucher: ExpenseVoucher) -> dict[str, Any]:
    return {
        "id": voucher.pk,
        "organization_id": voucher.organization_id,
        "branch_id": voucher.branch_id,
        "number": voucher.number,
        "status": voucher.status,
        "business_date": voucher.business_date,
        "beneficiary": voucher.beneficiary,
        "total_amount": money_export(voucher.total_amount),
    }


def _accrual(accrual: AccrualDocument) -> dict[str, Any]:
    return {
        "id": accrual.pk,
        "organization_id": accrual.organization_id,
        "branch_id": accrual.branch_id,
        "number": accrual.number,
        "status": accrual.status,
        "business_date": accrual.business_date,
        "description": accrual.description,
        "auto_reverse_on": accrual.auto_reverse_on,
        "total_amount": money_export(accrual.total_amount),
        "journal_entry_id": accrual.journal_entry_id,
        "reversal_entry_id": accrual.reversal_entry_id,
        "lines": [
            {
                "id": line.pk,
                "sequence": line.sequence,
                "account_id": line.account_id,
                "account_code": line.account.code,
                "cost_center_id": line.cost_center_id,
                "description": line.description,
                "amount": money_export(line.amount),
            }
            for line in accrual.lines.select_related("account").order_by("sequence")
        ],
    }


def _accrual_summary(accrual: AccrualDocument) -> dict[str, Any]:
    return {
        "id": accrual.pk,
        "organization_id": accrual.organization_id,
        "number": accrual.number,
        "status": accrual.status,
        "business_date": accrual.business_date,
        "description": accrual.description,
        "total_amount": money_export(accrual.total_amount),
    }


def _prepayment(prepayment: Prepayment) -> dict[str, Any]:
    lines = list(prepayment.schedule_lines.order_by("sequence"))
    schedule_total = sum((line.amount for line in lines), start=type(prepayment.total_amount)(0))
    return {
        "id": prepayment.pk,
        "organization_id": prepayment.organization_id,
        "branch_id": prepayment.branch_id,
        "number": prepayment.number,
        "status": prepayment.status,
        "business_date": prepayment.business_date,
        "description": prepayment.description,
        "total_amount": money_export(prepayment.total_amount),
        # Reported beside the header rather than assumed equal to it. The
        # allocator makes them agree exactly; showing both is how a reader
        # finds out on the day something stops agreeing.
        "schedule_total": money_export(schedule_total),
        "start_date": prepayment.start_date,
        "end_date": prepayment.end_date,
        "frequency": prepayment.frequency,
        "period_count": prepayment.period_count,
        "expense_account_id": prepayment.expense_account_id,
        "prepaid_account_id": prepayment.prepaid_account_id,
        "journal_entry_id": prepayment.journal_entry_id,
        "schedule_lines": [
            {
                "id": line.pk,
                "sequence": line.sequence,
                "period_start": line.period_start,
                "period_end": line.period_end,
                "amount": money_export(line.amount),
                "status": line.status,
                "journal_entry_id": line.journal_entry_id,
            }
            for line in lines
        ],
    }


def _prepayment_summary(prepayment: Prepayment) -> dict[str, Any]:
    return {
        "id": prepayment.pk,
        "organization_id": prepayment.organization_id,
        "number": prepayment.number,
        "status": prepayment.status,
        "business_date": prepayment.business_date,
        "description": prepayment.description,
        "total_amount": money_export(prepayment.total_amount),
    }


# ---------------------------------------------------------------------------
# المصروفات — expense vouchers
# ---------------------------------------------------------------------------


@router.get(
    "/expense-vouchers/",
    response=list[ExpenseSummaryOut],
    summary="List expense vouchers within the caller's scope",
)
def list_expenses(
    request: HttpRequest,
    organization_id: int | None = None,
    branch_id: int | None = None,
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    vouchers = list_expense_vouchers(
        actor=_actor(request),
        organization_id=organization_id,
        branch_id=branch_id,
        status=status,
    )
    window = vouchers[offset : offset + min(limit, 200)]
    return [_expense_summary(voucher) for voucher in window]


@router.get(
    "/expense-vouchers/{voucher_id}/", response=ExpenseOut, summary="Read one expense voucher"
)
def read_expense(request: HttpRequest, voucher_id: int) -> dict[str, Any]:
    return _expense(read_expense_voucher(actor=_actor(request), voucher_id=voucher_id))


@router.post(
    "/expense-vouchers/", response={201: ExpenseOut}, summary="Open a draft expense voucher"
)
def create_expense(request: HttpRequest, payload: ExpenseIn) -> Status[dict[str, Any]]:
    voucher = open_expense(actor=_actor(request), **payload.dict())
    return Status(201, _expense(voucher))


@router.post(
    "/expense-vouchers/{voucher_id}/lines/",
    response={201: ExpenseOut},
    summary="Append a line to a draft voucher",
)
def add_expense_line_endpoint(
    request: HttpRequest, voucher_id: int, payload: DocumentLineIn
) -> Status[dict[str, Any]]:
    line = add_expense_voucher_line(
        actor=_actor(request),
        voucher_id=voucher_id,
        account_id=payload.account_id,
        amount=quantize_money(payload.amount, field="amount"),
        cost_center_id=payload.cost_center_id,
        description=payload.description,
    )
    line.voucher.refresh_from_db()
    return Status(201, _expense(line.voucher))


@router.delete(
    "/expense-vouchers/{voucher_id}/lines/{line_id}/",
    response=ExpenseOut,
    summary="Remove a line from a draft voucher",
)
def remove_expense_line_endpoint(
    request: HttpRequest, voucher_id: int, line_id: int
) -> dict[str, Any]:
    actor = _actor(request)
    remove_expense_voucher_line(actor=actor, voucher_id=voucher_id, line_id=line_id)
    return _expense(read_expense_voucher(actor=actor, voucher_id=voucher_id))


@router.post(
    "/expense-vouchers/{voucher_id}/approve/",
    response=ExpenseOut,
    summary="Approve a draft — refused if the approver wrote it",
)
def approve_expense_endpoint(
    request: HttpRequest, voucher_id: int, payload: ReasonIn
) -> dict[str, Any]:
    return _expense(
        approve_expense(actor=_actor(request), voucher_id=voucher_id, reason=payload.reason)
    )


@router.post(
    "/expense-vouchers/{voucher_id}/post/",
    response=ExpenseOut,
    summary="Post an approved voucher — refused if the poster wrote it",
)
def post_expense_endpoint(
    request: HttpRequest, voucher_id: int, payload: ReasonIn
) -> dict[str, Any]:
    return _expense(
        post_expense(actor=_actor(request), voucher_id=voucher_id, reason=payload.reason)
    )


@router.post(
    "/expense-vouchers/{voucher_id}/reverse/",
    response=ExpenseOut,
    summary="Reverse a posted voucher — the original stays in the ledger",
)
def reverse_expense_endpoint(
    request: HttpRequest, voucher_id: int, payload: RequiredReasonIn
) -> dict[str, Any]:
    return _expense(
        reverse_expense(actor=_actor(request), voucher_id=voucher_id, reason=payload.reason)
    )


@router.delete(
    "/expense-vouchers/{voucher_id}/",
    response={204: None},
    summary="Abandon a draft voucher — drafts only",
)
def discard_expense_endpoint(
    request: HttpRequest, voucher_id: int, reason: str = ""
) -> Status[None]:
    discard_expense(actor=_actor(request), voucher_id=voucher_id, reason=reason)
    return Status(204, None)


# ---------------------------------------------------------------------------
# المستحقات — accruals
# ---------------------------------------------------------------------------


@router.get("/accruals/", response=list[AccrualSummaryOut], summary="List accruals within scope")
def list_accrual_documents(
    request: HttpRequest,
    organization_id: int | None = None,
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    accruals = list_accruals(actor=_actor(request), organization_id=organization_id, status=status)
    return [_accrual_summary(row) for row in accruals[offset : offset + min(limit, 200)]]


@router.get("/accruals/{accrual_id}/", response=AccrualOut, summary="Read one accrual")
def read_accrual_endpoint(request: HttpRequest, accrual_id: int) -> dict[str, Any]:
    return _accrual(read_accrual(actor=_actor(request), accrual_id=accrual_id))


@router.post("/accruals/", response={201: AccrualOut}, summary="Open a draft accrual")
def create_accrual(request: HttpRequest, payload: AccrualIn) -> Status[dict[str, Any]]:
    return Status(201, _accrual(open_accrual_document(actor=_actor(request), **payload.dict())))


@router.post(
    "/accruals/{accrual_id}/lines/",
    response={201: AccrualOut},
    summary="Append a line to a draft accrual",
)
def add_accrual_line_endpoint(
    request: HttpRequest, accrual_id: int, payload: DocumentLineIn
) -> Status[dict[str, Any]]:
    actor = _actor(request)
    add_accrual_document_line(
        actor=actor,
        accrual_id=accrual_id,
        account_id=payload.account_id,
        amount=quantize_money(payload.amount, field="amount"),
        cost_center_id=payload.cost_center_id,
        description=payload.description,
    )
    return Status(201, _accrual(read_accrual(actor=actor, accrual_id=accrual_id)))


@router.delete(
    "/accruals/{accrual_id}/lines/{line_id}/",
    response=AccrualOut,
    summary="Remove a line from a draft accrual",
)
def remove_accrual_line_endpoint(
    request: HttpRequest, accrual_id: int, line_id: int
) -> dict[str, Any]:
    actor = _actor(request)
    remove_accrual_document_line(actor=actor, accrual_id=accrual_id, line_id=line_id)
    return _accrual(read_accrual(actor=actor, accrual_id=accrual_id))


@router.post(
    "/accruals/{accrual_id}/approve/",
    response=AccrualOut,
    summary="Approve a draft accrual — refused if the approver wrote it",
)
def approve_accrual_endpoint(
    request: HttpRequest, accrual_id: int, payload: ReasonIn
) -> dict[str, Any]:
    return _accrual(
        approve_accrual_document(
            actor=_actor(request), accrual_id=accrual_id, reason=payload.reason
        )
    )


@router.post(
    "/accruals/{accrual_id}/post/",
    response=AccrualOut,
    summary="Post an approved accrual — Dr expense · Cr accrued expenses payable",
)
def post_accrual_endpoint(
    request: HttpRequest, accrual_id: int, payload: ReasonIn
) -> dict[str, Any]:
    return _accrual(
        post_accrual_document(actor=_actor(request), accrual_id=accrual_id, reason=payload.reason)
    )


@router.post(
    "/accruals/{accrual_id}/reverse/",
    response=AccrualOut,
    summary="Unwind a posted accrual when the real document arrives",
)
def reverse_accrual_endpoint(
    request: HttpRequest, accrual_id: int, payload: RequiredReasonIn
) -> dict[str, Any]:
    return _accrual(
        reverse_accrual_document(
            actor=_actor(request), accrual_id=accrual_id, reason=payload.reason
        )
    )


# ---------------------------------------------------------------------------
# المقدمات — prepayments
# ---------------------------------------------------------------------------


@router.get(
    "/prepayments/", response=list[PrepaymentSummaryOut], summary="List prepayments within scope"
)
def list_prepayment_documents(
    request: HttpRequest,
    organization_id: int | None = None,
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    prepayments = list_prepayments(
        actor=_actor(request), organization_id=organization_id, status=status
    )
    return [_prepayment_summary(row) for row in prepayments[offset : offset + min(limit, 200)]]


@router.get(
    "/prepayments/{prepayment_id}/",
    response=PrepaymentOut,
    summary="Read one prepayment and its schedule",
)
def read_prepayment_endpoint(request: HttpRequest, prepayment_id: int) -> dict[str, Any]:
    return _prepayment(read_prepayment(actor=_actor(request), prepayment_id=prepayment_id))


@router.post(
    "/prepayments/",
    response={201: PrepaymentOut},
    summary="Open a prepayment and build its schedule exactly",
)
def create_prepayment(request: HttpRequest, payload: PrepaymentIn) -> Status[dict[str, Any]]:
    data = payload.dict()
    data["total_amount"] = quantize_money(data["total_amount"], field="total_amount")
    return Status(201, _prepayment(open_prepayment_document(actor=_actor(request), **data)))


@router.post(
    "/prepayments/{prepayment_id}/approve/",
    response=PrepaymentOut,
    summary="Approve a draft prepayment — refused if the approver wrote it",
)
def approve_prepayment_endpoint(
    request: HttpRequest, prepayment_id: int, payload: ReasonIn
) -> dict[str, Any]:
    return _prepayment(
        approve_prepayment_document(
            actor=_actor(request), prepayment_id=prepayment_id, reason=payload.reason
        )
    )


@router.post(
    "/prepayments/{prepayment_id}/post/",
    response=PrepaymentOut,
    summary="Post the payment itself — Dr prepaid expense · Cr cash/bank",
)
def post_prepayment_endpoint(
    request: HttpRequest, prepayment_id: int, payload: ReasonIn
) -> dict[str, Any]:
    return _prepayment(
        post_prepayment_document(
            actor=_actor(request), prepayment_id=prepayment_id, reason=payload.reason
        )
    )


@router.post(
    "/prepayment-schedule-lines/{line_id}/post/",
    response=ScheduleLineOut,
    summary="Amortize one period — Dr expense · Cr prepaid expense",
)
def post_schedule_line_endpoint(
    request: HttpRequest, line_id: int, payload: ReasonIn
) -> dict[str, Any]:
    line = post_prepayment_schedule_line(
        actor=_actor(request), line_id=line_id, reason=payload.reason
    )
    return {
        "id": line.pk,
        "sequence": line.sequence,
        "period_start": line.period_start,
        "period_end": line.period_end,
        "amount": money_export(line.amount),
        "status": line.status,
        "journal_entry_id": line.journal_entry_id,
    }


@router.post(
    "/prepayment-schedule-lines/{line_id}/reverse/",
    response=ScheduleLineOut,
    summary="Reverse one amortization instalment",
)
def reverse_schedule_line_endpoint(
    request: HttpRequest, line_id: int, payload: RequiredReasonIn
) -> dict[str, Any]:
    line = reverse_prepayment_schedule_line(
        actor=_actor(request), line_id=line_id, reason=payload.reason
    )
    return {
        "id": line.pk,
        "sequence": line.sequence,
        "period_start": line.period_start,
        "period_end": line.period_end,
        "amount": money_export(line.amount),
        "status": line.status,
        "journal_entry_id": line.journal_entry_id,
    }
