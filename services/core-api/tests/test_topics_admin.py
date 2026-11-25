"""WP-1: course topic import (app.topics): validation, dry run, idempotency, isolation, concurrency, visibility."""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.harvest import queries
from app.topics import admin, catalogue
from app.topics.catalogue import CatalogueError, parse_catalogue
from tests.harvest_support import opt_in, wikipedia_source

SAMPLE = Path(__file__).parent / "fixtures" / "topics.dev-sample.json"


def _doc(*names) -> bytes:
    return json.dumps({"topics": [{"name": n} for n in names]}).encode()


# --- file validation (no database) -------------------------------------------------------------


def test_parse_normalizes_and_keeps_file_order():
    data = _doc("  Chemical \t equilibrium ", "Café chemistry", "x" * 150)
    assert parse_catalogue(data) == ["Chemical equilibrium", "Café chemistry", "x" * 150]


def test_development_sample_is_a_valid_file():
    assert len(parse_catalogue(SAMPLE.read_bytes())) == 4


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not json", "not valid JSON"),
        (b'{"topics": [], "topics": []}', "not valid JSON"),
        (b'{"topics": [{"name": NaN}]}', "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        (b'[{"name": "A"}]', 'exactly one key, "topics"'),
        (b'{"topics": [{"name": "A"}], "source": "x"}', 'exactly one key, "topics"'),
        (b'{"topics": {"name": "A"}}', "non-empty array"),
        (b'{"topics": []}', "non-empty array"),
        (b'{"topics": ["A"]}', r'topics\[0\] must be an object'),
        (b'{"topics": [{"name": "A", "id": "x"}]}', r'topics\[0\] must be an object'),
        (b'{"topics": [{"name": 7}]}', r"topics\[0\].name must be a string"),
        (b'{"topics": [{"name": "  "}]}', r"topics\[0\].name is empty"),
        (_doc("A", "x" * 151), r"topics\[1\].name is longer than 150"),
        (_doc("bell\u0007"), "U\\+0007"),
        (_doc("zero​width"), "U\\+200B"),
        (b'{"topics": [{"name": "lone \\ud800"}]}', "U\\+D800"),
        (_doc("Stoichiometry", "  STOICHIOMETRY "), r"topics\[1\] repeats topics\[0\]"),
        (_doc(*(f"T{i}" for i in range(501))), "more than 500"),
        (b" " * (catalogue.MAX_FILE_BYTES + 1), "larger than"),
    ],
)
def test_invalid_files_are_refused(data, message):
    with pytest.raises(CatalogueError, match=message):
        parse_catalogue(data)


def test_cli_validates_everything_before_connecting(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_bytes(_doc("ok", "OK"))
    # An empty environment has no DATABASE_URL: reaching the database step would report that instead.
    assert admin.main(["import", "--database", "x", "--course", str(uuid.uuid4()), "--file", str(bad)], {}) == 1
    assert "invalid topic file: topics[1] repeats topics[0]" in capsys.readouterr().err
    assert admin.main(["import", "--database", "x", "--course", "nope", "--file", str(bad)], {}) == 1
    assert "--course is not a UUID" in capsys.readouterr().err
    assert admin.main(["import", "--database", "x", "--course", str(uuid.uuid4()),
                       "--file", str(tmp_path / "missing.json")], {}) == 1  # fmt: skip
    assert "cannot read --file" in capsys.readouterr().err


# --- against the disposable database ------------------------------------------------------------


@pytest.fixture
def environ(disposable_target):
    return {
        "DATABASE_URL": disposable_target.url.render_as_string(hide_password=False),
        "MIGRATIONS_REQUIRE_DATABASE_COMMENT": disposable_target.marker,
    }


@pytest.fixture
def course(world):
    return world.course(world.user("professor"))


def _import(environ, course_id, tmp_path, *names, dry_run=False, capsys=None):
    path = tmp_path / f"{uuid.uuid4().hex}.json"
    path.write_bytes(_doc(*names))
    argv = ["import", "--database", environ_db(environ), "--course", str(course_id), "--file", str(path)]
    code = admin.main(argv + (["--dry-run"] if dry_run else []), environ)
    out = capsys.readouterr() if capsys else None
    return code, (out.out if out else ""), (out.err if out else "")


def environ_db(environ) -> str:
    return environ["DATABASE_URL"].rsplit("/", 1)[1].split("?", 1)[0]


def _topics(world, course_id) -> dict[str, uuid.UUID]:
    with world.engine.connect() as connection:
        rows = connection.execute(text("SELECT id, name FROM topics WHERE course_id = :c"), {"c": course_id}).all()
    return {row.name: row.id for row in rows}


def test_dry_run_reports_the_plan_and_writes_nothing(world, environ, course, tmp_path, capsys):
    world.topic(course, "Stoichiometry")
    code, out, _ = _import(environ, course, tmp_path, "stoichiometry", "Thermochemistry", dry_run=True, capsys=capsys)
    assert code == 0
    assert "would create\t-\tThermochemistry" in out
    assert "(stored as 'Stoichiometry'; not renamed)" in out
    assert "1 to create, 1 already present, 0 kept (dry run: nothing written)" in out
    assert set(_topics(world, course)) == {"Stoichiometry"}


def test_repeated_import_is_idempotent_and_never_renames_or_deletes(world, environ, course, tmp_path, capsys):
    code, out, _ = _import(environ, course, tmp_path, "Stoichiometry", "Chemical equilibrium", capsys=capsys)
    assert code == 0 and "2 created, 0 already present, 0 kept" in out
    first = _topics(world, course)
    assert set(first) == {"Stoichiometry", "Chemical equilibrium"}
    assert f"created\t{first['Stoichiometry']}\tStoichiometry" in out

    code, out, _ = _import(environ, course, tmp_path, "Stoichiometry", "Chemical equilibrium", capsys=capsys)
    assert code == 0 and "0 created, 2 already present, 0 kept" in out
    assert _topics(world, course) == first

    # Different case/spacing matches (no rename); a topic left out is kept; a new one is added.
    code, out, _ = _import(environ, course, tmp_path, "  STOICHIOMETRY", "Acids and bases", capsys=capsys)
    assert code == 0 and "1 created, 1 already present, 1 kept" in out
    assert f"kept\t{first['Chemical equilibrium']}\tChemical equilibrium" in out
    after = _topics(world, course)
    assert after["Stoichiometry"] == first["Stoichiometry"] and after["Chemical equilibrium"] == first["Chemical equilibrium"]
    assert set(after) == {"Stoichiometry", "Chemical equilibrium", "Acids and bases"}


def test_courses_are_isolated_and_unknown_courses_refused(world, environ, course, tmp_path, capsys):
    other = world.course(world.user("professor"))
    world.topic(other, "Stoichiometry")
    assert _import(environ, course, tmp_path, "Stoichiometry", capsys=capsys)[0] == 0
    mine, theirs = _topics(world, course), _topics(world, other)
    assert set(mine) == set(theirs) == {"Stoichiometry"} and mine["Stoichiometry"] != theirs["Stoichiometry"]

    missing = uuid.uuid4()
    code, _, err = _import(environ, missing, tmp_path, "Orphan", capsys=capsys)
    assert code == 1 and f"course {missing}: the course does not exist" in err
    with world.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM topics WHERE name = 'Orphan'")).scalar_one() == 0


def test_target_must_be_named_explicitly(environ, course, tmp_path, capsys):
    path = tmp_path / "t.json"
    path.write_bytes(_doc("A"))
    code = admin.main(["import", "--database", "not_this_one", "--course", str(course), "--file", str(path)], environ)
    assert code == 1 and "does not match the target" in capsys.readouterr().err


def test_stdin_input(world, environ, course, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(_doc("From stdin"))))
    argv = ["import", "--database", environ_db(environ), "--course", str(course), "--file", "-"]
    assert admin.main(argv, environ) == 0
    assert set(_topics(world, course)) == {"From stdin"}


