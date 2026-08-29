from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def forward_statuses(apps, schema_editor):
    Batch = apps.get_model("sales", "PosSalesImportBatch")
    Batch.objects.filter(status="VERIFIED").update(status="AWAITING_CASHIER")
    Batch.objects.filter(status="REVIEW_REQUIRED").update(status="DRAFT")


def backward_statuses(apps, schema_editor):
    Batch = apps.get_model("sales", "PosSalesImportBatch")
    Batch.objects.filter(status__in=["DRAFT", "RETURNED_TO_CASHIER"]).update(
        status="REVIEW_REQUIRED"
    )
    Batch.objects.exclude(status="REVIEW_REQUIRED").update(status="VERIFIED")


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0022_pos_menu_item_mapping"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="possalesimportbatch",
            name="review_step",
            field=models.PositiveSmallIntegerField(default=0, verbose_name="accountant review step"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="review_data",
            field=models.JSONField(blank=True, default=dict, verbose_name="accountant review evidence"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="cashier_confirmed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="cashier confirmed at"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="cashier_confirmed_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="pos_sales_imports_confirmed", to=settings.AUTH_USER_MODEL, verbose_name="cashier confirmed by"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="accountant_started_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="accountant review started at"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="accountant_started_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="pos_sales_imports_reviewed", to=settings.AUTH_USER_MODEL, verbose_name="accountant reviewer"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="returned_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="returned at"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="returned_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="pos_sales_imports_returned", to=settings.AUTH_USER_MODEL, verbose_name="returned by"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="return_reason",
            field=models.TextField(blank=True, verbose_name="return reason"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="linked_sales_day",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="pos_import_batch", to="sales.salesday", verbose_name="linked sales day"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="posted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="posted at"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="posted_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="pos_sales_imports_posted", to=settings.AUTH_USER_MODEL, verbose_name="posted by"),
        ),
        migrations.AddField(
            model_name="possalesimportbatch",
            name="posting_reference",
            field=models.CharField(blank=True, max_length=64, verbose_name="posting reference"),
        ),
        migrations.AlterField(
            model_name="possalesimportbatch",
            name="status",
            field=models.CharField(choices=[("DRAFT", "مسودة"), ("AWAITING_CASHIER", "بانتظار تأكيد الكاشير"), ("AWAITING_ACCOUNTANT", "بانتظار مراجعة المحاسب"), ("ACCOUNTANT_REVIEW", "قيد مراجعة المحاسب"), ("RETURNED_TO_CASHIER", "معادة للكاشير"), ("READY_TO_POST", "جاهزة للترحيل"), ("POSTED", "مرحلة ومقفلة"), ("CANCELLED", "ملغاة"), ("REVERSED", "معكوسة")], default="DRAFT", max_length=24, verbose_name="status"),
        ),
        migrations.AlterModelOptions(
            name="possalesimportbatch",
            options={"ordering": ["-business_date", "-created_at"], "permissions": [("confirm_pos_sales_import", "Can confirm imported POS sales as cashier"), ("review_pos_sales_import", "Can review imported POS sales as accountant"), ("post_pos_sales_import", "Can post and close imported POS sales"), ("return_pos_sales_import", "Can return imported POS sales to cashier")], "verbose_name": "POS sales import", "verbose_name_plural": "POS sales imports"},
        ),
        migrations.RunPython(forward_statuses, backward_statuses),
        migrations.AddConstraint(model_name="possalesimportbatch", constraint=models.CheckConstraint(condition=models.Q(("review_step__gte", 0), ("review_step__lte", 5)), name="sales_pos_import_review_step_range")),
        migrations.AddConstraint(model_name="possalesimportbatch", constraint=models.CheckConstraint(condition=models.Q(("posted_at__isnull", True), ("posted_by__isnull", True)) | models.Q(("posted_at__isnull", False), ("posted_by__isnull", False)), name="sales_pos_import_posting_stamp_complete")),
    ]
