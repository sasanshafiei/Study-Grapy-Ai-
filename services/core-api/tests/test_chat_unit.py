"""Chat pieces that need no database: protocol validation, SSE framing, limits, the HTTP client.

The HTTP client tests talk to tests/fake_chat_service.py over real TCP, so the
framing rules (Content-Length, Transfer-Encoding, Content-Encoding, redirects,
sizes) are exercised on real HTTP/1.1 bytes.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import time
import uuid

import pytest

from app.chat import protocol
from app.chat.client import AiChatClient, CapabilityCache, ChatUpstreamError
from app.chat.protocol import (
    Capabilities,
    Limits,
    ProtocolViolation,
    build_request,
    validate_answer,
)
from app.chat.scopes import parse_enabled_scopes
from app.chat.stream import ChatStream, error_frame, frame, message_frames, token_pieces
from app.chat.verify import (
    INSUFFICIENT_EVIDENCE,
    VerificationFailed,
    VerifiedAnswer,
    public_locator,
    public_title,
    public_url,
    snippet_for,
)
from app.config import Settings
from app.limits import ConcurrencyLimiter, RateLimiter
from tests.fake_chat_service import (
    TOKEN,
    FakeChatService,
    answer_for,
    http_response,
)

LIMITS = Limits(max_answer_chars=8000, max_citations=16, max_topics=16)
COURSE = uuid.uuid4()


def parse_sse_like_ce2(raw: str) -> list[tuple[str, object]]:
    """A line-for-line port of CE-2's parser (apps/web/src/lib/api/live.ts, ask())."""
    events = []
    buffer = raw
    boundary = buffer.find("\n\n")
    while boundary != -1:
        chunk, buffer = buffer[:boundary], buffer[boundary + 2 :]
        boundary = buffer.find("\n\n")
        event, data = "message", []
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if not data:
            continue
        events.append((event, json.loads("\n".join(data))))
    assert buffer == "", "trailing bytes outside a frame"
    return events


def _request(scope: str = "class_only") -> dict:
    return build_request(uuid.uuid4(), COURSE, scope, "What limits the yield?", LIMITS)


def _citation(marker=1, origin="class", quote="The limiting reactant limits the amount of product.", **kw):
    return {"marker": marker, "chunk_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4()), "origin": origin,
            "quote": quote, **kw}  # fmt: skip


# --- settings and scopes --------------------------------------------------------------------------


def test_chat_is_off_by_default_and_only_class_only_is_enabled():
    settings = Settings()
    assert settings.chat_enabled is False
    assert parse_enabled_scopes(settings.chat_enabled_scopes) == {"class_only"}


@pytest.mark.parametrize("raw", ["class_plus_web", "class_only,class_plus_web", "everything", "", "class_only,,"])
def test_unknown_or_unavailable_scopes_cannot_be_enabled(raw):
    with pytest.raises(ValueError):
        parse_enabled_scopes(raw)
    with pytest.raises(ValueError):
        Settings(chat_enabled_scopes=raw)


# --- capabilities --------------------------------------------------------------------------------


def _caps(**overrides):
    body = {"protocol": protocol.PROTOCOL, "service": "ai-service", "mock": False, "scopes": ["class_only"],
            "max_question_chars": 4000, **overrides}  # fmt: skip
    return body


@pytest.mark.parametrize(
    "body, reason",
    [
        (_caps(mock=True), "mock_service"),
        (_caps(mock="false"), "mock_service"),
        (_caps(mock=0), "mock_service"),
        ({k: v for k, v in _caps().items() if k != "mock"}, "capabilities_invalid"),
        (_caps(protocol="studygraph.chat/2"), "protocol_mismatch"),
        (_caps(service="other"), "capabilities_invalid"),
        (_caps(scopes=["class_only", "class_only"]), "capabilities_invalid"),
        (_caps(scopes=["anything"]), "capabilities_invalid"),
        (_caps(max_question_chars=True), "capabilities_invalid"),
        (_caps(extra=1), "capabilities_invalid"),
        ([], "capabilities_invalid"),
    ],
)
def test_mock_or_incompatible_capabilities_are_never_ready(body, reason):
    result = protocol.validate_capabilities(200, body)
    assert result.ready is False and result.reason == reason


