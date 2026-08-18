"""
Cross the DRAFT-only boundary, and put something stronger in its place.

Task 3.4 shipped `production_batch_is_draft_only_until_task_3_5`, a check
constraint named after the task that had to remove it so nobody would delete it
while tidying. This is that task, and this is that removal — additive, in its
own migration, and not by editing 0010.

What replaces it is not weaker. The old rule refused one status; the five
constraints added here refuse every **half-posted** row:

* posting evidence is all present or all absent — no stock entry without a
  value, no value without a number;
* `input_value = output_value`, so value conservation (RCP-034) is a property
  of the schema and not of one code path;
* reversal evidence is all present or all absent;
* a posting key belongs to a posting, so a draft cannot hold a key consumed by
  a command that never ran;
* the posting key is unique per organization, partial so that every draft's
  empty string does not collide.

Also here: the two posting permissions Task 3.4 deliberately withheld, and
`ProductionBatchAllocation` — which lot, out of which bin, each actual row came
from.
"""

import django.db.models.deletion
import simple_history.models
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0014_supplier_payment_roles'),
        ('inventory', '0020_procurement_import_kinds'),
        ('kitchen', '0016_production_actual_order_is_positive'),
        ('organizations', '0004_inventory_master_data_and_warehouse_scope'),
        ('units', '0002_historicalunitofmeasure'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='HistoricalProductionBatchAllocation',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('created_at', models.DateTimeField(blank=True, db_index=True, editable=False, verbose_name='created at')),
                ('updated_at', models.DateTimeField(blank=True, editable=False, verbose_name='updated at')),
                ('allocation_order', models.PositiveIntegerField(verbose_name='allocation order')),
                ('base_quantity', models.DecimalField(decimal_places=6, max_digits=21, verbose_name='quantity')),
                ('consumed_value', models.DecimalField(blank=True, decimal_places=3, max_digits=18, null=True, verbose_name='consumed value')),
                ('public_id', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, verbose_name='public id')),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
            ],
            options={
                'verbose_name': 'historical production allocation',
                'verbose_name_plural': 'historical production allocations',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name='ProductionBatchAllocation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('allocation_order', models.PositiveIntegerField(verbose_name='allocation order')),
                ('base_quantity', models.DecimalField(decimal_places=6, max_digits=21, verbose_name='quantity')),
                ('consumed_value', models.DecimalField(blank=True, decimal_places=3, max_digits=18, null=True, verbose_name='consumed value')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='public id')),
            ],
            options={
                'verbose_name': 'production allocation',
                'verbose_name_plural': 'production allocations',
                'ordering': ['actual', 'allocation_order'],
            },
        ),
        migrations.AlterModelOptions(
            name='productionbatch',
            options={'ordering': ['-planned_business_date', '-id'], 'permissions': [('create_production_batch', 'Can draft and edit production batches'), ('post_production_batch', 'Can post production batches'), ('reverse_production_batch', 'Can reverse posted production batches')], 'verbose_name': 'production batch', 'verbose_name_plural': 'production batches'},
        ),
        migrations.RemoveConstraint(
            model_name='productionbatch',
            name='production_batch_is_draft_only_until_task_3_5',
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='input_value',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=18, null=True, verbose_name='consumed value'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='journal_entry',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='accounting.journalentry', verbose_name='journal entry'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='output_item',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='inventory.inventoryitem', verbose_name='output item'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='output_lot',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='inventory.inventorylot', verbose_name='output lot'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='output_movement',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='inventory.stockmovement', verbose_name='output movement'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='output_value',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=18, null=True, verbose_name='output value'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='post_idempotency_key',
            field=models.CharField(blank=True, max_length=128, verbose_name='posting key'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='post_request_fingerprint',
            field=models.CharField(blank=True, max_length=64, verbose_name='posting fingerprint'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='posted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='posted at'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='posted_by',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='posted by'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='posting_rule_version',
            field=models.CharField(blank=True, max_length=32, verbose_name='posting rule version'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='reversal_journal_entry',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='accounting.journalentry', verbose_name='reversing journal entry'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='reversal_reason',
            field=models.CharField(blank=True, max_length=200, verbose_name='reversal reason'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='reversal_stock_entry',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='inventory.stockledgerentry', verbose_name='reversing stock posting'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='reversed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='reversed at'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='reversed_by',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='reversed by'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatch',
            name='stock_entry',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='inventory.stockledgerentry', verbose_name='stock posting'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='input_value',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=18, null=True, verbose_name='consumed value'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='journal_entry',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batches', to='accounting.journalentry', verbose_name='journal entry'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='output_item',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batch_outputs', to='inventory.inventoryitem', verbose_name='output item'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='output_lot',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batches', to='inventory.inventorylot', verbose_name='output lot'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='output_movement',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batch_outputs', to='inventory.stockmovement', verbose_name='output movement'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='output_value',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=18, null=True, verbose_name='output value'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='post_idempotency_key',
            field=models.CharField(blank=True, max_length=128, verbose_name='posting key'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='post_request_fingerprint',
            field=models.CharField(blank=True, max_length=64, verbose_name='posting fingerprint'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='posted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='posted at'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='posted_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batches_posted', to=settings.AUTH_USER_MODEL, verbose_name='posted by'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='posting_rule_version',
            field=models.CharField(blank=True, max_length=32, verbose_name='posting rule version'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='reversal_journal_entry',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batch_reversals', to='accounting.journalentry', verbose_name='reversing journal entry'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='reversal_reason',
            field=models.CharField(blank=True, max_length=200, verbose_name='reversal reason'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='reversal_stock_entry',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batch_reversals', to='inventory.stockledgerentry', verbose_name='reversing stock posting'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='reversed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='reversed at'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='reversed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batches_reversed', to=settings.AUTH_USER_MODEL, verbose_name='reversed by'),
        ),
        migrations.AddField(
            model_name='productionbatch',
            name='stock_entry',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_batches', to='inventory.stockledgerentry', verbose_name='stock posting'),
        ),
        migrations.AddConstraint(
            model_name='productionbatch',
            constraint=models.UniqueConstraint(condition=models.Q(('post_idempotency_key', ''), _negated=True), fields=('organization', 'post_idempotency_key'), name='production_batch_post_key_unique_per_organization'),
        ),
        migrations.AddConstraint(
            model_name='productionbatch',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('input_value__isnull', True), ('output_item__isnull', True), ('output_movement__isnull', True), ('output_value__isnull', True), ('posted_at__isnull', True), ('status', 'DRAFT'), ('stock_entry__isnull', True)), models.Q(models.Q(('status', 'DRAFT'), _negated=True), models.Q(('number', ''), _negated=True), ('input_value__isnull', False), ('output_item__isnull', False), ('output_movement__isnull', False), ('output_value__isnull', False), ('posted_at__isnull', False), ('stock_entry__isnull', False)), _connector='OR'), name='production_batch_posting_evidence_is_complete'),
        ),
        migrations.AddConstraint(
            model_name='productionbatch',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('input_value__isnull', True), ('output_value__isnull', True)), ('input_value', models.F('output_value')), _connector='OR'), name='production_batch_conserves_value'),
        ),
        migrations.AddConstraint(
            model_name='productionbatch',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('reversal_stock_entry__isnull', False), ('reversed_at__isnull', False), ('status', 'REVERSED'), models.Q(('reversal_reason', ''), _negated=True)), models.Q(models.Q(('status', 'REVERSED'), _negated=True), ('reversal_reason', ''), ('reversal_stock_entry__isnull', True), ('reversed_at__isnull', True)), _connector='OR'), name='production_batch_reversal_evidence_is_complete'),
        ),
        migrations.AddConstraint(
            model_name='productionbatch',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('post_idempotency_key', ''), ('status', 'DRAFT')), models.Q(models.Q(('status', 'DRAFT'), _negated=True), models.Q(('post_idempotency_key', ''), _negated=True)), _connector='OR'), name='production_batch_posting_key_belongs_to_a_posting'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatchallocation',
            name='actual',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='kitchen.productionbatchactualline', verbose_name='actual line'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatchallocation',
            name='history_user',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicalproductionbatchallocation',
            name='location',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='inventory.stocklocation', verbose_name='location'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatchallocation',
            name='lot',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='inventory.inventorylot', verbose_name='lot'),
        ),
        migrations.AddField(
            model_name='historicalproductionbatchallocation',
            name='movement',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='inventory.stockmovement', verbose_name='stock movement'),
        ),
        migrations.AddField(
            model_name='productionbatchallocation',
            name='actual',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='allocations', to='kitchen.productionbatchactualline', verbose_name='actual line'),
        ),
        migrations.AddField(
            model_name='productionbatchallocation',
            name='location',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_allocations', to='inventory.stocklocation', verbose_name='location'),
        ),
        migrations.AddField(
            model_name='productionbatchallocation',
            name='lot',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_allocations', to='inventory.inventorylot', verbose_name='lot'),
        ),
        migrations.AddField(
            model_name='productionbatchallocation',
            name='movement',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='production_allocations', to='inventory.stockmovement', verbose_name='stock movement'),
        ),
        migrations.AddConstraint(
            model_name='productionbatchallocation',
            constraint=models.UniqueConstraint(fields=('actual', 'allocation_order'), name='production_allocation_order_unique_per_actual'),
        ),
        migrations.AddConstraint(
            model_name='productionbatchallocation',
            constraint=models.CheckConstraint(condition=models.Q(('allocation_order__gte', 1)), name='production_allocation_order_is_positive'),
        ),
        migrations.AddConstraint(
            model_name='productionbatchallocation',
            constraint=models.CheckConstraint(condition=models.Q(('base_quantity__gt', Decimal('0'))), name='production_allocation_quantity_is_positive'),
        ),
        migrations.AddConstraint(
            model_name='productionbatchallocation',
            constraint=models.UniqueConstraint(fields=('actual', 'lot', 'location'), name='production_allocation_position_unique_per_actual', nulls_distinct=False),
        ),
        migrations.AddConstraint(
            model_name='productionbatchallocation',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('consumed_value__isnull', True), ('movement__isnull', True)), models.Q(('consumed_value__isnull', False), ('movement__isnull', False)), _connector='OR'), name='production_allocation_posting_evidence_is_complete'),
        ),
    ]
