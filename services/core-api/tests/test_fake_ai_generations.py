"""The test double enforces the proposal's generation rules (docs/ai-indexing-integration.md §6, rules 5 and 6).

Reviewer finding: the fake once committed three requests for one document (the
original, changed content under a new key, another course). Without these rules in the
double, no test could notice a CE-side regression that replaced or duplicated a
generation. The double stays a double: nothing here shows the real AI service behaves so.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from app.config import Settings
from app.indexing.client import AiIndexClient, Outcome
from app.indexing.payload import build_request
from tests.fake_ai_service import TOKEN, FakeAiService, Fault


@pytest.fixture
def fake():
    service = FakeAiService().start()
    yield service
    service.stop()


def client_for(fake) -> AiIndexClient:
    return AiIndexClient(
        Settings(
            ai_service_url=fake.url, ai_service_token=TOKEN, indexing_enabled=True,
            ai_connect_timeout_seconds=2, ai_read_timeout_seconds=5, ai_request_deadline_seconds=10,
            ai_probe_timeout_seconds=2,
        )  # fmt: skip
    )


def make(document_id, course_id, text="alpha"):
    return build_request(
        {"id": document_id, "course_id": course_id, "origin": "class", "kind": None, "title": "T", "url": None,
         "licence": None, "language": "en", "content_hash": None, "page_count": 1},
        [{"ordinal": 0, "text": text, "locator": {"page": 1}}],
    )  # fmt: skip


def test_changed_content_for_a_committed_document_is_a_generation_conflict(fake):
    client, document, course = client_for(fake), uuid.uuid4(), uuid.uuid4()
    original, changed = make(document, course), make(document, course, "different text")
    assert original.idempotency_key != changed.idempotency_key
    assert client.index(original).outcome is Outcome.INDEXED
    result = client.index(changed)
    assert (result.outcome, result.code, result.detail) == (Outcome.REJECTED, "index_rejected", "generation_conflict")
    assert fake.commit_count == 1
    assert [c.key for c in fake.commits_for_course(course)] == [original.idempotency_key]


def test_another_course_for_a_committed_document_is_a_course_mismatch(fake):
    client, document = client_for(fake), uuid.uuid4()
    course, other = uuid.uuid4(), uuid.uuid4()
    assert client.index(make(document, course)).outcome is Outcome.INDEXED
    result = client.index(make(document, other))
    assert (result.outcome, result.detail) == (Outcome.REJECTED, "course_mismatch")
    result = client.index(make(document, other, "different text"))  # both differ: still the course
    assert (result.outcome, result.detail) == (Outcome.REJECTED, "course_mismatch")
    assert fake.commit_count == 1 and fake.commits_for_course(other) == []


def test_the_original_key_still_replays_after_refusals(fake):
    client, document, course = client_for(fake), uuid.uuid4(), uuid.uuid4()
    original = make(document, course)
    assert client.index(original).replayed is False
    assert client.index(make(document, course, "x")).outcome is Outcome.REJECTED
    replay = client.index(original)
    assert (replay.outcome, replay.replayed) == (Outcome.INDEXED, True)
    assert fake.commit_count == 1


def test_lost_response_replay_still_works_and_later_changes_still_conflict(fake):
    client, document, course = client_for(fake), uuid.uuid4(), uuid.uuid4()
    original = make(document, course)
    fake.faults.append(Fault("lose_after_commit"))
    assert client.index(original).code == "ai_connection_lost"
    assert fake.commit_count == 1  # the commit happened although nobody heard
    assert client.index(make(document, course, "changed")).outcome is Outcome.REJECTED
    replay = client.index(original)
    assert (replay.outcome, replay.replayed) == (Outcome.INDEXED, True)
    assert fake.commit_count == 1


def test_other_documents_are_unaffected(fake):
    client, course = client_for(fake), uuid.uuid4()
    assert client.index(make(uuid.uuid4(), course)).outcome is Outcome.INDEXED
    assert client.index(make(uuid.uuid4(), course)).outcome is Outcome.INDEXED  # same text, another document
    assert fake.commit_count == 2


def _race(fake, requests):
    results = [None] * len(requests)
    barrier = threading.Barrier(len(requests))

    def run(i):
        client = client_for(fake)
        barrier.wait(10)
        results[i] = client.index(requests[i])

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(requests))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


@pytest.mark.parametrize("round_", range(3))
def test_concurrent_requests_with_different_keys_commit_exactly_one_generation(fake, round_):
    document, course = uuid.uuid4(), uuid.uuid4()
    requests = [make(document, course, f"text {i}") for i in range(8)]
    results = _race(fake, requests)
    indexed = [r for r in results if r.outcome is Outcome.INDEXED]
    assert len(indexed) == 1 and indexed[0].replayed is False
    assert all((r.outcome, r.detail) == (Outcome.REJECTED, "generation_conflict") for r in results if r not in indexed)
    assert fake.commit_count == 1 and len(fake.generations) == 1


def test_concurrent_courses_and_replays_commit_exactly_one_generation(fake):
    document = uuid.uuid4()
    courses = [uuid.uuid4() for _ in range(3)]
    requests = [make(document, c, f"text {i}") for i, c in enumerate(courses) for _ in range(2)]
    results = _race(fake, requests)
    winners = [r for r in results if r.outcome is Outcome.INDEXED]
    # the winner's own key may be sent twice: both answers are confirmations (one of them a replay)
    assert 1 <= len(winners) <= 2 and fake.commit_count == 1
    assert sorted(r.replayed for r in winners) in ([False], [False, True])
    assert {r.detail for r in results if r.outcome is Outcome.REJECTED} <= {"course_mismatch", "generation_conflict"}
