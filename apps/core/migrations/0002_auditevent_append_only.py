"""
Make the audit trail append-only in the database.

A Python-level guard would be bypassed by bulk updates, raw SQL, the admin,
a data migration, and anyone with a psql prompt — which is precisely the set
of people an audit trail exists to keep honest. The trigger refuses UPDATE and
DELETE for everyone, including the application's own ORM.

SQLSTATE 23001 (restrict_violation) is in the integrity-violation class, so
psycopg surfaces it as django.db.IntegrityError and callers handle it like any
other constraint.
"""

from django.db import migrations

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION core_auditevent_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'core_auditevent is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGERS = """
CREATE TRIGGER core_auditevent_no_update
    BEFORE UPDATE ON core_auditevent
    FOR EACH ROW EXECUTE FUNCTION core_auditevent_append_only();

CREATE TRIGGER core_auditevent_no_delete
    BEFORE DELETE ON core_auditevent
    FOR EACH ROW EXECUTE FUNCTION core_auditevent_append_only();
"""

DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS core_auditevent_no_update ON core_auditevent;
DROP TRIGGER IF EXISTS core_auditevent_no_delete ON core_auditevent;
DROP FUNCTION IF EXISTS core_auditevent_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=APPEND_ONLY_FUNCTION + CREATE_TRIGGERS,
            reverse_sql=DROP_TRIGGERS,
        ),
    ]
