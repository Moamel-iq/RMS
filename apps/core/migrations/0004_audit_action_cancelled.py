"""
`AuditAction.CANCELLED`, added by Task 1.6.

Choices only — the column is a `CharField` with no check constraint, so this
alters nothing in the database and exists so the migration state matches the
models. `manage.py makemigrations --check` is part of the quality gate, and a
pending migration nobody generated is indistinguishable from one somebody
forgot.

The value earns its place: a cancelled physical count froze a warehouse for an
afternoon and is kept, not deleted. `REJECTED` is somebody refusing an
approval and `DELETED` removes the row; neither says what happened here.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_task_0_7_permissions_and_source_identity"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditevent",
            name="action",
            field=models.CharField(
                choices=[
                    ("CREATED", "إنشاء"),
                    ("UPDATED", "تعديل"),
                    ("DELETED", "حذف"),
                    ("DEACTIVATED", "إيقاف"),
                    ("SUBMITTED", "إرسال"),
                    ("APPROVED", "اعتماد"),
                    ("REJECTED", "رفض"),
                    ("CANCELLED", "إلغاء"),
                    ("POSTED", "ترحيل"),
                    ("POSTING_FAILED", "فشل الترحيل"),
                    ("REVERSED", "عكس القيد"),
                    ("PERIOD_CLOSED", "إقفال فترة"),
                    ("PERIOD_REOPENED", "إعادة فتح فترة"),
                    ("ACCESS_GRANTED", "منح صلاحية"),
                    ("ACCESS_REVOKED", "سحب صلاحية"),
                    ("IMPORTED", "استيراد"),
                    ("PERMISSION_OVERRIDE", "تجاوز صلاحية"),
                ],
                max_length=32,
                verbose_name="action",
            ),
        ),
    ]
