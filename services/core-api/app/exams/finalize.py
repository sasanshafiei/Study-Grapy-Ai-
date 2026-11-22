"""Preview (E18), finalize (E19), archive (E20), duplicate (E21) and exam history (E24).

Finalization (contract E19, architecture §7.1): the request carries If-Match and the preview_fingerprint
the professor reviewed. Under the documented locks (users, courses FOR SHARE; exam FOR UPDATE; blueprint
rows FOR SHARE and items FOR UPDATE in ascending id; questions FOR SHARE in ascending id) the digest is
recomputed from the locked rows; any difference is 412 exam_preview_stale before anything is written (the
current digest is not returned). Readiness, the strict blueprint decision (with an optional explicit
deviation reason) and the export budget are then evaluated on exactly that snapshot; every item is pinned
to the approved revision the digest named; course header, totals, content hash and size,
finalized_version, blueprint decision and the accepted fingerprint are frozen; the audit event records
them; all in one transaction. exams_guard re-verifies items, pins and blueprint in the database.

A repeated finalize is a replay only for the identical intent: finalized_version = If-Match + 1 and the
same fingerprint and deviation reason as stored; the same old If-Match with anything else is 409
finalize_intent_mismatch.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from ..errors import ApiError
from ..quiz.access import short_tx
from . import audit, idempotency
from . import snapshot as snap
from .access import exam_not_found, require_exam_owner
from .bank import REPLAYED, Answer
from .builder import _check_exam_limit, _record, budget_exceeded, deliver, lock_exam
from .http import mutation_response_read, version_mismatch
from .validation import copy_title
from .views import exam_view


def preview(factory, settings, user, course_id, exam_id, variant: str) -> dict:
    with short_tx(factory, read_only=True, snapshot=True) as session:
        course = require_exam_owner(session, user, course_id)
        state = snap.load_exam(session, course_id, exam_id, course)
        if state is None:
            raise exam_not_found()
        draft = state.exam.status == "draft"
        header = snap.header_values(state)
        if variant == "student":  # built from the student-only query (no answer data loaded for it)
            model = snap.student_print_model(state.exam, header, snap.student_print_rows(session, state.exam))
            answer_key = None
        else:
            model = snap.answer_key_print_model(state)
            answer_key = model["answer_key"]
        readiness = None
        fingerprint = None
        if draft:
            full = snap.readiness(state, settings)
            readiness = {"ready": full["ready"], "issues": full["issues"], "warnings": full["warnings"]}
            fingerprint = snap.digest(snap.fingerprint_input(session, state.exam, course))
        exam = state.exam
    return {"exam_id": str(exam.id), "status": exam.status, "version": exam.version, "variant": variant,
            "header": model["header"], "questions": model["questions"], "answer_key": answer_key,
            "readiness": readiness, "preview_fingerprint": fingerprint}  # fmt: skip


def not_ready(issues: list) -> ApiError:
    return ApiError(409, "exam_not_ready", "The exam is not ready to be finalized.", extra={"issues": issues})


def finalize(factory, settings, user, course_id, exam_id, expected: int, fingerprint: str, reason: str | None) -> Answer:
    replay = False
    with short_tx(factory) as session:
        course = require_exam_owner(session, user, course_id, lock=True)
        state = snap.load_exam(session, course_id, exam_id, course, lock=True)
        if state is None:
            raise exam_not_found()
        exam = state.exam
        if exam.version != expected:
            if exam.status in ("finalized", "archived") and exam.finalized_version == expected + 1:
                if exam.finalize_preview_fingerprint == fingerprint and exam.blueprint_deviation_reason == reason:
                    replay = True
                else:
                    raise ApiError(409, "finalize_intent_mismatch",
                                   "This exam was already finalized with a different reviewed preview or reason. Reload it.")  # fmt: skip
            else:
                raise version_mismatch(exam.version)
        elif exam.status != "draft":
            raise ApiError(409, "exam_not_draft", "Only a draft exam can be finalized.")
        if not replay:
            _finalize_locked(session, settings, user, course_id, state, course, expected, fingerprint, reason)
            session.commit()
    return Answer(200, deliver(factory, settings, user, course_id, exam_id), REPLAYED if replay else {})


def _finalize_locked(session, settings, user, course_id, state, course, expected, fingerprint, reason) -> None:
    if snap.digest(snap.state_fingerprint_object(state)) != fingerprint:
        raise ApiError(412, "exam_preview_stale",
                       "The exam or its questions changed after this preview. Preview it again and review it before finalizing.",
                       extra={"current_version": state.exam.version})  # fmt: skip
    result = snap.readiness(state, settings)
    blocking = [i for i in result["issues"] if not i["overridable"]]
    deviations = [i for i in result["issues"] if i["overridable"]]
    if blocking or (deviations and reason is None):
        raise not_ready(result["issues"])
    if reason is not None and not deviations:
        raise ApiError(422, "blueprint_override_not_needed", "The blueprint is fulfilled or absent; send no deviation reason.")
    blueprint_status = "none" if not state.rows else ("deviation_accepted" if deviations else "fulfilled")
    session.execute(
        text("UPDATE exam_items i SET question_revision_no = q.approved_revision_no, updated_at = now() FROM questions q "
             "WHERE q.id = i.question_id AND i.exam_id = :e"),  # fmt: skip
        {"e": state.exam.id},
    )
    size = snap.content_chars(state)
    if size > settings.exam_export_max_content_chars:  # already a blocking issue; kept as a direct guard
        raise budget_exceeded(size, settings.exam_export_max_content_chars)
    content_sha = snap.content_sha256(state)
    total = sum(s.item.points for s in state.items)
    session.execute(
        text("UPDATE exams SET status = 'finalized', version = version + 1, finalized_version = version + 1, "
             "finalized_at = now(), finalized_by = :u, course_code_snapshot = :code, course_name_snapshot = :name, "
             "item_count = :n, total_points = :t, content_sha256 = :sha, content_chars = :chars, blueprint_status = :bs, "
             "blueprint_deviation_reason = :reason, finalize_preview_fingerprint = :fp, updated_at = now() "
             "WHERE id = :e AND version = :v"),  # fmt: skip
        {"u": user.id, "code": course.code or "", "name": course.name or "", "n": len(state.items), "t": total,
         "sha": content_sha, "chars": size, "bs": blueprint_status, "reason": reason, "fp": fingerprint,
         "e": state.exam.id, "v": expected},  # fmt: skip
    )
    rows = [[i["blueprint_row_id"], i["count"], i["assigned"]] for i in deviations if "blueprint_row_id" in i and "count" in i]
    details = {"preview_fingerprint": fingerprint, "content_sha256": content_sha, "content_chars": size,
               "item_count": len(state.items), "total_points": total, "blueprint_status": blueprint_status}  # fmt: skip
    if deviations:
        details["deviations"] = {
            "rows": rows[:40], "rows_truncated": len(rows) > 40,
            "unassigned_items": sum(1 for i in deviations if i["code"] == "item_not_in_blueprint"),
            "criteria_mismatches": sum(1 for i in deviations if i["code"] == "blueprint_criteria_mismatch"),
        }  # fmt: skip
    _record(session, course_id, user, state.exam.id, "exam_finalized", expected + 1, details)


def archive(factory, settings, user, course_id, exam_id, expected: int) -> Answer:
    replay = False
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        exam = lock_exam(session, course_id, exam_id, None, draft=False)
        if exam.version != expected:
            if audit.transition_event(session, exam.id, "exam_archived", expected + 1) is None:
                raise version_mismatch(exam.version)
            replay = True
        elif exam.status == "archived":
            replay = True
        else:
            session.execute(text("UPDATE exams SET status = 'archived', archived_at = now(), version = version + 1, "
                                 "updated_at = now() WHERE id = :e AND version = :v"), {"e": exam.id, "v": expected})  # fmt: skip
            _record(session, course_id, user, exam.id, "exam_archived", expected + 1, {"previous_status": exam.status})
            session.commit()
    return Answer(200, deliver(factory, settings, user, course_id, exam_id), REPLAYED if replay else {})


# --- E21 duplicate ----------------------------------------------------------------------------------------------


def duplicate_report(session, new_exam_id, source_exam_id) -> list[dict]:
    source = snap.source_revisions(session, source_exam_id)
    items = session.execute(
        text("SELECT i.id, i.question_id, q.status, q.approved_revision_no, q.content_hash FROM exam_items i "
             "JOIN questions q ON q.id = i.question_id WHERE i.exam_id = :e ORDER BY i.position"),  # fmt: skip
        {"e": new_exam_id},
    ).all()
    report = []
    for i in items:
        revision = source.get(i.question_id)
        report.append({"item_id": str(i.id), "question_id": str(i.question_id),
                       "source_revision": None if revision is None else revision.revision_no,
                       "current_status": i.status, "current_approved_revision": i.approved_revision_no,
                       "content": snap.changed_since_source(i, revision)})  # fmt: skip
    return report


def _deliver_duplicate(factory, settings, user, course_id, exam_id) -> dict:
    with mutation_response_read(), short_tx(factory, read_only=True, snapshot=True) as session:
        course = require_exam_owner(session, user, course_id)
        state = snap.load_exam(session, course_id, exam_id, course)
        if state is None:
            raise exam_not_found()
        fingerprint = snap.digest(snap.fingerprint_input(session, state.exam, course)) if state.exam.status == "draft" else None
        view = exam_view(state, settings, fingerprint)
        report = duplicate_report(session, exam_id, state.exam.derived_from_exam_id)
    return {"exam": view, "duplicate_report": report}


def duplicate(factory, settings, user, course_id, source_id, key: uuid.UUID, title: str | None) -> Answer:
    operation = "exam_duplicate"
    print_ = idempotency.fingerprint(operation, course_id, {"source_exam_id": str(source_id), "title": title})
    try:
        with short_tx(factory) as session:
            course = require_exam_owner(session, user, course_id, lock=True)
            existing = idempotency.lookup(session, user.id, key, operation, print_)
            if existing is None:
                source = session.execute(
                    text(f"SELECT {snap.EXAM_COLUMNS} FROM exams WHERE id = :e AND course_id = :c FOR SHARE"),
                    {"e": source_id, "c": course_id},
                ).first()
                if source is None:
                    raise exam_not_found()
                idempotency.lock_capacity_for_create(session, course_id, user.id, key, operation, print_)
                _check_exam_limit(session, settings, course_id)
                new_id = session.execute(
                    text("INSERT INTO exams (course_id, created_by, title, instructions, duration_minutes, status, version, "
                         "derived_from_exam_id) VALUES (:c, :u, :t, :i, :d, 'draft', 1, :src) RETURNING id"),  # fmt: skip
                    {"c": course_id, "u": user.id, "t": title or copy_title(source.title), "i": source.instructions,
                     "d": source.duration_minutes, "src": source.id},  # fmt: skip
                ).scalar_one()
                rows = session.execute(
                    text(f"SELECT {snap.ROW_COLUMNS} FROM exam_blueprint_rows WHERE exam_id = :e ORDER BY position"),
                    {"e": source.id},
                ).all()
                mapping = {}
                for row in rows:
                    mapping[row.id] = session.execute(
                        text("INSERT INTO exam_blueprint_rows (exam_id, course_id, position, topic_id, qtype, difficulty, "
                             "bloom_level, question_count, points_each) VALUES (:e, :c, :p, :t, :q, :d, :b, :n, :pe) RETURNING id"),  # fmt: skip
                        {"e": new_id, "c": course_id, "p": row.position, "t": row.topic_id, "q": row.qtype,
                         "d": row.difficulty, "b": row.bloom_level, "n": row.question_count, "pe": row.points_each},  # fmt: skip
                    ).scalar_one()
                items = session.execute(
                    text(f"SELECT {snap.ITEM_COLUMNS} FROM exam_items WHERE exam_id = :e ORDER BY position"), {"e": source.id}
                ).all()
                for item in items:
                    session.execute(
                        text("INSERT INTO exam_items (exam_id, course_id, question_id, position, points, blueprint_row_id) "
                             "VALUES (:e, :c, :q, :p, :pts, :r)"),  # fmt: skip
                        {"e": new_id, "c": course_id, "q": item.question_id, "p": item.position, "pts": item.points,
                         "r": mapping.get(item.blueprint_row_id)},  # fmt: skip
                    )
                state = snap.load_exam(session, course_id, new_id, course, with_source=False)
                size = snap.content_chars(state)
                if size > settings.exam_export_max_content_chars:
                    raise budget_exceeded(size, settings.exam_export_max_content_chars)
                idempotency.store(session, user.id, key, operation, print_, course_id, new_id)
                _record(session, course_id, user, new_id, "exam_duplicated", 1,
                        {"source_exam_id": str(source.id), "source_version": source.version, "items": len(items),
                         "rows": len(rows)})  # fmt: skip
                session.commit()
                return Answer(201, _deliver_duplicate(factory, settings, user, course_id, new_id),
                              {"Location": f"/api/v1/courses/{course_id}/exams/{new_id}"})  # fmt: skip
    except idempotency.KeyTaken:
        with short_tx(factory, read_only=True) as session:
            require_exam_owner(session, user, course_id)
            existing = idempotency.lookup(session, user.id, key, operation, print_)
    return Answer(200, _deliver_duplicate(factory, settings, user, course_id, existing), REPLAYED)


def exam_exists(course_id, exam_id):
    def check(session) -> None:
        if session.execute(text("SELECT 1 FROM exams WHERE id = :e AND course_id = :c"), {"e": exam_id, "c": course_id}).first() is None:
            raise exam_not_found()

    return check
