import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0021_pos_sales_imports"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PosMenuItemMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
                ("source_name", models.CharField(max_length=200, verbose_name="POS item name")),
                ("normalized_source_name", models.CharField(max_length=200, verbose_name="normalized POS item name")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="pos_menu_item_mappings_created", to=settings.AUTH_USER_MODEL, verbose_name="created by")),
                ("menu_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="pos_name_mappings", to="sales.menuitem", verbose_name="menu item")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="pos_menu_item_mappings", to="organizations.organization", verbose_name="organization")),
            ],
            options={"verbose_name": "POS menu item mapping", "verbose_name_plural": "POS menu item mappings", "ordering": ["source_name"]},
        ),
        migrations.AddConstraint(
            model_name="posmenuitemmapping",
            constraint=models.UniqueConstraint(fields=("organization", "normalized_source_name"), name="sales_pos_item_mapping_source_unique"),
        ),
        migrations.AddConstraint(
            model_name="posmenuitemmapping",
            constraint=models.CheckConstraint(condition=models.Q(("source_name", ""), _negated=True) & models.Q(("normalized_source_name", ""), _negated=True), name="sales_pos_item_mapping_names_not_empty"),
        ),
    ]
