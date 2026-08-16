"""
One supplier's terms for one item and package cannot overlap in time.

A `UniqueConstraint` cannot say this: the clash is between *ranges*, not
values, and two rows can both be legitimate on their own and contradictory
together. PostgreSQL's exclusion constraint is the only place the rule can
live where a raw `INSERT` cannot walk past it, and a service check alone would
be a promise rather than a guarantee.

`COALESCE(package_unit_id, 0)` because a NULL package means "bought in base
units", which is one answer. Left as NULL, SQL's own semantics would treat
every base-unit row as distinct from every other and the rule would silently
not apply to exactly the simplest case.

Same shape as `inventory/0002`, which does this for item package conversions.
"""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations

NO_OVERLAP = """
ALTER TABLE procurement_supplieritem
    ADD CONSTRAINT procurement_supplier_item_no_overlapping_periods
    EXCLUDE USING gist (
        supplier_id WITH =,
        item_id WITH =,
        (COALESCE(package_unit_id, 0)) WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    WHERE (is_active);
"""

DROP = """
ALTER TABLE procurement_supplieritem
    DROP CONSTRAINT IF EXISTS procurement_supplier_item_no_overlapping_periods;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0002_supplier_item_catalogue"),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.RunSQL(sql=NO_OVERLAP, reverse_sql=DROP),
    ]
