"""
Database guards for reason codes, waste, physical counts and adjustments.

Seven things the ORM cannot express.

**A reason code's identity is frozen.** The code and what it applies to are
what a year of postings mean by it; the names and the evidence rules are how
it is presented. Renaming `SPOIL` clarifies the record, repointing it at count
variances rewrites it — and no reader of a waste report could tell that had
happened.

**A reason code belongs to a waste line and to no other.** The document type
lives on the parent row, so this is a trigger rather than a check constraint:
the line cannot see its own document's type without looking.

**A count's book snapshot is immutable after the cutoff**, and its counted
figures are immutable after submission. Between them these are what make the
variance mean anything: a book column edited later would let somebody produce
whatever variance they wanted, and a counted column edited after submission
would let the conductor answer the approver's question after seeing it.

**A finished count is immutable as a whole row**, with the same allowlist idiom
migrations 0010 and 0013 use — everything not explicitly permitted is refused,
so a column added later is protected before anybody remembers to protect it.

**A frozen warehouse names an active count.** The single most important
invariant in this task: `frozen_by_count` is the *only* statement that a
warehouse is frozen, so a row pointing at a posted, cancelled or reversed count
would be a warehouse frozen by nothing, shut until somebody found the column by
hand. Deferred, because approval legitimately writes the count to POSTED and
then clears the warehouse, in that order, inside one transaction.

**A posted adjustment and its lines are immutable**, and a line cannot move to
another document.
"""

from django.db import migrations

REASON_CODE_IDENTITY = """
CREATE OR REPLACE FUNCTION inventory_reason_code_identity_is_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'a reason code is archived, never deleted: its code stays reserved'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.code IS DISTINCT FROM OLD.code THEN
        RAISE EXCEPTION 'the code of an inventory reason code is immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.applies_to IS DISTINCT FROM OLD.applies_to THEN
        RAISE EXCEPTION
            'reason code % applies to %, and repurposing it would restate every '
            'document already posted against it', OLD.code, OLD.applies_to
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER inventory_reason_code_identity_is_immutable
    BEFORE UPDATE OR DELETE ON inventory_inventoryreasoncode
    FOR EACH ROW EXECUTE FUNCTION inventory_reason_code_identity_is_immutable();
"""

WASTE_LINE_REASON = """
CREATE OR REPLACE FUNCTION inventory_document_line_reason_matches_type()
RETURNS TRIGGER AS $$
DECLARE
    doc_type text;
    doc_org integer;
    reason_application text;
    reason_org integer;
    reason_active boolean;
BEGIN
    SELECT document_type, organization_id INTO doc_type, doc_org
    FROM inventory_inventorymovementdocument
    WHERE id = NEW.document_id;

    IF doc_type = 'INVENTORY_WASTE' THEN
        IF NEW.reason_code_id IS NULL THEN
            RAISE EXCEPTION 'a waste line needs a reason code'
                USING ERRCODE = 'check_violation';
        END IF;
        SELECT applies_to, organization_id, is_active
        INTO reason_application, reason_org, reason_active
        FROM inventory_inventoryreasoncode
        WHERE id = NEW.reason_code_id;

        IF reason_org IS DISTINCT FROM doc_org THEN
            RAISE EXCEPTION 'reason code belongs to another organization'
                USING ERRCODE = 'check_violation';
        END IF;
        IF reason_application <> 'WASTE' THEN
            RAISE EXCEPTION 'reason code applies to %, not to waste', reason_application
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.reason_code_id IS NOT NULL THEN
        RAISE EXCEPTION 'a % line takes no reason code', doc_type
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER inventory_document_line_reason_matches_type
    BEFORE INSERT OR UPDATE ON inventory_inventorymovementdocumentline
    FOR EACH ROW EXECUTE FUNCTION inventory_document_line_reason_matches_type();
"""

