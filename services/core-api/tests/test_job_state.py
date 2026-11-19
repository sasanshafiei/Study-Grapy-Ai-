"""The parse-job state machine on real PostgreSQL: claim, fencing, completion, failure, recovery, retry.

Concurrency is produced with real concurrent sessions (threads, each with its
own connection) and barriers, never simulated. The engine is the verified
disposable one (every connection re-checks the database identity).
"""

from __future__ import annotations

import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.ingestion import jobs
from app.ingestion.jobs import (
    CompletionStatus,
    CompletionUnconfirmed,
    Failure,
    FailureStatus,
    PUBLIC_MESSAGES,
    RetryRefused,
)
from app.ingestion.normalize import Section
from tests.pdf_samples import make_pdf
from tests.worker_support import add_stored_document, document_row, index_jobs, job_row, sections_of

SETTINGS = Settings(retry_backoff_base_seconds=30, retry_backoff_max_seconds=600)
SECTIONS = [Section(0, 1, "Page one"), Section(1, 3, "Page three → H₂O")]


class Fixed(random.Random):
    def uniform(self, a, b):  # no jitter: deterministic backoff
        return 1.0


@pytest.fixture
def factory(world) -> sessionmaker[Session]:
    return sessionmaker(bind=world.engine, autoflush=False)


@pytest.fixture
def owner(world):
    return world.user("professor")


@pytest.fixture
def course(world, owner):
    return world.course(owner)


@pytest.fixture
def make_doc(world, owner, course, tmp_path):
    from app.storage import LocalFileStorage

    storage = LocalFileStorage(tmp_path / "store")
    counter = iter(range(10_000))

    def make(**kwargs):
        return add_stored_document(world.engine, world, storage, course, owner, make_pdf(f"doc {next(counter)}"), **kwargs)

    return make


def _claim(factory, job_id, lease=300):
    with factory() as session:
        claim = jobs.claim_parse(session, job_id, lease)
        session.commit()
    return claim


def _expire_lease(world, job_id):
    world.execute(
        "UPDATE ingestion_jobs SET lease_expires_at = started_at + interval '1 microsecond' WHERE id = :j", j=job_id
    )


# --- claim ----------------------------------------------------------------------------------------


def test_claim_starts_an_attempt_with_a_fresh_token_and_lease(factory, make_doc, world):
    doc = make_doc()
    claim = _claim(factory, doc.job_id, lease=300)
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_count"], row["attempt_token"]) == ("running", 1, claim.token)
    assert (row["lease_expires_at"] - row["started_at"]).total_seconds() == 300
    assert claim.storage_key == doc.key and claim.document_status == "pending"
    assert _claim(factory, doc.job_id) is None  # a duplicate delivery claims nothing


def test_only_one_of_many_concurrent_claims_wins(factory, make_doc, world):
    doc = make_doc()
    barrier = threading.Barrier(8, timeout=30)

    def claim():
        barrier.wait()
        return _claim(factory, doc.job_id)

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: claim(), range(8)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert job_row(world.engine, doc.job_id)["attempt_count"] == 1


@pytest.mark.parametrize(
    "setup",
    [
        "UPDATE ingestion_jobs SET available_at = now() + interval '1 hour' WHERE id = :j",
        "UPDATE ingestion_jobs SET kind = 'index' WHERE id = :j",
        "UPDATE ingestion_jobs SET attempt_count = 3, started_at = now() WHERE id = :j",
        "UPDATE ingestion_jobs SET status = 'failed', finished_at = now() WHERE id = :j",
    ],
    ids=["backoff-not-over", "index-job", "no-attempts-left", "not-queued"],
)
def test_jobs_that_are_not_claimable(factory, make_doc, world, setup):
    doc = make_doc()
    world.execute(setup, j=doc.job_id)
    assert _claim(factory, doc.job_id) is None
    assert _claim(factory, uuid.uuid4()) is None  # unknown job


# --- completion and fencing -----------------------------------------------------------------------


