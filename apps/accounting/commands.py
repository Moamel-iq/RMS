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
    AccountReportMapping,
    AccountRole,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    ManualPostingPolicy,
    OrganizationAccountMapping,
    PeriodState,
    PresentationSection,
)
from apps.accounting.permissions import (
    CLOSE_PERIOD,
    CREATE_DRAFT,
    EDIT_DRAFT,
    MANAGE_ACCOUNT_MAPPINGS,
    MANAGE_ACCOUNTS,
    MANAGE_CHART_OF_ACCOUNTS,
    MANAGE_REPORT_MAPPINGS,
    POST_JOURNAL,
    POST_RESTRICTED_MANUAL_JOURNAL,
    POST_SOFT_CLOSED_ADJUSTMENT,
    REOPEN_PERIOD,
    REVERSE_IN_SOFT_CLOSED_PERIOD,
    REVERSE_JOURNAL,
    SOFT_CLOSE_PERIOD,
    VIEW_CHART_OF_ACCOUNTS,
    VIEW_JOURNAL,
)
from apps.accounting.services import (
    amend_account_mapping,
    archive_account,
    archive_account_mapping,
    clear_report_mapping,
    close_account_mapping,
    close_period,
    create_account,
    create_account_mapping,
    create_draft,
    discard_draft,
    post_draft,
    reactivate_account,
    reopen_period,
    reverse_entry,
    set_report_mapping,
    soft_close_period,
    update_account_metadata,
    update_draft,
)
from apps.accounting.validators import PostingLine
from apps.core.context import audit_context, get_correlation_id
from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.organizations.authorization import (
    OutOfScope,
    PermissionMissing,
    has_organization_permission,
    has_organization_scope,
    organization_scope,
    organizations_with_permission,
    require_branch_permission,
    require_organization_permission,
    require_reachable_organization_permission,
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
        raise OutOfScope(_("Account %(id)s does not exist.") % {"id": account_id})
    return account


def _scoped_cost_center(organization: Organization, cost_center_id: int) -> CostCenter:
    """A cost centre id, resolved inside one organization (ADR-015)."""
    cost_center = CostCenter.objects.filter(organization=organization, pk=cost_center_id).first()
    if cost_center is None:
        raise OutOfScope(_("Cost center %(id)s does not exist.") % {"id": cost_center_id})
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
            raise OutOfScope(_("Branch %(id)s does not exist.") % {"id": line.branch_id})
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
        # Same exception and same wording as the out-of-scope case below:
        # a caller must not be able to tell a missing entry from one that
        # belongs to another organization.
        raise OutOfScope(_("Journal entry %(id)s does not exist.") % {"id": entry_id})

    # Raises if the organization itself is out of reach, which covers the
    # cross-organization case before any branch is considered.
    resolve_organization(actor, entry.organization_id)

    if has_organization_scope(actor, entry.organization):
        return entry

    reachable = set(accessible_branch_ids(actor))
    touched = {line.branch_id for line in entry.lines.all()}
    if not touched or not touched.issubset(reachable):
        raise OutOfScope(_("Journal entry %(id)s does not exist.") % {"id": entry_id})
    return entry


def _resolve_period(actor: User, period_id: int) -> AccountingPeriod:
    """An accounting period id, resolved against the caller's organizations."""
    period = (
        AccountingPeriod.objects.filter(pk=period_id)
        .select_related("fiscal_year", "fiscal_year__organization")
        .first()
    )
    if period is None:
        raise OutOfScope(_("Accounting period %(id)s does not exist.") % {"id": period_id})
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

    A manual line landing on a `RESTRICTED` control account needs
    `post_restricted_manual_journal`. The permission is resolved **here** and
    handed to the kernel as a boolean: the kernel knows accounting, this layer
    knows who is allowed, and a service that reads permissions has two jobs.
    """
    entry = _resolve_entry(actor, entry_id)
    _require_at_every_branch(actor, POST_JOURNAL, entry)

    allow_restricted = has_organization_permission(
        actor, POST_RESTRICTED_MANUAL_JOURNAL, entry.organization
    )

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
        return post_draft(
            entry=entry,
            allow_closed_period=allow_closed_period,
            allow_restricted_accounts=allow_restricted,
        )


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
        raise PermissionMissing(_("%(permission)s is not held.") % {"permission": VIEW_JOURNAL})

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
# Chart-of-accounts commands (ADR-014, ADR-029 §7, ADR-031)
# ---------------------------------------------------------------------------
#
# The chart is organization structure: one branch must not reshape what the
# others post to. So every act here is organization-scoped, and the two halves
# come from different helpers on purpose.
#
# `manage_chart_of_accounts` and `manage_report_mappings` use
# `require_organization_permission` — an OrganizationMembership, the same bar
# `manage_account_mappings` and the period acts clear, because all four decide
# something every branch then lives with.
#
# `view_chart_of_accounts` uses `require_reachable_organization_permission`,
# which is weaker in scope and identical in provenance. Reading the chart is
# how an accountant codes a journal line, and this repository's accountants
# hold branch posts; demanding organization membership to *read* the chart
# would either lock them out of their own job or push deployments into granting
# organization membership widely — which would quietly hand out period-closing
# authority as a side effect (ADR-016).


def _resolve_account(actor: User, account_id: int) -> Account:
    """
    An account id, resolved against the caller's reach — foreign is a 404.

    Same shape and same wording as `_resolve_mapping`: an account in another
    organization must be indistinguishable from one that was never created,
    because account ids are sequential and a 403 would confirm it exists.
    """
    account = Account.objects.filter(pk=account_id).select_related("organization", "parent").first()
    if account is None:
        raise OutOfScope(_("Account %(id)s does not exist.") % {"id": account_id})
    resolve_organization(actor, account.organization_id)
    return account


def _resolve_report_mapping(actor: User, mapping_id: int) -> AccountReportMapping:
    """A statement classification id, resolved against the caller's reach."""
    mapping = (
        AccountReportMapping.objects.filter(pk=mapping_id)
        .select_related("organization", "account")
        .first()
    )
    if mapping is None:
        raise OutOfScope(_("Report mapping %(id)s does not exist.") % {"id": mapping_id})
    resolve_organization(actor, mapping.organization_id)
    return mapping


@transaction.atomic
def create_chart_account(
    *,
    actor: User,
    organization_id: int,
    code: str,
    name_ar: str,
    name_en: str,
    requires_cost_center: bool | None = None,
    manual_posting_policy: str = ManualPostingPolicy.ALLOWED,
    external_accounting_system: str = "",
    external_account_code: str = "",
) -> Account:
    """
    Add an account to one organization's chart.

    `is_system` is deliberately not a parameter. A system account is seeded
    reference data, and a user-creatable one would let an ordinary account
    claim the protection the seeded control accounts rely on — the same
    reasoning `AccountRole.is_system` records for the role vocabulary.
    """
    organization = resolve_organization(actor, organization_id)
    require_organization_permission(actor, MANAGE_CHART_OF_ACCOUNTS, organization)
    with _acting_as(actor):
        return create_account(
            organization=organization,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            requires_cost_center=requires_cost_center,
            manual_posting_policy=manual_posting_policy,
            external_accounting_system=external_accounting_system,
            external_account_code=external_account_code,
        )


@transaction.atomic
def update_chart_account(
    *,
    actor: User,
    account_id: int,
    name_ar: str,
    name_en: str,
    requires_cost_center: bool,
    manual_posting_policy: str,
    reason: str = "",
    allow_system: bool = False,
) -> Account:
    """
    Amend the metadata an account with journal history may safely change.

    `allow_system` is the one elevated path, and it is gated here rather than
    in the kernel because the kernel does not know who is asking. Changing the
    manual-posting policy of a seeded control account opens `2-01-01-001` to
    hand-written entries, which is exactly what `RESTRICTED` was seeded to
    prevent — so it needs `accounting.manage_accounts`, the structural
    authority from Task 0.7, and not merely the chart screen's own permission.
    The two happen to sit with the same roles today; they are separate entries
    so a deployment can widen the chart screen without widening this.
    """
    account = _resolve_account(actor, account_id)
    require_organization_permission(actor, MANAGE_CHART_OF_ACCOUNTS, account.organization)
    if allow_system:
        require_organization_permission(actor, MANAGE_ACCOUNTS, account.organization)
    with _acting_as(actor):
        return update_account_metadata(
            account=account,
            name_ar=name_ar,
            name_en=name_en,
            requires_cost_center=requires_cost_center,
            manual_posting_policy=manual_posting_policy,
            reason=reason,
            allow_system=allow_system,
        )


@transaction.atomic
def archive_chart_account(*, actor: User, account_id: int, reason: str = "") -> Account:
    """Withdraw an account from use. Never a delete — the code stays reserved."""
    account = _resolve_account(actor, account_id)
    require_organization_permission(actor, MANAGE_CHART_OF_ACCOUNTS, account.organization)
    with _acting_as(actor):
        return archive_account(account=account, reason=reason)


@transaction.atomic
def reactivate_chart_account(*, actor: User, account_id: int, reason: str = "") -> Account:
    """Bring an archived account back into use."""
    account = _resolve_account(actor, account_id)
    require_organization_permission(actor, MANAGE_CHART_OF_ACCOUNTS, account.organization)
    with _acting_as(actor):
        return reactivate_account(account=account, reason=reason)


@transaction.atomic
def set_account_report_mapping(
    *,
    actor: User,
    organization_id: int,
    account_id: int,
    statement_group: str,
    presentation_section: str = PresentationSection.NOT_APPLICABLE,
    display_order: int = 0,
) -> AccountReportMapping:
    """
    Classify one account for the financial statements.

    The account is resolved inside the named organization rather than fetched
    and compared afterwards, so an id from elsewhere selects nothing at all —
    a classification is the one place a foreign account id would be silently
    plausible, because the statement would still balance without it.
    """
    organization = resolve_organization(actor, organization_id)
    require_organization_permission(actor, MANAGE_REPORT_MAPPINGS, organization)
    account = _scoped_account(organization, account_id)
    with _acting_as(actor):
        return set_report_mapping(
            organization=organization,
            account=account,
            statement_group=statement_group,
            presentation_section=presentation_section,
            display_order=display_order,
        )


@transaction.atomic
def clear_account_report_mapping(
    *, actor: User, mapping_id: int, reason: str = ""
) -> AccountReportMapping:
    """Withdraw a classification. The account becomes unmapped, visibly."""
    mapping = _resolve_report_mapping(actor, mapping_id)
    require_organization_permission(actor, MANAGE_REPORT_MAPPINGS, mapping.organization)
    with _acting_as(actor):
        return clear_report_mapping(mapping=mapping, reason=reason)


def list_chart_accounts(
    *,
    actor: User,
    organization_id: int | None = None,
    include_archived: bool = False,
) -> QuerySet[Account]:
    """
    The accounts this caller may see, filtered in SQL rather than in Python.

    Filtered, not checked-then-listed, for the reason `list_journal_entries`
    records: an endpoint that fetched everything and dropped the rows it should
    not show would still have loaded them, and the first aggregate written
    against that queryset would report figures the caller is not entitled to.
    """
    organizations = organizations_with_permission(actor, VIEW_CHART_OF_ACCOUNTS)
    accounts = Account.objects.filter(organization__in=organizations)
    if organization_id is not None:
        organization = resolve_organization(actor, organization_id)
        require_reachable_organization_permission(actor, VIEW_CHART_OF_ACCOUNTS, organization)
        accounts = accounts.filter(organization=organization)
    if not include_archived:
        accounts = accounts.filter(is_active=True)
    return accounts.select_related("organization", "parent").order_by("organization__code", "code")


def read_chart_account(*, actor: User, account_id: int) -> Account:
    """One account, if the caller may see the chart it belongs to."""
    account = _resolve_account(actor, account_id)
    require_reachable_organization_permission(actor, VIEW_CHART_OF_ACCOUNTS, account.organization)
    return account


# ---------------------------------------------------------------------------
# Account-mapping commands (ADR-019)
# ---------------------------------------------------------------------------
#
# `accounting.manage_account_mappings` is ORGANIZATION authority: which
# account carries a role decides where every branch's postings land, so it is
# held through an OrganizationMembership, never assembled from branch posts,
# and never satisfied by a global Django grant alone.


def list_account_roles(*, actor: User) -> QuerySet[AccountRole]:
    """The system vocabulary. Global, read-only, and visible to any signed-in user."""
    if not actor.is_authenticated or not actor.is_active:
        return AccountRole.objects.none()
    return AccountRole.objects.order_by("domain", "code")


def list_account_mappings(
    *, actor: User, organization_id: int
) -> QuerySet[OrganizationAccountMapping]:
    """One organization's default mappings, for a caller who reaches it."""
    organization = resolve_organization(actor, organization_id)
    return (
        OrganizationAccountMapping.objects.filter(organization=organization)
        .select_related("account_role", "account", "organization")
        .order_by("account_role__code", "-version")
    )


def _resolve_mapping(actor: User, mapping_id: int) -> OrganizationAccountMapping:
    """A mapping id, resolved against the caller's reach — foreign is a 404."""
    mapping = (
        OrganizationAccountMapping.objects.filter(pk=mapping_id)
        .select_related("organization", "account_role", "account")
        .first()
    )
    if mapping is None:
        raise OutOfScope(_("Account mapping %(id)s does not exist.") % {"id": mapping_id})
    resolve_organization(actor, mapping.organization_id)
    return mapping


def _resolve_role(role_id: int) -> AccountRole:
    role = AccountRole.objects.filter(pk=role_id).first()
    if role is None:
        raise OutOfScope(_("Account role %(id)s does not exist.") % {"id": role_id})
    return role


@transaction.atomic
def map_account_role(
    *,
    actor: User,
    organization_id: int,
    account_role_id: int,
    account_id: int,
    effective_from: datetime.date,
    effective_to: datetime.date | None = None,
) -> OrganizationAccountMapping:
    """Create an organization default mapping, with organization authority."""
    organization = resolve_organization(actor, organization_id)
    require_organization_permission(actor, MANAGE_ACCOUNT_MAPPINGS, organization)
    role = _resolve_role(account_role_id)
    account = _scoped_account(organization, account_id)
    with _acting_as(actor):
        return create_account_mapping(
            organization=organization,
            account_role=role,
            account=account,
            effective_from=effective_from,
            effective_to=effective_to,
        )


@transaction.atomic
def amend_account_role_mapping(
    *,
    actor: User,
    mapping_id: int,
    account_id: int | None = None,
    effective_from: datetime.date | None = None,
    effective_to: datetime.date | None = None,
    clear_effective_to: bool = False,
) -> OrganizationAccountMapping:
    """Correct an unused mapping."""
    mapping = _resolve_mapping(actor, mapping_id)
    require_organization_permission(actor, MANAGE_ACCOUNT_MAPPINGS, mapping.organization)
    account = _scoped_account(mapping.organization, account_id) if account_id is not None else None
    with _acting_as(actor):
        return amend_account_mapping(
            mapping=mapping,
            account=account,
            effective_from=effective_from,
            effective_to=effective_to,
            clear_effective_to=clear_effective_to,
        )


@transaction.atomic
def close_account_role_mapping(
    *, actor: User, mapping_id: int, effective_to: datetime.date, reason: str = ""
) -> OrganizationAccountMapping:
    """End a mapping's effective range — the correction path for a used one."""
    mapping = _resolve_mapping(actor, mapping_id)
    require_organization_permission(actor, MANAGE_ACCOUNT_MAPPINGS, mapping.organization)
    with _acting_as(actor):
        return close_account_mapping(mapping=mapping, effective_to=effective_to, reason=reason)


@transaction.atomic
def archive_account_role_mapping(
    *, actor: User, mapping_id: int, reason: str = ""
) -> OrganizationAccountMapping:
    """Withdraw an unused mapping recorded in error."""
    mapping = _resolve_mapping(actor, mapping_id)
    require_organization_permission(actor, MANAGE_ACCOUNT_MAPPINGS, mapping.organization)
    with _acting_as(actor):
        return archive_account_mapping(mapping=mapping, reason=reason)


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
