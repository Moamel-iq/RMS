"""User account commands."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.organizations.authorization import (
    organizations_with_organization_permission,
    require_organization_permission,
)
from apps.organizations.models import Organization, OrganizationMembership, Role
from apps.organizations.permissions import sync_user_role_groups
from apps.organizations.security_permissions import MANAGE_USERS
from apps.users.models import User


def _require_user_administrator(*, actor: User | None, organization: Organization) -> None:
    """Require a named organization boundary for account administration."""
    if actor is None:
        return
    require_organization_permission(actor, MANAGE_USERS, organization)


def _require_manageable_target(*, actor: User | None, user: User) -> None:
    """Refuse self, privileged, owner, and cross-organization account edits."""
    if actor is None:
        return
    if actor.pk == user.pk:
        raise ValidationError(_("لا يمكن للمستخدم تعديل حسابه الأمني من هذه الشاشة."))
    if user.is_staff or user.is_superuser:
        raise ValidationError(_("لا تُدار الحسابات الإدارية أو فائقة الصلاحية من هذه الشاشة."))

    allowed = organizations_with_organization_permission(actor, MANAGE_USERS)
    visible = (
        OrganizationMembership.objects.filter(
            user=user, organization__in=allowed, is_active=True
        ).exists()
        or user.branch_memberships.filter(branch__organization__in=allowed, is_active=True).exists()
    )
    if not visible:
        raise ValidationError(_("هذا الحساب خارج نطاق مؤسستك."), code="user_out_of_scope")
    if (
        OrganizationMembership.objects.filter(
            user=user, organization__in=allowed, role=Role.OWNER, is_active=True
        ).exists()
        or user.branch_memberships.filter(
            branch__organization__in=allowed, role=Role.OWNER, is_active=True
        ).exists()
    ):
        raise ValidationError(_("لا يمكن تعديل حساب مالك قائم من هذا المسار."))


@transaction.atomic
def create_user_account(
    *,
    username: str,
    password: str,
    phone: str | None = None,
    first_name: str = "",
    last_name: str = "",
    is_staff: bool = False,
    organization: Organization | None = None,
    actor: User | None = None,
) -> User:
    """
    Create an account.

    Accounts are created by an administrator, never by self-registration —
    which is why the sign-in screen offers no way to make one.
    """
    if is_staff:
        raise ValidationError(
            _("إنشاء حساب موظف إداري من واجهة النظام محظور."), code="staff_creation_forbidden"
        )
    if actor is not None:
        if organization is None:
            raise ValidationError(
                _("يجب اختيار مؤسسة للحساب الجديد."), code="organization_required"
            )
        _require_user_administrator(actor=actor, organization=organization)

    user = User.objects.create_user(
        username=username.strip(),
        password=password,
        phone=phone or None,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        is_staff=False,
    )
    # snapshot() drops the password hash; see NEVER_SNAPSHOT.
    record_audit_event(action=AuditAction.CREATED, target=user, new_state=snapshot(user))
    if organization is not None:
        # A new account must be attributable to one organization before it is
        # shown in any access selector.  VIEWER is a deliberate minimum; a
        # separate access grant is required before it can operate anything.
        membership = OrganizationMembership.objects.create(
            user=user, organization=organization, role=Role.VIEWER, is_active=True
        )
        sync_user_role_groups(user)
        record_audit_event(
            action=AuditAction.ACCESS_GRANTED,
            target=membership,
            new_state=snapshot(membership),
            reason=f"{user} created with viewer access across {organization.code}",
        )
    return user


@transaction.atomic
def update_user_account(
    *,
    user: User,
    phone: str | None,
    first_name: str,
    last_name: str,
    is_active: bool,
    actor: User | None = None,
) -> User:
    """
    Update an account.

    The username is not editable: it is what the audit trail records as the
    actor label, and renaming would make historic events harder to attribute
    even though the label itself is preserved.
    """
    _require_manageable_target(actor=actor, user=user)
    # Re-read from the database: a ModelForm mutates its instance in place
    # during validation, so an in-memory snapshot would already be the new
    # values and the trail would show no change at all.
    before = snapshot(User.objects.get(pk=user.pk))
    user.phone = phone or None
    user.first_name = first_name.strip()
    user.last_name = last_name.strip()
    user.is_active = is_active
    # Staff membership is owned by the separately locked-down Django admin
    # path.  This ERP surface can neither grant nor retain a changed value.
    user.full_clean()
    user.save()

    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=user,
        previous_state=before,
        new_state=snapshot(user),
    )
    return user
