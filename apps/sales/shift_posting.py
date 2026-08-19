"""
The authoritative cashier-shift APPROVE and REVERSE.

Same shape as `posting.py`, `adjustment_posting.py` and `settlement_posting.py`:
one command, one `transaction.atomic()`, no second ledger, no stock movement.

## The journal, and it is the whole of it

    shortage (variance_amount < 0):
        Dr  SALES_CASH_OVER_SHORT     |variance|
            Cr  SALES_CASH_ON_HAND    |variance|

    overage  (variance_amount > 0):
        Dr  SALES_CASH_ON_HAND         variance
            Cr  SALES_CASH_OVER_SHORT  variance

and when `variance_amount == 0`, **no journal at all**. That is a legitimate
outcome rather than a failure: the shift still reaches `APPROVED` and still
takes a number, because the document exists whether or not it moved money. The
kitchen's posting verifier already recognises a legitimate no-journal case; this
is the sales one.

## What this must never contain, and why it is the trap

**Sales revenue.** **The day's takings.** **The opening float.** **Card
takings.** **An `ApplicationReceivableEntry`.**

The intuitive design — the closing records what the till took — is wrong in a
way that looks right on screen. The sale already recognised the revenue and
already debited `SALES_CASH_ON_HAND` when the day posted. A closing that posted
takings again would double every cash sales figure in the system, and the
duplication would be *invisible*: both entries would be individually
defensible, both would name a real document, and the only symptom would be a
cash account that grows twice as fast as the bank deposits behind it
(ADR-027 §8).

The opening float is not revenue and is not an economic event — it is the
restaurant's own money moved from a safe to a drawer, and posting it would
invent a transaction to describe a cupboard being opened. Card takings sit in
`SALES_CARD_CLEARING` until the acquirer remits; they are not in the drawer to
be counted and nothing here touches them. And an application's debt is cleared
by a settlement, never by a count.

So what is left is the difference between what the day says should be there and
what the cashier says is there — which is the only fact this document
independently establishes, and therefore the only one it may recognise.

## The three controls, all load-bearing

1. **The day must be `POSTED` before the shift may close.** Enforced in
   `shift_services.close_cashier_shift`. An expectation derived from a draft is
   a target that can move after the count.
2. **Maker-checker on the actor**, checked here under the row lock *and* by
   `sales_shift_approver_is_not_the_closer` at the database. Neither is
   redundant: the service check is the usable error message, the constraint is
   what survives a data fix applied through a shell.
3. **The counted figures freeze at `CLOSED`**, by `0012`'s allowlist trigger.
   A declaration that can be edited after approval fails is not a declaration.

## Lock order

Extends the documented global order rather than reinterpreting it:

    1. the shift row                        select_for_update
    2. the organization's mapping resolution
    3. the sales document-number counter    select_for_update
    4. the journal-number counter           inside post_entry
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    SALES_CASH_ON_HAND,
    SALES_CASH_OVER_SHORT,
    Account,
    JournalEntry,
    SourceEvent,
)
from apps.accounting.services import post_entry, resolve_default_account, reverse_entry
from apps.accounting.validators import PostingLine
from apps.core.context import audit_context, get_correlation_id
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.sales.models import CashierShift, CashierShiftStatus
from apps.sales.posting import next_document_number

if TYPE_CHECKING:
    from apps.organizations.models import Organization
    from apps.users.models import User

ZERO = Decimal("0")

#: This document's own counter. Never the sales day's, the adjustment's or the
#: settlement's — sharing a sequence between two document types is the one thing
#: a gapless sequence must not do.
CASHIER_SHIFT_DOCUMENT_TYPE = "CASHIER_SHIFT"

#: Written in the **stored** form, upper-case with the dot retained.
#: `canonical_source_identity` case-folds the document type before persisting
#: it, so a constant spelled `sales.CashierShift` would write
#: `SALES.CASHIERSHIFT` and then fail to find itself.
SOURCE_DOCUMENT_TYPE = "SALES.CASHIERSHIFT"


@dataclass
class _Plan:
    """
    Every account and figure resolved before a single effect exists.

    `posting_lines` is **empty** when the variance is zero, and that is not an
    error state — it is the ordinary outcome of a drawer that counted right.
    """

    posting_lines: list[PostingLine]
    variance: Decimal


def _account(organization: Organization, role: str, on_date: datetime.date) -> Account:
    """The organization's account for a role on a date, or a refusal."""
    return resolve_default_account(
        organization=organization, account_role=role, on_date=on_date
    ).account


