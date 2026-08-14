"""
Database guards for a posted supplier return.

The same whole-row allowlist shape migrations `0011`, `0013`, `0015` and `0018`
use, and for the same reason: everything not explicitly permitted is refused,
so a column added to this table next year is protected before anybody
remembers to protect it (the `accounting/0005` lesson).

**A posted return is immutable except for its reversal.** It moved stock and
posted a journal; both are append-only, and a document that could still change
after making them would be describing something other than what happened.

**Lines freeze with their return**, with the one window posting needs carved
out precisely: it writes each line's movement, its posted value and the two
accounts it moved between while the return is still `DRAFT`. Rather than trust
a service to stay inside that window, the trigger names the columns posting may
touch and refuses every other change.

**Only a draft can be deleted**, return or line. A deleted posted return would
leave a stock movement and a journal citing a document that no longer exists.
"""

from django.db import migrations

RETURN_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_supplier_return_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    posting_columns text[] := ARRAY[
        'status', 'number', 'posted_by_id', 'posted_at', 'posted_value',
        'stock_entry_id', 'journal_entry_id', 'business_date',
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
                'supplier return % has left draft and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'supplier return % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        IF NEW.status <> 'REVERSED'
           OR (to_jsonb(NEW) - reversal_columns) <> (to_jsonb(OLD) - reversal_columns) THEN
            RAISE EXCEPTION
                'supplier return % is posted; the only permitted change is its reversal',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'DRAFT' AND NEW.status = 'POSTED' THEN
        IF (to_jsonb(NEW) - posting_columns) <> (to_jsonb(OLD) - posting_columns) THEN
            RAISE EXCEPTION
                'supplier return % may be posted, not rewritten', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

RETURN_GUARD_TRIGGER = """
CREATE TRIGGER procurement_supplier_return_immutable
    BEFORE UPDATE OR DELETE ON procurement_supplierreturn
    FOR EACH ROW EXECUTE FUNCTION procurement_supplier_return_is_immutable();
"""

LINE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_supplier_return_line_follows_return()
RETURNS TRIGGER AS $$
DECLARE
    return_status text;
    posting_columns text[] := ARRAY[
        'movement_id', 'posted_value', 'inventory_account_id', 'contra_account_id',
        'updated_at'
    ];
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.supplier_return_id IS DISTINCT FROM OLD.supplier_return_id THEN
        RAISE EXCEPTION
            'supplier return line % cannot move to another return', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO return_status
            FROM procurement_supplierreturn WHERE id = OLD.supplier_return_id;
    ELSE
        SELECT status INTO return_status
            FROM procurement_supplierreturn WHERE id = NEW.supplier_return_id;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF return_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'supplier return is % and its lines cannot be deleted', return_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'INSERT' AND return_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'supplier return is % and takes no further lines', return_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'UPDATE' AND return_status <> 'DRAFT' THEN
        -- The one permitted write outside DRAFT: posting stamping this line
        -- with what the kernel moved and where it moved it.
        IF (to_jsonb(NEW) - posting_columns) <> (to_jsonb(OLD) - posting_columns) THEN
            RAISE EXCEPTION
                'supplier return is % and its lines are frozen', return_status
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

LINE_GUARD_TRIGGER = """
CREATE TRIGGER procurement_supplier_return_line_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON procurement_supplierreturnline
    FOR EACH ROW EXECUTE FUNCTION procurement_supplier_return_line_follows_return();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0022_supplier_returns"),
    ]

    operations = [
        migrations.RunSQL(
            sql=RETURN_GUARD_FUNCTION + RETURN_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_supplier_return_immutable "
                "ON procurement_supplierreturn;"
                "DROP FUNCTION IF EXISTS procurement_supplier_return_is_immutable();"
            ),
        ),
        migrations.RunSQL(
            sql=LINE_GUARD_FUNCTION + LINE_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_supplier_return_line_frozen "
                "ON procurement_supplierreturnline;"
                "DROP FUNCTION IF EXISTS procurement_supplier_return_line_follows_return();"
            ),
        ),
    ]
