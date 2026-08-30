"""Accounting reads. Nothing here writes."""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, DecimalField, Max, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce

from apps.accounting.models import (
    Account,
    AccountReportMapping,
    AccountRole,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
    OrganizationAccountMapping,
)
from apps.core.money import MONEY_PLACES
from apps.organizations.models import Branch, Organization

_ZERO = Value(Decimal("0"), output_field=DecimalField(max_digits=30, decimal_places=MONEY_PLACES))


def posted_lines(*, organization: Organization) -> QuerySet[JournalLine]:
    """
    Every line that counts.

    Only POSTED and REVERSED entries: a reversal is itself posted, and its
    original stays in the ledger, so both belong in a balance. Drafts do not.
    """
    return JournalLine.objects.filter(
        entry__organization=organization,
        entry__status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
    )


def account_balance(
    *,
    account: Account,
    branch: Branch | None = None,
    cost_center: CostCenter | None = None,
    up_to: datetime.date | None = None,
) -> Decimal:
    """
    Debits minus credits for one account, derived from the ledger.

    Never a stored number. A balance that can drift from its own movements is
    the failure this architecture exists to prevent.
    """
    lines = posted_lines(organization=account.organization).filter(account=account)
    if branch is not None:
        lines = lines.filter(branch=branch)
    if cost_center is not None:
        lines = lines.filter(cost_center=cost_center)
    if up_to is not None:
        lines = lines.filter(entry__accounting_date__lte=up_to)

    totals = lines.aggregate(
        debits=Coalesce(Sum("debit"), _ZERO), credits=Coalesce(Sum("credit"), _ZERO)
    )
    balance: Decimal = totals["debits"] - totals["credits"]
    return balance


def account_balances(
    *,
    organization: Organization,
    up_to: datetime.date | None = None,
    branch: Branch | None = None,
    cost_center: CostCenter | None = None,
    accounts: Iterable[Account] | None = None,
) -> dict[int, Decimal]:
    """
    Debits minus credits for every account with a movement, in **one** query.

    The plural of `account_balance`, and not a convenience: every report Phase 5
    adds — trial balance, income statement, balance sheet, the unmapped-account
    check — needs the balance of every account at once. Looping
    `account_balance` over the chart is one round trip per account, so a
    seventy-account chart is seventy queries for a page that has to render in
    one, and the cost grows with the chart rather than with the ledger.

    Accounts with no posted line are simply absent rather than present with a
    zero — a caller asking "which accounts moved" must not have to filter the
    answer, and a caller asking for a specific account's balance should use
    `.get(account_id, Decimal("0"))`, which says what an absent key means.
    """
    lines = posted_lines(organization=organization)
    if up_to is not None:
        lines = lines.filter(entry__accounting_date__lte=up_to)
    if branch is not None:
        lines = lines.filter(branch=branch)
    if cost_center is not None:
        lines = lines.filter(cost_center=cost_center)
    if accounts is not None:
        lines = lines.filter(account__in=[account.pk for account in accounts])

    rows = lines.values("account_id").annotate(
        debits=Coalesce(Sum("debit"), _ZERO), credits=Coalesce(Sum("credit"), _ZERO)
    )
    return {row["account_id"]: row["debits"] - row["credits"] for row in rows}


@dataclass
class ChartNode:
    """One account and the accounts filed under it."""

    account: Account
    children: list[ChartNode] = field(default_factory=list)


def chart_tree(*, organization: Organization, include_archived: bool = False) -> list[ChartNode]:
    """
    The chart as a hierarchy, from one query, assembled in Python.

    Assembled by **code prefix** rather than by walking `parent`, because the
    code is what carries the level (ADR-014) and a recursive walk would be one
    query per level per node for a tree that is only ever four deep. The parent
    foreign key and the code agree — `create_account` derives one from the
    other — so either would give the same shape; the prefix gives it without
    asking the database again.

    An account whose parent is filtered out — an active leaf under an archived
    group, which no constraint forbids — is returned as a root rather than
    dropped. A balance that vanished from a chart page because of a filter
    applied to a *different* row is exactly the silent omission ADR-031 §2 is
    about.
    """
    accounts = Account.objects.filter(organization=organization)
    if not include_archived:
        accounts = accounts.filter(is_active=True)

    nodes = {account.code: ChartNode(account=account) for account in accounts.order_by("code")}

    roots: list[ChartNode] = []
    for code, node in nodes.items():
        parent_code = code.rsplit("-", 1)[0] if "-" in code else None
        parent = nodes.get(parent_code) if parent_code is not None else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
    return roots


