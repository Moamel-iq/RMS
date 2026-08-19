"""
Opening, counting and closing a till. Nothing here writes a journal.

Kept apart from `shift_posting.py` for the reason `day_services.py` is kept
apart from `posting.py`: this module decides *what the shift says*, and the
other one is the only place value moves. A closing that reached the ledger
directly would put the maker-checker gap on the wrong side of the write.

## The expected figures are derived once and then stamped

`expected_cash_for` reads the posted day's cash lines and adds the opening
float. It is called **at close**, its answer is written to the row, and it is
never consulted again for that shift.

That is deliberate and it is the opposite of this repository's usual instinct.
Everywhere else a derived figure is recomputed on demand precisely so it cannot
drift; here the figure is *evidence of what was expected at the moment the
drawer was counted*, and a number that recomputed would make an approved
variance change whenever a later document did — a variance that moves after
somebody signed it is not evidence of anything. The freeze is enforced by
`0012`'s allowlist trigger, not merely by this module's discipline.

## Why the day must already be posted

`close_cashier_shift` refuses unless the named `SalesDay` is `POSTED`, with code
`day_not_posted`. A draft day's lines can still change — a line added, a
quantity corrected, a tender summary re-entered — so an expected figure derived
from one is a target that can move *after* the count. The variance would then
be the difference between a count and something still being edited, which is
not a control, and the first person to notice would be told the till was short
by an amount nobody could reproduce.

## What a shift may never do

Post the day's takings. Post the opening float. Post card takings. Write an
`ApplicationReceivableEntry`. See `shift_posting.py` for what is left, which is
one line pair and only when the count disagreed.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.sales.models import (
    CashierShift,
    CashierShiftStatus,
    CashierTenderCount,
    SalesAdjustmentLine,
    SalesAdjustmentStatus,
    SalesChannel,
    SalesDay,
    SalesDayStatus,
    TenderDestination,
)

if TYPE_CHECKING:
    from apps.organizations.models import Branch, Organization
    from apps.users.models import User

ZERO = Decimal("0")

#: The tenders a drawer can actually be counted in. `APPLICATION_RECEIVABLE` is
#: absent here and refused by a check constraint on the row: a delivery
#: application's debt is not in a drawer, it is cleared by a settlement, and a
#: box to type it into would invite somebody to.
COUNTABLE_TENDERS: tuple[str, ...] = (
    TenderDestination.CASH,
    TenderDestination.CARD,
)


def _require_open(shift: CashierShift) -> None:
    if shift.status != CashierShiftStatus.OPEN:
        raise ValidationError(
            _("Only an open cashier shift can be changed."), code="shift_not_open"
        )


@transaction.atomic
def open_cashier_shift(
    *,
    organization: Organization,
    branch: Branch,
    business_date: datetime.date,
    cashier: User,
    opening_float: Decimal,
    actor: User,
    notes: str = "",
) -> CashierShift:
    """
    Open a till for one branch on one business date.

    `opening_float` is recorded and **never posted**. It is the restaurant's own
    money moved from a safe into a drawer: no economic event happened, nothing
    changed hands with anybody outside, and posting it would invent a
    transaction to describe a cupboard being opened. It raises what the count
    should be, and that is its entire role.

    One shift per branch per date, enforced by a unique constraint. See the
    model docstring for why Release 1 decides that rather than tolerating it.
    """
    if branch.organization_id != organization.pk:
        raise ValidationError(
            _("That branch belongs to another organization."), code="branch_out_of_organization"
        )
    if opening_float < ZERO:
        raise ValidationError(_("An opening float cannot be negative."), code="float_is_negative")
    if business_date > timezone.localdate():
        raise ValidationError(
            _("A till cannot be opened for a future date."), code="future_business_date"
        )

    shift = CashierShift(
        organization=organization,
        branch=branch,
        business_date=business_date,
        cashier=cashier,
        opening_float=quantize_money(opening_float),
        opened_by=actor,
        opened_at=timezone.now(),
        notes=notes.strip(),
    )
    shift.full_clean()
    shift.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=shift,
        new_state=snapshot(shift),
        branch=branch,
    )
    return shift


# ---------------------------------------------------------------------------
# What the day says should be there
# ---------------------------------------------------------------------------


def _tender_of(channel: SalesChannel) -> str:
    """Which countable tender a non-application channel settles into."""
    if channel.default_tender == TenderDestination.CARD:
        return TenderDestination.CARD
    return TenderDestination.CASH


def refunded_by_tender(shift: CashierShift) -> dict[str, Decimal]:
    """
    What posted corrections handed back out of **this** drawer, per tender.

    Scoped by branch and business date rather than by the shift's own sales
    day, because a drawer is a physical box on one date and a refund paid from
    it today reduces it whichever day's sale it corrects. That is not a wider
    net for its own sake: it is the same scope the ledger already uses —
    `build_adjustment_plan` credits `SALES_CASH_ON_HAND` or
    `SALES_CARD_CLEARING` with `adjusted_net_amount` on the adjustment's own
    business date, and an expectation that disagreed with the credit would
    charge the difference to `SALES_CASH_OVER_SHORT` as a shortage nobody was
    short of.

    Application lines are excluded here for the reason they are excluded from
    the expectation: a delivery company's debt is not in a drawer, and the
    correction against it moves a receivable rather than cash.
    """
    refunds: dict[str, Decimal] = dict.fromkeys(COUNTABLE_TENDERS, ZERO)
    lines = SalesAdjustmentLine.objects.filter(
        adjustment__status=SalesAdjustmentStatus.POSTED,
        adjustment__branch_id=shift.branch_id,
        adjustment__business_date=shift.business_date,
        original_line__delivery_application__isnull=True,
    ).select_related("original_line__channel")
    for line in lines:
        tender = _tender_of(line.original_line.channel)
        refunds[tender] = refunds[tender] + line.adjusted_net_amount
    return refunds


def expected_by_tender(shift: CashierShift) -> dict[str, Decimal]:
    """
    What the shift's posted day says each tender took, before the float, less
    what posted corrections handed back on the same date.

    Derived from the **lines** rather than from `SalesTenderSummary`, and the
    difference matters: the summary is what the operator *declared*, the lines
    are what the document actually posted to the ledger, and reconciling a count
    against a declaration would compare two things the same person typed. The
    declaration is compared separately, on المطابقة اليومية, which is where a
    disagreement between the two is itself the finding.

    **A same-day refund is subtracted**, and reading the lines alone was the
    defect: the adjustment credits the drawer account in the ledger, so a count
    measured against the un-refunded figure is short by exactly the refund, and
    approving that shortage credits the same cash a second time — the drawer
    ends negative in the ledger against a box that really holds what it holds.
    A refund decided on a *later* date belongs to that date's drawer and is not
    subtracted here, which is why the scope is the date and not the day.

    An open shift with no day named yet answers zero for every tender rather
    than refusing — the screen wants to show a running expectation before the
    day posts, and zero is the honest answer to "what has posted so far".
    """
    totals: dict[str, Decimal] = dict.fromkeys(COUNTABLE_TENDERS, ZERO)
    day = shift.sales_day
    if day is None or day.status != SalesDayStatus.POSTED:
        return totals

    for line in day.lines.select_related("channel").all():
        if line.is_application_sale:
            # Not countable, and not this document's business. It is cleared by
            # a settlement.
            continue
        tender = _tender_of(line.channel)
        totals[tender] = totals[tender] + line.net_amount

    refunds = refunded_by_tender(shift)
    return {tender: quantize_money(amount - refunds[tender]) for tender, amount in totals.items()}


def expected_cash_for(shift: CashierShift) -> Decimal:
    """
    The opening float plus the day's posted cash takings.

    The float is in the drawer and has to be counted, so it belongs in the
    expectation — and it belongs *nowhere else*, which is the distinction this
    one line carries: it changes what should be there without ever changing what
    was earned.
    """
    return quantize_money(shift.opening_float + expected_by_tender(shift)[TenderDestination.CASH])


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


@transaction.atomic
def set_tender_count(
    *,
    shift: CashierShift,
    tender: str,
    counted_amount: Decimal,
    actor: User,
    notes: str = "",
) -> CashierTenderCount:
    """
    Record what was counted in one tender. Only while the shift is open.

    Upserts rather than appending, because a recount before the drawer is closed
    is an ordinary thing and two rows for one tender would make "what was
    counted" a question with two answers. Once the shift closes, `0012`'s child
    guard refuses both the update and the delete.
    """
    _require_open(shift)
    if tender not in COUNTABLE_TENDERS:
        raise ValidationError(
            _("%(tender)s is not something a drawer can be counted in.") % {"tender": tender},
            code="tender_is_not_countable",
        )
    amount = quantize_money(counted_amount)
    if amount < ZERO:
        raise ValidationError(_("A count cannot be negative."), code="count_is_negative")

    row = CashierTenderCount.objects.filter(shift=shift, tender=tender).first()
    previous = snapshot(row) if row is not None else None
    if row is None:
        row = CashierTenderCount(shift=shift, tender=tender)
    row.counted_amount = amount
    row.notes = notes.strip()
    row.full_clean()
    row.save()
    record_audit_event(
        action=AuditAction.UPDATED if previous is not None else AuditAction.CREATED,
        target=row,
        previous_state=previous,
        new_state=snapshot(row),
        branch=shift.branch,
    )
    return row


@transaction.atomic
def close_cashier_shift(
    *, shift: CashierShift, sales_day: SalesDay, actor: User, notes: str = ""
) -> CashierShift:
    """
    Declare the count, stamp what was expected, and compute the variance.

    Refuses unless `sales_day` is `POSTED` (`day_not_posted`) — see the module
    docstring for why an expectation derived from a draft is a moving target
    rather than a control.

    Everything this writes is then frozen by `0012`'s allowlist trigger. The way
    back is `reopen_cashier_shift`, which needs a reason and stays on the
    record; there is no way back that does not.
    """
    locked = CashierShift.objects.select_for_update().get(pk=shift.pk)
    if locked.status == CashierShiftStatus.REVERSED:
        raise ValidationError(_("A reversed shift cannot be closed again."), code="shift_reversed")
    if locked.status != CashierShiftStatus.OPEN:
        raise ValidationError(_("Only an open shift can be closed."), code="shift_not_open")

    if sales_day.status != SalesDayStatus.POSTED:
        raise ValidationError(
            _(
                "The sales day must be posted before the drawer is reconciled against "
                "it: a draft can still change after the count."
            ),
            code="day_not_posted",
        )
    if sales_day.branch_id != locked.branch_id:
        raise ValidationError(
            _("That sales day belongs to another branch."), code="day_out_of_branch"
        )
    if sales_day.business_date != locked.business_date:
        raise ValidationError(
            _("That sales day is not this shift's business date."), code="day_date_mismatch"
        )

    locked.sales_day = sales_day
    expected = expected_by_tender(locked)
    counts = {row.tender: row for row in locked.tender_counts.all()}

    # Every countable tender gets a row, whether or not anybody typed one. A
    # missing card count means zero was counted, not that card was out of
    # scope, and leaving the row out would make the two indistinguishable on
    # the reconciliation.
    for tender in COUNTABLE_TENDERS:
        row = counts.get(tender)
        if row is None:
            row = CashierTenderCount(shift=locked, tender=tender)
        row.expected_amount = expected[tender]
        row.full_clean()
        row.save()

    counted_cash = quantize_money(
        counts[TenderDestination.CASH].counted_amount if TenderDestination.CASH in counts else ZERO
    )
    expected_cash = quantize_money(locked.opening_float + expected[TenderDestination.CASH])

    previous = snapshot(locked)
    locked.status = CashierShiftStatus.CLOSED
    locked.expected_cash = expected_cash
    locked.counted_cash = counted_cash
    # Signed, and deliberately not an absolute value: the direction is what
    # decides which way the journal goes, and a magnitude would need a second
    # field to carry it.
    locked.variance_amount = quantize_money(counted_cash - expected_cash)
    locked.closed_by = actor
    locked.closed_at = timezone.now()
    if notes.strip():
        locked.notes = notes.strip()
    locked.save(
        update_fields=[
            "status",
            "sales_day",
            "expected_cash",
            "counted_cash",
            "variance_amount",
            "closed_by",
            "closed_at",
            "notes",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        branch=locked.branch,
        metadata={
            "expected_cash": str(locked.expected_cash),
            "counted_cash": str(locked.counted_cash),
            "variance": str(locked.variance_amount),
            "sales_day": sales_day.number or str(sales_day.public_id),
        },
    )
    return locked


@transaction.atomic
def reopen_cashier_shift(*, shift: CashierShift, actor: User, reason: str) -> CashierShift:
    """
    Take a closed shift back to open, on the record.

    The way back from a declaration, and the reason `0012`'s allowlist permits
    `status`, `closed_at` and `closed_by_id` to move at `CLOSED` and nothing
    else. A count that could be quietly edited would not be a declaration; one
    that could never be undone would make the first miscount permanent and would
    guarantee somebody eventually fixes it in the database instead.

    The stamped figures stay where they are until the shift closes again, which
    recomputes them from whatever the day says then.
    """
    if not reason.strip():
        raise ValidationError(_("Reopening a shift needs a reason."), code="reason_required")

    locked = CashierShift.objects.select_for_update().get(pk=shift.pk)
    if locked.status != CashierShiftStatus.CLOSED:
        raise ValidationError(_("Only a closed shift can be reopened."), code="shift_not_closed")

    previous = snapshot(locked)
    locked.status = CashierShiftStatus.OPEN
    locked.closed_by = None
    locked.closed_at = None
    locked.save(update_fields=["status", "closed_by", "closed_at", "updated_at"])
    record_audit_event(
        # `REJECTED` is the vocabulary this system already uses for sending a
        # declaration back for correction — `return_sales_day_to_draft` records
        # the same action for the same shape of act. Inventing a `REOPENED`
        # value would migrate a column to say what an existing one says.
        action=AuditAction.REJECTED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
        branch=locked.branch,
    )
    return locked


# ---------------------------------------------------------------------------
# Reads a screen needs
# ---------------------------------------------------------------------------


def candidate_days(shift: CashierShift) -> list[SalesDay]:
    """
    The posted days this shift could be closed against.

    One at most, in Release 1 — a day is unique per branch and date — but
    returned as a list so the screen offers a choice rather than asserting one,
    and so a branch that has not posted yet sees an empty selector instead of a
    dead form.
    """
    return list(
        SalesDay.objects.filter(
            branch_id=shift.branch_id,
            business_date=shift.business_date,
            status=SalesDayStatus.POSTED,
        ).select_related("branch")
    )


__all__ = [
    "COUNTABLE_TENDERS",
    "candidate_days",
    "close_cashier_shift",
    "expected_by_tender",
    "expected_cash_for",
    "open_cashier_shift",
    "refunded_by_tender",
    "reopen_cashier_shift",
    "set_tender_count",
]
