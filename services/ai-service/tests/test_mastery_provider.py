"""The real mastery provider, exercised with CE's own request builder and response validator."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import (EPOCH, PROTOCOL, ROOT, SERVICE, TOKEN, ce_protocol, fresh_app, headers, make_pins)

PINS = make_pins()
USER, COURSE = uuid.UUID(int=6), uuid.UUID(int=1)
TOPIC_A, TOPIC_B = uuid.UUID(int=0xA), uuid.UUID(int=0xB)
# Recent enough that reviews are in the future (not clamped to "now"), so replayed scores are byte-identical.
BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)


def event(topic, seq, correct=True, difficulty="medium", qtype="mcq", minutes=0, event_id=None):
    return ce_protocol.BatchEvent(event_id or uuid.uuid4(), topic, seq, qtype, difficulty, correct, 0, 1200,
                                  BASE + timedelta(minutes=minutes or seq))


def send(client, events, pins=PINS, user=USER):
    request = ce_protocol.build_request(uuid.uuid4(), user, COURSE, events, pins)
    reply = client.post("/ai/v1/mastery/update", headers=headers(), json=request)
    return request, reply


def accepted(client, events, pins=PINS, user=USER):
    request, reply = send(client, events, pins, user)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    validated = ce_protocol.validate_update(body, request, events, pins)
    ce_protocol.check_review_bounds(validated.scores, datetime.now(timezone.utc))
    return body, {str(s.topic_id): s for s in validated.scores}


def test_capabilities_pass_ce_probe(client):
    reply = client.get("/ai/v1/mastery/capabilities", headers=headers())
    assert reply.status_code == 200
    body = reply.json()
    assert body["mock"] is False and body["ordering"] == "per_topic"
    assert ce_protocol.validate_capabilities(200, body, PINS).status == "compatible"
    assert client.get("/health").json()["real_profiles"] == ["mastery"]


def test_shared_fixture_request_is_accepted_by_ce(client):
    fixture = json.loads((ROOT / "contracts/fixtures/mastery.json").read_text())
    req = fixture["request"]
    reply = client.post("/ai/v1/mastery/update", headers=headers(), json=req)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["applied_event_ids"] == [req["events"][0]["event_id"]] and body["duplicate_event_ids"] == []
    score = body["scores"][0]
    assert score["attempts_seen"] == score["through_seq"] == 1 and score["mastery"] > 0.3


def test_learning_curve_and_replay_is_duplicate(client):
    events = [event(TOPIC_A, s) for s in (1, 2, 3)]
    body, scores = accepted(client, events)
    assert len(body["applied_event_ids"]) == 3
    first = scores[str(TOPIC_A)].mastery
    replay_request = ce_protocol.build_request(uuid.UUID(body["request_id"]), USER, COURSE, events, PINS)
    reply = client.post("/ai/v1/mastery/update", headers=headers(), json=replay_request)
    replay = reply.json()
    ce_protocol.validate_update(replay, replay_request, events, PINS)
    assert replay["applied_event_ids"] == [] and len(replay["duplicate_event_ids"]) == 3
    assert replay["scores"] == body["scores"]

    _, scores = accepted(client, [event(TOPIC_A, 4), event(TOPIC_A, 5)])
    higher = scores[str(TOPIC_A)].mastery
    _, scores = accepted(client, [event(TOPIC_A, 6, correct=False)])
    assert first < higher and scores[str(TOPIC_A)].mastery < higher


def test_multi_topic_batch_in_ce_order(client):
    events = [event(TOPIC_A, 1, minutes=3), event(TOPIC_B, 1, correct=False, minutes=1), event(TOPIC_A, 2, minutes=2)]
    body, scores = accepted(client, events)
    assert scores[str(TOPIC_A)].through_seq == 2 and scores[str(TOPIC_B)].through_seq == 1
    assert scores[str(TOPIC_A)].mastery > scores[str(TOPIC_B)].mastery


def test_lost_state_is_reported_as_state_mismatch_and_stores_nothing(client):
    gap = [event(TOPIC_A, 4), event(TOPIC_A, 5)]
    request, reply = send(client, gap)
    assert reply.status_code == 200
    assert reply.json()["scores"][0]["attempts_seen"] == 0
    with pytest.raises(ce_protocol.StateMismatch):
        ce_protocol.validate_update(reply.json(), request, gap, PINS)
    _, scores = accepted(client, [event(TOPIC_A, 1)])  # nothing from the refused batch was kept
    assert scores[str(TOPIC_A)].through_seq == 1


def test_lost_state_in_one_topic_rolls_back_the_whole_batch(client):
    request, reply = send(client, [event(TOPIC_B, 1), event(TOPIC_A, 3)])
    assert {s["attempts_seen"] for s in reply.json()["scores"]} == {0}
    _, scores = accepted(client, [event(TOPIC_B, 1)])
    assert scores[str(TOPIC_B)].through_seq == 1


def test_conflicting_event_at_a_sequence_is_refused(client):
    accepted(client, [event(TOPIC_A, 1)])
    _, reply = send(client, [event(TOPIC_A, 1)])  # same slot, different event id
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "sequence_conflict"


def test_event_id_reused_on_another_topic_is_refused(client):
    shared = uuid.uuid4()
    accepted(client, [event(TOPIC_A, 1, event_id=shared)])
    _, reply = send(client, [event(TOPIC_B, 1, event_id=shared)])
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "event_conflict"


def test_other_epoch_and_bad_batches_are_refused(client):
    _, reply = send(client, [event(TOPIC_A, 1)], pins=make_pins(epoch=str(uuid.UUID(int=99))))
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "epoch_mismatch"
    _, reply = send(client, [event(TOPIC_A, 1), event(TOPIC_A, 3)])
    assert reply.status_code == 422
    request = ce_protocol.build_request(uuid.uuid4(), USER, COURSE, [event(TOPIC_A, 1)], PINS)
    request["events"][0]["telemetry_trusted"] = True
    assert client.post("/ai/v1/mastery/update", headers=headers(), json=request).status_code == 422


def test_transport_rules_are_shared_with_the_fixture_service(client):
    url = "/ai/v1/mastery/capabilities"
    assert client.get(url, headers=headers(token="x" * 40)).status_code == 401
    assert client.get(url, headers=headers(protocol="studygraph.mastery/2")).status_code == 400
    request = ce_protocol.build_request(uuid.uuid4(), USER, COURSE, [event(TOPIC_A, 1)], PINS)
    raw = json.dumps(request).encode()
    reply = client.post("/ai/v1/mastery/update", headers={**headers(), "Content-Type": "text/plain"}, content=raw)
    assert reply.status_code == 415


def test_max_events_is_enforced(monkeypatch, state_path):
    with TestClient(fresh_app(monkeypatch, state_path, max_events="2")) as small:
        _, reply = send(small, [event(TOPIC_A, s) for s in (1, 2, 3)])
        assert reply.status_code == 422 and reply.json()["error"]["code"] == "too_many_events"


def test_state_survives_a_restart(monkeypatch, state_path):
    events = [event(TOPIC_A, 1), event(TOPIC_A, 2)]
    with TestClient(fresh_app(monkeypatch, state_path)) as first:
        body, _ = accepted(first, events)
    with TestClient(fresh_app(monkeypatch, state_path)) as second:
        request = ce_protocol.build_request(uuid.UUID(body["request_id"]), USER, COURSE, events, PINS)
        replay = second.post("/ai/v1/mastery/update", headers=headers(), json=request).json()
        assert replay["applied_event_ids"] == [] and replay["scores"] == body["scores"]


def test_parameter_upgrade_refolds_history(monkeypatch, state_path, tmp_path):
    events = [event(TOPIC_A, s, correct=s != 2) for s in (1, 2, 3)]
    with TestClient(fresh_app(monkeypatch, state_path)) as old:
        accepted(old, events)
    raw = json.loads((SERVICE / "app/mastery/params/bkt_assist09.json").read_text())
    raw["model"]["version"] = "0.2.0-test"
    raw["p_transit"] = 0.3
    new_params = Path(tmp_path / "bkt_v2.json")
    new_params.write_text(json.dumps(raw))
    pins_v2 = make_pins(version="0.2.0-test")
    with TestClient(fresh_app(monkeypatch, state_path, params_path=new_params)) as new:
        _, scores = accepted(new, [event(TOPIC_A, 4)], pins=pins_v2)
    with TestClient(fresh_app(monkeypatch, tmp_path / "fresh.sqlite3", params_path=new_params)) as clean:
        _, expected = accepted(clean, events + [event(TOPIC_A, 4)], pins=pins_v2)
    assert scores[str(TOPIC_A)].mastery == pytest.approx(expected[str(TOPIC_A)].mastery, abs=1e-6)


def test_backfilled_events_are_due_now(client):
    old = [ce_protocol.BatchEvent(uuid.uuid4(), TOPIC_A, 1, "mcq", "easy", True, 0, None,
                                  datetime(2025, 1, 1, tzinfo=timezone.utc))]
    _, scores = accepted(client, old)
    due = scores[str(TOPIC_A)].next_review_at
    assert abs((due - datetime.now(timezone.utc)).total_seconds()) < 60


def test_fixture_mode_keeps_the_mock(monkeypatch, state_path):
    monkeypatch.setenv("MASTERY_PROVIDER", "fixture")
    monkeypatch.setenv("AI_SERVICE_TOKEN", TOKEN)
    import importlib
    import sys
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]
    with TestClient(importlib.import_module("app.main").app) as mock:
        reply = mock.get("/ai/v1/mastery/capabilities", headers={**headers(), "X-StudyGraph-Protocol": PROTOCOL})
        assert reply.json()["mock"] is True
    assert EPOCH  # fixture epoch is the default test epoch