def test_valid_capabilities():
    result = protocol.validate_capabilities(200, _caps(scopes=["class_only", "class_plus_external"]))
    assert result == Capabilities(True, "ready", frozenset({"class_only", "class_plus_external"}), 4000)
    assert protocol.validate_capabilities(500, _caps()).ready is False


# --- answer validation ----------------------------------------------------------------------------


def test_valid_grounded_answer_allows_repeated_markers():
    request = _request()
    body = answer_for(request, answer="First [1]. Again [1]. Other [2].", citations=[_citation(1), _citation(2)])
    proposed = validate_answer(body, request, LIMITS)
    assert proposed.grounded and [c.marker for c in proposed.citations] == [1, 2]


def test_ungrounded_answer_drops_text_and_requires_no_citations():
    request = _request()
    proposed = validate_answer(answer_for(request, grounded=False, answer="a guess"), request, LIMITS)
    assert proposed.grounded is False and proposed.text == "" and proposed.citations == ()
    with pytest.raises(ProtocolViolation):
        validate_answer(answer_for(request, grounded=False, citations=[_citation()]), request, LIMITS)


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda b: b.update(mock=True), "mock_answer"),
        (lambda b: b.pop("mock"), "answer_keys"),
        (lambda b: b.update(extra=1), "answer_keys"),
        (lambda b: b.update(request_id=str(uuid.uuid4())), "echo_request_id"),
        (lambda b: b.update(course_id=str(uuid.uuid4())), "echo_course_id"),
        (lambda b: b.update(scope="class_plus_external"), "echo_scope"),
        (lambda b: b.update(protocol="studygraph.chat/0"), "echo_protocol"),
        (lambda b: b.update(used_web=True), "used_web"),
        (lambda b: b.update(grounded=1), "grounded"),
        (lambda b: b.update(answer="Text [1] and [3]."), "marker_mismatch"),
        (lambda b: b.update(answer="No markers at all."), "marker_mismatch"),
        (lambda b: b.update(answer="Zero padded [01]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus unmatched [12345]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus padded [00001]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus zero [0]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus signed [+2]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus spaced [ 1 ]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus list [1,2]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus decimal [1.0]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus full-width [\uff12]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus Arabic-Indic [\u0662]."), "marker_format"),
        (lambda b: b.update(answer="Valid [1] plus out of range [17]."), "marker_range"),
        (lambda b: b.update(answer="Valid [1] plus out of range [99]."), "marker_range"),
        (lambda b: b.update(answer="Lone high surrogate \ud800 [1]."), "answer_text"),
        (lambda b: b.update(answer="Lone low surrogate \udc00 [1]."), "answer_text"),
        (lambda b: b.update(citations=[_citation(1, quote="half a pair \ud83d")]), "citation_quote"),
        (lambda b: b.update(answer="   "), "empty_answer"),
        (lambda b: b.update(answer="bell \x07 [1]"), "answer_text"),
        (lambda b: b.update(answer="x" * 8001), "answer_text"),
        (lambda b: b.update(citations=[]), "citation_count"),
        (lambda b: b.update(citations=[_citation(1), _citation(1)]), "duplicate_marker"),
        (lambda b: b.update(citations=[_citation(marker=True)]), "citation_marker"),
        (lambda b: b.update(citations=[_citation(marker=0)]), "citation_marker"),
        (lambda b: b.update(citations=[_citation(origin="external")]), "citation_origin_not_permitted"),
        (lambda b: b.update(citations=[_citation(origin="web")]), "citation_origin_not_permitted"),
        (lambda b: b.update(citations=[_citation(origin="internet")]), "citation_origin"),
        (lambda b: b.update(citations=[_citation(quote="")]), "citation_quote"),
        (lambda b: b.update(citations=[_citation(quote="x" * 501)]), "citation_quote"),
        (lambda b: b.update(citations=[_citation(title="Injected")]), "citation_keys"),
        (lambda b: b.update(citations=[_citation(url="javascript:alert(1)")]), "citation_keys"),
        (lambda b: b.update(citations=[{**_citation(), "chunk_id": "C_81F"}]), "citation_identity"),
        (lambda b: b.update(citations=[{**_citation(), "chunk_id": str(uuid.uuid4()).upper()}]), "citation_identity"),
        (lambda b: b.update(topic_ids=["t_stoich"]), "topic_ids"),
        (lambda b: b.update(topic_ids=[str(uuid.UUID(int=1))] * 2), "topic_ids"),
        (lambda b: b.update(topic_ids=[str(uuid.uuid4()) for _ in range(17)]), "too_many_topics"),
    ],
)
def test_invalid_answers_are_rejected_whole(mutate, reason):
    request = _request()
    body = answer_for(request, answer="Text [1].", citations=[_citation(1)])
    mutate(body)
    with pytest.raises(ProtocolViolation) as caught:
        validate_answer(body, request, LIMITS)
    assert caught.value.reason == reason


