"""Mastery delivery on the disposable database with a deterministic in-process studygraph.mastery/1 service.

No socket is opened: the client's transport is an httpx.MockTransport (tests/m8_support.FakeMastery).
"""

from __future__ import annotations

import threading
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.auth import CurrentUser
from app.config import Settings
from app.ingestion import runtime
from app.ingestion.dispatch import PublishError
from app.mastery import admin, delivery, runner
from app.mastery.client import AiMasteryClient
from app.mastery.pins import pins_of
from tests.m8_support import PINS, TOKEN, FakeMastery, graded_attempt


@pytest.fixture
def setup(world):
    professor = world.user("professor")
    course = world.course(professor)
    topics = [world.topic(course, "Stoichiometry"), world.topic(course, "Kinetics")]
    student = world.user("student")
    world.enroll(student, course)
    return {"world": world, "engine": world.engine, "course": course, "topics": topics, "student": student.id,
            "professor": professor}  # fmt: skip


def make_ctx(engine, fake: FakeMastery, **overrides) -> runner.MasteryContext:
    settings = Settings(
        **{**PINS, "mastery_delivery_enabled": True, "ai_service_token": SecretStr(TOKEN),
           "ai_service_url": "http://ai.test:8000", "mastery_capabilities_cache_seconds": 0, **overrides}
    )  # fmt: skip
    return runner.MasteryContext(
        sessionmaker(bind=engine, autoflush=False), settings, AiMasteryClient(settings, transport=fake.transport())
    )


@pytest.fixture
def fake():
    return FakeMastery()


