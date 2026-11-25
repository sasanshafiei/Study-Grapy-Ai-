-- StudyGraph PostgreSQL group roles (F05). Cluster-wide: run as an administrator with CREATEROLE (the
-- bootstrap superuser in development) once per cluster and again whenever this file changes:
--     psql -X --single-transaction -v ON_ERROR_STOP=1 -f db_roles.sql
-- Idempotent. Creates only NOLOGIN groups and re-asserts their attributes on every run, so a pre-existing
-- role of the same name gains nothing. No logins, passwords, memberships or object privileges here:
-- login identities are provisioned per deployment (docs/database-security.md) and object privileges come
-- from db_privileges.sql, run by the schema owner after each migration. Plain SQL: the tests run this file.
DO $$
DECLARE
    name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['studygraph_ce_runtime', 'studygraph_ai_indexer', 'studygraph_ai_retriever'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = name) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', name);
        END IF;
        EXECUTE format('ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', name);
        -- A group that is itself a member of another role could inherit an owner's or a superuser's power.
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid = m.member
                   WHERE r.rolname = name) THEN
            RAISE EXCEPTION 'role % is a member of another role; remove that membership first', name;
        END IF;
    END LOOP;
END $$;
