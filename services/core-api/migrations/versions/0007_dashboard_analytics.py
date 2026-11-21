"""Mastery outbox and delivery streams, model provenance, study events, analytics releases (M8).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-21

Added (docs/ai-mastery-integration.md, docs/study-events.md, docs/analytics-privacy.md,
docs/database-migrations.md). 25 tables before, 30 after:

- attempts: UNIQUE (id, user_id, course_id, topic_id, stats_attempts), the target of the
  outbox's composite key; a partial UNIQUE index (user_id, topic_id, stats_attempts) WHERE
  quiz_item_id IS NOT NULL (the per-topic sequence of graded attempts cannot repeat); a
  partial index (course_id, submitted_at) WHERE quiz_item_id IS NOT NULL (weekly analytics).
- mastery_streams: one row per (student, course) that has graded attempts. Delivery state
  of that learner's events: ready | delivering (claim token, lease) | paused (access
  revoked) | blocked (terminal until an operator acts). At most ONE active batch: its
  immutable identity (batch_id, also the request id), the state epoch it was built for,
  its size and how often it was claimed.
- mastery_outbox: one row per graded M7 attempt, keyed by the attempt id (the event id).
  A composite key ties (event_id, user_id, course_id, topic_id, topic_seq) to the attempt's
  own (id, user_id, course_id, topic_id, stats_attempts); UNIQUE (user_id, topic_id,
  topic_seq) is the ordering key. pending -> batched (batch_id fixed) -> delivered
  (applied | duplicate) | failed (batch blocked) -> batched (operator retry).
- mastery_model_state: one row; the outcome of the last capability probe against the
  pinned protocol, model, version, state epoch and pseudonym key.
- mastery_scores: course_id (backfilled from the topic, NOT NULL), a composite topic key,
  provenance (protocol, model name/version, state epoch, pseudonym key id, through_seq,
  attempts_seen, batch id, received_at). next_review_at becomes timestamptz (existing
  values are read as UTC). A CHECK accepts a legacy row (no provenance at all; kept, never
  shown as model output) or a complete M8 prediction with mastery in [0, 1] (NaN and
  infinities fail that comparison).
- study_events: client-reported, allowlisted study telemetry (UNIQUE (user_id,
  client_event_id), request fingerprint, composite topic and document keys). Never
  evidence for grading, mastery, streaks or analytics.
- documents: UNIQUE (id, course_id), the target of study_events' document key.
- analytics_weekly_releases: one immutable, already privacy-filtered release per course and
  completed UTC ISO week.

Triggers (PL/pgSQL; outside the reflected-schema comparison):
- attempts_mastery_sequence_guard (AFTER INSERT of a graded attempt, so the row's CHECKs and
  M7's attempts_guard are reported first):
  the attempt must carry exactly the counters of its (user, topic) statistics row, locked
  here (the M7 grading path already holds that lock), and continue the previous graded
  attempt of the topic (stats_attempts - 1, stats_correct + correctness). A graded attempt
  can therefore not start, skip or fork a sequence. Legacy attempts are not affected.
- attempts_outbox_enqueue (AFTER INSERT of a graded attempt): creates the learner's stream
  row if needed and the outbox row, in the attempt's own transaction. No graded attempt can
  commit without its event; a rollback removes both.
- mastery_outbox_guard (BEFORE UPDATE): event identity and batch membership are immutable,
  delivered is final, only the documented status transitions are possible. DELETE stays
  possible for administrators (M8 never deletes outbox rows).
- analytics_weekly_releases_guard (BEFORE UPDATE): a published release is never changed.

Data safety:
- upgrade locks topic_practice_stats and attempts (M7's lock order) for the whole
  migration, so no graded attempt can commit in between; it refuses before any change when
  a graded attempt sequence repeats or has a gap, when cumulative correctness disagrees
  with the attempts, or when topic_practice_stats disagrees with the attempts. It then
  backfills one outbox row (origin 'backfill') per existing graded attempt and checks the
  count. It never changes an existing value except mastery_scores.course_id (new column)
  and the type of next_review_at.
- downgrade refuses, before any change, while study events, analytics releases, model
  predictions, or delivery state (non-pending outbox rows, streams that are not plain
  ready) exist. Pending outbox rows are regenerated from the attempts by a new upgrade.
Both run as SQL, so they also apply to offline (`--sql`) output.

Self-contained on purpose: it does not import app.models.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ERROR_CODE = "^[a-z][a-z0-9_]{0,39}$"

STREAM_STATE_CHECK = "state IN ('ready', 'delivering', 'paused', 'blocked')"
STREAM_BATCH_CHECK = (
    "((batch_id IS NULL AND batch_epoch IS NULL AND batch_size IS NULL AND batch_created_at IS NULL"
    " AND batch_claims = 0)"
    " OR (batch_id IS NOT NULL AND batch_epoch IS NOT NULL AND batch_size BETWEEN 1 AND 500"
    " AND batch_created_at IS NOT NULL AND batch_claims >= 0)) IS TRUE"
)
STREAM_LEASE_CHECK = (
    "((state = 'delivering' AND batch_id IS NOT NULL AND claim_token IS NOT NULL AND claimed_at IS NOT NULL"
    " AND lease_expires_at IS NOT NULL AND lease_expires_at > claimed_at)"
    " OR (state <> 'delivering' AND claim_token IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL)) IS TRUE"
)
STREAM_ERROR_CHECK = (
    f"(last_error_code IS NULL OR last_error_code ~ '{ERROR_CODE}') AND (state <> 'blocked' OR last_error_code IS NOT NULL)"
)
STREAM_COUNTERS_CHECK = (
    "dispatch_count >= 0 AND claim_count >= 0 AND failure_count >= 0 AND operator_retry_count >= 0"
)

OUTBOX_STATUS_CHECK = "status IN ('pending', 'batched', 'delivered', 'failed')"
OUTBOX_LIFECYCLE_CHECK = (
    "((status = 'pending' AND batch_id IS NULL AND batched_at IS NULL AND delivered_at IS NULL AND outcome IS NULL)"
    " OR (status IN ('batched', 'failed') AND batch_id IS NOT NULL AND batched_at IS NOT NULL"
    " AND delivered_at IS NULL AND outcome IS NULL)"
    " OR (status = 'delivered' AND batch_id IS NOT NULL AND batched_at IS NOT NULL AND delivered_at IS NOT NULL"
    " AND delivered_at >= batched_at AND outcome IN ('applied', 'duplicate'))) IS TRUE"
)

MODEL_STATE_CHECK = (
    "((status = 'unknown')"
    " OR (status = 'compatible' AND protocol IS NOT NULL AND model_name IS NOT NULL AND model_version IS NOT NULL"
    " AND state_epoch IS NOT NULL AND pseudonym_key_id IS NOT NULL AND probed_at IS NOT NULL AND reason IS NULL)"
    f" OR (status = 'incompatible' AND reason IS NOT NULL AND reason ~ '{ERROR_CODE}' AND probed_at IS NOT NULL)) IS TRUE"
)

SCORE_PROVENANCE_CHECK = (
    "(protocol IS NULL AND model_name IS NULL AND model_version IS NULL AND model_epoch IS NULL"
    " AND pseudonym_key_id IS NULL AND through_seq IS NULL AND attempts_seen IS NULL AND batch_id IS NULL"
    " AND received_at IS NULL)"
    " OR ((protocol = 'studygraph.mastery/1' AND model_name IS NOT NULL AND model_version IS NOT NULL"
    " AND model_epoch IS NOT NULL AND pseudonym_key_id IS NOT NULL AND pseudonym_key_id ~ '^[0-9a-f]{16}$'"
    " AND through_seq IS NOT NULL AND through_seq >= 1 AND attempts_seen = through_seq AND attempts = through_seq"
    " AND batch_id IS NOT NULL AND received_at IS NOT NULL AND mastery >= 0 AND mastery <= 1) IS TRUE)"
)

STUDY_EVENT_TYPE_CHECK = (
    "event_type IN ('document_viewed', 'recommendation_shown', 'recommendation_opened', "
    "'recommendation_dismissed', 'focus_interval')"
)
STUDY_EVENT_SHAPE_CHECK = (
    "((event_type = 'document_viewed' AND document_id IS NOT NULL AND topic_id IS NULL AND started_at IS NULL"
    " AND ended_at IS NULL AND surface IS NULL AND recommendation_id IS NULL AND recommendation_type IS NULL)"
    " OR (event_type IN ('recommendation_shown', 'recommendation_opened', 'recommendation_dismissed')"
    " AND recommendation_id IS NOT NULL AND recommendation_id ~ '^[0-9a-f]{16}$'"
    " AND recommendation_type IN ('review_topic', 'practice_quiz', 'class_material', 'external_resource')"
    " AND started_at IS NULL AND ended_at IS NULL AND surface IS NULL)"
    " OR (event_type = 'focus_interval' AND started_at IS NOT NULL AND ended_at IS NOT NULL"
    " AND ended_at > started_at AND ended_at - started_at <= interval '1 hour' AND occurred_at = ended_at"
    " AND surface IN ('document', 'quiz', 'dashboard', 'chat')"
    " AND recommendation_id IS NULL AND recommendation_type IS NULL)) IS TRUE"
)
STUDY_EVENT_TIME_CHECK = (
    "occurred_at >= received_at - interval '25 hours' AND occurred_at <= received_at + interval '10 minutes'"
    " AND (started_at IS NULL OR started_at >= received_at - interval '25 hours')"
)

RELEASE_WEEK_CHECK = (
    "extract(isodow FROM week_start) = 1"
    " AND created_at >= ((week_start + 7)::timestamp AT TIME ZONE 'UTC')"
)
RELEASE_PAYLOAD_CHECK = (
    "jsonb_typeof(payload) = 'object' AND payload_sha256 ~ '^[0-9a-f]{64}$' AND policy = 'analytics_release/1'"
    " AND min_group_size >= 5"
)

UPGRADE_PREFLIGHT = """
DO $migration_0007_preflight$
DECLARE
    repeated bigint;
    gaps bigint;
    bad_cumulative bigint;
    stats_mismatch bigint;
