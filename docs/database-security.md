# Database security: least-privilege roles and network exposure (F05)

The API and every Celery worker connect as an unprivileged runtime login. Schema changes run as a separate
owner identity. PostgreSQL is not published on the host. Role provisioning is separate from Alembic
migrations: no migration creates roles or grants privileges, except the PUBLIC revocation of the F03
routines in revision 0009.

## Identities

| Identity | Kind | Privileges |
| --- | --- | --- |
| Schema owner (migrations) | Login. In compose: the bootstrap superuser `studygraph`. In staging and production: a dedicated `NOSUPERUSER` login that owns the database. | Owns every table, routine and trigger in `public`. Runs `alembic` and `db_privileges.sql`. Never configured as an API or worker `DATABASE_URL`. |
| `studygraph_ce_runtime` | `NOLOGIN` group | `CONNECT`; `USAGE` on `public`; `SELECT, INSERT, UPDATE, DELETE` on application tables; `USAGE` on sequences; `EXECUTE` on `verified_index_generation`. Exceptions: `alembic_version`, `index_generations` and `chunk_topics` are read-only. `chunks` is read-only apart from `UPDATE (generation_key)`, which quiz provenance needs for `FOR SHARE` row locks; constraints make any real change to that column fail. `exam_audit_events` is append-only. |
| `studygraph_app` | Runtime login, member of `studygraph_ce_runtime` | Inherits from the group only. Used by core-api and all workers. |
| `studygraph_ai_indexer` | `NOLOGIN` group | `CONNECT`, `USAGE` on `public`, `EXECUTE` on `commit_index_generation`. It has no table access. |
| `studygraph_ai_retriever` | `NOLOGIN` group | `CONNECT`, `USAGE` on `public`, `EXECUTE` on `retrieve_index_chunks`. It has no table access. |
| `PUBLIC` | — | No `CONNECT`, `TEMPORARY` or `CREATE` on the database. No `CREATE` on `public`, no table privileges and no `EXECUTE` on the F03 routines. It keeps PostgreSQL's default `EXECUTE` on trigger functions, which cannot be called directly, and on pgvector functions, which the invoker-rights verifier needs. |

No group or runtime login is `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION` or `BYPASSRLS`. None of
them owns, or is a member of the owner of, any object, so none can run `ALTER`, `DROP`, `TRUNCATE` or
`DISABLE TRIGGER`, or set `session_replication_role`. The two `SECURITY DEFINER` routines (from 0009)
run as the schema owner with `search_path = pg_catalog, pg_temp` and fully qualified objects.

**Row-level security is not used.** No table has RLS policies. Course isolation is enforced by the
application (CE authorization on every request and job). For the AI identities, it is enforced by the
course and origin parameters of the definer routines. Because the runtime role is not `BYPASSRLS`, RLS
could be added later without changing this design. It is not provided today.

### Files

- `services/core-api/scripts/db_roles.sql` is cluster-wide and run by a `CREATEROLE` admin. It creates the three groups and re-asserts their attributes on every run. It refuses a group that is a member of another role.
- `services/core-api/scripts/db_privileges.sql` is per database and run by the schema owner after every migration. It is convergent: it revokes, then grants exactly the set above. It refuses to run if a group, or a direct member of one, holds role attributes or owns objects. It sets no default privileges, so a new table gets no access until the script is re-run.
- `infra/postgres/dev-provision.sh` is used by the compose `db-init` service only. It runs both scripts and creates the `studygraph_app` login.
- Runtime guard: unless `ENVIRONMENT` is `development` or `test`, `/health/ready` returns 503 and every worker preflight exits when the connected role is a superuser, `BYPASSRLS`, `CREATEROLE`, `CREATEDB` or `REPLICATION`, can create in the database or schema, owns a table, or is a member of a predefined `pg_*` role (such as `pg_read_server_files`). This applies directly or through membership. `db_privileges.sql` applies the same rules to the groups and their direct members before it grants anything. Unknown `ENVIRONMENT` values enforce the guard.

## Development (docker-compose)

- `db` publishes no port. For host tools, publish it on loopback only in an untracked `docker-compose.override.yml`: `services: { db: { ports: ["127.0.0.1:5432:5432"] } }`.
- core-api and ai-service are published on loopback only (`127.0.0.1:8000`, `127.0.0.1:8001`). A browser, the web dev server and `curl` on the same machine reach them; other devices on the network do not. To test from another device, use staging, or publish a chosen interface in the untracked override: `services: { core-api: { ports: !override ["<host-ip>:8000:8000"] } }`. Remove it when you are done: the development stack has no login and uses development secrets.
- **Project name.** Compose names the containers and the `pgdata` and `uploads` volumes after the project, which is the checkout's folder name unless `COMPOSE_PROJECT_NAME` is set. Two checkouts under one project name share one database volume, and `docker compose up` in either recreates the containers with its own code and its own compose file (an older branch can publish ports or connect as the superuser again). Set `COMPOSE_PROJECT_NAME` in the untracked `.env` of the one checkout whose stack holds data you keep, and run every command for that stack from there. Check with `docker compose ls` before `up`.
- Runtime services use `studygraph_app`. Its password comes from `STUDYGRAPH_APP_DB_PASSWORD`, which has a development-only default.
- `docker compose up` never changes roles, grants, ownership or passwords. Provisioning is an explicit
  operator action: the `db-init` tool (profile `tools`, like `migrate`). It runs as the bootstrap superuser
  over the Compose network, is idempotent, and resets the `studygraph_app` password to the configured value.
