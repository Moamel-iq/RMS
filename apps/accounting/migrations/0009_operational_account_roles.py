"""
Seed the two account roles the Task 1.4 operational documents need.

`GOODS_RECEIVED_NOT_INVOICED` is the credit side of a physical receipt that no
supplier invoice has caught up with yet — a clearing liability, cleared by
Procurement in Phase 2. `INVENTORY_CONSUMPTION` is the debit side of stock
leaving custody for good.

Seeded here **and** re-asserted by `sync_system_account_roles` on every
`post_migrate`, for the reason migration 0008 records: a test-suite flush
truncates data-migration rows and replays only post_migrate, and a database
without the vocabulary cannot post.

Seeding a role is not seeding a mapping. Which account carries either of these
stays an organization decision an accounting manager makes deliberately, and
posting fails with `account_role_unmapped` until they do.
"""

from django.db import migrations

ROLES = (
    (
        "GOODS_RECEIVED_NOT_INVOICED",
        "بضاعة مستلمة غير مفوترة",
        "Goods received not invoiced",
        "ORGANIZATION",
    ),
    ("INVENTORY_CONSUMPTION", "استهلاك المخزون", "Inventory consumption", "ITEM"),
)


def seed_roles(apps, schema_editor):
    AccountRole = apps.get_model("accounting", "AccountRole")
    for code, name_ar, name_en, mapping_scope in ROLES:
        AccountRole.objects.update_or_create(
            code=code,
            defaults={
                "name_ar": name_ar,
                "name_en": name_en,
                "domain": "INVENTORY",
                "mapping_scope": mapping_scope,
                "is_system": True,
                "is_active": True,
            },
        )


def unseed_roles(apps, schema_editor):  # pragma: no cover - rollback path
    """
    Delete directly rather than through the ORM's normal path: the reserved
    trigger refuses a DELETE on a system role, and a rollback is the one
    context where removing the vocabulary is the point.
    """
    schema_editor.execute(
        "ALTER TABLE accounting_accountrole DISABLE TRIGGER accounting_system_role_reserved"
    )
    AccountRole = apps.get_model("accounting", "AccountRole")
    AccountRole.objects.filter(code__in=[code for code, *_rest in ROLES]).delete()
    schema_editor.execute(
        "ALTER TABLE accounting_accountrole ENABLE TRIGGER accounting_system_role_reserved"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0008_account_role_seed_and_guards"),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