def test_repeated_valid_markers_and_ordinary_brackets_are_accepted():
    request = _request()
    text = "The yield [1] is capped [1]. The concentration [H+] and [A] = 0.5 M, array x[i], then [1] again."
    body = answer_for(request, answer=text, citations=[_citation(1)])
    assert validate_answer(body, request, LIMITS).text == text
    two = answer_for(request, answer="First [1], second [2], first again [1].", citations=[_citation(1), _citation(2)])
    assert [c.marker for c in validate_answer(two, request, LIMITS).citations] == [1, 2]
    pair = answer_for(request, answer="Smile \U0001f600 and \u00e9 [1].", citations=[_citation(1)])  # a valid pair is fine
    assert validate_answer(pair, request, LIMITS).grounded


def test_a_json_surrogate_pair_is_one_character_and_a_lone_one_is_rejected_in_text():
    from app.routers.chat import validate_question

    pair = json.loads('"smile \\ud83d\\ude00"')  # a valid pair decodes to one astral character
    assert pair == "smile \U0001f600" and validate_question(pair, 2000) == pair
    for bad in (json.loads('"x \\ud800"'), json.loads('"\\udc00 y"'), json.loads('"a \\ud83d b"')):
        with pytest.raises(Exception) as caught:
            validate_question(bad, 2000)
        assert getattr(caught.value, "code", None) == "invalid_question"


def test_request_carries_scope_origins_and_never_the_web():
    request = _request()
    assert request["allowed_origins"] == ["class"]
    assert set(request) == {"protocol", "request_id", "course_id", "scope", "allowed_origins", "question",
                            "max_citations", "max_answer_chars"}  # fmt: skip


# --- metadata derived from records -------------------------------------------------------------------


def test_locators_from_records():
    assert public_locator({"page": 6}) == "Page 6"
    assert public_locator({"slide": 12}) == "Slide 12"
    assert public_locator({"section": "4.4"}) == "Section 4.4"
    for bad in (None, {}, {"page": 0}, {"page": "6"}, {"page": True}, {"page": 1, "x": 2}, {"section": "a\nb"},
                {"heading": "x" * 41}, {"chapter": 1}):  # fmt: skip
        with pytest.raises(VerificationFailed):
            public_locator(bad)


def test_unsafe_urls_and_titles_are_rejected():
    assert public_url(None) is None
    assert public_url("https://openstax.org/books/chemistry-2e/pages/4-4") == "https://openstax.org/books/chemistry-2e/pages/4-4"
    for bad in ("javascript:alert(1)", "data:text/html,x", "https://user:pw@host/", "https:///nohost", "https://a b/",
                "ftp://host/x", "https://host/\x00"):  # fmt: skip
        with pytest.raises(VerificationFailed):
            public_url(bad)
    with pytest.raises(VerificationFailed):
        public_title("Lecture\x1b[31m")
    assert public_title("  Lecture 04.pdf ") == "Lecture 04.pdf"


