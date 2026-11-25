"""Restricted HTTPS transport for public knowledge providers (M6).

Every provider request goes through `ProviderTransport.get`, which only ever
talks to a fixed HTTPS host and path chosen by code (never by the database, a
setting or a provider response):

- Destination. The host comes from `ProviderEndpoint` (for Wikipedia,
  "<language>.wikipedia.org" for an allowlisted language; for OpenStax,
  "api.github.com") and the path is fixed ("/w/api.php") or, for OpenStax,
  built in code from the book's fixed repository and a 40-hex git object id
  taken from a GitHub answer (validated by ProviderEndpoint). Port 443, HTTPS only. Query parameters are built by
  the connector; no URL from a provider response is ever fetched.
- One DNS resolution, then pinned connections. The host is resolved exactly
  once per request. Every returned address must be a public unicast address
  (loopback, private, link-local, shared, reserved, multicast, unspecified,
  documentation, NAT64, 6to4 and Teredo ranges are refused, IPv4-mapped IPv6
  is judged by its IPv4 address). If ANY address is not public the request is
  refused (no partial use of a mixed answer). The TCP connection is then made
  to one of those verified IP addresses directly, so there is no second,
  unchecked resolution between validation and connection.
- TLS. The socket is wrapped with a verifying context (CERT_REQUIRED,
  check_hostname, TLS >= 1.2) and server_hostname = the provider host, so
  SNI and certificate hostname verification use the provider host although
  the connection goes to an IP; the HTTP Host header is the provider host.
- No redirects (any 3xx is an error and Location is never read), no
  environment proxies (the socket is opened directly; HTTP(S)_PROXY /
  NO_PROXY are never read), no cookies (nothing stores or sends them), no
  authentication headers.
- Identity encoding. Accept-Encoding: identity is requested and a response
  with any other Content-Encoding is refused, so the size limit applies to the
  bytes actually received and no decompression bomb is possible.
- HTTP framing is delegated to h11 (strict, sans-I/O HTTP/1.1; already part
  of the pinned runtime set as a dependency of httpx, now pinned directly):
  this module only moves bytes between the pinned, verified TLS socket and
  h11. A chunked body without its terminating chunk, a malformed chunk
  delimiter or size line, a body shorter than its Content-Length, and a
  response without exactly one framing header are all refused; they never
  reach the JSON parser. (The standard library's http.client accepted a
  missing final chunk and an invalid chunk delimiter, which is why it is not
  used for response parsing.)
- Bounded response. A declared Content-Length over the limit is refused
  before reading; the body is read in chunks and refused as soon as it
  exceeds the limit; header blocks are size-limited by h11.
- Time bounds. Connect and read timeouts bound each socket operation's
  inactivity; a deadline is checked before each body read. Name resolution
  (getaddrinfo) is not covered by any socket timeout. The absolute wall-clock
  bound is the harvest task's Celery limits (soft raises, hard kills the
  process).
- Strict JSON: UTF-8, application/json, no duplicate object keys, no NaN or
  Infinity, an object at the top level.

What this module does NOT provide: network isolation. A compromised or buggy
process could still open other connections. Restricting egress (only TCP 443
to the provider's published ranges, from the harvest worker only) is an
infrastructure control, documented in docs/external-harvesting.md.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import h11

READ_CHUNK = 64 * 1024

# NAT64 and IPv6 transition ranges that can embed or reach arbitrary IPv4 space.
_REFUSED_V6_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "100::/64")
)

_USER_AGENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}/[0-9A-Za-z][0-9A-Za-z._+-]{0,31}"
    r" \((?P<contact>https://[^\s();]{4,200}|mailto:[^\s();@]+@[^\s();@]+\.[A-Za-z]{2,}"
    r"|[^\s();@]+@[^\s();@]+\.[A-Za-z]{2,})(?:; [^()\x00-\x1f\x7f]{1,100})?\)"
    r"(?: [\x21-\x7e][\x20-\x7e]{0,99})?\Z"
)
_PLACEHOLDER_CONTACT = re.compile(r"example\.(com|org|net)|localhost|\.test\b|\.invalid\b|\.local\b", re.I)


def validate_user_agent(value: str) -> str:
    """A Wikimedia-compliant User-Agent: "<client>/<version> (<contact URL or e-mail>) [library/version]".

    Raises ValueError (without echoing the value) for anything else, for
    placeholder contacts and for generic library user agents.
    """
    if not isinstance(value, str) or value != value.strip() or not _USER_AGENT.match(value):
        raise ValueError(
            "HARVEST_USER_AGENT must look like 'StudyGraphHarvester/1.0 (https://your.site/contact; ops@your.site)': "
            "client/version, then a real contact URL or e-mail in parentheses"
        )
    if _PLACEHOLDER_CONTACT.search(value):
        raise ValueError("HARVEST_USER_AGENT must carry a real, reachable contact, not a placeholder")
    if value.lower().startswith(("python", "curl", "mozilla", "requests", "httpx", "urllib")):
        raise ValueError("HARVEST_USER_AGENT must name this client, not a generic library or browser")
    return value


class TransportError(Exception):
    """No usable response. `retryable` is False for policy refusals (they will not change by retrying)."""

    def __init__(self, code: str, detail: str = "", *, retryable: bool) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retryable = retryable
        # Set when the validated head was a 429/503 with a valid Retry-After but the body was rejected.
        self.throttle_status: int | None = None
        self.retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ProviderEndpoint:
    host: str
    path: str
    port: int = 443

    def __post_init__(self) -> None:
        if not re.fullmatch(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", self.host):
            raise ValueError("invalid provider host")
        if not re.fullmatch(r"/[A-Za-z0-9/._-]{1,100}", self.path):
            raise ValueError("invalid provider path")
        if self.port != 443:
            raise ValueError("providers are reached on port 443 only")


@dataclass(frozen=True)
class TransportBounds:
    connect_timeout: float
    read_timeout: float
    deadline: float
    max_response_bytes: int


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    content_type: str
    body: bytes
    retry_after_seconds: float | None = None
    # GitHub (OpenStax content, WP-6): seconds until the primary rate-limit window resets, set only when the
    # response says no request is left (x-ratelimit-remaining: 0 with an x-ratelimit-reset epoch).
    rate_limit_reset_seconds: float | None = None


Resolver = Callable[[str, int], list[str]]
SocketFactory = Callable[[str, int, float], socket.socket]


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def system_socket(ip: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=timeout)


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return False
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in network for network in _REFUSED_V6_NETWORKS):
            return False
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def resolve_public(host: str, port: int, resolver: Resolver) -> list[str]:
    """Resolve once; every address must be public, else the whole answer is refused."""
    try:
        answers = resolver(host, port)
    except (OSError, UnicodeError) as exc:
        raise TransportError("dns_failed", type(exc).__name__, retryable=True) from None
    unique = list(dict.fromkeys(answers))
    if not unique:
        raise TransportError("dns_failed", "no addresses", retryable=True)
    refused = [a for a in unique if not is_public_address(a)]
    if refused:
        raise TransportError("non_public_address", f"{len(refused)} of {len(unique)} addresses", retryable=False)
    return unique


def tls_context(cafile: str | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cafile)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _declared_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    """Content-Length, or None for chunked framing; anything ambiguous is refused."""
    lengths = [v.decode("latin-1").strip() for k, v in headers if k == b"content-length"]
    encodings = [v.decode("latin-1").strip().lower() for k, v in headers if k == b"transfer-encoding"]
    if encodings:
        if lengths or encodings != ["chunked"]:
            raise TransportError("invalid_framing", "ambiguous or unsupported Transfer-Encoding", retryable=True)
        return None
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,18}", lengths[0]):
        raise TransportError("invalid_framing", "missing, repeated or non-numeric Content-Length", retryable=True)
    return int(lengths[0])


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    """The header's value; repeated headers are joined (so a repeated Retry-After parses as invalid)."""
    values = [v.decode("latin-1").strip() for k, v in headers if k == name]
    return ", ".join(values) if values else None


