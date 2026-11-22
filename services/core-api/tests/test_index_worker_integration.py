"""Real Celery index workers (prefork processes) against the disposable PostgreSQL and Redis.

The AI side is the in-process TEST DOUBLE tests/fake_ai_service.py, reached
by the worker subprocesses over 127.0.0.1. These tests show that core-api's
index worker, dispatch and recovery behave as specified; they are not
evidence that a real AI service indexes anything.
"""

from __future__ import annotations

import signal
import sys
import time

import pytest
from sqlalchemy.orm import sessionmaker

from app.celery_app import make_celery
from app.ingestion import dispatch, jobs
from app.ingestion.dispatch import CeleryPublisher
from app.storage import LocalFileStorage
from tests.index_writer import persist
from tests.fake_ai_service import TOKEN, FakeAiService, Fault
from tests.pdf_samples import build_pdf, text_page
from tests.worker_support import add_stored_document, document_row, index_jobs, job_row, start_beat, start_worker, wait_for

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux process controls")
NO_BACKOFF = {"retry_backoff_base_seconds": 0, "retry_backoff_max_seconds": 0, "publish_retry_delay_seconds": 0}


@pytest.fixture
def owner(world):
    return world.user("professor", name="Owner")


@pytest.fixture
def course(world, owner):
    return world.course(owner)


@pytest.fixture
def store(infra):
    return LocalFileStorage(infra.upload_dir)


@pytest.fixture
def fake(world):
    service = FakeAiService(persist=lambda body: persist(world.engine, body)).start()
    yield service
    service.stop()


@pytest.fixture
def ai_env(fake):
    """Environment of an enabled index worker. UPLOAD_DIR is removed: the index worker never needs it."""
    return {
        "upload_dir": "",
        "indexing_enabled": "true",
        "ai_service_url": fake.url,
        "ai_service_token": TOKEN,
        "ai_probe_cache_seconds": "0",
        "ai_read_timeout_seconds": "10",
        "ai_request_deadline_seconds": "20",
        **NO_BACKOFF,
    }


@pytest.fixture
def settings(infra, fake):
    return infra.settings(indexing_enabled=True, ai_service_url=fake.url, ai_service_token=TOKEN, **NO_BACKOFF)


@pytest.fixture
def publisher(settings):
    return CeleryPublisher(make_celery(settings), settings)


@pytest.fixture
def add(world, owner, course, store):
    counter = iter(range(10_000))

    def add(**kwargs):
        data = build_pdf([text_page(f"Lecture {next(counter)}", "Le Chatelier, H₂O → H⁺ + OH⁻")])
        return add_stored_document(world.engine, world, store, course, owner, data, **kwargs)

    return add


def _index_job(world, document_id):
    jobs = index_jobs(world.engine, document_id)
    return jobs[0]["id"] if jobs else None


def _parsed(world, infra, celery_processes, add, publisher, count=1):
    """Documents parsed by a real parse worker; returns [(document_id, index_job_id)]."""
    parse = start_worker(infra, "parse", [infra.parse_queue], fake="real", **NO_BACKOFF)
    celery_processes.append(parse)
    parse.wait_ready()
    docs = [add() for _ in range(count)]
    for doc in docs:
        publisher.publish_parse(doc.job_id)
    result = []
    for doc in docs:
        wait_for(lambda d=doc: job_row(world.engine, d.job_id)["status"] == "succeeded", 90, what="parse")
        result.append((doc.document_id, _index_job(world, doc.document_id)))
    return result


def test_real_index_worker_indexes_without_the_upload_volume(world, infra, celery_processes, add, publisher, fake, ai_env):
    [(document_id, index_job)] = _parsed(world, infra, celery_processes, add, publisher)
    assert document_row(world.engine, document_id)["status"] == "pending"
    worker = start_worker(infra, "index", [infra.index_queue], concurrency=2, **ai_env)
    celery_processes.append(worker)
    worker.wait_ready()
    publisher.publish_index(index_job)
    wait_for(lambda: document_row(world.engine, document_id)["status"] == "indexed", 60, what="indexing")
    row = job_row(world.engine, index_job)
    assert (row["status"], row["attempt_count"]) == ("succeeded", 1)
    assert fake.commit_count == 1
    # UPLOAD_DIR is not in this worker's environment: the processing preflight would
    # have exited with 78, so the worker ran the index role's checks.
    assert worker.proc.poll() is None
    log = worker.log()
    assert "upload store" not in log
    assert TOKEN not in log


