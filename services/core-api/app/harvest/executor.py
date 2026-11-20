"""One harvest slice: claim, bounded work against the provider, persist under the lease, then yield or finish.

    HARVEST_ENABLED? gates open (course opted in, source enabled, target)?   } nothing claimed
    -> claim (claim_count + 1, token, lease; committed)
    -> refresh phase: resources of this course x source due for a check, in
       id order (cursor persisted): page info (batched), then for a changed
       revision the revision-pinned content; persist each decision
    -> (once per run, before the first unit that can accept content: one
       budgeted request verifies the provider's declared site licence)
    -> search phase: approved queries (cursor persisted): search, info for
       unknown pages, quality gates, revision-pinned content, lexical gate,
       persist; stops at the per-run candidate/accepted limits or the quota
    -> complete (target: generation, next refresh)

Before EVERY provider request: the run's request limit, the slice's request
and time limits, the gates (HARVEST_ENABLED, course opt-in, source enabled,
target present), lease ownership and - for every request that belongs to a
query (the search itself and the info/content requests of its hits) - the
exact approved query (still approved, same row, its topic still deriving
exactly the approved text), all read from the database, not cached; then an
atomic budget reservation (which may wait); then the same checks AGAIN
immediately before the request is sent, so a revocation, rename, opt-out,
source disablement or lost lease that happened during the wait stops it.
Persistence re-checks the gates under locks. No database transaction is open
while a request is in flight.

What cannot be prevented: a request already on the wire (sent after the last
check, in the milliseconds between that check and the socket write, or being
answered) when an operator revokes, renames, opts out or disables. It is not
recalled; its response is used only if the persistence step, which re-checks
the gates under locks, still permits it - and the next request does not
happen. Holding a transaction (and its locks) across network I/O to close
this window is deliberately not done.

Search progress is keyed by stable query identity: the persisted cursor is
the query ROW id (progress["query_id"], finished ones in progress["done_queries"]),
never a list position, so a changed approval set can never attach the hits of
one query to another; hits of a query that stopped being usable are dropped.

Outcomes of a slice:
- slice limits reached, budget wait too long, provider throttle (429/503,
  maxlag, Retry-After): YIELD - progress kept, no failure counted; a throttle
  also blocks the source until the provider's time.
- transient provider or database error: FAILED ATTEMPT (failure_count + 1,
  backoff), progress kept; permanent provider refusal (403, API error, policy
  refusal): run failed.
- a candidate that fails a quality gate, or whose responses are ambiguous
  (revision mismatch, missing revision): skipped and counted; never stale.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from . import budget, persist, queries, runs
from .persist import MissingStatus, Outcome
from .runs import RunClaim
from .wikipedia import CandidateRefused, ProviderError, ProviderThrottled, WikipediaConnector, lexically_relevant

log = logging.getLogger(__name__)

PERSISTENT_GATES = ("course_not_opted_in", "source_disabled", "target_missing", "course_missing")


@dataclass
class HarvestContext:
    session_factory: sessionmaker[Session]
    settings: Settings
    connector_factory: Callable[[str], WikipediaConnector]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


class _Yield(Exception):
    def __init__(self, reason: str, delay: float = 0.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.delay = delay


class _Stop(Exception):
    """The attempt lost its run or a gate closed: stop without writing anything more."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _QueryGone(Exception):
    """The approved query behind this unit of work is no longer usable (revoked, renamed topic): drop its hits."""


class _WorkDone(Exception):
    """A per-run limit was reached: finish the run as succeeded with what it has."""


