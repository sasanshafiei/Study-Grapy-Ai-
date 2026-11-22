"""An isolated, standard-library fake of the *proposed* AI index protocol, for tests only.

TEST DOUBLE. This is not the AI service, not a mock shipped with the product,
and not evidence that real indexing works: it stores nothing in a database,
embeds nothing and chunks nothing. It implements the CE-side proposal of
docs/ai-indexing-integration.md just far enough to exercise core-api's
client, state machine and workers:

- GET /ai/v1/index/capabilities and POST /ai/v1/index/documents, bearer auth;
- an independent re-computation of the canonical digest and idempotency key
  (it does not import app.indexing), so a payload that is not
  self-consistent is refused as the real service is expected to refuse it;
- idempotency: a key it has committed is answered again (replayed: true)
  without a second commit;
- generations, as the proposal requires (§6 rules 5 and 6): ONE committed
  generation per document. A request for a document that already has one is
  refused with 409 course_mismatch (another course) or 409 generation_conflict
  (same course, different key, i.e. different content), never committed.
  The check and the commit happen under one lock, so concurrent requests with
  different keys cannot both commit;
- "commits" count durable index operations, per course, for the duplicate,
  lost-response and course-isolation tests;
- scripted faults: lose the response after committing, block until released,
  answer with a given status/body, or delay;
- M6 locator negotiation: `locators` (None = an M4 service that advertises
  nothing, i.e. page only) is advertised in the capabilities when set; a
  section-based request to a service that did not advertise "section" is
  refused (422 locator_scheme_unsupported) and counted in
  `unadvertised_section_requests`, so tests can prove core never sends one.

It listens on 127.0.0.1 on an ephemeral port, in a daemon thread of the test
process; real worker subprocesses started by the tests reach it there.
"""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Callable
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL = "studygraph.index/1"
TOKEN = "test-token-" + "x" * 32


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass
class Commit:
    key: str
    digest: str
    document_id: str
    course_id: str
    section_count: int
    chunk_count: int


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict
    body: bytes


@dataclass
class Fault:
    """A scripted answer for the next index request(s)."""

    kind: str  # "status" | "lose_after_commit" | "drop_before_commit" | "delay"
    status: int = 500
    body: bytes = b""
    content_type: str = "application/json"
    seconds: float = 0.0


