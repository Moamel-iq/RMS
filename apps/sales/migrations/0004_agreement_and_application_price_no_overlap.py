"""
Two more range-exclusion constraints: one for agreements, one for application
prices.

Fourth and fifth uses of the same idiom in this repository
(`inventory/0002`, `procurement/0003`, `kitchen/0005`, `sales/0002`), and
deliberately identical to them. A `UniqueConstraint` cannot say either rule:
the clash is between *ranges* rather than values, and two rows can each be
legitimate on their own and contradictory together — which is exactly what a
service check misses under concurrency, because both requests read a clean
table before either writes.

## The agreement constraint

One branch and one application cannot have two commission agreements in force
at the same time. If they could, `resolve_agreement` would have to pick, and
whatever it picked would decide an expense on every order — silently, and
differently depending on insertion order.

`WHERE (is_active)`: a withdrawn agreement is history, and history is allowed
to contain the row that was replaced.

## The application price constraint

`sales/0002` created one exclusion constraint per price scope, because the
scopes are *meant* to overlap each other — that is what "most specific wins"
means. `APPLICATION` was declared then and refused by a check constraint,
because its master did not exist. It exists now, so the refusal is replaced by
the real rule: two application prices for the same item, branch and
application cannot overlap in time.
"""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations

AGREEMENT_NO_OVERLAP = """
ALTER TABLE sales_deliveryagreement
    ADD CONSTRAINT sales_agreement_no_overlapping_periods
    EXCLUDE USING gist (
        branch_id WITH =,
        delivery_application_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    WHERE (is_active);
"""

APPLICATION_PRICE_NO_OVERLAP = """
ALTER TABLE sales_menupriceversion
    ADD CONSTRAINT sales_menu_price_application_no_overlap
    EXCLUDE USING gist (
        menu_item_id WITH =,
        branch_id WITH =,
        delivery_application_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    WHERE (is_active AND scope = 'APPLICATION');
"""

DROP = """
ALTER TABLE sales_deliveryagreement
    DROP CONSTRAINT IF EXISTS sales_agreement_no_overlapping_periods;
ALTER TABLE sales_menupriceversion
    DROP CONSTRAINT IF EXISTS sales_menu_price_application_no_overlap;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0003_delivery_applications_agreements_and_discounts"),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.RunSQL(sql=AGREEMENT_NO_OVERLAP, reverse_sql=DROP),
        migrations.RunSQL(sql=APPLICATION_PRICE_NO_OVERLAP, reverse_sql=DROP),
    ]