def rows(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


def stream(engine, user):
    return rows(engine, "SELECT * FROM mastery_streams WHERE user_id = :u", u=user)[0]


def make_due(engine, user):
    with engine.begin() as c:
        c.execute(text("UPDATE mastery_streams SET next_attempt_at = now() - interval '1 second' WHERE user_id = :u"),
                  {"u": user})  # fmt: skip


def expire_lease(engine, user):
    with engine.begin() as c:
        c.execute(
            text("UPDATE mastery_streams SET claimed_at = now() - interval '10 minutes', "
                 "lease_expires_at = now() - interval '1 minute' WHERE user_id = :u AND state = 'delivering'"),  # fmt: skip
            {"u": user},
        )


def attempts(s, topic_index=0, n=1, correct=True):
    return [graded_attempt(s["engine"], s["student"], s["course"], s["topics"][topic_index], correct=correct)
            for _ in range(n)]  # fmt: skip


def deliver(s, ctx):
    return runner.deliver_stream(s["student"], s["course"], ctx)


# --- flags and the happy path ---------------------------------------------------------------------------


def test_disabled_delivery_keeps_writing_events_and_sends_nothing(setup, fake):
    events = attempts(setup, 0, 2)
    ctx = make_ctx(setup["engine"], fake, mastery_delivery_enabled=False)
    assert deliver(setup, ctx) == "disabled"
    assert runner.dispatch(ctx.session_factory, None, ctx.settings) == (0, 0, "delivery_disabled")
    assert fake.requests == [] and fake.probes == 0
    assert sorted(r.event_id for r in rows(setup["engine"], "SELECT event_id FROM mastery_outbox WHERE status = 'pending'")
                  ) == sorted(events)  # fmt: skip


def test_a_batch_is_delivered_and_its_predictions_stored_atomically(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 2)
    attempts(setup, 1, 1, correct=False)
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "completed"
    request = fake.requests[0]
    assert len(request["events"]) == 3 and str(setup["student"]) not in str(request)
    outbox = rows(e, "SELECT status, outcome, batch_id FROM mastery_outbox")
    assert {(r.status, r.outcome) for r in outbox} == {("delivered", "applied")}
    assert {str(r.batch_id) for r in outbox} == {request["request_id"]}
    scores = {r.topic_id: r for r in rows(e, "SELECT * FROM mastery_scores WHERE user_id = :u", u=setup["student"])}
    assert scores[setup["topics"][0]].through_seq == 2 and scores[setup["topics"][1]].through_seq == 1
    assert scores[setup["topics"][0]].model_version == "1.4.0" and scores[setup["topics"][0]].received_at is not None
    st = stream(e, setup["student"])
    assert (st.state, st.batch_id, st.failure_count, st.claim_count) == ("ready", None, 0, 1)
    assert rows(e, "SELECT status FROM mastery_model_state")[0].status == "compatible"
    assert deliver(setup, ctx) == "nothing_pending"


# --- retries keep the batch --------------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["timeout", "http_500", "connect"])
def test_retries_resend_the_same_batch_and_new_events_wait(setup, fake, failure):
    e = setup["engine"]
    first = attempts(setup, 0, 2)
    ctx = make_ctx(e, fake)
    fake.mode = failure
    assert deliver(setup, ctx) == "retry"
    st = stream(e, setup["student"])
    assert st.failure_count == 1 and st.state == "ready" and st.last_error_code.startswith("upstream_")
    later = attempts(setup, 0, 1)  # committed while the batch is unfinished
    make_due(e, setup["student"])
    fake.mode = "ok"
    assert deliver(setup, ctx) == "completed"
    retried, original = fake.requests[1], fake.requests[0]
    assert retried == original  # same batch id, same events, byte-identical payload
    assert {ev["event_id"] for ev in retried["events"]} == {str(a) for a in first}
    assert rows(e, "SELECT status FROM mastery_outbox WHERE event_id = :e", e=later[0])[0].status == "pending"
    assert deliver(setup, ctx) == "completed"
    assert [ev["event_id"] for ev in fake.requests[2]["events"]] == [str(later[0])]
    assert fake.requests[2]["request_id"] != original["request_id"]
    assert fake.requests[2]["events"][0]["topic_seq"] == 3


def test_a_lost_response_completes_through_duplicate_accounting(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 2)
    ctx = make_ctx(e, fake)
    fake.mode = "lost"  # the service applied the events, the response never arrived
    assert deliver(setup, ctx) == "retry"
    make_due(e, setup["student"])
    fake.mode = "ok"
    assert deliver(setup, ctx) == "completed"
    assert {r.outcome for r in rows(e, "SELECT outcome FROM mastery_outbox")} == {"duplicate"}
    assert rows(e, "SELECT through_seq FROM mastery_scores WHERE user_id = :u", u=setup["student"])[0].through_seq == 2


def test_worker_loss_and_stale_completion(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 2)
    ctx = make_ctx(e, fake)
    pins = pins_of(ctx.settings)
    claim = delivery.claim(ctx.session_factory, setup["student"], setup["course"], ctx.settings, pins, 100)
    assert isinstance(claim, delivery.Claim)
    expire_lease(e, setup["student"])  # the worker died holding the claim
    assert delivery.recover(ctx.session_factory, ctx.settings, 10) == (1, 0)
    st = stream(e, setup["student"])
    assert (st.state, st.failure_count, st.last_error_code, st.batch_id) == ("ready", 1, "lease_expired", claim.batch_id)
    # the old attempt's late answer is rejected: its token is no longer current
    from app.mastery.protocol import build_request, validate_update

    request = build_request(claim.batch_id, setup["student"], setup["course"], list(claim.events), pins)
    update = validate_update(fake._update(request), request, list(claim.events), pins)
    delivery.record_probe(ctx.session_factory, delivery.probe_started_at(ctx.session_factory), "compatible", None,
                          pins, "ai-service")  # fmt: skip
    assert delivery.complete(ctx.session_factory, claim, update, pins) == "stale"
    assert rows(e, "SELECT count(*) AS n FROM mastery_scores WHERE user_id = :u", u=setup["student"])[0].n == 0
    make_due(e, setup["student"])
    again = delivery.claim(ctx.session_factory, setup["student"], setup["course"], ctx.settings, pins, 100)
    assert again.batch_id == claim.batch_id and again.token != claim.token  # same batch, renewed claim
    assert [ev.event_id for ev in again.events] == [ev.event_id for ev in claim.events]
    expire_lease(e, setup["student"])
    assert delivery.complete(ctx.session_factory, again, update, pins) == "stale"  # expired lease: database time


def test_commit_uncertainty_is_resolved_by_the_lease_with_the_same_batch(setup, fake, monkeypatch):
    e = setup["engine"]
    attempts(setup, 0, 1)
    ctx = make_ctx(e, fake)

    def uncertain(*args, **kwargs):
        raise ConnectionError("connection lost while committing")

    monkeypatch.setattr(delivery, "complete", uncertain)
    assert deliver(setup, ctx) == "uncertain"
    monkeypatch.undo()
    assert stream(e, setup["student"]).state == "delivering"
    expire_lease(e, setup["student"])
    delivery.recover(ctx.session_factory, ctx.settings, 10)
    make_due(e, setup["student"])
    assert deliver(setup, ctx) == "completed"
    assert fake.requests[0] == fake.requests[1]
    assert {r.outcome for r in rows(e, "SELECT outcome FROM mastery_outbox")} == {"duplicate"}


def test_attempts_commit_during_delivery_without_waiting(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 1)
    inserted = []

    def during_request(body):
        inserted.append(attempts(setup, 0, 1)[0])  # a graded attempt commits while the request is in flight
        return body

    fake.tamper = during_request
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "completed"
    assert rows(e, "SELECT status FROM mastery_outbox WHERE event_id = :e", e=inserted[0])[0].status == "pending"
    fake.tamper = None
    assert deliver(setup, ctx) == "completed"
    assert fake.requests[1]["events"][0]["topic_seq"] == 2


def test_only_one_claim_per_stream(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 2)
    ctx = make_ctx(e, fake)
    pins = pins_of(ctx.settings)
    results = []
    barrier = threading.Barrier(4)

    def claim():
        barrier.wait()
        results.append(delivery.claim(ctx.session_factory, setup["student"], setup["course"], ctx.settings, pins, 100))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claims = [r for r in results if isinstance(r, delivery.Claim)]
    assert len(claims) == 1
    assert {r.reason for r in results if isinstance(r, delivery.NoClaim)} <= {"busy", "not_due"}


# --- order -------------------------------------------------------------------------------------------------------


def test_batches_are_per_topic_prefixes_and_failures_block_the_head(setup, fake):
    e = setup["engine"]
    a = attempts(setup, 0, 3)
    b = attempts(setup, 1, 1)
    ctx = make_ctx(e, fake, mastery_max_batch_events=2, mastery_max_failures=2)
    fake.mode = "http_500"
    assert deliver(setup, ctx) == "retry"
    make_due(e, setup["student"])
    assert deliver(setup, ctx) == "blocked"
    sent = {ev["event_id"] for ev in fake.requests[0]["events"]}
    assert sent == {str(a[0]), str(b[0])}  # topic prefixes: seq 1 of each topic
    st = stream(e, setup["student"])
    assert (st.state, st.last_error_code) == ("blocked", "upstream_http_500")
    statuses = dict(rows(e, "SELECT event_id, status FROM mastery_outbox"))
    assert statuses[a[0]] == statuses[b[0]] == "failed" and statuses[a[1]] == statuses[a[2]] == "pending"
    make_due(e, setup["student"])
    fake.mode = "ok"
    assert deliver(setup, ctx) == "not_due" and len(fake.requests) == 2  # blocked: nothing later is sent
    admin_user = setup["world"].user("admin")
    assert admin.retry_stream(ctx.session_factory, setup["student"], setup["course"], setup["professor"].id,
                              "after_fix") == "not_allowed"  # fmt: skip
    assert admin.retry_stream(ctx.session_factory, setup["student"], setup["course"], admin_user.id, "after_fix") == "retried"
    st = stream(e, setup["student"])
    assert (st.operator_retry_count, st.last_operator_actor_id, st.failure_count) == (1, admin_user.id, 0)
    assert deliver(setup, ctx) == "completed"
    assert fake.requests[2]["request_id"] == fake.requests[0]["request_id"]
    assert deliver(setup, ctx) == "completed"
    assert [ev["topic_seq"] for ev in fake.requests[3]["events"]] == [2, 3]


def test_a_sequence_gap_blocks_instead_of_skipping(setup, fake):
    e = setup["engine"]
    a = attempts(setup, 0, 2)
    with e.begin() as c:  # an administrator removed the first event (outbox DELETE stays possible)
        c.execute(text("DELETE FROM mastery_outbox WHERE event_id = :e"), {"e": a[0]})
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "blocked"
    assert stream(e, setup["student"]).last_error_code == "sequence_gap" and fake.requests == []


# --- model identity, epochs, state ---------------------------------------------------------------------------


def test_an_unexpected_model_version_blocks_delivery_and_is_sticky(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 1)
    fake.model = {"name": "ai2-bkt", "version": "2.0.0"}
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "probe_incompatible"
    state = rows(e, "SELECT status, reason FROM mastery_model_state")[0]
    assert (state.status, state.reason) == ("incompatible", "model_mismatch") and fake.requests == []
    fake.model = {"name": "ai2-bkt", "version": "1.4.0"}  # the service changes back: not trusted automatically
    assert deliver(setup, ctx) == "model_blocked"
    assert runner.dispatch(ctx.session_factory, None, ctx.settings)[2] == "model_blocked"
    admin_user = setup["world"].user("admin")
    assert admin.clear_model_block(ctx.session_factory, admin_user.id, "version_reviewed") == "cleared"
    assert deliver(setup, ctx) == "completed"


def test_a_stale_capability_probe_never_overwrites_a_newer_one(setup, fake):
    ctx = make_ctx(setup["engine"], fake)
    pins = pins_of(ctx.settings)
    old = delivery.probe_started_at(ctx.session_factory)
    new = delivery.probe_started_at(ctx.session_factory)
    assert delivery.record_probe(ctx.session_factory, new, "compatible", None, pins, "ai-service")
    assert not delivery.record_probe(ctx.session_factory, old, "incompatible", "model_mismatch", pins, "ai-service")
    assert rows(setup["engine"], "SELECT status FROM mastery_model_state")[0].status == "compatible"


@pytest.mark.parametrize("change", ["mock", "epoch"])
def test_mock_and_foreign_epoch_services_receive_nothing(setup, fake, change):
    attempts(setup, 0, 1)
    if change == "mock":
        fake.mock = True
    else:
        fake.epoch = str(uuid.uuid4())
    assert deliver(setup, make_ctx(setup["engine"], fake)) == "probe_incompatible"
    assert fake.requests == []


def test_a_changed_pinned_epoch_blocks_an_old_batch(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 1)
    fake.mode = "timeout"
    assert deliver(setup, make_ctx(e, fake)) == "retry"
    new_epoch = str(uuid.uuid4())
    fake.epoch, fake.mode = new_epoch, "ok"
    with e.begin() as c:
        c.execute(text("UPDATE mastery_model_state SET status = 'unknown', protocol = NULL, service = NULL, "
                       "model_name = NULL, model_version = NULL, state_epoch = NULL, pseudonym_key_id = NULL"))  # fmt: skip
    make_due(e, setup["student"])
    assert deliver(setup, make_ctx(e, fake, mastery_state_epoch=new_epoch)) == "blocked"
    assert stream(e, setup["student"]).last_error_code == "epoch_changed" and len(fake.requests) == 1


def test_lost_remote_state_blocks_the_model_and_stores_nothing(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 2)
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "completed"
    attempts(setup, 0, 1)
    fake.mode = "forget"
    assert deliver(setup, ctx) == "blocked"
    state = rows(e, "SELECT status, reason FROM mastery_model_state")[0]
    assert (state.status, state.reason) == ("incompatible", "ai_state_mismatch")
    assert rows(e, "SELECT through_seq FROM mastery_scores WHERE user_id = :u", u=setup["student"])[0].through_seq == 2
    assert stream(e, setup["student"]).last_error_code == "ai_state_mismatch"


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"model": {"name": "ai2-bkt", "version": "1.4.1"}}, "response_model_mismatch"),
        ({"model": {"name": "other-model", "version": "1.4.0"}}, "response_model_mismatch"),
        ({"state_epoch": str(uuid.uuid4())}, "response_state_epoch_mismatch"),
        ({"protocol": "studygraph.mastery/2"}, "response_protocol_mismatch"),
        ({"mock": True}, "response_mock_response"),
    ],
)
def test_an_identity_change_after_a_cached_probe_suspends_the_model_and_keeps_the_batch(
    setup, fake, make_api, change, reason
):
    e = setup["engine"]
    attempts(setup, 0, 1)
    ctx = make_ctx(e, fake, mastery_capabilities_cache_seconds=300)  # one client: its probe result is cached
    assert deliver(setup, ctx) == "completed" and fake.probes == 1
    attempts(setup, 0, 1)
    fake.tamper = lambda body: {**body, **change}
    assert deliver(setup, ctx) == "model_blocked"
    assert fake.probes == 1  # the compatible probe was cached: only the response revealed the change
    state = rows(e, "SELECT status, reason FROM mastery_model_state")[0]
    assert (state.status, state.reason) == ("incompatible", reason)
    st = stream(e, setup["student"])
    assert (st.state, st.failure_count, st.claim_token, st.last_error_code) == ("ready", 0, None, reason)
    batch = st.batch_id
    assert batch is not None and {r.status for r in rows(e, "SELECT status FROM mastery_outbox WHERE batch_id = :b",
                                                         b=batch)} == {"batched"}  # fmt: skip
    assert rows(e, "SELECT through_seq FROM mastery_scores WHERE user_id = :u", u=setup["student"])[0].through_seq == 1
    make_due(e, setup["student"])
    assert deliver(setup, ctx) == "model_blocked" and len(fake.requests) == 2  # every stream stops
    assert runner.dispatch(ctx.session_factory, None, ctx.settings)[2] == "model_blocked"
    learner = CurrentUser(id=setup["student"], role="student", status="active")
    dashboard = make_api(settings_overrides=PINS).as_user(learner)
    topics = dashboard.client.get(f"/api/v1/courses/{setup['course']}/dashboard").json()["topics"]
    assert all(t["prediction"] is None for t in topics)  # incompatible predictions are hidden
    fake.tamper = None
    admin_user = setup["world"].user("admin")
    assert admin.clear_model_block(ctx.session_factory, admin_user.id, "identity_reviewed") == "cleared"
    assert deliver(setup, ctx) == "completed"
    assert fake.requests[2]["request_id"] == fake.requests[1]["request_id"] == str(batch)  # the same durable batch


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: {**b, "request_id": str(uuid.uuid4())},
        lambda b: {**b, "learner_ref": "0" * 64},
        lambda b: {**b, "scores": [{**b["scores"][0], "mastery": 1.5}]},
        lambda b: {**b, "applied_event_ids": []},
    ],
)
def test_ordinary_invalid_responses_stay_failures_of_one_batch(setup, fake, mutate):
    attempts(setup, 0, 1)
    fake.tamper = mutate
    ctx = make_ctx(setup["engine"], fake)
    assert deliver(setup, ctx) == "retry"
    assert rows(setup["engine"], "SELECT status FROM mastery_model_state")[0].status == "compatible"
    st = stream(setup["engine"], setup["student"])
    assert st.failure_count == 1 and st.last_error_code.startswith("protocol_") and st.batch_id is not None


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"model": None}, "protocol_model_malformed"),
        ({"model": {}}, "protocol_model_malformed"),
        ({"model": {"name": "ai2-bkt"}}, "protocol_model_malformed"),
        ({"model": {"name": "ai2-bkt", "version": 140}}, "protocol_model_malformed"),
        ({"state_epoch": "not-a-uuid"}, "protocol_state_epoch_malformed"),
        ({"protocol": None}, "protocol_protocol_malformed"),
        ({"mock": None}, "protocol_mock_malformed"),
        ({"mock": "true"}, "protocol_mock_malformed"),
        # V4: a terminal newline makes an identity malformed, never a deliberate change
        ({"protocol": "studygraph.mastery/2\n"}, "protocol_protocol_malformed"),
        ({"state_epoch": "0b9e8f5e-1f3a-4c55-9d0a-6f1d2c3b4a5a\n"}, "protocol_state_epoch_malformed"),
        ({"model": {"name": "other-model\n", "version": "1.4.0"}}, "protocol_model_malformed"),
        ({"model": {"name": "ai2-bkt", "version": "1.5.0\n"}}, "protocol_model_malformed"),
    ],
)
def test_malformed_identity_fields_fail_one_batch_without_blocking_the_model(setup, fake, change, reason):
    """V3: a malformed identity field establishes no identity change: per-batch failure, never a global block."""
    e = setup["engine"]
    attempts(setup, 0, 1)
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "completed"
    scores_before = rows(e, "SELECT topic_id, through_seq, batch_id, received_at FROM mastery_scores WHERE user_id = :u",
                         u=setup["student"])  # fmt: skip
    attempts(setup, 0, 1)
    fake.tamper = lambda body: {**body, **change}
    assert deliver(setup, ctx) == "retry"
    assert rows(e, "SELECT status, reason FROM mastery_model_state")[0] == ("compatible", None)
    st = stream(e, setup["student"])
    assert (st.state, st.failure_count, st.last_error_code, st.claim_token) == ("ready", 1, reason, None)
    assert st.batch_id is not None and str(st.batch_id) == fake.requests[1]["request_id"]  # the durable batch is kept
    assert {r.status for r in rows(e, "SELECT status FROM mastery_outbox WHERE batch_id = :b", b=st.batch_id)} == {"batched"}
    assert rows(e, "SELECT topic_id, through_seq, batch_id, received_at FROM mastery_scores WHERE user_id = :u",
                u=setup["student"]) == scores_before  # nothing of the rejected response was stored  # fmt: skip
    fake.tamper = None
    make_due(e, setup["student"])
    assert deliver(setup, ctx) == "completed"  # a healthy model goes on, with the same batch
    assert fake.requests[2]["request_id"] == fake.requests[1]["request_id"]


