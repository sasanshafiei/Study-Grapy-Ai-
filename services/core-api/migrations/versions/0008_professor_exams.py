"""Professor question bank, exams, blueprints, approval revisions, audit and idempotency (M9-A1).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-21

Added (docs/exam-api.md, docs/exam-export.md, docs/database-migrations.md). 30 tables before,
36 after:

- questions: version (the M9 marker and optimistic-concurrency counter), created_by,
  updated_at, approved_revision_no, bloom_level. All nullable without default (metadata-only
  ALTERs) and NULL for every existing row. questions_bank_check accepts every legacy and M7
  row through its first branch (version IS NULL ...) and requires a complete exam-purpose
  bank row otherwise (structural content shape, canonical true/false choices, bounds).
  questions_bloom_level_check limits the vocabulary. A composite key
  (id, approved_revision_no) -> exam_question_revisions (MATCH SIMPLE, added after that
  table: the two tables reference each other).
- exam_question_revisions: one immutable row per approval, PK (question_id, revision_no),
  purpose CHECKed to 'exam', composite keys to the question (id, course_id, purpose) and the
  topic (id, course_id), frozen source references.
- exams: draft | finalized | archived, version, frozen finalization columns (course header,
  totals, content hash and size, finalized_version, blueprint decision and deviation reason,
  finalize_preview_fingerprint), UNIQUE (id, course_id), same-course derived_from key.
- exam_blueprint_rows: per-exam requirements; UNIQUE (id, exam_id) (non-deferrable, the
  target of exam_items' row key) and exam_blueprint_rows_exam_id_position_key
  UNIQUE (exam_id, position) DEFERRABLE INITIALLY DEFERRED (reorder in one transaction).
- exam_items: purpose CHECKed to 'exam'; composite keys to the exam, to an exam-purpose
  question of the same course, to the pinned revision and to a blueprint row of the same
  exam; exam_items_exam_id_position_key UNIQUE (exam_id, position) DEFERRABLE INITIALLY
  DEFERRED; UNIQUE (exam_id, question_id).
- exam_audit_events: append-only, ids/hashes/counts only (details <= 4 KiB).
- exam_idempotency_keys: PK (user_id, idempotency_key), request fingerprint, resource.

Triggers (PL/pgSQL; outside the reflected-schema comparison). BEFORE triggers of one event fire
in name order: questions_bank_guard before M7's questions_guard, attempts_exam_guard before M7's
attempts_guard; each accepts the other's legitimate writes, and no M7/M8 function is changed.
- questions_bank_guard: a bank row is inserted as draft version 1; every UPDATE adds exactly 1
  to version; identity (id, course_id, purpose, created_by, created_at) is immutable; only
  draft->draft|approved|retired and approved->draft|retired; retired is final; approval and
  retirement change no content; approved requires the newest revision with the same hash; a
  legacy row can never become a bank row; bank rows are never deleted. Legacy/M7 rows: no effect.
- exam_question_revisions_guard: INSERT locks the question FOR UPDATE (serializing approvals),
  requires a draft bank row, revision_no = max + 1, and content, hash and the
  (document_id, locator) sequence of source_refs equal to the working copy. No UPDATE/DELETE.
- exams_guard: inserted draft version 1; no DELETE; identity immutable; version +1 per UPDATE;
  only draft->draft|finalized|archived and finalized->archived (which changes nothing but status,
  archived_at, version, updated_at); draft->finalized verifies items (>= 1, positions 1..n,
  all pinned to the question's current approved revision, totals), finalized_version and the
  blueprint decision against the pinned revisions.
- exam_items_guard / exam_blueprint_rows_guard: writes only while the parent exam is a draft
  (parent locked FOR SHARE); row identity immutable.
- exam_audit_events_guard: UPDATE and DELETE refused.
- attempts_exam_guard: no attempt can be inserted on, or moved to, an exam-purpose question.
- *_truncate_guard (statement level) on exams, exam_items, exam_question_revisions and
  exam_audit_events: TRUNCATE refused.

Data safety:
- upgrade locks questions and attempts (SHARE ROW EXCLUSIVE) and reports, as a NOTICE with
  counts only, legacy exam-purpose questions and attempts on them. Nothing existing can violate
  a new constraint (every new column starts NULL), so it never refuses and changes no value.
- downgrade refuses, before any change, while any row exists in the six new tables or any
  question has version or bloom_level set; otherwise it removes exactly what upgrade added.
Both run as SQL, so they also apply to offline (`--sql`) output.

Self-contained on purpose: it does not import app.models. The CHECK expressions below are a
verbatim copy of app/schema_checks.py (asserted equal by tests/test_migration_0008.py).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

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

UPGRADE_PREFLIGHT = """
DO $migration_0008_preflight$
DECLARE
    legacy_exam_questions bigint;
    legacy_exam_attempts bigint;