def test_duplicate_messages_to_real_workers_index_once(world, infra, celery_processes, add, publisher, fake, ai_env):
    [(document_id, index_job)] = _parsed(world, infra, celery_processes, add, publisher)
    worker = start_worker(infra, "index", [infra.index_queue], concurrency=2, **ai_env)
    celery_processes.append(worker)
    worker.wait_ready()
    for _ in range(4):
        publisher.publish_index(index_job)
    wait_for(lambda: document_row(world.engine, document_id)["status"] == "indexed", 60, what="indexing")
    time.sleep(2)  # let the duplicates be consumed
    assert (job_row(world.engine, index_job)["attempt_count"], len(fake.index_requests()), fake.commit_count) == (1, 1, 1)


def test_disabled_index_worker_claims_nothing(world, infra, celery_processes, add, publisher, fake, ai_env):
    [(document_id, index_job)] = _parsed(world, infra, celery_processes, add, publisher)
    worker = start_worker(infra, "index", [infra.index_queue], **{**ai_env, "indexing_enabled": "false"})
    celery_processes.append(worker)
    worker.wait_ready()
    publisher.publish_index(index_job)
    wait_for(lambda: "reason=indexing_disabled" in worker.log(), 30, what="the disabled refusal")
    row = job_row(world.engine, index_job)
    assert (row["status"], row["attempt_count"]) == ("queued", 0)
    assert fake.requests == []


def test_mock_service_is_refused_by_a_real_worker_without_consuming_attempts(
    world, infra, celery_processes, add, publisher, fake, ai_env
):
    [(document_id, index_job)] = _parsed(world, infra, celery_processes, add, publisher)
    fake.capabilities = {"status": "ok", "service": "ai-service", "mock": True}
    worker = start_worker(infra, "index", [infra.index_queue], **ai_env)
    celery_processes.append(worker)
    worker.wait_ready()
    for _ in range(3):
        publisher.publish_index(index_job)
    wait_for(lambda: worker.log().count("reason=ai_not_ready") >= 3, 30, what="three refusals")
    row = job_row(world.engine, index_job)
    assert (row["status"], row["attempt_count"]) == ("queued", 0)
    assert document_row(world.engine, document_id)["status"] == "pending"
    assert fake.index_requests() == []


@pytest.mark.parametrize(
    "queues, overrides",
    [
        ("mixed", {}),  # index queue together with the parse queue
        ("index", {"ai_service_url": ""}),  # AI_SERVICE_URL missing
        ("index", {"ai_service_token": ""}),  # enabled without a token
        ("index", {"ai_service_token": "short"}),
        ("index", {"ai_service_url": "http://user:pw@ai:8000"}),
        ("index", {"database_require_comment": "studygraph-disposable-test:other"}),  # another database
    ],
)
def test_index_worker_refuses_to_start_on_bad_configuration(infra, celery_processes, ai_env, queues, overrides):
    names = [infra.index_queue, infra.parse_queue] if queues == "mixed" else [infra.index_queue]
    worker = start_worker(infra, "bad-index", names, **{**ai_env, **overrides})
    celery_processes.append(worker)
    assert worker.proc.wait(60) == 78
    assert "start-up check failed" in worker.log()


