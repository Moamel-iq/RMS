"""
Freeze a cashier shift once the drawer has been counted.

Two guards, the same whole-row allowlist idiom `0006_sales_day_guards`,
`0008_sales_adjustment_guards` and `0010_application_settlement_guards` use:
copy `OLD` into a `%ROWTYPE` variable, apply the columns this state may move,
and refuse anything that still differs. An allowlist freezes a column added next
year *by default*, which is the safe direction to be wrong in — a blocklist
would let tomorrow's field through silently.

## Why `CLOSED` freezes the counted figures

`close_cashier_shift` is the cashier saying "this is what was in the drawer".
A declaration that can be edited after the approval fails is not a declaration:
the obvious move for somebody facing an awkward shortage is to adjust the count
until the variance disappears, and the count is the *only* independent evidence
this document has. The expected figures are frozen for the same reason from the
other direction — they are evidence of what was expected at the moment of the
count, and a figure that could be re-derived later would make an old variance
change whenever a later document did.

The way back is `reopen_cashier_shift`, which is deliberate, needs a reason, and
stays on the record. It writes `status`, `closed_at` and `closed_by_id`, all
three of which are on the `CLOSED` allowlist.

## What is *not* enforced here, and where it lives instead

Maker-checker — `closed_by` and `approved_by` differing — is a **check
constraint** on the table rather than a trigger, because it is a statement about
one row that is true at every moment rather than a rule about a transition. It
is `sales_shift_approver_is_not_the_closer` in `0011`, it is re-checked in
`approve_cashier_shift` under the row lock, and both are wanted: the service
check is the sentence an operator can act on, and the constraint is the one that
survives a data fix applied through a shell.

The refusal to close against a draft sales day lives in the service and not
here. It is a rule about *which document* may be named, the trigger would have
to re-read the day on every update of an unrelated column, and the constraint
`sales_shift_closed_names_its_day` already guarantees that a closed shift names
one at all.
"""

from django.db import migrations

SHIFT_GUARD = """
CREATE OR REPLACE FUNCTION sales_shift_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted sales_cashiershift%ROWTYPE;
BEGIN
    permitted := OLD;

    IF OLD.status = 'REVERSED' THEN
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a reversed cashier shift is history and may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'APPROVED' THEN
        permitted.status          := NEW.status;
        permitted.reversed_at     := NEW.reversed_at;
        permitted.reversed_by_id  := NEW.reversed_by_id;
        permitted.reversal_reason := NEW.reversal_reason;
        permitted.updated_at      := NEW.updated_at;

        IF NEW.status NOT IN ('APPROVED', 'REVERSED') THEN
            RAISE EXCEPTION
                'an approved cashier shift may only become REVERSED'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'an approved cashier shift is frozen: its counted and expected '
            'figures and its approval evidence may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'CLOSED' THEN
        permitted.status              := NEW.status;
        permitted.notes               := NEW.notes;
        permitted.closed_at           := NEW.closed_at;
        permitted.closed_by_id        := NEW.closed_by_id;
        permitted.approved_at         := NEW.approved_at;
        permitted.approved_by_id      := NEW.approved_by_id;
        permitted.number              := NEW.number;
        permitted.idempotency_key     := NEW.idempotency_key;
        permitted.request_fingerprint := NEW.request_fingerprint;
        permitted.updated_at          := NEW.updated_at;

        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a closed cashier shift is frozen: the counted and expected '
            'figures may not move. Reopen it instead, which stays on the record'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Open. The drawer is still being counted.
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_shift_is_frozen
    BEFORE UPDATE ON sales_cashiershift
    FOR EACH ROW EXECUTE FUNCTION sales_shift_is_frozen();
"""

TENDER_GUARD = """
CREATE OR REPLACE FUNCTION sales_shift_tender_follows_its_shift()
RETURNS TRIGGER AS $$
DECLARE
    header_status text;
    header_id bigint;
BEGIN
    header_id := COALESCE(NEW.shift_id, OLD.shift_id);
    -- Read through the foreign key, never from a copy on the child. A
    -- denormalised status would be one more thing to keep true, and the moment
    -- it drifted the guard would protect the wrong rows — the failure mode
    -- where a freeze is worse than none, because everybody believes it holds.
    SELECT status INTO header_status
      FROM sales_cashiershift
     WHERE id = header_id;

    IF header_status = 'OPEN' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    RAISE EXCEPTION
        'a tender count may only be changed while its shift is open'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_shift_tender_follows_its_shift
    BEFORE UPDATE OR DELETE ON sales_cashiertendercount
    FOR EACH ROW EXECUTE FUNCTION sales_shift_tender_follows_its_shift();
"""

DROP = """
DROP TRIGGER IF EXISTS sales_shift_is_frozen ON sales_cashiershift;
DROP FUNCTION IF EXISTS sales_shift_is_frozen();
DROP TRIGGER IF EXISTS sales_shift_tender_follows_its_shift ON sales_cashiertendercount;
DROP FUNCTION IF EXISTS sales_shift_tender_follows_its_shift();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0011_cashier_shifts"),
    ]

    operations = [
        migrations.RunSQL(sql=SHIFT_GUARD, reverse_sql=DROP),
        migrations.RunSQL(sql=TENDER_GUARD, reverse_sql=DROP),
    ]
