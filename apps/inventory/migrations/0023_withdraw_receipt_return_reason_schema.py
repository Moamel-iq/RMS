"""
The schema half of the withdrawal, split from the data half.

Deleting the withdrawn drafts and then ALTERing the same table inside one
transaction leaves PostgreSQL holding deferred trigger events, and it refuses
the ALTER with "pending trigger events". Two migrations are two transactions.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0023_daily_financial_close_controls"),
        ("inventory", "0022_withdraw_receipt_return_reason_data"),
        ("organizations", "0007_accesschangerequest"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="importbatch",
            options={
                "ordering": ("-created_at", "-id"),
                "permissions": [
                    ("import_master_data", "Can import inventory master data"),
                    ("import_opening_draft", "Can import an opening stock draft"),
                ],
                "verbose_name": "import batch",
                "verbose_name_plural": "import batches",
            },
        ),
        migrations.AlterModelOptions(
            name="inventoryitem",
            options={
                "ordering": ["organization__code", "code"],
                "permissions": [
                    ("view_item", "Can view the item master"),
                    ("manage_items", "Can create and archive inventory items"),
                    ("create_item", "Can register a new inventory item"),
                    ("edit_item", "Can change an existing inventory item"),
                    ("manage_conversions", "Can manage item package conversions"),
                    ("view_stock", "Can view stock on hand"),
                    ("view_valuation", "Can view inventory cost and valuation"),
                    ("create_draft_movement", "Can create a draft stock movement"),
                    ("create_opening_stock", "Can prepare and submit an opening stock document"),
                    ("post_opening_stock", "Can post opening stock"),
                    ("post_receipt", "Can post stock into a warehouse"),
                    ("post_issue", "Can post a stock issue"),
                    ("post_transfer", "Can post a stock transfer"),
                    (
                        "close_transfer_shortage",
                        "Can close a transfer's missing quantity as a loss",
                    ),
                    ("post_waste", "Can post stock waste"),
                    ("conduct_stock_count", "Can conduct a stock count"),
                    ("approve_stock_count", "Can approve a stock count"),
                    ("post_adjustment", "Can post a stock adjustment"),
                    ("reverse_movement", "Can reverse a stock movement"),
                    ("override_negative_stock", "Can post stock below zero"),
                ],
                "verbose_name": "inventory item",
                "verbose_name_plural": "inventory items",
            },
        ),
        migrations.AlterModelOptions(
            name="inventoryreasoncode",
            options={
                "ordering": ["organization__code", "applies_to", "code"],
                "permissions": [],
                "verbose_name": "inventory reason code",
                "verbose_name_plural": "inventory reason codes",
            },
        ),
        migrations.RemoveConstraint(
            model_name="inventorymovementdocument",
            name="inventory_document_type_is_operational",
        ),
        migrations.AlterField(
            model_name="historicalinventorymovementdocument",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("INVENTORY_OPENING", "رصيد افتتاحي"),
                    ("INVENTORY_ISSUE", "صرف مخزني للاستهلاك"),
                    ("INVENTORY_TRANSFER", "تحويل مخزني"),
                    ("INVENTORY_TRANSFER_RECEIPT", "استلام تحويل"),
                    ("INVENTORY_TRANSFER_SHORTAGE", "إقفال عجز تحويل"),
                    ("INVENTORY_WASTE", "إتلاف مخزني"),
                    ("INVENTORY_STOCK_COUNT", "جرد فعلي"),
                    ("INVENTORY_ADJUSTMENT", "تسوية مخزنية يدوية"),
                ],
                max_length=32,
                verbose_name="document type",
            ),
        ),
        migrations.AlterField(
            model_name="inventoryadjustmentline",
            name="reason_code",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="adjustment_lines",
                to="inventory.inventoryreasoncode",
                verbose_name="reason code",
            ),
        ),
        migrations.AlterField(
            model_name="inventorydocumentsequence",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("INVENTORY_OPENING", "رصيد افتتاحي"),
                    ("INVENTORY_ISSUE", "صرف مخزني للاستهلاك"),
                    ("INVENTORY_TRANSFER", "تحويل مخزني"),
                    ("INVENTORY_TRANSFER_RECEIPT", "استلام تحويل"),
                    ("INVENTORY_TRANSFER_SHORTAGE", "إقفال عجز تحويل"),
                    ("INVENTORY_WASTE", "إتلاف مخزني"),
                    ("INVENTORY_STOCK_COUNT", "جرد فعلي"),
                    ("INVENTORY_ADJUSTMENT", "تسوية مخزنية يدوية"),
                ],
                max_length=32,
                verbose_name="document type",
            ),
        ),
        migrations.AlterField(
            model_name="inventorymovementdocument",
            name="document_type",
            field=models.CharField(
                choices=[
                    ("INVENTORY_OPENING", "رصيد افتتاحي"),
                    ("INVENTORY_ISSUE", "صرف مخزني للاستهلاك"),
                    ("INVENTORY_TRANSFER", "تحويل مخزني"),
                    ("INVENTORY_TRANSFER_RECEIPT", "استلام تحويل"),
                    ("INVENTORY_TRANSFER_SHORTAGE", "إقفال عجز تحويل"),
                    ("INVENTORY_WASTE", "إتلاف مخزني"),
                    ("INVENTORY_STOCK_COUNT", "جرد فعلي"),
                    ("INVENTORY_ADJUSTMENT", "تسوية مخزنية يدوية"),
                ],
                max_length=32,
                verbose_name="document type",
            ),
        ),
        migrations.AddConstraint(
            model_name="inventorymovementdocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("document_type__in", ["INVENTORY_ISSUE", "INVENTORY_WASTE"])),
                name="inventory_document_type_is_operational",
            ),
        ),
    ]