def test_snippet_is_the_matching_database_passage():
    chunk = "Intro.\n\nThe limiting   reactant limits the amount of product formed. More."
    assert snippet_for(chunk, "The limiting reactant limits the amount\nof product") == (
        "The limiting reactant limits the amount of product"
    )
    with pytest.raises(VerificationFailed):
        snippet_for(chunk, "The limiting reactant limits nothing at all here")
    with pytest.raises(VerificationFailed):
        snippet_for(chunk, "limits")  # trivially short


# --- SSE ------------------------------------------------------------------------------------------


def test_frames_parse_with_ce2_parser_and_tokens_rebuild_the_text():
    text = "Line one [1].\nLine two with \"quotes\", unicode é → ✓ and   separators. [2]  " * 5
    verified = VerifiedAnswer(True, text, ({"marker": 1}, {"marker": 2}), (uuid.UUID(int=5),))
    raw = b"".join(message_frames(verified, "class_only")).decode("ascii")
    assert "\r" not in raw
    events = parse_sse_like_ce2(raw)
    tokens = [p["text"] for e, p in events if e == "token"]
    assert "".join(tokens) == text and len(tokens) > 1
    (kind, done), = [e for e in events if e[0] == "done"]
    assert set(done) == {"id", "role", "text", "citations", "grounded", "used_web", "scope", "topic_ids", "created_at"}
    assert done["text"] == text and done["role"] == "assistant" and done["used_web"] is False
    assert done["topic_ids"] == [str(uuid.UUID(int=5))] and done["created_at"].endswith("Z")
    assert events[-1][0] == "done"


def test_error_frame_shape():
    ((event, payload),) = parse_sse_like_ce2(error_frame("timeout").decode())
    assert event == "error" and set(payload) == {"message", "code"} and payload["code"] == "timeout"


def test_insufficient_evidence_message_has_no_citations():
    raw = b"".join(message_frames(VerifiedAnswer(False, INSUFFICIENT_EVIDENCE, (), ()), "class_only")).decode()
    done = [p for e, p in parse_sse_like_ce2(raw) if e == "done"][0]
    assert done["grounded"] is False and done["citations"] == [] and done["text"] == INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize("text", ["", "a", "word " * 100, "x" * 200, "  leading and trailing  "])
def test_token_pieces_are_lossless(text):
    assert "".join(token_pieces(text)) == text


def test_frame_is_single_line_data():
    raw = frame("token", {"text": "a\r\nb\nc"}).decode()
    assert raw.count("\n") == 3 and raw.startswith("event: token\ndata: ")


# --- limits ---------------------------------------------------------------------------------------


def test_concurrency_limiter_global_and_per_user_and_idempotent_release():
    limiter = ConcurrencyLimiter(max_total=2, max_per_user=1)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lease_a = limiter.try_acquire(a)
    assert lease_a is not None and limiter.try_acquire(a) is None  # per user
    lease_b = limiter.try_acquire(b)
    assert limiter.try_acquire(c) is None  # global
    lease_a.release()
    lease_a.release()  # idempotent: does not free someone else's slot
    assert limiter.in_use() == (1, 1)
    lease_b.release()
    assert limiter.in_use() == (0, 0)


def test_rate_limiter_window_and_bounded_memory():
    now = [0.0]
    limiter = RateLimiter(attempts=3, window=60, max_users=100, clock=lambda: now[0])
    user = uuid.uuid4()
    assert [limiter.hit(user) for _ in range(3)] == [None, None, None]
    assert limiter.hit(user) == pytest.approx(60)
    now[0] = 61
    assert limiter.hit(user) is None
    for _ in range(500):
        limiter.hit(uuid.uuid4())
    assert limiter.tracked_users() == 100


def test_capability_cache_is_bounded_in_size_and_lifetime():
    now = [0.0]
    cache = CapabilityCache(ttl=30, max_entries=4, clock=lambda: now[0])
    for i in range(10):
        cache.put((str(i), "t"), Capabilities(True, "ready"))
    assert len(cache) == 4 and cache.get(("0", "t")) is None
    now[0] = 31
    assert cache.get(("9", "t")) is None
    assert Settings(chat_capabilities_cache_seconds=300).chat_capabilities_cache_seconds == 300
    with pytest.raises(ValueError):
        Settings(chat_capabilities_cache_seconds=301)


