"""Test helpers: a fresh real-mode ai-service per test, and CE's own mastery consumer.

core-api and ai-service both name their package `app`, so CE's mastery protocol module is
loaded under the alias package `ce_mastery` straight from services/core-api/app/mastery.
Nothing in core-api is modified or imported as `app`.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
CE_MASTERY = ROOT / "services" / "core-api" / "app" / "mastery"
TOKEN = "ai2-test-service-credential-0000000001"
EPOCH = "00000000-0000-0000-0000-000000000008"  # the shared fixture's epoch
PROTOCOL = "studygraph.mastery/1"

if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))


def _load_ce():
    if "ce_mastery" not in sys.modules:
        package = types.ModuleType("ce_mastery")
        package.__path__ = [str(CE_MASTERY)]
        sys.modules["ce_mastery"] = package
    return importlib.import_module("ce_mastery.protocol"), importlib.import_module("ce_mastery.pins")


ce_protocol, ce_pins = _load_ce()


def default_model():
    raw = json.loads((SERVICE / "app/mastery/params/bkt_assist09.json").read_text(encoding="utf-8"))
    return raw["model"]["name"], raw["model"]["version"]


def make_pins(name=None, version=None, epoch=EPOCH):
    default_name, default_version = default_model()
    return ce_pins.Pins(name or default_name, version or default_version, uuid.UUID(epoch), "test-ns",
                        b"ai2-test-pseudonym-key-000000000001")


def fresh_app(monkeypatch, state_path, params_path=None, epoch=EPOCH, max_events="200"):
    monkeypatch.setenv("AI_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("MASTERY_PROVIDER", "real")
    monkeypatch.setenv("MASTERY_STATE_EPOCH", epoch)
    monkeypatch.setenv("MASTERY_STATE_PATH", str(state_path))
    monkeypatch.setenv("MASTERY_MAX_EVENTS", max_events)
    if params_path:
        monkeypatch.setenv("MASTERY_PARAMS_PATH", str(params_path))
    else:
        monkeypatch.delenv("MASTERY_PARAMS_PATH", raising=False)
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]
    return importlib.import_module("app.main").app


def headers(protocol=PROTOCOL, token=TOKEN):
    return {"Authorization": f"Bearer {token}", "X-StudyGraph-Protocol": protocol}


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state" / "mastery.sqlite3"


@pytest.fixture
def client(monkeypatch, state_path):
    with TestClient(fresh_app(monkeypatch, state_path)) as c:
        yield c