def test_completion_writes_sections_page_count_success_and_index_handoff_atomically(factory, make_doc, world):
    doc = make_doc()
    claim = _claim(factory, doc.job_id)
    assert jobs.complete_parse(factory, claim, 3, SECTIONS) is CompletionStatus.COMPLETED

    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_token"], row["lease_expires_at"]) == ("succeeded", claim.token, None)
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["page_count"], document["error_message"]) == ("pending", 3, None)
    sections = sections_of(world.engine, doc.document_id)
    assert [(s["ordinal"], s["locator"], s["text"]) for s in sections] == [
        (0, {"page": 1}, "Page one"),
        (1, {"page": 3}, "Page three → H₂O"),
    ]
    [index] = index_jobs(world.engine, doc.document_id)
    assert (index["status"], index["attempt_count"], index["attempt_token"]) == ("queued", 0, None)


def test_a_stale_attempt_writes_nothing(factory, make_doc, world):
    """A, frozen past its lease, is recovered; B claims and completes; A resumes: fenced off."""
    doc = make_doc()
    stale = _claim(factory, doc.job_id)
    _expire_lease(world, doc.job_id)
    jobs.recover_expired_leases(factory, Settings(retry_backoff_base_seconds=0, retry_backoff_max_seconds=0))
    fresh = _claim(factory, doc.job_id)
    assert fresh.token != stale.token and fresh.attempt == 2

    assert jobs.complete_parse(factory, fresh, 3, SECTIONS) is CompletionStatus.COMPLETED
    before = sections_of(world.engine, doc.document_id)

    assert jobs.complete_parse(factory, stale, 9, [Section(0, 9, "stale result")]) is CompletionStatus.STALE
    assert jobs.record_failure(factory, stale, Failure("invalid_pdf", "x", permanent=True), SETTINGS) is FailureStatus.STALE
    assert jobs.record_failure(factory, stale, Failure("io", "x", permanent=False), SETTINGS) is FailureStatus.STALE
    assert sections_of(world.engine, doc.document_id) == before  # same rows, same ids, same times
    assert job_row(world.engine, doc.job_id)["attempt_token"] == fresh.token
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["page_count"], document["error_message"]) == ("pending", 3, None)
    assert len(index_jobs(world.engine, doc.document_id)) == 1


def test_stale_attempt_cannot_fail_a_newer_running_attempt(factory, make_doc, world):
    doc = make_doc()
    stale = _claim(factory, doc.job_id)
    _expire_lease(world, doc.job_id)
    jobs.recover_expired_leases(factory, Settings(retry_backoff_base_seconds=0, retry_backoff_max_seconds=0))
    fresh = _claim(factory, doc.job_id)
    assert jobs.record_failure(factory, stale, Failure("invalid_pdf", "x", permanent=True), SETTINGS) is FailureStatus.STALE
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_token"]) == ("running", fresh.token)
    assert document_row(world.engine, doc.document_id)["status"] == "pending"


def test_duplicate_completion_after_success_is_harmless(factory, make_doc, world):
    doc = make_doc()
    claim = _claim(factory, doc.job_id)
    assert jobs.complete_parse(factory, claim, 3, SECTIONS) is CompletionStatus.COMPLETED
    before = sections_of(world.engine, doc.document_id)
    assert jobs.complete_parse(factory, claim, 3, SECTIONS) is CompletionStatus.STALE
    assert sections_of(world.engine, doc.document_id) == before
    assert len(index_jobs(world.engine, doc.document_id)) == 1


@pytest.mark.parametrize("status", ["indexed", "stale", "failed"])
def test_completion_never_overwrites_a_document_that_is_no_longer_pending(factory, make_doc, world, status):
    doc = make_doc()
    claim = _claim(factory, doc.job_id)
    world.execute("UPDATE documents SET status = :s, error_message = 'kept' WHERE id = :d", s=status, d=doc.document_id)
    assert jobs.complete_parse(factory, claim, 3, SECTIONS) is CompletionStatus.DOCUMENT_NOT_PENDING
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["error_message"], document["page_count"]) == (status, "kept", None)
    assert sections_of(world.engine, doc.document_id) == [] and index_jobs(world.engine, doc.document_id) == []
    assert job_row(world.engine, doc.job_id)["status"] == "failed"


