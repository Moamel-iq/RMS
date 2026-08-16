"""
Seed `PURCHASE_PRICE_VARIANCE`, the account an invoice-versus-receipt price
difference is parked in.

Task 2.12 is the task that posts to it, which is why it is seeded now and not
with the other four §15 roles in migration 0011: a role with no posting rule
behind it is a mapping an accounting manager can be asked to fill in for a
workflow that does not exist. `SUPPLIER_ADVANCE` and the two payment-source
roles still wait for 2.15.

**A clearing role, not an expense one.** Task 2.0 §15 proposed `5-02-01-001`,
a cost-of-sales code, and that is superseded here (ADR-022, amended at 2.12).
Two independent reasons, either sufficient:

1. Mechanically, class 5 sets `requires_cost_center`, and a supplier invoice
   has no cost centre to supply. The document belongs to a branch, not to a
   department, and `SupplierInvoiceLine.cost_center` is constrained to
   direct-account lines. `post_supplier_invoice` already refuses at exactly
   this boundary for the payable.
2. Substantively, ADR-022's own rejected alternatives include "post the
   variance to cost of goods sold directly — conflates a purchasing outcome
   with a consumption outcome". Food cost would move for reasons that have
   nothing to do with the kitchen.

The difference is therefore **parked, not classified**. A later, separately
specified period-end process splits the balance between inventory still on
hand and cost of sales for what has been consumed, taking its branch and cost
centre from inventory ownership and consumption rather than from the invoice.
Task 2.12 does not build that process and does not guess at it, so this
balance is expected to be non-zero and is reconciled by invoice, match and
allocation rather than cleared.

Seeded here **and** re-asserted by `sync_system_account_roles` on every
`post_migrate`, for the reason migrations 0008, 0009 and 0011 record: a
test-suite flush truncates data-migration rows and replays only
`post_migrate`, and a database without the vocabulary cannot post.

Seeding a role is not seeding a mapping. Which account carries the variance
stays an organization decision, and posting fails with `account_role_unmapped`
until an accounting manager makes it — against `8-01-03-001`, which
`seed_chart_of_accounts` now creates.
"""

from django.db import migrations

ROLES = (
    (
        "PURCHASE_PRICE_VARIANCE",
        "تسوية فروقات أسعار المشتريات",
        "Purchase price variance clearing",
        "ORGANIZATION",
    ),
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
        ("accounting", "0011_supplier_payable_role"),
    ]

    operations = [
        migrations.RunPython(seed_roles, drop_roles),
    ]
