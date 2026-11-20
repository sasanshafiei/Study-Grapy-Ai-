"""An isolated raw-socket fake of the *proposed* studygraph.chat/1 endpoints, for tests only.

TEST DOUBLE. Not the AI service, not shipped, and not evidence that retrieval
or generation works: it retrieves nothing and generates nothing. It answers
what each test scripts, so core-api's client, validation, verification and
streaming can be exercised over a real TCP connection (real HTTP framing,
which an in-process transport would not test).

- GET /ai/v1/chat/capabilities and POST /ai/v1/chat/answer, bearer auth;
- `capabilities` / `answer` (a function of the request JSON) script normal
  replies; `raw` queues literal response bytes for the next requests;
- `hold` makes the next requests wait until `release()` (or the client goes);
- `requests` records every request; `closed` counts connections the client
  closed (resource-cleanup checks).
Listens on 127.0.0.1, ephemeral port, daemon threads.
"""

from __future__ import annotations

import json
import socket
import socketserver
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

PROTOCOL = "studygraph.chat/1"
TOKEN = "chat-test-token-" + "x" * 32


def http_response(status: int, body: bytes, content_type: str = "application/json", extra: str = "") -> bytes:
    reason = {200: "OK", 302: "Found", 401: "Unauthorized", 500: "Internal Server Error", 503: "Unavailable"}
    head = (
        f"HTTP/1.1 {status} {reason.get(status, 'Status')}\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n{extra}\r\n"
    )
    return head.encode("latin-1") + body


def json_response(status: int, payload) -> bytes:
    return http_response(status, json.dumps(payload).encode())


def default_capabilities() -> dict:
    return {
        "protocol": PROTOCOL,
        "service": "ai-service",
        "mock": False,
        "scopes": ["class_only", "class_plus_external"],
        "max_question_chars": 4000,
    }


def answer_for(request: dict, *, grounded=True, answer="", citations=(), topic_ids=(), **overrides) -> dict:
    body = {
        "protocol": request["protocol"],
        "request_id": request["request_id"],
        "course_id": request["course_id"],
        "scope": request["scope"],
        "mock": False,
        "grounded": grounded,
        "answer": answer,
        "citations": list(citations),
        "topic_ids": list(topic_ids),
        "used_web": False,
    }
    body.update(overrides)
    return body


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


@dataclass
class FakeChatService:
    token: str = TOKEN
    capabilities: dict = field(default_factory=default_capabilities)
    answer: Callable[[dict], dict] | None = None
    raw: list[bytes] = field(default_factory=list)
    requests: list[Recorded] = field(default_factory=list)
    closed: int = 0
    on_answer: Callable[[dict], None] | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._gate = threading.Event()
        self._gate.set()
        self.waiting = threading.Event()
        self._server: socketserver.ThreadingTCPServer | None = None

    # control

    def start(self) -> "FakeChatService":
        service = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                service._handle(self.request)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self._gate.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def paths(self) -> list[str]:
        return [r.path for r in self.requests]

    # serving

    def _read_request(self, conn: socket.socket) -> Recorded | None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        while len(rest) < length:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            rest += chunk
        return Recorded(method, path, headers, rest[:length])

    def _reply(self, request: Recorded) -> bytes:
        with self._lock:
            if self.raw:
                return self.raw.pop(0)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return json_response(401, {"error": {"code": "unauthorized", "message": "no", "retryable": False}})
        if request.method == "GET" and request.path == "/ai/v1/chat/capabilities":
            return json_response(200, self.capabilities)
        if request.method == "POST" and request.path == "/ai/v1/chat/answer":
            payload = request.json()
            if self.on_answer is not None:
                self.on_answer(payload)
            if self.answer is None:
                return json_response(503, {"error": {"code": "unavailable", "message": "no", "retryable": True}})
            return json_response(200, self.answer(payload))
        return json_response(404, {"error": {"code": "not_found", "message": "no", "retryable": False}})

    def _handle(self, conn: socket.socket) -> None:
        try:
            request = self._read_request(conn)
            if request is None:
                return
            with self._lock:
                self.requests.append(request)
            if not self._gate.is_set():
                self.waiting.set()
                while not self._gate.wait(0.05):
                    conn.setblocking(False)
                    try:
                        if conn.recv(1, socket.MSG_PEEK) == b"":
                            return  # the client closed the connection while we held it
                    except BlockingIOError:
                        pass
                    finally:
                        conn.setblocking(True)
            conn.sendall(self._reply(request))
            conn.shutdown(socket.SHUT_WR)
            while conn.recv(65536):
                pass
        except OSError:
            pass
        finally:
            with self._lock:
                self.closed += 1
            conn.close()