def test_an_existing_active_index_job_is_a_conflict_not_a_handoff(factory, make_doc, world):
    doc = make_doc()
    claim = _claim(factory, doc.job_id)
    world.execute("INSERT INTO ingestion_jobs (document_id, kind) VALUES (:d, 'index')", d=doc.document_id)
    assert jobs.complete_parse(factory, claim, 3, SECTIONS) is CompletionStatus.INDEX_CONFLICT
    assert sections_of(world.engine, doc.document_id) == []
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_token"]) == ("running", claim.token)  # nothing written; the caller fails it
    assert jobs.record_failure(
        factory, claim, Failure("index_handoff_conflict", "x", permanent=True), SETTINGS
    ) is FailureStatus.FAILED
    assert document_row(world.engine, doc.document_id)["error_message"] == PUBLIC_MESSAGES["index_handoff_conflict"]


def test_a_failure_inside_completion_leaves_no_partial_section_set(factory, make_doc, world):
    doc = make_doc()
    claim = _claim(factory, doc.job_id)
    name = f"sg_test_{uuid.uuid4().hex[:8]}"
    world.execute(
        f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.ordinal = 1 THEN RAISE EXCEPTION 'simulated failure on the second section'; END IF; RETURN NEW; END $$"
    )
    world.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON document_sections FOR EACH ROW EXECUTE FUNCTION {name}()")
    try:
        with pytest.raises(Exception, match="simulated failure"):
            jobs.complete_parse(factory, claim, 3, SECTIONS)
    finally:
        world.execute(f"DROP TRIGGER {name} ON document_sections")
        world.execute(f"DROP FUNCTION {name}()")
    assert sections_of(world.engine, doc.document_id) == []
    assert document_row(world.engine, doc.document_id)["page_count"] is None
    assert job_row(world.engine, doc.job_id)["status"] == "running" and index_jobs(world.engine, doc.document_id) == []
    # The same attempt can still complete (e.g. after a transient error).
    assert jobs.complete_parse(factory, claim, 3, SECTIONS) is CompletionStatus.COMPLETED


def test_commit_that_succeeded_but_raised_is_recognized(world, make_doc):
    doc = make_doc()

    class CommitThenRaise(Session):
        def commit(self):
            super().commit()
            raise ConnectionError("simulated: connection lost after COMMIT")

    plain = sessionmaker(bind=world.engine)
    claim = _claim(plain, doc.job_id)
    raising = sessionmaker(bind=world.engine, class_=CommitThenRaise)
    assert jobs.complete_parse(raising, claim, 3, SECTIONS) is CompletionStatus.COMPLETED
    assert job_row(world.engine, doc.job_id)["status"] == "succeeded"
    assert len(sections_of(world.engine, doc.document_id)) == 2


def test_commit_that_did_not_happen_is_unconfirmed_and_recovered_idempotently(world, make_doc, factory):
    doc = make_doc()

    class LostCommit(Session):
        def commit(self):
            self.rollback()
            raise ConnectionError("simulated: COMMIT never reached the server")

    claim = _claim(factory, doc.job_id)
    with pytest.raises(CompletionUnconfirmed):
        jobs.complete_parse(sessionmaker(bind=world.engine, class_=LostCommit), claim, 3, SECTIONS)
    assert job_row(world.engine, doc.job_id)["status"] == "running" and sections_of(world.engine, doc.document_id) == []
    # The lease expires; recovery requeues; the next attempt produces the same result.
    _expire_lease(world, doc.job_id)
    assert jobs.recover_expired_leases(factory, Settings(retry_backoff_base_seconds=0, retry_backoff_max_seconds=0)).requeued
    retry = _claim(factory, doc.job_id)
    assert jobs.complete_parse(factory, retry, 3, SECTIONS) is CompletionStatus.COMPLETED
    assert [s["text"] for s in sections_of(world.engine, doc.document_id)] == [s.text for s in SECTIONS]


