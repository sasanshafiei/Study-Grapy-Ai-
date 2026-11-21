"""Course materials a student may be pointed to, with the existing M5/M6 visibility rules (read-only queries).

Class material (as for chat and quiz citations): document and chunk origin 'class',
document `indexed`, chunk course == document course == the course.

External material (M6, as chat verification accepts it): document and chunk origin
'external', document `indexed`, chunk course == document course == the course, its source
exists and is enabled, and its provenance is intact: the document belongs to a harvested
external resource of the same course (composite key) and has a version record of that
resource that is not stale. The URL, title and licence come from those persisted records
(documents.title, external_document_versions.canonical_url / licence), never from a
generated value, and the URL must be a safe https URL. current_document_id is NOT required
to point at the document: while a replacement version is pending, the older indexed
version stays visible, exactly as in M6.

A topic is linked to material through chunk_topics (class and external) or, for external
resources, through the approved harvest query that discovered them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..chat.verify import VerificationFailed, public_title, public_url


@dataclass(frozen=True)
class Material:
    document_id: uuid.UUID
    title: str
    origin: str  # class | external
    chunks: int
    url: str | None = None
    licence: str | None = None
    provider: str | None = None


_CLASS = text(
    """
    SELECT ct.topic_id, d.id, d.title, count(*) AS chunks
    FROM chunk_topics ct
    JOIN topics t ON t.id = ct.topic_id AND t.course_id = :course
    JOIN chunks c ON c.id = ct.chunk_id
    JOIN documents d ON d.id = c.document_id
    WHERE ct.topic_id = ANY(:topics) AND c.origin = 'class' AND d.origin = 'class' AND d.status = 'indexed'
      AND c.course_id = :course AND d.course_id = :course
    GROUP BY ct.topic_id, d.id, d.title
    """
)

_EXTERNAL_VISIBLE = """
    d.origin = 'external' AND d.status = 'indexed' AND d.course_id = :course AND d.external_resource_id IS NOT NULL
    AND s.enabled IS TRUE AND r.course_id = :course AND r.source_id = d.source_id
    AND v.resource_id = d.external_resource_id AND v.stale_reason IS NULL
"""

_EXTERNAL = text(
    f"""
    WITH links AS (
        SELECT ct.topic_id, c.document_id, count(*) AS chunks
        FROM chunk_topics ct
        JOIN topics t ON t.id = ct.topic_id AND t.course_id = :course
        JOIN chunks c ON c.id = ct.chunk_id AND c.origin = 'external' AND c.course_id = :course
        WHERE ct.topic_id = ANY(:topics)
        GROUP BY ct.topic_id, c.document_id
        UNION ALL
        SELECT q.topic_id, d.id, 0 AS chunks
        FROM external_resources r0
        JOIN harvest_queries q ON q.id = r0.discovered_by_query_id AND q.course_id = :course
        JOIN documents d ON d.external_resource_id = r0.id
        WHERE r0.course_id = :course AND q.topic_id = ANY(:topics)
    )
    SELECT l.topic_id, d.id, d.title, sum(l.chunks) AS chunks, v.canonical_url, v.licence, s.provider
    FROM links l
    JOIN documents d ON d.id = l.document_id
    JOIN sources s ON s.id = d.source_id
    JOIN external_resources r ON r.id = d.external_resource_id
    JOIN external_document_versions v ON v.document_id = d.id
    WHERE {_EXTERNAL_VISIBLE}
    GROUP BY l.topic_id, d.id, d.title, v.canonical_url, v.licence, s.provider
    """
)


def _safe_https(url: object) -> str | None:
    try:
        checked = public_url(url)
    except VerificationFailed:
        return None
    if checked is None or urlsplit(checked).scheme != "https":
        return None
    return checked


def _safe_title(title: object) -> str | None:
    try:
        return public_title(title)
    except VerificationFailed:
        return None


def _best(rows: list[tuple[uuid.UUID, Material]]) -> dict[uuid.UUID, Material]:
    """Per topic: most linked chunks, then title, then id (deterministic)."""
    best: dict[uuid.UUID, Material] = {}
    for topic, material in sorted(rows, key=lambda r: (str(r[0]), -r[1].chunks, r[1].title, str(r[1].document_id))):
        best.setdefault(topic, material)
    return best


def class_material(session: Session, course_id: uuid.UUID, topic_ids: list[uuid.UUID]) -> dict[uuid.UUID, Material]:
    if not topic_ids:
        return {}
    rows = []
    for r in session.execute(_CLASS, {"course": course_id, "topics": topic_ids}).all():
        title = _safe_title(r.title)
        if title is not None:
            rows.append((r.topic_id, Material(r.id, title, "class", int(r.chunks))))
    return _best(rows)


def external_material(session: Session, course_id: uuid.UUID, topic_ids: list[uuid.UUID]) -> dict[uuid.UUID, Material]:
    if not topic_ids:
        return {}
    rows = []
    for r in session.execute(_EXTERNAL, {"course": course_id, "topics": topic_ids}).all():
        title, url = _safe_title(r.title), _safe_https(r.canonical_url)
        if title is None or url is None or not isinstance(r.licence, str) or not r.licence.strip():
            continue
        rows.append((r.topic_id, Material(r.id, title, "external", int(r.chunks), url, r.licence.strip()[:100], r.provider)))
    return _best(rows)


def visible_document(session: Session, course_id: uuid.UUID, document_id: uuid.UUID) -> bool:
    """A document of this course that a student may be shown (class or external rules above)."""
    row = session.execute(
        text(
            f"""
            SELECT d.origin, d.title, v.canonical_url FROM documents d
            LEFT JOIN sources s ON s.id = d.source_id
            LEFT JOIN external_resources r ON r.id = d.external_resource_id
            LEFT JOIN external_document_versions v ON v.document_id = d.id
            WHERE d.id = :doc AND d.course_id = :course AND d.status = 'indexed'
              AND (d.origin = 'class' OR ({_EXTERNAL_VISIBLE}))
            """
        ),
        {"doc": document_id, "course": course_id},
    ).one_or_none()
    if row is None or _safe_title(row.title) is None:
        return False
    return row.origin == "class" or _safe_https(row.canonical_url) is not None
