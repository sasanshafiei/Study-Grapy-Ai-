"""Harvest slices end to end (M6), against the disposable database and an offline fake provider.

The fake connector records every provider request; these tests assert what
would leave the system, and that nothing leaves it while a gate is closed.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.harvest import runs
from app.harvest.executor import HarvestContext, run_harvest
from app.harvest.wikipedia import ProviderError, ProviderThrottled, SearchHit
from tests.harvest_support import (
    RIGHTSINFO,
    FakeConnector,
    approve_topic,
    fetched,
    harvest_settings,
    index_now,
    info,
    opt_in,
    run_row,
    versions_of,
    wikipedia_source,
)

QUERY = "Chemical equilibrium"


class Scene:
    def __init__(self, world, **settings) -> None:
        self.world = world
        self.owner = world.user("professor", name="Rosalind Franklin")
        self.course = world.course(self.owner, code="CHEM 101")
        self.source = wikipedia_source(world)
        opt_in(world, self.course, self.source)
        approve_topic(world, self.course, QUERY)
        world.topic(self.course, "Unapproved private topic")  # never searched
        world.document(self.course, origin="class", title="Secret lecture.pdf", status="indexed")
        self.factory = sessionmaker(bind=world.engine)
        self.settings = harvest_settings(**settings)
        self.connector = FakeConnector()
        self.connector.search_results[QUERY] = [SearchHit(7, QUERY), SearchHit(9, "Photosynthesis")]
        self.connector.pages[7] = (info(7, 42, QUERY), fetched(7, 42, QUERY))
        plants = "Plants convert light into stored sugar for growth. " * 6
        self.connector.pages[9] = (info(9, 90, "Photosynthesis"), fetched(9, 90, "Photosynthesis", body=plants))
        self.calls: list[str] = []

    def ctx(self, settings=None, sleep=None) -> HarvestContext:
        def factory(language):
            self.calls.append(language)
            return self.connector

        return HarvestContext(self.factory, settings or self.settings, factory, sleep=sleep or (lambda seconds: None))

    def new_run(self):
        with self.factory() as session:
            run_id = runs.create_run(session, self.settings, self.course, self.source, manual=True)
            session.commit()
        return run_id


@pytest.fixture
def scene(world):
    return Scene(world)


def test_nothing_is_claimed_or_sent_while_harvesting_is_disabled(scene):
    run_id = scene.new_run()
    assert run_harvest(run_id, scene.ctx(harvest_settings(harvest_enabled=False))) == "harvest_disabled"
    assert scene.calls == [] and scene.connector.requests == []
    assert (run_row(scene.world, run_id)["status"], run_row(scene.world, run_id)["claim_count"]) == ("queued", 0)


def test_a_run_sends_only_the_approved_query_and_page_ids(scene):
    run_id = scene.new_run()
    assert run_harvest(run_id, scene.ctx()) == "succeeded"
    assert scene.connector.requests == [("search", QUERY), ("licence", None), ("info", (7, 9)), ("parse", 42), ("parse", 90)]
    sent = repr(scene.connector.requests)
    for private in (str(scene.course), "CHEM 101", "Secret lecture", "Unapproved", "Rosalind", "Franklin"):
        assert private not in sent
    (version,) = versions_of(scene.world, 7, scene.course)  # page 9 failed the lexical gate
    assert version["status"] == "pending" and version["revision_id"] == 42
    row = run_row(scene.world, run_id)
    assert row["status"] == "succeeded" and row["failure_count"] == 0
    assert row["progress"]["accepted"] == 1 and row["progress"]["refused"] == 1 and row["progress"]["requests"] == 5
    assert scene.world.scalar("SELECT next_refresh_at > now() + interval '6 days' FROM harvest_targets WHERE course_id = :c", c=scene.course)


def test_gates_are_rechecked_before_every_request(scene):
    run_id = scene.new_run()
    original = scene.connector.search

    def search_then_opt_out(query, limit):
        scene.world.execute("UPDATE harvest_course_settings SET enabled = false WHERE course_id = :c", c=scene.course)
        return original(query, limit)

    scene.connector.search = search_then_opt_out
    assert run_harvest(run_id, scene.ctx()) == "yielded"
    assert scene.connector.requests == [("search", QUERY)]  # nothing after the opt-out
    assert scene.world.scalar("SELECT count(*) FROM documents WHERE course_id = :c AND origin = 'external'", c=scene.course) == 0
    scene.world.execute("UPDATE harvest_runs SET available_at = now() WHERE id = :id", id=run_id)
    assert run_harvest(run_id, scene.ctx()) == "gate_closed"
    assert run_row(scene.world, run_id)["status"] == "cancelled"
    assert len(scene.connector.requests) == 1


def test_provider_throttling_yields_without_a_failure_and_blocks_the_source(scene):
    run_id = scene.new_run()
    scene.connector.fail_with = ProviderThrottled("http_429", 120)
    assert run_harvest(run_id, scene.ctx()) == "yielded"
    row = run_row(scene.world, run_id)
    assert (row["status"], row["failure_count"], row["claim_count"], row["error_code"]) == ("queued", 0, 1, "throttled_http_429")
    assert scene.world.scalar("SELECT available_at > now() + interval '100 seconds' FROM harvest_runs WHERE id = :id", id=run_id)
    assert scene.world.scalar(
        "SELECT blocked_until > now() + interval '100 seconds' FROM harvest_provider_budgets WHERE source_id = :s", s=scene.source
    )


def test_provider_errors_are_failed_attempts_transient_or_permanent(scene):
    run_id = scene.new_run()
    scene.connector.fail_with = ProviderError("http_500", retryable=True)
    assert run_harvest(run_id, scene.ctx()) == "requeued"
    assert run_row(scene.world, run_id)["failure_count"] == 1
    scene.world.execute("UPDATE harvest_runs SET available_at = now() WHERE id = :id", id=run_id)
    scene.connector.fail_with = ProviderError("http_403", "refused", retryable=False)
    assert run_harvest(run_id, scene.ctx()) == "failed"
    row = run_row(scene.world, run_id)
    assert (row["status"], row["failure_count"], row["error_code"]) == ("failed", 2, "provider_http_403")


def test_refresh_inspects_existing_resources_independently_of_search(scene):
    assert run_harvest(scene.new_run(), scene.ctx()) == "succeeded"
    (v1,) = versions_of(scene.world, 7, scene.course)
    index_now(scene.factory, scene.world, v1["id"])
    scene.connector.search_results[QUERY] = []  # the page no longer appears in search results
    scene.connector.pages[7] = (info(7, 43, QUERY), fetched(7, 43, QUERY, extra=" Updated text."))
    scene.world.execute("UPDATE external_resources SET next_check_at = now() WHERE course_id = :c", c=scene.course)
    scene.connector.requests.clear()
    assert run_harvest(scene.new_run(), scene.ctx()) == "succeeded"
    assert scene.connector.requests[:3] == [("licence", None), ("info", (7,)), ("parse", 43)]
    versions = versions_of(scene.world, 7, scene.course)
    assert [v["revision_id"] for v in versions] == [42, 43]
    # Disappearing from search results never stales anything.
    assert all(v["stale_reason"] is None for v in versions)


def test_slice_limits_yield_with_persisted_progress_and_runs_are_finite(world):
    scene = Scene(world, harvest_slice_max_requests=2, harvest_run_max_claims=10)
    run_id = scene.new_run()
    assert run_harvest(run_id, scene.ctx()) == "yielded"
    row = run_row(world, run_id)
    assert (row["claim_count"], row["failure_count"], row["progress"]["requests"]) == (1, 0, 2)  # search + licence
    outcomes = []
    for _ in range(8):
        world.execute("UPDATE harvest_runs SET available_at = now() WHERE id = :id", id=run_id)
        outcomes.append(run_harvest(run_id, scene.ctx()))
        if outcomes[-1] == "succeeded":
            break
    assert outcomes[-1] == "succeeded"
    assert run_row(world, run_id)["failure_count"] == 0
    assert len(versions_of(world, 7, scene.course)) == 1


def test_the_run_request_limit_ends_the_run(world):
    scene = Scene(world, harvest_run_max_requests=1)
    run_id = scene.new_run()
    assert run_harvest(run_id, scene.ctx()) == "succeeded"
    row = run_row(world, run_id)
    assert row["progress"]["limit_reached"] is True and row["progress"]["requests"] == 1
    assert len(scene.connector.requests) == 1


# --- H1: approval and activation are re-read right before each request --------------------------------------


def _revoke(scene):
    scene.world.execute("UPDATE harvest_queries SET status = 'revoked', revoked_at = now() WHERE course_id = :c", c=scene.course)


def _rename(scene):
    scene.world.execute("UPDATE topics SET name = 'Renamed after approval' WHERE course_id = :c AND name = :n", c=scene.course, n=QUERY)


def _opt_out(scene):
    scene.world.execute("UPDATE harvest_course_settings SET enabled = false WHERE course_id = :c", c=scene.course)


def _disable_source(scene):
    scene.world.execute("UPDATE sources SET enabled = false WHERE id = :s", s=scene.source)


def _lose_lease(scene):
    scene.world.execute(
        "UPDATE harvest_runs SET status = 'queued', attempt_token = NULL, lease_expires_at = NULL WHERE course_id = :c",
        c=scene.course,
    )


def test_a_query_revoked_after_its_search_is_not_used_for_any_further_request(scene):
    run_id = scene.new_run()
    original = scene.connector.search

    def search_then_revoke(query, limit):
        hits = original(query, limit)
        _revoke(scene)  # during the in-flight search: the response is in hand, nothing else may follow
        return hits

    scene.connector.search = search_then_revoke
    assert run_harvest(run_id, scene.ctx()) == "succeeded"
    assert scene.connector.requests == [("search", QUERY)]  # no licence, info or content request for the hits
    assert scene.world.scalar("SELECT count(*) FROM external_resources WHERE course_id = :c", c=scene.course) == 0
    assert run_row(scene.world, run_id)["progress"]["queries_withdrawn"] == 1


def test_a_topic_renamed_during_the_run_stops_the_old_text_before_the_next_request(scene):
    run_id = scene.new_run()
    original = scene.connector.search

    def search_then_rename(query, limit):
        hits = original(query, limit)
        _rename(scene)
        return hits

    scene.connector.search = search_then_rename
    assert run_harvest(run_id, scene.ctx()) == "succeeded"
    assert scene.connector.requests == [("search", QUERY)]


@pytest.mark.parametrize(
    "change, outcome",
    [(_revoke, "succeeded"), (_rename, "succeeded"), (_opt_out, "yielded"), (_disable_source, "yielded"), (_lose_lease, "stale")],
)
def test_changes_during_a_budget_wait_stop_the_request_that_waited(world, change, outcome):
    """The gates, the lease and the exact approval are checked again after the wait, right before network I/O."""
    scene = Scene(world, harvest_budget_max_wait_seconds=5)
    world.execute("UPDATE harvest_provider_budgets SET min_interval_ms = 2000 WHERE source_id = :s", s=scene.source)
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        change(scene)  # happens while this worker waits for its reservation
        world.execute("UPDATE harvest_provider_budgets SET blocked_until = now() WHERE source_id = :s", s=scene.source)

    assert run_harvest(scene.new_run(), scene.ctx(sleep=sleep)) == outcome
    assert slept  # the second request (the licence check) had to wait
    assert scene.connector.requests == [("search", QUERY)]  # the waiting request was never sent


def test_approval_changes_between_slices_never_attach_old_hits_to_another_query(world):
    scene = Scene(world, harvest_slice_max_requests=2, harvest_run_max_claims=20)
    run_id = scene.new_run()
    assert run_harvest(run_id, scene.ctx()) == "yielded"  # search + licence; the hits [7, 9] are persisted
    first = run_row(world, run_id)["progress"]
    assert first["hits"] == [7, 9] and first["query_id"] is not None
    # The approval is replaced: the old query is revoked and another topic is approved.
    _revoke(scene)
    approve_topic(world, scene.course, "Reaction rates")
    scene.connector.search_results["Reaction rates"] = [SearchHit(11, "Reaction rates")]
    scene.connector.pages[11] = (info(11, 50, "Reaction rates"), fetched(11, 50, "Reaction rates"))
    scene.connector.requests.clear()
    outcomes = []
    for _ in range(10):
        world.execute("UPDATE harvest_runs SET available_at = now() WHERE id = :id", id=run_id)
        outcomes.append(run_harvest(run_id, scene.ctx()))
        if outcomes[-1] == "succeeded":
            break
    assert outcomes[-1] == "succeeded"
    assert scene.connector.requests[0] == ("search", "Reaction rates")  # the new text, not the old hits
    assert ("info", (7, 9)) not in scene.connector.requests and ("parse", 42) not in scene.connector.requests
    assert world.scalar("SELECT count(*) FROM external_resources WHERE course_id = :c AND page_id IN (7, 9)", c=scene.course) == 0
    assert world.scalar(
        "SELECT q.query_text FROM external_resources r JOIN harvest_queries q ON q.id = r.discovered_by_query_id "
        "WHERE r.course_id = :c AND r.page_id = 11", c=scene.course
    ) == "Reaction rates"  # fmt: skip


# --- H2: the provider's full Retry-After is stored; the cap only bounds scheduling --------------------------------


def test_retry_after_above_the_cap_is_stored_in_full_while_the_run_is_rescheduled_within_the_cap(scene):
    run_id = scene.new_run()
    scene.connector.fail_with = ProviderThrottled("http_429", 172_800)  # two days; HARVEST_RETRY_AFTER_MAX_SECONDS = 1 day
    assert run_harvest(run_id, scene.ctx()) == "yielded"
    assert scene.world.scalar(
        "SELECT blocked_until > now() + interval '170000 seconds' FROM harvest_provider_budgets WHERE source_id = :s",
        s=scene.source,
    )
    assert scene.world.scalar(
        "SELECT available_at BETWEEN now() + interval '80000 seconds' AND now() + interval '90000 seconds' "
        "FROM harvest_runs WHERE id = :id", id=run_id
    )  # fmt: skip
    scene.connector.fail_with = None
    scene.connector.requests.clear()
    scene.world.execute("UPDATE harvest_runs SET available_at = now() WHERE id = :id", id=run_id)
    assert run_harvest(run_id, scene.ctx()) == "yielded"  # woken early: the budget still refuses the request
    assert scene.connector.requests == []


# --- M3: the declared licence is verified before content is accepted ---------------------------------------------


@pytest.mark.parametrize(
    "declared, code",
    [
        ({"url": "https://creativecommons.org/licenses/by-nc/4.0/", "text": "Creative Commons Attribution-NonCommercial 4.0"}, "licence_mismatch"),
        ({"url": RIGHTSINFO["url"], "text": "Creative Commons Attribution-ShareAlike 3.0 Unported License"}, "licence_mismatch"),
        ({"url": "https://creativecommons.org/licenses/by-sa/4.0/", "text": "All rights reserved"}, "licence_mismatch"),
        ({"url": RIGHTSINFO["url"]}, "licence_unverified"),
        (None, "licence_unverified"),
    ],
)
def test_a_licence_mismatch_or_ambiguity_fails_the_run_before_any_content_is_requested(scene, declared, code):
    scene.connector.rightsinfo = declared
    run_id = scene.new_run()
    assert run_harvest(run_id, scene.ctx()) == "failed"
    row = run_row(scene.world, run_id)
    assert (row["status"], row["error_code"]) == ("failed", f"provider_{code}")
    assert scene.connector.requests == [("search", QUERY), ("licence", None)]  # no info, no parse
    assert scene.world.scalar("SELECT count(*) FROM documents WHERE course_id = :c AND origin = 'external'", c=scene.course) == 0
    assert scene.world.scalar(  # a failed licence check also blocks the source for a while (non-retryable error)
        "SELECT blocked_until > now() + interval '200 seconds' FROM harvest_provider_budgets WHERE source_id = :s", s=scene.source
    )  # fmt: skip


def test_the_licence_check_is_counted_in_the_run_budget_once_per_run(scene):
    run_id = scene.new_run()
    assert run_harvest(run_id, scene.ctx()) == "succeeded"
    assert [op for op, _ in scene.connector.requests].count("licence") == 1
    assert scene.world.scalar("SELECT used_requests FROM harvest_provider_budgets WHERE source_id = :s", s=scene.source) == 5
    assert run_row(scene.world, run_id)["progress"]["licence_ok"] is True


# --- H3: duplicates are reconsidered when what they duplicate changes ------------------------------------------


def _two_identical_pages(scene):
    scene.connector.search_results[QUERY] = [SearchHit(7, QUERY), SearchHit(8, QUERY)]
    scene.connector.pages[8] = (info(8, 80, QUERY), fetched(8, 80, QUERY))  # same normalized text as page 7


def _due_again(scene):
    scene.world.execute("UPDATE external_resources SET next_check_at = now() WHERE course_id = :c", c=scene.course)


def _resource_state(scene, page):
    return scene.world.rows("SELECT state, current_document_id FROM external_resources WHERE course_id = :c AND page_id = :p", c=scene.course, p=page)[0]


def test_a_duplicate_is_reconsidered_when_the_page_it_duplicates_changes_content(scene):
    _two_identical_pages(scene)
    assert run_harvest(scene.new_run(), scene.ctx()) == "succeeded"
    assert _resource_state(scene, 8)["state"] == "duplicate"
    (v1,) = versions_of(scene.world, 7, scene.course)
    index_now(scene.factory, scene.world, v1["id"])
    # Page 7 changes; page 8's own revision (80) never changes.
    scene.connector.pages[7] = (info(7, 43, QUERY), fetched(7, 43, QUERY, extra=" Updated text."))
    _due_again(scene)
    assert run_harvest(scene.new_run(), scene.ctx()) == "succeeded"
    assert _resource_state(scene, 8)["state"] == "duplicate"  # 7's old version is still indexed and visible: it holds the text
    v1, v2 = versions_of(scene.world, 7, scene.course)
    index_now(scene.factory, scene.world, v2["id"])  # the old version turns stale; nobody holds the old text now
    scene.connector.requests.clear()
    _due_again(scene)
    assert run_harvest(scene.new_run(), scene.ctx()) == "succeeded"
    assert ("parse", 80) in scene.connector.requests  # reconsidered although revision 80 is unchanged
    assert _resource_state(scene, 8)["state"] == "accepted" and _resource_state(scene, 8)["current_document_id"] is not None


def test_a_duplicate_stays_a_duplicate_while_the_referenced_content_is_held(scene):
    _two_identical_pages(scene)
    assert run_harvest(scene.new_run(), scene.ctx()) == "succeeded"
    scene.connector.requests.clear()
    _due_again(scene)
    assert run_harvest(scene.new_run(), scene.ctx()) == "succeeded"
    assert ("parse", 80) not in scene.connector.requests  # unchanged and still held: no content request
    assert _resource_state(scene, 8)["state"] == "duplicate"
