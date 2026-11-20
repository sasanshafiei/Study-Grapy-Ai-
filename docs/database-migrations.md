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

- Run Alembic **inside the core-api container** (`docker compose exec core-api alembic ...`),
  where docker-compose sets `DATABASE_URL` to the `db` service.
- Never create or alter tables by hand or with `create_all()`.
- Never edit a revision that has been merged; add a new one.
- Revisions are self-contained: they must not import `app.models` (the models keep
  changing; old revisions must not).

If you run Compose with extra `-f` files (for example a local, untracked override),
pass the same files to every command below.

## Everyday commands

```bash
docker compose up -d --build                       # start the stack (rebuild after dependency changes)
docker compose exec core-api alembic upgrade head  # create / update the schema
docker compose exec core-api alembic current       # revision the database is at, e.g. "0004 (head)"
docker compose exec core-api alembic history       # all revisions
docker compose exec core-api alembic check         # models vs. database: expect "No new upgrade operations detected."
docker compose exec core-api alembic upgrade head --sql   # print the SQL only, connect to nothing
```

A brand-new database volume needs nothing but `alembic upgrade head`.
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
  docker-compose the database user is a superuser. On a managed database, have an
  administrator run `CREATE EXTENSION vector;` first; the migration then skips it.
- Downgrading never drops the extension. It is shared by the whole database and
  may not have been created by the migration.
- The embedding size (384) is fixed by `0001`. Changing it is a new revision plus
  re-embedding every chunk; agree it with AI-1 first.

## Writing a new migration

```bash
docker compose exec core-api alembic revision --autogenerate --rev-id 0002 -m "add ingestion jobs"
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

   ```bash
   docker compose up -d --build core-api
   docker compose exec -T -e PYTHONDONTWRITEBYTECODE=1 core-api python -m tests.schema_snapshot --compare tests/fixtures/scaffold_schema.json
   docker compose exec -T db pg_dump -U studygraph -d studygraph --schema-only --no-owner --no-privileges | docker compose exec -T core-api python scripts/normalize_schema_dump.py | diff -u services/core-api/tests/fixtures/scaffold_schema.sql -
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
   docker compose exec -T core-api alembic stamp 0001
   ```

   Stamp the explicit revision `0001`, never `head`. Once later revisions exist,
   `stamp head` would mark them as applied without running them.

3. **Verify, then continue normally.**

   ```bash
   docker compose exec -T core-api alembic current   # 0001 (later revisions are not applied yet)
   docker compose exec -T -e PYTHONDONTWRITEBYTECODE=1 core-api python -m tests.schema_snapshot --compare tests/fixtures/scaffold_schema.json
   docker compose exec -T core-api alembic upgrade head   # applies the later revisions (see 0002's preflight)
   docker compose exec -T core-api alembic check     # No new upgrade operations detected.
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
