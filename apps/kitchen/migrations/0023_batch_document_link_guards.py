"""
Database guards for `BatchDocumentLink`: immutability, no delete, and the cap.

Three rules, and the third is the one this file exists for.

## The attribution cap (RCP-102)

For any Inventory source line, the sum of `attributed_quantity` across every
`ACTIVE` link pointing at it may not exceed that line's own `base_quantity`.
`document_links.py` checks the same thing under `SELECT ... FOR UPDATE`, and
the service check is genuinely necessary — it is what produces a readable
Arabic refusal instead of an `IntegrityError`. It is also not sufficient on its
own, because two concurrent writers can each read a total that is fine and
write a pair that is not.

The trigger is `DEFERRABLE INITIALLY DEFERRED`, so it fires at COMMIT with both
rows visible and the second transaction loses. That is the same idiom migration
`0015` used for rescale consistency, and it is chosen for the same reason: an
IMMEDIATE trigger would refuse a legitimate multi-row service call halfway
through its own work.

Without this, one waste document could be charged in full to three different
batches, and every batch's variance report would balance against a quantity
that only one of them can honestly claim.

## Immutability

An `ACTIVE` link may move to `CANCELLED` and nothing else may change. A
`CANCELLED` link may not change at all. Correction is cancellation plus a new
link with a reason, never an edit — the standing rule for every kitchen record
that somebody may already have read.

An allowlist rather than a blocklist, because a blocklist has to be remembered
(see `accounting/0005` for what forgetting one cost).

## No delete

A link is evidence of a claim somebody made. Withdrawing the claim is
`CANCELLED` with a reason on the row; removing the row erases that there was
ever an attribution to withdraw.
"""

from django.db import migrations

LINK_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_batch_document_link_is_frozen()
RETURNS TRIGGER AS $$
DECLARE
    permitted kitchen_batchdocumentlink%ROWTYPE;
BEGIN
    permitted := OLD;

    IF OLD.status = 'CANCELLED' THEN
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'a cancelled batch document link is history and may not change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- ACTIVE: the only permitted transition, and only these columns.
    permitted.status              := NEW.status;
    permitted.cancelled_at        := NEW.cancelled_at;
    permitted.cancelled_by_id     := NEW.cancelled_by_id;
    permitted.cancellation_reason := NEW.cancellation_reason;
    permitted.updated_at          := NEW.updated_at;

    IF NEW.status NOT IN ('ACTIVE', 'CANCELLED') THEN
        RAISE EXCEPTION
            'a batch document link is ACTIVE or CANCELLED'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'a batch document link is frozen: batch, source line, item, quantity '
        'and reason may not change. Cancel it and make another'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_batch_document_link_is_frozen
    BEFORE UPDATE ON kitchen_batchdocumentlink
    FOR EACH ROW EXECUTE FUNCTION kitchen_batch_document_link_is_frozen();
"""

LINK_DELETE_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_batch_document_link_survives()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'a batch document link is cancelled with a reason, never deleted'
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_batch_document_link_survives
    BEFORE DELETE ON kitchen_batchdocumentlink
    FOR EACH ROW EXECUTE FUNCTION kitchen_batch_document_link_survives();
"""

#: Runs on the link table for INSERT and UPDATE. Deferred to COMMIT so two
#: concurrent writers are compared against each other rather than each against
#: a total that was true when it read.
ATTRIBUTION_CAP = """
CREATE OR REPLACE FUNCTION kitchen_batch_link_attribution_within_source()
RETURNS TRIGGER AS $$
DECLARE
    attributed  NUMERIC;
    available   NUMERIC;
BEGIN
    IF NEW.transfer_line_id IS NOT NULL THEN
        SELECT COALESCE(SUM(link.attributed_quantity), 0)
          INTO attributed
          FROM kitchen_batchdocumentlink AS link
         WHERE link.transfer_line_id = NEW.transfer_line_id
           AND link.status = 'ACTIVE';

        SELECT line.base_quantity
          INTO available
          FROM inventory_stocktransferline AS line
         WHERE line.id = NEW.transfer_line_id;

        IF attributed > available THEN
            RAISE EXCEPTION
                'attributed quantity % exceeds transfer line % quantity %',
                attributed, NEW.transfer_line_id, available
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    IF NEW.waste_line_id IS NOT NULL THEN
        SELECT COALESCE(SUM(link.attributed_quantity), 0)
          INTO attributed
          FROM kitchen_batchdocumentlink AS link
         WHERE link.waste_line_id = NEW.waste_line_id
           AND link.status = 'ACTIVE';

        SELECT line.base_quantity
          INTO available
          FROM inventory_inventorymovementdocumentline AS line
         WHERE line.id = NEW.waste_line_id;

        IF attributed > available THEN
            RAISE EXCEPTION
                'attributed quantity % exceeds waste line % quantity %',
                attributed, NEW.waste_line_id, available
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER kitchen_batch_link_attribution_within_source
    AFTER INSERT OR UPDATE ON kitchen_batchdocumentlink
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION kitchen_batch_link_attribution_within_source();
"""

DROP = """
DROP TRIGGER IF EXISTS kitchen_batch_link_attribution_within_source
    ON kitchen_batchdocumentlink;
DROP FUNCTION IF EXISTS kitchen_batch_link_attribution_within_source();
DROP TRIGGER IF EXISTS kitchen_batch_document_link_survives
    ON kitchen_batchdocumentlink;
DROP FUNCTION IF EXISTS kitchen_batch_document_link_survives();
DROP TRIGGER IF EXISTS kitchen_batch_document_link_is_frozen
    ON kitchen_batchdocumentlink;
DROP FUNCTION IF EXISTS kitchen_batch_document_link_is_frozen();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0022_batch_document_links"),
    ]

    operations = [
        migrations.RunSQL(sql=LINK_GUARD, reverse_sql=DROP),
        migrations.RunSQL(sql=LINK_DELETE_GUARD, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(sql=ATTRIBUTION_CAP, reverse_sql=migrations.RunSQL.noop),
    ]
