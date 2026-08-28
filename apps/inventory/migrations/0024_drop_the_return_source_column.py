"""
Drop the column that only a return-from-issue ever filled.

`source_issue_line` named the posted issue a return went back against. There
is no return document any more, and the guard installed by migration 0010
already refused the column on every other type — so the column is unreachable
as well as unused, and it holds no rows: the two return drafts went in 0022.

The guard has to be rewritten before the column can go, because it reads it.
Its receipt clauses go at the same time: they name a document type that no
longer exists, and a rule about a type nothing can create is a rule nobody can
read. What survives is the part that still means something — a line may not
move between documents, a posted document's lines are frozen, and nothing in
this family enters a cost of its own, because the ledger decides all of them.
"""

from django.db import migrations

LINE_GUARD_WITHOUT_THE_WITHDRAWN_TYPES = """
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

    -- Every surviving type is valued by the ledger, so an entered cost would
    -- be an opinion competing with the moving average.
    --
    -- Checked on INSERT only, because that is when a line is *entered*.
    -- Posting later writes the computed cost onto the same row by UPDATE, and
    -- that write is the ledger speaking, not a user.
    IF TG_OP = 'INSERT' AND NEW.unit_cost IS NOT NULL THEN
        RAISE EXCEPTION
            'a % line is valued by the ledger and cannot be entered with a cost', doc_type
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: Exactly the function migration 0010 installed, for `migrate` backwards.
LINE_GUARD_WITH_THE_WITHDRAWN_TYPES = """
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


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0023_withdraw_receipt_return_reason_schema"),
    ]

    operations = [
        migrations.RunSQL(
            LINE_GUARD_WITHOUT_THE_WITHDRAWN_TYPES,
            reverse_sql=LINE_GUARD_WITH_THE_WITHDRAWN_TYPES,
        ),
        migrations.RemoveField(
            model_name="inventorymovementdocumentline",
            name="source_issue_line",
        ),
    ]
