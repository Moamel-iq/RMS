"""
The authorized way into the accounting kernel.

    Role -> Permission -> Scope -> Application service -> Accounting kernel

`apps/accounting/services.py` is the kernel: it knows accounting and nothing
about users. This module is the layer above it, and it does exactly four
things — resolve every submitted identifier against the caller's own access,
check the permission at the right scope, bind the audit actor, then call the
kernel. It contains no accounting rule of its own, and if it ever needs one
that rule belongs in the kernel instead.

Two properties this arrangement is built to give:

* **An identifier cannot widen access.** Nothing here fetches an object by id
  and checks it afterwards. `resolve_branch`, `_scoped_account`, and their
  siblings take the caller with the id, so there is no intermediate state in
  which an out-of-scope object exists in a local variable waiting to be
  misused.
* **The authorized actor is the audited actor.** Every kernel call runs inside
  `audit_context(actor=...)` with the same user the permission was checked
  against, so "who was allowed to do this" and "who is recorded as having done
  it" cannot disagree.

The emergency path is not a bypass. A superuser satisfies the permission and
the scope, and then reaches the same kernel function as everyone else — the
reason is still required, the period ordering still applies, the audit event
is still written.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Model, Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    PeriodState,
)
from apps.accounting.permissions import (
    CLOSE_PERIOD,
    CREATE_DRAFT,
    EDIT_DRAFT,
    POST_JOURNAL,
    POST_SOFT_CLOSED_ADJUSTMENT,
    REOPEN_PERIOD,
    REVERSE_IN_SOFT_CLOSED_PERIOD,
    REVERSE_JOURNAL,
    SOFT_CLOSE_PERIOD,
    VIEW_JOURNAL,
)
from apps.accounting.services import (
    close_period,
    create_draft,
    discard_draft,
    post_draft,
    reopen_period,
    reverse_entry,
    soft_close_period,
    update_draft,
)
from apps.accounting.validators import PostingLine
from apps.core.context import audit_context, get_correlation_id
from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.organizations.authorization import (
    ScopeError,
    has_organization_scope,
    organization_scope,
    require_branch_permission,
    require_organization_permission,
    resolve_branch,
    resolve_organization,
)
from apps.organizations.models import Branch, Organization
from apps.organizations.selectors import accessible_branch_ids
from apps.users.models import User


@dataclass(frozen=True)
class LineInput:
    """
    One requested journal line, as identifiers rather than objects.

    Identifiers on purpose. A command that accepted model instances would
    force its caller — the API — to fetch them, and the fetch is precisely the
    step that has to be scoped. Keeping it here means there is one place where
    an account id becomes an Account, and that place checks.
    """

    account_id: int
    branch_id: int
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    cost_center_id: int | None = None
    narration: str = ""


@contextmanager
def _acting_as(actor: User) -> Iterator[None]:
    """
    Bind the audit actor to the user whose permission was just checked.

    Keeps the correlation id already established for this unit of work, so the
    kernel's audit events still group with the request that caused them.
    """
    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        yield


# ---------------------------------------------------------------------------
# Scoped resolution
# ---------------------------------------------------------------------------


def _scoped_account(organization: Organization, account_id: int) -> Account:
    """
    An account id, resolved inside one organization.

    Filtered by organization rather than fetched and compared, so another
    organization's account is simply not in the queryset. The posting
    validators check organization consistency again; this stops the request
    long before it gets there, with an error that names the real problem.
    """
    account = Account.objects.filter(organization=organization, pk=account_id).first()
    if account is None:
        raise ScopeError(
            _("Account %(id)s does not belong to %(organization)s.")
            % {"id": account_id, "organization": organization.code}
        )
    return account


def _scoped_cost_center(organization: Organization, cost_center_id: int) -> CostCenter:
    """A cost centre id, resolved inside one organization (ADR-015)."""
    cost_center = CostCenter.objects.filter(organization=organization, pk=cost_center_id).first()
    if cost_center is None:
        raise ScopeError(
            _("Cost center %(id)s does not belong to %(organization)s.")
            % {"id": cost_center_id, "organization": organization.code}
        )
    return cost_center


def _resolve_lines(
    *, actor: User, organization: Organization, lines: list[LineInput]
) -> list[PostingLine]:
    """
    Turn requested lines into posting lines, refusing anything out of scope.

    Enforces the relationship invariant at the boundary:

        journal.organization
            == line.branch.organization
            == line.account.organization
            == line.cost_center.organization when present
    """
    if not lines:
        raise ValidationError(_("A journal entry needs at least one line."), code="no_lines")

    resolved: list[PostingLine] = []
    for line in lines:
        branch = resolve_branch(actor, line.branch_id)
        if branch.organization_id != organization.pk:
            raise ScopeError(
                _("Branch %(branch)s does not belong to %(organization)s.")
                % {"branch": branch.code, "organization": organization.code}
            )
        resolved.append(
            PostingLine(
                account=_scoped_account(organization, line.account_id),
                branch=branch,
                cost_center=(
                    _scoped_cost_center(organization, line.cost_center_id)
                    if line.cost_center_id is not None
                    else None
                ),
                debit=line.debit,
                credit=line.credit,
                narration=line.narration,
            )
        )
    return resolved


def _entry_branches(entry: JournalEntry) -> list[Branch]:
    """Every distinct branch this entry touches."""
    seen: dict[int, Branch] = {}
    for line in entry.lines.select_related("branch"):
        seen[line.branch_id] = line.branch
    return list(seen.values())


def _require_at_every_branch(actor: User, permission: str, entry: JournalEntry) -> None:
    """
    The permission must hold at *every* branch the entry touches.

    Not "at least one". An entry spanning two branches moves value in both, so
    authority over half of it authorizes nothing — a user able to act at one
    branch would otherwise post a line against a branch they cannot see.
    """
    branches = _entry_branches(entry)
    if not branches:
        raise ValidationError(_("Entry %(id)s has no lines.") % {"id": entry.pk}, code="no_lines")
    for branch in branches:
        require_branch_permission(actor, permission, branch)


def _resolve_entry(actor: User, entry_id: int) -> JournalEntry:
    """
    A journal entry id, resolved against the caller's access.

    Reachable means: the entry's organization is one the caller reaches, and
    the caller works at a branch the entry touches — or holds organization-wide
    authority, which reaches entries at every branch including ones with no
    lines yet.
    """
    entry = (
        JournalEntry.objects.filter(pk=entry_id)
        .select_related("organization", "period", "period__fiscal_year")
        .first()
    )
    if entry is None:
        raise JournalEntry.DoesNotExist(f"journal entry {entry_id} does not exist")

    # Raises if the organization itself is out of reach, which covers the
    # cross-organization case before any branch is considered.
    resolve_organization(actor, entry.organization_id)

    if has_organization_scope(actor, entry.organization):
        return entry

    reachable = set(accessible_branch_ids(actor))
    touched = {line.branch_id for line in entry.lines.all()}
    if not touched or not touched.issubset(reachable):
        raise ScopeError(_("Journal entry %(id)s is outside your access.") % {"id": entry_id})
    return entry


def _resolve_period(actor: User, period_id: int) -> AccountingPeriod:
    """An accounting period id, resolved against the caller's organizations."""
    period = (
        AccountingPeriod.objects.filter(pk=period_id)
        .select_related("fiscal_year", "fiscal_year__organization")
        .first()
    )
    if period is None:
        raise AccountingPeriod.DoesNotExist(f"accounting period {period_id} does not exist")
    resolve_organization(actor, period.fiscal_year.organization_id)
    return period


