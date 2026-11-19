"""Run the PDF extractor in a resource-limited child process and validate what it returns.

Lifecycle of the child (one per attempt):

- Started with `python -I extract_child.py`, an empty environment (no
  database or broker settings, no credentials in its arguments either), the
  root directory as working directory, stdin from /dev/null, and only the PDF
  descriptor inherited (close_fds plus pass_fds).
- In its own session/process group, so that it can be killed as a group.
- Bound to its parent: before exec it sets PR_SET_PDEATHSIG = SIGKILL and then
  checks that its parent is still the process that started it (closing the
  race where the parent dies first). If the Celery pool process dies (hard
  time limit, SIGKILL, OOM kill), the kernel kills the extractor. The return
  value of prctl is checked: if the signal cannot be set (or prctl is not
  available, i.e. not Linux) no extractor is run, and the attempt fails as
  transient (`extractor_unprotected`). The signal follows the parent
  *thread*, which is the pool process's main thread where tasks run.
  Independently, RLIMIT_CPU ends a runaway extractor after timeout + 5 s of CPU.
- Its output is read incrementally with a deadline and a byte cap on stdout
  (sized for the worst case of the text limit: 6 bytes per character for
  JSON escapes, plus per-page overhead) and on stderr (kept for the log).
  Exceeding either cap or the deadline kills it.
- On every exit path (success, failure, timeout, oversized output, an
  exception or a Celery soft time limit while waiting) the process group is
  killed if still running and the child is reaped, so neither orphans nor
  zombies are left.

What this is and is not: a resource boundary (memory, CPU, wall time, output
size, descriptors, environment) around a parser that handles untrusted
input. It is not a security sandbox: the child runs as the same user with the
same filesystem view and network access, without seccomp or namespaces. A
parser vulnerability that leads to code execution would not be contained by
these limits.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

EXTRACTOR_SCRIPT = Path(__file__).with_name("extract_child.py")
# Fixed, not configurable: tests that need a fake extractor replace this
# constant in their own worker process (tests/worker_app.py), never through
# settings or environment variables.
EXTRACTOR_COMMAND: tuple[str, ...] = (sys.executable, "-I", str(EXTRACTOR_SCRIPT))

EXIT_OK = 0
EXIT_CLASSIFIED = 2
EXIT_PARENT_GONE = 70
EXIT_NO_PDEATHSIG = 71
STDERR_CAP_BYTES = 64 * 1024
_PAGE_OVERHEAD_BYTES = 64
_ENVELOPE_BYTES = 64 * 1024
_REAP_SECONDS = 10.0

PERMANENT_CODES = frozenset(
    {"encrypted_pdf", "invalid_pdf", "too_many_pages", "too_much_text", "processing_limit"}
)

log = logging.getLogger(__name__)

_PR_SET_PDEATHSIG = 1
try:
    _prctl = ctypes.CDLL(None, use_errno=True).prctl if sys.platform.startswith("linux") else None
except (OSError, AttributeError):  # pragma: no cover - libc without prctl
    _prctl = None


@dataclass(frozen=True)
class ExtractionLimits:
    max_pages: int
    max_text_chars: int
    timeout_seconds: float
    memory_bytes: int

    @property
    def cpu_seconds(self) -> int:
        return int(self.timeout_seconds) + 5

    @property
    def stdout_cap_bytes(self) -> int:
        return 6 * self.max_text_chars + _PAGE_OVERHEAD_BYTES * self.max_pages + _ENVELOPE_BYTES


@dataclass(frozen=True)
class Extraction:
    page_count: int
    pages: tuple[tuple[int, str], ...]  # (1-based page number, raw text), increasing


class ExtractionFailed(Exception):
    """The extractor did not produce a usable result.

    permanent: the document itself is the problem (retrying cannot help).
    Otherwise the failure is attributed to the environment and retried.
    """

    def __init__(self, code: str, detail: str, *, permanent: bool) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.permanent = permanent


def _child_setup(parent_pid: int):
    prctl = _prctl

    def setup() -> None:  # runs in the child between fork and exec
        # Never exec the extractor without the parent-death signal in place.
        if prctl is None or prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
            os._exit(EXIT_NO_PDEATHSIG)
        if os.getppid() != parent_pid:
            os._exit(EXIT_PARENT_GONE)

    return setup


def _kill_and_reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILLed process stuck in the kernel
        log.error("extractor pid=%s did not exit after SIGKILL", proc.pid)


class _Overflow(Exception):
    pass


class _Deadline(Exception):
    pass


def _read_bounded(proc: subprocess.Popen, deadline: float, stdout_cap: int) -> tuple[bytes, bytes]:
    out = bytearray()
    err = bytearray()
    selector = selectors.DefaultSelector()
    try:
        for stream in (proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Deadline()
            for key, _ in selector.select(min(remaining, 0.5)):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is proc.stdout:
                    if len(out) + len(chunk) > stdout_cap:
                        raise _Overflow()
                    out += chunk
                elif len(err) < STDERR_CAP_BYTES:
                    err += chunk[: STDERR_CAP_BYTES - len(err)]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Deadline()
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise _Deadline() from None
    finally:
        selector.close()
    return bytes(out), bytes(err)


def _invalid_output(detail: str) -> ExtractionFailed:
    return ExtractionFailed("extractor_output_invalid", detail, permanent=False)


def parse_output(raw: bytes, limits: ExtractionLimits) -> Extraction:
    """Validate the success document strictly; anything unexpected is invalid output."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _invalid_output("not UTF-8 JSON") from None
    if not isinstance(body, dict) or set(body) != {"status", "page_count", "pages"} or body["status"] != "ok":
        raise _invalid_output("unexpected document shape")
    page_count = body["page_count"]
    if type(page_count) is not int or not 1 <= page_count <= limits.max_pages:
        raise _invalid_output("page_count out of range")
    pages = body["pages"]
    if not isinstance(pages, list) or len(pages) > page_count:
        raise _invalid_output("pages is not a list of at most page_count items")
    result: list[tuple[int, str]] = []
    previous = 0
    total = 0
    for item in pages:
        if not isinstance(item, dict) or set(item) != {"page", "text"}:
            raise _invalid_output("unexpected page shape")
        page, text = item["page"], item["text"]
        if type(page) is not int or not previous < page <= page_count or not isinstance(text, str):
            raise _invalid_output("page numbers must increase within the document")
        total += len(text)
        if total > limits.max_text_chars:
            raise _invalid_output("more text than the limit")
        result.append((page, text))
        previous = page
    return Extraction(page_count=page_count, pages=tuple(result))