def report_mapping_for(*, organization: Organization) -> dict[int, AccountReportMapping]:
    """
    The organization's active statement classifications, keyed by account id.

    Active only. A cleared mapping is kept so last year's statement stays
    explicable, but it classifies nothing today, and a statement that read it
    would file a balance under a grouping somebody had explicitly withdrawn.
    """
    return {
        mapping.account_id: mapping
        for mapping in AccountReportMapping.objects.filter(
            organization=organization, is_active=True
        ).select_related("account")
    }


def trial_balance(
    *, organization: Organization, up_to: datetime.date | None = None
) -> list[dict[str, object]]:
    """
    Every postable account with a movement, and its debit and credit totals.

    The smoke test for the whole kernel: the two columns must be equal, and
    they can only be equal if every entry balanced.
    """
    lines = posted_lines(organization=organization)
    if up_to is not None:
        lines = lines.filter(entry__accounting_date__lte=up_to)

    rows = (
        lines.values("account__code", "account__name")
        .annotate(debits=Coalesce(Sum("debit"), _ZERO), credits=Coalesce(Sum("credit"), _ZERO))
        .order_by("account__code")
    )
    return [
        {
            "code": row["account__code"],
            "name": row["account__name"],
            "debits": row["debits"],
            "credits": row["credits"],
            "balance": row["debits"] - row["credits"],
        }
        for row in rows
    ]


def trial_balance_totals(
    *, organization: Organization, up_to: datetime.date | None = None
) -> tuple[Decimal, Decimal]:
    """Total debits and total credits. They must be equal."""
    lines = posted_lines(organization=organization)
    if up_to is not None:
        lines = lines.filter(entry__accounting_date__lte=up_to)
    totals = lines.aggregate(
        debits=Coalesce(Sum("debit"), _ZERO), credits=Coalesce(Sum("credit"), _ZERO)
    )
    return totals["debits"], totals["credits"]


def entry_by_idempotency_key(*, organization: Organization, key: str) -> JournalEntry | None:
    """
    The entry a key produced, within one organization.

    The organization is required, not optional. A lookup on the key alone
    would return another organization's journal to anyone who guessed their
    key — and keys are frequently predictable, because upstream modules build
    them from document numbers.
    """
    return JournalEntry.objects.filter(organization=organization, idempotency_key=key).first()


# ---------------------------------------------------------------------------
# Role usage — what الأدوار المحاسبية needs to show
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleUsage:
    """One role, and the state of its mappings across the organizations in view."""

    role: AccountRole
    active_mappings: int
    organizations_used: int
    last_effective_from: datetime.date | None
    #: Organizations in scope with no mapping in effect on the as-of date. The
    #: warning that matters: a posting service resolving this role in one of
    #: them fails at the moment somebody tries to post, not now.
    unresolved: list[Organization]
    #: Posted journal lines that landed on an account this role currently maps
    #: to. Evidence that the role is genuinely in use, as opposed to merely
    #: configured.
    posted_lines: int

    @property
    def is_mapped(self) -> bool:
        return self.active_mappings > 0


