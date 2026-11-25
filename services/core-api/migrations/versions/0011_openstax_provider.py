"""OpenStax as a second provider (WP-6): book identity, git-native version keys, insert-disabled sources.

- sources.provider_book: the allowlisted OpenStax book a provider-managed
  source harvests (NULL for Wikipedia and every legacy row). The provider
  CHECK admits `openstax` only with language `en`, a licence URL and a book
  slug; Wikipedia rows keep exactly their previous rule.
- A BEFORE INSERT trigger stores every new OpenStax source row disabled,
  whatever the INSERT says: creating the row never activates it; only an
  explicit UPDATE (`app.harvest.admin enable-source`) does.
- OpenStax has no numeric revisions. Its version identity is the git blob
  SHA-1 of the module file in OpenStax's official book repository, stored as
  external_resources.last_revision_key / external_document_versions.revision_key
  (40 lowercase hex). Exactly one of revision_id / revision_key is set on a
  version; Wikipedia versions keep revision_id and a NULL key.

Additive except for external_document_versions.revision_id becoming
nullable. Downgrade refuses while any OpenStax source, resource or version
exists (the 0010 constraints cannot represent them, and deleting provenance
is never done silently); remove them first.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE sources
            ADD COLUMN provider_book varchar(100),
            DROP CONSTRAINT sources_provider_check,
            ADD CONSTRAINT sources_provider_check CHECK (
                (provider IS NULL AND provider_book IS NULL)
                OR (provider = 'wikipedia' AND language IS NOT NULL AND language ~ '^[a-z]{2,3}$'
                    AND licence_url IS NOT NULL AND provider_book IS NULL)
                OR (provider = 'openstax' AND language IS NOT NULL AND language = 'en' AND licence_url IS NOT NULL
                    AND provider_book IS NOT NULL AND provider_book ~ '^[a-z0-9]+(-[a-z0-9]+)*$'));

        CREATE FUNCTION sources_openstax_insert_disabled() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.enabled := false;
            RETURN NEW;
        END
        $$;
        CREATE TRIGGER sources_openstax_insert_disabled BEFORE INSERT ON sources
            FOR EACH ROW WHEN (NEW.provider = 'openstax') EXECUTE FUNCTION sources_openstax_insert_disabled();

        ALTER TABLE external_resources
            ADD COLUMN last_revision_key varchar(40),
            ADD CONSTRAINT external_resources_last_revision_key_check CHECK (
                last_revision_key IS NULL OR (last_revision_key ~ '^[0-9a-f]{40}$' AND last_revision_id IS NULL));

        ALTER TABLE external_document_versions
            ADD COLUMN revision_key varchar(40),
            ALTER COLUMN revision_id DROP NOT NULL,
            DROP CONSTRAINT external_document_versions_identity_check,
            ADD CONSTRAINT external_document_versions_identity_check CHECK (
                version >= 1 AND page_id > 0 AND (
                    (revision_id IS NOT NULL AND revision_id > 0 AND revision_key IS NULL)
                    OR (revision_id IS NULL AND revision_key IS NOT NULL AND revision_key ~ '^[0-9a-f]{40}$')));
    """)


def downgrade():
    op.execute("""
        LOCK TABLE sources, external_resources, external_document_versions IN SHARE ROW EXCLUSIVE MODE;
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM sources WHERE provider = 'openstax' OR provider_book IS NOT NULL)
               OR EXISTS (SELECT 1 FROM external_resources WHERE last_revision_key IS NOT NULL)
               OR EXISTS (SELECT 1 FROM external_document_versions WHERE revision_key IS NOT NULL) THEN
                RAISE EXCEPTION 'downgrade to 0010 refused: OpenStax sources or harvested OpenStax versions exist';
            END IF;
        END
        $$;

        ALTER TABLE external_document_versions
            DROP CONSTRAINT external_document_versions_identity_check,
            ADD CONSTRAINT external_document_versions_identity_check CHECK (
                version >= 1 AND page_id > 0 AND revision_id > 0),
            ALTER COLUMN revision_id SET NOT NULL,
            DROP COLUMN revision_key;

        ALTER TABLE external_resources
            DROP CONSTRAINT external_resources_last_revision_key_check,
            DROP COLUMN last_revision_key;

        DROP TRIGGER sources_openstax_insert_disabled ON sources;
        DROP FUNCTION sources_openstax_insert_disabled();

        ALTER TABLE sources
            DROP CONSTRAINT sources_provider_check,
            ADD CONSTRAINT sources_provider_check CHECK (
                provider IS NULL OR (provider = 'wikipedia' AND language IS NOT NULL AND language ~ '^[a-z]{2,3}$'
                    AND licence_url IS NOT NULL)),
            DROP COLUMN provider_book;
    """)