def _record_override(
    *, permission: str, target: Model, organization: Organization, reason: str
) -> None:
    """
    Note that an elevated authority was used, separately from the act itself.

    A soft-closed period is meant to stop routine posting. When someone posts
    into one anyway, "an entry was posted" and "the soft close was overridden
    to allow it" are two different facts, and only the second answers the
    question a reviewer will actually ask.
    """
    record_audit_event(
        action=AuditAction.PERMISSION_OVERRIDE,
        target=target,
        reason=reason,
        metadata={"permission": permission, "organization": organization.code},
    )


def _require_soft_close_override(
    *, actor: User, permission: str, period: AccountingPeriod, reason: str, target: Model
) -> None:
    """The authorization a SOFT_CLOSED period demands: permission and a reason."""
    organization = period.fiscal_year.organization
    require_organization_permission(actor, permission, organization)
    if not reason.strip():
        raise ValidationError(
            _("Acting in soft-closed period %(period)s requires a reason.")
            % {"period": str(period)},
            code="soft_closed_reason_required",
        )
    _record_override(
        permission=permission,
        target=target,
        organization=organization,
        reason=reason.strip(),
    )


# ---------------------------------------------------------------------------
# Journal commands
# ---------------------------------------------------------------------------


@transaction.atomic
def create_draft_entry(
    *,
    actor: User,
    organization_id: int,
    accounting_date: datetime.date,
    lines: list[LineInput],
    document_date: datetime.date | None = None,
    narration: str = "",
    is_adjustment: bool = False,
    idempotency_key: str | None = None,
) -> JournalEntry:
    """Create a draft, having checked the caller may do so at every branch named."""
    organization = resolve_organization(actor, organization_id)
    posting_lines = _resolve_lines(actor=actor, organization=organization, lines=lines)

    for branch in {line.branch.pk: line.branch for line in posting_lines}.values():
        require_branch_permission(actor, CREATE_DRAFT, branch)

    with _acting_as(actor):
        return create_draft(
            organization=organization,
            accounting_date=accounting_date,
            lines=posting_lines,
            document_date=document_date,
            narration=narration,
            is_adjustment=is_adjustment,
            idempotency_key=idempotency_key,
        )


