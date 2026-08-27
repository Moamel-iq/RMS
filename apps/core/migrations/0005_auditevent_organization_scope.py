"""Give every auditable fact an explicit organization boundary.

The audit table is append-only at database level.  This one migration is the
only controlled exception: it adds a new nullable scope column, fills it from
the already immutable branch relation, then immediately reenables the trigger
inside the same transaction.  No historic fact is otherwise changed.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_audit_action_cancelled"),
        ("organizations", "0006_organization_security_permissions"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditevent",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="audit_events",
                to="organizations.organization",
                verbose_name="organization",
            ),
        ),
        # The trigger deliberately rejects ALL UPDATEs.  Backfilling a brand
        # new scope column is safe only while the exact statement below runs.
        migrations.RunSQL(
            sql="ALTER TABLE core_auditevent DISABLE TRIGGER core_auditevent_no_update;",
            reverse_sql="ALTER TABLE core_auditevent ENABLE TRIGGER core_auditevent_no_update;",
        ),
        migrations.RunSQL(
            sql="""
                UPDATE core_auditevent AS event
                   SET organization_id = branch.organization_id
                  FROM organizations_branch AS branch
                 WHERE event.branch_id = branch.id
                   AND event.organization_id IS NULL;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="ALTER TABLE core_auditevent ENABLE TRIGGER core_auditevent_no_update;",
            reverse_sql="ALTER TABLE core_auditevent DISABLE TRIGGER core_auditevent_no_update;",
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["organization", "-occurred_at"], name="audit_org_time_idx"),
        ),
    ]
