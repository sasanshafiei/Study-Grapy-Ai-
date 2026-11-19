"""The AI index client: bounded HTTP, strict validation, readiness, mock refusal. No database.

Servers here are test doubles in this process: tests/fake_ai_service.py (the
proposed protocol) and raw sockets for misbehaving peers.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.indexing.client import (
    AiIndexClient,
    HttpResponse,
    InvalidServiceUrl,
    Outcome,
    classify,
    parse_service_url,
    validate_capabilities,
)
from app.indexing.payload import PROTOCOL, build_request
from tests.fake_ai_service import TOKEN, FakeAiService, Fault


def make_settings(url: str, **overrides) -> Settings:
    values = dict(
        ai_service_url=url, ai_service_token=TOKEN, indexing_enabled=True,
        ai_connect_timeout_seconds=1, ai_read_timeout_seconds=1, ai_request_deadline_seconds=3,
        ai_probe_timeout_seconds=1, ai_probe_cache_seconds=30,
    )  # fmt: skip
    values.update(overrides)
    return Settings(**values)


def a_request(course=None):
    return build_request(
        {"id": uuid.uuid4(), "course_id": course or uuid.uuid4(), "origin": "class", "kind": None, "title": "T",
         "url": None, "licence": None, "language": "en", "content_hash": None, "page_count": 2},
        [{"ordinal": 0, "text": "alpha", "locator": {"page": 1}}, {"ordinal": 1, "text": "beta", "locator": {"page": 2}}],
    )  # fmt: skip


@pytest.fixture
def fake():
    service = FakeAiService().start()
    yield service
    service.stop()


class RawServer:
    """Accepts connections and hands each to `handler(conn)`; counts connections."""

    def __init__(self, handler) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.connections = 0
        self._handler = handler
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._run, args=(conn,), daemon=True).start()

    def _run(self, conn) -> None:
        try:
            conn.settimeout(10)
            self._handler(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.sock.close()


def _read_request(conn) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(rest) < length:
        rest += conn.recv(65536)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- readiness and mock refusal -----------------------------------------------------------------


def test_compatible_service_is_ready_and_the_probe_is_authenticated(fake):
    client = AiIndexClient(make_settings(fake.url))
    readiness = client.readiness()
    assert (readiness.ready, readiness.max_request_bytes) == (True, fake.max_request_bytes)
    assert fake.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "capabilities, reason",
    [
        # The shape of the repository's mock /health (mock: true) and similar.
        ({"status": "ok", "service": "ai-service", "mock": True}, "capabilities_invalid"),
        ({"protocol": PROTOCOL, "service": "ai-service", "mock": True, "completion": ["sync"], "max_request_bytes": 9}, "mock_service"),
        ({"protocol": PROTOCOL, "service": "ai-service", "mock": "false", "completion": ["sync"], "max_request_bytes": 9}, "mock_service"),
        ({"protocol": PROTOCOL, "service": "ai-service", "mock": 0, "completion": ["sync"], "max_request_bytes": 9}, "mock_service"),
        ({"protocol": "studygraph.index/2", "service": "ai-service", "mock": False, "completion": ["sync"], "max_request_bytes": 9}, "protocol_mismatch"),
        ({"protocol": PROTOCOL, "service": "ai-service", "mock": False, "completion": ["async"], "max_request_bytes": 9}, "sync_completion_unsupported"),
        ({"protocol": PROTOCOL, "service": "ai-service", "mock": False, "completion": ["sync"], "max_request_bytes": True}, "capabilities_invalid"),
        ({"protocol": PROTOCOL, "service": "ai-service", "mock": False, "completion": ["sync"], "max_request_bytes": 9, "x": 1}, "capabilities_invalid"),
    ],
)  # fmt: skip
def test_mock_or_incompatible_services_are_not_ready(fake, capabilities, reason):
    fake.capabilities = capabilities
    readiness = AiIndexClient(make_settings(fake.url)).readiness()
    assert (readiness.ready, readiness.reason) == (False, reason)
    assert fake.index_requests() == []


def test_the_repository_mock_endpoints_are_refused():
    # services/ai-service has /health (mock: true) but no capabilities endpoint: 404.
    def mock(conn):
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nContent-Length: 22\r\n\r\n"
                     b'{"detail":"Not Found"}')  # fmt: skip

    server = RawServer(mock)
    try:
        readiness = AiIndexClient(make_settings(server.url)).readiness()
        assert (readiness.ready, readiness.reason) == (False, "capabilities_http_404")
    finally:
        server.close()


def test_wrong_credentials_are_not_ready(fake):
    readiness = AiIndexClient(make_settings(fake.url, ai_service_token="w" * 40)).readiness()
    assert (readiness.ready, readiness.reason) == (False, "capabilities_http_401")


def test_readiness_is_cached_both_ways_so_a_down_service_is_not_hammered(fake):
    now = [1000.0]
    fake.capabilities_status = 503
    client = AiIndexClient(make_settings(fake.url, ai_probe_cache_seconds=30), clock=lambda: now[0])
    for _ in range(20):
        assert not client.readiness().ready
    assert len(fake.requests) == 1
    fake.capabilities_status = 200
    now[0] += 29
    assert not client.readiness().ready  # still cached
    now[0] += 2
    assert client.readiness().ready
    assert len(fake.requests) == 2
    client.mark_unready("ai_auth_refused")
    assert client.readiness().reason == "ai_auth_refused"
    assert len(fake.requests) == 2


def test_probe_of_an_unreachable_service_is_bounded():
    client = AiIndexClient(make_settings(f"http://127.0.0.1:{_free_port()}"))
    started = time.monotonic()
    readiness = client.readiness()
    assert (readiness.ready, readiness.reason) == (False, "probe_connect_failed")
    assert time.monotonic() - started < 2


def test_probe_of_a_silent_service_is_bounded_by_the_probe_timeout():
    server = RawServer(lambda conn: (_read_request(conn), time.sleep(5)))
    try:
        started = time.monotonic()
        readiness = AiIndexClient(make_settings(server.url, ai_probe_timeout_seconds=0.3)).readiness()
        assert (readiness.ready, readiness.reason) == (False, "probe_timeout")
        assert time.monotonic() - started < 1.5
    finally:
        server.close()


# --- index calls against the fake -----------------------------------------------------------------


def test_index_confirmed_sends_key_header_token_and_canonical_body(fake):
    client = AiIndexClient(make_settings(fake.url))
    request = a_request()
    result = client.index(request)
    assert (result.outcome, result.replayed, result.chunk_count) == (Outcome.INDEXED, False, 2)
    sent = fake.index_requests()[0]
    assert sent.body == request.body
    assert sent.headers["Idempotency-Key"] == request.idempotency_key
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["Content-Type"] == "application/json"


def test_lost_response_then_replay_commits_once(fake):
    client = AiIndexClient(make_settings(fake.url))
    request = a_request()
    fake.faults.append(Fault("lose_after_commit"))
    lost = client.index(request)
    assert (lost.outcome, lost.code) == (Outcome.RETRY, "ai_connection_lost")
    replay = client.index(request)
    assert (replay.outcome, replay.replayed) == (Outcome.INDEXED, True)
    assert fake.commit_count == 1
    keys = {r.headers["Idempotency-Key"] for r in fake.index_requests()}
    assert keys == {request.idempotency_key}


def test_concurrent_duplicate_requests_commit_once(fake):
    client = AiIndexClient(make_settings(fake.url))
    request = a_request()
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.index(request))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.outcome is Outcome.INDEXED for r in results)
    assert sorted(r.replayed for r in results) == [False] + [True] * 5
    assert fake.commit_count == 1


def test_course_isolation_at_the_fake(fake):
    client = AiIndexClient(make_settings(fake.url))
    course_a, course_b = uuid.uuid4(), uuid.uuid4()
    assert client.index(a_request(course_a)).outcome is Outcome.INDEXED
    assert client.index(a_request(course_b)).outcome is Outcome.INDEXED
    assert len(fake.commits_for_course(course_a)) == len(fake.commits_for_course(course_b)) == 1


def test_202_is_never_success(fake):
    fake.faults.append(Fault("status", status=202, body=b'{"job_id":"j1"}'))
    result = AiIndexClient(make_settings(fake.url)).index(a_request())
    assert result.outcome is Outcome.ACCEPTED
    assert fake.commit_count == 0


def test_unreachable_service_is_unavailable_not_delivered():
    result = AiIndexClient(make_settings(f"http://127.0.0.1:{_free_port()}")).index(a_request())
    assert (result.outcome, result.code) == (Outcome.UNAVAILABLE, "ai_unreachable")


def test_silent_service_hits_the_read_timeout_as_a_possibly_delivered_request():
    server = RawServer(lambda conn: (_read_request(conn), time.sleep(5)))
    try:
        started = time.monotonic()
        result = AiIndexClient(make_settings(server.url, ai_read_timeout_seconds=0.3)).index(a_request())
        assert (result.outcome, result.code) == (Outcome.RETRY, "ai_timeout")
        assert time.monotonic() - started < 1.5
    finally:
        server.close()


def test_a_trickling_peer_is_cut_off_by_the_deadline_not_only_the_inactivity_timeout():
    def trickle(conn):
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n")
        for _ in range(100):
            conn.sendall(b" ")
            time.sleep(0.1)  # always within the 0.5 s inactivity timeout

    server = RawServer(trickle)
    try:
        started = time.monotonic()
        settings = make_settings(server.url, ai_read_timeout_seconds=0.5, ai_request_deadline_seconds=1)
        result = AiIndexClient(settings).index(a_request())
        elapsed = time.monotonic() - started
        assert (result.outcome, result.code) == (Outcome.RETRY, "ai_deadline_exceeded")
        assert elapsed < 1 + 0.5 + 0.5  # deadline + one inactivity timeout, plus slack
    finally:
        server.close()


def test_oversized_responses_are_refused():
    big = b"x" * 5000

    def answer(conn):
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 5000\r\n\r\n" + big)

    server = RawServer(answer)
    try:
        result = AiIndexClient(make_settings(server.url, index_max_response_bytes=1024)).index(a_request())
        assert (result.outcome, result.code) == (Outcome.RETRY, "ai_response_too_large")
    finally:
        server.close()


# --- HTTP framing and strict JSON, over raw sockets -------------------------------------------------


def _answer_raw(raw: bytes, request=None):
    """Serve `raw` verbatim to one index call, close the connection, return the client's result."""
    request = request or a_request()

    def answer(conn):
        _read_request(conn)
        conn.sendall(raw)

    server = RawServer(answer)
    try:
        return AiIndexClient(make_settings(server.url)).index(request), request
    finally:
        server.close()


