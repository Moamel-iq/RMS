"""
Seed the Sales account-role vocabulary and open the `SALES` domain.

Task 4.0. Eleven roles, and every one of them is posted to by Phase 4 — the
module's standing rule for when a role may arrive, and the reason
`SUPPLIER_ADVANCE` waited two tasks for the payment that used it. A role with
no posting rule behind it is a grant nobody can audit.

Where each one is used:

    daily sale, cash or card       SALES_CASH_ON_HAND / SALES_CARD_CLEARING
                                   SALES_DISCOUNT, SALES_REVENUE
    daily sale, delivery app       DELIVERY_APP_RECEIVABLE, SALES_DISCOUNT,
                                   DELIVERY_COMMISSION_EXPENSE,
                                   DELIVERY_OTHER_FEE_EXPENSE, SALES_REVENUE
    return or cancellation         SALES_RETURNS against the original tender
    application settlement         SALES_SETTLEMENT_BANK / SALES_CASH_ON_HAND,
                                   DELIVERY_SETTLEMENT_VARIANCE,
                                   DELIVERY_APP_RECEIVABLE
    cashier closing                SALES_CASH_OVER_SHORT and nothing else

`AccountRoleDomain.SALES` is added in the same change. The domain is a
`TextChoices` value rather than a table row, so there is nothing to seed for
it here; the roles simply reference it and the choices validation now accepts
them.

Re-asserted by `sync_system_account_roles` on every `post_migrate`, for the
reason migrations 0008 through 0014 all record: a test-suite flush truncates
data-migration rows and replays only `post_migrate`, and a database without
the vocabulary cannot post.

**No mapping is created here.** Which account carries `SALES_REVENUE` is the
organization's decision recorded in `OrganizationAccountMapping`, never
something a migration decides. `seed_chart_of_accounts` creates the accounts
those mappings will point at.
"""

from django.db import migrations, models

#: `AccountRoleDomain` gained `SALES`. Choices are validated by Django rather
#: than by the database, so these two `AlterField` operations change no column
#: — they keep the migration state honest so `makemigrations --check` stays
#: clean, and they come **before** the seed so the rows below are written
#: against a field that already accepts their domain.
DOMAIN_CHOICES = [
    ("INVENTORY", "المخزون"),
    ("PURCHASING", "المشتريات"),
    ("SALES", "المبيعات"),
]

ROLES = (
    ("SALES_REVENUE", "إيرادات المبيعات", "Sales revenue", "ORGANIZATION"),
    ("SALES_DISCOUNT", "خصومات المبيعات", "Sales discounts", "ORGANIZATION"),
    ("SALES_RETURNS", "مردودات المبيعات", "Sales returns", "ORGANIZATION"),
    ("SALES_CASH_ON_HAND", "نقدية المبيعات", "Sales cash on hand", "ORGANIZATION"),
    ("SALES_CARD_CLEARING", "تسوية مبيعات البطاقات", "Card clearing", "ORGANIZATION"),
    (
        "DELIVERY_APP_RECEIVABLE",
        "ذمم تطبيقات التوصيل",
        "Delivery application receivable",
        "ORGANIZATION",
    ),
    (
        "DELIVERY_COMMISSION_EXPENSE",
        "عمولات تطبيقات التوصيل",
        "Delivery commission expense",
        "ORGANIZATION",
    ),
    (
        "DELIVERY_OTHER_FEE_EXPENSE",
        "رسوم تطبيقات التوصيل الأخرى",
        "Delivery other fee expense",
        "ORGANIZATION",
    ),
    (
        "DELIVERY_SETTLEMENT_VARIANCE",
        "فروقات تسويات التطبيقات",
        "Delivery settlement variance",
        "ORGANIZATION",
    ),
    (
        "SALES_SETTLEMENT_BANK",
        "تحصيلات التطبيقات عبر المصرف",
        "Settlement bank receipts",
        "ORGANIZATION",
    ),
    ("SALES_CASH_OVER_SHORT", "فروقات الصندوق", "Cash over and short", "ORGANIZATION"),
)


def seed_roles(apps, schema_editor):
    AccountRole = apps.get_model("accounting", "AccountRole")
    for code, name_ar, name_en, mapping_scope in ROLES:
        AccountRole.objects.update_or_create(
            code=code,
            defaults={
                "name_ar": name_ar,
                "name_en": name_en,
                "domain": "SALES",
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
        ("accounting", "0014_supplier_payment_roles"),
    ]

    operations = [
        migrations.AlterField(
            model_name="accountrole",
            name="domain",
            field=models.CharField(choices=DOMAIN_CHOICES, max_length=20, verbose_name="domain"),
        ),
        migrations.AlterField(
            model_name="historicalaccountrole",
            name="domain",
            field=models.CharField(choices=DOMAIN_CHOICES, max_length=20, verbose_name="domain"),
        ),
        migrations.RunPython(seed_roles, unseed_roles),
    ]
