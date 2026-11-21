"""Harvest run state (M6): budgets, scheduling races, dirty generations, fencing, yield vs failure, recovery."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.harvest import budget, maintenance, runs
from tests.harvest_support import claimed_run, harvest_settings, opt_in, run_row, wikipedia_source


@pytest.fixture
def env(world):
    course = world.course(world.user("professor"))
    source = wikipedia_source(world)
    opt_in(world, course, source)
    return world, course, source, sessionmaker(bind=world.engine), harvest_settings()


# --- budgets -----------------------------------------------------------------------------------


def test_budget_reservations_never_exceed_the_window_even_concurrently(world):
    source = wikipedia_source(world, max_requests=7)
    factory = sessionmaker(bind=world.engine)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: budget.reserve(factory, source), range(20)))
    assert sum(r.granted for r in results) == 7
    refused = [r for r in results if not r.granted]
    assert all(0 < r.wait_seconds <= 60 for r in refused)
    assert world.scalar("SELECT used_requests FROM harvest_provider_budgets WHERE source_id = :s", s=source) == 7


def test_window_restarts_and_spacing_uses_database_time(world):
    source = wikipedia_source(world, max_requests=1, min_interval_ms=0)
    factory = sessionmaker(bind=world.engine)
    assert budget.reserve(factory, source).granted
    assert not budget.reserve(factory, source).granted
    world.execute("UPDATE harvest_provider_budgets SET window_started_at = now() - interval '61 seconds' WHERE source_id = :s", s=source)
    assert budget.reserve(factory, source).granted
    world.execute("UPDATE harvest_provider_budgets SET max_requests = 100, min_interval_ms = 30000 WHERE source_id = :s", s=source)
    world.execute("UPDATE harvest_provider_budgets SET blocked_until = now() WHERE source_id = :s", s=source)
    assert budget.reserve(factory, source).granted
    spaced = budget.reserve(factory, source)  # the minimum spacing applies across callers
    assert not spaced.granted and 25 < spaced.wait_seconds <= 30


def test_retry_after_blocks_and_is_never_shortened(world):
    source = wikipedia_source(world)
    factory = sessionmaker(bind=world.engine)
    budget.block(factory, source, 120)
    budget.block(factory, source, 5)  # a shorter delay never lets a request through earlier
    reservation = budget.reserve(factory, source)
    assert not reservation.granted and 100 < reservation.wait_seconds <= 120


def test_retry_after_above_the_scheduling_cap_is_stored_in_full(world):
    """H2: the cap bounds scheduling only; the provider's blocking period is never shortened."""
    source = wikipedia_source(world)
    factory = sessionmaker(bind=world.engine)
    assert budget.block(factory, source, 172_800) is True  # two days; HARVEST_RETRY_AFTER_MAX_SECONDS defaults to one
    assert 172_000 < budget.blocked_for(factory, source) <= 172_800
    assert not budget.reserve(factory, source).granted
    assert budget.block(factory, source, 3600) is True  # a later, shorter delay never shortens the stored block
    assert budget.blocked_for(factory, source) > 172_000
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is True


@pytest.mark.parametrize("seconds", [budget.MAX_BLOCK_SECONDS + 1, float("inf"), float("nan"), 10**12])
def test_an_unrepresentable_delay_suspends_the_source_for_the_operator(world, seconds):
    source = wikipedia_source(world)
    factory = sessionmaker(bind=world.engine)
    assert budget.block(factory, source, seconds) is False
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is False  # gates now refuse every run


def test_concurrent_reservations_cannot_pass_a_block_longer_than_the_cap(world):
    source = wikipedia_source(world)
    factory = sessionmaker(bind=world.engine)
    budget.block(factory, source, 90_000)
    barrier = threading.Barrier(8)

    def attempt(_):
        barrier.wait()
        return budget.reserve(factory, source)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert not any(r.granted for r in results) and all(r.wait_seconds > 89_000 for r in results)


