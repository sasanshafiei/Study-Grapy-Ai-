"""Importing a course's topic catalogue (WP-1). The only production writer of `topics`.

Topics are course-local labels. Harvest queries (app/harvest/queries.py), quiz
and exam blueprints, chunk topic tags and the dashboard refer to them by id,
so an import only ever ADDS topics:

- Input: {"topics": [{"name": "..."}, ...]}, parsed strictly (UTF-8, no
  repeated keys, no NaN, no unknown keys). Every name is normalized (NFC,
  whitespace runs collapsed to one space, trimmed) and must be 1..150
  characters (topics.name is varchar(150)) without control, format or other
  invisible characters (Unicode category C). Two names of one file that
  differ only in case or spacing are an error.
- Identity: a name matches an existing topic of the same course when both,
  normalized as above, are equal ignoring case (str.casefold). A match is
  reported, never renamed: the stored spelling stays, because approved harvest
  queries are bound to the exact text derived from it.
- Existing topics that the file does not list are reported and kept. Nothing
  is ever deleted (questions, attempts, mastery and queries reference topics).
- Course isolation: every statement is restricted to the one course.
- Concurrency: a real import locks its course row FOR NO KEY UPDATE before it
  reads the existing names, so imports of one course are serialized and a
  name is inserted at most once. The lock does not conflict with the FOR KEY
  SHARE that foreign-key checks take (uploads, enrollments and other inserts
  continue); course joins (FOR SHARE) wait for the short import transaction.
  Imports of other courses are not blocked. lock_timeout bounds the wait.
- A dry run reads in a READ ONLY transaction and takes no lock.

Uniqueness is enforced here rather than by a database index: this module is
the only writer, and an index on (course_id, name) could neither express the
case/whitespace identity above nor be added safely to a database that already
holds near-duplicate legacy rows.

REQUIRED of any future code that inserts or renames topics (an API, the AI
topic extractor, a seed): lock the course row FOR NO KEY UPDATE in the same
transaction before reading the course's names, and compare with topic_key().
Without that lock nothing prevents duplicates; if that cannot be guaranteed,
add a migration-backed uniqueness rule instead.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..json_body import strict_loads

MAX_FILE_BYTES = 1024 * 1024
MAX_TOPICS = 500
MAX_NAME_CHARS = 150  # topics.name is varchar(150)
LOCK_TIMEOUT = "10s"  # how long an import waits for another import of the same course

_SPACES = re.compile(r"\s+")


class CatalogueError(ValueError):
    """The input or the target is not acceptable (message safe to show an operator)."""


def normalize_name(value: object) -> str:
    """The stored form of a topic name; raises CatalogueError (message without the value)."""
    if not isinstance(value, str):
        raise CatalogueError("must be a string")
    name = _SPACES.sub(" ", unicodedata.normalize("NFC", value)).strip()
    if not name:
        raise CatalogueError("is empty")
    for ch in name:
        if unicodedata.category(ch).startswith("C"):
            raise CatalogueError(f"contains a control or invisible character (U+{ord(ch):04X})")
    if len(name) > MAX_NAME_CHARS:
        raise CatalogueError(f"is longer than {MAX_NAME_CHARS} characters")
    return name


def topic_key(name: str) -> str:
    """Identity of a topic within its course: normalized name, case-insensitive."""
    return _SPACES.sub(" ", unicodedata.normalize("NFC", name)).strip().casefold()


def _item_name(index: int, item: object) -> str:
    if not isinstance(item, dict) or set(item) != {"name"}:
        raise CatalogueError(f'topics[{index}] must be an object with exactly one key, "name"')
    try:
        return normalize_name(item["name"])
    except CatalogueError as exc:
        raise CatalogueError(f"topics[{index}].name {exc}") from None


def parse_catalogue(data: bytes) -> list[str]:
    """Validate an import file; returns the normalized names in file order."""
    if len(data) > MAX_FILE_BYTES:
        raise CatalogueError(f"the file is larger than {MAX_FILE_BYTES} bytes")
    try:
        document = strict_loads(data)
    except ValueError:
        raise CatalogueError("the file is not valid JSON (UTF-8, no repeated keys, no NaN/Infinity)") from None
    if not isinstance(document, dict) or set(document) != {"topics"}:
        raise CatalogueError('the file must be a JSON object with exactly one key, "topics"')
    items = document["topics"]
    if not isinstance(items, list) or not items:
        raise CatalogueError('"topics" must be a non-empty array')
    if len(items) > MAX_TOPICS:
        raise CatalogueError(f'"topics" has more than {MAX_TOPICS} entries')
    names: list[str] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        name = _item_name(index, item)
        key = topic_key(name)
        if key in seen:
            raise CatalogueError(f"topics[{index}] repeats topics[{seen[key]}] (names differ only in case or spacing)")
        seen[key] = index
        names.append(name)
    return names


@dataclass(frozen=True)
class Existing:
    topic_id: uuid.UUID
    name: str  # as stored


@dataclass
class ImportResult:
    created: list[tuple[uuid.UUID | None, str]] = field(default_factory=list)  # id is None in a dry run
    matched: list[tuple[str, Existing]] = field(default_factory=list)  # (name in the file, existing topic)
    kept: list[Existing] = field(default_factory=list)  # existing topics the file does not list


def import_topics(session: Session, course_id: uuid.UUID, names: list[str], *, dry_run: bool) -> ImportResult:
    """Add the missing names to the course. The caller commits (or rolls back a dry run).

    Raises CatalogueError when the course does not exist. Must run at the start of a transaction.
    """
    if dry_run:
        session.execute(text("SET TRANSACTION READ ONLY"))
        course = session.execute(text("SELECT id FROM courses WHERE id = :c"), {"c": course_id}).first()
    else:
        session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        course = session.execute(
            text("SELECT id FROM courses WHERE id = :c FOR NO KEY UPDATE"), {"c": course_id}
        ).first()
    if course is None:
        raise CatalogueError("the course does not exist")

    rows = session.execute(
        text("SELECT id, name FROM topics WHERE course_id = :c ORDER BY name, id"), {"c": course_id}
    ).all()
    existing: dict[str, Existing] = {}
    for row in rows:  # legacy near-duplicates: the first in (name, id) order represents the key
        existing.setdefault(topic_key(row.name), Existing(row.id, row.name))

    result = ImportResult()
    missing: list[str] = []
    for name in names:
        match = existing.get(topic_key(name))
        if match is None:
            missing.append(name)
        else:
            result.matched.append((name, match))
    listed = {topic_key(name) for name in names}
    result.kept = [Existing(row.id, row.name) for row in rows if topic_key(row.name) not in listed]

    if dry_run or not missing:
        result.created = [(None, name) for name in missing]
        return result
    inserted = session.execute(
        text(
            "INSERT INTO topics (course_id, name) SELECT :c, n FROM unnest(CAST(:names AS text[])) "
            "WITH ORDINALITY AS t(n, i) ORDER BY i RETURNING id, name"
        ),
        {"c": course_id, "names": missing},
    ).all()
    ids = {row.name: row.id for row in inserted}
    result.created = [(ids[name], name) for name in missing]
    return result
