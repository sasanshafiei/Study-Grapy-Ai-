"""HTTP client for the proposed studygraph.quiz/1 endpoints.

It reuses the M5 chat client's transport unchanged (app/chat/client.py): a new
AsyncClient per call closed on every exit, trust_env=False (no environment
proxies, .netrc or certificate overrides), no redirects (a 3xx is an error),
identity encoding only, no Transfer-Encoding, exactly one Content-Length that
the raw body must match, a running byte limit, strict JSON (no repeated keys,
no NaN), an absolute anyio deadline around each call, and NO automatic retry.
Only the paths, the protocol header, the timeouts, the limits and the
capability validation differ; the chat client itself is not modified.

The bearer token (AI_SERVICE_TOKEN) is only placed in the Authorization header
and never appears in logs, reprs or errors.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

import httpx

from ..chat.client import AiChatClient, CapabilityCache, ChatUpstreamError, _parse_json
from ..config import Settings
from .protocol import CAPABILITIES_PATH, GENERATE_PATH, PROTOCOL, QuizCapabilities, validate_capabilities

log = logging.getLogger(__name__)

USER_AGENT = "studygraph-core-api/quiz-client"
PROBE_MAX_RESPONSE_BYTES = 16 * 1024

QuizUpstreamError = ChatUpstreamError  # same transport failures, same safe-to-log codes


class AiQuizClient(AiChatClient):
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(settings, transport=transport, clock=clock)
        self.cache = CapabilityCache(settings.quiz_capabilities_cache_seconds, clock=clock)

    def __repr__(self) -> str:
        return f"AiQuizClient(url={self._url}, token={'set' if self._token else 'unset'})"

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        headers = super()._headers(json_body=json_body)
        headers["User-Agent"] = USER_AGENT
        headers["X-StudyGraph-Protocol"] = PROTOCOL
        return headers

    def _new_client(self) -> httpx.AsyncClient:
        s = self._settings
        timeout = httpx.Timeout(
            connect=s.quiz_connect_timeout_seconds,
            read=s.quiz_read_timeout_seconds,
            write=s.quiz_read_timeout_seconds,
            pool=s.quiz_connect_timeout_seconds,
        )
        return httpx.AsyncClient(
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            http2=False,
        )

    async def capabilities(self) -> QuizCapabilities:  # type: ignore[override]
        """Authenticated capability probe, cached (successful and failed results alike) for a short time."""
        cached = self.cache.get(self._cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        try:
            response = await self._exchange(
                "GET",
                CAPABILITIES_PATH,
                None,
                deadline=self._settings.quiz_probe_deadline_seconds,
                max_bytes=PROBE_MAX_RESPONSE_BYTES,
            )
            body = _parse_json(response) if response.status == 200 else None
            result = validate_capabilities(response.status, body)
        except ChatUpstreamError as exc:
            result = QuizCapabilities(False, f"probe_{exc.code}")
        self.cache.put(self._cache_key, result)  # type: ignore[arg-type]
        if not result.ready:
            log.warning("quiz_ai_not_ready reason=%s url=%s", result.reason, self._url)
        return result

    async def answer(self, payload: dict) -> object:  # pragma: no cover - the chat path is not offered here
        raise NotImplementedError("use generate()")

    async def generate(self, payload: dict) -> object:
        """POST the generation request once (never retried). Parsed JSON of a 200, else QuizUpstreamError."""
        s = self._settings
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        response = await self._exchange(
            "POST", GENERATE_PATH, body, deadline=s.quiz_generate_deadline_seconds, max_bytes=s.quiz_max_response_bytes
        )
        if response.status != 200:
            raise ChatUpstreamError(f"http_{response.status}")
        return _parse_json(response)
