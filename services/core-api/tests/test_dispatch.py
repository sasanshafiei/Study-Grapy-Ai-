"""Publishing parse jobs through a real (disposable) Redis: reservations, sweep, bounds, outages.

Messages are inspected in Redis directly (the queue is a list named after the
queue; each test owns uniquely named queues).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.orm import sessionmaker

from app.celery_app import PARSE_TASK, make_celery
from app.ingestion import dispatch
from app.ingestion.dispatch import CeleryPublisher, PublishError
from app.storage import LocalFileStorage
from tests.api_support import RecordingPublisher
from tests.pdf_samples import make_pdf
from tests.worker_support import add_stored_document, job_row


@pytest.fixture
def owner(world):
    return world.user("professor")


@pytest.fixture
def course(world, owner):
    return world.course(owner)


@pytest.fixture
def factory(world):
    return sessionmaker(bind=world.engine)


@pytest.fixture
def settings(infra):
    return infra.settings(publish_retry_delay_seconds=30, redispatch_after_seconds=300)


@pytest.fixture
def publisher(settings):
    return CeleryPublisher(make_celery(settings), settings)


@pytest.fixture
def redis_client(disposable_redis):
    client = disposable_redis.client()
    yield client
    client.close()


@pytest.fixture
def add(world, owner, course, infra):
    store = LocalFileStorage(infra.upload_dir)
    counter = iter(range(10_000))

    def add(**kwargs):
        return add_stored_document(world.engine, world, store, course, owner, make_pdf(f"d {next(counter)}"), **kwargs)

    return add


def _messages(redis_client, queue: str) -> list[dict]:
    return [json.loads(raw) for raw in redis_client.lrange(queue, 0, -1)]


def _job_ids(redis_client, queue: str) -> list[str]:
    result = []
    for message in _messages(redis_client, queue):
        assert message["headers"]["task"] == PARSE_TASK
        body = json.loads(__import__("base64").b64decode(message["body"]))
        args, kwargs, _ = body
        assert args == [] and set(kwargs) == {"job_id"}
        result.append(kwargs["job_id"])
    return result


# --- publisher ----------------------------------------------------------------------------------


def test_published_message_is_json_and_carries_only_the_job_id(publisher, settings, redis_client):
    job_id = uuid.uuid4()
    publisher.publish_parse(job_id)
    [message] = _messages(redis_client, settings.parse_queue)
    assert message["content-type"] == "application/json"
    assert _job_ids(redis_client, settings.parse_queue) == [str(job_id)]
    assert publisher.parse_queue_depth() == 1


class _SilentServer:
    """Accepts TCP connections and never answers (a hung broker)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted: list[socket.socket] = []
        self._stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
                self.accepted.append(conn)
            except OSError:
                continue

    def open_connections(self) -> int:
        """Connections the client has not closed: drain what it sent (e.g. AUTH), then look for EOF."""
        count = 0
        for conn in self.accepted:
            conn.setblocking(False)
            try:
                while conn.recv(65536):
                    pass  # data the client sent before closing
            except BlockingIOError:
                count += 1  # no EOF: the client still holds the connection
            except OSError:
                pass  # reset: closed
        return count

    def close(self):
        self._stop = True
        self.thread.join(2)
        for conn in self.accepted:
            conn.close()
        self.sock.close()


@pytest.mark.parametrize("failure", ["refused", "silent"])
def test_publishing_to_an_unavailable_broker_fails_within_its_bound(infra, failure):
    server = _SilentServer() if failure == "silent" else None
    if server is None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # nothing listens there now
    else:
        port = server.port
    settings = infra.settings(
        redis_url=f"redis://:x@127.0.0.1:{port}/0",
        broker_connect_timeout_seconds=1,
        broker_socket_timeout_seconds=1,
        broker_publish_attempts=2,
    )
    publisher = CeleryPublisher(make_celery(settings), settings)
    try:
        started = time.monotonic()
        with pytest.raises(PublishError):
            publisher.publish_parse(uuid.uuid4())
        elapsed = time.monotonic() - started
        assert elapsed < 2 * (1 + 2 * 1) + 1  # attempts x (connect + 2 x socket) + slack
        if server is not None:
            assert len(server.accepted) == 2  # one connection per attempt, no pool retries
            time.sleep(0.2)
            assert server.open_connections() == 0  # and every one was closed
    finally:
        if server is not None:
            server.close()