def _newline_event(body):
    return {**body, "applied_event_ids": [body["applied_event_ids"][0] + "\n", *body["applied_event_ids"][1:]]}


def _newline_topic(body):
    return {**body, "scores": [{**body["scores"][0], "topic_id": body["scores"][0]["topic_id"] + "\n"}]}


@pytest.mark.parametrize("mutate, reason", [(_newline_event, "protocol_event_accounting"),
                                            (_newline_topic, "protocol_score_topics")])  # fmt: skip
def test_malformed_response_uuids_fail_one_batch_and_store_nothing(setup, fake, mutate, reason):
    """A UUID with a terminal newline is a protocol violation of this batch, never an uncaught error."""
    e = setup["engine"]
    attempts(setup, 0, 1)
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "completed"
    scores_before = rows(e, "SELECT topic_id, through_seq, batch_id, received_at FROM mastery_scores WHERE user_id = :u",
                         u=setup["student"])  # fmt: skip
    attempts(setup, 0, 1)
    fake.tamper = mutate
    assert deliver(setup, ctx) == "retry"
    assert rows(e, "SELECT status, reason FROM mastery_model_state")[0] == ("compatible", None)
    st = stream(e, setup["student"])
    assert (st.state, st.failure_count, st.last_error_code, st.claim_token) == ("ready", 1, reason, None)
    assert str(st.batch_id) == fake.requests[1]["request_id"]
    assert {r.status for r in rows(e, "SELECT status FROM mastery_outbox WHERE batch_id = :b", b=st.batch_id)} == {"batched"}
    assert rows(e, "SELECT topic_id, through_seq, batch_id, received_at FROM mastery_scores WHERE user_id = :u",
                u=setup["student"]) == scores_before  # fmt: skip
    fake.tamper = None
    make_due(e, setup["student"])
    assert deliver(setup, ctx) == "completed"
    assert fake.requests[2]["request_id"] == fake.requests[1]["request_id"]


