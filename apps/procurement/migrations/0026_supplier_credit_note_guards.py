"""
Database guards for a posted supplier credit note.

The same whole-row allowlist shape migrations `0011`, `0013`, `0015`, `0018`
and `0023` use, and for the same reason: everything not explicitly permitted
is refused, so a column added next year is protected before anybody remembers
to protect it.

**A posted note is immutable except for its reversal.** It moved the payable
and closed a claim; a document that could still change after doing that would
be describing something other than what happened.

**Allocations freeze the moment the note leaves DRAFT.** Posting writes
nothing to them — unlike the return's lines there is no posting window to
carve out — so outside DRAFT every insert, update and delete is refused.

**Only a draft can be deleted**, note or allocation.
"""

from django.db import migrations

NOTE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_supplier_credit_note_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    posting_columns text[] := ARRAY[
        'status', 'number', 'posted_by_id', 'posted_at', 'journal_entry_id',
        'business_date_timezone', 'business_day_start', 'updated_at'
    ];
    reversal_columns text[] := ARRAY[
        'status', 'reversed_by_id', 'reversed_at', 'reversal_reason',
        'reversal_journal_entry_id', 'updated_at'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'supplier credit note % has left draft and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'supplier credit note % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        IF NEW.status <> 'REVERSED'
           OR (to_jsonb(NEW) - reversal_columns) <> (to_jsonb(OLD) - reversal_columns) THEN
            RAISE EXCEPTION
                'supplier credit note % is posted; the only permitted change is its reversal',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'DRAFT' AND NEW.status = 'POSTED' THEN
        IF (to_jsonb(NEW) - posting_columns) <> (to_jsonb(OLD) - posting_columns) THEN
            RAISE EXCEPTION
                'supplier credit note % may be posted, not rewritten', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

NOTE_GUARD_TRIGGER = """
CREATE TRIGGER procurement_supplier_credit_note_immutable
    BEFORE UPDATE OR DELETE ON procurement_suppliercreditnote
    FOR EACH ROW EXECUTE FUNCTION procurement_supplier_credit_note_is_immutable();
"""

ALLOCATION_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_credit_allocation_follows_note()
RETURNS TRIGGER AS $$
DECLARE
    note_status text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.credit_note_id IS DISTINCT FROM OLD.credit_note_id THEN
        RAISE EXCEPTION
            'credit allocation % cannot move to another note', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO note_status
            FROM procurement_suppliercreditnote WHERE id = OLD.credit_note_id;
        IF note_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'credit note is % and its allocations cannot be deleted', note_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    SELECT status INTO note_status
        FROM procurement_suppliercreditnote WHERE id = NEW.credit_note_id;
    IF note_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'credit note is % and its allocations are frozen', note_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

ALLOCATION_GUARD_TRIGGER = """
CREATE TRIGGER procurement_credit_allocation_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON procurement_suppliercreditallocation
    FOR EACH ROW EXECUTE FUNCTION procurement_credit_allocation_follows_note();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0025_supplier_credit_notes"),
    ]

    operations = [
        migrations.RunSQL(
            sql=NOTE_GUARD_FUNCTION + NOTE_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_supplier_credit_note_immutable "
                "ON procurement_suppliercreditnote;"
                "DROP FUNCTION IF EXISTS procurement_supplier_credit_note_is_immutable();"
            ),
        ),
        migrations.RunSQL(
            sql=ALLOCATION_GUARD_FUNCTION + ALLOCATION_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_credit_allocation_frozen "
                "ON procurement_suppliercreditallocation;"
                "DROP FUNCTION IF EXISTS procurement_credit_allocation_follows_note();"
            ),
        ),
    ]
