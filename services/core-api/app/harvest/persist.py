"""Persisting harvested revisions: deduplication, quotas, versions, refresh bookkeeping, missing evidence.

Every function here is one short transaction that first takes, in the
global lock order, the course gate (FOR NO KEY UPDATE: serializes quota and
dedup decisions of one course), the source (FOR SHARE), the target (FOR
SHARE), the run (fenced by its attempt token) and then the resource row. It
re-checks HARVEST_ENABLED, the course opt-in and the source activation under
those locks, so a concurrent opt-out or source disable either happens before
(nothing is written) or after (it sees and handles what was written).

Versions. A resource's first accepted revision creates version 1: the
document (external, fileless, `pending`, course-scoped), its normalized
sections, its provenance row and its queued index job, all in one
transaction together with the run's progress. Later revisions:

- Deduplication is version-aware (_duplicate_of, under the course gate
  lock): content counts as held by another accepted resource of the course
  while any of its versions is pending, indexed or failed - not only its
  newest version. A duplicate whose content is no longer held by anyone is
  reconsidered at its next refresh check (ORPHANED_DUPLICATE_SQL), even if
  its own provider revision is unchanged.
- same normalized content (whatever the revision id): nothing new is
  created; the resource remembers the revision it checked.
- content another accepted resource holds (WP-5): nothing new is created and
  nothing is staled. An accepted resource keeps its visible version and its
  content_sha256, records the revision and its refresh bookkeeping, and stores
  the duplicated digest in duplicate_content_sha256, so the revision is not
  fetched again while that content is held and is reconsidered (and becomes a
  new version) once nobody holds it (ORPHANED_DUPLICATE_SQL). A missing
  resource whose page returns with held content becomes a duplicate.
- different content: a new document version, unless a version of the
  resource is still `pending` (then nothing is created now and the revision
  is not recorded, so the next refresh looks again).
The previously indexed version stays `indexed` (and visible) until the new
version's index job succeeds; index completion then switches old -> stale
and new -> indexed in one transaction (app/ingestion/jobs.py). Indexed
documents are never modified here, so no canonical digest changes.

Missing pages. A page reported missing (or turned into a redirect, or moved
out of the article namespace) is only *observed* the first time; it is
confirmed, and the indexed version marked stale with the stored evidence,
only by a second observation at least HARVEST_MISSING_CONFIRM_SECONDS later.
Search results, provider errors and ambiguous responses never stale anything.
Nothing is ever deleted.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import DocumentSection
from . import wikipedia
from .runs import RunClaim, lock_owned, write_progress
from .wikipedia import FetchedRevision, PageInfo


class Outcome(enum.Enum):
    ACCEPTED = "accepted"  # new resource, version 1 created
    NEW_VERSION = "new_version"  # changed content, new version created
    UNCHANGED = "unchanged"  # same normalized content; nothing created
    DUPLICATE = "duplicate"  # same content as another accepted resource of the course
    QUOTA = "quota"  # the course already has its maximum of accepted resources
    PENDING = "pending"  # a version is still pending; nothing created now
    GATE_CLOSED = "gate_closed"  # harvesting, the course or the source was switched off
    STALE = "stale"  # the attempt no longer owns its run; nothing written


@dataclass(frozen=True)
class _Gates:
    max_active_documents: int
    language: str
    refresh_interval_seconds: int


def lock_gates(session: Session, settings: Settings, run: RunClaim) -> _Gates | Outcome:
    if not settings.harvest_enabled:
        return Outcome.GATE_CLOSED
    gate = session.execute(
        text(
            "SELECT enabled, max_active_documents FROM harvest_course_settings WHERE course_id = :c FOR NO KEY UPDATE"
        ),
        {"c": run.course_id},
    ).one_or_none()
    if gate is None or gate.enabled is not True:
        return Outcome.GATE_CLOSED
    source = session.execute(
        text("SELECT enabled, provider, language FROM sources WHERE id = :s FOR SHARE"), {"s": run.source_id}
    ).one_or_none()
    if source is None or source.enabled is not True or source.provider != wikipedia.PROVIDER:
        return Outcome.GATE_CLOSED
    target = session.execute(
        text("SELECT refresh_interval_seconds FROM harvest_targets WHERE course_id = :c AND source_id = :s FOR SHARE"),
        {"c": run.course_id, "s": run.source_id},
    ).one_or_none()
    if target is None:
        return Outcome.GATE_CLOSED
    if not lock_owned(session, run):
        return Outcome.STALE
    return _Gates(gate.max_active_documents, source.language, target.refresh_interval_seconds)


def _resource(session: Session, run: RunClaim, page_id: int):
    return session.execute(
        text(
            "SELECT r.id, r.state, r.current_document_id, r.content_sha256, r.last_revision_id, r.missing_checks, "
            "r.duplicate_content_sha256, d.status AS current_status FROM external_resources r "
            "LEFT JOIN documents d ON d.id = r.current_document_id "
            "WHERE r.course_id = :c AND r.source_id = :s AND r.page_id = :p FOR NO KEY UPDATE OF r"
        ),
        {"c": run.course_id, "s": run.source_id, "p": page_id},
    ).one_or_none()


# Content a resource still holds: its newest version (resources.content_sha256, the key of the partial unique
# index) or ANY version whose document is pending or indexed, or failed while still the current version (only
# the current one can be retried). A superseded failed
# or missing version is stale and holds nothing. The newest version alone is not enough: while version Y of a
# resource is pending, its older version X stays indexed and visible, and X must still count for deduplication.
HOLDER_PREDICATE = (
    "o.course_id = {course} AND o.state = 'accepted' AND o.id IS DISTINCT FROM {me} AND ("
    "o.content_sha256 = {digest} OR EXISTS (SELECT 1 FROM external_document_versions v "
    "JOIN documents d ON d.id = v.document_id WHERE v.resource_id = o.id AND v.content_sha256 = {digest} "
    "AND (d.status IN ('pending', 'indexed') OR (d.status = 'failed' AND o.current_document_id = d.id))))"
)

# Evaluated on a resource aliased `r`: a duplicate whose content no accepted resource holds any more (the
# referenced resource changed content, went missing, ...) must be reconsidered at its next check even if its own
# provider revision did not change (app/harvest/executor.py). The same holds for an accepted resource whose
# recorded newest revision duplicated another's content (duplicate_content_sha256).
ORPHANED_DUPLICATE_SQL = (
    "((r.state = 'duplicate' AND NOT EXISTS (SELECT 1 FROM external_resources o WHERE "
    + HOLDER_PREDICATE.format(course="r.course_id", me="r.id", digest="r.content_sha256")
    + ")) OR (r.state = 'accepted' AND r.duplicate_content_sha256 IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM external_resources o WHERE "
    + HOLDER_PREDICATE.format(course="r.course_id", me="r.id", digest="r.duplicate_content_sha256")
    + ")))"
)


def _duplicate_of(session: Session, course_id: uuid.UUID, digest: str, resource_id: uuid.UUID | None):
    """Another accepted resource of the course that holds this content (called under the course gate lock)."""
    predicate = HOLDER_PREDICATE.format(course=":c", me=":r", digest=":h")
    return session.execute(
        text(f"SELECT o.id FROM external_resources o WHERE {predicate} ORDER BY o.created_at, o.id LIMIT 1"),
        {"c": course_id, "h": digest, "r": resource_id},
    ).scalar_one_or_none()


def _accepted_count(session: Session, course_id: uuid.UUID) -> int:
    return session.execute(
        text("SELECT count(*) FROM external_resources WHERE course_id = :c AND state = 'accepted'"), {"c": course_id}
    ).scalar_one()


def _create_version(
    session: Session, run: RunClaim, gates: _Gates, resource_id: uuid.UUID, fetched: FetchedRevision, info: PageInfo
) -> uuid.UUID:
    language = gates.language
    url = wikipedia.canonical_url(language, fetched.title)
    document_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO documents (id, course_id, source_id, origin, title, url, licence, language, content_hash, "
            "status, fetched_at, created_at, kind, external_resource_id) VALUES (:id, :c, :s, 'external', :title, "
            ":url, :licence, :language, :hash, 'pending', timezone('UTC', now()), timezone('UTC', now()), 'article', :r)"
        ),
        {
            "id": document_id,
            "c": run.course_id,
            "s": run.source_id,
            "title": fetched.title,
            "url": url,
            "licence": wikipedia.LICENCE,
            "language": language,
            "hash": fetched.content_sha256,
            "r": resource_id,
        },
    )
    session.execute(
        insert(DocumentSection),
        [
            {"document_id": document_id, "ordinal": ordinal, "text": section.text, "locator": section.locator}
            for ordinal, section in enumerate(fetched.sections)
        ],
    )
    session.execute(
        text(
            "INSERT INTO external_document_versions (document_id, resource_id, version, page_id, revision_id, "
            "revision_timestamp, canonical_url, revision_url, licence, licence_url, attribution, content_sha256, "
            "language, fetched_at, harvest_run_id) VALUES (:d, :r, (SELECT coalesce(max(version), 0) + 1 FROM "
            "external_document_versions WHERE resource_id = :r), :page, :rev, :ts, :url, :rev_url, :licence, "
            ":licence_url, :attribution, :hash, :language, now(), :run)"
        ),
        {
            "d": document_id,
            "r": resource_id,
            "page": fetched.page_id,
            "rev": fetched.revision_id,
            "ts": info.revision_timestamp,
            "url": url,
            "rev_url": wikipedia.revision_url(language, fetched.revision_id),
            "licence": wikipedia.LICENCE,
            "licence_url": wikipedia.LICENCE_URL,
            "attribution": wikipedia.attribution(language, fetched.title, fetched.page_id, fetched.revision_id),
            "hash": fetched.content_sha256,
            "language": language,
            "run": run.run_id,
        },
    )
    session.execute(
        text("INSERT INTO ingestion_jobs (document_id, kind) VALUES (:d, 'index')"), {"d": document_id}
    )
    session.execute(
        text(
            "UPDATE external_resources SET state = 'accepted', duplicate_of_id = NULL, duplicate_content_sha256 = NULL, "
            "current_document_id = :d, content_sha256 = :hash, last_revision_id = :rev, title = :title, canonical_url = :url, "
            "missing_since = NULL, missing_checks = 0, missing_evidence = NULL, last_checked_at = now(), "
            "next_check_at = now() + :interval * interval '1 second', updated_at = now() WHERE id = :r"
        ),
        {
            "d": document_id,
            "hash": fetched.content_sha256,
            "rev": fetched.revision_id,
            "title": fetched.title,
            "url": url,
            "interval": gates.refresh_interval_seconds,
            "r": resource_id,
        },
    )
    return document_id


def _mark_duplicate(session: Session, resource_id: uuid.UUID, holder: uuid.UUID | None, digest: str | None) -> None:
    """An accepted resource's newest revision duplicates `holder`'s content `digest` (None: it no longer does)."""
    session.execute(
        text("UPDATE external_resources SET duplicate_of_id = :d, duplicate_content_sha256 = :h WHERE id = :r"),
        {"d": holder, "h": digest, "r": resource_id},
    )


def _checked(session: Session, resource_id: uuid.UUID, gates: _Gates, *, revision_id: int | None = None,
             digest: str | None = None, title: str | None = None) -> None:  # fmt: skip
    session.execute(
        text(
            "UPDATE external_resources SET last_revision_id = coalesce(:rev, last_revision_id), "
            "content_sha256 = coalesce(:hash, content_sha256), title = coalesce(:title, title), "
            "missing_since = NULL, missing_checks = 0, missing_evidence = NULL, last_checked_at = now(), "
            "next_check_at = now() + :interval * interval '1 second', updated_at = now() WHERE id = :r"
        ),
        {"rev": revision_id, "hash": digest, "title": title, "interval": gates.refresh_interval_seconds, "r": resource_id},
    )


def persist_revision(
    session_factory: sessionmaker[Session],
    settings: Settings,
    run: RunClaim,
    info: PageInfo,
    fetched: FetchedRevision,
    query_id: uuid.UUID | None,
) -> Outcome:
    """Decide and persist what a verified revision means for the course, with the run's progress."""
    with session_factory() as session:
        gates = lock_gates(session, settings, run)
        if isinstance(gates, Outcome):
            session.rollback()
            return gates
        outcome = _decide(session, run, gates, info, fetched, query_id)
        write_progress(session, run)
        session.commit()
        return outcome