# --- an expired claim changes nothing (V2, H1) -----------------------------------------------------------------


def _claimed(setup, fake):
    ctx = make_ctx(setup["engine"], fake)
    pins = pins_of(ctx.settings)
    delivery.record_probe(ctx.session_factory, delivery.probe_started_at(ctx.session_factory), "compatible", None,
                          pins, "ai-service")  # fmt: skip
    claim = delivery.claim(ctx.session_factory, setup["student"], setup["course"], ctx.settings, pins, 100)
    assert isinstance(claim, delivery.Claim)
    return ctx, claim


def _snapshot(setup):
    st = stream(setup["engine"], setup["student"])
    model = rows(setup["engine"], "SELECT status, reason FROM mastery_model_state")[0]
    outbox = sorted((r.event_id, r.status) for r in rows(setup["engine"], "SELECT event_id, status FROM mastery_outbox"))
    return (st.state, st.claim_token, st.batch_id, st.failure_count, st.last_error_code, st.next_attempt_at,
            st.lease_expires_at, tuple(model), tuple(outbox))  # fmt: skip


@pytest.mark.parametrize("exit_path", ["pause", "fail", "block", "block_model", "suspend"])
def test_an_expired_claim_cannot_change_the_stream_or_the_model(setup, fake, exit_path):
    attempts(setup, 0, 2)
    ctx, claim = _claimed(setup, fake)
    expire_lease(setup["engine"], setup["student"])  # recovery has not run yet
    before = _snapshot(setup)
    outcome = {
        "pause": lambda: delivery.pause(ctx.session_factory, claim),
        "fail": lambda: delivery.fail(ctx.session_factory, claim, ctx.settings, "upstream_http_500"),
        "block": lambda: delivery.block(ctx.session_factory, claim, "request_too_large"),
        "block_model": lambda: delivery.block(ctx.session_factory, claim, "ai_state_mismatch",
                                              model_reason="ai_state_mismatch"),  # fmt: skip
        "suspend": lambda: delivery.suspend_for_model(ctx.session_factory, claim, "response_model_mismatch"),
    }[exit_path]()
    assert outcome == "stale"
    assert _snapshot(setup) == before
    assert before[7] == ("compatible", None)  # the global model state was not touched
    assert delivery.recover(ctx.session_factory, ctx.settings, 10) == (1, 0)  # recovery counts the failure once
    assert stream(setup["engine"], setup["student"]).failure_count == 1


