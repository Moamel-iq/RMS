from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("sales", "0023_pos_import_closing_workflow")]

    operations = [
        migrations.AlterModelOptions(
            name="possalesimportbatch",
            options={
                "ordering": ["-business_date", "-created_at"],
                "permissions": [
                    ("view_pos_sales_import", "Can view imported POS sales workflow"),
                    ("confirm_pos_sales_import", "Can confirm imported POS sales as cashier"),
                    ("review_pos_sales_import", "Can review imported POS sales as accountant"),
                    ("post_pos_sales_import", "Can post and close imported POS sales"),
                    ("return_pos_sales_import", "Can return imported POS sales to cashier"),
                ],
                "verbose_name": "POS sales import",
                "verbose_name_plural": "POS sales imports",
            },
        )
    ]