COUNT_IMMUTABLE = """
CREATE OR REPLACE FUNCTION inventory_stock_count_is_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'count % is % and cannot be deleted; cancel it instead', OLD.id, OLD.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- The cutoff and the identity it was taken under never move once taken.
    IF OLD.status <> 'DRAFT' THEN
        IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
           OR NEW.branch_id IS DISTINCT FROM OLD.branch_id
           OR NEW.warehouse_id IS DISTINCT FROM OLD.warehouse_id
           OR NEW.public_id IS DISTINCT FROM OLD.public_id
           OR NEW.count_number IS DISTINCT FROM OLD.count_number
           OR NEW.scope_type IS DISTINCT FROM OLD.scope_type
           OR NEW.cutoff_at IS DISTINCT FROM OLD.cutoff_at
           OR NEW.business_date IS DISTINCT FROM OLD.business_date
           OR NEW.business_date_timezone IS DISTINCT FROM OLD.business_date_timezone
           OR NEW.business_day_start IS DISTINCT FROM OLD.business_day_start
           OR NEW.conducted_by_id IS DISTINCT FROM OLD.conducted_by_id
           OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
            RAISE EXCEPTION
                'the cutoff and identity of count % are frozen once it starts', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    -- A finished count moves nothing at all. REVERSED is reachable from
    -- POSTED, and is the single exception.
    IF OLD.status IN ('CANCELLED', 'REVERSED') THEN
        RAISE EXCEPTION 'count % is % and is immutable', OLD.id, OLD.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.status = 'POSTED' AND NEW.status <> 'REVERSED' THEN
        RAISE EXCEPTION
            'count % is posted; the only change left to it is a reversal', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER inventory_stock_count_is_immutable
    BEFORE UPDATE OR DELETE ON inventory_stockcount
    FOR EACH ROW EXECUTE FUNCTION inventory_stock_count_is_immutable();
"""

COUNT_LINE_FROZEN = """
CREATE OR REPLACE FUNCTION inventory_stock_count_line_follows_count()
RETURNS TRIGGER AS $$
DECLARE
    parent_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status FROM inventory_stockcount WHERE id = OLD.count_id;
        IF parent_status IS NOT NULL AND parent_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'count is % and its lines cannot be removed', parent_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.count_id IS DISTINCT FROM OLD.count_id THEN
        RAISE EXCEPTION 'a count line cannot be moved to another count'
            USING ERRCODE = 'restrict_violation';
    END IF;

    SELECT status INTO parent_status FROM inventory_stockcount WHERE id = NEW.count_id;

    IF TG_OP = 'INSERT' THEN
        -- Lines appear when the count starts, and unexpected stock is added
        -- while it is being counted. Never afterwards.
        IF parent_status NOT IN ('IN_PROGRESS') THEN
            RAISE EXCEPTION
                'count is % and takes no further lines', parent_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- The book photograph never changes after it is taken. Not once, not by a
    -- correction, not by a bulk update: it is the only record of what the
    -- ledger said at the cutoff, and the variance is computed from it.
    IF NEW.book_quantity IS DISTINCT FROM OLD.book_quantity
       OR NEW.book_value IS DISTINCT FROM OLD.book_value
       OR NEW.book_average IS DISTINCT FROM OLD.book_average
       OR NEW.book_control_account_id IS DISTINCT FROM OLD.book_control_account_id
       OR NEW.book_last_movement_id IS DISTINCT FROM OLD.book_last_movement_id
       OR NEW.book_posted_sequence IS DISTINCT FROM OLD.book_posted_sequence
       OR NEW.item_id IS DISTINCT FROM OLD.item_id
       OR NEW.lot_id IS DISTINCT FROM OLD.lot_id
       OR NEW.is_unexpected IS DISTINCT FROM OLD.is_unexpected THEN
        RAISE EXCEPTION
            'the book snapshot of a count line is fixed at the cutoff'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF parent_status IN ('CANCELLED', 'REVERSED') THEN
        RAISE EXCEPTION 'count is % and its lines are frozen', parent_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- After submission the counted figures are the approver's evidence. What
    -- may still change is what the approver themselves decides: the unit cost
    -- for a gain the books cannot value, and the posted result.
    IF parent_status IN ('SUBMITTED', 'POSTED') THEN
        IF NEW.counted_quantity IS DISTINCT FROM OLD.counted_quantity
           OR NEW.package_conversion_id IS DISTINCT FROM OLD.package_conversion_id
           OR NEW.entered_package_quantity IS DISTINCT FROM OLD.entered_package_quantity
           OR NEW.measured_base_quantity IS DISTINCT FROM OLD.measured_base_quantity
           OR NEW.line_note IS DISTINCT FROM OLD.line_note THEN
            RAISE EXCEPTION
                'the counted figures of a submitted count are frozen'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER inventory_stock_count_line_follows_count
    BEFORE INSERT OR UPDATE OR DELETE ON inventory_stockcountline
    FOR EACH ROW EXECUTE FUNCTION inventory_stock_count_line_follows_count();
"""

