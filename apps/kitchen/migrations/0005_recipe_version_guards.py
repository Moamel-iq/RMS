"""
What the ORM cannot express about an approved recipe version.

Five guarantees, and the module refuses to expose approval until every one of
them is in place — an approval screen over rows a plain `UPDATE` can still
rewrite is worse than no approval screen, because it looks like a control.

**No two claims on one (recipe, branch, date).** `EXCLUDE USING gist` over
`(recipe, branch, daterange(effective_from, effective_to, '[]'))`, the same
mechanism `ItemPackageConversion` has used since Task 1.0 (invariant 7, now
invariant 5). Two versions both covering Tuesday at Al-Bunook would make every
historical read ambiguous, and the resolver would have to guess. `DEFERRABLE
INITIALLY IMMEDIATE`: it behaves exactly as an immediate constraint in normal
use, and a test can defer it inside a transaction it then rolls back, which is
the only honest way to prove the resolver's ambiguity branch and the verifier's
ambiguity finding actually work.

**A frozen version is frozen as a whole row.** The allowlist idiom migration
`accounting/0005` established, applied to five permitted transitions: each one
builds the row it expects to see and compares whole rows, so a column added to
this table next year is protected before anybody remembers to protect it. A
blocklist has to be remembered; an allowlist cannot be forgotten.

**Every owned child row follows its version.** Lines, substitutes, steps, step
links and servings may be inserted, updated or deleted only while the version
that owns them is `DRAFT`, and none may be moved to another version. Approval
that froze the header and left the lines editable would be theatre.

**A scope row cannot drift from its version.** The exclusion constraint can
only see columns on its own table, so `recipe` and the effective range are
denormalised onto the scope row — and held equal to the version's by trigger,
inserted only into an `ACTIVE` version, and thereafter movable only by the
supersession that has already closed the version itself.

**Demo evidence stays visibly demo.** `DEMO_FICTIONAL` approval evidence is
permitted only for the `DEMO-` namespace, and a real recipe may not carry it —
nor a demo recipe carry `SIGNED_FORM`. A demo signoff that looked like a signed
`KM-RCP-004` is precisely how unapproved figures acquire authority (RCP-126).
"""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations

# ---------------------------------------------------------------------------
# Overlap
# ---------------------------------------------------------------------------

SCOPE_OVERLAP = """
ALTER TABLE kitchen_recipeversionbranchscope
    ADD CONSTRAINT recipe_scope_no_overlapping_ranges
    EXCLUDE USING gist (
        recipe_id WITH =,
        branch_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
    DEFERRABLE INITIALLY IMMEDIATE;
"""

# ---------------------------------------------------------------------------
# Shared lookups
# ---------------------------------------------------------------------------