def _http200(body: bytes, headers: bytes = b"") -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + headers + b"\r\n" + body


def test_a_well_framed_raw_confirmation_is_indexed():
    request = a_request()
    body = json.dumps(_indexed(request)).encode()
    result, _ = _answer_raw(_http200(body, b"Content-Length: %d\r\n" % len(body)), request)
    assert result.outcome is Outcome.INDEXED


def test_a_confirmation_shorter_than_its_content_length_is_never_indexed():
    """Reproduced by the reviewer: 493 declared, 393 sent, valid JSON, clean close."""
    request = a_request()
    body = json.dumps(_indexed(request)).encode()
    result, _ = _answer_raw(_http200(body, b"Content-Length: %d\r\n" % (len(body) + 100)), request)
    assert (result.outcome, result.code) == (Outcome.RETRY, "ai_incomplete_response")


@pytest.mark.parametrize(
    "chunks",
    [
        b"%x\r\n%s\r\n0\r\n\r\n",  # a complete, valid chunked body
        b"%x\r\n%s\r\n0\r\n",  # missing final terminator
        b"%x\r\n%s\r\n",  # missing last chunk
        b"%x\r\n%sXX0\r\n\r\n",  # invalid chunk delimiter
    ],
    ids=["complete", "no-final-crlf", "no-last-chunk", "bad-delimiter"],
)
def test_chunked_responses_are_never_indexed(chunks):
    request = a_request()
    body = json.dumps(_indexed(request)).encode()
    raw = _http200(chunks % (len(body), body), b"Transfer-Encoding: chunked\r\n")
    result, _ = _answer_raw(raw, request)
    assert (result.outcome, result.code) == (Outcome.RETRY, "ai_invalid_framing")


