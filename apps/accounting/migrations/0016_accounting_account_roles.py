"""
Seed the Accounting account-role vocabulary and open the `ACCOUNTING` domain.

Task 5.0, checkpoint 1. Four roles, and each one is posted to by a Phase 5
document — the module's standing rule for when a role may arrive, and the
reason `SUPPLIER_ADVANCE` waited two tasks for the payment that used it. A role
with no posting rule behind it is a grant nobody can audit.

Where each one is used:

    accrual document               Dr Expense · Cr ACCRUED_EXPENSES_PAYABLE
    prepayment, when paid          Dr PREPAID_EXPENSE · Cr cash/bank
    prepayment, as consumed        Dr Expense · Cr PREPAID_EXPENSE
    balance sheet, before close    CURRENT_YEAR_EARNINGS, computed from the
                                   statement mapping and never posted monthly
    year-end close                 revenue and expense zeroed, the result to
                                   RETAINED_EARNINGS

`AccountRoleDomain.ACCOUNTING` is added in the same change. It is the first
domain whose posting rules are about the organization's **own** financial
administration rather than a trading module's: nothing is bought, sold,
produced or moved when an expense is accrued at month end. The domain is a
`TextChoices` value rather than a table row, so there is nothing to seed for it
here; the roles simply reference it and the choices validation now accepts
them (ADR-030 §6, ADR-031 §5).

Re-asserted by `sync_system_account_roles` on every `post_migrate`, for the
reason migrations 0008 through 0015 all record: a test-suite flush truncates
data-migration rows and replays only `post_migrate`, and a database without the
vocabulary cannot post.

**No mapping is created here.** Which account carries `RETAINED_EARNINGS` is
the organization's decision recorded in `OrganizationAccountMapping`, never
something a migration decides. `seed_chart_of_accounts` creates the accounts
those mappings will point at.
"""

from django.db import migrations, models

#: `AccountRoleDomain` gained `ACCOUNTING`. Choices are validated by Django
#: rather than by the database, so these two `AlterField` operations change no
#: column — they keep the migration state honest so `makemigrations --check`
#: stays clean, and they come **before** the seed so the rows below are written
#: against a field that already accepts their domain.
DOMAIN_CHOICES = [
    ("INVENTORY", "المخزون"),
    ("PURCHASING", "المشتريات"),
    ("SALES", "المبيعات"),
    ("ACCOUNTING", "المحاسبة"),
]

ROLES = (
    (
        "ACCRUED_EXPENSES_PAYABLE",
        "مصروفات مستحقة الدفع",
        "Accrued expenses payable",
        "ORGANIZATION",
    ),
    ("PREPAID_EXPENSE", "مصروفات مدفوعة مقدماً", "Prepaid expenses", "ORGANIZATION"),
    ("CURRENT_YEAR_EARNINGS", "نتيجة السنة الحالية", "Current year earnings", "ORGANIZATION"),
    ("RETAINED_EARNINGS", "الأرباح المحتجزة", "Retained earnings", "ORGANIZATION"),
)


def seed_roles(apps, schema_editor):
    AccountRole = apps.get_model("accounting", "AccountRole")
    for code, name_ar, name_en, mapping_scope in ROLES:
        AccountRole.objects.update_or_create(
            code=code,
            defaults={
                "name_ar": name_ar,
                "name_en": name_en,
                "domain": "ACCOUNTING",
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
        ("accounting", "0015_sales_account_roles"),
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
