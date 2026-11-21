"""Revision 0007 (M8) on the disposable database: preflight, backfill, locking, constraints, guards, downgrade."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.alembic_cli import ALEMBIC_INI, CORE_API_DIR, run_alembic
from tests.m8_support import graded_attempt, legacy_attempt
from tests.test_migration_0003 import _failed, scratch3  # noqa: F401  (fixture chain)
from tests.test_migration_0005 import scratch5  # noqa: F401
from tests.test_migration_0006 import scratch6  # noqa: F401
from tests.test_migrations import _current, _insert_full_baseline_dataset

REVISION, PREVIOUS = "0007", "0006"
NEW_TABLES = {"mastery_streams", "mastery_outbox", "mastery_model_state", "study_events", "analytics_weekly_releases"}


def _tables(engine) -> set[str]:
    with engine.connect() as c:
        return set(c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())


def _people(connection) -> dict:
    """Two students, two topics of the baseline course (revision 0006)."""
    course, topic = connection.execute(text("SELECT course_id, id FROM topics LIMIT 1")).one()
    other_topic = connection.execute(
        text("INSERT INTO topics (course_id, name) VALUES (:c, 'Second') RETURNING id"), {"c": course}
    ).scalar_one()
    students = [
        connection.execute(
            text("INSERT INTO users (email, display_name, hashed_password, role, status) "
                 "VALUES (:e, 'S', 'x', 'student', 'active') RETURNING id"),  # fmt: skip
            {"e": f"{uuid.uuid4().hex}@example.test"},
        ).scalar_one()
        for _ in range(2)
    ]
    return {"course": course, "topics": [topic, other_topic], "students": students}


@pytest.fixture
def at_0006_with_m7(scratch6, disposable_engine, alembic, committed_transaction):  # noqa: F811
    alembic("upgrade", PREVIOUS)
    with committed_transaction() as c:
        _insert_full_baseline_dataset(c)
        ids = _people(c)
    for student in ids["students"]:
        for index, topic in enumerate(ids["topics"]):
            for n in range(index + 2):
                graded_attempt(disposable_engine, student, ids["course"], topic, correct=n % 2 == 0)
    return ids


def test_upgrade_backfills_every_graded_attempt_exactly_once_and_downgrades(
    at_0006_with_m7, disposable_engine, alembic
):
    ids = at_0006_with_m7
    with disposable_engine.connect() as c:
        graded = c.execute(text("SELECT count(*) FROM attempts WHERE quiz_item_id IS NOT NULL")).scalar_one()
        legacy = c.execute(text("SELECT count(*) FROM attempts WHERE quiz_item_id IS NULL")).scalar_one()
        review_before = c.execute(text("SELECT next_review_at FROM mastery_scores")).scalars().all()
    assert graded == 10 and legacy >= 1
    alembic("upgrade", REVISION)
    with disposable_engine.connect() as c:
        rows = c.execute(
            text("SELECT o.event_id, o.origin, o.status, o.topic_seq = a.stats_attempts AS same_seq FROM mastery_outbox o "
                 "JOIN attempts a ON a.id = o.event_id")  # fmt: skip
        ).all()
        assert len(rows) == graded and {r.origin for r in rows} == {"backfill"} and {r.status for r in rows} == {"pending"}
        assert all(r.same_seq for r in rows)
        streams = c.execute(text("SELECT user_id, course_id, state FROM mastery_streams")).all()
        assert {(s.user_id, s.course_id) for s in streams} == {(u, ids["course"]) for u in ids["students"]}
        legacy_score = c.execute(
            text("SELECT course_id, protocol, received_at, next_review_at FROM mastery_scores")
        ).one()
        assert legacy_score.course_id == ids["course"] and legacy_score.protocol is None
        assert legacy_score.received_at is None
        if review_before[0] is not None:
            assert legacy_score.next_review_at == review_before[0].replace(tzinfo=timezone.utc)
        assert c.execute(text("SELECT status FROM mastery_model_state")).scalar_one() == "unknown"
    alembic("upgrade", REVISION)  # no-op
    alembic("downgrade", PREVIOUS)  # only pending, regenerable events: allowed
    assert not NEW_TABLES & _tables(disposable_engine)
    alembic("upgrade", REVISION)
    with disposable_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM mastery_outbox")).scalar_one() == graded


@pytest.mark.parametrize("tamper", ["stats_counter", "gap", "repeat", "cumulative"])
def test_preflight_refuses_inconsistent_m7_sequences_before_any_change(
    at_0006_with_m7, disposable_engine, alembic, alembic_env, committed_transaction, tamper
):
    ids = at_0006_with_m7
    student, course, topic = ids["students"][0], ids["course"], ids["topics"][0]
    if tamper == "stats_counter":
        with committed_transaction() as c:
            c.execute(text("UPDATE topic_practice_stats SET attempts = attempts + 1 WHERE user_id = :u AND topic_id = :t"),
                      {"u": student, "t": topic})  # fmt: skip
    else:
        forged = {"gap": (4, 2), "repeat": (2, 1), "cumulative": (3, 3)}[tamper]
        graded_attempt(disposable_engine, student, course, topic, correct=True, forged=forged)
        with committed_transaction() as c:  # keep the statistics consistent with the count, so only the sequence is wrong
            c.execute(text("UPDATE topic_practice_stats SET attempts = 3, correct = 2 WHERE user_id = :u AND topic_id = :t"),
                      {"u": student, "t": topic})  # fmt: skip
    output = _failed(run_alembic("upgrade", REVISION, env=alembic_env))
    assert "Migration 0007 stopped before any change" in output
    assert _current(alembic).split()[0] == PREVIOUS
    assert not NEW_TABLES & _tables(disposable_engine)


def test_upgrade_waits_for_an_attempt_in_flight_and_loses_nothing(
    at_0006_with_m7, disposable_engine, alembic_env, alembic
):
    ids = at_0006_with_m7
    connection = disposable_engine.connect()
    transaction = connection.begin()
    try:
        graded_attempt(disposable_engine, ids["students"][1], ids["course"], ids["topics"][1], connection=connection)
        process = subprocess.Popen(
            [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", REVISION],
            cwd=CORE_API_DIR, env={"PATH": os.environ.get("PATH", ""), **alembic_env},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        time.sleep(3)
        assert process.poll() is None, "the migration must wait for the open attempt transaction"
        transaction.commit()
        _, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
    with disposable_engine.connect() as c:
        graded = c.execute(text("SELECT count(*) FROM attempts WHERE quiz_item_id IS NOT NULL")).scalar_one()
        assert c.execute(text("SELECT count(*) FROM mastery_outbox")).scalar_one() == graded == 11


def test_offline_sql_carries_the_guards_locks_and_triggers(scratch6, alembic):  # noqa: F811
    upgrade = alembic("upgrade", f"{PREVIOUS}:{REVISION}", "--sql").stdout
    assert "migration_0007_preflight" in upgrade and "LOCK TABLE topic_practice_stats, attempts" in upgrade
    assert "CREATE TRIGGER attempts_outbox_enqueue" in upgrade and "migration_0007_backfill_check" in upgrade
    assert upgrade.index("migration_0007_preflight") < upgrade.index("CREATE TABLE mastery_streams")
    downgrade = alembic("downgrade", f"{REVISION}:{PREVIOUS}", "--sql").stdout
    assert "migration_0007_downgrade_guard" in downgrade


_M8_STATE = {
    "study_event": "INSERT INTO study_events (user_id, course_id, client_event_id, fingerprint, event_type, occurred_at, "
    "started_at, ended_at, surface) VALUES (:u, :c, gen_random_uuid(), repeat('a', 64), 'focus_interval', now(), "
    "now() - interval '5 minutes', now(), 'quiz')",
    "release": "INSERT INTO analytics_weekly_releases (course_id, week_start, policy, min_group_size, payload, "
    "payload_sha256) VALUES (:c, DATE '2026-11-02', 'analytics_release/1', 5, '{}'::jsonb, repeat('a', 64))",
    "prediction": "UPDATE mastery_scores SET protocol = 'studygraph.mastery/1', model_name = 'm', model_version = '1', "
    "model_epoch = gen_random_uuid(), pseudonym_key_id = repeat('a', 16), through_seq = 1, attempts_seen = 1, "
    "attempts = 1, mastery = 0.5, batch_id = gen_random_uuid(), received_at = now()",
    "delivery": "UPDATE mastery_streams SET state = 'blocked', last_error_code = 'x'",
}


@pytest.mark.parametrize("state", sorted(_M8_STATE))
def test_downgrade_refuses_while_m8_state_exists(
    at_0006_with_m7, disposable_engine, alembic, alembic_env, committed_transaction, state
):
    ids = at_0006_with_m7
    alembic("upgrade", REVISION)
    with committed_transaction() as c:
        c.execute(text(_M8_STATE[state]), {"u": ids["students"][0], "c": ids["course"]})
    output = _failed(run_alembic("downgrade", PREVIOUS, env=alembic_env))
    assert "Downgrade below 0007 refused before any change" in output
    assert _current(alembic).split()[0] == REVISION
    with committed_transaction() as c:  # leave the scratch database removable
        c.execute(text("DELETE FROM study_events"))
        c.execute(text("DELETE FROM analytics_weekly_releases"))
        c.execute(text("UPDATE mastery_streams SET state = 'ready', last_error_code = NULL"))
        c.execute(text("DELETE FROM mastery_scores WHERE protocol IS NOT NULL"))


# --- rules at head ------------------------------------------------------------------------------------------


@pytest.fixture
def m8(world):
    professor = world.user("professor")
    course, other = world.course(professor), world.course(professor)
    topic, other_topic = world.topic(course, "T"), world.topic(other, "U")
    student = world.user("student")
    world.enroll(student, course)
    return {"world": world, "course": course, "other": other, "topic": topic, "other_topic": other_topic,
            "student": student.id, "engine": world.engine}  # fmt: skip


def _refused(engine, sql: str, params: dict, match: str):
    with pytest.raises(DBAPIError, match=match):
        with engine.begin() as c:
            c.execute(text(sql), params)


def test_graded_attempts_must_continue_their_counters(m8):
    e, u, course, topic = m8["engine"], m8["student"], m8["course"], m8["topic"]
    first = graded_attempt(e, u, course, topic)
    for forged, match in (((1, 1), "attempts_topic_sequence_idx|topic statistics"),
                          ((3, 2), "topic statistics"), ((2, 2), "topic statistics")):  # fmt: skip
        with pytest.raises(DBAPIError, match=match):
            graded_attempt(e, u, course, topic, forged=forged)
    # the statistics advanced by two (an attempt skipped) cannot be used either
    with pytest.raises(DBAPIError, match="continue the sequence"):
        with e.begin() as c:
            c.execute(text("UPDATE topic_practice_stats SET attempts = 3, correct = 2 WHERE user_id = :u"), {"u": u})
            graded_attempt(e, u, course, topic, forged=(3, 2), connection=c)
    second = graded_attempt(e, u, course, topic, correct=False)
    with e.connect() as c:
        seqs = c.execute(text("SELECT event_id, topic_seq, origin FROM mastery_outbox WHERE user_id = :u ORDER BY topic_seq"),
                         {"u": u}).all()  # fmt: skip
    assert [(r.event_id, r.topic_seq, r.origin) for r in seqs] == [(first, 1, "trigger"), (second, 2, "trigger")]
    legacy_attempt(e, u, course, topic)  # legacy writers are not affected
    with e.begin() as c:  # an administrator may still correct statistics: there is no trigger on them
        c.execute(text("UPDATE topic_practice_stats SET updated_at = now() WHERE user_id = :u"), {"u": u})


def test_the_outbox_row_is_part_of_the_attempt_transaction(m8):
    e, u, course, topic = m8["engine"], m8["student"], m8["course"], m8["topic"]
    with e.connect() as c:
        transaction = c.begin()
        graded_attempt(e, u, course, topic, connection=c)
        assert c.execute(text("SELECT count(*) FROM mastery_outbox WHERE user_id = :u"), {"u": u}).scalar_one() == 1
        transaction.rollback()
    with e.connect() as c:
        assert c.execute(text("SELECT count(*) FROM mastery_outbox WHERE user_id = :u"), {"u": u}).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM mastery_streams WHERE user_id = :u"), {"u": u}).scalar_one() == 0


def test_outbox_identity_and_batch_membership_are_immutable(m8):
    e, u, course, topic = m8["engine"], m8["student"], m8["course"], m8["topic"]
    event = graded_attempt(e, u, course, topic)
    p = {"e": event}
    _refused(e, "UPDATE mastery_outbox SET topic_seq = 5 WHERE event_id = :e", p, "identity|mastery_outbox_attempt_fkey")
    _refused(e, "UPDATE mastery_outbox SET status = 'delivered', delivered_at = now(), outcome = 'applied' "
                "WHERE event_id = :e", p, "lifecycle|transition")  # fmt: skip
    with e.begin() as c:
        c.execute(text("UPDATE mastery_outbox SET status = 'batched', batch_id = gen_random_uuid(), batched_at = now() "
                       "WHERE event_id = :e"), p)  # fmt: skip
    _refused(e, "UPDATE mastery_outbox SET batch_id = gen_random_uuid() WHERE event_id = :e", p, "membership")
    _refused(e, "UPDATE mastery_outbox SET status = 'pending', batch_id = NULL, batched_at = NULL WHERE event_id = :e",
             p, "membership|transition")  # fmt: skip
    with e.begin() as c:
        c.execute(text("UPDATE mastery_outbox SET status = 'delivered', delivered_at = now(), outcome = 'duplicate' "
                       "WHERE event_id = :e"), p)  # fmt: skip
    _refused(e, "UPDATE mastery_outbox SET status = 'failed' WHERE event_id = :e", p, "final")


def test_prediction_provenance_and_range_are_enforced(m8):
    e, u, course, topic = m8["engine"], m8["student"], m8["course"], m8["topic"]
    base = ("INSERT INTO mastery_scores (user_id, topic_id, course_id, mastery, attempts, protocol, model_name, "
            "model_version, model_epoch, pseudonym_key_id, through_seq, attempts_seen, batch_id, received_at) VALUES "
            "(:u, :t, :c, CAST(:m AS double precision), :n, 'studygraph.mastery/1', 'm', '1', gen_random_uuid(), "
            "repeat('a', 16), :n, :seen, gen_random_uuid(), now())")  # fmt: skip
    for mastery, seen in (("NaN", 1), ("Infinity", 1), ("1.5", 1), ("-0.1", 1), ("0.5", 2)):
        _refused(e, base, {"u": u, "t": topic, "c": course, "m": mastery, "n": 1, "seen": seen}, "provenance")
    _refused(e, "INSERT INTO mastery_scores (user_id, topic_id, course_id, mastery, attempts, protocol) "
                "VALUES (:u, :t, :c, 0.5, 1, 'studygraph.mastery/1')", {"u": u, "t": topic, "c": course}, "provenance")
    _refused(e, "INSERT INTO mastery_scores (user_id, topic_id, course_id, mastery, attempts) VALUES (:u, :t, :c, 0.5, 1)",
             {"u": u, "t": topic, "c": m8["other"]}, "mastery_scores_topic_course_fkey")  # fmt: skip
    with e.begin() as c:  # a legacy row (no provenance) is still accepted, even out of range: it is never shown
        c.execute(text("INSERT INTO mastery_scores (user_id, topic_id, course_id, mastery, attempts) "
                       "VALUES (:u, :t, :c, 7, 1)"), {"u": u, "t": topic, "c": course})  # fmt: skip


def test_releases_are_immutable_and_only_for_completed_weeks(m8):
    e, course = m8["engine"], m8["course"]
    insert = ("INSERT INTO analytics_weekly_releases (course_id, week_start, policy, min_group_size, payload, "
              "payload_sha256, created_at) VALUES (:c, :w, 'analytics_release/1', :k, '{}'::jsonb, repeat('a', 64), :at)")
    now = datetime.now(timezone.utc)
    _refused(e, insert, {"c": course, "w": date(2026, 11, 3), "k": 5, "at": now}, "week_check")  # a Tuesday
    _refused(e, insert, {"c": course, "w": date(2026, 11, 2), "k": 4, "at": now}, "payload_check")
    _refused(e, insert, {"c": course, "w": date(2026, 11, 2), "k": 5,
                         "at": datetime(2026, 11, 8, 23, tzinfo=timezone.utc)}, "week_check")  # before the week ended
    with e.begin() as c:
        c.execute(text(insert), {"c": course, "w": date(2026, 11, 2), "k": 5, "at": now})
    _refused(e, "UPDATE analytics_weekly_releases SET payload = '{\"x\": 1}'::jsonb WHERE course_id = :c",
             {"c": course}, "immutable")  # fmt: skip


def test_study_event_rows_are_shape_checked(m8):
    e, u, course = m8["engine"], m8["student"], m8["course"]
    base = ("INSERT INTO study_events (user_id, course_id, client_event_id, fingerprint, event_type, occurred_at, "
            "started_at, ended_at, surface, recommendation_id, recommendation_type) VALUES (:u, :c, gen_random_uuid(), "
            "repeat('a', 64), :kind, :occ, :start, :end, :surface, :rec, :rec_type)")  # fmt: skip
    now = datetime.now(timezone.utc)
    from datetime import timedelta

    good = {"u": u, "c": course, "kind": "focus_interval", "occ": now, "start": now - timedelta(minutes=5), "end": now,
            "surface": "quiz", "rec": None, "rec_type": None}  # fmt: skip
    for change in ({"start": now - timedelta(hours=2)}, {"surface": "web"}, {"occ": now - timedelta(minutes=1)},
                   {"rec": "abc"}, {"kind": "document_viewed"}, {"occ": now - timedelta(days=3),
                   "start": now - timedelta(days=3, minutes=5), "end": now - timedelta(days=3)}):  # fmt: skip
        _refused(e, base, {**good, **change}, "study_events_")
    with e.begin() as c:
        c.execute(text(base), good)
    _refused(e, "INSERT INTO study_events (user_id, course_id, client_event_id, fingerprint, event_type, occurred_at, "
                "topic_id, recommendation_id, recommendation_type) VALUES (:u, :c, gen_random_uuid(), repeat('a', 64), "
                "'recommendation_shown', now(), :t, repeat('a', 16), 'review_topic')",
             {"u": u, "c": course, "t": m8["other_topic"]}, "study_events_topic_fkey")  # fmt: skip


def test_stream_lifecycle_check(m8):
    e, u, course, topic = m8["engine"], m8["student"], m8["course"], m8["topic"]
    graded_attempt(e, u, course, topic)
    p = {"u": u}
    _refused(e, "UPDATE mastery_streams SET state = 'delivering' WHERE user_id = :u", p, "lease_check")
    _refused(e, "UPDATE mastery_streams SET state = 'blocked' WHERE user_id = :u", p, "error_check")
    _refused(e, "UPDATE mastery_streams SET batch_id = gen_random_uuid() WHERE user_id = :u", p, "batch_check")
    assert json.dumps({"ok": True})