HELPERS = """
CREATE OR REPLACE FUNCTION kitchen_version_status(version bigint)
RETURNS text AS $$
    SELECT status FROM kitchen_recipeversion WHERE id = version;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION kitchen_version_of_line(line bigint)
RETURNS bigint AS $$
    SELECT version_id FROM kitchen_recipeline WHERE id = line;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION kitchen_version_of_step(step bigint)
RETURNS bigint AS $$
    SELECT version_id FROM kitchen_recipestep WHERE id = step;
$$ LANGUAGE sql STABLE;

-- A null status means the version row is already gone, which happens exactly
-- once: Django deletes a discarded draft's children before the draft itself.
-- The foreign key is what refuses a genuinely orphaned row.
CREATE OR REPLACE FUNCTION kitchen_require_draft_version(version bigint, label text)
RETURNS void AS $$
DECLARE
    current_status text;
BEGIN
    IF version IS NULL THEN
        RETURN;
    END IF;
    current_status := kitchen_version_status(version);
    IF current_status IS NULL OR current_status = 'DRAFT' THEN
        RETURN;
    END IF;
    RAISE EXCEPTION
        '% belongs to a % recipe version and is frozen', label, current_status
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

# ---------------------------------------------------------------------------
# The version header
# ---------------------------------------------------------------------------

VERSION_IMMUTABILITY = """
CREATE OR REPLACE FUNCTION kitchen_recipe_version_is_immutable()
RETURNS TRIGGER AS $$
DECLARE
    permitted kitchen_recipeversion%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'DRAFT' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'recipe version % is % and cannot be deleted', OLD.id, OLD.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- A draft is still being written, and everything about it may change.
    IF OLD.status = 'DRAFT' AND NEW.status = 'DRAFT' THEN
        RETURN NEW;
    END IF;

    -- Reported separately: "you moved the version's identity" and "you edited
    -- a frozen version" send a developer to two different places.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.recipe_id IS DISTINCT FROM OLD.recipe_id
       OR NEW.version_number IS DISTINCT FROM OLD.version_number
       OR NEW.public_id IS DISTINCT FROM OLD.public_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id THEN
        RAISE EXCEPTION
            'the identity of recipe version % is immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Transition one: DRAFT -> SUBMITTED. The submitter and the moment move,
    -- and nothing else does.
    permitted := OLD;
    permitted.updated_at := NEW.updated_at;
    permitted.status := NEW.status;
    permitted.submitted_by_id := NEW.submitted_by_id;
    permitted.submitted_at := NEW.submitted_at;
    IF OLD.status = 'DRAFT'
       AND NEW.status = 'SUBMITTED'
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    -- Transition two: SUBMITTED -> APPROVED, carrying its evidence.
    permitted := OLD;
    permitted.updated_at := NEW.updated_at;
    permitted.status := NEW.status;
    permitted.approved_by_id := NEW.approved_by_id;
    permitted.approved_at := NEW.approved_at;
    permitted.approval_reference := NEW.approval_reference;
    permitted.approval_evidence_kind := NEW.approval_evidence_kind;
    IF OLD.status = 'SUBMITTED'
       AND NEW.status = 'APPROVED'
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    -- Transition three: SUBMITTED -> REJECTED, carrying its reason.
    permitted := OLD;
    permitted.updated_at := NEW.updated_at;
    permitted.status := NEW.status;
    permitted.rejected_by_id := NEW.rejected_by_id;
    permitted.rejected_at := NEW.rejected_at;
    permitted.rejection_reason := NEW.rejection_reason;
    IF OLD.status = 'SUBMITTED'
       AND NEW.status = 'REJECTED'
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    -- Transition four: APPROVED -> ACTIVE, which is where the effective range
    -- is claimed. Approval is agreement; this is the claim on a date.
    permitted := OLD;
    permitted.updated_at := NEW.updated_at;
    permitted.status := NEW.status;
    permitted.activated_by_id := NEW.activated_by_id;
    permitted.activated_at := NEW.activated_at;
    permitted.effective_from := NEW.effective_from;
    permitted.effective_to := NEW.effective_to;
    IF OLD.status = 'APPROVED'
       AND NEW.status = 'ACTIVE'
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    -- Transition five: ACTIVE -> SUPERSEDED. The range closes at the day
    -- before the replacement begins, and never re-opens.
    permitted := OLD;
    permitted.updated_at := NEW.updated_at;
    permitted.status := NEW.status;
    permitted.superseded_at := NEW.superseded_at;
    permitted.superseded_by_version_id := NEW.superseded_by_version_id;
    permitted.effective_to := NEW.effective_to;
    IF OLD.status = 'ACTIVE'
       AND NEW.status = 'SUPERSEDED'
       AND NEW.effective_to IS NOT NULL
       AND (OLD.effective_to IS NULL OR NEW.effective_to <= OLD.effective_to)
       AND NEW IS NOT DISTINCT FROM permitted THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'recipe version % is % and is immutable', OLD.id, OLD.status
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_recipe_version_is_immutable
    BEFORE UPDATE OR DELETE ON kitchen_recipeversion
    FOR EACH ROW EXECUTE FUNCTION kitchen_recipe_version_is_immutable();
"""

VERSION_EVIDENCE_NAMESPACE = """
CREATE OR REPLACE FUNCTION kitchen_version_evidence_matches_namespace()
RETURNS TRIGGER AS $$
DECLARE
    recipe_code text;
    is_demo boolean;
BEGIN
    IF NEW.approval_evidence_kind = '' OR NEW.approval_evidence_kind IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT code INTO recipe_code FROM kitchen_recipe WHERE id = NEW.recipe_id;
    is_demo := recipe_code LIKE 'DEMO-%';

    IF NEW.approval_evidence_kind = 'DEMO_FICTIONAL' AND NOT is_demo THEN
        RAISE EXCEPTION
            'recipe % is not in the demo namespace and cannot be approved on '
            'fictional evidence', recipe_code
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approval_evidence_kind = 'SIGNED_FORM' AND is_demo THEN
        RAISE EXCEPTION
            'demo recipe % cannot claim a signed approval form', recipe_code
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_version_evidence_matches_namespace
    BEFORE INSERT OR UPDATE ON kitchen_recipeversion
    FOR EACH ROW EXECUTE FUNCTION kitchen_version_evidence_matches_namespace();
"""

# ---------------------------------------------------------------------------
# Owned child rows
# ---------------------------------------------------------------------------


def _child_guard(function: str, table: str, version_expression: str, label: str) -> str:
    """
    One table's "may only change while the version is a draft" trigger.

    `version_expression` reaches the owning version from the row: directly for
    the tables that carry `version_id`, and through the parent for the two that
    do not.
    """
    return f"""
