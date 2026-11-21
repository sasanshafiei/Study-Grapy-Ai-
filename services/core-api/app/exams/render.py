"""Run the renderer child (render_child.py) as a short-lived, resource-limited subprocess.

What this provides (architecture §9.5): `python -I` (interpreter isolation: no environment variables, no
user site-packages, neither the script directory nor the working directory on sys.path), an empty
environment except PYTHONHASHSEED=0, no inherited descriptors (close_fds), a fresh empty working directory,
its own session and process group (killed as a group), the child's own RLIMIT_AS / RLIMIT_CPU /
RLIMIT_FSIZE 0 / RLIMIT_NOFILE / no core files, a bounded input (<= 2 MiB), a bounded stdout (PDF 16 MiB /
DOCX 8 MiB), a wall-clock deadline (30 s) after which it is killed and reaped.

Launch safety: render() runs in the API's thread pool, so the launch uses NO Python `preexec_fn` (which may
deadlock between fork and exec in a multithreaded process; the wall-clock deadline would not bound it,
because Popen has not returned yet). Everything between fork and exec is done by subprocess's C code:
close_fds, cwd, the empty environment and setsid (start_new_session). The Linux parent-death signal is
armed by the child itself as its first action after exec, followed by a check of its actual parent PID
(passed as --parent-pid), see render_child.py.

What it is NOT: an OS-level sandbox. The child runs as the same OS user, in the same container, with the same
filesystem view and network namespace as core-api; it could technically read what core-api can read and
open sockets. The controls that address the residual risk (a defect in ReportLab, python-docx, lxml or
Pillow) are the validated, parent-generated input, the escaping in the child, fixed vendored fonts, the
limits above and the output verification in the tests. Stronger isolation (separate UID, network-less
render container) is a deployment option owned by CE-3 (decision D-30).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("app.exams.render")

CHILD = Path(__file__).with_name("render_child.py")
FONTS = Path(__file__).with_name("fonts")
FONT_FILES = {"regular": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf", "oblique": "DejaVuSans-Oblique.ttf"}
# Pinned with the renderer version: a change of any of these is a new renderer (bytes may change, content never).
FONT_SHA256 = {
    "DejaVuSans.ttf": "57f73e11f51999432bf7ab22ce55b6f945d5eca1bf824404cfa9ec2e3718c84e",
    "DejaVuSans-Bold.ttf": "a4c5bc453ca281d90ea079e596da7ae0dfeb5777497c29ec254e76d97ff6f890",
    "DejaVuSans-Oblique.ttf": "e2f09289f4276309a36b9a93e5a0ac64957ef3eb7158151b243d41f667151ee4",
}
LIBRARY_PINS = {"reportlab": "5.1.0", "python-docx": "1.2.0", "lxml": "6.1.3", "pillow": "13.0.0"}
RENDERER_VERSION = "exam-render/1"
EXIT_OK, EXIT_CLASSIFIED = 0, 2
_STDERR_CAP = 256 * 1024
_REAP_SECONDS = 5
# Exit codes of render_child.py before any rendering (see its docstring).
_EXIT_USAGE = 64
_EXIT_NO_PDEATHSIG = 121
_EXIT_PARENT_GONE = 122


class RenderFailed(Exception):
    """kind: timeout | failed (layout, memory, too large, crash, invalid output)."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True)
class RenderLimits:
    timeout_seconds: float
    cpu_seconds: int
    memory_bytes: int
    max_input_bytes: int
    max_output_bytes: int

    @classmethod
    def from_settings(cls, settings, fmt: str) -> "RenderLimits":
        cap = settings.exam_export_max_pdf_bytes if fmt == "pdf" else settings.exam_export_max_docx_bytes
        return cls(settings.exam_export_timeout_seconds, settings.exam_export_cpu_seconds,
                   settings.exam_export_memory_mb * 1024 * 1024, settings.exam_export_max_input_bytes, cap)  # fmt: skip


@dataclass(frozen=True)
class Rendered:
    data: bytes
    sha256: str
    wall_seconds: float
    cpu_seconds: float
    max_rss_kib: int  # the child's own peak resident set after exec (VmHWM)
    audit_events: tuple = ()


def font_paths() -> dict[str, str]:
    return {k: str(FONTS / v) for k, v in FONT_FILES.items()}


def _spawn(argv: list[str], workdir: str) -> subprocess.Popen:
    """Start the child. No preexec_fn: only subprocess's C code runs between fork and exec."""
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        env={"PYTHONHASHSEED": "0"}, cwd=workdir, start_new_session=True,
    )


def child_command() -> list[str]:
    return [sys.executable, "-I", str(CHILD), "--parent-pid", str(os.getpid())]


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group, but only while the leader is NOT yet reaped by us.

    The child leads its own group (start_new_session), so the group ID is the child's PID. As long as the leader
    is unreaped (running or a zombie) its PID, and therefore the group ID, cannot be reused by an unrelated
    process, so signalling the group is safe even when the leader itself has already exited (H2: a descendant
    that kept the pipes open). After the leader is reaped the group ID is never signalled again.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        proc.kill()


def _kill_and_reap(proc: subprocess.Popen) -> None:
    """Kill the group unconditionally (not only when the leader still runs), then reap the leader."""
    _kill_group(proc)
    if proc.returncode is not None:
        return
    try:
        proc.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover
        log.error("renderer pid=%s did not exit after SIGKILL", proc.pid)