# --- reservations ---------------------------------------------------------------------------------


def test_new_job_is_published_once_and_reserved(add, factory, publisher, settings, redis_client, world):
    doc = add()
    assert dispatch.publish_new_job(factory, publisher, doc.job_id, settings) is True
    assert dispatch.publish_new_job(factory, publisher, doc.job_id, settings) is False  # already reserved
    assert _job_ids(redis_client, settings.parse_queue) == [str(doc.job_id)]
    assert job_row(world.engine, doc.job_id)["last_dispatched_at"] is not None


def test_failed_publication_releases_its_reservation_with_a_delay(add, factory, settings, world):
    doc = add()
    assert dispatch.publish_new_job(factory, RecordingPublisher(fail=True), doc.job_id, settings) is False
    row = job_row(world.engine, doc.job_id)
    assert row["last_dispatched_at"] is None and row["status"] == "queued"
    assert 29 <= (row["available_at"] - row["updated_at"]).total_seconds() <= 31  # retried soon, not in a loop


def test_release_never_overwrites_a_newer_reservation(add, factory, settings, world):
    doc = add()
    with factory() as session:
        first = dispatch.reserve_job(session, doc.job_id, settings)
        session.commit()
    world.execute(  # meanwhile: redispatch interval passed and another publisher reserved it again
        "UPDATE ingestion_jobs SET last_dispatched_at = now() + interval '1 second' WHERE id = :j", j=doc.job_id
    )
    newer = job_row(world.engine, doc.job_id)["last_dispatched_at"]
    assert dispatch.release_reservations(factory, [doc.job_id], first, settings) == 0
    assert job_row(world.engine, doc.job_id)["last_dispatched_at"] == newer


def test_concurrent_upload_and_sweep_publications_publish_each_job_once(add, factory, publisher, settings, redis_client):
    docs = [add() for _ in range(10)]
    barrier = threading.Barrier(len(docs) + 2, timeout=30)

    def upload_publish(doc):
        barrier.wait()
        return dispatch.publish_new_job(factory, publisher, doc.job_id, settings)

    def sweep():
        barrier.wait()
        return dispatch.sweep(factory, publisher, settings)

    with ThreadPoolExecutor(len(docs) + 2) as pool:
        futures = [pool.submit(upload_publish, doc) for doc in docs] + [pool.submit(sweep) for _ in range(2)]
        for future in futures:
            future.result(60)
    published = _job_ids(redis_client, settings.parse_queue)
    assert sorted(published) == sorted(str(doc.job_id) for doc in docs)  # each exactly once


# --- sweep ---------------------------------------------------------------------------------------


def test_sweep_recovers_unpublished_jobs_and_skips_others(add, factory, publisher, settings, redis_client, world):
    never = add()  # e.g. created before M3, or the API crashed before publishing
    backoff = add()
    world.execute("UPDATE ingestion_jobs SET available_at = now() + interval '1 hour' WHERE id = :j", j=backoff.job_id)
    recent = add()
    world.execute("UPDATE ingestion_jobs SET last_dispatched_at = now() WHERE id = :j", j=recent.job_id)
    lost = add()  # published long ago, message presumably lost
    world.execute("UPDATE ingestion_jobs SET last_dispatched_at = now() - interval '1 hour' WHERE id = :j", j=lost.job_id)
    exhausted = add()
    world.execute("UPDATE ingestion_jobs SET attempt_count = 3, started_at = now() WHERE id = :j", j=exhausted.job_id)
    index_doc = add()
    world.execute("UPDATE ingestion_jobs SET kind = 'index' WHERE id = :j", j=index_doc.job_id)

    result = dispatch.sweep(factory, publisher, settings)

    assert result.published == 2
    assert sorted(_job_ids(redis_client, settings.parse_queue)) == sorted([str(never.job_id), str(lost.job_id)])
    assert dispatch.sweep(factory, publisher, settings).published == 0  # nothing due any more