BEGIN
    -- M7's lock order (statistics row, then the attempt insert): held until the migration commits, so no graded
    -- attempt can commit between this check, the backfill and the new triggers.
    LOCK TABLE topic_practice_stats, attempts IN SHARE ROW EXCLUSIVE MODE;
    SELECT count(*) INTO repeated FROM (
        SELECT 1 FROM attempts WHERE quiz_item_id IS NOT NULL
        GROUP BY user_id, topic_id, stats_attempts HAVING count(*) > 1
    ) r;
    SELECT count(*) INTO gaps FROM (
        SELECT 1 FROM attempts WHERE quiz_item_id IS NOT NULL
        GROUP BY user_id, topic_id HAVING count(*) <> max(stats_attempts) OR min(stats_attempts) <> 1
    ) g;
    SELECT count(*) INTO bad_cumulative FROM (
        SELECT stats_correct,
               sum(CASE WHEN is_correct THEN 1 ELSE 0 END)
                   OVER (PARTITION BY user_id, topic_id ORDER BY stats_attempts, id) AS running
        FROM attempts WHERE quiz_item_id IS NOT NULL
    ) c WHERE c.stats_correct <> c.running;
    SELECT count(*) INTO stats_mismatch FROM (
        SELECT 1 FROM topic_practice_stats s
        FULL JOIN (
            SELECT user_id, topic_id, min(course_id::text) AS course_id, count(*) AS n,
                   sum(CASE WHEN is_correct THEN 1 ELSE 0 END) AS k
            FROM attempts WHERE quiz_item_id IS NOT NULL GROUP BY user_id, topic_id
        ) a ON a.user_id = s.user_id AND a.topic_id = s.topic_id
        WHERE a.user_id IS NULL OR s.user_id IS NULL OR s.course_id::text <> a.course_id
           OR s.attempts <> a.n OR s.correct <> a.k
    ) m;
    IF repeated > 0 OR gaps > 0 OR bad_cumulative > 0 OR stats_mismatch > 0 THEN
        RAISE EXCEPTION 'Migration 0007 stopped before any change. % repeated and % incomplete graded attempt sequences, % graded attempts with inconsistent cumulative correctness, % topic statistics rows that disagree with the graded attempts. Reconcile them first (docs/database-migrations.md).', repeated, gaps, bad_cumulative, stats_mismatch;
    END IF;
