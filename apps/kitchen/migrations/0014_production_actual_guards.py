"""
Make an unapproved substitution unrepresentable, not merely refused.

`production.py` checks every rule below and returns an Arabic sentence naming
the field, which is what an operator needs. These triggers are what make the
check *true* rather than customary: a bulk update, raw SQL, the admin, a data
migration and a psql prompt all reach these tables, and the whole point of the
substitute table is that somebody approved what went in the pot.

Three facts a `CheckConstraint` cannot see, because each spans two tables:

1. **The approval belongs to this requirement's own recipe line.** A substitute
   approved for the rice line is not approved for the oil line, even when both
   name rice. Without this, an id from the wrong line is one crafted request
   away from looking approved.
2. **The item is the one that approval names.** An approval row pointing at
   cardamom does not authorize consuming saffron; storing both and never
   comparing them would make the link decorative.
3. **The item belongs to the batch's own organization.** Refused in the service
   before the lookup, and refused again here, because a foreign id must never
   widen scope even by accident.

`BEFORE INSERT OR UPDATE`, so a row that would break any of the three never
lands rather than being found later by the verifier.
"""

from django.db import migrations

ACTUAL_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_production_actual_is_approved()
RETURNS TRIGGER AS $$
DECLARE
    requirement_source_line bigint;
    requirement_item bigint;
    batch_organization bigint;
    approval_line bigint;
    approval_item bigint;
    actual_item_organization bigint;
BEGIN
    SELECT l.source_line_id, l.item_id, b.organization_id
      INTO requirement_source_line, requirement_item, batch_organization
      FROM kitchen_productionbatchline l
      JOIN kitchen_productionbatch b ON b.id = l.batch_id
     WHERE l.id = NEW.line_id;

    SELECT organization_id INTO actual_item_organization
      FROM inventory_inventoryitem WHERE id = NEW.item_id;
    IF actual_item_organization IS DISTINCT FROM batch_organization THEN
        RAISE EXCEPTION
            'a production actual item must belong to the batch organization'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.substitute_id IS NULL THEN
        -- A primary row answers for the requirement's own item and nothing else.
        IF NEW.item_id IS DISTINCT FROM requirement_item THEN
            RAISE EXCEPTION
                'a primary production actual must name the requirement item'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    SELECT line_id, substitute_item_id INTO approval_line, approval_item
      FROM kitchen_recipelinesubstitute WHERE id = NEW.substitute_id;

    IF approval_line IS DISTINCT FROM requirement_source_line THEN
        RAISE EXCEPTION
            'a production substitute must be approved for this requirement''s own recipe line'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF approval_item IS DISTINCT FROM NEW.item_id THEN
        RAISE EXCEPTION
            'a production substitute must name the item its approval names'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_productionbatchactualline_is_approved
    BEFORE INSERT OR UPDATE ON kitchen_productionbatchactualline
    FOR EACH ROW EXECUTE FUNCTION kitchen_production_actual_is_approved();
"""

DROP = """
DROP TRIGGER IF EXISTS kitchen_productionbatchactualline_is_approved
    ON kitchen_productionbatchactualline;
DROP FUNCTION IF EXISTS kitchen_production_actual_is_approved();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0013_production_actual_cardinality"),
    ]

    operations = [
        migrations.RunSQL(sql=ACTUAL_GUARD, reverse_sql=DROP),
    ]
