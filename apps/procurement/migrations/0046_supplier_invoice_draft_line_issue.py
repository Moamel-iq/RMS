import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("procurement", "0045_cycle_collecting_due_settled"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierInvoiceDraftLineIssue",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, db_index=True, verbose_name="created at"
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
                (
                    "public_id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, unique=True, verbose_name="public id"
                    ),
                ),
                ("sequence", models.PositiveIntegerField(verbose_name="sequence")),
                ("payload", models.JSONField(default=dict, verbose_name="entered values")),
                (
                    "errors",
                    models.JSONField(blank=True, default=dict, verbose_name="validation errors"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("OPEN", "يحتاج تعديل"), ("RESOLVED", "تم التصحيح")],
                        default="OPEN",
                        max_length=10,
                        verbose_name="status",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_supplier_invoice_line_issues",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="created by",
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="draft_line_issues",
                        to="procurement.supplierinvoice",
                        verbose_name="invoice",
                    ),
                ),
                (
                    "resolved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="resolved_supplier_invoice_line_issues",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="resolved by",
                    ),
                ),
                (
                    "resolved_line",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="draft_issue",
                        to="procurement.supplierinvoiceline",
                        verbose_name="resolved line",
                    ),
                ),
                (
                    "resolved_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="resolved at"),
                ),
            ],
            options={
                "verbose_name": "supplier invoice draft line issue",
                "verbose_name_plural": "supplier invoice draft line issues",
                "ordering": ["invoice", "sequence", "id"],
                "indexes": [
                    models.Index(
                        fields=["invoice", "status", "sequence"], name="sinv_issue_status_idx"
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                status="OPEN",
                                resolved_line__isnull=True,
                                resolved_by__isnull=True,
                                resolved_at__isnull=True,
                            )
                            | models.Q(
                                status="RESOLVED",
                                resolved_line__isnull=False,
                                resolved_by__isnull=False,
                                resolved_at__isnull=False,
                            )
                        ),
                        name="procurement_invoice_line_issue_resolution_complete",
                    )
                ],
            },
        )
    ]