END
$migration_0007_preflight$
"""

BACKFILL = """
INSERT INTO mastery_streams (user_id, course_id)
SELECT DISTINCT user_id, course_id FROM attempts WHERE quiz_item_id IS NOT NULL
ON CONFLICT DO NOTHING;
INSERT INTO mastery_outbox (event_id, user_id, course_id, topic_id, topic_seq, origin)
SELECT id, user_id, course_id, topic_id, stats_attempts, 'backfill' FROM attempts WHERE quiz_item_id IS NOT NULL
ON CONFLICT (event_id) DO NOTHING;
DO $migration_0007_backfill_check$
DECLARE
    graded bigint;
    events bigint;
BEGIN
    SELECT count(*) INTO graded FROM attempts WHERE quiz_item_id IS NOT NULL;
    SELECT count(*) INTO events FROM mastery_outbox;
    IF graded <> events THEN
        RAISE EXCEPTION 'Migration 0007: % graded attempts but % outbox events after the backfill', graded, events;
    END IF;
END
$migration_0007_backfill_check$;
"""

DOWNGRADE_GUARD = """
DO $migration_0007_downgrade_guard$
DECLARE
    telemetry bigint;
    releases bigint;
    predictions bigint;
    delivery bigint;
BEGIN
    LOCK TABLE topic_practice_stats, attempts, mastery_streams, mastery_outbox, mastery_scores, study_events,
        analytics_weekly_releases, mastery_model_state IN ACCESS EXCLUSIVE MODE;
    SELECT count(*) INTO telemetry FROM study_events;
    SELECT count(*) INTO releases FROM analytics_weekly_releases;
    SELECT count(*) INTO predictions FROM mastery_scores WHERE protocol IS NOT NULL OR received_at IS NOT NULL;
    SELECT (SELECT count(*) FROM mastery_outbox WHERE status <> 'pending')
         + (SELECT count(*) FROM mastery_streams WHERE state <> 'ready' OR batch_id IS NOT NULL OR claim_count > 0
                                                  OR operator_retry_count > 0)
      INTO delivery;
    IF telemetry > 0 OR releases > 0 OR predictions > 0 OR delivery > 0 THEN
        RAISE EXCEPTION 'Downgrade below 0007 refused before any change. % study events, % analytics releases, % model predictions and % rows of mastery delivery state would be lost. Remove that state deliberately first (docs/database-migrations.md).', telemetry, releases, predictions, delivery;
    END IF;
