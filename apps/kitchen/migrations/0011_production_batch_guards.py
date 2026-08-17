"""
Freeze what a production draft may not correct, in the database.

Task 3.4 separates two kinds of fact on one document, and only a trigger can
keep them apart against every writer:

* **The decision** — which organization, branch, warehouse, recipe, version and
  planned date this batch is for, and, on each requirement, exactly which line
  of which version it came from by which path and at what recipe quantity.
  Frozen from creation.

  Note what is *not* in that list: the **multiplier**. How much of a recipe to
  make is a decision an operator may revise while the batch is a draft, and
  `rescale_production_batch` is the approved way to do it. What may not change
  is *which* recipe at *which* version — because that is what the requirements
  were flattened from. A wrong decision there is corrected by discarding the
  draft and creating another, because editing it in place would leave a
  half-finished document claiming to have been drafted from a structure it was
  not.

  Revisable is not the same as independently mutable. Migration **0015** adds a
  deferred consistency trigger holding `multiplier`,
  `expected_output_quantity` and every `planned_base_quantity` in agreement at
  COMMIT, so the scale can be changed by the approved command and by nothing
  that would leave the three disagreeing.
* **Reality** — how much was actually consumed, of what, and how much came out.
  Editable for as long as the batch is a draft, because that is what a draft is
  for.

A Python guard would be bypassed by a bulk update, raw SQL, the admin, a data
migration and anybody with a psql prompt. The services check first so the
operator gets an Arabic sentence naming the field; these triggers are what make
the check true rather than customary.

## The allowlist idiom

Both triggers use the whole-row allowlist from `accounting/0005`: copy `OLD`,
overwrite the columns that are *permitted* to change, and compare. Anything not
copied over is refused, so a column added next year is frozen by default rather
than forgotten. A blocklist has to be remembered; this does not.

`ProductionBatchLine` has an **empty** allowlist — every column on it is the
recipe's own fact — except `updated_at`, which `TimeStampedModel` writes on any
save and which carries no business meaning of its own.
"""

from django.db import migrations

BATCH_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_batch_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted kitchen_productionbatch%ROWTYPE;
BEGIN
    permitted := OLD;

    -- Reality, and the operator's own notes.
    permitted.actual_output_entered_quantity := NEW.actual_output_entered_quantity;
    permitted.actual_output_unit_id          := NEW.actual_output_unit_id;
    permitted.actual_output_base_quantity    := NEW.actual_output_base_quantity;
    permitted.notes                          := NEW.notes;
    permitted.updated_at                     := NEW.updated_at;

    -- How MUCH of the recipe, which `rescale_production_batch` is an approved
    -- command to change. The frozen decision is *which* recipe, at *which*
    -- version, for *which* branch, warehouse and date — not the scale. The
    -- expected output moves with the multiplier because it is derived from it,
    -- and a rescale that could not update it would leave the two disagreeing.
    permitted.multiplier                := NEW.multiplier;
    permitted.expected_output_quantity  := NEW.expected_output_quantity;

    -- Task 3.5 will need these to move; it removes this trigger when it does.
    permitted.status := NEW.status;
    permitted.number := NEW.number;

    IF NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'a production batch decision is frozen: organization, branch, warehouse, '
        'recipe, version and planned date may not change'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_productionbatch_is_frozen
    BEFORE UPDATE ON kitchen_productionbatch
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_batch_is_frozen();
"""

LINE_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_line_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted kitchen_productionbatchline%ROWTYPE;
BEGIN
    permitted := OLD;

    -- A rescale rewrites the planned quantity from the frozen source basis,
    -- and nothing else on this row. The source figures it recomputes *from*
    -- stay exactly as the recipe stated them, and migration 0015 checks at
    -- COMMIT that the rewritten figure is exactly that basis scaled by the
    -- batch's own multiplier — so "editable" here does not mean "arbitrary".
    permitted.planned_base_quantity := NEW.planned_base_quantity;
    permitted.updated_at            := NEW.updated_at;

    IF NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    -- "cumulative multiplier", not the batch's: the product of the component
    -- factors above this leaf, which is the recipe's own fact and frozen. The
    -- batch multiplier is neither on this table nor frozen anywhere.
    RAISE EXCEPTION
        'a production requirement is frozen: source version, source line, path, '
        'item, recipe quantity, cumulative multiplier and cost class may not change'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_productionbatchline_is_frozen
    BEFORE UPDATE ON kitchen_productionbatchline
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_line_is_frozen();
"""

WAREHOUSE_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_batch_warehouse_in_branch()
RETURNS TRIGGER AS $$
DECLARE
    warehouse_branch bigint;
    branch_organization bigint;
BEGIN
    SELECT branch_id INTO warehouse_branch
      FROM inventory_warehouse WHERE id = NEW.warehouse_id;
    IF warehouse_branch IS DISTINCT FROM NEW.branch_id THEN
        RAISE EXCEPTION
            'a production batch warehouse must belong to its own branch'
            USING ERRCODE = 'restrict_violation';
    END IF;

    SELECT organization_id INTO branch_organization
      FROM organizations_branch WHERE id = NEW.branch_id;
    IF branch_organization IS DISTINCT FROM NEW.organization_id THEN
        RAISE EXCEPTION
            'a production batch branch must belong to its own organization'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_productionbatch_warehouse_in_branch
    BEFORE INSERT OR UPDATE ON kitchen_productionbatch
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_batch_warehouse_in_branch();
"""

DROP = """
DROP TRIGGER IF EXISTS kitchen_productionbatch_is_frozen ON kitchen_productionbatch;
DROP TRIGGER IF EXISTS kitchen_productionbatchline_is_frozen ON kitchen_productionbatchline;
DROP TRIGGER IF EXISTS kitchen_productionbatch_warehouse_in_branch ON kitchen_productionbatch;
DROP FUNCTION IF EXISTS kitchen_production_batch_is_frozen();
DROP FUNCTION IF EXISTS kitchen_production_line_is_frozen();
DROP FUNCTION IF EXISTS kitchen_production_batch_warehouse_in_branch();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0010_production_batch_drafting"),
    ]

    operations = [
        migrations.RunSQL(
            sql=BATCH_GUARD + LINE_GUARD + WAREHOUSE_GUARD,
            reverse_sql=DROP,
        ),
    ]
