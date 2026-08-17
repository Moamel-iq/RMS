"""
Make the three cost-snapshot tables append-only in the database.

A Python guard would be bypassed by a bulk update, raw SQL, the admin, a data
migration, and anybody with a psql prompt — which is exactly the set of people
a costing record exists to keep honest. The triggers refuse UPDATE and DELETE
for everyone, including the application's own ORM and including a superuser.

Same shape as `core/0002_auditevent_append_only`, deliberately: a reader who
knows how the audit trail is protected already knows how this is. SQLSTATE
23001 (`restrict_violation`) is in the integrity-violation class, so psycopg
surfaces it as `django.db.IntegrityError` and callers handle it like any other
constraint.

**All three tables, not only the header.** A snapshot whose header could not be
edited but whose lines could would be a document whose total no longer agreed
with the figures that produced it — the one failure the append-only rule exists
to prevent. The serving rows are protected for the same reason: their
allocation has to keep summing to the header's total.

The historical (`simple_history`) shadow tables are deliberately **not**
covered. They are the audit of these rows rather than the rows themselves, and
they will only ever hold one entry per snapshot because the snapshot can never
change.
"""

from django.db import migrations

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION kitchen_cost_snapshot_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '% is append-only; % is not permitted', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGERS = """
CREATE TRIGGER kitchen_recipecostsnapshot_no_update
    BEFORE UPDATE ON kitchen_recipecostsnapshot
    FOR EACH ROW EXECUTE FUNCTION kitchen_cost_snapshot_append_only();

CREATE TRIGGER kitchen_recipecostsnapshot_no_delete
    BEFORE DELETE ON kitchen_recipecostsnapshot
    FOR EACH ROW EXECUTE FUNCTION kitchen_cost_snapshot_append_only();

CREATE TRIGGER kitchen_recipecostsnapshotline_no_update
    BEFORE UPDATE ON kitchen_recipecostsnapshotline
    FOR EACH ROW EXECUTE FUNCTION kitchen_cost_snapshot_append_only();

CREATE TRIGGER kitchen_recipecostsnapshotline_no_delete
    BEFORE DELETE ON kitchen_recipecostsnapshotline
    FOR EACH ROW EXECUTE FUNCTION kitchen_cost_snapshot_append_only();

CREATE TRIGGER kitchen_recipecostsnapshotserving_no_update
    BEFORE UPDATE ON kitchen_recipecostsnapshotserving
    FOR EACH ROW EXECUTE FUNCTION kitchen_cost_snapshot_append_only();

CREATE TRIGGER kitchen_recipecostsnapshotserving_no_delete
    BEFORE DELETE ON kitchen_recipecostsnapshotserving
    FOR EACH ROW EXECUTE FUNCTION kitchen_cost_snapshot_append_only();
"""

DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS kitchen_recipecostsnapshot_no_update ON kitchen_recipecostsnapshot;
DROP TRIGGER IF EXISTS kitchen_recipecostsnapshot_no_delete ON kitchen_recipecostsnapshot;
DROP TRIGGER IF EXISTS kitchen_recipecostsnapshotline_no_update ON kitchen_recipecostsnapshotline;
DROP TRIGGER IF EXISTS kitchen_recipecostsnapshotline_no_delete ON kitchen_recipecostsnapshotline;
DROP TRIGGER IF EXISTS kitchen_recipecostsnapshotserving_no_update
    ON kitchen_recipecostsnapshotserving;
DROP TRIGGER IF EXISTS kitchen_recipecostsnapshotserving_no_delete
    ON kitchen_recipecostsnapshotserving;
DROP FUNCTION IF EXISTS kitchen_cost_snapshot_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0008_recipe_cost_snapshots"),
    ]

    operations = [
        migrations.RunSQL(
            sql=APPEND_ONLY_FUNCTION + CREATE_TRIGGERS,
            reverse_sql=DROP_TRIGGERS,
        ),
    ]
