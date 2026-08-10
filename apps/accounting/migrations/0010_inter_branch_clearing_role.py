"""
Seed the account role the Task 1.5 cross-branch transfer receipt needs.

`INTER_BRANCH_CLEARING` is what keeps each branch's standalone trial balance
balanced when goods cross between them. A single two-line journal debiting the
destination's inventory and crediting the source's in-transit would leave both
branches unbalanced on their own books; two coordinated journals meeting at
this account leave each one balanced and the organization's clearing account
netting to zero for the complete event (ADR-020 §9).

The chart already carries the leaf it will normally be mapped to
(`8-01-01-001`, "حساب وسيط بين الفروع"), seeded with Phase 0. Seeding a role is
still not seeding a mapping: which account carries it stays an organization
decision, and a cross-branch receipt fails with `account_role_unmapped` — and
rolls back every stock effect with it — until an accounting manager makes that
decision deliberately.

Seeded here **and** re-asserted by `sync_system_account_roles` on every
`post_migrate`, for the reason migration 0008 records.
"""

from django.db import migrations

ROLES = (
    ("INTER_BRANCH_CLEARING", "حساب وسيط بين الفروع", "Inter-branch clearing", "ORGANIZATION"),
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
        ("accounting", "0009_operational_account_roles"),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
