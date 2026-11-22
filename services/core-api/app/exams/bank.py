"""Question bank transactions (E1-E7): list, create, read, replace, approve, retire, revisions, history.

Every write runs in one short READ COMMITTED transaction with statement and lock timeouts (short_tx) and
takes its locks in the M9 order: users, courses FOR SHARE (authorization) -> the question FOR UPDATE ->
documents FOR SHARE (approval) -> revision insert -> idempotency key -> audit event. Bank operations never
lock an exam. Every response body is built afterwards in a fresh read-only transaction that re-checks
authorization right before it is sent.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..chat.verify import VerificationFailed, public_title
from ..errors import ApiError
from ..quiz.access import short_tx
from ..uploads import PDF_MEDIA_TYPE
from . import audit, idempotency
from .access import question_not_found, require_exam_owner
from .http import decode_cursor, encode_cursor, mutation_response_read, version_mismatch
from .validation import QuestionContent, content_from_row, invalid_question, locator_label
from .views import load_question, question_summary, question_view, revision_view

REPLAYED = {"Idempotent-Replayed": "true"}


@dataclass
class Answer:
    status: int
    body: dict
    headers: dict


def question_retired() -> ApiError:
    return ApiError(409, "question_retired", "This question is retired and can no longer be changed or used.")


def read_question(factory, user, course_id, question_id) -> dict:
    """The current representation, in a fresh read-only snapshot that re-checks authorization."""
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_exam_owner(session, user, course_id)
        view = question_view(session, course_id, question_id)
    if view is None:
        raise question_not_found()
    return view


def deliver(factory, user, course_id, question_id) -> dict:
    with mutation_response_read():
        return read_question(factory, user, course_id, question_id)


def _check_page_bound(page: int, page_count: int | None) -> None:
    if page_count is not None and page > page_count:
        raise invalid_question("source_refs", "locator", "A source reference points past the document's last page.")


def check_references(session: Session, course_id, content: QuestionContent) -> None:
    """Topic of this course; class documents of this course; page within the known page count."""
    if content.topic_id is not None:
        found = session.execute(text("SELECT 1 FROM topics WHERE id = :t AND course_id = :c"),
                                {"t": content.topic_id, "c": course_id}).first()  # fmt: skip
        if found is None:
            raise invalid_question("topic_id", "topic", "The topic is not a topic of this course.")
    if not content.source_refs:
        return
    documents = {d.id: d for d in session.execute(
        text("SELECT id, page_count FROM documents WHERE course_id = :c AND origin = 'class' AND id = ANY(CAST(:ids AS uuid[]))"),
        {"c": course_id, "ids": sorted({d for d, _ in content.source_refs})},
    ).all()}  # fmt: skip
    for document_id, page in content.source_refs:
        document = documents.get(document_id)
        if document is None:
            raise invalid_question("source_refs", "source_ref", "A source reference names no class document of this course.")
        _check_page_bound(page, document.page_count)


def _content_params(content: QuestionContent) -> dict:
    return {"t": content.topic_id, "qtype": content.qtype, "d": content.difficulty, "stem": content.stem,
            "choices": None if content.choices is None else json.dumps(content.choices_json()),
            "key": json.dumps(content.answer_key), "expl": content.explanation,
            "cit": None if content.citations_json() is None else json.dumps(content.citations_json()),
            "hash": content.content_hash(), "bloom": content.bloom_level}  # fmt: skip


# --- E2 create ------------------------------------------------------------------------------------------------


def create_question(factory, settings, user, course_id, key: uuid.UUID, content: QuestionContent) -> Answer:
    operation = "question_create"
    print_ = idempotency.fingerprint(operation, course_id, content.canonical())
    try:
        with short_tx(factory) as session:
            require_exam_owner(session, user, course_id, lock=True)
            existing = idempotency.lookup(session, user.id, key, operation, print_)
            if existing is None:
                idempotency.lock_capacity_for_create(session, course_id, user.id, key, operation, print_)
                count = session.execute(text("SELECT count(*) FROM questions WHERE course_id = :c AND version IS NOT NULL"),
                                        {"c": course_id}).scalar_one()  # fmt: skip
                if count >= settings.exam_max_bank_questions:
                    raise ApiError(409, "question_limit_reached", "This course's question bank is full.",
                                   extra={"limit": settings.exam_max_bank_questions})  # fmt: skip
                check_references(session, course_id, content)
                question_id = session.execute(
                    text("INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, choices, answer_key, "
                         "explanation, citations, status, content_hash, version, created_by, created_at, updated_at, bloom_level) "
                         "VALUES (:c, :t, 'exam', :qtype, :d, :stem, CAST(:choices AS jsonb), CAST(:key AS jsonb), :expl, "
                         "CAST(:cit AS jsonb), 'draft', :hash, 1, :u, now() AT TIME ZONE 'UTC', now(), :bloom) RETURNING id"),  # fmt: skip
                    {"c": course_id, "u": user.id, **_content_params(content)},
                ).scalar_one()
                idempotency.store(session, user.id, key, operation, print_, course_id, question_id)
                audit.record(session, course_id=course_id, actor_id=user.id, entity_kind="question", entity_id=question_id,
                             action="question_created", entity_version=1, details={"content_sha256": content.content_hash()})  # fmt: skip
                session.commit()
                return Answer(201, deliver(factory, user, course_id, question_id),
                              {"Location": f"/api/v1/courses/{course_id}/exam-questions/{question_id}"})  # fmt: skip
    except idempotency.KeyTaken:
        with short_tx(factory, read_only=True) as session:
            require_exam_owner(session, user, course_id)
            existing = idempotency.lookup(session, user.id, key, operation, print_)
    return Answer(200, deliver(factory, user, course_id, existing), REPLAYED)


# --- E1 list --------------------------------------------------------------------------------------------------


def list_questions(factory, user, course_id, filters: dict, limit: int, cursor: str | None) -> dict:
    endpoint = "exam_questions.list"
    position = decode_cursor(cursor, endpoint, user.id, course_id, None, filters)
    clauses = ["course_id = :c", "version IS NOT NULL", "purpose = 'exam'", "status = ANY(CAST(:statuses AS text[]))"]
    params: dict = {"c": course_id, "n": limit + 1,
                    "statuses": [filters["status"]] if filters["status"] else ["draft", "approved"]}  # fmt: skip
    for name, column in (("type", "qtype"), ("difficulty", "difficulty"), ("bloom_level", "bloom_level"), ("topic_id", "topic_id")):
        if filters[name]:
            clauses.append(f"{column} = :{name}")
            params[name] = filters[name]
    if filters["q"]:
        escaped = filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("text ILIKE :q ESCAPE '\\'")
        params["q"] = f"%{escaped}%"
    if position is not None:
        clauses.append("(updated_at, id) < (:k, :last)")
        params["k"], params["last"] = position
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_exam_owner(session, user, course_id)
        rows = session.execute(
            text("SELECT id, status, version, qtype, difficulty, bloom_level, topic_id, text, approved_revision_no, "
                 f"updated_at FROM questions WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC LIMIT :n"),
            params,
        ).all()
        page = rows[:limit]
        topics = dict(session.execute(text("SELECT id, name FROM topics WHERE course_id = :c AND id = ANY(CAST(:ids AS uuid[]))"),
                                      {"c": course_id, "ids": sorted({r.topic_id for r in page if r.topic_id})}).all())  # fmt: skip
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(endpoint, user.id, course_id, None, filters, page[-1].updated_at, page[-1].id)
    return {"items": [question_summary(r, topics) for r in page], "next_cursor": next_cursor}


# --- E4 replace -----------------------------------------------------------------------------------------------


def replace_question(factory, user, course_id, question_id, expected: int, content: QuestionContent) -> dict:
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        q = load_question(session, course_id, question_id, lock=" FOR UPDATE")
        if q is None:
            raise question_not_found()
        if q.version != expected:
            raise version_mismatch(q.version)
        if q.status == "retired":
            raise question_retired()
        if content_from_row(q) == content:
            revoked = False  # an identical body changes nothing (no version bump)
        else:
            check_references(session, course_id, content)
            revoked = q.status == "approved"
            session.execute(
                text("UPDATE questions SET topic_id = :t, qtype = :qtype, difficulty = :d, text = :stem, "
                     "choices = CAST(:choices AS jsonb), answer_key = CAST(:key AS jsonb), explanation = :expl, "
                     "citations = CAST(:cit AS jsonb), content_hash = :hash, bloom_level = :bloom, status = 'draft', "
                     "version = version + 1, updated_at = now() WHERE id = :q AND version = :v"),  # fmt: skip
                {"q": q.id, "v": expected, **_content_params(content)},
            )
            details = {"content_sha256": content.content_hash(), "previous_content_sha256": q.content_hash}
            audit.record(session, course_id=course_id, actor_id=user.id, entity_kind="question", entity_id=q.id,
                         action="question_updated", entity_version=expected + 1, details=details)  # fmt: skip
            if revoked:
                audit.record(session, course_id=course_id, actor_id=user.id, entity_kind="question", entity_id=q.id,
                             action="question_approval_revoked", entity_version=expected + 1,
                             details={"revision_no": q.approved_revision_no})  # fmt: skip
            session.commit()
    return {"question": deliver(factory, user, course_id, question_id), "approval_revoked": revoked}


# --- E5 approve, E6 retire --------------------------------------------------------------------------------------


def _frozen_refs(session: Session, course_id, q) -> list[dict]:
    """Validate page references and freeze titles while holding the document locks through approval."""
    refs = list(q.citations or [])
    if not refs:
        return []
    documents = {d.id: d for d in session.execute(
        text("SELECT id, title, mime_type, page_count FROM documents "
             "WHERE course_id = :c AND origin = 'class' AND id = ANY(CAST(:ids AS uuid[])) "
             "ORDER BY id FOR SHARE"),
        {"c": course_id, "ids": sorted({uuid.UUID(r["document_id"]) for r in refs})},
    ).all()}  # fmt: skip
    frozen = []
    for ref in refs:
        document = documents.get(uuid.UUID(ref["document_id"]))
        try:
            title = public_title(document.title) if document is not None else None
        except VerificationFailed:
            title = None
        if title is None:
            raise invalid_question("source_refs", "source_ref_unavailable",
                                   "A referenced document is no longer available in this course; edit the source references first.")  # fmt: skip
        if document.mime_type == PDF_MEDIA_TYPE and document.page_count is None:
            raise invalid_question(
                "source_refs", "source_ref_unavailable",
                "The PDF's page count is unavailable. Wait for parsing or edit the reference before approval.",
            )
        page = ref["locator"]["page"]
        _check_page_bound(page, document.page_count)
        frozen.append({"document_id": ref["document_id"], "locator": ref["locator"], "title": title,
                       "locator_label": locator_label(page)})  # fmt: skip
    return frozen


def approve_question(factory, user, course_id, question_id, expected: int) -> Answer:
    replayed = None
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        q = load_question(session, course_id, question_id, lock=" FOR UPDATE")
        if q is None:
            raise question_not_found()
        if q.version != expected:
            event = audit.transition_event(session, q.id, "question_approved", expected + 1)
            if event is None:
                raise version_mismatch(q.version)
            replayed = event.details.get("revision_no")
        elif q.status == "approved":
            replayed = q.approved_revision_no
        elif q.status == "retired":
            raise question_retired()
        else:
            refs = _frozen_refs(session, course_id, q)
            number = session.execute(text("SELECT coalesce(max(revision_no), 0) + 1 FROM exam_question_revisions "
                                          "WHERE question_id = :q"), {"q": q.id}).scalar_one()  # fmt: skip
            session.execute(
                text("INSERT INTO exam_question_revisions (question_id, revision_no, course_id, topic_id, qtype, difficulty, "
                     "bloom_level, stem, choices, answer_key, explanation, source_refs, content_sha256, approved_by, approved_at) "
                     "VALUES (:q, :n, :c, :t, :qtype, :d, :bloom, :stem, CAST(:choices AS jsonb), CAST(:key AS jsonb), :expl, "
                     "CAST(:refs AS jsonb), :hash, :u, now())"),  # fmt: skip
                {"q": q.id, "n": number, "c": course_id, "t": q.topic_id, "qtype": q.qtype, "d": q.difficulty,
                 "bloom": q.bloom_level, "stem": q.text, "choices": None if q.choices is None else json.dumps(q.choices),
                 "key": json.dumps(q.answer_key), "expl": q.explanation, "refs": json.dumps(refs), "hash": q.content_hash,
                 "u": user.id},  # fmt: skip
            )
            session.execute(
                text("UPDATE questions SET status = 'approved', approved_revision_no = :n, version = version + 1, "
                     "updated_at = now() WHERE id = :q AND version = :v"),  # fmt: skip
                {"n": number, "q": q.id, "v": expected},
            )
            audit.record(session, course_id=course_id, actor_id=user.id, entity_kind="question", entity_id=q.id,
                         action="question_approved", entity_version=expected + 1,
                         details={"revision_no": number, "content_sha256": q.content_hash})  # fmt: skip
            session.commit()
    view = deliver(factory, user, course_id, question_id)
    if replayed is None:
        return Answer(200, view, {})
    return Answer(200, {**view, "replayed_revision": replayed}, REPLAYED)


def retire_question(factory, user, course_id, question_id, expected: int) -> Answer:
    replay = False
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        q = load_question(session, course_id, question_id, lock=" FOR UPDATE")
        if q is None:
            raise question_not_found()
        if q.version != expected:
            if audit.transition_event(session, q.id, "question_retired", expected + 1) is None:
                raise version_mismatch(q.version)
            replay = True
        elif q.status == "retired":
            replay = True
        else:
            session.execute(text("UPDATE questions SET status = 'retired', version = version + 1, updated_at = now() "
                                 "WHERE id = :q AND version = :v"), {"q": q.id, "v": expected})  # fmt: skip
            audit.record(session, course_id=course_id, actor_id=user.id, entity_kind="question", entity_id=q.id,
                         action="question_retired", entity_version=expected + 1, details={"previous_status": q.status})  # fmt: skip
            session.commit()
    return Answer(200, deliver(factory, user, course_id, question_id), REPLAYED if replay else {})


# --- E7 revisions, question history ------------------------------------------------------------------------------


def list_revisions(factory, user, course_id, question_id, limit: int, cursor: str | None) -> dict:
    endpoint = "exam_questions.revisions"
    position = decode_cursor(cursor, endpoint, user.id, course_id, question_id, {}, integer_key=True)
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_exam_owner(session, user, course_id)
        if load_question(session, course_id, question_id) is None:
            raise question_not_found()
        rows = session.execute(
            text("SELECT r.*, u.display_name AS approver FROM exam_question_revisions r JOIN users u ON u.id = r.approved_by "
                 "WHERE r.question_id = :q AND r.course_id = :c AND (CAST(:before AS integer) IS NULL OR r.revision_no < :before) "
                 "ORDER BY r.revision_no DESC LIMIT :n"),  # fmt: skip
            {"q": question_id, "c": course_id, "before": None if position is None else position[0], "n": limit + 1},
        ).all()
        page = rows[:limit]
        topics = dict(session.execute(text("SELECT id, name FROM topics WHERE course_id = :c AND id = ANY(CAST(:ids AS uuid[]))"),
                                      {"c": course_id, "ids": sorted({r.topic_id for r in page if r.topic_id})}).all())  # fmt: skip
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(endpoint, user.id, course_id, question_id, {}, page[-1].revision_no, question_id)
    return {"items": [revision_view(r, topics, r.approver) for r in page], "next_cursor": next_cursor}


def history(factory, user, course_id, entity_kind: str, entity_id, limit: int, cursor: str | None, endpoint: str,
            exists) -> dict:  # fmt: skip
    """Audit events of one question or exam (with its items and exports), newest first."""
    from .views import audit_view

    position = decode_cursor(cursor, endpoint, user.id, course_id, entity_id, {})
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_exam_owner(session, user, course_id)
        exists(session)
        params = {"c": course_id, "k": entity_kind, "e": entity_id, "n": limit + 1}
        extra = ""
        if position is not None:
            extra = " AND (a.created_at, a.id) < (:at, :last)"
            params["at"], params["last"] = position
        rows = session.execute(
            text("SELECT a.id, a.action, a.entity_kind, a.entity_id, a.entity_version, a.details, a.created_at, "
                 "u.display_name AS actor_name FROM exam_audit_events a JOIN users u ON u.id = a.actor_id "
                 f"WHERE a.course_id = :c AND a.entity_kind = :k AND a.entity_id = :e{extra} "
                 "ORDER BY a.created_at DESC, a.id DESC LIMIT :n"),  # fmt: skip
            params,
        ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(endpoint, user.id, course_id, entity_id, {}, page[-1].created_at, page[-1].id)
    return {"items": [audit_view(r) for r in page], "next_cursor": next_cursor}
