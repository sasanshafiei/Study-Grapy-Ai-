"""How the renderer child is launched (independent review H1): no Python preexec_fn in the multithreaded API
process, the parent-death signal armed by the child after exec, process-group cleanup, concurrent launches.

These tests need Linux (/proc and prctl); the disposable test runner and the API container are Linux.
"""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.exams import render as rd
from tests.test_exam_render_unit import DOCX_LIMITS, LIMITS, TS, answer_model, student_model

CORE_API_DIR = Path(__file__).resolve().parents[1]
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux /proc and prctl")


def _gone(pid: int) -> bool:
    """The process no longer runs (it may linger as an unreaped zombie of PID 1 in a container)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state in ("Z", "X")


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return _gone(pid)


def test_launch_uses_no_preexec_fn_and_keeps_every_other_protection(monkeypatch):
    seen: list[tuple[list, dict]] = []
    real = subprocess.Popen

    def recording(argv, **kwargs):
        seen.append((list(argv), dict(kwargs)))
        return real(argv, **kwargs)

    monkeypatch.setattr(rd.subprocess, "Popen", recording)
    for fmt in ("pdf", "docx"):
        assert rd.render(fmt, student_model(), LIMITS if fmt == "pdf" else DOCX_LIMITS, timestamp=TS).data
    assert len(seen) == 2
    for argv, kwargs in seen:
        assert kwargs.get("preexec_fn") is None and "preexec_fn" not in kwargs
        assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
        assert kwargs["env"] == {"PYTHONHASHSEED": "0"} and "pass_fds" not in kwargs
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
        assert Path(kwargs["cwd"]).name.startswith("exam-render-") and not Path(kwargs["cwd"]).exists()  # removed again
        assert argv == [sys.executable, "-I", str(rd.CHILD), "--parent-pid", str(os.getpid())]
    source = inspect.getsource(rd)
    assert "preexec_fn=" not in source and "ctypes" not in source


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_pdf_and_docx_render_and_stay_byte_stable(fmt):
    limits = LIMITS if fmt == "pdf" else DOCX_LIMITS
    for model in (student_model(), answer_model()):
        first, second = rd.render(fmt, model, limits, timestamp=TS), rd.render(fmt, model, limits, timestamp=TS)
        assert first.data == second.data and first.max_rss_kib > 0
        assert first.data.startswith(b"%PDF-") if fmt == "pdf" else first.data.startswith(b"PK")


def test_concurrent_launches_from_many_threads_while_other_threads_hold_locks():
    """Many simultaneous launches from pool threads, while other threads keep taking the logging and import locks
    (the situation in which a preexec_fn could deadlock after fork). Every render must finish."""
    import importlib
    import logging
    import threading

    stop = threading.Event()
    log = logging.getLogger("tests.render.noise")

    def noise():
        while not stop.is_set():
            log.debug("noise %s", time.monotonic())
            importlib.import_module("json")
            _ = [bytearray(4096) for _ in range(50)]

    noisy = [threading.Thread(target=noise, daemon=True) for _ in range(4)]
    for thread in noisy:
        thread.start()
    jobs = [("pdf" if i % 2 == 0 else "docx", student_model() if i % 3 else answer_model()) for i in range(24)]
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda job: rd.render(job[0], job[1], LIMITS if job[0] == "pdf" else DOCX_LIMITS,
                                                          timestamp=TS), jobs, timeout=120))  # fmt: skip
    finally:
        stop.set()
        for thread in noisy:
            thread.join(5)
    assert len(results) == 24 and time.monotonic() - started < 120
    by_job: dict = {}
    for (fmt, model), result in zip(jobs, results):
        by_job.setdefault((fmt, model["variant"]), set()).add(result.sha256)
    assert all(len(hashes) == 1 for hashes in by_job.values())  # concurrency changes no byte


GRANDCHILD = """
import os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
with open(sys.argv[1], "w") as handle:
    handle.write(f"{os.getpid()} {os.getpgrp()} {grandchild.pid}")
