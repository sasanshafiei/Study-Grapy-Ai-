"""Document withdrawal (WP-10): a durable, terminal tombstone for professor-uploaded class materials.

- documents.status gains 'withdrawn'; documents.withdrawn_at and documents.withdrawn_by record when and
  by whom (users.id).
- documents_withdrawal_check: only a class upload of a course (origin 'class', no source, no external
  resource) can be withdrawn, and a withdrawn row carries its withdrawal record; other rows carry none.
- documents_withdrawal_final (BEFORE UPDATE, row, only for rows already withdrawn): a withdrawn row is
  never changed again, whichever code path or role tries (parse or index completion, retries, operator
  SQL as the runtime role). Deleting the row stays possible (a future purge).
- documents_course_id_content_hash_idx: withdrawn rows leave course-scoped deduplication, so the same
  file can be uploaded again after a withdrawal.

Nothing is deleted or rewritten: existing rows keep their status, and a withdrawn document's stored file,
sections, chunks, index generation, topic links and jobs are retained (docs/document-withdrawal.md).
Retrieval (retrieve_index_chunks), index publication (commit_index_generation, complete_index), citation
verification, dashboards and study events already require status 'indexed' or 'pending'; listing and
deduplication exclude 'withdrawn' in application code.

No grant changes: the WP-8 runtime role's table-level DML on documents covers the new columns, and a
trigger function is not EXECUTE-checked when it fires. Re-run scripts/db_privileges.sql after the upgrade
as after every upgrade (docs/database-security.md).

Downgrade refuses while any withdrawn document exists: the previous CHECK and unique index cannot hold
them. An operator who really wants to downgrade deletes them deliberately first
(docs/database-migrations.md).

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# Verbatim copies of the ORM expressions in app/models.py (tests/test_wp10_withdrawal.py compares them).
STATUS_CHECK = "status IN ('pending', 'indexed', 'failed', 'stale', 'withdrawn')"
WITHDRAWAL_CHECK = (
    "((status = 'withdrawn' AND withdrawn_at IS NOT NULL AND withdrawn_by IS NOT NULL AND origin = 'class'"
    " AND course_id IS NOT NULL AND source_id IS NULL AND external_resource_id IS NULL)"
    " OR (status <> 'withdrawn' AND withdrawn_at IS NULL AND withdrawn_by IS NULL)) IS TRUE"
)
DEDUP_WHERE = "origin = 'class' AND storage_key IS NOT NULL AND status <> 'withdrawn'"

PREVIOUS_STATUS_CHECK = "status IN ('pending', 'indexed', 'failed', 'stale')"
PREVIOUS_DEDUP_WHERE = "origin = 'class' AND storage_key IS NOT NULL"

FINAL_GUARD = """
CREATE FUNCTION studygraph_documents_withdrawal_final() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    RAISE EXCEPTION 'document % is withdrawn; a withdrawn document cannot change', OLD.id
        USING ERRCODE = 'check_violation';
END
$fn$;

CREATE TRIGGER documents_withdrawal_final BEFORE UPDATE ON documents
    FOR EACH ROW WHEN (OLD.status = 'withdrawn') EXECUTE FUNCTION studygraph_documents_withdrawal_final();
"""


def upgrade():
    op.execute(f"""
        LOCK TABLE documents IN ACCESS EXCLUSIVE MODE;
        ALTER TABLE documents
            DROP CONSTRAINT documents_status_check,
            ADD CONSTRAINT documents_status_check CHECK ({STATUS_CHECK}),
            ADD COLUMN withdrawn_at timestamptz,
            ADD COLUMN withdrawn_by uuid,
            ADD CONSTRAINT documents_withdrawn_by_fkey FOREIGN KEY (withdrawn_by) REFERENCES users(id),
            ADD CONSTRAINT documents_withdrawal_check CHECK ({WITHDRAWAL_CHECK});
        DROP INDEX documents_course_id_content_hash_idx;
        CREATE UNIQUE INDEX documents_course_id_content_hash_idx ON documents (course_id, content_hash)
            WHERE {DEDUP_WHERE};
    """)
    op.execute(FINAL_GUARD)


def downgrade():
    op.execute(f"""
        LOCK TABLE documents IN ACCESS EXCLUSIVE MODE;
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM documents WHERE status = 'withdrawn') THEN
                RAISE EXCEPTION 'downgrade to 0012 refused: withdrawn documents exist';
            END IF;
        END
        $$;
        DROP TRIGGER documents_withdrawal_final ON documents;
        DROP FUNCTION studygraph_documents_withdrawal_final();
        DROP INDEX documents_course_id_content_hash_idx;
        CREATE UNIQUE INDEX documents_course_id_content_hash_idx ON documents (course_id, content_hash)
            WHERE {PREVIOUS_DEDUP_WHERE};
        ALTER TABLE documents
            DROP CONSTRAINT documents_withdrawal_check,
            DROP CONSTRAINT documents_withdrawn_by_fkey,
            DROP COLUMN withdrawn_by,
            DROP COLUMN withdrawn_at,
            DROP CONSTRAINT documents_status_check,
            ADD CONSTRAINT documents_status_check CHECK ({PREVIOUS_STATUS_CHECK});
    """)
