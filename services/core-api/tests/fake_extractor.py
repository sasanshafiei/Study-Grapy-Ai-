"""Test double for app/ingestion/extract_child.py (tests only; never configured in production).

    python -I fake_extractor.py MODE SIGNAL_DIR --fd N --max-pages P ...   (the real extractor's arguments)

Tests select it by passing `command=` to extract_pdf() or, for real worker
processes, through tests/worker_app.py. Modes:

  real        run the real extractor (exec) with the same arguments
  wait        record pid/ppid in SIGNAL_DIR, wait until SIGNAL_DIR/release exists, then run the real one
  hang        record pid/ppid, then sleep (until killed)
  crash:N     exit 7 on the first N runs (counted in SIGNAL_DIR), then run the real one
  garbage     print text that is not JSON and exit 0
  huge        print far more than any output cap and exit 0
  inspect     report its descriptors, environment and parent as a one-page "ok" document
  limits      apply the real extractor's limits, then report getrlimit values the same way
"""

import importlib.util
import json
import os
import resource
import sys
import time
from pathlib import Path

REAL = Path(__file__).resolve().parents[1] / "app" / "ingestion" / "extract_child.py"


def _record(signal_dir: Path) -> None:
    pid = os.getpid()
    (signal_dir / f"extractor-{pid}.tmp").write_text(f"{pid} {os.getppid()}")
    os.replace(signal_dir / f"extractor-{pid}.tmp", signal_dir / f"extractor-{pid}.pid")


def _run_real(args: list[str]) -> None:
    os.execv(sys.executable, [sys.executable, "-I", str(REAL), *args])


def _ok(text: str) -> None:
    doc = {"status": "ok", "page_count": 1, "pages": [{"page": 1, "text": text}]}
    sys.stdout.buffer.write(json.dumps(doc).encode())
    sys.stdout.flush()


def _option(args: list[str], name: str) -> int:
    return int(args[args.index(name) + 1])


def main() -> None:
    mode, signal_dir, args = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
    if mode == "real":
        _run_real(args)
    elif mode == "wait":
        _record(signal_dir)
        deadline = time.monotonic() + 600
        while not (signal_dir / "release").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        _run_real(args)
    elif mode == "hang":
        _record(signal_dir)
        while True:
            time.sleep(1)
    elif mode.startswith("crash:"):
        counter = signal_dir / "crash-count"
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        if count <= int(mode.split(":", 1)[1]):
            sys.exit(7)
        _run_real(args)
    elif mode == "garbage":
        sys.stdout.write("this is not json")
    elif mode == "huge":
        block = b"x" * (1024 * 1024)
        try:
            for _ in range(512):
                sys.stdout.buffer.write(block)
        except BrokenPipeError:
            pass
    elif mode == "inspect":
        fds = sorted(int(name) for name in os.listdir("/proc/self/fd"))
        _ok(json.dumps({"fds": fds, "env": dict(os.environ), "ppid": os.getppid(), "fd": _option(args, "--fd")}))
    elif mode == "limits":
        spec = importlib.util.spec_from_file_location("extract_child", REAL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._apply_limits(_option(args, "--cpu-seconds"), _option(args, "--memory-bytes"))
        names = ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_CORE", "RLIMIT_FSIZE", "RLIMIT_NOFILE")
        _ok(json.dumps({name: list(resource.getrlimit(getattr(resource, name))) for name in names}))
    else:
        sys.exit(64)


if __name__ == "__main__":
    main()
