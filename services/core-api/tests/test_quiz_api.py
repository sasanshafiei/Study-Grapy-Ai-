"""M7 quiz API on the disposable database, with the raw-socket fake of studygraph.quiz/1.

Every database test runs through the guarded fixtures (disposable PostgreSQL only).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, StatementError

from app.auth import CurrentUser
from app.quiz import attempts as attempts_module
from app.quiz import generation
from app.routers import quizzes as quizzes_router
from tests.fake_chat_service import http_response
from tests.fake_quiz_service import TOKEN, FakeQuizService, Source, quiz_for

TEXT1 = "The limiting reagent is the reactant that is completely consumed first and so sets the theoretical yield."
TEXT2 = "Reaction rate increases with temperature because more collisions exceed the activation energy barrier."
FORBIDDEN = ("answer_key", "explanation", "quote", "grading", "generation", "correct", "evidence", "document_id")


@dataclass
class Setup:
    professor: CurrentUser
    student: CurrentUser
    course: uuid.UUID
    topics: list[uuid.UUID]
    document: uuid.UUID
    chunks: list[uuid.UUID]
    sources: list[Source]


@pytest.fixture
def fake():
    service = FakeQuizService().start()
    try:
        yield service
    finally:
        service.release()
        service.stop()


def make_setup(world) -> Setup:
    professor = world.user("professor")
    course = world.course(professor, code="CHEM 101")
    student = world.user("student")
    world.enroll(student, course)
    topics = [world.topic(course, "Stoichiometry"), world.topic(course, "Kinetics")]
    document = world.document(course, status="indexed", title="Lecture 3.pdf")
    chunks = [world.chunk(document, course, TEXT1), world.chunk(document, course, TEXT2, locator={"page": 2})]
    sources = [Source(chunks[0], document, TEXT1[:60]), Source(chunks[1], document, TEXT2[:60], {"page": 2})]
    return Setup(professor, student, course, topics, document, chunks, sources)


@pytest.fixture
def setup(world, fake) -> Setup:
    s = make_setup(world)
    fake.generate = lambda request: quiz_for(request, s.sources)
    return s


@pytest.fixture
def quiz_api(make_api, fake, setup):
    def build(user=None, **overrides):
        settings = {
            "quiz_enabled": True, "ai_service_url": fake.url, "ai_service_token": TOKEN,
            "quiz_read_timeout_seconds": 5, "quiz_probe_deadline_seconds": 2, "quiz_generate_deadline_seconds": 4,
            "quiz_pipeline_deadline_seconds": 8, **overrides,
        }  # fmt: skip
        return make_api(settings_overrides=settings).as_user(setup.student if user is None else user)

    return build


def generate(api, course, topics, count=2, difficulty="medium", key=None, client=None, **extra):
    body = {"topic_ids": [str(t) for t in topics], "count": count, "difficulty": difficulty, **extra}
    headers = {"Idempotency-Key": str(key or uuid.uuid4())}
    return (client or api.client).post(f"/api/v1/courses/{course}/quizzes", json=body, headers=headers)


def answer(api, course, quiz_id, question_id, choice, hint_count=0, response_ms=1200, client=None):
    body = {"quiz_id": str(quiz_id), "question_id": str(question_id), "answer": choice, "hint_count": hint_count,
            "response_ms": response_ms}  # fmt: skip
    return (client or api.client).post(f"/api/v1/courses/{course}/attempts", json=body)


def key_of(world, question_id) -> str:
    return world.scalar("SELECT answer_key ->> 'choice_id' FROM questions WHERE id = :q", q=question_id)


def wrong_of(world, question) -> str:
    key = key_of(world, question["id"])
    return next(c["id"] for c in question["choices"] if c["id"] != key)


def session_row(world, quiz_id):
    return world.rows("SELECT * FROM quiz_sessions WHERE id = :q", q=quiz_id)[0]


def keys_in(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_in(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_in(v)}
    return set()


# --- access, flags, validation ------------------------------------------------------------------


def test_authentication_and_practice_authorization_fail_closed(world, quiz_api, setup, fake):
    api = quiz_api()
    other_prof = world.user("professor")
    admin = world.user("admin")
    disabled = world.user("student", status="disabled")
    world.enroll(disabled, setup.course)
    outsider = world.user("student")
    cases = [(None, 401), (setup.professor, 403), (other_prof, 404), (admin, 404), (disabled, 403), (outsider, 404)]
    quiz_id, question_id = uuid.uuid4(), uuid.uuid4()
    for user, status in cases:
        api.as_user(user)
        assert generate(api, setup.course, setup.topics).status_code == status
        assert api.client.get(f"/api/v1/courses/{setup.course}/quizzes/{quiz_id}").status_code == status
        assert answer(api, setup.course, quiz_id, question_id, "a").status_code == status
    assert generate(api.as_user(setup.professor), setup.course, setup.topics).json()["code"] == "practice_not_allowed"
    assert fake.requests == []


def test_quiz_is_off_by_default_and_nothing_is_contacted(make_api, setup, fake):
    api = make_api(settings_overrides={"ai_service_url": fake.url, "ai_service_token": TOKEN}).as_user(setup.student)
    response = generate(api, setup.course, setup.topics)
    assert response.status_code == 503 and response.json()["code"] == "quiz_unavailable"
    assert response.headers["cache-control"] == "no-store"
    assert answer(api, setup.course, uuid.uuid4(), uuid.uuid4(), "a").status_code == 503
    assert fake.requests == []


def test_idempotency_key_is_required_and_canonical(quiz_api, setup, fake):
    api = quiz_api()
    url = f"/api/v1/courses/{setup.course}/quizzes"
    body = {"topic_ids": [str(setup.topics[0])], "count": 1, "difficulty": "easy"}
    assert api.client.post(url, json=body).json()["code"] == "idempotency_key_required"
    upper = str(uuid.uuid4()).upper()
    assert api.client.post(url, json=body, headers={"Idempotency-Key": upper}).json()["code"] == "invalid_idempotency_key"
    two = [("Idempotency-Key", str(uuid.uuid4())), ("Idempotency-Key", str(uuid.uuid4()))]
    assert api.client.post(url, json=body, headers=two).status_code == 400
    assert fake.requests == []


def test_topics_of_another_course_are_refused_without_quota_or_generation(world, quiz_api, setup, fake):
    other = world.course(setup.professor)
    foreign = world.topic(other, "Elsewhere")
    response = generate(quiz_api(), setup.course, [setup.topics[0], foreign])
    assert response.status_code == 422 and response.json()["code"] == "invalid_topics"
    assert fake.generations() == []
    assert world.scalar("SELECT count(*) FROM quiz_generation_quotas WHERE user_id = :u", u=setup.student.id) == 0


# --- generation ----------------------------------------------------------------------------------


def test_generated_quiz_is_student_safe_persisted_and_bound_to_the_request(world, quiz_api, setup, fake):
    key = uuid.uuid4()
    response = generate(quiz_api(), setup.course, setup.topics, count=4, key=key)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    quiz = response.json()
    assert response.headers["location"] == f"/api/v1/courses/{setup.course}/quizzes/{quiz['quiz_id']}"
    assert quiz["status"] == "ready" and quiz["scope"] == "class_only" and len(quiz["questions"]) == 4
    assert not keys_in(quiz) & set(FORBIDDEN)
    assert TEXT1[:30] not in response.text and "Explanation 1" not in response.text
    sent = fake.generations()[0]
    assert sent["request_id"] == quiz["quiz_id"] and sent["course_id"] == str(setup.course)
    assert sent["scope"] == "class_only" and sent["allowed_origins"] == ["class"] and sent["count"] == 4
    assert sent["question_types"] == ["mcq", "true_false"]
    rows = world.rows(
        "SELECT q.status, q.purpose, q.qtype, q.generated_for_user_id, q.answer_key, q.choices, qi.position "
        "FROM quiz_items qi JOIN questions q ON q.id = qi.question_id WHERE qi.session_id = :s ORDER BY qi.position",
        s=quiz["quiz_id"],
    )
    assert [r["position"] for r in rows] == [1, 2, 3, 4]
    assert all(r["status"] == "generated" and r["purpose"] == "practice" for r in rows)
    assert all(r["generated_for_user_id"] == setup.student.id for r in rows)
    for row, question in zip(rows, quiz["questions"]):
        assert question["choices"] == row["choices"]  # persisted delivery order
        if row["qtype"] == "true_false":
            assert [c["id"] for c in row["choices"]] == ["true", "false"]
    row = session_row(world, quiz["quiz_id"])
    assert row["status"] == "ready" and row["idempotency_key"] == key and row["attempt_token"] is None


def test_identical_request_replays_without_upstream_quota_or_generation_switch(world, quiz_api, setup, fake):
    key = uuid.uuid4()
    first = generate(quiz_api(), setup.course, setup.topics, key=key)
    assert first.status_code == 201
    fake.capabilities = {**fake.capabilities, "mock": True}  # the AI service is now unusable
    fake.generate = None
    for api in (quiz_api(), quiz_api(quiz_generation_enabled=False)):
        replay = generate(api, setup.course, list(reversed(setup.topics)), key=key)  # topic order is normalized
        assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
        assert replay.json() == first.json()
    assert len(fake.generations()) == 1
    assert world.scalar("SELECT used FROM quiz_generation_quotas WHERE user_id = :u", u=setup.student.id) == 1


def test_same_key_with_a_different_request_or_course_conflicts(world, quiz_api, setup, fake):
    key = uuid.uuid4()
    assert generate(quiz_api(), setup.course, setup.topics, key=key).status_code == 201
    second = world.course(setup.professor)
    world.enroll(setup.student, second)
    topic2 = world.topic(second, "Other")
    for response in (
        generate(quiz_api(), setup.course, setup.topics, count=3, key=key),
        generate(quiz_api(), setup.course, setup.topics, difficulty="hard", key=key),
        generate(quiz_api(), setup.course, setup.topics[:1], key=key),
        generate(quiz_api(), second, [topic2], key=key),
    ):
        assert response.status_code == 409 and response.json()["code"] == "idempotency_key_reused"
    assert len(fake.generations()) == 1


def test_another_student_may_use_the_same_key_independently(world, quiz_api, setup, fake):
    key = uuid.uuid4()
    other = world.user("student")
    world.enroll(other, setup.course)
    a = generate(quiz_api(), setup.course, setup.topics, key=key)
    b = generate(quiz_api(other), setup.course, setup.topics, key=key)
    assert (a.status_code, b.status_code) == (201, 201) and a.json()["quiz_id"] != b.json()["quiz_id"]


@pytest.mark.parametrize("capabilities", [{"mock": True}, {"protocol": "studygraph.quiz/0"}, {"question_types": ["short"]},
                                          {"scopes": ["class_plus_external"]}, {"max_questions": 1}])  # fmt: skip
def test_unusable_service_is_refused_before_any_session_or_quota(world, quiz_api, setup, fake, capabilities):
    fake.capabilities = {**fake.capabilities, **capabilities}
    response = generate(quiz_api(), setup.course, setup.topics, count=2)
    assert response.status_code == 503 and response.json()["code"] == "quiz_ai_unavailable"
    assert fake.generations() == []
    assert world.scalar("SELECT count(*) FROM quiz_sessions WHERE user_id = :u", u=setup.student.id) == 0


def _last_session(world, setup):
    return world.rows("SELECT * FROM quiz_sessions WHERE user_id = :u ORDER BY created_at DESC LIMIT 1", u=setup.student.id)[0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(mock=True),
        lambda b: b["questions"].pop(),
        lambda b: b["questions"][0].update(answer=9),
        lambda b: b["questions"][1].update(stem=b["questions"][0]["stem"]),
        lambda b: b["questions"][0].update(hint="Correct claim 1"),
    ],
)
def test_an_invalid_generation_is_rejected_whole_and_replayed_stably(world, quiz_api, setup, fake, mutate):
    def broken(request):
        body = quiz_for(request, setup.sources)
        mutate(body)
        return body

    fake.generate = broken
    key = uuid.uuid4()
    first = generate(quiz_api(), setup.course, setup.topics, key=key)
    assert first.status_code == 502 and first.json()["code"] == "quiz_rejected"
    assert first.json()["retry"] == "new_idempotency_key" and "Correct claim" not in first.text
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "rejected_protocol"
    assert world.scalar("SELECT count(*) FROM questions WHERE generated_by_session_id = :s", s=row["id"]) == 0
    again = generate(quiz_api(), setup.course, setup.topics, key=key)
    assert (again.status_code, again.json()) == (first.status_code, first.json())
    assert len(fake.generations()) == 1


@pytest.mark.parametrize("problem", ["other_course", "stale", "external", "document", "quote", "short", "locator"])
def test_citations_must_be_provenanced_in_this_course(world, quiz_api, setup, fake, problem):
    other = world.course(setup.professor)
    foreign_doc = world.document(other, status="indexed")
    foreign_chunk = world.chunk(foreign_doc, other, TEXT1)
    source = setup.sources[0]
    if problem == "other_course":
        source = Source(foreign_chunk, foreign_doc, TEXT1[:60])
    elif problem == "stale":
        world.execute("UPDATE documents SET status = 'stale' WHERE id = :d", d=setup.document)
    elif problem == "external":
        src = world.source()
        ext = world.document(setup.course, origin="external", status="indexed", source_id=src)
        source = Source(world.chunk(ext, setup.course, TEXT1, origin="external"), ext, TEXT1[:60])
    elif problem == "document":
        source = Source(source.chunk_id, foreign_doc, source.quote)
    elif problem == "quote":
        source = Source(source.chunk_id, source.document_id, "A sentence that the lecture notes never contain at all.")
    elif problem == "short":
        source = Source(source.chunk_id, source.document_id, TEXT1[:12])
    elif problem == "locator":
        source = Source(source.chunk_id, source.document_id, source.quote, {"page": 7})
    fake.generate = lambda request: quiz_for(request, [source])
    response = generate(quiz_api(), setup.course, setup.topics)
    assert response.status_code == 502 and response.json()["code"] == "quiz_rejected"
    row = _last_session(world, setup)
    assert row["failure_code"] == "rejected_provenance"


@pytest.mark.parametrize("failure", ["http_500", "redirect", "framing"])
def test_upstream_failures_fail_the_session_without_retry(world, quiz_api, setup, fake, failure):
    raw = {
        "http_500": http_response(500, b"{}"),
        "redirect": http_response(302, b"", extra="Location: http://127.0.0.1:1/\r\n"),
        "framing": b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    }[failure]
    api = quiz_api()
    warm(api)  # probe first, so the scripted reply hits the generation request
    fake.raw.append(raw)
    response = generate(api, setup.course, setup.topics)
    assert response.status_code == 503 and response.json()["code"] == "quiz_ai_unavailable"
    assert _last_session(world, setup)["failure_code"] == "ai_unavailable"
    assert len([r for r in fake.requests if r.path == "/ai/v1/quiz/generate"]) == 1


def test_upstream_deadline_fails_the_session(world, quiz_api, setup, fake):
    api = quiz_api(quiz_generate_deadline_seconds=1, quiz_read_timeout_seconds=5, quiz_pipeline_deadline_seconds=4)
    warm(api)
    fake.hold()
    response = generate(api, setup.course, setup.topics)
    fake.release()
    assert response.status_code == 504 and response.json()["code"] == "quiz_timeout"
    assert _last_session(world, setup)["failure_code"] == "upstream_timeout"


# --- concurrency, leases, cancellation -----------------------------------------------------------


def _in_thread(fn):
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("r", fn()), daemon=True)
    thread.start()
    return thread, result


def warm(*apis) -> None:
    """Probe capabilities now, so a held fake only holds the generation request."""
    for api in apis:
        assert asyncio.run(api.app.state.quiz_client.capabilities()).ready


def test_duplicate_delivery_while_generating_and_one_generation_per_user(world, quiz_api, setup, fake):
    api, other_process = quiz_api(), quiz_api()  # two apps: two processes, no shared in-process limiter
    warm(api, other_process)
    key = uuid.uuid4()
    fake.hold()
    thread, result = _in_thread(lambda: generate(api, setup.course, setup.topics, key=key, client=TestClient(api.app)))
    assert fake.waiting.wait(5)
    same = generate(other_process, setup.course, setup.topics, key=key)
    assert same.status_code == 409 and same.json()["code"] == "quiz_generation_in_progress"
    assert same.headers["retry-after"] == "5" and same.json()["status"] == "generating"
    busy = generate(api, setup.course, setup.topics)  # same process: the in-process slot
    assert busy.status_code == 429 and busy.json()["code"] == "quiz_generation_busy"
    busy = generate(other_process, setup.course, setup.topics)  # other process: the partial unique index
    assert busy.status_code == 429 and busy.json()["code"] == "quiz_generation_busy"
    fake.release()
    thread.join(10)
    assert result["r"].status_code == 201
    assert generate(other_process, setup.course, setup.topics, key=key).status_code == 200
    assert world.scalar("SELECT used FROM quiz_generation_quotas WHERE user_id = :u", u=setup.student.id) == 1


def test_a_generator_that_lost_its_lease_never_finalizes(world, quiz_api, setup, fake):
    def expire(payload):
        world.execute("UPDATE quiz_sessions SET lease_expires_at = now() - interval '1 second' WHERE id = :q",
                      q=payload["request_id"])  # fmt: skip

    fake.on_generate = expire
    response = generate(quiz_api(), setup.course, setup.topics)
    assert response.status_code == 503 and response.json()["code"] == "quiz_generation_failed"
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "lease_expired"
    assert world.scalar("SELECT count(*) FROM questions WHERE generated_by_session_id = :s", s=row["id"]) == 0


def test_recovery_fails_expired_sessions_and_get_reports_them(world, quiz_api, setup, fake):
    api = quiz_api()
    warm(api)
    fake.hold()
    thread, result = _in_thread(lambda: generate(api, setup.course, setup.topics, client=TestClient(api.app)))
    assert fake.waiting.wait(5)
    quiz_id = _last_session(world, setup)["id"]
    read = api.client.get(f"/api/v1/courses/{setup.course}/quizzes/{quiz_id}").json()
    assert read["status"] == "generating" and read["questions"] == []
    world.execute("UPDATE quiz_sessions SET lease_expires_at = now() - interval '1 second' WHERE id = :q", q=quiz_id)
    assert generation.recover_expired(api.session_factory) == [quiz_id]
    fake.release()
    thread.join(10)
    assert result["r"].status_code == 503  # the delayed generator lost its lease
    read = api.client.get(f"/api/v1/courses/{setup.course}/quizzes/{quiz_id}").json()
    assert read["status"] == "failed" and read["failure"]["code"] == "quiz_generation_failed"


def test_cancellation_marks_the_session_failed(world, quiz_api, setup, fake):
    api = quiz_api()
    warm(api)
    fake.hold()
    req = generation.QuizRequest(setup.course, uuid.uuid4(), tuple(sorted(setup.topics)), 2, "easy", "class_only")

    async def main():
        with anyio.move_on_after(1.0):
            await generation.create_quiz(api.app.state, setup.student, req)

    anyio.run(main)
    fake.release()
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "cancelled"
    assert api.app.state.quiz_limiter.in_use() == (0, 0)


def test_access_revoked_during_generation_persists_nothing(world, quiz_api, setup, fake):
    fake.on_generate = lambda payload: world.execute(
        "DELETE FROM enrollments WHERE user_id = :u AND course_id = :c", u=setup.student.id, c=setup.course
    )
    response = generate(quiz_api(), setup.course, setup.topics)
    assert response.status_code == 404 and response.json()["code"] == "course_not_found"
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "access_lost"
    assert world.scalar("SELECT count(*) FROM questions WHERE generated_by_session_id = :s", s=row["id"]) == 0


def test_an_uncertain_commit_is_resolved_from_the_database(world, quiz_api, setup, fake, monkeypatch):
    real = generation.persist

    def committed_then_lost(*args):
        real(*args)
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(generation, "persist", committed_then_lost)
    response = generate(quiz_api(), setup.course, setup.topics)
    assert response.status_code == 200 and response.json()["status"] == "ready"
    assert _last_session(world, setup)["status"] == "ready"


def test_daily_quota_counts_failures_not_replays_and_holds_under_concurrency(world, quiz_api, setup, fake):
    api = quiz_api(quiz_daily_generation_quota=2)
    fake.generate = None  # upstream 503: a failed generation still consumes its unit
    assert generate(api, setup.course, setup.topics).status_code == 503
    fake.generate = lambda request: quiz_for(request, setup.sources)
    key = uuid.uuid4()
    assert generate(api, setup.course, setup.topics, key=key).status_code == 201
    assert generate(api, setup.course, setup.topics, key=key).status_code == 200
    third = generate(api, setup.course, setup.topics)
    assert third.status_code == 429 and third.json()["code"] == "quiz_quota_exceeded"
    assert int(third.headers["retry-after"]) >= 1

    # Concurrent reservations of other students against a quota of 1: exactly one each.
    others = [world.user("student") for _ in range(3)]
    for other in others:
        world.enroll(other, setup.course)
    barrier = threading.Barrier(9)
    outcomes = []
    settings = api.settings.model_copy(update={"quiz_daily_generation_quota": 1})

    def reserve(user):
        req = generation.QuizRequest(setup.course, uuid.uuid4(), tuple(sorted(setup.topics)), 1, "easy", "class_only")
        barrier.wait()
        try:
            outcomes.append(type(generation.reserve(api.session_factory, user, req, settings)).__name__)
        except Exception as exc:  # noqa: BLE001
            outcomes.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=reserve, args=(u,)) for u in others for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert outcomes.count("Reservation") == 3, outcomes
    assert set(outcomes) <= {"Reservation", "quiz_generation_busy", "quiz_quota_exceeded"}
    used = world.rows("SELECT user_id, used FROM quiz_generation_quotas WHERE user_id = ANY(:u)", u=[o.id for o in others])
    assert sorted(r["used"] for r in used) == [1, 1, 1]


# --- reading and answering -----------------------------------------------------------------------


@pytest.fixture
def quiz(quiz_api, setup):
    response = generate(quiz_api(), setup.course, setup.topics[:1], count=4)
    assert response.status_code == 201, response.text
    return response.json()


def test_grading_is_deterministic_and_baseline_is_atomic(world, quiz_api, setup, quiz):
    api = quiz_api()
    q1, q2, q3 = quiz["questions"][:3]
    right = answer(api, setup.course, quiz["quiz_id"], q1["id"], key_of(world, q1["id"]), hint_count=1)
    assert right.status_code == 201 and right.headers["cache-control"] == "no-store"
    body = right.json()
    assert body["correct"] is True and body["replayed"] is False
    assert body["correct_answer"]["choice_id"] == key_of(world, q1["id"])
    assert body["explanation"].startswith("Explanation") and body["citations"][0]["title"] == "Lecture 3.pdf"
    assert body["mastery"] == {
        "kind": "baseline", "method": "cumulative_accuracy/1", "topic_id": str(setup.topics[0]),
        "topic_name": "Stoichiometry", "before": None, "after": 1.0, "attempts": 1, "correct": 1, "trend": "first",
        "next_review_at": None,
    }  # fmt: skip
    wrong = answer(api, setup.course, quiz["quiz_id"], q2["id"], wrong_of(world, q2)).json()
    assert wrong["correct"] is False and wrong["mastery"]["before"] == 1.0 and wrong["mastery"]["after"] == 0.5
    assert wrong["mastery"]["trend"] == "down"
    third = answer(api, setup.course, quiz["quiz_id"], q3["id"], key_of(world, q3["id"])).json()
    assert third["mastery"]["before"] == 0.5 and third["mastery"]["after"] == pytest.approx(2 / 3)
    stats = world.rows("SELECT attempts, correct FROM topic_practice_stats WHERE user_id = :u", u=setup.student.id)
    assert [(r["attempts"], r["correct"]) for r in stats] == [(3, 2)]
    assert world.scalar("SELECT count(*) FROM mastery_scores WHERE user_id = :u", u=setup.student.id) == 0


def test_retry_replays_and_a_different_answer_conflicts(world, quiz_api, setup, quiz):
    api = quiz_api()
    question = quiz["questions"][0]
    choice = wrong_of(world, question)
    first = answer(api, setup.course, quiz["quiz_id"], question["id"], choice, hint_count=1, response_ms=900)
    again = answer(api, setup.course, quiz["quiz_id"], question["id"], choice, hint_count=0, response_ms=50)
    assert (first.status_code, again.status_code) == (201, 200) and again.json()["replayed"] is True
    a, b = first.json(), again.json()
    assert {k: v for k, v in a.items() if k != "replayed"} == {k: v for k, v in b.items() if k != "replayed"}
    assert (b["hint_count"], b["response_ms"]) == (1, 900)  # the first submission's telemetry
    other = answer(api, setup.course, quiz["quiz_id"], question["id"], key_of(world, question["id"]))
    assert other.status_code == 409 and other.json()["code"] == "already_answered"
    assert not keys_in(other.json()) & {"correct_answer", "explanation", "citations"}
    assert world.scalar("SELECT attempts FROM topic_practice_stats WHERE user_id = :u", u=setup.student.id) == 1
    assert world.scalar("SELECT count(*) FROM attempts WHERE user_id = :u", u=setup.student.id) == 1


def test_invalid_choices_and_unsupported_types_store_nothing(world, quiz_api, setup, quiz):
    api = quiz_api()
    question = quiz["questions"][0]
    for choice in ("z", "TRUE", "A"):
        response = answer(api, setup.course, quiz["quiz_id"], question["id"], choice)
        assert response.status_code == 422 and response.json()["code"] == "invalid_answer"
    assert answer(api, setup.course, quiz["quiz_id"], question["id"], "a", hint_count=2).status_code == 422
    assert world.scalar("SELECT count(*) FROM attempts WHERE user_id = :u", u=setup.student.id) == 0


def test_students_only_reach_their_own_assigned_practice_questions(world, quiz_api, setup, quiz, fake):
    other = world.user("student")
    world.enroll(other, setup.course)
    question = quiz["questions"][0]
    choice = question["choices"][0]["id"]
    # another student, with their own quiz id or with the owner's
    other_quiz = generate(quiz_api(other), setup.course, setup.topics).json()
    for quiz_id in (quiz["quiz_id"], other_quiz["quiz_id"]):
        response = answer(quiz_api(other), setup.course, quiz_id, question["id"], choice)
        assert response.status_code == 404 and response.json()["code"] == "question_not_found"
    assert quiz_api(other).client.get(f"/api/v1/courses/{setup.course}/quizzes/{quiz['quiz_id']}").status_code == 404
    # the owner, through another enrolled course's path
    second = world.course(setup.professor)
    world.enroll(setup.student, second)
    assert answer(quiz_api(), second, quiz["quiz_id"], question["id"], choice).status_code == 404
    assert quiz_api().client.get(f"/api/v1/courses/{second}/quizzes/{quiz['quiz_id']}").status_code == 404
    # unassigned practice and (approved) exam questions of the course
    for purpose, status in (("practice", "approved"), ("exam", "approved"), ("exam", "draft")):
        legacy = world.scalar(
            "INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, choices, answer, status) "
            "VALUES (:c, :t, :p, 'mcq', 'easy', 'Legacy?', '[{\"id\": \"a\", \"text\": \"A\"}]'::jsonb, 'a', :s) "
            "RETURNING id",
            c=setup.course, t=setup.topics[0], p=purpose, s=status,
        )  # fmt: skip
        assert answer(quiz_api(), setup.course, quiz["quiz_id"], legacy, "a").status_code == 404
    # questions of another quiz of the same student under the wrong quiz id
    mine2 = generate(quiz_api(), setup.course, setup.topics).json()
    assert answer(quiz_api(), setup.course, quiz["quiz_id"], mine2["questions"][0]["id"], "a").status_code == 404


def test_simultaneous_submissions_are_serialized(world, quiz_api, setup, quiz):
    api = quiz_api()
    same_q, diff_q = quiz["questions"][0], quiz["questions"][1]
    scenarios = [
        (same_q, (key_of(world, same_q["id"]), key_of(world, same_q["id"])), [200, 201]),
        (diff_q, (wrong_of(world, diff_q), key_of(world, diff_q["id"])), [201, 409]),
    ]
    for question, choices, expected in scenarios:
        holder = api.session_factory()  # holds the assignment row so both requests queue on it
        holder.execute(text("SELECT 1 FROM quiz_items WHERE question_id = :q FOR UPDATE"), {"q": question["id"]})
        threads = [
            _in_thread(lambda c=c: answer(api, setup.course, quiz["quiz_id"], question["id"], c,
                                          client=TestClient(api.app)))  # fmt: skip
            for c in choices
        ]
        threading.Event().wait(0.5)
        holder.rollback()
        holder.close()
        statuses = []
        for thread, result in threads:
            thread.join(10)
            statuses.append(result["r"].status_code)
        assert sorted(statuses) == expected, statuses
        count = world.scalar(
            "SELECT count(*) FROM attempts a JOIN quiz_items qi ON qi.id = a.quiz_item_id WHERE qi.question_id = :q",
            q=question["id"],
        )
        assert count == 1
    assert world.scalar("SELECT attempts FROM topic_practice_stats WHERE user_id = :u", u=setup.student.id) == 2


def test_attempt_and_baseline_commit_together(world, quiz_api, setup, quiz, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("simulated failure after the statistics update")

    monkeypatch.setattr(attempts_module, "feedback_view", broken)
    question = quiz["questions"][0]
    # Unexpected errors no longer reach the ASGI server (its traceback could print SQL parameters, review v2 H4):
    # the endpoint answers the generic 500 itself, and the transaction was rolled back all the same.
    response = answer(quiz_api(), setup.course, quiz["quiz_id"], question["id"], key_of(world, question["id"]))
    assert response.status_code == 500 and response.json()["code"] == "server_error"
    assert "simulated failure" not in response.text
    assert world.scalar("SELECT count(*) FROM attempts WHERE user_id = :u", u=setup.student.id) == 0
    assert world.scalar("SELECT count(*) FROM topic_practice_stats WHERE user_id = :u", u=setup.student.id) == 0


def test_feedback_omits_citations_that_are_no_longer_visible_without_changing_the_grade(world, quiz_api, setup, quiz):
    api = quiz_api()
    question = quiz["questions"][0]
    world.execute("UPDATE documents SET status = 'stale' WHERE id = :d", d=setup.document)
    first = answer(api, setup.course, quiz["quiz_id"], question["id"], key_of(world, question["id"]))
    assert first.status_code == 201 and first.json()["correct"] is True
    assert first.json()["citations"] == [] and first.json()["citations_omitted"] == 1
    assert "Lecture 3.pdf" not in first.text and TEXT1[:30] not in first.text
    world.execute("UPDATE documents SET status = 'indexed' WHERE id = :d", d=setup.document)
    replay = answer(api, setup.course, quiz["quiz_id"], question["id"], key_of(world, question["id"])).json()
    assert replay["citations_omitted"] == 0 and replay["citations"][0]["snippet"].startswith("The limiting reagent")
    world.execute("UPDATE chunks SET text = 'Rewritten.' WHERE document_id = :d", d=setup.document)
    read = api.client.get(f"/api/v1/courses/{setup.course}/quizzes/{quiz['quiz_id']}").json()
    feedback = read["questions"][0]["attempt"]
    assert feedback["citations"] == [] and feedback["citations_omitted"] == 1
    assert (feedback["correct"], feedback["answer"], feedback["mastery"]["after"]) == (True, replay["answer"], 1.0)
    assert all(q["attempt"] is None for q in read["questions"][1:])
    assert not keys_in(read["questions"][1]) & set(FORBIDDEN)


def test_flags_are_independent_and_grading_needs_no_ai(world, quiz_api, setup, quiz, fake):
    question = quiz["questions"][0]
    no_attempts = quiz_api(quiz_attempts_enabled=False)
    response = answer(no_attempts, setup.course, quiz["quiz_id"], question["id"], "a")
    assert response.status_code == 503 and response.json()["code"] == "quiz_attempts_unavailable"
    assert no_attempts.client.get(f"/api/v1/courses/{setup.course}/quizzes/{quiz['quiz_id']}").status_code == 200
    assert generate(no_attempts, setup.course, setup.topics).status_code == 201
    fake.stop()  # the AI service is gone
    no_generation = quiz_api(quiz_generation_enabled=False)
    response = generate(no_generation, setup.course, setup.topics)
    assert response.status_code == 503 and response.json()["code"] == "quiz_generation_unavailable"
    graded = answer(no_generation, setup.course, quiz["quiz_id"], question["id"], key_of(world, question["id"]))
    assert graded.status_code == 201 and graded.json()["correct"] is True
    graded2 = answer(quiz_api(), setup.course, quiz["quiz_id"], quiz["questions"][1]["id"],
                     key_of(world, quiz["questions"][1]["id"]))  # fmt: skip
    assert graded2.status_code == 201


def test_removed_student_loses_access_but_history_is_kept(world, quiz_api, setup, quiz):
    question = quiz["questions"][0]
    assert answer(quiz_api(), setup.course, quiz["quiz_id"], question["id"], key_of(world, question["id"])).status_code == 201
    world.execute("DELETE FROM enrollments WHERE user_id = :u AND course_id = :c", u=setup.student.id, c=setup.course)
    assert quiz_api().client.get(f"/api/v1/courses/{setup.course}/quizzes/{quiz['quiz_id']}").status_code == 404
    assert answer(quiz_api(), setup.course, quiz["quiz_id"], question["id"], "a").status_code == 404
    assert world.scalar("SELECT count(*) FROM attempts WHERE user_id = :u", u=setup.student.id) == 1
    assert session_row(world, quiz["quiz_id"])["status"] == "ready"


# --- review v2: reservation identity under timeout and cancellation (H3) -------------------------


def _tight(**overrides):
    return {"quiz_probe_deadline_seconds": 0.3, "quiz_generate_deadline_seconds": 0.5,
            "quiz_pipeline_deadline_seconds": 1, **overrides}  # fmt: skip


def _slow_after_reserve(monkeypatch, seconds, then=None):
    """The reservation COMMITS inside the worker thread, then the thread lingers: the result reaches the event loop
    only after the deadline / the cancellation, exactly the lost-reservation case of the independent review."""
    real = generation.reserve

    def slow(*args, **kwargs):
        result = real(*args, **kwargs)
        time.sleep(seconds)
        if then is not None:
            then()
        return result

    monkeypatch.setattr(generation, "reserve", slow)


def _quota_used(world, setup):
    return world.scalar("SELECT used FROM quiz_generation_quotas WHERE user_id = :u", u=setup.student.id)


def test_a_deadline_right_after_the_reservation_commit_fails_that_reservation(world, quiz_api, setup, fake, monkeypatch):
    api = quiz_api(**_tight())
    warm(api)
    _slow_after_reserve(monkeypatch, 1.4)  # the AnyIO deadline (1 s) expires while the thread still runs
    key = uuid.uuid4()
    response = generate(api, setup.course, setup.topics, key=key)
    assert response.status_code == 504 and response.json()["code"] == "quiz_timeout"
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "deadline" and row["attempt_token"] is None
    assert fake.generations() == [] and _quota_used(world, setup) == 1
    assert api.app.state.quiz_limiter.in_use() == (0, 0)
    monkeypatch.undo()
    again = generate(api, setup.course, setup.topics, key=key)  # the durable outcome is stable and consumes nothing
    assert again.status_code == 504 and _quota_used(world, setup) == 1


def test_a_cancellation_right_after_the_reservation_commit_fails_that_reservation(world, quiz_api, setup, fake, monkeypatch):
    api = quiz_api()
    warm(api)
    _slow_after_reserve(monkeypatch, 1.0)
    req = generation.QuizRequest(setup.course, uuid.uuid4(), tuple(sorted(setup.topics)), 2, "easy", "class_only")

    async def main():
        with anyio.move_on_after(0.4):  # cancelled while the thread has already committed the reservation
            await generation.create_quiz(api.app.state, setup.student, req)

    anyio.run(main)
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "cancelled"
    assert fake.generations() == [] and _quota_used(world, setup) == 1
    assert api.app.state.quiz_limiter.in_use() == (0, 0)


def test_an_uncertain_reservation_commit_is_resolved_by_its_token(world, quiz_api, setup, fake, monkeypatch):
    real = generation.reserve

    def committed_then_lost(*args, **kwargs):
        real(*args, **kwargs)
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(generation, "reserve", committed_then_lost)
    response = generate(quiz_api(), setup.course, setup.topics)
    assert response.status_code == 503 and response.json()["code"] == "quiz_generation_failed"
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "server_error"
    assert fake.generations() == [] and _quota_used(world, setup) == 1


def test_a_cancellation_after_a_committed_ready_outcome_never_overwrites_it(world, quiz_api, setup, fake, monkeypatch):
    api = quiz_api()
    warm(api)
    real = generation.persist

    def committed_then_slow(*args):
        view = real(*args)
        time.sleep(1.0)  # the commit is done; the cancellation below arrives while the thread lingers
        return view

    monkeypatch.setattr(generation, "persist", committed_then_slow)
    req = generation.QuizRequest(setup.course, uuid.uuid4(), tuple(sorted(setup.topics)), 2, "easy", "class_only")

    async def main():
        with anyio.move_on_after(0.7):
            await generation.create_quiz(api.app.state, setup.student, req)

    anyio.run(main)
    row = _last_session(world, setup)
    assert row["status"] == "ready" and row["failure_code"] is None
    assert world.scalar("SELECT count(*) FROM quiz_items WHERE session_id = :s", s=row["id"]) == 2
    assert _quota_used(world, setup) == 1
    monkeypatch.undo()
    replay = generate(api, setup.course, setup.topics, count=2, difficulty="easy", key=req.idempotency_key)
    assert replay.status_code == 200 and replay.json()["status"] == "ready"


# --- review v2: deadline, lease and recovery during database steps (M1) ----------------------------


def _hold_session_row(world, quiz_id, seconds) -> threading.Thread:
    locked = threading.Event()

    def run():
        with world.engine.connect() as connection:
            with connection.begin():
                connection.execute(text("SELECT 1 FROM quiz_sessions WHERE id = :q FOR UPDATE"), {"q": quiz_id})
                locked.set()
                time.sleep(seconds)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert locked.wait(5)
    return thread


def test_the_deadline_bounds_a_lock_wait_inside_the_persist_transaction(world, quiz_api, setup, fake):
    api = quiz_api(**_tight(quiz_pipeline_deadline_seconds=2, quiz_generate_deadline_seconds=1))
    warm(api)
    holders = []
    fake.on_generate = lambda payload: holders.append(_hold_session_row(world, payload["request_id"], 2.3))
    started = time.monotonic()
    response = generate(api, setup.course, setup.topics)
    for holder in holders:
        holder.join(5)
    assert response.status_code == 504 and response.json()["code"] == "quiz_timeout"
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "deadline"
    assert world.scalar("SELECT count(*) FROM questions WHERE generated_by_session_id = :s", s=row["id"]) == 0
    assert time.monotonic() - started < 6


def test_a_deadline_that_expires_before_commit_rolls_everything_back(world, quiz_api, setup, fake, monkeypatch):
    api = quiz_api(**_tight(quiz_pipeline_deadline_seconds=2, quiz_generate_deadline_seconds=1))
    warm(api)
    real = generation.quiz_view

    def slow_view(*args, **kwargs):
        view = real(*args, **kwargs)
        time.sleep(2.3)  # questions, assignments and the ready update are in the transaction, nothing committed
        return view

    monkeypatch.setattr(generation, "quiz_view", slow_view)
    response = generate(api, setup.course, setup.topics)
    assert response.status_code == 504 and response.json()["code"] == "quiz_timeout"
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "deadline"
    assert world.scalar("SELECT count(*) FROM questions WHERE generated_by_session_id = :s", s=row["id"]) == 0
    assert world.scalar("SELECT count(*) FROM quiz_items WHERE session_id = :s", s=row["id"]) == 0


def test_a_lease_that_expires_during_a_lock_wait_is_judged_by_the_database_clock(world, quiz_api, setup, fake):
    api = quiz_api()
    warm(api)
    holders = []

    def shorten_and_hold(payload):
        world.execute("UPDATE quiz_sessions SET lease_expires_at = now() + interval '0.6 second' WHERE id = :q",
                      q=payload["request_id"])  # fmt: skip
        holders.append(_hold_session_row(world, payload["request_id"], 1.3))

    fake.on_generate = shorten_and_hold
    response = generate(api, setup.course, setup.topics)
    for holder in holders:
        holder.join(5)
    # now() would still be the transaction's start, inside the lease; clock_timestamp() is after it.
    assert response.status_code == 503 and response.json()["code"] == "quiz_generation_failed"
    row = _last_session(world, setup)
    assert row["status"] == "failed" and row["failure_code"] == "lease_expired"
    assert world.scalar("SELECT count(*) FROM questions WHERE generated_by_session_id = :s", s=row["id"]) == 0


def test_a_completion_racing_the_recovery_is_reported_as_ready_not_failed(world, quiz_api, setup, fake, monkeypatch):
    api = quiz_api()
    warm(api)
    key = uuid.uuid4()
    real_view = generation.quiz_view
    paused, release, armed = threading.Event(), threading.Event(), {"first": True}

    def paused_view(*args, **kwargs):
        view = real_view(*args, **kwargs)
        if armed["first"]:  # the generator's own call: ready is UPDATEd, nothing committed yet
            armed["first"] = False
            paused.set()
            assert release.wait(15)
        return view

    monkeypatch.setattr(generation, "quiz_view", paused_view)
    fake.on_generate = lambda payload: world.execute(
        "UPDATE quiz_sessions SET lease_expires_at = now() + interval '1 second' WHERE id = :q", q=payload["request_id"]
    )
    generator, generated = _in_thread(lambda: generate(api, setup.course, setup.topics, count=2, difficulty="medium",
                                                       key=key, client=TestClient(api.app)))  # fmt: skip
    assert paused.wait(10)
    time.sleep(1.3)  # the lease is over; the finalizing transaction still has not committed
    req = generation.QuizRequest(setup.course, key, tuple(sorted(setup.topics)), 2, "medium", "class_only")
    reader, read = _in_thread(lambda: generation.lookup(api.session_factory, setup.student, req))
    time.sleep(0.5)
    assert reader.is_alive()  # the recovering UPDATE waits for the finalizing transaction's row lock
    release.set()
    reader.join(15)
    generator.join(15)
    outcome = read["r"]
    assert outcome.status == "ready" and outcome.view is not None and outcome.failure_code is None
    assert generated["r"].status_code == 201
    assert session_row(world, outcome.quiz_id)["status"] == "ready"


# --- review v2: fresh authorization before sensitive delivery (M1) --------------------------------


def _revoke(world, setup):
    world.execute("DELETE FROM enrollments WHERE user_id = :u AND course_id = :c", u=setup.student.id, c=setup.course)


def _no_quiz_content(response):
    assert response.status_code == 404 and response.json()["code"] == "course_not_found"
    assert response.headers["cache-control"] == "no-store"
    assert not keys_in(response.json()) & {"questions", "choices", "explanation", "stem", "hint"}


def test_access_revoked_before_a_ready_replay_is_delivered(world, quiz_api, setup, monkeypatch):
    api = quiz_api()
    key = uuid.uuid4()
    assert generate(api, setup.course, setup.topics, key=key).status_code == 201
    real = generation.lookup

    def revoked_after_the_read(*args):
        outcome = real(*args)
        _revoke(world, setup)
        return outcome

    monkeypatch.setattr(generation, "lookup", revoked_after_the_read)
    _no_quiz_content(generate(api, setup.course, setup.topics, key=key))


def test_access_revoked_before_a_fresh_quiz_is_delivered(world, quiz_api, setup, monkeypatch):
    real = generation.persist

    def revoked_after_commit(*args):
        view = real(*args)
        _revoke(world, setup)
        return view

    monkeypatch.setattr(generation, "persist", revoked_after_commit)
    _no_quiz_content(generate(quiz_api(), setup.course, setup.topics))
    assert _last_session(world, setup)["status"] == "ready"  # stored, just not delivered


def test_access_revoked_before_a_get_is_delivered(world, quiz_api, setup, quiz, monkeypatch):
    real = quizzes_router.read_quiz

    def revoked_after_the_read(*args):
        view = real(*args)
        _revoke(world, setup)
        return view

    monkeypatch.setattr(quizzes_router, "read_quiz", revoked_after_the_read)
    _no_quiz_content(quiz_api().client.get(f"/api/v1/courses/{setup.course}/quizzes/{quiz['quiz_id']}"))


def test_access_revoked_before_graded_feedback_is_delivered(world, quiz_api, setup, quiz, monkeypatch):
    real = quizzes_router.submit_attempt

    def revoked_after_the_commit(*args):
        result = real(*args)
        _revoke(world, setup)
        return result

    monkeypatch.setattr(quizzes_router, "submit_attempt", revoked_after_the_commit)
    question = quiz["questions"][0]
    response = answer(quiz_api(), setup.course, quiz["quiz_id"], question["id"], key_of(world, question["id"]))
    assert response.status_code == 404 and "explanation" not in response.text and "correct" not in response.text
    assert world.scalar("SELECT count(*) FROM attempts WHERE user_id = :u", u=setup.student.id) == 1  # recorded


# --- review v2: no sensitive payload in logs (H4) ------------------------------------------------

SENTINEL = "SENTINEL-4b81e7-never-log-this"


def _log_text(caplog) -> str:
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(record.exc_text or "")
        parts.append("" if record.exc_info is None else "".join(map(str, record.exc_info)))
    return "\n".join(parts)


def _assert_clean(caplog, *extra_secrets):
    logged = _log_text(caplog)
    for secret in (SENTINEL, "INSERT INTO", "[parameters", "answer_key", *extra_secrets):
        assert secret not in logged, secret
    assert all(r.exc_info is None for r in caplog.records), "a traceback was logged"


def test_the_sentinel_would_leak_through_a_raw_database_error(world):
    """Control: the failure used below really does render parameters in str(exc)."""
    with pytest.raises(DBAPIError) as raised:
        with world.engine.connect() as connection:
            connection.execute(text("SELECT CAST(:v AS integer)"), {"v": SENTINEL})
    assert SENTINEL in str(raised.value)


def test_a_database_error_while_persisting_a_quiz_logs_only_class_and_sqlstate(world, quiz_api, setup, fake, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    def leaky(request):
        body = quiz_for(request, setup.sources)
        for question in body["questions"]:
            question["explanation"] = f"{question['explanation']} {SENTINEL}-explanation"
            question["hint"] = f"{SENTINEL}-hint"
            question["stem"] = f"{question['stem']} {SENTINEL}-stem"
        return body

    fake.generate = leaky
    # content_hash is varchar(64): the INSERT of the first question fails with a DataError whose message and
    # SQLAlchemy's rendering contain the statement and every bound parameter (answer key, explanation, passages).
    monkeypatch.setattr(generation, "_content_hash", lambda *args, **kwargs: "x" * 70)
    response = generate(quiz_api(), setup.course, setup.topics)
    assert response.status_code == 503 and response.json()["code"] == "quiz_generation_failed"
    assert SENTINEL not in response.text
    assert _last_session(world, setup)["failure_code"] == "server_error"
    assert any("error_class=DataError sqlstate=22001" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog, TEXT1[:30], "Correct claim")


@pytest.mark.parametrize("endpoint", ["create", "read", "attempt"])
def test_unexpected_endpoint_errors_are_generic_and_leave_no_traceback(world, quiz_api, setup, quiz, caplog, monkeypatch, endpoint):
    caplog.set_level(logging.DEBUG)

    def broken(*args, **kwargs):
        raise StatementError(f"boom {SENTINEL}", f"INSERT INTO attempts (answer_key) VALUES ('{SENTINEL}')",
                             {"answer_key": SENTINEL, "explanation": SENTINEL}, ValueError(SENTINEL))  # fmt: skip

    api = quiz_api()
    course, quiz_id, question = setup.course, quiz["quiz_id"], quiz["questions"][0]
    if endpoint == "create":
        monkeypatch.setattr(generation, "lookup", broken)
        response = generate(api, course, setup.topics)
    elif endpoint == "read":
        monkeypatch.setattr(quizzes_router, "read_quiz", broken)
        response = api.client.get(f"/api/v1/courses/{course}/quizzes/{quiz_id}")
    else:
        monkeypatch.setattr(quizzes_router, "submit_attempt", broken)
        response = answer(api, course, quiz_id, question["id"], "a")
    assert response.status_code == 500 and response.json()["code"] == "server_error"
    assert response.headers["cache-control"] == "no-store" and SENTINEL not in response.text
    assert any("error_class=StatementError" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


# --- v3: same Idempotency-Key at the final quota unit ---------------------------------------------


def _req(setup, key, count=1):
    return generation.QuizRequest(setup.course, key, tuple(sorted(setup.topics)), count, "easy", "class_only")


def _stale_first_find(monkeypatch):
    """Both racers 'initially find no session': the first _find of the call under test reports none."""
    real = generation._find
    calls = []

    def find(session, user, req):
        calls.append(1)
        return None if len(calls) == 1 else real(session, user, req)

    monkeypatch.setattr(generation, "_find", find)
    return calls


@pytest.mark.parametrize("final_state", ["generating", "failed"])
def test_same_key_at_the_final_quota_unit_returns_the_durable_outcome(world, quiz_api, setup, monkeypatch, final_state):
    api = quiz_api(quiz_daily_generation_quota=1)
    key = uuid.uuid4()
    first = generation.reserve(api.session_factory, setup.student, _req(setup, key), api.settings)
    assert isinstance(first, generation.Reservation)
    if final_state == "failed":
        assert generation.mark_failed(api.session_factory, first.quiz_id, first.token, "deadline")
    calls = _stale_first_find(monkeypatch)
    second = generation.reserve(api.session_factory, setup.student, _req(setup, key), api.settings)
    assert isinstance(second, generation.Outcome) and second.quiz_id == first.quiz_id and second.status == final_state
    assert len(calls) == 2 and _quota_used(world, setup) == 1


def test_a_different_fingerprint_at_the_final_quota_unit_is_a_key_conflict(world, quiz_api, setup, monkeypatch):
    api = quiz_api(quiz_daily_generation_quota=1)
    key = uuid.uuid4()
    generation.reserve(api.session_factory, setup.student, _req(setup, key, count=1), api.settings)
    _stale_first_find(monkeypatch)
    with pytest.raises(generation.ApiError) as raised:
        generation.reserve(api.session_factory, setup.student, _req(setup, key, count=2), api.settings)
    assert raised.value.code == "idempotency_key_reused" and _quota_used(world, setup) == 1


def test_a_new_key_at_the_final_quota_unit_is_still_refused(world, quiz_api, setup):
    api = quiz_api(quiz_daily_generation_quota=1)
    generation.reserve(api.session_factory, setup.student, _req(setup, uuid.uuid4()), api.settings)
    with pytest.raises(generation.ApiError) as raised:
        generation.reserve(api.session_factory, setup.student, _req(setup, uuid.uuid4()), api.settings)
    assert raised.value.code == "quiz_quota_exceeded" and raised.value.headers["Retry-After"]
    assert _quota_used(world, setup) == 1


def test_concurrent_same_key_requests_share_one_reservation_and_one_quota_unit(world, quiz_api, setup):
    api = quiz_api(quiz_daily_generation_quota=1)
    key = uuid.uuid4()
    barrier = threading.Barrier(4)
    outcomes = []

    def race():
        barrier.wait()
        try:
            outcomes.append(generation.reserve(api.session_factory, setup.student, _req(setup, key), api.settings))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)

    threads = [threading.Thread(target=race) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    reservations = [o for o in outcomes if isinstance(o, generation.Reservation)]
    others = [o for o in outcomes if isinstance(o, generation.Outcome)]
    busy = [o for o in outcomes if getattr(o, "code", None) == "quiz_generation_busy"]
    assert len(reservations) == 1 and len(reservations) + len(others) + len(busy) == 4, outcomes
    assert all(o.quiz_id == reservations[0].quiz_id and o.status == "generating" for o in others)
    assert not [o for o in outcomes if getattr(o, "code", None) == "quiz_quota_exceeded"]
    assert _quota_used(world, setup) == 1