def test_completion_and_recovery_racing_on_one_job_end_consistently(factory, make_doc, world):
    """Whichever wins, the job ends in one consistent state, without deadlock, many times over."""
    settings = Settings(retry_backoff_base_seconds=0, retry_backoff_max_seconds=0)
    outcomes = set()
    for _ in range(15):
        doc = make_doc()
        claim = _claim(factory, doc.job_id)
        _expire_lease(world, doc.job_id)
        barrier = threading.Barrier(2, timeout=30)

        def complete():
            barrier.wait()
            return jobs.complete_parse(factory, claim, 3, SECTIONS)

        def recover():
            barrier.wait()
            return jobs.recover_expired_leases(factory, settings)

        with ThreadPoolExecutor(2) as pool:
            completing, recovering = pool.submit(complete), pool.submit(recover)
            completed, recovered = completing.result(30), recovering.result(30)
        row = job_row(world.engine, doc.job_id)
        sections = sections_of(world.engine, doc.document_id)
        if completed is CompletionStatus.COMPLETED:
            assert row["status"] == "succeeded" and len(sections) == 2 and doc.job_id not in recovered.requeued
        else:
            assert completed is CompletionStatus.STALE
            assert row["status"] == "queued" and sections == [] and doc.job_id in recovered.requeued
        outcomes.add(completed)
    assert outcomes  # at least one outcome observed; both are valid


# --- failure, retry and recovery ---------------------------------------------------------------


def test_transient_failure_requeues_with_backoff_then_exhausts(factory, make_doc, world):
    doc = make_doc(max_attempts=2)
    first = _claim(factory, doc.job_id)
    assert jobs.record_failure(factory, first, Failure("storage_error", "EIO", permanent=False), SETTINGS, Fixed()) is FailureStatus.REQUEUED
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_token"], row["lease_expires_at"], row["last_dispatched_at"]) == ("queued", None, None, None)
    assert 29 <= (row["available_at"] - row["updated_at"]).total_seconds() <= 31
    assert row["error_message"] == "storage_error: EIO"
    assert _claim(factory, doc.job_id) is None  # backoff not over

    world.execute("UPDATE ingestion_jobs SET available_at = now() WHERE id = :j", j=doc.job_id)
    second = _claim(factory, doc.job_id)
    assert second.attempt == 2
    assert jobs.record_failure(factory, second, Failure("storage_error", "EIO", permanent=False), SETTINGS) is FailureStatus.FAILED
    assert job_row(world.engine, doc.job_id)["status"] == "failed"
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["error_message"]) == ("failed", PUBLIC_MESSAGES["retries_exhausted"])


def test_permanent_failure_is_not_retried_and_shows_a_safe_message(factory, make_doc, world):
    doc = make_doc()
    claim = _claim(factory, doc.job_id)
    failure = Failure("encrypted_pdf", "the document is encrypted", permanent=True)
    assert jobs.record_failure(factory, claim, failure, SETTINGS) is FailureStatus.FAILED
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_count"]) == ("failed", 1)
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["error_message"]) == ("failed", PUBLIC_MESSAGES["encrypted_pdf"])


def test_failures_never_overwrite_indexed_or_stale_documents(factory, make_doc, world):
    doc = make_doc()
    claim = _claim(factory, doc.job_id)
    world.execute("UPDATE documents SET status = 'indexed' WHERE id = :d", d=doc.document_id)
    jobs.record_failure(factory, claim, Failure("invalid_pdf", "x", permanent=True), SETTINGS)
    assert document_row(world.engine, doc.document_id)["status"] == "indexed"


def test_recovery_requeues_or_fails_expired_attempts_and_spares_live_ones(factory, make_doc, world):
    expired, last_chance, live = make_doc(), make_doc(max_attempts=1), make_doc()
    for doc in (expired, last_chance, live):
        _claim(factory, doc.job_id)
    _expire_lease(world, expired.job_id)
    _expire_lease(world, last_chance.job_id)

    result = jobs.recover_expired_leases(factory, SETTINGS, Fixed())

    assert result.requeued == (expired.job_id,) and result.failed == (last_chance.job_id,)
    row = job_row(world.engine, expired.job_id)
    assert (row["status"], row["attempt_token"], row["attempt_count"]) == ("queued", None, 1)
    assert row["error_message"].startswith("lease_expired")
    assert document_row(world.engine, last_chance.document_id)["status"] == "failed"
    assert job_row(world.engine, live.job_id)["status"] == "running"