def _refuse_a_required_cost_center(account: Account) -> None:
    """
    Neither of this journal's accounts should require a cost centre.

    7-09 and 1-01 are a variance account and a balance-sheet asset. A shift has
    no principled source for a dimension: the drawer took money through every
    channel the branch sells on, and picking one channel's cost centre would
    attribute the whole day's difference to whichever happened to be first.

    So the service **refuses** rather than inventing one. A refusal is a mapping
    to fix; a fabricated dimension is a report that is quietly wrong, which is
    the worse of the two by a long way.
    """
    if account.requires_cost_center:
        raise ValidationError(
            _("%(account)s requires a cost centre, and a cashier closing has none to give.")
            % {"account": account.code},
            code="cost_center_required",
        )


def build_shift_plan(shift: CashierShift) -> _Plan:
    """
    Resolve the over/short journal for a shift, without writing.

    Reads `variance_amount` from the row rather than recomputing it from the
    counts. The figure was stamped at close and frozen by a trigger, and
    recomputing it here would introduce exactly the drift the freeze exists to
    prevent — a journal that disagreed with the document it names.
    """
    variance = quantize_money(shift.variance_amount)
    if variance == ZERO:
        return _Plan(posting_lines=[], variance=variance)

    on_date = shift.business_date
    over_short = _account(shift.organization, SALES_CASH_OVER_SHORT, on_date)
    cash = _account(shift.organization, SALES_CASH_ON_HAND, on_date)
    _refuse_a_required_cost_center(over_short)
    _refuse_a_required_cost_center(cash)

    if variance < ZERO:
        # A shortage: less in the drawer than the day says. The loss is
        # recognised and the cash asset is reduced to what is actually there.
        amount = -variance
        lines = [
            PostingLine(
                account=over_short, branch=shift.branch, cost_center=None, debit=amount, credit=ZERO
            ),
            PostingLine(
                account=cash, branch=shift.branch, cost_center=None, debit=ZERO, credit=amount
            ),
        ]
    else:
        # An overage. Recognised in the same account and not as revenue: money
        # in the drawer that no sale explains is a difference, and calling it
        # income would let a mis-rung sale look like a good day.
        lines = [
            PostingLine(
                account=cash, branch=shift.branch, cost_center=None, debit=variance, credit=ZERO
            ),
            PostingLine(
                account=over_short,
                branch=shift.branch,
                cost_center=None,
                debit=ZERO,
                credit=variance,
            ),
        ]
    return _Plan(posting_lines=lines, variance=variance)


