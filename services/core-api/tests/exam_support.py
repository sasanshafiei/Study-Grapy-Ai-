"""SQL-level helpers for the M9 (revision 0008) tests.

They write rows directly, the way the application does, so database-level tests (triggers, CHECKs,
keys, locking) can build exact states without going through the API. Every helper takes an open
connection and leaves committing to the caller. Content is shape-valid by default; tests override
single columns to provoke one violation at a time.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

TF_CHOICES = [{"id": "true", "text": "True"}, {"id": "false", "text": "False"}]
MCQ_CHOICES = [{"id": "a", "text": "Alpha"}, {"id": "b", "text": "Beta"}]
HASH = "a" * 64
FINGERPRINT = "pf1:" + "0" * 64

_DEFAULTS = {
    "mcq": {"choices": MCQ_CHOICES, "answer_key": {"choice_id": "a"}},
    "true_false": {"choices": TF_CHOICES, "answer_key": {"choice_id": "true"}},
    "short": {"choices": None, "answer_key": {"model_answer": "Because.", "rubric": None}},
}


def _json(value):
    return None if value is None else json.dumps(value)


def user(c, role: str = "professor", status: str = "active") -> uuid.UUID:
    return c.execute(
        text("INSERT INTO users (email, display_name, hashed_password, role, status) "
             "VALUES (:e, 'Prof', 'x', :r, :s) RETURNING id"),  # fmt: skip
        {"e": f"{uuid.uuid4().hex}@example.test", "r": role, "s": status},
    ).scalar_one()


def course(c, owner: uuid.UUID, code: str = "CHEM 101", name: str = "General Chemistry") -> uuid.UUID:
    return c.execute(
        text("INSERT INTO courses (owner_id, name, join_code, code) VALUES (:o, :n, :j, :code) RETURNING id"),
        {"o": owner, "n": name, "j": uuid.uuid4().hex[:12], "code": code},
    ).scalar_one()


def topic(c, course_id: uuid.UUID, name: str = "Stoichiometry") -> uuid.UUID:
    return c.execute(
        text("INSERT INTO topics (course_id, name) VALUES (:c, :n) RETURNING id"), {"c": course_id, "n": name}
    ).scalar_one()


def bank_question(c, course_id, author, *, qtype: str = "mcq", **overrides) -> uuid.UUID:
    """A draft bank row at version 1 (what POST /exam-questions writes)."""
    values = {
        "course_id": course_id, "topic_id": None, "purpose": "exam", "qtype": qtype, "difficulty": "medium",
        "text": "What is the stem?", "explanation": None, "citations": None, "status": "draft",
        "content_hash": HASH, "version": 1, "created_by": author, "bloom_level": None,
        **_DEFAULTS[qtype], **overrides,
    }  # fmt: skip
    for key in ("choices", "answer_key", "citations"):
        values[key] = _json(values[key])
    columns = ", ".join(values)
    placeholders = ", ".join(
        f"CAST(:{k} AS jsonb)" if k in ("choices", "answer_key", "citations") else f":{k}" for k in values
    )
    return c.execute(
        text(f"INSERT INTO questions ({columns}, created_at, updated_at) "
             f"VALUES ({placeholders}, now() AT TIME ZONE 'UTC', now()) RETURNING id"),  # fmt: skip
        values,
    ).scalar_one()


def legacy_question(c, course_id, *, purpose: str = "exam", **overrides) -> uuid.UUID:
    values = {"course_id": course_id, "purpose": purpose, "qtype": "mcq", "difficulty": "easy", "text": "Legacy?",
              "status": "approved", **overrides}  # fmt: skip
    columns = ", ".join(values)
    return c.execute(
        text(f"INSERT INTO questions ({columns}) VALUES ({', '.join(':' + k for k in values)}) RETURNING id"), values
    ).scalar_one()


def revision_values(c, question_id, approver, revision_no: int | None = None) -> dict:
    """The INSERT values of the next revision as an exact copy of the working copy."""
    row = c.execute(
        text("SELECT course_id, topic_id, qtype, difficulty, bloom_level, text, choices, answer_key, explanation, "
             "citations, content_hash FROM questions WHERE id = :q"),  # fmt: skip
        {"q": question_id},
    ).one()
    if revision_no is None:
        revision_no = c.execute(
            text("SELECT coalesce(max(revision_no), 0) + 1 FROM exam_question_revisions WHERE question_id = :q"),
            {"q": question_id},
        ).scalar_one()
    refs = [
        {**ref, "title": "Lecture.pdf", "locator_label": "p. 1"} for ref in (row.citations or [])
    ]
    return {
        "question_id": question_id, "revision_no": revision_no, "course_id": row.course_id, "topic_id": row.topic_id,
        "qtype": row.qtype, "difficulty": row.difficulty, "bloom_level": row.bloom_level, "stem": row.text,
        "choices": _json(row.choices), "answer_key": _json(row.answer_key), "explanation": row.explanation,
        "source_refs": json.dumps(refs), "content_sha256": row.content_hash, "approved_by": approver,
    }  # fmt: skip


INSERT_REVISION = (
    "INSERT INTO exam_question_revisions (question_id, revision_no, course_id, topic_id, qtype, difficulty, "
    "bloom_level, stem, choices, answer_key, explanation, source_refs, content_sha256, approved_by, approved_at) "
    "VALUES (:question_id, :revision_no, :course_id, :topic_id, :qtype, :difficulty, :bloom_level, :stem, "
    "CAST(:choices AS jsonb), CAST(:answer_key AS jsonb), :explanation, CAST(:source_refs AS jsonb), "
    ":content_sha256, :approved_by, now())"
)


def approve(c, question_id, approver) -> int:
    """Insert revision n+1 and mark the question approved (what E5 writes)."""
    values = revision_values(c, question_id, approver)
    c.execute(text(INSERT_REVISION), values)
    c.execute(
        text("UPDATE questions SET status = 'approved', approved_revision_no = :n, version = version + 1, "
             "updated_at = now() WHERE id = :q"),  # fmt: skip
        {"n": values["revision_no"], "q": question_id},
    )
    return values["revision_no"]


def approved_question(c, course_id, author, **overrides) -> uuid.UUID:
    question = bank_question(c, course_id, author, **overrides)
    approve(c, question, author)
    return question


def exam(c, course_id, author, title: str = "Midterm 1") -> uuid.UUID:
    return c.execute(
        text("INSERT INTO exams (course_id, created_by, title, status, version) VALUES (:c, :u, :t, 'draft', 1) "
             "RETURNING id"),  # fmt: skip
        {"c": course_id, "u": author, "t": title},
    ).scalar_one()


def bump(c, exam_id) -> None:
    c.execute(text("UPDATE exams SET version = version + 1, updated_at = now() WHERE id = :e"), {"e": exam_id})


def item(c, exam_id, course_id, question_id, position: int, points: int = 2, row=None) -> uuid.UUID:
    return c.execute(
        text("INSERT INTO exam_items (exam_id, course_id, question_id, position, points, blueprint_row_id) "
             "VALUES (:e, :c, :q, :p, :pts, :r) RETURNING id"),  # fmt: skip
        {"e": exam_id, "c": course_id, "q": question_id, "p": position, "pts": points, "r": row},
    ).scalar_one()


def blueprint_row(c, exam_id, course_id, position: int, count: int = 1, points: int = 2, **criteria) -> uuid.UUID:
    values = {"topic_id": None, "qtype": None, "difficulty": None, "bloom_level": None, **criteria}
    return c.execute(
        text("INSERT INTO exam_blueprint_rows (exam_id, course_id, position, topic_id, qtype, difficulty, bloom_level, "
             "question_count, points_each) VALUES (:e, :c, :p, :topic_id, :qtype, :difficulty, :bloom_level, :n, :pe) "
             "RETURNING id"),  # fmt: skip
        {"e": exam_id, "c": course_id, "p": position, "n": count, "pe": points, **values},
    ).scalar_one()


def pin_items(c, exam_id) -> None:
    c.execute(
        text("UPDATE exam_items i SET question_revision_no = q.approved_revision_no, updated_at = now() "
             "FROM questions q WHERE q.id = i.question_id AND i.exam_id = :e"),  # fmt: skip
        {"e": exam_id},
    )


FINALIZE = (
    "UPDATE exams SET status = 'finalized', version = version + 1, finalized_version = version + 1, "
    "finalized_at = now(), finalized_by = :u, course_code_snapshot = 'CHEM 101', course_name_snapshot = 'Chem', "
    "item_count = :n, total_points = :t, content_sha256 = repeat('b', 64), content_chars = :chars, "
    "blueprint_status = :bs, blueprint_deviation_reason = :reason, finalize_preview_fingerprint = :fp, "
    "updated_at = now() WHERE id = :e"
)


def finalize(c, exam_id, user_id, *, blueprint_status: str = "none", reason: str | None = None, pin: bool = True,
             item_count: int | None = None, total_points: int | None = None, chars: int = 100,
             fingerprint: str = FINGERPRINT) -> None:  # fmt: skip
    """Pin and finalize in SQL (what E19 writes once the application checks passed)."""
    if pin:
        pin_items(c, exam_id)
    n, total = c.execute(
        text("SELECT count(*), coalesce(sum(points), 0) FROM exam_items WHERE exam_id = :e"), {"e": exam_id}
    ).one()
    c.execute(
        text(FINALIZE),
        {"u": user_id, "n": n if item_count is None else item_count, "t": total if total_points is None else total_points,
         "chars": chars, "bs": blueprint_status, "reason": reason, "fp": fingerprint, "e": exam_id},  # fmt: skip
    )


def archive(c, exam_id) -> None:
    c.execute(
        text("UPDATE exams SET status = 'archived', archived_at = now(), version = version + 1, updated_at = now() "
             "WHERE id = :e"),  # fmt: skip
        {"e": exam_id},
    )


def m9_row_counts(c) -> dict[str, int]:
    tables = ("exams", "exam_items", "exam_blueprint_rows", "exam_question_revisions", "exam_audit_events",
              "exam_idempotency_keys")  # fmt: skip
    counts = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}
    counts["bank_questions"] = c.execute(
        text("SELECT count(*) FROM questions WHERE version IS NOT NULL OR bloom_level IS NOT NULL")
    ).scalar_one()
    return counts


# --- API helpers ------------------------------------------------------------------------------------------------

ENABLED = {"exams_enabled": True}
EXPORT_ENABLED = {"exams_enabled": True, "exam_export_enabled": True}


def key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def match(version: int) -> dict:
    return {"If-Match": f'"{version}"'}


def mcq_body(**overrides) -> dict:
    return {"type": "mcq", "difficulty": "medium", "bloom_level": "apply", "topic_id": None,
            "stem": "How many moles of O₂ burn 2.0 mol of CH₄?",
            "choices": [{"id": "a", "text": "1.0 mol"}, {"id": "b", "text": "2.0 mol"}, {"id": "c", "text": "4.0 mol"},
                        {"id": "d", "text": "8.0 mol"}],
            "answer_key": {"choice_id": "c"}, "explanation": "CH₄ + 2 O₂ → CO₂ + 2 H₂O.", "source_refs": [],
            **overrides}  # fmt: skip


def tf_body(**overrides) -> dict:
    return {"type": "true_false", "difficulty": "easy", "bloom_level": None, "topic_id": None,
            "stem": "Water boils at 100 °C at sea level.", "choices": None, "answer_key": {"choice_id": "true"},
            "explanation": None, "source_refs": [], **overrides}  # fmt: skip


def short_body(**overrides) -> dict:
    return {"type": "short", "difficulty": "hard", "bloom_level": "analyze", "topic_id": None,
            "stem": "Explain which reactant limits N₂ + 3 H₂ → 2 NH₃ with 6 mol H₂ and 3 mol N₂.", "choices": None,
            "answer_key": {"model_answer": "H₂ is limiting: 6 mol H₂ give 4 mol NH₃.", "rubric": "2 pts ratio, 1 pt limiting"},
            "explanation": "Compare mole ratios.", "source_refs": [], **overrides}  # fmt: skip


class ExamClient:
    """Thin helpers over an ApiHarness whose principal is the course owner."""

    def __init__(self, api, course):
        self.api, self.course = api, course
        self.c = api.client

    def q_path(self, question=None, suffix: str = "") -> str:
        base = f"/api/v1/courses/{self.course}/exam-questions"
        return base if question is None else f"{base}/{question}{suffix}"

    def e_path(self, exam=None, suffix: str = "") -> str:
        base = f"/api/v1/courses/{self.course}/exams"
        return base if exam is None else f"{base}/{exam}{suffix}"

    def question(self, body=None, *, approve: bool = False) -> dict:
        r = self.c.post(self.q_path(), json=body or mcq_body(), headers=key())
        assert r.status_code == 201, r.text
        q = r.json()
        if approve:
            q = self.approve(q)
        return q

    def approve(self, q: dict) -> dict:
        r = self.c.post(self.q_path(q["id"], "/approve"), headers=match(q["version"]))
        assert r.status_code == 200, r.text
        return r.json()

    def edit(self, q: dict, body: dict) -> dict:
        r = self.c.put(self.q_path(q["id"]), json=body, headers=match(q["version"]))
        assert r.status_code == 200, r.text
        return r.json()["question"]

    def exam(self, title: str = "Midterm 1", **fields) -> dict:
        body = {"title": title, "instructions": "Answer all questions.", "duration_minutes": 90, **fields}
        r = self.c.post(self.e_path(), json=body, headers=key())
        assert r.status_code == 201, r.text
        return r.json()

    def get(self, exam: dict | str) -> dict:
        r = self.c.get(self.e_path(exam if isinstance(exam, str) else exam["id"]))
        assert r.status_code == 200, r.text
        return r.json()

    def add(self, exam: dict, q: dict, points: int = 2, row=None, position=None) -> dict:
        r = self.c.post(self.e_path(exam["id"], "/items"), headers=match(exam["version"]),
                        json={"question_id": q["id"], "points": points, "position": position, "blueprint_row_id": row})  # fmt: skip
        assert r.status_code == 201, r.text
        return r.json()

    def blueprint(self, exam: dict, rows: list) -> dict:
        r = self.c.put(self.e_path(exam["id"], "/blueprint"), json={"rows": rows}, headers=match(exam["version"]))
        assert r.status_code == 200, r.text
        return r.json()

    def preview(self, exam: dict, variant: str = "student") -> dict:
        r = self.c.get(self.e_path(exam["id"], "/preview"), params={"variant": variant})
        assert r.status_code == 200, r.text
        return r.json()

    def finalize(self, exam: dict, fingerprint: str | None = None, reason=None, version: int | None = None):
        fingerprint = fingerprint or self.preview(exam)["preview_fingerprint"]
        return self.c.post(self.e_path(exam["id"], "/finalize"), headers=match(version or exam["version"]),
                           json={"preview_fingerprint": fingerprint, "blueprint_deviation_reason": reason})  # fmt: skip

    def finalized(self, exam: dict, **kwargs) -> dict:
        r = self.finalize(exam, **kwargs)
        assert r.status_code == 200, r.text
        return r.json()


def row(count: int, points: int = 2, *, id=None, topic_id=None, type=None, difficulty=None, bloom_level=None) -> dict:
    return {"id": id, "topic_id": topic_id, "type": type, "difficulty": difficulty, "bloom_level": bloom_level,
            "count": count, "points_each": points}  # fmt: skip
