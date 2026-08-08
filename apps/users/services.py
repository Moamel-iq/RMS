"""User account commands."""

from __future__ import annotations

from django.db import transaction

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.users.models import User


@transaction.atomic
def create_user_account(
    *,
    username: str,
    password: str,
    phone: str | None = None,
    first_name: str = "",
    last_name: str = "",
    is_staff: bool = False,
) -> User:
    """
    Create an account.

    Accounts are created by an administrator, never by self-registration —
    which is why the sign-in screen offers no way to make one.
    """
    user = User.objects.create_user(
        username=username.strip(),
        password=password,
        phone=phone or None,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        is_staff=is_staff,
    )
    # snapshot() drops the password hash; see NEVER_SNAPSHOT.
    record_audit_event(action=AuditAction.CREATED, target=user, new_state=snapshot(user))
    return user


@transaction.atomic
def update_user_account(
    *,
    user: User,
    phone: str | None,
    first_name: str,
    last_name: str,
    is_active: bool,
    is_staff: bool,
) -> User:
    """
    Update an account.

    The username is not editable: it is what the audit trail records as the
    actor label, and renaming would make historic events harder to attribute
    even though the label itself is preserved.
    """
    # Re-read from the database: a ModelForm mutates its instance in place
    # during validation, so an in-memory snapshot would already be the new
    # values and the trail would show no change at all.
    before = snapshot(User.objects.get(pk=user.pk))
    user.phone = phone or None
    user.first_name = first_name.strip()
    user.last_name = last_name.strip()
    user.is_active = is_active
    user.is_staff = is_staff
    user.full_clean()
    user.save()

    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=user,
        previous_state=before,
        new_state=snapshot(user),
    )
    return user
