"""
Close a hole in the posted-entry immutability trigger.

The trigger written in 0002 permitted its two legitimate transitions by
listing the columns that must *not* change:

    IF OLD.status = NEW.status
       AND NEW.entry_number = OLD.entry_number
       AND NEW.organization_id = OLD.organization_id
       AND NEW.period_id = OLD.period_id
       AND NEW.accounting_date = OLD.accounting_date
       AND NEW.idempotency_key = OLD.idempotency_key
       AND OLD.reverses_id IS NULL THEN
        RETURN NEW;

That is a blocklist, and it was missing columns. Any column it did not name
was editable on a posted entry: `narration`, `document_date`, `is_adjustment`,
`posted_at`, `posted_by`, `posting_rule_version`. A posted journal's narration
could be rewritten years later with no error, no history row — `JournalEntry`
is deliberately not historied — and no audit event. The stated invariant is
"posted journals are immutable; corrections are reversals", and on those
columns it was not being kept.

The fix inverts the test. Instead of naming what must not move, each permitted
transition now builds the row it expects to see and compares whole rows:

    permitted := OLD;
    permitted.status := NEW.status;
    IF ... AND NEW IS NOT DISTINCT FROM permitted THEN

A column added to this table in future is covered automatically, because it
will differ from `permitted` unless the transition explicitly allowed it. A
blocklist has to be remembered; an allowlist cannot be forgotten.

Only two transitions exist, and each moves exactly one column:

* `POSTED -> REVERSED` on the original, performed by `reverse_entry`.
* `reverses_id` set from NULL, linking a reversal to what it reverses.
"""

from django.db import migrations

STRICT_ENTRY_IMMUTABILITY = """
CREATE OR REPLACE FUNCTION accounting_posted_entry_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    permitted accounting_journalentry%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'DRAFT' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'a posted journal entry cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- A draft is still being written. Everything about it may change, which
    -- is what `post_draft` relies on when it takes an entry number.
    IF OLD.status = 'DRAFT' THEN
        RETURN NEW;
    END IF;

    -- Reported separately, because "you moved the source identity" and "you
    -- edited a posted entry" send a developer to two different places.
    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.source_document_type IS DISTINCT FROM OLD.source_document_type
       OR NEW.source_document_id IS DISTINCT FROM OLD.source_document_id
       OR NEW.source_event IS DISTINCT FROM OLD.source_event THEN
        RAISE EXCEPTION
            'the source identity of a posted journal entry is immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Transition one: the original becomes REVERSED, and nothing else moves.
    permitted := OLD;
    permitted.status := NEW.status;
    IF OLD.status = 'POSTED'
       AND NEW.status = 'REVERSED'
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    -- Transition two: the reversal is linked to what it reverses, once, and
    -- nothing else moves.
    permitted := OLD;
    permitted.reverses_id := NEW.reverses_id;
    IF OLD.reverses_id IS NULL
       AND NEW.reverses_id IS NOT NULL
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'a posted journal entry is immutable'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

# Reverting restores the 0004 wording — the loose blocklist plus the source
# identity guard — rather than jumping back past 0004 to the 0002 version.
LOOSE_ENTRY_IMMUTABILITY_0004 = """
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


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0004_source_identity_and_posting_triggers"),
    ]

    operations = [
        migrations.RunSQL(
            sql=STRICT_ENTRY_IMMUTABILITY,
            reverse_sql=LOOSE_ENTRY_IMMUTABILITY_0004,
        ),
    ]
