"""HTTP client for the proposed studygraph.chat/1 AI endpoints (httpx.AsyncClient).

Transport rules, for every call (capabilities probe and answer):
- a new AsyncClient per call, closed by `async with` on every exit
  (success, error, timeout, task cancellation); one connection, no pool reuse;
- trust_env=False: no HTTP(S)_PROXY / NO_PROXY / ALL_PROXY, no .netrc, no
  SSL_CERT_* overrides from the environment;
- follow_redirects=False: a 3xx is an error, never followed;
- timeouts: connect, read, write and pool are *inactivity* timeouts (httpx
  applies them per operation); the absolute bound is an anyio deadline around
  the whole call (CHAT_PROBE_DEADLINE_SECONDS / CHAT_ANSWER_DEADLINE_SECONDS),
  which cancels the call wherever it is, name resolution included;
- `Accept-Encoding: identity` and any Content-Encoding other than identity in
  the response is refused (no decompression); Transfer-Encoding (chunked
  included) is refused; exactly one numeric Content-Length is required, must
  not exceed CHAT_MAX_RESPONSE_BYTES, and the raw body must match it exactly;
- the body is read raw with a running byte count and abandoned beyond the limit;
- no automatic retry: a failed answer request is reported, never repeated
  (the user decides whether to ask again).
Response JSON is parsed strictly (no repeated keys, no NaN/Infinity).

The bearer token is only placed in the Authorization header. It never
appears in logs, reprs or errors; logged URLs never carry credentials
(AI_SERVICE_URL with user info is refused by parse_service_url).

Capabilities are cached per (service URL, token fingerprint) for at most
CHAT_CAPABILITIES_CACHE_SECONDS (<= 300 s), in at most MAX_CACHE_ENTRIES
entries of a few small values each; expired entries are dropped on access.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import anyio
import httpx
from pydantic import SecretStr

from ..config import Settings
from ..indexing.client import ServiceUrl, parse_service_url
from ..json_body import strict_loads
from .protocol import (
    ANSWER_PATH,
    CAPABILITIES_PATH,
    PROTOCOL,
    Capabilities,
    validate_capabilities,
)

log = logging.getLogger(__name__)

USER_AGENT = "studygraph-core-api/chat-client"
MIN_TOKEN_LENGTH = 32
MAX_CACHE_ENTRIES = 4
PROBE_MAX_RESPONSE_BYTES = 16 * 1024
_DIGITS = frozenset("0123456789")


class ChatUpstreamError(Exception):
    """No usable answer from the AI service. `code` is safe to log; nothing here is shown to users."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RawResponse:
    status: int
    content_type: str
    body: bytes


