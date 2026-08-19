"""
Freeze a posted sales day in the database, and make the receivable ledger
append-only there too.

Three guards, all of them whole-row allowlists rather than blocklists of
forbidden columns. The distinction is not stylistic: a blocklist has to be
*remembered* every time a column is added, and `accounting/0005` records what
forgetting one cost. An allowlist copies the old row, applies the permitted
changes, and refuses anything that still differs — so a column added next year
is frozen by default, which is the safe direction to be wrong in.

## The sales day

* **DRAFT** — everything except the posting evidence may move. This is where
  a cashier types.
* **SUBMITTED** — the notes and the lifecycle columns may move. The figures may
  not: submitting is the statement "this is what I counted", and a statement
  that can be edited after it is made is not one. The way back is the service's
  own return-to-draft, which writes `status` and clears the submission — both
  on the allowlist.
* **POSTED** — the reversal columns and nothing else. A posted day has reached
  the ledger, the application receivable and the kitchen's consumption figures;
  correcting it is `REVERSED` plus a replacement day, which is the standing
  rule for every posted document in this system.
* **REVERSED** — nothing at all. "Reverse the reversal" is the shape that turns
  an audit trail into a negotiation.

## The lines

Editable exactly while their day is a draft, and the trigger re-reads the day's
status through the foreign key rather than trusting a copy on the line. A
denormalised copy would be one more thing to keep true, and the moment it
drifted the guard would protect the wrong rows — the failure mode where a
freeze is worse than none, because everybody believes it is holding.

`DELETE` is guarded for the same reason `UPDATE` is: removing a line from a
posted day is a rewrite wearing the clothes of a correction.

## The receivable ledger

Append-only in the strict sense: no `UPDATE`, no `DELETE`, ever, in any state.
There is no lifecycle here to branch on — an entry is a fact about a moment,
and a settlement that needs undoing writes a *reversing* entry rather than
editing the original. This is the same guarantee `AuditEvent` and the cost
snapshots already have.
"""

from django.db import migrations

SALES_DAY_GUARD = """
CREATE OR REPLACE FUNCTION sales_day_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted sales_salesday%ROWTYPE;
BEGIN
    permitted := OLD;

    IF OLD.status = 'REVERSED' THEN
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a reversed sales day is history and may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        permitted.status          := NEW.status;
        permitted.reversed_at     := NEW.reversed_at;
        permitted.reversed_by_id  := NEW.reversed_by_id;
        permitted.reversal_reason := NEW.reversal_reason;
        permitted.updated_at      := NEW.updated_at;

        IF NEW.status NOT IN ('POSTED', 'REVERSED') THEN
            RAISE EXCEPTION
                'a posted sales day may only become REVERSED'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a posted sales day is frozen: its number, dates and posting '
            'evidence may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'SUBMITTED' THEN
        permitted.status              := NEW.status;
        permitted.notes               := NEW.notes;
        permitted.submitted_at        := NEW.submitted_at;
        permitted.submitted_by_id     := NEW.submitted_by_id;
        permitted.posted_at           := NEW.posted_at;
        permitted.posted_by_id        := NEW.posted_by_id;
        permitted.number              := NEW.number;
        permitted.idempotency_key     := NEW.idempotency_key;
        permitted.request_fingerprint := NEW.request_fingerprint;
        permitted.updated_at          := NEW.updated_at;

        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a submitted sales day may not have its figures changed; return it '
            'to draft first'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- A draft. Everything moves except the evidence that it posted.
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_day_is_frozen
    BEFORE UPDATE ON sales_salesday
    FOR EACH ROW EXECUTE FUNCTION sales_day_is_frozen();
"""

SALES_LINE_GUARD = """
CREATE OR REPLACE FUNCTION sales_day_line_follows_its_day()
RETURNS TRIGGER AS $$
DECLARE
    day_status text;
    day_id bigint;
BEGIN
    day_id := COALESCE(NEW.sales_day_id, OLD.sales_day_id);
    SELECT status INTO day_status FROM sales_salesday WHERE id = day_id;

    IF day_status = 'DRAFT' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    RAISE EXCEPTION
        'sales day lines may only be changed while the day is a draft'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_day_line_follows_its_day
    BEFORE UPDATE OR DELETE ON sales_salesdayline
    FOR EACH ROW EXECUTE FUNCTION sales_day_line_follows_its_day();

CREATE TRIGGER sales_tender_follows_its_day
    BEFORE UPDATE OR DELETE ON sales_salestendersummary
    FOR EACH ROW EXECUTE FUNCTION sales_day_line_follows_its_day();
"""

RECEIVABLE_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION sales_receivable_is_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'the application receivable ledger is append-only: correct it with a '
        'reversing entry, never by editing one'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_receivable_is_append_only
    BEFORE UPDATE OR DELETE ON sales_applicationreceivableentry
    FOR EACH ROW EXECUTE FUNCTION sales_receivable_is_append_only();
"""

DROP = """
DROP TRIGGER IF EXISTS sales_day_is_frozen ON sales_salesday;
DROP FUNCTION IF EXISTS sales_day_is_frozen();
DROP TRIGGER IF EXISTS sales_day_line_follows_its_day ON sales_salesdayline;
DROP TRIGGER IF EXISTS sales_tender_follows_its_day ON sales_salestendersummary;
DROP FUNCTION IF EXISTS sales_day_line_follows_its_day();
DROP TRIGGER IF EXISTS sales_receivable_is_append_only ON sales_applicationreceivableentry;
DROP FUNCTION IF EXISTS sales_receivable_is_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0005_daily_sales_and_receivable_ledger"),
    ]

    operations = [
        migrations.RunSQL(sql=SALES_DAY_GUARD, reverse_sql=DROP),
        migrations.RunSQL(sql=SALES_LINE_GUARD, reverse_sql=DROP),
        migrations.RunSQL(sql=RECEIVABLE_APPEND_ONLY, reverse_sql=DROP),
    ]
