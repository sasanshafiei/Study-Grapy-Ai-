"""The restricted provider transport (M6): DNS pinning, SSRF refusals, TLS/SNI, bounded responses, strict JSON.

Offline: a local TLS server presents certificates signed by a test-only CA
(tests/fixtures/tls); a deterministic resolver returns fixed addresses and
the socket factory maps the one verified public address to the local
server. Nothing here resolves or contacts a real host.
"""

from __future__ import annotations

import socket
import ssl
import threading
from pathlib import Path

import pytest

from app.harvest import transport
from app.harvest.transport import (
    InvalidJson,
    ProviderEndpoint,
    ProviderResponse,
    ProviderTransport,
    TransportBounds,
    TransportError,
    is_public_address,
    resolve_public,
    strict_json_object,
    validate_user_agent,
)
from tests.harvest_support import USER_AGENT

TLS = Path(__file__).parent / "fixtures" / "tls"
PUBLIC_IP = "208.80.154.224"
ENDPOINT = ProviderEndpoint(host="en.wikipedia.org", path="/w/api.php")
BOUNDS = TransportBounds(connect_timeout=2, read_timeout=2, deadline=5, max_response_bytes=4096)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
     "224.0.0.1", "192.0.2.10", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
     "64:ff9b::a00:1", "2002:a00:1::1", "2001::1", "not-an-ip", "fe80::1%eth0"],
)  # fmt: skip
def test_non_public_addresses_are_refused(address):
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", [PUBLIC_IP, "2620:0:861:ed1a::1", "::ffff:208.80.154.224"])
def test_public_addresses_are_accepted(address):
    assert is_public_address(address) is True


def test_a_mixed_dns_answer_is_refused_as_a_whole():
    with pytest.raises(TransportError) as raised:
        resolve_public("en.wikipedia.org", 443, lambda host, port: [PUBLIC_IP, "10.0.0.5"])
    assert raised.value.code == "non_public_address" and not raised.value.retryable


def test_dns_failure_is_transient():
    def broken(host, port):
        raise socket.gaierror("no name")

    with pytest.raises(TransportError) as raised:
        resolve_public("en.wikipedia.org", 443, broken)
    assert raised.value.code == "dns_failed" and raised.value.retryable


@pytest.mark.parametrize(
    "value",
    ["", "python-requests/2.32", "StudyGraph", "StudyGraph/1.0", "StudyGraph/1.0 (ops@example.com)",
     "StudyGraph/1.0 (http://insecure.dev/contact)", "Mozilla/5.0 (contact@studygraph.dev)",
     " StudyGraph/1.0 (ops@studygraph.dev)", "StudyGraph/1.0 (ops@studygraph.dev)\r\nX: y"],
)  # fmt: skip
def test_user_agent_must_name_the_client_and_a_real_contact(value):
    with pytest.raises(ValueError):
        validate_user_agent(value)


def test_compliant_user_agent_is_accepted():
    assert validate_user_agent(USER_AGENT) == USER_AGENT
    assert validate_user_agent("StudyGraph/1.0 (mailto:ops@studygraph.dev) python-httpclient/3.11")


def test_endpoint_accepts_only_fixed_https_hosts_and_paths():
    with pytest.raises(ValueError):
        ProviderEndpoint(host="127.0.0.1", path="/w/api.php")
    with pytest.raises(ValueError):
        ProviderEndpoint(host="en.wikipedia.org", path="/w/api.php?x=1")
    with pytest.raises(ValueError):
        ProviderEndpoint(host="en.wikipedia.org", path="/w/api.php", port=8443)


# --- local TLS server -------------------------------------------------------------------------------


class _Server:
    def __init__(self, cert: str, responses: list[bytes]) -> None:
        self.context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.context.load_cert_chain(TLS / f"{cert}.pem", TLS / f"{cert}.key")
        self.server_names: list[str | None] = []
        self.context.sni_callback = lambda sock, name, ctx: self.server_names.append(name)
        self.responses = list(responses)
        self.requests: list[bytes] = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.listener.settimeout(5)
        while self.responses:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(raw, server_side=True) as conn:
                    conn.settimeout(5)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    self.requests.append(data)
                    conn.sendall(self.responses.pop(0))
            except (OSError, ssl.SSLError):
                self.responses and self.responses.pop(0)

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=5)