BEGIN
    -- Held until the migration commits: no question or attempt can change between this report and the new
    -- constraints and triggers.
    LOCK TABLE questions, attempts IN SHARE ROW EXCLUSIVE MODE;
    SELECT count(*) INTO legacy_exam_questions FROM questions WHERE purpose = 'exam';
    SELECT count(*) INTO legacy_exam_attempts FROM attempts a JOIN questions q ON q.id = a.question_id
     WHERE q.purpose = 'exam';
    RAISE NOTICE 'Migration 0008 preflight: % legacy exam-purpose questions and % attempts on them; they are kept unchanged and are invisible to the exam API.', legacy_exam_questions, legacy_exam_attempts;
END
$migration_0008_preflight$
"""

M9_TABLES = (
    "exams", "exam_items", "exam_blueprint_rows", "exam_question_revisions", "exam_audit_events",
    "exam_idempotency_keys",
)  # fmt: skip

DOWNGRADE_GUARD = """
DO $migration_0008_downgrade_guard$
DECLARE
    m9_rows bigint;
    bank_questions bigint;
BEGIN
    LOCK TABLE questions, attempts, exams, exam_items, exam_blueprint_rows, exam_question_revisions,
        exam_audit_events, exam_idempotency_keys IN ACCESS EXCLUSIVE MODE;
    SELECT (SELECT count(*) FROM exams) + (SELECT count(*) FROM exam_items) + (SELECT count(*) FROM exam_blueprint_rows)
         + (SELECT count(*) FROM exam_question_revisions) + (SELECT count(*) FROM exam_audit_events)
         + (SELECT count(*) FROM exam_idempotency_keys)
      INTO m9_rows;
    SELECT count(*) INTO bank_questions FROM questions WHERE version IS NOT NULL OR bloom_level IS NOT NULL;
    IF m9_rows > 0 OR bank_questions > 0 THEN
        RAISE EXCEPTION 'Downgrade below 0008 refused before any change. % rows of exams, items, blueprints, revisions, audit events or idempotency keys and % bank questions or Bloom levels would be lost. Remove that state deliberately first (docs/database-migrations.md).', m9_rows, bank_questions;
    END IF;