@transaction.atomic
def amend_draft_entry(
    *,
    actor: User,
    entry_id: int,
    accounting_date: datetime.date | None = None,
    lines: list[LineInput] | None = None,
    document_date: datetime.date | None = None,
    narration: str | None = None,
    is_adjustment: bool | None = None,
) -> JournalEntry:
    """
    Amend a draft.

    Authorized twice when the lines change: at the branches the draft touches
    now, and at the branches it would touch afterwards. Checking only the
    first would let someone move an entry onto a branch they cannot reach;
    checking only the second would let them empty one they cannot reach.
    """
    entry = _resolve_entry(actor, entry_id)
    if entry.status != JournalEntryStatus.DRAFT:
        raise ValidationError(
            _("Entry %(number)s is not a draft and cannot be amended.")
            % {"number": entry.entry_number or entry.pk},
            code="not_a_draft",
        )
    _require_at_every_branch(actor, EDIT_DRAFT, entry)

    posting_lines = None
    if lines is not None:
        posting_lines = _resolve_lines(actor=actor, organization=entry.organization, lines=lines)
        for branch in {line.branch.pk: line.branch for line in posting_lines}.values():
            require_branch_permission(actor, EDIT_DRAFT, branch)

    with _acting_as(actor):
        return update_draft(
            entry=entry,
            accounting_date=accounting_date,
            lines=posting_lines,
            document_date=document_date,
            narration=narration,
            is_adjustment=is_adjustment,
        )


@transaction.atomic
def post_journal_entry(*, actor: User, entry_id: int, reason: str = "") -> JournalEntry:
    """
    Post a draft to the ledger.

    A SOFT_CLOSED period needs more than `post_journal`: it needs
    `post_soft_closed_adjustment` over the organization and a stated reason,
    and the override is audited in its own right.
    """
    entry = _resolve_entry(actor, entry_id)
    _require_at_every_branch(actor, POST_JOURNAL, entry)

    allow_closed_period = False
    if entry.period.state == PeriodState.SOFT_CLOSED:
        _require_soft_close_override(
            actor=actor,
            permission=POST_SOFT_CLOSED_ADJUSTMENT,
            period=entry.period,
            reason=reason,
            target=entry,
        )
        allow_closed_period = True

    with _acting_as(actor):
        return post_draft(entry=entry, allow_closed_period=allow_closed_period)


@transaction.atomic
def reverse_journal_entry(
    *,
    actor: User,
    entry_id: int,
    reason: str,
    accounting_date: datetime.date | None = None,
    idempotency_key: str | None = None,
) -> JournalEntry:
    """
    Reverse a posted entry.

    The idempotency key defaults to one derived from the entry being reversed,
    so a retried request cannot produce a second mirror even if the caller
    forgets to send a key. A genuine repeat is reported as `already_reversed`
    rather than quietly returning something.
    """
    entry = _resolve_entry(actor, entry_id)
    _require_at_every_branch(actor, REVERSE_JOURNAL, entry)

    target_date = accounting_date or entry.accounting_date
    target_period = (
        AccountingPeriod.objects.filter(
            fiscal_year__organization=entry.organization,
            start_date__lte=target_date,
            end_date__gte=target_date,
        )
        .select_related("fiscal_year", "fiscal_year__organization")
        .first()
    )

    if target_period is not None and target_period.state == PeriodState.SOFT_CLOSED:
        _require_soft_close_override(
            actor=actor,
            permission=REVERSE_IN_SOFT_CLOSED_PERIOD,
            period=target_period,
            reason=reason,
            target=entry,
        )

    with _acting_as(actor):
        return reverse_entry(
            entry=entry,
            idempotency_key=idempotency_key or f"reversal-of:{entry.pk}",
            reason=reason,
            accounting_date=accounting_date,
        )


