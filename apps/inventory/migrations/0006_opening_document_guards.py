"""
Database guards for the account-mapping overrides and the opening document.

**Override ranges cannot overlap.** Same rule as the organization defaults,
with one twist: `item_id` and `category_id` are nullable, and in an EXCLUDE
constraint a NULL comparison never conflicts — so two category mappings would
sail past a constraint over the raw columns. `COALESCE(..., 0)` pins the
absent side to a value, which is safe because 0 is not a real primary key.

**A posted opening document is immutable, as a whole row.** The one permitted
change is the reversal transition, expressed as an allowlist over the columns
that transition writes — per the `accounting/0005` lesson, everything not
explicitly allowed is refused, so a column added later is protected before
anyone remembers to protect it. A reversed document is immutable outright,
and only a DRAFT may be deleted.

**Lines follow their document.** Once the document is POSTED or REVERSED its
lines refuse insert, update, and delete. During the posting transaction the
document is still SUBMITTED, which is what lets posting write the resolved
account references onto the lines it is about to freeze.
"""

from django.db import migrations

MAPPING_OVERLAP = """
ALTER TABLE inventory_inventoryaccountmapping
    ADD CONSTRAINT inventory_mapping_no_overlapping_periods
    EXCLUDE USING gist (
        organization_id WITH =,
        account_role_id WITH =,
        (COALESCE(item_id, 0)) WITH =,
        (COALESCE(category_id, 0)) WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    WHERE (is_active);
"""

DOCUMENT_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION inventory_opening_document_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    allowed text[] := ARRAY[
        'status', 'reversed_by_id', 'reversed_at', 'reversal_reason',
        'reversal_journal_entry_id', 'updated_at'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'opening document % has left draft and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'REVERSED' THEN
        RAISE EXCEPTION
            'opening document % is reversed and immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'POSTED' THEN
        IF NEW.status <> 'REVERSED'
           OR (to_jsonb(NEW) - allowed) <> (to_jsonb(OLD) - allowed) THEN
            RAISE EXCEPTION
                'opening document % is posted; the only permitted change is its reversal', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

DOCUMENT_GUARD_TRIGGER = """
CREATE TRIGGER inventory_opening_document_immutable
    BEFORE UPDATE OR DELETE ON inventory_openingstockdocument
    FOR EACH ROW EXECUTE FUNCTION inventory_opening_document_is_immutable();
"""

LINE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION inventory_opening_line_follows_document()
RETURNS TRIGGER AS $$
DECLARE
    doc_status text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.document_id IS DISTINCT FROM OLD.document_id THEN
        RAISE EXCEPTION
            'opening line % cannot move to another document', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT status INTO doc_status
            FROM inventory_openingstockdocument WHERE id = OLD.document_id;
    ELSE
        SELECT status INTO doc_status
            FROM inventory_openingstockdocument WHERE id = NEW.document_id;
    END IF;

    IF doc_status IN ('POSTED', 'REVERSED') THEN
        RAISE EXCEPTION
            'opening document is % and its lines are frozen', doc_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

LINE_GUARD_TRIGGER = """
CREATE TRIGGER inventory_opening_line_frozen_with_document
    BEFORE INSERT OR UPDATE OR DELETE ON inventory_openingstockline
    FOR EACH ROW EXECUTE FUNCTION inventory_opening_line_follows_document();
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS inventory_opening_document_immutable
    ON inventory_openingstockdocument;
DROP TRIGGER IF EXISTS inventory_opening_line_frozen_with_document
    ON inventory_openingstockline;
DROP FUNCTION IF EXISTS inventory_opening_document_is_immutable();
DROP FUNCTION IF EXISTS inventory_opening_line_follows_document();
ALTER TABLE inventory_inventoryaccountmapping
    DROP CONSTRAINT IF EXISTS inventory_mapping_no_overlapping_periods;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0005_alter_inventoryitem_options_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                MAPPING_OVERLAP
                + DOCUMENT_GUARD_FUNCTION
                + DOCUMENT_GUARD_TRIGGER
                + LINE_GUARD_FUNCTION
                + LINE_GUARD_TRIGGER
            ),
            reverse_sql=DROP_ALL,
        ),
    ]