def test_a_block_racing_reservations_never_lets_one_through_afterwards(world):
    source = wikipedia_source(world, min_interval_ms=0)
    factory = sessionmaker(bind=world.engine)
    barrier = threading.Barrier(9)

    def reserve(_):
        barrier.wait()
        return budget.reserve(factory, source).granted

    def throttle():
        barrier.wait()
        budget.block(factory, source, 100_000)

    with ThreadPoolExecutor(max_workers=9) as pool:
        blocker = pool.submit(throttle)
        list(pool.map(reserve, range(8)))
        blocker.result()
    assert budget.blocked_for(factory, source) > 99_000  # whichever reservations won, the full block stands
    assert not budget.reserve(factory, source).granted


def test_a_source_without_budget_is_refused(world):
    source = world.source(kind="wikipedia")
    with pytest.raises(budget.BudgetMissing):
        budget.reserve(sessionmaker(bind=world.engine), source)


# --- scheduling ---------------------------------------------------------------------------------


def test_scheduler_creates_one_run_per_due_target_and_nothing_while_disabled(env):
    world, course, source, factory, settings = env
    assert runs.schedule(factory, harvest_settings(harvest_enabled=False)) == []
    assert world.scalar("SELECT count(*) FROM harvest_runs WHERE course_id = :c", c=course) == 0
    barrier = threading.Barrier(6)

    def race(_):
        barrier.wait()
        return runs.schedule(factory, settings)

    with ThreadPoolExecutor(max_workers=6) as pool:
        created = [run for result in pool.map(race, range(6)) for run in result]
    assert len(created) == 1
    row = run_row(world, created[0])
    assert row["trigger"] == "refresh" and row["status"] == "queued" and row["generation"] == 0


def test_scheduler_skips_opted_out_courses_and_disabled_sources(env):
    world, course, source, factory, settings = env
    world.execute("UPDATE harvest_course_settings SET enabled = false WHERE course_id = :c", c=course)
    assert runs.schedule(factory, settings) == []
    world.execute("UPDATE harvest_course_settings SET enabled = true WHERE course_id = :c", c=course)
    world.execute("UPDATE sources SET enabled = false WHERE id = :s", s=source)
    assert runs.schedule(factory, settings) == []


def test_dirty_notification_during_a_run_is_not_lost(env):
    world, course, source, factory, settings = env
    world.execute("UPDATE harvest_targets SET next_refresh_at = now() + interval '7 days' WHERE course_id = :c", c=course)
    assert runs.schedule(factory, settings) == []  # not dirty, not due
    world.execute("UPDATE harvest_course_settings SET dirty_generation = 1 WHERE course_id = :c", c=course)
    (first,) = runs.schedule(factory, settings)
    assert run_row(world, first)["generation"] == 1 and run_row(world, first)["trigger"] == "dirty"
    with factory() as session:
        claim = runs.claim(session, first, settings)
        session.commit()
    # New class material is indexed while the run is in flight.
    world.execute("UPDATE harvest_course_settings SET dirty_generation = 2 WHERE course_id = :c", c=course)
    assert runs.schedule(factory, settings) == []  # one active run per target
    assert runs.complete(factory, claim, settings) == "succeeded"
    assert world.scalar("SELECT harvested_generation FROM harvest_targets WHERE course_id = :c", c=course) == 1
    (second,) = runs.schedule(factory, settings)
    assert run_row(world, second)["generation"] == 2


# --- claims, fencing, yields, failures -------------------------------------------------------------


def test_a_stale_attempt_writes_nothing(env):
    world, course, source, factory, settings = env
    claim = claimed_run(factory, settings, course, source)
    world.execute("UPDATE harvest_runs SET started_at = now() - interval '2 hours', lease_expires_at = now() - interval '1 second' WHERE id = :id", id=claim.run_id)
    recovered = runs.recover(factory, settings)
    assert recovered.requeued == (claim.run_id,)
    claim.progress["requests"] = 99
    assert runs.save_progress(factory, claim) is False
    assert runs.yield_run(factory, claim, settings, 0, "x") == "stale"
    assert runs.fail_attempt(factory, claim, settings, "boom", permanent=False) == "stale"
    assert runs.complete(factory, claim, settings) == "stale"
    row = run_row(world, claim.run_id)
    assert row["status"] == "queued" and row["failure_count"] == 1 and row["progress"] == {}