END
$migration_0008_downgrade_guard$
"""

TRIGGER_FUNCTIONS = """
CREATE FUNCTION studygraph_questions_bank_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    revision_hash text;
    newest integer;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.version IS NULL THEN
            RETURN NEW;  -- legacy and M7 rows: unchanged rules (questions_guard)
        END IF;
        IF NEW.version <> 1 OR NEW.status IS DISTINCT FROM 'draft' OR NEW.approved_revision_no IS NOT NULL THEN
            RAISE EXCEPTION 'a new bank question starts as a draft at version 1' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.version IS NULL THEN
        IF TG_OP = 'UPDATE' AND (NEW.version IS NOT NULL OR NEW.created_by IS NOT NULL OR NEW.updated_at IS NOT NULL
                                 OR NEW.approved_revision_no IS NOT NULL) THEN
            RAISE EXCEPTION 'an existing question cannot become a bank question' USING ERRCODE = 'check_violation';
        END IF;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank questions are never deleted' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
        RAISE EXCEPTION 'every change of a bank question increments its version by one' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.id <> OLD.id OR NEW.course_id IS DISTINCT FROM OLD.course_id OR NEW.purpose IS DISTINCT FROM OLD.purpose
       OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'the identity of a bank question is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'retired' THEN
        RAISE EXCEPTION 'a retired bank question is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT ((OLD.status = 'draft' AND NEW.status IN ('draft', 'approved', 'retired'))
            OR (OLD.status = 'approved' AND NEW.status IN ('draft', 'retired'))) THEN
        RAISE EXCEPTION 'invalid bank question transition' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NEW.status IN ('approved', 'retired') AND (
           NEW.qtype IS DISTINCT FROM OLD.qtype OR NEW.difficulty IS DISTINCT FROM OLD.difficulty
        OR NEW.bloom_level IS DISTINCT FROM OLD.bloom_level OR NEW.topic_id IS DISTINCT FROM OLD.topic_id
        OR NEW.text IS DISTINCT FROM OLD.text OR NEW.choices IS DISTINCT FROM OLD.choices
        OR NEW.answer_key IS DISTINCT FROM OLD.answer_key OR NEW.explanation IS DISTINCT FROM OLD.explanation
        OR NEW.citations IS DISTINCT FROM OLD.citations OR NEW.content_hash IS DISTINCT FROM OLD.content_hash) THEN
        RAISE EXCEPTION 'approving or retiring a bank question cannot change its content' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approved_revision_no IS DISTINCT FROM OLD.approved_revision_no OR NEW.status = 'approved' THEN
        SELECT max(r.revision_no) INTO newest FROM exam_question_revisions r WHERE r.question_id = NEW.id;
        IF NEW.approved_revision_no IS DISTINCT FROM newest
           OR (NEW.approved_revision_no IS DISTINCT FROM OLD.approved_revision_no AND NEW.status <> 'approved') THEN
            RAISE EXCEPTION 'the approved revision of a bank question can only move to its newest revision, by approval'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.status = 'approved' THEN
        SELECT r.content_sha256 INTO revision_hash FROM exam_question_revisions r
         WHERE r.question_id = NEW.id AND r.revision_no = NEW.approved_revision_no;
        IF revision_hash IS DISTINCT FROM NEW.content_hash THEN
            RAISE EXCEPTION 'an approved bank question must equal its approved revision' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION studygraph_exam_question_revisions_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    q record;
    newest integer;
    refs jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'approved question revisions are immutable' USING ERRCODE = 'check_violation';
    END IF;
    -- The trigger takes the question lock itself, so revision inserts of one question are serialized even if a
    -- caller forgot to lock; a waiting insert then sees the committed approval below and is refused.
    SELECT x.id, x.version, x.status, x.content_hash, x.qtype, x.difficulty, x.bloom_level, x.topic_id, x.text,
           x.choices, x.answer_key, x.explanation, x.citations
      INTO q FROM questions x WHERE x.id = NEW.question_id FOR UPDATE;
    IF NOT FOUND OR q.version IS NULL OR q.status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'a revision can only approve a draft bank question' USING ERRCODE = 'check_violation';
    END IF;
    SELECT coalesce(max(r.revision_no), 0) INTO newest FROM exam_question_revisions r WHERE r.question_id = NEW.question_id;
    IF NEW.revision_no IS DISTINCT FROM newest + 1 THEN
        RAISE EXCEPTION 'revision numbers of a question are contiguous' USING ERRCODE = 'check_violation';
    END IF;
    IF jsonb_typeof(NEW.source_refs) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'a revision must be an exact copy of the working copy' USING ERRCODE = 'check_violation';
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object('document_id', s.e -> 'document_id', 'locator', s.e -> 'locator')
                              ORDER BY s.n), '[]'::jsonb)
      INTO refs FROM jsonb_array_elements(NEW.source_refs) WITH ORDINALITY AS s(e, n);
    IF NEW.content_sha256 IS DISTINCT FROM q.content_hash OR NEW.qtype IS DISTINCT FROM q.qtype
       OR NEW.difficulty IS DISTINCT FROM q.difficulty OR NEW.bloom_level IS DISTINCT FROM q.bloom_level
       OR NEW.topic_id IS DISTINCT FROM q.topic_id OR NEW.stem IS DISTINCT FROM q.text
       OR NEW.choices IS DISTINCT FROM q.choices OR NEW.answer_key IS DISTINCT FROM q.answer_key
       OR NEW.explanation IS DISTINCT FROM q.explanation OR refs IS DISTINCT FROM coalesce(q.citations, '[]'::jsonb) THEN
        RAISE EXCEPTION 'a revision must be an exact copy of the working copy' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION studygraph_exams_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    n bigint;
    lo integer;
    hi integer;
    distinct_positions bigint;
    points bigint;
    unpinned bigint;
    not_current bigint;
    row_count bigint;
    unassigned bigint;
    wrong_count bigint;
    mismatched bigint;
    fulfilled boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status IS DISTINCT FROM 'draft' OR NEW.version IS DISTINCT FROM 1 THEN
            RAISE EXCEPTION 'a new exam starts as a draft at version 1' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'exams are never deleted' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.id <> OLD.id OR NEW.course_id <> OLD.course_id OR NEW.created_by <> OLD.created_by
       OR NEW.created_at <> OLD.created_at OR NEW.derived_from_exam_id IS DISTINCT FROM OLD.derived_from_exam_id THEN
        RAISE EXCEPTION 'the identity of an exam is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
        RAISE EXCEPTION 'every change of an exam increments its version by one' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'archived' THEN
        RAISE EXCEPTION 'an archived exam is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'finalized' THEN
        IF NEW.status IS DISTINCT FROM 'archived'
           OR NEW.title IS DISTINCT FROM OLD.title OR NEW.instructions IS DISTINCT FROM OLD.instructions
           OR NEW.duration_minutes IS DISTINCT FROM OLD.duration_minutes
           OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at OR NEW.finalized_by IS DISTINCT FROM OLD.finalized_by
           OR NEW.finalized_version IS DISTINCT FROM OLD.finalized_version
           OR NEW.course_code_snapshot IS DISTINCT FROM OLD.course_code_snapshot
           OR NEW.course_name_snapshot IS DISTINCT FROM OLD.course_name_snapshot
           OR NEW.item_count IS DISTINCT FROM OLD.item_count OR NEW.total_points IS DISTINCT FROM OLD.total_points
           OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256 OR NEW.content_chars IS DISTINCT FROM OLD.content_chars
           OR NEW.blueprint_status IS DISTINCT FROM OLD.blueprint_status
           OR NEW.blueprint_deviation_reason IS DISTINCT FROM OLD.blueprint_deviation_reason
           OR NEW.finalize_preview_fingerprint IS DISTINCT FROM OLD.finalize_preview_fingerprint THEN
            RAISE EXCEPTION 'a finalized exam is immutable; it can only be archived' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    -- OLD.status = 'draft'
    IF NEW.status = 'archived' THEN
        IF NEW.finalized_at IS NOT NULL OR NEW.finalized_version IS NOT NULL THEN
            RAISE EXCEPTION 'an archived draft was never finalized' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status = 'draft' THEN
        RETURN NEW;  -- exams_state_check keeps every finalization column NULL
    END IF;
    -- draft -> finalized: the finalization completeness is verified here, in the database.
    IF NEW.finalized_version IS DISTINCT FROM NEW.version THEN
        RAISE EXCEPTION 'finalized_version must be the version produced by finalization' USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*), min(i.position), max(i.position), count(DISTINCT i.position), coalesce(sum(i.points), 0),
           count(*) FILTER (WHERE i.question_revision_no IS NULL)
      INTO n, lo, hi, distinct_positions, points, unpinned
      FROM exam_items i WHERE i.exam_id = NEW.id;
    IF n < 1 OR lo IS DISTINCT FROM 1 OR hi IS DISTINCT FROM n OR distinct_positions <> n OR unpinned <> 0
       OR NEW.item_count IS DISTINCT FROM n OR NEW.total_points IS DISTINCT FROM points THEN
        RAISE EXCEPTION 'a finalized exam needs items at positions 1..n, all pinned, with matching totals'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*) INTO not_current FROM exam_items i JOIN questions q ON q.id = i.question_id
     WHERE i.exam_id = NEW.id AND (q.status IS DISTINCT FROM 'approved'
                                   OR q.approved_revision_no IS DISTINCT FROM i.question_revision_no);
    IF not_current <> 0 THEN
        RAISE EXCEPTION 'every item of a finalized exam pins its question''s current approved revision'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*) INTO row_count FROM exam_blueprint_rows r WHERE r.exam_id = NEW.id;
    SELECT count(*) INTO unassigned FROM exam_items i WHERE i.exam_id = NEW.id AND i.blueprint_row_id IS NULL;
    SELECT count(*) INTO wrong_count FROM exam_blueprint_rows r
     WHERE r.exam_id = NEW.id
       AND r.question_count <> (SELECT count(*) FROM exam_items i WHERE i.exam_id = NEW.id AND i.blueprint_row_id = r.id);
    SELECT count(*) INTO mismatched
      FROM exam_items i
      JOIN exam_blueprint_rows r ON r.id = i.blueprint_row_id AND r.exam_id = i.exam_id
      JOIN exam_question_revisions v ON v.question_id = i.question_id AND v.revision_no = i.question_revision_no
     WHERE i.exam_id = NEW.id
       AND NOT ((r.topic_id IS NULL OR v.topic_id IS NOT DISTINCT FROM r.topic_id)
                AND (r.qtype IS NULL OR v.qtype = r.qtype)
                AND (r.difficulty IS NULL OR v.difficulty = r.difficulty)
                AND (r.bloom_level IS NULL OR v.bloom_level IS NOT DISTINCT FROM r.bloom_level));
    fulfilled := row_count > 0 AND unassigned = 0 AND wrong_count = 0 AND mismatched = 0;
    IF NEW.blueprint_status = 'none' AND row_count <> 0 THEN
        RAISE EXCEPTION 'blueprint status none requires an exam without blueprint rows' USING ERRCODE = 'check_violation';
    ELSIF NEW.blueprint_status = 'fulfilled' AND NOT fulfilled THEN
        RAISE EXCEPTION 'blueprint status fulfilled requires every row filled exactly with matching pinned revisions'
            USING ERRCODE = 'check_violation';
    ELSIF NEW.blueprint_status = 'deviation_accepted' AND (row_count = 0 OR fulfilled) THEN
        RAISE EXCEPTION 'blueprint status deviation_accepted requires a blueprint that is not fulfilled'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION studygraph_exam_items_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    parent text;