END
$migration_0007_downgrade_guard$
"""

TRIGGER_FUNCTIONS = """
CREATE FUNCTION studygraph_attempts_sequence_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    stats record;
    previous integer;
BEGIN
    -- The M7 grading path has already updated and locked this row (INSERT .. ON CONFLICT DO UPDATE); any other
    -- writer waits here, so the counters read below cannot move before this transaction ends.
    SELECT s.attempts, s.correct, s.course_id INTO stats FROM topic_practice_stats s
     WHERE s.user_id = NEW.user_id AND s.topic_id = NEW.topic_id FOR NO KEY UPDATE;
    IF NOT FOUND OR stats.course_id IS DISTINCT FROM NEW.course_id OR stats.attempts IS DISTINCT FROM NEW.stats_attempts
       OR stats.correct IS DISTINCT FROM NEW.stats_correct THEN
        RAISE EXCEPTION 'a graded practice attempt must carry the counters of its topic statistics'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.stats_attempts = 1 THEN
        previous := 0;
    ELSE
        SELECT a.stats_correct INTO previous FROM attempts a
         WHERE a.user_id = NEW.user_id AND a.topic_id = NEW.topic_id AND a.quiz_item_id IS NOT NULL
           AND a.stats_attempts = NEW.stats_attempts - 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'a graded practice attempt must continue the sequence of its topic'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.stats_correct IS DISTINCT FROM previous + (CASE WHEN NEW.is_correct THEN 1 ELSE 0 END) THEN
        RAISE EXCEPTION 'a graded practice attempt must continue the sequence of its topic'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION studygraph_attempts_mastery_outbox() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    -- DO NOTHING never waits for, or locks, an existing stream row; the outbox key below takes only a KEY SHARE lock
    -- on it, which the delivery transactions (FOR NO KEY UPDATE) never block.
    INSERT INTO mastery_streams (user_id, course_id) VALUES (NEW.user_id, NEW.course_id) ON CONFLICT DO NOTHING;
    INSERT INTO mastery_outbox (event_id, user_id, course_id, topic_id, topic_seq, origin)
    VALUES (NEW.id, NEW.user_id, NEW.course_id, NEW.topic_id, NEW.stats_attempts, 'trigger');
    RETURN NULL;