time.sleep(60)
"""


@linux_only
def test_timeout_kills_and_reaps_the_whole_process_group(tmp_path):
    marker = tmp_path / "pids"
    command = [sys.executable, "-I", "-c", GRANDCHILD, str(marker)]
    limits = rd.RenderLimits(2.0, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 1024)
    started = time.monotonic()
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), limits, timestamp=TS, command=command)
    assert raised.value.kind == "timeout" and time.monotonic() - started < 10
    child, group, grandchild = (int(x) for x in marker.read_text().split())
    assert group == child  # start_new_session: the child leads its own process group
    assert group != os.getpgrp()
    assert _wait_gone(child) and _wait_gone(grandchild), "the whole process group must be killed"


@linux_only
def test_child_refuses_when_its_parent_is_already_gone_or_arguments_are_wrong():
    not_the_parent = str(4194305)  # above Linux's maximum pid_max: never the actual parent (PID 1 may be, in a container)
    for argv, reason in (([sys.executable, "-I", str(rd.CHILD), "--parent-pid", not_the_parent], "parent_gone"),
                         ([sys.executable, "-I", str(rd.CHILD)], "child_usage"),
                         ([sys.executable, "-I", str(rd.CHILD), "--parent-pid", "x"], "child_usage")):  # fmt: skip
        with pytest.raises(rd.RenderFailed) as raised:
            rd.render("pdf", student_model(), LIMITS, timestamp=TS, command=argv)
        assert (raised.value.kind, raised.value.reason) == ("failed", reason), argv


INTERMEDIATE = """
import os, sys, tempfile, time
from app.exams import render
proc = render._spawn(render.child_command(), tempfile.mkdtemp())
print(proc.pid, flush=True)
time.sleep(60)
"""


@linux_only
def test_parent_death_kills_a_running_child():
    """The parent-death signal is armed before the child reads its input: kill the parent while the child waits."""
    parent = subprocess.Popen([sys.executable, "-c", INTERMEDIATE], cwd=CORE_API_DIR, stdout=subprocess.PIPE,
                              env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}, text=True)  # fmt: skip
    try:
        child = int(parent.stdout.readline())
        time.sleep(1.0)  # well past interpreter start-up: the signal is armed, the child blocks on stdin
        assert not _gone(child), "the child must still be waiting for its input"
        status = Path(f"/proc/{child}/status").read_text()
        assert f"PPid:\t{parent.pid}" in status
        parent.send_signal(signal.SIGKILL)
        parent.wait(5)
        assert _wait_gone(child), "the child must die with its parent"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(5)


def test_resource_limits_still_apply_inside_the_child():
    tiny_memory = rd.RenderLimits(30, 25, 48 * 1024 * 1024, 2 * 1024 * 1024, 16 * 1024 * 1024)
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", answer_model(), tiny_memory, timestamp=TS)
    assert raised.value.kind == "failed"
    small_output = rd.RenderLimits(30, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 4096)
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", answer_model(), small_output, timestamp=TS)
    assert raised.value.reason in ("too_large", "output_too_large")


# --- H2: process-group cleanup after the leader exited early -----------------------------------------------------

EARLY_EXIT = """
import os, subprocess, sys
keep_pipes = sys.argv[2] == "keep"
streams = {} if keep_pipes else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
grandchild = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **streams)
with open(sys.argv[1], "w") as handle:
    handle.write(f"{os.getpid()} {grandchild.pid}")
sys.exit(0)  # the process-group leader is gone; the grandchild stays in its group
"""


def _leader_leaked(pid: int) -> bool:
    """An exited leader that is still OUR unreaped zombie would be a leak (reaping is our job)."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return fields[0] == "Z" and int(fields[1]) == os.getpid()


@linux_only
def test_h2_leader_exits_early_grandchild_keeps_the_pipes_and_the_deadline_kills_it(tmp_path):
    marker = tmp_path / "pids"
    limits = rd.RenderLimits(2.0, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 1024)
    started = time.monotonic()
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), limits, timestamp=TS,
                  command=[sys.executable, "-I", "-c", EARLY_EXIT, str(marker), "keep"])  # fmt: skip
    assert (raised.value.kind, raised.value.reason) == ("timeout", "deadline")
    assert time.monotonic() - started < 10
    leader, grandchild = (int(x) for x in marker.read_text().split())
    assert _wait_gone(grandchild, 5), "the grandchild that held the pipes must be killed"
    assert not _leader_leaked(leader), "the exited leader must be reaped"


@linux_only
def test_h2_leader_exits_early_detached_grandchild_is_killed_too(tmp_path):
    """The pipes reach EOF (the grandchild closed them): the group is killed before the leader is reaped."""
    marker = tmp_path / "pids"
    with pytest.raises(rd.RenderFailed) as raised:
        rd.render("pdf", student_model(), LIMITS, timestamp=TS,
                  command=[sys.executable, "-I", "-c", EARLY_EXIT, str(marker), "detach"])  # fmt: skip
    assert (raised.value.kind, raised.value.reason) == ("failed", "empty_output")
    leader, grandchild = (int(x) for x in marker.read_text().split())
    assert _wait_gone(grandchild, 5) and not _leader_leaked(leader)


@linux_only
def test_h2_repeated_failures_leak_no_process(tmp_path):
    pids = []
    limits = rd.RenderLimits(1.0, 25, 512 * 1024 * 1024, 2 * 1024 * 1024, 1024)
    for index in range(6):
        marker = tmp_path / f"pids{index}"
        mode = "keep" if index % 2 == 0 else "detach"
        with pytest.raises(rd.RenderFailed):
            rd.render("pdf", student_model(), limits, timestamp=TS,
                      command=[sys.executable, "-I", "-c", EARLY_EXIT, str(marker), mode])  # fmt: skip
        pids.append(tuple(int(x) for x in marker.read_text().split()))
    for command in ([sys.executable, "-I", "-c", "import sys; sys.stdin.read(); sys.exit(1)"],
                    [sys.executable, "-I", "-c", "import time; time.sleep(30)"]):  # crash and plain timeout
        with pytest.raises(rd.RenderFailed):
            rd.render("pdf", student_model(), limits, timestamp=TS, command=command)
    for leader, grandchild in pids:
        assert _wait_gone(grandchild, 5) and not _leader_leaked(leader), (leader, grandchild)
    # and a normal render afterwards is unaffected
    assert rd.render("pdf", student_model(), LIMITS, timestamp=TS).data.startswith(b"%PDF-")


def test_h2_group_is_signalled_only_while_the_leader_is_unreaped():
    """After the leader was reaped (returncode known), its group ID is never signalled again (no reuse risk)."""
    calls = []

    class Reaped:
        pid, returncode = 999_999, 0

        def kill(self):
            calls.append("kill")

    original = rd.os.killpg
    rd.os.killpg = lambda *args: calls.append(("killpg", args))
    try:
        rd._kill_group(Reaped())
        rd._kill_and_reap(Reaped())
    finally:
        rd.os.killpg = original
    assert calls == []
