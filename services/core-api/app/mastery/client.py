"""HTTP client for studygraph.mastery/1, on the M5/M7 transport (app/chat/client.py), synchronous entry points.

Same rules as the chat and quiz clients: a new AsyncClient per call, trust_env=False (no
environment proxies, .netrc or certificate overrides), no redirects, identity encoding
only, exactly one Content-Length that the raw body must match, a running byte limit,
strict JSON (no repeated keys, no NaN), an absolute anyio deadline around each call, and
NO automatic retry: a failed update is retried only by the delivery state machine, with
the SAME batch (app/mastery/delivery.py). The bearer token is only placed in the
Authorization header and never logged. Responses are never logged, not even in part.

The worker is synchronous (Celery prefork); `anyio.run` drives each call inside the task,
so the task's soft and hard time limits bound it as well.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

import anyio
import httpx

from ..chat.client import AiChatClient, CapabilityCache, ChatUpstreamError, _parse_json
from ..config import Settings
from .pins import PROTOCOL, Pins
from .protocol import CAPABILITIES_PATH, UPDATE_PATH, Capabilities, validate_capabilities

log = logging.getLogger(__name__)

USER_AGENT = "studygraph-core-api/mastery-client"
PROBE_MAX_RESPONSE_BYTES = 16 * 1024

MasteryUpstreamError = ChatUpstreamError


class AiMasteryClient(AiChatClient):
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(settings, transport=transport, clock=clock)
        self.cache = CapabilityCache(settings.mastery_capabilities_cache_seconds, clock=clock)

    def __repr__(self) -> str:
        return f"AiMasteryClient(url={self._url}, token={'set' if self._token else 'unset'})"

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        headers = super()._headers(json_body=json_body)
        headers["User-Agent"] = USER_AGENT
        headers["X-StudyGraph-Protocol"] = PROTOCOL
        return headers

    def _new_client(self) -> httpx.AsyncClient:
        s = self._settings
        timeout = httpx.Timeout(
            connect=s.mastery_connect_timeout_seconds,
            read=s.mastery_read_timeout_seconds,
            write=s.mastery_read_timeout_seconds,
            pool=s.mastery_connect_timeout_seconds,
        )
        return httpx.AsyncClient(
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            http2=False,
        )

    async def probe(self, pins: Pins) -> Capabilities:  # cached per process, successes and failures alike
        key = (*self._cache_key, f"{pins.model_name}/{pins.model_version}/{pins.state_epoch}")
        cached = self.cache.get(key)  # type: ignore[arg-type]
        if cached is not None:
            return cached  # type: ignore[return-value]
        try:
            response = await self._exchange(
                "GET", CAPABILITIES_PATH, None,
                deadline=self._settings.mastery_probe_deadline_seconds, max_bytes=PROBE_MAX_RESPONSE_BYTES,
            )  # fmt: skip
            body = _parse_json(response) if response.status == 200 else None
            result = validate_capabilities(response.status, body, pins)
        except ChatUpstreamError as exc:
            result = Capabilities("unavailable", f"probe_{exc.code}"[:40])
        self.cache.put(key, result)  # type: ignore[arg-type]
        if result.status != "compatible":
            log.warning("mastery_ai_not_ready status=%s reason=%s url=%s", result.status, result.reason, self._url)
        return result

    async def update(self, body: bytes) -> object:
        """POST one batch once. The parsed JSON of a 200, else MasteryUpstreamError (code safe to log)."""
        s = self._settings
        response = await self._exchange(
            "POST", UPDATE_PATH, body, deadline=s.mastery_update_deadline_seconds, max_bytes=s.mastery_max_response_bytes
        )
        if response.status != 200:
            raise ChatUpstreamError(f"http_{response.status}")
        return _parse_json(response)

    async def answer(self, payload: dict) -> object:  # pragma: no cover - not offered here
        raise NotImplementedError("use update()")

    # --- synchronous entry points for the Celery task --------------------------------------
    def probe_sync(self, pins: Pins) -> Capabilities:
        return anyio.run(self.probe, pins)

    def update_sync(self, body: bytes) -> object:
        return anyio.run(self.update, body)


def encode_request(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
