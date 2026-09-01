"""
Make the insight history unrewritable, in the database.

Four tables carry claims that a person may act on months later — the identity
of a case, what a run observed about it, what somebody decided, and what a
detector reported. If any of those can be UPDATEd, then "this was HIGH in
August" is a statement about the current row rather than about August, and the
whole point of separating identity from observation is lost.

Same shape as `core/0002_auditevent_append_only`, deliberately: one plpgsql
function per concern, `TG_TABLE_NAME` in the message so one function can serve
several tables, and a `reverse_sql` that drops triggers before the function.

## `InsightRun` is the exception, and it is an allowlist

A run is created when it starts and stamped when it finishes, so it cannot be
strictly append-only. Its trigger permits `finished_at` and `updated_at` to
change and refuses everything else — an **allowlist**, per `CLAUDE.md`, because
a blocklist has to be remembered and `accounting/0005` records what forgetting
one column cost.

`DetectorSetting` is append-only too. A threshold is versioned rather than
edited so an observation can name the version it was judged under; letting a
row be edited would silently restate every past finding.
"""

from django.db import migrations

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION insights_history_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '% is append-only; % is not permitted', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

RUN_STAMP_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION insights_run_stamp_only()
RETURNS TRIGGER AS $$
DECLARE
    stampable text[] := ARRAY['finished_at', 'updated_at'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'insights_insightrun is append-only; DELETE is not permitted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (to_jsonb(NEW) - stampable) <> (to_jsonb(OLD) - stampable) THEN
        RAISE EXCEPTION
            'insights_insightrun may only record its completion; no other column may change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGERS = """
CREATE TRIGGER insights_insight_no_update
    BEFORE UPDATE ON insights_insight
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();
CREATE TRIGGER insights_insight_no_delete
    BEFORE DELETE ON insights_insight
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();

CREATE TRIGGER insights_observation_no_update
    BEFORE UPDATE ON insights_insightobservation
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();
CREATE TRIGGER insights_observation_no_delete
    BEFORE DELETE ON insights_insightobservation
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();

CREATE TRIGGER insights_event_no_update
    BEFORE UPDATE ON insights_insightevent
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();
CREATE TRIGGER insights_event_no_delete
    BEFORE DELETE ON insights_insightevent
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();

CREATE TRIGGER insights_outcome_no_update
    BEFORE UPDATE ON insights_insightdetectoroutcome
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();
CREATE TRIGGER insights_outcome_no_delete
    BEFORE DELETE ON insights_insightdetectoroutcome
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();

CREATE TRIGGER insights_setting_no_update
    BEFORE UPDATE ON insights_detectorsetting
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();
CREATE TRIGGER insights_setting_no_delete
    BEFORE DELETE ON insights_detectorsetting
    FOR EACH ROW EXECUTE FUNCTION insights_history_append_only();

CREATE TRIGGER insights_run_stamp_only
    BEFORE UPDATE OR DELETE ON insights_insightrun
    FOR EACH ROW EXECUTE FUNCTION insights_run_stamp_only();
"""

DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS insights_insight_no_update ON insights_insight;
DROP TRIGGER IF EXISTS insights_insight_no_delete ON insights_insight;
DROP TRIGGER IF EXISTS insights_observation_no_update ON insights_insightobservation;
DROP TRIGGER IF EXISTS insights_observation_no_delete ON insights_insightobservation;
DROP TRIGGER IF EXISTS insights_event_no_update ON insights_insightevent;
DROP TRIGGER IF EXISTS insights_event_no_delete ON insights_insightevent;
DROP TRIGGER IF EXISTS insights_outcome_no_update ON insights_insightdetectoroutcome;
DROP TRIGGER IF EXISTS insights_outcome_no_delete ON insights_insightdetectoroutcome;
DROP TRIGGER IF EXISTS insights_setting_no_update ON insights_detectorsetting;
DROP TRIGGER IF EXISTS insights_setting_no_delete ON insights_detectorsetting;
DROP TRIGGER IF EXISTS insights_run_stamp_only ON insights_insightrun;
DROP FUNCTION IF EXISTS insights_history_append_only();
DROP FUNCTION IF EXISTS insights_run_stamp_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("insights", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=APPEND_ONLY_FUNCTION + RUN_STAMP_ONLY_FUNCTION + CREATE_TRIGGERS,
            reverse_sql=DROP_TRIGGERS,
        ),
    ]