BEGIN
    -- FOR SHARE: finalizing or archiving the exam (UPDATE) waits for this transaction, and this write waits for a
    -- finalization in progress and then sees the new status.
    SELECT e.status INTO parent FROM exams e
     WHERE e.id = (CASE WHEN TG_OP = 'DELETE' THEN OLD.exam_id ELSE NEW.exam_id END) FOR SHARE;
    IF parent IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'exam items can only change while their exam is a draft' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.id <> OLD.id OR NEW.exam_id <> OLD.exam_id OR NEW.course_id <> OLD.course_id
                             OR NEW.question_id <> OLD.question_id OR NEW.purpose <> OLD.purpose
                             OR NEW.created_at <> OLD.created_at) THEN
        RAISE EXCEPTION 'the identity of an exam item is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION studygraph_exam_blueprint_rows_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    parent text;
BEGIN
    SELECT e.status INTO parent FROM exams e
     WHERE e.id = (CASE WHEN TG_OP = 'DELETE' THEN OLD.exam_id ELSE NEW.exam_id END) FOR SHARE;
    IF parent IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'blueprint rows can only change while their exam is a draft' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.id <> OLD.id OR NEW.exam_id <> OLD.exam_id OR NEW.course_id <> OLD.course_id) THEN
        RAISE EXCEPTION 'the identity of a blueprint row is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION studygraph_exam_audit_events_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'exam audit events are append-only' USING ERRCODE = 'check_violation';
