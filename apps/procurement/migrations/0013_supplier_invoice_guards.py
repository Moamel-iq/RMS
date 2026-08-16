"""
Database guards for a posted supplier invoice.

The same whole-row allowlist shape migration `0011_goods_receipt_guards` uses,
and for the same reason: everything not explicitly permitted is refused, so a
column added to this table next year is protected before anybody remembers to
protect it (the `accounting/0005` lesson).

**A posted invoice is immutable except for its reversal.** An approved one is
immutable except for posting or a return to draft — because an approval is
somebody agreeing to a specific claim, and editing the claim underneath them
would leave an approval attached to a document that no longer says what was
approved.

**Lines freeze with their invoice.** Once it leaves `DRAFT` its lines refuse
insert, update and delete, with one exception carved out precisely: posting
writes each line's `journal_line` and resolved mapping while the invoice is
still `APPROVED`, which is the same window Task 2.9 uses for the receipt.
Rather than trust a service to stay inside that window, the trigger names the
columns posting may touch and refuses every other change.

**A line's net amount must be its own arithmetic.** `line_amount +
allocated_freight - allocated_discount`, checked in the database, so no path
can write a net figure its own components do not support — the invoice twin of
the receipt-line value trigger.

**Only a draft can be deleted**, invoice or line. A deleted posted invoice
would leave a journal entry citing a document that no longer exists.
"""

from django.db import migrations

INVOICE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_supplier_invoice_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    posting_columns text[] := ARRAY[
        'status', 'number', 'posted_by_id', 'posted_at', 'posted_amount',
        'journal_entry_id', 'business_date_timezone', 'business_day_start',
        'updated_at'
    ];
    reversal_columns text[] := ARRAY[
        'status', 'reversed_by_id', 'reversed_at', 'reversal_reason',
        'reversal_journal_entry_id', 'updated_at'
    ];
    approval_columns text[] := ARRAY[
        'status', 'approved_by_id', 'approved_at', 'updated_at'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'supplier invoice % has left draft and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'supplier invoice % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        IF NEW.status <> 'REVERSED'
           OR (to_jsonb(NEW) - reversal_columns) <> (to_jsonb(OLD) - reversal_columns) THEN
            RAISE EXCEPTION
                'supplier invoice % is posted; the only permitted change is its reversal',
                OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'APPROVED' THEN
        -- Two permitted transitions out of APPROVED: posting it, or sending
        -- it back to DRAFT so the claim can be corrected and re-approved.
        IF NEW.status = 'POSTED' THEN
            IF (to_jsonb(NEW) - posting_columns) <> (to_jsonb(OLD) - posting_columns) THEN
                RAISE EXCEPTION
                    'supplier invoice % is approved; posting may not change its terms', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSIF NEW.status = 'DRAFT' THEN
            IF (to_jsonb(NEW) - approval_columns) <> (to_jsonb(OLD) - approval_columns) THEN
                RAISE EXCEPTION
                    'supplier invoice % may only be returned to draft, not edited in place',
                    OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSE
            RAISE EXCEPTION
                'supplier invoice % is approved and its terms are frozen', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

INVOICE_GUARD_TRIGGER = """
CREATE TRIGGER procurement_supplier_invoice_immutable
    BEFORE UPDATE OR DELETE ON procurement_supplierinvoice
    FOR EACH ROW EXECUTE FUNCTION procurement_supplier_invoice_is_immutable();
"""

LINE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_supplier_invoice_line_follows_invoice()
RETURNS TRIGGER AS $$
DECLARE
    invoice_status text;
    posting_columns text[] := ARRAY[
        'journal_line_id', 'resolved_organization_mapping_id', 'updated_at'
    ];
    expected numeric;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.invoice_id IS DISTINCT FROM OLD.invoice_id THEN
        RAISE EXCEPTION
            'supplier invoice line % cannot move to another invoice', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO invoice_status
            FROM procurement_supplierinvoice WHERE id = OLD.invoice_id;
    ELSE
        SELECT status INTO invoice_status
            FROM procurement_supplierinvoice WHERE id = NEW.invoice_id;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF invoice_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'supplier invoice is % and its lines cannot be deleted', invoice_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'INSERT' AND invoice_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'supplier invoice is % and takes no further lines', invoice_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'UPDATE' AND invoice_status <> 'DRAFT' THEN
        -- The one permitted write outside DRAFT: posting stamping this line
        -- with the journal line and mapping it produced.
        IF (to_jsonb(NEW) - posting_columns) <> (to_jsonb(OLD) - posting_columns) THEN
            RAISE EXCEPTION
                'supplier invoice is % and its lines are frozen', invoice_status
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    -- The net amount is the line's own arithmetic, stated here so no path can
    -- write a figure its components do not support.
    expected := NEW.line_amount + NEW.allocated_freight - NEW.allocated_discount;
    IF NEW.net_amount <> expected THEN
        RAISE EXCEPTION
            'supplier invoice line % nets to % which is not % + % - %',
            NEW.id, NEW.net_amount, NEW.line_amount,
            NEW.allocated_freight, NEW.allocated_discount
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

LINE_GUARD_TRIGGER = """
CREATE TRIGGER procurement_supplier_invoice_line_frozen
    BEFORE INSERT OR UPDATE OR DELETE ON procurement_supplierinvoiceline
    FOR EACH ROW EXECUTE FUNCTION procurement_supplier_invoice_line_follows_invoice();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0012_supplier_invoices"),
    ]

    operations = [
        migrations.RunSQL(
            sql=INVOICE_GUARD_FUNCTION + INVOICE_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_supplier_invoice_immutable "
                "ON procurement_supplierinvoice;\n"
                "DROP FUNCTION IF EXISTS procurement_supplier_invoice_is_immutable();"
            ),
        ),
        migrations.RunSQL(
            sql=LINE_GUARD_FUNCTION + LINE_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_supplier_invoice_line_frozen "
                "ON procurement_supplierinvoiceline;\n"
                "DROP FUNCTION IF EXISTS procurement_supplier_invoice_line_follows_invoice();"
            ),
        ),
    ]
