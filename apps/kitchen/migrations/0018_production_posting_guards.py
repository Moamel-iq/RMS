"""
Freeze a posted batch in the database, and let a draft keep moving.

Task 3.4's `kitchen_production_batch_is_frozen` had two lines reading

    permitted.status := NEW.status;
    permitted.number := NEW.number;

with the comment "Task 3.5 will need these to move; it removes this trigger
when it does." This is that removal. The replacement is one function rather
than two triggers, because the rule is genuinely one rule with a branch in it:

* **While the batch is a draft** the old allowlist applies unchanged — reality
  and the operator's notes and the scale may move, the decision may not — and
  in addition the posting columns may be written, because writing them is what
  posting *is*. The service is the only thing that writes them together, and
  `production_batch_posting_evidence_is_complete` refuses the row where they
  are written apart.

* **Once the batch is `POSTED`** the allowlist collapses to the reversal
  columns and nothing else. Not the quantities, not the output, not the notes,
  not the number, not the values. A posted batch is an economic event that has
  already reached two ledgers; the way to correct it is `REVERSED` plus a fresh
  draft, which is the standing rule for every posted document in this system.

* **Once the batch is `REVERSED`** nothing may change at all. A reversal is
  once-only, and "reverse the reversal" is the shape that turns an audit trail
  into a negotiation.

The requirement and actual-row triggers gain the same branch: they were
editable because the batch was a draft, so they stop being editable when it
stops being one. The `DELETE` guards are new here for the same reason — Task
3.4 let an operator remove a substitute row from a draft, and there is no
reading under which removing one from a posted batch is a correction rather
than a rewrite.

Allocations are frozen by the same rule from the same fact, and they carry one
extra permission while the batch is a draft: posting writes `movement_id` and
`consumed_value` back onto each row from what the kernel actually charged.

## Why the parent's status, and not a column on the child

Every child trigger re-reads the batch's status through its own foreign keys
rather than trusting a denormalised copy. A copy would be one more thing to
keep true, and the moment it drifted the guard would protect the wrong rows —
which is the failure mode where a freeze is worse than no freeze, because
everybody believes it is holding.
"""

from django.db import migrations

BATCH_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_batch_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted kitchen_productionbatch%ROWTYPE;
BEGIN
    permitted := OLD;

    IF OLD.status = 'REVERSED' THEN
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a reversed production batch is history and may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        -- The only permitted transition out of POSTED, and only these columns.
        permitted.status                    := NEW.status;
        permitted.reversed_at               := NEW.reversed_at;
        permitted.reversed_by_id            := NEW.reversed_by_id;
        permitted.reversal_reason           := NEW.reversal_reason;
        permitted.reversal_stock_entry_id   := NEW.reversal_stock_entry_id;
        permitted.reversal_journal_entry_id := NEW.reversal_journal_entry_id;
        permitted.updated_at                := NEW.updated_at;

        IF NEW.status NOT IN ('POSTED', 'REVERSED') THEN
            RAISE EXCEPTION
                'a posted production batch may only become REVERSED'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a posted production batch is frozen: quantities, output, values, '
            'number and posting evidence may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- ---- The batch is a draft. Task 3.4's allowlist, unchanged. ----------
    permitted.actual_output_entered_quantity := NEW.actual_output_entered_quantity;
    permitted.actual_output_unit_id          := NEW.actual_output_unit_id;
    permitted.actual_output_base_quantity    := NEW.actual_output_base_quantity;
    permitted.notes                          := NEW.notes;
    permitted.updated_at                     := NEW.updated_at;
    permitted.multiplier                     := NEW.multiplier;
    permitted.expected_output_quantity       := NEW.expected_output_quantity;

    -- ---- ...plus everything one posting writes, together. ----------------
    permitted.status                    := NEW.status;
    permitted.number                    := NEW.number;
    permitted.posted_at                 := NEW.posted_at;
    permitted.posted_by_id              := NEW.posted_by_id;
    permitted.stock_entry_id            := NEW.stock_entry_id;
    permitted.journal_entry_id          := NEW.journal_entry_id;
    permitted.output_item_id            := NEW.output_item_id;
    permitted.output_lot_id             := NEW.output_lot_id;
    permitted.output_movement_id        := NEW.output_movement_id;
    permitted.input_value               := NEW.input_value;
    permitted.output_value              := NEW.output_value;
    permitted.post_idempotency_key      := NEW.post_idempotency_key;
    permitted.post_request_fingerprint  := NEW.post_request_fingerprint;
    permitted.posting_rule_version      := NEW.posting_rule_version;

    IF NEW.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'a production batch must be posted before it can be reversed'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'a production batch decision is frozen: organization, branch, warehouse, '
        'recipe, version and planned date may not change'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

LINE_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_line_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted kitchen_productionbatchline%ROWTYPE;
    batch_status varchar(12);
BEGIN
    SELECT status INTO batch_status
      FROM kitchen_productionbatch WHERE id = NEW.batch_id;

    IF batch_status IS DISTINCT FROM 'DRAFT' THEN
        permitted := OLD;
        permitted.updated_at := NEW.updated_at;
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a requirement of a posted production batch is frozen'
            USING ERRCODE = 'restrict_violation';
    END IF;

    permitted := OLD;
    permitted.planned_base_quantity := NEW.planned_base_quantity;
    permitted.updated_at            := NEW.updated_at;

    IF NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'a production requirement is frozen: source version, source line, path, '
        'item, recipe quantity, cumulative multiplier and cost class may not change'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

