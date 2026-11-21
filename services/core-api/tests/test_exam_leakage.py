"""Exam leakage (capstone R3, "a test in CI that proves it"): LEAK-01 ... LEAK-04.

Canary strings are written into every exam field (titles, instructions, stems, choices, answer keys,
explanations, model answers, rubrics), into drafts, finalized exams and exports. Then every student-facing
endpoint is called as an enrolled student (courses, topics, documents, chat with the fake chat service, quiz
generate/read/attempt with the fake quiz service, dashboard, study events, analytics) and no canary may
appear in any body or header; the AI payloads carry none; the student-side tables and the upload directory
are unchanged by every M9 operation; and no log line written during the M9 operations carries one.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests import exam_support as ex
from tests.exam_support import ExamClient, key, match, mcq_body, row, short_body, tf_body
from tests.fake_chat_service import FakeChatService, answer_for
from tests.fake_quiz_service import TOKEN, FakeQuizService, quiz_for
from tests.test_dashboard_api import _focus
from tests.test_quiz_api import answer, generate, make_setup, wrong_of

CANARY = "EXAMCANARY"
STUDENT_TABLES = ("documents", "chunks", "chunk_topics", "topics", "ingestion_jobs", "document_sections", "mastery_outbox",
                  "mastery_streams", "mastery_scores", "study_events", "quiz_sessions", "quiz_items", "attempts",
                  "topic_practice_stats", "analytics_weekly_releases")  # fmt: skip


def _canaries(tag: str) -> dict:
    return {f: f"{CANARY}-{tag}-{f}" for f in ("title", "instructions", "stem", "choice", "explanation", "model", "rubric")}


def _build_exam_content(x, tag: str) -> list[dict]:
    c = _canaries(tag)
    questions = [
        x.question(mcq_body(stem=c["stem"] + "-mcq?", explanation=c["explanation"],
                            choices=[{"id": "a", "text": c["choice"] + "-a"}, {"id": "b", "text": c["choice"] + "-b"}],
                            answer_key={"choice_id": "b"}), approve=True),
        x.question(tf_body(stem=c["stem"] + "-tf", explanation=c["explanation"] + "-tf"), approve=True),
        x.question(short_body(stem=c["stem"] + "-short", explanation=c["explanation"] + "-s",
                              answer_key={"model_answer": c["model"], "rubric": c["rubric"]}), approve=True),
        x.question(mcq_body(stem=c["stem"] + "-draft-only?")),
    ]  # fmt: skip
    exam = x.exam(c["title"], instructions=c["instructions"])
    exam = x.blueprint(exam, [row(3)])["exam"]
    for q in questions[:3]:
        exam = x.add(exam, q, row=exam["blueprint"]["rows"][0]["id"])
    final = x.finalized(exam)
    draft = x.add(x.exam(c["title"] + "-draft", instructions=c["instructions"]), questions[3])
    for variant in ("student", "answer_key"):
        for fmt in ("pdf", "docx"):
            assert x.c.get(x.e_path(final["id"], "/export"), params={"variant": variant, "format": fmt}).status_code == 200
            assert x.c.get(x.e_path(draft["id"], "/preview-file"), params={"variant": variant, "format": fmt}).status_code == 200
    dup = x.c.post(x.e_path(final["id"], "/duplicate"), json={"title": None}, headers=key())
    assert dup.status_code == 201
    x.c.post(x.e_path(draft["id"], "/archive"), headers=match(draft["version"]))
    return [final, draft, dup.json()["exam"]]


def _table_state(world, course) -> dict:
    out = {}
    for table in STUDENT_TABLES:
        column = "course_id" if table not in ("chunk_topics", "document_sections", "ingestion_jobs") else None
        if column:
            out[table] = world.scalar(f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {table} t "
                                      f"WHERE t.course_id = :c", c=course)  # fmt: skip
        else:
            out[table] = world.scalar(f"SELECT count(*) FROM {table}")
    return out


def _no_canary(response, where: str) -> None:
    assert CANARY not in response.text, where
    assert all(CANARY not in v for v in response.headers.values()), where


@pytest.fixture
def services():
    chat, quiz = FakeChatService().start(), FakeQuizService().start()
    try:
        yield chat, quiz
    finally:
        quiz.release()
        quiz.stop()
        chat.stop()


def test_leak01_to_leak04_exam_content_never_reaches_student_paths_ai_payloads_or_logs(world, make_api, services, caplog):
    chat_fake, quiz_fake = services
    s = make_setup(world)
    quiz_fake.generate = lambda request: quiz_for(request, s.sources)
    chat_fake.answer = lambda request: answer_for(request, answer="No exam content here.", grounded=False)
    professor_api = make_api(settings_overrides=ex.EXPORT_ENABLED).as_user(s.professor)
    x = ExamClient(professor_api, s.course)
    before = _table_state(world, s.course)
    uploads_before = professor_api.stored_files()
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    exams = _build_exam_content(x, "A")
    # also a failing request path: an unexpected error is logged as class + SQLSTATE only
    bad = professor_api.client.put(x.q_path(str(uuid.uuid4())), json=mcq_body(stem=f"{CANARY}-404?"), headers=match(1))
    assert bad.status_code == 404
    m9_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in m9_logs  # LEAK-04
    assert _table_state(world, s.course) == before  # LEAK-03
    assert professor_api.stored_files() == uploads_before and not professor_api.incoming_files()

    # LEAK-01: every student-facing endpoint, as the enrolled student
    student = make_api(settings_overrides={**ex.EXPORT_ENABLED, "quiz_enabled": True, "ai_service_url": quiz_fake.url,
                                            "ai_service_token": TOKEN, "dashboard_enabled": True, "study_events_enabled": True,
                                            "analytics_enabled": True, "quiz_read_timeout_seconds": 5,
                                            "quiz_probe_deadline_seconds": 2, "quiz_generate_deadline_seconds": 4,
                                            "quiz_pipeline_deadline_seconds": 8}).as_user(s.student)  # fmt: skip
    c = student.client
    base = f"/api/v1/courses/{s.course}"
    for path in ("/api/v1/courses", f"{base}/topics", f"{base}/documents", f"{base}/dashboard", f"{base}/analytics"):
        _no_canary(c.get(path), path)
    quiz = generate(student, s.course, s.topics[:1], count=2)
    assert quiz.status_code == 201, quiz.text
    _no_canary(quiz, "quiz create")
    body = quiz.json()
    read = c.get(f"{base}/quizzes/{body['quiz_id']}")
    _no_canary(read, "quiz read")
    question = body["questions"][0]
    attempt = answer(student, s.course, body["quiz_id"], question["id"], wrong_of(world, question))
    assert attempt.status_code in (200, 201), attempt.text
    _no_canary(attempt, "attempt")
    _no_canary(c.get(f"{base}/dashboard"), "dashboard after attempt")
    now = datetime.now(timezone.utc)
    _no_canary(c.post(f"{base}/study-events", json={"events": [_focus(now - timedelta(minutes=5), 2)]}), "study events")
    for exam in exams:
        for suffix in ("", "/preview", "/history"):
            r = c.get(f"{base}/exams/{exam['id']}{suffix}")
            assert r.status_code == 403 and r.json()["code"] == "exams_not_allowed"
            _no_canary(r, suffix)
        r = c.get(f"{base}/exams/{exam['id']}/export", params={"variant": "answer_key", "format": "pdf"})
        assert r.status_code == 403
    _no_canary(c.get(f"{base}/exam-questions"), "bank list")
    chat = make_api(settings_overrides={"chat_enabled": True, "ai_service_url": chat_fake.url, "ai_service_token": TOKEN,
                                         "chat_read_timeout_seconds": 5, "chat_probe_deadline_seconds": 2,
                                         "chat_answer_deadline_seconds": 4}).as_user(s.student)  # fmt: skip
    asked = chat.client.post(f"{base}/chat", json={"question": "What is on the exam?", "scope": "class_only"},
                             headers={"Accept": "text/event-stream"})  # fmt: skip
    assert asked.status_code == 200
    _no_canary(asked, "chat")

    # LEAK-02: what the AI services received
    exam_question_ids = {i["question_id"] for e in exams for i in e["items"]}
    for recorded in chat_fake.requests + quiz_fake.requests:
        payload = recorded.body.decode("utf-8", "replace")
        assert CANARY not in payload and not any(q in payload for q in exam_question_ids)
        assert not any(e["id"] in payload for e in exams)
    assert quiz_fake.generations(), "the practice quiz was generated through the fake service"
    # the practice quiz never contains a bank question
    delivered = {q["id"] for q in body["questions"]}
    assert not delivered & exam_question_ids
    assert world.scalar("SELECT count(*) FROM quiz_items i JOIN questions q ON q.id = i.question_id WHERE q.purpose = 'exam'") == 0
    assert world.scalar("SELECT count(*) FROM attempts a JOIN questions q ON q.id = a.question_id WHERE q.purpose = 'exam'") == 0


def test_leak01_other_professors_admins_and_other_courses_see_nothing(world, make_api):
    owner = world.user("professor")
    course = world.course(owner, code="CHEM 101")
    x = ExamClient(make_api(settings_overrides=ex.EXPORT_ENABLED).as_user(owner), course)
    exams = _build_exam_content(x, "B")
    for user in (world.user("professor"), world.user("admin"), world.user("student")):
        api = make_api(settings_overrides=ex.EXPORT_ENABLED).as_user(user)
        own = world.course(user) if user.role == "professor" else None
        for exam in exams:
            r = api.client.get(f"/api/v1/courses/{course}/exams/{exam['id']}")
            assert r.status_code == 404 and CANARY not in r.text
            if own is not None:  # the exam id under the caller's own course
                r = api.client.get(f"/api/v1/courses/{own}/exams/{exam['id']}")
                assert r.status_code == 404 and r.json()["code"] == "exam_not_found"
                r = api.client.get(f"/api/v1/courses/{own}/exams/{exam['id']}/export", params={"variant": "answer_key", "format": "pdf"})
                assert r.status_code == 404 and CANARY not in r.text