def _wait_for_lock_waiters(world, count: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with world.engine.connect() as connection:
            waiting = connection.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query LIKE 'SELECT id FROM courses WHERE id = % FOR NO KEY UPDATE'"
            )).scalar_one()  # fmt: skip
        if waiting >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"expected {count} imports waiting for the course lock")


def test_concurrent_imports_insert_each_name_once(world, environ, course, tmp_path):
    files = []
    for names in (("Alpha", "Beta", "Gamma"), ("beta", "GAMMA", "Delta")):
        path = tmp_path / f"{names[0]}.json"
        path.write_bytes(_doc(*names))
        files.append(path)
    codes: list[int] = []

    def run(path):
        argv = ["import", "--database", environ_db(environ), "--course", str(course), "--file", str(path)]
        codes.append(admin.main(argv, environ))

    holder = world.engine.connect()
    try:
        holder.execute(text("SELECT 1 FROM courses WHERE id = :c FOR NO KEY UPDATE"), {"c": course})
        threads = [threading.Thread(target=run, args=(path,)) for path in files]
        for thread in threads:
            thread.start()
        _wait_for_lock_waiters(world, 2)  # both imports are blocked on the same course row
        holder.rollback()
    finally:
        holder.close()
    for thread in threads:
        thread.join(timeout=30)
    assert codes == [0, 0]
    with world.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT lower(name), count(*) FROM topics WHERE course_id = :c GROUP BY 1 ORDER BY 1"), {"c": course}
        ).all()
    assert [(name, n) for name, n in rows] == [("alpha", 1), ("beta", 1), ("delta", 1), ("gamma", 1)]


def test_a_busy_course_fails_cleanly_after_the_lock_timeout(world, environ, course, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(catalogue, "LOCK_TIMEOUT", "200ms")
    holder = world.engine.connect()
    try:
        holder.execute(text("SELECT 1 FROM courses WHERE id = :c FOR NO KEY UPDATE"), {"c": course})
        code, _, err = _import(environ, course, tmp_path, "Blocked", capsys=capsys)
    finally:
        holder.rollback()
        holder.close()
    assert code == 1 and "locked by another topic import; nothing was written" in err
    assert _topics(world, course) == {}


def test_imported_topics_reach_topic_dependent_features(api, world, environ, course, tmp_path, capsys):
    student = world.user("student")
    world.enroll(student, course)
    assert _import(environ, course, tmp_path, "Chemical  equilibrium", "Stoichiometry", capsys=capsys)[0] == 0
    ids = _topics(world, course)

    response = api.as_user(student).client.get(f"/api/v1/courses/{course}/topics")
    assert response.status_code == 200
    assert response.json() == [{"id": str(ids[name]), "name": name} for name in ("Chemical equilibrium", "Stoichiometry")]

    opt_in(world, course, wikipedia_source(world))
    with sessionmaker(bind=world.engine)() as session:
        proposals = queries.propose(session, course)
        session.rollback()
    assert {(p.topic_id, p.query_text, p.refused) for p in proposals} == {
        (ids["Chemical equilibrium"], "Chemical equilibrium", None),
        (ids["Stoichiometry"], "Stoichiometry", None),
    }
