"""The General Chemistry demo dataset (WP-2): development and test databases only.

What it creates, identified by natural keys that are unique in the schema:

- three users by e-mail (users_email_key), on the reserved `.test` domain:
  an admin, a professor and a student, all `active`;
- one course by join code (courses_join_code_key), owned by the professor;
- the student's enrollment (enrollments_user_id_course_id_key);
- the course's sample topics, through the WP-1 importer (app.topics.catalogue).

Never modify, never delete. A row that already exists is reused only if it is
what the seed would have created in the respects that matter (a user's role,
the course owner, the enrollment role); anything else is a conflict and the
whole seed is refused. Display names, passwords, course names and existing
topics are never touched.

No usable credentials. CE-3 owns authentication and password hashing. Seeded
users get LOCKED_PASSWORD, a marker that is not a hash of anything ('!' marks
"no password", as in Unix shadow files): nobody can sign in as them until CE-3
gives them real credentials. An existing user's stored value is never replaced.
The marker is public (it is in this file), so CE-3's login MUST treat any
stored value starting with '!' as "no password" and refuse before calling a
hash verifier, and must never compare a submitted password with the stored
value directly (docs/demo-seed.md, "CE-3 integration requirement").

One transaction. Everything is written or nothing is. A transaction-level
advisory lock serializes seed runs; INSERT ... ON CONFLICT DO NOTHING followed
by a re-read keeps a concurrent writer of the same keys (another seed, a
course join) from causing errors or duplicates. The topic import takes the
course row lock that WP-1 requires of every topic writer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from importlib import resources

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..topics import catalogue

LOCKED_PASSWORD = "!locked:studygraph-dev-seed"
SEED_LOCK_KEY = 0x5EED_C0DE  # pg_advisory_xact_lock key of the demo seed

DEMO_USERS = (  # (role, e-mail, display name); .test is reserved and never routable
    ("admin", "admin@demo.studygraph.test", "Demo Admin"),
    ("professor", "professor@demo.studygraph.test", "Demo Professor"),
    ("student", "student@demo.studygraph.test", "Demo Student"),
)
COURSE_NAME = "General Chemistry"
COURSE_CODE = "CHEM DEMO"
COURSE_JOIN_CODE = "SEEDCHEM"  # 8 characters from the join-code alphabet (app/services/courses.py)


class SeedConflict(RuntimeError):
    """Existing data contradicts the demo dataset; nothing was written."""


def sample_topics() -> bytes:
    """The packaged development sample topic file (four placeholder topics, not a curriculum)."""
    return resources.files(__package__).joinpath("demo_topics.json").read_bytes()


@dataclass
class SeedResult:
    lines: list[str] = field(default_factory=list)  # "status<TAB>kind<TAB>id<TAB>label"
    topics: catalogue.ImportResult | None = None
    topics_planned: int = 0  # dry run on a database without the course: names that would be imported

    def note(self, status: str, kind: str, row_id: uuid.UUID | None, label: str) -> None:
        self.lines.append(f"{status}\t{kind}\t{row_id or '-'}\t{label}")


def _insert(session: Session, sql: str, params: dict, dry_run: bool) -> bool:
    """Run an INSERT ... ON CONFLICT DO NOTHING RETURNING id; True when this run created the row."""
    return not dry_run and session.execute(text(sql), params).first() is not None


def _user(session: Session, result: SeedResult, role: str, email: str, name: str, dry_run: bool) -> uuid.UUID | None:
    created = _insert(
        session,
        "INSERT INTO users (email, display_name, hashed_password, role, status) "
        "VALUES (:e, :n, :p, :r, 'active') ON CONFLICT (email) DO NOTHING RETURNING id",
        {"e": email, "n": name, "p": LOCKED_PASSWORD, "r": role},
        dry_run,
    )
    lock = "" if dry_run else " FOR SHARE"  # no row locks in a read-only transaction
    row = session.execute(
        text(f"SELECT id, role, status FROM users WHERE email = :e{lock}"), {"e": email}
    ).one_or_none()
    if row is None:
        result.note("would create", "user", None, f"{role} {email}")
        return None
    if row.role != role:
        raise SeedConflict(f"user {email} exists with role {row.role!r}, not {role!r}")
    if row.status != "active":
        raise SeedConflict(f"user {email} exists but is {row.status!r}")
    result.note("created" if created else "exists", "user", row.id, f"{role} {email}")
    return row.id


def _course(session: Session, result: SeedResult, professor_id: uuid.UUID | None, dry_run: bool):
    created = _insert(
        session,
        "INSERT INTO courses (owner_id, name, code, join_code) VALUES (:o, :n, :code, :j) "
        "ON CONFLICT (join_code) DO NOTHING RETURNING id",
        {"o": professor_id, "n": COURSE_NAME, "code": COURSE_CODE, "j": COURSE_JOIN_CODE},
        dry_run,
    )
    lock = "" if dry_run else " FOR NO KEY UPDATE"  # the course lock WP-1 requires of topic writers
    row = session.execute(
        text(f"SELECT id, owner_id FROM courses WHERE join_code = :j{lock}"), {"j": COURSE_JOIN_CODE}
    ).one_or_none()
    label = f"{COURSE_NAME} (join code {COURSE_JOIN_CODE})"
    if row is None:
        result.note("would create", "course", None, label)
        return None
    if row.owner_id != professor_id:
        raise SeedConflict(f"join code {COURSE_JOIN_CODE} belongs to a course the demo professor does not own")
    result.note("created" if created else "exists", "course", row.id, label)
    return row.id


def _enrollment(session: Session, result: SeedResult, student_id, course_id, dry_run: bool) -> None:
    if student_id is None or course_id is None:
        result.note("would create", "enrollment", None, "demo student in the course")
        return
    created = _insert(
        session,
        "INSERT INTO enrollments (user_id, course_id, role) VALUES (:u, :c, 'student') "
        "ON CONFLICT (user_id, course_id) DO NOTHING RETURNING id",
        {"u": student_id, "c": course_id},
        dry_run,
    )
    row = session.execute(
        text("SELECT id, role FROM enrollments WHERE user_id = :u AND course_id = :c"),
        {"u": student_id, "c": course_id},
    ).one_or_none()
    if row is None:
        result.note("would create", "enrollment", None, "demo student in the course")
    elif row.role != "student":
        raise SeedConflict(f"the demo student is enrolled with role {row.role!r}, not 'student'")
    else:
        result.note("created" if created else "exists", "enrollment", row.id, "demo student in the course")


def seed_demo(session: Session, topic_names: list[str], *, dry_run: bool) -> SeedResult:
    """Create the missing parts of the demo dataset. The caller commits (or rolls back a dry run).

    Raises SeedConflict or catalogue.CatalogueError; the caller must then roll back.
    """
    if dry_run:
        session.execute(text("SET TRANSACTION READ ONLY"))
    else:
        # Bounds every wait of this transaction (the seed lock, row locks, the topic import's course lock).
        session.execute(text(f"SET LOCAL lock_timeout = '{catalogue.LOCK_TIMEOUT}'"))
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SEED_LOCK_KEY})
    result = SeedResult()
    ids = {role: _user(session, result, role, email, name, dry_run) for role, email, name in DEMO_USERS}
    course_id = _course(session, result, ids["professor"], dry_run)
    _enrollment(session, result, ids["student"], course_id, dry_run)
    if course_id is None:
        result.topics_planned = len(topic_names)  # dry run only: the course does not exist yet
    else:
        result.topics = catalogue.import_topics(session, course_id, topic_names, dry_run=dry_run)
    return result
