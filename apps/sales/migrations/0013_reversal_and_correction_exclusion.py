"""
A sales day and its corrections may not both be taken back.

`0006` froze a posted day and allowed exactly one transition out of it —
`POSTED` to `REVERSED`. That was right about *which columns* may move and
silent about *when*, and the silence is the defect this migration closes: a day
carrying a posted `SalesAdjustment` has already had part of it un-recognised,
in the general ledger and in the receivable subledger both. Reversing the whole
day afterwards un-recognises the same sale a second time.

The result is invisible to every verifier the module has. `verify_receivable_
ledger` compares the subledger against the general ledger and both are wrong by
the identical amount, so they agree; `verify_adjustments_are_within_their_
originals` reads the original line's quantity and gross, which a reversal never
touches. What is left is a receivable with a credit balance the application
never owed and a class-6 expense account holding a credit — two figures that
look like data-entry noise and are actually one sale counted backwards twice.

Enforced here as well as in `reverse_sales_day` for the reason the containment
trigger in `0008` gives: a service check holds for the screens and the API and
fails for the shell session, the data fix and the import, and this particular
walk-past leaves no evidence of which document was the wrong one.

`sales_day_is_frozen` is replaced whole rather than wrapped. `CREATE OR REPLACE
FUNCTION` keeps the existing trigger bound to the new body, so there is one
definition of the day's freeze rather than two that have to agree — and the
allowlist stays a single readable statement of what a posted day may do.
"""

from django.db import migrations

SALES_DAY_GUARD = """
CREATE OR REPLACE FUNCTION sales_day_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted sales_salesday%ROWTYPE;
    corrected boolean;
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

        IF NEW.status = 'REVERSED' THEN
            -- Posted corrections only. A draft proposes nothing and has
            -- reached no ledger; a reversed one has already been undone, and
            -- refusing on either would make a day permanently unreversible
            -- because somebody once opened a correction and abandoned it.
            SELECT EXISTS (
                SELECT 1
                  FROM sales_salesadjustment AS correction
                 WHERE correction.sales_day_id = OLD.id
                   AND correction.status = 'POSTED'
            ) INTO corrected;

            IF corrected THEN
                RAISE EXCEPTION
                    'a sales day with a posted adjustment against it may not be '
                    'reversed: reverse the adjustment first, or the same sale is '
                    'taken back twice'
                    USING ERRCODE = 'restrict_violation';
            END IF;
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
"""

#: `0006`'s body, restored verbatim. A reverse migration that dropped the
#: function would take the whole freeze with it rather than only this rule.
SALES_DAY_GUARD_BEFORE = """
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

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0012_cashier_shift_guards"),
    ]

    operations = [
        migrations.RunSQL(sql=SALES_DAY_GUARD, reverse_sql=SALES_DAY_GUARD_BEFORE),
    ]
