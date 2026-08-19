"""
Freeze a posted adjustment, and keep every adjustment inside its original.

Three guards. The first two are the same whole-row allowlist idiom
`0006_sales_day_guards` uses — copy `OLD` into a `%ROWTYPE` variable, apply the
columns this state may move, and refuse anything that still differs. An
allowlist freezes a column added next year by default, which is the safe
direction to be wrong in; a blocklist has to be remembered, and
`accounting/0005` records what forgetting one cost.

The third guard is different in kind, and it is the one that carries real
policy.

## Why the containment rule is a trigger and not a service check

`sales_adjustment_line_is_within_its_original` enforces five things a raw
`INSERT` would otherwise walk straight past:

* the original line's day must be **POSTED** — there is nothing to take back
  from a draft, and a return against a day that never reached the ledger would
  credit `SALES_RETURNS` against a sale that was never recognised;
* the adjustment's `business_date` may not precede the day it corrects, because
  a correction that happened before the thing it corrects is not a correction;
* a `FINANCIAL_CORRECTION` may not touch quantity at all (ADR-028 §8) — a money
  correction is not a claim that less food was sold, and letting a pricing fix
  silently rewrite sold quantity would change what the kitchen is measured
  against for a reason that has nothing to do with the kitchen;
* a cancellation or a return **must** touch quantity, for the converse reason;
* and the running totals of quantity and gross across every **posted**
  adjustment against the same original line may not exceed what that line sold.

Over-adjusting is the interesting one. A service check would hold for the
screens and fail for the shell session, the data fix and the import — and the
symptom (a `SALES_RETURNS` figure larger than the sale it reverses) surfaces at
the end of a quarter, in an argument, with nobody able to say which of the
overlapping corrections was the wrong one.

Only **posted** adjustments count toward the running totals. Two drafts may
each propose the full amount; only one of them can then post, which is exactly
the behaviour a draft should have.
"""

from django.db import migrations

ADJUSTMENT_GUARD = """
CREATE OR REPLACE FUNCTION sales_adjustment_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted sales_salesadjustment%ROWTYPE;
BEGIN
    permitted := OLD;

    IF OLD.status = 'REVERSED' THEN
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a reversed sales adjustment is history and may not change'
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
                'a posted sales adjustment may only become REVERSED'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a posted sales adjustment is frozen: its number, reason, evidence '
            'and posting evidence may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- A draft. Everything moves except the evidence that it posted.
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_adjustment_is_frozen
    BEFORE UPDATE ON sales_salesadjustment
    FOR EACH ROW EXECUTE FUNCTION sales_adjustment_is_frozen();
"""

ADJUSTMENT_LINE_GUARD = """
CREATE OR REPLACE FUNCTION sales_adjustment_line_follows_its_adjustment()
RETURNS TRIGGER AS $$
DECLARE
    header_status text;
    header_id bigint;
BEGIN
    header_id := COALESCE(NEW.adjustment_id, OLD.adjustment_id);
    -- Read through the foreign key, never from a copy on the line. A
    -- denormalised status would be one more thing to keep true, and the moment
    -- it drifted the guard would protect the wrong rows — the failure mode
    -- where a freeze is worse than none, because everybody believes it holds.
    SELECT status INTO header_status FROM sales_salesadjustment WHERE id = header_id;

    IF header_status = 'DRAFT' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    RAISE EXCEPTION
        'sales adjustment lines may only be changed while the adjustment is a draft'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_adjustment_line_follows_its_adjustment
    BEFORE UPDATE OR DELETE ON sales_salesadjustmentline
    FOR EACH ROW EXECUTE FUNCTION sales_adjustment_line_follows_its_adjustment();
"""

ADJUSTMENT_LINE_CONTAINMENT = """
CREATE OR REPLACE FUNCTION sales_adjustment_line_is_within_its_original()
RETURNS TRIGGER AS $$
DECLARE
    header_kind text;
    header_date date;
    original_quantity numeric;
    original_gross numeric;
    original_day_status text;
    original_day_date date;
    claimed_quantity numeric;
    claimed_gross numeric;
BEGIN
    SELECT reason_kind, business_date
      INTO header_kind, header_date
      FROM sales_salesadjustment
     WHERE id = NEW.adjustment_id;

    SELECT line.quantity, line.gross_amount, day.status, day.business_date
      INTO original_quantity, original_gross, original_day_status, original_day_date
      FROM sales_salesdayline AS line
      JOIN sales_salesday AS day ON day.id = line.sales_day_id
     WHERE line.id = NEW.original_line_id;

    IF original_day_status IS DISTINCT FROM 'POSTED' THEN
        RAISE EXCEPTION
            'a sales adjustment may only correct a posted sales day'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF header_date < original_day_date THEN
        RAISE EXCEPTION
            'a sales adjustment may not be dated before the day it corrects'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF header_kind = 'FINANCIAL_CORRECTION' THEN
        IF NEW.adjusted_quantity <> 0 THEN
            RAISE EXCEPTION
                'a financial correction may not change sold quantity: a money '
                'correction is not a claim that less food was sold'
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSE
        IF NEW.adjusted_quantity <= 0 THEN
            RAISE EXCEPTION
                'a cancellation or a return must name the quantity it takes back'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    -- Posted adjustments only. Two drafts may each propose the whole amount;
    -- only one of them can then post, which is what a draft should be able to do.
    SELECT COALESCE(SUM(other.adjusted_quantity), 0),
           COALESCE(SUM(other.adjusted_gross), 0)
      INTO claimed_quantity, claimed_gross
      FROM sales_salesadjustmentline AS other
      JOIN sales_salesadjustment AS header ON header.id = other.adjustment_id
     WHERE other.original_line_id = NEW.original_line_id
       AND other.id IS DISTINCT FROM NEW.id
       AND header.status = 'POSTED';

    IF claimed_quantity + NEW.adjusted_quantity > original_quantity THEN
        RAISE EXCEPTION
            'sales adjustments may not take back more quantity than the line sold'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF claimed_gross + NEW.adjusted_gross > original_gross THEN
        RAISE EXCEPTION
            'sales adjustments may not take back more value than the line sold'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_adjustment_line_is_within_its_original
    BEFORE INSERT OR UPDATE ON sales_salesadjustmentline
    FOR EACH ROW EXECUTE FUNCTION sales_adjustment_line_is_within_its_original();
"""

DROP = """
DROP TRIGGER IF EXISTS sales_adjustment_is_frozen ON sales_salesadjustment;
DROP FUNCTION IF EXISTS sales_adjustment_is_frozen();
DROP TRIGGER IF EXISTS sales_adjustment_line_follows_its_adjustment
    ON sales_salesadjustmentline;
DROP FUNCTION IF EXISTS sales_adjustment_line_follows_its_adjustment();
DROP TRIGGER IF EXISTS sales_adjustment_line_is_within_its_original
    ON sales_salesadjustmentline;
DROP FUNCTION IF EXISTS sales_adjustment_line_is_within_its_original();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0007_sales_adjustments"),
    ]

    operations = [
        migrations.RunSQL(sql=ADJUSTMENT_GUARD, reverse_sql=DROP),
        migrations.RunSQL(sql=ADJUSTMENT_LINE_GUARD, reverse_sql=DROP),
        migrations.RunSQL(sql=ADJUSTMENT_LINE_CONTAINMENT, reverse_sql=DROP),
    ]
