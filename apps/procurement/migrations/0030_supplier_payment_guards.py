"""
Database guards for a posted supplier payment.

The same whole-row allowlist shape every posted document here carries. A
posted payment moved money; the only permitted change is its reversal.
Allocations freeze the moment the payment leaves DRAFT — posting writes
nothing to them, so there is no carve-out — and only a draft can be deleted,
payment or allocation.
"""

from django.db import migrations

PAYMENT_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_supplier_payment_is_immutable()
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
                'supplier payment % has left draft and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'supplier payment % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        IF NEW.status <> 'REVERSED'
           OR (to_jsonb(NEW) - reversal_columns) <> (to_jsonb(OLD) - reversal_columns) THEN
            RAISE EXCEPTION
                'supplier payment % is posted; the only permitted change is its reversal',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'DRAFT' AND NEW.status = 'POSTED' THEN
        IF (to_jsonb(NEW) - posting_columns) <> (to_jsonb(OLD) - posting_columns) THEN
            RAISE EXCEPTION
                'supplier payment % may be posted, not rewritten', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

PAYMENT_GUARD_TRIGGER = """
CREATE TRIGGER procurement_supplier_payment_immutable
    BEFORE UPDATE OR DELETE ON procurement_supplierpayment
    FOR EACH ROW EXECUTE FUNCTION procurement_supplier_payment_is_immutable();
"""

ALLOCATION_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_payment_allocation_follows_payment()
RETURNS TRIGGER AS $$
DECLARE
    payment_status text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.payment_id IS DISTINCT FROM OLD.payment_id THEN
        RAISE EXCEPTION
            'payment allocation % cannot move to another payment', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO payment_status
            FROM procurement_supplierpayment WHERE id = OLD.payment_id;
        IF payment_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'supplier payment is % and its allocations cannot be deleted', payment_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    SELECT status INTO payment_status
        FROM procurement_supplierpayment WHERE id = NEW.payment_id;
    IF payment_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'supplier payment is % and its allocations are frozen', payment_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

ALLOCATION_GUARD_TRIGGER = """
CREATE TRIGGER procurement_payment_allocation_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON procurement_paymentallocation
    FOR EACH ROW EXECUTE FUNCTION procurement_payment_allocation_follows_payment();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0029_supplier_payments"),
    ]

    operations = [
        migrations.RunSQL(
            sql=PAYMENT_GUARD_FUNCTION + PAYMENT_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_supplier_payment_immutable "
                "ON procurement_supplierpayment;"
                "DROP FUNCTION IF EXISTS procurement_supplier_payment_is_immutable();"
            ),
        ),
        migrations.RunSQL(
            sql=ALLOCATION_GUARD_FUNCTION + ALLOCATION_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_payment_allocation_frozen "
                "ON procurement_paymentallocation;"
                "DROP FUNCTION IF EXISTS procurement_payment_allocation_follows_payment();"
            ),
        ),
    ]
