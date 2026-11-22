"""Helpers for the M6 harvesting tests: offline Wikipedia fixtures, sources, opt-in, claims.

Nothing here performs a network request. The fake connector answers from
in-memory pages and records every request (operation and parameters), so
tests can assert exactly what would have been sent to the provider.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.harvest import runs, wikipedia
from app.harvest.transport import ProviderResponse
from app.harvest.wikipedia import ExtractedSection, FetchedRevision, PageInfo, SearchHit, content_sha256

USER_AGENT = "StudyGraphHarvester/0.6 (https://studygraph.dev/contact; harvest-ops@studygraph.dev)"
PARAGRAPH = "Chemical equilibrium is the state in which reactant and product concentrations stop changing. " * 4


RIGHTSINFO = {
    "url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "text": "Creative Commons Attribution-ShareAlike 4.0 International License",
}


def harvest_settings(**overrides) -> Settings:
    values = dict(
        harvest_enabled=True,
        harvest_user_agent=USER_AGENT,
        harvest_min_document_chars=200,
        harvest_retry_backoff_seconds=1,
        harvest_failed_run_backoff_seconds=60,
        harvest_budget_max_wait_seconds=0,
    )
    values.update(overrides)
    return Settings(**values)


def sections_for(title: str, body: str = PARAGRAPH, extra: str = "") -> tuple[ExtractedSection, ...]:
    return (
        ExtractedSection(None, None, 0, f"{title} is a topic. {body}"),
        ExtractedSection("History", "History", 2, f"History of {title}. {body}{extra}"),
    )


def fetched(page_id: int, revision_id: int, title: str, body: str = PARAGRAPH, extra: str = "") -> FetchedRevision:
    sections = sections_for(title, body, extra)
    return FetchedRevision(
        page_id=page_id,
        revision_id=revision_id,
        title=title,
        sections=sections,
        content_sha256=content_sha256(sections),
        char_count=sum(len(s.text) for s in sections),
    )


def info(page_id: int, revision_id: int | None, title: str = "Chemical equilibrium", **flags) -> PageInfo:
    values = dict(
        page_id=page_id,
        present=True,
        namespace=0,
        title=title,
        content_model="wikitext",
        page_language="en",
        revision_id=revision_id,
        revision_timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    values.update(flags)
    return PageInfo(**values)


# --- database fixtures ---------------------------------------------------------------------------


def wikipedia_source(world, *, enabled: bool = True, max_requests: int = 1000, min_interval_ms: int = 0) -> uuid.UUID:
    with world.engine.begin() as connection:
        source_id = connection.execute(
            text(
                "INSERT INTO sources (kind, domain, licence, enabled, provider, language, licence_url) "
                "VALUES ('wikipedia', 'en.wikipedia.org', :licence, :e, 'wikipedia', 'en', :url) RETURNING id"
            ),
            {"licence": wikipedia.LICENCE, "e": enabled, "url": wikipedia.LICENCE_URL},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO harvest_provider_budgets (source_id, window_seconds, max_requests, min_interval_ms) "
                "VALUES (:s, 60, :m, :i)"
            ),
            {"s": source_id, "m": max_requests, "i": min_interval_ms},
        )
    world.sources.append(source_id)
    return source_id


def opt_in(world, course_id: uuid.UUID, source_id: uuid.UUID, *, quota: int = 20, enabled: bool = True) -> None:
    world.execute(
        "INSERT INTO harvest_course_settings (course_id, enabled, max_active_documents) VALUES (:c, :e, :q) "
        "ON CONFLICT (course_id) DO UPDATE SET enabled = :e, max_active_documents = :q",
        c=course_id, e=enabled, q=quota,
    )  # fmt: skip
    world.execute(
        "INSERT INTO harvest_targets (course_id, source_id) VALUES (:c, :s) ON CONFLICT DO NOTHING",
        c=course_id, s=source_id,
    )  # fmt: skip


def approve_topic(world, course_id: uuid.UUID, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    topic_id = world.topic(course_id, name)
    query_id = world._one(
        "INSERT INTO harvest_queries (course_id, topic_id, query_text) VALUES (:c, :t, :q) RETURNING id",
        c=course_id, t=topic_id, q=name,
    )  # fmt: skip
    return topic_id, query_id


def claimed_run(factory: sessionmaker, settings: Settings, course_id, source_id, *, manual: bool = True):
    with factory() as session:
        run_id = runs.create_run(session, settings, course_id, source_id, manual=manual)
        session.commit()
    assert run_id is not None
    with factory() as session:
        claim = runs.claim(session, run_id, settings)
        session.commit()
    assert claim is not None
    return claim


def run_row(world, run_id):
    return world.rows("SELECT * FROM harvest_runs WHERE id = :id", id=run_id)[0]


def versions_of(world, resource_page: int, course_id) -> list:
    return world.rows(
        "SELECT d.id, d.status, v.version, v.revision_id, v.stale_reason, v.stale_evidence FROM documents d "
        "JOIN external_document_versions v ON v.document_id = d.id JOIN external_resources r ON r.id = v.resource_id "
        "WHERE r.page_id = :p AND r.course_id = :c ORDER BY v.version",
        p=resource_page, c=course_id,
    )  # fmt: skip


def index_job_of(world, document_id):
    rows = world.rows(
        "SELECT id, status, attempt_count FROM ingestion_jobs WHERE document_id = :d AND kind = 'index' ORDER BY created_at",
        d=document_id,
    )
    return rows[-1] if rows else None


def index_now(factory, world, document_id):
    """Claim the document's queued index job and complete it with the digest of what is stored (real path)."""
    import json

    from tests.index_writer import persist
    from app.indexing.payload import request_for
    from app.ingestion import jobs

    job = index_job_of(world, document_id)
    with factory() as session:
        claim = jobs.claim_index(session, job["id"], 600)
        session.commit()
    assert claim is not None
    with factory() as session:
        request = request_for(session, document_id)
        session.rollback()
    count, _ = persist(world.engine, json.loads(request.body))
    return jobs.complete_index(factory, claim, request.content_digest, count)