def test_a_late_exit_of_an_old_claim_never_overwrites_a_newer_claim(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 1)
    ctx, old = _claimed(setup, fake)
    expire_lease(e, setup["student"])
    delivery.recover(ctx.session_factory, ctx.settings, 10)
    make_due(e, setup["student"])
    new = delivery.claim(ctx.session_factory, setup["student"], setup["course"], ctx.settings, pins_of(ctx.settings), 100)
    assert new.batch_id == old.batch_id and new.token != old.token
    for late in (lambda: delivery.fail(ctx.session_factory, old, ctx.settings, "upstream_timeout"),
                 lambda: delivery.block(ctx.session_factory, old, "ai_state_mismatch", model_reason="ai_state_mismatch"),
                 lambda: delivery.suspend_for_model(ctx.session_factory, old, "response_model_mismatch"),
                 lambda: delivery.pause(ctx.session_factory, old)):  # fmt: skip
        assert late() == "stale"
    st = stream(e, setup["student"])
    assert (st.state, st.claim_token, st.failure_count) == ("delivering", new.token, 1)
    assert rows(e, "SELECT status FROM mastery_model_state")[0].status == "compatible"


def test_a_late_state_mismatch_after_lease_expiry_changes_nothing(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 2)
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "completed"
    attempts(setup, 0, 1)
    fake.mode = "forget"  # the service lost its state ...
    fake.on_request = lambda: expire_lease(e, setup["student"])  # ... and answers only after the lease expired
    assert deliver(setup, ctx) == "stale"
    assert rows(e, "SELECT status FROM mastery_model_state")[0].status == "compatible"
    st = stream(e, setup["student"])
    assert (st.state, st.failure_count, st.last_error_code) == ("delivering", 0, None)
    assert rows(e, "SELECT through_seq FROM mastery_scores WHERE user_id = :u", u=setup["student"])[0].through_seq == 2
    assert delivery.recover(ctx.session_factory, ctx.settings, 10) == (1, 0)


