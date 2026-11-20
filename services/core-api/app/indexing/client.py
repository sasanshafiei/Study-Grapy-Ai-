"""HTTP client for the AI service's index endpoint (CE-side proposal "studygraph.index/1").

Built on http.client from the standard library, deliberately:
- No redirects: http.client never follows them; a 3xx is an error here.
- No environment proxies: http.client never reads HTTP(S)_PROXY / NO_PROXY,
  ~/.netrc or any other ambient configuration. The connection goes to the
  host in AI_SERVICE_URL and nowhere else.
- No connection pool: one connection per request, closed on every path.

Time bounds, precisely (and what is NOT bounded):
- connect timeout: TCP connect (and the TLS handshake for https).
  Name resolution (getaddrinfo) is not covered by any socket timeout.
- read timeout: each later socket operation (sending the body, the status
  line, *each* header line, each body read) individually. It bounds
  *inactivity*, not the total: every byte that arrives restarts it.
- request deadline: checked only before each body read, i.e. after the status
  line and all headers have arrived. It does NOT cover connecting, sending,
  getresponse() or the header phase, so a peer that trickles header bytes can
  keep one call (probe or index) running well past it, and past the probe
  timeout.
- the only absolute wall-clock bound is the index task's Celery limits (the
  soft limit raises in the task, the hard limit kills the pool process).
  Every network call, the readiness probe included, runs inside an index task;
  none runs at worker start-up. The settings' probe + deadline + read-timeout
  relation to the soft limit is a sanity check for well-behaved peers, not a
  guarantee.

Sizes: the caller checks the request body against the smaller of
INDEX_MAX_PAYLOAD_BYTES and the service's advertised max_request_bytes before
anything is sent; the response body is read with a cap and rejected beyond it.

Response validation is strict: the HTTP framing must be complete and
unambiguous (exactly one numeric Content-Length that the body fills; any
Transfer-Encoding, chunked included, and close-delimited bodies are refused:
the protocol's responses are small, so no chunk parsing is needed), JSON
objects must not repeat a key, exact key sets, exact types (a bool is not an
int), echoed identities equal to what was sent. HTTP 202 is never success.
"""

from __future__ import annotations

import enum
import http.client
import json
import logging
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import SecretStr

from ..config import Settings
from .payload import PROTOCOL, IndexRequest

log = logging.getLogger(__name__)

INDEX_PATH = "/ai/v1/index/documents"
CAPABILITIES_PATH = "/ai/v1/index/capabilities"
READ_CHUNK = 64 * 1024
USER_AGENT = "studygraph-core-api/index-client"
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")

INDEXED_KEYS = frozenset(
    {"protocol", "status", "document_id", "course_id", "content_digest", "idempotency_key",
     "section_count", "chunk_count", "replayed"}
)  # fmt: skip
CAPABILITY_KEYS = frozenset({"protocol", "service", "mock", "completion", "max_request_bytes"})
# M6, backward compatible: an optional list of locator schemes the service indexes.
# Absent (every M4 service) means ["page"] only; section-based documents are then
# never sent. Any other extra key is still refused.
OPTIONAL_CAPABILITY_KEYS = frozenset({"locators"})
LOCATOR_SCHEMES = frozenset({"page", "section"})
ERROR_KEYS = frozenset({"code", "message", "retryable"})


class InvalidServiceUrl(ValueError):
    pass


@dataclass(frozen=True)
class ServiceUrl:
    scheme: str
    host: str
    port: int
    base_path: str

    def __str__(self) -> str:  # the URL carries no credentials (refused below)
        return f"{self.scheme}://{self.host}:{self.port}{self.base_path}"


