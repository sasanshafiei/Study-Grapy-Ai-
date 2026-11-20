"""M5 course endpoints: list, topics, create, join (disposable database)."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import event

from app.schemas.courses import COURSE_KEYS, TOPIC_KEYS
from app.services import courses as course_service
from tests.api_support import asgi_call

JOIN = "/api/v1/courses/join"


@pytest.fixture
def people(world):
    professor = world.user("professor", name="Prof. Ada")
    student = world.user("student", name="Sam")
    return professor, student


# --- list ------------------------------------------------------------------------------------------


def test_list_shapes_and_join_code_only_for_the_owner(api, world, people):
    professor, student = people
    course = world.course(professor, "Chemistry", code="CHEM 101", join_code="ABCD2345")
    world.enroll(student, course)

    owner_view = api.as_user(professor).client.get("/api/v1/courses")
    assert owner_view.status_code == 200
    (item,) = owner_view.json()
    assert set(item) == COURSE_KEYS
    assert item == {"id": str(course), "code": "CHEM 101", "name": "Chemistry", "professor_name": "Prof. Ada",
                    "join_code": "ABCD2345", "student_count": 1}  # fmt: skip

    (student_item,) = api.as_user(student).client.get("/api/v1/courses").json()
    assert student_item["join_code"] == "" and student_item["id"] == str(course)
    assert student_item["professor_name"] == "Prof. Ada"


def test_legacy_course_has_empty_code(api, world, people):
    professor, _ = people
    world.course(professor, "Legacy")  # no code: the column default
    (item,) = api.as_user(professor).client.get("/api/v1/courses").json()
    assert item["code"] == ""


def test_lists_are_isolated_and_follow_database_roles(api, world, people):
    professor, student = people
    other_professor = world.user("professor")
    mine = world.course(professor, "Mine")
    world.course(other_professor, "Theirs")
    world.enroll(student, mine)
    other_student = world.user("student")
    assert [c["name"] for c in api.as_user(professor).client.get("/api/v1/courses").json()] == ["Mine"]
    assert [c["name"] for c in api.as_user(student).client.get("/api/v1/courses").json()] == ["Mine"]
    assert api.as_user(other_student).client.get("/api/v1/courses").json() == []
    assert api.as_user(world.user("admin")).client.get("/api/v1/courses").json() == []
    # The database role decides, not the principal's claim.
    world.execute("UPDATE users SET role = 'student' WHERE id = :id", id=professor.id)
    assert api.as_user(professor).client.get("/api/v1/courses").json() == []


def test_disabled_missing_and_anonymous_callers(api, world, people):
    professor, _ = people
    assert api.as_user(None).client.get("/api/v1/courses").status_code == 401
    world.execute("UPDATE users SET status = 'disabled' WHERE id = :id", id=professor.id)
    response = api.as_user(professor).client.get("/api/v1/courses")
    assert response.status_code == 403 and response.json()["code"] == "account_disabled"
    ghost = type(professor)(id=uuid.uuid4(), role="professor", status="active")
    assert api.as_user(ghost).client.get("/api/v1/courses").status_code == 401


def test_student_count_counts_only_active_enrolled_students(api, world, people):
    professor, student = people
    course = world.course(professor)
    world.enroll(student, course)
    disabled = world.user("student", status="disabled")
    world.enroll(disabled, course)
    assistant = world.user("student")
    world.enroll(assistant, course, role="assistant")
    promoted = world.user("professor")  # a professor holding a student enrollment is not a student
    world.enroll(promoted, course)
    (item,) = api.as_user(professor).client.get("/api/v1/courses").json()
    assert item["student_count"] == 1


def test_course_list_is_complete_and_uses_a_constant_number_of_queries(api, world, people):
    professor, student = people
    for i in range(30):
        course = world.course(professor, f"Course {i:02d}")
        world.enroll(student, course)
    statements = []

    def count(*_args):
        statements.append(1)

    event.listen(world.engine, "before_cursor_execute", count)
    try:
        response = api.as_user(professor).client.get("/api/v1/courses")
    finally:
        event.remove(world.engine, "before_cursor_execute", count)
    assert len(response.json()) == 30
    assert [c["name"] for c in response.json()] == [f"Course {i:02d}" for i in range(30)]
    assert all(c["student_count"] == 1 for c in response.json())
    assert len(statements) <= 3  # the account and the list, whatever the number of courses


# --- topics ----------------------------------------------------------------------------------------


def test_topics_are_isolated_per_course(api, world, people):
    professor, student = people
    course = world.course(professor)
    other = world.course(world.user("professor"))
    world.enroll(student, course)
    stoich = world.topic(course, "Stoichiometry")
    world.topic(course, "Equilibrium")
    world.topic(other, "Secret topic")
    for user in (professor, student):
        response = api.as_user(user).client.get(f"/api/v1/courses/{course}/topics")
        assert response.status_code == 200
        assert [t["name"] for t in response.json()] == ["Equilibrium", "Stoichiometry"]
        assert all(set(t) == TOPIC_KEYS for t in response.json())
        assert str(stoich) in {t["id"] for t in response.json()}
    for user in (professor, student):
        assert api.as_user(user).client.get(f"/api/v1/courses/{other}/topics").status_code == 404
    assert api.as_user(student).client.get(f"/api/v1/courses/{uuid.uuid4()}/topics").status_code == 404
    assert api.as_user(None).client.get(f"/api/v1/courses/{course}/topics").status_code == 401


# --- create ----------------------------------------------------------------------------------------


def test_professor_creates_a_course_with_a_secure_join_code(api, world, people):
    professor, student = people
    response = api.as_user(professor).client.post("/api/v1/courses", json={"name": " Organic ", "code": "CHEM 201"})
    assert response.status_code == 201, response.text
    item = response.json()
    world.track_course(item["id"])
    assert set(item) == COURSE_KEYS and item["name"] == "Organic" and item["code"] == "CHEM 201"
    assert item["student_count"] == 0 and item["professor_name"] == "Prof. Ada"
    code = item["join_code"]
    assert len(code) == course_service.JOIN_CODE_LENGTH and set(code) <= set(course_service.JOIN_CODE_ALPHABET)
    assert api.as_user(professor).client.get("/api/v1/courses").json()[0]["join_code"] == code


@pytest.mark.parametrize("role", ["student", "admin"])
def test_only_professors_create_courses(api, world, role):
    user = world.user(role)
    response = api.as_user(user).client.post("/api/v1/courses", json={"name": "X", "code": "X1"})
    assert response.status_code == 403 and response.json()["code"] == "course_create_not_allowed"


@pytest.mark.parametrize(
    "body, status",
    [
        ({"name": "X"}, 422),
        ({"name": "X", "code": "C", "extra": 1}, 422),
        ({"name": "", "code": "C"}, 422),
        ({"name": "X", "code": "C" * 33}, 422),
        ({"name": "X" * 151, "code": "C"}, 422),
        ({"name": "Bad\x00name", "code": "C"}, 422),
        ({"name": 5, "code": "C"}, 422),
    ],
)
def test_create_validation(api, people, body, status):
    professor, _ = people
    response = api.as_user(professor).client.post("/api/v1/courses", json=body)
    assert response.status_code == status


def test_create_rejects_ambiguous_json(api, people):
    professor, _ = people
    client = api.as_user(professor).client
    headers = {"Content-Type": "application/json"}
    assert client.post("/api/v1/courses", content=b'{"name":"a","name":"b","code":"c"}', headers=headers).status_code == 400
    assert client.post("/api/v1/courses", content=b"[]", headers=headers).status_code == 400
    assert client.post("/api/v1/courses", content=b'{"name":"a"', headers=headers).status_code == 400
    assert client.post("/api/v1/courses", content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
    gz = {**headers, "Content-Encoding": "gzip"}
    assert client.post("/api/v1/courses", content=b"{}", headers=gz).status_code == 415
    assert client.post("/api/v1/courses", content=b"{" + b" " * 5000 + b"}", headers=headers).status_code == 413


def test_join_code_collisions_are_retried_then_fail_cleanly(world, people):
    professor, _ = people
    world.course(professor, "Taken", join_code="TAKEN234")
    codes = iter(["taken234", "TAKEN234", "FRESH234"])  # case-insensitive and exact collisions first
    with world.engine.connect() as connection:
        from sqlalchemy.orm import Session

        with Session(bind=connection) as session:
            item = course_service.create_course(session, professor, "New", "N1", generate=lambda: next(codes))
            assert item["join_code"] == "FRESH234"
            session.rollback()
            with pytest.raises(Exception) as caught:
                course_service.create_course(session, professor, "New", "N1", generate=lambda: "TAKEN234")
            assert getattr(caught.value, "code", None) == "join_code_unavailable"
            session.rollback()


def test_unauthenticated_create_never_reads_the_body(api, world):
    result = asgi_call(api.as_user(None).app, "POST", "/api/v1/courses",
                       {"content-type": "application/json", "content-length": "2"}, [b"{}"])  # fmt: skip
    assert result.status == 401 and result.receive_calls == 0


# --- join ------------------------------------------------------------------------------------------


def test_student_joins_once_and_again_idempotently(api, world, people):
    professor, student = people
    course = world.course(professor, "Chem", code="CHEM 101", join_code="JOIN2345")
    client = api.as_user(student).client
    first = client.post(JOIN, json={"join_code": " join-2345 "})
    assert first.status_code == 201, first.text
    assert set(first.json()) == COURSE_KEYS and first.json()["join_code"] == "" and first.json()["student_count"] == 1
    again = client.post(JOIN, json={"join_code": "JOIN2345"})
    assert again.status_code == 200 and again.json()["id"] == str(course)
    assert world.scalar("SELECT count(*) FROM enrollments WHERE course_id = :c", c=course) == 1
    assert client.get(f"/api/v1/courses/{course}/topics").status_code == 200  # the enrollment grants access


def test_legacy_lower_case_code_matches_exactly(api, world, people):
    professor, student = people
    course = world.course(professor, join_code="legacy0code1")
    response = api.as_user(student).client.post(JOIN, json={"join_code": "legacy0code1"})
    assert response.status_code == 201 and response.json()["id"] == str(course)


def test_conflicting_enrollment_role_is_not_accepted(api, world, people):
    professor, student = people
    course = world.course(professor, join_code="ROLE2345")
    world.enroll(student, course, role="assistant")
    response = api.as_user(student).client.post(JOIN, json={"join_code": "ROLE2345"})
    assert response.status_code == 409 and response.json()["code"] == "enrollment_conflict"
    assert world.scalar("SELECT role FROM enrollments WHERE course_id = :c", c=course) == "assistant"


@pytest.mark.parametrize("owner_state", ["disabled", "student", "admin"])
def test_course_needs_an_active_professor_owner(api, world, people, owner_state):
    professor, student = people
    world.course(professor, join_code="OWNR2345")
    column = "status" if owner_state == "disabled" else "role"
    world.execute(f"UPDATE users SET {column} = :v WHERE id = :id", v=owner_state, id=professor.id)
    response = api.as_user(student).client.post(JOIN, json={"join_code": "OWNR2345"})
    assert response.status_code == 404 and response.json()["code"] == "join_code_not_found"


@pytest.mark.parametrize("role", ["professor", "admin"])
def test_only_students_join(api, world, people, role):
    professor, _ = people
    world.course(professor, join_code="ONLY2345")
    response = api.as_user(world.user(role)).client.post(JOIN, json={"join_code": "ONLY2345"})
    assert response.status_code == 403 and response.json()["code"] == "join_not_allowed"


def test_disabled_student_cannot_join(api, world, people):
    professor, _ = people
    world.course(professor, join_code="DISA2345")
    student = world.user("student", status="disabled")
    assert api.as_user(student).client.post(JOIN, json={"join_code": "DISA2345"}).status_code == 403


@pytest.mark.parametrize("body, status", [({"join_code": "NOPE2345"}, 404), ({"join_code": ""}, 422),
                                          ({"join_code": 12345678}, 422), ({"code": "X"}, 422),
                                          ({"join_code": "A" * 40}, 422)])  # fmt: skip
def test_invalid_join_codes(api, people, body, status):
    _, student = people
    assert api.as_user(student).client.post(JOIN, json=body).status_code == status


def test_concurrent_joins_create_exactly_one_enrollment(make_api, world, people):
    professor, student = people
    course = world.course(professor, join_code="RACE2345")
    harnesses = [make_api().as_user(student) for _ in range(8)]
    barrier = threading.Barrier(len(harnesses))
    statuses = []

    def join(harness):
        barrier.wait()
        statuses.append(harness.client.post(JOIN, json={"join_code": "RACE2345"}).status_code)

    threads = [threading.Thread(target=join, args=(h,)) for h in harnesses]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(statuses) == [200] * 7 + [201]
    assert world.scalar("SELECT count(*) FROM enrollments WHERE course_id = :c", c=course) == 1


def test_join_attempts_are_rate_limited_per_user(make_api, world, people):
    professor, student = people
    world.course(professor, join_code="RATE2345")
    api = make_api(settings_overrides={"join_rate_limit_attempts": 3}).as_user(student)
    codes = [api.client.post(JOIN, json={"join_code": f"WRONG{i:03d}"}).status_code for i in range(3)]
    assert codes == [404, 404, 404]
    limited = api.client.post(JOIN, json={"join_code": "RATE2345"})
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    other = world.user("student")
    assert api.as_user(other).client.post(JOIN, json={"join_code": "RATE2345"}).status_code == 201


def test_unauthenticated_join_never_reads_the_body(api):
    result = asgi_call(api.as_user(None).app, "POST", JOIN,
                       {"content-type": "application/json", "content-length": "2"}, [b"{}"])  # fmt: skip
    assert result.status == 401 and result.receive_calls == 0
