"""Browser CORS policy (F12, docs/frontend-exam-adaptation.md#browser-origins-and-readable-headers).

CORS only decides which other web origins a browser lets read API responses. It is not
authentication: every protected route still requires a bearer token, with or without an
Origin header, and non-browser clients ignore CORS entirely.

Only the origin allowlist is configurable (CORS_ALLOWED_ORIGINS, exact origins). The empty
default installs no CORS middleware at all, which suits same-origin deployments. Methods,
request headers and readable response headers are fixed here to what the routes use.
Credentials mode stays off: the API authenticates with the Authorization header, never with
cookies, so `Access-Control-Allow-Credentials` is never sent.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Methods the routes use. GET, HEAD and POST are CORS-safelisted, the others need a preflight.
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# Request headers the API reads beyond the CORS-safelisted ones. Content-Type is listed because
# application/json is not a safelisted content type.
ALLOWED_HEADERS = ("Authorization", "Content-Type", "Idempotency-Key", "If-Match")
# Application response headers browsers may read; each one is emitted by an existing endpoint.
# Safelisted response headers (Cache-Control, Content-Type, ...) are readable anyway.
EXPOSED_HEADERS = (
    "ETag",
    "Location",
    "Retry-After",
    "Idempotent-Replayed",
    "Content-Disposition",
    "X-Exam-Item-Id",
    "X-Exam-Renderer-Version",
    "X-Exam-Version",
    "X-Exam-Preview-Fingerprint",
    "X-Exam-Finalized-Version",
    "X-Exam-Content-SHA256",
)
PREFLIGHT_MAX_AGE_SECONDS = 600

# Plain http and loopback origins are accepted only in these environments (same set as the demo seed).
LOCAL_ENVIRONMENTS = frozenset({"development", "test"})

_ORIGIN = re.compile(r"(?P<scheme>https?)://(?P<host>[a-z0-9.-]+|\[[0-9a-f:.]+\])(?::(?P<port>[1-9][0-9]{0,4}))?")
_DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_SHAPE = "must be an exact origin: lower-case http(s)://host[:port] without path, slash, query, fragment or user info"


def _origin_problem(origin: str, local: bool) -> str | None:
    """Why `origin` is not acceptable, or None. Messages never echo the value."""
    if "*" in origin:
        return "must not contain a wildcard; list each exact origin"
    if origin.strip().lower() == "null":
        return "must not be null"
    match = _ORIGIN.fullmatch(origin)
    if match is None:
        return _SHAPE
    scheme, host, port = match["scheme"], match["host"], match["port"]
    if port is not None:
        if int(port) > 65535:
            return "has an invalid port"
        if int(port) == _DEFAULT_PORTS[scheme]:
            return "must omit the scheme's default port (browsers never send it)"
    if host.startswith("["):
        try:
            address = ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return "has an invalid IPv6 address"
        if f"[{address.compressed}]" != host:
            return "must write the IPv6 address in its compressed form, as browsers send it"
        loopback = address.is_loopback
    elif host.rsplit(".", 1)[-1].isdigit():
        try:
            loopback = ipaddress.IPv4Address(host).is_loopback
        except ValueError:
            return "has an invalid IPv4 address"
    elif len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in host.split(".")):
        return "has an invalid host name"
    else:
        loopback = host == "localhost" or host.endswith(".localhost")
    if not local:
        if scheme != "https":
            return "must use https unless ENVIRONMENT is development or test"
        if loopback:
            return "must not be a loopback origin unless ENVIRONMENT is development or test"
    return None


def validate_allowed_origins(origins: Sequence[str], environment: str) -> None:
    """Raise ValueError unless every entry is a distinct, exact browser origin.

    Each entry must equal the Origin header a browser sends: no wildcard, no `null`, no
    trailing slash or path, no default port, lower case. Outside ENVIRONMENT=development or
    test only https, non-loopback origins are accepted.
    """
    local = environment in LOCAL_ENVIRONMENTS
    seen: set[str] = set()
    for index, origin in enumerate(origins):
        problem = _origin_problem(origin, local)
        if problem is None and origin in seen:
            problem = "is listed twice"
        if problem is not None:
            raise ValueError(f"CORS_ALLOWED_ORIGINS[{index}] {problem}")
        seen.add(origin)


class ExactOriginCORS:
    """Starlette's CORSMiddleware with exact origins, plus two guarantees it lacks.

    - A request whose Origin is not allowlisted gets no `Access-Control-*` header at all
      (Starlette would still send the exposed headers, and the allowed methods/headers on a
      refused preflight).
    - Every response carries `Vary: Origin`, because its CORS headers depend on that header.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Sequence[str]) -> None:
        self.allowed_origins = frozenset(allowed_origins)
        self.app = CORSMiddleware(
            app,
            allow_origins=list(allowed_origins),
            allow_methods=ALLOWED_METHODS,
            allow_headers=ALLOWED_HEADERS,
            allow_credentials=False,
            expose_headers=EXPOSED_HEADERS,
            max_age=PREFLIGHT_MAX_AGE_SECONDS,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - the API serves no websockets
            await self.app(scope, receive, send)
            return
        allowed = Headers(scope=scope).get("origin") in self.allowed_origins

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                if not allowed:
                    for name in [key for key in headers.keys() if key.startswith("access-control-")]:
                        del headers[name]
                vary = [token.strip().lower() for token in headers.get("vary", "").split(",")]
                if "origin" not in vary and "*" not in vary:
                    headers.add_vary_header("Origin")
            await send(message)

        await self.app(scope, receive, send_with_policy)
