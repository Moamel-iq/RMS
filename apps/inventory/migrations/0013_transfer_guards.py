"""
Database guards for transfers, and the conditional control-account invariant.

Six things the ORM cannot express.

**A stock ledger entry may name its journal, once.** Task 1.2 froze the entry
outright except for the reversal back-link. Task 1.5 adds one more permitted
transition — NULL to non-NULL on `journal_entry_id` — because the journal
cannot exist before the values the kernel computes, so the link has to be
written a moment after the entry. Everything else stays refused.

**An accounted posting names an account for every dinar it moved.** If an
entry reached the general ledger, each of its value-bearing movements must
carry the control account that value moved through (§S). Deferred to commit on
purpose: the entry, its movements and its journal are written in that order
inside one transaction, and a check that fired immediately would fail on the
correct sequence rather than on the incorrect one. The column stays nullable so
the bare kernel — a focused test, a tool with no accounting in play — keeps
working; what this forbids is reaching the GL *without* one.

**A transfer's two warehouses are real, distinct, and not the system one.**
Same organization, both active, neither in-transit. Four rules over rows in
another table, so a trigger and not a check constraint. The in-transit
warehouse is chosen by the posting service from the source branch and is never
a user's answer — offering it in a selector would let somebody dispatch goods
out of the place goods in flight are already sitting.

**A posted transfer, receipt and shortage are immutable, as whole rows**, with
the same allowlist idiom migration 0010 uses: everything not explicitly
permitted is refused, so a column added later is protected before anybody
remembers to protect it. The transfer's allowlist is wider than the others'
because its status is *computed* — a receipt or a shortage legitimately moves
it between DISPATCHED, PARTIALLY_RECEIVED, COMPLETED and CLOSED_WITH_SHORTAGE
long after dispatch — but its dispatch facts stay frozen.

**Their lines freeze once posted**, and a line cannot move to another parent.

**Nothing may be received or written off that was not dispatched.** The
per-line check constraint bounds the *retained* remaining balance; this bounds
the sum of what the children actually claim, which is the same rule seen from
the other side and the one that catches a child written without its parent
being updated.
"""

from django.db import migrations

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

    -- Permitted transition one: link a reversal to what it reverses, once,
    -- moving nothing else.
    permitted := OLD;
    permitted.reverses_id := NEW.reverses_id;
    permitted.updated_at := NEW.updated_at;
    IF OLD.reverses_id IS NULL
       AND NEW.reverses_id IS NOT NULL
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    -- Permitted transition two: name the journal this posting produced, once.
    -- The journal is written a moment after the entry because it needs the
    -- values the kernel computes, so this cannot be an insert-time column.
    permitted := OLD;
    permitted.journal_entry_id := NEW.journal_entry_id;
    permitted.updated_at := NEW.updated_at;
    IF OLD.journal_entry_id IS NULL
       AND NEW.journal_entry_id IS NOT NULL
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'a stock ledger entry is immutable'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

RESTORE_ENTRY_IS_IMMUTABLE = """
CREATE OR REPLACE FUNCTION inventory_stock_entry_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    permitted inventory_stockledgerentry%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a stock ledger entry cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.source_document_type IS DISTINCT FROM OLD.source_document_type
       OR NEW.source_document_id IS DISTINCT FROM OLD.source_document_id
       OR NEW.source_event IS DISTINCT FROM OLD.source_event THEN
        RAISE EXCEPTION
            'the source identity of a stock ledger entry is immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;

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
"""

ACCOUNTED_MOVEMENTS = """
CREATE OR REPLACE FUNCTION inventory_accounted_entry_has_control_accounts()
RETURNS TRIGGER AS $$
DECLARE
    offender bigint;
BEGIN
    IF NEW.journal_entry_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT id INTO offender
        FROM inventory_stockmovement
        WHERE entry_id = NEW.id
          AND inventory_value <> 0
          AND control_account_id IS NULL
        LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION
            'movement % moved value under a journalled posting with no control account',
            offender
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER stock_entry_accounted_movements_have_accounts
    AFTER INSERT OR UPDATE ON inventory_stockledgerentry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION inventory_accounted_entry_has_control_accounts();

CREATE OR REPLACE FUNCTION inventory_accounted_movement_has_control_account()
RETURNS TRIGGER AS $$
DECLARE
    journalled bigint;
BEGIN
    IF NEW.inventory_value = 0 OR NEW.control_account_id IS NOT NULL THEN
        RETURN NEW;
    END IF;
    SELECT journal_entry_id INTO journalled
        FROM inventory_stockledgerentry WHERE id = NEW.entry_id;
    IF journalled IS NOT NULL THEN
        RAISE EXCEPTION
            'movement % moved value under a journalled posting with no control account',
            NEW.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The mirror of the trigger above, for the other write order: a movement
-- inserted after its entry already named a journal would slip past a check
-- that only looked at the entry.
CREATE CONSTRAINT TRIGGER stock_movement_accounted_has_control_account
    AFTER INSERT ON inventory_stockmovement
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION inventory_accounted_movement_has_control_account();
"""