def _decide(
    session: Session, run: RunClaim, gates: _Gates, info: PageInfo, fetched: FetchedRevision, query_id
) -> Outcome:
    resource = _resource(session, run, fetched.page_id)
    if resource is not None:
        if (
            resource.state == "accepted"
            and resource.content_sha256 == fetched.content_sha256
            and resource.current_status in ("pending", "indexed", "failed")
        ):
            # Same normalized text, even under a new revision id: no document, no job.
            if resource.duplicate_content_sha256 is not None:  # back to its own text: no duplicate revision now
                _mark_duplicate(session, resource.id, None, None)
            _checked(session, resource.id, gates, revision_id=fetched.revision_id)
            return Outcome.UNCHANGED
        if resource.current_status == "pending":
            return Outcome.PENDING  # one pending version at a time; looked at again next refresh
        duplicate = _duplicate_of(session, run.course_id, fetched.content_sha256, resource.id)
        if duplicate is not None:
            if resource.state == "accepted":
                # Its visible version and content_sha256 (the dedup key of that version) stay; the revision is
                # recorded with the duplicated digest, so it is fetched again only once nobody holds that text.
                _mark_duplicate(session, resource.id, duplicate, fetched.content_sha256)
                _checked(session, resource.id, gates, revision_id=fetched.revision_id)
            else:  # duplicate, or missing and back with held text: tracked as a duplicate, nothing visible
                session.execute(
                    text("UPDATE external_resources SET state = 'duplicate', duplicate_of_id = :d WHERE id = :r"),
                    {"d": duplicate, "r": resource.id},
                )
                _checked(session, resource.id, gates, revision_id=fetched.revision_id, digest=fetched.content_sha256)
            return Outcome.DUPLICATE
        if resource.state != "accepted" and _accepted_count(session, run.course_id) >= gates.max_active_documents:
            return Outcome.QUOTA
        _create_version(session, run, gates, resource.id, fetched, info)
        return Outcome.NEW_VERSION

    if _accepted_count(session, run.course_id) >= gates.max_active_documents:
        return Outcome.QUOTA
    duplicate = _duplicate_of(session, run.course_id, fetched.content_sha256, None)
    url = wikipedia.canonical_url(gates.language, fetched.title)
    params = {
        "c": run.course_id,
        "s": run.source_id,
        "p": fetched.page_id,
        "title": fetched.title,
        "url": url,
        "rev": fetched.revision_id,
        "hash": fetched.content_sha256,
        "q": query_id,
        "dup": duplicate,
        "interval": gates.refresh_interval_seconds,
    }
    try:
        with session.begin_nested():
            resource_id = session.execute(
                text(
                    "INSERT INTO external_resources (course_id, source_id, page_id, title, canonical_url, state, "
                    "duplicate_of_id, last_revision_id, content_sha256, discovered_by_query_id, last_checked_at, "
                    "next_check_at) VALUES (:c, :s, :p, :title, :url, :state, :dup, :rev, :hash, :q, now(), "
                    "now() + :interval * interval '1 second') RETURNING id"
                ),
                {**params, "state": "duplicate" if duplicate else "accepted"},
            ).scalar_one()
    except IntegrityError:
        # Only reachable after manual edits: the gate lock serializes a course's decisions.
        return Outcome.DUPLICATE
    if duplicate is not None:
        return Outcome.DUPLICATE
    _create_version(session, run, gates, resource_id, fetched, info)
    return Outcome.ACCEPTED


