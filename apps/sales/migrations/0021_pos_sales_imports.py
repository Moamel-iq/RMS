from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0001_initial"),
        ("sales", "0020_remove_deliveryapplication_sales_delivery_application_name_not_empty_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PosSalesImportBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
                ("public_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name="public id")),
                ("business_date", models.DateField(verbose_name="business date")),
                ("status", models.CharField(choices=[("VERIFIED", "مستورد ومتطابق"), ("REVIEW_REQUIRED", "مستورد ويحتاج مراجعة")], default="VERIFIED", max_length=24, verbose_name="status")),
                ("source_hash", models.CharField(max_length=64, verbose_name="source hash")),
                ("total_sales", models.DecimalField(decimal_places=3, max_digits=21, verbose_name="total sales")),
                ("application_sales", models.DecimalField(decimal_places=3, max_digits=21, verbose_name="application sales")),
                ("reported_expenses", models.DecimalField(decimal_places=3, max_digits=21, verbose_name="reported POS expenses")),
                ("operational_expenses", models.DecimalField(decimal_places=3, max_digits=21, verbose_name="operational expenses")),
                ("net_cash", models.DecimalField(decimal_places=3, max_digits=21, verbose_name="net cash")),
                ("total_quantity", models.DecimalField(decimal_places=3, max_digits=18, verbose_name="total quantity")),
                ("report_data", models.JSONField(default=dict, verbose_name="parsed report data")),
                ("checks", models.JSONField(default=list, verbose_name="reconciliation checks")),
                ("warnings", models.JSONField(default=list, verbose_name="warnings")),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="pos_sales_imports", to="organizations.branch", verbose_name="branch")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="pos_sales_imports_created", to=settings.AUTH_USER_MODEL, verbose_name="created by")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="pos_sales_imports", to="organizations.organization", verbose_name="organization")),
            ],
            options={"verbose_name": "POS sales import", "verbose_name_plural": "POS sales imports", "ordering": ["-business_date", "-created_at"]},
        ),
        migrations.CreateModel(
            name="PosSalesImportFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
                ("report_type", models.CharField(max_length=40, verbose_name="report type")),
                ("original_name", models.CharField(max_length=255, verbose_name="original file name")),
                ("file", models.FileField(upload_to="sales/pos-imports/%Y/%m/", verbose_name="file")),
                ("checksum", models.CharField(max_length=64, verbose_name="checksum")),
                ("size", models.PositiveBigIntegerField(verbose_name="size")),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="files", to="sales.possalesimportbatch", verbose_name="POS sales import")),
            ],
            options={"verbose_name": "POS sales import file", "verbose_name_plural": "POS sales import files", "ordering": ["report_type"]},
        ),
        migrations.AddConstraint(model_name="possalesimportbatch", constraint=models.UniqueConstraint(fields=("organization", "source_hash"), name="sales_pos_import_source_unique_per_organization")),
        migrations.AddConstraint(model_name="possalesimportbatch", constraint=models.CheckConstraint(condition=models.Q(("total_sales__gte", 0), ("application_sales__gte", 0), ("reported_expenses__gte", 0), ("operational_expenses__gte", 0), ("total_quantity__gte", 0)), name="sales_pos_import_totals_are_not_negative")),
        migrations.AddIndex(model_name="possalesimportbatch", index=models.Index(fields=["organization", "branch", "business_date"], name="sales_pos_import_day_idx")),
        migrations.AddConstraint(model_name="possalesimportfile", constraint=models.UniqueConstraint(fields=("batch", "report_type"), name="sales_pos_import_file_type_unique")),
    ]