CREATE OR REPLACE FUNCTION {function}()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM kitchen_require_draft_version({version_expression.format(row="OLD")}, '{label}');
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        PERFORM kitchen_require_draft_version({version_expression.format(row="OLD")}, '{label}');
        IF {version_expression.format(row="NEW")}
           IS DISTINCT FROM {version_expression.format(row="OLD")} THEN
            RAISE EXCEPTION
                '{label} cannot be moved to another recipe version'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    PERFORM kitchen_require_draft_version({version_expression.format(row="NEW")}, '{label}');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER {function}
    BEFORE INSERT OR UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION {function}();
"""


CHILD_GUARDS = "".join(
    [
        _child_guard(
            "kitchen_recipe_line_follows_its_version",
            "kitchen_recipeline",
            "{row}.version_id",
            "a recipe line",
        ),
        _child_guard(
            "kitchen_recipe_substitute_follows_its_version",
            "kitchen_recipelinesubstitute",
            "kitchen_version_of_line({row}.line_id)",
            "a recipe line substitute",
        ),
        _child_guard(
            "kitchen_recipe_step_follows_its_version",
            "kitchen_recipestep",
            "{row}.version_id",
            "a recipe step",
        ),
        _child_guard(
            "kitchen_recipe_step_link_follows_its_version",
            "kitchen_recipestepingredient",
            "kitchen_version_of_step({row}.step_id)",
            "a recipe step ingredient link",
        ),
        _child_guard(
            "kitchen_recipe_serving_follows_its_version",
            "kitchen_recipeserving",
            "{row}.version_id",
            "a recipe serving",
        ),
    ]
)

# ---------------------------------------------------------------------------
# Effective branch scope
# ---------------------------------------------------------------------------

SCOPE_INTEGRITY = """
CREATE OR REPLACE FUNCTION kitchen_scope_follows_its_version()
RETURNS TRIGGER AS $$
DECLARE
    owner kitchen_recipeversion%ROWTYPE;
    permitted kitchen_recipeversionbranchscope%ROWTYPE;
    branch_organization bigint;
    recipe_organization bigint;
    applicable_branches bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'an effective scope row is closed, never deleted: it is how a '
            'superseded version still resolves for its own dates'
            USING ERRCODE = 'restrict_violation';
    END IF;

    SELECT * INTO owner FROM kitchen_recipeversion WHERE id = NEW.version_id;

    IF TG_OP = 'UPDATE' THEN
        -- The only permitted movement, and only after the version itself has
        -- already been superseded. Closing a live version's scope without
        -- superseding it would end a recipe with no record of why.
        IF owner.status <> 'SUPERSEDED' THEN
            RAISE EXCEPTION
                'the effective scope of a % version is immutable', owner.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        permitted := OLD;
        permitted.updated_at := NEW.updated_at;
        permitted.effective_to := NEW.effective_to;
        IF NEW.effective_to IS DISTINCT FROM owner.effective_to THEN
            RAISE EXCEPTION
                'an effective scope row must close on the same day as its version'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW IS NOT DISTINCT FROM permitted THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'an effective scope row is immutable except for its close date'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- INSERT. Scope is claimed at activation and at no other moment.
    IF owner.status <> 'ACTIVE' THEN
        RAISE EXCEPTION
            'recipe version % is % and claims no effective scope', owner.id, owner.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.recipe_id IS DISTINCT FROM owner.recipe_id THEN
        RAISE EXCEPTION 'an effective scope row must name its version''s own recipe'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.effective_from IS DISTINCT FROM owner.effective_from
       OR NEW.effective_to IS DISTINCT FROM owner.effective_to THEN
        RAISE EXCEPTION
            'an effective scope row inherits its version''s range and cannot differ'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT organization_id INTO branch_organization
    FROM organizations_branch WHERE id = NEW.branch_id;
    SELECT organization_id INTO recipe_organization
    FROM kitchen_recipe WHERE id = NEW.recipe_id;
    IF branch_organization IS DISTINCT FROM recipe_organization THEN
        RAISE EXCEPTION 'branch % belongs to another organization', NEW.branch_id
            USING ERRCODE = 'check_violation';
    END IF;

    -- A recipe that names its branches applies at those and no others. A
    -- recipe that names none applies organization-wide, which is Task 3.1's
    -- rule and stays true here.
    SELECT count(*) INTO applicable_branches
    FROM kitchen_recipebranch WHERE recipe_id = NEW.recipe_id;
    IF applicable_branches > 0 AND NOT EXISTS (
        SELECT 1 FROM kitchen_recipebranch
        WHERE recipe_id = NEW.recipe_id AND branch_id = NEW.branch_id
    ) THEN
        RAISE EXCEPTION
            'recipe % does not apply at branch %', NEW.recipe_id, NEW.branch_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_scope_follows_its_version
    BEFORE INSERT OR UPDATE OR DELETE ON kitchen_recipeversionbranchscope
    FOR EACH ROW EXECUTE FUNCTION kitchen_scope_follows_its_version();
