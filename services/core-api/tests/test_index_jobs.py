"""Index jobs on the M3 state machine, against the disposable database and the in-process fake AI.

Everything AI-side here is the TEST DOUBLE tests/fake_ai_service.py: these
tests prove core-api's behaviour (claim, fencing, completion, retries,
recovery, refusals), not that any real AI service indexes anything.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.indexing.client import AiIndexClient
from app.indexing.payload import request_for
from app.indexing.pipeline import IndexContext, run_index_job
from app.ingestion import dispatch, jobs
from app.ingestion.jobs import Failure, FailureStatus
from app.ingestion.normalize import Section
from tests.api_support import DOCUMENT_ITEM_KEYS
from tests.index_writer import persist
from tests.fake_ai_service import TOKEN, FakeAiService, Fault
from tests.pdf_samples import make_pdf
from tests.worker_support import document_row, job_row, sections_of, wait_for

NO_BACKOFF = {"retry_backoff_base_seconds": 0, "retry_backoff_max_seconds": 0}


@pytest.fixture
def owner(world):
    return world.user("professor", name="Owner")


@pytest.fixture
def course(world, owner):
    return world.course(owner)


@pytest.fixture
def factory(world):
    return sessionmaker(bind=world.engine)


@pytest.fixture
def fake(world):
    service = FakeAiService(persist=lambda body: persist(world.engine, body)).start()
    yield service
    service.stop()


def index_settings(fake: FakeAiService, **overrides) -> Settings:
    values = dict(
        indexing_enabled=True, ai_service_url=fake.url, ai_service_token=TOKEN,
        ai_connect_timeout_seconds=2, ai_read_timeout_seconds=5, ai_request_deadline_seconds=10,
        ai_probe_timeout_seconds=2, ai_probe_cache_seconds=0, **NO_BACKOFF,
    )  # fmt: skip
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def ctx(factory, fake):
    settings = index_settings(fake)
    return IndexContext(session_factory=factory, settings=settings, client=AiIndexClient(settings))


@pytest.fixture
def parsed(world, course, owner):
    def make(*args, **kwargs):
        return _parsed(world, course, owner, *args, **kwargs)

    return make


def _parsed(world, course, owner, texts=("Le Chatelier", "H₂O → H⁺"), *, title="Lecture.pdf", max_attempts=3):
    """A parsed class document (succeeded parse job, sections) and its queued index job, as M3 leaves them."""
    document_id = world.document(
        course, title=title, uploaded_by=owner.id, content_hash="c" * 64, page_count=len(texts) + 1, kind="notes"
    )
    world.execute(
        "INSERT INTO ingestion_jobs (document_id, kind, status, attempt_count, started_at, finished_at, attempt_token) "
        "VALUES (:d, 'parse', 'succeeded', 1, now(), now(), gen_random_uuid())",
        d=document_id,
    )
    for ordinal, body in enumerate(texts):
        world.execute(
            "INSERT INTO document_sections (document_id, ordinal, text, locator) "
            "VALUES (:d, :o, :t, jsonb_build_object('page', CAST(:p AS integer)))",
            d=document_id, o=ordinal, t=body, p=ordinal + 1,
        )  # fmt: skip
    world.execute(
        "INSERT INTO ingestion_jobs (document_id, kind, max_attempts) VALUES (:d, 'index', :m)",
        d=document_id,
        m=max_attempts,
    )
    job_id = world.scalar(
        "SELECT id FROM ingestion_jobs WHERE document_id = :d AND kind = 'index' AND status = 'queued'", d=document_id
    )
    return document_id, job_id


def _job(world, job_id):
    return job_row(world.engine, job_id)


def _doc(world, document_id):
    return document_row(world.engine, document_id)


# --- the pre-claim gate: nothing claimed, no attempt consumed ---------------------------------------


def test_disabled_indexing_claims_nothing_and_calls_nothing(world, parsed, factory, fake):
    document_id, job_id = parsed()
    settings = index_settings(fake, indexing_enabled=False)
    ctx = IndexContext(session_factory=factory, settings=settings, client=AiIndexClient(settings))
    assert run_index_job(job_id, ctx).outcome == "indexing_disabled"
    assert (_job(world, job_id)["status"], _job(world, job_id)["attempt_count"]) == ("queued", 0)
    assert _doc(world, document_id)["status"] == "pending"
    assert fake.requests == []


@pytest.mark.parametrize(
    "setup",
    [
        lambda f: setattr(f, "capabilities", {"status": "ok", "service": "ai-service", "mock": True}),
        lambda f: setattr(f, "capabilities", {"protocol": "studygraph.index/1", "service": "ai-service", "mock": True,
                                               "completion": ["sync"], "max_request_bytes": 10**6}),
        lambda f: setattr(f, "capabilities_status", 503),
        lambda f: setattr(f, "token", "another-token-" + "y" * 32),
    ],
    ids=["repository-mock-shape", "mock-true", "unavailable", "wrong-token"],
)  # fmt: skip
def test_mock_incompatible_or_unavailable_services_consume_no_attempt(world, parsed, ctx, fake, setup):
    document_id, job_id = parsed()
    setup(fake)
    for _ in range(3):
        assert run_index_job(job_id, ctx).outcome == "ai_not_ready"
    row = _job(world, job_id)
    assert (row["status"], row["attempt_count"], row["attempt_token"]) == ("queued", 0, None)
    assert _doc(world, document_id)["status"] == "pending"
    assert fake.index_requests() == []


# --- confirmed completion ---------------------------------------------------------------------------


def test_receipt_without_committed_rows_never_publishes(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    with ctx.session_factory() as session:
        request = request_for(session, document_id)
    receipt = {"protocol": "studygraph.index/1", "status": "indexed", "document_id": str(document_id),
               "course_id": str(request.course_id), "content_digest": request.content_digest,
               "idempotency_key": request.idempotency_key, "section_count": request.section_count,
               "chunk_count": request.section_count, "replayed": False}
    fake.faults.append(Fault("status", status=200, body=json.dumps(receipt).encode()))
    assert world.scalar("SELECT count(*) FROM chunks WHERE document_id = :d", d=document_id) == 0
    result = run_index_job(job_id, ctx)
    assert result.outcome == "requeued" and result.code == "index_proof_missing"
    assert _doc(world, document_id)["status"] == "pending"


def test_confirmed_indexing_completes_job_and_document_together(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    result = run_index_job(job_id, ctx)
    assert result.outcome == "succeeded"
    row = _job(world, job_id)
    assert (row["status"], row["attempt_count"], row["lease_expires_at"], row["error_message"]) == ("succeeded", 1, None, None)
    assert (_doc(world, document_id)["status"], _doc(world, document_id)["error_message"]) == ("indexed", None)
    with sessionmaker(bind=world.engine)() as session:
        expected = request_for(session, document_id)
    assert fake.index_requests()[0].body == expected.body
    assert fake.commit_count == 1


def test_public_status_contract_ready_only_after_confirmed_indexing(api, world, owner, course, fake):
    """queued -> processing (parsed, not indexed) -> ready; same keys as before M3/M4."""
    api.as_user(owner)
    document_id = uuid.UUID(api.upload(course, make_pdf("contract")).json()["id"])
    factory = sessionmaker(bind=world.engine)
    parse_job = world.scalar("SELECT id FROM ingestion_jobs WHERE document_id = :d AND kind = 'parse'", d=document_id)
    assert api.list(course).json()[0]["status"] == "queued"
    with factory() as session:
        claim = jobs.claim_parse(session, parse_job, 300)
        session.commit()
    jobs.complete_parse(factory, claim, 3, [Section(0, 1, "Text one"), Section(1, 3, "Text three")])
    item = api.list(course).json()[0]
    assert (set(item), item["status"], item["pages"]) == (DOCUMENT_ITEM_KEYS, "processing", 3)

    index_job = world.scalar("SELECT id FROM ingestion_jobs WHERE document_id = :d AND kind = 'index'", d=document_id)
    settings = index_settings(fake)
    ctx = IndexContext(session_factory=factory, settings=settings, client=AiIndexClient(settings))
    assert run_index_job(index_job, ctx).outcome == "succeeded"
    item = api.list(course).json()[0]
    assert (set(item), item["status"], item["error"], item["pages"]) == (DOCUMENT_ITEM_KEYS, "ready", None, 3)


def test_failed_indexing_is_shown_as_failed_with_a_public_message(api, world, owner, course, fake):
    api.as_user(owner)
    document_id = uuid.UUID(api.upload(course, make_pdf("rejected")).json()["id"])
    factory = sessionmaker(bind=world.engine)
    parse_job = world.scalar("SELECT id FROM ingestion_jobs WHERE document_id = :d AND kind = 'parse'", d=document_id)
    with factory() as session:
        claim = jobs.claim_parse(session, parse_job, 300)
        session.commit()
    jobs.complete_parse(factory, claim, 1, [Section(0, 1, "Text")])
    index_job = world.scalar("SELECT id FROM ingestion_jobs WHERE document_id = :d AND kind = 'index'", d=document_id)
    fake.faults.append(Fault("status", status=422, body=b'{"error":{"code":"unsupported_language","message":"m","retryable":false}}'))
    settings = index_settings(fake)
    assert run_index_job(index_job, IndexContext(factory, settings, AiIndexClient(settings))).outcome == "failed"
    item = api.list(course).json()[0]
    assert (item["status"], item["error"]) == ("failed", jobs.PUBLIC_MESSAGES["index_rejected"])
    assert "unsupported_language" not in item["error"]  # operational detail stays in the job row
    assert "unsupported_language" in _job(world, index_job)["error_message"]


# --- atomic claim, fencing, recovery ------------------------------------------------------------------


def test_exactly_one_of_many_concurrent_claims_wins(world, parsed, factory):
    _, job_id = parsed()
    barrier = threading.Barrier(8)

    def claim():
        with factory() as session:
            barrier.wait()
            result = jobs.claim_index(session, job_id, 300)
            session.commit()
            return result

    with ThreadPoolExecutor(8) as pool:
        claims = [c for c in pool.map(lambda _: claim(), range(8)) if c is not None]
    assert len(claims) == 1 and claims[0].kind == "index"
    assert _job(world, job_id)["attempt_count"] == 1


def test_parse_claims_never_take_index_jobs_and_vice_versa(world, parsed, factory):
    document_id, job_id = parsed()
    parse_job = world.scalar("SELECT id FROM ingestion_jobs WHERE document_id = :d AND kind = 'parse'", d=document_id)
    world.execute("UPDATE ingestion_jobs SET status = 'queued', finished_at = NULL WHERE id = :j", j=parse_job)
    with factory() as session:
        assert jobs.claim_parse(session, job_id, 300) is None
        assert jobs.claim_index(session, parse_job, 300) is None
        session.rollback()
    world.execute("UPDATE ingestion_jobs SET status = 'succeeded', finished_at = now() WHERE id = :j", j=parse_job)


def test_a_stale_attempt_writes_nothing_after_recovery(world, parsed, factory, ctx, fake):
    document_id, job_id = parsed()
    with factory() as session:
        stale = jobs.claim_index(session, job_id, 300)
        session.commit()
    world.execute("UPDATE ingestion_jobs SET started_at = now() - interval '1 hour', "
        "lease_expires_at = now() - interval '1 second' WHERE id = :j", j=job_id)
    result = jobs.recover_expired_leases(factory, ctx.settings, kind="index")
    assert result.requeued == (job_id,)
    with factory() as session:
        digest = request_for(session, document_id).content_digest
    assert jobs.complete_index(factory, stale, digest, 1) is jobs.CompletionStatus.STALE
    failure = Failure("ai_timeout", "x", permanent=True)
    assert jobs.record_failure(factory, stale, failure, ctx.settings) is FailureStatus.STALE
    assert (_job(world, job_id)["status"], _doc(world, document_id)["status"]) == ("queued", "pending")
    # The next attempt owns the job and completes it; attempts are counted, never given back.
    assert run_index_job(job_id, ctx).outcome == "succeeded"
    assert (_job(world, job_id)["attempt_count"], _doc(world, document_id)["status"]) == (2, "indexed")


def test_index_recovery_fails_exhausted_attempts_and_ignores_parse_jobs(world, parsed, factory, ctx):
    document_id, job_id = parsed()
    world.execute(
        "UPDATE ingestion_jobs SET status = 'running', attempt_count = 3, started_at = now() - interval '2 hours', "
        "attempt_token = gen_random_uuid(), lease_expires_at = now() - interval '1 hour' WHERE id = :j",
        j=job_id,
    )
    assert jobs.recover_expired_leases(factory, ctx.settings) == jobs.RecoveryResult((), ())  # parse only
    assert jobs.recover_expired_leases(factory, ctx.settings, kind="index").failed == (job_id,)
    doc = _doc(world, document_id)
    assert (doc["status"], doc["error_message"]) == ("failed", jobs.PUBLIC_MESSAGES["index_retries_exhausted"])
    assert _job(world, job_id)["attempt_count"] == 3


# --- duplicates, lost responses, retries ------------------------------------------------------------


def test_duplicate_delivery_is_a_no_op(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    assert run_index_job(job_id, ctx).outcome == "succeeded"
    assert run_index_job(job_id, ctx).outcome == "not_claimed"
    assert (len(fake.index_requests()), fake.commit_count, _job(world, job_id)["attempt_count"]) == (1, 1, 1)


def test_concurrent_duplicate_deliveries_claim_once(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    with ThreadPoolExecutor(4) as pool:
        outcomes = sorted(r.outcome for r in pool.map(lambda _: run_index_job(job_id, ctx), range(4)))
    assert outcomes == ["not_claimed"] * 3 + ["succeeded"]
    assert (len(fake.index_requests()), fake.commit_count) == (1, 1)


def test_lost_response_is_replayed_with_the_same_key_and_committed_once(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    fake.faults.append(Fault("lose_after_commit"))
    first = run_index_job(job_id, ctx)
    assert (first.outcome, first.code) == ("requeued", "ai_connection_lost")
    assert (_job(world, job_id)["status"], _doc(world, document_id)["status"]) == ("queued", "pending")
    assert run_index_job(job_id, ctx).outcome == "succeeded"
    keys = [r.headers["Idempotency-Key"] for r in fake.index_requests()]
    assert len(keys) == 2 and len(set(keys)) == 1
    assert fake.commit_count == 1
    assert (_job(world, job_id)["attempt_count"], _doc(world, document_id)["status"]) == (2, "indexed")


def test_202_is_retried_never_treated_as_success(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    fake.faults.append(Fault("status", status=202, body=b'{"job_id":"later"}'))
    result = run_index_job(job_id, ctx)
    assert (result.outcome, result.code) == ("requeued", "index_not_confirmed")
    assert _doc(world, document_id)["status"] == "pending"
    assert run_index_job(job_id, ctx).outcome == "succeeded"  # the replay confirms


def test_transient_failures_exhaust_attempts_monotonically(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    fake.faults.extend(Fault("status", status=503) for _ in range(3))
    seen = []
    for expected in ("requeued", "requeued", "failed"):
        assert run_index_job(job_id, ctx).outcome == expected
        seen.append(_job(world, job_id)["attempt_count"])
    assert seen == [1, 2, 3]
    assert run_index_job(job_id, ctx).outcome == "not_claimed"
    assert _job(world, job_id)["attempt_count"] == 3
    doc = _doc(world, document_id)
    assert (doc["status"], doc["error_message"]) == ("failed", jobs.PUBLIC_MESSAGES["index_retries_exhausted"])


def test_an_incompatible_answer_after_the_claim_stops_further_claims(world, parsed, factory, fake):
    settings = index_settings(fake, ai_probe_cache_seconds=3600)
    ctx = IndexContext(session_factory=factory, settings=settings, client=AiIndexClient(settings))
    first_doc, first = parsed()
    _, second = parsed(texts=("other",))
    fake.faults.append(Fault("status", status=200, body=b'{"chunk_count": 12, "document_id": "d_mock"}'))
    result = run_index_job(first, ctx)
    assert (result.outcome, result.code) == ("requeued", "invalid_response")
    assert run_index_job(second, ctx).outcome == "ai_not_ready"
    assert _job(world, second)["attempt_count"] == 0
    assert _doc(world, first_doc)["status"] == "pending"


def test_payload_over_the_limit_fails_permanently_without_a_request(world, parsed, factory, fake):
    document_id, job_id = parsed(texts=("x" * 5000,))
    settings = index_settings(fake, index_max_payload_bytes=2048)
    ctx = IndexContext(session_factory=factory, settings=settings, client=AiIndexClient(settings))
    result = run_index_job(job_id, ctx)
    assert (result.outcome, result.code) == ("failed", "index_payload_too_large")
    assert fake.index_requests() == []
    assert _doc(world, document_id)["error_message"] == jobs.PUBLIC_MESSAGES["index_payload_too_large"]


def test_the_service_limit_is_respected_too(world, parsed, ctx, fake):
    document_id, job_id = parsed(texts=("y" * 5000,))
    fake.max_request_bytes = 1024
    assert run_index_job(job_id, ctx).code == "index_payload_too_large"
    assert fake.index_requests() == []


def test_a_document_that_is_no_longer_pending_is_left_alone(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    world.execute("UPDATE documents SET status = 'stale' WHERE id = :d", d=document_id)
    assert run_index_job(job_id, ctx).code == "document_not_pending"
    assert (_job(world, job_id)["status"], _doc(world, document_id)["status"]) == ("failed", "stale")
    assert fake.index_requests() == []


# --- content changes during processing ------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE document_sections SET text = 'edited' WHERE document_id = :d AND ordinal = 0",
        "UPDATE documents SET title = 'Renamed.pdf' WHERE id = :d",
        "DELETE FROM document_sections WHERE document_id = :d AND ordinal = 1",
    ],
    ids=["section-text", "metadata", "section-removed"],
)
def test_content_changed_while_indexing_is_never_marked_indexed(world, parsed, ctx, fake, change):
    document_id, job_id = parsed()
    fake.faults.append(Fault("hold_after_commit"))
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("r", run_index_job(job_id, ctx)))
    worker.start()
    wait_for(fake.blocked.is_set, 10, what="the index request to be in flight")
    world.execute(change, d=document_id)
    fake.release()
    worker.join(30)
    assert (outcome["r"].outcome, outcome["r"].code) == ("failed", "index_content_changed")
    assert _job(world, job_id)["status"] == "failed"
    doc = _doc(world, document_id)
    assert (doc["status"], doc["error_message"]) == ("failed", jobs.PUBLIC_MESSAGES["index_content_changed"])
    assert len(fake.index_requests()) == 1  # nothing re-sent automatically


# --- retry-index ------------------------------------------------------------------------------------


def _reject(fake):
    fake.faults.append(Fault("status", status=400, body=b'{"error":{"code":"bad","message":"m","retryable":false}}'))


def test_retry_index_reuses_the_stored_sections_without_extraction(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    _reject(fake)
    assert run_index_job(job_id, ctx).outcome == "failed"
    sections_before = sections_of(world.engine, document_id)
    parse_jobs_before = world.rows("SELECT id, status, attempt_count FROM ingestion_jobs WHERE document_id = :d AND kind = 'parse'", d=document_id)

    new_job = jobs.retry_index(ctx.session_factory, document_id)
    assert new_job != job_id
    assert (_job(world, new_job)["attempt_count"], _job(world, job_id)["status"]) == (0, "failed")  # history kept
    assert (_doc(world, document_id)["status"], _doc(world, document_id)["error_message"]) == ("pending", None)
    assert run_index_job(new_job, ctx).outcome == "succeeded"

    assert sections_of(world.engine, document_id) == sections_before  # same rows, ids and timestamps
    parse_jobs_after = world.rows("SELECT id, status, attempt_count FROM ingestion_jobs WHERE document_id = :d AND kind = 'parse'", d=document_id)
    assert parse_jobs_after == parse_jobs_before  # no new parse, no extraction
    keys = {r.headers["Idempotency-Key"] for r in fake.index_requests()}
    assert len(keys) == 1  # unchanged content: the same request identity as the failed job
    assert _doc(world, document_id)["status"] == "indexed"


def test_retry_index_refusals(world, parsed, ctx, fake, course):
    indexed_doc, indexed_job = parsed()
    assert run_index_job(indexed_job, ctx).outcome == "succeeded"
    with pytest.raises(jobs.RetryRefused, match="not failed"):
        jobs.retry_index(ctx.session_factory, indexed_doc)
    world.execute("UPDATE documents SET status = 'failed' WHERE id = :d", d=indexed_doc)
    with pytest.raises(jobs.RetryRefused, match="already indexed"):
        jobs.retry_index(ctx.session_factory, indexed_doc)

    queued_doc, _ = parsed()
    world.execute("UPDATE documents SET status = 'failed' WHERE id = :d", d=queued_doc)
    with pytest.raises(jobs.RetryRefused, match="already queued or running"):
        jobs.retry_index(ctx.session_factory, queued_doc)

    never_parsed = world.document(course, title="parse failed")
    world.execute("UPDATE documents SET status = 'failed' WHERE id = :d", d=never_parsed)
    with pytest.raises(jobs.RetryRefused, match="never parsed"):
        jobs.retry_index(ctx.session_factory, never_parsed)
    with pytest.raises(jobs.RetryRefused, match="no such document"):
        jobs.retry_index(ctx.session_factory, uuid.uuid4())


def test_concurrent_retry_index_creates_one_job(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    _reject(fake)
    run_index_job(job_id, ctx)
    barrier = threading.Barrier(4)

    def retry():
        barrier.wait()
        try:
            return jobs.retry_index(ctx.session_factory, document_id)
        except jobs.RetryRefused:
            return None

    with ThreadPoolExecutor(4) as pool:
        created = [j for j in pool.map(lambda _: retry(), range(4)) if j is not None]
    assert len(created) == 1


def test_retry_parse_still_refuses_parsed_documents(world, parsed, ctx, fake):
    document_id, job_id = parsed()
    _reject(fake)
    run_index_job(job_id, ctx)
    with pytest.raises(jobs.RetryRefused):  # no stored file here; with one: "already parsed"
        jobs.retry_parse(ctx.session_factory, document_id)
    world.execute(
        "UPDATE documents SET storage_key = :k, original_filename = 'x.pdf', mime_type = 'application/pdf', "
        "size_bytes = 1 WHERE id = :d",
        k=f"{uuid.uuid4()}/{document_id}.pdf", d=document_id,
    )  # fmt: skip
    with pytest.raises(jobs.RetryRefused, match="already parsed"):
        jobs.retry_parse(ctx.session_factory, document_id)
    world.execute("UPDATE documents SET storage_key = NULL WHERE id = :d", d=document_id)


# --- course isolation end to end --------------------------------------------------------------------


def test_identical_documents_in_two_courses_are_indexed_separately(world, owner, ctx, fake):
    course_a, course_b = world.course(owner, "A"), world.course(owner, "B")
    doc_a, job_a = _parsed(world, course_a, owner)
    doc_b, job_b = _parsed(world, course_b, owner)
    assert run_index_job(job_a, ctx).outcome == run_index_job(job_b, ctx).outcome == "succeeded"
    assert len(fake.commits_for_course(course_a)) == len(fake.commits_for_course(course_b)) == 1
    keys = {r.headers["Idempotency-Key"] for r in fake.index_requests()}
    assert len(keys) == 2


# --- dispatch ---------------------------------------------------------------------------------------


class RecordingIndexPublisher:
    def __init__(self) -> None:
        self.published: list[uuid.UUID] = []

    def publish_index(self, job_id: uuid.UUID) -> None:
        self.published.append(job_id)

    def index_queue_depth(self) -> int:
        return 0


def test_index_sweep_publishes_nothing_while_disabled_and_only_index_jobs_when_enabled(world, parsed, factory, fake):
    _, job_id = parsed()
    publisher = RecordingIndexPublisher()
    disabled = index_settings(fake, indexing_enabled=False)
    assert dispatch.sweep_index(factory, publisher, disabled).skipped == "indexing_disabled"
    assert publisher.published == [] and _job(world, job_id)["last_dispatched_at"] is None
    enabled = index_settings(fake)
    assert dispatch.sweep_index(factory, publisher, enabled).published == 1
    assert publisher.published == [job_id]
    assert dispatch.sweep_index(factory, publisher, enabled).published == 0  # reserved, not due again
    assert _job(world, job_id)["attempt_count"] == 0  # publishing never consumes an attempt
