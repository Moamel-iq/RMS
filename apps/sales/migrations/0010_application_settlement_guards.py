"""
Freeze a settlement once it has been declared, and keep every allocation
inside the receivable entry it claims.

Three guards, the same whole-row allowlist idiom `0006_sales_day_guards` and
`0008_sales_adjustment_guards` use: copy `OLD` into a `%ROWTYPE` variable, apply
the columns this state may move, and refuse anything that still differs. An
allowlist freezes a column added next year *by default*, which is the safe
direction to be wrong in.

## Why `RECONCILED` freezes the three figures

Reconciling is somebody asserting that every dinar of both gaps has been claimed
by a named reason. A statement that can be edited after it is made is not a
statement — moving `statement_amount` under a completed reconciliation would
leave the adjustment rows claiming a gap that no longer exists, and the
settlement would post a variance nobody agreed to. The way back is deliberate
and on the record: `return_settlement_to_draft` writes `status` and clears the
reconciliation, both of which are on the allowlist.

`expected_amount` is not on that allowlist either, and that is the point of
stamping it: it is evidence of what the allocations claimed at the moment of
reconciliation, and the allocations themselves are frozen alongside it by the
second guard.

## Why over-allocation is a trigger and not a service check

`sales_settlement_allocation_is_within_its_entry` enforces four things a raw
`INSERT` would walk straight past:

* the entry must be a **debit** — a credit entry is a payment or a return, not
  something to be paid, and allocating against one would be claiming to settle
  a settlement;
* the entry must belong to the same organization *and* the same delivery
  application as the settlement, because an allocation across counterparties
  would clear one company's debt with another's money;
* the entry's `business_date` may not be after the settlement's `period_end`,
  because a statement cannot pay for a sale that had not happened when it was
  issued;
* and Σ `allocated_amount` over allocations belonging to **posted** settlements,
  plus this row, may not exceed `entry.debit`.

Over-allocating is the interesting one, and it is the same class of error as
absorbing a variance: "this receivable was paid twice" surfaces mid-argument
with the counterparty, months later, with nobody able to say which of the
overlapping settlements was the wrong one.

Only **posted** settlements count toward the running total. Two drafts may each
propose the whole of an entry; only one of them can then post — which is exactly
what a draft should be able to do, and it is why `unallocated_debit` in
`apps/sales/receivables.py` counts the same set.
"""

from django.db import migrations

SETTLEMENT_GUARD = """
CREATE OR REPLACE FUNCTION sales_settlement_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted sales_deliveryapplicationsettlement%ROWTYPE;
BEGIN
    permitted := OLD;

    IF OLD.status = 'REVERSED' THEN
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a reversed application settlement is history and may not change'
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
                'a posted application settlement may only become REVERSED'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a posted application settlement is frozen: its figures, its '
            'statement reference and its posting evidence may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'RECONCILED' THEN
        permitted.status              := NEW.status;
        permitted.notes               := NEW.notes;
        permitted.reconciled_at       := NEW.reconciled_at;
        permitted.reconciled_by_id    := NEW.reconciled_by_id;
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
            'a reconciled application settlement is frozen: the expected, '
            'statement and remitted figures may not move. Return it to draft '
            'instead, which stays on the record'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- A draft. Everything moves except the evidence that it reconciled.
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_settlement_is_frozen
    BEFORE UPDATE ON sales_deliveryapplicationsettlement
    FOR EACH ROW EXECUTE FUNCTION sales_settlement_is_frozen();
"""

SETTLEMENT_CHILD_GUARD = """
CREATE OR REPLACE FUNCTION sales_settlement_child_follows_its_settlement()
RETURNS TRIGGER AS $$
DECLARE
    header_status text;
    header_id bigint;
BEGIN
    header_id := COALESCE(NEW.settlement_id, OLD.settlement_id);
    -- Read through the foreign key, never from a copy on the child. A
    -- denormalised status would be one more thing to keep true, and the moment
    -- it drifted the guard would protect the wrong rows — the failure mode
    -- where a freeze is worse than none, because everybody believes it holds.
    SELECT status INTO header_status
      FROM sales_deliveryapplicationsettlement
     WHERE id = header_id;

    IF header_status = 'DRAFT' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    RAISE EXCEPTION
        'settlement allocations and adjustments may only be changed while the '
        'settlement is a draft'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_settlement_allocation_follows_its_settlement
    BEFORE UPDATE OR DELETE ON sales_deliveryapplicationsettlementallocation
    FOR EACH ROW EXECUTE FUNCTION sales_settlement_child_follows_its_settlement();

CREATE TRIGGER sales_settlement_adjustment_follows_its_settlement
    BEFORE UPDATE OR DELETE ON sales_deliveryapplicationsettlementadjustment
    FOR EACH ROW EXECUTE FUNCTION sales_settlement_child_follows_its_settlement();
"""

