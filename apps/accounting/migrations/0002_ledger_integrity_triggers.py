"""
Database-level enforcement of the two non-negotiable ledger invariants.

CLAUDE.md calls these defects, not style disagreements: every journal entry
balances, and posted records are immutable. Both are enforced here as well as
in the posting service, because the service can be bypassed by a data
migration, a bulk update, a management command, or a psql prompt — and those
are exactly the routes by which a ledger quietly stops reconciling.

Balance is checked by a DEFERRED constraint trigger. It cannot run per
statement: an entry is necessarily unbalanced between inserting its first line
and its last. Deferring to COMMIT means the check sees the finished entry.
"""

from django.db import migrations

BALANCE_FUNCTION = """
CREATE OR REPLACE FUNCTION accounting_entry_must_balance()
RETURNS TRIGGER AS $$
DECLARE
    target_entry BIGINT;
    entry_status TEXT;
    total_debit NUMERIC;
    total_credit NUMERIC;
BEGIN
    target_entry := COALESCE(NEW.entry_id, OLD.entry_id);

    SELECT status INTO entry_status
    FROM accounting_journalentry WHERE id = target_entry;

    -- The entry may have been deleted in the same transaction (a draft being
    -- discarded); nothing left to balance.
    IF entry_status IS NULL THEN
        RETURN NULL;
    END IF;

    -- Drafts are allowed to be unbalanced while they are being built.
    IF entry_status = 'DRAFT' THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
      INTO total_debit, total_credit
      FROM accounting_journalline WHERE entry_id = target_entry;

    IF total_debit <> total_credit THEN
        RAISE EXCEPTION
            'journal entry % does not balance: debits %, credits %',
            target_entry, total_debit, total_credit
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

BALANCE_TRIGGER = """
CREATE CONSTRAINT TRIGGER accounting_journalline_balance
    AFTER INSERT OR UPDATE OR DELETE ON accounting_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION accounting_entry_must_balance();
"""

# A posted line may never change. There is no legitimate edit: corrections are
# reversals, which append new lines to a new entry.
LINE_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION accounting_posted_line_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    entry_status TEXT;
BEGIN
    SELECT status INTO entry_status
    FROM accounting_journalentry WHERE id = COALESCE(NEW.entry_id, OLD.entry_id);

    IF entry_status IS NULL OR entry_status = 'DRAFT' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    RAISE EXCEPTION
        'journal lines of a posted entry are immutable; % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

LINE_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER accounting_journalline_no_change
    BEFORE UPDATE OR DELETE ON accounting_journalline
    FOR EACH ROW EXECUTE FUNCTION accounting_posted_line_is_immutable();
"""

# A posted entry may change in exactly one way: POSTED -> REVERSED, which the
# reversal service performs. Everything else about it is frozen, and it can
# never be deleted.
ENTRY_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION accounting_posted_entry_is_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'DRAFT' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'a posted journal entry cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'DRAFT' THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'POSTED' AND NEW.status = 'REVERSED'
       AND NEW.id = OLD.id
       AND NEW.entry_number = OLD.entry_number
       AND NEW.organization_id = OLD.organization_id
       AND NEW.period_id = OLD.period_id
       AND NEW.accounting_date = OLD.accounting_date
       AND NEW.idempotency_key = OLD.idempotency_key THEN
        RETURN NEW;
    END IF;

    -- Setting `reverses` on the reversal itself happens while it is POSTED and
    -- changes nothing else.
    IF OLD.status = NEW.status
       AND NEW.entry_number = OLD.entry_number
       AND NEW.organization_id = OLD.organization_id
       AND NEW.period_id = OLD.period_id
       AND NEW.accounting_date = OLD.accounting_date
       AND NEW.idempotency_key = OLD.idempotency_key
       AND OLD.reverses_id IS NULL THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'a posted journal entry is immutable'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

ENTRY_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER accounting_journalentry_no_change
    BEFORE UPDATE OR DELETE ON accounting_journalentry
    FOR EACH ROW EXECUTE FUNCTION accounting_posted_entry_is_immutable();
"""

# Only leaf accounts accept lines. Posting to a parent stops its children
# summing to it and no report over the hierarchy can be trusted afterwards.
POSTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION accounting_line_account_must_be_postable()
RETURNS TRIGGER AS $$
DECLARE
    postable BOOLEAN;
BEGIN
    SELECT is_postable INTO postable
    FROM accounting_account WHERE id = NEW.account_id;

    IF postable IS NOT TRUE THEN
        RAISE EXCEPTION 'account % is a group account and cannot receive journal lines',
            NEW.account_id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

POSTABLE_TRIGGER = """
CREATE TRIGGER accounting_journalline_account_postable
    BEFORE INSERT ON accounting_journalline
    FOR EACH ROW EXECUTE FUNCTION accounting_line_account_must_be_postable();
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS accounting_journalline_balance ON accounting_journalline;
DROP TRIGGER IF EXISTS accounting_journalline_no_change ON accounting_journalline;
DROP TRIGGER IF EXISTS accounting_journalentry_no_change ON accounting_journalentry;
DROP TRIGGER IF EXISTS accounting_journalline_account_postable ON accounting_journalline;
DROP FUNCTION IF EXISTS accounting_entry_must_balance();
DROP FUNCTION IF EXISTS accounting_posted_line_is_immutable();
DROP FUNCTION IF EXISTS accounting_posted_entry_is_immutable();
DROP FUNCTION IF EXISTS accounting_line_account_must_be_postable();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                BALANCE_FUNCTION
                + BALANCE_TRIGGER
                + LINE_IMMUTABILITY_FUNCTION
                + LINE_IMMUTABILITY_TRIGGER
                + ENTRY_IMMUTABILITY_FUNCTION
                + ENTRY_IMMUTABILITY_TRIGGER
                + POSTABLE_FUNCTION
                + POSTABLE_TRIGGER
            ),
            reverse_sql=DROP_ALL,
        ),
    ]
