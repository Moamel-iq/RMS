"""
Make the stock ledger append-only at the database, not by convention.

The application already refuses to edit a posted movement. That is not the
same as it being impossible: a data-repair script, a bulk `update()`, the
admin, or `psql` all bypass every line of Python. An invariant that only the
application enforces is an invariant that holds until the first incident, and
the first incident is exactly when somebody reaches for `psql`.

Three guards, each stated as an **allowlist** rather than a blocklist. A
blocklist names the columns that must not move and silently permits any column
added later; `accounting/0005` exists because that was forgotten once already.
Each permitted transition here builds the row it expects and compares whole
rows with `IS NOT DISTINCT FROM`, so a column added to these tables in future
is protected automatically.

1. `StockMovement` is **insert-only**. No update at all, no delete at all.
   There is no legitimate edit: the only permitted mutable link, the reversal
   back-pointer, lives on the reversing row and is written at insert time.

2. `StockLedgerEntry` is immutable once written, with a single exception —
   the `reverses_id` link, set once from NULL when a reversal is posted.

3. `ValuationLayer.remaining_quantity` may move (a layered strategy consumes
   it); nothing else about a layer may. What a receipt cost is a historical
   fact.
"""

from django.db import migrations

MOVEMENT_IS_INSERT_ONLY = """
CREATE OR REPLACE FUNCTION inventory_stock_movement_is_insert_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a stock movement cannot be deleted; reverse it instead'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RAISE EXCEPTION 'a stock movement is immutable; reverse it instead'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_movement_is_insert_only
BEFORE UPDATE OR DELETE ON inventory_stockmovement
FOR EACH ROW EXECUTE FUNCTION inventory_stock_movement_is_insert_only();
"""

DROP_MOVEMENT_TRIGGER = """
DROP TRIGGER IF EXISTS stock_movement_is_insert_only ON inventory_stockmovement;
DROP FUNCTION IF EXISTS inventory_stock_movement_is_insert_only();
"""


ENTRY_IS_IMMUTABLE = """
CREATE OR REPLACE FUNCTION inventory_stock_entry_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    permitted inventory_stockledgerentry%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a stock ledger entry cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Reported separately: "you moved the source identity" and "you edited a
    -- posted entry" send a developer to two different places.
    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.source_document_type IS DISTINCT FROM OLD.source_document_type
       OR NEW.source_document_id IS DISTINCT FROM OLD.source_document_id
       OR NEW.source_event IS DISTINCT FROM OLD.source_event THEN
        RAISE EXCEPTION
            'the source identity of a stock ledger entry is immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- The one permitted transition: link a reversal to what it reverses,
    -- once, moving nothing else.
    permitted := OLD;
    permitted.reverses_id := NEW.reverses_id;
    permitted.updated_at := NEW.updated_at;
    IF OLD.reverses_id IS NULL
       AND NEW.reverses_id IS NOT NULL
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'a stock ledger entry is immutable'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_entry_is_immutable
BEFORE UPDATE OR DELETE ON inventory_stockledgerentry
FOR EACH ROW EXECUTE FUNCTION inventory_stock_entry_is_immutable();
"""

DROP_ENTRY_TRIGGER = """
DROP TRIGGER IF EXISTS stock_entry_is_immutable ON inventory_stockledgerentry;
DROP FUNCTION IF EXISTS inventory_stock_entry_is_immutable();
"""


LAYER_COST_IS_HISTORICAL = """
CREATE OR REPLACE FUNCTION inventory_valuation_layer_cost_is_historical()
RETURNS TRIGGER AS $$
DECLARE
    permitted inventory_valuationlayer%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a valuation layer cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Only the remaining quantity may move, and only a layered strategy moves
    -- it. What the goods cost on arrival is a fact about the past.
    permitted := OLD;
    permitted.remaining_quantity := NEW.remaining_quantity;
    permitted.updated_at := NEW.updated_at;
    IF NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'the cost and origin of a valuation layer are immutable'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER valuation_layer_cost_is_historical
BEFORE UPDATE OR DELETE ON inventory_valuationlayer
FOR EACH ROW EXECUTE FUNCTION inventory_valuation_layer_cost_is_historical();
"""

DROP_LAYER_TRIGGER = """
DROP TRIGGER IF EXISTS valuation_layer_cost_is_historical ON inventory_valuationlayer;
DROP FUNCTION IF EXISTS inventory_valuation_layer_cost_is_historical();
"""