@pytest.mark.parametrize("late", ["http_500", "identity", "pause"])
def test_late_failures_identity_changes_and_pauses_after_lease_expiry_change_nothing(setup, fake, monkeypatch, late):
    e = setup["engine"]
    attempts(setup, 0, 1)
    if late == "pause":
        original, calls = delivery.has_access, []

        def revoked_after_expiry(session, user_id, course_id):
            calls.append(1)
            if len(calls) == 1:
                return original(session, user_id, course_id)
            expire_lease(e, setup["student"])
            return False

        monkeypatch.setattr(delivery, "has_access", revoked_after_expiry)
    else:
        fake.on_request = lambda: expire_lease(e, setup["student"])
        if late == "http_500":
            fake.mode = "http_500"
        else:
            fake.tamper = lambda body: {**body, "model": {"name": "ai2-bkt", "version": "9.9.9"}}
    assert deliver(setup, make_ctx(e, fake)) == "stale"
    st = stream(e, setup["student"])
    assert (st.state, st.failure_count, st.last_error_code) == ("delivering", 0, None)
    assert rows(e, "SELECT status FROM mastery_model_state")[0].status == "compatible"


def test_an_older_sequence_never_overwrites_a_newer_prediction(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 1)
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "completed"
    attempts(setup, 0, 1)
    with e.begin() as c:  # a newer prediction than the batch can produce is already stored
        c.execute(text("UPDATE mastery_scores SET through_seq = 5, attempts_seen = 5, attempts = 5 WHERE user_id = :u"),
                  {"u": setup["student"]})  # fmt: skip
    assert deliver(setup, ctx) == "blocked"
    assert stream(e, setup["student"]).last_error_code == "sequence_mismatch"
    assert rows(e, "SELECT through_seq FROM mastery_scores WHERE user_id = :u", u=setup["student"])[0].through_seq == 5


