"""
One menu item cannot have two prices in force at the same time in one scope.

A `UniqueConstraint` cannot say this. The clash is between *ranges* rather
than values, and two rows can each be legitimate on their own and
contradictory together — which is precisely the case a service check misses
under concurrency, because both requests read a clean table before either
writes.

So it lives where a raw `INSERT` cannot walk past it. Same shape as
`inventory/0002` for package conversions, `procurement/0003` for supplier
terms and `kitchen/0005` for recipe version branch scopes; this is the fourth
use of the idiom and it is deliberately identical to the other three.

## Why one constraint per scope rather than one for all of them

Because the scopes are allowed to overlap **each other** — that is the entire
point of "most specific wins". A branch default running all year and a channel
price running for Ramadan are both correct and both in force; the resolver
picks the narrower one. A single constraint over `(menu_item, branch,
daterange)` would refuse exactly the arrangement the design requires.

What must not overlap is two rows *competing to answer the same question*, so
there is one constraint per scope:

* `BRANCH_DEFAULT` — unique over `(menu_item, branch, range)`
* `CHANNEL` — unique over `(menu_item, branch, channel, range)`

The `APPLICATION` scope's constraint arrives with checkpoint 2, together with
the delivery-application column it needs. Until then a check constraint on the
model refuses that scope outright, so there is no window in which an
unconstrained application price can be written.

`WHERE (is_active)` on both: an archived price is history, and history is
allowed to contain the row that was replaced.
"""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations

BRANCH_DEFAULT_NO_OVERLAP = """
ALTER TABLE sales_menupriceversion
    ADD CONSTRAINT sales_menu_price_branch_default_no_overlap
    EXCLUDE USING gist (
        menu_item_id WITH =,
        branch_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    WHERE (is_active AND scope = 'BRANCH_DEFAULT');
"""

CHANNEL_NO_OVERLAP = """
ALTER TABLE sales_menupriceversion
    ADD CONSTRAINT sales_menu_price_channel_no_overlap
    EXCLUDE USING gist (
        menu_item_id WITH =,
        branch_id WITH =,
        channel_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    WHERE (is_active AND scope = 'CHANNEL');
"""

DROP = """
ALTER TABLE sales_menupriceversion
    DROP CONSTRAINT IF EXISTS sales_menu_price_branch_default_no_overlap;
ALTER TABLE sales_menupriceversion
    DROP CONSTRAINT IF EXISTS sales_menu_price_channel_no_overlap;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0001_initial"),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.RunSQL(sql=BRANCH_DEFAULT_NO_OVERLAP, reverse_sql=DROP),
        migrations.RunSQL(sql=CHANNEL_NO_OVERLAP, reverse_sql=DROP),
    ]