def record_checked(
    session_factory: sessionmaker[Session], settings: Settings, run: RunClaim, resource_id: uuid.UUID, *,
    present: bool,
) -> Outcome:  # fmt: skip
    """The resource was inspected and nothing needs a new version (same revision, or an ambiguous answer).

    `present` = the provider showed the page as a normal article: any
    earlier missing observation is cleared. An ambiguous answer only moves
    next_check_at (it is neither evidence of presence nor of absence).
    """
    with session_factory() as session:
        gates = lock_gates(session, settings, run)
        if isinstance(gates, Outcome):
            session.rollback()
            return gates
        session.execute(text("SELECT 1 FROM external_resources WHERE id = :r FOR NO KEY UPDATE"), {"r": resource_id})
        if present:
            _checked(session, resource_id, gates)
        else:
            session.execute(
                text(
                    "UPDATE external_resources SET last_checked_at = now(), "
                    "next_check_at = now() + :interval * interval '1 second', updated_at = now() WHERE id = :r"
                ),
                {"r": resource_id, "interval": gates.refresh_interval_seconds},
            )
        write_progress(session, run)
        session.commit()
    return Outcome.UNCHANGED


class MissingStatus(enum.Enum):
    OBSERVED = "observed"  # first observation recorded
    WAITING = "waiting"  # observed again too early to confirm; nothing changed
    CONFIRMED = "confirmed"  # resource missing, indexed version stale with evidence


