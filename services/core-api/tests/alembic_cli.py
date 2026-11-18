"""Run the real `alembic` command line, and read the revision graph."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

CORE_API_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = CORE_API_DIR / "alembic.ini"

# The first revision. Tests about the historical baseline target it explicitly;
# tests about the current schema use head_revision() instead.
BASELINE_REVISION = "0001"


def script_directory() -> ScriptDirectory:
    """The revision files (reading them needs no database)."""
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def head_revision() -> str:
    """The single current head; raises if the history has branched."""
    heads = script_directory().get_heads()
    if len(heads) != 1:
        raise AssertionError(f"expected exactly one Alembic head, found {sorted(heads)}")
    return heads[0]


def run_alembic(
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `alembic <args>` from core-api.

    The child process gets only PATH plus `env`; nothing else is inherited, so
    a DATABASE_URL, TEST_* or PG* variable from the calling process can never
    leak into a migration command by accident.
    """
    child_env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    child_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=CORE_API_DIR,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=180,
    )
