"""
Seed `SUPPLIER_PAYABLE`, and open the `PURCHASING` role domain.

Task 2.0 §15 names five procurement roles. Exactly one is seeded here: the one
Task 2.10 actually posts to. `PURCHASE_PRICE_VARIANCE`, `SUPPLIER_ADVANCE`,
`SUPPLIER_PAYMENT_CASH` and `SUPPLIER_PAYMENT_BANK` arrive with the tasks whose
posting rules use them (2.12 and 2.15), because a role with no posting rule
behind it is a mapping an accounting manager can be asked to fill in for a
workflow that does not exist.

`PURCHASING` is a new `AccountRoleDomain` value. The enum's own docstring
invited it — "Purchases, Sales, and Payroll add their own values when their
posting rules arrive, never before" — and this is that moment. A supplier
payable is not an inventory concept, and filing it under `INVENTORY` would make
the domain column a label instead of a fact.

Seeded here **and** re-asserted by `sync_system_account_roles` on every
`post_migrate`, for the reason migrations 0008 and 0009 record: a test-suite
flush truncates data-migration rows and replays only `post_migrate`, and a
database without the vocabulary cannot post.

Seeding a role is not seeding a mapping. Which account carries the payable
stays an organization decision, and posting fails with `account_role_unmapped`
until an accounting manager makes it.
"""

from django.db import migrations, models

ROLES = (("SUPPLIER_PAYABLE", "ذمم الموردين", "Supplier payable", "ORGANIZATION"),)


def seed_roles(apps, schema_editor):
    AccountRole = apps.get_model("accounting", "AccountRole")
    for code, name_ar, name_en, mapping_scope in ROLES:
        AccountRole.objects.update_or_create(
            code=code,
            defaults={
                "name_ar": name_ar,
                "name_en": name_en,
                "domain": "PURCHASING",
                "mapping_scope": mapping_scope,
                "is_system": True,
                "is_active": True,
            },
        )


def drop_roles(apps, schema_editor):
    """
    Deactivate rather than delete.

    A system role code is reserved forever (the model says so and a trigger
    backs it up), and a posted journal line may already cite an account this
    role resolved. Removing the row would orphan that explanation.
    """
    AccountRole = apps.get_model("accounting", "AccountRole")
    AccountRole.objects.filter(code__in=[code for code, *_rest in ROLES]).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0010_inter_branch_clearing_role"),
    ]

    operations = [
        migrations.AlterField(
            model_name="accountrole",
            name="domain",
            field=models.CharField(
                choices=[("INVENTORY", "المخزون"), ("PURCHASING", "المشتريات")],
                max_length=20,
                verbose_name="domain",
            ),
        ),
        migrations.AlterField(
            model_name="historicalaccountrole",
            name="domain",
            field=models.CharField(
                choices=[("INVENTORY", "المخزون"), ("PURCHASING", "المشتريات")],
                max_length=20,
                verbose_name="domain",
            ),
        ),
        migrations.RunPython(seed_roles, drop_roles),
    ]