TRANSFER_WAREHOUSES = """
CREATE OR REPLACE FUNCTION inventory_transfer_warehouses_are_valid()
RETURNS TRIGGER AS $$
DECLARE
    source_org bigint;
    destination_org bigint;
    source_kind text;
    destination_kind text;
    source_live boolean;
    destination_live boolean;
BEGIN
    SELECT b.organization_id, w.warehouse_type, w.is_active
        INTO source_org, source_kind, source_live
        FROM inventory_warehouse w
        JOIN organizations_branch b ON b.id = w.branch_id
        WHERE w.id = NEW.source_warehouse_id;

    SELECT b.organization_id, w.warehouse_type, w.is_active
        INTO destination_org, destination_kind, destination_live
        FROM inventory_warehouse w
        JOIN organizations_branch b ON b.id = w.branch_id
        WHERE w.id = NEW.destination_warehouse_id;

    IF source_org IS DISTINCT FROM NEW.organization_id
       OR destination_org IS DISTINCT FROM NEW.organization_id THEN
        RAISE EXCEPTION
            'a stock transfer stays inside one organization'
            USING ERRCODE = 'check_violation';
    END IF;

    IF source_kind = 'IN_TRANSIT' OR destination_kind = 'IN_TRANSIT' THEN
        RAISE EXCEPTION
            'the in-transit warehouse is chosen by the posting service, never by a user'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NOT source_live OR NOT destination_live THEN
        RAISE EXCEPTION
            'a stock transfer needs two active warehouses'
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_transfer_warehouses_valid
    BEFORE INSERT OR UPDATE ON inventory_stocktransfer
    FOR EACH ROW EXECUTE FUNCTION inventory_transfer_warehouses_are_valid();
"""

TRANSFER_IMMUTABLE = """
CREATE OR REPLACE FUNCTION inventory_stock_transfer_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    -- Wider than the operational-document allowlist, and deliberately: a
    -- transfer's status is recomputed from its posted children for as long as
    -- it stays open, so status is a permitted change at every stage. What is
    -- frozen is the dispatch itself — the warehouses, the dates, the postings.
    allowed text[] := ARRAY[
        'status', 'reversed_by_id', 'reversed_at', 'reversal_reason',
        'reversal_journal_entry_id', 'updated_at'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'stock transfer % has been dispatched and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'stock transfer % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status <> 'DRAFT'
       AND (to_jsonb(NEW) - allowed) <> (to_jsonb(OLD) - allowed) THEN
        RAISE EXCEPTION
            'stock transfer % is dispatched; only its status and reversal may change',
            OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_transfer_immutable
    BEFORE UPDATE OR DELETE ON inventory_stocktransfer
    FOR EACH ROW EXECUTE FUNCTION inventory_stock_transfer_is_immutable();

CREATE OR REPLACE FUNCTION inventory_transfer_line_follows_transfer()
RETURNS TRIGGER AS $$
DECLARE
    parent_status text;
    -- A dispatched line keeps its own remaining balance, which every receipt
    -- and the shortage closure decrement. Everything describing what was
    -- dispatched is frozen with the transfer.
    allowed text[] := ARRAY['remaining_quantity', 'remaining_value', 'updated_at'];
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.transfer_id IS DISTINCT FROM OLD.transfer_id THEN
        RAISE EXCEPTION
            'transfer line % cannot move to another transfer', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status
            FROM inventory_stocktransfer WHERE id = OLD.transfer_id;
        IF parent_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'transfer is % and its lines are frozen', parent_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    SELECT status INTO parent_status
        FROM inventory_stocktransfer WHERE id = NEW.transfer_id;

    IF TG_OP = 'INSERT' AND parent_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'transfer is % and takes no further lines', parent_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.source_movement_id IS NOT NULL
       AND (to_jsonb(NEW) - allowed) <> (to_jsonb(OLD) - allowed) THEN
        RAISE EXCEPTION
            'transfer line % is dispatched; only its remaining balance may change', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_transfer_line_follows_transfer
    BEFORE INSERT OR UPDATE OR DELETE ON inventory_stocktransferline
    FOR EACH ROW EXECUTE FUNCTION inventory_transfer_line_follows_transfer();
"""

