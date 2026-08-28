from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0037_direct_supplier_invoice_receipt"),
    ]

    operations = [
        migrations.AlterField(
            model_name="historicalsupplierinvoiceposting",
            name="direct_inventory_value",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0.000"),
                max_digits=21,
                verbose_name="direct inventory receipt",
            ),
        ),
        migrations.AlterField(
            model_name="supplierinvoiceposting",
            name="direct_inventory_value",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0.000"),
                max_digits=21,
                verbose_name="direct inventory receipt",
            ),
        ),
    ]
