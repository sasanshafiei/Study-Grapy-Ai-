"""Remember the digest of an accepted resource's duplicated newest revision (WP-5 / F10).

When the newest provider revision of an ACCEPTED resource has the same
normalized text as content another accepted resource of the course holds,
the resource keeps its visible version and its own content_sha256 (the key of
the partial unique deduplication index), records the revision it checked and
stores the duplicated digest here, so the revision is not fetched again while
that text is held and is reconsidered as soon as nobody holds it.

Additive and nullable: no backfill. Downgrade forgets the recorded revision of
those resources (last_revision_id NULL, due now) so revision 0009 code fetches
and decides them again instead of trusting a revision whose text it never kept.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE external_resources
            ADD COLUMN duplicate_content_sha256 varchar(64),
            ADD CONSTRAINT external_resources_duplicate_content_sha256_check CHECK (
                duplicate_content_sha256 IS NULL OR (
                    state = 'accepted' AND duplicate_content_sha256 ~ '^[0-9a-f]{64}$'
                    AND duplicate_content_sha256 IS DISTINCT FROM content_sha256));
    """)


def downgrade():
    op.execute("""
        LOCK TABLE external_resources IN SHARE ROW EXCLUSIVE MODE;
        UPDATE external_resources
            SET last_revision_id = NULL, duplicate_of_id = NULL,
                next_check_at = least(next_check_at, now()), updated_at = now()
            WHERE duplicate_content_sha256 IS NOT NULL;
        ALTER TABLE external_resources
            DROP CONSTRAINT external_resources_duplicate_content_sha256_check,
            DROP COLUMN duplicate_content_sha256;
    """)
