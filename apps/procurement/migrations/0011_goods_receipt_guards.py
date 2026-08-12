"""
Database guards for a posted goods receipt.

Three things the ORM cannot express, and one that it can but should not have
to be trusted with.

**A posted receipt is immutable, as a whole row.** The only permitted change is
the reversal transition, written as an *allowlist* over the columns that
transition touches. Everything not explicitly allowed is refused, so a column
added to this table next year is protected before anybody remembers to protect
it — the `accounting/0005` lesson, applied ahead of the mistake rather than
after it.

**Lines freeze with their receipt.** Once it is POSTED or REVERSED its lines
refuse insert, update and delete. Posting writes the resolved accounts, the
movement, the journal line and the posted value onto each line *before*
flipping the status, which is what leaves that window open exactly long enough
and no longer.

**A receipt that reached the ledgers cannot be deleted**, and neither can its
lines. Only a DRAFT may go, which is the same rule inventory applies to its own
documents and for the same reason: a deleted posted receipt would leave a stock
movement and a journal entry citing a document that no longer exists.

**A posted line's value must be the accepted quantity at the posted cost.** A
`CheckConstraint` cannot express it, because rounding happens once at the
storage boundary and the comparison has to allow the half-unit the quantize
introduces. The trigger states it exactly: the stored value is the money
quantization of `accepted_base_quantity x posted_unit_cost`, so a posting path
that ever wrote a value its own quantity does not support is refused by the
database rather than caught by a reconciliation report later.
"""

from django.db import migrations

RECEIPT_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_goods_receipt_is_immutable()
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
                'goods receipt % has posted and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'goods receipt % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        IF NEW.status <> 'REVERSED'
           OR (to_jsonb(NEW) - allowed) <> (to_jsonb(OLD) - allowed) THEN
            RAISE EXCEPTION
                'goods receipt % is posted; the only permitted change is its reversal',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

RECEIPT_GUARD_TRIGGER = """
CREATE TRIGGER procurement_goods_receipt_immutable
    BEFORE UPDATE OR DELETE ON procurement_goodsreceipt
    FOR EACH ROW EXECUTE FUNCTION procurement_goods_receipt_is_immutable();
"""

LINE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_goods_receipt_line_follows_receipt()
RETURNS TRIGGER AS $$
DECLARE
    receipt_status text;
    expected numeric;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.receipt_id IS DISTINCT FROM OLD.receipt_id THEN
        RAISE EXCEPTION
            'goods receipt line % cannot move to another receipt', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO receipt_status
            FROM procurement_goodsreceipt WHERE id = OLD.receipt_id;
    ELSE
        SELECT status INTO receipt_status
            FROM procurement_goodsreceipt WHERE id = NEW.receipt_id;
    END IF;

    IF receipt_status IN ('POSTED', 'REVERSED') THEN
        RAISE EXCEPTION
            'goods receipt is % and its lines are frozen', receipt_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    -- The value that reached both ledgers is the accepted quantity at the
    -- posted cost, rounded once. Stated here so no posting path can write a
    -- figure its own quantity does not support.
    IF NEW.posted_value IS NOT NULL THEN
        expected := round(NEW.accepted_base_quantity * NEW.posted_unit_cost, 3);
        IF NEW.posted_value <> expected THEN
            RAISE EXCEPTION
                'goods receipt line % posted % which is not % x %',
                NEW.id, NEW.posted_value, NEW.accepted_base_quantity, NEW.posted_unit_cost
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

LINE_GUARD_TRIGGER = """
CREATE TRIGGER procurement_goods_receipt_line_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON procurement_goodsreceiptline
    FOR EACH ROW EXECUTE FUNCTION procurement_goods_receipt_line_follows_receipt();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0010_goods_receipt_posting"),
    ]

    operations = [
        migrations.RunSQL(
            sql=RECEIPT_GUARD_FUNCTION + RECEIPT_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_goods_receipt_immutable "
                "ON procurement_goodsreceipt;\n"
                "DROP FUNCTION IF EXISTS procurement_goods_receipt_is_immutable();"
            ),
        ),
        migrations.RunSQL(
            sql=LINE_GUARD_FUNCTION + LINE_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_goods_receipt_line_frozen "
                "ON procurement_goodsreceiptline;\n"
                "DROP FUNCTION IF EXISTS procurement_goods_receipt_line_follows_receipt();"
            ),
        ),
    ]
