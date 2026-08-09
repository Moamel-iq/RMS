"""
Accounting kernel commands.

`post_entry` is the only way money enters the ledger. Nothing else writes a
JournalEntry or JournalLine — not a view, not a signal, not a later module.
Purchases, sales, payroll, and inventory will build a posting request and hand
it here.
"""

from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    CLASSES_REQUIRING_COST_CENTER,
    DETAIL_CODE_PATTERN,
    Account,
    AccountClass,
    AccountingPeriod,
    AccountingSettings,
    AccountRole,
    CostCenter,
    FiscalYear,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
    JournalNumberSequence,
    OrganizationAccountMapping,
    PeriodState,
    SourceEvent,
)
from apps.accounting.validators import (
    PostingLine,
    validate_account_parentage,
    validate_accounts_are_postable,
    validate_balanced,
    validate_both_sides_present,
    validate_branches_are_active,
    validate_cost_centers,
    validate_entry_has_value,
    validate_line_sides,
    validate_lines_present,
    validate_organization_consistency,
    validate_parent_has_no_posting_history,
    validate_period_accepts_postings,
    validate_source_identity,
)
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.services import record_audit_event, snapshot
from apps.core.source_identity import canonical_source_identity
from apps.organizations.models import Organization
from apps.users.models import User

_DETAIL_CODE_RE = re.compile(DETAIL_CODE_PATTERN)


# ---------------------------------------------------------------------------
# 1. Organization accounting settings
# ---------------------------------------------------------------------------


@transaction.atomic
def configure_accounting(
    *, organization: Organization, fiscal_year_start_month: int = 1
) -> AccountingSettings:
    """
    Set up an organization's accounting configuration.

    Refuses to change the fiscal year start once any entry has been posted:
    that would re-bucket every posted entry into a different period, silently
    restating every report ever produced (ADR-013).
    """
    existing = AccountingSettings.objects.filter(organization=organization).first()

    if existing is None:
        settings_row = AccountingSettings(
            organization=organization, fiscal_year_start_month=fiscal_year_start_month
        )
        settings_row.full_clean()
        settings_row.save()
        record_audit_event(
            action=AuditAction.CREATED, target=settings_row, new_state=snapshot(settings_row)
        )
        return settings_row

    if existing.fiscal_year_start_month == fiscal_year_start_month:
        return existing

    if JournalEntry.objects.filter(
        organization=organization, status=JournalEntryStatus.POSTED
    ).exists():
        raise ValidationError(
            _(
                "The fiscal year start cannot be changed once entries are posted. "
                "It would re-bucket every posted entry."
            ),
            code="fiscal_year_locked",
        )

    before = snapshot(AccountingSettings.objects.get(pk=existing.pk))
    existing.fiscal_year_start_month = fiscal_year_start_month
    existing.full_clean()
    existing.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=existing,
        previous_state=before,
        new_state=snapshot(existing),
    )
    return existing


# ---------------------------------------------------------------------------
# 2. Fiscal years and periods
# ---------------------------------------------------------------------------


def _month_span(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, 1), datetime.date(year, month, last_day)


