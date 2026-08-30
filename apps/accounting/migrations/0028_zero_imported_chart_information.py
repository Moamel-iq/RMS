from decimal import Decimal

from django.db import migrations


def zero_imported_chart_information(apps, schema_editor):
    ImportedChartAccount = apps.get_model("accounting", "ImportedChartAccount")
    ImportedChartAccount.objects.update(
        statement_name="",
        category="",
        currency="",
        source_debit=Decimal("0"),
        source_credit=Decimal("0"),
        source_balance=Decimal("0"),
        organizer="",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0027_imported_chart_register"),
    ]

    operations = [
        migrations.RunPython(zero_imported_chart_information, migrations.RunPython.noop),
    ]
