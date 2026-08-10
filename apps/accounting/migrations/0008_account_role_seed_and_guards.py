"""
Seed the system account-role vocabulary, and guard it (ADR-019).

Three things the ORM migration before this one could not do:

**The seven inventory roles exist from migration zero.** They are system
vocabulary the posting services refer to by code, so a fresh database that
lacked them would fail its first opening post. Seeded here, idempotently,
rather than by a command someone must remember to run.

**A system role's code is immutable and its row undeletable.** The code is a
technical identifier that posting rules and mapping rows both name; renaming
it would silently orphan every mapping, and deleting it would orphan history.
The trigger allows name and activation edits and refuses everything else.

**Organization mapping ranges cannot overlap.** Two active answers to "which
account carries INVENTORY_CONTROL today" is not a question to resolve at
posting time; the EXCLUDE constraint makes the state unrepresentable, exactly
as `item_conversion_no_overlapping_periods` does for conversions.
"""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations

SYSTEM_ROLE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION accounting_system_role_is_reserved()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.is_system THEN
            RAISE EXCEPTION
                'account role % is system vocabulary and cannot be deleted', OLD.code
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.is_system AND NEW.code IS DISTINCT FROM OLD.code THEN
        RAISE EXCEPTION
            'account role % is system vocabulary and cannot be renamed', OLD.code
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.is_system AND NOT NEW.is_system THEN
        RAISE EXCEPTION
            'account role % cannot stop being a system role', OLD.code
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

SYSTEM_ROLE_GUARD_TRIGGER = """
CREATE TRIGGER accounting_system_role_reserved
    BEFORE UPDATE OR DELETE ON accounting_accountrole
    FOR EACH ROW EXECUTE FUNCTION accounting_system_role_is_reserved();
"""

MAPPING_OVERLAP = """
ALTER TABLE accounting_organizationaccountmapping
    ADD CONSTRAINT org_account_mapping_no_overlapping_periods
    EXCLUDE USING gist (
        organization_id WITH =,
        account_role_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    WHERE (is_active);
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS accounting_system_role_reserved ON accounting_accountrole;
DROP FUNCTION IF EXISTS accounting_system_role_is_reserved();
ALTER TABLE accounting_organizationaccountmapping
    DROP CONSTRAINT IF EXISTS org_account_mapping_no_overlapping_periods;
"""


def seed_inventory_roles(apps, schema_editor):
    """
    Idempotent: an existing code is updated in place, never duplicated.

    The literals are duplicated from `SYSTEM_INVENTORY_ROLES` in
    apps/accounting/models.py deliberately — a migration must not import
    application code, and the fresh-database test asserts the two agree.
    """
    AccountRole = apps.get_model("accounting", "AccountRole")
    roles = (
        ("INVENTORY_CONTROL", "مخزون - حساب المراقبة", "Inventory control", "ITEM"),
        (
            "INVENTORY_OPENING_EQUITY",
            "أرصدة افتتاحية - مخزون",
            "Inventory opening equity",
            "ORGANIZATION",
        ),
        ("INVENTORY_COUNT_VARIANCE", "فروقات الجرد", "Inventory count variance", "ORGANIZATION"),
        ("INVENTORY_WASTE_EXPENSE", "هالك المخزون", "Inventory waste expense", "ORGANIZATION"),
        ("INVENTORY_IN_TRANSIT", "بضاعة بالطريق", "Inventory in transit", "ORGANIZATION"),
        ("INVENTORY_SHORTAGE_LOSS", "عجز التحويلات", "Inventory shortage loss", "ORGANIZATION"),
        ("INVENTORY_ADJUSTMENT", "تسويات المخزون", "Inventory adjustment", "ORGANIZATION"),
    )
    for code, name_ar, name_en, mapping_scope in roles:
        AccountRole.objects.update_or_create(
            code=code,
            defaults={
                "name_ar": name_ar,
                "name_en": name_en,
                "domain": "INVENTORY",
                "mapping_scope": mapping_scope,
                "is_system": True,
                "is_active": True,
            },
        )


def unseed_inventory_roles(apps, schema_editor):  # pragma: no cover - rollback path
    AccountRole = apps.get_model("accounting", "AccountRole")
    AccountRole.objects.filter(domain="INVENTORY", is_system=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0007_accountrole_historicalaccountrole_and_more"),
    ]

    operations = [
        # Needed here as well as in inventory/0002: on a fresh database this
        # migration can run before inventory's, and the EXCLUDE constraint
        # compares plain integer columns.
        BtreeGistExtension(),
        # The roles are seeded BEFORE the guard trigger exists, so the seed
        # itself is not refused on a re-run that updates a system row's names.
        migrations.RunPython(seed_inventory_roles, unseed_inventory_roles),
        migrations.RunSQL(
            sql=(SYSTEM_ROLE_GUARD_FUNCTION + SYSTEM_ROLE_GUARD_TRIGGER + MAPPING_OVERLAP),
            reverse_sql=DROP_ALL,
        ),
    ]