@transaction.atomic
def open_fiscal_year(*, organization: Organization, year: int) -> FiscalYear:
    """
    Create a fiscal year and its twelve monthly periods (ADR-013).

    Twelve, never thirteen. Year-end adjustments post on the final date with
    `is_adjustment=True`; a thirteenth period would be a second calendar every
    report then has to decide whether to include.
    """
    settings_row, _created = AccountingSettings.objects.get_or_create(organization=organization)
    start_month = settings_row.fiscal_year_start_month

    first_start, _ = _month_span(year, start_month)
    months = [
        ((year + (start_month - 1 + offset) // 12), (start_month - 1 + offset) % 12 + 1)
        for offset in range(12)
    ]
    _, last_end = _month_span(*months[-1])

    fiscal_year = FiscalYear(
        organization=organization, year=year, start_date=first_start, end_date=last_end
    )
    fiscal_year.full_clean()
    fiscal_year.save()

    for number, (period_year, period_month) in enumerate(months, start=1):
        period_start, period_end = _month_span(period_year, period_month)
        AccountingPeriod.objects.create(
            fiscal_year=fiscal_year,
            period_number=number,
            start_date=period_start,
            end_date=period_end,
        )

    record_audit_event(
        action=AuditAction.CREATED, target=fiscal_year, new_state=snapshot(fiscal_year)
    )
    return fiscal_year


def resolve_period(
    *, organization: Organization, accounting_date: datetime.date
) -> AccountingPeriod:
    """Find the period an accounting date falls in, or say so clearly."""
    period = AccountingPeriod.objects.filter(
        fiscal_year__organization=organization,
        start_date__lte=accounting_date,
        end_date__gte=accounting_date,
    ).first()
    if period is None:
        raise ValidationError(
            _("No accounting period covers %(date)s. Open the fiscal year first."),
            code="no_period",
            params={"date": accounting_date.isoformat()},
        )
    return period


def _change_period_state(
    *, period: AccountingPeriod, new_state: str, action: str, reason: str
) -> AccountingPeriod:
    with transaction.atomic():
        # Locked: two concurrent closes, or a close racing a posting, must not
        # interleave into a state nobody chose.
        locked = AccountingPeriod.objects.select_for_update().get(pk=period.pk)
        before = snapshot(locked)
        locked.state = new_state
        locked.full_clean()
        locked.save(update_fields=["state", "updated_at"])
        record_audit_event(
            action=action,
            target=locked,
            previous_state=before,
            new_state=snapshot(locked),
            reason=reason,
        )
    period.refresh_from_db()
    return period


def _validate_close_order(period: AccountingPeriod) -> None:
    """
    A period cannot close while an earlier one in the same year is still open.

    Closing out of order would let February be sealed while January is still
    accepting entries, so January's closing figures — and every balance
    carried forward from them — could still change afterwards.
    """
    earlier_open = (
        AccountingPeriod.objects.filter(
            fiscal_year=period.fiscal_year, period_number__lt=period.period_number
        )
        .exclude(state=PeriodState.CLOSED)
        .order_by("period_number")
        .first()
    )
    if earlier_open is not None:
        raise ValidationError(
            _("Period %(earlier)s must be closed before %(period)s."),
            code="close_out_of_order",
            params={"earlier": str(earlier_open), "period": str(period)},
        )


def _validate_reopen_order(period: AccountingPeriod) -> None:
    """
    A period cannot reopen while a later one is still closed.

    Reverse chronology, and for the same reason: reopening January under a
    sealed February would let January change beneath figures February has
    already carried forward.
    """
    later_closed = (
        AccountingPeriod.objects.filter(
            fiscal_year=period.fiscal_year,
            period_number__gt=period.period_number,
            state=PeriodState.CLOSED,
        )
        .order_by("-period_number")
        .first()
    )
    if later_closed is not None:
        raise ValidationError(
            _("Period %(later)s must be reopened before %(period)s."),
            code="reopen_out_of_order",
            params={"later": str(later_closed), "period": str(period)},
        )


def soft_close_period(*, period: AccountingPeriod, reason: str = "") -> AccountingPeriod:
    """
    Stop routine posting. Specifically-authorized adjustments and reversals
    still pass; that authorization arrives with the Task 0.7 permissions.

    Not order-constrained: a soft close is reversible and carries no figures
    forward, so sealing March before February is merely unusual, not unsound.
    """
    return _change_period_state(
        period=period,
        new_state=PeriodState.SOFT_CLOSED,
        action=AuditAction.PERIOD_CLOSED,
        reason=reason or "soft close",
    )


def close_period(*, period: AccountingPeriod, reason: str = "") -> AccountingPeriod:
    """Close a period. Nothing posts into it without an authorized reopening."""
    _validate_close_order(period)
    return _change_period_state(
        period=period,
        new_state=PeriodState.CLOSED,
        action=AuditAction.PERIOD_CLOSED,
        reason=reason or "close",
    )


def reopen_period(*, period: AccountingPeriod, reason: str) -> AccountingPeriod:
    """
    Reopen a closed period.

    A reason is mandatory. Reopening a closed period is the single most
    sensitive act in the ledger — it lets history be added to after the fact —
    so the audit event must say why, not merely that it happened.
    """
    if not reason.strip():
        raise ValidationError(
            _("Reopening a period requires a reason."),
            code="reopen_reason_required",
        )
    _validate_reopen_order(period)
    return _change_period_state(
        period=period,
        new_state=PeriodState.OPEN,
        action=AuditAction.PERIOD_REOPENED,
        reason=reason.strip(),
    )


# ---------------------------------------------------------------------------
# 3. Chart of accounts
# ---------------------------------------------------------------------------


def default_requires_cost_center(account_class: str) -> bool:
    """Revenue, COGS, and operating expenses; nothing on the balance sheet."""
    return account_class in CLASSES_REQUIRING_COST_CENTER


@transaction.atomic
def create_account(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    name_en: str,
    requires_cost_center: bool | None = None,
    external_accounting_system: str = "",
    external_account_code: str = "",
) -> Account:
    """
    Add an account. Class and parent are derived from the code (ADR-014).

    Deriving them means the code, the class, and the position in the tree can
    never disagree with each other.
    """
    code = code.strip()
    account_class = code[0]
    if account_class not in AccountClass.values:
        raise ValidationError(
            _("%(code)s does not start with a known account class."),
            code="unknown_account_class",
            params={"code": code},
        )

    is_postable = bool(_DETAIL_CODE_RE.match(code))

    parent = None
    if "-" in code:
        parent_code = code.rsplit("-", 1)[0]
        parent = Account.objects.filter(organization=organization, code=parent_code).first()
        if parent is None:
            raise ValidationError(
                _("Parent account %(parent)s does not exist."),
                code="missing_parent",
                params={"parent": parent_code},
            )
        validate_parent_has_no_posting_history(parent)

    if requires_cost_center is None:
        requires_cost_center = is_postable and default_requires_cost_center(account_class)

    account = Account(
        organization=organization,
        code=code,
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        account_class=account_class,
        parent=parent,
        is_postable=is_postable,
        requires_cost_center=requires_cost_center,
        external_accounting_system=external_accounting_system.strip(),
        external_account_code=external_account_code.strip(),
    )
    account.full_clean()
    validate_account_parentage(account)
    account.save()
    record_audit_event(action=AuditAction.CREATED, target=account, new_state=snapshot(account))
    return account


@transaction.atomic
def archive_account(*, account: Account, reason: str = "") -> Account:
    """
    Take an account out of use without deleting it.

    Deletion is refused by PROTECT wherever a posted line references it, and
    would destroy the trail even where it is not. Archiving keeps the code
    reserved, so it can never be reissued to mean something else.
    """
    before = snapshot(Account.objects.get(pk=account.pk))
    account.is_active = False
    account.save(update_fields=["is_active", "updated_at"])
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=account,
        previous_state=before,
        new_state=snapshot(account),
        reason=reason,
    )
    return account


@transaction.atomic
def create_cost_center(
    *, organization: Organization, code: str, name_ar: str, name_en: str
) -> CostCenter:
    cost_center = CostCenter(
        organization=organization,
        code=code.strip().upper(),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
    )
    cost_center.full_clean()
    cost_center.save()
    record_audit_event(
        action=AuditAction.CREATED, target=cost_center, new_state=snapshot(cost_center)
    )
    return cost_center


# ---------------------------------------------------------------------------
# 3b. Account role mappings (ADR-019)
# ---------------------------------------------------------------------------
#
# An organization decides which of its accounts carries each system role.
# Posting services resolve the role; they never hold an account id or code.
#
# A mapping that has been referenced by a posted document is immutable except
# for closing its effective range — the correction is a new version. That is
# what keeps "which account did this movement post to" answerable after the
# chart changes.


def sync_system_account_roles() -> None:
    """
    Assert the system role vocabulary exists, idempotently.

    Seeded by migration 0008 for real databases, AND re-asserted on every
    `post_migrate` — because a test-suite flush (any `transaction=True` test)
    truncates data-migration rows and replays only post_migrate handlers.
    Reference data the application cannot run without has to survive that,
    exactly as the role groups do.
    """
    from apps.accounting.models import SYSTEM_INVENTORY_ROLES, AccountRole, AccountRoleDomain

    for code, name_ar, name_en, mapping_scope in SYSTEM_INVENTORY_ROLES:
        AccountRole.objects.update_or_create(
            code=code,
            defaults={
                "name_ar": name_ar,
                "name_en": name_en,
                "domain": AccountRoleDomain.INVENTORY,
                "mapping_scope": mapping_scope,
                "is_system": True,
                "is_active": True,
            },
        )


#: A guard takes the organization and role about to change and returns a
#: verifier to run after the change, still inside the transaction. Domains
#: register one at app-ready — inventory's refuses an `INVENTORY_CONTROL`
#: change that would silently re-home standing stock value — and accounting
#: never has to know what an item is. Raising from the verifier rolls the
#: whole mutation back.
MappingGuard = Callable[[Organization, AccountRole], Callable[[], None]]

_MAPPING_GUARDS: list[MappingGuard] = []


def register_mapping_guard(guard: MappingGuard) -> None:
    """Register a domain veto over organization mapping changes."""
    if guard not in _MAPPING_GUARDS:
        _MAPPING_GUARDS.append(guard)


def _guard_verifiers(
    organization: Organization, account_role: AccountRole
) -> list[Callable[[], None]]:
    return [guard(organization, account_role) for guard in _MAPPING_GUARDS]


def mapping_is_used(mapping: models.Model) -> bool:
    """
    Whether any domain row references this mapping.

    Walked generically over the reverse relations rather than by naming them:
    the referencing rows live in the domains that own them (inventory today,
    purchases and payroll later), and accounting must not import any of them.
    A relation this model does not know about yet is still found. Generic in
    its parameter for the same reason — the inventory override table applies
    the identical used-means-immutable rule to itself.
    """
    for relation in mapping._meta.related_objects:
        accessor = relation.get_accessor_name()
        if accessor and getattr(mapping, accessor).exists():
            return True
    return False


def _validate_mapping_account(*, organization: Organization, account: Account) -> None:
    if account.organization_id != organization.pk:
        raise ValidationError(
            _("Account %(code)s belongs to another organization."),
            code="account_organization_mismatch",
            params={"code": account.code},
        )
    if not account.is_postable:
        raise ValidationError(
            _("Account %(code)s is a group account and cannot carry a role mapping."),
            code="account_not_postable",
            params={"code": account.code},
        )
    if not account.is_active:
        raise ValidationError(
            _("Account %(code)s is archived."),
            code="account_inactive",
            params={"code": account.code},
        )


def _validate_no_mapping_overlap(
    *,
    organization: Organization,
    account_role: AccountRole,
    effective_from: datetime.date,
    effective_to: datetime.date | None,
    exclude_pk: int | None = None,
) -> None:
    """
    The readable half of the overlap rule; the EXCLUDE constraint is the
    durable half. Two mappings answering "which account carries this role
    today" is not a question to resolve at posting time.
    """
    existing = OrganizationAccountMapping.objects.filter(
        organization=organization, account_role=account_role, is_active=True
    )
    if exclude_pk is not None:
        existing = existing.exclude(pk=exclude_pk)
    for row in existing:
        starts_before_existing_ends = row.effective_to is None or effective_from <= row.effective_to
        ends_after_existing_starts = effective_to is None or effective_to >= row.effective_from
        if starts_before_existing_ends and ends_after_existing_starts:
            raise ValidationError(
                _("%(role)s already maps to %(account)s between %(from)s and %(to)s."),
                code="mapping_period_overlaps",
                params={
                    "role": account_role.code,
                    "account": row.account.code,
                    "from": row.effective_from.isoformat(),
                    "to": row.effective_to.isoformat() if row.effective_to else _("مفتوح"),
                },
            )


@transaction.atomic
def create_account_mapping(
    *,
    organization: Organization,
    account_role: AccountRole,
    account: Account,
    effective_from: datetime.date,
    effective_to: datetime.date | None = None,
) -> OrganizationAccountMapping:
    """Map a role to one of the organization's postable accounts, from a date."""
    if not account_role.is_active:
        raise ValidationError(
            _("Role %(code)s is not active."),
            code="account_role_inactive",
            params={"code": account_role.code},
        )
    _validate_mapping_account(organization=organization, account=account)
    if effective_to is not None and effective_to < effective_from:
        raise ValidationError(
            _("The effective range ends before it starts."), code="period_inverted"
        )
    _validate_no_mapping_overlap(
        organization=organization,
        account_role=account_role,
        effective_from=effective_from,
        effective_to=effective_to,
    )

    # The version counter is read under lock so two concurrent creates cannot
    # both claim v3. Locking the existing rows is enough: the unique
    # constraint on (organization, role, version) is the backstop.
    latest = (
        OrganizationAccountMapping.objects.select_for_update()
        .filter(organization=organization, account_role=account_role)
        .order_by("-version")
        .first()
    )
    verifiers = _guard_verifiers(organization, account_role)
    mapping = OrganizationAccountMapping(
        organization=organization,
        account_role=account_role,
        account=account,
        effective_from=effective_from,
        effective_to=effective_to,
        version=(latest.version + 1) if latest is not None else 1,
    )
    mapping.full_clean()
    mapping.save()
    for verify in verifiers:
        verify()
    record_audit_event(action=AuditAction.CREATED, target=mapping, new_state=snapshot(mapping))
    return mapping


@transaction.atomic
def amend_account_mapping(
    *,
    mapping: OrganizationAccountMapping,
    account: Account | None = None,
    effective_from: datetime.date | None = None,
    effective_to: datetime.date | None = None,
    clear_effective_to: bool = False,
) -> OrganizationAccountMapping:
    """
    Correct a mapping nothing has posted through yet.

    A used mapping is history: postings snapshotted it, and editing it would
    restate what they meant. The correction for one is `close_account_mapping`
    followed by a new version.
    """
    locked = OrganizationAccountMapping.objects.select_for_update().get(pk=mapping.pk)
    if mapping_is_used(locked):
        raise ValidationError(
            _("Mapping v%(version)s has been used by postings. Close it and create a new version."),
            code="mapping_in_use",
            params={"version": locked.version},
        )
    before = snapshot(locked)

    if account is not None:
        _validate_mapping_account(organization=locked.organization, account=account)
        locked.account = account
    if effective_from is not None:
        locked.effective_from = effective_from
    if clear_effective_to:
        locked.effective_to = None
    elif effective_to is not None:
        locked.effective_to = effective_to

    if locked.effective_to is not None and locked.effective_to < locked.effective_from:
        raise ValidationError(
            _("The effective range ends before it starts."), code="period_inverted"
        )
    _validate_no_mapping_overlap(
        organization=locked.organization,
        account_role=locked.account_role,
        effective_from=locked.effective_from,
        effective_to=locked.effective_to,
        exclude_pk=locked.pk,
    )
    verifiers = _guard_verifiers(locked.organization, locked.account_role)
    locked.full_clean()
    locked.save()
    for verify in verifiers:
        verify()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def close_account_mapping(
    *, mapping: OrganizationAccountMapping, effective_to: datetime.date, reason: str = ""
) -> OrganizationAccountMapping:
    """
    End a mapping's effective range. Permitted even when used — this is the
    correction path — because it changes what the mapping says about the
    future, never about the postings that already snapshotted it.
    """
    locked = OrganizationAccountMapping.objects.select_for_update().get(pk=mapping.pk)
    if effective_to < locked.effective_from:
        raise ValidationError(
            _("The effective range ends before it starts."), code="period_inverted"
        )
    before = snapshot(locked)
    verifiers = _guard_verifiers(locked.organization, locked.account_role)
    locked.effective_to = effective_to
    locked.full_clean()
    locked.save(update_fields=["effective_to", "updated_at"])
    for verify in verifiers:
        verify()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=before,
        new_state=snapshot(locked),
        reason=reason,
    )
    return locked