FREEZE_OWNERSHIP = """
CREATE OR REPLACE FUNCTION inventory_warehouse_freeze_owner_is_active()
RETURNS TRIGGER AS $$
DECLARE
    owner_status text;
    owner_warehouse integer;
BEGIN
    IF NEW.frozen_by_count_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT status, warehouse_id INTO owner_status, owner_warehouse
    FROM inventory_stockcount
    WHERE id = NEW.frozen_by_count_id;

    IF owner_status IS NULL OR owner_status NOT IN ('IN_PROGRESS', 'SUBMITTED') THEN
        RAISE EXCEPTION
            'warehouse % cannot be frozen by count % because that count is %',
            NEW.code, NEW.frozen_by_count_id, COALESCE(owner_status, 'missing')
            USING ERRCODE = 'check_violation';
    END IF;
    IF owner_warehouse IS DISTINCT FROM NEW.id THEN
        RAISE EXCEPTION
            'count % is counting a different warehouse', NEW.frozen_by_count_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- **Immediate**, and the choice is load-bearing. A deferred constraint trigger
-- captures NEW as it was when the statement ran and re-evaluates its queries at
-- commit; so the `start_count` write that froze the warehouse would be re-judged
-- against the count's *final* status and fail for every count that ever posted.
-- Immediate checking means the services must release the freeze **before**
-- moving the count out of an active state, which is the order they use.
CREATE TRIGGER inventory_warehouse_freeze_owner_is_active
    BEFORE INSERT OR UPDATE ON inventory_warehouse
    FOR EACH ROW EXECUTE FUNCTION inventory_warehouse_freeze_owner_is_active();


-- The same invariant from the other side, which a trigger on the warehouse
-- alone cannot see: nothing may finish a count while a warehouse still names
-- it. Without this, releasing the freeze could simply be forgotten and the
-- warehouse would stay shut, pointed at a count that had nothing left to do.
CREATE OR REPLACE FUNCTION inventory_count_releases_its_freeze()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN ('IN_PROGRESS', 'SUBMITTED')
       AND NEW.status NOT IN ('IN_PROGRESS', 'SUBMITTED')
       AND EXISTS (
           SELECT 1 FROM inventory_warehouse WHERE frozen_by_count_id = NEW.id
       ) THEN
        RAISE EXCEPTION
            'count % cannot become % while it still holds a warehouse frozen',
            OLD.id, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER inventory_count_releases_its_freeze
    BEFORE UPDATE ON inventory_stockcount
    FOR EACH ROW EXECUTE FUNCTION inventory_count_releases_its_freeze();
"""