- Fresh volume, or after every migration:

  ```bash
  docker compose run --rm migrate          # alembic upgrade head as the owner
  docker compose run --rm db-init          # roles, studygraph_app login, grants for the migrated schema
  docker compose up -d
  ```

- Running `docker compose exec core-api alembic upgrade head` is now refused (`permission denied for schema public`). This is intended.

**Existing development volume.** Until you run `db-init` once, the runtime services cannot log in
(`password authentication failed for user "studygraph_app"`). Readiness returns 503 and the workers exit;
no data changes. To switch over, run `docker compose run --rm db-init`, then `docker compose up -d`. On that
`up`, compose recreates `db` without the published port, keeping the data volume. Recreate any loopback
port you used before in the override file. To postpone the switch, set the old URL for each runtime service
in `docker-compose.override.yml` (see Recovery).

## Staging and production rollout

Do every step on staging first. Do not use compose defaults, `db-init` or the development passwords.

1. **Preflight and backup.**
   - Record roles and memberships (`\du+`), ownership, `\dp` and `\l+`.
   - Confirm that the schema is at the expected revision and that no runtime role is listed as an owner.
   - Take a backup and verify that it restores. Use `pg_dumpall --roles-only` for the role catalog as well as the database dump.
2. **Provision roles.**
   - Run `db_roles.sql` as a `CREATEROLE` admin: `psql -X --single-transaction -v ON_ERROR_STOP=1 -f db_roles.sql`.
   - Create the logins without putting a password in the SQL: `CREATE ROLE <app_login> LOGIN INHERIT IN ROLE studygraph_ce_runtime;`, then `\password <app_login>`. Do the same for the AI logins in their groups.
   - Create the owner as a `NOSUPERUSER` login. Make it the database owner.
3. **Grant.** Run `db_privileges.sql` as the owner in one transaction, after `alembic upgrade head`.
4. **Validate on disposable or staging infrastructure.**
   - Run `services/core-api/scripts/test_migrations.sh tests/test_db_privileges.py`.
   - On staging, run `SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE pg_has_role('<app_login>', oid, 'MEMBER')`. Expect all false.
   - Run the `\dp` checks against the identity table above.
5. **Configure credentials.**
   - Set each service's `DATABASE_URL` to its own login through the secret store.
   - Give the migration job the owner URL only.
   - Use `sslmode=verify-full` where the server presents a certificate.
6. **Verify.**
   - `/health/ready` returns 200 with `ENVIRONMENT=staging` or `production`, which proves that the guard accepted the identity.
   - Every worker passes preflight and starts.
   - Exercise uploads, parse, index publication, quizzes and exams.
7. **Remove superuser use.**
   - Confirm that no service uses a superuser: `SELECT usename, application_name FROM pg_stat_activity WHERE usesysid IN (SELECT oid FROM pg_roles WHERE rolsuper)`.
   - Rotate any superuser password that was in service configuration.
8. **Restrict network exposure.**
   - Expose no public listener. Restrict `pg_hba.conf` to the service subnets with `hostssl ... scram-sha-256`.
   - Allow superuser login from the admin path only. Allow the migration login from the deploy runner only.

Each later release follows the same order: `alembic upgrade head`, then `db_privileges.sql`, then start
the new API and workers. The workers already refuse a schema revision that does not match their code.

## Recovery

- **Wrong or missing runtime credentials** (readiness 503, worker exit 78, `password authentication failed`): point the services back at the previous credentials and restart. In development, set `DATABASE_URL` in `docker-compose.override.yml`. The data is never touched.
- **Missing grant** (`permission denied for table X` after a migration): run `db_privileges.sql` again as the owner. It is convergent. Never fix a grant by making a runtime role superuser or an owner.
- **A legitimate role lost access** through the PUBLIC `CONNECT`/`TEMPORARY` revocation: `GRANT CONNECT ON DATABASE <db> TO <role>`.
- **Full rollback:** move the services back first. Then, as the owner, run `DROP OWNED BY studygraph_ce_runtime, studygraph_ai_indexer, studygraph_ai_retriever;`, which removes their privileges in this database. Finally, `GRANT CONNECT, TEMPORARY ON DATABASE <db> TO PUBLIC;` and drop the logins and groups.
- **Do not run** `REASSIGN OWNED` or change ownership during an incident. Ownership transfer is a planned change (see below).

## Decisions that need cross-team approval (CE-3, AI-1)

Not decided here:

- Login names, the secret store, password rotation, and TLS and `pg_hba` policy for staging and production.
- The staging and production owner identity. Transferring ownership of objects created by an existing superuser to it (`REASSIGN OWNED`) needs a maintenance window. Until that transfer, the definer routines run with that superuser's rights.
- Whether revoking `CONNECT` from `PUBLIC` on the application database affects monitoring or backup roles.
- AI-1 login provisioning for `studygraph_ai_indexer` and `studygraph_ai_retriever`.
- Whether to add PostgreSQL RLS for course isolation.