#: Balance rows carry organization and branch denormalised from the warehouse.
#: A row whose denormalised owner disagrees with its warehouse would be
#: invisible to one tenancy filter and visible to another, so the database
#: refuses to hold one rather than trusting every future writer to get it right.
BALANCE_OWNERSHIP_AGREES = """
CREATE OR REPLACE FUNCTION inventory_stock_balance_ownership_agrees()
RETURNS TRIGGER AS $$
DECLARE
    warehouse_branch INTEGER;
    warehouse_organization INTEGER;
    item_organization INTEGER;
    lot_item INTEGER;
BEGIN
    SELECT w.branch_id, b.organization_id
      INTO warehouse_branch, warehouse_organization
      FROM inventory_warehouse w
      JOIN organizations_branch b ON b.id = w.branch_id
     WHERE w.id = NEW.warehouse_id;

    IF NEW.branch_id IS DISTINCT FROM warehouse_branch
       OR NEW.organization_id IS DISTINCT FROM warehouse_organization THEN
        RAISE EXCEPTION
            'stock balance owner does not match its warehouse'
            USING ERRCODE = 'restrict_violation';
    END IF;

    SELECT organization_id INTO item_organization
      FROM inventory_inventoryitem WHERE id = NEW.item_id;
    IF item_organization IS DISTINCT FROM warehouse_organization THEN
        RAISE EXCEPTION
            'stock balance item belongs to another organization'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.lot_id IS NOT NULL THEN
        SELECT item_id INTO lot_item FROM inventory_inventorylot WHERE id = NEW.lot_id;
        IF lot_item IS DISTINCT FROM NEW.item_id THEN
            RAISE EXCEPTION 'stock balance lot belongs to another item'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_balance_ownership_agrees
BEFORE INSERT OR UPDATE ON inventory_stockbalance
FOR EACH ROW EXECUTE FUNCTION inventory_stock_balance_ownership_agrees();

CREATE TRIGGER stock_movement_ownership_agrees
BEFORE INSERT ON inventory_stockmovement
FOR EACH ROW EXECUTE FUNCTION inventory_stock_movement_ownership_agrees();
"""

MOVEMENT_OWNERSHIP_AGREES = """
CREATE OR REPLACE FUNCTION inventory_stock_movement_ownership_agrees()
RETURNS TRIGGER AS $$
DECLARE
    warehouse_branch INTEGER;
    warehouse_organization INTEGER;
    item_organization INTEGER;
    lot_item INTEGER;
BEGIN
    SELECT w.branch_id, b.organization_id
      INTO warehouse_branch, warehouse_organization
      FROM inventory_warehouse w
      JOIN organizations_branch b ON b.id = w.branch_id
     WHERE w.id = NEW.warehouse_id;

    IF NEW.branch_id IS DISTINCT FROM warehouse_branch
       OR NEW.organization_id IS DISTINCT FROM warehouse_organization THEN
        RAISE EXCEPTION 'stock movement owner does not match its warehouse'
            USING ERRCODE = 'restrict_violation';
    END IF;

    SELECT organization_id INTO item_organization
      FROM inventory_inventoryitem WHERE id = NEW.item_id;
    IF item_organization IS DISTINCT FROM warehouse_organization THEN
        RAISE EXCEPTION 'stock movement item belongs to another organization'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.lot_id IS NOT NULL THEN
        SELECT item_id INTO lot_item FROM inventory_inventorylot WHERE id = NEW.lot_id;
        IF lot_item IS DISTINCT FROM NEW.item_id THEN
            RAISE EXCEPTION 'stock movement lot belongs to another item'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

DROP_OWNERSHIP_TRIGGERS = """
DROP TRIGGER IF EXISTS stock_balance_ownership_agrees ON inventory_stockbalance;
DROP TRIGGER IF EXISTS stock_movement_ownership_agrees ON inventory_stockmovement;
DROP FUNCTION IF EXISTS inventory_stock_balance_ownership_agrees();
DROP FUNCTION IF EXISTS inventory_stock_movement_ownership_agrees();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0003_stock_ledger"),
    ]

    operations = [
        migrations.RunSQL(sql=MOVEMENT_IS_INSERT_ONLY, reverse_sql=DROP_MOVEMENT_TRIGGER),
        migrations.RunSQL(sql=ENTRY_IS_IMMUTABLE, reverse_sql=DROP_ENTRY_TRIGGER),
        migrations.RunSQL(sql=LAYER_COST_IS_HISTORICAL, reverse_sql=DROP_LAYER_TRIGGER),
        migrations.RunSQL(sql=MOVEMENT_OWNERSHIP_AGREES, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(sql=BALANCE_OWNERSHIP_AGREES, reverse_sql=DROP_OWNERSHIP_TRIGGERS),
    ]
