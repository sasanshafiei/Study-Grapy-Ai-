"""Durable, course-bound index generations and restricted provider entry points.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        LOCK TABLE documents, chunks, chunk_topics IN ACCESS EXCLUSIVE MODE;
        CREATE TABLE index_generations (
            document_id uuid PRIMARY KEY,
            course_id uuid NOT NULL,
            idempotency_key varchar(64) NOT NULL UNIQUE,
            content_digest varchar(64) NOT NULL,
            section_count integer NOT NULL,
            chunk_count integer NOT NULL,
            model_name varchar(100) NOT NULL,
            model_version varchar(100) NOT NULL,
            committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT index_generations_document_course_fkey FOREIGN KEY (document_id, course_id)
                REFERENCES documents(id, course_id) ON DELETE CASCADE,
            CONSTRAINT index_generations_identity_key UNIQUE (document_id, course_id, idempotency_key),
            CONSTRAINT index_generations_counts_check CHECK (section_count > 0 AND chunk_count >= section_count),
            CONSTRAINT index_generations_hashes_check CHECK (
                content_digest ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$'),
            CONSTRAINT index_generations_model_check CHECK (
                model_name ~ '^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$' AND
                model_version ~ '^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}$')
        );
        ALTER TABLE chunks
            ADD COLUMN generation_key varchar(64),
            ADD COLUMN section_ordinal integer,
            ADD CONSTRAINT chunks_id_course_id_key UNIQUE (id, course_id),
            ADD CONSTRAINT chunks_generation_fkey FOREIGN KEY (document_id, course_id, generation_key)
                REFERENCES index_generations(document_id, course_id, idempotency_key) ON DELETE CASCADE,
            ADD CONSTRAINT chunks_generation_check CHECK (
                (generation_key IS NULL AND section_ordinal IS NULL) OR
                (generation_key IS NOT NULL AND course_id IS NOT NULL AND section_ordinal IS NOT NULL
                 AND section_ordinal >= 0 AND embedding IS NOT NULL AND vector_norm(embedding) > 0
                 AND locator IS NOT NULL AND text ~ '[^[:space:]]'));
        CREATE INDEX chunks_document_id_generation_key_idx ON chunks(document_id, generation_key);
        ALTER TABLE chunk_topics ADD COLUMN course_id uuid,
            ADD CONSTRAINT chunk_topics_chunk_course_fkey FOREIGN KEY (chunk_id, course_id)
                REFERENCES chunks(id, course_id) ON DELETE CASCADE,
            ADD CONSTRAINT chunk_topics_topic_course_fkey FOREIGN KEY (topic_id, course_id)
                REFERENCES topics(id, course_id);
        -- Historical links retain NULL. New links must bind both endpoints to the same course.
        CREATE FUNCTION index_link_course() RETURNS trigger LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.course_id IS NULL THEN
                SELECT c.course_id INTO NEW.course_id FROM public.chunks c WHERE c.id = NEW.chunk_id;
            END IF;
            IF NEW.course_id IS NULL THEN RAISE EXCEPTION 'unscoped chunk topic'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER chunk_topics_scope BEFORE INSERT OR UPDATE ON chunk_topics
            FOR EACH ROW EXECUTE FUNCTION index_link_course();
        -- Keep legacy chunks, associations, jobs and external provenance. Withdraw unproven publication.
        UPDATE documents SET status = 'failed' WHERE status = 'indexed';
    """)
    op.execute(VERIFY_GENERATION)
    op.execute(COMMIT_GENERATION)
    op.execute(RETRIEVAL)
    op.execute("""
        REVOKE ALL ON FUNCTION verified_index_generation(uuid,uuid,text,text,integer,integer) FROM PUBLIC;
        REVOKE ALL ON FUNCTION commit_index_generation(uuid,uuid,text,text,integer,text,text,jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION retrieve_index_chunks(uuid,text[]) FROM PUBLIC;
    """)


