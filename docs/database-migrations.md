# Database migrations

The core-api database schema is managed by [Alembic](https://alembic.sqlalchemy.org/).
Every schema change is a versioned revision under
`services/core-api/migrations/versions/`, applied with `alembic upgrade`.
Schema changes go through CE-1 (see `CONTRIBUTING.md`).

| Path (under `services/core-api/`) | What it is |
| --- | --- |
| `alembic.ini` | Alembic settings. Deliberately has no database URL. |
| `migrations/env.py` | Connects to `DATABASE_URL` after the safety checks below. |
| `migrations/versions/0001_baseline_schema.py` | Baseline: the 11 original tables. |
| `migrations/versions/0002_document_upload_ingestion.py` | Upload metadata on `documents`, `ingestion_jobs` (M2). |
| `app/migration_safety.py` | Refuses unsafe migration targets. |
| `app/db.py` | `Base` with the constraint naming convention. |
| `tests/` + `scripts/test_migrations.sh` | Migration tests on a throwaway database. |
| `tests/fixtures/scaffold_schema.{json,sql}` | The original `init_db.py` schema: catalog snapshot and normalized `pg_dump`. |
| `scripts/normalize_schema_dump.py` | Normalizes `pg_dump --schema-only` output for comparison. |

`init_db.py` (`Base.metadata.create_all()`) is **deprecated**. It now exits with an
error and creates nothing: tables created that way are invisible to Alembic.

## Ground rules

- Run Alembic with the **`migrate` tool** (`docker compose run --rm migrate alembic ...`), the only
  service whose `DATABASE_URL` is the schema owner. The API and workers connect as `studygraph_app`,
  which cannot run DDL ([database-security.md](database-security.md)). After every migration, re-apply
  the runtime grants (`docker compose run --rm db-init`).
- Never create or alter tables by hand or with `create_all()`.
- Never edit a revision that has been merged; add a new one.
- Revisions are self-contained: they must not import `app.models` (the models keep
  changing; old revisions must not).

If you run Compose with extra `-f` files (for example a local, untracked override),
pass the same files to every command below. Run them from the checkout that owns the stack,
so that they reach its project (`COMPOSE_PROJECT_NAME`, or the folder name by default) and its
database volume; `docker compose ls` shows which projects exist
([database-security.md](database-security.md#development-docker-compose)).

## Revision 0009 rollout

Revision 0009 quarantines previously indexed documents and preserves their derived rows and job history. Plan the maintenance window, backup and legacy inventory before upgrading a populated database. Workers require revision 0009. Production indexing stays disabled pending role provisioning, real-provider verification and joint approval. [Index persistence and rollout](ai-indexing-integration.md#8-durable-postgresql-handoff-revision-0009)

## Revision 0010 rollout

Revision 0010 (WP-5 / F10) adds one nullable column, `external_resources.duplicate_content_sha256`,
with a CHECK that allows it only on `accepted` resources, as a lowercase SHA-256 hex digest,
never equal to the row's own `content_sha256`. It is metadata-only: no backfill, no table rewrite,
no document or index change. It records the text digest of an accepted resource's newest checked
revision when that text duplicates content another accepted resource holds. The resource keeps
its visible version and its `content_sha256`, the revision is not fetched again while the text is
held, and it is reconsidered as soon as nobody holds it (`app/harvest/persist.py`).

- **Deploy order.** Run `alembic upgrade head` before starting the new core-api and workers.
  `REQUIRED_SCHEMA_REVISION` is `0010`: until the database is there, `GET /health/ready` answers
  503 `not_ready` and the worker preflight refuses to start. Code at 0009 does not know the
  column, so roll the schema and the code forward together.
- **Downgrade** to 0009 drops the column. Resources that had a recorded duplicate revision
  get `last_revision_id = NULL`, `duplicate_of_id = NULL` and are due now, so 0009 code fetches
  and decides them again. No document, version or index row is touched.
- Test 0010 only on disposable infrastructure (`scripts/test_migrations.sh`); `tests/test_migration_0010.py`
  covers the CHECK and the downgrade.

## Revision 0011 rollout

Revision 0011 (WP-6, OpenStax connector) makes room for a second provider without changing any Wikipedia row:

- `sources.provider_book` (nullable): the allowlisted OpenStax book of an `openstax` source. `sources_provider_check`
  now admits `openstax` only with language `en`, a licence URL and a book slug; Wikipedia keeps its rule (and a
  NULL book). The unique index on `(provider, language)` is unchanged: one OpenStax source (one book) for now.
- Trigger `sources_openstax_insert_disabled`: every inserted `openstax` source row is stored with
  `enabled = false`, whatever the INSERT says. Activation is only the explicit UPDATE of `enable-source`.
- `external_resources.last_revision_key` and `external_document_versions.revision_key` (nullable, 40 lowercase
  hex): OpenStax's version identity, the git blob SHA-1 of the module file. `external_document_versions.revision_id`
  becomes nullable; its identity CHECK requires exactly one of a positive `revision_id` (Wikipedia) or a
  `revision_key` (OpenStax). A resource never has both.

Metadata-only: no backfill, no rewrite of existing rows.

- **Deploy order.** `alembic upgrade head` before the new core-api and workers; `REQUIRED_SCHEMA_REVISION` is
  `0011`. Revision 0011 depends on 0010 (WP-5, PR #23): both ship together, in order.
- **Downgrade** to 0010 is refused (the migration raises, nothing changes) while any OpenStax source, resource or
  version exists, because 0010 cannot represent them and provenance is never deleted silently. Remove that state
  deliberately first; without it the downgrade restores the 0010 constraints and drops the columns and trigger.
- Test 0011 only on disposable infrastructure; `tests/test_migration_0011.py` covers the refusal and the round trip,
  `tests/test_harvest_openstax.py` the constraints and the trigger.

## Revision 0012 rollout

Revision 0012 (WP-9, admin mutations) adds one table and changes nothing else:

- `admin_audit_events`: one row per committed admin mutation (docs/admin-api.md). Trigger
  `admin_audit_events_guard` refuses an inserted row whose actor is not an active admin and refuses UPDATE and
  DELETE; `admin_audit_events_truncate_guard` refuses TRUNCATE. `actor_id` references `users`; the audited
  entity and created run/job have no foreign key, so history survives them.

- **Deploy order.** `alembic upgrade head` before the new core-api and workers; `REQUIRED_SCHEMA_REVISION` is
  `0012`.
- **Least-privilege runtime (WP-8).** After the upgrade, re-run `scripts/db_privileges.sql`; the runtime group
  needs only SELECT and INSERT on `admin_audit_events` (the trigger already refuses the rest). The mutations
  also lock the admin's `users` row `FOR SHARE`, which needs the UPDATE privilege on `users` the runtime group
  already holds.
- **Downgrade** to 0011 is refused while any audit event exists (history is never dropped silently). Remove them
  deliberately first, as the table owner in one transaction: disable `admin_audit_events_guard`, delete, enable it.
- Test 0012 only on disposable infrastructure; `tests/test_migration_0012.py` covers the refusal and the round
  trip, `tests/test_admin_mutations.py` the triggers.

## Revision 0013 rollout

Revision 0013 (WP-10, document withdrawal, [document-withdrawal.md](document-withdrawal.md)) changes
`documents` only and rewrites no row:

- `documents_status_check` gains `withdrawn`; new nullable columns `withdrawn_at` (timestamptz) and
  `withdrawn_by` (→ `users`); `documents_withdrawal_check` allows `withdrawn` only on a course's class upload
  and only with both columns set (and neither column on other rows).
- Trigger `documents_withdrawal_final` (function `studygraph_documents_withdrawal_final`) refuses any UPDATE of
  a row that is already withdrawn. DELETE is not affected.
- `documents_course_id_content_hash_idx` is rebuilt with `AND status <> 'withdrawn'`, so a withdrawn file can be
  uploaded again. The rebuild and the constraint changes run under one `ACCESS EXCLUSIVE` lock on `documents`.

- **Deploy order.** `alembic upgrade head` before the new core-api and workers; `REQUIRED_SCHEMA_REVISION` is
  `0013`.
- **Least-privilege runtime (WP-8).** No grant change: table-level DML on `documents` covers the new columns,
  the withdrawal locks `users`/`courses` `FOR SHARE` as an upload does, and a trigger function needs no
  EXECUTE grant to fire. Re-run `scripts/db_privileges.sql` after the upgrade as usual.
- **Downgrade** to 0012 is refused while any withdrawn document exists (the old CHECK and unique index cannot
  hold them). Remove them deliberately first, as the owner: `DELETE FROM documents WHERE status = 'withdrawn'`
  (jobs, sections, the index generation and its chunks cascade; legacy chunks without a generation must be
  removed first, as for any document). Their stored files are not removed by SQL.
- Test 0013 only on disposable infrastructure; `tests/test_wp10_withdrawal.py` covers the refusal, the round
  trip and the trigger as the runtime role.

## Everyday commands

```bash
docker compose up -d --build                       # start the stack (rebuild after dependency changes)
docker compose run --rm migrate                    # create / update the schema, as the schema owner
docker compose run --rm db-init                    # then re-apply the runtime grants (F05)
docker compose run --rm migrate alembic current   # revision the database is at; "(head)" when up to date
docker compose run --rm migrate alembic history   # all revisions
docker compose run --rm migrate alembic check     # models vs. database: expect "No new upgrade operations detected."
docker compose run --rm migrate alembic upgrade head --sql   # print the SQL only, connect to nothing
```

A brand-new database volume needs nothing but `alembic upgrade head` (then `db-init`).
The API and workers connect as the unprivileged `studygraph_app` login, which cannot run DDL:
`docker compose exec core-api alembic upgrade head` is refused with `permission denied for schema public`.
Read-only commands (`current`, `history`, `check`, `--sql`) still work there. See
[database-security.md](database-security.md).

**A database that already holds data** is upgraded as an explicit rollout, never by recreating it:
take a backup and confirm it restores, read the SQL (`--sql`), check the rollout notes of every revision
between its current revision and head (above), then `migrate`, `db-init` and restart the API and workers.
Staging and production use their own owner and runtime credentials, never the Compose defaults or `db-init`
([database-security.md](database-security.md#staging-and-production-rollout)).
A database created earlier by `init_db.py` needs adopting first, see below. Running
`upgrade` on it fails with `relation "users" already exists` and changes nothing
(PostgreSQL runs the whole migration in one transaction).

## Revision 0002: upload metadata and ingestion jobs

Adds to `documents` the nullable columns `uploaded_by`, `original_filename`,
`storage_key`, `mime_type`, `size_bytes`, `page_count`, `kind` and
`error_message` (no defaults, so no table rewrite, and legacy rows stay as they
are), CHECK constraints, the course-scoped dedup index and the `ingestion_jobs`
table. See [document-ingestion.md](document-ingestion.md) for what they mean.

**Upgrade preflight.** New CHECK constraints restrict `documents.origin` to
`class | external | web_cache` and `documents.status` to
`pending | indexed | failed | stale`. Before any change, the migration counts
existing rows outside those values and, if there are any, stops with the counts
(never the values). It does not modify data. Check a real database read-only
before upgrading it:

```bash
docker compose exec -T db psql -U studygraph -d studygraph -X -At -c \
  "SELECT count(*) FILTER (WHERE origin NOT IN ('class','external','web_cache')),
          count(*) FILTER (WHERE status NOT IN ('pending','indexed','failed','stale'))
   FROM documents"
# expect: 0|0
```

**Downgrade guard.** `alembic downgrade 0001` (or `base`) is refused, before any
change, while `ingestion_jobs` has rows or any document has upload metadata:
dropping the columns would silently lose that data and leave the stored files
unreferenced. There is deliberately no override flag. To downgrade, remove that
data on purpose first (and decide what happens to the stored files). Without such
data the downgrade restores the baseline schema exactly.

**Offline SQL.** Both checks are `DO` blocks, so `alembic upgrade --sql` /
`downgrade --sql` output contains them and they run wherever that SQL is executed
later. Run that output as a script (`psql -f`); it contains `%` placeholders for
PL/pgSQL `RAISE`, so do not pass it through a driver that interprets `%`.

## Revision 0003: document sections, job scheduling and fencing, storage identity

Adds to `ingestion_jobs` the columns `attempt_token` (uuid), `lease_expires_at`,
`last_dispatched_at` (nullable `timestamptz`) and `available_at` (`timestamptz NOT
NULL DEFAULT now()`; PostgreSQL stores the value once, so the table is not rewritten
and existing jobs become dispatchable at once), the CHECK `ingestion_jobs_lease_check`
(a running job has a token and a lease after its start), and two tables:
`document_sections` (ordered, page-aware normalized text, `ON DELETE CASCADE` from
`documents`, unique `(document_id, ordinal)`, CHECKs on ordinal, text and locator) and
`storage_identity` (one row). `documents` is unchanged. See
[document-processing.md](document-processing.md) for what they mean.

**Upgrade preflight.** The migration locks `ingestion_jobs` against concurrent writes
(`SHARE ROW EXCLUSIVE`, until it commits) and stops before any change if a job is
`running`: such a job has no attempt token or lease to give it. M2 never sets jobs
running, so a real M2 database passes. Check read-only before upgrading:

```bash
docker compose exec -T db psql -U studygraph -d studygraph -X -At -c \
  "SELECT count(*) FROM ingestion_jobs WHERE status = 'running'"
# expect: 0
```

**Downgrade guard.** `alembic downgrade 0002` (or lower) is refused, before any change,
while `document_sections` has rows, a job carries attempt or dispatch state (`running`,
`attempt_token`, `lease_expires_at` or `last_dispatched_at` set), a queued job is
scheduled for a later retry (`available_at` in the future: the backoff would be lost),
or a storage identity is recorded. Remove that state on purpose first. Without it the
downgrade restores the 0002 schema exactly.

**Offline SQL.** Both checks are `DO` blocks, as for 0002.

**Order.** Migrate before starting the M3 workers: they check the revision at start and
exit (status 78) otherwise. After migrating, bind the upload store once with
`python -m app.ingestion.admin init-storage --database <name>`.

## Revision 0004: course code and course/enrollment indexes (M5)

Adds `courses.code` (`VARCHAR(32) NOT NULL DEFAULT ''`; stored once, no table rewrite)
with the CHECK `courses_code_check` (trimmed, no control characters), and the indexes
`courses_owner_id_idx`, `enrollments_course_id_idx` and `topics_course_id_idx` for the
M5 course list, student counts and topic list. Existing courses get `''`, the
documented "no code recorded" fallback ([courses-api.md](courses-api.md)). Nothing
else changes: nullability was checked against the scaffold (all relevant columns are
already `NOT NULL`, `join_code` is already unique), so no constraint is tightened and
no preflight on existing data is needed. The upgrade first locks `courses`,
`enrollments` and `topics` (`SHARE ROW EXCLUSIVE`, fixed order, until commit).

**Downgrade guard.** `alembic downgrade 0003` (or lower) is refused, before any change,
while any course has a non-empty `code` (it would be lost). Clear codes on purpose
first. Without codes the downgrade restores the 0003 schema exactly.

**Order.** Migrate before starting workers: they now require revision `0004`.

## Revision 0005: external harvesting (M6)

Adds provider columns to `sources` (`provider`, `language`, `licence_url`; NULL for
every existing row, CHECK `sources_provider_check` admits only `wikipedia`, unique per
provider+language among provider rows), `documents.external_resource_id` (NULL for every
existing row; CHECK `documents_external_resource_check`; partial unique indexes
`documents_external_resource_indexed_idx` / `…_pending_idx`), widens
`document_sections_locator_check` to also accept section locators, and creates
`harvest_course_settings`, `harvest_targets`, `harvest_queries`,
`harvest_provider_budgets`, `harvest_runs`, `external_resources` and
`external_document_versions` ([external-harvesting.md](external-harvesting.md)). Nothing
is seeded. Every new unique constraint covers only new tables or columns that are NULL
in every existing row, so no legacy row can violate it; the preflight (`DO
$migration_0005_preflight$`, after locking `sources`, `documents`,
`document_sections`) additionally verifies that every existing section satisfies the
page-locator rule before the CHECK is replaced. Existing values are never modified.

**Referential integrity between the two mutually dependent tables.** `documents` and
`external_resources` reference each other, so the keys are created in dependency order and
dropped in reverse. Upgrade: `external_resources` (with the unique key
`external_resources_id_course_id_source_id_key`) → `documents.external_resource_id` with the
unique key `documents_id_external_resource_id_key` and the composite key
`documents_external_resource_id_fkey` `(external_resource_id, course_id, source_id) →
external_resources (id, course_id, source_id)` (a harvested document can only reference a
resource of its own course and source) → last, once both tables exist,
`external_resources_current_document_id_fkey` `(current_document_id, id) → documents (id,
external_resource_id)` (the current version must exist and belong to the same resource).
Both are `MATCH SIMPLE`, `NO ACTION`: a NULL `current_document_id` (no version yet) or a
class document (no resource) is not checked. The model declares the second key with
`use_alter=True`. Downgrade drops `external_resources_current_document_id_fkey` first. Revision
0005 is not deployed; it was edited in place and no new revision was introduced.

**Downgrade guard.** `alembic downgrade 0004` (or lower) is refused, before any change,
while any harvesting row, provider-managed source, document linked to a resource or
section with a section locator exists. Without M6 state the downgrade restores 0004
exactly (tested against a snapshot).

**Order.** Migrate before starting workers: they now require revision `0005`.

## Revision 0006: quizzes, graded attempts and practice statistics (M7)

Adds `quiz_sessions`, `quiz_items`, `topic_practice_stats` and `quiz_generation_quotas`;
the generation columns of `questions` (`hint`, `answer_key`, `grading_method`,
`generated_by_session_id`, `generated_for_user_id`, `generation`, `content_hash`) and the
grading columns of `attempts` (`course_id`, `quiz_item_id`, `answer`, `grading_method`,
`feedback`, `mastery_before`, `mastery_after`, `mastery_method`, `stats_attempts`,
`stats_correct`, `submitted_at`); `topics_id_course_id_key`, the target of every
course-consistent topic key ([quiz-api.md](quiz-api.md), "Data model").

- **Legacy rows.** Every new questions/attempts column is NULL for existing rows except
  `attempts.course_id`, which is backfilled from `questions.course_id` (deterministic; the
  question is NOT NULL) and then made NOT NULL. A legacy-style `INSERT INTO attempts`
  without `course_id` still works: the attempts trigger derives it the same way. New CHECKs
  on `questions.purpose/qtype/difficulty/status` admit every value the application ever
  wrote (`practice|exam`, `mcq|true_false|short`, `easy|medium|hard`,
  `draft|approved|generated|retired`).
- **Preflight** (`DO $migration_0006_preflight$`, after locking `topics`, `questions`,
  `attempts`): stops before any change while a question carries an unknown
  purpose/type/difficulty/status (or already says `generated`), or a question or attempt
  references a topic of another course. Inspect and correct such rows first.
- **NULL semantics.** A CHECK passes when it evaluates to UNKNOWN. The generated branch of
  `questions_generated_check` and the graded branch of `attempts_practice_check` are therefore
  `(...) IS TRUE` (a NULL `grading_method`, `mastery_method` or an `answer_key` without
  `choice_id` is refused); the legacy branches only use `IS [NOT] NULL` on NOT NULL columns.
  A generated question's `answer_key` must be exactly `{"choice_id": <id of one of its
  choices>}`. The composite keys are `MATCH SIMPLE`: they are not checked for legacy rows (whose new columns
  are NULL) and fully checked for M7 rows, whose CHECKs require every key column.
- **Triggers** (not part of the reflected-schema comparison; tested in
  `tests/test_migration_0006.py`): `questions_guard`, `quiz_items_guard`,
  `quiz_sessions_guard`, `attempts_guard` with the functions `studygraph_*_guard()`.
  Generated questions, assignments, completed sessions and graded attempts cannot be
  updated or deleted; legacy questions and attempts stay editable. A generated question can only
  be answered through its assignment in a `ready` session: a legacy-style `INSERT` naming it, or an
  `UPDATE` moving a legacy attempt onto it, is refused. A session cannot be inserted `ready`
  (`quiz_sessions_insert_guard`, AFTER INSERT so its own CHECKs are reported first). Question and
  assignment inserts lock the session row `FOR SHARE`, and finalization is an `UPDATE` of that row,
  so adding rows and finalizing cannot interleave around the state guards (tested with two
  concurrent transactions).
- **Deliberate removal** of M7 rows (e.g. a data-protection request) is an operator task:
  as the table owner, in one transaction, `ALTER TABLE ... DISABLE TRIGGER <guard>`, delete
  in the order attempts, topic_practice_stats, quiz_generation_quotas, quiz_items, questions,
  quiz_sessions, then `ENABLE TRIGGER` again (`tests/api_support.py::remove_quiz_state`).
- **Downgrade guard.** `alembic downgrade 0005` (or lower) is refused, before any change,
  while any quiz session, assignment, practice statistic, quota row, graded attempt or
  generated/M7-annotated question exists. Without M7 state the downgrade restores the 0005
  contents exactly (tested with a checksum of every legacy table).

**Order.** Migrate before starting workers: they now require revision `0006`. The running
M6 development environment stays at 0005 until it is deliberately migrated.

## Revision 0007: mastery outbox and delivery, provenance, study events, analytics releases (M8)

Tables: 25 before, **30 after** (`mastery_streams`, `mastery_outbox`, `mastery_model_state`,
`study_events`, `analytics_weekly_releases`). No quota table: the study-event quota is an
advisory transaction lock plus a count over an index ([study-events.md](study-events.md)).
Changed: `attempts` (UNIQUE `attempts_mastery_event_key (id, user_id, course_id, topic_id,
stats_attempts)`, the outbox key target; partial UNIQUE `attempts_topic_sequence_idx (user_id,
topic_id, stats_attempts) WHERE quiz_item_id IS NOT NULL`; partial index
`attempts_graded_course_submitted_idx`), `documents` (UNIQUE `documents_id_course_id_key`, the
target of the study-event document key), `mastery_scores` (`course_id` backfilled from the topic
and NOT NULL, composite topic key, provenance columns, `next_review_at` now `timestamptz`, values
read as UTC, `mastery_scores_provenance_check`). Details: [ai-mastery-integration.md](ai-mastery-integration.md).

- **Preflight** (`DO $migration_0007_preflight$`). Locks `topic_practice_stats, attempts IN
  SHARE ROW EXCLUSIVE MODE` in M7's own lock order (statistics row, then attempt insert), held
  until the migration commits: no graded attempt can commit between the checks, the backfill and
  the new triggers, and an attempt transaction already open is waited for, not lost (tested with
  an open transaction during `alembic upgrade`). Stops before any change when a graded attempt
  sequence of a (student, topic) repeats or has a gap (`count <> max(stats_attempts)` or
  `min <> 1`), when `stats_correct` is not the running count of correct answers, or when
  `topic_practice_stats` disagrees with the graded attempts (count, correct count, course, or a
  row without attempts). Reconcile such rows first; the message gives the counts only.
- **Backfill.** One `mastery_outbox` row (`origin = 'backfill'`, `pending`) per existing graded
  attempt and one `mastery_streams` row per (student, course), `ON CONFLICT DO NOTHING`, then a
  count check (graded attempts = outbox rows) that aborts the migration otherwise. Legacy
  attempts (no assignment) never get an event. Existing values are not changed, except the new
  `mastery_scores.course_id` and the type of `next_review_at`. Legacy `mastery_scores` rows keep
  their values and have no provenance: they are never shown as model output.
- **Triggers** (outside the reflected-schema comparison; `tests/test_migration_0007.py`):
  `attempts_mastery_sequence_guard` and `attempts_outbox_enqueue` (AFTER INSERT of a graded
  attempt, so the row's CHECKs and M7's `attempts_guard` report first), `mastery_outbox_guard`
  (BEFORE UPDATE), `analytics_weekly_releases_guard` (BEFORE UPDATE). The sequence guard locks the
  statistics row (already locked by the grading path) and requires the attempt to carry exactly
  its counters and to continue the previous graded attempt of the topic; legacy attempts,
  statistics updates by an administrator and outbox deletes are not restricted (no broad
  trigger). An administrator who corrects `topic_practice_stats` must keep it equal to the graded
  attempts, otherwise the next graded attempt of that topic is refused (fail closed).
- **NULL semantics.** Every multi-branch CHECK is `(...) IS TRUE`; the expressions live in
  `app/schema_checks.py` and are asserted identical to the migration's copies.
- **Downgrade guard.** `alembic downgrade 0006` is refused, before any change, while study
  events, analytics releases, model predictions (`mastery_scores` with provenance) or delivery
  state (outbox rows that are not `pending`, streams that are not plain `ready` or carry a batch,
  claims or operator retries) exist. Pending outbox rows do not block it: a later upgrade
  regenerates them from the immutable attempts. The downgrade turns `next_review_at` back into a
  naive UTC timestamp.
- **Deliberate removal** of M8 rows: delete `study_events`, `analytics_weekly_releases`,
  `mastery_outbox`, M8 `mastery_scores`, `mastery_streams` in that order (outbox rows reference
  attempts and streams), and reset `mastery_model_state` to `unknown`
  (`tests/api_support.py::remove_m8_state`). Releases cannot be UPDATEd; delete only deliberately
  (a deleted week would be regenerated from current data by the next tick).

**Order.** Migrate before starting workers: every worker now requires revision `0007` and exits
(status 78) otherwise. The running M7 development environment stays at 0006 until it is
deliberately migrated; upgrading it is an operator step, not part of M8 delivery.

## Revision 0008: professor question bank, exams, blueprints, revisions, audit (M9-A1)

Tables: 30 before, **36 after** (`exam_question_revisions`, `exams`, `exam_blueprint_rows`,
`exam_items`, `exam_audit_events`, `exam_idempotency_keys`). Changed: `questions` gains five
nullable columns without default (`version`, `created_by`, `updated_at`, `approved_revision_no`,
`bloom_level`; metadata-only `ADD COLUMN`s), `questions_bloom_level_check`, `questions_bank_check`
and the composite key `questions_approved_revision_fkey (id, approved_revision_no) ->
exam_question_revisions (question_id, revision_no)` (added after that table; the two reference
each other, `use_alter` in the ORM). API and rules: [exam-api.md](exam-api.md),
[exam-export.md](exam-export.md).

- **The M9 marker.** A bank question is exactly `questions.version IS NOT NULL` (always
  `purpose = 'exam'`, status `draft | approved | retired`). Every new CHECK on `questions` is TRUE
  for every legacy and M7 row through its first branch, so no existing row, test or error name
  changes (PostgreSQL evaluates CHECKs in name order). Legacy `purpose = 'exam'` rows stay as they
  are and are invisible to the exam API.
- **Preflight** (`DO $migration_0008_preflight$`). Locks `questions, attempts IN SHARE ROW
  EXCLUSIVE MODE` until commit and reports (NOTICE, counts only) legacy exam-purpose questions
  and attempts on them. Nothing existing can violate a new constraint, so it never refuses.
- **Keys.** Revisions, items and blueprint rows carry `course_id` on every path: items reference
  `(question_id, course_id, purpose)` with `purpose` CHECKed to `'exam'` (an item can never name a
  practice question or one of another course), `(exam_id, course_id)`, the pinned revision
  `(question_id, question_revision_no)` and a blueprint row of the same exam
  `(blueprint_row_id, exam_id) -> exam_blueprint_rows (id, exam_id)`. Positions:
  `exam_items_exam_id_position_key` and `exam_blueprint_rows_exam_id_position_key` are
  `UNIQUE (exam_id, position) DEFERRABLE INITIALLY DEFERRED` (checked at commit or at
  `SET CONSTRAINTS ... IMMEDIATE`, so rows and items can be reordered in one transaction);
  `exam_blueprint_rows_id_exam_id_key UNIQUE (id, exam_id)` stays non-deferrable because it is a
  foreign-key target. No M9 statement uses `ON CONFLICT` on a position constraint.
- **Triggers** (outside the reflected-schema comparison; `tests/test_migration_0008.py`):
  `questions_bank_guard` (fires before M7's `questions_guard`; inserted as draft version 1, version
  +1 per UPDATE, immutable identity, documented transitions only, approval/retirement change no
  content, `approved` needs the newest revision with the same hash, no DELETE, a legacy row can
  never become a bank row), `exam_question_revisions_guard` (locks the question FOR UPDATE, draft
  only, contiguous numbers, exact copy of the working copy incl. the `(document_id, locator)`
  sequence; no UPDATE/DELETE), `exams_guard` (no DELETE, identity immutable, version +1, only
  draft->draft|finalized|archived and finalized->archived, which changes nothing but status,
  `archived_at`, version and `updated_at`; at draft->finalized it verifies items 1..n, every item
  pinned to its question's current approved revision, totals, `finalized_version` and the
  blueprint decision against the pinned revisions), `exam_items_guard` and
  `exam_blueprint_rows_guard` (writes only while the exam is a draft, parent FOR SHARE; identity
  immutable), `exam_audit_events_guard` (append-only), `attempts_exam_guard` (fires before M7's
  `attempts_guard`: no attempt can be inserted on, or moved to, an exam-purpose question; existing
  rows are untouched) and statement-level `*_truncate_guard` on `exams`, `exam_items`,
  `exam_question_revisions`, `exam_audit_events`. No M7/M8 function is changed.
- **NULL semantics.** Every multi-branch CHECK is `(...) IS TRUE`; `exams_state_check` compares
  the deviation status with `IS NOT DISTINCT FROM`, so a draft (both NULL) passes and nothing
  slips through as "unknown". The expressions live in `app/schema_checks.py` and are asserted
  identical to the migration's copies (MIG-04).
- **Downgrade guard** (`DO $migration_0008_downgrade_guard$`, ACCESS EXCLUSIVE locks).
  `alembic downgrade 0007` is refused, before any change, while any row exists in the six new
  tables or any question has `version` or `bloom_level` set. Otherwise it removes exactly what
  upgrade added (catalog snapshot, triggers, functions and data checksums equal to 0007, MIG-05).
- **Limits of the guarantees.** Triggers do not hold against the table owner or a superuser who
  disables them (`ALTER TABLE ... DISABLE TRIGGER`, `session_replication_role = replica`), drops
  constraints, restores dumps or edits catalogs; TRUNCATE is refused by the statement triggers.
- **Deliberate removal of exam data** (operator only, as the table owner, in ONE transaction):
  disable `exam_audit_events_guard`, `exam_items_guard`, `exam_blueprint_rows_guard`,
  `exams_guard`, `exam_question_revisions_guard`, `questions_bank_guard`; delete
  `exam_audit_events`, `exam_idempotency_keys`, `exam_items`, `exam_blueprint_rows`, `exams`; set
  `approved_revision_no = NULL, status = 'draft'` on the bank questions; delete
  `exam_question_revisions`, then the bank questions; enable the six guards. The test helper
  `tests/api_support.py::remove_m9_state` is exactly this procedure (it keeps the runner's final
  `downgrade base` working).

**Order.** Migrate before starting workers or the M9 API: every worker now requires revision
`0008` and exits (status 78) otherwise. The running M8 development environment stays at 0007
until it is deliberately migrated (adoption plan: M9_IMPLEMENTATION_TEST_PLAN.md §8, explicit
go-ahead required); with every M9 switch off no M9 state can be created, so a downgrade stays
possible.

## Safety guard: which database a migration may touch

`app/migration_safety.py` holds the checks; `migrations/env.py` applies them.

**Before connecting:**

1. `DATABASE_URL` must be set in the environment. Unlike the app, migrations
   never read a `.env` file or fall back to a built-in default.
2. It must be a PostgreSQL URL with:
   - an explicit **user**;
   - **one** plain host name or canonical IP address;
   - a valid port;
   - a database name.

   Refused: multi-host lists (`db,replica`), socket paths, percent-encoded hosts
   and non-canonical numeric IPv4 forms. libpq resolves `127.1`, `2130706433`,
   `0x7f000001` and `0177.0.0.1` to `127.0.0.1`.
3. Only query parameters that cannot change the server, database, role or schema
   are accepted:
   - TLS (`sslmode`, `sslrootcert`, ...);
   - timeouts and keepalives;
   - `application_name`.

   `host`, `hostaddr`, `port`, `dbname`, `user`, `service`, `options`,
   `target_session_attrs` and any unknown key are refused.
4. The driver always gets an **explicit port** (5432 when the URL has none).
   Explicit host, port, user and database take precedence over `PGHOST`, `PGPORT`,
   `PGUSER` and `PGDATABASE`. libpq would still apply `PGHOSTADDR`, `PGSERVICE`
   and `PGOPTIONS` on top of the URL, so the command refuses to run while any of
   them is set.
5. Hosts that mean "this machine" are refused:
   - `localhost`, `*.localhost`;
   - loopback and unspecified IPs in any spelling (including `::ffff:127.0.0.1`);
   - Docker Desktop's `host.docker.internal` / `gateway.docker.internal`.

   A developer machine may run an unrelated PostgreSQL server there. If you really
   mean a local server (for example a disposable CI service container published on
   the runner), set `MIGRATIONS_ALLOW_LOOPBACK_PORT` to exactly that port; it opens
   that one port only.

**On the migration's own connection, before any DDL:**

6. `current_database()` must be the URL's database.
7. `current_user` and `session_user` must be the URL's user, so no role is
   switched in by role or database defaults.
8. `current_schema()` must be `public`, so no `search_path` redirect is active.
9. If `MIGRATIONS_REQUIRE_DATABASE_COMMENT` is set, the database `COMMENT` must
   equal it. An empty value is an error, not "no requirement". The migration
   tests use this; it is available for any extra interlock.

Alembic logs the target as `host:port/database` and the verified database.
Usernames and passwords are never printed. Staging and CI use a real host name
and need no override.

Known limitation: host names are not resolved by the guard. A custom name that
your own DNS or `/etc/hosts` points at 127.0.0.1 is not detected. Use the
service name or the real server name.

## pgvector

Revision `0001` runs `CREATE EXTENSION IF NOT EXISTS vector` before creating
`chunks.embedding vector(384)`, so it works on a fresh server without
`infra/postgres/init.sql`.

- Installing pgvector needs a **superuser** (it is not a "trusted" extension). In
  docker-compose the `migrate` tool connects as the bootstrap superuser `studygraph`
  (the runtime `studygraph_app` login is not one). On a managed database, have an
  administrator run `CREATE EXTENSION vector;` first; the migration then skips it.
- Downgrading never drops the extension. It is shared by the whole database and
  may not have been created by the migration.
- The embedding size (384) is fixed by `0001`. Changing it is a new revision plus
  re-embedding every chunk; agree it with AI-1 first.

## Writing a new migration

```bash
docker compose run --rm migrate alembic revision --autogenerate --rev-id 0002 -m "add ingestion jobs"
```

Use the next sequential `--rev-id`; the file becomes `0002_add_ingestion_jobs.py`.
Then review it by hand. Autogenerate is a draft, not a result:

- It does not handle extensions, generated columns or `tsvector` triggers, data
  migrations, or renames (it emits drop + add). Write those yourself.
- Keep constraint names as rendered (`op.f("...")`).
- Read the SQL with `alembic upgrade head --sql` and check that `downgrade()`
  reverses `upgrade()`.
- Run `scripts/test_migrations.sh` and announce the schema change to the team.

The tests are written to keep working as revisions are added. Current-schema tests
use the actual head and the ORM models. Baseline tests target `0001` explicitly. A
new revision normally needs no test changes, unless it changes columns that the
baseline sample rows insert into.

### Naming convention

`Base.metadata` (in `app/db.py`) names constraints deterministically, so a
later migration can always refer to them by name:

| Kind | Pattern | Example |
| --- | --- | --- |
| Primary key | `<table>_pkey` | `users_pkey` |
| Foreign key | `<table>_<column>_fkey` | `courses_owner_id_fkey` |
| Unique | `<table>_<columns>_key` | `enrollments_user_id_course_id_key` |
| Index | `<table>_<columns>_idx` | `chunks_document_id_idx` |
| Check | `<table>_<name>_check` | `mastery_scores_mastery_range_check` |

The first three reproduce PostgreSQL's own defaults (the scaffold database already
uses them). Index and check names are our project convention, not PostgreSQL
defaults. A `CheckConstraint` **must** get a short explicit `name=`. SQLAlchemy
refuses an unnamed one as soon as the table is defined. Expression indexes and
names longer than 63 characters (PostgreSQL's limit) also need an explicit name.

## Testing migrations

```bash
services/core-api/scripts/test_migrations.sh            # whole suite
services/core-api/scripts/test_migrations.sh -k naming  # extra arguments go to pytest
```

The script needs Docker, `bash` and `openssl`. It:

- builds a test image with the test dependencies installed. This is the only step
  that uses the network.
- creates a new **`--internal`** Docker network. It has no outbound traffic and no
  route to the host machine, so `host.docker.internal` and anything published on
  the host are unreachable. The docker-compose network is not attached, so the
  development `db` and `redis` services do not resolve.
- starts a throwaway `pgvector/pgvector:pg16` container on that network, with tmpfs
  storage and no published ports.
- starts a throwaway Redis (`redis:7`) on the same network: random password, no
  persistence, tmpfs, no published ports. The M3 broker and worker tests use it.
- uses random names, credentials and random identity tokens; stores the database
  token as the database's `COMMENT` and the Redis token under the key
  `studygraph:disposable-test`. Tests refuse a Redis without that key
  (`tests/disposable_redis.py`), as they refuse a database without the comment, and
  never flush it: every test uses uniquely named queues.
- runs pytest in a separate container on that network. Celery workers and Beat
  started by the M3 tests run inside that container against the two throwaway
  servers; their own start-up check verifies the database comment
  (`DATABASE_REQUIRE_COMMENT`).
- migrates the test database to `0001`, normalizes its `pg_dump --schema-only`, and
  requires it to equal `tests/fixtures/scaffold_schema.sql`.
- removes only the resources it created.

It never touches the docker-compose `db` service or any volume. Set
`MIGTEST_SCHEMA_DUMP=<file>` to also get a `pg_dump --schema-only` of the test
database at head.

**Where the safety checks run** (`tests/disposable_db.py`):

- Static checks:
  - `TEST_DATABASE_URL` and `TEST_DATABASE_TOKEN` are both set, with no fallback to
    `DATABASE_URL`;
  - the URL passes the same shape rules as migrations;
  - the database name starts with `sg_disposable_`;
  - the host is not this machine and not the compose `db` service;
  - the target differs from `DATABASE_URL`;
  - no `PGHOSTADDR` / `PGSERVICE` / `PGOPTIONS`.

  Without the two variables, the database tests are **skipped**. With only one of
  them, or an unsafe value, they **error**.
- In-process test connections check database, role, schema and token on the
  connection they then use.
- Alembic runs in a subprocess with `MIGRATIONS_REQUIRE_DATABASE_COMMENT` set to
  the token marker, so `env.py` checks the marker on the **migration's own
  connection** before any DDL. The test fixture also checks the database on a
  separate connection first; that is extra, not a replacement.

**What the suite verifies:**

- Current head:
  - a fresh upgrade installs pgvector itself;
  - repeated upgrades are no-ops;
  - reflection and catalog constraint names match the ORM;
  - `compare_metadata` and `alembic check` are clean;
  - baseline data survives later upgrades;
  - a full downgrade with data and re-upgrade works and keeps the extension.
- Revision `0002`:
  - upgrading a database with representative legacy data keeps every row and
    table OID and fills no new column;
  - invalid legacy `origin`/`status` values stop the upgrade before any change;
  - every new CHECK, unique, foreign-key and cascade rule, including NULLs;
  - a downgrade without upload data restores the scaffold schema exactly; with
    upload data it is refused and nothing changes;
  - the offline SQL carries both checks, and the rendered upgrade still refuses
    invalid data when executed.
- Revision `0003`:
  - upgrading from 0002 with representative data (legacy rows, stored uploads,
    queued, requeued and failed jobs) keeps every row and table OID and makes queued
    jobs dispatchable;
  - a running job stops the upgrade before any change (also from rendered SQL);
  - every new CHECK (locator, text, ordinal, lease), unique, foreign-key and cascade
    rule, including NULLs, and that ordered reads use the unique index;
  - a downgrade without M3 state restores 0002 exactly and upgrades again; with any
    kind of M3 state it is refused and nothing changes;
  - the historical baseline is unchanged after going through 0003.
- Revision `0004`:
  - upgrading from 0003 with representative data keeps every row and table OID and
    gives every existing course the empty code;
  - the code CHECK (trimmed, printable), NOT NULL and length;
  - a downgrade without codes restores 0003 exactly and upgrades again; with a code it
    is refused (also from rendered SQL) and nothing changes;
  - the historical baseline is unchanged after going through 0004.
- Baseline `0001`:
  - the catalog snapshot equals the scaffold;
  - PK, FK, unique, NOT NULL and default behaviour hold;
  - the embedding column accepts 384 dimensions and rejects others;
  - offline SQL renders.
- Adoption: a throwaway copy of the legacy schema, with data in every table, is
  stamped. Schema, data and table OIDs stay unchanged; only `alembic_version`
  appears; `upgrade head` and `check` then work.
- Guard:
  - unsafe URLs and PG* overrides are refused;
  - `PGHOST` / `PGPORT` / `PGUSER` / `PGDATABASE` cannot redirect a real migration;
  - a wrong marker, another database, a role switch or a `search_path` redirect
    stops the command before any change;
  - passwords never leak.

## Adopting a database created by `init_db.py`

Revision `0001` reproduces the scaffold schema exactly. A scaffold database can
therefore be marked as being at `0001` (`alembic stamp`) instead of being
rebuilt. The test suite rehearses this procedure on a throwaway database. Take
fresh evidence immediately before adopting a real database. Do not reuse old
comparison output.

1. **Read-only checks.** Both comparisons are required: they cover different
   things, see the next section.

   These run through the `migrate` tool (schema owner), since a scaffold database has no
   `studygraph_app` grants yet. Take a backup before step 2.

   ```bash
   docker compose build migrate
   docker compose run --rm -T -e PYTHONDONTWRITEBYTECODE=1 migrate python -m tests.schema_snapshot --compare tests/fixtures/scaffold_schema.json
   docker compose exec -T db pg_dump -U studygraph -d studygraph --schema-only --no-owner --no-privileges | docker compose run --rm -T migrate python scripts/normalize_schema_dump.py | diff -u services/core-api/tests/fixtures/scaffold_schema.sql -
   docker compose exec -T db psql -U studygraph -d studygraph -X -At -c "SELECT to_regclass('public.alembic_version') IS NULL"
   ```

   Expect the following. If anything differs, **stop**: the database was built from
   different models or changed by hand, and stamping it would record a lie. Ask CE-1.
   - `Schema of db:5432/studygraph matches ...`;
   - no output from `diff`;
   - `t` (no Alembic table yet).

2. **Stamp.** This creates only Alembic's bookkeeping table `alembic_version` with
   one row. No table, column or row of application data is touched; the adoption
   test checks this.

   ```bash
   docker compose run --rm -T migrate alembic stamp 0001
   ```

   Stamp the explicit revision `0001`, never `head`. Once later revisions exist,
   `stamp head` would mark them as applied without running them.

3. **Verify, then continue normally.**

   ```bash
   docker compose run --rm -T migrate alembic current   # 0001 (later revisions are not applied yet)
   docker compose run --rm -T -e PYTHONDONTWRITEBYTECODE=1 migrate python -m tests.schema_snapshot --compare tests/fixtures/scaffold_schema.json
   docker compose run --rm -T migrate alembic upgrade head   # applies the later revisions (see 0002's preflight)
   docker compose run --rm -T migrate alembic check     # No new upgrade operations detected.
   docker compose run --rm db-init                      # runtime roles and grants for the migrated schema
   ```

   `alembic check` only passes at head, so run it after `upgrade head`.
   Rerunning the `pg_dump` comparison at this point should show no difference: the
   normalizer drops the `alembic_version` statements.

To undo step 2 before anything else has run, use `alembic stamp base`, which
removes the revision row through Alembic. Do not edit `alembic_version` by hand.

## Schema comparison: what each check covers

**Catalog snapshot** (`python -m tests.schema_snapshot`, `tests/fixtures/scaffold_schema.json`).
It is built from PostgreSQL catalog queries, not from the ORM, and is designed to be
easy to read and diff for the schema this project actually uses.

It covers the `public` schema:

- extensions and their schema (not their version);
- tables;
- columns in physical order, with `format_type` types, NOT NULL and default
  expressions;
- every constraint via `pg_get_constraintdef`, with FK actions and match type;
- every index definition;
- the names and kinds of any other relations (views, sequences, ...).

It does **not** cover:

- other schemas;
- functions and procedures, triggers, rules, policies or row-level security;
- view definitions;
- sequence options;
- comments on objects;
- column collations or storage parameters;
- generated or identity column flags (only their expressions);
- partitioning, tablespaces or replica identity;
- ownership and privileges.

It is therefore **not** a general proof of schema equivalence.

**Normalized `pg_dump`** (`scripts/normalize_schema_dump.py`, `tests/fixtures/scaffold_schema.sql`).
This covers everything `pg_dump --schema-only` emits: the items above plus
functions, triggers, views, sequences, comments, policies and more, across all
schemas in the database. It is taken with `--no-owner --no-privileges`, so
**ownership and ACLs are not compared**: roles differ between databases and are
not part of the migrated schema. Compare them separately if they matter.

The normalizer removes only:

- `--` comment lines (dump headers and versions);
- the `\restrict` / `\unrestrict` lines (a random key per dump);
- `SET ...` and `SELECT pg_catalog.set_config(...)` session settings;
- statements on `public.alembic_version`;
- blank lines.

Every other line must match.

Both checks exclude only Alembic's own bookkeeping. Neither hides an application
schema difference. For adopting a real database both are required, together with
the check that `alembic_version` does not exist yet.