@pytest.fixture
def serve():
    servers: list[_Server] = []

    def start(responses: list[bytes], cert: str = "en.wikipedia.org") -> _Server:
        server = _Server(cert, responses)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def _transport(server: _Server, resolved=(PUBLIC_IP,), calls: list | None = None) -> ProviderTransport:
    def resolver(host, port):
        if calls is not None:
            calls.append(("resolve", host, port))
        return list(resolved)

    def socket_factory(ip, port, timeout):
        if calls is not None:
            calls.append(("connect", ip, port))
        assert ip == PUBLIC_IP and port == 443  # only the verified address is ever dialled
        return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

    return ProviderTransport(
        USER_AGENT, resolver=resolver, socket_factory=socket_factory, context=transport.tls_context(str(TLS / "ca.pem"))
    )


def _http(status: str, body: bytes, *headers: str) -> bytes:
    head = [f"HTTP/1.1 {status}", "Content-Type: application/json", *headers]
    if not any(h.lower().startswith(("content-length", "transfer-encoding")) for h in headers):
        head.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def test_request_pins_the_verified_ip_with_sni_and_host_of_the_provider(serve, monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://10.9.9.9:3128")  # ignored: the socket is opened directly, no proxy setting is read
    server = serve([_http("200 OK", b'{"ok":true}')])
    calls: list = []
    response = _transport(server, calls=calls).get(ENDPOINT, {"action": "query", "b": "x y"}, BOUNDS)
    assert response.status == 200 and strict_json_object(response) == {"ok": True}
    assert calls == [("resolve", "en.wikipedia.org", 443), ("connect", PUBLIC_IP, 443)]  # resolved once
    assert server.server_names == ["en.wikipedia.org"]
    request = server.requests[0].decode()
    assert request.startswith("GET /w/api.php?action=query&b=x+y HTTP/1.1\r\n")
    headers = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip() for line in request.split("\r\n")[1:] if ":" in line}
    assert headers["host"] == "en.wikipedia.org"
    assert headers["user-agent"] == USER_AGENT
    assert headers["accept-encoding"] == "identity"
    assert "cookie" not in headers and "authorization" not in headers and "proxy-authorization" not in headers


def test_a_certificate_for_another_host_is_refused(serve):
    server = serve([_http("200 OK", b"{}")], cert="wrong.example.net")
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "tls_verification_failed" and not raised.value.retryable
    assert server.requests == []  # nothing was sent


def test_private_resolution_never_connects(serve):
    server = serve([_http("200 OK", b"{}")])
    calls: list = []
    with pytest.raises(TransportError) as raised:
        _transport(server, resolved=("127.0.0.1",), calls=calls).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "non_public_address"
    assert calls == [("resolve", "en.wikipedia.org", 443)]


