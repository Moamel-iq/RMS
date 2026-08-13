"""
Database guards for a READY purchase match.

The same whole-row allowlist shape migrations `0011` and `0013` use, and for
the same reason: everything not explicitly permitted is refused, so a column
added later is protected before anybody remembers to protect it.

**A READY match is immutable except for its cancellation.** `READY` is the
claim Task 2.12 will post from, so a row that could still change after being
frozen would make the posting a race against whoever was still editing.

**Allocations freeze with their match.** Once it leaves `DRAFT` its allocations
refuse insert, update and delete outright — there is no posting window to keep
open here, because Task 2.11 writes nothing back onto an allocation.

**Only a DRAFT can be deleted**, match or allocation. A cancelled match stays:
it is the record of an answer somebody withdrew, and the reason is on it.

Deliberately absent: any trigger that would let a match reach a ledger. There
is no financial state in this table for one to protect.
"""

from django.db import migrations

MATCH_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_purchase_match_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    ready_columns text[] := ARRAY[
        'status', 'number', 'ready_by_id', 'ready_at', 'allocation_fingerprint',
        'updated_at'
    ];
    cancel_columns text[] := ARRAY[
        'status', 'cancelled_by_id', 'cancelled_at', 'cancellation_reason', 'updated_at'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'purchase match % has left draft and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'CANCELLED' THEN
        RAISE EXCEPTION
            'purchase match % is cancelled and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'READY' THEN
        IF NEW.status <> 'CANCELLED'
           OR (to_jsonb(NEW) - cancel_columns) <> (to_jsonb(OLD) - cancel_columns) THEN
            RAISE EXCEPTION
                'purchase match % is ready; the only permitted change is its cancellation',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'DRAFT' AND NEW.status = 'READY' THEN
        IF (to_jsonb(NEW) - ready_columns) <> (to_jsonb(OLD) - ready_columns) THEN
            RAISE EXCEPTION
                'purchase match % may be readied, not rewritten', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

MATCH_GUARD_TRIGGER = """
CREATE TRIGGER procurement_purchase_match_immutable
    BEFORE UPDATE OR DELETE ON procurement_purchasematch
    FOR EACH ROW EXECUTE FUNCTION procurement_purchase_match_is_immutable();
"""

ALLOCATION_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_match_allocation_follows_match()
RETURNS TRIGGER AS $$
DECLARE
    match_status text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.match_id IS DISTINCT FROM OLD.match_id THEN
        RAISE EXCEPTION
            'match allocation % cannot move to another match', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO match_status
            FROM procurement_purchasematch WHERE id = OLD.match_id;
    ELSE
        SELECT status INTO match_status
            FROM procurement_purchasematch WHERE id = NEW.match_id;
    END IF;

    IF match_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'purchase match is % and its allocations are frozen', match_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

ALLOCATION_GUARD_TRIGGER = """
CREATE TRIGGER procurement_match_allocation_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON procurement_purchasematchallocation
    FOR EACH ROW EXECUTE FUNCTION procurement_match_allocation_follows_match();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0014_three_way_matching"),
    ]

    operations = [
        migrations.RunSQL(
            sql=MATCH_GUARD_FUNCTION + MATCH_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_purchase_match_immutable "
                "ON procurement_purchasematch;\n"
                "DROP FUNCTION IF EXISTS procurement_purchase_match_is_immutable();"
            ),
        ),
        migrations.RunSQL(
            sql=ALLOCATION_GUARD_FUNCTION + ALLOCATION_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_match_allocation_frozen "
                "ON procurement_purchasematchallocation;\n"
                "DROP FUNCTION IF EXISTS procurement_match_allocation_follows_match();"
            ),
        ),
    ]