def parse_service_url(raw: str) -> ServiceUrl:
    """Validate AI_SERVICE_URL: http(s), a host, no credentials, query or fragment."""
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        raise InvalidServiceUrl("AI_SERVICE_URL is not a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise InvalidServiceUrl("AI_SERVICE_URL must use http or https")
    if not parts.hostname:
        raise InvalidServiceUrl("AI_SERVICE_URL has no host")
    if parts.username is not None or parts.password is not None:
        raise InvalidServiceUrl("AI_SERVICE_URL must not carry credentials (use AI_SERVICE_TOKEN)")
    if parts.query or parts.fragment:
        raise InvalidServiceUrl("AI_SERVICE_URL must not have a query or fragment")
    return ServiceUrl(
        scheme=parts.scheme,
        host=parts.hostname,
        port=port or (443 if parts.scheme == "https" else 80),
        base_path=parts.path.rstrip("/"),
    )


# --- transport ---------------------------------------------------------------------------------


class TransportError(Exception):
    """No usable HTTP response. `delivered` is False only when nothing can have reached the service."""

    def __init__(self, code: str, detail: str, *, delivered: bool) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.delivered = delivered


@dataclass(frozen=True)
class HttpResponse:
    status: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class Bounds:
    connect_timeout: float
    read_timeout: float
    deadline: float
    max_response_bytes: int


_BODYLESS = frozenset({204, 304})


def _declared_length(response: http.client.HTTPResponse) -> int:
    """The body length the framing promises; TransportError if it is missing, ambiguous or invalid."""
    status = response.status
    if 100 <= status < 200 or status in _BODYLESS:
        return 0
    lengths = response.msg.get_all("Content-Length") or []
    if response.msg.get_all("Transfer-Encoding"):
        raise TransportError("invalid_framing", "Transfer-Encoding is not accepted", delivered=True)
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,18}", lengths[0].strip()):
        raise TransportError("invalid_framing", "missing, repeated or non-numeric Content-Length", delivered=True)
    return int(lengths[0].strip())