CHILD_EVENT_IMMUTABLE = """
CREATE OR REPLACE FUNCTION inventory_transfer_event_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    allowed text[] := ARRAY[
        'status', 'reversed_by_id', 'reversed_at', 'reversal_reason',
        'source_reversal_journal_entry_id', 'destination_reversal_journal_entry_id',
        'reversal_journal_entry_id', 'updated_at'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'transfer event % has been posted and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'transfer event % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        IF NEW.status <> 'REVERSED'
           OR (to_jsonb(NEW) - allowed) <> (to_jsonb(OLD) - allowed) THEN
            RAISE EXCEPTION
                'transfer event % is posted; the only permitted change is its reversal',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_transfer_receipt_immutable
    BEFORE UPDATE OR DELETE ON inventory_stocktransferreceipt
    FOR EACH ROW EXECUTE FUNCTION inventory_transfer_event_is_immutable();

CREATE TRIGGER stock_transfer_shortage_immutable
    BEFORE UPDATE OR DELETE ON inventory_stocktransfershortage
    FOR EACH ROW EXECUTE FUNCTION inventory_transfer_event_is_immutable();

CREATE OR REPLACE FUNCTION inventory_transfer_receipt_line_follows_receipt()
RETURNS TRIGGER AS $$
DECLARE
    parent_status text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.receipt_id IS DISTINCT FROM OLD.receipt_id THEN
        RAISE EXCEPTION
            'transfer receipt line % cannot move to another receipt', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status
            FROM inventory_stocktransferreceipt WHERE id = OLD.receipt_id;
    ELSE
        SELECT status INTO parent_status
            FROM inventory_stocktransferreceipt WHERE id = NEW.receipt_id;
    END IF;
    IF parent_status IN ('POSTED', 'REVERSED') THEN
        RAISE EXCEPTION
            'transfer receipt is % and its lines are frozen', parent_status
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_transfer_receipt_line_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON inventory_stocktransferreceiptline
    FOR EACH ROW EXECUTE FUNCTION inventory_transfer_receipt_line_follows_receipt();

CREATE OR REPLACE FUNCTION inventory_transfer_shortage_line_follows_shortage()
RETURNS TRIGGER AS $$
DECLARE
    parent_status text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.shortage_id IS DISTINCT FROM OLD.shortage_id THEN
        RAISE EXCEPTION
            'transfer shortage line % cannot move to another shortage', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status
            FROM inventory_stocktransfershortage WHERE id = OLD.shortage_id;
    ELSE
        SELECT status INTO parent_status
            FROM inventory_stocktransfershortage WHERE id = NEW.shortage_id;
    END IF;
    IF parent_status IN ('POSTED', 'REVERSED') THEN
        RAISE EXCEPTION
            'transfer shortage is % and its lines are frozen', parent_status
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_transfer_shortage_line_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON inventory_stocktransfershortageline
    FOR EACH ROW EXECUTE FUNCTION inventory_transfer_shortage_line_follows_shortage();
"""