def test_yields_use_claims_but_never_failures_and_are_finite(env):
    world, course, source, factory, settings = env
    settings = harvest_settings(harvest_run_max_claims=3)
    claim = claimed_run(factory, settings, course, source)
    for expected in ("yielded", "yielded", "exhausted"):
        claim.progress["cursor"] = expected
        assert runs.yield_run(factory, claim, settings, 0, "slice_requests") == expected
        if expected != "exhausted":
            with factory() as session:
                claim = runs.claim(session, claim.run_id, settings)
                session.commit()
            assert claim.progress["cursor"] == expected  # progress survives the yield
    row = run_row(world, claim.run_id)
    assert (row["status"], row["error_code"], row["claim_count"], row["failure_count"]) == ("failed", "work_limit_exhausted", 3, 0)


def test_failures_count_and_back_off_then_fail_the_run(env):
    world, course, source, factory, settings = env
    settings = harvest_settings(harvest_run_max_failures=2)
    claim = claimed_run(factory, settings, course, source)
    assert runs.fail_attempt(factory, claim, settings, "provider_http_500", permanent=False) == "requeued"
    row = run_row(world, claim.run_id)
    assert row["status"] == "queued" and row["failure_count"] == 1 and row["attempt_token"] is None
    world.execute("UPDATE harvest_runs SET available_at = now() WHERE id = :id", id=claim.run_id)
    with factory() as session:
        claim = runs.claim(session, claim.run_id, settings)
        session.commit()
    assert runs.fail_attempt(factory, claim, settings, "provider_http_500", permanent=False) == "failed"
    row = run_row(world, claim.run_id)
    assert (row["status"], row["failure_count"], row["claim_count"]) == ("failed", 2, 2)
    assert world.scalar("SELECT not_before > now() FROM harvest_targets WHERE course_id = :c", c=course)


def test_expired_lease_on_the_last_failure_fails_the_run(env):
    world, course, source, factory, settings = env
    settings = harvest_settings(harvest_run_max_failures=1)
    claim = claimed_run(factory, settings, course, source)
    world.execute("UPDATE harvest_runs SET started_at = now() - interval '2 hours', lease_expires_at = now() - interval '1 second' WHERE id = :id", id=claim.run_id)
    assert runs.recover(factory, settings).failed == (claim.run_id,)
    assert run_row(world, claim.run_id)["error_code"] == "lease_expired"


def test_opt_out_and_source_disable_cancel_active_runs(env):
    world, course, source, factory, settings = env
    claim = claimed_run(factory, settings, course, source)
    with factory() as session:
        assert runs.cancel_active(session, course_id=course, code="course_opted_out") == 1
        session.commit()
    assert run_row(world, claim.run_id)["status"] == "cancelled"
    assert runs.save_progress(factory, claim) is False  # fenced
    with factory() as session:
        run_id = runs.create_run(session, settings, course, source, manual=True)
        session.commit()
    with factory() as session:
        assert runs.cancel_active(session, source_id=source, code="source_disabled") == 1
        session.commit()
    assert run_row(world, run_id)["error_code"] == "source_disabled"


def test_history_cleanup_is_bounded_and_keeps_recent_runs(env):
    world, course, source, factory, settings = env
    for age in (40, 40, 40, 1):
        world.execute(
            "INSERT INTO harvest_runs (course_id, source_id, trigger, generation, max_claims, max_failures, status, "
            "finished_at) VALUES (:c, :s, 'manual', 0, 1, 1, 'succeeded', now() - :days * interval '1 day')",
            c=course, s=source, days=age,
        )  # fmt: skip
    settings = harvest_settings(harvest_history_retention_days=30, harvest_history_cleanup_batch=2)
    assert runs.cleanup_history(factory, settings) == 2
    assert runs.cleanup_history(factory, settings) == 1
    assert runs.cleanup_history(factory, settings) == 0
    assert world.scalar("SELECT count(*) FROM harvest_runs WHERE course_id = :c", c=course) == 1


