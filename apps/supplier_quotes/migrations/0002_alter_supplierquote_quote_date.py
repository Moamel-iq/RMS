# Generated manually to keep this module independent of unrelated inventory changes.

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [("supplier_quotes", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="historicalsupplierquote",
            name="quote_date",
            field=models.DateField(db_index=True, default=timezone.localdate),
        ),
        migrations.AlterField(
            model_name="supplierquote",
            name="quote_date",
            field=models.DateField(db_index=True, default=timezone.localdate),
        ),
    ]
