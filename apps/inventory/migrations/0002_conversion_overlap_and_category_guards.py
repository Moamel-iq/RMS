"""
Two guarantees the ORM cannot express as ordinary constraints.

**No overlapping conversion periods.** Two answers to "how many kilograms are
in a sack today" is not a question to resolve at query time; it is a state to
prevent. An `EXCLUDE` constraint over `(item, package_unit, daterange)` makes
it unrepresentable, which needs `btree_gist` because the first two columns are
plain integers.

**Categories hold items or children, never both.** The invariant ADR-014
enforces for accounts, applied to item categories. It cannot be a check
constraint because it spans two tables, so it is a pair of triggers — one
guarding each direction. The service raises the readable error; these hold
when someone bypasses the service, exactly as the accounting kernel's triggers
do.
"""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations

CATEGORY_LEAF_FUNCTION = """
CREATE OR REPLACE FUNCTION inventory_category_must_be_leaf()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM inventory_itemcategory WHERE parent_id = NEW.category_id
    ) THEN
        RAISE EXCEPTION
            'category % has child categories and cannot hold items', NEW.category_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

CATEGORY_LEAF_TRIGGER = """
CREATE TRIGGER inventory_item_category_is_leaf
    BEFORE INSERT OR UPDATE OF category_id ON inventory_inventoryitem
    FOR EACH ROW EXECUTE FUNCTION inventory_category_must_be_leaf();
"""

CATEGORY_CHILDLESS_FUNCTION = """
CREATE OR REPLACE FUNCTION inventory_parent_category_holds_no_items()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF EXISTS (
        SELECT 1 FROM inventory_inventoryitem WHERE category_id = NEW.parent_id
    ) THEN
        RAISE EXCEPTION
            'category % already holds items and cannot acquire children', NEW.parent_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

CATEGORY_CHILDLESS_TRIGGER = """
CREATE TRIGGER inventory_category_parent_holds_no_items
    BEFORE INSERT OR UPDATE OF parent_id ON inventory_itemcategory
    FOR EACH ROW EXECUTE FUNCTION inventory_parent_category_holds_no_items();
"""

CONVERSION_OVERLAP = """
ALTER TABLE inventory_itempackageconversion
    ADD CONSTRAINT item_conversion_no_overlapping_periods
    EXCLUDE USING gist (
        item_id WITH =,
        package_unit_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    WHERE (is_active);
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS inventory_item_category_is_leaf ON inventory_inventoryitem;
DROP TRIGGER IF EXISTS inventory_category_parent_holds_no_items ON inventory_itemcategory;
DROP FUNCTION IF EXISTS inventory_category_must_be_leaf();
DROP FUNCTION IF EXISTS inventory_parent_category_holds_no_items();
ALTER TABLE inventory_itempackageconversion
    DROP CONSTRAINT IF EXISTS item_conversion_no_overlapping_periods;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0001_inventory_master_data_and_warehouse_scope"),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.RunSQL(
            sql=(
                CATEGORY_LEAF_FUNCTION
                + CATEGORY_LEAF_TRIGGER
                + CATEGORY_CHILDLESS_FUNCTION
                + CATEGORY_CHILDLESS_TRIGGER
                + CONVERSION_OVERLAP
            ),
            reverse_sql=DROP_ALL,
        ),
    ]
