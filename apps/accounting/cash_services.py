"""
Cashbox and bank-account master data.

Kept out of `services.py`, which is the ledger kernel. A cashbox is not a
kernel concept: the kernel knows accounts and journal lines, and a cashbox is
Accounting's *name* for one of those accounts plus who is responsible for
counting it. Putting it in the kernel would make the kernel know about drawers.

Every rule here is stated once and applies to both kinds, because the two
records differ only in what a branch means to them.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, AccountClass, BankAccount, Cashbox
from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.organizations.models import Branch, Organization

#: Which account classes a cash or bank account may point at.
#:
#: Class 1 only. A cashbox pointing at a revenue account would produce a
#: statement that runs backwards — every receipt would read as a credit and the
#: drawer would appear to owe money — and nothing on the page would say why.
#: Refused here rather than left to be noticed.
CASH_ACCOUNT_CLASSES = frozenset({AccountClass.ASSET})


def _validate_cash_account(
    *,
    organization: Organization,
    account: Account,
    exclude_cashbox: int | None = None,
    exclude_bank: int | None = None,
) -> None:
    """
    Whether this GL account may back a cash or bank record.

    The cross-table half — "and not already used by the *other* kind" — cannot
    be a database constraint, because a constraint sees one table. It is
    checked here and re-checked by `verify_accounting`, so a row inserted by
    some other path is still reported rather than silently accepted.
    """
    if account.organization_id != organization.pk:
        raise ValidationError(
            _("The account belongs to another organization."),
            code="account_organization_mismatch",
        )
    if not account.is_postable:
        raise ValidationError(
            _("Only a detail account accepts journal lines."),
            code="account_not_postable",
        )
    if not account.is_active:
        raise ValidationError(
            _("The account is archived."),
            code="account_archived",
        )
    if account.account_class not in CASH_ACCOUNT_CLASSES:
        raise ValidationError(
            _("A cash or bank record must point at an asset account."),
            code="account_not_an_asset",
        )

    clash = Cashbox.objects.filter(account=account, is_active=True)
    if exclude_cashbox is not None:
        clash = clash.exclude(pk=exclude_cashbox)
    if clash.exists():
        raise ValidationError(
            _("An active cashbox already uses this account."),
            code="account_already_a_cashbox",
        )

    bank_clash = BankAccount.objects.filter(account=account, is_active=True)
    if exclude_bank is not None:
        bank_clash = bank_clash.exclude(pk=exclude_bank)
    if bank_clash.exists():
        raise ValidationError(
            _("An active bank account already uses this account."),
            code="account_already_a_bank_account",
        )


# ---------------------------------------------------------------------------
# Cashboxes
# ---------------------------------------------------------------------------


@transaction.atomic
def create_cashbox(
    *,
    organization: Organization,
    branch: Branch,
    account: Account,
    code: str,
    name: str,
    opened_on: datetime.date,
    responsible_note: str = "",
    notes: str = "",
) -> Cashbox:
    """Register a drawer, and the account its movements land in."""
    if branch.organization_id != organization.pk:
        raise ValidationError(
            _("The branch belongs to another organization."), code="branch_organization_mismatch"
        )
    _validate_cash_account(organization=organization, account=account)

    cashbox = Cashbox(
        organization=organization,
        branch=branch,
        account=account,
        code=code.strip().upper(),
        name=name.strip(),
        opened_on=opened_on,
        responsible_note=responsible_note.strip(),
        notes=notes.strip(),
    )
    cashbox.full_clean()
    cashbox.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=cashbox,
        branch=branch,
        new_state=snapshot(cashbox),
        reason=str(_("cashbox registered")),
    )
    return cashbox


@transaction.atomic
def update_cashbox(
    *,
    cashbox: Cashbox,
    name: str,
    responsible_note: str,
    notes: str,
    reason: str = "",
) -> Cashbox:
    """
    Amend the descriptive metadata only.

    The code, the branch and above all the **account** are not amendable. A
    cashbox that changed account would silently re-attribute every statement it
    has ever shown: the same drawer, a different history. The path for a real
    change is to archive this record and register the next one, which leaves
    both readable.
    """
    before = snapshot(cashbox)
    cashbox.name = name.strip()
    cashbox.name = name.strip()
    cashbox.responsible_note = responsible_note.strip()
    cashbox.notes = notes.strip()
    cashbox.full_clean()
    cashbox.save(update_fields=["name", "responsible_note", "notes", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=cashbox,
        branch=cashbox.branch,
        previous_state=before,
        new_state=snapshot(cashbox),
        reason=reason,
    )
    return cashbox


@transaction.atomic
def archive_cashbox(*, cashbox: Cashbox, reason: str = "") -> Cashbox:
    """Withdraw a drawer from use. Its statement stays readable forever."""
    if not cashbox.is_active:
        return cashbox
    before = snapshot(cashbox)
    cashbox.is_active = False
    cashbox.archived_at = timezone.now()
    cashbox.full_clean()
    cashbox.save(update_fields=["is_active", "archived_at", "updated_at"])
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=cashbox,
        branch=cashbox.branch,
        previous_state=before,
        new_state=snapshot(cashbox),
        reason=reason,
    )
    return cashbox


@transaction.atomic
def reactivate_cashbox(*, cashbox: Cashbox, reason: str = "") -> Cashbox:
    """
    Return a drawer to use, if its account is still free.

    Re-checked rather than assumed: another cashbox may have taken the account
    while this one was archived, and the partial unique constraint would refuse
    the save with a database error rather than a sentence somebody can act on.
    """
    if cashbox.is_active:
        return cashbox
    _validate_cash_account(
        organization=cashbox.organization,
        account=cashbox.account,
        exclude_cashbox=cashbox.pk,
    )
    before = snapshot(cashbox)
    cashbox.is_active = True
    cashbox.archived_at = None
    cashbox.full_clean()
    cashbox.save(update_fields=["is_active", "archived_at", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=cashbox,
        branch=cashbox.branch,
        previous_state=before,
        new_state=snapshot(cashbox),
        reason=reason or str(_("cashbox reactivated")),
    )
    return cashbox


# ---------------------------------------------------------------------------
# Bank accounts
# ---------------------------------------------------------------------------


@transaction.atomic
def create_bank_account(
    *,
    organization: Organization,
    account: Account,
    code: str,
    bank_name: str,
    name: str,
    masked_account_number: str,
    branch: Branch | None = None,
    iban: str = "",
    notes: str = "",
) -> BankAccount:
    """Register a bank account, and the GL account its movements land in."""
    if branch is not None and branch.organization_id != organization.pk:
        raise ValidationError(
            _("The branch belongs to another organization."), code="branch_organization_mismatch"
        )
    _validate_cash_account(organization=organization, account=account)

    bank = BankAccount(
        organization=organization,
        branch=branch,
        account=account,
        code=code.strip().upper(),
        bank_name=bank_name.strip(),
        name=name.strip(),
        masked_account_number=_mask(masked_account_number),
        iban=iban.strip().upper(),
        notes=notes.strip(),
    )
    bank.full_clean()
    bank.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=bank,
        branch=branch,
        new_state=snapshot(bank),
        reason=str(_("bank account registered")),
    )
    return bank


def _mask(raw: str) -> str:
    """
    Keep the last four characters and mask the rest.

    Applied on the way in rather than trusted from the form, because the field
    is the *only* protection: if a full number reaches the column it is stored,
    exported, and in every history row from then on. Masking at the boundary
    means a full number typed by mistake never lands.
    """
    digits = raw.strip().replace(" ", "")
    if len(digits) <= 4:
        return digits
    return f"****{digits[-4:]}"


@transaction.atomic
def update_bank_account(
    *,
    bank: BankAccount,
    name: str,
    bank_name: str,
    masked_account_number: str,
    iban: str,
    notes: str,
    reason: str = "",
) -> BankAccount:
    """Amend descriptive metadata. The GL account is not amendable — see `update_cashbox`."""
    before = snapshot(bank)
    bank.name = name.strip()
    bank.name = name.strip()
    bank.bank_name = bank_name.strip()
    bank.masked_account_number = _mask(masked_account_number)
    bank.iban = iban.strip().upper()
    bank.notes = notes.strip()
    bank.full_clean()
    bank.save(
        update_fields=[
            "name",
            "name",
            "bank_name",
            "masked_account_number",
            "iban",
            "notes",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=bank,
        branch=bank.branch,
        previous_state=before,
        new_state=snapshot(bank),
        reason=reason,
    )
    return bank


@transaction.atomic
def archive_bank_account(*, bank: BankAccount, reason: str = "") -> BankAccount:
    if not bank.is_active:
        return bank
    before = snapshot(bank)
    bank.is_active = False
    bank.archived_at = timezone.now()
    bank.full_clean()
    bank.save(update_fields=["is_active", "archived_at", "updated_at"])
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=bank,
        branch=bank.branch,
        previous_state=before,
        new_state=snapshot(bank),
        reason=reason,
    )
    return bank


@transaction.atomic
def reactivate_bank_account(*, bank: BankAccount, reason: str = "") -> BankAccount:
    if bank.is_active:
        return bank
    _validate_cash_account(
        organization=bank.organization, account=bank.account, exclude_bank=bank.pk
    )
    before = snapshot(bank)
    bank.is_active = True
    bank.archived_at = None
    bank.full_clean()
    bank.save(update_fields=["is_active", "archived_at", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=bank,
        branch=bank.branch,
        previous_state=before,
        new_state=snapshot(bank),
        reason=reason or str(_("bank account reactivated")),
    )
    return bank


@transaction.atomic
def record_reconciliation(*, record: Any, on_date: datetime.date, reason: str = "") -> Any:
    """
    Mark a cash or bank record reconciled to a date.

    A **date**, never an amount. Storing "the balance we agreed" would create
    exactly the mutable figure ADR-030 §1 refuses; the balance on that date is
    already derivable from the ledger, and if the two ever disagreed the stored
    one would be believed.
    """
    before = snapshot(record)
    record.last_reconciled_on = on_date
    record.full_clean()
    record.save(update_fields=["last_reconciled_on", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=record,
        branch=record.branch,
        previous_state=before,
        new_state=snapshot(record),
        reason=reason or str(_("reconciled")),
    )
    return record


__all__ = [
    "CASH_ACCOUNT_CLASSES",
    "archive_bank_account",
    "archive_cashbox",
    "create_bank_account",
    "create_cashbox",
    "reactivate_bank_account",
    "reactivate_cashbox",
    "record_reconciliation",
    "update_bank_account",
    "update_cashbox",
]