END
$fn$;

CREATE FUNCTION studygraph_mastery_outbox_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.event_id <> OLD.event_id OR NEW.user_id <> OLD.user_id OR NEW.course_id <> OLD.course_id
       OR NEW.topic_id <> OLD.topic_id OR NEW.topic_seq <> OLD.topic_seq OR NEW.origin <> OLD.origin
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'the identity of a mastery event is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.batch_id IS NOT NULL AND NEW.batch_id IS DISTINCT FROM OLD.batch_id THEN
        RAISE EXCEPTION 'the batch membership of a mastery event is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.batched_at IS NOT NULL AND NEW.batched_at IS DISTINCT FROM OLD.batched_at THEN
        RAISE EXCEPTION 'the batch membership of a mastery event is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'delivered' THEN
        RAISE EXCEPTION 'a delivered mastery event is final' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
           (OLD.status = 'pending' AND NEW.status = 'batched')
        OR (OLD.status = 'batched' AND NEW.status IN ('delivered', 'failed'))
        OR (OLD.status = 'failed' AND NEW.status = 'batched')) THEN
        RAISE EXCEPTION 'invalid mastery event transition' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION studygraph_analytics_release_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'a published analytics release is immutable' USING ERRCODE = 'check_violation';
END
$fn$;

-- AFTER triggers: the row's CHECK constraints, keys and M7's BEFORE trigger attempts_guard (assignment and grading)
-- are evaluated and reported first. Triggers of the same event fire in name order: the sequence guard, then the outbox.
CREATE TRIGGER attempts_mastery_sequence_guard AFTER INSERT ON attempts
    FOR EACH ROW WHEN (NEW.quiz_item_id IS NOT NULL) EXECUTE FUNCTION studygraph_attempts_sequence_guard();
CREATE TRIGGER attempts_outbox_enqueue AFTER INSERT ON attempts
    FOR EACH ROW WHEN (NEW.quiz_item_id IS NOT NULL) EXECUTE FUNCTION studygraph_attempts_mastery_outbox();
CREATE TRIGGER mastery_outbox_guard BEFORE UPDATE ON mastery_outbox
    FOR EACH ROW EXECUTE FUNCTION studygraph_mastery_outbox_guard();
CREATE TRIGGER analytics_weekly_releases_guard BEFORE UPDATE ON analytics_weekly_releases
    FOR EACH ROW EXECUTE FUNCTION studygraph_analytics_release_guard();
