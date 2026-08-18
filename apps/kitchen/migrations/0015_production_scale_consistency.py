"""
Make the batch multiplier revisable without making it independently mutable.

Migration 0011 froze the *decision* a production draft records — which
organization, branch, warehouse, recipe, version and planned date — and
deliberately left the **multiplier** out of that list. How much of a recipe to
make is a decision an operator may revise while the batch is a draft, and
`rescale_production_batch` is the approved way to revise it.

That left a gap this migration closes. Three columns are three views of one
decision:

    ProductionBatch.multiplier
    ProductionBatch.expected_output_quantity
    ProductionBatchLine.planned_base_quantity   (one per requirement)

Permitting the first to change necessarily permits the other two, because a
rescale must rewrite them. But permitting each *independently* would allow a
batch that claims to be double the recipe, expects a single output, and asks the
kitchen for one and a half times the rice — three numbers, no two of which agree,
and nothing in the schema to say which one is the lie.

So the invariant is enforced instead of the immutability:

    expected_output_quantity
        = round(recipe_version.expected_output_quantity × multiplier, 6)

    planned_base_quantity
        = round(source_base_quantity × cumulative_multiplier × multiplier, 6)

## Why deferred

A legitimate rescale updates the header and every requirement row. Between those
statements the batch is *inconsistent by construction* — after the header is
written and before the last line is, the multiplier is new and some planned
quantities are old. An immediate check would refuse the only correct way to
perform the operation.

`CREATE CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED` moves the check to
COMMIT, which is the only boundary at which the question "is this batch
coherent?" has a meaningful answer. The project idiom is
`inventory/0013_transfer_guards`, which defers for the same reason: posting
writes child lines and flips the parent, and the order cannot be made to satisfy
an immediate check.

## Why it re-reads instead of trusting NEW

A deferred trigger fires at COMMIT with the tuple captured when the statement
ran, and by then the row may be gone — discarding a draft is the ordinary case,
and it cascades to every requirement. Validating the captured tuple would refuse
a discard because of a batch that no longer exists. So the function looks the
current row up by id and returns silently when it is absent: the invariant is
about what is *being committed*, not about what was written mid-transaction.

## Why `round(x, 6)` matches Python exactly

PostgreSQL `round(numeric, n)` rounds halves away from zero, which is
`ROUND_HALF_UP` — the same policy `apps/core/quantity.quantize_calculation`
applies at `CALCULATION_PLACES`. `numeric` multiplication is exact, so
`production.scaled_line_quantity` carries eighty digits of context to be exact
too; see the comment there for the two defects that mirror surfaced.

## What it does not do

It does not repair. A planted inconsistency is refused at the boundary and
reported by `verify_production_drafts` where it already exists; neither one
rewrites a figure to make a batch look coherent, because a corrected number
nobody can trace is worse than a refusal somebody has to read.

## Why this is additive and 0011 was corrected at source

0011 was edited after it was written — the multiplier belongs out of its frozen
allowlist — which normally means the file can no longer be trusted to describe
what an existing database holds. Checked before assuming: every database on this
machine reports `kitchen` at `0009` or earlier and no
`kitchen_production_line_is_frozen` function anywhere, so the edited 0011 is the
only definition that has ever existed and it converges by construction. Its
requirement-guard message still said "multiplier" where it meant the line's own
frozen `cumulative_multiplier`; that wording was corrected in 0011 itself rather
than replaced here, because a second `CREATE OR REPLACE` of one function across
two migrations is a definition a reader has to diff.

This migration is additive for the ordinary reason instead: the invariant it adds
did not exist before.
"""

from django.db import migrations

SCALE_CONSISTENCY = """
CREATE OR REPLACE FUNCTION kitchen_production_scale_is_consistent()
RETURNS TRIGGER AS $$
DECLARE
    subject bigint;
    batch_multiplier numeric;
    stored_expected numeric;
    version_output numeric;
    required_expected numeric;
    offender record;
BEGIN
    -- One function, two tables: the batch's own event and any of its lines'.
    IF TG_TABLE_NAME = 'kitchen_productionbatch' THEN
        subject := NEW.id;
    ELSE
        subject := NEW.batch_id;
    END IF;

    -- Deferred to COMMIT, so the row this event captured may since have been
    -- deleted. A discarded draft must not be refused on account of the batch it
    -- no longer is.
    SELECT b.multiplier, b.expected_output_quantity, v.expected_output_quantity
      INTO batch_multiplier, stored_expected, version_output
      FROM kitchen_productionbatch b
      JOIN kitchen_recipeversion v ON v.id = b.recipe_version_id
     WHERE b.id = subject;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    required_expected := round(version_output * batch_multiplier, 6);
    IF stored_expected IS DISTINCT FROM required_expected THEN
        RAISE EXCEPTION
            'production batch % expects output % but its version produces % at multiplier % (%)',
            subject, stored_expected, version_output, batch_multiplier, required_expected
            USING ERRCODE = 'check_violation';
    END IF;

    -- The first disagreeing requirement, in business order, so the message names
    -- a line an operator can find rather than whichever row the planner reached.
    SELECT l.line_order,
           l.planned_base_quantity,
           round(l.source_base_quantity * l.cumulative_multiplier * batch_multiplier, 6)
               AS required
      INTO offender
      FROM kitchen_productionbatchline l
     WHERE l.batch_id = subject
       AND l.planned_base_quantity IS DISTINCT FROM
           round(l.source_base_quantity * l.cumulative_multiplier * batch_multiplier, 6)
     ORDER BY l.line_order
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION
            'production batch % requirement % is planned at % but its source basis '
            'at multiplier % gives %',
            subject, offender.line_order, offender.planned_base_quantity,
            batch_multiplier, offender.required
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER kitchen_productionbatch_scale_is_consistent
    AFTER INSERT OR UPDATE ON kitchen_productionbatch
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_scale_is_consistent();

CREATE CONSTRAINT TRIGGER kitchen_productionbatchline_scale_is_consistent
    AFTER INSERT OR UPDATE ON kitchen_productionbatchline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_scale_is_consistent();
"""

DROP = """
DROP TRIGGER IF EXISTS kitchen_productionbatchline_scale_is_consistent
    ON kitchen_productionbatchline;
DROP TRIGGER IF EXISTS kitchen_productionbatch_scale_is_consistent
    ON kitchen_productionbatch;
DROP FUNCTION IF EXISTS kitchen_production_scale_is_consistent();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0014_production_actual_guards"),
    ]

    operations = [
        migrations.RunSQL(sql=SCALE_CONSISTENCY, reverse_sql=DROP),
    ]
