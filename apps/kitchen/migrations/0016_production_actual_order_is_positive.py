"""
An actual row's stable order must be positive, not merely non-negative.

`PositiveIntegerField` renders as a `>= 0` check, and zero is a value the
generated primary row can never hold: `_write_default_actuals` writes 1, and
`add_production_batch_substitute` takes the highest plus one. So a zero-ordered
row is either a hand-made insert or a bulk update, and it would sort *ahead* of
the primary row — making an added substitute the first statement an operator
reads about a requirement whose own item is the one the recipe asked for.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0015_production_scale_consistency"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="productionbatchactualline",
            constraint=models.CheckConstraint(
                condition=models.Q(entry_order__gte=1),
                name="production_actual_order_is_positive",
            ),
        ),
    ]
