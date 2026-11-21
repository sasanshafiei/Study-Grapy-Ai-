"""JSON representations of bank questions, revisions and exams (validated by app/schemas/exams.py).

Lists never carry answer keys or explanations; detail views do (owner only, no-store).
Timestamps are ISO 8601 UTC with `Z`, second precision; ids canonical lower-case UUIDs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import snapshot as snap
from .validation import locator_label

STEM_PREVIEW = 200


def ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # questions.created_at is a naive UTC timestamp (baseline schema)
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _topic(session: Session, course_id, topic_id) -> dict | None:
    if topic_id is None:
        return None
    name = session.execute(text("SELECT name FROM topics WHERE id = :t AND course_id = :c"), {"t": topic_id, "c": course_id}).scalar()
    return {"id": str(topic_id), "name": name or ""}


def live_source_refs(session: Session, course_id, citations) -> list[dict]:
    refs = list(citations or [])
    ids = sorted({uuid.UUID(r["document_id"]) for r in refs})
    documents = {d.id: d for d in session.execute(
        text("SELECT id, title FROM documents WHERE course_id = :c AND origin = 'class' AND id = ANY(CAST(:ids AS uuid[]))"),
        {"c": course_id, "ids": ids},
    ).all()}  # fmt: skip
    out = []
    for ref in refs:
        document = documents.get(uuid.UUID(ref["document_id"]))
        out.append({"document_id": ref["document_id"], "title": None if document is None else snap._safe_title(document.title),
                    "locator": ref["locator"], "locator_label": locator_label(ref["locator"]["page"]),
                    "available": document is not None})  # fmt: skip
    return out


def load_question(session: Session, course_id, question_id, *, lock: str = ""):
    """A bank row of this course (version IS NOT NULL), or None. Legacy and practice rows are invisible."""
    return session.execute(
        text(f"SELECT id, course_id, status, version, qtype, difficulty, bloom_level, topic_id, text, choices, answer_key, "
             f"explanation, citations, content_hash, approved_revision_no, created_by, created_at, updated_at FROM questions "
             f"WHERE id = :q AND course_id = :c AND version IS NOT NULL AND purpose = 'exam'{lock}"),  # fmt: skip
        {"q": question_id, "c": course_id},
    ).first()


def question_view(session: Session, course_id, question_id) -> dict | None:
    q = load_question(session, course_id, question_id)
    if q is None:
        return None
    used = session.execute(
        text("SELECT count(*) FILTER (WHERE e.status = 'draft') AS drafts, "
             "count(*) FILTER (WHERE e.finalized_at IS NOT NULL) AS finalized "
             "FROM exam_items i JOIN exams e ON e.id = i.exam_id WHERE i.question_id = :q"),  # fmt: skip
        {"q": q.id},
    ).one()
    return {
        "id": str(q.id), "course_id": str(q.course_id), "status": q.status, "version": q.version, "type": q.qtype,
        "difficulty": q.difficulty, "bloom_level": q.bloom_level, "topic": _topic(session, course_id, q.topic_id),
        "stem": q.text, "choices": q.choices, "answer_key": q.answer_key, "explanation": q.explanation,
        "source_refs": live_source_refs(session, course_id, q.citations), "approved_revision": q.approved_revision_no,
        "content_sha256": q.content_hash, "used_in": {"draft_exams": used.drafts, "finalized_exams": used.finalized},
        "created_at": ts(q.created_at), "updated_at": ts(q.updated_at),
    }  # fmt: skip


def question_summary(row, topics: dict) -> dict:
    return {"id": str(row.id), "status": row.status, "version": row.version, "type": row.qtype,
            "difficulty": row.difficulty, "bloom_level": row.bloom_level, "topic": snap.topic_view(topics, row.topic_id),
            "stem_preview": row.text[:STEM_PREVIEW], "approved_revision": row.approved_revision_no,
            "updated_at": ts(row.updated_at)}  # fmt: skip


def revision_view(row, topics: dict, approver_name: str | None) -> dict:
    return {"revision": row.revision_no, "approved_at": ts(row.approved_at), "approved_by_name": approver_name or "",
            "content_sha256": row.content_sha256, "type": row.qtype, "difficulty": row.difficulty,
            "bloom_level": row.bloom_level, "topic": snap.topic_view(topics, row.topic_id), "stem": row.stem,
            "choices": row.choices, "answer_key": row.answer_key, "explanation": row.explanation,
            "source_refs": [{"document_id": r["document_id"], "title": r["title"], "locator_label": r["locator_label"]}
                            for r in row.source_refs]}  # fmt: skip


def exam_summary(row) -> dict:
    return {"id": str(row.id), "status": row.status, "version": row.version, "title": row.title,
            "item_count": row.item_count_live, "total_points": row.total_points_live, "finalized_at": ts(row.finalized_at),
            "archived_at": ts(row.archived_at),
            "derived_from_exam_id": None if row.derived_from_exam_id is None else str(row.derived_from_exam_id),
            "updated_at": ts(row.updated_at)}  # fmt: skip


def _item_question(state: snap.ExamState, content) -> dict:
    return {"type": content.qtype, "difficulty": content.difficulty, "bloom_level": content.bloom_level,
            "topic": snap.topic_view(state.topics, content.topic_id), "stem": snap.stem_of(content),
            "choices": content.choices, "answer_key": content.answer_key, "explanation": content.explanation,
            "source_refs": snap.source_refs_view(state, content)}  # fmt: skip


def exam_view(state: snap.ExamState, settings, fingerprint: str | None) -> dict:
    exam = state.exam
    finalized = state.finalized
    rows = []
    for row in state.rows:
        rows.append({"id": str(row.id), "position": row.position, "topic": snap.topic_view(state.topics, row.topic_id),
                     "type": row.qtype, "difficulty": row.difficulty, "bloom_level": row.bloom_level,
                     "count": row.question_count, "points_each": row.points_each,
                     "assigned": sum(1 for s in state.items if s.item.blueprint_row_id == row.id)})  # fmt: skip
    items = []
    for s in state.items:
        items.append({"id": str(s.item.id), "position": s.item.position, "points": s.item.points,
                      "blueprint_row_id": None if s.item.blueprint_row_id is None else str(s.item.blueprint_row_id),
                      "question_id": str(s.question.id), "question_status": s.question.status,
                      "question_version": s.question.version,
                      "pinned_revision": s.item.question_revision_no if finalized else None,
                      "question": _item_question(state, s.content)})  # fmt: skip
    readiness = None
    if exam.status == "draft":
        readiness = snap.readiness(state, settings)
        readiness["preview_fingerprint"] = fingerprint
    return {
        "id": str(exam.id), "course_id": str(exam.course_id), "status": exam.status, "version": exam.version,
        "title": exam.title, "instructions": exam.instructions, "duration_minutes": exam.duration_minutes,
        "derived_from_exam_id": None if exam.derived_from_exam_id is None else str(exam.derived_from_exam_id),
        "created_at": ts(exam.created_at), "updated_at": ts(exam.updated_at), "finalized_at": ts(exam.finalized_at),
        "archived_at": ts(exam.archived_at),
        "course_snapshot": {"code": exam.course_code_snapshot, "name": exam.course_name_snapshot} if finalized else None,
        "content_sha256": exam.content_sha256, "finalized_version": exam.finalized_version,
        "blueprint_status": exam.blueprint_status, "blueprint_deviation_reason": exam.blueprint_deviation_reason,
        "finalize_preview_fingerprint": exam.finalize_preview_fingerprint,
        "content_chars": exam.content_chars if finalized else snap.content_chars(state),
        "export_budget": settings.exam_export_max_content_chars,
        "item_count": exam.item_count if finalized else len(state.items),
        "total_points": exam.total_points if finalized else sum(s.item.points for s in state.items),
        "blueprint": {"rows": rows, "total_count": sum(r.question_count for r in state.rows),
                      "total_points": sum(r.question_count * r.points_each for r in state.rows)},
        "items": items, "readiness": readiness,
    }  # fmt: skip


def audit_view(row) -> dict:
    return {"id": str(row.id), "action": row.action, "entity_kind": row.entity_kind, "entity_id": str(row.entity_id),
            "entity_version": row.entity_version, "actor_name": row.actor_name or "", "created_at": ts(row.created_at),
            "details": row.details}  # fmt: skip