RESOLVED_NEVER_EXCEEDS_DISPATCH = """
CREATE OR REPLACE FUNCTION inventory_transfer_resolution_within_dispatch()
RETURNS TRIGGER AS $$
DECLARE
    line_id bigint;
    dispatched numeric;
    dispatched_value numeric;
    taken numeric;
    taken_value numeric;
BEGIN
    line_id := NEW.transfer_line_id;

    SELECT base_quantity, COALESCE(total_value, 0)
        INTO dispatched, dispatched_value
        FROM inventory_stocktransferline WHERE id = line_id;

    SELECT
        COALESCE(SUM(rl.base_quantity), 0),
        COALESCE(SUM(COALESCE(rl.allocated_value, 0)), 0)
        INTO taken, taken_value
        FROM inventory_stocktransferreceiptline rl
        JOIN inventory_stocktransferreceipt r ON r.id = rl.receipt_id
        WHERE rl.transfer_line_id = line_id AND r.status = 'POSTED';

    SELECT taken + COALESCE(SUM(sl.base_quantity), 0),
           taken_value + COALESCE(SUM(COALESCE(sl.allocated_value, 0)), 0)
        INTO taken, taken_value
        FROM inventory_stocktransfershortageline sl
        JOIN inventory_stocktransfershortage s ON s.id = sl.shortage_id
        WHERE sl.transfer_line_id = line_id AND s.status = 'POSTED';

    IF taken > dispatched THEN
        RAISE EXCEPTION
            'transfer line % has % dispatched and % resolved', line_id, dispatched, taken
            USING ERRCODE = 'check_violation';
    END IF;
    IF taken_value > dispatched_value THEN
        RAISE EXCEPTION
            'transfer line % dispatched % of value and % is resolved',
            line_id, dispatched_value, taken_value
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Deferred, because posting writes the child lines and flips the parent to
-- POSTED in that order; an immediate check would see lines that are not yet
-- claimed by anything and then never see them again.
CREATE CONSTRAINT TRIGGER transfer_receipt_line_within_dispatch
    AFTER INSERT OR UPDATE ON inventory_stocktransferreceiptline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION inventory_transfer_resolution_within_dispatch();

CREATE CONSTRAINT TRIGGER transfer_shortage_line_within_dispatch
    AFTER INSERT OR UPDATE ON inventory_stocktransfershortageline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION inventory_transfer_resolution_within_dispatch();
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS transfer_shortage_line_within_dispatch
    ON inventory_stocktransfershortageline;
DROP TRIGGER IF EXISTS transfer_receipt_line_within_dispatch
    ON inventory_stocktransferreceiptline;
DROP TRIGGER IF EXISTS stock_transfer_shortage_line_frozen
    ON inventory_stocktransfershortageline;
DROP TRIGGER IF EXISTS stock_transfer_receipt_line_frozen
    ON inventory_stocktransferreceiptline;
DROP TRIGGER IF EXISTS stock_transfer_shortage_immutable ON inventory_stocktransfershortage;
DROP TRIGGER IF EXISTS stock_transfer_receipt_immutable ON inventory_stocktransferreceipt;
DROP TRIGGER IF EXISTS stock_transfer_line_follows_transfer ON inventory_stocktransferline;
DROP TRIGGER IF EXISTS stock_transfer_immutable ON inventory_stocktransfer;
DROP TRIGGER IF EXISTS stock_transfer_warehouses_valid ON inventory_stocktransfer;
DROP TRIGGER IF EXISTS stock_movement_accounted_has_control_account ON inventory_stockmovement;
DROP TRIGGER IF EXISTS stock_entry_accounted_movements_have_accounts
    ON inventory_stockledgerentry;
DROP FUNCTION IF EXISTS inventory_transfer_resolution_within_dispatch();
DROP FUNCTION IF EXISTS inventory_transfer_shortage_line_follows_shortage();
DROP FUNCTION IF EXISTS inventory_transfer_receipt_line_follows_receipt();
DROP FUNCTION IF EXISTS inventory_transfer_event_is_immutable();
DROP FUNCTION IF EXISTS inventory_transfer_line_follows_transfer();
DROP FUNCTION IF EXISTS inventory_stock_transfer_is_immutable();
DROP FUNCTION IF EXISTS inventory_transfer_warehouses_are_valid();
DROP FUNCTION IF EXISTS inventory_accounted_movement_has_control_account();
DROP FUNCTION IF EXISTS inventory_accounted_entry_has_control_accounts();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0012_transfer_aggregate"),
    ]

    operations = [
        migrations.RunSQL(sql=ENTRY_IS_IMMUTABLE, reverse_sql=RESTORE_ENTRY_IS_IMMUTABLE),
        migrations.RunSQL(
            sql=(
                ACCOUNTED_MOVEMENTS
                + TRANSFER_WAREHOUSES
                + TRANSFER_IMMUTABLE
                + CHILD_EVENT_IMMUTABLE
                + RESOLVED_NEVER_EXCEEDS_DISPATCH
            ),
            reverse_sql=DROP_ALL,
        ),
    ]
