"""One consistent view of an exam: loading, readiness, preview fingerprint, export size and print models.

load_exam() reads an exam with a fixed number of statements (exam, blueprint rows, items, their questions,
the revisions they pin or would pin, topics, documents, and for duplicates the source items). With
lock=True it takes the finalization locks in the M9 lock order: exam FOR UPDATE, blueprint rows FOR SHARE
and items FOR UPDATE (each in ascending id), questions FOR SHARE in ascending id.

preview fingerprint (V3, H9; architecture §7.1): "pf1:" + SHA-256 of the canonical JSON of
  {"v": 1, "exam": {id, version, status, title, instructions, duration_minutes}, "course": {id, code, name},
   "items": [{id, position, points, blueprint_row_id, question_id, question_status, approved_revision_no,
              approved_content_sha256}] (by position),
   "blueprint": [{id, position, topic_id, qtype, difficulty, bloom_level, question_count, points_each}] (by position)}
It is built by fingerprint_input() from a content-free query: no stem, choice, answer key, model answer,
rubric or explanation is ever loaded for it, and only the 68-character digest leaves the server.

Export size (architecture §9.10): code points of every text the answer-key variant prints. A finalized
exam's print models read only frozen exam columns and immutable revisions.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from .http import sha256_hex
from .validation import TRUE_FALSE_CHOICES, locator_label, matches_row, normalized_for_comparison

FINGERPRINT_PREFIX = "pf1:"
ANSWER_LINES = 6
PRINT_MODEL_VERSION = 1
_COVERAGE = Path(__file__).with_name("glyph_coverage.json")

EXAM_COLUMNS = (
    "id, course_id, created_by, title, instructions, duration_minutes, status, version, finalized_at, finalized_by, "
    "finalized_version, course_code_snapshot, course_name_snapshot, item_count, total_points, content_sha256, "
    "content_chars, blueprint_status, blueprint_deviation_reason, finalize_preview_fingerprint, derived_from_exam_id, "
    "archived_at, created_at, updated_at"
)
QUESTION_COLUMNS = (
    "id, status, version, qtype, difficulty, bloom_level, topic_id, text, choices, answer_key, explanation, citations, "
    "content_hash, approved_revision_no, created_at"
)
REVISION_COLUMNS = (
    "question_id, revision_no, topic_id, qtype, difficulty, bloom_level, stem, choices, answer_key, explanation, "
    "source_refs, content_sha256, approved_by, approved_at"
)
ROW_COLUMNS = "id, position, topic_id, qtype, difficulty, bloom_level, question_count, points_each"
ITEM_COLUMNS = "id, position, points, blueprint_row_id, question_id, question_revision_no"


@dataclass
class ItemState:
    item: object  # exam_items row
    question: object  # the live bank row
    approved: object | None  # the question's current approved revision
    pinned: object | None  # the revision this item is pinned to (finalized exams)

    @property
    def content(self):
        """What this item shows and prints: the pinned revision when finalized, else the live working copy."""
        return self.pinned if self.pinned is not None else self.question


@dataclass
class ExamState:
    exam: object
    course: object  # live (id, code, name)
    rows: list
    items: list[ItemState]
    topics: dict = field(default_factory=dict)
    documents: dict = field(default_factory=dict)  # id -> row (title, page_count) for class documents of the course
    source: dict | None = None  # question_id -> (source revision row or None, source status) for derived drafts

    @property
    def finalized(self) -> bool:
        return self.exam.finalized_at is not None

    @property
    def rows_by_id(self) -> dict:
        return {r.id: r for r in self.rows}


def load_exam(session: Session, course_id: uuid.UUID, exam_id: uuid.UUID, course, *, lock: bool = False,
              with_source: bool = True) -> ExamState | None:  # fmt: skip
    exam = session.execute(
        text(f"SELECT {EXAM_COLUMNS} FROM exams WHERE id = :e AND course_id = :c{' FOR UPDATE' if lock else ''}"),
        {"e": exam_id, "c": course_id},
    ).first()
    if exam is None:
        return None
    rows = session.execute(
        text(f"SELECT {ROW_COLUMNS} FROM exam_blueprint_rows WHERE exam_id = :e ORDER BY id{' FOR SHARE' if lock else ''}"),
        {"e": exam_id},
    ).all()
    items = session.execute(
        text(f"SELECT {ITEM_COLUMNS} FROM exam_items WHERE exam_id = :e ORDER BY id{' FOR UPDATE' if lock else ''}"),
        {"e": exam_id},
    ).all()
    question_ids = sorted({i.question_id for i in items})
    questions = {
        q.id: q
        for q in session.execute(
            text(f"SELECT {QUESTION_COLUMNS} FROM questions WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id{' FOR SHARE' if lock else ''}"),
            {"ids": question_ids},
        ).all()
    }
    wanted = {(q.id, q.approved_revision_no) for q in questions.values() if q.approved_revision_no is not None}
    wanted |= {(i.question_id, i.question_revision_no) for i in items if i.question_revision_no is not None}
    revisions = _revisions(session, wanted)
    rows_sorted = sorted(rows, key=lambda r: r.position)
    states = [
        ItemState(i, questions[i.question_id],
                  revisions.get((i.question_id, questions[i.question_id].approved_revision_no)),
                  revisions.get((i.question_id, i.question_revision_no)) if i.question_revision_no is not None else None)
        for i in sorted(items, key=lambda i: i.position)
    ]  # fmt: skip
    topic_ids = {r.topic_id for r in rows if r.topic_id} | {s.content.topic_id for s in states if s.content.topic_id}
    topics = dict(session.execute(text("SELECT id, name FROM topics WHERE course_id = :c AND id = ANY(CAST(:ids AS uuid[]))"),
                                  {"c": course_id, "ids": sorted(topic_ids)}).all())  # fmt: skip
    document_ids = set()
    for s in states:
        for ref in _refs_of(s.content):
            document_ids.add(uuid.UUID(ref["document_id"]))
    documents = {d.id: d for d in session.execute(
        text("SELECT id, title, page_count FROM documents WHERE course_id = :c AND origin = 'class' AND id = ANY(CAST(:ids AS uuid[]))"),
        {"c": course_id, "ids": sorted(document_ids)},
    ).all()}  # fmt: skip
    state = ExamState(exam, course, rows_sorted, states, topics, documents)
    if with_source and exam.derived_from_exam_id is not None and not state.finalized:
        state.source = source_revisions(session, exam.derived_from_exam_id)
    return state


def _revisions(session: Session, pairs: set) -> dict:
    if not pairs:
        return {}
    ids, numbers = zip(*sorted(pairs))
    found = session.execute(
        text(f"SELECT {REVISION_COLUMNS} FROM exam_question_revisions r "
             "JOIN unnest(CAST(:ids AS uuid[]), CAST(:nos AS integer[])) AS w(q, n) ON r.question_id = w.q AND r.revision_no = w.n"),  # fmt: skip
        {"ids": list(ids), "nos": list(numbers)},
    ).all()
    return {(r.question_id, r.revision_no): r for r in found}


def source_revisions(session: Session, source_exam_id: uuid.UUID) -> dict:
    """question_id -> source revision row (None for a draft source) of the exam a draft was duplicated from."""
    source = session.execute(text("SELECT finalized_at FROM exams WHERE id = :e"), {"e": source_exam_id}).first()
    items = session.execute(
        text("SELECT question_id, question_revision_no FROM exam_items WHERE exam_id = :e"), {"e": source_exam_id}
    ).all()
    if source is None or source.finalized_at is None:
        return {i.question_id: None for i in items}
    revisions = _revisions(session, {(i.question_id, i.question_revision_no) for i in items})
    return {i.question_id: revisions.get((i.question_id, i.question_revision_no)) for i in items}


def _refs_of(content) -> list[dict]:
    refs = content.source_refs if hasattr(content, "source_refs") else content.citations
    return list(refs or [])


def is_revision(content) -> bool:
    return hasattr(content, "stem")


def stem_of(content) -> str:
    return content.stem if is_revision(content) else content.text


# --- presentation helpers -----------------------------------------------------------------------------------


def source_refs_view(state: ExamState, content) -> list[dict]:
    """Live or frozen source references, each with `available` (the document still exists in the course)."""
    out = []
    for ref in _refs_of(content):
        document = state.documents.get(uuid.UUID(ref["document_id"]))
        if is_revision(content):  # frozen at approval
            title, label = ref["title"], ref["locator_label"]
        else:
            title = _safe_title(document.title) if document is not None else None
            label = locator_label(ref["locator"]["page"])
        out.append({"document_id": ref["document_id"], "title": title, "locator": ref["locator"],
                    "locator_label": label, "available": document is not None})  # fmt: skip
    return out


def _safe_title(title) -> str | None:
    from ..chat.verify import VerificationFailed, public_title

    try:
        return public_title(title)
    except VerificationFailed:
        return None


def topic_view(topics: dict, topic_id) -> dict | None:
    if topic_id is None:
        return None
    return {"id": str(topic_id), "name": topics.get(topic_id) or ""}


# --- size, coverage, fingerprint, readiness ---------------------------------------------------------------------


def header_values(state: ExamState) -> dict:
    exam = state.exam
    if state.finalized:
        code, name = exam.course_code_snapshot, exam.course_name_snapshot
    else:
        code, name = state.course.code or "", state.course.name or ""
    return {"course_code": code, "course_name": name, "title": exam.title, "instructions": exam.instructions,
            "duration_minutes": exam.duration_minutes, "question_count": len(state.items),
            "total_points": sum(s.item.points for s in state.items)}  # fmt: skip


def item_texts(state: ExamState, content) -> list[str]:
    """Every text of one item that the answer-key variant prints."""
    texts = [stem_of(content)]
    texts += [c["text"] for c in (content.choices or [])]
    key = content.answer_key or {}
    texts += [key.get("model_answer") or "", key.get("rubric") or ""]
    texts.append(content.explanation or "")
    for ref in source_refs_view(state, content):
        texts += [ref["title"] or "", ref["locator_label"]]
    return texts


def content_chars(state: ExamState, contents: list | None = None) -> int:
    header = header_values(state)
    total = sum(len(header[k] or "") for k in ("course_code", "course_name", "title", "instructions"))
    for content in contents if contents is not None else [s.content for s in state.items]:
        total += sum(len(t) for t in item_texts(state, content))
    return total


@lru_cache(maxsize=1)
def _coverage() -> tuple[tuple[int, int], ...]:
    data = json.loads(_COVERAGE.read_text())
    return tuple((a, b) for a, b in data["ranges"])


def unsupported(value: str) -> bool:
    import bisect

    ranges = _coverage()
    starts = [a for a, _ in ranges]
    for ch in set(value):
        if ch in "\n\t":
            continue
        cp = ord(ch)
        index = bisect.bisect_right(starts, cp) - 1
        if index < 0 or cp > ranges[index][1]:
            return True
    return False


def fingerprint_input(session: Session, exam, course) -> dict:
    """The canonical fingerprint object, from a query that loads no question content (FP-10)."""
    rows = session.execute(
        text(f"SELECT {ROW_COLUMNS} FROM exam_blueprint_rows WHERE exam_id = :e ORDER BY position, id"), {"e": exam.id}
    ).all()
    items = session.execute(
        text("SELECT i.id, i.position, i.points, i.blueprint_row_id, i.question_id, q.status AS question_status, "
             "q.approved_revision_no, r.content_sha256 AS approved_content_sha256 FROM exam_items i "
             "JOIN questions q ON q.id = i.question_id "
             "LEFT JOIN exam_question_revisions r ON r.question_id = q.id AND r.revision_no = q.approved_revision_no "
             "WHERE i.exam_id = :e ORDER BY i.position, i.id"),  # fmt: skip
        {"e": exam.id},
    ).all()
    return fingerprint_object(exam, course, rows, items)


def fingerprint_object(exam, course, rows, items) -> dict:
    def uid(value):
        return None if value is None else str(value)

    return {
        "v": 1,
        "exam": {"id": str(exam.id), "version": exam.version, "status": exam.status, "title": exam.title,
                 "instructions": exam.instructions, "duration_minutes": exam.duration_minutes},
        "course": {"id": str(course.id), "code": course.code, "name": course.name},
        "items": [{"id": str(i.id), "position": i.position, "points": i.points, "blueprint_row_id": uid(i.blueprint_row_id),
                   "question_id": str(i.question_id), "question_status": i.question_status,
                   "approved_revision_no": i.approved_revision_no, "approved_content_sha256": i.approved_content_sha256}
                  for i in items],
        "blueprint": [{"id": str(r.id), "position": r.position, "topic_id": uid(r.topic_id), "qtype": r.qtype,
                       "difficulty": r.difficulty, "bloom_level": r.bloom_level, "question_count": r.question_count,
                       "points_each": r.points_each} for r in rows],
    }  # fmt: skip


def state_fingerprint_object(state: ExamState) -> dict:
    """The same object, from an already loaded state (the finalization transaction, under its locks)."""

    @dataclass
    class _Item:
        id: object
        position: int
        points: int
        blueprint_row_id: object
        question_id: object
        question_status: str
        approved_revision_no: int | None
        approved_content_sha256: str | None

    items = [_Item(s.item.id, s.item.position, s.item.points, s.item.blueprint_row_id, s.item.question_id,
                   s.question.status, s.question.approved_revision_no,
                   None if s.approved is None else s.approved.content_sha256) for s in state.items]  # fmt: skip
    return fingerprint_object(state.exam, state.course, state.rows, items)


def digest(obj: dict) -> str:
    return FINGERPRINT_PREFIX + sha256_hex(obj)


BLUEPRINT_ISSUES = ("blueprint_row_underfilled", "blueprint_row_overfilled", "item_not_in_blueprint",
                    "blueprint_criteria_mismatch")  # fmt: skip


def _matches(row, content) -> bool:
    return matches_row(row, topic_id=content.topic_id, qtype=content.qtype, difficulty=content.difficulty,
                       bloom_level=content.bloom_level)  # fmt: skip


def readiness(state: ExamState, settings, *, contents: list | None = None) -> dict:
    """Blocking issues (finalization) and warnings, for the given item contents (default: what the items show)."""
    contents = contents if contents is not None else [s.content for s in state.items]
    issues, warnings = [], []
    if not state.items:
        issues.append({"code": "no_items", "overridable": False})
    if len(state.items) > settings.exam_max_items:
        issues.append({"code": "too_many_items", "overridable": False})
    for s in state.items:
        if s.question.status == "retired":
            issues.append({"code": "question_retired", "item_id": str(s.item.id), "question_id": str(s.question.id),
                           "overridable": False})  # fmt: skip
        elif s.question.status != "approved":
            issues.append({"code": "question_not_approved", "item_id": str(s.item.id),
                           "question_id": str(s.question.id), "overridable": False})  # fmt: skip
    size = content_chars(state, contents)
    budget = settings.exam_export_max_content_chars
    if size > budget:
        issues.append({"code": "export_budget_exceeded", "content_chars": size, "export_budget": budget, "overridable": False})
    rows = state.rows_by_id
    if state.rows:
        for s, content in zip(state.items, contents):
            row = rows.get(s.item.blueprint_row_id)
            if row is None:
                issues.append({"code": "item_not_in_blueprint", "item_id": str(s.item.id), "overridable": True})
            elif not _matches(row, content):
                issues.append({"code": "blueprint_criteria_mismatch", "item_id": str(s.item.id),
                               "blueprint_row_id": str(row.id), "overridable": True})  # fmt: skip
        for row in state.rows:
            assigned = sum(1 for s in state.items if s.item.blueprint_row_id == row.id)
            if assigned != row.question_count:
                code = "blueprint_row_underfilled" if assigned < row.question_count else "blueprint_row_overfilled"
                issues.append({"code": code, "blueprint_row_id": str(row.id), "assigned": assigned,
                               "count": row.question_count, "overridable": True})  # fmt: skip
    for s in state.items:
        row = rows.get(s.item.blueprint_row_id)
        if row is not None and s.item.points != row.points_each:
            warnings.append({"code": "points_differ_from_blueprint", "item_id": str(s.item.id), "points": s.item.points,
                             "points_each": row.points_each})  # fmt: skip
    groups: dict[str, list[str]] = {}
    for s, content in zip(state.items, contents):
        groups.setdefault(normalized_for_comparison(stem_of(content)), []).append(str(s.item.id))
    for ids in groups.values():
        if len(ids) > 1:
            warnings.append({"code": "duplicate_stem", "item_ids": ids})
    header = header_values(state)
    odd = [str(s.item.id) for s, content in zip(state.items, contents) if any(unsupported(t) for t in item_texts(state, content))]
    if odd or any(unsupported(header[k] or "") for k in ("course_code", "course_name", "title", "instructions")):
        warnings.append({"code": "unsupported_characters", "item_ids": odd})
    if state.exam.duration_minutes is None:
        warnings.append({"code": "missing_duration"})
    if state.source is not None:
        for s in state.items:
            if s.question.id in state.source and changed_since_source(s.question, state.source[s.question.id]) != "unchanged":
                warnings.append({"code": "item_changed_since_source", "item_id": str(s.item.id)})
    declared = bool(state.rows)
    fulfilled = declared and not any(i["code"] in BLUEPRINT_ISSUES for i in issues)
    return {"ready": not issues, "issues": issues, "warnings": warnings,
            "blueprint": {"declared": declared, "fulfilled": fulfilled}, "content_chars": size, "export_budget": budget}  # fmt: skip


def changed_since_source(question, source_revision) -> str:
    """unchanged | changed | not_approved | retired (duplicate report, architecture §5.2)."""
    if question.status == "retired":
        return "retired" if source_revision is not None else "unchanged"
    if source_revision is None:
        return "unchanged"
    if question.status != "approved":
        return "not_approved"
    return "unchanged" if question.content_hash == source_revision.content_sha256 else "changed"


# --- print models ----------------------------------------------------------------------------------------------


def _options(content) -> list[dict] | None:
    if content.qtype == "mcq":
        return [{"label": "ABCDEF"[i], "text": c["text"]} for i, c in enumerate(content.choices or [])]
    if content.qtype == "true_false":
        return [{"label": None, "text": c["text"]} for c in TRUE_FALSE_CHOICES]
    return None


def student_question(number: int, points: int, qtype: str, stem: str, choices) -> dict:
    """The student entry of one question: built from type, stem and choice texts only (H6)."""

    class _C:
        pass

    content = _C()
    content.qtype, content.choices = qtype, choices
    return {"number": number, "points": points, "type": qtype, "stem": stem, "options": _options(content),
            "answer_lines": ANSWER_LINES if qtype == "short" else None}  # fmt: skip


def answer_entry(state: ExamState, number: int, points: int, content) -> dict:
    key = content.answer_key or {}
    answer = answer_text = model_answer = rubric = None
    if content.qtype == "mcq":
        ids = [c["id"] for c in content.choices]
        index = ids.index(key["choice_id"])
        answer, answer_text = "ABCDEF"[index], content.choices[index]["text"]
    elif content.qtype == "true_false":
        answer = "True" if key["choice_id"] == "true" else "False"
    else:
        model_answer, rubric = key["model_answer"], key.get("rubric")
    sources = [f"{r['title'] or ''}, {r['locator_label']}" for r in source_refs_view(state, content)]
    return {"number": number, "points": points, "answer": answer, "answer_text": answer_text,
            "model_answer": model_answer, "rubric": rubric, "explanation": content.explanation, "sources": sources}  # fmt: skip


def answer_key_print_model(state: ExamState, contents: list | None = None) -> dict:
    """The superset print model whose canonical SHA-256 is exams.content_sha256 (renderer independent)."""
    contents = contents if contents is not None else [s.content for s in state.items]
    header = header_values(state)
    questions, answers = [], []
    for number, (s, content) in enumerate(zip(state.items, contents), start=1):
        questions.append(student_question(number, s.item.points, content.qtype, stem_of(content), content.choices))
        answers.append(answer_entry(state, number, s.item.points, content))
    return {"v": PRINT_MODEL_VERSION, "header": header, "questions": questions, "answer_key": answers}


def content_sha256(state: ExamState, contents: list | None = None) -> str:
    return sha256_hex(answer_key_print_model(state, contents))


def student_print_rows(session: Session, exam) -> list:
    """Student content from a query that selects only position, points, type, stem and choice texts (H6)."""
    if exam.finalized_at is not None:
        sql = ("SELECT i.position, i.points, r.qtype, r.stem, r.choices FROM exam_items i JOIN exam_question_revisions r "
               "ON r.question_id = i.question_id AND r.revision_no = i.question_revision_no WHERE i.exam_id = :e "
               "ORDER BY i.position")  # fmt: skip
    else:
        sql = ("SELECT i.position, i.points, q.qtype, q.text AS stem, q.choices FROM exam_items i "
               "JOIN questions q ON q.id = i.question_id WHERE i.exam_id = :e ORDER BY i.position")  # fmt: skip
    return session.execute(text(sql), {"e": exam.id}).all()


def student_print_model(exam, header: dict, rows) -> dict:
    questions = [student_question(n, r.points, r.qtype, r.stem, r.choices) for n, r in enumerate(rows, start=1)]
    return {"v": PRINT_MODEL_VERSION, "header": header, "questions": questions}
