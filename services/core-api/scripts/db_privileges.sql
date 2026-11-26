-- StudyGraph object privileges for one database (F05; includes the F03 index grants).
-- Run as the schema owner (the identity that runs Alembic) after db_roles.sql and after EVERY
-- `alembic upgrade`, before the new API and workers start, in one transaction:
--     psql -X --single-transaction -v ON_ERROR_STOP=1 -f db_privileges.sql
-- Idempotent and convergent: it removes what the StudyGraph groups hold on this schema's tables, sequences
-- and protected routines, then grants exactly the set below. No role creation, passwords, memberships,
-- ownership changes or default privileges (new tables get no access until this file is re-run).
-- Plain SQL, no psql meta-commands: the tests execute this same file. docs/database-security.md.
--
-- PostgreSQL row-level security is NOT used: no table has RLS policies. Course isolation is enforced by
-- the application (CE authorization) and, for the AI identities, by the course-scoped definer routines.

-- 0. Refuse to grant anything to a group, or to a direct member of one, that holds role attributes, is a
--    member of a predefined pg_* role, or owns (directly or through membership) objects of this database.
DO $$
DECLARE
    groups text[] := ARRAY['studygraph_ce_runtime', 'studygraph_ai_indexer', 'studygraph_ai_retriever'];
    problem text;
BEGIN
    IF (SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname = ANY (groups)) <> 3 THEN
        RAISE EXCEPTION 'StudyGraph group roles are missing; run db_roles.sql first';
    END IF;
    WITH identity AS (
        SELECT r.oid, r.rolname, r.rolcanlogin AND r.rolname = ANY (groups) AS group_login,
               r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls
               -- predefined roles (pg_read_server_files, pg_write_all_data, ...) confer power without attributes
               OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles p WHERE starts_with(p.rolname, 'pg_')
                          AND pg_catalog.pg_has_role(r.oid, p.oid, 'MEMBER')) AS attributes
        FROM pg_catalog.pg_roles r
        WHERE r.rolname = ANY (groups) OR r.oid IN (
            SELECT m.member FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
            WHERE g.rolname = ANY (groups))
    ), owner AS (
        SELECT relowner AS oid FROM pg_catalog.pg_class WHERE relnamespace = 'public'::regnamespace
        UNION SELECT proowner FROM pg_catalog.pg_proc WHERE pronamespace = 'public'::regnamespace
        UNION SELECT typowner FROM pg_catalog.pg_type WHERE typnamespace = 'public'::regnamespace
        UNION SELECT datdba FROM pg_catalog.pg_database WHERE datname = current_database()
    )
    SELECT string_agg(DISTINCT i.rolname, ', ') INTO problem FROM identity i
    WHERE i.group_login OR i.attributes
       OR EXISTS (SELECT 1 FROM owner o WHERE pg_catalog.pg_has_role(i.oid, o.oid, 'MEMBER'));
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'refusing to grant: % is privileged, a login group, or an object owner', problem;
    END IF;
END $$;

-- 1. Database: only the StudyGraph groups (and the owner) connect; nobody gets TEMPORARY or CREATE (schemas).
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT, TEMPORARY, CREATE ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM studygraph_ce_runtime, studygraph_ai_indexer, studygraph_ai_retriever',
                   current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO studygraph_ce_runtime, studygraph_ai_indexer, studygraph_ai_retriever',
                   current_database());
END $$;

-- 2. Schema: lookup only. CREATE stays with the owner (PUBLIC lost it in PostgreSQL 15; re-asserted for
--    clusters upgraded from older releases).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM studygraph_ce_runtime, studygraph_ai_indexer, studygraph_ai_retriever;
GRANT USAGE ON SCHEMA public TO studygraph_ce_runtime, studygraph_ai_indexer, studygraph_ai_retriever;

-- 3. Tables. The AI groups get none: they reach data only through the routines in 5.
--    CE runtime (API and every Celery worker) gets row DML on the application tables; never TRUNCATE,
--    REFERENCES or TRIGGER, and nothing on structure (it owns nothing). Exceptions, tested:
--    - alembic_version: read-only (readiness and worker preflight read the revision);
--    - index_generations, chunk_topics: read-only; rows are written only by commit_index_generation;
--    - chunks: read-only plus UPDATE (generation_key), because quiz provenance locks cited chunks with
--      SELECT ... FOR SHARE, which needs UPDATE on one column. That column cannot actually change:
--      chunks_generation_fkey only matches the document's single generation key, and chunks_generation_check
--      forbids clearing it (or setting it on a legacy chunk) without section_ordinal, which stays read-only;
--    - exam_audit_events, admin_audit_events: append-only (SELECT, INSERT), as their triggers already require.
REVOKE ALL ON ALL TABLES IN SCHEMA public
    FROM PUBLIC, studygraph_ce_runtime, studygraph_ai_indexer, studygraph_ai_retriever;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO studygraph_ce_runtime;
REVOKE INSERT, UPDATE, DELETE
    ON public.alembic_version, public.index_generations, public.chunk_topics, public.chunks
    FROM studygraph_ce_runtime;
REVOKE UPDATE, DELETE ON public.exam_audit_events, public.admin_audit_events FROM studygraph_ce_runtime;
GRANT UPDATE (generation_key) ON public.chunks TO studygraph_ce_runtime;

-- 4. Sequences: none today (UUID keys). A serial column added later needs nextval (USAGE), never setval.
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
    FROM PUBLIC, studygraph_ce_runtime, studygraph_ai_indexer, studygraph_ai_retriever;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO studygraph_ce_runtime;

-- 5. F03 routines (revision 0009). Each is callable by one group only; PUBLIC lost EXECUTE in 0009 and it
--    is re-asserted here. The two definer-rights routines run as their owner with a fixed search_path.
REVOKE ALL ON FUNCTION public.commit_index_generation(uuid,uuid,text,text,integer,text,text,jsonb),
                       public.retrieve_index_chunks(uuid,text[]),
                       public.verified_index_generation(uuid,uuid,text,text,integer,integer)
    FROM PUBLIC, studygraph_ce_runtime, studygraph_ai_indexer, studygraph_ai_retriever;
GRANT EXECUTE ON FUNCTION public.commit_index_generation(uuid,uuid,text,text,integer,text,text,jsonb)
    TO studygraph_ai_indexer;
GRANT EXECUTE ON FUNCTION public.retrieve_index_chunks(uuid,text[]) TO studygraph_ai_retriever;
GRANT EXECUTE ON FUNCTION public.verified_index_generation(uuid,uuid,text,text,integer,integer)
    TO studygraph_ce_runtime;