def test_lease_is_released_when_a_stream_is_never_started():
    limiter = ConcurrencyLimiter(1, 1)
    lease = limiter.try_acquire(uuid.uuid4())

    async def produce():  # never awaited
        raise AssertionError("must not run")

    stream = ChatStream(produce, lease)
    assert limiter.in_use() == (1, 1)
    del stream
    gc.collect()
    assert lease.released and limiter.in_use() == (0, 0)


def test_lease_is_released_when_the_stream_is_cancelled():
    limiter = ConcurrencyLimiter(1, 1)
    lease = limiter.try_acquire(uuid.uuid4())
    started = asyncio.Event()

    async def produce():
        started.set()
        await asyncio.sleep(3600)
        return []

    async def main():
        sent = []

        async def receive():
            await asyncio.sleep(3600)

        async def send(message):
            sent.append(message)

        task = asyncio.create_task(ChatStream(produce, lease)({"type": "http"}, receive, send))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return sent

    sent = asyncio.run(main())
    assert sent[0]["status"] == 200 and lease.released and limiter.in_use() == (0, 0)


def test_lease_is_released_and_pipeline_cancelled_on_client_disconnect():
    limiter = ConcurrencyLimiter(1, 1)
    lease = limiter.try_acquire(uuid.uuid4())
    cancelled = []

    async def produce():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return []

    async def main():
        async def receive():
            await asyncio.sleep(0.05)
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        await asyncio.wait_for(ChatStream(produce, lease)({"type": "http"}, receive, send), 5)

    asyncio.run(main())
    assert cancelled == [True] and lease.released and limiter.in_use() == (0, 0)


def _stalled_send_run(stall_when, *, delivery_deadline=0.3, frames=(b"event: token\n\n", b"event: done\n\n")):
    """Run ChatStream with a connected client (no disconnect) whose ASGI send stalls; returns (sent, seconds, lease)."""
    limiter = ConcurrencyLimiter(1, 1)
    lease = limiter.try_acquire(uuid.uuid4())

    async def produce():
        return list(frames)

    async def main():
        sent = []

        async def receive():
            await asyncio.sleep(3600)  # the client never disconnects

        async def send(message):
            if stall_when(message, sent):
                await asyncio.Event().wait()
            sent.append(message)

        started = time.monotonic()
        await asyncio.wait_for(
            ChatStream(produce, lease, delivery_deadline=delivery_deadline)({"type": "http"}, receive, send), 5
        )
        return sent, time.monotonic() - started

    sent, seconds = asyncio.run(main())
    return sent, seconds, lease, limiter


@pytest.mark.parametrize(
    "name, stall_when",
    [
        ("response_start", lambda m, sent: m["type"] == "http.response.start"),
        ("opening_comment", lambda m, sent: m.get("body") == b": studygraph validated answer stream\n\n"),
        ("first_frame", lambda m, sent: m.get("body") == b"event: token\n\n"),
        ("final_body", lambda m, sent: m["type"] == "http.response.body" and m.get("more_body") is False),
    ],
)
def test_a_stalled_asgi_send_does_not_hold_the_lease(name, stall_when):
    sent, seconds, lease, limiter = _stalled_send_run(stall_when)
    assert lease.released and limiter.in_use() == (0, 0)
    assert 0.25 <= seconds < 2.0, f"{name}: released after {seconds:.2f}s (deadline 0.3s)"
    assert not any(m.get("more_body") is False for m in sent)  # the stream was abandoned, not completed


def test_the_delivery_deadline_does_not_cut_a_healthy_stream():
    sent, seconds, lease, _ = _stalled_send_run(lambda m, s: False, delivery_deadline=5)
    assert [m.get("more_body") for m in sent if m["type"] == "http.response.body"][-1] is False
    assert lease.released and seconds < 1