class _Overflow(Exception):
    pass


class _Deadline(Exception):
    pass


def _exchange(proc: subprocess.Popen, payload: bytes, deadline: float, cap: int) -> tuple[bytes, bytes]:
    out, err = bytearray(), bytearray()
    selector = selectors.DefaultSelector()
    view = memoryview(payload)
    try:
        for stream, events in ((proc.stdin, selectors.EVENT_WRITE), (proc.stdout, selectors.EVENT_READ),
                               (proc.stderr, selectors.EVENT_READ)):  # fmt: skip
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events)
        if not view:
            selector.unregister(proc.stdin)
            proc.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Deadline()
            for key, _ in selector.select(min(remaining, 0.5)):
                if key.fileobj is proc.stdin:
                    try:
                        written = os.write(key.fd, view[:65536])
                    except BrokenPipeError:
                        written, view = 0, view[:0]
                    except BlockingIOError:
                        continue
                    view = view[written:]
                    if not view:
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is proc.stdout:
                    if len(out) + len(chunk) > cap:
                        raise _Overflow()
                    out += chunk
                elif len(err) < _STDERR_CAP:
                    err += chunk[: _STDERR_CAP - len(err)]
    finally:
        selector.close()
    return bytes(out), bytes(err)


def _wait(proc: subprocess.Popen, deadline: float):
    """Wait for the child's exit WITHOUT reaping it (waitid WNOWAIT), kill whatever is left of its process group
    while the unreaped leader still pins the group ID, then reap it with wait4 (its own CPU time and peak RSS)."""
    while True:
        if _WNOWAIT_AVAILABLE:
            exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                _kill_group(proc)  # descendants that outlived the leader (pipes closed or not)
                _, status, usage = os.wait4(proc.pid, 0)
                proc.returncode = os.waitstatus_to_exitcode(status)
                return usage
        else:  # pragma: no cover - platforms without waitid/WNOWAIT (development machines only)
            pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
            if pid == proc.pid:
                proc.returncode = os.waitstatus_to_exitcode(status)
                return usage
        if time.monotonic() >= deadline:
            raise _Deadline()
        time.sleep(0.005)


_WNOWAIT_AVAILABLE = all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOWAIT"))


def render(fmt: str, model: dict, limits: RenderLimits, *, timestamp: str, audit: bool = False,
           command: list[str] | None = None) -> Rendered:  # fmt: skip
    """Render `model` (already validated against its variant schema) to `fmt`; raises RenderFailed."""
    request = {"format": fmt, "model": model, "fonts": font_paths(), "timestamp": timestamp, "audit": audit,
               "limits": {"cpu_seconds": limits.cpu_seconds, "memory_bytes": limits.memory_bytes,
                          "max_output_bytes": limits.max_output_bytes}}  # fmt: skip
    payload = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > limits.max_input_bytes:
        raise RenderFailed("failed", "input_too_large")
    argv = command or child_command()
    started = time.monotonic()
    deadline = started + limits.timeout_seconds
    usage = None
    with tempfile.TemporaryDirectory(prefix="exam-render-") as workdir:
        proc = _spawn(argv, workdir)
        try:
            try:
                out, err = _exchange(proc, payload, deadline, limits.max_output_bytes)
                usage = _wait(proc, deadline)
            except _Deadline:
                raise RenderFailed("timeout", "deadline") from None
            except _Overflow:
                raise RenderFailed("failed", "output_too_large") from None
        finally:
            _kill_and_reap(proc)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
    wall = time.monotonic() - started
    code = proc.returncode
    events = tuple(_audit_events(err)) if audit else ()
    cpu = usage.ru_utime + usage.ru_stime
    if code == EXIT_OK:
        if not out:
            raise RenderFailed("failed", "empty_output")
        return Rendered(out, hashlib.sha256(out).hexdigest(), wall, cpu, _peak_kib(err, usage), events)
    if code == EXIT_CLASSIFIED:
        try:
            reason = json.loads(out.decode("utf-8"))["code"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            reason = "invalid_error_document"
        raise RenderFailed("failed", str(reason)[:40])
    if code == -signal.SIGXCPU:
        raise RenderFailed("timeout", "cpu_limit")
    if code in (_EXIT_USAGE, _EXIT_NO_PDEATHSIG, _EXIT_PARENT_GONE):
        reason = {_EXIT_USAGE: "child_usage", _EXIT_NO_PDEATHSIG: "no_pdeathsig", _EXIT_PARENT_GONE: "parent_gone"}[code]
        raise RenderFailed("failed", reason)
    log.warning("renderer exited abnormally code=%s", code)  # stderr may echo content: never logged
    raise RenderFailed("failed", f"exit_{code}")


def _peak_kib(err: bytes, usage) -> int:
    """The child's own peak RSS (VmHWM it reports); ru_maxrss as a fallback (it includes the pre-exec fork image)."""
    for line in reversed(err.decode("utf-8", "replace").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and type(value.get("vmhwm_kib")) is int:
            return value["vmhwm_kib"]
    return int(usage.ru_maxrss)


def _audit_events(err: bytes):
    for line in err.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and "audit" in event:
            yield (event["audit"], event.get("arg", ""))