# Count all stored chunks. The outer join keeps orphan ordinals in the proof so they fail it.
# The same predicate runs before the provider commit and in CE's independent publication transaction.
VERIFY_GENERATION = """
CREATE FUNCTION verified_index_generation(
    p_document uuid, p_course uuid, p_key text, p_digest text, p_sections integer, p_chunks integer
) RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.index_generations g
        JOIN public.chunks c ON c.document_id = g.document_id
        JOIN public.documents d ON d.id = g.document_id
        LEFT JOIN public.document_sections s ON s.document_id = c.document_id AND s.ordinal = c.section_ordinal
        WHERE g.document_id = p_document AND g.course_id = p_course AND g.idempotency_key = p_key
          AND g.content_digest = p_digest AND g.section_count = p_sections AND g.chunk_count = p_chunks
        GROUP BY g.document_id
        HAVING count(*) = p_chunks AND count(DISTINCT s.ordinal) = p_sections
           AND bool_and((c.course_id = g.course_id AND c.origin = d.origin AND c.generation_key = g.idempotency_key
               AND c.embedding IS NOT NULL AND public.vector_norm(c.embedding) > 0 AND c.text ~ '[^[:space:]]'
               AND position(c.text in s.text) > 0 AND c.locator = CASE WHEN s.locator ? 'page' THEN s.locator
                   ELSE jsonb_build_object('section', left(btrim(regexp_replace(
                       coalesce(s.locator->>'heading', 'Introduction'), '[[:cntrl:]]', ' ', 'g')), 40)) END) IS TRUE)
    )
$$;
"""


# The writer may only call this routine. PostgreSQL serializes calls for one document, including first writers.
# A new statement snapshot after waiting on the document lock observes a committed prior call at READ COMMITTED.
COMMIT_GENERATION = """
CREATE FUNCTION commit_index_generation(
    p_document uuid, p_course uuid, p_key text, p_digest text, p_sections integer,
    p_model text, p_version text, p_chunks jsonb
) RETURNS TABLE (chunk_count integer, replayed boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    doc record;
    generation public.index_generations%ROWTYPE;
    item jsonb;
    new_chunk uuid;
    n integer;
    separator bytea := decode('00', 'hex');
BEGIN
    SELECT course_id, status, origin INTO STRICT doc FROM public.documents WHERE id = p_document FOR UPDATE;
    IF p_course IS NULL OR doc.course_id IS DISTINCT FROM p_course THEN
        RAISE EXCEPTION 'course_mismatch';
    END IF;
    IF p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$' OR p_key IS DISTINCT FROM encode(sha256(
        convert_to('studygraph.index/1', 'UTF8') || separator || convert_to(p_document::text, 'UTF8') ||
        separator || convert_to(p_course::text, 'UTF8') || separator || convert_to(p_digest, 'UTF8')), 'hex') THEN
        RAISE EXCEPTION 'inconsistent_request';
    END IF;
    SELECT * INTO generation FROM public.index_generations WHERE document_id = p_document;
    IF FOUND THEN
        IF generation.idempotency_key <> p_key THEN RAISE EXCEPTION 'generation_conflict'; END IF;
        IF generation.section_count IS DISTINCT FROM p_sections THEN RAISE EXCEPTION 'inconsistent_request'; END IF;
        RETURN QUERY SELECT generation.chunk_count, true;
        RETURN;
    END IF;
    IF p_chunks IS NULL THEN RETURN; END IF;  -- optional replay lookup before embedding
    IF doc.status <> 'pending' OR EXISTS (SELECT 1 FROM public.chunks WHERE document_id = p_document) THEN
        RAISE EXCEPTION 'document_not_writable';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.ingestion_jobs WHERE document_id = p_document AND kind = 'index'
                   AND status = 'running' AND lease_expires_at > clock_timestamp()) THEN
        RAISE EXCEPTION 'no_live_index_claim';
    END IF;
    PERFORM 1 FROM public.document_sections WHERE document_id = p_document ORDER BY ordinal FOR SHARE;
    IF (SELECT count(*) FROM public.document_sections WHERE document_id = p_document) <> p_sections THEN
        RAISE EXCEPTION 'source_sections_changed';
    END IF;
    n := jsonb_array_length(p_chunks);
    INSERT INTO public.index_generations
        (document_id, course_id, idempotency_key, content_digest, section_count, chunk_count, model_name, model_version)
        VALUES (p_document, p_course, p_key, p_digest, p_sections, n, p_model, p_version);
    FOR item IN SELECT value FROM jsonb_array_elements(p_chunks) LOOP
        IF jsonb_typeof(item->'text') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item->'embedding') IS DISTINCT FROM 'array'
           OR jsonb_typeof(item->'locator') IS DISTINCT FROM 'object'
           OR (item->>'section_ordinal') !~ '^(0|[1-9][0-9]*)$'
           OR jsonb_typeof(item->'section_ordinal') IS DISTINCT FROM 'number'
           OR (item->>'section_ordinal')::integer >= p_sections THEN
            RAISE EXCEPTION 'invalid_index_chunk';
        END IF;
        INSERT INTO public.chunks(document_id, course_id, origin, generation_key, section_ordinal, text, locator, embedding)
            VALUES (p_document, p_course, doc.origin, p_key, (item->>'section_ordinal')::integer,
                    item->>'text', item->'locator', (item->'embedding')::text::public.vector(384))
            RETURNING id INTO new_chunk;
        INSERT INTO public.chunk_topics(chunk_id, topic_id, course_id)
            SELECT new_chunk, value::uuid, p_course FROM jsonb_array_elements_text(item->'topic_ids');
    END LOOP;
    IF NOT public.verified_index_generation(p_document, p_course, p_key, p_digest, p_sections, n) THEN
        RAISE EXCEPTION 'invalid_index_evidence';
    END IF;
    RETURN QUERY SELECT n, false;
END $$;
"""