@transaction.atomic
def archive_account_mapping(
    *, mapping: OrganizationAccountMapping, reason: str = ""
) -> OrganizationAccountMapping:
    """
    Withdraw a mapping that was recorded in error and never used.

    A used mapping cannot be archived: rows that postings snapshotted must
    stay readable, and the way to stop one applying is to close its range.
    """
    locked = OrganizationAccountMapping.objects.select_for_update().get(pk=mapping.pk)
    if mapping_is_used(locked):
        raise ValidationError(
            _("Mapping v%(version)s has been used by postings. Close it instead."),
            code="mapping_in_use",
            params={"version": locked.version},
        )
    before = snapshot(locked)
    verifiers = _guard_verifiers(locked.organization, locked.account_role)
    locked.is_active = False
    locked.save(update_fields=["is_active", "updated_at"])
    for verify in verifiers:
        verify()
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=locked,
        previous_state=before,
        new_state=snapshot(locked),
        reason=reason,
    )
    return locked


def resolve_default_account(
    *, organization: Organization, account_role: AccountRole | str, on_date: datetime.date
) -> OrganizationAccountMapping:
    """
    The organization's mapping for a role on a date — or a refusal that names
    the role. **There is no guessed account and no fallback code.** A posting
    that cannot resolve its role must fail before any effect exists, because a
    figure in the wrong account is worse than no figure: it reconciles.
    """
    role_code = account_role.code if isinstance(account_role, AccountRole) else account_role
    mapping = (
        OrganizationAccountMapping.objects.filter(
            organization=organization,
            account_role__code=role_code,
            is_active=True,
            effective_from__lte=on_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .select_related("account", "account_role")
        .order_by("-version")
        .first()
    )
    if mapping is None:
        raise ValidationError(
            _("No account is mapped for role %(role)s in %(organization)s on %(date)s."),
            code="account_role_unmapped",
            params={
                "role": role_code,
                "organization": organization.code,
                "date": on_date.isoformat(),
            },
        )
    return mapping


# ---------------------------------------------------------------------------
# 4. Posting
# ---------------------------------------------------------------------------


def _idempotency_fingerprint(
    *,
    command: str,
    accounting_date: datetime.date,
    document_date: datetime.date | None,
    is_adjustment: bool,
    source_document_type: str,
    source_document_id: str,
    source_event: str,
    lines: Sequence[PostingLine],
) -> str:
    """
    A digest of what the command actually asks for.

    The key answers "have I seen this request id before". This answers "and
    was it the same request". Without it, a caller who reused a key with
    different lines — a corrected amount, a different account — would silently
    receive the earlier journal and believe their correction had posted.

    What goes in is the financial substance: the dates, the source identity,
    and every line's account, branch, cost centre, and both amounts. Narration
    is deliberately excluded — it describes the entry without changing what it
    does to the ledger, and a client that regenerates descriptive text on
    retry should not be told its retry is a conflict.

    Amounts are quantized and stringified. A float in a fingerprint would make
    two identical requests hash differently on different machines.
    """
    payload = {
        "command": command,
        "accounting_date": accounting_date.isoformat(),
        "document_date": (document_date or accounting_date).isoformat(),
        "is_adjustment": is_adjustment,
        "source": [source_document_type, source_document_id, source_event],
        "lines": [
            [
                line.account.pk,
                line.branch.pk,
                line.cost_center.pk if line.cost_center is not None else None,
                str(quantize_money(line.debit)),
                str(quantize_money(line.credit)),
            ]
            for line in lines
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _replay(
    *,
    organization: Organization,
    idempotency_key: str,
    fingerprint: str,
) -> JournalEntry | None:
    """
    The entry this key already produced in this organization, if any.

    Scoped to the organization in the query itself. A lookup on the key alone
    would return another organization's journal to whoever guessed their key,
    which is a tenancy leak dressed up as a convenience.

    A key that comes back with a different fingerprint is not a retry. It is
    the same request id used for a different request, and returning the
    earlier entry would answer a question the caller did not ask.
    """
    existing = JournalEntry.objects.filter(
        organization=organization, idempotency_key=idempotency_key
    ).first()
    if existing is None:
        return None

    if existing.idempotency_fingerprint != fingerprint:
        raise ValidationError(
            _(
                "Idempotency key %(key)s was already used in %(organization)s for a "
                "different operation."
            ),
            code="idempotency_key_conflict",
            params={"key": idempotency_key, "organization": organization.code},
        )
    return existing


def _entry_for_source(
    *,
    organization: Organization,
    source_document_type: str,
    source_document_id: str,
    source_event: str,
) -> JournalEntry | None:
    """The entry already recording this economic event, if there is one."""
    return JournalEntry.objects.filter(
        organization=organization,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=source_event,
    ).first()


def _next_entry_number(*, organization: Organization, year: int) -> str:
    """
    Take the next journal number under a row lock.

    MAX(number)+1 would let two concurrent postings read the same maximum and
    both claim it.
    """
    sequence, _created = JournalNumberSequence.objects.get_or_create(
        organization=organization, year=year
    )
    locked = JournalNumberSequence.objects.select_for_update().get(pk=sequence.pk)
    locked.last_number += 1
    locked.save(update_fields=["last_number"])
    return f"JE-{year}-{locked.last_number:06d}"


def _validate_posting(
    *, organization: Organization, period: AccountingPeriod, lines: Sequence[PostingLine]
) -> None:
    validate_lines_present(lines)
    validate_line_sides(lines)
    validate_balanced(lines)
    validate_accounts_are_postable(lines)
    validate_branches_are_active(lines)
    validate_cost_centers(lines)
    validate_organization_consistency(organization, lines)
    validate_period_accepts_postings(period)


@transaction.atomic
def post_entry(
    *,
    organization: Organization,
    accounting_date: datetime.date,
    lines: Sequence[PostingLine],
    idempotency_key: str,
    document_date: datetime.date | None = None,
    narration: str = "",
    source_document_type: str = "",
    source_document_id: str = "",
    source_event: str = "",
    posting_rule_version: str = "",
    is_adjustment: bool = False,
    allow_closed_period: bool = False,
) -> JournalEntry:
    """
    Post a balanced entry to the ledger. The only way money is recorded.

    Atomic: the entry, its lines, and its audit event all commit or none do.
    A half-posted entry is worse than a failed one, because it looks complete.

    Idempotent on two independent keys, because they answer different
    questions:

    * `idempotency_key` — "is this the same *command* again?" A retried
      request after a network timeout returns the entry already posted.
    * `organization + source_document_type + source_document_id +
      source_event` — "is this the same *economic event* again?" Purchase
      invoice 145 can be posted once, whatever key the caller composes.

    Both are scoped to the organization, and the key is checked against a
    fingerprint of the request as well as against itself:

    | Presented | Result |
    |---|---|
    | same key, same request | the existing entry |
    | same key, different request | `idempotency_key_conflict` |
    | same key, another organization | an independent operation |
    | same source identity, different key | `source_event_already_posted` |

    A key alone was never enough. Matching on it and returning would let a
    caller reuse a key with corrected lines and receive the uncorrected
    journal, believing the correction had posted.

    `allow_closed_period` exists for the authorized correction path only —
    a reversal into a soft-closed period, for instance. It never bypasses a
    CLOSED period, which requires an explicit, audited reopening.
    """
    # Canonicalise FIRST. Everything below — the completeness check, the
    # fingerprint, the two uniqueness lookups, and the row itself — must see
    # the same bytes, or `"145 "` posts a second journal beside `"145"` and
    # the uniqueness guarantee misses the one retry it exists to catch.
    source = canonical_source_identity(
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=source_event,
    )
    source_document_type, source_document_id, source_event = source.as_tuple()

    validate_source_identity(
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=source_event,
    )

    fingerprint = _idempotency_fingerprint(
        command="post_entry",
        accounting_date=accounting_date,
        document_date=document_date,
        is_adjustment=is_adjustment,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=source_event,
        lines=lines,
    )
    existing = _replay(
        organization=organization,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
    )
    if existing is not None:
        return existing

    if source_event:
        already = _entry_for_source(
            organization=organization,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
            source_event=source_event,
        )
        if already is not None:
            raise ValidationError(
                _("%(type)s %(id)s has already been recorded as %(event)s by entry %(entry)s."),
                code="source_event_already_posted",
                params={
                    "type": source_document_type,
                    "id": source_document_id,
                    "event": source_event,
                    "entry": already.entry_number,
                },
            )

    period = resolve_period(organization=organization, accounting_date=accounting_date)

    validate_lines_present(lines)
    validate_line_sides(lines)
    validate_both_sides_present(lines)
    validate_entry_has_value(lines)
    validate_balanced(lines)
    validate_accounts_are_postable(lines)
    validate_branches_are_active(lines)
    validate_cost_centers(lines)
    validate_organization_consistency(organization, lines)
    if not allow_closed_period:
        validate_period_accepts_postings(period)
    elif period.state == PeriodState.CLOSED:
        raise ValidationError(
            _("Period %(period)s is closed. Reopen it explicitly first."),
            code="period_closed",
            params={"period": str(period)},
        )

    entry = JournalEntry(
        organization=organization,
        period=period,
        entry_number=_next_entry_number(organization=organization, year=period.fiscal_year.year),
        accounting_date=accounting_date,
        document_date=document_date or accounting_date,
        status=JournalEntryStatus.POSTED,
        is_adjustment=is_adjustment,
        idempotency_key=idempotency_key,
        idempotency_fingerprint=fingerprint,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=source_event,
        posting_rule_version=posting_rule_version,
        narration=narration,
        posted_at=timezone.now(),
        posted_by=_current_actor(),
    )
    entry.save()

    for number, line in enumerate(lines, start=1):
        JournalLine.objects.create(
            entry=entry,
            line_number=number,
            account=line.account,
            branch=line.branch,
            cost_center=line.cost_center,
            debit=quantize_money(line.debit),
            credit=quantize_money(line.credit),
            narration=line.narration,
        )

    record_audit_event(
        action=AuditAction.POSTED,
        target=entry,
        branch=lines[0].branch,
        new_state=snapshot(entry),
        reason=narration,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        metadata={"entry_number": entry.entry_number, "line_count": len(lines)},
    )
    return entry


def _current_actor() -> User | None:
    # Imported lazily: apps.core.context is request-scoped state, and importing
    # it at module level would tie the kernel to a web request.
    from apps.core.context import get_actor

    return get_actor()


@transaction.atomic
def reverse_entry(
    *,
    entry: JournalEntry,
    idempotency_key: str,
    reason: str,
    accounting_date: datetime.date | None = None,
) -> JournalEntry:
    """
    Reverse a posted entry by appending its mirror image.

    The original is never edited or deleted. Original, reversal, and any
    replacement all stay visible, which is what makes the correction auditable
    rather than a quiet rewrite.
    """
    # The explicit relationship is checked first so a second attempt returns
    # `already_reversed` — the accurate domain error — rather than the generic
    # `not_posted` it would otherwise hit, since the first reversal already
    # moved the original to REVERSED.
    if JournalEntry.objects.filter(reverses=entry).exists():
        raise ValidationError(
            _("Entry %(number)s has already been reversed."),
            code="already_reversed",
            params={"number": entry.entry_number},
        )
    if entry.status != JournalEntryStatus.POSTED:
        raise ValidationError(
            _("Only a posted entry can be reversed."),
            code="not_posted",
        )
    if not reason.strip():
        raise ValidationError(_("A reversal requires a reason."), code="reversal_reason_required")
    if entry.source_event == SourceEvent.REVERSED:
        # The reversal of a reversal would need to claim the same source
        # identity its target already holds, and the accounting answer is not
        # a third mirror anyway: post the corrected entry. Reported as a
        # domain error rather than left to collide on the unique index.
        raise ValidationError(
            _(
                "Entry %(number)s is itself a reversal. Post a replacement "
                "entry instead of reversing it."
            ),
            code="cannot_reverse_a_reversal",
            params={"number": entry.entry_number},
        )

    mirrored = [
        PostingLine(
            account=line.account,
            branch=line.branch,
            cost_center=line.cost_center,
            # Sides swap; magnitudes are identical, so the pair nets to zero.
            debit=line.credit,
            credit=line.debit,
            narration=line.narration,
        )
        for line in entry.lines.select_related("account", "branch", "cost_center").order_by(
            "line_number"
        )
    ]

    reversal = post_entry(
        organization=entry.organization,
        accounting_date=accounting_date or entry.accounting_date,
        lines=mirrored,
        idempotency_key=idempotency_key,
        document_date=entry.document_date,
        narration=reason.strip(),
        source_document_type=entry.source_document_type,
        source_document_id=entry.source_document_id,
        # The same upstream document, a different economic event. This is what
        # makes `PURCHASE_INVOICE / 145 / REVERSED` unique in its own right,
        # so the reversal is protected against duplication exactly as the
        # original posting is.
        source_event=SourceEvent.REVERSED if entry.source_event else "",
        posting_rule_version=entry.posting_rule_version,
        # A reversal is a correction, so it may enter a soft-closed period.
        allow_closed_period=True,
    )

    reversal.reverses = entry
    reversal.save(update_fields=["reverses"])

    # The original's status becomes REVERSED. This is the one permitted
    # transition on a posted entry; the trigger allows it and nothing else.
    JournalEntry.objects.filter(pk=entry.pk).update(status=JournalEntryStatus.REVERSED)
    entry.refresh_from_db()

    record_audit_event(
        action=AuditAction.REVERSED,
        target=entry,
        branch=mirrored[0].branch,
        new_state=snapshot(entry),
        reason=reason.strip(),
        metadata={
            "reversed_by_entry": reversal.entry_number,
            "original_entry": entry.entry_number,
        },
    )
    return reversal


def entry_total(entry: JournalEntry) -> Decimal:
    """The entry's debit total, which by construction equals its credit total."""
    return sum((line.debit for line in entry.lines.all()), Decimal("0")).quantize(Decimal("0.001"))


# ---------------------------------------------------------------------------
# 5. Drafts
# ---------------------------------------------------------------------------
#
# A draft is a journal being written, not a journal. It is deliberately
# allowed to be unbalanced: an entry is unbalanced between its first line and
# its last, and refusing to save that state would mean the only way to build
# an entry is to get it right in one action. The balance rule applies at the
# moment of posting, and the database enforces it there as well as here.
#
# A draft holds no entry number. Journal numbering is gapless, and a draft
# that is abandoned would otherwise leave a hole indistinguishable from a
# deleted entry.


def _write_lines(entry: JournalEntry, lines: Sequence[PostingLine]) -> None:
    """Replace an entry's lines. Only ever called on a draft."""
    entry.lines.all().delete()
    for number, line in enumerate(lines, start=1):
        JournalLine.objects.create(
            entry=entry,
            line_number=number,
            account=line.account,
            branch=line.branch,
            cost_center=line.cost_center,
            debit=quantize_money(line.debit),
            credit=quantize_money(line.credit),
            narration=line.narration,
        )


def _validate_draft_shape(*, organization: Organization, lines: Sequence[PostingLine]) -> None:
    """
    What must hold even for a half-written entry.

    Balance, both-sides, and non-zero value are absent on purpose — those are
    properties of a finished entry. Everything here is a property of a *line*,
    and a line that names another organization's account or a group account is
    wrong the moment it is written, not later.
    """
    validate_line_sides(lines)
    validate_accounts_are_postable(lines)
    validate_branches_are_active(lines)
    validate_cost_centers(lines)
    validate_organization_consistency(organization, lines)


@transaction.atomic
def create_draft(
    *,
    organization: Organization,
    accounting_date: datetime.date,
    lines: Sequence[PostingLine],
    idempotency_key: str | None = None,
    document_date: datetime.date | None = None,
    narration: str = "",
    is_adjustment: bool = False,
) -> JournalEntry:
    """
    Start a journal entry without committing it to the ledger.

    The period is resolved now so the draft cannot be dated outside the
    calendar, but its state is not checked: whether the period still accepts
    postings is a question for the moment of posting, which may be days later.

    `idempotency_key` is optional here and required for posting. A duplicated
    draft is a nuisance a user can delete; a duplicated posting is not
    something anyone can delete. When a key *is* supplied it carries the same
    guarantee as posting: same key and same request returns the existing
    draft, same key and a different request is a conflict.
    """
    period = resolve_period(organization=organization, accounting_date=accounting_date)
    _validate_draft_shape(organization=organization, lines=lines)

    fingerprint = _idempotency_fingerprint(
        command="create_draft",
        accounting_date=accounting_date,
        document_date=document_date,
        is_adjustment=is_adjustment,
        source_document_type="",
        source_document_id="",
        source_event="",
        lines=lines,
    )
    if idempotency_key is not None:
        existing = _replay(
            organization=organization,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
        if existing is not None:
            return existing

    entry = JournalEntry(
        organization=organization,
        period=period,
        entry_number="",
        accounting_date=accounting_date,
        document_date=document_date or accounting_date,
        status=JournalEntryStatus.DRAFT,
        is_adjustment=is_adjustment,
        idempotency_key=idempotency_key or f"draft:{uuid.uuid4()}",
        idempotency_fingerprint=fingerprint,
        narration=narration,
    )
    entry.save()
    _write_lines(entry, lines)

    record_audit_event(
        action=AuditAction.CREATED,
        target=entry,
        branch=lines[0].branch if lines else None,
        new_state=snapshot(entry),
        reason=narration,
        metadata={"line_count": len(lines)},
    )
    return entry


@transaction.atomic
def update_draft(
    *,
    entry: JournalEntry,
    accounting_date: datetime.date | None = None,
    lines: Sequence[PostingLine] | None = None,
    document_date: datetime.date | None = None,
    narration: str | None = None,
    is_adjustment: bool | None = None,
) -> JournalEntry:
    """
    Amend a draft. Refuses anything that has left draft.

    Lines are replaced wholesale rather than patched. A partial line update
    would need a stable line identity across edits, and line numbers are also
    the deterministic tie-break for allocation — reusing one for a different
    line would quietly change how a later residual is distributed.
    """
    if entry.status != JournalEntryStatus.DRAFT:
        raise ValidationError(
            _("Entry %(number)s is not a draft and cannot be amended."),
            code="not_a_draft",
            params={"number": entry.entry_number or entry.pk},
        )

    before = snapshot(JournalEntry.objects.get(pk=entry.pk))

    if accounting_date is not None:
        entry.period = resolve_period(
            organization=entry.organization, accounting_date=accounting_date
        )
        entry.accounting_date = accounting_date
    if document_date is not None:
        entry.document_date = document_date
    if narration is not None:
        entry.narration = narration
    if is_adjustment is not None:
        entry.is_adjustment = is_adjustment
    entry.save()

    if lines is not None:
        _validate_draft_shape(organization=entry.organization, lines=lines)
        _write_lines(entry, lines)

    record_audit_event(
        action=AuditAction.UPDATED,
        target=entry,
        branch=lines[0].branch if lines else None,
        previous_state=before,
        new_state=snapshot(entry),
    )
    return entry


@transaction.atomic
def post_draft(*, entry: JournalEntry, allow_closed_period: bool = False) -> JournalEntry:
    """
    Move a draft into the ledger.

    Every posting rule runs here, on the lines as they now stand — the draft
    was allowed to be unbalanced while it was being written, so this is the
    first moment the finished entry is checked. It takes its gapless journal
    number at this point and not before.
    """
    if entry.status != JournalEntryStatus.DRAFT:
        raise ValidationError(
            _("Entry %(number)s has already been posted."),
            code="not_a_draft",
            params={"number": entry.entry_number or entry.pk},
        )

    lines = [
        PostingLine(
            account=line.account,
            branch=line.branch,
            cost_center=line.cost_center,
            debit=line.debit,
            credit=line.credit,
            narration=line.narration,
        )
        for line in entry.lines.select_related("account", "branch", "cost_center").order_by(
            "line_number"
        )
    ]

    validate_lines_present(lines)
    validate_line_sides(lines)
    validate_both_sides_present(lines)
    validate_entry_has_value(lines)
    validate_balanced(lines)
    validate_accounts_are_postable(lines)
    validate_branches_are_active(lines)
    validate_cost_centers(lines)
    validate_organization_consistency(entry.organization, lines)

    period = entry.period
    if not allow_closed_period:
        validate_period_accepts_postings(period)
    elif period.state == PeriodState.CLOSED:
        raise ValidationError(
            _("Period %(period)s is closed. Reopen it explicitly first."),
            code="period_closed",
            params={"period": str(period)},
        )

    entry.entry_number = _next_entry_number(
        organization=entry.organization, year=period.fiscal_year.year
    )
    entry.status = JournalEntryStatus.POSTED
    entry.posted_at = timezone.now()
    entry.posted_by = _current_actor()
    entry.save(update_fields=["entry_number", "status", "posted_at", "posted_by", "updated_at"])

    record_audit_event(
        action=AuditAction.POSTED,
        target=entry,
        branch=lines[0].branch,
        new_state=snapshot(entry),
        reason=entry.narration,
        metadata={"entry_number": entry.entry_number, "line_count": len(lines)},
    )
    return entry


@transaction.atomic
def discard_draft(*, entry: JournalEntry, reason: str = "") -> None:
    """
    Delete a draft.

    Only a draft. A posted entry is deleted by nothing and by nobody — the
    trigger refuses it — and the correction for one is a reversal. The audit
    event is recorded before the row goes, so the discard survives it.
    """
    if entry.status != JournalEntryStatus.DRAFT:
        raise ValidationError(
            _("Entry %(number)s is posted and cannot be deleted. Reverse it instead."),
            code="not_a_draft",
            params={"number": entry.entry_number or entry.pk},
        )

    record_audit_event(
        action=AuditAction.DELETED,
        target=entry,
        previous_state=snapshot(entry),
        reason=reason,
    )
    entry.lines.all().delete()
    entry.delete()
