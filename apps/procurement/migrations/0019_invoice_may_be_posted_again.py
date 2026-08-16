"""
A reversed invoice may reach the ledger again — once its evidence is corrected.

Migration `0013` made `REVERSED` terminal, which was right for Task 2.10: an
invoice posted only its own direct charges, so a reversal meant the charge was
wrong and the answer was a replacement document. Task 2.12 introduces a second
reason to reverse, and it is the ordinary one: the **match** was wrong. The
invoice is correct, the supplier is owed exactly what it says, and what has to
change is which deliveries it was set against.

Forcing a replacement document there would be wrong twice over. It would put a
second supplier reference in the system for one supplier invoice — the exact
duplicate PRC-037 exists to prevent — and it would leave the original
permanently visible as a reversed debt nobody owes.

So `REVERSED → POSTED` is permitted, under a whole-row allowlist that is the
union of the posting and reversal column sets and nothing else. The invoice's
**terms cannot change**: not a line, not a total, not the supplier reference,
not the due date. What is being corrected is always the evidence, never the
claim. `post_supplier_invoice` refuses unless the previous generation is fully
reversed, no generation is live, and a new `READY` match exists.

The `SupplierInvoicePosting` generations are what make this safe to allow. The
ledger keeps both entries and each names its own generation's `public_id`, so
a re-post is never mistaken for a retry of the first posting, and the history
reads as what happened rather than as the latest opinion.
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
    repost_columns text[] := ARRAY[
        'status', 'number', 'posted_by_id', 'posted_at', 'posted_amount',
        'journal_entry_id', 'business_date_timezone', 'business_day_start',
        'reversed_by_id', 'reversed_at', 'reversal_reason',
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
        -- The one permitted transition out of REVERSED: posting it again from
        -- a corrected match. Its terms may not move by a single column.
        IF NEW.status <> 'POSTED'
           OR (to_jsonb(NEW) - repost_columns) <> (to_jsonb(OLD) - repost_columns) THEN
            RAISE EXCEPTION
                'supplier invoice % is reversed; the only permitted change is posting it '
                'again from a new match', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
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

PREVIOUS_INVOICE_GUARD_FUNCTION = """
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


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0018_posting_guards"),
    ]

    operations = [
        migrations.RunSQL(
            sql=INVOICE_GUARD_FUNCTION,
            reverse_sql=PREVIOUS_INVOICE_GUARD_FUNCTION,
        ),
    ]
