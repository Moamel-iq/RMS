"""
دليل الحسابات · الأدوار المحاسبية · ربط الحسابات · الصناديق · الحسابات البنكية.

The master-data half of the accounting API. Commands, not CRUD, for the same
reason `api.py` gives: an account is withdrawn rather than deleted and a
mapping is closed rather than overwritten, so the verbs are named after the
acts and there is no writable resource that could rewrite history by accident.

Every write here calls `apps/accounting/commands.py`. Nothing in this file
touches `Model.objects.create`, and nothing restates an accounting rule — a
rule duplicated in a view is a rule that will disagree with the kernel the
first time one of them changes.

**No balance is ever returned by a cashbox or bank endpoint.** There is no
stored balance to return (ADR-030 §1): a cash position is derived from posted
journal lines through the trial balance and the ledger endpoints, which is
where a caller reconciling cash should be reading it from anyway.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.accounting.commands import (
    amend_account_role_mapping,
    amend_bank_account,
    amend_cashbox,
    archive_account_role_mapping,
    archive_chart_account,
    clear_account_report_mapping,
    close_account_role_mapping,
    create_chart_account,
    list_account_mappings,
    list_account_roles,
    list_chart_accounts,
    map_account_role,
    mark_cash_record_reconciled,
    reactivate_chart_account,
    read_chart_account,
    register_bank_account,
    register_cashbox,
    restore_bank_account,
    restore_cashbox,
    set_account_report_mapping,
    update_chart_account,
    withdraw_bank_account,
    withdraw_cashbox,
)
from apps.accounting.models import (
    Account,
    AccountReportMapping,
    AccountRole,
    BankAccount,
    Cashbox,
    ManualPostingPolicy,
    OrganizationAccountMapping,
    PresentationSection,
)
from apps.accounting.permissions import MANAGE_BANK_ACCOUNTS, MANAGE_CASHBOXES
from apps.organizations.authorization import organizations_with_permission
from apps.users.models import User

router = Router(tags=["accounting-master"])


def _actor(request: HttpRequest) -> User:
    user: User = request.user  # type: ignore[assignment]
    return user


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class AccountIn(Schema):
    organization_id: int
    code: str
    name_ar: str
    name_en: str
    requires_cost_center: bool | None = None
    manual_posting_policy: str = ManualPostingPolicy.ALLOWED
    external_accounting_system: str = ""
    external_account_code: str = ""


class AccountPatchIn(Schema):
    name_ar: str
    name_en: str
    requires_cost_center: bool
    manual_posting_policy: str
    reason: str = ""
    #: The elevated path. Changing a seeded control account's posting policy
    #: needs `accounting.manage_accounts` on top of the chart permission, and
    #: the command checks it — asking for it here does not grant it.
    allow_system: bool = False


class ReasonIn(Schema):
    reason: str = ""


class RequiredReasonIn(Schema):
    reason: str


class ReportMappingIn(Schema):
    organization_id: int
    account_id: int
    statement_group: str
    presentation_section: str = PresentationSection.NOT_APPLICABLE
    display_order: int = 0


class RoleMappingIn(Schema):
    organization_id: int
    account_role_id: int
    account_id: int
    effective_from: datetime.date
    effective_to: datetime.date | None = None


class RoleMappingPatchIn(Schema):
    account_id: int | None = None
    effective_from: datetime.date | None = None
    effective_to: datetime.date | None = None
    clear_effective_to: bool = False


class RoleMappingCloseIn(Schema):
    effective_to: datetime.date
    reason: str = ""


class CashboxIn(Schema):
    organization_id: int
    branch_id: int
    account_id: int
    code: str
    name_ar: str
    name_en: str
    opened_on: datetime.date
    responsible_note: str = ""
    notes: str = ""


class CashboxPatchIn(Schema):
    name_ar: str
    name_en: str
    responsible_note: str = ""
    notes: str = ""
    reason: str = ""


class BankAccountIn(Schema):
    organization_id: int
    account_id: int
    code: str
    bank_name: str
    name_ar: str
    name_en: str
    masked_account_number: str
    branch_id: int | None = None
    iban: str = ""
    notes: str = ""


class BankAccountPatchIn(Schema):
    bank_name: str
    name_ar: str
    name_en: str
    masked_account_number: str
    iban: str = ""
    notes: str = ""
    reason: str = ""


class ReconcileIn(Schema):
    on_date: datetime.date
    reason: str = ""


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------


class AccountOut(Schema):
    id: int
    organization_id: int
    code: str
    name_ar: str
    name_en: str
    account_class: str
    parent_id: int | None
    is_postable: bool
    is_active: bool
    is_system: bool
    requires_cost_center: bool
    manual_posting_policy: str


class RoleOut(Schema):
    id: int
    code: str
    domain: str
    name_ar: str
    name_en: str
    is_system: bool


class RoleMappingOut(Schema):
    id: int
    organization_id: int
    account_role_id: int
    account_role_code: str
    account_id: int
    account_code: str
    version: int
    effective_from: datetime.date
    effective_to: datetime.date | None
    is_active: bool


class ReportMappingOut(Schema):
    id: int
    organization_id: int
    account_id: int
    account_code: str
    statement_group: str
    presentation_section: str
    display_order: int
    is_active: bool


class CashboxOut(Schema):
    id: int
    organization_id: int
    branch_id: int
    account_id: int
    account_code: str
    code: str
    name_ar: str
    name_en: str
    opened_on: datetime.date
    responsible_note: str
    is_active: bool
    last_reconciled_on: datetime.date | None


class BankAccountOut(Schema):
    id: int
    organization_id: int
    branch_id: int | None
    account_id: int
    account_code: str
    code: str
    bank_name: str
    name_ar: str
    name_en: str
    masked_account_number: str
    iban: str
    is_active: bool
    last_reconciled_on: datetime.date | None


def _account(account: Account) -> dict[str, Any]:
    return {
        "id": account.pk,
        "organization_id": account.organization_id,
        "code": account.code,
        "name_ar": account.name_ar,
        "name_en": account.name_en,
        "account_class": account.account_class,
        "parent_id": account.parent_id,
        "is_postable": account.is_postable,
        "is_active": account.is_active,
        "is_system": account.is_system,
        "requires_cost_center": account.requires_cost_center,
        "manual_posting_policy": account.manual_posting_policy,
    }


def _role(role: AccountRole) -> dict[str, Any]:
    return {
        "id": role.pk,
        "code": role.code,
        "domain": role.domain,
        "name_ar": role.name_ar,
        "name_en": role.name_en,
        "is_system": role.is_system,
    }


def _role_mapping(mapping: OrganizationAccountMapping) -> dict[str, Any]:
    return {
        "id": mapping.pk,
        "organization_id": mapping.organization_id,
        "account_role_id": mapping.account_role_id,
        "account_role_code": mapping.account_role.code,
        "account_id": mapping.account_id,
        "account_code": mapping.account.code,
        "version": mapping.version,
        "effective_from": mapping.effective_from,
        "effective_to": mapping.effective_to,
        "is_active": mapping.is_active,
    }


def _report_mapping(mapping: AccountReportMapping) -> dict[str, Any]:
    return {
        "id": mapping.pk,
        "organization_id": mapping.organization_id,
        "account_id": mapping.account_id,
        "account_code": mapping.account.code,
        "statement_group": mapping.statement_group,
        "presentation_section": mapping.presentation_section,
        "display_order": mapping.display_order,
        "is_active": mapping.is_active,
    }


def _cashbox(cashbox: Cashbox) -> dict[str, Any]:
    return {
        "id": cashbox.pk,
        "organization_id": cashbox.organization_id,
        "branch_id": cashbox.branch_id,
        "account_id": cashbox.account_id,
        "account_code": cashbox.account.code,
        "code": cashbox.code,
        "name_ar": cashbox.name_ar,
        "name_en": cashbox.name_en,
        "opened_on": cashbox.opened_on,
        "responsible_note": cashbox.responsible_note,
        "is_active": cashbox.is_active,
        "last_reconciled_on": cashbox.last_reconciled_on,
    }


def _bank(bank: BankAccount) -> dict[str, Any]:
    return {
        "id": bank.pk,
        "organization_id": bank.organization_id,
        "branch_id": bank.branch_id,
        "account_id": bank.account_id,
        "account_code": bank.account.code,
        "code": bank.code,
        "bank_name": bank.bank_name,
        "name_ar": bank.name_ar,
        "name_en": bank.name_en,
        "masked_account_number": bank.masked_account_number,
        "iban": bank.iban,
        "is_active": bank.is_active,
        "last_reconciled_on": bank.last_reconciled_on,
    }


# ---------------------------------------------------------------------------
# دليل الحسابات — chart of accounts
# ---------------------------------------------------------------------------


@router.get("/accounts/", response=list[AccountOut], summary="List chart accounts in scope")
def list_accounts(
    request: HttpRequest,
    organization_id: int | None = None,
    include_archived: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    accounts = list_chart_accounts(
        actor=_actor(request),
        organization_id=organization_id,
        include_archived=include_archived,
    )
    window = accounts[offset : offset + min(limit, 500)]
    return [_account(account) for account in window]


@router.get("/accounts/{account_id}/", response=AccountOut, summary="Read one chart account")
def read_account(request: HttpRequest, account_id: int) -> dict[str, Any]:
    return _account(read_chart_account(actor=_actor(request), account_id=account_id))


@router.post("/accounts/", response={201: AccountOut}, summary="Add an account to a chart")
def create_account_endpoint(request: HttpRequest, payload: AccountIn) -> Status[dict[str, Any]]:
    account = create_chart_account(actor=_actor(request), **payload.dict())
    return Status(201, _account(account))


@router.patch(
    "/accounts/{account_id}/",
    response=AccountOut,
    summary="Amend the metadata an account may safely change",
)
def amend_account_endpoint(
    request: HttpRequest, account_id: int, payload: AccountPatchIn
) -> dict[str, Any]:
    account = update_chart_account(actor=_actor(request), account_id=account_id, **payload.dict())
    return _account(account)


@router.post(
    "/accounts/{account_id}/archive/",
    response=AccountOut,
    summary="Withdraw an account from use — never a delete",
)
def archive_account_endpoint(
    request: HttpRequest, account_id: int, payload: ReasonIn
) -> dict[str, Any]:
    account = archive_chart_account(
        actor=_actor(request), account_id=account_id, reason=payload.reason
    )
    return _account(account)


@router.post(
    "/accounts/{account_id}/reactivate/",
    response=AccountOut,
    summary="Bring an archived account back into use",
)
def reactivate_account_endpoint(
    request: HttpRequest, account_id: int, payload: ReasonIn
) -> dict[str, Any]:
    account = reactivate_chart_account(
        actor=_actor(request), account_id=account_id, reason=payload.reason
    )
    return _account(account)


# ---------------------------------------------------------------------------
# الأدوار المحاسبية · ربط الحسابات — roles and their mappings
# ---------------------------------------------------------------------------


@router.get("/account-roles/", response=list[RoleOut], summary="The system role vocabulary")
def list_roles(request: HttpRequest) -> list[dict[str, Any]]:
    return [_role(role) for role in list_account_roles(actor=_actor(request))]


@router.get(
    "/account-role-mappings/",
    response=list[RoleMappingOut],
    summary="One organization's role mappings, every version",
)
def list_role_mappings(request: HttpRequest, organization_id: int) -> list[dict[str, Any]]:
    mappings = list_account_mappings(actor=_actor(request), organization_id=organization_id)
    return [_role_mapping(mapping) for mapping in mappings]


@router.post(
    "/account-role-mappings/",
    response={201: RoleMappingOut},
    summary="Map a role to an account from a date",
)
def create_role_mapping(request: HttpRequest, payload: RoleMappingIn) -> Status[dict[str, Any]]:
    mapping = map_account_role(actor=_actor(request), **payload.dict())
    return Status(201, _role_mapping(mapping))


@router.patch(
    "/account-role-mappings/{mapping_id}/",
    response=RoleMappingOut,
    summary="Correct a mapping nothing has posted through yet",
)
def amend_role_mapping(
    request: HttpRequest, mapping_id: int, payload: RoleMappingPatchIn
) -> dict[str, Any]:
    mapping = amend_account_role_mapping(
        actor=_actor(request), mapping_id=mapping_id, **payload.dict()
    )
    return _role_mapping(mapping)


@router.post(
    "/account-role-mappings/{mapping_id}/close/",
    response=RoleMappingOut,
    summary="End a mapping's effective range — the path for a used one",
)
def close_role_mapping(
    request: HttpRequest, mapping_id: int, payload: RoleMappingCloseIn
) -> dict[str, Any]:
    mapping = close_account_role_mapping(
        actor=_actor(request),
        mapping_id=mapping_id,
        effective_to=payload.effective_to,
        reason=payload.reason,
    )
    return _role_mapping(mapping)


@router.post(
    "/account-role-mappings/{mapping_id}/archive/",
    response=RoleMappingOut,
    summary="Withdraw a mapping recorded in error",
)
def archive_role_mapping(
    request: HttpRequest, mapping_id: int, payload: ReasonIn
) -> dict[str, Any]:
    mapping = archive_account_role_mapping(
        actor=_actor(request), mapping_id=mapping_id, reason=payload.reason
    )
    return _role_mapping(mapping)


# ---------------------------------------------------------------------------
# Statement classification (ADR-031 §1)
# ---------------------------------------------------------------------------


@router.post(
    "/report-mappings/",
    response={201: ReportMappingOut},
    summary="Classify an account for the financial statements",
)
def set_report_mapping_endpoint(
    request: HttpRequest, payload: ReportMappingIn
) -> Status[dict[str, Any]]:
    mapping = set_account_report_mapping(actor=_actor(request), **payload.dict())
    return Status(201, _report_mapping(mapping))


@router.post(
    "/report-mappings/{mapping_id}/clear/",
    response=ReportMappingOut,
    summary="Withdraw a classification — the account becomes visibly unmapped",
)
def clear_report_mapping_endpoint(
    request: HttpRequest, mapping_id: int, payload: ReasonIn
) -> dict[str, Any]:
    mapping = clear_account_report_mapping(
        actor=_actor(request), mapping_id=mapping_id, reason=payload.reason
    )
    return _report_mapping(mapping)


# ---------------------------------------------------------------------------
# الصناديق — cashboxes
# ---------------------------------------------------------------------------


@router.get("/cashboxes/", response=list[CashboxOut], summary="Cashboxes in scope")
def list_cashboxes(
    request: HttpRequest, organization_id: int | None = None, include_archived: bool = False
) -> list[dict[str, Any]]:
    actor = _actor(request)
    cashboxes = Cashbox.objects.filter(
        organization__in=organizations_with_permission(actor, MANAGE_CASHBOXES)
    ).select_related("organization", "branch", "account")
    if organization_id is not None:
        cashboxes = cashboxes.filter(organization_id=organization_id)
    if not include_archived:
        cashboxes = cashboxes.filter(is_active=True)
    return [_cashbox(row) for row in cashboxes.order_by("code")]


@router.post("/cashboxes/", response={201: CashboxOut}, summary="Register a cashbox")
def create_cashbox_endpoint(request: HttpRequest, payload: CashboxIn) -> Status[dict[str, Any]]:
    return Status(201, _cashbox(register_cashbox(actor=_actor(request), **payload.dict())))


@router.patch(
    "/cashboxes/{cashbox_id}/", response=CashboxOut, summary="Amend a cashbox's description"
)
def amend_cashbox_endpoint(
    request: HttpRequest, cashbox_id: int, payload: CashboxPatchIn
) -> dict[str, Any]:
    return _cashbox(amend_cashbox(actor=_actor(request), cashbox_id=cashbox_id, **payload.dict()))


@router.post(
    "/cashboxes/{cashbox_id}/withdraw/", response=CashboxOut, summary="Take a cashbox out of use"
)
def withdraw_cashbox_endpoint(
    request: HttpRequest, cashbox_id: int, payload: ReasonIn
) -> dict[str, Any]:
    return _cashbox(
        withdraw_cashbox(actor=_actor(request), cashbox_id=cashbox_id, reason=payload.reason)
    )


@router.post(
    "/cashboxes/{cashbox_id}/restore/", response=CashboxOut, summary="Return a cashbox to use"
)
def restore_cashbox_endpoint(
    request: HttpRequest, cashbox_id: int, payload: ReasonIn
) -> dict[str, Any]:
    return _cashbox(
        restore_cashbox(actor=_actor(request), cashbox_id=cashbox_id, reason=payload.reason)
    )


# ---------------------------------------------------------------------------
# الحسابات البنكية — bank accounts
# ---------------------------------------------------------------------------


@router.get("/bank-accounts/", response=list[BankAccountOut], summary="Bank accounts in scope")
def list_bank_accounts(
    request: HttpRequest, organization_id: int | None = None, include_archived: bool = False
) -> list[dict[str, Any]]:
    actor = _actor(request)
    banks = BankAccount.objects.filter(
        organization__in=organizations_with_permission(actor, MANAGE_BANK_ACCOUNTS)
    ).select_related("organization", "branch", "account")
    if organization_id is not None:
        banks = banks.filter(organization_id=organization_id)
    if not include_archived:
        banks = banks.filter(is_active=True)
    return [_bank(row) for row in banks.order_by("code")]


@router.post("/bank-accounts/", response={201: BankAccountOut}, summary="Register a bank account")
def create_bank_endpoint(request: HttpRequest, payload: BankAccountIn) -> Status[dict[str, Any]]:
    return Status(201, _bank(register_bank_account(actor=_actor(request), **payload.dict())))


@router.patch(
    "/bank-accounts/{bank_id}/",
    response=BankAccountOut,
    summary="Amend a bank account's description",
)
def amend_bank_endpoint(
    request: HttpRequest, bank_id: int, payload: BankAccountPatchIn
) -> dict[str, Any]:
    return _bank(amend_bank_account(actor=_actor(request), bank_id=bank_id, **payload.dict()))


@router.post(
    "/bank-accounts/{bank_id}/withdraw/",
    response=BankAccountOut,
    summary="Take a bank account out of use",
)
def withdraw_bank_endpoint(request: HttpRequest, bank_id: int, payload: ReasonIn) -> dict[str, Any]:
    return _bank(
        withdraw_bank_account(actor=_actor(request), bank_id=bank_id, reason=payload.reason)
    )


@router.post(
    "/bank-accounts/{bank_id}/restore/",
    response=BankAccountOut,
    summary="Return a bank account to use",
)
def restore_bank_endpoint(request: HttpRequest, bank_id: int, payload: ReasonIn) -> dict[str, Any]:
    return _bank(
        restore_bank_account(actor=_actor(request), bank_id=bank_id, reason=payload.reason)
    )


@router.post(
    "/cash-records/{kind}/{record_id}/reconcile/",
    response=dict,
    summary="Stamp a reconciliation date on a cashbox or bank account",
)
def reconcile_cash_record(
    request: HttpRequest, kind: str, record_id: int, payload: ReconcileIn
) -> dict[str, Any]:
    """
    `kind` is `cashbox` or `bank`.

    Stamping a date records that a human compared the record to its statement.
    It does not change a balance, because there is no stored balance to change
    — the figure being reconciled against comes from the ledger.
    """
    record = mark_cash_record_reconciled(
        actor=_actor(request),
        kind=kind,
        record_id=record_id,
        on_date=payload.on_date,
        reason=payload.reason,
    )
    return _cashbox(record) if isinstance(record, Cashbox) else _bank(record)
