"""WP-3: maintenance query shape (F11, A01) on a real database.

F11: the orphan scan of file reconciliation looks its candidates up in one query (plus the one
recheck after the in-flight check), not once per candidate.

A01: study-event retention and stale quiz-lease recovery select a bounded, ordered victim set
exactly once (MATERIALIZED CTE, `id` as tie-breaker) and skip rows another worker has locked,
the same shape as harvest history cleanup (tests/test_harvest_state.py).
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.dashboard import study_events
from app.ingestion.reconcile import reconcile
from app.quiz import generation
from app.storage import LocalFileStorage
from tests.api_support import bind_storage
from tests.pdf_samples import make_pdf
from tests.worker_support import add_stored_document, store_file

LOOKUP = "SELECT storage_key FROM documents WHERE storage_key = ANY"


@contextmanager
def counting(engine, needle: str):
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if needle in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --- F11: reconciliation orphan scan --------------------------------------------------------------------------


@pytest.fixture
def store(world, tmp_path) -> LocalFileStorage:
    bind_storage(world.engine, tmp_path / "store")
    return LocalFileStorage(tmp_path / "store")


def test_orphan_scan_looks_candidates_up_once_and_rechecks_once(world, store):
    owner = world.user("professor")
    course = world.course(owner)
    referenced = {add_stored_document(world.engine, world, store, course, owner, make_pdf(f"r{n}")).key
                  for n in range(5)}  # fmt: skip
    orphans = {store_file(store, uuid.uuid4(), uuid.uuid4(), b"%PDF-1.4 orphan " + bytes([n])) for n in range(4)}
    settings = Settings(reconcile_orphan_grace_seconds=0, reconcile_batch_size=100)

    with counting(world.engine, LOOKUP) as lookups:
        report = reconcile(sessionmaker(bind=world.engine), store, settings)

    assert report.errors == [] and report.skipped is None
    assert sorted(report.orphans) == sorted(orphans)  # same findings, each once
    assert not referenced & set(report.orphans)
    assert len(lookups) == 2  # 9 candidates: one initial lookup + one recheck (was 9 + 1)


def test_orphan_scan_of_only_referenced_files_needs_one_lookup(world, store):
    owner = world.user("professor")
    course = world.course(owner)
    for n in range(6):
        add_stored_document(world.engine, world, store, course, owner, make_pdf(f"k{n}"))
    settings = Settings(reconcile_orphan_grace_seconds=0, reconcile_batch_size=100)

    with counting(world.engine, LOOKUP) as lookups:
        report = reconcile(sessionmaker(bind=world.engine), store, settings)

    assert report.errors == [] and report.orphans == []
    assert len(lookups) == 1  # nothing left to recheck (was 6)


# --- A01: study-event retention -------------------------------------------------------------------------------


@pytest.fixture
def learner(world):
    professor = world.user("professor")
    course = world.course(professor)
    student = world.user("student")
    world.enroll(student, course)
    return student, course


def _event(world, student, course, event_id, received_at):
    world.execute(
        "INSERT INTO study_events (id, user_id, course_id, client_event_id, fingerprint, event_type, occurred_at, "
        "started_at, ended_at, surface, received_at) VALUES (:id, :u, :c, gen_random_uuid(), repeat('a', 64), "
        "'focus_interval', :t, :t - interval '5 minutes', :t, 'quiz', :t)",
        id=event_id, u=student.id, c=course, t=received_at,
    )  # fmt: skip


def _events(world, course) -> set[uuid.UUID]:
    return {row["id"] for row in world.rows("SELECT id FROM study_events WHERE course_id = :c", c=course)}


def test_event_retention_deletes_only_the_ordered_batch(world, learner):
    student, course = learner
    settings = Settings(study_events_retention_days=180, study_events_cleanup_batch=2)
    now = world.scalar("SELECT now()")
    lower, higher, oldest = sorted(uuid.uuid4() for _ in range(3))
    recent = uuid.uuid4()
    _event(world, student, course, oldest, now - timedelta(days=300))
    world.execute("ANALYZE study_events")  # one-row statistics invite a nested-loop rescan of a locking subquery
    tied = now - timedelta(days=200)
    _event(world, student, course, higher, tied)
    _event(world, student, course, lower, tied)
    _event(world, student, course, recent, now - timedelta(days=1))
    factory = sessionmaker(bind=world.engine)

    assert study_events.cleanup(factory, settings) == 2
    assert _events(world, course) == {higher, recent}  # oldest first, then the lower id of the tie
    assert study_events.cleanup(factory, settings) == 1
    assert _events(world, course) == {recent}
    assert study_events.cleanup(factory, settings) == 0
    assert _events(world, course) == {recent}


def _hold_before_commit(factory, reached: threading.Event, release: threading.Event) -> None:
    @event.listens_for(factory, "before_commit")
    def hold(session):
        reached.set()  # the statement ran and this transaction still holds its row locks
        assert release.wait(timeout=10), "the second worker did not finish while the first held its locks"


def test_concurrent_event_retention_skips_locked_victims(world, learner):
    student, course = learner
    settings = Settings(study_events_retention_days=180, study_events_cleanup_batch=2)
    old = world.scalar("SELECT now() - interval '200 days'")
    ids = sorted(uuid.uuid4() for _ in range(5))
    for event_id in ids:
        _event(world, student, course, event_id, old)
    first_factory, reached, release = sessionmaker(bind=world.engine), threading.Event(), threading.Event()
    _hold_before_commit(first_factory, reached, release)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(study_events.cleanup, first_factory, settings)
        try:
            assert reached.wait(timeout=10)
            assert study_events.cleanup(sessionmaker(bind=world.engine), settings) == 2
            assert _events(world, course) == {ids[0], ids[1], ids[4]}  # only the second worker's batch is gone
        finally:
            release.set()
        assert first.result() == 2
    assert _events(world, course) == {ids[4]}


# --- A01: stale quiz-lease recovery ---------------------------------------------------------------------------


def _generating(world, course, session_id, lease_expires_at):
    student = world.user("student")  # one generating session per user (quiz_sessions_one_generating_idx)
    world.execute(
        "INSERT INTO quiz_sessions (id, user_id, course_id, status, idempotency_key, request_fingerprint, "
        "requested_topic_ids, requested_count, difficulty, scope, attempt_token, lease_expires_at) VALUES "
        "(:id, :u, :c, 'generating', gen_random_uuid(), repeat('f', 64), ARRAY[gen_random_uuid()], 1, 'easy', "
        "'class_only', gen_random_uuid(), :lease)",
        id=session_id, u=student.id, c=course, lease=lease_expires_at,
    )  # fmt: skip


def _sessions(world, course) -> dict[uuid.UUID, tuple]:
    rows = world.rows("SELECT id, status, failure_code, attempt_token, lease_expires_at, completed_at "
                      "FROM quiz_sessions WHERE course_id = :c", c=course)  # fmt: skip
    return {r["id"]: (r["status"], r["failure_code"], r["attempt_token"] is None, r["lease_expires_at"] is None,
                      r["completed_at"] is not None) for r in rows}  # fmt: skip


FAILED = ("failed", "lease_expired", True, True, True)


def test_lease_recovery_fails_only_the_ordered_batch(world):
    course = world.course(world.user("professor"))
    now = world.scalar("SELECT now()")
    lower, higher, oldest = sorted(uuid.uuid4() for _ in range(3))
    live = uuid.uuid4()
    _generating(world, course, oldest, now - timedelta(minutes=30))
    world.execute("ANALYZE quiz_sessions")
    tied = now - timedelta(minutes=10)
    _generating(world, course, higher, tied)
    _generating(world, course, lower, tied)
    _generating(world, course, live, now + timedelta(minutes=10))
    factory = sessionmaker(bind=world.engine)

    assert sorted(generation.recover_expired(factory, batch=2)) == sorted([oldest, lower])
    state = _sessions(world, course)
    assert state[oldest] == state[lower] == FAILED and state[higher][0] == state[live][0] == "generating"
    assert generation.recover_expired(factory, batch=2) == [higher]
    assert generation.recover_expired(factory, batch=2) == []
    assert _sessions(world, course)[live][0] == "generating"  # an unexpired lease is never touched


def test_concurrent_lease_recovery_skips_locked_sessions(world):
    course = world.course(world.user("professor"))
    expired = world.scalar("SELECT now() - interval '10 minutes'")
    ids = sorted(uuid.uuid4() for _ in range(5))
    for session_id in ids:
        _generating(world, course, session_id, expired)
    first_factory, reached, release = sessionmaker(bind=world.engine), threading.Event(), threading.Event()
    _hold_before_commit(first_factory, reached, release)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(generation.recover_expired, first_factory, 2)
        try:
            assert reached.wait(timeout=10)
            assert sorted(generation.recover_expired(sessionmaker(bind=world.engine), batch=2)) == ids[2:4]
        finally:
            release.set()
        assert sorted(first.result()) == ids[:2]
    state = _sessions(world, course)
    assert [state[i][0] for i in ids] == ["failed", "failed", "failed", "failed", "generating"]