END
$fn$;

CREATE FUNCTION studygraph_exam_truncate_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'TRUNCATE of % is refused; remove exam data with the documented procedure', TG_TABLE_NAME
        USING ERRCODE = 'check_violation';
END
$fn$;

CREATE FUNCTION studygraph_attempts_exam_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF (TG_OP = 'INSERT' OR NEW.question_id IS DISTINCT FROM OLD.question_id) AND EXISTS (
        SELECT 1 FROM questions q WHERE q.id = NEW.question_id AND q.purpose = 'exam'
    ) THEN
        RAISE EXCEPTION 'exam questions cannot be attempted' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER questions_bank_guard BEFORE INSERT OR UPDATE OR DELETE ON questions
    FOR EACH ROW EXECUTE FUNCTION studygraph_questions_bank_guard();
CREATE TRIGGER exam_question_revisions_guard BEFORE INSERT OR UPDATE OR DELETE ON exam_question_revisions
    FOR EACH ROW EXECUTE FUNCTION studygraph_exam_question_revisions_guard();
CREATE TRIGGER exams_guard BEFORE INSERT OR UPDATE OR DELETE ON exams
    FOR EACH ROW EXECUTE FUNCTION studygraph_exams_guard();
CREATE TRIGGER exam_items_guard BEFORE INSERT OR UPDATE OR DELETE ON exam_items
    FOR EACH ROW EXECUTE FUNCTION studygraph_exam_items_guard();
CREATE TRIGGER exam_blueprint_rows_guard BEFORE INSERT OR UPDATE OR DELETE ON exam_blueprint_rows
    FOR EACH ROW EXECUTE FUNCTION studygraph_exam_blueprint_rows_guard();
CREATE TRIGGER exam_audit_events_guard BEFORE UPDATE OR DELETE ON exam_audit_events
    FOR EACH ROW EXECUTE FUNCTION studygraph_exam_audit_events_guard();
