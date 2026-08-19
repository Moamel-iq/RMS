"""
Account metadata and the financial-statement mapping.

Task 5.0, checkpoint 1. Three columns on `Account` and one new model, all of
them additions — nothing already posted moves, and every default is the value
that leaves an existing row meaning exactly what it meant before:

    manual_posting_policy  ALLOWED  every account accepted a manual line
                                    before this column existed (ADR-029 §2)
    is_system              False    no account was protected before
    archived_at            NULL     for an active account, which is the only
                                    state the `if and only if` constraint
                                    would otherwise accept

`archived_at` is the one that needs help. An account archived before this
migration has `is_active = False` and no archive date, which the new
constraint refuses — so `stamp_existing_archives` fills it in **before** the
constraint is added. `updated_at` is used as the value rather than "now": the
row's last change is the closest honest record of when it was withdrawn, and
stamping today's date would assert that a two-year-old archival happened this
morning.

Historical rows are deliberately left alone. A history row records what the
account looked like at the time, and at that time the column did not exist;
back-filling it would put a fact into the past that nobody recorded.
"""

import django.db.models.deletion
import simple_history.models
from django.conf import settings
from django.db import migrations, models
from django.db.models import F


def stamp_existing_archives(apps, schema_editor):
    """
    Give every already-archived account the archive date it never had.

    A single UPDATE, and `.update()` rather than a save loop on purpose:
    `updated_at` is `auto_now`, so saving each row would overwrite the very
    value being copied out of it.
    """
    Account = apps.get_model("accounting", "Account")
    Account.objects.filter(is_active=False, archived_at__isnull=True).update(
        archived_at=F("updated_at")
    )