"""

DROP_TRIGGERS = """
DROP TRIGGER analytics_weekly_releases_guard ON analytics_weekly_releases;
DROP TRIGGER mastery_outbox_guard ON mastery_outbox;
DROP TRIGGER attempts_outbox_enqueue ON attempts;
DROP TRIGGER attempts_mastery_sequence_guard ON attempts;
DROP FUNCTION studygraph_analytics_release_guard();
DROP FUNCTION studygraph_mastery_outbox_guard();
DROP FUNCTION studygraph_attempts_mastery_outbox();
DROP FUNCTION studygraph_attempts_sequence_guard();
"""


def _tz(name: str, nullable: bool = False, default: bool = True) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()") if default else None, nullable=nullable
    )


def upgrade() -> None:
    op.execute(UPGRADE_PREFLIGHT)

    # --- attempts: the event key, the sequence key and the analytics index ---------------------
    op.create_unique_constraint(
        "attempts_mastery_event_key", "attempts", ["id", "user_id", "course_id", "topic_id", "stats_attempts"]
    )
    op.create_index(
        "attempts_topic_sequence_idx",
        "attempts",
        ["user_id", "topic_id", "stats_attempts"],
        unique=True,
        postgresql_where=sa.text("quiz_item_id IS NOT NULL"),
    )
    op.create_index(
        "attempts_graded_course_submitted_idx",
        "attempts",
        ["course_id", "submitted_at"],
        postgresql_where=sa.text("quiz_item_id IS NOT NULL"),
    )

    # --- delivery streams -----------------------------------------------------------------------
    op.create_table(
        "mastery_streams",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(length=20), server_default=sa.text("'ready'::character varying"), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=True),
        sa.Column("batch_epoch", sa.UUID(), nullable=True),
        sa.Column("batch_size", sa.SmallInteger(), nullable=True),
        _tz("batch_created_at", nullable=True, default=False),
        sa.Column("batch_claims", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        _tz("claimed_at", nullable=True, default=False),
        _tz("lease_expires_at", nullable=True, default=False),
        _tz("next_attempt_at"),
        _tz("last_dispatched_at", nullable=True, default=False),
        sa.Column("dispatch_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("claim_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(length=40), nullable=True),
        sa.Column("operator_retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _tz("last_operator_retry_at", nullable=True, default=False),
        sa.Column("last_operator_actor_id", sa.UUID(), nullable=True),
        _tz("created_at"),
        _tz("updated_at"),
        sa.CheckConstraint(STREAM_STATE_CHECK, name=op.f("mastery_streams_state_check")),
        sa.CheckConstraint(STREAM_BATCH_CHECK, name=op.f("mastery_streams_batch_check")),
        sa.CheckConstraint(STREAM_LEASE_CHECK, name=op.f("mastery_streams_lease_check")),
        sa.CheckConstraint(STREAM_ERROR_CHECK, name=op.f("mastery_streams_error_check")),
        sa.CheckConstraint(STREAM_COUNTERS_CHECK, name=op.f("mastery_streams_counters_check")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("mastery_streams_user_id_fkey")),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("mastery_streams_course_id_fkey")),
        sa.ForeignKeyConstraint(
            ["last_operator_actor_id"], ["users.id"], name=op.f("mastery_streams_last_operator_actor_id_fkey")
        ),
        sa.PrimaryKeyConstraint("user_id", "course_id", name=op.f("mastery_streams_pkey")),
    )
    op.create_index(op.f("mastery_streams_state_next_attempt_at_idx"), "mastery_streams", ["state", "next_attempt_at"])
    op.create_index(op.f("mastery_streams_course_id_idx"), "mastery_streams", ["course_id"])

    # --- outbox -----------------------------------------------------------------------------------
    op.create_table(
        "mastery_outbox",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("topic_id", sa.UUID(), nullable=False),
        sa.Column("topic_seq", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'pending'::character varying"), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        _tz("created_at"),
        _tz("batched_at", nullable=True, default=False),
        _tz("delivered_at", nullable=True, default=False),
        sa.CheckConstraint("topic_seq >= 1", name=op.f("mastery_outbox_topic_seq_check")),
        sa.CheckConstraint("origin IN ('trigger', 'backfill')", name=op.f("mastery_outbox_origin_check")),
        sa.CheckConstraint(OUTBOX_STATUS_CHECK, name=op.f("mastery_outbox_status_check")),
        sa.CheckConstraint(OUTBOX_LIFECYCLE_CHECK, name=op.f("mastery_outbox_lifecycle_check")),
        sa.ForeignKeyConstraint(
            ["event_id", "user_id", "course_id", "topic_id", "topic_seq"],
            ["attempts.id", "attempts.user_id", "attempts.course_id", "attempts.topic_id", "attempts.stats_attempts"],
            name="mastery_outbox_attempt_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "course_id"],
            ["mastery_streams.user_id", "mastery_streams.course_id"],
            name="mastery_outbox_stream_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="mastery_outbox_topic_fkey"
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("mastery_outbox_pkey")),
        sa.UniqueConstraint("user_id", "topic_id", "topic_seq", name=op.f("mastery_outbox_user_id_topic_id_topic_seq_key")),
    )
    op.create_index(
        "mastery_outbox_pending_idx",
        "mastery_outbox",
        ["user_id", "course_id", "topic_seq", "topic_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "mastery_outbox_batch_idx", "mastery_outbox", ["batch_id"], postgresql_where=sa.text("batch_id IS NOT NULL")
    )

    # --- model state (one row) ----------------------------------------------------------------------
    op.create_table(
        "mastery_model_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'unknown'::character varying"), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=True),
        sa.Column("protocol", sa.String(length=40), nullable=True),
        sa.Column("service", sa.String(length=40), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("model_version", sa.String(length=100), nullable=True),
        sa.Column("state_epoch", sa.UUID(), nullable=True),
        sa.Column("pseudonym_key_id", sa.String(length=16), nullable=True),
        _tz("probe_started_at", nullable=True, default=False),
        _tz("probed_at", nullable=True, default=False),
        _tz("updated_at"),
        sa.CheckConstraint("id = 1", name=op.f("mastery_model_state_single_row_check")),
        sa.CheckConstraint(
            "status IN ('unknown', 'compatible', 'incompatible')", name=op.f("mastery_model_state_status_check")
        ),
        sa.CheckConstraint(MODEL_STATE_CHECK, name=op.f("mastery_model_state_state_check")),
        sa.PrimaryKeyConstraint("id", name=op.f("mastery_model_state_pkey")),
    )
    op.execute("INSERT INTO mastery_model_state (id, status) VALUES (1, 'unknown')")

    # --- mastery_scores: course, provenance, timezone-aware review time ------------------------------
    op.execute("LOCK TABLE mastery_scores IN ACCESS EXCLUSIVE MODE")
    op.add_column("mastery_scores", sa.Column("course_id", sa.UUID(), nullable=True))
    op.execute("UPDATE mastery_scores m SET course_id = t.course_id FROM topics t WHERE t.id = m.topic_id")
    op.alter_column("mastery_scores", "course_id", nullable=False)
    op.alter_column(
        "mastery_scores",
        "next_review_at",
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(),
        existing_nullable=True,
        postgresql_using="next_review_at AT TIME ZONE 'UTC'",
    )
    op.add_column("mastery_scores", sa.Column("protocol", sa.String(length=40), nullable=True))
    op.add_column("mastery_scores", sa.Column("model_name", sa.String(length=100), nullable=True))
    op.add_column("mastery_scores", sa.Column("model_version", sa.String(length=100), nullable=True))
    op.add_column("mastery_scores", sa.Column("model_epoch", sa.UUID(), nullable=True))
    op.add_column("mastery_scores", sa.Column("pseudonym_key_id", sa.String(length=16), nullable=True))
    op.add_column("mastery_scores", sa.Column("through_seq", sa.Integer(), nullable=True))
    op.add_column("mastery_scores", sa.Column("attempts_seen", sa.Integer(), nullable=True))
    op.add_column("mastery_scores", sa.Column("batch_id", sa.UUID(), nullable=True))
    op.add_column("mastery_scores", _tz("received_at", nullable=True, default=False))
    op.create_foreign_key(op.f("mastery_scores_course_id_fkey"), "mastery_scores", "courses", ["course_id"], ["id"])
    op.create_foreign_key(
        "mastery_scores_topic_course_fkey", "mastery_scores", "topics", ["topic_id", "course_id"], ["id", "course_id"]
    )
    op.create_check_constraint(op.f("mastery_scores_provenance_check"), "mastery_scores", SCORE_PROVENANCE_CHECK)

    # --- study events -------------------------------------------------------------------------------
    op.create_unique_constraint(op.f("documents_id_course_id_key"), "documents", ["id", "course_id"])
    op.create_table(
        "study_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("client_event_id", sa.UUID(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        _tz("occurred_at", default=False),
        _tz("started_at", nullable=True, default=False),
        _tz("ended_at", nullable=True, default=False),
        sa.Column("surface", sa.String(length=20), nullable=True),
        sa.Column("topic_id", sa.UUID(), nullable=True),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("recommendation_id", sa.String(length=16), nullable=True),
        sa.Column("recommendation_type", sa.String(length=20), nullable=True),
        _tz("received_at"),
        sa.CheckConstraint(STUDY_EVENT_TYPE_CHECK, name=op.f("study_events_event_type_check")),
        sa.CheckConstraint(STUDY_EVENT_SHAPE_CHECK, name=op.f("study_events_shape_check")),
        sa.CheckConstraint(STUDY_EVENT_TIME_CHECK, name=op.f("study_events_time_check")),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name=op.f("study_events_fingerprint_check")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("study_events_user_id_fkey")),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("study_events_course_id_fkey")),
        sa.ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="study_events_topic_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "course_id"],
            ["documents.id", "documents.course_id"],
            name="study_events_document_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("study_events_pkey")),
        sa.UniqueConstraint("user_id", "client_event_id", name=op.f("study_events_user_id_client_event_id_key")),
    )
    op.create_index(op.f("study_events_user_id_received_at_idx"), "study_events", ["user_id", "received_at"])
    op.create_index(
        op.f("study_events_user_id_course_id_occurred_at_idx"), "study_events", ["user_id", "course_id", "occurred_at"]
    )
    op.create_index(op.f("study_events_received_at_idx"), "study_events", ["received_at"])

    # --- analytics releases -------------------------------------------------------------------------
    op.create_table(
        "analytics_weekly_releases",
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("policy", sa.String(length=40), nullable=False),
        sa.Column("min_group_size", sa.SmallInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        _tz("created_at"),
        sa.CheckConstraint(RELEASE_WEEK_CHECK, name=op.f("analytics_weekly_releases_week_check")),
        sa.CheckConstraint(RELEASE_PAYLOAD_CHECK, name=op.f("analytics_weekly_releases_payload_check")),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("analytics_weekly_releases_course_id_fkey"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("course_id", "week_start", name=op.f("analytics_weekly_releases_pkey")),
    )

    op.execute(TRIGGER_FUNCTIONS)
    op.execute(BACKFILL)


def downgrade() -> None:
    """Remove everything upgrade() added, only if no M8 state would be lost."""
    op.execute(DOWNGRADE_GUARD)
    op.execute(DROP_TRIGGERS)
    op.drop_table("analytics_weekly_releases")

    op.drop_index(op.f("study_events_received_at_idx"), table_name="study_events")
    op.drop_index(op.f("study_events_user_id_course_id_occurred_at_idx"), table_name="study_events")
    op.drop_index(op.f("study_events_user_id_received_at_idx"), table_name="study_events")
    op.drop_table("study_events")
    op.drop_constraint(op.f("documents_id_course_id_key"), "documents", type_="unique")

    op.drop_constraint(op.f("mastery_scores_provenance_check"), "mastery_scores", type_="check")
    op.drop_constraint("mastery_scores_topic_course_fkey", "mastery_scores", type_="foreignkey")
    op.drop_constraint(op.f("mastery_scores_course_id_fkey"), "mastery_scores", type_="foreignkey")
    for column in (
        "received_at", "batch_id", "attempts_seen", "through_seq", "pseudonym_key_id", "model_epoch",
        "model_version", "model_name", "protocol",
    ):  # fmt: skip
        op.drop_column("mastery_scores", column)
    op.alter_column(
        "mastery_scores",
        "next_review_at",
        type_=sa.DateTime(),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="next_review_at AT TIME ZONE 'UTC'",
    )
    op.drop_column("mastery_scores", "course_id")

    op.drop_table("mastery_model_state")
    op.drop_index("mastery_outbox_batch_idx", table_name="mastery_outbox", postgresql_where=sa.text("batch_id IS NOT NULL"))
    op.drop_index("mastery_outbox_pending_idx", table_name="mastery_outbox", postgresql_where=sa.text("status = 'pending'"))
    op.drop_table("mastery_outbox")
    op.drop_index(op.f("mastery_streams_course_id_idx"), table_name="mastery_streams")
    op.drop_index(op.f("mastery_streams_state_next_attempt_at_idx"), table_name="mastery_streams")
    op.drop_table("mastery_streams")

    op.drop_index(
        "attempts_graded_course_submitted_idx", table_name="attempts", postgresql_where=sa.text("quiz_item_id IS NOT NULL")
    )
    op.drop_index("attempts_topic_sequence_idx", table_name="attempts", postgresql_where=sa.text("quiz_item_id IS NOT NULL"))
    op.drop_constraint("attempts_mastery_event_key", "attempts", type_="unique")
