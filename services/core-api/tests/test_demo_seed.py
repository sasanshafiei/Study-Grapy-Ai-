"""WP-2: development/demo seed (app.seed): environment guard, idempotency, conflicts, rollback, concurrency."""

from __future__ import annotations

import json
import threading
import time

import pytest
from sqlalchemy import text

from app.auth import CurrentUser
from app.database_compatibility import IncompatibleDatabase
from app.seed import admin, demo

EMAILS = [email for _, email, _ in demo.DEMO_USERS]


def _purge(engine) -> None:
    """Remove demo rows from the disposable database (tests only; production code never deletes)."""
    with engine.begin() as connection:
        course = "SELECT id FROM courses WHERE join_code = :j"
        connection.execute(text(f"DELETE FROM topics WHERE course_id IN ({course})"), {"j": demo.COURSE_JOIN_CODE})
        connection.execute(text(f"DELETE FROM enrollments WHERE course_id IN ({course})"), {"j": demo.COURSE_JOIN_CODE})
        connection.execute(text("DELETE FROM enrollments WHERE user_id IN (SELECT id FROM users WHERE email = ANY(:e))"),
                           {"e": EMAILS})  # fmt: skip
        connection.execute(text("DELETE FROM courses WHERE join_code = :j"), {"j": demo.COURSE_JOIN_CODE})
        connection.execute(text("DELETE FROM users WHERE email = ANY(:e)"), {"e": EMAILS})


@pytest.fixture
def clean(world):
    _purge(world.engine)
    try:
        yield world
    finally:
        _purge(world.engine)


@pytest.fixture
def environ(disposable_target):
    return {
        "DATABASE_URL": disposable_target.url.render_as_string(hide_password=False),
        "MIGRATIONS_REQUIRE_DATABASE_COMMENT": disposable_target.marker,
        "ENVIRONMENT": "test",
    }


def _run(environ, *extra, capsys=None):
    argv = ["demo", "--database", environ["DATABASE_URL"].rsplit("/", 1)[1].split("?", 1)[0], *extra]
    code = admin.main(argv, environ)
    out = capsys.readouterr() if capsys else None
    return code, (out.out if out else ""), (out.err if out else "")


def _state(engine) -> dict:
    with engine.connect() as connection:
        users = {r.email: r for r in connection.execute(
            text("SELECT id, email, role, status, display_name, hashed_password FROM users WHERE email = ANY(:e)"),
            {"e": EMAILS})}  # fmt: skip
        course = connection.execute(
            text("SELECT id, owner_id, name, code FROM courses WHERE join_code = :j"), {"j": demo.COURSE_JOIN_CODE}
        ).one_or_none()
        enrollments = connection.execute(
            text("SELECT user_id, role FROM enrollments WHERE course_id = :c"), {"c": course.id if course else None}
        ).all()
        topics = connection.execute(
            text("SELECT id, name FROM topics WHERE course_id = :c ORDER BY name"), {"c": course.id if course else None}
        ).all()
    return {"users": users, "course": course, "enrollments": enrollments, "topics": topics}


def _empty(engine) -> bool:
    state = _state(engine)
    return not state["users"] and state["course"] is None


# --- environment and target guards (no writes) ---------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "production", "staging", "Development", ""])
def test_refused_outside_explicit_development_or_test(value, capsys):
    environ = {} if value is None else {"ENVIRONMENT": value}  # no DATABASE_URL: never reaches the database
    assert admin.main(["demo", "--database", "studygraph"], environ) == 1
    assert "runs only with ENVIRONMENT=development or test" in capsys.readouterr().err