def test_a_chunked_error_body_is_not_a_protocol_error():
    raw = b"HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    result, _ = _answer_raw(raw)
    assert (result.outcome, result.code) == (Outcome.RETRY, "ai_invalid_framing")


@pytest.mark.parametrize(
    "headers",
    [
        b"",  # close-delimited: a truncation is indistinguishable from the end
        b"Content-Length: abc\r\n",
        b"Content-Length: -1\r\n",
        b"Content-Length: +10\r\n",
        b"Content-Length: 1 0\r\n",
        b"Content-Length: 10\r\nContent-Length: 10\r\n",
        b"Content-Length: 10\r\nContent-Length: 11\r\n",
        b"Content-Length: 10\r\nTransfer-Encoding: chunked\r\n",
        b"Transfer-Encoding: gzip, chunked\r\n",
        b"Transfer-Encoding: identity\r\n",
    ],
    ids=["none", "alpha", "negative", "plus", "space", "repeated-equal", "repeated-differ", "both", "gzip", "identity"],
)
def test_invalid_or_ambiguous_framing_is_never_indexed(headers):
    request = a_request()
    body = json.dumps(_indexed(request)).encode()
    result, _ = _answer_raw(_http200(body, headers), request)
    assert (result.outcome, result.code) == (Outcome.RETRY, "ai_invalid_framing")