def clear_archive_stamps(apps, schema_editor):
    """
    Reverse: drop the stamps again.

    The column is about to be removed by the reverse of the `AddField` above,
    so this only has to leave the table in a state the older constraint set
    accepts — and the older set had no opinion about `archived_at` at all.
    """
    Account = apps.get_model("accounting", "Account")
    Account.objects.filter(is_active=False).update(archived_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0016_accounting_account_roles'),
        ('organizations', '0004_inventory_master_data_and_warehouse_scope'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AccountReportMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('statement_group', models.CharField(choices=[('ASSET', 'الأصول'), ('LIABILITY', 'الالتزامات'), ('EQUITY', 'حقوق الملكية'), ('REVENUE', 'الإيرادات'), ('COST_OF_SALES', 'كلفة المبيعات'), ('OPERATING_EXPENSE', 'المصروفات التشغيلية'), ('OTHER_INCOME', 'إيرادات أخرى'), ('OTHER_EXPENSE', 'مصروفات أخرى')], max_length=24, verbose_name='statement group')),
                ('presentation_section', models.CharField(choices=[('CURRENT', 'متداول'), ('NON_CURRENT', 'غير متداول'), ('NOT_APPLICABLE', 'لا ينطبق')], default='NOT_APPLICABLE', max_length=16, verbose_name='presentation section')),
                ('display_order', models.PositiveIntegerField(default=0, verbose_name='display order')),
                ('is_active', models.BooleanField(default=True, verbose_name='active')),
            ],
            options={
                'verbose_name': 'account report mapping',
                'verbose_name_plural': 'account report mappings',
                'ordering': ['organization__code', 'statement_group', 'display_order', 'account__code'],
                'permissions': [('manage_report_mappings', 'Can map accounts to financial-statement groups')],
            },
        ),
        migrations.CreateModel(
            name='HistoricalAccountReportMapping',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('created_at', models.DateTimeField(blank=True, db_index=True, editable=False, verbose_name='created at')),
                ('updated_at', models.DateTimeField(blank=True, editable=False, verbose_name='updated at')),
                ('statement_group', models.CharField(choices=[('ASSET', 'الأصول'), ('LIABILITY', 'الالتزامات'), ('EQUITY', 'حقوق الملكية'), ('REVENUE', 'الإيرادات'), ('COST_OF_SALES', 'كلفة المبيعات'), ('OPERATING_EXPENSE', 'المصروفات التشغيلية'), ('OTHER_INCOME', 'إيرادات أخرى'), ('OTHER_EXPENSE', 'مصروفات أخرى')], max_length=24, verbose_name='statement group')),
                ('presentation_section', models.CharField(choices=[('CURRENT', 'متداول'), ('NON_CURRENT', 'غير متداول'), ('NOT_APPLICABLE', 'لا ينطبق')], default='NOT_APPLICABLE', max_length=16, verbose_name='presentation section')),
                ('display_order', models.PositiveIntegerField(default=0, verbose_name='display order')),
                ('is_active', models.BooleanField(default=True, verbose_name='active')),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
            ],
            options={
                'verbose_name': 'historical account report mapping',
                'verbose_name_plural': 'historical account report mappings',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.AlterModelOptions(
            name='account',
            options={'ordering': ['organization__code', 'code'], 'permissions': [('manage_accounts', 'Can create and archive accounts'), ('view_chart_of_accounts', 'Can read the chart of accounts'), ('manage_chart_of_accounts', 'Can create, amend and archive chart accounts')], 'verbose_name': 'account', 'verbose_name_plural': 'accounts'},
        ),
        migrations.AddField(
            model_name='account',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='archived at'),
        ),
        migrations.AddField(
            model_name='account',
            name='is_system',
            field=models.BooleanField(default=False, help_text='Seeded reference data. A user may not repurpose it.', verbose_name='system account'),
        ),
        migrations.AddField(
            model_name='account',
            name='manual_posting_policy',
            field=models.CharField(choices=[('ALLOWED', 'متاح'), ('RESTRICTED', 'مقيّد'), ('FORBIDDEN', 'ممنوع')], default='ALLOWED', help_text='Whether a hand-written journal line may name this account.', max_length=16, verbose_name='manual posting policy'),
        ),
        migrations.AddField(
            model_name='historicalaccount',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='archived at'),
        ),
        migrations.AddField(
            model_name='historicalaccount',
            name='is_system',
            field=models.BooleanField(default=False, help_text='Seeded reference data. A user may not repurpose it.', verbose_name='system account'),
        ),
        migrations.AddField(
            model_name='historicalaccount',
            name='manual_posting_policy',
            field=models.CharField(choices=[('ALLOWED', 'متاح'), ('RESTRICTED', 'مقيّد'), ('FORBIDDEN', 'ممنوع')], default='ALLOWED', help_text='Whether a hand-written journal line may name this account.', max_length=16, verbose_name='manual posting policy'),
        ),
        # Before the constraint, never after: an account archived under the
        # old schema has no archive date, and `archived_at IS NOT NULL` is
        # exactly what the constraint below is about to demand of it.
        migrations.RunPython(stamp_existing_archives, clear_archive_stamps),
        migrations.AddConstraint(
            model_name='account',
            constraint=models.CheckConstraint(condition=models.Q(('is_postable', True), ('manual_posting_policy', 'ALLOWED'), _connector='OR'), name='account_only_postable_restricts_manual_posting'),
        ),
        migrations.AddConstraint(
            model_name='account',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('is_active', True), ('archived_at__isnull', True)), models.Q(('is_active', False), ('archived_at__isnull', False)), _connector='OR'), name='account_archived_at_iff_inactive'),
        ),
        migrations.AddField(
            model_name='accountreportmapping',
            name='account',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='report_mappings', to='accounting.account', verbose_name='account'),
        ),
        migrations.AddField(
            model_name='accountreportmapping',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='report_mappings', to='organizations.organization', verbose_name='organization'),
        ),
        migrations.AddField(
            model_name='historicalaccountreportmapping',
            name='account',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='accounting.account', verbose_name='account'),
        ),
        migrations.AddField(
            model_name='historicalaccountreportmapping',
            name='history_user',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicalaccountreportmapping',
            name='organization',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='organizations.organization', verbose_name='organization'),
        ),
        migrations.AddIndex(
            model_name='accountreportmapping',
            index=models.Index(fields=['organization', 'statement_group', 'is_active'], name='report_mapping_group_idx'),
        ),
        migrations.AddConstraint(
            model_name='accountreportmapping',
            constraint=models.UniqueConstraint(fields=('organization', 'account'), name='report_mapping_unique_per_account'),
        ),
        migrations.AddConstraint(
            model_name='accountreportmapping',
            constraint=models.CheckConstraint(condition=models.Q(('presentation_section', 'NOT_APPLICABLE'), ('statement_group__in', ['ASSET', 'LIABILITY']), _connector='OR'), name='report_mapping_section_only_on_balance_sheet_groups'),
        ),
    ]
