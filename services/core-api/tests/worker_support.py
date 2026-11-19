"""Helpers for the M3 processing tests: stored documents, waiting, and real Celery processes.

Real worker and Beat processes are started with `python -m celery -A
tests.worker_app ...` inside the test container, with an environment built
here from scratch (nothing inherited but PATH): the verified disposable
database and Redis, a temporary upload store, and broker queues whose names
are unique to the test. Their database connections are checked against the
disposable database's COMMENT marker by the worker's own start-up check
(DATABASE_REQUIRE_COMMENT), so a worker started here cannot reach any other
database. Processes run in their own session and are stopped (SIGTERM, then
SIGKILL) when the test ends.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.auth import CurrentUser
from app.config import Settings
from app.storage import LocalFileStorage, storage_key

CORE_API_DIR = Path(__file__).resolve().parents[1]


def wait_for(predicate: Callable[[], object], timeout: float = 30.0, interval: float = 0.1, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


def store_file(storage: LocalFileStorage, course_id: uuid.UUID, document_id: uuid.UUID, data: bytes) -> str:
    """Place a file in the store exactly as an upload does; returns its storage key."""
    key = storage_key(course_id, document_id)
    with storage.stage() as staged:
        staged.write(data)
        storage.place(staged, key)
        staged.release()
    return key


@dataclass
class StoredDocument:
    document_id: uuid.UUID
    job_id: uuid.UUID
    key: str


def add_stored_document(
    engine: Engine,
    world,
    storage: LocalFileStorage,
    course_id: uuid.UUID,
    owner: CurrentUser,
    data: bytes,
    *,
    title: str = "Lecture.pdf",
    with_file: bool = True,
    max_attempts: int | None = None,
) -> StoredDocument:
    """A document with a stored file and a queued parse job, as an upload leaves them."""
    document_id = uuid.uuid4()
    key = store_file(storage, course_id, document_id, data) if with_file else storage_key(course_id, document_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO documents (id, course_id, origin, title, language, content_hash, status, uploaded_by, "
                "original_filename, storage_key, mime_type, size_bytes, kind) VALUES (:id, :course, 'class', :title, "
                "'en', :sha, 'pending', :owner, :title, :key, 'application/pdf', :size, 'notes')"
            ),
            {
                "id": document_id,
                "course": course_id,
                "title": title,
                "sha": hashlib.sha256(data).hexdigest(),
                "owner": owner.id,
                "key": key,
                "size": len(data),
            },
        )
        job_id = connection.execute(
            text(
                "INSERT INTO ingestion_jobs (document_id, kind, max_attempts) "
                "VALUES (:id, 'parse', coalesce(:max, 3)) RETURNING id"
            ),
            {"id": document_id, "max": max_attempts},
        ).scalar_one()
    world.documents.append(document_id)
    return StoredDocument(document_id, job_id, key)


def job_row(engine: Engine, job_id: uuid.UUID):
    with engine.connect() as connection:
        return connection.execute(text("SELECT * FROM ingestion_jobs WHERE id = :id"), {"id": job_id}).mappings().one()


def document_row(engine: Engine, document_id: uuid.UUID):
    with engine.connect() as connection:
        return connection.execute(text("SELECT * FROM documents WHERE id = :id"), {"id": document_id}).mappings().one()


def sections_of(engine: Engine, document_id: uuid.UUID) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id, ordinal, text, locator, created_at FROM document_sections "
                    "WHERE document_id = :id ORDER BY ordinal"
                ),
                {"id": document_id},
            ).mappings()
        ]


def index_jobs(engine: Engine, document_id: uuid.UUID) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM ingestion_jobs WHERE document_id = :id AND kind = 'index'"), {"id": document_id}
            ).mappings()
        ]


# --- real Celery processes ---------------------------------------------------------------------


@dataclass
class Infra:
    """Everything a worker process of one test needs; queues are unique to the test."""

    database_url: str
    database_marker: str
    redis_url: str
    upload_dir: Path
    work_dir: Path
    parse_queue: str = field(default_factory=lambda: f"test.{uuid.uuid4().hex}.parse")
    maintenance_queue: str = field(default_factory=lambda: f"test.{uuid.uuid4().hex}.maintenance")

    def settings(self, **overrides) -> Settings:
        values = dict(
            database_url=self.database_url,
            redis_url=self.redis_url,
            upload_dir=self.upload_dir,
            parse_queue=self.parse_queue,
            maintenance_queue=self.maintenance_queue,
            broker_connect_timeout_seconds=2,
            broker_socket_timeout_seconds=3,
        )
        values.update(overrides)
        return Settings(**values)

    def environment(self, **overrides) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.work_dir),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENVIRONMENT": "test",
            "DATABASE_URL": self.database_url,
            "DATABASE_REQUIRE_COMMENT": self.database_marker,
            "REDIS_URL": self.redis_url,
            "UPLOAD_DIR": str(self.upload_dir),
            "PARSE_QUEUE": self.parse_queue,
            "MAINTENANCE_QUEUE": self.maintenance_queue,
            "RETRY_BACKOFF_BASE_SECONDS": "0",
            "RETRY_BACKOFF_MAX_SECONDS": "0",
            "PUBLISH_RETRY_DELAY_SECONDS": "0",
            "BROKER_CONNECT_TIMEOUT_SECONDS": "2",
            "BROKER_SOCKET_TIMEOUT_SECONDS": "3",
        }
        env.update({key.upper(): str(value) for key, value in overrides.items()})
        return {key: value for key, value in env.items() if value != ""}


class CeleryProcess:
    def __init__(self, name: str, argv: list[str], env: dict[str, str], work_dir: Path) -> None:
        self.name = name
        self.work_dir = work_dir
        self.ready_file = work_dir / f"{name}.ready"
        self.signal_dir = work_dir / f"{name}.signals"
        self.signal_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = work_dir / f"{name}.log"
        env = {**env, "SG_TEST_READY_FILE": str(self.ready_file), "SG_TEST_SIGNAL_DIR": str(self.signal_dir)}
        self._log = open(self.log_path, "wb")  # noqa: SIM115 - closed in stop()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "celery", "-A", "tests.worker_app", *argv],
            cwd=CORE_API_DIR,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def log(self) -> str:
        self._log.flush()
        return self.log_path.read_text(errors="replace")

    def wait_ready(self, timeout: float = 60) -> "CeleryProcess":
        def ready():
            if self.proc.poll() is not None:
                raise AssertionError(f"{self.name} exited with {self.proc.returncode}:\n{self.log()[-4000:]}")
            return self.ready_file.exists()

        wait_for(ready, timeout, what=f"{self.name} to be ready")
        return self

    def extractor_pids(self) -> list[tuple[int, int]]:
        """(pid, parent pid) of every fake extractor this worker started."""
        result = []
        for path in sorted(self.signal_dir.glob("extractor-*.pid")):
            pid, ppid = path.read_text().split()
            result.append((int(pid), int(ppid)))
        return result

    def release_extractors(self) -> None:
        (self.signal_dir / "release").write_text("go")

    def signal(self, signum: int) -> None:
        try:
            os.killpg(self.proc.pid, signum)
        except ProcessLookupError:
            pass

    def stop(self, timeout: float = 60) -> int:
        if self.proc.poll() is None:
            self.signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.signal(signal.SIGKILL)
                self.proc.wait(10)
        self.signal(signal.SIGKILL)  # anything left in the group
        self._log.close()
        return self.proc.returncode


def start_worker(
    infra: Infra, name: str, queues: list[str], *, concurrency: int = 1, fake: str | None = None, **overrides
) -> CeleryProcess:
    env = infra.environment(**overrides)
    if fake:
        env["SG_TEST_FAKE_EXTRACTOR"] = fake
    argv = [
        "worker", "-Q", ",".join(queues), "-c", str(concurrency), "-P", "prefork", "-n", f"{name}@%h",
        "--without-mingle", "--without-gossip", "--without-heartbeat", "-l", "INFO",
    ]  # fmt: skip
    return CeleryProcess(name, argv, env, infra.work_dir)


def start_beat(infra: Infra, name: str = "beat", **overrides) -> CeleryProcess:
    env = infra.environment(**overrides)
    argv = ["beat", "-s", str(infra.work_dir / f"{name}-schedule"), "--pidfile=", "-l", "INFO", "--max-interval", "1"]
    return CeleryProcess(name, argv, env, infra.work_dir)


def pid_alive(pid: int) -> bool:
    """True if `pid` exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"
