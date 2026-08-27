"""Keep the audit-model state aligned with sensitive document downloads."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0005_auditevent_organization_scope")]

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
                    ("DOCUMENT_DOWNLOADED", "تنزيل مستند حساس"),
                    ("IMPORTED", "استيراد"),
                    ("PERMISSION_OVERRIDE", "تجاوز صلاحية"),
                ],
                max_length=32,
                verbose_name="action",
            ),
        )
    ]
