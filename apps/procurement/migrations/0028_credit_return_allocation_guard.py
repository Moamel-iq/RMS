"""
Database guard for credit-note return allocations.

The same shape as `0026`'s allocation guard, with one carve-out: posting
stamps each allocation's `settled_book_value` while the note is leaving
DRAFT, exactly as the return's own lines take their posted value — so the
trigger names the columns posting may touch and refuses every other change
outside DRAFT.
"""

from django.db import migrations

GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_credit_return_allocation_follows_note()
RETURNS TRIGGER AS $$
DECLARE
    note_status text;
    posting_columns text[] := ARRAY['settled_book_value', 'updated_at'];
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.credit_note_id IS DISTINCT FROM OLD.credit_note_id THEN
        RAISE EXCEPTION
            'credit return allocation % cannot move to another note', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO note_status
            FROM procurement_suppliercreditnote WHERE id = OLD.credit_note_id;
        IF note_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'credit note is % and its return allocations cannot be deleted', note_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    SELECT status INTO note_status
        FROM procurement_suppliercreditnote WHERE id = NEW.credit_note_id;

    IF TG_OP = 'INSERT' AND note_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'credit note is % and takes no further return allocations', note_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'UPDATE' AND note_status <> 'DRAFT' THEN
        -- The one permitted write outside DRAFT: posting stamping the
        -- settled book value it computed under locks.
        IF (to_jsonb(NEW) - posting_columns) <> (to_jsonb(OLD) - posting_columns) THEN
            RAISE EXCEPTION
                'credit note is % and its return allocations are frozen', note_status
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

GUARD_TRIGGER = """
CREATE TRIGGER procurement_credit_return_allocation_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON procurement_suppliercreditreturnallocation
    FOR EACH ROW EXECUTE FUNCTION procurement_credit_return_allocation_follows_note();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0027_credit_note_partial_settlement"),
    ]

    operations = [
        migrations.RunSQL(
            sql=GUARD_FUNCTION + GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_credit_return_allocation_frozen "
                "ON procurement_suppliercreditreturnallocation;"
                "DROP FUNCTION IF EXISTS procurement_credit_return_allocation_follows_note();"
            ),
        ),
    ]
