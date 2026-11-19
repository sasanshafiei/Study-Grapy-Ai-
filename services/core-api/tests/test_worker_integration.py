"""Real distributed behaviour: Celery worker and Beat processes, the disposable Redis and PostgreSQL.

No eager mode. Workers are started as separate processes (prefork pool) with
`celery -A tests.worker_app`, which is the production app.worker plus the
test-only fake-extractor hook. Where an attempt must be "frozen past its
lease", the lease is moved into the past in the database: that simulates the
passage of time for a process that cannot run, nothing else.
"""

from __future__ import annotations

import os
import signal
import sys
import time
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.celery_app import PARSE_TASK, make_celery
from app.ingestion import dispatch, jobs
from app.ingestion.dispatch import CeleryPublisher
from app.storage import INCOMING_DIR, LocalFileStorage
from tests.api_support import ApiHarness
from tests.pdf_samples import build_pdf, encrypt_pdf, text_page
from tests.worker_support import (
    add_stored_document,
    document_row,
    index_jobs,
    job_row,
    pid_alive,
    sections_of,
    start_beat,
    start_worker,
    wait_for,
)

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
def factory(world):
    return sessionmaker(bind=world.engine)


@pytest.fixture
def settings(infra):
    return infra.settings(**NO_BACKOFF)


@pytest.fixture
def publisher(settings):
    return CeleryPublisher(make_celery(settings), settings)


@pytest.fixture
def add(world, owner, course, store):
    counter = iter(range(10_000))

    def add(data: bytes | None = None, **kwargs):
        data = data or build_pdf([text_page(f"Lecture {next(counter)}", "H₂O → H⁺ + OH⁻")])
        return add_stored_document(world.engine, world, store, course, owner, data, **kwargs)

    return add


def _status(world, job_id):
    return job_row(world.engine, job_id)["status"]


def _wait_status(world, job_id, status, timeout=60):
    wait_for(lambda: _status(world, job_id) == status, timeout, what=f"job {job_id} to be {status}")


def test_upload_is_parsed_by_a_real_worker(world, infra, celery_processes, owner, course, settings, tmp_path):
    api = ApiHarness(world, tmp_path / "api", publisher=CeleryPublisher(make_celery(settings), settings))
    try:
        worker = start_worker(infra, "w1", [infra.parse_queue], upload_dir=api.upload_root)
        celery_processes.append(worker)
        worker.wait_ready()
        pdf = build_pdf([text_page("Chapter 3: Stoichiometry"), text_page("2H₂ + O₂ → 2H₂O")])
        response = api.as_user(owner).upload(course, pdf, filename="Chapter 3.pdf")
        assert response.status_code == 202 and response.json()["status"] == "queued"
        job_id = uuid.UUID(response.json()["job_id"])
        _wait_status(world, job_id, "succeeded")

        document_id = uuid.UUID(response.json()["id"])
        assert [(s["ordinal"], s["locator"]) for s in sections_of(world.engine, document_id)] == [
            (0, {"page": 1}),
            (1, {"page": 2}),
        ]
        [index] = index_jobs(world.engine, document_id)
        assert index["status"] == "queued"
        [item] = api.list(course).json()
        assert (item["status"], item["pages"]) == ("processing", 2)  # parsed, awaiting indexing: not ready
        assert "parse_succeeded" in worker.log()
    finally:
        api.client.close()


def test_concurrent_workers_and_duplicate_deliveries_process_each_job_once(
    world, infra, celery_processes, add, publisher, factory, settings, disposable_redis
):
    workers = [start_worker(infra, f"w{n}", [infra.parse_queue], concurrency=2) for n in range(2)]
    celery_processes.extend(workers)
    for worker in workers:
        worker.wait_ready()
    docs = [add() for _ in range(12)]
    for _ in range(3):  # every job delivered three times...
        for doc in docs:
            publisher.publish_parse(doc.job_id)
    dispatch.sweep(factory, publisher, settings)  # ...plus the sweep's own publication
    for doc in docs:
        _wait_status(world, doc.job_id, "succeeded", timeout=120)
    for doc in docs:
        assert job_row(world.engine, doc.job_id)["attempt_count"] == 1
        assert len(sections_of(world.engine, doc.document_id)) == 1
        assert len(index_jobs(world.engine, doc.document_id)) == 1
    client = disposable_redis.client()
    try:
        wait_for(lambda: client.llen(infra.parse_queue) == 0, 60, what="the queue to drain")
    finally:
        client.close()
    assert sum(w.log().count("parse_not_claimed") for w in workers) >= 24  # the duplicates, harmlessly


def test_permanent_failure_with_a_real_worker(world, infra, celery_processes, add, publisher):
    worker = start_worker(infra, "w1", [infra.parse_queue])
    celery_processes.append(worker)
    worker.wait_ready()
    doc = add(encrypt_pdf(build_pdf([text_page("secret")])))
    publisher.publish_parse(doc.job_id)
    _wait_status(world, doc.job_id, "failed")
    row = job_row(world.engine, doc.job_id)
    assert (row["attempt_count"], row["error_message"].split(":")[0]) == (1, "encrypted_pdf")
    assert document_row(world.engine, doc.document_id)["error_message"] == jobs.PUBLIC_MESSAGES["encrypted_pdf"]


