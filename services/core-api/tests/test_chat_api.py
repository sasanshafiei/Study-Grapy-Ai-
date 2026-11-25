"""POST /api/v1/courses/{id}/chat end to end: disposable database + the raw-socket fake AI service."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.chat.db import (
    SHORT_LOCK_TIMEOUT_MS,
    SHORT_STATEMENT_TIMEOUT_MS,
    apply_short_timeouts,
    authorize_read,
)
from app.chat.verify import INSUFFICIENT_EVIDENCE
from app.harvest import persist
from tests.api_support import asgi_call
from tests.fake_chat_service import TOKEN, FakeChatService, answer_for
from tests.harvest_support import (
    claimed_run,
    fetched,
    harvest_settings,
    index_now,
    info,
    opt_in,
    versions_of,
    wikipedia_source,
)
from tests.test_chat_unit import parse_sse_like_ce2

LECTURE = (
    "Stoichiometry recap. The limiting reactant limits the amount of product that can form. "
    "Excess reactant remains after the reaction is complete."
)
QUOTE = "The limiting reactant limits the amount of product that can form."
CITATION_KEYS = {"marker", "chunk_id", "origin", "title", "locator", "url", "snippet", "provenance"}


@pytest.fixture
def fake():
    service = FakeChatService().start()
    yield service
    service.stop()


@pytest.fixture
def scene(world):
    professor = world.user("professor", name="Prof. Ada")
    student = world.user("student")
    course = world.course(professor, "Chemistry", code="CHEM 101")
    world.enroll(student, course)
    document = world.document(course, status="indexed", title="Lecture 04 - Stoichiometry.pdf")
    chunk = world.chunk(document, course, LECTURE, locator={"page": 12})
    topic = world.topic(course, "Stoichiometry")
    return {"professor": professor, "student": student, "course": course, "document": document, "chunk": chunk,
            "topic": topic}  # fmt: skip


@pytest.fixture
def chat_api(make_api, fake):
    def build(**overrides):
        settings = {
            "chat_enabled": True,
            "ai_service_url": fake.url,
            "ai_service_token": TOKEN,
            "chat_read_timeout_seconds": 5,
            "chat_probe_deadline_seconds": 2,
            "chat_answer_deadline_seconds": 4,
            **overrides,
        }
        return make_api(settings_overrides=settings)

    return build


def path(course) -> str:
    return f"/api/v1/courses/{course}/chat"


def ask(api, course, question="What limits the yield?", scope="class_only"):
    return api.client.post(
        path(course), json={"question": question, "scope": scope}, headers={"Accept": "text/event-stream"}
    )


def events(response) -> list[tuple[str, dict]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse_like_ce2(response.text)


def cite(scene, marker=1, quote=QUOTE, **overrides) -> dict:
    return {"marker": marker, "chunk_id": str(scene["chunk"]), "document_id": str(scene["document"]),
            "origin": "class", "quote": quote, **overrides}  # fmt: skip


def only_error(response) -> dict:
    received = events(response)
    assert [e for e, _ in received] == ["error"], received
    return received[0][1]


# --- success --------------------------------------------------------------------------------------


def test_grounded_answer_with_verified_citations(chat_api, fake, scene):
    text = "The limiting reactant caps the product [1]. That is why yields fall short [1]."
    fake.answer = lambda r: answer_for(r, answer=text, citations=[cite(scene)], topic_ids=[str(scene["topic"])])
    api = chat_api().as_user(scene["student"])

    received = events(ask(api, scene["course"]))

    assert "".join(p["text"] for e, p in received if e == "token") == text
    (done,) = [p for e, p in received if e == "done"]
    assert received[-1][0] == "done"
    assert done["text"] == text and done["grounded"] is True and done["scope"] == "class_only"
    assert done["used_web"] is False and done["topic_ids"] == [str(scene["topic"])]
    (citation,) = done["citations"]
    assert set(citation) == CITATION_KEYS
    assert citation == {"marker": 1, "chunk_id": str(scene["chunk"]), "origin": "class",
                        "title": "Lecture 04 - Stoichiometry.pdf", "locator": "Page 12", "url": None,
                        "snippet": QUOTE, "provenance": None}  # fmt: skip
    capabilities, answer = fake.requests
    assert capabilities.path == "/ai/v1/chat/capabilities" and answer.path == "/ai/v1/chat/answer"
    sent = answer.json()
    assert sent["course_id"] == str(scene["course"]) and sent["scope"] == "class_only"
    assert sent["allowed_origins"] == ["class"] and sent["question"] == "What limits the yield?"
    assert answer.headers["authorization"] == f"Bearer {TOKEN}"
    assert api.app.state.chat_limiter.in_use() == (0, 0)


def test_owner_professor_may_ask_too(chat_api, fake, scene):
    fake.answer = lambda r: answer_for(r, grounded=False)
    assert events(ask(chat_api().as_user(scene["professor"]), scene["course"]))[-1][0] == "done"


def test_ungrounded_answer_is_the_ce_message_without_citations(chat_api, fake, scene):
    fake.answer = lambda r: answer_for(r, grounded=False, answer="Maybe it is the catalyst?")
    received = events(ask(chat_api().as_user(scene["student"]), scene["course"]))
    done = received[-1][1]
    assert done["grounded"] is False and done["citations"] == [] and done["text"] == INSUFFICIENT_EVIDENCE
    assert "catalyst" not in json.dumps(received)


def test_external_citation_in_an_enabled_external_scope(chat_api, fake, world, scene):
    source = world.source(enabled=True)
    external = world.document(scene["course"], origin="external", status="indexed", source_id=source,
                              title="OpenStax Chemistry 2e, 4.4 Reaction Yields",
                              url="https://openstax.org/books/chemistry-2e/pages/4-4")  # fmt: skip
    text_value = "Work out how much product each reactant could make, then compare the amounts."
    chunk = world.chunk(external, scene["course"], text_value, origin="external", locator={"section": "4.4"})
    citation = {"marker": 1, "chunk_id": str(chunk), "document_id": str(external), "origin": "external",
                "quote": "how much product each reactant could make"}  # fmt: skip
    fake.answer = lambda r: answer_for(r, answer="Compare yields [1].", citations=[citation])
    api = chat_api(chat_enabled_scopes="class_only,class_plus_external").as_user(scene["student"])
    done = events(ask(api, scene["course"], scope="class_plus_external"))[-1][1]
    assert done["citations"][0]["url"] == "https://openstax.org/books/chemistry-2e/pages/4-4"
    assert done["citations"][0]["locator"] == "Section 4.4" and done["citations"][0]["origin"] == "external"
    assert done["citations"][0]["provenance"] is None  # not harvested: no version record, nothing is filled in
    assert fake.requests[-1].json()["allowed_origins"] == ["class", "external"]


# --- WP-5 / F09: version-specific provenance of harvested external material -----------------------------

WIKI_TITLE = "Chemical equilibrium"
EXTERNAL = {"chat_enabled_scopes": "class_only,class_plus_external"}


class Harvested:
    """Wikipedia pages harvested into a course through the real persistence path."""

    def __init__(self, world, course) -> None:
        self.world, self.course = world, course
        self.source = wikipedia_source(world)
        opt_in(world, course, self.source)
        self.factory = sessionmaker(bind=world.engine)
        self.settings = harvest_settings()
        self.run = claimed_run(self.factory, self.settings, course, self.source)

    def revision(self, rev: int, *, page: int = 7, extra: str = "") -> dict:
        page_info, revision = info(page, rev, WIKI_TITLE), fetched(page, rev, WIKI_TITLE, extra=extra)
        persist.persist_revision(self.factory, self.settings, self.run, page_info, revision, None)
        return versions_of(self.world, page, self.course)[-1]

    def citation(self, version: dict) -> dict:
        chunk = self.world.rows("SELECT id, text FROM chunks WHERE document_id = :d ORDER BY id LIMIT 1", d=version["id"])[0]
        return {"marker": 1, "chunk_id": str(chunk["id"]), "document_id": str(version["id"]), "origin": "external",
                "quote": " ".join(chunk["text"].split())[:60]}  # fmt: skip


def _ask_citing(api, fake, scene, citation):
    fake.answer = lambda r: answer_for(r, answer="Equilibrium is dynamic [1].", citations=[citation])
    return ask(api, scene["course"], scope="class_plus_external")


def test_external_citation_carries_the_provenance_of_the_exact_cited_version(chat_api, fake, world, scene):
    harvest = Harvested(world, scene["course"])
    v1 = harvest.revision(42)
    index_now(harvest.factory, world, v1["id"])
    v2 = harvest.revision(50, extra=" Revised.")  # pending: version 1 stays the visible one
    api = chat_api(**EXTERNAL).as_user(scene["student"])
    (citation,) = events(_ask_citing(api, fake, scene, harvest.citation(v1)))[-1][1]["citations"]
    stored = world.rows("SELECT * FROM external_document_versions WHERE document_id = :d", d=v1["id"])[0]
    assert set(citation) == CITATION_KEYS and citation["url"] == "https://en.wikipedia.org/wiki/Chemical_equilibrium"
    assert citation["provenance"] == {
        "source_url": "https://en.wikipedia.org/wiki/Chemical_equilibrium", "revision_id": 42,
        "revision_url": "https://en.wikipedia.org/w/index.php?oldid=42", "attribution": stored["attribution"],
        "licence": "CC BY-SA 4.0", "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    }  # fmt: skip
    assert "revision 42" in citation["provenance"]["attribution"]
    # Version 2 indexed: its citation carries revision 50; the superseded version 1 cannot be cited any more.
    index_now(harvest.factory, world, v2["id"])
    (citation,) = events(_ask_citing(api, fake, scene, harvest.citation(v2)))[-1][1]["citations"]
    assert (citation["provenance"]["revision_id"], citation["provenance"]["revision_url"]) == (
        50, "https://en.wikipedia.org/w/index.php?oldid=50",
    )  # fmt: skip
    assert "revision 50" in citation["provenance"]["attribution"]
    assert only_error(_ask_citing(api, fake, scene, harvest.citation(v1)))["code"] == "rejected"


@pytest.mark.parametrize(
    "tamper",
    [
        "DELETE FROM external_document_versions WHERE document_id = :d",
        "UPDATE external_document_versions SET licence_url = 'javascript:alert(1)' WHERE document_id = :d",
        "UPDATE external_document_versions SET revision_url = 'https://user:pw@en.wikipedia.org/' WHERE document_id = :d",
        "UPDATE external_document_versions SET attribution = E'Wikipedia\\nInjected' WHERE document_id = :d",
        "UPDATE external_document_versions SET resource_id = :other, version = 99 WHERE document_id = :d",
    ],
)
def test_missing_or_tampered_provenance_rejects_the_whole_answer(chat_api, fake, world, scene, tamper):
    harvest = Harvested(world, scene["course"])
    v1 = harvest.revision(42)
    index_now(harvest.factory, world, v1["id"])
    harvest.revision(90, page=9, extra=" Another article.")
    other = world.scalar("SELECT id FROM external_resources WHERE course_id = :c AND page_id = 9", c=scene["course"])
    world.execute(tamper, d=v1["id"], other=other)
    api = chat_api(**EXTERNAL).as_user(scene["student"])
    assert only_error(_ask_citing(api, fake, scene, harvest.citation(v1)))["code"] == "rejected"


def test_model_supplied_attribution_is_never_accepted(chat_api, fake, world, scene):
    harvest = Harvested(world, scene["course"])
    v1 = harvest.revision(42)
    index_now(harvest.factory, world, v1["id"])
    api = chat_api(**EXTERNAL).as_user(scene["student"])
    forged = {**harvest.citation(v1), "attribution": "Public domain, no attribution needed", "licence": "CC0"}
    response = _ask_citing(api, fake, scene, forged)
    assert only_error(response)["code"] == "rejected"
    assert "Public domain" not in response.text and "CC0" not in response.text


# --- before the stream: authentication, authorization, switches, body ------------------------------


def test_chat_is_disabled_by_default_and_contacts_nothing(api, fake, scene):
    response = ask(api.as_user(scene["student"]), scene["course"])
    assert response.status_code == 503 and response.json()["code"] == "chat_unavailable"
    assert fake.requests == []


def test_unauthenticated_and_outsiders_are_refused_before_the_body_is_read(chat_api, world, fake, scene):
    api = chat_api()
    headers = {"content-type": "application/json", "content-length": "2"}
    assert asgi_call(api.as_user(None).app, "POST", path(scene["course"]), headers, [b"{}"]).receive_calls == 0
    outsider = world.user("student")
    result = asgi_call(api.as_user(outsider).app, "POST", path(scene["course"]), headers, [b"{}"])
    assert result.status == 404 and result.receive_calls == 0
    disabled = world.user("student", status="disabled")
    world.enroll(disabled, scene["course"])
    assert ask(api.as_user(disabled), scene["course"]).status_code == 403
    assert ask(api.as_user(scene["student"]), uuid.uuid4()).status_code == 404
    assert fake.requests == []


def test_missing_token_means_unavailable(make_api, fake, scene):
    api = make_api(settings_overrides={"chat_enabled": True, "ai_service_url": fake.url}).as_user(scene["student"])
    assert ask(api, scene["course"]).status_code == 503 and fake.requests == []


@pytest.mark.parametrize(
    "body, status, code",
    [
        ({"question": "Q?"}, 422, "invalid_fields"),
        ({"question": "Q?", "scope": "class_only", "stream": True}, 422, "invalid_fields"),
        ({"question": "", "scope": "class_only"}, 422, "invalid_question"),
        ({"question": "   ", "scope": "class_only"}, 422, "invalid_question"),
        ({"question": 7, "scope": "class_only"}, 422, "invalid_question"),
        ({"question": "a\x00b", "scope": "class_only"}, 422, "invalid_question"),
        ({"question": "x" * 2001, "scope": "class_only"}, 422, "question_too_long"),
        ({"question": "Q?", "scope": "everything"}, 422, "invalid_scope"),
        ({"question": "Q?", "scope": "class_plus_web"}, 422, "scope_unavailable"),
        ({"question": "Q?", "scope": "class_plus_external"}, 422, "scope_unavailable"),
    ],
)
def test_strict_body_validation(chat_api, fake, scene, body, status, code):
    response = chat_api().as_user(scene["student"]).client.post(path(scene["course"]), json=body)
    assert response.status_code == status and response.json()["code"] == code
    assert fake.requests == []


@pytest.mark.parametrize("escaped", ["\\ud800", "\\udc00", "x \\ud83d y"])
def test_lone_surrogates_in_the_question_are_refused(chat_api, fake, scene, escaped):
    api = chat_api().as_user(scene["student"])
    body = ('{"question":"What is ' + escaped + '?","scope":"class_only"}').encode()
    response = api.client.post(path(scene["course"]), content=body, headers={"content-type": "application/json"})
    assert response.status_code == 422 and response.json()["code"] == "invalid_question"
    assert fake.requests == []
    paired = b'{"question":"What is \\ud83d\\ude00?","scope":"class_only"}'  # a valid pair is one character
    fake.answer = lambda r: answer_for(r, grounded=False)
    ok = api.client.post(path(scene["course"]), content=paired, headers={"content-type": "application/json"})
    assert ok.status_code == 200


@pytest.mark.parametrize("answer", ["Text \ud800 [1].", "Text \udfff [1]."])
def test_lone_surrogates_in_the_answer_are_never_shown(chat_api, fake, scene, answer):
    fake.answer = lambda r: answer_for(r, answer=answer, citations=[cite(scene)])
    response = ask(chat_api().as_user(scene["student"]), scene["course"])
    assert only_error(response)["code"] == "rejected"


@pytest.mark.parametrize("answer", ["Fact [1] and [12345].", "Fact [1] and [00001].", "Fact [1] and [17]."])
def test_unmatched_or_noncanonical_numeric_markers_are_never_shown(chat_api, fake, scene, answer):
    fake.answer = lambda r: answer_for(r, answer=answer, citations=[cite(scene)])
    response = ask(chat_api().as_user(scene["student"]), scene["course"])
    assert only_error(response)["code"] == "rejected" and "Fact" not in response.text


def test_ambiguous_or_oversized_bodies(chat_api, fake, scene):
    api = chat_api(chat_max_body_bytes=512).as_user(scene["student"])
    json_headers = {"content-type": "application/json"}
    duplicate = b'{"question":"a","question":"b","scope":"class_only"}'
    assert api.client.post(path(scene["course"]), content=duplicate, headers=json_headers).status_code == 400
    gz = {**json_headers, "content-encoding": "gzip"}
    assert api.client.post(path(scene["course"]), content=b"{}", headers=gz).status_code == 415
    # No Content-Length: the bytes actually received are counted.
    chunks = [b'{"question":"' + b"x" * 300, b"y" * 300 + b'","scope":"class_only"}']
    result = asgi_call(api.app, "POST", path(scene["course"]), json_headers, chunks)
    assert result.status == 413
    # A Content-Length that does not match the bytes received is not trusted.
    small = [b'{"question":"Q?","scope":"class_only"}']
    lying = {**json_headers, "content-length": "10"}
    assert asgi_call(api.app, "POST", path(scene["course"]), lying, small).status == 400
    assert asgi_call(api.app, "POST", path(scene["course"]), lying, chunks).status == 413
    assert fake.requests == []


def test_slow_body_is_cut_off(chat_api, fake, scene):
    api = chat_api(chat_body_timeout_seconds=0.3).as_user(scene["student"])
    sent = []

    async def receive():
        if not sent:
            sent.append(1)
            return {"type": "http.request", "body": b'{"question":', "more_body": True}
        await asyncio.sleep(3600)

    messages = []

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path(scene["course"]), "raw_path": path(scene["course"]).encode(), "query_string": b"",
             "root_path": "", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1),
             "server": ("testserver", 80), "state": {}}  # fmt: skip
    asyncio.run(asyncio.wait_for(api.app(scope, receive, send), 10))
    assert messages[0]["status"] == 408 and fake.requests == []


# --- concurrency slots -----------------------------------------------------------------------------


def _ask_in_thread(api, course):
    results = []
    thread = threading.Thread(target=lambda: results.append(ask(api, course)))
    thread.start()
    return thread, results


@pytest.mark.parametrize("second", ["same_user", "other_user"])
def test_slots_are_taken_before_any_upstream_io(chat_api, fake, world, scene, second):
    options = {"chat_max_concurrent_per_user": 1, "chat_max_concurrent": 1 if second == "other_user" else 4}
    first_api = chat_api(**options).as_user(scene["student"])
    fake.answer = lambda r: answer_for(r, grounded=False)
    fake.hold()
    thread, results = _ask_in_thread(first_api, scene["course"])
    assert fake.waiting.wait(10)
    other = scene["student"]
    if second == "other_user":
        other = world.user("student")
        world.enroll(other, scene["course"])
    # Same application (same limiter), another client.
    from fastapi.testclient import TestClient

    first_api.principal = other
    with TestClient(first_api.app) as client:
        busy = client.post(path(scene["course"]), json={"question": "Q?", "scope": "class_only"})
    assert busy.status_code == 429 and busy.json()["code"] == "chat_busy" and "Retry-After" in busy.headers
    assert len(fake.requests) == 1  # the busy request made no upstream request at all
    first_api.principal = scene["student"]
    fake.release()
    thread.join(20)
    assert results[0].status_code == 200
    assert first_api.app.state.chat_limiter.in_use() == (0, 0)


def test_no_database_session_is_held_during_upstream_calls(chat_api, fake, world, scene):
    api = chat_api().as_user(scene["student"])
    fake.answer = lambda r: answer_for(r, grounded=False)
    fake.hold()
    thread, results = _ask_in_thread(api, scene["course"])
    assert fake.waiting.wait(10)
    held = world.scalar(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND backend_type = 'client backend' "
        "AND (state LIKE 'idle in transaction%' OR state = 'active')"
    )
    fake.release()
    thread.join(20)
    assert held == 0 and results[0].status_code == 200


# --- failures inside the stream ---------------------------------------------------------------------


def test_mock_service_never_answers(chat_api, fake, scene):
    fake.capabilities["mock"] = True
    fake.answer = lambda r: answer_for(r, answer="Fake [1].", citations=[cite(scene)])
    error = only_error(ask(chat_api().as_user(scene["student"]), scene["course"]))
    assert error["code"] == "ai_unavailable" and fake.paths() == ["/ai/v1/chat/capabilities"]


def test_mock_answer_is_rejected_even_from_a_real_looking_service(chat_api, fake, scene):
    fake.answer = lambda r: answer_for(r, answer="Fake [1].", citations=[cite(scene)], mock=True)
    assert only_error(ask(chat_api().as_user(scene["student"]), scene["course"]))["code"] == "rejected"


def test_scope_not_offered_by_the_service_is_not_replaced(chat_api, fake, scene):
    fake.capabilities["scopes"] = ["class_plus_external"]
    fake.answer = lambda r: answer_for(r, grounded=False)
    assert only_error(ask(chat_api().as_user(scene["student"]), scene["course"]))["code"] == "scope_unavailable"
    assert fake.paths() == ["/ai/v1/chat/capabilities"]


def _variant(world, scene, name):
    """A citation (and topics) that must fail verification, by name."""
    other_course = world.course(world.user("professor"), "Other")
    if name == "other_course_chunk":
        doc = world.document(other_course, status="indexed")
        chunk = world.chunk(doc, other_course, LECTURE)
        return cite(scene, chunk_id=str(chunk), document_id=str(doc)), []
    if name == "chunk_course_differs_from_document":
        chunk = world.chunk(scene["document"], other_course, LECTURE)
        return cite(scene, chunk_id=str(chunk)), []
    if name in ("pending_document", "stale_document", "failed_document"):
        doc = world.document(scene["course"], status=name.split("_")[0])
        chunk = world.chunk(doc, scene["course"], LECTURE)
        return cite(scene, chunk_id=str(chunk), document_id=str(doc)), []
    if name == "invented_chunk":
        return cite(scene, chunk_id=str(uuid.uuid4())), []
    if name == "wrong_document":
        return cite(scene, document_id=str(world.document(scene["course"], status="indexed"))), []
    if name == "invented_quote":
        return cite(scene, quote="The catalyst limits the amount of product that can form."), []
    if name == "origin_claimed_class_for_external":
        source = world.source()
        doc = world.document(None, origin="external", status="indexed", source_id=source)
        chunk = world.chunk(doc, None, LECTURE, origin="external")
        return cite(scene, chunk_id=str(chunk), document_id=str(doc)), []
    if name == "web_cache_chunk":
        doc = world.document(scene["course"], origin="web_cache", status="indexed")
        chunk = world.chunk(doc, scene["course"], LECTURE, origin="web_cache")
        return cite(scene, chunk_id=str(chunk), document_id=str(doc)), []
    if name == "unknown_locator":
        chunk = world.chunk(scene["document"], scene["course"], LECTURE, locator={"chapter": 4})
        return cite(scene, chunk_id=str(chunk)), []
    if name == "unsafe_title":
        doc = world.document(scene["course"], status="indexed", title="Lecture\x1b[2J")
        chunk = world.chunk(doc, scene["course"], LECTURE)
        return cite(scene, chunk_id=str(chunk), document_id=str(doc)), []
    if name == "other_course_topic":
        return cite(scene), [str(world.topic(other_course, "Elsewhere"))]
    if name == "invented_topic":
        return cite(scene), [str(uuid.uuid4())]
    raise AssertionError(name)


@pytest.mark.parametrize(
    "name",
    ["other_course_chunk", "chunk_course_differs_from_document", "pending_document", "stale_document",
     "failed_document", "invented_chunk", "wrong_document", "invented_quote", "origin_claimed_class_for_external",
     "web_cache_chunk", "unknown_locator", "unsafe_title", "other_course_topic", "invented_topic"],
)  # fmt: skip
def test_unverifiable_answers_are_never_shown(chat_api, fake, world, scene, name):
    citation, topics = _variant(world, scene, name)
    fake.answer = lambda r: answer_for(r, answer="Secret from elsewhere [1].", citations=[citation], topic_ids=topics)
    response = ask(chat_api().as_user(scene["student"]), scene["course"])
    assert only_error(response)["code"] == "rejected"
    assert "Secret from elsewhere" not in response.text and LECTURE[:30] not in response.text


@pytest.mark.parametrize(
    "problem",
    ["disabled_source", "unsafe_url", "other_course_external", "shared_null_external", "chunk_null_document_course",
     "document_null_chunk_course", "chunk_other_course"],
)  # fmt: skip
def test_external_material_needs_an_enabled_source_and_safe_metadata(chat_api, fake, world, scene, problem):
    source = world.source(enabled=problem != "disabled_source")
    url = "javascript:alert(1)" if problem == "unsafe_url" else "https://openstax.org/x"
    other = world.course(world.user("professor"))
    mine = scene["course"]
    doc_course, chunk_course = {
        "other_course_external": (other, other),
        "shared_null_external": (None, None),  # globally shared material: deferred to M6
        "chunk_null_document_course": (mine, None),
        "document_null_chunk_course": (None, mine),
        "chunk_other_course": (mine, other),
    }.get(problem, (mine, mine))
    doc = world.document(doc_course, origin="external", status="indexed", source_id=source, url=url)
    chunk = world.chunk(doc, chunk_course, LECTURE, origin="external")
    citation = {"marker": 1, "chunk_id": str(chunk), "document_id": str(doc), "origin": "external", "quote": QUOTE}
    fake.answer = lambda r: answer_for(r, answer="External [1].", citations=[citation])
    api = chat_api(chat_enabled_scopes="class_only,class_plus_external").as_user(scene["student"])
    assert only_error(ask(api, scene["course"], scope="class_plus_external"))["code"] == "rejected"


def test_external_citation_is_refused_in_class_only(chat_api, fake, world, scene):
    source = world.source()
    doc = world.document(None, origin="external", status="indexed", source_id=source)
    chunk = world.chunk(doc, None, LECTURE, origin="external")
    citation = {"marker": 1, "chunk_id": str(chunk), "document_id": str(doc), "origin": "external", "quote": QUOTE}
    fake.answer = lambda r: answer_for(r, answer="External [1].", citations=[citation])
    assert only_error(ask(chat_api().as_user(scene["student"]), scene["course"]))["code"] == "rejected"


def test_access_lost_during_the_upstream_call_stops_the_answer(chat_api, fake, world, scene):
    def unenroll(_request):
        world.execute("DELETE FROM enrollments WHERE user_id = :u", u=scene["student"].id)

    fake.on_answer = unenroll
    fake.answer = lambda r: answer_for(r, answer="Answer [1].", citations=[cite(scene)])
    response = ask(chat_api().as_user(scene["student"]), scene["course"])
    assert only_error(response)["code"] == "access" and "Answer" not in response.text


def test_upstream_timeout_is_an_error_event_and_frees_everything(chat_api, fake, scene):
    api = chat_api(chat_answer_deadline_seconds=0.5, chat_probe_deadline_seconds=0.3).as_user(scene["student"])
    asyncio.run(api.app.state.chat_client.capabilities())  # warm the cache, then stall the answer
    fake.hold()
    started = time.monotonic()
    error = only_error(ask(api, scene["course"]))
    assert error["code"] == "timeout" and time.monotonic() - started < 5
    assert api.app.state.chat_limiter.in_use() == (0, 0)
    assert len([p for p in fake.paths() if p.endswith("/answer")]) == 1  # not retried
    fake.release()


def test_client_disconnect_cancels_the_upstream_call_and_frees_the_slot(chat_api, fake, scene):
    api = chat_api(chat_answer_deadline_seconds=30, chat_read_timeout_seconds=30).as_user(scene["student"])
    fake.answer = lambda r: answer_for(r, grounded=False)
    fake.hold()
    body = json.dumps({"question": "Q?", "scope": "class_only"}).encode()
    state = {"sent": False}
    messages = []

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        while not fake.waiting.is_set():
            await asyncio.sleep(0.02)
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path(scene["course"]), "raw_path": path(scene["course"]).encode(), "query_string": b"",
             "root_path": "", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1),
             "server": ("testserver", 80), "state": {}}  # fmt: skip
    started = time.monotonic()
    asyncio.run(asyncio.wait_for(api.app(scope, receive, send), 20))
    assert time.monotonic() - started < 10
    assert api.app.state.chat_limiter.in_use() == (0, 0)
    sent = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert b"event:" not in sent  # nothing but the opening comment
    for _ in range(100):
        if fake.closed:
            break
        time.sleep(0.05)
    assert fake.closed >= 1  # the held upstream connection was closed by core-api
    fake.release()


# --- bounded resource lifetime ----------------------------------------------------------------------


@pytest.mark.parametrize("stall_after", [1, 2])  # the opening comment / the first token frame never gets accepted
def test_a_stalled_send_without_disconnect_releases_the_slot(chat_api, fake, scene, stall_after):
    fake.answer = lambda r: answer_for(r, answer="Compare yields [1].", citations=[cite(scene)])
    api = chat_api(chat_delivery_deadline_seconds=0.5).as_user(scene["student"])
    body = [json.dumps({"question": "What limits the yield?", "scope": "class_only"}).encode()]
    headers = {"content-type": "application/json", "content-length": str(len(body[0]))}
    started = time.monotonic()
    result = asgi_call(api.app, "POST", path(scene["course"]), headers, body, stall_send_after=stall_after)
    assert result.status == 200
    assert time.monotonic() - started < 5
    assert api.app.state.chat_limiter.in_use() == (0, 0)
    # the slot is usable again at once
    again = ask(api, scene["course"])
    assert again.status_code == 200 and events(again)[-1][0] == "done"


def test_chat_database_transactions_have_statement_and_lock_timeouts(chat_api, scene, world):
    factory = chat_api().app.state.session_factory
    with factory() as session:
        apply_short_timeouts(session)
        assert session.execute(text("SHOW statement_timeout")).scalar() == f"{SHORT_STATEMENT_TIMEOUT_MS // 1000}s"
        assert session.execute(text("SHOW lock_timeout")).scalar() == f"{SHORT_LOCK_TIMEOUT_MS // 1000}s"
        session.rollback()
    with factory() as session:  # SET LOCAL ends with the transaction: a pooled connection is not left limited
        assert session.execute(text("SHOW lock_timeout")).scalar() == "0"
        session.rollback()


def test_authorization_does_not_wait_forever_for_a_lock(chat_api, scene, world):
    factory = chat_api().app.state.session_factory
    with world.engine.connect() as blocker:
        blocker.exec_driver_sql("LOCK TABLE enrollments IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(OperationalError):
            authorize_read(factory, scene["student"], scene["course"])
        assert 1.5 < time.monotonic() - started < 6  # lock_timeout (2 s), not an unbounded wait
        blocker.rollback()
    authorize_read(factory, scene["student"], scene["course"])  # fine again once the lock is gone


def test_authorization_transaction_is_read_only(chat_api, scene, monkeypatch):
    from app.chat import db

    seen = []
    real = db.require_read

    def spy(session, user, course_id):
        seen.append(session.execute(text("SHOW transaction_read_only")).scalar())
        return real(session, user, course_id)

    monkeypatch.setattr(db, "require_read", spy)
    authorize_read(chat_api().app.state.session_factory, scene["student"], scene["course"])
    assert seen == ["on"]