ADJUSTMENT_IMMUTABLE = """
CREATE OR REPLACE FUNCTION inventory_adjustment_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    permitted inventory_inventoryadjustmentdocument%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'adjustment % is % and cannot be deleted', OLD.id, OLD.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'DRAFT' THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION 'adjustment % is reversed and is immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Posted: the one permitted transition is the reversal, and it may write
    -- only the columns that record one. Everything else — the warehouse, the
    -- dates, the amounts, the postings — stays exactly as posted.
    permitted := OLD;
    permitted.status := NEW.status;
    permitted.reversed_by_id := NEW.reversed_by_id;
    permitted.reversed_at := NEW.reversed_at;
    permitted.reversal_reason := NEW.reversal_reason;
    permitted.reversal_journal_entry_id := NEW.reversal_journal_entry_id;
    permitted.updated_at := NEW.updated_at;
    IF NEW.status = 'REVERSED' AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'adjustment % is posted and is immutable', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER inventory_adjustment_is_immutable
    BEFORE UPDATE OR DELETE ON inventory_inventoryadjustmentdocument
    FOR EACH ROW EXECUTE FUNCTION inventory_adjustment_is_immutable();


CREATE OR REPLACE FUNCTION inventory_adjustment_line_follows_document()
RETURNS TRIGGER AS $$
DECLARE
    parent_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status
        FROM inventory_inventoryadjustmentdocument WHERE id = OLD.document_id;
        IF parent_status IS NOT NULL AND parent_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'adjustment is % and its lines cannot be removed', parent_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.document_id IS DISTINCT FROM OLD.document_id THEN
        RAISE EXCEPTION 'an adjustment line cannot be moved to another document'
            USING ERRCODE = 'restrict_violation';
    END IF;

    SELECT status INTO parent_status
    FROM inventory_inventoryadjustmentdocument WHERE id = NEW.document_id;

    IF TG_OP = 'INSERT' THEN
        IF parent_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'adjustment is % and takes no further lines', parent_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- Posting writes the movement, the value and the account onto a line whose
    -- parent is still DRAFT at that moment; a reversal writes nothing. So what
    -- a non-draft parent permits is nothing at all.
    IF parent_status <> 'DRAFT' THEN
        RAISE EXCEPTION 'adjustment is % and its lines are frozen', parent_status
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER inventory_adjustment_line_follows_document
    BEFORE INSERT OR UPDATE OR DELETE ON inventory_inventoryadjustmentline
    FOR EACH ROW EXECUTE FUNCTION inventory_adjustment_line_follows_document();
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS inventory_adjustment_line_follows_document
    ON inventory_inventoryadjustmentline;
DROP TRIGGER IF EXISTS inventory_adjustment_is_immutable
    ON inventory_inventoryadjustmentdocument;
DROP TRIGGER IF EXISTS inventory_warehouse_freeze_owner_is_active ON inventory_warehouse;
DROP TRIGGER IF EXISTS inventory_count_releases_its_freeze ON inventory_stockcount;
DROP TRIGGER IF EXISTS inventory_stock_count_line_follows_count ON inventory_stockcountline;
DROP TRIGGER IF EXISTS inventory_stock_count_is_immutable ON inventory_stockcount;
DROP FUNCTION IF EXISTS inventory_count_releases_its_freeze();
DROP TRIGGER IF EXISTS inventory_document_line_reason_matches_type
    ON inventory_inventorymovementdocumentline;
DROP TRIGGER IF EXISTS inventory_reason_code_identity_is_immutable
    ON inventory_inventoryreasoncode;
DROP FUNCTION IF EXISTS inventory_adjustment_line_follows_document();
DROP FUNCTION IF EXISTS inventory_adjustment_is_immutable();
DROP FUNCTION IF EXISTS inventory_warehouse_freeze_owner_is_active();
DROP FUNCTION IF EXISTS inventory_stock_count_line_follows_count();
DROP FUNCTION IF EXISTS inventory_stock_count_is_immutable();
DROP FUNCTION IF EXISTS inventory_document_line_reason_matches_type();
DROP FUNCTION IF EXISTS inventory_reason_code_identity_is_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0014_task_1_6_waste_counts_adjustments"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                REASON_CODE_IDENTITY
                + WASTE_LINE_REASON
                + COUNT_IMMUTABLE
                + COUNT_LINE_FROZEN
                + FREEZE_OWNERSHIP
                + ADJUSTMENT_IMMUTABLE
            ),
            reverse_sql=DROP_ALL,
        ),
    ]
