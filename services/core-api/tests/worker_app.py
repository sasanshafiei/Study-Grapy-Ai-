"""Celery app module for the real-worker integration tests (celery -A tests.worker_app ...).

It is the production app.worker, unchanged, plus two test hooks that exist
only in this module and only act in worker processes the tests start:

- SG_TEST_FAKE_EXTRACTOR=<mode> replaces the extractor command with
  tests/fake_extractor.py in that mode (SG_TEST_SIGNAL_DIR is where the fake
  records its pid and waits for release). Production code has no setting or
  variable that selects another executable.
- SG_TEST_READY_FILE: written when the worker is ready to consume (the
  worker runs without remote control, so `celery inspect ping` is not used).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from celery import signals

from app.ingestion import extraction
from app.worker import celery_app  # noqa: F401 - the app and its tasks

_mode = os.environ.get("SG_TEST_FAKE_EXTRACTOR", "").strip()
if _mode:
    extraction.EXTRACTOR_COMMAND = (
        sys.executable,
        "-I",
        str(Path(__file__).with_name("fake_extractor.py")),
        _mode,
        os.environ["SG_TEST_SIGNAL_DIR"],
    )


@signals.worker_ready.connect
def _announce_ready(**_):
    ready = os.environ.get("SG_TEST_READY_FILE")
    if ready:
        Path(ready).write_text(str(os.getpid()))