ACTUAL_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_actual_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted kitchen_productionbatchactualline%ROWTYPE;
    batch_status varchar(12);
BEGIN
    SELECT b.status INTO batch_status
      FROM kitchen_productionbatchline l
      JOIN kitchen_productionbatch b ON b.id = l.batch_id
     WHERE l.id = NEW.line_id;

    IF batch_status IS DISTINCT FROM 'DRAFT' THEN
        permitted := OLD;
        permitted.updated_at := NEW.updated_at;
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a consumption row of a posted production batch is frozen'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_productionbatchactualline_is_frozen
    BEFORE UPDATE ON kitchen_productionbatchactualline
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_actual_is_frozen();
"""

ALLOCATION_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_allocation_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted kitchen_productionbatchallocation%ROWTYPE;
    batch_status varchar(12);
BEGIN
    SELECT b.status INTO batch_status
      FROM kitchen_productionbatchactualline a
      JOIN kitchen_productionbatchline l ON l.id = a.line_id
      JOIN kitchen_productionbatch b ON b.id = l.batch_id
     WHERE a.id = NEW.actual_id;

    permitted := OLD;
    permitted.updated_at := NEW.updated_at;

    IF batch_status IS DISTINCT FROM 'DRAFT' THEN
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'an allocation of a posted production batch is frozen'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- A draft may be re-allocated freely, and posting writes back what the
    -- kernel charged. Both happen while the batch is still DRAFT, inside the
    -- posting transaction, before the header moves to POSTED.
    permitted.lot_id          := NEW.lot_id;
    permitted.location_id     := NEW.location_id;
    permitted.base_quantity   := NEW.base_quantity;
    permitted.movement_id     := NEW.movement_id;
    permitted.consumed_value  := NEW.consumed_value;

    IF NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'a production allocation may not change which actual row it belongs to'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_productionbatchallocation_is_frozen
    BEFORE UPDATE ON kitchen_productionbatchallocation
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_allocation_is_frozen();
"""

DELETE_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_posted_row_survives()
RETURNS TRIGGER AS $$
DECLARE
    batch_status varchar(12);
BEGIN
    IF TG_TABLE_NAME = 'kitchen_productionbatch' THEN
        batch_status := OLD.status;
    ELSIF TG_TABLE_NAME = 'kitchen_productionbatchline' THEN
        SELECT status INTO batch_status
          FROM kitchen_productionbatch WHERE id = OLD.batch_id;
    ELSIF TG_TABLE_NAME = 'kitchen_productionbatchactualline' THEN
        SELECT b.status INTO batch_status
          FROM kitchen_productionbatchline l
          JOIN kitchen_productionbatch b ON b.id = l.batch_id
         WHERE l.id = OLD.line_id;
    ELSE
        SELECT b.status INTO batch_status
          FROM kitchen_productionbatchactualline a
          JOIN kitchen_productionbatchline l ON l.id = a.line_id
          JOIN kitchen_productionbatch b ON b.id = l.batch_id
         WHERE a.id = OLD.actual_id;
    END IF;

    -- The parent is already gone, so this is a cascade from a batch that the
    -- batch-level guard has itself allowed. Nothing to protect.
    IF batch_status IS NULL THEN
        RETURN OLD;
    END IF;

    IF batch_status = 'DRAFT' THEN
        RETURN OLD;
    END IF;

    RAISE EXCEPTION
        'a posted production batch and its rows may not be deleted; reverse it instead'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_productionbatch_posted_row_survives
    BEFORE DELETE ON kitchen_productionbatch
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_posted_row_survives();
CREATE TRIGGER kitchen_productionbatchline_posted_row_survives
    BEFORE DELETE ON kitchen_productionbatchline
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_posted_row_survives();
CREATE TRIGGER kitchen_productionbatchactualline_posted_row_survives
    BEFORE DELETE ON kitchen_productionbatchactualline
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_posted_row_survives();
CREATE TRIGGER kitchen_productionbatchallocation_posted_row_survives
    BEFORE DELETE ON kitchen_productionbatchallocation
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_posted_row_survives();
"""

#: The 0014 trigger checked that a substitute row names an approved stand-in.
#: It stays; this one is additional, and the two coexist on the same table.
DROP = """
DROP TRIGGER IF EXISTS kitchen_productionbatchactualline_is_frozen
    ON kitchen_productionbatchactualline;
DROP TRIGGER IF EXISTS kitchen_productionbatchallocation_is_frozen
    ON kitchen_productionbatchallocation;
DROP TRIGGER IF EXISTS kitchen_productionbatch_posted_row_survives ON kitchen_productionbatch;
DROP TRIGGER IF EXISTS kitchen_productionbatchline_posted_row_survives
    ON kitchen_productionbatchline;
DROP TRIGGER IF EXISTS kitchen_productionbatchactualline_posted_row_survives
    ON kitchen_productionbatchactualline;
DROP TRIGGER IF EXISTS kitchen_productionbatchallocation_posted_row_survives
    ON kitchen_productionbatchallocation;
DROP FUNCTION IF EXISTS kitchen_production_actual_is_frozen();
DROP FUNCTION IF EXISTS kitchen_production_allocation_is_frozen();
DROP FUNCTION IF EXISTS kitchen_production_posted_row_survives();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0017_production_posting"),
    ]

    operations = [
        migrations.RunSQL(
            sql=BATCH_GUARD + LINE_GUARD + ACTUAL_GUARD + ALLOCATION_GUARD + DELETE_GUARD,
            reverse_sql=DROP,
        ),
    ]