"""

# ---------------------------------------------------------------------------
# Review evidence
# ---------------------------------------------------------------------------

REVIEW_GUARD = """
CREATE OR REPLACE FUNCTION kitchen_review_is_append_only()
RETURNS TRIGGER AS $$
DECLARE
    owner_status text;
    recipe_code text;
    is_demo boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a recipe version review is evidence and cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'a recipe version review is a signature and cannot be edited; a '
            'reviewer who changes their mind rejects the version'
            USING ERRCODE = 'restrict_violation';
    END IF;

    owner_status := kitchen_version_status(NEW.version_id);
    IF owner_status <> 'SUBMITTED' THEN
        RAISE EXCEPTION
            'recipe version % is % and is not under review', NEW.version_id, owner_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.evidence_kind <> '' THEN
        SELECT r.code INTO recipe_code
        FROM kitchen_recipe r
        JOIN kitchen_recipeversion v ON v.recipe_id = r.id
        WHERE v.id = NEW.version_id;
        is_demo := recipe_code LIKE 'DEMO-%';
        IF NEW.evidence_kind = 'DEMO_FICTIONAL' AND NOT is_demo THEN
            RAISE EXCEPTION
                'recipe % is not in the demo namespace and cannot be reviewed on '
                'fictional evidence', recipe_code
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.evidence_kind = 'SIGNED_FORM' AND is_demo THEN
            RAISE EXCEPTION
                'demo recipe % cannot claim a signed approval form', recipe_code
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER kitchen_review_is_append_only
    BEFORE INSERT OR UPDATE OR DELETE ON kitchen_recipeversionreview
    FOR EACH ROW EXECUTE FUNCTION kitchen_review_is_append_only();
"""

DROP_ALL = """
DROP TRIGGER IF EXISTS kitchen_review_is_append_only ON kitchen_recipeversionreview;
DROP TRIGGER IF EXISTS kitchen_scope_follows_its_version ON kitchen_recipeversionbranchscope;
DROP TRIGGER IF EXISTS kitchen_recipe_serving_follows_its_version ON kitchen_recipeserving;
DROP TRIGGER IF EXISTS kitchen_recipe_step_link_follows_its_version
    ON kitchen_recipestepingredient;
DROP TRIGGER IF EXISTS kitchen_recipe_step_follows_its_version ON kitchen_recipestep;
DROP TRIGGER IF EXISTS kitchen_recipe_substitute_follows_its_version
    ON kitchen_recipelinesubstitute;
DROP TRIGGER IF EXISTS kitchen_recipe_line_follows_its_version ON kitchen_recipeline;
DROP TRIGGER IF EXISTS kitchen_version_evidence_matches_namespace ON kitchen_recipeversion;
DROP TRIGGER IF EXISTS kitchen_recipe_version_is_immutable ON kitchen_recipeversion;

DROP FUNCTION IF EXISTS kitchen_review_is_append_only();
DROP FUNCTION IF EXISTS kitchen_scope_follows_its_version();
DROP FUNCTION IF EXISTS kitchen_recipe_serving_follows_its_version();
DROP FUNCTION IF EXISTS kitchen_recipe_step_link_follows_its_version();
DROP FUNCTION IF EXISTS kitchen_recipe_step_follows_its_version();
DROP FUNCTION IF EXISTS kitchen_recipe_substitute_follows_its_version();
DROP FUNCTION IF EXISTS kitchen_recipe_line_follows_its_version();
DROP FUNCTION IF EXISTS kitchen_version_evidence_matches_namespace();
DROP FUNCTION IF EXISTS kitchen_recipe_version_is_immutable();
DROP FUNCTION IF EXISTS kitchen_require_draft_version(bigint, text);
DROP FUNCTION IF EXISTS kitchen_version_of_step(bigint);
DROP FUNCTION IF EXISTS kitchen_version_of_line(bigint);
DROP FUNCTION IF EXISTS kitchen_version_status(bigint);

ALTER TABLE kitchen_recipeversionbranchscope
    DROP CONSTRAINT IF EXISTS recipe_scope_no_overlapping_ranges;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("kitchen", "0004_recipe_version_lifecycle"),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.RunSQL(
            sql=(
                SCOPE_OVERLAP
                + HELPERS
                + VERSION_IMMUTABILITY
                + VERSION_EVIDENCE_NAMESPACE
                + CHILD_GUARDS
                + SCOPE_INTEGRITY
                + REVIEW_GUARD
            ),
            reverse_sql=DROP_ALL,
        ),
    ]
