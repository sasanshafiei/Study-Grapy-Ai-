"""CHECK expressions of revisions 0007 (M8) and 0008 (M9), shared by the ORM models.

Migrations 0007 and 0008 are self-contained and keep their own verbatim copies;
tests/test_m8_unit.py (0007) and tests/test_migration_0008.py (0008) assert that
they are identical, so the ORM and the migrated database cannot drift apart silently. Every multi-branch CHECK is
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


# --- Revision 0008 (M9, professor exams; docs/exam-api.md) ----------------------------------------
# Migration 0008 keeps its own verbatim copy; tests/test_migration_0008.py asserts equality (MIG-04).
# Every M9 CHECK on `questions` is gated on the M9 marker `version`: it is TRUE for every legacy and M7
# row, so no existing row, test or error name changes (PostgreSQL evaluates CHECKs in name order).

BLOOM_LEVELS_SQL = "'remember', 'understand', 'apply', 'analyze', 'evaluate', 'create'"
QUESTION_BLOOM_CHECK = f"bloom_level IS NULL OR bloom_level IN ({BLOOM_LEVELS_SQL})"

# Structural shape of exam content, shared by bank rows (questions) and approved revisions. The
# true/false form is exactly M7's canonical choice array, so practice semantics are unchanged.
QUESTION_CONTENT_SHAPE = (
    "((qtype = 'mcq' AND jsonb_typeof(choices) = 'array' AND jsonb_array_length(choices) BETWEEN 2 AND 6"
    " AND jsonb_typeof(answer_key) = 'object' AND (answer_key - 'choice_id') = '{}'::jsonb"
    " AND jsonb_typeof(answer_key -> 'choice_id') = 'string' AND (answer_key ->> 'choice_id') ~ '^[a-f]$'"
    " AND choices @> jsonb_build_array(jsonb_build_object('id', answer_key ->> 'choice_id')))"
    " OR (qtype = 'true_false'"
    " AND choices = '[{\"id\": \"true\", \"text\": \"True\"}, {\"id\": \"false\", \"text\": \"False\"}]'::jsonb"
    " AND answer_key IN ('{\"choice_id\": \"true\"}'::jsonb, '{\"choice_id\": \"false\"}'::jsonb))"
    " OR (qtype = 'short' AND choices IS NULL AND jsonb_typeof(answer_key) = 'object'"
    " AND (answer_key - 'model_answer' - 'rubric') = '{}'::jsonb"
    " AND jsonb_typeof(answer_key -> 'model_answer') = 'string'"
    " AND char_length(answer_key ->> 'model_answer') BETWEEN 1 AND 4000"
    " AND jsonb_typeof(answer_key -> 'rubric') IN ('string', 'null')"
    " AND char_length(coalesce(answer_key ->> 'rubric', '')) <= 4000))"
)

QUESTION_BANK_CHECK = (
    "(version IS NULL AND created_by IS NULL AND updated_at IS NULL AND approved_revision_no IS NULL)"
    " OR ((version >= 1 AND purpose = 'exam' AND status IN ('draft', 'approved', 'retired')"
    " AND created_by IS NOT NULL AND updated_at IS NOT NULL"
    " AND generated_by_session_id IS NULL AND generated_for_user_id IS NULL AND generation IS NULL"
    " AND hint IS NULL AND grading_method IS NULL AND answer IS NULL"
    " AND content_hash ~ '^[0-9a-f]{64}$'"
    " AND char_length(btrim(text)) BETWEEN 1 AND 4000"
    " AND (explanation IS NULL OR char_length(explanation) <= 4000)"
    " AND (approved_revision_no IS NULL OR approved_revision_no >= 1)"
    " AND (status <> 'approved' OR approved_revision_no IS NOT NULL)"
    f" AND {QUESTION_CONTENT_SHAPE}"
    " AND (citations IS NULL OR (jsonb_typeof(citations) = 'array' AND jsonb_array_length(citations) <= 4))) IS TRUE)"
)

REVISION_PURPOSE_CHECK = "purpose = 'exam'"
REVISION_DOMAIN_CHECK = (
    "qtype IN ('mcq', 'true_false', 'short') AND difficulty IN ('easy', 'medium', 'hard')"
    f" AND (bloom_level IS NULL OR bloom_level IN ({BLOOM_LEVELS_SQL}))"
)
REVISION_CONTENT_CHECK = (
    "(revision_no >= 1 AND char_length(btrim(stem)) BETWEEN 1 AND 4000"
    " AND (explanation IS NULL OR char_length(explanation) <= 4000)"
    " AND content_sha256 ~ '^[0-9a-f]{64}$'"
    " AND jsonb_typeof(source_refs) = 'array' AND jsonb_array_length(source_refs) <= 4"
    f" AND {QUESTION_CONTENT_SHAPE}) IS TRUE"
)

EXAM_STATUS_CHECK = "status IN ('draft', 'finalized', 'archived')"
EXAM_FIELDS_CHECK = (
    "(version >= 1 AND title = btrim(title) AND char_length(title) BETWEEN 1 AND 200 AND title !~ '[[:cntrl:]]'"
    " AND (instructions IS NULL OR char_length(instructions) <= 4000)"
    " AND (duration_minutes IS NULL OR duration_minutes BETWEEN 1 AND 600)) IS TRUE"
)
EXAM_FINGERPRINT_CHECK = "finalize_preview_fingerprint IS NULL OR finalize_preview_fingerprint ~ '^pf1:[0-9a-f]{64}$'"
_EXAM_UNFINALIZED = (
    "finalized_at IS NULL AND finalized_by IS NULL AND finalized_version IS NULL AND course_code_snapshot IS NULL"
    " AND course_name_snapshot IS NULL AND item_count IS NULL AND total_points IS NULL AND content_sha256 IS NULL"
    " AND content_chars IS NULL AND blueprint_status IS NULL AND blueprint_deviation_reason IS NULL"
    " AND finalize_preview_fingerprint IS NULL"
)
_EXAM_FINALIZED = (
    "finalized_at IS NOT NULL AND finalized_by IS NOT NULL AND finalized_version IS NOT NULL"
    " AND finalized_version >= 2 AND finalized_version <= version"
    " AND course_code_snapshot IS NOT NULL AND course_name_snapshot IS NOT NULL"
    " AND item_count BETWEEN 1 AND 200 AND total_points >= 1 AND content_sha256 ~ '^[0-9a-f]{64}$'"
    " AND content_chars BETWEEN 1 AND 300000"
    " AND blueprint_status IN ('none', 'fulfilled', 'deviation_accepted')"
    " AND finalize_preview_fingerprint ~ '^pf1:[0-9a-f]{64}$'"
)
EXAM_STATE_CHECK = (
    f"(((status = 'draft' AND archived_at IS NULL AND {_EXAM_UNFINALIZED})"
    f" OR (status = 'finalized' AND archived_at IS NULL AND {_EXAM_FINALIZED})"
    f" OR (status = 'archived' AND archived_at IS NOT NULL AND (({_EXAM_UNFINALIZED}) OR ({_EXAM_FINALIZED}))))"
    " AND (blueprint_status IS NOT DISTINCT FROM 'deviation_accepted') = (blueprint_deviation_reason IS NOT NULL)"
    " AND (blueprint_deviation_reason IS NULL OR char_length(blueprint_deviation_reason) BETWEEN 10 AND 500)) IS TRUE"
)

EXAM_ITEM_PURPOSE_CHECK = "purpose = 'exam'"
EXAM_ITEM_VALUES_CHECK = "position BETWEEN 1 AND 200 AND points BETWEEN 1 AND 100"

BLUEPRINT_VALUES_CHECK = (
    "(position BETWEEN 1 AND 50 AND question_count BETWEEN 1 AND 200 AND points_each BETWEEN 1 AND 100"
    " AND (qtype IS NULL OR qtype IN ('mcq', 'true_false', 'short'))"
    " AND (difficulty IS NULL OR difficulty IN ('easy', 'medium', 'hard'))"
    f" AND (bloom_level IS NULL OR bloom_level IN ({BLOOM_LEVELS_SQL}))) IS TRUE"
)

AUDIT_ACTIONS = (
    "question_created", "question_updated", "question_approved", "question_approval_revoked", "question_retired",
    "exam_created", "exam_updated", "blueprint_replaced", "item_added", "item_updated", "item_removed",
    "items_reordered", "exam_filled_from_blueprint", "exam_finalized", "exam_archived", "exam_duplicated",
    "export_downloaded", "preview_file_downloaded",
)  # fmt: skip
AUDIT_SHAPE_CHECK = (
    "(entity_kind IN ('question', 'exam', 'export')"
    " AND action IN (" + ", ".join(f"'{a}'" for a in AUDIT_ACTIONS) + ")"
    " AND (entity_version IS NULL OR entity_version >= 1)"
    " AND jsonb_typeof(details) = 'object' AND octet_length(details::text) <= 4096) IS TRUE"
)

IDEMPOTENCY_CHECK = (
    "operation IN ('question_create', 'exam_create', 'exam_duplicate') AND request_fingerprint ~ '^[0-9a-f]{64}$'"
)