ALLOCATION_CONTAINMENT = """
CREATE OR REPLACE FUNCTION sales_settlement_allocation_is_within_its_entry()
RETURNS TRIGGER AS $$
DECLARE
    settlement_organization_id bigint;
    settlement_application_id bigint;
    settlement_period_end date;
    entry_organization_id bigint;
    entry_application_id bigint;
    entry_business_date date;
    entry_debit numeric;
    claimed numeric;
BEGIN
    SELECT organization_id, delivery_application_id, period_end
      INTO settlement_organization_id, settlement_application_id, settlement_period_end
      FROM sales_deliveryapplicationsettlement
     WHERE id = NEW.settlement_id;

    SELECT organization_id, delivery_application_id, business_date, debit
      INTO entry_organization_id, entry_application_id, entry_business_date, entry_debit
      FROM sales_applicationreceivableentry
     WHERE id = NEW.receivable_entry_id;

    IF entry_debit <= 0 THEN
        RAISE EXCEPTION
            'a settlement allocates against what an application owes: a credit '
            'entry is a payment, not something to be paid'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF entry_organization_id IS DISTINCT FROM settlement_organization_id
       OR entry_application_id IS DISTINCT FROM settlement_application_id THEN
        RAISE EXCEPTION
            'a settlement allocation must name a receivable entry of its own '
            'organization and its own delivery application'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF entry_business_date > settlement_period_end THEN
        RAISE EXCEPTION
            'a statement cannot pay for a sale that had not happened when it '
            'was issued'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Posted settlements only. Two drafts may each propose the whole entry;
    -- only one of them can then post.
    SELECT COALESCE(SUM(other.allocated_amount), 0)
      INTO claimed
      FROM sales_deliveryapplicationsettlementallocation AS other
      JOIN sales_deliveryapplicationsettlement AS header ON header.id = other.settlement_id
     WHERE other.receivable_entry_id = NEW.receivable_entry_id
       AND other.id IS DISTINCT FROM NEW.id
       AND header.status = 'POSTED';

    IF claimed + NEW.allocated_amount > entry_debit THEN
        RAISE EXCEPTION
            'settlements may not allocate more than a receivable entry owes'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sales_settlement_allocation_is_within_its_entry
    BEFORE INSERT OR UPDATE ON sales_deliveryapplicationsettlementallocation
    FOR EACH ROW EXECUTE FUNCTION sales_settlement_allocation_is_within_its_entry();
"""

DROP = """
DROP TRIGGER IF EXISTS sales_settlement_is_frozen
    ON sales_deliveryapplicationsettlement;
DROP FUNCTION IF EXISTS sales_settlement_is_frozen();
DROP TRIGGER IF EXISTS sales_settlement_allocation_follows_its_settlement
    ON sales_deliveryapplicationsettlementallocation;
DROP TRIGGER IF EXISTS sales_settlement_adjustment_follows_its_settlement
    ON sales_deliveryapplicationsettlementadjustment;
DROP FUNCTION IF EXISTS sales_settlement_child_follows_its_settlement();
DROP TRIGGER IF EXISTS sales_settlement_allocation_is_within_its_entry
    ON sales_deliveryapplicationsettlementallocation;
DROP FUNCTION IF EXISTS sales_settlement_allocation_is_within_its_entry();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0009_application_settlements"),
    ]

    operations = [
        migrations.RunSQL(sql=SETTLEMENT_GUARD, reverse_sql=DROP),
        migrations.RunSQL(sql=SETTLEMENT_CHILD_GUARD, reverse_sql=DROP),
        migrations.RunSQL(sql=ALLOCATION_CONTAINMENT, reverse_sql=DROP),
    ]