@dataclass
class _Slice:
    run: RunClaim
    ctx: HarvestContext
    connector: WikipediaConnector
    started: float
    requests: int = 0
    tallies: dict = field(default_factory=dict)

    @property
    def progress(self) -> dict:
        return self.run.progress

    def count(self, key: str, amount: int = 1) -> None:
        self.progress[key] = int(self.progress.get(key, 0)) + amount

    def ensure_room(self, needed: int) -> int:
        """Start a unit of work only if `needed` requests fit in the run and the slice; returns the room.

        Units (a search; an info request plus one content request) never
        straddle a slice boundary, so every slice makes progress and a run
        always ends: by completing, or at HARVEST_RUN_MAX_REQUESTS (_WorkDone),
        or at its max_claims.
        """
        s = self.ctx.settings
        run_left = s.harvest_run_max_requests - int(self.progress.get("requests", 0))
        slice_left = s.harvest_slice_max_requests - self.requests
        if run_left < needed:
            raise _WorkDone()
        if slice_left < needed:
            raise _Yield("slice_requests")
        if self.ctx.clock() - self.started > s.harvest_slice_seconds:
            raise _Yield("slice_time")
        return min(run_left, slice_left)

    def _revalidate(self, query: queries.UsableQuery | None) -> None:
        """Gates, lease ownership and (for a query's requests) the exact approved query, from the database now."""
        with self.ctx.session_factory() as session:
            reason = runs.closed_gate(session, self.ctx.settings, self.run.course_id, self.run.source_id)
            owned = session.execute(
                text("SELECT 1 FROM harvest_runs WHERE id = :id AND status = 'running' AND attempt_token = :t"),
                {"id": self.run.run_id, "t": self.run.token},
            ).first()
            usable = query is None or queries.still_usable(session, self.run.course_id, query)
            session.rollback()
        if reason is not None:
            raise _Stop(reason)
        if owned is None:
            raise _Stop("stale")
        if not usable:
            raise _QueryGone()

    def before_request(self, query: queries.UsableQuery | None = None) -> None:
        s = self.ctx.settings
        if int(self.progress.get("requests", 0)) >= s.harvest_run_max_requests:
            raise _WorkDone()
        if self.requests >= s.harvest_slice_max_requests:
            raise _Yield("slice_requests")
        if self.ctx.clock() - self.started > s.harvest_slice_seconds:
            raise _Yield("slice_time")
        self._revalidate(query)
        reservation = budget.reserve(self.ctx.session_factory, self.run.source_id)
        if not reservation.granted:
            if reservation.wait_seconds > s.harvest_budget_max_wait_seconds:
                raise _Yield("budget", reservation.wait_seconds)
            self.ctx.sleep(reservation.wait_seconds)
            reservation = budget.reserve(self.ctx.session_factory, self.run.source_id)
            if not reservation.granted:
                raise _Yield("budget", reservation.wait_seconds)
        # After any wait, immediately before network I/O: gates, lease and approval may have changed. (The
        # reservation just taken is then spent without a request: the provider budget errs on the safe side.)
        self._revalidate(query)
        self.requests += 1
        self.count("requests")


def run_harvest(run_id: uuid.UUID, ctx: HarvestContext) -> str:
    s = ctx.settings
    if not s.harvest_enabled:
        log.info("harvest_not_claimed run_id=%s reason=harvest_disabled", run_id)
        return "harvest_disabled"
    try:
        with ctx.session_factory() as session:
            peek = session.execute(
                text(
                    "SELECT r.course_id, r.source_id, s.language FROM harvest_runs r "
                    "JOIN sources s ON s.id = r.source_id WHERE r.id = :id"
                ),
                {"id": run_id},
            ).one_or_none()
            reason = None if peek is None else runs.closed_gate(session, s, peek.course_id, peek.source_id)
            session.rollback()
        if peek is None:
            return "not_found"
        if reason is not None:
            if reason in PERSISTENT_GATES:
                _cancel_closed(ctx, peek.course_id, peek.source_id, reason)
            log.info("harvest_not_claimed run_id=%s reason=%s", run_id, reason)
            return "gate_closed"
        with ctx.session_factory() as session:
            run = runs.claim(session, run_id, s)
            session.commit()
    except SQLAlchemyError:
        log.warning("harvest_not_claimed run_id=%s reason=database", run_id, exc_info=True)
        return "database_unavailable"
    if run is None:
        return "not_claimed"
    log.info("harvest_claimed run_id=%s claim=%d failures=%d", run_id, run.claim_count, run.failure_count)
    work = _Slice(run=run, ctx=ctx, connector=ctx.connector_factory(peek.language), started=ctx.clock())
    try:
        _execute(work)
    except _Yield as exc:
        return runs.yield_run(ctx.session_factory, run, s, exc.delay, exc.reason)
    except _Stop as exc:
        log.info("harvest_slice_stopped run_id=%s reason=%s", run_id, exc.reason)
        if exc.reason == "stale":
            return "stale"
        return runs.yield_run(ctx.session_factory, run, s, s.harvest_retry_backoff_seconds, exc.reason)
    except ProviderThrottled as exc:
        # The full provider delay is stored; the cap only bounds when this run is looked at again.
        budget.block(ctx.session_factory, run.source_id, exc.retry_after_seconds)
        delay = min(exc.retry_after_seconds, s.harvest_retry_after_max_seconds)
        return runs.yield_run(ctx.session_factory, run, s, delay, f"throttled_{exc.code}")
    except ProviderError as exc:
        if not exc.retryable:
            budget.block(ctx.session_factory, run.source_id, s.harvest_throttle_default_seconds)
        return _fail(ctx, run, f"provider_{exc.code}", permanent=not exc.retryable)
    except budget.BudgetMissing:
        return _fail(ctx, run, "budget_missing", permanent=True)
    except SoftTimeLimitExceeded:
        return _fail(ctx, run, "soft_time_limit", permanent=False)
    except SQLAlchemyError as exc:
        return _fail(ctx, run, f"database_{type(exc).__name__}", permanent=False)
    except Exception as exc:  # noqa: BLE001 - retried within max_failures
        log.exception("harvest_slice_error run_id=%s", run_id)
        return _fail(ctx, run, f"unexpected_{type(exc).__name__}", permanent=False)
    return runs.complete(ctx.session_factory, run, s)