def test_redirects_are_refused_not_followed(serve):
    server = serve([_http("301 Moved Permanently", b"", "Location: https://169.254.169.254/latest/meta-data")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "redirect_refused"
    assert len(server.requests) == 1


def test_compressed_responses_are_refused(serve):
    server = serve([_http("200 OK", b"\x1f\x8b\x08\x00", "Content-Encoding: gzip")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "encoded_response"


def test_declared_oversize_is_refused_before_reading(serve):
    server = serve([_http("200 OK", b"{}", "Content-Length: 999999")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "response_too_large"


def test_chunked_bodies_are_bounded_by_the_bytes_received(serve):
    chunk = b"x" * 3000
    body = b"%x\r\n%s\r\n%x\r\n%s\r\n0\r\n\r\n" % (len(chunk), chunk, len(chunk), chunk)
    server = serve([_http("200 OK", body, "Transfer-Encoding: chunked")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "response_too_large"


def test_truncated_and_ambiguous_framing_is_refused(serve):
    server = serve([_http("200 OK", b'{"a"', "Content-Length: 10")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "incomplete_response"
    server = serve([_http("200 OK", b"{}", "Content-Length: 2", "Transfer-Encoding: chunked")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "invalid_framing"


def _chunked(*parts: bytes, end: bytes = b"0\r\n\r\n", delimiter: bytes = b"\r\n") -> bytes:
    return b"".join(b"%x\r\n%s%s" % (len(part), part, delimiter) for part in parts) + end


def test_a_complete_chunked_body_is_accepted(serve):
    body = _chunked(b'{"ok":', b"true}")
    server = serve([_http("200 OK", body, "Transfer-Encoding: chunked")])
    response = _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert strict_json_object(response) == {"ok": True}


@pytest.mark.parametrize("end", [b"", b"0\r\n"], ids=["no_last_chunk", "no_final_crlf"])
def test_a_chunked_body_without_its_terminator_is_refused(serve, end):
    """The connection closes after a complete-looking JSON body but before "0\r\n\r\n": it must not be accepted."""
    body = _chunked(b'{"ok":', b"true}", end=end)
    server = serve([_http("200 OK", body, "Transfer-Encoding: chunked")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "incomplete_response" and raised.value.retryable


def test_a_chunked_body_with_an_invalid_chunk_delimiter_is_refused(serve):
    body = _chunked(b'{"ok":', b"true}", delimiter=b"XX")
    server = serve([_http("200 OK", body, "Transfer-Encoding: chunked")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "invalid_framing" and raised.value.retryable


def test_a_chunked_body_with_a_bad_size_line_is_refused(serve):
    server = serve([_http("200 OK", b"zz\r\n{}\r\n0\r\n\r\n", "Transfer-Encoding: chunked")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "invalid_framing"


def test_a_response_without_any_framing_header_is_refused(serve):
    server = serve([b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}"])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "invalid_framing"


def test_retry_after_is_returned_in_seconds(serve):
    server = serve([_http("429 Too Many Requests", b"{}", "Retry-After: 120")])
    response = _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert response.status == 429 and response.retry_after_seconds == 120.0
    assert transport.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT", now=0) > 0
    assert transport.parse_retry_after("soon") is None


@pytest.mark.parametrize(
    "body, content_type",
    [(b'{"a":1,"a":2}', "application/json"), (b'{"a":NaN}', "application/json"), (b"[1]", "application/json"),
     (b"\xff", "application/json"), (b"{}", "text/html")],
)  # fmt: skip
def test_strict_json(body, content_type):
    with pytest.raises(InvalidJson):
        strict_json_object(ProviderResponse(200, content_type, body))


@pytest.mark.parametrize("value,expected", [("172800", 172800.0), ("0000172800", 172800.0), ("0000", 0.0), ("0", 0.0), ("999999999", 999999999.0)])
def test_numeric_retry_after_ignores_leading_zeros(value, expected):
    assert transport.parse_retry_after(value) == expected


@pytest.mark.parametrize("value", ["1000000000", "0001000000000", "9" * 5000])
def test_excessive_numeric_retry_after_is_infinite_so_the_source_is_suspended(value):
    assert transport.parse_retry_after(value) == float("inf")


@pytest.mark.parametrize("value", ["", "-5", "1.5", "1e3", "+7", "\u0661\u0662"])
def test_non_delta_retry_after_is_invalid(value):
    assert transport.parse_retry_after(value) is None


def test_retry_after_survives_a_rejected_throttle_body(serve):
    server = serve([_http("429 Too Many Requests", b"\x1f\x8b\x08\x00", "Retry-After: 172800", "Content-Encoding: gzip")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "encoded_response"
    assert (raised.value.throttle_status, raised.value.retry_after_seconds) == (429, 172800.0)


def test_retry_after_survives_an_incomplete_throttle_body(serve):
    server = serve([_http("503 Service Unavailable", b"{}", "Retry-After: 0000172800", "Content-Length: 50")])
    with pytest.raises(TransportError) as raised:
        _transport(server).get(ENDPOINT, {}, BOUNDS)
    assert raised.value.code == "incomplete_response"
    assert (raised.value.throttle_status, raised.value.retry_after_seconds) == (503, 172800.0)


def test_no_delay_is_attached_without_a_valid_retry_after_or_for_other_statuses(serve):
    server = serve([
        _http("429 Too Many Requests", b"x", "Retry-After: soon", "Content-Encoding: gzip"),
        _http("200 OK", b"x", "Retry-After: 60", "Content-Encoding: gzip"),
    ])
    for _ in range(2):
        with pytest.raises(TransportError) as raised:
            _transport(server).get(ENDPOINT, {}, BOUNDS)
        assert raised.value.throttle_status is None and raised.value.retry_after_seconds is None