def record_gone(
    session_factory: sessionmaker[Session], settings: Settings, run: RunClaim, resource_id: uuid.UUID, info: PageInfo
) -> MissingStatus | Outcome:
    reason = info.gone_reason
    if reason is None:
        raise ValueError("record_gone needs a page that is missing, a redirect or outside namespace 0")
    observation = json.dumps(
        {
            "page_id": info.page_id,
            "result": reason,
            "namespace": info.namespace,
            "request": "action=query&prop=info|pageprops|revisions&pageids=<page_id>",
        },
        sort_keys=True,
    )
    with session_factory() as session:
        gates = lock_gates(session, settings, run)
        if isinstance(gates, Outcome):
            session.rollback()
            return gates
        row = session.execute(
            text(
                "SELECT state, missing_checks, missing_evidence, "
                "(missing_since IS NOT NULL AND missing_since <= now() - :confirm * interval '1 second') AS confirmable "
                "FROM external_resources WHERE id = :r FOR NO KEY UPDATE"
            ),
            {"r": resource_id, "confirm": settings.harvest_missing_confirm_seconds},
        ).one()
        observed = "jsonb_build_object('observed_at', now()) || CAST(:obs AS jsonb)"
        if row.missing_checks == 0:
            session.execute(
                text(
                    "UPDATE external_resources SET missing_since = now(), missing_checks = 1, "
                    f"missing_evidence = jsonb_build_object('observations', jsonb_build_array({observed})), "
                    "last_checked_at = now(), next_check_at = now() + :confirm * interval '1 second', "
                    "updated_at = now() WHERE id = :r"
                ),
                {"r": resource_id, "obs": observation, "confirm": settings.harvest_missing_confirm_seconds},
            )
            status = MissingStatus.OBSERVED
        elif not row.confirmable or row.state == "missing":
            session.execute(
                text(
                    "UPDATE external_resources SET last_checked_at = now(), "
                    "next_check_at = greatest(next_check_at, now() + :confirm * interval '1 second'), "
                    "updated_at = now() WHERE id = :r"
                ),
                {"r": resource_id, "confirm": settings.harvest_missing_confirm_seconds},
            )
            status = MissingStatus.WAITING
        else:
            evidence = session.execute(
                text(
                    "UPDATE external_resources SET state = 'missing', missing_checks = missing_checks + 1, "
                    "duplicate_of_id = CASE WHEN duplicate_content_sha256 IS NULL THEN duplicate_of_id END, "
                    "duplicate_content_sha256 = NULL, "
                    "missing_evidence = jsonb_set(missing_evidence, '{observations}', "
                    f"(missing_evidence -> 'observations') || jsonb_build_array({observed})), "
                    "last_checked_at = now(), next_check_at = now() + :interval * interval '1 second', "
                    "updated_at = now() WHERE id = :r RETURNING missing_evidence"
                ),
                {"r": resource_id, "obs": observation, "interval": gates.refresh_interval_seconds},
            ).scalar_one()
            indexed = session.execute(
                text(
                    "SELECT id FROM documents WHERE external_resource_id = :r AND status = 'indexed' FOR UPDATE"
                ),
                {"r": resource_id},
            ).scalar_one_or_none()
            if indexed is not None:
                session.execute(text("UPDATE documents SET status = 'stale' WHERE id = :d"), {"d": indexed})
                session.execute(
                    text(
                        "UPDATE external_document_versions SET stale_reason = 'missing', staled_at = now(), "
                        "stale_evidence = CAST(:e AS jsonb) WHERE document_id = :d"
                    ),
                    {"d": indexed, "e": json.dumps(evidence, sort_keys=True)},
                )
            status = MissingStatus.CONFIRMED
        write_progress(session, run)
        session.commit()
    return status
