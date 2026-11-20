"""Course code and the lookups behind the course and enrollment endpoints (M5).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20

courses gains `code` (the short code shown next to the name, "CHEM 101"):
VARCHAR(32) NOT NULL DEFAULT ''. PostgreSQL stores the default once, so the
table is not rewritten and every existing course gets the empty string, the
documented "no code recorded" fallback (docs/courses-api.md). A CHECK keeps
new values trimmed and free of control characters; '' satisfies it, so no
legacy row can violate it.

Indexes for the M5 queries (none existed: the baseline has only primary keys
and the two unique constraints):
- courses(owner_id): a professor's course list;
- enrollments(course_id): student counts and access checks by course (the
  unique (user_id, course_id) constraint already serves lookups by user);
- topics(course_id): a course's topic list.

Nullability was verified against the scaffold schema before writing this
revision (tests/fixtures/scaffold_schema.sql): courses.owner_id, name and
join_code, enrollments.user_id, course_id and role, topics.course_id and name
are already NOT NULL, and join_code is already unique. Nothing is tightened.
Join codes stay case-sensitive in the database; new codes are generated in
upper case and lookups prefer an exact match (docs/courses-api.md).

Data safety:
- upgrade only adds; it never modifies existing values. The three tables are
  locked up front, in a fixed order, so the change cannot interleave with
  concurrent writes or deadlock against them.
- downgrade refuses, before any change, while any course has a non-empty
  code (it would be lost). Clear the codes deliberately first.
Both run as SQL, so they also apply when the SQL is rendered with
`alembic upgrade --sql` / `downgrade --sql` and executed later.

Self-contained on purpose: it does not import app.models.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODE_CHECK = "code = btrim(code) AND code !~ '[[:cntrl:]]'"

UPGRADE_LOCKS = "LOCK TABLE courses, enrollments, topics IN SHARE ROW EXCLUSIVE MODE"

# The table is locked first so no code can be written between the check and the drop.
DOWNGRADE_GUARD = """
DO $migration_0004_downgrade_guard$
DECLARE
    coded_courses bigint;
BEGIN
    LOCK TABLE courses IN ACCESS EXCLUSIVE MODE;
    SELECT count(*) INTO coded_courses FROM courses WHERE code <> '';
    IF coded_courses > 0 THEN
        RAISE EXCEPTION 'Downgrade below 0004 refused before any change. % courses have a course code that would be lost. Clear the codes deliberately first (docs/database-migrations.md).', coded_courses;
    END IF;
END
$migration_0004_downgrade_guard$
"""


def upgrade() -> None:
    op.execute(UPGRADE_LOCKS)
    op.add_column(
        "courses",
        sa.Column("code", sa.String(length=32), server_default=sa.text("''::character varying"), nullable=False),
    )
    op.create_check_constraint(op.f("courses_code_check"), "courses", CODE_CHECK)
    op.create_index(op.f("courses_owner_id_idx"), "courses", ["owner_id"])
    op.create_index(op.f("enrollments_course_id_idx"), "enrollments", ["course_id"])
    op.create_index(op.f("topics_course_id_idx"), "topics", ["course_id"])


def downgrade() -> None:
    """Remove everything upgrade() added, only if no course code would be lost."""
    op.execute(DOWNGRADE_GUARD)
    op.drop_index(op.f("topics_course_id_idx"), table_name="topics")
    op.drop_index(op.f("enrollments_course_id_idx"), table_name="enrollments")
    op.drop_index(op.f("courses_owner_id_idx"), table_name="courses")
    op.drop_constraint(op.f("courses_code_check"), "courses", type_="check")
    op.drop_column("courses", "code")
