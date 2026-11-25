#!/bin/sh
# Development-only role provisioning for docker-compose (the db-init service; F05). Idempotent.
# Staging and production never run this file: docs/database-security.md describes their rollout.
# Connection settings come from PGHOST/PGDATABASE/PGUSER/PGPASSWORD (the bootstrap superuser).
set -eu
: "${STUDYGRAPH_APP_DB_PASSWORD:?STUDYGRAPH_APP_DB_PASSWORD must be set}"
SCRIPTS=/provision/scripts

psql -X -q -v ON_ERROR_STOP=1 --single-transaction -f "$SCRIPTS/db_roles.sql"

# The runtime login: created once, attributes and password re-asserted on every run.
psql -X -q -v ON_ERROR_STOP=1 --single-transaction -v app_password="$STUDYGRAPH_APP_DB_PASSWORD" -f - <<'SQL'
SET client_min_messages = warning;
SELECT 'CREATE ROLE studygraph_app LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'studygraph_app') \gexec
ALTER ROLE studygraph_app LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'app_password';
GRANT studygraph_ce_runtime TO studygraph_app;
SQL

migrated=$(psql -X -At -v ON_ERROR_STOP=1 -c \
  "SELECT to_regprocedure('public.commit_index_generation(uuid,uuid,text,text,integer,text,text,jsonb)') IS NOT NULL")
if [ "$migrated" = t ]; then
  psql -X -q -v ON_ERROR_STOP=1 --single-transaction -f "$SCRIPTS/db_privileges.sql"
  echo "db-init: runtime roles and grants applied"
else
  echo "db-init: schema not migrated; grants skipped. Run: docker compose run --rm migrate && docker compose run --rm db-init"
fi