@pytest.mark.parametrize("raw", [b"", b"HTTP/1.1 200", b"garbage\r\n\r\n", b"HTTP/9.9 200 OK\r\n\r\n"])
def test_a_broken_status_line_is_never_indexed(raw):
    assert _answer_raw(raw)[0].outcome is Outcome.RETRY


def test_duplicate_keys_in_a_confirmation_are_incompatible_even_when_the_last_value_matches():
    request = a_request()
    good = json.dumps(_indexed(request)).encode()
    # an earlier, different "chunk_count" followed by the value the validator would accept
    duplicated = good[:-1] + b',"chunk_count":3}'
    assert json.loads(duplicated) == json.loads(good)  # a plain json.loads would accept it
    result, _ = _answer_raw(_http200(duplicated, b"Content-Length: %d\r\n" % len(duplicated)), request)
    assert (result.outcome, result.code) == (Outcome.INCOMPATIBLE, "invalid_response")


@pytest.mark.parametrize(
    "body",
    [
        b'{"error":{"code":"bad","code":"worse","message":"m","retryable":false}}',
        b'{"error":{"code":"bad","message":"m","retryable":false},"error":{"code":"bad","message":"m","retryable":false}}',
    ],
)
def test_duplicate_keys_in_an_error_body_are_not_a_protocol_error(body):
    response = HttpResponse(400, "application/json", body)  # a valid error body would be REJECTED here
    assert classify(response, a_request()).outcome is Outcome.INCOMPATIBLE


def test_nested_duplicate_keys_and_non_finite_numbers_are_rejected():
    request = a_request()
    nested = json.dumps(_indexed(request)).replace('"replayed": false', '"replayed": false, "x": {"a": 1, "a": 2}')
    nan = json.dumps(_indexed(request)).replace('"chunk_count": 3', '"chunk_count": NaN')
    for text in (nested, nan):
        response = HttpResponse(200, "application/json", text.encode())
        assert classify(response, request).outcome is Outcome.INCOMPATIBLE


def test_redirects_are_not_followed(fake):
    def redirect(conn):
        _read_request(conn)
        location = f"{fake.url}/ai/v1/index/documents".encode()
        conn.sendall(b"HTTP/1.1 307 Temporary Redirect\r\nLocation: " + location + b"\r\nContent-Length: 0\r\n\r\n")

    server = RawServer(redirect)
    try:
        result = AiIndexClient(make_settings(server.url)).index(a_request())
        assert (result.outcome, result.code) == (Outcome.INCOMPATIBLE, "unexpected_redirect")
        assert fake.requests == []
    finally:
        server.close()


def test_environment_proxies_are_ignored(fake, monkeypatch):
    proxy = RawServer(lambda conn: None)
    try:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, proxy.url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        client = AiIndexClient(make_settings(fake.url))
        assert client.readiness().ready
        assert client.index(a_request()).outcome is Outcome.INDEXED
        assert proxy.connections == 0
    finally:
        proxy.close()


def test_the_token_never_appears_in_reprs_or_logs(fake, caplog):
    caplog.set_level(logging.DEBUG)
    settings = make_settings(fake.url)
    client = AiIndexClient(settings)
    fake.capabilities_status = 503
    client.readiness()
    fake.faults.append(Fault("status", status=500, body=b"{}"))
    client.index(a_request())
    assert TOKEN not in repr(settings) and TOKEN not in str(settings) and TOKEN not in repr(client)
    assert TOKEN not in settings.model_dump_json()
    assert TOKEN not in caplog.text


# --- classification of answers (pure) -------------------------------------------------------------


def _error(code="bad", retryable=False):
    return json.dumps({"error": {"code": code, "message": "m", "retryable": retryable}}).encode()