def _fail(ctx: HarvestContext, run: RunClaim, code: str, *, permanent: bool) -> str:
    try:
        return runs.fail_attempt(ctx.session_factory, run, ctx.settings, code, permanent=permanent)
    except SQLAlchemyError:
        log.error("harvest_failure_unrecorded run_id=%s code=%s; lease recovery decides", run.run_id, code)
        return "unrecorded"


def _cancel_closed(ctx: HarvestContext, course_id: uuid.UUID, source_id: uuid.UUID, reason: str) -> None:
    """A queued run whose course opted out or whose source is disabled will never run: cancel it."""
    with ctx.session_factory() as session:
        session.execute(
            text("SELECT 1 FROM harvest_course_settings WHERE course_id = :c FOR NO KEY UPDATE"), {"c": course_id}
        )
        session.execute(text("SELECT 1 FROM sources WHERE id = :s FOR SHARE"), {"s": source_id})
        if runs.closed_gate(session, ctx.settings, course_id, source_id) in PERSISTENT_GATES:
            session.execute(
                text(
                    "UPDATE harvest_runs SET status = 'cancelled', finished_at = now(), lease_expires_at = NULL, "
                    "attempt_token = NULL, error_code = :code, updated_at = now() "
                    "WHERE course_id = :c AND source_id = :s AND status = 'queued'"
                ),
                {"c": course_id, "s": source_id, "code": reason},
            )
        session.commit()


def _ensure_licence(work: _Slice, query: queries.UsableQuery | None = None) -> None:
    """Once per run, at the start of the first unit that can accept content: the declared licence must match.

    One more budgeted provider request (counted in the run and slice limits, reserved atomically like every
    other), a unit of its own so that small slices still make progress. A mismatch or an ambiguous answer raises
    a non-retryable ProviderError: the run fails and nothing new is accepted; documents already harvested are
    not touched.
    """
    if work.progress.get("licence_ok"):
        return
    work.ensure_room(1)
    work.before_request(query)
    work.connector.verify_licence()
    work.progress["licence_ok"] = True


def _persisted(work: _Slice, outcome) -> None:
    if outcome in (Outcome.GATE_CLOSED, Outcome.STALE):
        raise _Stop("stale" if outcome is Outcome.STALE else "gate_closed")
    work.count(outcome.value if isinstance(outcome, (Outcome, MissingStatus)) else str(outcome))


def _execute(work: _Slice) -> None:
    progress = work.progress
    progress.setdefault("phase", "refresh")
    try:
        if progress["phase"] == "refresh":
            _refresh(work)
            progress["phase"] = "search"
            progress.pop("refresh_cursor", None)
        if progress["phase"] == "search":
            _search(work)
            progress["phase"] = "done"
    except _WorkDone:
        progress["phase"] = "done"
        progress["limit_reached"] = True


def _refresh(work: _Slice) -> None:
    s = work.ctx.settings
    run = work.run
    while True:
        with work.ctx.session_factory() as session:
            batch = session.execute(
                text(
                    "SELECT r.id, r.page_id, r.state, r.last_revision_id, r.missing_checks, "
                    f"{persist.ORPHANED_DUPLICATE_SQL} AS orphaned FROM external_resources r "
                    "WHERE r.course_id = :c AND r.source_id = :s AND r.next_check_at <= now() "
                    "AND (CAST(:cursor AS uuid) IS NULL OR r.id > CAST(:cursor AS uuid)) ORDER BY r.id LIMIT :limit"
                ),
                {"c": run.course_id, "s": run.source_id, "cursor": work.progress.get("refresh_cursor"),
                 "limit": s.harvest_refresh_batch_size},
            ).all()  # fmt: skip
            session.rollback()
        if not batch:
            return
        _ensure_licence(work)
        work.ensure_room(2)  # the batch's info request and at least one content request
        work.before_request()
        infos = work.connector.page_info([row.page_id for row in batch])
        for row in batch:
            page = infos[row.page_id]
            if page.gone_reason is not None:
                _persisted(work, persist.record_gone(work.ctx.session_factory, s, run, row.id, page))
            elif page.revision_id is None:
                _persisted(work, persist.record_checked(work.ctx.session_factory, s, run, row.id, present=False))
                work.count("ambiguous")
            elif (
                page.revision_id == row.last_revision_id
                and row.state != "missing"
                and row.missing_checks == 0
                and not row.orphaned  # a duplicate nobody holds the content of any more is decided again
            ):
                _persisted(work, persist.record_checked(work.ctx.session_factory, s, run, row.id, present=True))
            else:
                try:
                    work.before_request()
                    revision = work.connector.fetch_revision(page)
                except CandidateRefused as exc:
                    work.count("refused")
                    log.info("harvest_refresh_refused run_id=%s page_id=%d code=%s", run.run_id, row.page_id, exc.code)
                    _persisted(work, persist.record_checked(work.ctx.session_factory, s, run, row.id, present=False))
                else:
                    _persisted(work, persist.persist_revision(work.ctx.session_factory, s, run, page, revision, None))
            work.progress["refresh_cursor"] = str(row.id)
        runs.save_progress(work.ctx.session_factory, run)


