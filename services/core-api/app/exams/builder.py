"""Exam drafts, blueprint and items (E8-E17).

Every composition change is versioned on the exam: the transaction locks the exam row FOR UPDATE first
(after the users/courses FOR SHARE authorization locks), checks If-Match and the draft status, makes its
change and bumps the exam version exactly once with a conditional UPDATE (the database trigger also
requires +1). Blueprint replacement, item insert/remove/reorder and fill are therefore serialized per
exam; the loser of a race gets 412. Position constraints are DEFERRABLE INITIALLY DEFERRED and are set
IMMEDIATE (and revalidated) before the version bump, so intermediate duplicates inside one transaction are
legal and the committed state is always 1..n. No statement uses ON CONFLICT on a position constraint.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..quiz.access import short_tx
from . import audit, idempotency
from . import snapshot as snap
from .access import exam_item_not_found, exam_not_found, question_not_found, require_exam_owner
from .bank import REPLAYED, Answer
from .http import decode_cursor, encode_cursor, mutation_response_read, version_mismatch
from .validation import BlueprintRowRequest, invalid_blueprint, matches_row
from .views import exam_summary, exam_view, load_question

MAX_AUDIT_IDS = 80


def exam_not_draft() -> ApiError:
    return ApiError(409, "exam_not_draft", "Only a draft exam can be changed. Duplicate it to make a new draft.")


def budget_exceeded(size: int, budget: int) -> ApiError:
    return ApiError(409, "export_budget_exceeded", "The exam would exceed the export size limit.",
                    extra={"content_chars": size, "export_budget": budget})  # fmt: skip


def bounded_ids(ids: list) -> dict:
    values = [str(i) for i in ids]
    if len(values) <= MAX_AUDIT_IDS:
        return {"ids": values}
    return {"ids": values[:MAX_AUDIT_IDS], "truncated": True, "count": len(values)}


def read_exam(factory, settings, user, course_id, exam_id) -> dict:
    """The current Exam, from one REPEATABLE READ READ ONLY snapshot that re-checks authorization first."""
    with short_tx(factory, read_only=True, snapshot=True) as session:
        course = require_exam_owner(session, user, course_id)
        state = snap.load_exam(session, course_id, exam_id, course)
        if state is None:
            raise exam_not_found()
        fingerprint = snap.digest(snap.fingerprint_input(session, state.exam, course)) if state.exam.status == "draft" else None
        return exam_view(state, settings, fingerprint)


def deliver(factory, settings, user, course_id, exam_id) -> dict:
    with mutation_response_read():
        return read_exam(factory, settings, user, course_id, exam_id)


def lock_exam(session: Session, course_id, exam_id, expected: int | None, *, draft: bool = True):
    exam = session.execute(
        text(f"SELECT {snap.EXAM_COLUMNS} FROM exams WHERE id = :e AND course_id = :c FOR UPDATE"),
        {"e": exam_id, "c": course_id},
    ).first()
    if exam is None:
        raise exam_not_found()
    if expected is not None and exam.version != expected:
        raise version_mismatch(exam.version)
    if draft and exam.status != "draft":
        raise exam_not_draft()
    return exam


def bump(session: Session, exam_id, expected: int, extra: str = "", params: dict | None = None) -> None:
    result = session.execute(
        text(f"UPDATE exams SET version = version + 1, updated_at = now(){extra} WHERE id = :e AND version = :v"),
        {"e": exam_id, "v": expected, **(params or {})},
    )
    if result.rowcount != 1:  # the row is locked FOR UPDATE: a defect, never a race
        raise RuntimeError("exam version bump lost")


def _record(session, course_id, user, exam_id, action, version, details) -> None:
    audit.record(session, course_id=course_id, actor_id=user.id, entity_kind="exam", entity_id=exam_id, action=action,
                 entity_version=version, details=details)  # fmt: skip


def _items_immediate(session: Session, exam_id) -> None:
    session.execute(text("SET CONSTRAINTS exam_items_exam_id_position_key IMMEDIATE"))
    n, lo, hi, distinct = session.execute(
        text("SELECT count(*), min(position), max(position), count(DISTINCT position) FROM exam_items WHERE exam_id = :e"),
        {"e": exam_id},
    ).one()
    if n and (lo != 1 or hi != n or distinct != n):
        raise RuntimeError("exam item positions are not 1..n")


def _check_budget(session: Session, settings, course_id, exam_id, course) -> None:
    state = snap.load_exam(session, course_id, exam_id, course, with_source=False)
    size = snap.content_chars(state)
    if size > settings.exam_export_max_content_chars:
        raise budget_exceeded(size, settings.exam_export_max_content_chars)


# --- E8 list, E9 create, E11 metadata ------------------------------------------------------------------------


def list_exams(factory, user, course_id, status: str | None, limit: int, cursor: str | None) -> dict:
    endpoint = "exams.list"
    filters = {"status": status or ""}
    position = decode_cursor(cursor, endpoint, user.id, course_id, None, filters)
    params = {"c": course_id, "s": [status] if status else ["draft", "finalized"], "n": limit + 1}
    extra = ""
    if position is not None:
        extra = " AND (e.updated_at, e.id) < (:k, :last)"
        params["k"], params["last"] = position
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_exam_owner(session, user, course_id)
        rows = session.execute(
            text("SELECT e.id, e.status, e.version, e.title, e.finalized_at, e.archived_at, e.derived_from_exam_id, e.updated_at, "
                 "coalesce(e.item_count, (SELECT count(*) FROM exam_items i WHERE i.exam_id = e.id)) AS item_count_live, "
                 "coalesce(e.total_points, (SELECT coalesce(sum(i.points), 0) FROM exam_items i WHERE i.exam_id = e.id)) "
                 "AS total_points_live FROM exams e "
                 f"WHERE e.course_id = :c AND e.status = ANY(CAST(:s AS text[])){extra} ORDER BY e.updated_at DESC, e.id DESC LIMIT :n"),  # fmt: skip
            params,
        ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(endpoint, user.id, course_id, None, filters, page[-1].updated_at, page[-1].id)
    return {"items": [exam_summary(r) for r in page], "next_cursor": next_cursor}


def _check_exam_limit(session: Session, settings, course_id) -> None:
    count = session.execute(text("SELECT count(*) FROM exams WHERE course_id = :c"), {"c": course_id}).scalar_one()
    if count >= settings.exam_max_exams:
        raise ApiError(409, "exam_limit_reached", "This course has reached its exam limit.",
                       extra={"limit": settings.exam_max_exams})  # fmt: skip


def create_exam(factory, settings, user, course_id, key: uuid.UUID, fields: dict) -> Answer:
    operation = "exam_create"
    print_ = idempotency.fingerprint(operation, course_id, fields)
    try:
        with short_tx(factory) as session:
            require_exam_owner(session, user, course_id, lock=True)
            existing = idempotency.lookup(session, user.id, key, operation, print_)
            if existing is None:
                idempotency.lock_capacity_for_create(session, course_id, user.id, key, operation, print_)
                _check_exam_limit(session, settings, course_id)
                exam_id = session.execute(
                    text("INSERT INTO exams (course_id, created_by, title, instructions, duration_minutes, status, version) "
                         "VALUES (:c, :u, :t, :i, :d, 'draft', 1) RETURNING id"),  # fmt: skip
                    {"c": course_id, "u": user.id, "t": fields["title"], "i": fields["instructions"],
                     "d": fields["duration_minutes"]},  # fmt: skip
                ).scalar_one()
                idempotency.store(session, user.id, key, operation, print_, course_id, exam_id)
                _record(session, course_id, user, exam_id, "exam_created", 1, {})
                session.commit()
                return Answer(201, deliver(factory, settings, user, course_id, exam_id),
                              {"Location": f"/api/v1/courses/{course_id}/exams/{exam_id}"})  # fmt: skip
    except idempotency.KeyTaken:
        with short_tx(factory, read_only=True) as session:
            require_exam_owner(session, user, course_id)
            existing = idempotency.lookup(session, user.id, key, operation, print_)
    return Answer(200, deliver(factory, settings, user, course_id, existing), REPLAYED)


def update_exam(factory, settings, user, course_id, exam_id, expected: int, fields: dict) -> dict:
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        exam = lock_exam(session, course_id, exam_id, expected)
        changed = {k: v for k, v in fields.items() if getattr(exam, k) != v}
        if changed:
            assignments = "".join(f", {k} = :{k}" for k in changed)
            bump(session, exam_id, expected, assignments, changed)
            _record(session, course_id, user, exam_id, "exam_updated", expected + 1, {"fields": sorted(changed)})
            session.commit()
    return deliver(factory, settings, user, course_id, exam_id)


# --- E12 blueprint replacement (V3, H8; architecture §4.5) ----------------------------------------------------------


def replace_blueprint(factory, settings, user, course_id, exam_id, expected: int, rows: list[BlueprintRowRequest]) -> dict:
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)  # 1. users, courses FOR SHARE
        lock_exam(session, course_id, exam_id, expected)  # 2. exam FOR UPDATE, draft, If-Match
        current = session.execute(  # 3. the current rows FOR UPDATE, ascending id
            text(f"SELECT {snap.ROW_COLUMNS} FROM exam_blueprint_rows WHERE exam_id = :e ORDER BY id FOR UPDATE"),
            {"e": exam_id},
        ).all()
        by_id = {r.id: r for r in current}
        # 4. the whole request is validated before any write
        for index, row in enumerate(rows):
            if row.id is not None and row.id not in by_id:  # also an id of another exam of the same professor
                raise invalid_blueprint(index, "unknown_row", "This blueprint row does not exist in this exam.")
        topic_ids = sorted({r.topic_id for r in rows if r.topic_id})
        known = set(session.execute(text("SELECT id FROM topics WHERE course_id = :c AND id = ANY(CAST(:ids AS uuid[])) ORDER BY id FOR SHARE"),
                                    {"c": course_id, "ids": topic_ids}).scalars())  # fmt: skip
        for index, row in enumerate(rows):
            if row.topic_id is not None and row.topic_id not in known:
                raise invalid_blueprint(index, "topic", "The topic is not a topic of this course.")
        kept_ids = [r.id for r in rows if r.id is not None]
        removed = sorted(set(by_id) - set(kept_ids))
        identical = not removed and len(kept_ids) == len(rows) == len(current) and all(
            (by_id[r.id].position, by_id[r.id].topic_id, by_id[r.id].qtype, by_id[r.id].difficulty, by_id[r.id].bloom_level,
             by_id[r.id].question_count, by_id[r.id].points_each)
            == (index + 1, r.topic_id, r.qtype, r.difficulty, r.bloom_level, r.count, r.points_each)
            for index, r in enumerate(rows)
        )  # fmt: skip
        if identical:
            change = {"kept": len(rows), "inserted": 0, "removed": 0, "unassigned_item_ids": []}
        else:
            # 5. unassign the items of removed rows, 6. delete those rows
            unassigned = list(session.execute(
                text("UPDATE exam_items SET blueprint_row_id = NULL, updated_at = now() WHERE exam_id = :e "
                     "AND blueprint_row_id = ANY(CAST(:removed AS uuid[])) RETURNING id"),
                {"e": exam_id, "removed": removed},
            ).scalars())  # fmt: skip
            session.execute(text("DELETE FROM exam_blueprint_rows WHERE exam_id = :e AND id = ANY(CAST(:removed AS uuid[]))"),
                            {"e": exam_id, "removed": removed})  # fmt: skip
            # 7. update retained rows and insert new ones at their new positions (deferred position key)
            inserted = 0
            for index, row in enumerate(rows):
                values = {"e": exam_id, "c": course_id, "p": index + 1, "t": row.topic_id, "q": row.qtype,
                          "d": row.difficulty, "b": row.bloom_level, "n": row.count, "pe": row.points_each}  # fmt: skip
                if row.id is not None:
                    session.execute(
                        text("UPDATE exam_blueprint_rows SET position = :p, topic_id = :t, qtype = :q, difficulty = :d, "
                             "bloom_level = :b, question_count = :n, points_each = :pe WHERE id = :r AND exam_id = :e"),  # fmt: skip
                        {**values, "r": row.id},
                    )
                else:
                    inserted += 1
                    session.execute(
                        text("INSERT INTO exam_blueprint_rows (exam_id, course_id, position, topic_id, qtype, difficulty, "
                             "bloom_level, question_count, points_each) VALUES (:e, :c, :p, :t, :q, :d, :b, :n, :pe)"),  # fmt: skip
                        values,
                    )
            # 8. check the deferred key now and revalidate; a failure here is a defect (rollback, 500)
            session.execute(text("SET CONSTRAINTS exam_blueprint_rows_exam_id_position_key IMMEDIATE"))
            n, lo, hi, distinct, total = session.execute(
                text("SELECT count(*), min(position), max(position), count(DISTINCT position), coalesce(sum(question_count), 0) "
                     "FROM exam_blueprint_rows WHERE exam_id = :e"),  # fmt: skip
                {"e": exam_id},
            ).one()
            foreign = session.execute(
                text("SELECT count(*) FROM exam_items i WHERE i.exam_id = :e AND i.blueprint_row_id IS NOT NULL AND NOT EXISTS "
                     "(SELECT 1 FROM exam_blueprint_rows r WHERE r.id = i.blueprint_row_id AND r.exam_id = i.exam_id)"),  # fmt: skip
                {"e": exam_id},
            ).scalar_one()
            if n != len(rows) or (n and (lo != 1 or hi != n or distinct != n)) or total > 200 or foreign:
                raise RuntimeError("blueprint revalidation failed")
            # 9. one version bump, the audit event, commit (the deferred key is checked again at commit)
            bump(session, exam_id, expected)
            change = {"kept": len(kept_ids), "inserted": inserted, "removed": len(removed),
                      "unassigned_item_ids": [str(i) for i in unassigned]}  # fmt: skip
            _record(session, course_id, user, exam_id, "blueprint_replaced", expected + 1,
                    {"kept": change["kept"], "inserted": inserted, "removed": len(removed),
                     "unassigned_items": bounded_ids(unassigned)})  # fmt: skip
            session.commit()
    return {"exam": deliver(factory, settings, user, course_id, exam_id), "blueprint_change": change}


# --- E13 add, E14 update, E15 remove, E16 reorder ------------------------------------------------------------


def _row_of_exam(session: Session, exam_id, row_id):
    row = session.execute(text(f"SELECT {snap.ROW_COLUMNS} FROM exam_blueprint_rows WHERE id = :r AND exam_id = :e"),
                          {"r": row_id, "e": exam_id}).first()  # fmt: skip
    if row is None:
        raise ApiError(422, "invalid_blueprint_row", "This blueprint row does not exist in this exam.",
                       extra={"reason": "unknown_row"})  # fmt: skip
    return row


def _check_row_match(row, q) -> None:
    if not matches_row(row, topic_id=q.topic_id, qtype=q.qtype, difficulty=q.difficulty, bloom_level=q.bloom_level):
        raise ApiError(422, "invalid_blueprint_row", "The question does not match this blueprint row's criteria.",
                       extra={"reason": "criteria_mismatch"})  # fmt: skip


def add_item(factory, settings, user, course_id, exam_id, expected: int, question_id, points: int,
             position: int | None, row_id) -> tuple[dict, str]:  # fmt: skip
    with short_tx(factory) as session:
        course = require_exam_owner(session, user, course_id, lock=True)
        lock_exam(session, course_id, exam_id, expected)
        count = session.execute(text("SELECT count(*) FROM exam_items WHERE exam_id = :e"), {"e": exam_id}).scalar_one()
        if count >= settings.exam_max_items:
            raise ApiError(409, "exam_item_limit", "This exam already has the maximum number of questions.",
                           extra={"limit": settings.exam_max_items})  # fmt: skip
        if position is not None and not 1 <= position <= count + 1:
            raise ApiError(422, "invalid_position", f"position must be between 1 and {count + 1}, or null.")
        q = None if question_id is None else load_question(session, course_id, question_id, lock=" FOR SHARE")
        if q is None:
            raise question_not_found()
        if q.status == "retired":
            raise ApiError(409, "question_retired", "This question is retired and can no longer be added to exams.")
        already = session.execute(text("SELECT id FROM exam_items WHERE exam_id = :e AND question_id = :q"),
                                  {"e": exam_id, "q": q.id}).scalar()  # fmt: skip
        if already is not None:
            raise ApiError(409, "question_already_in_exam", "This question is already in the exam.",
                           extra={"item_id": str(already)})  # fmt: skip
        if row_id is not None:
            _check_row_match(_row_of_exam(session, exam_id, row_id), q)
        at = count + 1 if position is None else position
        if position is not None:
            session.execute(text("UPDATE exam_items SET position = position + 1, updated_at = now() WHERE exam_id = :e "
                                 "AND position >= :p"), {"e": exam_id, "p": position})  # fmt: skip
        item_id = session.execute(
            text("INSERT INTO exam_items (exam_id, course_id, question_id, position, points, blueprint_row_id) "
                 "VALUES (:e, :c, :q, :p, :pts, :r) RETURNING id"),  # fmt: skip
            {"e": exam_id, "c": course_id, "q": q.id, "p": at, "pts": points, "r": row_id},
        ).scalar_one()
        _items_immediate(session, exam_id)
        _check_budget(session, settings, course_id, exam_id, course)
        bump(session, exam_id, expected)
        _record(session, course_id, user, exam_id, "item_added", expected + 1,
                {"item_id": str(item_id), "question_id": str(q.id), "position": at, "points": points})  # fmt: skip
        session.commit()
    return deliver(factory, settings, user, course_id, exam_id), str(item_id)


def _lock_item(session: Session, exam_id, item_id):
    item = None if item_id is None else session.execute(
        text(f"SELECT {snap.ITEM_COLUMNS} FROM exam_items WHERE id = :i AND exam_id = :e FOR UPDATE"),
        {"i": item_id, "e": exam_id},
    ).first()  # fmt: skip
    if item is None:
        raise exam_item_not_found()
    return item


_UNSET = object()


def update_item(factory, settings, user, course_id, exam_id, item_id, expected: int, points=_UNSET, row_id=_UNSET) -> dict:
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        lock_exam(session, course_id, exam_id, expected)
        item = _lock_item(session, exam_id, item_id)
        changes = {}
        if points is not _UNSET and points != item.points:
            changes["points"] = points
        if row_id is not _UNSET and row_id != item.blueprint_row_id:
            if row_id is not None:
                q = session.execute(text(f"SELECT {snap.QUESTION_COLUMNS} FROM questions WHERE id = :q FOR SHARE"),
                                    {"q": item.question_id}).one()  # fmt: skip
                _check_row_match(_row_of_exam(session, exam_id, row_id), q)
            changes["blueprint_row_id"] = row_id
        if changes:
            assignments = ", ".join(f"{k} = :{k}" for k in changes)
            session.execute(text(f"UPDATE exam_items SET {assignments}, updated_at = now() WHERE id = :i"),
                            {"i": item.id, **changes})  # fmt: skip
            bump(session, exam_id, expected)
            details = {"item_id": str(item.id)}
            if "points" in changes:
                details["points"] = changes["points"]
            if "blueprint_row_id" in changes:
                details["blueprint_row_id"] = None if changes["blueprint_row_id"] is None else str(changes["blueprint_row_id"])
            _record(session, course_id, user, exam_id, "item_updated", expected + 1, details)
            session.commit()
    return deliver(factory, settings, user, course_id, exam_id)


def remove_item(factory, settings, user, course_id, exam_id, item_id, expected: int) -> dict:
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        lock_exam(session, course_id, exam_id, expected)
        item = _lock_item(session, exam_id, item_id)
        session.execute(text("DELETE FROM exam_items WHERE id = :i"), {"i": item.id})
        session.execute(text("UPDATE exam_items SET position = position - 1, updated_at = now() WHERE exam_id = :e "
                             "AND position > :p"), {"e": exam_id, "p": item.position})  # fmt: skip
        _items_immediate(session, exam_id)
        bump(session, exam_id, expected)
        _record(session, course_id, user, exam_id, "item_removed", expected + 1,
                {"item_id": str(item.id), "question_id": str(item.question_id), "position": item.position})  # fmt: skip
        session.commit()
    return deliver(factory, settings, user, course_id, exam_id)


def reorder_items(factory, settings, user, course_id, exam_id, expected: int, order: list) -> dict:
    with short_tx(factory) as session:
        require_exam_owner(session, user, course_id, lock=True)
        lock_exam(session, course_id, exam_id, expected)
        items = session.execute(text("SELECT id, position FROM exam_items WHERE exam_id = :e ORDER BY id FOR UPDATE"),
                                {"e": exam_id}).all()  # fmt: skip
        current = {i.id: i.position for i in items}
        if len(set(order)) != len(order):
            raise ApiError(422, "invalid_order", "An item is listed twice.", extra={"reason": "duplicate"})
        if any(i is None or i not in current for i in order):
            raise ApiError(422, "invalid_order", "An item does not belong to this exam.", extra={"reason": "unknown"})
        if len(order) != len(current):
            raise ApiError(422, "invalid_order", "Every item of the exam must be listed exactly once.",
                           extra={"reason": "missing"})  # fmt: skip
        moves = [(index + 1, item_id) for index, item_id in enumerate(order) if current[item_id] != index + 1]
        if moves:
            for new_position, item_id in moves:
                session.execute(text("UPDATE exam_items SET position = :p, updated_at = now() WHERE id = :i"),
                                {"p": new_position, "i": item_id})  # fmt: skip
            _items_immediate(session, exam_id)
            bump(session, exam_id, expected)
            _record(session, course_id, user, exam_id, "items_reordered", expected + 1, {"moved": len(moves)})
            session.commit()
    return deliver(factory, settings, user, course_id, exam_id)


# --- E17 fill from the blueprint -----------------------------------------------------------------------------------


def fill_from_blueprint(factory, settings, user, course_id, exam_id, expected: int, row_ids: list | None) -> dict:
    budget = settings.exam_export_max_content_chars
    filled = []
    with short_tx(factory) as session:
        course = require_exam_owner(session, user, course_id, lock=True)
        lock_exam(session, course_id, exam_id, expected)
        state = snap.load_exam(session, course_id, exam_id, course, with_source=False)
        rows = state.rows
        if row_ids is not None:
            known = state.rows_by_id
            for index, row_id in enumerate(row_ids):
                if row_id is None or row_id not in known:
                    raise invalid_blueprint(index, "unknown_row", "This blueprint row does not exist in this exam.")
            wanted = set(row_ids)
            rows = [r for r in rows if r.id in wanted]
        size = snap.content_chars(state)
        position = len(state.items)
        in_exam = {s.question.id for s in state.items}
        stopped: str | None = None
        added_total = 0
        for row in rows:
            assigned = sum(1 for s in state.items if s.item.blueprint_row_id == row.id)
            needed = max(0, row.question_count - assigned)
            added = 0
            if needed and not stopped:
                candidate_ids = session.execute(
                    text("SELECT id FROM questions WHERE course_id = :c AND version IS NOT NULL "
                         "AND purpose = 'exam' AND status = 'approved' AND (CAST(:t AS uuid) IS NULL OR topic_id = :t) "
                         "AND (CAST(:q AS text) IS NULL OR qtype = :q) AND (CAST(:d AS text) IS NULL OR difficulty = :d) "
                         "AND (CAST(:b AS text) IS NULL OR bloom_level = :b) AND id <> ALL(CAST(:taken AS uuid[])) "
                         "ORDER BY created_at ASC, id ASC LIMIT :n"),  # fmt: skip
                    {"c": course_id, "t": row.topic_id, "q": row.qtype, "d": row.difficulty, "b": row.bloom_level,
                     "taken": sorted(in_exam), "n": needed},  # fmt: skip
                ).scalars().all()
                approved_pairs = session.execute(
                    text("SELECT id, approved_revision_no FROM questions WHERE id = ANY(CAST(:ids AS uuid[])) "
                         "AND status = 'approved' ORDER BY id FOR SHARE"),
                    {"ids": candidate_ids},
                ).all()
                revisions = snap.load_revisions(session, set(approved_pairs))
                by_question = {r.question_id: r for r in revisions.values()}
                for question_id in candidate_ids:
                    revision = by_question.get(question_id)
                    # A candidate may have been edited and reapproved before the FOR SHARE lock.
                    if revision is None or not matches_row(
                        row, topic_id=revision.topic_id, qtype=revision.qtype,
                        difficulty=revision.difficulty, bloom_level=revision.bloom_level,
                    ):
                        continue
                    if position >= settings.exam_max_items:
                        stopped = "item_limit"
                        break
                    grow = sum(len(t) for t in snap.item_texts(state, revision))
                    if size + grow > budget:
                        stopped = "export_budget"
                        break
                    position += 1
                    size += grow
                    session.execute(
                        text("INSERT INTO exam_items (exam_id, course_id, question_id, position, points, blueprint_row_id) "
                             "VALUES (:e, :c, :q, :p, :pts, :r)"),  # fmt: skip
                        {"e": exam_id, "c": course_id, "q": question_id, "p": position, "pts": row.points_each, "r": row.id},
                    )
                    in_exam.add(question_id)
                    added += 1
            missing = needed - added
            entry = {"blueprint_row_id": str(row.id), "added": added, "missing": missing,
                     "stopped_by": stopped if missing and stopped else None}  # fmt: skip
            filled.append(entry)
            added_total += added
        if added_total:
            _items_immediate(session, exam_id)
            _check_budget(session, settings, course_id, exam_id, course)
            bump(session, exam_id, expected)
            _record(session, course_id, user, exam_id, "exam_filled_from_blueprint", expected + 1,
                    {"added": added_total, "rows": [[e["blueprint_row_id"], e["added"], e["missing"]] for e in filled][:40]})  # fmt: skip
            session.commit()
    return {"exam": deliver(factory, settings, user, course_id, exam_id), "filled": filled}