# --- access ---------------------------------------------------------------------------------------------------------


def test_revoked_access_pauses_delivery_and_restored_access_resumes_it(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 1)
    with e.begin() as c:
        c.execute(text("DELETE FROM enrollments WHERE user_id = :u"), {"u": setup["student"]})
    ctx = make_ctx(e, fake)
    assert deliver(setup, ctx) == "paused" and fake.requests == []
    assert delivery.resume_paused(ctx.session_factory, 10) == 0
    setup["world"].enroll(type("U", (), {"id": setup["student"]})(), setup["course"])
    assert delivery.resume_paused(ctx.session_factory, 10) == 1
    assert deliver(setup, ctx) == "completed"


def test_access_is_checked_again_right_before_sending(setup, fake, monkeypatch):
    attempts(setup, 0, 1)
    calls = []
    original = delivery.has_access

    def revoked_after_claim(session, user_id, course_id):
        calls.append(1)
        return original(session, user_id, course_id) if len(calls) == 1 else False

    monkeypatch.setattr(delivery, "has_access", revoked_after_claim)
    ctx = make_ctx(setup["engine"], fake)
    assert deliver(setup, ctx) == "paused" and fake.requests == [] and len(calls) == 2
    assert stream(setup["engine"], setup["student"]).batch_id is not None  # the batch is kept, unsent