def _search(work: _Slice) -> None:
    """Approved queries in order; per query, its new candidates in order (cursors persisted by query identity)."""
    s = work.ctx.settings
    progress = work.progress
    done: list[str] = progress.setdefault("done_queries", [])
    while True:
        with work.ctx.session_factory() as session:  # fresh every time: approvals may change between slices and units
            usable = queries.usable_queries(session, work.run.course_id, s.harvest_max_queries_per_course)
            session.rollback()
        query = next((q for q in usable if str(q.id) not in done), None)
        if query is None:
            return
        if progress.get("quota_reached") or int(progress.get("accepted", 0)) + int(
            progress.get("new_version", 0)
        ) >= s.harvest_max_accepted_per_run:
            raise _WorkDone()
        if progress.get("query_id") != str(query.id):  # hits belong to one query row, never to "the next one"
            progress.update(query_id=str(query.id), hits=None, hit_index=0)
        try:
            _search_query(work, query)
        except _QueryGone:
            work.count("queries_withdrawn")
            log.info("harvest_query_withdrawn run_id=%s query_id=%s", work.run.run_id, query.id)
        done.append(str(query.id))
        progress.update(query_id=None, hits=None, hit_index=0)
        runs.save_progress(work.ctx.session_factory, work.run)
        if int(progress.get("candidates", 0)) >= s.harvest_max_candidates_per_run:
            raise _WorkDone()


def _search_query(work: _Slice, query: queries.UsableQuery) -> None:
    s = work.ctx.settings
    run = work.run
    progress = work.progress
    if progress.get("hits") is None:
        work.ensure_room(1)
        work.before_request(query)  # the exact approved text, re-read from the database right before the request
        hits = work.connector.search(query.text, s.harvest_candidates_per_query)
        with work.ctx.session_factory() as session:
            known = set(
                session.execute(
                    text(
                        "SELECT page_id FROM external_resources WHERE course_id = :c AND source_id = :s "
                        "AND page_id = ANY(:ids)"
                    ),
                    {"c": run.course_id, "s": run.source_id, "ids": [h.page_id for h in hits]},
                ).scalars()
            )
            session.rollback()
        remaining = max(s.harvest_max_candidates_per_run - int(progress.get("candidates", 0)), 0)
        progress["hits"] = [hit.page_id for hit in hits if hit.page_id not in known][:remaining]
        progress["hit_index"] = 0
        runs.save_progress(work.ctx.session_factory, run)
    while int(progress["hit_index"]) < len(progress["hits"]):
        _ensure_licence(work, query)
        room = work.ensure_room(2)  # one info request and at least one content request
        batch = progress["hits"][int(progress["hit_index"]) :][: room - 1]
        work.before_request(query)
        infos = work.connector.page_info(batch)
        for page_id in batch:
            _candidate(work, query, infos[page_id])
            progress["hit_index"] = int(progress["hit_index"]) + 1
            if progress.get("quota_reached"):
                progress["hit_index"] = len(progress["hits"])
                break
        runs.save_progress(work.ctx.session_factory, run)


def _candidate(work: _Slice, query, page) -> None:
    run = work.run
    work.count("candidates")
    try:
        work.connector.check_article(page)
        work.before_request(query)
        revision = work.connector.fetch_revision(page)
        if not lexically_relevant(query.text, revision):
            raise CandidateRefused("not_lexically_relevant")
    except CandidateRefused as exc:
        work.count("refused")
        log.info("harvest_candidate_refused run_id=%s page_id=%d code=%s", run.run_id, page.page_id, exc.code)
        return
    outcome = persist.persist_revision(work.ctx.session_factory, work.ctx.settings, run, page, revision, query.id)
    _persisted(work, outcome)
    if outcome is Outcome.QUOTA:
        work.progress["quota_reached"] = True