def role_usage(
    *, organizations: Iterable[Organization], on_date: datetime.date | None = None
) -> list[RoleUsage]:
    """
    Every active role, with how it is mapped across the given organizations.

    Assembled from three bulk queries rather than one query per role: a role
    list is forty rows and each of the four facts below would otherwise be its
    own round trip.

    `unresolved` is computed by difference — organizations in view minus
    organizations with a mapping in effect — rather than by asking each
    organization whether it has one. An organization that has *never* mapped a
    role has no row to find, and a query built from the mapping table alone
    would silently report every role as fully mapped.
    """
    as_of = on_date or datetime.date.today()
    organization_list = list(organizations)
    organization_ids = [organization.pk for organization in organization_list]

    effective = OrganizationAccountMapping.objects.filter(
        organization_id__in=organization_ids,
        is_active=True,
        effective_from__lte=as_of,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=as_of))

    by_role: dict[int, list[OrganizationAccountMapping]] = {}
    for mapping in effective.select_related("account", "organization"):
        by_role.setdefault(mapping.account_role_id, []).append(mapping)

    latest: dict[int, datetime.date] = {}
    for row in (
        OrganizationAccountMapping.objects.filter(organization_id__in=organization_ids)
        .values("account_role_id")
        .annotate(last=Max("effective_from"))
    ):
        latest[row["account_role_id"]] = row["last"]

    mapped_account_ids = {
        mapping.account_id for mappings in by_role.values() for mapping in mappings
    }
    line_counts: dict[int, int] = {}
    if mapped_account_ids:
        counted = (
            JournalLine.objects.filter(
                account_id__in=mapped_account_ids,
                entry__organization_id__in=organization_ids,
                entry__status__in=[JournalEntryStatus.POSTED, JournalEntryStatus.REVERSED],
            )
            .values("account_id")
            .annotate(total=Count("id"))
        )
        for counted_row in counted:
            line_counts[counted_row["account_id"]] = counted_row["total"]

    usage: list[RoleUsage] = []
    for role in AccountRole.objects.filter(is_active=True).order_by("domain", "code"):
        mappings = by_role.get(role.pk, [])
        covered = {mapping.organization_id for mapping in mappings}
        usage.append(
            RoleUsage(
                role=role,
                active_mappings=len(mappings),
                organizations_used=len(covered),
                last_effective_from=latest.get(role.pk),
                unresolved=[
                    organization
                    for organization in organization_list
                    if organization.pk not in covered
                ],
                posted_lines=sum(line_counts.get(mapping.account_id, 0) for mapping in mappings),
            )
        )
    return usage


def mapping_history(
    *, organization: Organization, role: AccountRole
) -> QuerySet[OrganizationAccountMapping]:
    """Every version of one role's mapping in one organization, oldest first."""
    return (
        OrganizationAccountMapping.objects.filter(organization=organization, account_role=role)
        .select_related("account")
        .order_by("version")
    )


@dataclass(frozen=True)
class ContinuityGap:
    """A stretch of dates in which a role resolves to nothing."""

    organization: Organization
    role: AccountRole
    starts: datetime.date
    ends: datetime.date | None


def mapping_continuity_gaps(*, organization: Organization) -> list[ContinuityGap]:
    """
    Roles whose mapping versions leave a hole between them.

    The overlap constraint stops two versions covering the same day; nothing
    stops a *gap*, and a gap is the more dangerous of the two: an overlap is
    refused at write time and visible immediately, while a gap is discovered
    when a posting dated inside it fails months later.

    Reads the versions in order and reports each discontinuity, rather than
    only whether the role is covered today — a hole in March is still a hole
    after April closes it.
    """
    gaps: list[ContinuityGap] = []
    rows = (
        OrganizationAccountMapping.objects.filter(organization=organization, is_active=True)
        .select_related("account_role")
        .order_by("account_role__code", "effective_from")
    )
    previous_role_id: int | None = None
    previous_end: datetime.date | None = None
    for mapping in rows:
        if mapping.account_role_id != previous_role_id:
            previous_role_id = mapping.account_role_id
            previous_end = mapping.effective_to
            continue
        if previous_end is None:
            # An open-ended version followed by another would have been refused
            # by the overlap constraint, so this branch means the data changed
            # underneath us. Nothing to report; the next row sets the cursor.
            previous_end = mapping.effective_to
            continue
        expected = previous_end + datetime.timedelta(days=1)
        if mapping.effective_from > expected:
            gaps.append(
                ContinuityGap(
                    organization=organization,
                    role=mapping.account_role,
                    starts=expected,
                    ends=mapping.effective_from - datetime.timedelta(days=1),
                )
            )
        previous_end = mapping.effective_to
    return gaps
