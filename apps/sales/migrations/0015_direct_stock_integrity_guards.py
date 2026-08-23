"""Database integrity for direct-stock sales and their return evidence.

Migration ``0014`` introduces the schema.  This migration binds the rows into
one economic fact.  The guards are deliberately database-side: a management
command or import must not be able to attach a sale to another tenant's item,
attach COGS evidence to an unrelated stock movement, or return more than the
original issue.

Evidence checks are deferred until commit.  Posting creates the stock entry,
the journal link and the evidence before it makes the sales header ``POSTED``;
checking any one of those intermediate statements would reject the valid
transaction.  At commit all of them must agree.
"""

from django.db import migrations

GUARDS = r"""
-- -----------------------------------------------------------------------
-- A posted document may never acquire another line.
-- The older freeze triggers cover UPDATE/DELETE; these close the INSERT gap.
-- -----------------------------------------------------------------------

CREATE OR REPLACE FUNCTION sales_day_line_insert_parent_is_draft()
RETURNS TRIGGER AS $$
DECLARE
    parent_status text;
BEGIN
    SELECT status
      INTO STRICT parent_status
      FROM sales_salesday
     WHERE id = NEW.sales_day_id;

    IF parent_status IS DISTINCT FROM 'DRAFT' THEN
        RAISE EXCEPTION
            'sales day lines may only be inserted while the day is a draft'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_day_line_insert_parent_is_draft
    BEFORE INSERT ON sales_salesdayline
    FOR EACH ROW EXECUTE FUNCTION sales_day_line_insert_parent_is_draft();

CREATE OR REPLACE FUNCTION sales_adjustment_line_insert_parent_is_draft()
RETURNS TRIGGER AS $$
DECLARE
    parent_status text;
BEGIN
    SELECT status
      INTO STRICT parent_status
      FROM sales_salesadjustment
     WHERE id = NEW.adjustment_id;

    IF parent_status IS DISTINCT FROM 'DRAFT' THEN
        RAISE EXCEPTION
            'sales adjustment lines may only be inserted while the adjustment is a draft'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_adjustment_line_insert_parent_is_draft
    BEFORE INSERT ON sales_salesadjustmentline
    FOR EACH ROW EXECUTE FUNCTION sales_adjustment_line_insert_parent_is_draft();


-- -----------------------------------------------------------------------
-- Master-data and frozen sales-line scope.
-- -----------------------------------------------------------------------

CREATE OR REPLACE FUNCTION sales_menu_master_scope_is_consistent()
RETURNS TRIGGER AS $$
DECLARE
    current_row record;
    related_organization_id bigint;
    direct_item_type text;
    direct_item_tracks_lots boolean;
BEGIN
    SELECT id, organization_id, category_id, fulfillment_source, recipe_id, inventory_item_id
      INTO current_row
      FROM sales_menuitem
     WHERE id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF current_row.category_id IS NOT NULL THEN
        SELECT organization_id
          INTO STRICT related_organization_id
          FROM sales_menucategory
         WHERE id = current_row.category_id;
        IF related_organization_id IS DISTINCT FROM current_row.organization_id THEN
            RAISE EXCEPTION
                'a menu category must belong to the menu organization'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF current_row.fulfillment_source = 'RECIPE_SERVING' THEN
        SELECT organization_id
          INTO STRICT related_organization_id
          FROM kitchen_recipe
         WHERE id = current_row.recipe_id;
        IF related_organization_id IS DISTINCT FROM current_row.organization_id THEN
            RAISE EXCEPTION
                'a menu recipe must belong to the menu organization'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF current_row.fulfillment_source = 'DIRECT_STOCK' THEN
        SELECT organization_id, item_type, tracks_lots
          INTO STRICT related_organization_id, direct_item_type, direct_item_tracks_lots
          FROM inventory_inventoryitem
         WHERE id = current_row.inventory_item_id;
        IF related_organization_id IS DISTINCT FROM current_row.organization_id THEN
            RAISE EXCEPTION
                'a direct-stock menu item must belong to the menu organization'
                USING ERRCODE = 'check_violation';
        END IF;
        IF direct_item_type IS DISTINCT FROM 'GOODS_FOR_RESALE' THEN
            RAISE EXCEPTION
                'a direct-stock menu item must be classified as GOODS_FOR_RESALE'
                USING ERRCODE = 'check_violation';
        END IF;
        IF direct_item_tracks_lots THEN
            RAISE EXCEPTION
                'direct-stock menu sales do not support lot-tracked items'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_menu_master_scope_is_consistent
    AFTER INSERT OR UPDATE ON sales_menuitem
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_menu_master_scope_is_consistent();

CREATE OR REPLACE FUNCTION sales_menu_category_dependents_are_consistent()
RETURNS TRIGGER AS $$
DECLARE
    current_organization_id bigint;
BEGIN
    SELECT organization_id
      INTO current_organization_id
      FROM sales_menucategory
     WHERE id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM sales_menuitem
         WHERE category_id = NEW.id
           AND organization_id IS DISTINCT FROM current_organization_id
    ) THEN
        RAISE EXCEPTION
            'changing a menu category may not move linked menu items across organizations'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_menu_category_dependents_are_consistent
    AFTER UPDATE ON sales_menucategory
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_menu_category_dependents_are_consistent();

CREATE OR REPLACE FUNCTION sales_direct_item_dependents_are_consistent()
RETURNS TRIGGER AS $$
DECLARE
    current_row record;
BEGIN
    SELECT organization_id, item_type, tracks_lots
      INTO current_row
      FROM inventory_inventoryitem
     WHERE id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM sales_menuitem AS menu
         WHERE menu.inventory_item_id = NEW.id
           AND (
                menu.organization_id IS DISTINCT FROM current_row.organization_id
                OR menu.fulfillment_source IS DISTINCT FROM 'DIRECT_STOCK'
                OR current_row.item_type IS DISTINCT FROM 'GOODS_FOR_RESALE'
                OR current_row.tracks_lots
           )
    ) OR EXISTS (
        SELECT 1
          FROM sales_salesdayline AS line
          JOIN sales_salesday AS day ON day.id = line.sales_day_id
         WHERE line.inventory_item_id = NEW.id
           AND (
                day.organization_id IS DISTINCT FROM current_row.organization_id
                OR line.fulfillment_source IS DISTINCT FROM 'DIRECT_STOCK'
                OR current_row.item_type IS DISTINCT FROM 'GOODS_FOR_RESALE'
                OR current_row.tracks_lots
           )
    ) THEN
        RAISE EXCEPTION
            'changing an inventory item may not invalidate direct-stock menu or sales snapshots'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_direct_item_dependents_are_consistent
    AFTER UPDATE ON inventory_inventoryitem
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_direct_item_dependents_are_consistent();

CREATE OR REPLACE FUNCTION sales_menu_setting_scope_is_consistent()
RETURNS TRIGGER AS $$
DECLARE
    current_row record;
BEGIN
    SELECT setting.id,
           menu.organization_id AS menu_organization_id,
           branch.organization_id AS branch_organization_id
      INTO current_row
      FROM sales_menuitembranchsetting AS setting
      JOIN sales_menuitem AS menu ON menu.id = setting.menu_item_id
      JOIN organizations_branch AS branch ON branch.id = setting.branch_id
     WHERE setting.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF current_row.menu_organization_id IS DISTINCT FROM current_row.branch_organization_id THEN
        RAISE EXCEPTION
            'a menu branch setting must belong to the menu organization'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_menu_setting_scope_is_consistent
    AFTER INSERT OR UPDATE ON sales_menuitembranchsetting
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_menu_setting_scope_is_consistent();

CREATE OR REPLACE FUNCTION sales_day_line_snapshot_is_consistent()
RETURNS TRIGGER AS $$
DECLARE
    row_data record;
    related_organization_id bigint;
    related_recipe_id bigint;
    related_version_id bigint;
    related_serving_code text;
    related_branch_id bigint;
    direct_item_type text;
    direct_item_tracks_lots boolean;
    expected_total numeric;
BEGIN
    SELECT line.id,
           line.fulfillment_source,
           line.recipe_id,
           line.recipe_version_id,
           line.serving_id,
           line.inventory_item_id,
           line.source_warehouse_id,
           line.direct_stock_qty_per_unit,
           line.direct_stock_total_base_qty,
           line.quantity,
           day.organization_id AS day_organization_id,
           day.branch_id AS day_branch_id,
           branch.organization_id AS branch_organization_id,
           menu.organization_id AS menu_organization_id,
           menu.fulfillment_source AS menu_route,
           menu.recipe_id AS menu_recipe_id,
           menu.serving_code AS menu_serving_code,
           menu.inventory_item_id AS menu_inventory_item_id,
           menu.direct_stock_base_quantity AS menu_direct_quantity,
           channel.organization_id AS channel_organization_id,
           channel.cost_center_id AS channel_cost_center_id,
           center.organization_id AS center_organization_id,
           setting.id AS setting_id,
           setting.is_available AS setting_is_available,
           setting.source_warehouse_id AS setting_warehouse_id
      INTO row_data
      FROM sales_salesdayline AS line
      JOIN sales_salesday AS day ON day.id = line.sales_day_id
      JOIN organizations_branch AS branch ON branch.id = day.branch_id
      JOIN sales_menuitem AS menu ON menu.id = line.menu_item_id
      JOIN sales_saleschannel AS channel ON channel.id = line.channel_id
      JOIN accounting_costcenter AS center ON center.id = channel.cost_center_id
      LEFT JOIN sales_menuitembranchsetting AS setting
        ON setting.menu_item_id = line.menu_item_id
       AND setting.branch_id = day.branch_id
     WHERE line.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF row_data.day_organization_id IS DISTINCT FROM row_data.branch_organization_id
       OR row_data.day_organization_id IS DISTINCT FROM row_data.menu_organization_id
       OR row_data.day_organization_id IS DISTINCT FROM row_data.channel_organization_id
       OR row_data.day_organization_id IS DISTINCT FROM row_data.center_organization_id THEN
        RAISE EXCEPTION
            'a sales line, branch, menu item, channel and cost center must share an organization'
            USING ERRCODE = 'check_violation';
    END IF;

    IF row_data.setting_id IS NULL OR NOT row_data.setting_is_available THEN
        RAISE EXCEPTION
            'a sales line requires an available branch setting for its menu item'
            USING ERRCODE = 'check_violation';
    END IF;

    IF row_data.fulfillment_source IS DISTINCT FROM row_data.menu_route THEN
        RAISE EXCEPTION
            'a sales line fulfillment snapshot must match its menu route when captured'
            USING ERRCODE = 'check_violation';
    END IF;

    IF row_data.fulfillment_source = 'RECIPE_SERVING' THEN
        SELECT organization_id
          INTO STRICT related_organization_id
          FROM kitchen_recipe
         WHERE id = row_data.recipe_id;
        SELECT recipe_id
          INTO STRICT related_recipe_id
          FROM kitchen_recipeversion
         WHERE id = row_data.recipe_version_id;
        SELECT version_id, code
          INTO STRICT related_version_id, related_serving_code
          FROM kitchen_recipeserving
         WHERE id = row_data.serving_id;

        IF related_organization_id IS DISTINCT FROM row_data.day_organization_id
           OR row_data.recipe_id IS DISTINCT FROM row_data.menu_recipe_id
           OR related_recipe_id IS DISTINCT FROM row_data.recipe_id
           OR related_version_id IS DISTINCT FROM row_data.recipe_version_id
           OR related_serving_code IS DISTINCT FROM row_data.menu_serving_code THEN
            RAISE EXCEPTION
                'a recipe sales snapshot does not form one recipe/version/serving chain'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF row_data.fulfillment_source = 'DIRECT_STOCK' THEN
        SELECT organization_id, item_type, tracks_lots
          INTO STRICT related_organization_id, direct_item_type, direct_item_tracks_lots
          FROM inventory_inventoryitem
         WHERE id = row_data.inventory_item_id;
        SELECT branch_id
          INTO STRICT related_branch_id
          FROM inventory_warehouse
         WHERE id = row_data.source_warehouse_id;

        expected_total := round(row_data.quantity * row_data.direct_stock_qty_per_unit, 3);
        IF related_organization_id IS DISTINCT FROM row_data.day_organization_id
           OR related_branch_id IS DISTINCT FROM row_data.day_branch_id
           OR row_data.inventory_item_id IS DISTINCT FROM row_data.menu_inventory_item_id
           OR row_data.source_warehouse_id IS DISTINCT FROM row_data.setting_warehouse_id
           OR row_data.direct_stock_qty_per_unit IS DISTINCT FROM row_data.menu_direct_quantity
           OR row_data.direct_stock_total_base_qty IS DISTINCT FROM expected_total THEN
            RAISE EXCEPTION
                'a direct-stock sales snapshot does not match its menu, branch warehouse or quantity'
                USING ERRCODE = 'check_violation';
        END IF;
        IF direct_item_type IS DISTINCT FROM 'GOODS_FOR_RESALE' OR direct_item_tracks_lots THEN
            RAISE EXCEPTION
                'a direct-stock sales snapshot requires a non-lot GOODS_FOR_RESALE item'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_day_line_snapshot_is_consistent
    AFTER INSERT OR UPDATE ON sales_salesdayline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_day_line_snapshot_is_consistent();


-- -----------------------------------------------------------------------
-- Posted day -> stock entry -> movement -> fulfillment evidence.
-- -----------------------------------------------------------------------

CREATE OR REPLACE FUNCTION sales_day_stock_link_is_consistent()
RETURNS TRIGGER AS $$
DECLARE
    row_data record;
BEGIN
    SELECT posting.id,
           day.status AS day_status,
           day.organization_id AS day_organization_id,
           day.business_date AS day_business_date,
           day.public_id AS day_public_id,
           stock.organization_id AS stock_organization_id,
           stock.business_date AS stock_business_date,
           stock.source_document_type AS stock_source_type,
           stock.source_document_id AS stock_source_id,
           stock.source_event AS stock_source_event,
           stock.reverses_id AS stock_reverses_id,
           stock.journal_entry_id,
           journal.organization_id AS journal_organization_id,
           journal.accounting_date AS journal_accounting_date,
           journal.status AS journal_status,
           journal.source_document_type AS journal_source_type,
           journal.source_document_id AS journal_source_id,
           journal.source_event AS journal_source_event
      INTO row_data
      FROM sales_salesdaystockposting AS posting
      JOIN sales_salesday AS day ON day.id = posting.sales_day_id
      JOIN inventory_stockledgerentry AS stock ON stock.id = posting.stock_entry_id
      LEFT JOIN accounting_journalentry AS journal ON journal.id = stock.journal_entry_id
     WHERE posting.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF row_data.day_status IS DISTINCT FROM 'POSTED'
       OR row_data.stock_organization_id IS DISTINCT FROM row_data.day_organization_id
       OR row_data.stock_business_date IS DISTINCT FROM row_data.day_business_date
       OR row_data.stock_source_type IS DISTINCT FROM 'SALES.SALESDAY'
       OR row_data.stock_source_id IS DISTINCT FROM row_data.day_public_id::text
       OR row_data.stock_source_event IS DISTINCT FROM 'POSTED'
       OR row_data.stock_reverses_id IS NOT NULL
       OR row_data.journal_entry_id IS NULL
       OR row_data.journal_organization_id IS DISTINCT FROM row_data.day_organization_id
       OR row_data.journal_accounting_date IS DISTINCT FROM row_data.day_business_date
       OR row_data.journal_status IS DISTINCT FROM 'POSTED'
       OR row_data.journal_source_type IS DISTINCT FROM 'SALES.SALESDAY'
       OR row_data.journal_source_id IS DISTINCT FROM row_data.day_public_id::text
       OR row_data.journal_source_event IS DISTINCT FROM 'POSTED' THEN
        RAISE EXCEPTION
            'a sales-day stock posting must link the day to its matching posted stock entry and journal'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_day_stock_link_is_consistent
    AFTER INSERT ON sales_salesdaystockposting
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_day_stock_link_is_consistent();

CREATE OR REPLACE FUNCTION sales_direct_fulfillment_is_consistent()
RETURNS TRIGGER AS $$
DECLARE
    row_data record;
BEGIN
    SELECT evidence.id,
           evidence.base_quantity,
           evidence.cogs_value,
           evidence.consumption_account_id,
           evidence.cost_center_id,
           line.fulfillment_source,
           line.inventory_item_id,
           line.source_warehouse_id,
           line.direct_stock_total_base_qty,
           day.status AS day_status,
           day.organization_id AS day_organization_id,
           day.branch_id AS day_branch_id,
           channel.cost_center_id AS channel_cost_center_id,
           account.organization_id AS account_organization_id,
           center.organization_id AS center_organization_id,
           movement.entry_id AS movement_entry_id,
           movement.organization_id AS movement_organization_id,
           movement.branch_id AS movement_branch_id,
           movement.warehouse_id AS movement_warehouse_id,
           movement.item_id AS movement_item_id,
           movement.lot_id AS movement_lot_id,
           movement.movement_type,
           movement.effect_key,
           movement.base_quantity AS movement_quantity,
           movement.inventory_value AS movement_value,
           movement.control_account_id,
           movement.reverses_id AS movement_reverses_id,
           posting.stock_entry_id AS posting_entry_id
      INTO row_data
      FROM sales_salesdirectstockfulfillment AS evidence
      JOIN sales_salesdayline AS line ON line.id = evidence.sales_line_id
      JOIN sales_salesday AS day ON day.id = line.sales_day_id
      JOIN sales_saleschannel AS channel ON channel.id = line.channel_id
      JOIN accounting_account AS account ON account.id = evidence.consumption_account_id
      JOIN accounting_costcenter AS center ON center.id = evidence.cost_center_id
      JOIN inventory_stockmovement AS movement ON movement.id = evidence.stock_movement_id
      LEFT JOIN sales_salesdaystockposting AS posting ON posting.sales_day_id = day.id
     WHERE evidence.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF row_data.day_status IS DISTINCT FROM 'POSTED'
       OR row_data.fulfillment_source IS DISTINCT FROM 'DIRECT_STOCK'
       OR row_data.posting_entry_id IS NULL
       OR row_data.movement_entry_id IS DISTINCT FROM row_data.posting_entry_id
       OR row_data.movement_organization_id IS DISTINCT FROM row_data.day_organization_id
       OR row_data.movement_branch_id IS DISTINCT FROM row_data.day_branch_id
       OR row_data.movement_warehouse_id IS DISTINCT FROM row_data.source_warehouse_id
       OR row_data.movement_item_id IS DISTINCT FROM row_data.inventory_item_id
       OR row_data.movement_lot_id IS NOT NULL
       OR row_data.movement_type IS DISTINCT FROM 'ISSUE'
       OR row_data.movement_reverses_id IS NOT NULL
       OR row_data.effect_key NOT LIKE
            ('sale:' || row_data.consumption_account_id::text || ':' || row_data.cost_center_id::text || ':%')
       OR row_data.movement_quantity IS DISTINCT FROM -row_data.direct_stock_total_base_qty
       OR row_data.movement_value > 0
       OR row_data.base_quantity IS DISTINCT FROM row_data.direct_stock_total_base_qty
       OR row_data.base_quantity IS DISTINCT FROM -row_data.movement_quantity
       OR row_data.cogs_value IS DISTINCT FROM -row_data.movement_value
       OR row_data.cost_center_id IS DISTINCT FROM row_data.channel_cost_center_id
       OR row_data.account_organization_id IS DISTINCT FROM row_data.day_organization_id
       OR row_data.center_organization_id IS DISTINCT FROM row_data.day_organization_id
       OR (row_data.cogs_value > 0 AND row_data.control_account_id IS NULL) THEN
        RAISE EXCEPTION
            'direct-stock fulfillment evidence does not match its frozen sales line and issue movement'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_direct_fulfillment_is_consistent
    AFTER INSERT ON sales_salesdirectstockfulfillment
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_direct_fulfillment_is_consistent();

CREATE OR REPLACE FUNCTION sales_day_direct_stock_is_complete()
RETURNS TRIGGER AS $$
DECLARE
    current_status text;
    direct_count bigint;
    posting_count bigint;
    evidence_count bigint;
    movement_count bigint;
    posting_entry_id bigint;
BEGIN
    SELECT status INTO current_status FROM sales_salesday WHERE id = NEW.id;
    IF NOT FOUND OR current_status IS DISTINCT FROM 'POSTED' THEN
        RETURN NEW;
    END IF;

    SELECT count(*)
      INTO direct_count
      FROM sales_salesdayline
     WHERE sales_day_id = NEW.id
       AND fulfillment_source = 'DIRECT_STOCK';

    SELECT count(*), min(stock_entry_id)
      INTO posting_count, posting_entry_id
      FROM sales_salesdaystockposting
     WHERE sales_day_id = NEW.id;

    SELECT count(*)
      INTO evidence_count
      FROM sales_salesdirectstockfulfillment AS evidence
      JOIN sales_salesdayline AS line ON line.id = evidence.sales_line_id
     WHERE line.sales_day_id = NEW.id;

    IF direct_count = 0 THEN
        IF posting_count <> 0 OR evidence_count <> 0 THEN
            RAISE EXCEPTION
                'a posted sales day without direct-stock lines may not carry stock evidence'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF posting_count <> 1 OR evidence_count <> direct_count OR EXISTS (
        SELECT 1
          FROM sales_salesdayline AS line
          LEFT JOIN sales_salesdirectstockfulfillment AS evidence
            ON evidence.sales_line_id = line.id
         WHERE line.sales_day_id = NEW.id
           AND line.fulfillment_source = 'DIRECT_STOCK'
           AND evidence.id IS NULL
    ) OR EXISTS (
        SELECT 1
          FROM sales_salesdirectstockfulfillment AS evidence
          JOIN sales_salesdayline AS line ON line.id = evidence.sales_line_id
         WHERE line.sales_day_id = NEW.id
           AND (
                line.fulfillment_source <> 'DIRECT_STOCK'
                OR evidence.stock_movement_id NOT IN (
                    SELECT id FROM inventory_stockmovement WHERE entry_id = posting_entry_id
                )
           )
    ) THEN
        RAISE EXCEPTION
            'every direct-stock sales line must have exactly one issue in the day stock posting'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT count(*)
      INTO movement_count
      FROM inventory_stockmovement
     WHERE entry_id = posting_entry_id;
    IF movement_count <> direct_count THEN
        RAISE EXCEPTION
            'the sales-day stock entry contains movements not represented by direct-stock lines'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_day_direct_stock_is_complete
    AFTER INSERT OR UPDATE ON sales_salesday
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_day_direct_stock_is_complete();


-- -----------------------------------------------------------------------
-- Posted adjustment -> restock entry -> exact return evidence.
-- -----------------------------------------------------------------------

CREATE OR REPLACE FUNCTION sales_adjustment_stock_link_is_consistent()
RETURNS TRIGGER AS $$
DECLARE
    row_data record;
BEGIN
    SELECT posting.id,
           adjustment.status AS adjustment_status,
           adjustment.organization_id AS adjustment_organization_id,
           adjustment.business_date AS adjustment_business_date,
           adjustment.public_id AS adjustment_public_id,
           stock.organization_id AS stock_organization_id,
           stock.business_date AS stock_business_date,
           stock.source_document_type AS stock_source_type,
           stock.source_document_id AS stock_source_id,
           stock.source_event AS stock_source_event,
           stock.reverses_id AS stock_reverses_id,
           stock.journal_entry_id,
           journal.organization_id AS journal_organization_id,
           journal.accounting_date AS journal_accounting_date,
           journal.status AS journal_status,
           journal.source_document_type AS journal_source_type,
           journal.source_document_id AS journal_source_id,
           journal.source_event AS journal_source_event
      INTO row_data
      FROM sales_salesadjustmentstockposting AS posting
      JOIN sales_salesadjustment AS adjustment ON adjustment.id = posting.adjustment_id
      JOIN inventory_stockledgerentry AS stock ON stock.id = posting.stock_entry_id
      LEFT JOIN accounting_journalentry AS journal ON journal.id = stock.journal_entry_id
     WHERE posting.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    IF row_data.adjustment_status IS DISTINCT FROM 'POSTED'
       OR row_data.stock_organization_id IS DISTINCT FROM row_data.adjustment_organization_id
       OR row_data.stock_business_date IS DISTINCT FROM row_data.adjustment_business_date
       OR row_data.stock_source_type IS DISTINCT FROM 'SALES.SALESADJUSTMENT'
       OR row_data.stock_source_id IS DISTINCT FROM row_data.adjustment_public_id::text
       OR row_data.stock_source_event IS DISTINCT FROM 'POSTED'
       OR row_data.stock_reverses_id IS NOT NULL
       OR row_data.journal_entry_id IS NULL
       OR row_data.journal_organization_id IS DISTINCT FROM row_data.adjustment_organization_id
       OR row_data.journal_accounting_date IS DISTINCT FROM row_data.adjustment_business_date
       OR row_data.journal_status IS DISTINCT FROM 'POSTED'
       OR row_data.journal_source_type IS DISTINCT FROM 'SALES.SALESADJUSTMENT'
       OR row_data.journal_source_id IS DISTINCT FROM row_data.adjustment_public_id::text
       OR row_data.journal_source_event IS DISTINCT FROM 'POSTED' THEN
        RAISE EXCEPTION
            'an adjustment stock posting must link the adjustment to its matching posted stock entry and journal'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_adjustment_stock_link_is_consistent
    AFTER INSERT ON sales_salesadjustmentstockposting
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_adjustment_stock_link_is_consistent();

CREATE OR REPLACE FUNCTION sales_direct_return_is_consistent()
RETURNS TRIGGER AS $$
DECLARE
    row_data record;
    expected_quantity numeric;
    active_return_quantity numeric;
    active_return_value numeric;
BEGIN
    SELECT evidence.id,
           evidence.source_fulfillment_id,
           evidence.base_quantity,
           evidence.cogs_value,
           line.original_line_id,
           line.adjusted_quantity,
           adjustment.status AS adjustment_status,
           adjustment.reason_kind,
           adjustment.direct_stock_disposition,
           adjustment.organization_id AS adjustment_organization_id,
           adjustment.branch_id AS adjustment_branch_id,
           original.fulfillment_source AS original_route,
           original.direct_stock_qty_per_unit,
           source.sales_line_id AS source_sales_line_id,
           source.stock_movement_id AS source_movement_id,
           source.base_quantity AS source_quantity,
           source.cogs_value AS source_value,
           source.consumption_account_id AS source_consumption_account_id,
           source.cost_center_id AS source_cost_center_id,
           source_movement.organization_id AS source_organization_id,
           source_movement.branch_id AS source_branch_id,
           source_movement.warehouse_id AS source_warehouse_id,
           source_movement.item_id AS source_item_id,
           source_movement.lot_id AS source_lot_id,
           source_movement.control_account_id AS source_control_account_id,
           source_movement.movement_type AS source_movement_type,
           source_movement.effect_key AS source_effect_key,
           return_movement.entry_id AS return_entry_id,
           return_movement.organization_id AS return_organization_id,
           return_movement.branch_id AS return_branch_id,
           return_movement.warehouse_id AS return_warehouse_id,
           return_movement.item_id AS return_item_id,
           return_movement.lot_id AS return_lot_id,
           return_movement.control_account_id AS return_control_account_id,
           return_movement.movement_type AS return_movement_type,
           return_movement.effect_key AS return_effect_key,
           return_movement.base_quantity AS return_quantity,
           return_movement.inventory_value AS return_value,
           return_movement.reverses_id AS return_reverses_id,
           posting.stock_entry_id AS posting_entry_id
      INTO row_data
      FROM sales_salesdirectstockreturnfulfillment AS evidence
      JOIN sales_salesadjustmentline AS line ON line.id = evidence.adjustment_line_id
      JOIN sales_salesadjustment AS adjustment ON adjustment.id = line.adjustment_id
      JOIN sales_salesdayline AS original ON original.id = line.original_line_id
      JOIN sales_salesdirectstockfulfillment AS source
        ON source.id = evidence.source_fulfillment_id
      JOIN inventory_stockmovement AS source_movement
        ON source_movement.id = source.stock_movement_id
      JOIN inventory_stockmovement AS return_movement
        ON return_movement.id = evidence.stock_movement_id
      LEFT JOIN sales_salesadjustmentstockposting AS posting
        ON posting.adjustment_id = adjustment.id
     WHERE evidence.id = NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    expected_quantity := round(row_data.adjusted_quantity * row_data.direct_stock_qty_per_unit, 3);
    IF row_data.adjustment_status IS DISTINCT FROM 'POSTED'
       OR row_data.reason_kind NOT IN ('CANCELLED_BEFORE_FULFILLMENT', 'RETURNED_AFTER_FULFILLMENT')
       OR row_data.direct_stock_disposition IS DISTINCT FROM 'RESTOCK'
       OR row_data.original_route IS DISTINCT FROM 'DIRECT_STOCK'
       OR row_data.source_sales_line_id IS DISTINCT FROM row_data.original_line_id
       OR row_data.posting_entry_id IS NULL
       OR row_data.return_entry_id IS DISTINCT FROM row_data.posting_entry_id
       OR row_data.source_organization_id IS DISTINCT FROM row_data.adjustment_organization_id
       OR row_data.source_branch_id IS DISTINCT FROM row_data.adjustment_branch_id
       OR row_data.source_movement_type IS DISTINCT FROM 'ISSUE'
       OR row_data.source_effect_key NOT LIKE
            ('sale:' || row_data.source_consumption_account_id::text || ':' || row_data.source_cost_center_id::text || ':%')
       OR row_data.return_organization_id IS DISTINCT FROM row_data.source_organization_id
       OR row_data.return_branch_id IS DISTINCT FROM row_data.source_branch_id
       OR row_data.return_warehouse_id IS DISTINCT FROM row_data.source_warehouse_id
       OR row_data.return_item_id IS DISTINCT FROM row_data.source_item_id
       OR row_data.return_lot_id IS DISTINCT FROM row_data.source_lot_id
       OR row_data.return_control_account_id IS DISTINCT FROM row_data.source_control_account_id
       OR row_data.return_movement_type IS DISTINCT FROM 'RETURN_IN'
       OR row_data.return_effect_key NOT LIKE
            ('sale-restock:' || row_data.source_movement_id::text || ':%')
       OR row_data.return_reverses_id IS NOT NULL
       OR row_data.base_quantity IS DISTINCT FROM expected_quantity
       OR row_data.base_quantity IS DISTINCT FROM row_data.return_quantity
       OR row_data.cogs_value IS DISTINCT FROM row_data.return_value
       OR row_data.return_value < 0
       OR EXISTS (
            SELECT 1
              FROM inventory_stockmovement AS reversal
             WHERE reversal.reverses_id = row_data.source_movement_id
       ) THEN
        RAISE EXCEPTION
            'direct-stock return evidence does not match its source fulfillment, adjustment and return movement'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COALESCE(sum(e.base_quantity), 0), COALESCE(sum(e.cogs_value), 0)
      INTO active_return_quantity, active_return_value
      FROM sales_salesdirectstockreturnfulfillment AS e
      JOIN inventory_stockmovement AS movement ON movement.id = e.stock_movement_id
     WHERE e.source_fulfillment_id = row_data.source_fulfillment_id
       AND NOT EXISTS (
            SELECT 1
              FROM inventory_stockmovement AS reversal
             WHERE reversal.reverses_id = movement.id
       );

    IF active_return_quantity > row_data.source_quantity
       OR active_return_value > row_data.source_value THEN
        RAISE EXCEPTION
            'active direct-stock returns may not exceed their original quantity or COGS value'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_direct_return_is_consistent
    AFTER INSERT ON sales_salesdirectstockreturnfulfillment
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_direct_return_is_consistent();

CREATE OR REPLACE FUNCTION sales_adjustment_direct_stock_is_complete()
RETURNS TRIGGER AS $$
DECLARE
    row_data record;
    direct_count bigint;
    posting_count bigint;
    evidence_count bigint;
    movement_count bigint;
    posting_entry_id bigint;
    must_restock boolean;
BEGIN
    SELECT status, reason_kind, direct_stock_disposition
      INTO row_data
      FROM sales_salesadjustment
     WHERE id = NEW.id;
    IF NOT FOUND OR row_data.status IS DISTINCT FROM 'POSTED' THEN
        RETURN NEW;
    END IF;

    SELECT count(*)
      INTO direct_count
      FROM sales_salesadjustmentline AS line
      JOIN sales_salesdayline AS original ON original.id = line.original_line_id
     WHERE line.adjustment_id = NEW.id
       AND original.fulfillment_source = 'DIRECT_STOCK'
       AND line.adjusted_quantity > 0;

    SELECT count(*), min(stock_entry_id)
      INTO posting_count, posting_entry_id
      FROM sales_salesadjustmentstockposting
     WHERE adjustment_id = NEW.id;

    SELECT count(*)
      INTO evidence_count
      FROM sales_salesdirectstockreturnfulfillment AS evidence
      JOIN sales_salesadjustmentline AS line ON line.id = evidence.adjustment_line_id
     WHERE line.adjustment_id = NEW.id;

    IF direct_count = 0 THEN
        IF row_data.direct_stock_disposition IS DISTINCT FROM 'NOT_APPLICABLE'
           OR posting_count <> 0 OR evidence_count <> 0 THEN
            RAISE EXCEPTION
                'an adjustment without direct-stock quantity must use NOT_APPLICABLE and carry no stock evidence'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF row_data.reason_kind = 'CANCELLED_BEFORE_FULFILLMENT' THEN
        IF row_data.direct_stock_disposition IS DISTINCT FROM 'RESTOCK' THEN
            RAISE EXCEPTION
                'a cancelled direct-stock sale must be returned to inventory'
                USING ERRCODE = 'check_violation';
        END IF;
        must_restock := true;
    ELSIF row_data.reason_kind = 'RETURNED_AFTER_FULFILLMENT' THEN
        IF row_data.direct_stock_disposition = 'NOT_APPLICABLE' THEN
            RAISE EXCEPTION
                'a direct-stock return must explicitly choose RESTOCK or NO_RESTOCK'
                USING ERRCODE = 'check_violation';
        END IF;
        must_restock := row_data.direct_stock_disposition = 'RESTOCK';
    ELSE
        RAISE EXCEPTION
            'a financial correction may not carry direct-stock quantity'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NOT must_restock THEN
        IF posting_count <> 0 OR evidence_count <> 0 THEN
            RAISE EXCEPTION
                'a NO_RESTOCK adjustment may not carry stock-return evidence'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF posting_count <> 1 OR evidence_count <> direct_count OR EXISTS (
        SELECT 1
          FROM sales_salesadjustmentline AS line
          JOIN sales_salesdayline AS original ON original.id = line.original_line_id
          LEFT JOIN sales_salesdirectstockreturnfulfillment AS evidence
            ON evidence.adjustment_line_id = line.id
         WHERE line.adjustment_id = NEW.id
           AND original.fulfillment_source = 'DIRECT_STOCK'
           AND line.adjusted_quantity > 0
           AND evidence.id IS NULL
    ) OR EXISTS (
        SELECT 1
          FROM sales_salesdirectstockreturnfulfillment AS evidence
          JOIN sales_salesadjustmentline AS line ON line.id = evidence.adjustment_line_id
          JOIN sales_salesdayline AS original ON original.id = line.original_line_id
         WHERE line.adjustment_id = NEW.id
           AND (
                original.fulfillment_source <> 'DIRECT_STOCK'
                OR line.adjusted_quantity <= 0
                OR evidence.stock_movement_id NOT IN (
                    SELECT id FROM inventory_stockmovement WHERE entry_id = posting_entry_id
                )
           )
    ) THEN
        RAISE EXCEPTION
            'every restocked direct-stock adjustment line must have exactly one return movement'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT count(*)
      INTO movement_count
      FROM inventory_stockmovement
     WHERE entry_id = posting_entry_id;
    IF movement_count <> direct_count THEN
        RAISE EXCEPTION
            'the adjustment stock entry contains movements not represented by returned lines'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_adjustment_direct_stock_is_complete
    AFTER INSERT OR UPDATE ON sales_salesadjustment
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_adjustment_direct_stock_is_complete();


-- A sourced sales stock entry and its sales owner must agree.
--
-- The guard proves the two sides consistent wherever both exist: every owner
-- row pointing at this entry must belong to a POSTED document whose public id
-- is the entry's source id, and a document carrying the entry's source id must
-- own the entry through exactly one row. An entry whose document is in no
-- sales table at all is outside its reach — there is nothing on the sales side
-- for it to disagree with, and the inventory adapter's own certification suite
-- posts such entries deliberately, knowing no sales model.
CREATE OR REPLACE FUNCTION sales_stock_entry_has_sales_owner()
RETURNS TRIGGER AS $$
DECLARE
    current_row record;
    link_count bigint;
    owner_count bigint;
    document_count bigint;
BEGIN
    SELECT id, source_document_type, source_document_id, source_event
      INTO current_row
      FROM inventory_stockledgerentry
     WHERE id = NEW.id;
    IF NOT FOUND OR current_row.source_event IS DISTINCT FROM 'POSTED' THEN
        RETURN NEW;
    END IF;

    IF current_row.source_document_type = 'SALES.SALESDAY' THEN
        SELECT count(*)
          INTO link_count
          FROM sales_salesdaystockposting AS posting
         WHERE posting.stock_entry_id = current_row.id;
        SELECT count(*)
          INTO owner_count
          FROM sales_salesdaystockposting AS posting
          JOIN sales_salesday AS day ON day.id = posting.sales_day_id
         WHERE posting.stock_entry_id = current_row.id
           AND day.public_id::text = current_row.source_document_id
           AND day.status = 'POSTED';
        SELECT count(*)
          INTO document_count
          FROM sales_salesday AS day
         WHERE day.public_id::text = current_row.source_document_id;
        IF link_count <> owner_count OR (document_count > 0 AND owner_count <> 1) THEN
            RAISE EXCEPTION
                'a posted sales-day stock entry requires exactly one matching sales-day owner'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF current_row.source_document_type = 'SALES.SALESADJUSTMENT' THEN
        SELECT count(*)
          INTO link_count
          FROM sales_salesadjustmentstockposting AS posting
         WHERE posting.stock_entry_id = current_row.id;
        SELECT count(*)
          INTO owner_count
          FROM sales_salesadjustmentstockposting AS posting
          JOIN sales_salesadjustment AS adjustment ON adjustment.id = posting.adjustment_id
         WHERE posting.stock_entry_id = current_row.id
           AND adjustment.public_id::text = current_row.source_document_id
           AND adjustment.status = 'POSTED';
        SELECT count(*)
          INTO document_count
          FROM sales_salesadjustment AS adjustment
         WHERE adjustment.public_id::text = current_row.source_document_id;
        IF link_count <> owner_count OR (document_count > 0 AND owner_count <> 1) THEN
            RAISE EXCEPTION
                'a posted sales-adjustment stock entry requires exactly one matching adjustment owner'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER sales_stock_entry_has_sales_owner
    AFTER INSERT OR UPDATE ON inventory_stockledgerentry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION sales_stock_entry_has_sales_owner();
"""


