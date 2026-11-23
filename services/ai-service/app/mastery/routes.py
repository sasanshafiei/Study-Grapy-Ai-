"""Real studygraph.mastery/1 provider routes (AI-2). Enabled with MASTERY_PROVIDER=real.

Configuration (environment):
- MASTERY_STATE_EPOCH   required, lower-case canonical UUID agreed with CE (CE pins the same value)
- MASTERY_STATE_PATH    SQLite file on a persistent volume (default /data/mastery/state.sqlite3)
- MASTERY_PARAMS_PATH   BKT parameter JSON (default: app/mastery/params/bkt_assist09.json);
                        its model name and version are what CE pins as MASTERY_MODEL_NAME/_VERSION
- MASTERY_MAX_EVENTS    advertised batch limit, 1..1000 (default 200)

Transport rules (bearer token, protocol header, strict JSON, byte limit, error envelope) are
the shared ones from app/main.py, injected by `register` so both providers behave the same.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import model, service, store

PROTOCOL = service.PROTOCOL
CANONICAL_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def enabled() -> bool:
    return os.environ.get("MASTERY_PROVIDER", "fixture").strip().lower() == "real"


def _settings():
    epoch = os.environ.get("MASTERY_STATE_EPOCH", "")
    if not CANONICAL_UUID.fullmatch(epoch):
        raise RuntimeError("MASTERY_STATE_EPOCH must be a lower-case canonical UUID when MASTERY_PROVIDER=real")
    max_events = int(os.environ.get("MASTERY_MAX_EVENTS", "200"))
    if not 1 <= max_events <= 1000:
        raise RuntimeError("MASTERY_MAX_EVENTS must be between 1 and 1000")
    params = model.load_params(os.environ.get("MASTERY_PARAMS_PATH") or None)
    path = os.environ.get("MASTERY_STATE_PATH", "/data/mastery/state.sqlite3")
    store.connect(path).close()  # fail at start-up, not on the first batch, if the volume is unusable
    return epoch, max_events, params, path


def register(app, spec, make_validator, authorize, read_payload, wire_error) -> None:
    epoch, max_events, params, path = _settings()
    capabilities = {
        "protocol": PROTOCOL, "service": "ai-service", "mock": False,
        "model": {"name": params.name, "version": params.version},
        "state_epoch": epoch, "max_events": max_events, "ordering": "per_topic",
    }
    paths = spec["paths"]
    cap_op = paths["/ai/v1/mastery/capabilities"]["get"]
    upd_op = paths["/ai/v1/mastery/update"]["post"]
    cap_out = make_validator(cap_op["responses"]["200"]["content"]["application/json"]["schema"])
    upd_in = make_validator(upd_op["requestBody"]["content"]["application/json"]["schema"])
    upd_out = make_validator(upd_op["responses"]["200"]["content"]["application/json"]["schema"])
    cap_out.validate(capabilities)

    async def mastery_capabilities(request: Request):
        authorize(request, PROTOCOL)
        return JSONResponse(capabilities)

    def run(payload: dict) -> dict:
        conn = store.connect(path)
        try:
            outcome = service.apply_batch(conn, params, epoch, payload)
        finally:
            conn.close()
        return service.build_result(params, payload, outcome, datetime.now(timezone.utc))

    async def mastery_update(request: Request):
        authorize(request, PROTOCOL)
        payload = await read_payload(request)
        if not upd_in.is_valid(payload):
            raise wire_error(422, "invalid_request", "Request does not match the protocol schema.")
        if len(payload["events"]) > max_events:
            raise wire_error(422, "too_many_events", "The batch exceeds the advertised max_events.")
        try:
            result = await run_in_threadpool(run, payload)
        except service.MasteryError as exc:
            raise wire_error(exc.status, exc.code, exc.message) from None
        except sqlite3.OperationalError:
            raise wire_error(503, "state_unavailable", "Learner state is temporarily unavailable.") from None
        upd_out.validate(result)
        return JSONResponse(result)

    app.add_api_route("/ai/v1/mastery/capabilities", mastery_capabilities, methods=["GET"],
                      name=cap_op["operationId"])
    app.add_api_route("/ai/v1/mastery/update", mastery_update, methods=["POST"], name=upd_op["operationId"])
