"""
Seed `SUPPLIER_PAYMENT_CASH`, `SUPPLIER_PAYMENT_BANK` and `SUPPLIER_ADVANCE`.

Task 2.15 posts to all three, which is the module's standing rule for when a
role may arrive: a payment's journal is

    Dr  Supplier payable    the allocated amount
    Dr  Supplier advance    the unallocated remainder, where any
        Cr  Cash or bank    the full amount

with the source resolved by the payment's `method` through the two payment
roles (PRC-056) — never a hard-coded account id — and the remainder an
**asset** rather than a negative payable (PRC-055): cash handed over before
an invoice exists to net it against is a different fact from owing less, and
folding the two together would make the aging report lie about both. It is
also a different fact from a credit note's standing credit, which is the
supplier owing money *back* and lives in the payable as a debit — which is
why `SUPPLIER_ADVANCE` was refused for that role at Task 2.14.

All three are re-asserted by `sync_system_account_roles` on every
`post_migrate`, for the reason migrations 0008 through 0013 record: a
test-suite flush truncates data-migration rows and replays only
`post_migrate`, and a database without the vocabulary cannot post.

Accounts: the Phase 0 chart's `1-01-01-001` cash and `1-01-02-001` bank for
the sources, and the new `1-04-01-001 سلف الموردين` for the advance —
mappings the organization records deliberately, never something a migration
decides.
"""

from django.db import migrations

ROLES = (
    ("SUPPLIER_PAYMENT_CASH", "دفعات الموردين نقداً", "Supplier payments — cash", "ORGANIZATION"),
    (
        "SUPPLIER_PAYMENT_BANK",
        "دفعات الموردين عبر المصرف",
        "Supplier payments — bank",
        "ORGANIZATION",
    ),
    ("SUPPLIER_ADVANCE", "سلف الموردين", "Supplier advances", "ORGANIZATION"),
)


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


def unseed_roles(apps, schema_editor):
    AccountRole = apps.get_model("accounting", "AccountRole")
    AccountRole.objects.filter(code__in=[code for code, *_ in ROLES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0013_supplier_return_roles"),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