def _classified_error(raw: bytes) -> ExtractionFailed:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _invalid_output("error document is not UTF-8 JSON")
    if (
        not isinstance(body, dict)
        or set(body) != {"status", "code", "detail"}
        or body["status"] != "error"
        or body["code"] not in PERMANENT_CODES
        or not isinstance(body["detail"], str)
    ):
        return _invalid_output("unexpected error document")
    return ExtractionFailed(body["code"], body["detail"][:300], permanent=True)


def extract_pdf(fd: int, limits: ExtractionLimits, *, command: Sequence[str] | None = None) -> Extraction:
    """Extract page texts from the open PDF `fd`; raises ExtractionFailed."""
    if _prctl is None:
        # No parent-death signal on this platform (or libc): do not start an
        # extractor that could outlive a killed worker. Retried, then failed.
        raise ExtractionFailed("extractor_unprotected", "PR_SET_PDEATHSIG is not available", permanent=False)
    argv = [
        *(command or EXTRACTOR_COMMAND),
        "--fd", str(fd),
        "--max-pages", str(limits.max_pages),
        "--max-chars", str(limits.max_text_chars),
        "--cpu-seconds", str(limits.cpu_seconds),
        "--memory-bytes", str(limits.memory_bytes),
    ]  # fmt: skip
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(fd,),
        close_fds=True,
        env={},
        cwd="/",
        start_new_session=True,
        preexec_fn=_child_setup(os.getpid()),
    )
    try:
        try:
            out, err = _read_bounded(proc, started + limits.timeout_seconds, limits.stdout_cap_bytes)
        except _Deadline:
            raise ExtractionFailed("processing_limit", "extraction timed out", permanent=True) from None
        except _Overflow:
            raise ExtractionFailed("processing_limit", "extractor output too large", permanent=True) from None
    finally:
        _kill_and_reap(proc)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    code = proc.returncode
    if code == EXIT_OK:
        return parse_output(out, limits)
    if code == EXIT_CLASSIFIED:
        raise _classified_error(out)
    if code == -signal.SIGXCPU:
        raise ExtractionFailed("processing_limit", "CPU time limit reached", permanent=True)
    if code == EXIT_NO_PDEATHSIG:
        raise ExtractionFailed("extractor_unprotected", "PR_SET_PDEATHSIG could not be set", permanent=False)
    tail = err[-500:].decode("utf-8", "replace").strip()
    log.warning("extractor exited abnormally code=%s stderr=%r", code, tail)
    raise ExtractionFailed("extractor_crashed", f"exit status {code}", permanent=False)
