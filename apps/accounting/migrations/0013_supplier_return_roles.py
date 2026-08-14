"""
Seed `SUPPLIER_RETURN_CLEARING` and `PURCHASE_RETURN_VARIANCE`.

Task 2.13 posts to the first and names the second.

**Why the second is seeded early**, against this module's usual practice. Every
migration since 0008 records the same rule — a role with no posting rule behind
it is a mapping an accounting manager can be asked to fill in for a workflow
that does not exist — and `SUPPLIER_ADVANCE` and the two payment-source roles
are still waiting on it. `PURCHASE_RETURN_VARIANCE` is the exception, and the
reason is specific rather than convenient.

A supplier return is only half an event. Goods leave, and their book value
waits in `SUPPLIER_RETURN_CLEARING` for a credit note that says what the
supplier will actually allow. The difference between those two figures is the
return variance, and it is recognised at the credit note (Task 2.14), not at
the return. Naming the destination now is what stops 2.14 reaching for
`PURCHASE_PRICE_VARIANCE` — a different economic fact, invoice-versus-receipt
on goods coming *in*, whose report would then quietly contain the arithmetic of
having averaged two deliveries together and unwound one of them.

So: seeded as vocabulary, deliberately **unmapped**, and posted to by nothing
in this task. `verify_parked_return_variance` asserts the account is empty
until 2.14 gives it a rule.

Both roles are re-asserted by `sync_system_account_roles` on every
`post_migrate`, for the reason migrations 0008, 0009, 0011 and 0012 record: a
test-suite flush truncates data-migration rows and replays only
`post_migrate`, and a database without the vocabulary cannot post.

Accounts: `8-01-04-001 تسوية مرتجعات الموردين` for the clearing, and
`7-09-04-001 فروقات إرجاع المشتريات` for the variance — the second beside the
chart's other bidirectional difference accounts, because a return that credits
more than it removed is a gain and one that credits less is a loss, and neither
is negative spending (ADR-022 §2).
"""

from django.db import migrations

ROLES = (
    (
        "SUPPLIER_RETURN_CLEARING",
        "تسوية مرتجعات الموردين",
        "Supplier return clearing",
        "ORGANIZATION",
    ),
    (
        "PURCHASE_RETURN_VARIANCE",
        "فروقات إرجاع المشتريات",
        "Purchase return variance",
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

    A system role code is reserved forever and a posted journal line may
    already cite an account one of these resolved. Removing the row would
    orphan that explanation.
    """
    AccountRole = apps.get_model("accounting", "AccountRole")
    AccountRole.objects.filter(code__in=[code for code, *_rest in ROLES]).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0012_purchase_price_variance_role"),
    ]

    operations = [
        migrations.RunPython(seed_roles, drop_roles),
    ]
