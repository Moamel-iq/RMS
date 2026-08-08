"""
Two database guarantees the draft lifecycle and source identity now need.

**Balance at the moment of posting.** The deferred balance trigger from 0002
fires on journal *line* INSERT, UPDATE, and DELETE. That was complete while
the only way into the ledger was `post_entry`, which always writes lines. It
is not complete now: `post_draft` flips an existing draft from DRAFT to
POSTED without touching a single line, and a draft is deliberately allowed to
be unbalanced while it is being written. Without the trigger below, an
unbalanced draft promoted straight to POSTED would reach the ledger with the
database never once checking it — the Python validation would be the only
thing standing there, which is exactly the arrangement 0002 exists to refuse.

**Source identity is frozen once posted.** `organization + source_type +
source_id + source_event` is what stops one economic event becoming two
journals. A guarantee that can be edited afterwards is not a guarantee: an
UPDATE that repointed a posted entry at a different document would free the
original identity to be claimed a second time, and the unique index would
raise no objection because nothing would be duplicated at that instant.
"""

from django.db import migrations

# Fires at COMMIT, like the line-level balance check, and for the same reason:
# the entry, its lines, and the posting update all arrive in one transaction
# and only the finished state is worth judging.
BALANCE_ON_POST_FUNCTION = """
CREATE OR REPLACE FUNCTION accounting_entry_must_balance_when_posted()
RETURNS TRIGGER AS $$
DECLARE
    current_status TEXT;
    total_debit NUMERIC;
    total_credit NUMERIC;
    line_count INTEGER;
BEGIN
    -- Read the row as it now stands rather than trusting NEW: a later
    -- statement in the same transaction may have moved it on, and a deferred
    -- trigger judges the state that is actually being committed.
    SELECT status INTO current_status
    FROM accounting_journalentry WHERE id = NEW.id;

    -- Discarded inside the same transaction, or still a draft. A draft is
    -- allowed to be unbalanced; that is what makes it a draft.
    IF current_status IS NULL OR current_status = 'DRAFT' THEN
        RETURN NULL;
    END IF;

    SELECT COUNT(*), COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
      INTO line_count, total_debit, total_credit
      FROM accounting_journalline WHERE entry_id = NEW.id;

    IF line_count < 2 THEN
        RAISE EXCEPTION
            'journal entry % carries % line(s); a posted entry needs at least two',
            NEW.id, line_count
            USING ERRCODE = 'check_violation';
    END IF;

    IF total_debit <> total_credit THEN
        RAISE EXCEPTION
            'journal entry % does not balance: debits %, credits %',
            NEW.id, total_debit, total_credit
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

BALANCE_ON_POST_TRIGGER = """
CREATE CONSTRAINT TRIGGER accounting_journalentry_balance_on_post
    AFTER INSERT OR UPDATE ON accounting_journalentry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION accounting_entry_must_balance_when_posted();
"""

# Replaces the 0002 function. The permitted transitions are unchanged; what is
# added is the block that freezes the source identity, placed after the DRAFT
# early return so a draft may still be given one.
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

    -- Reported separately from the generic message below, because "you moved
    -- the source identity" and "you edited a posted entry" send a developer
    -- to two different places.
    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.source_document_type IS DISTINCT FROM OLD.source_document_type
       OR NEW.source_document_id IS DISTINCT FROM OLD.source_document_id
       OR NEW.source_event IS DISTINCT FROM OLD.source_event THEN
        RAISE EXCEPTION
            'the source identity of a posted journal entry is immutable'
            USING ERRCODE = 'restrict_violation';
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

# Restores the 0002 wording exactly, so rolling this migration back leaves the
# function as 0002 created it rather than as a near-miss of it.
ENTRY_IMMUTABILITY_FUNCTION_0002 = """
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

DROP_ADDED = """
DROP TRIGGER IF EXISTS accounting_journalentry_balance_on_post
    ON accounting_journalentry;
DROP FUNCTION IF EXISTS accounting_entry_must_balance_when_posted();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0003_task_0_7_permissions_and_source_identity"),
    ]

    operations = [
        migrations.RunSQL(
            sql=BALANCE_ON_POST_FUNCTION + BALANCE_ON_POST_TRIGGER,
            reverse_sql=DROP_ADDED,
        ),
        migrations.RunSQL(
            sql=ENTRY_IMMUTABILITY_FUNCTION,
            reverse_sql=ENTRY_IMMUTABILITY_FUNCTION_0002,
        ),
    ]