def http_request(
    url: ServiceUrl, method: str, path: str, body: bytes | None, headers: dict[str, str], bounds: Bounds
) -> HttpResponse:
    started = time.monotonic()
    if url.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            url.host, url.port, timeout=bounds.connect_timeout, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(url.host, url.port, timeout=bounds.connect_timeout)
    try:
        try:
            conn.connect()
        except (OSError, ssl.SSLError) as exc:
            raise TransportError("connect_failed", type(exc).__name__, delivered=False) from None
        conn.sock.settimeout(bounds.read_timeout)
        try:
            conn.request(method, url.base_path + path, body=body, headers=headers)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            code = "timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else "connection_lost"
            raise TransportError(code, type(exc).__name__, delivered=True) from None
        content_type = (response.getheader("Content-Type") or "").strip().lower()
        declared = _declared_length(response)
        if declared > bounds.max_response_bytes:
            raise TransportError("response_too_large", f"Content-Length {declared}", delivered=True)
        chunks: list[bytes] = []
        size = 0
        try:
            while True:
                if time.monotonic() - started > bounds.deadline:
                    raise TransportError("deadline_exceeded", f"{bounds.deadline}s", delivered=True)
                chunk = response.read1(READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > bounds.max_response_bytes:
                    raise TransportError("response_too_large", f"over {bounds.max_response_bytes} bytes", delivered=True)
                chunks.append(chunk)
            if size != declared:  # the peer closed before sending what it promised
                raise TransportError("incomplete_response", f"{size} of {declared} bytes", delivered=True)
        except (OSError, http.client.HTTPException) as exc:
            code = "timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else "connection_lost"
            raise TransportError(code, type(exc).__name__, delivered=True) from None
        return HttpResponse(response.status, content_type, b"".join(chunks))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            log.debug("closing the AI connection failed", exc_info=True)


Transport = Callable[[ServiceUrl, str, str, "bytes | None", dict, Bounds], HttpResponse]


# --- outcomes ----------------------------------------------------------------------------------


class Outcome(enum.Enum):
    INDEXED = "indexed"  # confirmed, validated, identities match: the only success
    ACCEPTED = "accepted"  # 202: not a success; handled by the completion adapter
    REJECTED = "rejected"  # the service refused this document, per protocol, not retryable
    TOO_LARGE = "too_large"  # per protocol: the request exceeds the service's limit
    RETRY = "retry"  # transient: try again later with the same key
    INCOMPATIBLE = "incompatible"  # not speaking the protocol (mock, wrong version, garbage)
    UNAVAILABLE = "unavailable"  # nothing reached the service / it is down or refuses us


@dataclass(frozen=True)
class IndexResult:
    outcome: Outcome
    code: str
    detail: str = ""
    chunk_count: int | None = None
    replayed: bool | None = None


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def _json_object(response: HttpResponse) -> dict | None:
    media_type = response.content_type.split(";", 1)[0].strip()
    if media_type != "application/json":
        return None
    try:
        value = json.loads(
            response.body.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _protocol_error(response: HttpResponse) -> dict | None:
    """A protocol error body {"error": {"code", "message", "retryable"}}, or None."""
    body = _json_object(response)
    if body is None or set(body) != {"error"} or not isinstance(body["error"], dict):
        return None
    error = body["error"]
    if set(error) != ERROR_KEYS:
        return None
    if not isinstance(error["code"], str) or not _ERROR_CODE.match(error["code"]):
        return None
    if not isinstance(error["message"], str) or type(error["retryable"]) is not bool:
        return None
    return error


def validate_indexed(response: HttpResponse, request: IndexRequest) -> IndexResult:
    """Strictly validate a 200 answer against the request it answers."""
    body = _json_object(response)
    if body is None:
        return IndexResult(Outcome.INCOMPATIBLE, "invalid_response", "not a JSON object")
    if set(body) != INDEXED_KEYS:
        return IndexResult(Outcome.INCOMPATIBLE, "invalid_response", "unexpected key set")
    expected = {
        "protocol": PROTOCOL,
        "status": "indexed",
        "document_id": str(request.document_id),
        "course_id": str(request.course_id),
        "content_digest": request.content_digest,
        "idempotency_key": request.idempotency_key,
    }
    for key, value in expected.items():
        if not isinstance(body[key], str) or body[key] != value:
            return IndexResult(Outcome.INCOMPATIBLE, "identity_mismatch", f"{key} does not match the request")
    if type(body["section_count"]) is not int or body["section_count"] != request.section_count:
        return IndexResult(Outcome.INCOMPATIBLE, "identity_mismatch", "section_count does not match the request")
    if type(body["chunk_count"]) is not int or body["chunk_count"] < 1:
        return IndexResult(Outcome.INCOMPATIBLE, "invalid_response", "chunk_count is not a positive integer")
    if type(body["replayed"]) is not bool:
        return IndexResult(Outcome.INCOMPATIBLE, "invalid_response", "replayed is not a boolean")
    return IndexResult(Outcome.INDEXED, "indexed", chunk_count=body["chunk_count"], replayed=body["replayed"])


def classify(response: HttpResponse, request: IndexRequest) -> IndexResult:
    status = response.status
    if status == 200:
        return validate_indexed(response, request)
    if status == 202:
        return IndexResult(Outcome.ACCEPTED, "index_accepted", "202 Accepted is not a confirmation")
    if 200 <= status < 300:
        return IndexResult(Outcome.INCOMPATIBLE, "unexpected_status", str(status))
    if 300 <= status < 400:
        return IndexResult(Outcome.INCOMPATIBLE, "unexpected_redirect", str(status))
    error = _protocol_error(response)
    code = error["code"] if error else None
    if status in (401, 403):
        return IndexResult(Outcome.UNAVAILABLE, "ai_auth_refused", str(status))
    # Permanent only with a protocol error body that says so; anything else may be a
    # service that does not speak the protocol, and must never fail documents.
    if status in (400, 409, 413, 422) and error is not None and not error["retryable"]:
        if status == 413:
            return IndexResult(Outcome.TOO_LARGE, "index_payload_too_large", code or "")
        return IndexResult(Outcome.REJECTED, "index_rejected", code or "")
    if status in (408, 409, 425, 429) or status >= 500 or (error is not None and error["retryable"]):
        return IndexResult(Outcome.RETRY, f"ai_http_{status}", code or "")
    # A 4xx without a protocol error body: something that does not speak the protocol.
    return IndexResult(Outcome.INCOMPATIBLE, "unexpected_status", f"{status} {code or ''}".strip())


# --- readiness -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str
    max_request_bytes: int | None = None
    locators: frozenset[str] = frozenset({"page"})


def validate_capabilities(response: HttpResponse) -> Readiness:
    if response.status != 200:
        return Readiness(False, f"capabilities_http_{response.status}")
    body = _json_object(response)
    if body is None or not CAPABILITY_KEYS <= set(body) <= CAPABILITY_KEYS | OPTIONAL_CAPABILITY_KEYS:
        return Readiness(False, "capabilities_invalid")
    if body["mock"] is not False:  # absent, true or not a boolean: refused
        return Readiness(False, "mock_service")
    if body["protocol"] != PROTOCOL:
        return Readiness(False, "protocol_mismatch")
    if body["service"] != "ai-service":
        return Readiness(False, "capabilities_invalid")
    completion = body["completion"]
    if not isinstance(completion, list) or "sync" not in completion:
        return Readiness(False, "sync_completion_unsupported")
    limit = body["max_request_bytes"]
    if type(limit) is not int or limit < 1:
        return Readiness(False, "capabilities_invalid")
    locators = body.get("locators", ["page"])
    if (
        not isinstance(locators, list)
        or not all(isinstance(item, str) for item in locators)
        or len(set(locators)) != len(locators)
        or not set(locators) <= LOCATOR_SCHEMES
        or "page" not in locators
    ):
        return Readiness(False, "capabilities_invalid")
    return Readiness(True, "ready", limit, frozenset(locators))


class AiIndexClient:
    """Readiness (cached per process) and index calls against one configured AI service."""

    def __init__(
        self,
        settings: Settings,
        transport: Transport = http_request,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._url = parse_service_url(settings.ai_service_url)
        self._token: SecretStr | None = settings.ai_service_token
        self._transport = transport
        self._clock = clock
        self._cached: tuple[float, Readiness] | None = None

    def __repr__(self) -> str:
        return f"AiIndexClient(url={self._url}, token={'set' if self._token else 'unset'})"

    @property
    def url(self) -> ServiceUrl:
        return self._url

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "X-StudyGraph-Protocol": PROTOCOL}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        return headers

    # readiness

    def readiness(self) -> Readiness:
        """Probe GET /ai/v1/index/capabilities, reusing a result for AI_PROBE_CACHE_SECONDS.

        Failed probes are cached too, so a down or incompatible service is
        asked at most once per cache period per worker process.
        """
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < self._settings.ai_probe_cache_seconds:
            return self._cached[1]
        result = self._probe()
        self._cached = (self._clock(), result)
        if not result.ready:
            log.warning("ai_not_ready reason=%s url=%s", result.reason, self._url)
        return result

    def mark_unready(self, reason: str) -> None:
        """Stop claiming further jobs in this process until the next probe period."""
        self._cached = (self._clock(), Readiness(False, reason))

    def _probe(self) -> Readiness:
        s = self._settings
        bounds = Bounds(
            connect_timeout=min(s.ai_connect_timeout_seconds, s.ai_probe_timeout_seconds),
            read_timeout=s.ai_probe_timeout_seconds,
            deadline=s.ai_probe_timeout_seconds,
            max_response_bytes=16 * 1024,
        )
        try:
            response = self._transport(self._url, "GET", CAPABILITIES_PATH, None, self._headers(), bounds)
        except TransportError as exc:
            return Readiness(False, f"probe_{exc.code}")
        return validate_capabilities(response)

    # indexing

    def max_request_bytes(self, readiness: Readiness) -> int:
        limit = self._settings.index_max_payload_bytes
        return min(limit, readiness.max_request_bytes) if readiness.max_request_bytes else limit

    def index(self, request: IndexRequest) -> IndexResult:
        s = self._settings
        bounds = Bounds(
            connect_timeout=s.ai_connect_timeout_seconds,
            read_timeout=s.ai_read_timeout_seconds,
            deadline=s.ai_request_deadline_seconds,
            max_response_bytes=s.index_max_response_bytes,
        )
        headers = {
            **self._headers(),
            "Content-Type": "application/json",
            "Idempotency-Key": request.idempotency_key,
        }
        try:
            response = self._transport(self._url, "POST", INDEX_PATH, request.body, headers, bounds)
        except TransportError as exc:
            if not exc.delivered:
                return IndexResult(Outcome.UNAVAILABLE, "ai_unreachable", exc.detail)
            # The request may have been processed: the same key is replayed later.
            return IndexResult(Outcome.RETRY, f"ai_{exc.code}", exc.detail)
        return classify(response, request)