class CapabilityCache:
    def __init__(self, ttl: float, max_entries: int = MAX_CACHE_ENTRIES, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], tuple[float, Capabilities]] = OrderedDict()

    def get(self, key: tuple[str, str]) -> Capabilities | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry[0]:
            del self._entries[key]
            return None
        return entry[1]

    def put(self, key: tuple[str, str], value: Capabilities) -> None:
        if self.ttl <= 0:
            return
        self._entries[key] = (self._clock() + self.ttl, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


def _parse_json(response: RawResponse) -> object:
    media_type = response.content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ChatUpstreamError("not_json")
    try:
        return strict_loads(response.body)
    except ValueError:
        raise ChatUpstreamError("invalid_json") from None


class AiChatClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._url: ServiceUrl = parse_service_url(settings.ai_service_url)
        self._token: SecretStr | None = settings.ai_service_token
        self._transport = transport
        fingerprint = (
            hashlib.sha256(self._token.get_secret_value().encode()).hexdigest()[:16] if self._token else "none"
        )
        self._cache_key = (str(self._url), fingerprint)
        self.cache = CapabilityCache(settings.chat_capabilities_cache_seconds, clock=clock)

    def __repr__(self) -> str:
        return f"AiChatClient(url={self._url}, token={'set' if self._token else 'unset'})"

    @property
    def configured(self) -> bool:
        """A bearer token is set and long enough (the service URL was validated at construction)."""
        return self._token is not None and len(self._token.get_secret_value()) >= MIN_TOKEN_LENGTH

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
            "X-StudyGraph-Protocol": PROTOCOL,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        return headers

    def _new_client(self) -> httpx.AsyncClient:
        s = self._settings
        timeout = httpx.Timeout(
            connect=s.chat_connect_timeout_seconds,
            read=s.chat_read_timeout_seconds,
            write=s.chat_read_timeout_seconds,
            pool=s.chat_connect_timeout_seconds,
        )
        return httpx.AsyncClient(
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            http2=False,
        )

    async def _exchange(
        self, method: str, path: str, body: bytes | None, *, deadline: float, max_bytes: int
    ) -> RawResponse:
        url = f"{self._url}{path}"
        try:
            with anyio.fail_after(deadline):
                async with self._new_client() as client:
                    request = client.build_request(method, url, content=body, headers=self._headers(json_body=body is not None))
                    response = await client.send(request, stream=True)
                    try:
                        return await self._read(response, max_bytes)
                    finally:
                        await response.aclose()
        except TimeoutError:
            raise ChatUpstreamError("deadline") from None
        except httpx.TimeoutException:
            raise ChatUpstreamError("timeout") from None
        except httpx.HTTPError as exc:
            raise ChatUpstreamError(f"transport_{type(exc).__name__}") from None

    @staticmethod
    async def _read(response: httpx.Response, max_bytes: int) -> RawResponse:
        if 300 <= response.status_code < 400:
            raise ChatUpstreamError("redirect_refused")
        headers = response.headers
        if headers.get_list("transfer-encoding"):
            raise ChatUpstreamError("invalid_framing")
        encodings = headers.get_list("content-encoding")
        if any(e.strip().lower() not in ("", "identity") for e in encodings):
            raise ChatUpstreamError("unsupported_encoding")
        lengths = headers.get_list("content-length")
        if len(lengths) != 1 or not lengths[0] or not set(lengths[0]) <= _DIGITS or len(lengths[0]) > 12:
            raise ChatUpstreamError("invalid_framing")
        declared = int(lengths[0])
        if declared > max_bytes:
            raise ChatUpstreamError("response_too_large")
        content_types = headers.get_list("content-type")
        if len(content_types) != 1:
            raise ChatUpstreamError("invalid_content_type")
        body = bytearray()
        async for chunk in response.aiter_raw():
            body += chunk
            if len(body) > declared:
                raise ChatUpstreamError("invalid_framing")
        if len(body) != declared:
            raise ChatUpstreamError("invalid_framing")
        return RawResponse(response.status_code, content_types[0], bytes(body))

    async def capabilities(self) -> Capabilities:
        """The service's capabilities, from the cache when fresh (successful and failed probes alike)."""
        cached = self.cache.get(self._cache_key)
        if cached is not None:
            return cached
        s = self._settings
        try:
            response = await self._exchange(
                "GET", CAPABILITIES_PATH, None, deadline=s.chat_probe_deadline_seconds, max_bytes=PROBE_MAX_RESPONSE_BYTES
            )
            body = _parse_json(response) if response.status == 200 else None
            result = validate_capabilities(response.status, body)
        except ChatUpstreamError as exc:
            result = Capabilities(False, f"probe_{exc.code}")
        self.cache.put(self._cache_key, result)
        if not result.ready:
            log.warning("chat_ai_not_ready reason=%s url=%s", result.reason, self._url)
        return result

    async def answer(self, payload: dict) -> object:
        """POST the question once (never retried). The parsed JSON of a 200 answer, else ChatUpstreamError."""
        s = self._settings
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        response = await self._exchange(
            "POST", ANSWER_PATH, body, deadline=s.chat_answer_deadline_seconds, max_bytes=s.chat_max_response_bytes
        )
        if response.status != 200:
            raise ChatUpstreamError(f"http_{response.status}")
        return _parse_json(response)