@transaction.atomic
def discard_draft_entry(*, actor: User, entry_id: int, reason: str = "") -> None:
    """Delete a draft. Never anything that reached the ledger."""
    entry = _resolve_entry(actor, entry_id)
    if entry.status != JournalEntryStatus.DRAFT:
        raise ValidationError(
            _("Entry %(number)s is posted and cannot be deleted. Reverse it instead.")
            % {"number": entry.entry_number or entry.pk},
            code="not_a_draft",
        )
    _require_at_every_branch(actor, EDIT_DRAFT, entry)

    with _acting_as(actor):
        discard_draft(entry=entry, reason=reason)


def read_journal_entry(*, actor: User, entry_id: int) -> JournalEntry:
    """One entry, if the caller may see it."""
    entry = _resolve_entry(actor, entry_id)
    _require_at_every_branch(actor, VIEW_JOURNAL, entry)
    return entry


def list_journal_entries(
    *,
    actor: User,
    organization_id: int | None = None,
    status: str | None = None,
) -> QuerySet[JournalEntry]:
    """
    The entries this caller may see, filtered in SQL rather than in Python.

    Filtered, not checked-then-listed: a list endpoint that fetched everything
    and dropped the rows it should not show would still have loaded them, and
    the first `.count()` or aggregate written against it would report figures
    the caller is not entitled to.
    """
    if not actor.has_perm(VIEW_JOURNAL):
        raise ScopeError(_("%(permission)s is not held.") % {"permission": VIEW_JOURNAL})

    branch_ids = accessible_branch_ids(actor)
    organization_ids = set(organization_scope(actor))
    organization_ids.update(
        Branch.objects.filter(pk__in=branch_ids).values_list("organization_id", flat=True)
    )

    entries = JournalEntry.objects.filter(organization_id__in=organization_ids)

    # An entry touching a branch outside the caller's access is hidden unless
    # they hold authority over the whole organization.
    from apps.accounting.models import JournalLine

    foreign = JournalLine.objects.exclude(branch_id__in=branch_ids).values("entry_id")
    entries = entries.exclude(Q(pk__in=foreign) & ~Q(organization_id__in=organization_scope(actor)))

    if organization_id is not None:
        resolve_organization(actor, organization_id)
        entries = entries.filter(organization_id=organization_id)
    if status is not None:
        entries = entries.filter(status=status)

    return entries.select_related("organization", "period").order_by("-accounting_date", "-id")


# ---------------------------------------------------------------------------
# Period commands
# ---------------------------------------------------------------------------


@transaction.atomic
def soft_close_accounting_period(
    *, actor: User, period_id: int, reason: str = ""
) -> AccountingPeriod:
    """Stop routine posting. Organization authority; not order-constrained."""
    period = _resolve_period(actor, period_id)
    require_organization_permission(actor, SOFT_CLOSE_PERIOD, period.fiscal_year.organization)
    with _acting_as(actor):
        return soft_close_period(period=period, reason=reason)


@transaction.atomic
def close_accounting_period(*, actor: User, period_id: int, reason: str = "") -> AccountingPeriod:
    """Close a period. Chronological order is enforced by the kernel."""
    period = _resolve_period(actor, period_id)
    require_organization_permission(actor, CLOSE_PERIOD, period.fiscal_year.organization)
    with _acting_as(actor):
        return close_period(period=period, reason=reason)


@transaction.atomic
def reopen_accounting_period(*, actor: User, period_id: int, reason: str) -> AccountingPeriod:
    """
    Reopen a closed period.

    The most sensitive act in the ledger, and the one place where every guard
    has to hold at once: `accounting.reopen_period` held over the organization
    — branch authority never reaches it — a reason that is not whitespace, a
    named actor, reverse-chronological ordering, and an audit event carrying
    all of it.

    The reason is checked here as well as in the kernel so the caller is
    refused before any state is touched. The kernel checks it again because
    the kernel is also reachable from a management command.
    """
    period = _resolve_period(actor, period_id)
    organization = period.fiscal_year.organization
    require_organization_permission(actor, REOPEN_PERIOD, organization)

    if not reason.strip():
        raise ValidationError(
            _("Reopening a period requires a reason."), code="reopen_reason_required"
        )

    before = snapshot(AccountingPeriod.objects.get(pk=period.pk))
    with _acting_as(actor):
        reopened = reopen_period(period=period, reason=reason)
        # The kernel audits the state change. This second event records the
        # authority that permitted it, which is the question an auditor asks
        # about a reopening — not "did it change" but "who was allowed to".
        record_audit_event(
            action=AuditAction.PERMISSION_OVERRIDE,
            target=reopened,
            previous_state=before,
            new_state=snapshot(reopened),
            reason=reason.strip(),
            metadata={
                "permission": REOPEN_PERIOD,
                "organization": organization.code,
                "period": str(reopened),
            },
        )
    return reopened