@dataclass
class FakeAiService:
    token: str = TOKEN
    capabilities: dict | None = None
    capabilities_status: int = 200
    max_request_bytes: int = 16 * 1024 * 1024
    commits: dict[str, Commit] = field(default_factory=dict)  # by idempotency key
    generations: dict[str, Commit] = field(default_factory=dict)  # by document id: at most one each
    requests: list[Recorded] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    locators: list[str] | None = None
    unadvertised_section_requests: int = 0
    persist: Callable | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._commit_lock = threading.Lock()  # serializes "is there a generation?" + "commit"
        self.gate = threading.Event()
        self.gate.set()  # open: requests are not blocked
        self.blocked = threading.Event()  # set while a request waits at the gate
        self.commit_count = 0
        self._server: ThreadingHTTPServer | None = None

    # lifecycle

    def start(self) -> "FakeAiService":
        service = self

        class Handler(_Handler):
            fake = service

        self._server = _Server(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.gate.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # helpers for tests

    def block(self) -> None:
        self.gate.clear()

    def release(self) -> None:
        self.gate.set()

    def index_requests(self) -> list[Recorded]:
        with self._lock:
            return [r for r in self.requests if r.path.endswith("/index/documents")]

    def commits_for_course(self, course_id) -> list[Commit]:
        with self._lock:
            return [c for c in self.commits.values() if c.course_id == str(course_id)]

    def next_fault(self) -> Fault | None:
        with self._lock:
            return self.faults.pop(0) if self.faults else None


class _Server(ThreadingHTTPServer):
    request_queue_size = 128  # the default backlog (5) drops simultaneous connects, which made racing tests flaky


class _Handler(BaseHTTPRequestHandler):
    fake: FakeAiService
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # quiet
        pass

    def _send(self, status: int, payload, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, retryable: bool) -> None:
        self._send(status, {"error": {"code": code, "message": code.replace("_", " "), "retryable": retryable}})

    def _record(self, body: bytes) -> None:
        with self.fake._lock:
            self.fake.requests.append(Recorded(self.command, self.path, dict(self.headers.items()), body))

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.fake.token}":
            self._error(401, "unauthorized", False)
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        self._record(b"")
        if self.path != "/ai/v1/index/capabilities":
            self._error(404, "not_found", False)
            return
        if not self._authorized():
            return
        if self.fake.capabilities_status != 200:
            self._error(self.fake.capabilities_status, "unavailable", True)
            return
        capabilities = self.fake.capabilities or {
            "protocol": PROTOCOL,
            "service": "ai-service",
            "mock": False,
            "completion": ["sync"],
            "max_request_bytes": self.fake.max_request_bytes,
        }
        if self.fake.capabilities is None and self.fake.locators is not None:
            capabilities = {**capabilities, "locators": list(self.fake.locators)}
        self._send(200, capabilities)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        if self.path != "/ai/v1/index/documents":
            self._error(404, "not_found", False)
            return
        if not self._authorized():
            return
        if not self.fake.gate.is_set():
            self.fake.blocked.set()
            self.fake.gate.wait(600)
        fault = self.fake.next_fault()
        if fault is not None and fault.kind == "status":
            self._send(fault.status, fault.body, fault.content_type)
            return
        if fault is not None and fault.kind == "drop_before_commit":
            self._drop()
            return
        if fault is not None and fault.kind == "delay":
            time.sleep(fault.seconds)
        if length > self.fake.max_request_bytes:
            self._error(413, "payload_too_large", False)
            return
        try:
            request = json.loads(body.decode("utf-8"))
            content = {k: request[k] for k in ("protocol", "document", "sections")}
            digest = hashlib.sha256(_canonical(content)).hexdigest()
            document, sections = request["document"], request["sections"]
            key = hashlib.sha256(
                "\0".join((PROTOCOL, document["id"], document["course_id"], digest)).encode()
            ).hexdigest()
        except (ValueError, KeyError, TypeError, AttributeError):
            self._error(400, "invalid_request", False)
            return
        if document.get("locator_scheme") == "section" and "section" not in (self.fake.locators or ["page"]):
            self.fake.unadvertised_section_requests += 1
            self._error(422, "locator_scheme_unsupported", False)
            return
        if (
            request.get("protocol") != PROTOCOL
            or request.get("content_digest") != digest
            or request.get("idempotency_key") != key
            or self.headers.get("Idempotency-Key") != key
        ):
            self._error(400, "inconsistent_request", False)
            return
        if self.fake.persist is not None:
            # The SQL transaction commits on another connection before the receipt or scripted loss.
            count, replayed = self.fake.persist(request)
            existing = Commit(key, digest, document["id"], document["course_id"], len(sections), count)
            with self.fake._lock:
                self.fake.commits[key] = existing  # observations for assertions only
                self.fake.commit_count += int(not replayed)
        else:
            with self.fake._commit_lock:  # one decision at a time: same key replays, other keys cannot also commit
                existing = self.fake.commits.get(key)
                replayed = existing is not None
                if existing is None:
                    generation = self.fake.generations.get(document["id"])
                    if generation is not None:
                        refused = (
                            "course_mismatch" if generation.course_id != document["course_id"] else "generation_conflict"
                        )
                    else:
                        refused = None
                        existing = Commit(key, digest, document["id"], document["course_id"], len(sections), len(sections))
                        with self.fake._lock:
                            self.fake.commits[key] = existing
                            self.fake.generations[document["id"]] = existing
                            self.fake.commit_count += 1
        if existing is None:
            self._error(409, refused, False)
            return
        if fault is not None and fault.kind == "hold_after_commit":
            self.fake.gate.clear()
            self.fake.blocked.set()
            self.fake.gate.wait(600)
        if fault is not None and fault.kind == "lose_after_commit":
            self._drop()
            return
        self._send(
            200,
            {
                "protocol": PROTOCOL,
                "status": "indexed",
                "document_id": existing.document_id,
                "course_id": existing.course_id,
                "content_digest": existing.digest,
                "idempotency_key": existing.key,
                "section_count": existing.section_count,
                "chunk_count": existing.chunk_count,
                "replayed": replayed,
            },
        )

    def _drop(self) -> None:
        """Close the connection without any response (a lost response, from the client's view)."""
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