def test_delivery_deadline_setting_is_bounded():
    assert Settings().chat_delivery_deadline_seconds == 15
    with pytest.raises(ValueError):
        Settings(chat_delivery_deadline_seconds=0)
    with pytest.raises(ValueError):
        Settings(chat_delivery_deadline_seconds=121)


class _CountingClient:
    """Stands in for AiChatClient: ready, answers a valid grounded reply, counts calls."""

    def __init__(self):
        self.capability_calls = 0
        self.answer_calls = 0

    async def capabilities(self):
        self.capability_calls += 1
        return Capabilities(True, "ready", frozenset({"class_only"}), 4000)

    async def answer(self, payload):
        self.answer_calls += 1
        return answer_for(payload, answer="Fact [1].", citations=[_citation(1)])


def _job(client, deadline=0.05):
    from app.auth import CurrentUser
    from app.chat.stream import ChatJob

    user = CurrentUser(id=uuid.uuid4(), role="student", status="active")
    return ChatJob(None, client, user, COURSE, "class_only", "Q?", LIMITS, deadline)


def test_an_expired_deadline_during_verification_never_emits_an_answer(monkeypatch):
    from app.chat import stream

    def slow_verify(*args):  # a synchronous worker call that outlives the 50 ms pipeline deadline
        time.sleep(0.3)
        return VerifiedAnswer(True, "Fact [1].", ({"marker": 1},), ())

    monkeypatch.setattr(stream, "authorize_read", lambda *a: None)
    monkeypatch.setattr(stream, "_verify", slow_verify)
    client = _CountingClient()
    frames = asyncio.run(stream.run_chat(_job(client)))
    assert len(frames) == 1 and frames[0].startswith(b"event: error") and b'"code":"timeout"' in frames[0]
    assert b"done" not in b"".join(frames)


def test_an_expired_deadline_during_authorization_sends_no_upstream_request(monkeypatch):
    from app.chat import stream

    def slow_authorize(*args):
        time.sleep(0.3)

    monkeypatch.setattr(stream, "authorize_read", slow_authorize)
    monkeypatch.setattr(stream, "_verify", lambda *a: pytest.fail("must not verify"))
    client = _CountingClient()
    frames = asyncio.run(stream.run_chat(_job(client)))
    assert b'"code":"timeout"' in frames[0]
    assert client.capability_calls == 0 and client.answer_calls == 0