# --- fake provider ----------------------------------------------------------------------------------


@dataclass
class FakeConnector:
    """Stands in for WikipediaConnector; answers from `pages` and records every request."""

    pages: dict[int, tuple[PageInfo, FetchedRevision | None]] = field(default_factory=dict)
    search_results: dict[str, list[SearchHit]] = field(default_factory=dict)
    requests: list[tuple[str, object]] = field(default_factory=list)
    fail_with: Exception | None = None
    language: str = "en"
    rightsinfo: object = field(default_factory=lambda: dict(RIGHTSINFO))

    def _record(self, op: str, value) -> None:
        self.requests.append((op, value))
        if self.fail_with is not None:
            raise self.fail_with

    def verify_licence(self) -> None:
        self._record("licence", None)
        wikipedia.check_rightsinfo(self.rightsinfo)  # the production comparison, on whatever the fake "declares"

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self._record("search", query)
        return self.search_results.get(query, [])[:limit]

    def page_info(self, page_ids: list[int]) -> dict[int, PageInfo]:
        self._record("info", tuple(page_ids))
        return {p: self.pages[p][0] if p in self.pages else PageInfo(page_id=p, present=False) for p in page_ids}

    def check_article(self, page: PageInfo) -> None:
        wikipedia.WikipediaConnector.check_article(self, page)  # same quality gate, same language

    def fetch_revision(self, page: PageInfo) -> FetchedRevision:
        self.check_article(page)
        self._record("parse", page.revision_id)
        revision = self.pages[page.page_id][1]
        if revision is None or revision.revision_id != page.revision_id:
            raise wikipedia.CandidateRefused("revision_mismatch")
        return revision


def json_response(body: dict, status: int = 200, retry_after: float | None = None) -> ProviderResponse:
    return ProviderResponse(status, "application/json; charset=utf-8", json.dumps(body).encode(), retry_after)
