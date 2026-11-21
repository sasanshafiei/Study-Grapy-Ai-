"""CHECK expressions of revision 0007 (M8), shared by the ORM models.

Migration 0007 is self-contained and keeps its own verbatim copy;
tests/test_mastery_migration.py asserts that both are identical, so the ORM and
the migrated database cannot drift apart silently. Every multi-branch CHECK is
`(...) IS TRUE`: a CHECK that evaluates to NULL would otherwise pass.
"""

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