def test_transient_failures_retry_and_then_exhaust(world, infra, celery_processes, add, publisher, factory, settings):
    worker = start_worker(infra, "w1", [infra.parse_queue], fake="crash:99", **NO_BACKOFF)
    celery_processes.append(worker)
    worker.wait_ready()
    doc = add()
    publisher.publish_parse(doc.job_id)
    for attempt in (1, 2):
        wait_for(
            lambda: (row := job_row(world.engine, doc.job_id))["status"] == "queued" and row["attempt_count"] == attempt,
            60,
            what=f"attempt {attempt} to be requeued",
        )
        assert dispatch.sweep(factory, publisher, settings).published == 1  # what Beat's dispatch task does
    _wait_status(world, doc.job_id, "failed")
    assert job_row(world.engine, doc.job_id)["attempt_count"] == 3
    assert document_row(world.engine, doc.document_id)["error_message"] == jobs.PUBLIC_MESSAGES["retries_exhausted"]


def test_killed_worker_process_leaves_no_extractor_and_its_job_is_recovered(
    world, infra, celery_processes, add, publisher, factory, settings
):
    limits = {"parse_timeout_seconds": 6, "parse_task_soft_time_limit_seconds": 7,
              "parse_task_time_limit_seconds": 8, "job_lease_margin_seconds": 1}  # fmt: skip
    victim = start_worker(infra, "victim", [infra.parse_queue], fake="hang", **limits)
    celery_processes.append(victim)
    victim.wait_ready()
    doc = add()
    publisher.publish_parse(doc.job_id)
    [(extractor, pool_process)] = wait_for(victim.extractor_pids, 60, what="the extractor to start")
    claimed = job_row(world.engine, doc.job_id)
    assert claimed["status"] == "running" and (claimed["lease_expires_at"] - claimed["started_at"]).total_seconds() == 9

    os.kill(pool_process, signal.SIGKILL)  # like a hard time limit or the OOM killer hitting the pool process
    wait_for(lambda: not pid_alive(extractor), 15, what="the extractor to die with its pool process")
    assert _status(world, doc.job_id) == "running"  # nobody recorded anything

    wait_for(
        lambda: jobs.recover_expired_leases(factory, settings).requeued, 30, interval=0.5, what="the lease to expire"
    )
    rescuer = start_worker(infra, "rescuer", [infra.parse_queue])
    celery_processes.append(rescuer)
    rescuer.wait_ready()
    victim.stop()
    assert dispatch.sweep(factory, publisher, settings).published == 1
    _wait_status(world, doc.job_id, "succeeded")
    assert job_row(world.engine, doc.job_id)["attempt_count"] == 2


def test_stale_worker_cannot_overwrite_the_newer_attempt(world, infra, celery_processes, add, publisher, factory, settings):
    stale = start_worker(infra, "stale", [infra.parse_queue], fake="wait")
    celery_processes.append(stale)
    stale.wait_ready()
    doc = add()
    publisher.publish_parse(doc.job_id)
    wait_for(stale.extractor_pids, 60, what="the first attempt to start")
    first_token = job_row(world.engine, doc.job_id)["attempt_token"]

    # The first worker is unresponsive for longer than its lease (simulated by moving the
    # attempt's start and lease an hour into the past; the lease stays after the start).
    world.execute(
        "UPDATE ingestion_jobs SET started_at = started_at - interval '1 hour', "
        "lease_expires_at = started_at - interval '59 minutes' WHERE id = :j",
        j=doc.job_id,
    )
    assert jobs.recover_expired_leases(factory, settings).requeued == (doc.job_id,)
    fresh = start_worker(infra, "fresh", [infra.parse_queue])
    celery_processes.append(fresh)
    fresh.wait_ready()
    assert dispatch.sweep(factory, publisher, settings).published == 1
    _wait_status(world, doc.job_id, "succeeded")
    winner = job_row(world.engine, doc.job_id)
    assert winner["attempt_token"] != first_token and winner["attempt_count"] == 2
    sections = sections_of(world.engine, doc.document_id)

    stale.release_extractors()  # the first worker resumes and tries to finish its attempt
    wait_for(lambda: "stale_attempt_discarded" in stale.log(), 60, what="the stale attempt to be discarded")
    assert sections_of(world.engine, doc.document_id) == sections
    assert job_row(world.engine, doc.job_id)["attempt_token"] == winner["attempt_token"]
    assert len(index_jobs(world.engine, doc.document_id)) == 1


def test_warm_shutdown_lets_the_running_parse_finish(world, infra, celery_processes, add, publisher):
    worker = start_worker(infra, "w1", [infra.parse_queue], fake="wait")
    celery_processes.append(worker)
    worker.wait_ready()
    doc = add()
    publisher.publish_parse(doc.job_id)
    [(extractor, _)] = wait_for(worker.extractor_pids, 60, what="the attempt to start")
    worker.proc.send_signal(signal.SIGTERM)  # what `docker stop` sends to the main process
    time.sleep(1)
    assert worker.proc.poll() is None and pid_alive(extractor)  # still finishing its task
    worker.release_extractors()
    assert worker.proc.wait(60) == 0
    assert _status(world, doc.job_id) == "succeeded"


