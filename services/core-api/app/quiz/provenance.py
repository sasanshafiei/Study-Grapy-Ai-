"""Citation provenance for generated questions, at persistence and again at every delivery.

verify_question_citations() runs INSIDE the transaction that persists the quiz
(app/quiz/generation.py), after FOR SHARE locks on the cited documents and
chunks (ordered by id), so no cited document can become stale, be deleted or
change course between the check and the commit. A citation is accepted only
if ALL hold, otherwise the whole generation is rejected:
- the chunk exists and belongs to the cited document;
- chunk origin == document origin == cited origin == 'class' (the only origin
  of class_only, the only M7 scope; external and web are never accepted);
- the document is `indexed`;
- chunk course == document course == the requested course;
- the cited locator equals the chunk's stored locator and has a known shape;
- the quote, whitespace-normalized, is at least MIN_QUOTE_CHARS long and
  occurs verbatim in the chunk text;
- the document title is safe to show.
The persisted evidence (questions.citations) keeps chunk and document ids,
origin, locator and the normalized quote. It is never delivered as such.

visible_citations() re-checks the same rules (without locks) whenever
feedback is delivered: the first graded answer, an identical retry and GET
quiz recovery. A citation that is no longer visible (document stale, failed
or deleted, moved, chunk changed so the quote no longer matches) is omitted;
its title and snippet are never shown from an old snapshot, and the recorded
grade does not change. Public fields come from the current records.

This establishes provenance only: the quoted passage exists in an indexed
source of this course. It does NOT establish that the passage entails the
question, its answer or its explanation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..chat.verify import (
    MIN_QUOTE_CHARS,
    VerificationFailed,
    normalize,
    public_locator,
    public_title,
    snippet_for,
)
from .protocol import ProposedQuestion

_ROWS = (
    "SELECT c.id, c.document_id, c.course_id AS chunk_course, c.origin AS chunk_origin, c.text, c.locator, "
    "d.course_id AS doc_course, d.origin AS doc_origin, d.status, d.title "
    "FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ANY(:ids)"
)


@dataclass(frozen=True)
class PublicCitations:
    citations: list[dict]
    omitted: int


def _same_locator(a: object, b: object) -> bool:
    """Exact JSON equality, with integers kept apart from booleans and floats."""
    if not isinstance(a, dict) or not isinstance(b, dict) or set(a) != set(b):
        return False
    return all(type(a[k]) is type(b[k]) and a[k] == b[k] for k in a)


def _check(row, document_id: uuid.UUID, course_id: uuid.UUID, locator: object, quote: str) -> tuple[str, str, str]:
    """(title, locator label, snippet) of a visible class citation; VerificationFailed otherwise."""
    if row is None:
        raise VerificationFailed("unknown_chunk")
    if row.document_id != document_id:
        raise VerificationFailed("document_mismatch")
    if row.chunk_origin != "class" or row.doc_origin != "class":
        raise VerificationFailed("origin_not_permitted")
    if row.status != "indexed":
        raise VerificationFailed("document_not_indexed")
    if row.chunk_course != course_id or row.doc_course != course_id:
        raise VerificationFailed("course_mismatch")
    if not _same_locator(locator, row.locator):
        raise VerificationFailed("locator_mismatch")
    label = public_locator(row.locator)
    if len(normalize(quote)) < MIN_QUOTE_CHARS:
        raise VerificationFailed("quote_too_short")
    return public_title(row.title), label, snippet_for(row.text, quote)


def verify_question_citations(
    session: Session, course_id: uuid.UUID, questions: tuple[ProposedQuestion, ...]
) -> list[list[dict]]:
    """Lock and verify every citation; the evidence to persist per question (VerificationFailed rejects all)."""
    chunk_ids = sorted({c.chunk_id for q in questions for c in q.citations})
    document_ids = sorted({c.document_id for q in questions for c in q.citations})
    # Lock order: documents, then chunks, each by id (the persistence transaction has already locked the
    # user, course, enrollment, session and topic rows).
    session.execute(
        text("SELECT id FROM documents WHERE id = ANY(:ids) ORDER BY id FOR SHARE"), {"ids": document_ids}
    ).all()
    session.execute(text("SELECT id FROM chunks WHERE id = ANY(:ids) ORDER BY id FOR SHARE"), {"ids": chunk_ids}).all()
    rows = {r.id: r for r in session.execute(text(_ROWS), {"ids": chunk_ids}).all()}
    evidence: list[list[dict]] = []
    for question in questions:
        items = []
        for cited in question.citations:
            _check(rows.get(cited.chunk_id), cited.document_id, course_id, cited.locator, cited.quote)
            items.append(
                {
                    "chunk_id": str(cited.chunk_id),
                    "document_id": str(cited.document_id),
                    "origin": "class",
                    "locator": cited.locator,
                    "quote": normalize(cited.quote),
                }
            )
        evidence.append(items)
    return evidence


def visible_citations(session: Session, course_id: uuid.UUID, evidence: object) -> PublicCitations:
    """The public form of the stored evidence that is still visible now; the rest is counted, not shown."""
    if not isinstance(evidence, list):
        return PublicCitations([], 0)
    parsed = []
    for item in evidence:
        try:
            parsed.append((uuid.UUID(item["chunk_id"]), uuid.UUID(item["document_id"]), item["locator"], item["quote"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            parsed.append(None)
    ids = [p[0] for p in parsed if p is not None]
    rows = {r.id: r for r in session.execute(text(_ROWS), {"ids": ids}).all()} if ids else {}
    public, omitted = [], 0
    for entry in parsed:
        if entry is None:
            omitted += 1
            continue
        chunk_id, document_id, locator, quote = entry
        try:
            title, label, snippet = _check(rows.get(chunk_id), document_id, course_id, locator, quote)
        except VerificationFailed:
            omitted += 1
            continue
        public.append(
            {"chunk_id": str(chunk_id), "origin": "class", "title": title, "locator": label, "url": None, "snippet": snippet}
        )
    return PublicCitations(public, omitted)