# --- dispatch ----------------------------------------------------------------------------------------------------------


class Publisher:
    def __init__(self, fail_after: int | None = None):
        self.sent, self.fail_after = [], fail_after

    def mastery_queue_depth(self) -> int:
        return 0

    def publish_mastery(self, user_id, course_id):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise PublishError("down")
        self.sent.append({"user_id": str(user_id), "course_id": str(course_id)})


def test_dispatch_publishes_identifiers_only_and_releases_on_failure(setup, fake):
    e = setup["engine"]
    attempts(setup, 0, 1)
    ctx = make_ctx(e, fake)
    publisher = Publisher()
    assert runner.dispatch(ctx.session_factory, publisher, ctx.settings) == (1, 0, None)
    assert publisher.sent == [{"user_id": str(setup["student"]), "course_id": str(setup["course"])}]
    assert runtime.stream_of((), publisher.sent[0]) == (setup["student"], setup["course"])
    assert runtime.stream_of((), {**publisher.sent[0], "extra": 1}) is None
    assert runtime.stream_of((), {"user_id": str(setup["student"]).upper(), "course_id": str(setup["course"])}) is None
    assert runner.dispatch(ctx.session_factory, publisher, ctx.settings) == (0, 0, None)  # reserved recently
    with e.begin() as c:
        c.execute(text("UPDATE mastery_streams SET last_dispatched_at = NULL"))
    assert runner.dispatch(ctx.session_factory, Publisher(fail_after=0), ctx.settings) == (0, 1, None)
    assert stream(e, setup["student"]).last_dispatched_at is None
    assert stream(e, setup["student"]).dispatch_count == 2 and stream(e, setup["student"]).claim_count == 0
