"""OpenStax connector (WP-6): CNXML extraction, licence and identity checks, GitHub transport rules, and harvest
runs end to end against the disposable database with an offline fake of GitHub's REST API.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.chat.verify import VerificationFailed, public_provenance
from app.harvest import openstax, persist, runs
from app.harvest.executor import HarvestContext, run_harvest
from app.harvest.openstax import ModuleRef, OpenStaxConnector, git_blob_sha, parse_collection, parse_module
from app.harvest.transport import TransportError, parse_rate_limit_reset
from app.harvest.wikipedia import CandidateRefused, ProviderError, ProviderThrottled
from app.indexing.payload import request_for
from app.ingestion.jobs import CompletionStatus
from tests.harvest_support import claimed_run, harvest_settings, index_now, opt_in, run_row
from tests.openstax_support import (
    CONTEXT_CONTENT,
    EXCLUDED_MARKERS,
    REPO,
    FakeGitHub,
    book_files,
    collection_xml,
    module_xml,
    response,
)

BOOK = openstax.BOOKS["chemistry-2e"]
CONTEXT = "modules/m68664/index.cnxml"
EQUILIBRIUM = "modules/m68797/index.cnxml"


# --- extraction (no database) ----------------------------------------------------------------------


def test_the_collection_lists_chapter_modules_in_book_order_and_skips_front_and_back_matter():
    assert parse_collection(collection_xml(), BOOK) == [
        ("m68663", "Essential Ideas"), ("m68664", "Essential Ideas"), ("m68797", "Fundamental Equilibrium Concepts"),
    ]  # fmt: skip


@pytest.mark.parametrize(
    "collection, code",
    [
        (collection_xml(licence=("https://creativecommons.org/licenses/by/4.0/", "Creative Commons Attribution 4.0")),
         "licence_mismatch"),
        (collection_xml(licence=("http://creativecommons.org/licenses/by-nc-sa/4.0/", "Creative Commons Attribution")),
         "licence_mismatch"),
        (collection_xml(licence=("http://creativecommons.org/licenses/by-nc-sa/3.0/",
                                 "Creative Commons Attribution-NonCommercial-ShareAlike 3.0")), "licence_mismatch"),
        (collection_xml(licence=None), "licence_unverified"),
        (collection_xml(licence=("", "")), "licence_unverified"),
        (collection_xml(uuid_="00000000-0000-0000-0000-000000000000"), "book_mismatch"),
        (collection_xml(slug="chemistry-atoms-first-2e"), "book_mismatch"),
        (collection_xml(chapters=()), "invalid_collection"),
        (collection_xml(chapters=(("Essential Ideas", ("m68664", "m68664")),)), "invalid_collection"),
        (collection_xml(chapters=(("Essential Ideas", ("../../etc",)),)), "invalid_collection"),
        (collection_xml(prolog='<!DOCTYPE x [<!ENTITY a "aaaa">]>'), "xml_doctype"),
        (b"<col:collection", "invalid_xml"),
        (b"<other/>", "invalid_collection"),
    ],
)  # fmt: skip
def test_a_collection_with_another_licence_or_book_identity_is_refused(collection, code):
    with pytest.raises(ProviderError) as caught:
        parse_collection(collection, BOOK)
    assert caught.value.code == code and caught.value.retryable is False


def test_module_text_keeps_prose_with_genuine_section_locators_and_drops_everything_else():
    title, sections = parse_module(module_xml("m68664", "Chemistry in Context", CONTEXT_CONTENT), "m68664")
    assert title == "Chemistry in Context"
    assert [s.locator for s in sections] == [
        {"anchor": None, "heading": None, "level": 0},
        {"anchor": "fs-central", "heading": "Chemistry: The Central Science", "level": 2},
        {"anchor": "fs-branches", "heading": "Branches of Chemistry", "level": 3},
        {"anchor": "fs-central", "heading": "Chemistry: The Central Science", "level": 2},  # continues after 3
        {"anchor": "fs-summary", "heading": "Key Concepts and Summary", "level": 2},
    ]
    text = "\n".join(s.text for s in sections)
    assert "Water, H2O, is a compound made of two elements" in text
    assert "Chemistry in Everyday Life\nCooking an egg" in text
    assert "Analytical chemistry measures what matter contains.\nPhysical chemistry" in text
    for excluded in EXCLUDED_MARKERS:
        assert excluded not in text


@pytest.mark.parametrize(
    "data, code",
    [
        (module_xml("m68797", "Other", "<para>text</para>"), "module_mismatch"),
        (module_xml("m68664", "Doc", "<para>x</para>", prolog='<!DOCTYPE d SYSTEM "file:///etc/passwd">'), "xml_doctype"),
        (module_xml("m68664", "Doc", "<para>x</para>", prolog='<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e "x">]>'),
         "xml_doctype"),
        (b"<document xmlns='http://cnx.rice.edu/cnxml'><title>broken", "invalid_xml"),
        (b"<html><body>Not CNXML</body></html>", "not_cnxml"),
        (module_xml("m68664", "", "<para>x</para>"), "invalid_module"),
        (module_xml("m68664", "Deep", "<para>" + "<emphasis>" * 150 + "x" + "</emphasis>" * 150 + "</para>"),
         "xml_too_deep"),
    ],
)  # fmt: skip
def test_a_malformed_unsafe_or_foreign_module_is_refused(data, code):
    with pytest.raises(CandidateRefused) as caught:
        parse_module(data, "m68664")
    assert caught.value.code == code


# --- connector and transport rules (no database) -----------------------------------------------------


def _connector(github, **settings):
    return OpenStaxConnector(github, harvest_settings(**settings), "chemistry-2e")


def test_the_connector_reads_one_pinned_snapshot_and_verifies_every_blob_by_its_git_id():
    github = FakeGitHub(book_files())
    connector = _connector(github)
    snapshot = connector.head()
    collection, blobs = connector.tree(snapshot.tree)
    chapters = connector.collection(collection)
    module = connector.fetch_module(ModuleRef("m68664", blobs["m68664"], chapters[1][1]), snapshot.commit,
                                    snapshot.committed_at)  # fmt: skip
    assert github.requests == [
        ("api.github.com", f"{REPO}/branches/main", {}),
        ("api.github.com", f"{REPO}/git/trees/{github.tree_sha}", {"recursive": "1"}),
        ("api.github.com", f"{REPO}/git/blobs/{github.blob_of('collections/chemistry-2e.collection.xml')}", {}),
        ("api.github.com", f"{REPO}/git/blobs/{github.blob_of(CONTEXT)}", {}),
    ]
    assert set(blobs) == {"m68662", "m68663", "m68664", "m68797", "m68859"}
    assert (module.page_id, module.revision_key, module.commit) == (68664, github.blob_of(CONTEXT), github.commit)
    assert module.title == "Chemistry 2e: Essential Ideas - Chemistry in Context"
    assert module.committed_at.isoformat() == "2026-11-12T19:45:50+00:00"
    # A blob whose bytes do not hash to the requested git id is refused, as is a malformed blob answer.
    tampered = github.blob_of(CONTEXT)
    github.objects[tampered] = github.objects[tampered].replace(b"central science", b"central sciencE")
    with pytest.raises(CandidateRefused, match="blob_mismatch"):
        connector.fetch_module(ModuleRef("m68664", tampered, "Essential Ideas"), snapshot.commit, None)
    github.overrides["/git/blobs/" + tampered] = response({"sha": tampered, "size": 3, "encoding": "utf-8",
                                                           "content": "abc"})  # fmt: skip
    with pytest.raises(CandidateRefused, match="blob_invalid"):
        connector.fetch_module(ModuleRef("m68664", tampered, "Essential Ideas"), snapshot.commit, None)


def test_a_truncated_or_incomplete_tree_fails_closed():
    github = FakeGitHub(book_files(drop=("collections/chemistry-2e.collection.xml",)))
    connector = _connector(github)
    with pytest.raises(ProviderError, match="collection_missing"):
        connector.tree(connector.head().tree)
    github.overrides["/git/trees/" + github.tree_sha] = response({"sha": github.tree_sha, "tree": [], "truncated": True})
    with pytest.raises(ProviderError, match="tree_truncated") as caught:
        connector.tree(github.tree_sha)
    assert caught.value.retryable is False
    with pytest.raises(ProviderError, match="invalid_response"):
        connector.tree("not-a-sha")
    assert all("not-a-sha" not in path for _, path, _ in github.requests)  # refused before any request


@pytest.mark.parametrize(
    "answer, expected",
    [
        (response({"message": "API rate limit exceeded"}, 403, reset=1800.0), ("throttled", "http_403", 1800.0)),
        (response({"message": "secondary rate limit"}, 403, retry_after=60.0), ("throttled", "http_403", 60.0)),
        (response({}, 429, retry_after=30.0, reset=90.0), ("throttled", "http_429", 90.0)),
        (response({}, 503), ("throttled", "http_503", 300.0)),
        (response({"message": "Forbidden"}, 403), ("error", "http_403", False)),
        (response({"message": "Not Found"}, 404), ("error", "http_404", False)),
        (response({}, 502), ("error", "http_502", True)),
        (TransportError("response_too_large", "over 4194304 bytes", retryable=False),
         ("error", "transport_response_too_large", False)),
        (TransportError("timeout", "read", retryable=True), ("error", "transport_timeout", True)),
        (TransportError("redirect_refused", "302", retryable=False), ("error", "transport_redirect_refused", False)),
    ],
)  # fmt: skip
def test_provider_failures_timeouts_and_limits_are_classified(answer, expected):
    github = FakeGitHub(book_files())
    github.overrides["/branches/main"] = answer
    with pytest.raises(ProviderError) as caught:
        _connector(github).head()
    if expected[0] == "throttled":
        assert isinstance(caught.value, ProviderThrottled)
        assert (caught.value.code, caught.value.retry_after_seconds) == expected[1:]
    else:
        assert not isinstance(caught.value, ProviderThrottled)
        assert (caught.value.code, caught.value.retryable) == expected[1:]


def test_malformed_answers_are_refused():
    github = FakeGitHub(book_files())
    github.overrides["/branches/main"] = response({"name": "main", "commit": {"sha": "zz", "commit": {}}})
    with pytest.raises(ProviderError, match="invalid_response"):
        _connector(github).head()
    github.overrides["/branches/main"] = response(["not", "an", "object"])
    with pytest.raises(ProviderError, match="invalid_json"):
        _connector(github).head()


def test_rate_limit_reset_is_read_only_when_no_request_is_left():
    assert parse_rate_limit_reset("0", "1000", now=400.0) == 600.0
    assert parse_rate_limit_reset("0", "100", now=400.0) == 0.0
    assert parse_rate_limit_reset("12", "1000", now=400.0) is None
    assert parse_rate_limit_reset("0", "soon", now=400.0) is None
    assert parse_rate_limit_reset(None, "1000", now=400.0) is None


def test_only_an_allowlisted_book_has_a_connector():
    with pytest.raises(ProviderError, match="book_not_allowlisted"):
        OpenStaxConnector(FakeGitHub(book_files()), harvest_settings(), "biology-2e")


def test_citation_provenance_carries_the_git_version_and_never_a_number_with_it():
    resource = "00000000-0000-0000-0000-000000000001"
    version = {"resource_id": resource, "revision_id": None, "revision_key": "a" * 40,
               "canonical_url": "https://github.com/openstax/osbooks-chemistry-bundle/blob/main/modules/m1/index.cnxml",
               "revision_url": "https://github.com/openstax/osbooks-chemistry-bundle/blob/" + "b" * 40 + "/x",
               "attribution": "Chemistry 2e by Paul Flowers et al.", "licence": "CC BY-NC-SA 4.0",
               "licence_url": openstax.LICENCE_URL}  # fmt: skip
    provenance = public_provenance(resource, version)
    assert (provenance["revision_id"], provenance["revision_key"]) == (None, "a" * 40)
    for broken in ({"revision_id": 5}, {"revision_key": "A" * 40}, {"revision_key": None}):
        with pytest.raises(VerificationFailed, match="provenance_revision"):
            public_provenance(resource, {**version, **broken})


# --- harvest runs end to end (disposable database) ---------------------------------------------------


def openstax_source(world, *, enabled: bool = True, book: str = "chemistry-2e"):
    source = world._one(
        "INSERT INTO sources (kind, domain, licence, enabled, provider, language, licence_url, provider_book) "
        "VALUES ('openstax', 'api.github.com', :licence, true, 'openstax', 'en', :url, :book) RETURNING id",
        licence=openstax.LICENCE, url=openstax.LICENCE_URL, book=book,
    )  # fmt: skip
    world.sources.append(source)
    world.execute(
        "INSERT INTO harvest_provider_budgets (source_id, window_seconds, max_requests, min_interval_ms) "
        "VALUES (:s, 60, 1000, 0)", s=source,
    )  # fmt: skip
    if enabled:  # the explicit activation step (enable-source); the INSERT alone never activates
        world.execute("UPDATE sources SET enabled = true WHERE id = :s", s=source)
    return source


class Scene:
    def __init__(self, world, *, quota: int = 20, **settings) -> None:
        self.world = world
        self.course = world.course(world.user("professor", name="Rosalind Franklin"), code="CHEM 101")
        world.topic(self.course, "Private topic name")
        self.source = openstax_source(world)
        opt_in(world, self.course, self.source, quota=quota)
        self.factory = sessionmaker(bind=world.engine)
        self.settings = harvest_settings(**settings)
        self.github = FakeGitHub(book_files())

    def ctx(self) -> HarvestContext:
        def wikipedia(language):
            raise AssertionError("an OpenStax run never builds a Wikipedia connector")

        return HarvestContext(self.factory, self.settings, wikipedia, sleep=lambda seconds: None,
                              openstax_factory=lambda book: OpenStaxConnector(self.github, self.settings, book))  # fmt: skip

    def new_run(self):
        with self.factory() as session:
            run_id = runs.create_run(session, self.settings, self.course, self.source, manual=True)
            session.commit()
        assert run_id is not None
        return run_id

    def run(self, expected: str = "succeeded", run_id=None):
        run_id = run_id or self.new_run()
        assert run_harvest(run_id, self.ctx()) == expected
        return run_id

    def due(self) -> None:
        self.world.execute("UPDATE external_resources SET next_check_at = now() WHERE course_id = :c", c=self.course)

    def versions(self, page_id: int) -> list:
        return self.world.rows(
            "SELECT d.id, d.status, d.kind, d.title, d.url, d.licence, v.version, v.revision_id, v.revision_key, "
            "v.revision_url, v.revision_timestamp, v.attribution, v.licence_url, v.stale_reason, v.stale_evidence "
            "FROM documents d JOIN external_document_versions v ON v.document_id = d.id "
            "JOIN external_resources r ON r.id = v.resource_id WHERE r.page_id = :p AND r.course_id = :c "
            "ORDER BY v.version",
            p=page_id, c=self.course,
        )  # fmt: skip


@pytest.fixture
def scene(world):
    return Scene(world)


def test_a_run_ingests_the_books_chapter_modules_with_provenance_sections_and_index_jobs(scene):
    run_id = scene.run()
    # Snapshot (3 requests), then chapter modules in book order; preface and appendix are never requested.
    assert [path.removeprefix(REPO).split("/")[1] for _, path, _ in scene.github.requests][:2] == ["branches", "git"]
    assert scene.github.blob_requests() == ["collection", "m68663", "m68664", "m68797"]
    sent = repr(scene.github.requests)
    for private in (str(scene.course), "CHEM 101", "Rosalind", "Private topic"):
        assert private not in sent
    row = run_row(scene.world, run_id)
    assert row["status"] == "succeeded" and row["failure_count"] == 0
    assert (row["progress"]["accepted"], row["progress"]["refused"], row["progress"]["requests"]) == (2, 1, 6)

    (version,) = scene.versions(68664)
    assert (version["status"], version["kind"], version["version"]) == ("pending", "textbook", 1)
    assert version["title"] == "Chemistry 2e: Essential Ideas - Chemistry in Context"
    assert (version["revision_id"], version["revision_key"]) == (None, scene.github.blob_of(CONTEXT))
    assert version["url"] == openstax.canonical_url(BOOK, "m68664")
    assert version["revision_url"].endswith(f"/blob/{scene.github.commit}/modules/m68664/index.cnxml")
    assert (version["licence"], version["licence_url"]) == ("CC BY-NC-SA 4.0", openstax.LICENCE_URL)
    assert version["revision_timestamp"].isoformat().startswith("2026-11-12T19:45:50")
    for part in ("Paul Flowers", "OpenStax", "m68664", scene.github.commit, "CC BY-NC-SA 4.0", "were excluded"):
        assert part in version["attribution"]
    sections = scene.world.rows(
        "SELECT ordinal, locator, text FROM document_sections WHERE document_id = :d ORDER BY ordinal", d=version["id"]
    )
    assert [s["locator"]["anchor"] for s in sections] == [None, "fs-central", "fs-branches", "fs-central", "fs-summary"]
    assert not any(marker in s["text"] for s in sections for marker in EXCLUDED_MARKERS)
    resource = scene.world.rows("SELECT * FROM external_resources WHERE page_id = 68664 AND course_id = :c", c=scene.course)[0]
    assert (resource["state"], resource["last_revision_id"], resource["last_revision_key"]) == (
        "accepted", None, scene.github.blob_of(CONTEXT))
    jobs = scene.world.scalar(
        "SELECT count(*) FROM ingestion_jobs j JOIN documents d ON d.id = j.document_id WHERE d.course_id = :c "
        "AND j.kind = 'index' AND j.status = 'queued'", c=scene.course,
    )  # fmt: skip
    assert jobs == 2
    # The index payload carries the git version id, never an invented numeric revision.
    with scene.factory() as session:
        attribution = request_for(session, version["id"]).body
        session.rollback()
    assert b'"revision_id":null' in attribution and scene.github.blob_of(CONTEXT).encode() in attribution
    assert b'"provider":"openstax"' in attribution


def test_an_unchanged_book_costs_no_module_request_and_creates_nothing(scene):
    scene.run()
    scene.due()
    before = len(scene.github.requests)
    scene.run()
    assert scene.github.blob_requests()[4:] == ["collection", "m68663"]  # only the refused module is tried again
    assert len(scene.github.requests) - before == 4
    assert len(scene.versions(68664)) == len(scene.versions(68797)) == 1


def test_a_changed_module_becomes_a_new_version_while_the_indexed_one_stays_visible(scene):
    scene.run()
    (first,) = scene.versions(68664)
    assert index_now(scene.factory, scene.world, first["id"]) is CompletionStatus.COMPLETED
    scene.github.publish(book_files(context=CONTEXT_CONTENT.replace("central science", "central discipline")))
    scene.due()
    scene.run()
    assert scene.github.blob_requests()[4:] == ["collection", "m68664", "m68663"]  # m68797 unchanged: no request
    old, new = scene.versions(68664)
    assert (old["status"], new["status"], new["version"]) == ("indexed", "pending", 2)
    assert new["revision_key"] == scene.github.blob_of(CONTEXT) != old["revision_key"]
    assert index_now(scene.factory, scene.world, new["id"]) is CompletionStatus.COMPLETED
    old, new = scene.versions(68664)
    assert (old["status"], old["stale_reason"], new["status"]) == ("stale", "superseded", "indexed")


def test_a_module_dropped_from_the_book_is_only_observed_missing_at_first(scene):
    scene.run()
    (version,) = scene.versions(68797)
    assert index_now(scene.factory, scene.world, version["id"]) is CompletionStatus.COMPLETED
    scene.github.publish(book_files(collection=collection_xml(chapters=(("Essential Ideas", ("m68663", "m68664")),)),
                                    drop=(EQUILIBRIUM,)))  # fmt: skip
    scene.due()
    scene.run()
    resource = scene.world.rows("SELECT * FROM external_resources WHERE page_id = 68797 AND course_id = :c", c=scene.course)[0]
    assert (resource["state"], resource["missing_checks"]) == ("accepted", 1)
    (observation,) = resource["missing_evidence"]["observations"]
    assert (observation["result"], observation["module_id"], observation["commit"]) == (
        "not_in_book", "m68797", scene.github.commit)
    assert scene.versions(68797)[0]["status"] == "indexed"  # nothing staled on one observation


def test_a_licence_change_fails_the_run_before_any_module_is_fetched(scene):
    scene.github.publish(book_files(collection=collection_xml(
        licence=("https://creativecommons.org/licenses/by/4.0/", "Creative Commons Attribution 4.0 International"))))
    run_id = scene.run("failed")
    row = run_row(scene.world, run_id)
    assert (row["status"], row["error_code"]) == ("failed", "provider_licence_mismatch")
    assert scene.github.blob_requests() == ["collection"]
    assert scene.world.scalar("SELECT count(*) FROM documents WHERE course_id = :c", c=scene.course) == 0
    assert scene.world.scalar("SELECT blocked_until > now() FROM harvest_provider_budgets WHERE source_id = :s",
                              s=scene.source)  # fmt: skip


def test_a_rate_limited_request_yields_and_blocks_the_source_without_a_failure(scene):
    scene.github.overrides["/branches/main"] = response({"message": "API rate limit exceeded"}, 403, reset=1800.0)
    run_id = scene.run("yielded")
    row = run_row(scene.world, run_id)
    assert (row["status"], row["failure_count"]) == ("queued", 0)
    assert scene.world.scalar("SELECT blocked_until > now() + interval '25 minutes' FROM harvest_provider_budgets "
                              "WHERE source_id = :s", s=scene.source)  # fmt: skip


def test_a_disabled_source_or_an_opted_out_course_sends_nothing(scene):
    queued = scene.new_run()
    scene.world.execute("UPDATE sources SET enabled = false WHERE id = :s", s=scene.source)
    scene.run("gate_closed", queued)
    assert run_row(scene.world, queued)["status"] == "cancelled"
    scene.world.execute("UPDATE sources SET enabled = true WHERE id = :s", s=scene.source)
    queued = scene.new_run()
    scene.world.execute("UPDATE harvest_course_settings SET enabled = false WHERE course_id = :c", c=scene.course)
    scene.run("gate_closed", queued)
    assert scene.github.requests == []


def test_a_source_naming_another_book_fails_without_a_request(world):
    scene = Scene(world)
    scene.world.execute("UPDATE sources SET provider_book = 'biology-2e' WHERE id = :s", s=scene.source)
    run_id = scene.run("failed")
    assert run_row(world, run_id)["error_code"] == "provider_book_not_allowlisted"
    assert scene.github.requests == []


def test_the_course_quota_bounds_what_is_accepted(world):
    scene = Scene(world, quota=1)
    run_id = scene.run()
    assert run_row(world, run_id)["progress"]["quota"] == 1
    assert scene.world.scalar("SELECT count(*) FROM external_resources WHERE course_id = :c AND state = 'accepted'",
                              c=scene.course) == 1  # fmt: skip


def test_persistence_is_idempotent_and_fenced_by_the_attempt_token(scene):
    connector = OpenStaxConnector(scene.github, scene.settings, "chemistry-2e")
    snapshot = connector.head()
    _, blobs = connector.tree(snapshot.tree)
    module = connector.fetch_module(ModuleRef("m68664", blobs["m68664"], "Essential Ideas"), snapshot.commit, None)
    claim = claimed_run(scene.factory, scene.settings, scene.course, scene.source)
    assert persist.persist_revision(scene.factory, scene.settings, claim, None, module, None) is persist.Outcome.ACCEPTED
    assert persist.persist_revision(scene.factory, scene.settings, claim, None, module, None) is persist.Outcome.UNCHANGED
    scene.world.execute("UPDATE harvest_runs SET attempt_token = gen_random_uuid() WHERE id = :r", r=claim.run_id)
    other = module.__class__(**{**module.__dict__, "page_id": 68797, "module_id": "m68797"})
    assert persist.persist_revision(scene.factory, scene.settings, claim, None, other, None) is persist.Outcome.STALE
    assert len(scene.versions(68664)) == 1 and scene.versions(68797) == []


def test_another_providers_run_cannot_persist_openstax_content(world):
    from tests.harvest_support import wikipedia_source

    scene = Scene(world)
    wiki = wikipedia_source(world)
    opt_in(world, scene.course, wiki)
    connector = OpenStaxConnector(scene.github, scene.settings, "chemistry-2e")
    snapshot = connector.head()
    _, blobs = connector.tree(snapshot.tree)
    module = connector.fetch_module(ModuleRef("m68664", blobs["m68664"], "Essential Ideas"), snapshot.commit, None)
    claim = claimed_run(scene.factory, scene.settings, scene.course, wiki)
    assert persist.persist_revision(scene.factory, scene.settings, claim, None, module, None) is persist.Outcome.GATE_CLOSED


def test_creating_an_openstax_source_row_never_activates_it_and_the_schema_admits_only_valid_identities(world):
    source = openstax_source(world, enabled=False)
    assert world.scalar("SELECT enabled FROM sources WHERE id = :s", s=source) is False  # INSERT said true
    invalid = [
        "('openstax', 'openstax', 'de', 'https://x', 'chemistry-2e')",
        "('openstax', 'openstax', 'en', 'https://x', NULL)",
        "('openstax', 'openstax', NULL, 'https://x', 'chemistry-2e')",
        "('openstax', 'openstax', 'en', NULL, 'chemistry-2e')",
        "('openstax', 'openstax', 'en', 'https://x', 'Chemistry 2e')",
        "('wikipedia', 'wikipedia', 'en', 'https://x', 'chemistry-2e')",
        "('legacy', NULL, NULL, NULL, 'chemistry-2e')",
    ]
    for values in invalid:
        with pytest.raises(IntegrityError, match="sources_provider_check"):
            world.execute("INSERT INTO sources (enabled, kind, provider, language, licence_url, provider_book) "
                          f"VALUES (false, {values[1:]}")
    with pytest.raises(IntegrityError, match="external_document_versions_identity_check|external_resources_last_revision_key"):
        world.execute(
            "INSERT INTO external_resources (course_id, source_id, page_id, title, canonical_url, state, last_revision_id, "
            "last_revision_key) VALUES (:c, :s, 1, 't', 'https://x', 'accepted', 5, :k)",
            c=world.course(world.user("professor")), s=source, k="a" * 40,
        )  # fmt: skip
    resource = world._one(
        "INSERT INTO external_resources (course_id, source_id, page_id, title, canonical_url, state) "
        "VALUES (:c, :s, 2, 't', 'https://x', 'duplicate') RETURNING id", c=world.course(world.user("professor")), s=source,
    )  # fmt: skip
    document = world.document(world.scalar("SELECT course_id FROM external_resources WHERE id = :r", r=resource),
                              origin="external", title="x", status="pending")  # fmt: skip
    for revision_id, key in ((None, None), (7, "a" * 40), (None, "A" * 40), (0, None)):
        with pytest.raises(IntegrityError, match="external_document_versions_identity_check"):
            world.execute(
                "INSERT INTO external_document_versions (document_id, resource_id, version, page_id, revision_id, "
                "revision_key, canonical_url, revision_url, licence, licence_url, attribution, content_sha256, "
                "language, fetched_at) VALUES (:d, :r, 1, 2, :rev, :key, 'https://x', 'https://x', 'L', 'https://x', "
                "'a', :h, 'en', now())", d=document, r=resource, rev=revision_id, key=key, h="c" * 64,
            )  # fmt: skip
    assert git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"  # git's well-known empty blob id


def test_an_interrupted_run_decides_the_interrupted_module_again_and_never_skips_it(scene):
    """A gate closing between a module's fetch and its persistence yields the run without moving its cursor."""
    connector = OpenStaxConnector(scene.github, scene.settings, "chemistry-2e")
    original = connector.fetch_module

    def fetch_then_opt_out(ref, commit, committed_at):
        module = original(ref, commit, committed_at)
        if ref.module_id == "m68664" and not getattr(fetch_then_opt_out, "done", False):
            fetch_then_opt_out.done = True
            scene.world.execute("UPDATE harvest_course_settings SET enabled = false WHERE course_id = :c", c=scene.course)
        return module

    connector.fetch_module = fetch_then_opt_out
    ctx = HarvestContext(scene.factory, scene.settings, None, sleep=lambda seconds: None,
                         openstax_factory=lambda book: connector)  # fmt: skip
    run_id = scene.new_run()
    assert run_harvest(run_id, ctx) == "yielded"
    progress = run_row(scene.world, run_id)["progress"]
    assert progress["catalogue_index"] == 1  # m68663 decided (refused); m68664 not written, not skipped
    assert scene.versions(68664) == []
    scene.world.execute("UPDATE harvest_course_settings SET enabled = true WHERE course_id = :c", c=scene.course)
    scene.world.execute("UPDATE harvest_runs SET available_at = now() WHERE id = :r", r=run_id)
    assert run_harvest(run_id, ctx) == "succeeded"
    assert len(scene.versions(68664)) == 1 and len(scene.versions(68797)) == 1  # same run: nothing skipped
    assert run_row(scene.world, run_id)["progress"]["catalogue_index"] == 3
