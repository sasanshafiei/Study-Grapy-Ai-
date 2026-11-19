"""Helpers for the API tests (the fixtures that use them live in conftest.py).

- World creates users, courses, enrollments and documents on the disposable
  database and deletes exactly what it created afterwards, so every API test
  leaves the database as it found it (the M1 runner downgrades to base after
  pytest, which migrations 0002 and 0003 refuse while upload, section, job
  or storage-identity data exists). Deleting documents also removes their
  jobs and sections (ON DELETE CASCADE); the storage identity row is removed
  as well.
- ApiHarness builds a separate application per test with create_app(): its
  own session factory (bound to the verified disposable database), its own
  temporary upload directory, bound to the database's storage identity (M3:
  the API refuses uploads into a store the database does not know), a
  recording publisher instead of a broker, and a principal injected through
  dependency_overrides on that instance only. The runtime `app.main.app` is
  never modified.
- asgi_call() drives the application with hand-made ASGI messages, for cases
  a normal HTTP client cannot produce (invalid Content-Length, a client that
  disconnects mid-upload) and to observe whether the body was read at all.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import storage_identity
from app.auth import CurrentUser, get_current_user, not_authenticated
from app.config import Settings
from app.ingestion.dispatch import PublishError
from app.main import create_app
from app.storage import INCOMING_DIR, LocalFileStorage
from tests.pdf_samples import make_pdf

_UNSET = object()


class RecordingPublisher:
    """Stands in for the broker in API tests: records what would have been published."""

    def __init__(self, *, fail: bool = False, depth: int = 0) -> None:
        self.published: list[uuid.UUID] = []
        self.fail = fail
        self.depth = depth

    def publish_parse(self, job_id: uuid.UUID) -> None:
        if self.fail:
            raise PublishError("simulated broker outage")
        self.published.append(job_id)

    def parse_queue_depth(self) -> int:
        if self.fail:
            raise PublishError("simulated broker outage")
        return self.depth


def bind_storage(engine: Engine, root: Path) -> uuid.UUID:
    """Make `root` carry the disposable database's storage identity (creating one if needed).

    Tests use many temporary directories against one database; each gets a
    marker with the same id, as if it were the same store.
    """
    root.mkdir(parents=True, exist_ok=True)
    with engine.begin() as connection:
        storage_id = connection.execute(
            text(
                "INSERT INTO storage_identity (id, storage_id) VALUES (1, gen_random_uuid()) "
                "ON CONFLICT (id) DO UPDATE SET storage_id = storage_identity.storage_id RETURNING storage_id"
            )
        ).scalar_one()
    if storage_identity.read_marker(LocalFileStorage(root)) is None:
        storage_identity.write_marker(LocalFileStorage(root), storage_id)
    return storage_id

# The keys of DocumentItem in apps/web/src/lib/api/types.ts (CE-2), in snake_case.
DOCUMENT_ITEM_KEYS = {
    "id", "title", "origin", "kind", "status", "pages", "source_name",
    "url", "licence", "added_by", "added_at", "error",
}  # fmt: skip


@dataclass
class World:
    engine: Engine
    users: list[uuid.UUID] = field(default_factory=list)
    courses: list[uuid.UUID] = field(default_factory=list)
    documents: list[uuid.UUID] = field(default_factory=list)

    def _one(self, sql: str, **params):
        with self.engine.begin() as connection:
            return connection.execute(text(sql), params).scalar_one()

    def execute(self, sql: str, **params) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(sql), params)

    def scalar(self, sql: str, **params):
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar()

    def rows(self, sql: str, **params) -> list:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).mappings().all()

    def user(self, role: str = "professor", status: str = "active", name: str | None = None) -> CurrentUser:
        user_id = self._one(
            "INSERT INTO users (email, display_name, hashed_password, role, status) "
            "VALUES (:email, :name, 'not-a-real-hash', :role, :status) RETURNING id",
            email=f"{uuid.uuid4().hex}@example.test",
            name=name or f"Test {role}",
            role=role,
            status=status,
        )
        self.users.append(user_id)
        return CurrentUser(id=user_id, role=role, status=status)

    def course(self, owner: CurrentUser, name: str = "General Chemistry") -> uuid.UUID:
        course_id = self._one(
            "INSERT INTO courses (owner_id, name, join_code) VALUES (:owner, :name, :code) RETURNING id",
            owner=owner.id,
            name=name,
            code=uuid.uuid4().hex[:12],
        )
        self.courses.append(course_id)
        return course_id

    def enroll(self, user: CurrentUser, course_id: uuid.UUID, role: str = "student") -> None:
        self.execute(
            "INSERT INTO enrollments (user_id, course_id, role) VALUES (:u, :c, :role)",
            u=user.id,
            c=course_id,
            role=role,
        )

    def document(self, course_id: uuid.UUID | None, **columns) -> uuid.UUID:
        """A document row inserted directly (legacy, external or a state set by hand)."""
        values = {"origin": "class", "title": "Legacy notes.pdf", "language": "en", "status": "pending", **columns}
        names = ", ".join(["course_id", *values])
        placeholders = ", ".join([":course_id", *(f":{name}" for name in values)])
        document_id = self._one(
            f"INSERT INTO documents ({names}) VALUES ({placeholders}) RETURNING id", course_id=course_id, **values
        )
        self.documents.append(document_id)
        return document_id

    def cleanup(self) -> None:
        with self.engine.begin() as connection:
            if self.courses:
                connection.execute(text("DELETE FROM documents WHERE course_id = ANY(:ids)"), {"ids": self.courses})
            if self.documents:
                connection.execute(text("DELETE FROM documents WHERE id = ANY(:ids)"), {"ids": self.documents})
            if self.courses:
                connection.execute(text("DELETE FROM enrollments WHERE course_id = ANY(:ids)"), {"ids": self.courses})
                connection.execute(text("DELETE FROM courses WHERE id = ANY(:ids)"), {"ids": self.courses})
            if self.users:
                connection.execute(text("DELETE FROM enrollments WHERE user_id = ANY(:ids)"), {"ids": self.users})
                connection.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": self.users})
            # The storage identity is per test (each test has its own directories).
            connection.execute(text("DELETE FROM storage_identity"))
            # Nothing from M2/M3 may be left behind (it would block `alembic downgrade`).
            leftover_jobs = connection.execute(text("SELECT count(*) FROM ingestion_jobs")).scalar_one()
            leftover_uploads = connection.execute(
                text("SELECT count(*) FROM documents WHERE storage_key IS NOT NULL")
            ).scalar_one()
            leftover_sections = connection.execute(text("SELECT count(*) FROM document_sections")).scalar_one()
        assert (leftover_jobs, leftover_uploads, leftover_sections) == (0, 0, 0), "a test left upload data behind"


class ApiHarness:
    def __init__(
        self,
        world: World,
        tmp_path: Path,
        *,
        max_upload_bytes: int = 25 * 1024 * 1024,
        publisher=_UNSET,
        bind: bool = True,
    ) -> None:
        self.world = world
        self.session_factory: sessionmaker[Session] = sessionmaker(bind=world.engine, autoflush=False)
        self.upload_root = tmp_path / "uploads"
        self.settings = Settings(upload_dir=self.upload_root, max_upload_bytes=max_upload_bytes)
        self.storage = LocalFileStorage(self.upload_root)
        if bind:
            bind_storage(world.engine, self.upload_root)
        self.publisher = RecordingPublisher() if publisher is _UNSET else publisher
        self.app = create_app(
            self.settings, session_factory=self.session_factory, storage=self.storage, publisher=self.publisher
        )
        self.principal: CurrentUser | None = None
        self.app.dependency_overrides[get_current_user] = self._current_user
        self.client = TestClient(self.app)

    def _current_user(self) -> CurrentUser:
        if self.principal is None:
            raise not_authenticated()
        return self.principal

    def as_user(self, user: CurrentUser | None) -> "ApiHarness":
        self.principal = user
        return self

    @staticmethod
    def path(course_id: uuid.UUID | str) -> str:
        return f"/api/v1/courses/{course_id}/documents"

    def upload(
        self,
        course_id: uuid.UUID,
        content: bytes | None = None,
        filename: str = "Lecture 01.pdf",
        content_type: str | None = "application/pdf",
    ):
        content = make_pdf() if content is None else content
        part = (filename, content, content_type) if content_type is not None else (filename, content)
        return self.client.post(self.path(course_id), files={"file": part})

    def list(self, course_id: uuid.UUID):
        return self.client.get(self.path(course_id))

    def stored_files(self) -> list[Path]:
        """Stored originals: files below the root, except .incoming and the root's own
        housekeeping files (the M3 identity marker and .incoming.lock)."""
        if not self.upload_root.exists():
            return []
        return sorted(
            p
            for p in self.upload_root.rglob("*")
            if p.is_file() and INCOMING_DIR not in p.parts and p.parent != self.upload_root
        )

    def incoming_files(self) -> list[Path]:
        incoming = self.upload_root / INCOMING_DIR
        return sorted(incoming.iterdir()) if incoming.exists() else []


def multipart_body(
    parts: Iterable[tuple[str, str | None, bytes, str | None]], boundary: str = "sgboundary"
) -> tuple[bytes, str]:
    """Encode (field, filename, data, content type) parts; returns (body, content-type header)."""
    out = bytearray()
    for name, filename, data, content_type in parts:
        out += f"--{boundary}\r\n".encode()
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += f"Content-Disposition: {disposition}\r\n".encode()
        if content_type:
            out += f"Content-Type: {content_type}\r\n".encode()
        out += b"\r\n" + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


@dataclass
class AsgiResult:
    status: int
    body: bytes
    receive_calls: int

    def json(self):
        return json.loads(self.body)


def asgi_call(
    app,
    method: str,
    path: str,
    headers: dict[str, str],
    chunks: list[bytes],
    *,
    disconnect_after_chunks: bool = False,
    on_receive: Callable[[], None] | None = None,
) -> AsgiResult:
    """Call the ASGI app directly. The body is sent as `chunks`; optionally the
    client then disconnects instead of finishing the request."""
    messages: list[dict] = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        messages.append({"type": "http.request", "body": chunk, "more_body": disconnect_after_chunks or not last})
    if not chunks:
        messages.append({"type": "http.request", "body": b"", "more_body": False})
    if disconnect_after_chunks:
        messages.append({"type": "http.disconnect"})
    sent: list[dict] = []
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if on_receive is not None:
            on_receive()
        if messages:
            return messages.pop(0)
        await asyncio.sleep(3600)  # nothing more to send; the app must not wait for it

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        # Raw header bytes, as a server would pass them on (non-ASCII values arrive as UTF-8 bytes).
        "headers": [(k.lower().encode("latin-1"), v.encode("utf-8")) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=60))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return AsgiResult(status=start["status"], body=body, receive_calls=calls)
