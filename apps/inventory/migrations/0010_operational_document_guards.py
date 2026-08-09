"""
Database guards for the operational documents.

Three things the ORM cannot express.

**A posted document is immutable, as a whole row.** The one permitted change
is the reversal transition, written as an allowlist over the columns that
transition touches — per the `accounting/0005` lesson, everything not
explicitly allowed is refused, so a column added later is protected before
anybody remembers to protect it. A reversed document is immutable outright,
and only a DRAFT may be deleted.

**Lines freeze with their document.** Once it is POSTED or REVERSED its lines
refuse insert, update, and delete. Posting itself writes the resolved accounts
and links onto the lines *before* flipping the status, which is what leaves
that window open exactly long enough and no longer.

**A line must match its document's type.** A receipt line carries an entered
unit cost; an issue takes its cost from the moving average and a return from
the issue it goes back against, so neither may carry one at entry. A return
line names the issue line it returns against; the other two must not. These
span two tables, so they are a trigger rather than a check constraint.
"""

from django.db import migrations

DOCUMENT_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION inventory_movement_document_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    allowed text[] := ARRAY[
        'status', 'reversed_by_id', 'reversed_at', 'reversal_reason',
        'reversal_journal_entry_id', 'updated_at'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'inventory document % has left draft and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'inventory document % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        IF NEW.status <> 'REVERSED'
           OR (to_jsonb(NEW) - allowed) <> (to_jsonb(OLD) - allowed) THEN
            RAISE EXCEPTION
                'inventory document % is posted; the only permitted change is its reversal',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

DOCUMENT_GUARD_TRIGGER = """
CREATE TRIGGER inventory_movement_document_immutable
    BEFORE UPDATE OR DELETE ON inventory_inventorymovementdocument
    FOR EACH ROW EXECUTE FUNCTION inventory_movement_document_is_immutable();
"""

LINE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION inventory_movement_line_follows_document()
RETURNS TRIGGER AS $$
DECLARE
    doc_status text;
    doc_type text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.document_id IS DISTINCT FROM OLD.document_id THEN
        RAISE EXCEPTION
            'inventory document line % cannot move to another document', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status, document_type INTO doc_status, doc_type
            FROM inventory_inventorymovementdocument WHERE id = OLD.document_id;
    ELSE
        SELECT status, document_type INTO doc_status, doc_type
            FROM inventory_inventorymovementdocument WHERE id = NEW.document_id;
    END IF;

    IF doc_status IN ('POSTED', 'REVERSED') THEN
        RAISE EXCEPTION
            'inventory document is % and its lines are frozen', doc_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    -- A receipt states what the goods cost; an issue and a return are valued
    -- by the ledger, so an entered cost on either would be an opinion
    -- competing with the moving average.
    --
    -- Checked on INSERT only, because that is when a line is *entered*.
    -- Posting later writes the computed cost onto the same row by UPDATE —
    -- the moving average for an issue, the original issue's cost for a
    -- return — and that write is the ledger speaking, not a user.
    IF TG_OP = 'INSERT' THEN
        IF doc_type = 'INVENTORY_RECEIPT' AND NEW.unit_cost IS NULL THEN
            RAISE EXCEPTION
                'a receipt line needs a unit cost'
                USING ERRCODE = 'check_violation';
        END IF;
        IF doc_type <> 'INVENTORY_RECEIPT' AND NEW.unit_cost IS NOT NULL THEN
            RAISE EXCEPTION
                'a % line is valued by the ledger and cannot be entered with a cost', doc_type
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    -- A return goes back against exactly one issue line; nothing else may.
    IF doc_type = 'INVENTORY_RETURN_IN' AND NEW.source_issue_line_id IS NULL THEN
        RAISE EXCEPTION
            'a return line must name the issue line it returns against'
            USING ERRCODE = 'check_violation';
    END IF;
    IF doc_type <> 'INVENTORY_RETURN_IN' AND NEW.source_issue_line_id IS NOT NULL THEN
        RAISE EXCEPTION
            'only a return line may name a source issue line'
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

LINE_GUARD_TRIGGER = """
CREATE TRIGGER inventory_movement_line_frozen_with_document
    BEFORE INSERT OR UPDATE OR DELETE ON inventory_inventorymovementdocumentline
    FOR EACH ROW EXECUTE FUNCTION inventory_movement_line_follows_document();
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS inventory_movement_document_immutable
    ON inventory_inventorymovementdocument;
DROP TRIGGER IF EXISTS inventory_movement_line_frozen_with_document
    ON inventory_inventorymovementdocumentline;
DROP FUNCTION IF EXISTS inventory_movement_document_is_immutable();
DROP FUNCTION IF EXISTS inventory_movement_line_follows_document();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0009_operational_documents"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                DOCUMENT_GUARD_FUNCTION
                + DOCUMENT_GUARD_TRIGGER
                + LINE_GUARD_FUNCTION
                + LINE_GUARD_TRIGGER
            ),
            reverse_sql=DROP_ALL,
        ),
    ]