def test_sweep_respects_the_batch_size(add, factory, publisher, infra, redis_client):
    settings = infra.settings(dispatch_batch_size=3)
    for _ in range(5):
        add()
    assert dispatch.sweep(factory, CeleryPublisher(make_celery(settings), settings), settings).published == 3
    assert dispatch.sweep(factory, CeleryPublisher(make_celery(settings), settings), settings).published == 2


def test_sweep_does_not_add_to_a_full_queue(add, factory, publisher, infra, world):
    settings = infra.settings(dispatch_max_queue_depth=2)
    publisher = CeleryPublisher(make_celery(settings), settings)
    for _ in range(2):
        publisher.publish_parse(uuid.uuid4())
    doc = add()
    assert dispatch.sweep(factory, publisher, settings).skipped == "queue_full"
    assert job_row(world.engine, doc.job_id)["last_dispatched_at"] is None


def test_sweep_with_the_broker_down_reserves_nothing(add, factory, settings, world):
    doc = add()
    assert dispatch.sweep(factory, RecordingPublisher(fail=True), settings).skipped == "broker_unavailable"
    assert job_row(world.engine, doc.job_id)["last_dispatched_at"] is None


def test_sweep_stops_at_the_first_publish_failure_and_releases_the_rest(add, factory, settings, world):
    docs = [add() for _ in range(4)]

    class FailsAfterOne(RecordingPublisher):
        def publish_parse(self, job_id):
            if self.published:
                raise PublishError("broker went away")
            super().publish_parse(job_id)

    publisher = FailsAfterOne()
    result = dispatch.sweep(factory, publisher, settings)
    assert (result.published, result.released) == (1, 3)
    rows = [job_row(world.engine, d.job_id) for d in docs]
    assert sum(r["last_dispatched_at"] is not None for r in rows) == 1


def test_upload_during_a_broker_outage_is_accepted_and_later_published(make_api, world, infra, redis_client):
    owner = world.user("professor")
    course = world.course(owner)
    settings = infra.settings(
        redis_url="redis://:x@sg-no-such-broker.invalid:6379/0", broker_connect_timeout_seconds=1,
        broker_socket_timeout_seconds=1, publish_retry_delay_seconds=0,
    )  # fmt: skip
    down = CeleryPublisher(make_celery(settings), settings)
    api = make_api(publisher=down)
    api.as_user(owner)

    started = time.monotonic()
    response = api.upload(course, make_pdf("during outage"))
    assert response.status_code == 202, response.text
    assert time.monotonic() - started < 15
    job_id = response.json()["job_id"]
    row = job_row(world.engine, uuid.UUID(job_id))
    assert (row["status"], row["last_dispatched_at"]) == ("queued", None)
    # The API's publish-retry delay (30 s by default) keeps the sweep from hammering the broker...
    assert 25 <= (row["available_at"] - row["updated_at"]).total_seconds() <= 35

    up_settings = infra.settings(publish_retry_delay_seconds=0)
    up = CeleryPublisher(make_celery(up_settings), up_settings)
    factory = sessionmaker(bind=world.engine)
    assert dispatch.sweep(factory, up, up_settings).published == 0
    # ...and once it has passed (simulated here), the next sweep publishes the job.
    world.execute("UPDATE ingestion_jobs SET available_at = now() WHERE id = :j", j=job_id)
    assert dispatch.sweep(factory, up, up_settings).published == 1
    assert _job_ids(redis_client, infra.parse_queue) == [job_id]