def test_invalid_topic_file_refused_before_connecting(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text('{"topics": []}')
    assert admin.main(["demo", "--database", "x", "--topics", str(path)], {"ENVIRONMENT": "development"}) == 1
    assert "invalid topic file" in capsys.readouterr().err


def test_target_must_match_and_schema_must_be_current(clean, environ, monkeypatch, capsys):
    assert admin.main(["demo", "--database", "not_this_one"], environ) == 1
    assert "does not match the target" in capsys.readouterr().err

    def incompatible(connection):
        raise IncompatibleDatabase("the database schema revision is not the one this code requires")

    monkeypatch.setattr(admin, "check_database_compatibility", incompatible)
    code, _, err = _run(environ, capsys=capsys)
    assert code == 1 and "schema revision" in err
    assert _empty(clean.engine)


def test_packaged_sample_is_a_valid_topic_file():
    assert admin._topic_names(None) == ["Stoichiometry", "Chemical equilibrium", "Acids and bases", "Thermochemistry"]


# --- behaviour ---------------------------------------------------------------------------------------------------


def test_dry_run_on_an_empty_database_writes_nothing(clean, environ, capsys):
    code, out, _ = _run(environ, "--dry-run", capsys=capsys)
    assert code == 0
    assert out.count("would create\tuser\t-\t") == 3
    assert "would create\tcourse\t-\tGeneral Chemistry (join code SEEDCHEM)" in out
    assert "would create\tenrollment" in out
    assert "topics 4 to import (dry run: nothing written)" in out
    assert _empty(clean.engine)


def test_seed_creates_the_dataset_once_and_repeats_as_a_no_op(clean, environ, capsys):
    code, out, _ = _run(environ, capsys=capsys)
    assert code == 0 and out.count("created\tuser\t") == 3 and "created\tcourse" in out
    assert "topics 4 created, 0 already present, 0 kept" in out
    first = _state(clean.engine)
    users = first["users"]
    assert {u.role for u in users.values()} == {"admin", "professor", "student"}
    assert all(u.status == "active" and u.hashed_password == demo.LOCKED_PASSWORD for u in users.values())
    professor, student = users["professor@demo.studygraph.test"], users["student@demo.studygraph.test"]
    assert first["course"].owner_id == professor.id and first["course"].name == "General Chemistry"
    assert [(e.user_id, e.role) for e in first["enrollments"]] == [(student.id, "student")]
    assert [t.name for t in first["topics"]] == ["Acids and bases", "Chemical equilibrium", "Stoichiometry",
                                                 "Thermochemistry"]  # fmt: skip

    code, out, _ = _run(environ, capsys=capsys)
    assert code == 0 and "created" not in out.replace("0 created", "")
    assert out.count("exists\t") == 5 and "topics 0 created, 4 already present, 0 kept" in out
    assert _state(clean.engine) == first

    code, out, _ = _run(environ, "--dry-run", capsys=capsys)
    assert code == 0 and "would create" not in out and "0 to create, 4 already present" in out
    assert _state(clean.engine) == first


def test_partial_existing_data_is_reused_and_never_modified(clean, environ, tmp_path, capsys):
    with clean.engine.begin() as connection:
        professor = connection.execute(text(
            "INSERT INTO users (email, display_name, hashed_password, role, status) "
            "VALUES ('professor@demo.studygraph.test', 'Dr. Existing', 'real-hash-from-ce3', 'professor', 'active') "
            "RETURNING id")).scalar_one()  # fmt: skip
        course = connection.execute(text(
            "INSERT INTO courses (owner_id, name, code, join_code) VALUES (:o, 'Renamed Chemistry', 'X1', :j) "
            "RETURNING id"), {"o": professor, "j": demo.COURSE_JOIN_CODE}).scalar_one()  # fmt: skip
        connection.execute(text("INSERT INTO topics (course_id, name) VALUES (:c, 'stoichiometry'), (:c, 'Kinetics')"),
                           {"c": course})  # fmt: skip
    topics = tmp_path / "topics.json"
    topics.write_text(json.dumps({"topics": [{"name": "Stoichiometry"}, {"name": "Gases"}]}))

    code, out, _ = _run(environ, "--topics", str(topics), capsys=capsys)
    assert code == 0
    assert f"exists\tuser\t{professor}\tprofessor" in out and f"exists\tcourse\t{course}" in out
    assert "topics 1 created, 1 already present, 1 kept" in out
    state = _state(clean.engine)
    kept = state["users"]["professor@demo.studygraph.test"]
    assert (kept.display_name, kept.hashed_password) == ("Dr. Existing", "real-hash-from-ce3")
    assert (state["course"].name, state["course"].code) == ("Renamed Chemistry", "X1")
    assert [t.name for t in state["topics"]] == ["Gases", "Kinetics", "stoichiometry"]


@pytest.mark.parametrize("conflict", ["admin_role", "foreign_course", "disabled_student", "enrollment_role"])
def test_conflicting_data_refuses_the_whole_seed(clean, environ, conflict, capsys):
    with clean.engine.begin() as connection:
        def user(email, role, status="active"):
            sql = ("INSERT INTO users (email, display_name, hashed_password, role, status) "
                   "VALUES (:e, 'Someone', 'x', :r, :s) RETURNING id")
            return connection.execute(text(sql), {"e": email, "r": role, "s": status}).scalar_one()

        if conflict == "admin_role":
            user("admin@demo.studygraph.test", "student")
        elif conflict == "foreign_course":
            other = clean.user("professor")
            clean.course(other, join_code=demo.COURSE_JOIN_CODE)
        elif conflict == "disabled_student":
            user("student@demo.studygraph.test", "student", "disabled")
        else:
            professor = user("professor@demo.studygraph.test", "professor")
            student = user("student@demo.studygraph.test", "student")
            course = connection.execute(text(
                "INSERT INTO courses (owner_id, name, join_code) VALUES (:o, 'C', :j) RETURNING id"),
                {"o": professor, "j": demo.COURSE_JOIN_CODE}).scalar_one()  # fmt: skip
            connection.execute(text("INSERT INTO enrollments (user_id, course_id, role) VALUES (:u, :c, 'ta')"),
                               {"u": student, "c": course})  # fmt: skip
    before = _state(clean.engine)
    code, _, err = _run(environ, capsys=capsys)
    assert code == 1 and "nothing was written" in err
    assert _state(clean.engine) == before  # no user, course, enrollment or topic was added


def test_a_failure_midway_rolls_everything_back(clean, environ, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("injected failure after users, course and enrollment were written")

    monkeypatch.setattr(demo.catalogue, "import_topics", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        _run(environ)
    assert _empty(clean.engine)


def _wait_for_advisory_waiters(engine, count: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND objid = :k"
            ), {"k": demo.SEED_LOCK_KEY}).scalar_one()  # fmt: skip
        if waiting >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"expected {count} seed runs waiting for the seed lock")


def test_concurrent_seeds_create_each_row_once(clean, environ):
    codes: list[int] = []
    holder = clean.engine.connect()
    try:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": demo.SEED_LOCK_KEY})
        threads = [threading.Thread(target=lambda: codes.append(_run(environ)[0])) for _ in range(2)]
        for thread in threads:
            thread.start()
        _wait_for_advisory_waiters(clean.engine, 2)  # both runs are blocked on the seed lock
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": demo.SEED_LOCK_KEY})
        holder.commit()
    finally:
        holder.close()
    for thread in threads:
        thread.join(timeout=30)
    assert codes == [0, 0]
    state = _state(clean.engine)
    assert len(state["users"]) == 3 and len(state["enrollments"]) == 1 and len(state["topics"]) == 4
    with clean.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM courses WHERE join_code = :j"),
                                  {"j": demo.COURSE_JOIN_CODE}).scalar_one() == 1  # fmt: skip


def test_a_blocked_seed_times_out_and_writes_nothing(clean, environ, monkeypatch, capsys):
    monkeypatch.setattr(demo.catalogue, "LOCK_TIMEOUT", "200ms")
    holder = clean.engine.connect()
    try:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": demo.SEED_LOCK_KEY})
        code, _, err = _run(environ, capsys=capsys)
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": demo.SEED_LOCK_KEY})
        holder.commit()
        holder.close()
    assert code == 1 and "holds a demo row; nothing was written" in err
    assert _empty(clean.engine)


def test_seeded_student_sees_the_course_and_its_topics(clean, api, environ):
    assert _run(environ)[0] == 0
    state = _state(clean.engine)
    student = state["users"]["student@demo.studygraph.test"]
    client = api.as_user(CurrentUser(id=student.id, role="student", status="active")).client
    courses = client.get("/api/v1/courses").json()
    assert [(c["id"], c["name"]) for c in courses] == [(str(state["course"].id), "General Chemistry")]
    topics = client.get(f"/api/v1/courses/{state['course'].id}/topics").json()
    assert [t["name"] for t in topics] == [t.name for t in state["topics"]]
