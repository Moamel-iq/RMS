"""
What the ORM cannot express about a nested recipe component.

Three guarantees, and each one closes a hole the model layer leaves open.

**A component follows its parent's draft.** The same rule migration `0005` gave
lines, substitutes, steps, step links and servings, extended to the sixth owned
table: insert, update and delete only while the parent version is `DRAFT`, and
no component may be moved to another version. Approval that froze the
ingredient list and left the sub-recipes editable would freeze half a recipe.

**A component is a whole row, not a list of fields.** Once the parent leaves
`DRAFT` there is no permitted transition at all — a component has no lifecycle
of its own, so the allowlist is empty and the comparison is whole-row. A column
added to this table next year is protected the day it is added, which a
blocklist of field names could not promise (`accounting/0005` is the migration
that taught this project the difference).

**Both denormalised recipes stay equal to their versions'.** `recipe` and
`component_recipe` exist so a `CheckConstraint` can say *"a version may not
contain its own recipe"* — a constraint sees only its own table, and that is the
one cycle case cheap enough to make unrepresentable rather than merely refused.
Held equal by trigger, so the columns cannot drift into telling a different
story from the versions they were copied from. The trigger also refuses the
**stocked** shape outright (RCP-070): a recipe with an `output_item` is consumed
as a `RecipeLine` at its book value, and admitting it here as well is precisely
the double counting the design exists to make unrepresentable.

The multi-level cycle and the depth bound are **not** here, and cannot be.
Postgres will not express "the transitive closure of this version's components
excludes its own recipe" as a row constraint, so `graph.py` enforces it under an
advisory lock — which is also what makes it hold against two transactions that
each see an acyclic graph.
"""

from django.db import migrations

COMPONENT_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_recipe_component_follows_its_version()
RETURNS TRIGGER AS $$
DECLARE
    parent_recipe bigint;
    child_recipe bigint;
    child_output bigint;
    child_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM kitchen_require_draft_version(OLD.version_id, 'a recipe component');
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        PERFORM kitchen_require_draft_version(OLD.version_id, 'a recipe component');
        IF NEW.version_id IS DISTINCT FROM OLD.version_id THEN
            RAISE EXCEPTION
                'a recipe component cannot be moved to another recipe version'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- A component has no lifecycle of its own: while the parent is a draft
        -- everything about it may change, and once the parent is frozen nothing
        -- may. So the allowlist of permitted transitions is **empty**, and the
        -- guard above it is the whole rule — which makes this stronger than
        -- `0005`'s five-transition allowlist rather than weaker. No column is
        -- named anywhere, so a column added to this table next year is
        -- protected the moment it exists, with nobody having to remember it.
        --
        -- Identity is refused even while the parent *is* a draft: `public_id`
        -- is what a Task 3.4 batch line will point at, and a UID somebody could
        -- rewrite is not an identity.
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.public_id IS DISTINCT FROM OLD.public_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION
                'the identity of recipe component % is immutable', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    PERFORM kitchen_require_draft_version(NEW.version_id, 'a recipe component');

    -- The denormalised recipes must be the versions' own.
    SELECT recipe_id INTO parent_recipe
    FROM kitchen_recipeversion WHERE id = NEW.version_id;
    SELECT recipe_id, status INTO child_recipe, child_status
    FROM kitchen_recipeversion WHERE id = NEW.component_version_id;

    IF NEW.recipe_id IS DISTINCT FROM parent_recipe THEN
        RAISE EXCEPTION 'a recipe component must name its version''s own recipe'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.component_recipe_id IS DISTINCT FROM child_recipe THEN
        RAISE EXCEPTION
            'a recipe component must name the child version''s own recipe'
            USING ERRCODE = 'check_violation';
    END IF;

    -- RCP-070. The mutual exclusion, at the database: a recipe that produces an
    -- inventory item is stock, and stock is consumed as a line at its book
    -- value. Admitting it here too is how a blend gets charged twice.
    SELECT output_item_id INTO child_output FROM kitchen_recipe WHERE id = child_recipe;
    IF child_output IS NOT NULL THEN
        RAISE EXCEPTION
            'recipe % produces a stocked item and is consumed as a line, not as '
            'a component', child_recipe
            USING ERRCODE = 'check_violation';
    END IF;

    -- Only a frozen, approved child. A parent built on a draft is a parent
    -- built on something somebody is still typing.
    IF child_status NOT IN ('APPROVED', 'ACTIVE', 'SUPERSEDED') THEN
        RAISE EXCEPTION
            'recipe version % is % and cannot be used as a component',
            NEW.component_version_id, child_status
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_recipe_component_follows_its_version
    BEFORE INSERT OR UPDATE OR DELETE ON kitchen_recipecomponent
    FOR EACH ROW EXECUTE FUNCTION kitchen_recipe_component_follows_its_version();
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS kitchen_recipe_component_follows_its_version
    ON kitchen_recipecomponent;
DROP FUNCTION IF EXISTS kitchen_recipe_component_follows_its_version();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0006_recipe_component"),
    ]

    operations = [
        migrations.RunSQL(sql=COMPONENT_GUARD, reverse_sql=DROP_ALL),
    ]