CREATE TRIGGER attempts_exam_guard BEFORE INSERT OR UPDATE OF question_id ON attempts
    FOR EACH ROW EXECUTE FUNCTION studygraph_attempts_exam_guard();
CREATE TRIGGER exams_truncate_guard BEFORE TRUNCATE ON exams
    FOR EACH STATEMENT EXECUTE FUNCTION studygraph_exam_truncate_guard();
CREATE TRIGGER exam_items_truncate_guard BEFORE TRUNCATE ON exam_items
    FOR EACH STATEMENT EXECUTE FUNCTION studygraph_exam_truncate_guard();
CREATE TRIGGER exam_question_revisions_truncate_guard BEFORE TRUNCATE ON exam_question_revisions
    FOR EACH STATEMENT EXECUTE FUNCTION studygraph_exam_truncate_guard();
CREATE TRIGGER exam_audit_events_truncate_guard BEFORE TRUNCATE ON exam_audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION studygraph_exam_truncate_guard();
"""

DROP_TRIGGERS = """
DROP TRIGGER exam_audit_events_truncate_guard ON exam_audit_events;
DROP TRIGGER exam_question_revisions_truncate_guard ON exam_question_revisions;
DROP TRIGGER exam_items_truncate_guard ON exam_items;
DROP TRIGGER exams_truncate_guard ON exams;
DROP TRIGGER attempts_exam_guard ON attempts;
DROP TRIGGER exam_audit_events_guard ON exam_audit_events;
DROP TRIGGER exam_blueprint_rows_guard ON exam_blueprint_rows;
DROP TRIGGER exam_items_guard ON exam_items;
DROP TRIGGER exams_guard ON exams;
DROP TRIGGER exam_question_revisions_guard ON exam_question_revisions;
DROP TRIGGER questions_bank_guard ON questions;
DROP FUNCTION studygraph_attempts_exam_guard();
DROP FUNCTION studygraph_exam_truncate_guard();
DROP FUNCTION studygraph_exam_audit_events_guard();
DROP FUNCTION studygraph_exam_blueprint_rows_guard();
DROP FUNCTION studygraph_exam_items_guard();
DROP FUNCTION studygraph_exams_guard();
DROP FUNCTION studygraph_exam_question_revisions_guard();
DROP FUNCTION studygraph_questions_bank_guard();
"""


def _tz(name: str, nullable: bool = False, default: bool = True) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()") if default else None, nullable=nullable
    )


def upgrade() -> None:
    op.execute(UPGRADE_PREFLIGHT)

    # --- questions: the bank columns (nullable, no default: metadata-only in PostgreSQL 16) ------------------
    op.add_column("questions", sa.Column("version", sa.Integer(), nullable=True))
    op.add_column("questions", sa.Column("created_by", sa.UUID(), nullable=True))
    op.add_column("questions", _tz("updated_at", nullable=True, default=False))
    op.add_column("questions", sa.Column("approved_revision_no", sa.Integer(), nullable=True))
    op.add_column("questions", sa.Column("bloom_level", sa.String(length=20), nullable=True))
    op.create_foreign_key(op.f("questions_created_by_fkey"), "questions", "users", ["created_by"], ["id"])
    op.create_check_constraint(op.f("questions_bloom_level_check"), "questions", QUESTION_BLOOM_CHECK)
    op.create_check_constraint(op.f("questions_bank_check"), "questions", QUESTION_BANK_CHECK)

    # --- approval revisions ------------------------------------------------------------------------------------
    op.create_table(
        "exam_question_revisions",
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=20), server_default=sa.text("'exam'::character varying"), nullable=False),
        sa.Column("topic_id", sa.UUID(), nullable=True),
        sa.Column("qtype", sa.String(length=20), nullable=False),
        sa.Column("difficulty", sa.String(length=20), nullable=False),
        sa.Column("bloom_level", sa.String(length=20), nullable=True),
        sa.Column("stem", sa.Text(), nullable=False),
        sa.Column("choices", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("answer_key", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("source_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.UUID(), nullable=False),
        _tz("approved_at", default=False),
        _tz("created_at"),
        sa.CheckConstraint(REVISION_PURPOSE_CHECK, name=op.f("exam_question_revisions_purpose_check")),
        sa.CheckConstraint(REVISION_DOMAIN_CHECK, name=op.f("exam_question_revisions_domain_check")),
        sa.CheckConstraint(REVISION_CONTENT_CHECK, name=op.f("exam_question_revisions_content_check")),
        sa.ForeignKeyConstraint(
            ["question_id", "course_id", "purpose"],
            ["questions.id", "questions.course_id", "questions.purpose"],
            name="exam_question_revisions_question_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="exam_question_revisions_topic_fkey"
        ),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], name=op.f("exam_question_revisions_approved_by_fkey")),
        sa.PrimaryKeyConstraint("question_id", "revision_no", name=op.f("exam_question_revisions_pkey")),
    )
    # The circular key (the tables reference each other), MATCH SIMPLE: a never-approved question is not checked.
    op.create_foreign_key(
        "questions_approved_revision_fkey",
        "questions",
        "exam_question_revisions",
        ["id", "approved_revision_no"],
        ["question_id", "revision_no"],
    )

    # --- exams -------------------------------------------------------------------------------------------------
    op.create_table(
        "exams",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _tz("finalized_at", nullable=True, default=False),
        sa.Column("finalized_by", sa.UUID(), nullable=True),
        sa.Column("finalized_version", sa.Integer(), nullable=True),
        sa.Column("course_code_snapshot", sa.String(length=32), nullable=True),
        sa.Column("course_name_snapshot", sa.String(length=150), nullable=True),
        sa.Column("item_count", sa.SmallInteger(), nullable=True),
        sa.Column("total_points", sa.Integer(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("content_chars", sa.Integer(), nullable=True),
        sa.Column("blueprint_status", sa.String(length=20), nullable=True),
        sa.Column("blueprint_deviation_reason", sa.Text(), nullable=True),
        sa.Column("finalize_preview_fingerprint", sa.String(length=68), nullable=True),
        sa.Column("derived_from_exam_id", sa.UUID(), nullable=True),
        _tz("archived_at", nullable=True, default=False),
        _tz("created_at"),
        _tz("updated_at"),
        sa.CheckConstraint(EXAM_STATUS_CHECK, name=op.f("exams_status_check")),
        sa.CheckConstraint(EXAM_FIELDS_CHECK, name=op.f("exams_fields_check")),
        sa.CheckConstraint(EXAM_FINGERPRINT_CHECK, name=op.f("exams_fingerprint_check")),
        sa.CheckConstraint(EXAM_STATE_CHECK, name=op.f("exams_state_check")),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("exams_course_id_fkey")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("exams_created_by_fkey")),
        sa.ForeignKeyConstraint(["finalized_by"], ["users.id"], name=op.f("exams_finalized_by_fkey")),
        sa.ForeignKeyConstraint(
            ["derived_from_exam_id", "course_id"], ["exams.id", "exams.course_id"], name="exams_derived_from_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("exams_pkey")),
        sa.UniqueConstraint("id", "course_id", name=op.f("exams_id_course_id_key")),
    )
    op.create_index(
        op.f("exams_course_id_status_updated_at_id_idx"), "exams", ["course_id", "status", "updated_at", "id"]
    )

    # --- blueprint rows ------------------------------------------------------------------------------------------
    op.create_table(
        "exam_blueprint_rows",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("exam_id", sa.UUID(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("topic_id", sa.UUID(), nullable=True),
        sa.Column("qtype", sa.String(length=20), nullable=True),
        sa.Column("difficulty", sa.String(length=20), nullable=True),
        sa.Column("bloom_level", sa.String(length=20), nullable=True),
        sa.Column("question_count", sa.SmallInteger(), nullable=False),
        sa.Column("points_each", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint(BLUEPRINT_VALUES_CHECK, name=op.f("exam_blueprint_rows_values_check")),
        sa.ForeignKeyConstraint(
            ["exam_id", "course_id"], ["exams.id", "exams.course_id"], name="exam_blueprint_rows_exam_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["topic_id", "course_id"], ["topics.id", "topics.course_id"], name="exam_blueprint_rows_topic_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("exam_blueprint_rows_pkey")),
        # Non-deferrable: the target of exam_items_blueprint_row_fkey (a deferrable constraint cannot be one).
        sa.UniqueConstraint("id", "exam_id", name=op.f("exam_blueprint_rows_id_exam_id_key")),
        # Checked at commit (or SET CONSTRAINTS ... IMMEDIATE), so rows can swap positions in one transaction.
        sa.UniqueConstraint(
            "exam_id", "position", name="exam_blueprint_rows_exam_id_position_key", deferrable=True, initially="DEFERRED"
        ),
    )

    # --- exam items ----------------------------------------------------------------------------------------------
    op.create_table(
        "exam_items",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("exam_id", sa.UUID(), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=20), server_default=sa.text("'exam'::character varying"), nullable=False),
        sa.Column("question_revision_no", sa.Integer(), nullable=True),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("points", sa.SmallInteger(), nullable=False),
        sa.Column("blueprint_row_id", sa.UUID(), nullable=True),
        _tz("created_at"),
        _tz("updated_at"),
        sa.CheckConstraint(EXAM_ITEM_PURPOSE_CHECK, name=op.f("exam_items_purpose_check")),
        sa.CheckConstraint(EXAM_ITEM_VALUES_CHECK, name=op.f("exam_items_values_check")),
        sa.ForeignKeyConstraint(["exam_id", "course_id"], ["exams.id", "exams.course_id"], name="exam_items_exam_fkey"),
        sa.ForeignKeyConstraint(
            ["question_id", "course_id", "purpose"],
            ["questions.id", "questions.course_id", "questions.purpose"],
            name="exam_items_question_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["question_id", "question_revision_no"],
            ["exam_question_revisions.question_id", "exam_question_revisions.revision_no"],
            name="exam_items_revision_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["blueprint_row_id", "exam_id"],
            ["exam_blueprint_rows.id", "exam_blueprint_rows.exam_id"],
            name="exam_items_blueprint_row_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("exam_items_pkey")),
        sa.UniqueConstraint(
            "exam_id", "position", name="exam_items_exam_id_position_key", deferrable=True, initially="DEFERRED"
        ),
        sa.UniqueConstraint("exam_id", "question_id", name=op.f("exam_items_exam_id_question_id_key")),
    )
    op.create_index(op.f("exam_items_question_id_idx"), "exam_items", ["question_id"])

    # --- audit events and idempotency keys -----------------------------------------------------------------------
    op.create_table(
        "exam_audit_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=False),
        sa.Column("entity_kind", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _tz("created_at"),
        sa.CheckConstraint(AUDIT_SHAPE_CHECK, name=op.f("exam_audit_events_shape_check")),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("exam_audit_events_course_id_fkey")),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name=op.f("exam_audit_events_actor_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("exam_audit_events_pkey")),
    )
    op.create_index(
        "exam_audit_events_entity_idx", "exam_audit_events", ["course_id", "entity_kind", "entity_id", "created_at"]
    )
    op.create_index("exam_audit_events_replay_idx", "exam_audit_events", ["entity_id", "action", "entity_version"])

    op.create_table(
        "exam_idempotency_keys",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.UUID(), nullable=False),
        sa.Column("operation", sa.String(length=30), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=False),
        _tz("created_at"),
        sa.CheckConstraint(IDEMPOTENCY_CHECK, name=op.f("exam_idempotency_keys_shape_check")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("exam_idempotency_keys_user_id_fkey")),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], name=op.f("exam_idempotency_keys_course_id_fkey")),
        sa.PrimaryKeyConstraint("user_id", "idempotency_key", name=op.f("exam_idempotency_keys_pkey")),
    )

    op.execute(TRIGGER_FUNCTIONS)


def downgrade() -> None:
    """Remove everything upgrade() added, only if no M9 state would be lost."""
    op.execute(DOWNGRADE_GUARD)
    op.execute(DROP_TRIGGERS)
    op.drop_table("exam_idempotency_keys")
    op.drop_index("exam_audit_events_replay_idx", table_name="exam_audit_events")
    op.drop_index("exam_audit_events_entity_idx", table_name="exam_audit_events")
    op.drop_table("exam_audit_events")
    op.drop_index(op.f("exam_items_question_id_idx"), table_name="exam_items")
    op.drop_table("exam_items")
    op.drop_table("exam_blueprint_rows")
    op.drop_index(op.f("exams_course_id_status_updated_at_id_idx"), table_name="exams")
    op.drop_table("exams")
    op.drop_constraint("questions_approved_revision_fkey", "questions", type_="foreignkey")
    op.drop_table("exam_question_revisions")
    op.drop_constraint(op.f("questions_bank_check"), "questions", type_="check")
    op.drop_constraint(op.f("questions_bloom_level_check"), "questions", type_="check")
    op.drop_constraint(op.f("questions_created_by_fkey"), "questions", type_="foreignkey")
    for column in ("bloom_level", "approved_revision_no", "updated_at", "created_by", "version"):
        op.drop_column("questions", column)