@pytest.mark.parametrize(
    "status, body, outcome",
    [
        (202, b"{}", Outcome.ACCEPTED),
        (201, b"{}", Outcome.INCOMPATIBLE),
        (204, b"", Outcome.INCOMPATIBLE),
        (301, b"", Outcome.INCOMPATIBLE),
        (400, _error(), Outcome.REJECTED),
        (422, _error(), Outcome.REJECTED),
        (413, _error("payload_too_large"), Outcome.TOO_LARGE),
        (400, b'{"detail":"x"}', Outcome.INCOMPATIBLE),  # not a protocol error body: never permanent
        (422, b'{"detail":[]}', Outcome.INCOMPATIBLE),  # FastAPI validation error of a non-protocol service
        (400, _error(retryable=True), Outcome.RETRY),
        (401, _error(), Outcome.UNAVAILABLE),
        (403, b"", Outcome.UNAVAILABLE),
        (404, b"", Outcome.INCOMPATIBLE),
        (405, b"", Outcome.INCOMPATIBLE),
        (408, b"", Outcome.RETRY),
        (409, _error("in_progress", True), Outcome.RETRY),
        (409, _error("generation_conflict", False), Outcome.REJECTED),
        (409, b"", Outcome.RETRY),
        (429, b"", Outcome.RETRY),
        (500, b"", Outcome.RETRY),
        (503, b"<html>", Outcome.RETRY),
    ],
)
def test_status_classification(status, body, outcome):
    assert classify(HttpResponse(status, "application/json", body), a_request()).outcome is outcome


def _indexed(request, **changes):
    body = {
        "protocol": PROTOCOL, "status": "indexed", "document_id": str(request.document_id),
        "course_id": str(request.course_id), "content_digest": request.content_digest,
        "idempotency_key": request.idempotency_key, "section_count": request.section_count,
        "chunk_count": 3, "replayed": False,
    }  # fmt: skip
    body.update(changes)
    return {k: v for k, v in body.items() if v is not ...}


def test_a_valid_confirmation_is_indexed():
    request = a_request()
    response = HttpResponse(200, "application/json; charset=utf-8", json.dumps(_indexed(request)).encode())
    assert classify(response, request).outcome is Outcome.INDEXED


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "accepted"},
        {"protocol": "studygraph.index/0"},
        {"document_id": str(uuid.uuid4())},
        {"course_id": str(uuid.uuid4())},  # another course's confirmation is never accepted
        {"content_digest": "0" * 64},
        {"idempotency_key": "0" * 64},
        {"section_count": 99},
        {"section_count": True},
        {"chunk_count": 0},
        {"chunk_count": 1.0},
        {"replayed": 1},
        {"replayed": ...},  # missing key
        {"extra": "x"},
    ],
)
def test_confirmations_that_do_not_match_exactly_are_incompatible(changes):
    request = a_request()
    response = HttpResponse(200, "application/json", json.dumps(_indexed(request, **changes)).encode())
    assert classify(response, request).outcome is Outcome.INCOMPATIBLE


@pytest.mark.parametrize(
    "content_type, body",
    [("text/plain", None), ("application/json", b"[]"), ("application/json", b"{not json"), ("application/json", b"\xff")],
)
def test_non_json_confirmations_are_incompatible(content_type, body):
    request = a_request()
    body = body if body is not None else json.dumps(_indexed(request)).encode()
    assert classify(HttpResponse(200, content_type, body), request).outcome is Outcome.INCOMPATIBLE


def test_validate_capabilities_requires_explicit_mock_false():
    body = {"protocol": PROTOCOL, "service": "ai-service", "completion": ["sync"], "max_request_bytes": 10}
    assert not validate_capabilities(HttpResponse(200, "application/json", json.dumps(body).encode())).ready


# --- configuration --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["ftp://ai:8000", "http://", "http://user:pw@ai:8000", "http://ai:8000/?x=1", "http://ai:8000/#f", "ai:8000", "http://ai:99999"],
)
def test_invalid_service_urls_are_refused(url):
    with pytest.raises(InvalidServiceUrl):
        parse_service_url(url)


def test_service_url_keeps_a_base_path():
    url = parse_service_url("https://ai.internal/base/")
    assert (url.scheme, url.host, url.port, url.base_path) == ("https", "ai.internal", 443, "/base")


def test_indexing_is_disabled_by_default():
    assert Settings().indexing_enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"index_queue": "ingestion.parse"},
        {"index_task_soft_time_limit_seconds": 100},  # below probe + deadline + read timeout
        {"index_task_time_limit_seconds": 240},  # not above the soft limit
        {"broker_visibility_timeout_seconds": 500},  # not above twice the index hard limit
    ],
)
def test_inconsistent_index_settings_are_refused(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_index_lease_exceeds_the_hard_limit():
    s = Settings()
    assert s.index_lease_seconds > s.index_task_time_limit_seconds > s.index_task_soft_time_limit_seconds
    assert s.ai_probe_timeout_seconds + s.ai_request_deadline_seconds + s.ai_read_timeout_seconds < s.index_task_soft_time_limit_seconds