def _insert_finished_run(world, course, source, run_id, status, finished_at):
    world.execute(
        "INSERT INTO harvest_runs (id, course_id, source_id, trigger, generation, max_claims, max_failures, "
        "status, finished_at) VALUES (:id, :c, :s, 'manual', 0, 1, 1, :status, :finished_at)",
        id=run_id, c=course, s=source, status=status, finished_at=finished_at,
    )


def _remaining_run_ids(world, source):
    return {row["id"] for row in world.rows("SELECT id FROM harvest_runs WHERE source_id = :s", s=source)}


def test_history_cleanup_deletes_only_the_ordered_batch(env):
    world, course, source, factory, _ = env
    settings = harvest_settings(harvest_history_retention_days=30, harvest_history_cleanup_batch=2)
    now = world.scalar("SELECT now()")
    lower, higher, oldest = sorted(uuid4() for _ in range(3))
    recent = uuid4()
    _insert_finished_run(world, course, source, oldest, "failed", now - timedelta(days=50))
    # One-row statistics can produce a nested-loop rescan of the old locking subquery.
    world.execute("ANALYZE harvest_runs")
    tied = now - timedelta(days=40)
    _insert_finished_run(world, course, source, higher, "cancelled", tied)
    _insert_finished_run(world, course, source, lower, "succeeded", tied)
    _insert_finished_run(world, course, source, recent, "succeeded", now - timedelta(days=1))

    with factory() as session:
        queued = runs.create_run(session, settings, course, source, manual=True)
        session.commit()
    other_course = world.course(world.user("professor"))
    opt_in(world, other_course, source)
    running = claimed_run(factory, settings, other_course, source).run_id
    protected = {queued, running, recent}

    assert runs.cleanup_history(factory, settings) == 2
    assert _remaining_run_ids(world, source) == protected | {higher}
    assert runs.cleanup_history(factory, settings) == 1
    assert _remaining_run_ids(world, source) == protected
    assert runs.cleanup_history(factory, settings) == 0
    assert _remaining_run_ids(world, source) == protected


def test_concurrent_history_cleanup_skips_uncommitted_victims(env):
    world, course, source, factory, _ = env
    settings = harvest_settings(harvest_history_retention_days=30, harvest_history_cleanup_batch=2)
    finished_at = world.scalar("SELECT now() - interval '40 days'")
    ids = sorted(uuid4() for _ in range(5))
    for run_id in ids:
        _insert_finished_run(world, course, source, run_id, "succeeded", finished_at)

    first_factory = sessionmaker(bind=world.engine)
    deleted = threading.Event()
    release = threading.Event()

    @event.listens_for(first_factory, "before_commit")
    def hold_first_batch(session):
        deleted.set()  # The DELETE ran, and this transaction still holds the row locks.
        assert release.wait(timeout=10), "second cleanup did not finish while the first held its locks"

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runs.cleanup_history, first_factory, settings)
        try:
            assert deleted.wait(timeout=10)
            assert runs.cleanup_history(factory, settings) == 2
            # A third connection sees only the second worker's committed deletions.
            assert _remaining_run_ids(world, source) == {ids[0], ids[1], ids[4]}
        finally:
            release.set()
        assert first.result() == 2
    assert _remaining_run_ids(world, source) == {ids[4]}


class _Publisher:
    def __init__(self) -> None:
        self.published: list = []

    def publish_harvest(self, run_id) -> None:
        self.published.append(run_id)

    def harvest_queue_depth(self) -> int:
        return 0


def test_maintenance_dispatches_only_while_enabled_and_reserves_once(env):
    world, course, source, factory, settings = env
    publisher = _Publisher()
    result = maintenance.maintain(factory, publisher, harvest_settings(harvest_enabled=False))
    assert (result.scheduled, result.published, result.skipped) == (0, 0, "harvest_disabled")
    result = maintenance.maintain(factory, publisher, settings)
    assert (result.scheduled, result.published) == (1, 1)
    result = maintenance.maintain(factory, publisher, settings)
    assert (result.scheduled, result.published) == (0, 0)  # reserved: not re-published within the interval
    assert len(publisher.published) == 1
