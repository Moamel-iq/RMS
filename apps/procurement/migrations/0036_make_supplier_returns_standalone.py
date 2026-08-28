from django.db import migrations


class Migration(migrations.Migration):
    """Detach retained supplier returns from removed purchasing documents."""

    dependencies = [("procurement", "0035_historicalsupplierinvoicecharge_and_more")]

    operations = [
        migrations.RemoveIndex(model_name="supplierreturn", name="sret_receipt_idx"),
        migrations.RemoveIndex(model_name="supplierreturnline", name="sret_line_receipt_line_idx"),
        migrations.RemoveField(model_name="supplierreturn", name="receipt"),
        migrations.RemoveField(model_name="supplierreturnline", name="goods_receipt_line"),
        migrations.RemoveField(model_name="historicalsupplierreturn", name="receipt"),
        migrations.RemoveField(model_name="historicalsupplierreturnline", name="goods_receipt_line"),
    ]
