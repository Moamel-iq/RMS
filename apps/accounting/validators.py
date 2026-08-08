"""
Accounting validators.

Every rule here is also a line in docs/specs/accounting-kernel-invariants.md.
They are separated from the posting service so each can be tested on its own
and so a later module cannot post by reimplementing "most of" the checks.

Several are also enforced by database constraints or triggers. That is
deliberate duplication: the Python check produces an error an operator can
act on, and the database check holds when someone bypasses Python entirely.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    CostCenter,
    PeriodState,
    SourceEvent,
)
from apps.core.money import quantize_money
from apps.organizations.models import Branch, Organization


@dataclass(frozen=True)
class PostingLine:
    """One line of a posting request, before it becomes a JournalLine."""

    account: Account
    branch: Branch
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    cost_center: CostCenter | None = None
    narration: str = ""


def validate_lines_present(lines: Sequence[PostingLine]) -> None:
    if len(lines) < 2:
        raise ValidationError(
            _("A journal entry needs at least two lines."),
            code="too_few_lines",
        )


def validate_line_sides(lines: Sequence[PostingLine]) -> None:
    """Exactly one of debit and credit carries the amount, and it is positive."""
    for index, line in enumerate(lines, start=1):
        debit = quantize_money(line.debit, field=f"line {index} debit")
        credit = quantize_money(line.credit, field=f"line {index} credit")

        if debit < 0 or credit < 0:
            raise ValidationError(
                _("Line %(line)s has a negative amount. Use the other side instead."),
                code="negative_amount",
                params={"line": index},
            )
        if debit > 0 and credit > 0:
            raise ValidationError(
                _("Line %(line)s carries both a debit and a credit."),
                code="both_sides",
                params={"line": index},
            )
        if debit == 0 and credit == 0:
            raise ValidationError(
                _("Line %(line)s has no amount."),
                code="zero_amount",
                params={"line": index},
            )


def validate_both_sides_present(lines: Sequence[PostingLine]) -> None:
    """
    At least one debit line and at least one credit line.

    Balance alone is not enough. Two lines of zero balance perfectly and record
    nothing; so would a set of debits that happened to sum to the same figure
    as no credits at all. An entry has to move value in both directions to be
    an entry.
    """
    has_debit = any(quantize_money(line.debit) > 0 for line in lines)
    has_credit = any(quantize_money(line.credit) > 0 for line in lines)

    if not has_debit or not has_credit:
        raise ValidationError(
            _("An entry needs at least one debit line and at least one credit line."),
            code="one_sided_entry",
        )


def validate_entry_has_value(lines: Sequence[PostingLine]) -> None:
    """
    The entry moves a non-zero amount.

    A balanced entry of zero passes every other check and means nothing. It is
    noise in a ledger that has to reconcile.
    """
    total = sum((quantize_money(line.debit) for line in lines), Decimal("0"))
    if total == 0:
        raise ValidationError(
            _("An entry must move a non-zero amount."),
            code="zero_value_entry",
        )


def validate_balanced(lines: Sequence[PostingLine]) -> None:
    """
    Total debits equal total credits, compared on STORED 3-decimal values.

    Quantizing first is the point: comparing unrounded values could pass a set
    of lines that, once stored, no longer balance.
    """
    debits = sum((quantize_money(line.debit) for line in lines), Decimal("0"))
    credits = sum((quantize_money(line.credit) for line in lines), Decimal("0"))

    if debits != credits:
        raise ValidationError(
            _("Entry does not balance: debits %(debits)s, credits %(credits)s."),
            code="unbalanced",
            params={"debits": str(debits), "credits": str(credits)},
        )


def validate_accounts_are_postable(lines: Sequence[PostingLine]) -> None:
    """
    Only leaf accounts accept lines.

    Posting to a parent stops its children summing to it, and from that point
    no report over the hierarchy can be trusted.

    "An account with children never accepts postings" is structural rather
    than checked: `is_postable` is true only for a four-segment detail code,
    and no valid code extends one, so a postable account cannot acquire a
    child. The explicit check here is the second lock.
    """
    for index, line in enumerate(lines, start=1):
        # Postability first: it is the reason in almost every real case, and
        # "this is a group account" is more useful than "this has children".
        if not line.account.is_postable:
            raise ValidationError(
                _("Line %(line)s posts to %(code)s, which is a group account."),
                code="account_not_postable",
                params={"line": index, "code": line.account.code},
            )
        if line.account.children.exists():
            raise ValidationError(
                _("Line %(line)s posts to %(code)s, which has child accounts."),
                code="account_has_children",
                params={"line": index, "code": line.account.code},
            )
        if not line.account.is_active:
            raise ValidationError(
                _("Line %(line)s posts to %(code)s, which is archived."),
                code="account_inactive",
                params={"line": index, "code": line.account.code},
            )


def validate_cost_centers(lines: Sequence[PostingLine]) -> None:
    """
    A cost centre is required exactly where the account says so (ADR-015).

    Not on every line: a bank balance has no meaningful cost centre, and a
    forced value there corrupts the managerial analysis it was meant to serve.
    """
    for index, line in enumerate(lines, start=1):
        if line.account.requires_cost_center and line.cost_center is None:
            raise ValidationError(
                _("Line %(line)s posts to %(code)s, which requires a cost center."),
                code="cost_center_required",
                params={"line": index, "code": line.account.code},
            )
        if line.cost_center is not None and not line.cost_center.is_active:
            raise ValidationError(
                _("Line %(line)s uses cost center %(code)s, which is archived."),
                code="cost_center_inactive",
                params={"line": index, "code": line.cost_center.code},
            )


def validate_organization_consistency(
    organization: Organization, lines: Sequence[PostingLine]
) -> None:
    """
    journal.organization == branch.organization == account.organization
    == cost_center.organization.

    One inconsistent line is one organization's money recorded in another's
    ledger, which no later report can unpick.
    """
    for index, line in enumerate(lines, start=1):
        if line.account.organization_id != organization.pk:
            raise ValidationError(
                _("Line %(line)s uses an account from another organization."),
                code="account_organization_mismatch",
                params={"line": index},
            )
        if line.branch.organization_id != organization.pk:
            raise ValidationError(
                _("Line %(line)s uses a branch from another organization."),
                code="branch_organization_mismatch",
                params={"line": index},
            )
        if line.cost_center is not None and line.cost_center.organization_id != organization.pk:
            raise ValidationError(
                _("Line %(line)s uses a cost center from another organization."),
                code="cost_center_organization_mismatch",
                params={"line": index},
            )


def validate_branches_are_active(lines: Sequence[PostingLine]) -> None:
    for index, line in enumerate(lines, start=1):
        if not line.branch.is_active:
            raise ValidationError(
                _("Line %(line)s posts to branch %(code)s, which is closed."),
                code="branch_inactive",
                params={"line": index, "code": line.branch.code},
            )


def validate_period_accepts_postings(period: AccountingPeriod) -> None:
    """
    Nothing posts into a CLOSED period except through the authorized reopening
    workflow, and SOFT_CLOSED stops routine posting.
    """
    if period.state == PeriodState.CLOSED:
        raise ValidationError(
            _("Period %(period)s is closed. Reopening is an authorized action."),
            code="period_closed",
            params={"period": str(period)},
        )
    if period.state == PeriodState.SOFT_CLOSED:
        raise ValidationError(
            _("Period %(period)s is soft-closed to routine postings."),
            code="period_soft_closed",
            params={"period": str(period)},
        )


def validate_source_identity(
    *, source_document_type: str, source_document_id: str, source_event: str
) -> None:
    """
    A source identity is all three fields or none of them.

    Together with the organization they name one upstream economic event —
    `KM / PURCHASE_INVOICE / 145 / POSTED` — and that tuple is what stops a
    retried purchase invoice becoming two journals. A half-populated identity
    is not merely untidy: the unique index is partial on `source_event`, so an
    entry carrying a type and an id but no event would sit *outside* the
    guarantee while looking, to a reader, exactly like an entry inside it.

    A manual journal has no upstream document and correctly carries none of
    the three.
    """
    present = [
        bool(source_document_type.strip()),
        bool(source_document_id.strip()),
        bool(source_event.strip()),
    ]
    if any(present) and not all(present):
        raise ValidationError(
            _(
                "A source identity needs a type, an id, and an event, or none of "
                "the three. Got type=%(type)r id=%(id)r event=%(event)r."
            ),
            code="incomplete_source_identity",
            params={
                "type": source_document_type,
                "id": source_document_id,
                "event": source_event,
            },
        )

    if source_event and source_event not in SourceEvent.values:
        raise ValidationError(
            _("%(event)s is not a known source event."),
            code="unknown_source_event",
            params={"event": source_event},
        )


def validate_parent_has_no_posting_history(parent: Account) -> None:
    """
    An account that has ever received posted lines must never become a parent.

    Adding a child beneath it would turn a posting account into a rollup, and
    its own historic lines would then sit at a level that no longer accepts
    them — the hierarchy would stop summing correctly from that day backwards.

    Structurally this cannot happen while codes carry their level: only a
    four-segment detail code is postable, and no valid code extends one. The
    check exists so that a future change to the code scheme cannot quietly
    open the hole.
    """
    if parent.journal_lines.exists():
        raise ValidationError(
            _("Account %(code)s already has posted lines and cannot become a parent."),
            code="parent_has_posting_history",
            params={"code": parent.code},
        )


def validate_account_parentage(account: Account) -> None:
    """
    A child's code extends its parent's, and both belong to one organization.

    Without the prefix rule the hierarchy is decorative: a rollup would total
    accounts that do not belong beneath it.
    """
    if account.parent is None:
        # Only a class-level account may stand alone.
        if account.code.count("-") != 0:
            raise ValidationError(
                _("Account %(code)s needs a parent."),
                code="missing_parent",
                params={"code": account.code},
            )
        return

    parent = account.parent
    if parent.organization_id != account.organization_id:
        raise ValidationError(
            _("Account %(code)s and its parent belong to different organizations."),
            code="parent_organization_mismatch",
            params={"code": account.code},
        )
    expected_prefix = account.code.rsplit("-", 1)[0]
    if parent.code != expected_prefix:
        raise ValidationError(
            _("Account %(code)s must sit under %(expected)s, not %(actual)s."),
            code="parent_code_mismatch",
            params={
                "code": account.code,
                "expected": expected_prefix,
                "actual": parent.code,
            },
        )
    if parent.account_class != account.account_class:
        raise ValidationError(
            _("Account %(code)s is in a different class from its parent."),
            code="parent_class_mismatch",
            params={"code": account.code},
        )
