"""
Role keys: the eight built-in posts and the posts an organization defines.

A membership's `role` column holds a key. For a built-in post the key is the
`Role` value (`ACCOUNTANT`); for a post the organization defined itself it is
`custom:<organization_id>:<code>`. Both resolve to a Django group named
`role:<key>` (ADR-016), so `roles_granting` and the provenance checks need no
second path: a custom role is a role.

This module is the one place that knows the key's shape. Nothing else parses
it, and nothing else decides whether a key is valid for a place — the grant
services ask `validate_role_key` and store what it returns (ADR-034).
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import Organization, Role, RoleDefinition
from apps.organizations.permissions import group_for_role

#: The prefix that marks an organization-defined key. A built-in value is
#: upper-case and never contains a colon, so the two cannot collide.
CUSTOM_PREFIX = "custom:"

#: The apps whose permissions an organization may hand to a custom role.
#: Django's own (`auth`, `admin`, `contenttypes`, `sessions`) and the
#: foundation apps are deliberately absent: "may create a user" is not a post
#: in a restaurant, it is administration, and it stays with staff.
CONFIGURABLE_APP_LABELS: tuple[str, ...] = (
    "inventory",
    "procurement",
    "kitchen",
    "sales",
    "accounting",
    "hr",
)


def custom_role_key(organization_id: int, code: str) -> str:
    return f"{CUSTOM_PREFIX}{organization_id}:{code}"


def is_custom_key(key: str) -> bool:
    return key.startswith(CUSTOM_PREFIX)


def is_builtin_key(key: str) -> bool:
    return key in Role.values


def parse_custom_key(key: str) -> tuple[int, str] | None:
    """`(organization_id, code)` for a custom key; None for anything else."""
    if not is_custom_key(key):
        return None
    _prefix, _sep, rest = key.partition(":")
    organization_id, sep, code = rest.partition(":")
    if not sep or not organization_id.isdigit() or not code:
        return None
    return int(organization_id), code


def definition_for_key(key: str) -> RoleDefinition | None:
    """The definition behind a custom key, active or not; None for a built-in."""
    parsed = parse_custom_key(key)
    if parsed is None:
        return None
    organization_id, code = parsed
    return RoleDefinition.objects.filter(organization_id=organization_id, code=code).first()


def role_label(key: str) -> str:
    """
    What a screen calls this role.

    A built-in post answers from the enumeration; a custom one from its
    definition. A key that resolves to neither — a definition deleted by hand,
    a typo in a fixture — is shown as it is rather than hidden, because a
    membership in an unknown role is exactly the row an auditor must see.
    """
    if is_builtin_key(key):
        return str(Role(key).label)
    definition = definition_for_key(key)
    return definition.name_ar if definition is not None else key


def validate_role_key(role: Role | str, organization: Organization) -> str:
    """
    The key to store for a grant inside `organization`, or a refusal.

    A built-in post is valid everywhere. A custom post is valid only inside the
    organization that defined it — its group carries permissions that
    organization decided, so granting it elsewhere would let one tenant's
    decisions authorize acts in another — and only while it is active.
    """
    key = role.value if isinstance(role, Role) else str(role)
    if is_builtin_key(key):
        return key

    parsed = parse_custom_key(key)
    if parsed is None:
        raise ValidationError(
            _("الدور %(role)s غير معروف."), code="unknown_role", params={"role": key}
        )
    organization_id, _code = parsed
    if organization_id != organization.pk:
        raise ValidationError(
            _("الدور %(role)s معرَّف في مؤسسة أخرى ولا يُمنح هنا."),
            code="role_belongs_to_another_organization",
            params={"role": key},
        )
    definition = definition_for_key(key)
    if definition is None:
        raise ValidationError(
            _("الدور %(role)s غير معروف."), code="unknown_role", params={"role": key}
        )
    if not definition.is_active:
        raise ValidationError(
            _("الدور %(role)s مؤرشف ولا يُمنح."), code="role_archived", params={"role": key}
        )
    return key


def role_choices(organizations: Iterable[Organization] | None = None) -> list[tuple[str, str]]:
    """
    `(key, label)` pairs for a role selector: the built-in posts, then the
    active custom posts of the given organizations (or of all of them).

    A custom role is labelled with its organization's code when more than one
    organization is in play, because two organizations may both define
    "محاسب" and the person granting has to know whose.
    """
    choices: list[tuple[str, str]] = [(role.value, str(role.label)) for role in Role]
    definitions = RoleDefinition.objects.filter(is_active=True).select_related("organization")
    if organizations is not None:
        definitions = definitions.filter(organization__in=list(organizations))
    rows = list(definitions.order_by("organization__code", "name_ar"))
    several = len({row.organization_id for row in rows}) > 1
    for row in rows:
        label = f"{row.organization.code} — {row.name_ar}" if several else row.name_ar
        choices.append((row.key, label))
    return choices


def module_permission_names() -> set[str]:
    """
    The acts the six modules declare — each module's own `ALL_PERMISSIONS`.

    Not every permission row under those apps: Django also creates `add_`,
    `change_`, `delete_` and `view_` for every model, twelve hundred rows that
    no service checks and that would bury the acts an owner can actually
    grant. The modules' registries are the vocabulary ADR-016 named; the
    catalogue and the matrix follow them.
    """
    names: set[str] = set()
    for app_label in CONFIGURABLE_APP_LABELS:
        module = import_module(f"apps.{app_label}.permissions")
        names.update(getattr(module, "ALL_PERMISSIONS", ()))
    return names


def configurable_permissions() -> list[Permission]:
    """Every act an organization may place on a custom role, in a stable order."""
    wanted = module_permission_names()
    rows = (
        Permission.objects.filter(content_type__app_label__in=CONFIGURABLE_APP_LABELS)
        .select_related("content_type")
        .order_by("content_type__app_label", "codename")
    )
    return [row for row in rows if f"{row.content_type.app_label}.{row.codename}" in wanted]


def resolve_permissions(names: Iterable[str]) -> list[Permission]:
    """
    `app_label.codename` strings to rows, refusing anything unknown or outside
    the configurable apps. Refused rather than skipped: a role saved with fewer
    permissions than the owner ticked would be a silent narrowing nobody asked
    for, and a permission outside the modules is not the owner's to hand out.
    """
    wanted = {str(name).strip() for name in names if str(name).strip()}
    resolved: dict[str, Permission] = {}
    for permission in configurable_permissions():
        name = f"{permission.content_type.app_label}.{permission.codename}"
        if name in wanted:
            resolved[name] = permission
    missing = sorted(wanted - set(resolved))
    if missing:
        raise ValidationError(
            _("صلاحيات غير معروفة أو خارج الوحدات القابلة للتهيئة: %(names)s"),
            code="unknown_permission",
            params={"names": ", ".join(missing)},
        )
    return [resolved[name] for name in sorted(resolved)]


def sync_role_definition_group(definition: RoleDefinition) -> Group:
    """
    Write the definition's permissions into its group, replacing the set.

    Replacing rather than adding: a permission the owner unticked must leave
    the group, or archiving it from the role would be cosmetic. Only this
    function writes a `role:custom:` group; the modules' own
    `sync_role_groups` touch the built-in groups alone.
    """
    group = group_for_role(definition.key)
    group.permissions.set(list(definition.permissions.all()))
    return group


__all__ = [
    "CONFIGURABLE_APP_LABELS",
    "CUSTOM_PREFIX",
    "configurable_permissions",
    "custom_role_key",
    "definition_for_key",
    "is_builtin_key",
    "is_custom_key",
    "parse_custom_key",
    "resolve_permissions",
    "role_choices",
    "role_label",
    "sync_role_definition_group",
    "validate_role_key",
]
