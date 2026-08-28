"""Make a supplier invoice the direct stock-receipt document.

The existing posting record previously only named a stock entry for landed
costs against a separately posted goods receipt.  Direct invoices now record
the value received into MAIN explicitly, while preserving historical match
figures for legacy rows.
"""

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0036_make_supplier_returns_standalone"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="supplierinvoiceposting",
            name="procurement_posting_payable_is_the_whole_invoice",
        ),
        migrations.RemoveConstraint(
            model_name="supplierinvoiceposting",
            name="procurement_posting_values_are_not_negative",
        ),
        migrations.RemoveConstraint(
            model_name="supplierinvoiceposting",
            name="procurement_posting_landed_cost_names_stock_entry",
        ),
        migrations.AddField(
            model_name="historicalsupplierinvoiceposting",
            name="direct_inventory_value",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0.000"),
                max_digits=18,
                verbose_name="direct inventory receipt",
            ),
        ),
        migrations.AddField(
            model_name="supplierinvoiceposting",
            name="direct_inventory_value",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0.000"),
                max_digits=18,
                verbose_name="direct inventory receipt",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplierinvoiceposting",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    payable_value=models.F("direct_charge_value")
                    + models.F("invoice_matched_value")
                    + models.F("direct_inventory_value")
                    + models.F("landed_cost_value")
                ),
                name="procurement_posting_payable_is_the_whole_invoice",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplierinvoiceposting",
            constraint=models.CheckConstraint(
                condition=models.Q(goods_cleared_value__gte=0)
                & models.Q(invoice_matched_value__gte=0)
                & models.Q(direct_charge_value__gte=0)
                & models.Q(direct_inventory_value__gte=0)
                & models.Q(landed_cost_value__gte=0)
                & models.Q(payable_value__gt=0),
                name="procurement_posting_values_are_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplierinvoiceposting",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    landed_cost_value=0,
                    direct_inventory_value=0,
                    stock_entry__isnull=True,
                )
                | models.Q(landed_cost_value__gt=0, stock_entry__isnull=False)
                | models.Q(direct_inventory_value__gt=0, stock_entry__isnull=False),
                name="procurement_posting_stock_value_names_stock_entry",
            ),
        ),
    ]