def test_a_cancelled_request_does_not_continue_after_the_threadpool_call(monkeypatch):
    from app.chat import stream

    def slow_authorize(*args):
        time.sleep(0.3)

    monkeypatch.setattr(stream, "authorize_read", slow_authorize)
    client = _CountingClient()

    async def main():
        task = asyncio.create_task(stream.run_chat(_job(client, deadline=30)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert client.capability_calls == 0 and client.answer_calls == 0


# --- the HTTP client against a real socket ---------------------------------------------------------------


@pytest.fixture
def fake():
    service = FakeChatService().start()
    yield service
    service.stop()


def _client(fake, **settings) -> AiChatClient:
    options = {"ai_service_url": fake.url, "ai_service_token": TOKEN, "chat_enabled": True,
               "chat_read_timeout_seconds": 2, "chat_probe_deadline_seconds": 2, "chat_answer_deadline_seconds": 3,
               **settings}  # fmt: skip
    return AiChatClient(Settings(**options))


def test_capabilities_probe_sends_bearer_and_is_cached(fake):
    client = _client(fake)
    assert asyncio.run(client.capabilities()).ready
    assert asyncio.run(client.capabilities()).ready
    assert fake.paths() == ["/ai/v1/chat/capabilities"]
    request = fake.requests[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept-encoding"] == "identity"


def test_mock_service_is_never_ready(fake):
    fake.capabilities["mock"] = True
    assert asyncio.run(_client(fake).capabilities()).reason == "mock_service"


def _raw_answer(fake, raw: bytes) -> str:
    fake.raw.append(raw)
    with pytest.raises(ChatUpstreamError) as caught:
        asyncio.run(_client(fake).answer({"protocol": protocol.PROTOCOL}))
    return caught.value.code


GOOD = json.dumps({"a": 1}).encode()


@pytest.mark.parametrize(
    "raw, code",
    [
        (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n" + GOOD, "invalid_framing"),
        (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n7\r\n{\"a\":1}\r\n0\r\n\r\n",
         "invalid_framing"),
        (http_response(200, GOOD, extra="Content-Encoding: gzip\r\n"), "unsupported_encoding"),
        (http_response(302, b"", extra="Location: http://example.invalid/\r\n"), "redirect_refused"),
        (http_response(200, b"x" * (300 * 1024)), "response_too_large"),
        (http_response(200, b'{"a": 1, "a": 2}'), "invalid_json"),
        (http_response(200, b'{"a": NaN}'), "invalid_json"),
        (http_response(200, b"\xff\xfe"), "invalid_json"),
        (http_response(200, GOOD, content_type="text/html"), "not_json"),
        (http_response(500, GOOD), "http_500"),
        (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 50\r\n\r\n" + GOOD, None),
    ],
)
def test_response_integrity(fake, raw, code):
    result = _raw_answer(fake, raw)
    if code is None:  # short body: the connection closes before Content-Length bytes arrived
        assert result.startswith("transport_") or result == "invalid_framing"
    else:
        assert result == code


def _json_reply(length_headers: str, body: bytes = GOOD) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + length_headers.encode() + b"Connection: close\r\n\r\n" + body
    )


def _answer_with(fake, raw: bytes):
    fake.raw.append(raw)
    return asyncio.run(_client(fake).answer({"protocol": protocol.PROTOCOL}))


# HTTPX/h11 normalize a repeated or comma-separated Content-Length whose values are all identical (RFC 9112
# section 6.3 allows that) and reject conflicting values. The client does not implement its own parser.
@pytest.mark.parametrize(
    "headers",
    [
        "Content-Length: 8\r\nContent-Length: 8\r\n",
        "Content-Length: 8, 8\r\n",
        "Content-Length: 8,8,8\r\n",
    ],
)
def test_identical_duplicate_content_lengths_are_normalized_by_httpx(fake, headers):
    assert _answer_with(fake, _json_reply(headers)) == {"a": 1}


@pytest.mark.parametrize(
    "headers",
    [
        "Content-Length: 8\r\nContent-Length: 9\r\n",
        "Content-Length: 8, 9\r\n",
        "Content-Length: 9\r\nContent-Length: 8\r\n",
        "Content-Length: 8, 8, 9\r\n",
        "Content-Length: +8\r\n",
        "Content-Length: 8abc\r\n",
    ],
)
def test_conflicting_or_malformed_content_lengths_are_rejected(fake, headers):
    code = _raw_answer(fake, _json_reply(headers))
    assert code.startswith("transport_") or code == "invalid_framing"


def test_answer_is_not_retried(fake):
    fake.raw.append(http_response(503, b"{}"))
    with pytest.raises(ChatUpstreamError):
        asyncio.run(_client(fake).answer({"protocol": protocol.PROTOCOL}))
    assert fake.paths() == ["/ai/v1/chat/answer"]


def test_environment_proxies_are_ignored(fake, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert asyncio.run(_client(fake).capabilities()).ready


def test_answer_deadline_bounds_a_stalled_service_and_closes_the_connection(fake):
    fake.hold()
    client = _client(fake, chat_read_timeout_seconds=30, chat_answer_deadline_seconds=0.5,
                     chat_probe_deadline_seconds=0.2)  # fmt: skip
    with pytest.raises(ChatUpstreamError) as caught:
        asyncio.run(client.answer({"protocol": protocol.PROTOCOL}))
    assert caught.value.code == "deadline"
    fake.release()
    for _ in range(100):
        if fake.closed:
            break
        asyncio.run(asyncio.sleep(0.02))
    assert fake.closed == 1


def test_token_never_appears_in_repr_or_logs(fake, caplog):
    fake.capabilities["mock"] = True
    client = _client(fake)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(client.capabilities())
    assert TOKEN not in repr(client) and TOKEN not in caplog.text
    assert "mock_service" in caplog.text
