from apps.organizations.models import Role

APP_LABEL = "supplier_quotes"
VIEW = f"{APP_LABEL}.view_supplierquote"
ADD = f"{APP_LABEL}.add_supplierquote"
CHANGE = f"{APP_LABEL}.change_supplierquote"
DELETE = f"{APP_LABEL}.delete_supplierquote"
DOWNLOAD = f"{APP_LABEL}.download_supplier_quote_attachment"
ALL = frozenset({VIEW, ADD, CHANGE, DELETE, DOWNLOAD})
ROLE_PERMISSIONS = {
    Role.OWNER.value: ALL,
    Role.MANAGER.value: ALL,
    Role.PURCHASING.value: frozenset({VIEW, ADD, CHANGE, DOWNLOAD}),
    Role.ACCOUNTANT.value: frozenset({VIEW, DOWNLOAD}),
    Role.ACCOUNTING_MANAGER.value: frozenset({VIEW, DOWNLOAD}),
    Role.STOREKEEPER.value: frozenset({VIEW}),
    Role.CASHIER.value: frozenset(),
    Role.VIEWER.value: frozenset({VIEW}),
}


def sync_role_groups() -> None:
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    known = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label=APP_LABEL)
    }
    ours = set(known.values())
    for role, names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in names if name in known}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)