@transaction.atomic
def approve_cashier_shift(*, shift: CashierShift, actor: User) -> CashierShift:
    """
    Agree a closed shift, take its number, and post the variance if there is one.

    The maker-checker check happens **here, under the row lock**, and not only
    at the database. Both are present and neither is redundant: this raises
    `approver_is_the_closer` with a sentence an operator can act on, and
    `sales_shift_approver_is_not_the_closer` is what still holds when somebody
    runs an `update()` in a shell at two in the morning.

    A zero variance takes a number and writes no journal. That is a legitimate
    outcome and the branch is deliberately silent about it rather than raising:
    a till that counted right must be able to finish its day.

    The idempotency key is derived from the shift's own `public_id` rather than
    accepted from a caller: approving *this shift* is the command, and an
    approved shift is frozen by a trigger, so a retry cannot present the same
    key with a different payload.
    """
    locked = CashierShift.objects.select_for_update().get(pk=shift.pk)
    if locked.status == CashierShiftStatus.APPROVED:
        raise ValidationError(_("This shift is already approved."), code="already_posted")
    if locked.status == CashierShiftStatus.REVERSED:
        raise ValidationError(
            _("A reversed shift cannot be approved again. Open a new one."),
            code="shift_reversed",
        )
    if locked.status != CashierShiftStatus.CLOSED:
        raise ValidationError(_("Only a closed shift can be approved."), code="shift_not_closed")
    if locked.closed_by_id == actor.pk:
        raise ValidationError(
            _("The person who counted the drawer cannot be the person who approves it."),
            code="approver_is_the_closer",
        )

    plan = build_shift_plan(locked)

    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        number = next_document_number(
            organization=locked.organization,
            document_type=CASHIER_SHIFT_DOCUMENT_TYPE,
            prefix="CS",
            year=locked.business_date.year,
        )
        entry_number = ""
        if plan.posting_lines:
            entry = post_entry(
                organization=locked.organization,
                accounting_date=locked.business_date,
                document_date=locked.business_date,
                lines=plan.posting_lines,
                idempotency_key=f"cashier-shift:{locked.public_id}",
                narration=f"{number} · {locked.branch.code} · {locked.business_date.isoformat()}",
                source_document_type=SOURCE_DOCUMENT_TYPE,
                source_document_id=str(locked.public_id),
                source_event=SourceEvent.POSTED,
            )
            entry_number = entry.entry_number

        previous = snapshot(locked)
        locked.status = CashierShiftStatus.APPROVED
        locked.number = number
        locked.approved_by = actor
        locked.approved_at = timezone.now()
        locked.idempotency_key = f"cashier-shift:{locked.public_id}"
        locked.save(
            update_fields=[
                "status",
                "number",
                "approved_by",
                "approved_at",
                "idempotency_key",
                "updated_at",
            ]
        )
        record_audit_event(
            action=AuditAction.POSTED,
            target=locked,
            previous_state=previous,
            new_state=snapshot(locked),
            branch=locked.branch,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
            metadata={
                # Empty when the drawer counted right, and recorded as such so
                # the trail says "no journal" rather than saying nothing.
                "journal_entry": entry_number,
                "expected_cash": str(locked.expected_cash),
                "counted_cash": str(locked.counted_cash),
                "variance": str(plan.variance),
            },
        )
    return locked


@transaction.atomic
def reverse_cashier_shift(*, shift: CashierShift, actor: User, reason: str) -> CashierShift:
    """
    Undo an approved shift: reverse its journal if it wrote one.

    Nothing is re-decided and no figure is recomputed. Only the reversal's
    *date* is current, because undoing something is an event that happens now.

    A shift approved with a zero variance has no journal to reverse, and the
    absence is handled rather than raised — refusing to reverse a document
    because it was correct would leave the only exit from a mistaken approval
    closed for exactly the shifts that had nothing wrong with them.
    """
    if not reason.strip():
        raise ValidationError(_("Reversing a shift needs a reason."), code="reason_required")

    locked = CashierShift.objects.select_for_update().get(pk=shift.pk)
    if locked.status == CashierShiftStatus.REVERSED:
        raise ValidationError(_("This shift is already reversed."), code="already_reversed")
    if locked.status != CashierShiftStatus.APPROVED:
        raise ValidationError(
            _("Only an approved shift can be reversed."), code="shift_not_approved"
        )

    entry = JournalEntry.objects.filter(
        organization=locked.organization,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
    ).first()

    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        if entry is not None:
            reverse_entry(
                entry=entry,
                idempotency_key=f"cashier-shift-reversal:{locked.public_id}",
                reason=reason.strip(),
                accounting_date=timezone.localdate(),
            )

        previous = snapshot(locked)
        locked.status = CashierShiftStatus.REVERSED
        locked.reversed_by = actor
        locked.reversed_at = timezone.now()
        locked.reversal_reason = reason.strip()
        locked.save(
            update_fields=[
                "status",
                "reversed_by",
                "reversed_at",
                "reversal_reason",
                "updated_at",
            ]
        )
        record_audit_event(
            action=AuditAction.REVERSED,
            target=locked,
            previous_state=previous,
            new_state=snapshot(locked),
            reason=reason.strip(),
            branch=locked.branch,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(locked.public_id),
            metadata={"journal_entry": entry.entry_number if entry is not None else ""},
        )
    return locked


__all__ = [
    "CASHIER_SHIFT_DOCUMENT_TYPE",
    "SOURCE_DOCUMENT_TYPE",
    "approve_cashier_shift",
    "build_shift_plan",
    "reverse_cashier_shift",
]