def parse_rate_limit_reset(remaining: str | None, reset: str | None, now: float | None = None) -> float | None:
    """Seconds until a GitHub-style rate-limit window resets, only when no request is left; else None."""
    if remaining is None or remaining.strip() != "0" or reset is None or not re.fullmatch(r"[0-9]{1,12}", reset.strip()):
        return None
    return max(0.0, float(int(reset.strip())) - (time.time() if now is None else now))


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date); None if absent or invalid."""
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        significant = value.lstrip("0")
        if len(significant) > 9:
            # Valid but far beyond any representable delay: infinite, so the source is suspended
            # (budget.block) without converting an arbitrarily large integer.
            return math.inf
        return float(int(significant or "0"))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


class ProviderTransport:
    def __init__(
        self,
        user_agent: str,
        *,
        resolver: Resolver = system_resolver,
        socket_factory: SocketFactory = system_socket,
        context: ssl.SSLContext | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._user_agent = validate_user_agent(user_agent)
        self._resolver = resolver
        self._socket_factory = socket_factory
        self._context = context or tls_context()
        self._clock = clock

    def get(self, endpoint: ProviderEndpoint, params: Mapping[str, str], bounds: TransportBounds) -> ProviderResponse:
        started = self._clock()
        target = endpoint.path + "?" + urlencode(sorted(params.items()))
        addresses = resolve_public(endpoint.host, endpoint.port, self._resolver)
        last: TransportError | None = None
        for ip in addresses:
            raw = None
            tls = None
            try:
                try:
                    raw = self._socket_factory(ip, endpoint.port, bounds.connect_timeout)
                    # SNI and certificate hostname verification use the provider host, not the pinned IP.
                    tls = self._context.wrap_socket(raw, server_hostname=endpoint.host)
                except ssl.SSLCertVerificationError as exc:
                    raise TransportError("tls_verification_failed", exc.reason or "", retryable=False) from None
                except (OSError, ssl.SSLError) as exc:
                    last = TransportError("connect_failed", type(exc).__name__, retryable=True)
                    continue
                return self._exchange(tls, endpoint.host, target, bounds, started)
            finally:
                for opened in (tls, raw):
                    if opened is not None:
                        try:
                            opened.close()
                        except Exception:  # noqa: BLE001
                            pass
        raise last or TransportError("connect_failed", "no address", retryable=True)

    def _exchange(
        self, sock: socket.socket, host: str, target: str, bounds: TransportBounds, started: float
    ) -> ProviderResponse:
        throttle: list[tuple[int, float]] = []
        try:
            return self._exchange_inner(sock, host, target, bounds, started, throttle)
        except TransportError as exc:
            if throttle:  # keep the provider's delay although its body was unusable
                exc.throttle_status, exc.retry_after_seconds = throttle[0]
            raise

    def _exchange_inner(
        self,
        sock: socket.socket,
        host: str,
        target: str,
        bounds: TransportBounds,
        started: float,
        throttle: list[tuple[int, float]],
    ) -> ProviderResponse:
        sock.settimeout(bounds.read_timeout)
        connection = h11.Connection(our_role=h11.CLIENT, max_incomplete_event_size=16 * 1024)
        request = h11.Request(
            method="GET",
            target=target,
            headers=[
                ("Host", host),
                ("User-Agent", self._user_agent),
                ("Accept", "application/json"),
                ("Accept-Encoding", "identity"),
                ("Connection", "close"),
            ],
        )
        try:
            sock.sendall(connection.send(request) + connection.send(h11.EndOfMessage()))
        except h11.LocalProtocolError:
            raise TransportError("invalid_request", "request refused by the HTTP framing layer", retryable=False) from None
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(_io_code(exc, "connection_lost"), type(exc).__name__, retryable=True) from None
        head: h11.Response | None = None
        declared: int | None = None
        chunks: list[bytes] = []
        size = 0
        at_eof = False
        while True:
            if self._clock() - started > bounds.deadline:
                raise TransportError("deadline_exceeded", f"{bounds.deadline}s", retryable=True)
            try:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    data = sock.recv(READ_CHUNK)
                    at_eof = at_eof or not data
                    connection.receive_data(data)
                    continue
            except h11.RemoteProtocolError as exc:
                # After EOF: the peer closed before the framed message was complete (e.g. a missing final
                # chunk). Before EOF: malformed framing (e.g. an invalid chunk delimiter or size line).
                if at_eof:
                    raise TransportError("incomplete_response", "connection closed before the message was complete", retryable=True) from None
                raise TransportError("invalid_framing", type(exc).__name__, retryable=True) from None
            except (OSError, ssl.SSLError) as exc:
                raise TransportError(_io_code(exc, "incomplete_response"), type(exc).__name__, retryable=True) from None
            if isinstance(event, h11.InformationalResponse):
                continue
            if isinstance(event, h11.Response):
                head = event
                if head.status_code in (429, 503):
                    delay = parse_retry_after(_header(head.headers, b"retry-after"))
                    if delay is not None:
                        throttle.append((head.status_code, delay))
                if 300 <= head.status_code < 400:  # never followed, whatever the Location says
                    raise TransportError("redirect_refused", str(head.status_code), retryable=False)
                encoding = (_header(head.headers, b"content-encoding") or "identity").lower()
                if encoding != "identity":
                    raise TransportError("encoded_response", "Content-Encoding is not identity", retryable=False)
                declared = _declared_length(head.headers)
                if declared is not None and declared > bounds.max_response_bytes:
                    raise TransportError("response_too_large", f"Content-Length {declared}", retryable=False)
            elif isinstance(event, h11.Data):
                size += len(event.data)
                if size > bounds.max_response_bytes:
                    raise TransportError("response_too_large", f"over {bounds.max_response_bytes} bytes", retryable=False)
                chunks.append(bytes(event.data))
            elif isinstance(event, h11.EndOfMessage):
                break
            else:  # ConnectionClosed or PAUSED before the message ended
                raise TransportError("incomplete_response", type(event).__name__, retryable=True)
        if head is None or (declared is not None and size != declared):
            raise TransportError("incomplete_response", f"{size} of {declared} bytes", retryable=True)
        return ProviderResponse(
            status=head.status_code,
            content_type=(_header(head.headers, b"content-type") or "").lower(),
            body=b"".join(chunks),
            retry_after_seconds=parse_retry_after(_header(head.headers, b"retry-after")),
            rate_limit_reset_seconds=parse_rate_limit_reset(
                _header(head.headers, b"x-ratelimit-remaining"), _header(head.headers, b"x-ratelimit-reset")
            ),
        )


def _io_code(exc: BaseException, default: str) -> str:
    return "timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else default


# --- strict JSON -------------------------------------------------------------------------------


class InvalidJson(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise InvalidJson("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise InvalidJson(f"{name} is not valid JSON")


def strict_json_object(response: ProviderResponse) -> dict:
    media_type = response.content_type.split(";", 1)[0].strip()
    if media_type != "application/json":
        raise InvalidJson("not application/json")
    try:
        value = json.loads(
            response.body.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except InvalidJson:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InvalidJson(type(exc).__name__) from None
    if not isinstance(value, dict):
        raise InvalidJson("top level is not an object")
    return value