# Keep the previous visible external version while its replacement is pending. Opt-out stops new harvesting;
# a disabled source or stale version immediately withdraws retrieval. Live-web retrieval remains unavailable.
RETRIEVAL = """
CREATE FUNCTION retrieve_index_chunks(p_course uuid, p_origins text[])
RETURNS TABLE (id uuid, document_id uuid, course_id uuid, origin varchar, text text, locator jsonb,
               embedding public.vector, model_name varchar, model_version varchar, topic_ids uuid[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT c.id, c.document_id, c.course_id, c.origin, c.text, c.locator, c.embedding, g.model_name, g.model_version,
           ARRAY(SELECT ct.topic_id FROM public.chunk_topics ct WHERE ct.chunk_id = c.id AND ct.course_id = p_course)
    FROM public.chunks c JOIN public.index_generations g
        ON (g.document_id, g.course_id, g.idempotency_key) = (c.document_id, c.course_id, c.generation_key)
    JOIN public.documents d ON d.id = g.document_id AND d.course_id = g.course_id AND d.origin = c.origin
    LEFT JOIN public.sources s ON s.id = d.source_id
    LEFT JOIN public.external_document_versions v ON v.document_id = d.id
    LEFT JOIN public.external_resources r ON r.id = v.resource_id
    WHERE c.course_id = p_course AND c.origin = ANY(p_origins) AND d.status = 'indexed'
      AND (d.source_id IS NULL OR s.enabled)
      AND (d.origin = 'class' OR (d.origin = 'external' AND s.enabled AND
           (d.external_resource_id IS NULL OR (v.resource_id = d.external_resource_id AND v.stale_reason IS NULL AND r.state = 'accepted'))))
$$;
"""


def downgrade():
    op.execute("""
        LOCK TABLE documents, index_generations, chunks, chunk_topics IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM index_generations) THEN
                RAISE EXCEPTION 'Downgrade below 0009 refused: committed index generations require operator retention decisions';
            END IF;
        END $$;
        DROP FUNCTION retrieve_index_chunks(uuid,text[]);
        DROP FUNCTION commit_index_generation(uuid,uuid,text,text,integer,text,text,jsonb);
        DROP FUNCTION verified_index_generation(uuid,uuid,text,text,integer,integer);
        DROP TRIGGER chunk_topics_scope ON chunk_topics;
        DROP FUNCTION index_link_course();
        ALTER TABLE chunk_topics DROP COLUMN course_id;
        DROP INDEX chunks_document_id_generation_key_idx;
        ALTER TABLE chunks DROP CONSTRAINT chunks_id_course_id_key, DROP COLUMN section_ordinal, DROP COLUMN generation_key;
        DROP TABLE index_generations;
    """)
