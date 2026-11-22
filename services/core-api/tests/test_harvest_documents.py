"""Harvested documents (M6): persistence, dedup, quotas, versions, supersession, index hand-off, listing.

AI-side behaviour comes only from the TEST DOUBLE tests/fake_ai_service.py.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.harvest import persist
from app.harvest.persist import MissingStatus, Outcome
from app.indexing.client import AiIndexClient, validate_capabilities
from app.harvest.wikipedia import PageInfo
from app.indexing.payload import request_for
from app.indexing.pipeline import IndexContext, run_index_job
from app.ingestion import jobs
from app.ingestion.jobs import CompletionStatus, RetryRefused
from app.services.documents import list_documents
from tests.api_support import DOCUMENT_ITEM_KEYS
from tests.index_writer import persist as persist_index
from tests.fake_ai_service import TOKEN, FakeAiService
from tests.harvest_support import (
    PARAGRAPH,
    claimed_run,
    fetched,
    harvest_settings,
    index_job_of,
    index_now,
    info,
    opt_in,
    versions_of,
    wikipedia_source,
)

TITLE = "Chemical equilibrium"


class Env:
    def __init__(self, world, quota: int = 20) -> None:
        self.world = world
        self.owner = world.user("professor")
        self.course = world.course(self.owner)
        self.source = wikipedia_source(world)
        opt_in(world, self.course, self.source, quota=quota)
        self.factory = sessionmaker(bind=world.engine)
        self.settings = harvest_settings()
        self.run = claimed_run(self.factory, self.settings, self.course, self.source)

    def persist(self, page: int, rev: int, *, title: str = TITLE, extra: str = "", body: str = PARAGRAPH):
        return persist.persist_revision(
            self.factory, self.settings, self.run, info(page, rev, title), fetched(page, rev, title, body, extra), None
        )

    def versions(self, page: int = 7):
        return versions_of(self.world, page, self.course)

    def listing(self):
        return list_documents(self.factory, self.owner, self.course)

    def resource(self, page: int = 7):
        return self.world.rows("SELECT * FROM external_resources WHERE course_id = :c AND page_id = :p", c=self.course, p=page)[0]

    def count(self, sql: str) -> int:
        return self.world.scalar(sql.replace(":c", f"'{self.course}'"))


@pytest.fixture
def env(world):
    return Env(world)


def test_an_accepted_revision_is_persisted_atomically_with_provenance_and_an_index_job(env):
    assert env.persist(7, 42) is Outcome.ACCEPTED
    (version,) = env.versions()
    document = env.world.rows("SELECT * FROM documents WHERE id = :d", d=version["id"])[0]
    assert (document["origin"], document["status"], document["kind"], document["storage_key"]) == ("external", "pending", "article", None)
    assert document["url"] == "https://en.wikipedia.org/wiki/Chemical_equilibrium" and document["licence"] == "CC BY-SA 4.0"
    assert document["course_id"] == env.course and document["source_id"] == env.source and document["page_count"] is None
    provenance = env.world.rows("SELECT * FROM external_document_versions WHERE document_id = :d", d=version["id"])[0]
    assert provenance["revision_id"] == 42 and provenance["page_id"] == 7 and provenance["version"] == 1
    assert provenance["revision_url"] == "https://en.wikipedia.org/w/index.php?oldid=42"
    assert provenance["licence_url"] == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert "Wikipedia contributors" in provenance["attribution"] and provenance["fetched_at"] is not None
    assert provenance["content_sha256"] == document["content_hash"] == fetched(7, 42, TITLE).content_sha256
    locators = [r["locator"] for r in env.world.rows("SELECT locator FROM document_sections WHERE document_id = :d ORDER BY ordinal", d=version["id"])]
    assert locators == [{"anchor": None, "heading": None, "level": 0}, {"anchor": "History", "heading": "History", "level": 2}]
    job = index_job_of(env.world, version["id"])
    assert job["status"] == "queued" and job["attempt_count"] == 0
    assert env.count("SELECT count(*) FROM ingestion_jobs j JOIN documents d ON d.id = j.document_id WHERE d.course_id = :c AND j.kind = 'parse'") == 0
    with env.factory() as session:
        request = request_for(session, version["id"])
        again = request_for(session, version["id"])
    body = json.loads(request.body)
    assert request.locator_scheme == "section" and request.body == again.body
    assert body["document"]["locator_scheme"] == "section"
    assert body["document"]["attribution"]["revision_id"] == 42 and body["document"]["attribution"]["provider"] == "wikipedia"
    assert body["sections"][1]["locator"] == {"anchor": "History", "heading": "History", "level": 2}
    assert "page" not in json.dumps(body["sections"])


def test_unchanged_text_under_a_new_revision_creates_no_document_or_job(env):
    env.persist(7, 42)
    assert env.persist(7, 43) is Outcome.UNCHANGED
    assert len(env.versions()) == 1
    assert env.count("SELECT count(*) FROM ingestion_jobs j JOIN documents d ON d.id = j.document_id WHERE d.course_id = :c") == 1
    assert env.resource()["last_revision_id"] == 43


def test_duplicate_content_is_tracked_not_stored_twice(env):
    env.persist(7, 42)
    assert env.persist(8, 80) is Outcome.DUPLICATE  # another page, identical normalized text
    duplicate = env.resource(8)
    assert duplicate["state"] == "duplicate" and duplicate["duplicate_of_id"] == env.resource(7)["id"]
    assert duplicate["current_document_id"] is None and duplicate["last_revision_id"] == 80
    assert env.count("SELECT count(*) FROM documents WHERE course_id = :c") == 1
    # The duplicate is refreshed later with its own text: it becomes its own document.
    assert env.persist(8, 81, extra=" Diverged.") is Outcome.NEW_VERSION
    assert env.resource(8)["state"] == "accepted"


def _orphaned(env, page: int) -> bool:
    return env.world.scalar(
        f"SELECT {persist.ORPHANED_DUPLICATE_SQL} FROM external_resources r WHERE r.course_id = :c AND r.page_id = :p",
        c=env.course, p=page,
    )  # fmt: skip


def test_deduplication_sees_the_visible_indexed_version_while_a_newer_one_is_pending(env):
    """Indexed X, pending Y on resource A; a second resource with content X must not become a second visible X."""
    env.persist(7, 42)  # content X
    (x,) = env.versions()
    assert index_now(env.factory, env.world, x["id"]) is CompletionStatus.COMPLETED
    assert env.persist(7, 50, extra=" Revised.") is Outcome.NEW_VERSION  # Y pending; the resource row now points at Y
    assert env.resource(7)["content_sha256"] != x_hash(env, x["id"])  # the old comparison would have missed X here
    assert env.persist(8, 80) is Outcome.DUPLICATE  # second resource, content X
    duplicate = env.resource(8)
    assert duplicate["state"] == "duplicate" and duplicate["duplicate_of_id"] == env.resource(7)["id"]
    assert env.count("SELECT count(*) FROM documents WHERE course_id = :c") == 2  # only resource 7's two versions
    assert not _orphaned(env, 8)  # X is still indexed and visible: the duplicate stays a duplicate


def x_hash(env, document_id) -> str:
    return env.world.scalar("SELECT content_sha256 FROM external_document_versions WHERE document_id = :d", d=document_id)


def test_a_pending_version_also_holds_its_content_for_deduplication(env):
    assert env.persist(7, 42) is Outcome.ACCEPTED  # pending, never indexed
    assert env.persist(8, 80) is Outcome.DUPLICATE
    assert env.count("SELECT count(*) FROM documents WHERE course_id = :c") == 1


def test_a_duplicate_is_decided_again_after_the_content_it_duplicates_is_superseded(env):
    env.persist(7, 42)
    (x,) = env.versions()
    index_now(env.factory, env.world, x["id"])
    env.persist(7, 50, extra=" Revised.")
    assert env.persist(8, 80) is Outcome.DUPLICATE
    v1, v2 = env.versions()
    assert index_now(env.factory, env.world, v2["id"]) is CompletionStatus.COMPLETED  # X becomes stale
    assert _orphaned(env, 8)  # nobody holds X any more
    assert env.persist(8, 80) is Outcome.NEW_VERSION  # same revision as before: still reconsidered and accepted
    assert env.resource(8)["state"] == "accepted" and len(env.versions(8)) == 1
    # Immutable history: resource 7's versions are untouched by the reconsideration.
    assert [(v["status"], v["stale_reason"]) for v in env.versions()] == [("stale", "superseded"), ("indexed", None)]


def test_a_duplicate_is_decided_again_after_the_page_it_duplicates_is_confirmed_missing(env):
    env.persist(7, 42)
    (x,) = env.versions()
    index_now(env.factory, env.world, x["id"])
    assert env.persist(8, 80) is Outcome.DUPLICATE
    assert not _orphaned(env, 8)
    resource = env.resource(7)["id"]
    gone = PageInfo(page_id=7, present=False)
    assert persist.record_gone(env.factory, env.settings, env.run, resource, gone) is MissingStatus.OBSERVED
    env.world.execute("UPDATE external_resources SET missing_since = now() - interval '2 days' WHERE id = :r", r=resource)
    assert persist.record_gone(env.factory, env.settings, env.run, resource, gone) is MissingStatus.CONFIRMED
    assert _orphaned(env, 8)  # the indexed version is stale: the duplicate must be reconsidered
    assert env.persist(8, 80) is Outcome.NEW_VERSION


def test_course_quota_is_enforced(world):
    env = Env(world, quota=1)
    assert env.persist(7, 42) is Outcome.ACCEPTED
    assert env.persist(9, 90, title="Reaction quotient") is Outcome.QUOTA
    assert env.count("SELECT count(*) FROM external_resources WHERE course_id = :c") == 1


def test_concurrent_persistence_of_one_page_creates_one_document(env):
    barrier = threading.Barrier(4)

    def attempt(_):
        barrier.wait()
        return env.persist(7, 42)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, range(4)))
    assert outcomes.count(Outcome.ACCEPTED) == 1 and set(outcomes) <= {Outcome.ACCEPTED, Outcome.UNCHANGED}
    assert len(env.versions()) == 1


def test_a_changed_page_gets_a_new_version_and_switches_only_once_indexed(env):
    env.persist(7, 42)
    (v1,) = env.versions()
    assert index_now(env.factory, env.world, v1["id"]) is CompletionStatus.COMPLETED
    with env.factory() as session:
        v1_digest = request_for(session, v1["id"]).content_digest
    assert env.persist(7, 50, extra=" Revised.") is Outcome.NEW_VERSION
    v1, v2 = env.versions()
    assert (v1["status"], v2["status"], v2["version"]) == ("indexed", "pending", 2)
    assert [(d.id, d.status) for d in env.listing()] == [(v1["id"], "ready")]  # replacement hidden until indexed
    assert env.persist(7, 51, extra=" Revised again.") is Outcome.PENDING  # one pending version at a time
    assert len(env.versions()) == 2
    assert index_now(env.factory, env.world, v2["id"]) is CompletionStatus.COMPLETED
    v1, v2 = env.versions()
    assert (v1["status"], v1["stale_reason"], v2["status"]) == ("stale", "superseded", "indexed")
    assert [(d.id, d.status) for d in env.listing()] == [(v2["id"], "ready")]
    with env.factory() as session:
        assert request_for(session, v1["id"]).content_digest == v1_digest  # the stale version's payload is untouched


def test_external_completion_fails_closed_while_inactive_and_retry_index_is_fileless(env):
    env.persist(7, 42)
    (v1,) = env.versions()
    job = index_job_of(env.world, v1["id"])
    with env.factory() as session:
        claim = jobs.claim_index(session, job["id"], 600)
        session.commit()
        digest = request_for(session, v1["id"]).content_digest
    env.world.execute("UPDATE sources SET enabled = false WHERE id = :s", s=env.source)
    assert jobs.complete_index(env.factory, claim, digest, 1) is CompletionStatus.EXTERNAL_INACTIVE
    assert env.versions()[0]["status"] == "failed"
    env.world.execute("UPDATE sources SET enabled = true WHERE id = :s", s=env.source)
    new_job = jobs.retry_index(env.factory, v1["id"])  # no parse job exists, none is invented
    assert env.count("SELECT count(*) FROM ingestion_jobs j JOIN documents d ON d.id = j.document_id WHERE d.course_id = :c AND j.kind = 'parse'") == 0
    assert index_job_of(env.world, v1["id"])["id"] == new_job
    assert index_now(env.factory, env.world, v1["id"]) is CompletionStatus.COMPLETED


def test_retry_index_refuses_a_version_that_is_no_longer_current(env):
    env.persist(7, 42)
    (v1,) = env.versions()
    env.world.execute("UPDATE ingestion_jobs SET status = 'failed', finished_at = now() WHERE document_id = :d", d=v1["id"])
    env.world.execute("UPDATE documents SET status = 'failed' WHERE id = :d", d=v1["id"])
    assert env.persist(7, 50, extra=" Revised.") is Outcome.NEW_VERSION  # a failed version does not block
    with pytest.raises(RetryRefused, match="newer version"):
        jobs.retry_index(env.factory, v1["id"])


def test_a_missing_page_is_stale_only_after_a_confirmed_second_observation(env):
    env.persist(7, 42)
    (v1,) = env.versions()
    index_now(env.factory, env.world, v1["id"])
    resource = env.resource()["id"]
    gone = info(7, None, present=False)
    assert persist.record_gone(env.factory, env.settings, env.run, resource, gone) is MissingStatus.OBSERVED
    assert persist.record_gone(env.factory, env.settings, env.run, resource, gone) is MissingStatus.WAITING
    assert env.versions()[0]["status"] == "indexed"
    # The page is back meanwhile: the observation is cleared.
    persist.record_checked(env.factory, env.settings, env.run, resource, present=True)
    assert env.resource()["missing_checks"] == 0
    assert persist.record_gone(env.factory, env.settings, env.run, resource, gone) is MissingStatus.OBSERVED
    env.world.execute("UPDATE external_resources SET missing_since = now() - interval '2 days' WHERE id = :r", r=resource)
    assert persist.record_gone(env.factory, env.settings, env.run, resource, gone) is MissingStatus.CONFIRMED
    (v1,) = env.versions()
    assert (v1["status"], v1["stale_reason"]) == ("stale", "missing")
    assert [o["result"] for o in v1["stale_evidence"]["observations"]] == ["missing", "missing"]
    assert env.resource()["state"] == "missing"
    assert env.listing() == []  # stale harvested versions are hidden
    assert env.count("SELECT count(*) FROM documents WHERE course_id = :c") == 1  # nothing deleted


def test_a_pending_version_of_a_missing_page_is_never_indexed(env):
    env.persist(7, 42)
    (v1,) = env.versions()
    job = index_job_of(env.world, v1["id"])
    with env.factory() as session:
        claim = jobs.claim_index(session, job["id"], 600)
        session.commit()
        digest = request_for(session, v1["id"]).content_digest
    env.world.execute("UPDATE external_resources SET state = 'missing' WHERE page_id = 7 AND course_id = :c", c=env.course)
    assert jobs.complete_index(env.factory, claim, digest, 1) is CompletionStatus.SUPERSEDED
    assert env.versions()[0]["status"] == "stale"


def _class_document(world, course) -> uuid.UUID:
    document = world.document(course, origin="class", title="Lecture.pdf", status="pending", page_count=1)
    world.execute(
        "INSERT INTO document_sections (document_id, ordinal, text, locator) VALUES (:d, 0, 'Le Chatelier', '{\"page\": 1}')",
        d=document,
    )
    world.execute("INSERT INTO ingestion_jobs (document_id, kind) VALUES (:d, 'index')", d=document)
    return document


def test_class_indexing_marks_the_course_dirty_but_external_indexing_never_does(env):
    generation = "SELECT dirty_generation FROM harvest_course_settings WHERE course_id = :c"
    env.persist(7, 42)
    (v1,) = env.versions()
    assert index_now(env.factory, env.world, v1["id"]) is CompletionStatus.COMPLETED
    assert env.count(generation) == 0  # no recursive harvesting
    document = _class_document(env.world, env.course)
    assert index_now(env.factory, env.world, document) is CompletionStatus.COMPLETED
    assert env.count(generation) == 1


@pytest.fixture
def fake(world):
    service = FakeAiService(persist=lambda body: persist_index(world.engine, body)).start()
    yield service
    service.stop()


def _index_ctx(env, fake):
    settings = harvest_settings(
        indexing_enabled=True, ai_service_url=fake.url, ai_service_token=TOKEN, ai_probe_cache_seconds=0,
        ai_connect_timeout_seconds=2, ai_read_timeout_seconds=5, ai_request_deadline_seconds=10, ai_probe_timeout_seconds=2,
    )  # fmt: skip
    return IndexContext(session_factory=env.factory, settings=settings, client=AiIndexClient(settings))


def test_section_documents_wait_for_advertised_support_without_consuming_attempts(env, fake):
    ctx = _index_ctx(env, fake)
    env.persist(7, 42)
    (v1,) = env.versions()
    external_job = index_job_of(env.world, v1["id"])["id"]
    class_document = _class_document(env.world, env.course)
    class_job = index_job_of(env.world, class_document)["id"]
    result = run_index_job(external_job, ctx)  # an M4 service: page locators only
    assert (result.outcome, result.code) == ("deferred", "section_locators_unsupported")
    row = env.world.rows("SELECT status, attempt_count, available_at > now() AS later, last_dispatched_at FROM ingestion_jobs WHERE id = :j", j=external_job)[0]
    assert (row["status"], row["attempt_count"], row["later"], row["last_dispatched_at"]) == ("queued", 0, True, None)
    assert run_index_job(class_job, ctx).outcome == "succeeded"  # PDF jobs are not held back
    assert fake.unadvertised_section_requests == 0 and len(fake.index_requests()) == 1
    fake.locators = ["page", "section"]
    env.world.execute("UPDATE ingestion_jobs SET available_at = now() WHERE id = :j", j=external_job)
    assert run_index_job(external_job, ctx).outcome == "succeeded"
    assert json.loads(fake.index_requests()[-1].body)["document"]["locator_scheme"] == "section"
    assert env.versions()[0]["status"] == "indexed"


def test_external_jobs_of_disabled_sources_or_opted_out_courses_are_deferred(env, fake):
    fake.locators = ["page", "section"]
    ctx = _index_ctx(env, fake)
    env.persist(7, 42)
    job = index_job_of(env.world, env.versions()[0]["id"])["id"]
    env.world.execute("UPDATE harvest_course_settings SET enabled = false WHERE course_id = :c", c=env.course)
    assert run_index_job(job, ctx).code == "course_not_opted_in"
    env.world.execute("UPDATE harvest_course_settings SET enabled = true WHERE course_id = :c", c=env.course)
    env.world.execute("UPDATE sources SET enabled = false WHERE id = :s", s=env.source)
    env.world.execute("UPDATE ingestion_jobs SET available_at = now() WHERE id = :j", j=job)
    assert run_index_job(job, ctx).code == "source_disabled"
    assert env.world.scalar("SELECT attempt_count FROM ingestion_jobs WHERE id = :j", j=job) == 0
    assert fake.index_requests() == []


def test_capabilities_stay_backward_compatible():
    from app.indexing.client import HttpResponse
    from app.indexing.payload import PROTOCOL

    base = {"protocol": PROTOCOL, "service": "ai-service", "mock": False, "completion": ["sync"], "max_request_bytes": 10}

    def caps(**extra):
        return validate_capabilities(HttpResponse(200, "application/json", json.dumps({**base, **extra}).encode()))

    assert caps().ready and caps().locators == frozenset({"page"})
    assert caps(locators=["page", "section"]).locators == frozenset({"page", "section"})
    for bad in (["section"], ["page", "page"], ["page", "pdf"], "page", [1]):
        assert caps(locators=bad).reason == "capabilities_invalid"
    assert caps(unknown=True).reason == "capabilities_invalid"


def test_listing_shows_permitted_external_material_in_the_existing_shape(env):
    legacy_source = env.world.source(kind="openstax")
    legacy = env.world.document(env.course, origin="external", title="Legacy page", status="stale", source_id=legacy_source)
    env.persist(7, 42)
    (v1,) = env.versions()
    items = {d.id: d for d in env.listing()}
    assert set(items) == {legacy, v1["id"]}
    harvested = items[v1["id"]]
    assert set(harvested.model_dump()) == DOCUMENT_ITEM_KEYS
    assert (harvested.origin, harvested.kind, harvested.status, harvested.source_name) == ("external", "article", "queued", "Wikipedia")
    assert (harvested.pages, harvested.added_by, harvested.licence) == (None, None, "CC BY-SA 4.0")
    assert items[legacy].source_name is None and items[legacy].status == "failed"  # legacy behaviour kept
    env.world.execute("UPDATE ingestion_jobs SET status = 'running', attempt_count = 1, started_at = now(), attempt_token = gen_random_uuid(), lease_expires_at = now() + interval '1 hour' WHERE document_id = :d", d=v1["id"])
    assert {d.id: d.status for d in env.listing()}[v1["id"]] == "processing"  # from its index job lifecycle
    env.world.execute("UPDATE ingestion_jobs SET status = 'queued', attempt_token = NULL, lease_expires_at = NULL WHERE document_id = :d", d=v1["id"])
    env.world.execute("UPDATE sources SET enabled = false WHERE id = :s", s=env.source)
    assert [d.id for d in env.listing()] == [legacy]


def test_concurrent_switch_persistence_and_class_completion_do_not_deadlock(env):
    env.persist(7, 42)
    (v1,) = env.versions()
    index_now(env.factory, env.world, v1["id"])
    errors: list[BaseException] = []
    for round_number in range(3):
        env.persist(7, 100 + round_number, extra=f" Revision {round_number}.")
        pending = env.versions()[-1]
        class_document = _class_document(env.world, env.course)
        barrier = threading.Barrier(3)

        def run(action):
            barrier.wait()
            try:
                return action()
            except OperationalError as exc:  # a deadlock would surface here
                errors.append(exc)

        actions = [
            lambda: index_now(env.factory, env.world, pending["id"]),
            lambda: env.persist(7, 200 + round_number, extra=f" Later {round_number}."),
            lambda: index_now(env.factory, env.world, class_document),
        ]
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(run, actions))
        statuses = [v["status"] for v in env.versions()]
        assert statuses.count("indexed") <= 1 and statuses.count("pending") <= 1
    assert errors == []
    with env.world.engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM (SELECT external_resource_id FROM documents WHERE status = 'indexed' "
            "AND external_resource_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1) x")).scalar_one() == 0  # fmt: skip


def test_a_failed_replacement_can_be_retried_and_then_switches(env):
    env.persist(7, 42)
    (v1,) = env.versions()
    index_now(env.factory, env.world, v1["id"])
    env.persist(7, 50, extra=" Revised.")
    v2 = env.versions()[1]
    job = index_job_of(env.world, v2["id"])
    with env.factory() as session:
        claim = jobs.claim_index(session, job["id"], 600)
        session.commit()
    jobs.record_failure(env.factory, claim, jobs.Failure("index_rejected", "test", permanent=True), env.settings)
    assert [v["status"] for v in env.versions()] == ["indexed", "failed"]
    assert [d.id for d in env.listing()] == [v1["id"]]  # the failed replacement stays hidden, v1 visible
    jobs.retry_index(env.factory, v2["id"])
    assert index_now(env.factory, env.world, v2["id"]) is CompletionStatus.COMPLETED
    assert [(v["status"], v["stale_reason"]) for v in env.versions()] == [("stale", "superseded"), ("indexed", None)]


def test_a_superseded_failed_version_holds_no_content_but_the_current_failed_one_does(env):
    env.persist(7, 42)  # content X
    (x,) = env.versions()
    env.world.execute("UPDATE documents SET status = 'failed' WHERE id = :d", d=x["id"])
    assert env.persist(8, 80) is Outcome.DUPLICATE  # X is failed but still current and retryable: it holds X
    assert env.persist(7, 50, extra=" Revised.") is Outcome.NEW_VERSION  # Y; the failed X is superseded
    assert _orphaned(env, 8)  # nothing holds X any more
    assert env.persist(9, 90) is Outcome.ACCEPTED  # a third resource with content X is harvested


def test_provider_retrieval_switches_external_versions_and_honours_source_disable(env):
    env.persist(7, 42)
    first = env.versions()[0]["id"]
    sql = "SELECT DISTINCT document_id FROM retrieve_index_chunks(:course, ARRAY['external'])"
    def documents():
        return [r["document_id"] for r in env.world.rows(sql, course=env.course)]
    assert documents() == []
    assert index_now(env.factory, env.world, first) is CompletionStatus.COMPLETED
    assert documents() == [first]
    env.persist(7, 51, extra=" A different version.")
    second = env.versions()[1]["id"]
    assert documents() == [first]
    assert index_now(env.factory, env.world, second) is CompletionStatus.COMPLETED
    assert documents() == [second]
    env.world.execute("UPDATE harvest_course_settings SET enabled=false WHERE course_id=:id", id=env.course)
    assert documents() == [second]
    env.world.execute("UPDATE sources SET enabled=false WHERE id=:id", id=env.source)
    assert documents() == []