DROP_GUARDS = r"""
DROP TRIGGER IF EXISTS sales_stock_entry_has_sales_owner
    ON inventory_stockledgerentry;
DROP FUNCTION IF EXISTS sales_stock_entry_has_sales_owner();

DROP TRIGGER IF EXISTS sales_adjustment_direct_stock_is_complete
    ON sales_salesadjustment;
DROP FUNCTION IF EXISTS sales_adjustment_direct_stock_is_complete();
DROP TRIGGER IF EXISTS sales_direct_return_is_consistent
    ON sales_salesdirectstockreturnfulfillment;
DROP FUNCTION IF EXISTS sales_direct_return_is_consistent();
DROP TRIGGER IF EXISTS sales_adjustment_stock_link_is_consistent
    ON sales_salesadjustmentstockposting;
DROP FUNCTION IF EXISTS sales_adjustment_stock_link_is_consistent();

DROP TRIGGER IF EXISTS sales_day_direct_stock_is_complete
    ON sales_salesday;
DROP FUNCTION IF EXISTS sales_day_direct_stock_is_complete();
DROP TRIGGER IF EXISTS sales_direct_fulfillment_is_consistent
    ON sales_salesdirectstockfulfillment;
DROP FUNCTION IF EXISTS sales_direct_fulfillment_is_consistent();
DROP TRIGGER IF EXISTS sales_day_stock_link_is_consistent
    ON sales_salesdaystockposting;
DROP FUNCTION IF EXISTS sales_day_stock_link_is_consistent();

DROP TRIGGER IF EXISTS sales_day_line_snapshot_is_consistent
    ON sales_salesdayline;
DROP FUNCTION IF EXISTS sales_day_line_snapshot_is_consistent();
DROP TRIGGER IF EXISTS sales_menu_setting_scope_is_consistent
    ON sales_menuitembranchsetting;
DROP FUNCTION IF EXISTS sales_menu_setting_scope_is_consistent();
DROP TRIGGER IF EXISTS sales_menu_master_scope_is_consistent
    ON sales_menuitem;
DROP FUNCTION IF EXISTS sales_menu_master_scope_is_consistent();
DROP TRIGGER IF EXISTS sales_direct_item_dependents_are_consistent
    ON inventory_inventoryitem;
DROP FUNCTION IF EXISTS sales_direct_item_dependents_are_consistent();
DROP TRIGGER IF EXISTS sales_menu_category_dependents_are_consistent
    ON sales_menucategory;
DROP FUNCTION IF EXISTS sales_menu_category_dependents_are_consistent();

DROP TRIGGER IF EXISTS sales_adjustment_line_insert_parent_is_draft
    ON sales_salesadjustmentline;
DROP FUNCTION IF EXISTS sales_adjustment_line_insert_parent_is_draft();
DROP TRIGGER IF EXISTS sales_day_line_insert_parent_is_draft
    ON sales_salesdayline;
DROP FUNCTION IF EXISTS sales_day_line_insert_parent_is_draft();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0014_direct_stock_schema"),
    ]

    operations = [
        migrations.RunSQL(sql=GUARDS, reverse_sql=DROP_GUARDS),
    ]
