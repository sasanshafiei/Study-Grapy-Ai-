"""Public queries: deterministic derivation from course topics, explicit approval, exact-text binding.

What may leave the system. A provider request carries exactly one approved
query text and fixed protocol parameters. Nothing else from StudyGraph is
ever sent: no document passages, no student questions, no course, document,
user or topic identifiers, no names, e-mail addresses or join codes.

Where the guarantee comes from (in this order):
1. Derivation only from course topic names (`topics.name`), the curated
   labels shown in the course's topic list. Document text, chunks, chat
   questions and answers are never inputs; there is no code path from them.
2. An explicit operator approval of the exact query text, per course and
   topic (`harvest_queries`). Harvesting sends only approved texts, and only
   while the text derived from the topic *now* is still exactly the approved
   text: a renamed topic silently stops being searched until its new text is
   approved again. Approval is never automatic and never carried over.
3. Screens (defence in depth, NOT the guarantee): refused are texts that
   contain an e-mail-like token, a URL, a long digit run, the course code,
   join code or id, or the display name of a course member, and texts outside
   the length and word limits. Pattern checks cannot recognize all personal
   information; that is why (2) exists.

The future AI topic extractor (proposed `studygraph.topics/1` at
POST /ai/v1/topics/extract, docs/external-harvesting.md) may only *propose*
candidates for (2); its output is never approved automatically, and mock
output is refused.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

MAX_QUERY_CHARS = 120
MIN_QUERY_CHARS = 2
MAX_QUERY_WORDS = 8

_URL = re.compile(r"(?i)\b(?:https?://|www\.)|\b[a-z0-9-]+\.(?:com|org|net|edu|io|de|tr|uk)\b")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
_DIGITS = re.compile(r"\d{5,}")
_UUID = re.compile(r"(?i)[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}")
_SPACES = re.compile(r"\s+")


class QueryRefused(ValueError):
    """The text cannot be approved (reason in the message, safe to show an operator)."""


def derive_query(topic_name: str) -> str:
    """The query text for a topic name: NFC, control characters removed, whitespace collapsed, trimmed."""
    value = unicodedata.normalize("NFC", topic_name or "")
    value = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in value)
    return _SPACES.sub(" ", value).strip()[:MAX_QUERY_CHARS].strip()


@dataclass(frozen=True)
class CourseContext:
    """What a query must never contain about the course (screen inputs, never sent anywhere)."""

    course_id: uuid.UUID
    course_code: str
    join_code: str
    member_names: tuple[str, ...]


def course_context(session: Session, course_id: uuid.UUID) -> CourseContext:
    course = session.execute(
        text("SELECT code, join_code FROM courses WHERE id = :c"), {"c": course_id}
    ).one_or_none()
    if course is None:
        raise QueryRefused("no such course")
    names = session.execute(
        text(
            "SELECT u.display_name FROM users u WHERE u.id = (SELECT owner_id FROM courses WHERE id = :c) "
            "UNION SELECT u.display_name FROM enrollments e JOIN users u ON u.id = e.user_id WHERE e.course_id = :c"
        ),
        {"c": course_id},
    ).scalars()
    return CourseContext(course_id, course.code or "", course.join_code or "", tuple(n for n in names if n))


def screen(query: str, context: CourseContext) -> None:
    """Defence-in-depth checks; QueryRefused with a reason. Passing them is not a privacy guarantee."""
    if not MIN_QUERY_CHARS <= len(query) <= MAX_QUERY_CHARS or query != query.strip():
        raise QueryRefused(f"the query must be {MIN_QUERY_CHARS}..{MAX_QUERY_CHARS} characters, trimmed")
    if len(query.split()) > MAX_QUERY_WORDS:
        raise QueryRefused(f"the query has more than {MAX_QUERY_WORDS} words")
    if any(unicodedata.category(ch).startswith("C") for ch in query):
        raise QueryRefused("the query contains control characters")
    if _EMAIL.search(query) or _URL.search(query):
        raise QueryRefused("the query contains an e-mail address or URL")
    if _DIGITS.search(query) or _UUID.search(query):
        raise QueryRefused("the query contains an identifier-like number")
    folded = query.casefold()
    identifiers = [context.course_code, context.join_code, str(context.course_id)]
    if any(value and len(value.strip()) >= 2 and value.strip().casefold() in folded for value in identifiers):
        raise QueryRefused("the query contains a course identifier")
    for name in context.member_names:
        for part in (p for p in re.split(r"\s+", name.casefold()) if len(p) >= 3):
            if re.search(rf"(?<!\w){re.escape(part)}(?!\w)", folded):
                raise QueryRefused("the query contains a course member's name")


@dataclass(frozen=True)
class ProposedQuery:
    topic_id: uuid.UUID
    topic_name: str
    query_text: str
    refused: str | None  # screen result; None = may be approved


def propose(session: Session, course_id: uuid.UUID) -> list[ProposedQuery]:
    """Deterministic candidates for every topic of the course (read only)."""
    context = course_context(session, course_id)
    rows = session.execute(
        text("SELECT id, name FROM topics WHERE course_id = :c ORDER BY name, id"), {"c": course_id}
    ).all()
    proposals = []
    for row in rows:
        query = derive_query(row.name)
        try:
            screen(query, context)
            reason = None
        except QueryRefused as exc:
            reason = str(exc)
        proposals.append(ProposedQuery(row.id, row.name, query, reason))
    return proposals


def approve(
    session: Session, course_id: uuid.UUID, topic_id: uuid.UUID, exact_text: str, max_queries: int
) -> uuid.UUID:
    """Approve exactly `exact_text` for a topic; it must equal the derivation of the topic name now.

    A previously approved query of the topic is revoked in the same
    transaction (a new text needs a new approval; the old approval never
    covers it). The caller commits.
    """
    settings = session.execute(
        text("SELECT course_id FROM harvest_course_settings WHERE course_id = :c FOR NO KEY UPDATE"), {"c": course_id}
    ).one_or_none()
    if settings is None:
        raise QueryRefused("the course has no harvesting settings; run course-opt-in first")
    topic = session.execute(
        text("SELECT name FROM topics WHERE id = :t AND course_id = :c"), {"t": topic_id, "c": course_id}
    ).one_or_none()
    if topic is None:
        raise QueryRefused("no such topic in this course")
    derived = derive_query(topic.name)
    if exact_text != derived:
        raise QueryRefused("the text differs from the query derived from the topic name; approve the derived text exactly")
    screen(derived, course_context(session, course_id))
    current = session.execute(
        text("SELECT id, query_text FROM harvest_queries WHERE course_id = :c AND topic_id = :t AND status = 'approved'"),
        {"c": course_id, "t": topic_id},
    ).one_or_none()
    if current is not None and current.query_text == derived:
        return current.id
    active = session.execute(
        text("SELECT count(*) FROM harvest_queries WHERE course_id = :c AND status = 'approved' AND topic_id <> :t"),
        {"c": course_id, "t": topic_id},
    ).scalar_one()
    if active >= max_queries:
        raise QueryRefused(f"the course already has {active} approved queries (limit {max_queries})")
    if current is not None:
        session.execute(
            text("UPDATE harvest_queries SET status = 'revoked', revoked_at = now() WHERE id = :id"), {"id": current.id}
        )
    try:
        with session.begin_nested():
            return session.execute(
                text(
                    "INSERT INTO harvest_queries (course_id, topic_id, query_text) VALUES (:c, :t, :q) RETURNING id"
                ),
                {"c": course_id, "t": topic_id, "q": derived},
            ).scalar_one()
    except IntegrityError:
        raise QueryRefused("a concurrent approval of this topic won; check the approved queries") from None


def revoke(session: Session, query_id: uuid.UUID) -> bool:
    return bool(
        session.execute(
            text(
                "UPDATE harvest_queries SET status = 'revoked', revoked_at = now() "
                "WHERE id = :id AND status = 'approved'"
            ),
            {"id": query_id},
        ).rowcount
    )


@dataclass(frozen=True)
class UsableQuery:
    id: uuid.UUID
    text: str


def usable_queries(session: Session, course_id: uuid.UUID, limit: int) -> list[UsableQuery]:
    """Approved queries whose topic still derives exactly the approved text (deterministic order)."""
    rows = session.execute(
        text(
            "SELECT q.id, q.query_text, t.name FROM harvest_queries q JOIN topics t ON t.id = q.topic_id "
            "WHERE q.course_id = :c AND q.status = 'approved' AND t.course_id = :c "
            "ORDER BY q.approved_at, q.id LIMIT :limit"
        ),
        {"c": course_id, "limit": limit},
    ).all()
    return [UsableQuery(row.id, row.query_text) for row in rows if derive_query(row.name) == row.query_text]


def still_usable(session: Session, course_id: uuid.UUID, query: UsableQuery) -> bool:
    """True only while this exact approved query row still exists, is approved, and its topic derives its text NOW.

    Read from the database at the moment of the call (never cached): the executor calls it right before each
    public request that belongs to the query, and again after any budget wait.
    """
    row = session.execute(
        text(
            "SELECT q.query_text, t.name FROM harvest_queries q JOIN topics t ON t.id = q.topic_id "
            "WHERE q.id = :id AND q.course_id = :c AND t.course_id = :c AND q.status = 'approved'"
        ),
        {"id": query.id, "c": course_id},
    ).one_or_none()
    return row is not None and row.query_text == query.text and derive_query(row.name) == row.query_text