@pytest.mark.parametrize("after_commit", [False, True], ids=["before-commit", "after-commit"])
def test_lost_response_and_dead_worker_recover_with_the_same_key(
    world, infra, celery_processes, add, publisher, fake, ai_env, settings, after_commit
):
    """A worker killed while its request is in flight: lease recovery requeues, the replay commits nothing new."""
    [(document_id, index_job)] = _parsed(world, infra, celery_processes, add, publisher)
    if after_commit:
        fake.faults.append(Fault("hold_after_commit"))
    else:
        fake.block()
    worker = start_worker(infra, "index", [infra.index_queue], **ai_env)
    celery_processes.append(worker)
    worker.wait_ready()
    publisher.publish_index(index_job)
    wait_for(fake.blocked.is_set, 30, what="the request to be in flight")
    if after_commit:
        assert world.scalar("SELECT chunk_count FROM index_generations WHERE document_id=:d", d=document_id) > 0
        assert document_row(world.engine, document_id)["status"] == "pending"
    worker.signal(signal.SIGKILL)
    worker.proc.wait(10)
    fake.release()  # the fake commits, then finds nobody to answer: a lost response
    wait_for(lambda: fake.commit_count == 1, 10, what="the fake to commit")
    assert job_row(world.engine, index_job)["status"] == "running"
    world.execute("UPDATE ingestion_jobs SET started_at = now() - interval '1 hour', "
        "lease_expires_at = now() - interval '1 second' WHERE id = :j", j=index_job)
    assert jobs.recover_expired_leases(sessionmaker(bind=world.engine), settings, kind="index").requeued == (index_job,)
    second = start_worker(infra, "index2", [infra.index_queue], **ai_env)
    celery_processes.append(second)
    second.wait_ready()
    publisher.publish_index(index_job)
    wait_for(lambda: document_row(world.engine, document_id)["status"] == "indexed", 60, what="indexing")
    keys = {r.headers["Idempotency-Key"] for r in fake.index_requests()}
    assert (len(fake.index_requests()), len(keys), fake.commit_count) == (2, 1, 1)
    assert job_row(world.engine, index_job)["attempt_count"] == 2


def test_parse_and_maintenance_stay_live_while_every_index_slot_is_blocked(
    world, infra, celery_processes, add, publisher, fake, ai_env
):
    intervals = {"dispatch_interval_seconds": 1, "recovery_interval_seconds": 1, "reconcile_interval_seconds": 5, **NO_BACKOFF}
    enabled = {"indexing_enabled": "true", "ai_service_url": fake.url}
    fake.block()
    index = start_worker(infra, "index", [infra.index_queue], concurrency=1, **ai_env)
    parse = start_worker(infra, "parse", [infra.parse_queue], concurrency=1, fake="real", **NO_BACKOFF)
    maintenance = start_worker(infra, "maint", [infra.maintenance_queue], concurrency=1, **intervals, **enabled)
    beat = start_beat(infra, **intervals, **enabled)
    celery_processes.extend([index, parse, maintenance, beat])
    for process in (index, parse, maintenance):
        process.wait_ready()

    first = add()  # parsed, then its index job is dispatched by the sweep and blocks the only index slot
    wait_for(lambda: job_row(world.engine, first.job_id)["status"] == "succeeded", 90, what="first parse")
    first_index = _index_job(world, first.document_id)
    wait_for(fake.blocked.is_set, 60, what="the only index slot to be blocked")
    assert job_row(world.engine, first_index)["status"] == "running"

    second = add()  # parse keeps working while indexing is blocked
    abandoned = add()
    world.execute(
        "UPDATE ingestion_jobs SET status = 'running', attempt_count = 1, started_at = now() - interval '1 hour', "
        "attempt_token = gen_random_uuid(), lease_expires_at = now() - interval '1 minute' WHERE id = :j",
        j=abandoned.job_id,
    )
    wait_for(lambda: job_row(world.engine, second.job_id)["status"] == "succeeded", 90, what="parse during blocked indexing")
    wait_for(lambda: job_row(world.engine, abandoned.job_id)["status"] in ("queued", "succeeded"), 30, what="recovery")
    assert job_row(world.engine, first_index)["status"] == "running"
    assert fake.commit_count == 0

    fake.release()
    for doc in (first, second, abandoned):
        wait_for(lambda d=doc: document_row(world.engine, d.document_id)["status"] == "indexed", 120, what="indexing")
    assert fake.commit_count == 3


def test_dispatch_sweep_publishes_index_jobs_to_the_index_queue_only_when_enabled(
    world, infra, add, publisher, fake, settings, celery_processes
):
    [(document_id, index_job)] = _parsed(world, infra, celery_processes, add, publisher)
    factory = sessionmaker(bind=world.engine)
    disabled = infra.settings(**NO_BACKOFF)
    assert dispatch.sweep_index(factory, CeleryPublisher(make_celery(disabled), disabled), disabled).skipped == "indexing_disabled"
    assert dispatch.sweep(factory, publisher, settings).published == 0  # the parse sweep never takes index jobs
    assert dispatch.sweep_index(factory, publisher, settings).published == 1
    assert publisher.index_queue_depth() == 1
    assert publisher.parse_queue_depth() == 0