def test_concurrent_recovery_runs_handle_each_job_once(factory, make_doc, world):
    docs = [make_doc() for _ in range(12)]
    for doc in docs:
        _claim(factory, doc.job_id)
        _expire_lease(world, doc.job_id)
    barrier = threading.Barrier(3, timeout=30)

    def run():
        barrier.wait()
        return jobs.recover_expired_leases(factory, SETTINGS)

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: run(), range(3)))
    handled = [job for r in results for job in r.requeued + r.failed]
    assert sorted(handled) == sorted(doc.job_id for doc in docs)  # each exactly once
    for doc in docs:
        assert job_row(world.engine, doc.job_id)["attempt_count"] == 1


def test_queued_jobs_without_attempts_left_are_failed(factory, make_doc, world):
    doc = make_doc()
    world.execute("UPDATE ingestion_jobs SET attempt_count = 3, started_at = now() WHERE id = :j", j=doc.job_id)
    assert jobs.recover_expired_leases(factory, SETTINGS).failed == (doc.job_id,)
    assert document_row(world.engine, doc.document_id)["status"] == "failed"


def test_recovery_ignores_index_jobs(factory, make_doc, world):
    doc = make_doc()
    world.execute(
        "INSERT INTO ingestion_jobs (document_id, kind, status, attempt_count, started_at, attempt_token, lease_expires_at) "
        "VALUES (:d, 'index', 'running', 1, now() - interval '2 hours', gen_random_uuid(), now() - interval '1 hour')",
        d=doc.document_id,
    )
    assert jobs.recover_expired_leases(factory, SETTINGS) == jobs.RecoveryResult((), ())


# --- operator retry ---------------------------------------------------------------------------------


def _fail(factory, doc):
    claim = _claim(factory, doc.job_id)
    jobs.record_failure(factory, claim, Failure("storage_error", "EIO", permanent=True), SETTINGS)


def test_retry_creates_a_new_job_and_reopens_the_document(factory, make_doc, world):
    doc = make_doc()
    _fail(factory, doc)
    new_job = jobs.retry_parse(factory, doc.document_id)
    assert new_job != doc.job_id
    row = job_row(world.engine, new_job)
    assert (row["status"], row["attempt_count"], row["attempt_token"]) == ("queued", 0, None)
    assert job_row(world.engine, doc.job_id)["status"] == "failed"  # history kept
    document = document_row(world.engine, doc.document_id)
    assert (document["status"], document["error_message"]) == ("pending", None)


def test_concurrent_retries_create_exactly_one_job(factory, make_doc, world):
    doc = make_doc()
    _fail(factory, doc)
    barrier = threading.Barrier(6, timeout=30)

    def retry():
        barrier.wait()
        try:
            return jobs.retry_parse(factory, doc.document_id)
        except RetryRefused:
            return None

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _: retry(), range(6)))
    assert len([r for r in results if r is not None]) == 1
    assert world.scalar(
        "SELECT count(*) FROM ingestion_jobs WHERE document_id = :d AND status = 'queued'", d=doc.document_id
    ) == 1


def test_retry_refuses_documents_that_are_not_failed_or_were_parsed(factory, make_doc, world):
    pending = make_doc()
    with pytest.raises(RetryRefused, match="not failed"):
        jobs.retry_parse(factory, pending.document_id)
    parsed = make_doc()
    claim = _claim(factory, parsed.job_id)
    jobs.complete_parse(factory, claim, 3, SECTIONS)
    world.execute("UPDATE documents SET status = 'failed' WHERE id = :d", d=parsed.document_id)  # e.g. indexing failed
    with pytest.raises(RetryRefused, match="already parsed"):
        jobs.retry_parse(factory, parsed.document_id)
    with pytest.raises(RetryRefused, match="no such document"):
        jobs.retry_parse(factory, uuid.uuid4())


def test_lock_order_is_job_then_document(world, make_doc, factory):
    """Completion holds the job row, then waits for the document row; retry follows the same order."""
    doc = make_doc()
    claim = _claim(factory, doc.job_id)
    statements: list[str] = []

    @event.listens_for(world.engine, "before_cursor_execute")
    def record(conn, cursor, statement, params, context, executemany):
        if "FOR UPDATE" in statement:
            statements.append(statement.split("FROM", 1)[1].split()[0])

    try:
        jobs.complete_parse(factory, claim, 3, SECTIONS)
    finally:
        event.remove(world.engine, "before_cursor_execute", record)
    assert statements[:2] == ["ingestion_jobs", "documents"]
