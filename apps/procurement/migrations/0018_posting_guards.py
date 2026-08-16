"""
Database guards for the financial posting of a matched invoice, and for the
match a live posting stands on.

Three claims, each enforced where a service cannot be refactored around it.

**A posting is immutable except for its own reversal.** Same whole-row
allowlist shape as `0011`, `0013` and `0015`: everything not explicitly
permitted is refused, so a column added later is protected before anybody
remembers to protect it. A posted generation is the ledger's explanation of
itself; the only thing that may ever happen to it is being reversed, once.

**A posting is never deleted.** Not a live one, not a reversed one. Reversal is
the correction, and the reversed generation is the record that it happened.

**A match a live posting stands on cannot be cancelled.** Migration `0015`
said, correctly for Task 2.11, that there was "no financial state in this
table" to protect. Task 2.12 gives it one. Cancelling a match underneath a live
posting would release the delivery for a second invoice while the ledger still
carries the first — and because availability is derived, both sides of every
Task 2.11 equality would move together and no verifier would notice.

The service refuses it too. This is the belt to that brace, for the reason
ADR-023 §3 gives about the over-allocation check: a guard that lives only
inside one service function is one refactor, one new code path, or one
management command away from not existing.

The ordering this implies is deliberate and load-bearing: a correction
**reverses the posting first**, then cancels the match. By the time the
cancellation runs there is no live posting, and this trigger lets it through.
"""

from django.db import migrations

POSTING_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_invoice_posting_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    reversal_columns text[] := ARRAY[
        'status', 'reversal_journal_entry_id', 'reversed_by_id', 'reversed_at',
        'reversal_reason', 'updated_at'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'supplier invoice posting % is a ledger record and cannot be deleted', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'supplier invoice posting % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.status <> 'REVERSED'
       OR (to_jsonb(NEW) - reversal_columns) <> (to_jsonb(OLD) - reversal_columns) THEN
        RAISE EXCEPTION
            'supplier invoice posting % is live; the only permitted change is its reversal',
            OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

POSTING_GUARD_TRIGGER = """
CREATE TRIGGER procurement_invoice_posting_immutable
    BEFORE UPDATE OR DELETE ON procurement_supplierinvoiceposting
    FOR EACH ROW EXECUTE FUNCTION procurement_invoice_posting_is_immutable();
"""

MATCH_POSTING_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION procurement_match_has_no_live_posting()
RETURNS TRIGGER AS $$
DECLARE
    live_count integer;
BEGIN
    IF NEW.status <> 'CANCELLED' OR OLD.status = 'CANCELLED' THEN
        RETURN NEW;
    END IF;

    SELECT count(*) INTO live_count
        FROM procurement_supplierinvoiceposting
        WHERE purchase_match_id = OLD.id AND status = 'LIVE';

    IF live_count > 0 THEN
        RAISE EXCEPTION
            'purchase match % backs a live posting; reverse the invoice first', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

MATCH_POSTING_GUARD_TRIGGER = """
CREATE TRIGGER procurement_match_cancellation_needs_no_live_posting
    BEFORE UPDATE ON procurement_purchasematch
    FOR EACH ROW EXECUTE FUNCTION procurement_match_has_no_live_posting();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0017_supplier_invoice_postings"),
    ]

    operations = [
        migrations.RunSQL(
            sql=POSTING_GUARD_FUNCTION + POSTING_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_invoice_posting_immutable "
                "ON procurement_supplierinvoiceposting;"
                "DROP FUNCTION IF EXISTS procurement_invoice_posting_is_immutable();"
            ),
        ),
        migrations.RunSQL(
            sql=MATCH_POSTING_GUARD_FUNCTION + MATCH_POSTING_GUARD_TRIGGER,
            reverse_sql=(
                "DROP TRIGGER IF EXISTS procurement_match_cancellation_needs_no_live_posting "
                "ON procurement_purchasematch;"
                "DROP FUNCTION IF EXISTS procurement_match_has_no_live_posting();"
            ),
        ),
    ]