def test_maintenance_keeps_running_while_every_parse_slot_is_busy(
    world, infra, celery_processes, add, publisher, store
):
    intervals = {"dispatch_interval_seconds": 1, "recovery_interval_seconds": 1, "reconcile_interval_seconds": 2,
                 "reconcile_temp_grace_seconds": 60, **NO_BACKOFF}  # fmt: skip
    parse = start_worker(infra, "parse", [infra.parse_queue], concurrency=1, fake="wait")
    maintenance = start_worker(infra, "maint", [infra.maintenance_queue], concurrency=1, **intervals)
    beat = start_beat(infra, **intervals)
    celery_processes.extend([parse, maintenance, beat])
    parse.wait_ready()
    maintenance.wait_ready()
    time.sleep(1)
    assert beat.proc.poll() is None, beat.log()[-2000:]

    blocker = add()
    publisher.publish_parse(blocker.job_id)
    wait_for(parse.extractor_pids, 60, what="the only parse slot to be busy")

    never_published = add()
    world.execute("UPDATE ingestion_jobs SET last_dispatched_at = NULL WHERE id = :j", j=never_published.job_id)
    abandoned = add()
    world.execute(
        "UPDATE ingestion_jobs SET status = 'running', attempt_count = 1, started_at = now() - interval '1 hour', "
        "attempt_token = gen_random_uuid(), lease_expires_at = now() - interval '1 minute' WHERE id = :j",
        j=abandoned.job_id,
    )
    temp = store.root / INCOMING_DIR / "staged-maintain.part"
    temp.parent.mkdir(exist_ok=True)
    temp.write_bytes(b"left by a crashed upload")
    past = time.time() - 7200
    os.utime(temp, (past, past))

    wait_for(lambda: job_row(world.engine, never_published.job_id)["last_dispatched_at"], 30, what="dispatch")
    wait_for(lambda: job_row(world.engine, abandoned.job_id)["status"] == "queued", 30, what="recovery")
    wait_for(lambda: not temp.exists(), 30, what="reconciliation")
    assert _status(world, blocker.job_id) == "running"  # all of that while the parse slot stayed busy
    parse.release_extractors()
    for doc in (blocker, never_published, abandoned):
        _wait_status(world, doc.job_id, "succeeded", timeout=90)


@pytest.mark.parametrize(
    "overrides",
    [
        {"database_require_comment": "studygraph-disposable-test:other"},  # another database
        {"database_require_comment": "   "},  # a blank marker must not switch the check off
        {"upload_dir": ""},  # missing explicit setting
        {"pgoptions": "-c search_path=elsewhere"},  # libpq override on top of the URL
        {"pghostaddr": "10.255.255.1"},
        {"database_url": "postgresql+psycopg://u:p@127.0.0.1:5432/sg_disposable_x"},  # loopback
    ],
    ids=["wrong-marker", "blank-marker", "no-upload-dir", "PGOPTIONS", "PGHOSTADDR", "loopback"],
)
def test_worker_refuses_to_start_against_an_unverified_database_target(infra, celery_processes, overrides):
    worker = start_worker(infra, "refused", [infra.parse_queue], **overrides)
    celery_processes.append(worker)
    assert worker.proc.wait(60) == 78
    assert "start-up check failed" in worker.log()
    assert infra.database_url not in worker.log()  # the URL (with its password) is never printed


def test_worker_with_an_unknown_store_claims_nothing(world, infra, celery_processes, add, publisher, tmp_path):
    empty = tmp_path / "wrong-volume"
    empty.mkdir()
    worker = start_worker(infra, "w1", [infra.parse_queue], upload_dir=empty)
    celery_processes.append(worker)
    worker.wait_ready()
    doc = add()
    for _ in range(3):
        publisher.publish_parse(doc.job_id)
    wait_for(lambda: worker.log().count("reason=storage_identity") >= 3, 60, what="the refusals")
    row = job_row(world.engine, doc.job_id)
    assert (row["status"], row["attempt_count"]) == ("queued", 0)
    assert os.listdir(empty) == []


def test_malformed_messages_are_rejected_without_effect(world, infra, celery_processes, add, settings):
    worker = start_worker(infra, "w1", [infra.parse_queue])
    celery_processes.append(worker)
    worker.wait_ready()
    doc = add()
    app = make_celery(settings)
    for kwargs in ({"job_id": str(doc.job_id).upper()}, {"job_id": str(doc.job_id), "path": "/etc/passwd"}, {}):
        app.send_task(PARSE_TASK, kwargs=kwargs, queue=infra.parse_queue)
    app.send_task(PARSE_TASK, args=[str(doc.job_id)], queue=infra.parse_queue)
    wait_for(lambda: worker.log().count("task_rejected") >= 4, 60, what="the rejections")
    assert job_row(world.engine, doc.job_id)["attempt_count"] == 0
