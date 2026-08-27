"""Guard organization/branch pairs for shared automation rows in PostgreSQL."""

from django.db import migrations


FORWARD_SQL = """
CREATE OR REPLACE FUNCTION core_automation_branch_org_guard()
RETURNS trigger AS $$
BEGIN
    IF NEW.branch_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
          FROM organizations_branch AS branch
         WHERE branch.id = NEW.branch_id
           AND branch.organization_id = NEW.organization_id
    ) THEN
        RAISE EXCEPTION 'Automation branch must belong to its organization';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_outbox_branch_org_guard
BEFORE INSERT OR UPDATE OF organization_id, branch_id ON core_automationoutboxevent
FOR EACH ROW EXECUTE FUNCTION core_automation_branch_org_guard();

CREATE TRIGGER core_exception_branch_org_guard
BEFORE INSERT OR UPDATE OF organization_id, branch_id ON core_automationexception
FOR EACH ROW EXECUTE FUNCTION core_automation_branch_org_guard();

CREATE TRIGGER core_task_branch_org_guard
BEFORE INSERT OR UPDATE OF organization_id, branch_id ON core_automationtask
FOR EACH ROW EXECUTE FUNCTION core_automation_branch_org_guard();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS core_task_branch_org_guard ON core_automationtask;
DROP TRIGGER IF EXISTS core_exception_branch_org_guard ON core_automationexception;
DROP TRIGGER IF EXISTS core_outbox_branch_org_guard ON core_automationoutboxevent;
DROP FUNCTION IF EXISTS core_automation_branch_org_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0007_automation_foundation")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
