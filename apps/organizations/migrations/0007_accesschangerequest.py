"""Maker-checker records for organization and branch access changes."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0006_organization_security_permissions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AccessChangeRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
                ("action", models.CharField(choices=[("GRANT", "منح"), ("REVOKE", "سحب")], max_length=10)),
                ("requested_role", models.CharField(blank=True, help_text="الدور المطلوب عند المنح فقط.", max_length=64)),
                ("previous_access", models.JSONField(blank=True, default=dict)),
                ("reason", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "بانتظار الاعتماد"),
                            ("APPROVED", "معتمد"),
                            ("REJECTED", "مرفوض"),
                            ("CANCELLED", "ملغى"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=12,
                    ),
                ),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_reason", models.TextField(blank=True)),
                (
                    "branch",
                    models.ForeignKey(
                        blank=True,
                        help_text="فارغ للصلاحية على مستوى المؤسسة كلها.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="access_change_requests",
                        to="organizations.branch",
                        verbose_name="branch",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="access_change_requests",
                        to="organizations.organization",
                        verbose_name="organization",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="requested_access_changes",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="requested by",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviewed_access_changes",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="reviewed by",
                    ),
                ),
                (
                    "target_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="access_change_requests",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="target user",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="accesschangerequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("action", "GRANT"), ("requested_role__gt", ""))
                    | models.Q(("action", "REVOKE"), ("requested_role", ""))
                ),
                name="access_change_request_role_matches_action",
            ),
        ),
        migrations.AddConstraint(
            model_name="accesschangerequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "PENDING")),
                fields=("organization", "branch", "target_user"),
                name="one_pending_access_change_per_scope",
            ),
        ),
    ]
