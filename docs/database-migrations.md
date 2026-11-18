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
docker compose exec core-api alembic current       # revision the database is at, e.g. "0001 (head)"
docker compose exec core-api alembic history       # all revisions
docker compose exec core-api alembic check         # models vs. database: expect "No new upgrade operations detected."
docker compose exec core-api alembic upgrade head --sql   # print the SQL only, connect to nothing
```

A brand-new database volume needs nothing but `alembic upgrade head`.
A database created earlier by `init_db.py` needs adopting first, see below. Running
`upgrade` on it fails with `relation "users" already exists` and changes nothing
(PostgreSQL runs the whole migration in one transaction).

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
  development `db` service does not resolve.
- starts a throwaway `pgvector/pgvector:pg16` container on that network, with tmpfs
  storage and no published ports.
- uses random names, credentials and a random identity token, and stores the token
  as the database's `COMMENT`.
- runs pytest in a separate container on that network.
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
   docker compose exec -T core-api alembic current   # 0001 (head) while 0001 is the newest revision
   docker compose exec -T core-api alembic check     # No new upgrade operations detected.
   docker compose exec -T -e PYTHONDONTWRITEBYTECODE=1 core-api python -m tests.schema_snapshot --compare tests/fixtures/scaffold_schema.json
   docker compose exec -T core-api alembic upgrade head   # applies any later revisions
   ```

   Run the snapshot comparison before `upgrade head` if later revisions exist.
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
