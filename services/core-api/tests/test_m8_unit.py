"""M8 rules that need no infrastructure: pins and pseudonyms, the mastery protocol and client, time zones,
analytics cells, recommendations, study-event parsing and configuration."""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app import schema_checks
from app.config import Settings
from app.dashboard import analytics, recommendations, study_events
from app.dashboard.materials import Material
from app.dashboard.timezones import InvalidTimezone, days_ending_today, parse_timezone
from app.errors import ApiError
from app.mastery import protocol
from app.mastery.client import AiMasteryClient, encode_request
from app.mastery.pins import pins_of
from tests.m8_support import PIN_KEY, PINS, TOKEN, FakeMastery, raw_response

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0007_dashboard_analytics.py"


def settings(**overrides) -> Settings:
    return Settings(**{**PINS, **overrides})


def pins(**overrides):
    return pins_of(settings(**overrides))


# --- configuration ---------------------------------------------------------------------------------------


def test_m8_flag_defaults():
    s = Settings()
    assert s.dashboard_enabled is True
    assert (s.analytics_enabled, s.study_events_enabled, s.mastery_delivery_enabled, s.ai_recommendations_enabled) == (
        False, False, False, False,
    )  # fmt: skip
    assert (s.quiz_enabled, s.harvest_enabled, s.chat_enabled, s.indexing_enabled) == (False, False, False, False)
    assert s.analytics_min_group_size == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"analytics_min_group_size": 4},
        {"analytics_publication_delay_seconds": 60},
        {"ai_recommendations_enabled": True},
        {"mastery_queue": "ingestion.maintenance"},
        {"mastery_delivery_enabled": True, "mastery_pseudonym_key": None},
        {"mastery_delivery_enabled": True, "mastery_state_epoch": None},
        {"mastery_state_epoch": "0B9E8F5E-1F3A-4C55-9D0A-6F1D2C3B4A59"},
        {"mastery_pseudonym_key": SecretStr("short")},
        {"mastery_pseudonym_namespace": "Has Spaces"},
        {"mastery_model_version": "1.0 beta"},
        {"mastery_task_soft_time_limit_seconds": 20},
        {"study_events_retention_days": 1},
    ],
)
def test_invalid_m8_settings_are_refused(overrides):
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_pins_are_complete_or_absent_and_never_show_the_key():
    assert pins_of(Settings()) is None
    p = pins()
    assert PIN_KEY not in repr(p) and "key=set" in repr(p)
    assert PIN_KEY not in repr(settings())


def test_pseudonyms_are_keyed_namespaced_and_stable():
    user, course = uuid.uuid4(), uuid.uuid4()
    a, b = pins(), pins(mastery_pseudonym_namespace="studygraph-prod")
    c = pins(mastery_pseudonym_key=SecretStr("z" * 48))
    assert a.learner_ref(user, course) == pins().learner_ref(user, course)
    assert len({a.learner_ref(user, course), b.learner_ref(user, course), c.learner_ref(user, course)}) == 3
    assert a.learner_ref(user, course) != a.learner_ref(user, uuid.uuid4())
    assert str(user) not in a.learner_ref(user, course) and re.fullmatch(r"[0-9a-f]{64}", a.course_ref(course))
    assert re.fullmatch(r"[0-9a-f]{16}", a.key_id) and a.key_id != c.key_id and a.key_id != b.key_id


def test_schema_checks_are_verbatim_copies_of_the_migration():
    source = MIGRATION.read_text()
    namespace: dict = {}
    exec(compile(source.split("UPGRADE_PREFLIGHT = ")[0], str(MIGRATION), "exec"), namespace)  # constants only
    # Revision 0008 (M9) appended its own constants; they are checked against migration 0008 (test_migration_0008, MIG-04).
    m9_start = Path(schema_checks.__file__).read_text().index("# --- Revision 0008 (M9")
    m9_names = {n for n in dir(schema_checks) if n.isupper() and Path(schema_checks.__file__).read_text().find(f"\n{n} = ") > m9_start}
    assert m9_names and "QUESTION_BANK_CHECK" in m9_names and "STREAM_STATE_CHECK" not in m9_names
    for name in dir(schema_checks):
        if name.isupper() and name not in m9_names:
            assert namespace[name] == getattr(schema_checks, name), name


# --- capabilities -----------------------------------------------------------------------------------------


def test_only_the_exact_pinned_non_mock_model_is_compatible():
    p = pins()
    fake = FakeMastery()
    assert protocol.validate_capabilities(200, fake.capabilities(), p).status == "compatible"
    for change, reason in (
        ({"mock": True}, "mock_service"), ({"mock": None}, "mock_service"), ({"mock": "false"}, "mock_service"),
        ({"mock": 0}, "mock_service"), ({"model": {"name": "ai2-bkt", "version": "1.5.0"}}, "model_mismatch"),
        ({"model": {"name": "other", "version": "1.4.0"}}, "model_mismatch"), ({"state_epoch": str(uuid.uuid4())}, "epoch_mismatch"),
        ({"ordering": "global"}, "ordering_unsupported"), ({"protocol": "studygraph.mastery/2"}, "protocol_mismatch"),
        ({"max_events": True}, "capabilities_shape"), ({"max_events": 0}, "capabilities_shape"),
    ):  # fmt: skip
        result = protocol.validate_capabilities(200, {**fake.capabilities(), **change}, p)
        assert (result.status, result.reason) == ("incompatible", reason), change
    missing = fake.capabilities()
    del missing["mock"]
    assert protocol.validate_capabilities(200, missing, p).reason == "capabilities_shape"
    assert protocol.validate_capabilities(503, None, p).status == "unavailable"


# --- requests and responses ----------------------------------------------------------------------------------


def _events(topic_a, topic_b):
    t0 = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    return [
        protocol.BatchEvent(uuid.uuid4(), topic_a, 1, "mcq", "easy", True, 0, 800, t0 + timedelta(seconds=5)),
        protocol.BatchEvent(uuid.uuid4(), topic_a, 2, "mcq", "easy", False, 1, None, t0),  # earlier start, later seq
        protocol.BatchEvent(uuid.uuid4(), topic_b, 1, "true_false", "hard", True, 0, 100, t0 + timedelta(seconds=1)),
    ]


def test_request_carries_only_pseudonymous_minimal_fields_in_topic_order():
    p, user, course = pins(), uuid.uuid4(), uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    events = _events(a, b)
    request = protocol.build_request(uuid.uuid4(), user, course, events, p)
    raw = encode_request(request).decode()
    assert str(user) not in raw and str(course) not in raw
    assert set(request) == {"protocol", "request_id", "state_epoch", "pseudonym_namespace", "course_ref", "learner_ref",
                            "events"}  # fmt: skip
    assert set(request["events"][0]) == {"event_id", "topic_id", "topic_seq", "qtype", "difficulty", "correct",
                                         "hint_used", "response_ms", "telemetry_trusted", "submitted_at"}  # fmt: skip
    assert all(e["telemetry_trusted"] is False for e in request["events"])
    seqs_a = [e["topic_seq"] for e in request["events"] if e["topic_id"] == str(a)]
    assert seqs_a == [1, 2]  # per-topic order wins over the approximate time order
    assert request == protocol.build_request(uuid.UUID(request["request_id"]), user, course, list(reversed(events)), p)


def _response(request, events, **changes):
    fake = FakeMastery()
    body = fake._update(request)
    body.update(changes)
    return body


def test_valid_response_is_accepted_and_lost_state_is_detected():
    p, a, b = pins(), uuid.uuid4(), uuid.uuid4()
    events = _events(a, b)
    request = protocol.build_request(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), events, p)
    update = protocol.validate_update(_response(request, events), request, events, p)
    assert len(update.applied) == 3 and not update.duplicate
    assert {s.topic_id: s.through_seq for s in update.scores} == {a: 2, b: 1}
    lost = _response(request, events)
    lost["scores"][0]["attempts_seen"] = 0
    with pytest.raises(protocol.StateMismatch):
        protocol.validate_update(lost, request, events, p)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(mock=None),
        lambda b: b.update(request_id=str(uuid.uuid4())),
        lambda b: b.update(state_epoch=str(uuid.uuid4())),
        lambda b: b.update(learner_ref="0" * 64),
        lambda b: b.update(model={"name": "ai2-bkt", "version": "9"}),
        lambda b: b.update(applied_event_ids=b["applied_event_ids"][:-1]),  # an event not accounted for
        lambda b: b.update(duplicate_event_ids=b["applied_event_ids"][:1]),  # in both lists
        lambda b: b.update(applied_event_ids=b["applied_event_ids"] + b["applied_event_ids"][:1]),  # repeated
        lambda b: b.update(applied_event_ids=b["applied_event_ids"][:-1] + [str(uuid.uuid4())]),  # foreign
        lambda b: b["scores"].pop(),
        lambda b: b["scores"].append(dict(b["scores"][0])),
        lambda b: b["scores"][0].update(topic_id=str(uuid.uuid4())),
        lambda b: b["scores"][0].update(mastery=1.5),
        lambda b: b["scores"][0].update(mastery=-0.1),
        lambda b: b["scores"][0].update(mastery=float("nan")),
        lambda b: b["scores"][0].update(mastery=float("inf")),
        lambda b: b["scores"][0].update(mastery=True),
        lambda b: b["scores"][0].update(mastery="0.5"),
        lambda b: b["scores"][0].update(through_seq=b["scores"][0]["through_seq"] + 1),
        lambda b: b["scores"][0].update(attempts_seen=b["scores"][0]["attempts_seen"] + 1),
        lambda b: b["scores"][0].update(attempts_seen=True),
        lambda b: b["scores"][0].update(next_review_at="2026-12-01T09:00:00"),  # no offset
        lambda b: b["scores"][0].update(next_review_at="tomorrow"),
        lambda b: b["scores"][0].update(extra=1),
        lambda b: b.update(extra=1),
    ],
)
def test_invalid_responses_are_rejected_as_a_whole(mutate):
    p, a, b = pins(), uuid.uuid4(), uuid.uuid4()
    events = _events(a, b)
    request = protocol.build_request(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), events, p)
    body = _response(request, events)
    mutate(body)
    with pytest.raises(protocol.ProtocolViolation):
        protocol.validate_update(body, request, events, p)


def test_duplicates_are_accepted_when_fully_accounted():
    p, a, b = pins(), uuid.uuid4(), uuid.uuid4()
    events = _events(a, b)
    request = protocol.build_request(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), events, p)
    body = _response(request, events)
    body["duplicate_event_ids"], body["applied_event_ids"] = body["applied_event_ids"], []
    update = protocol.validate_update(body, request, events, p)
    assert not update.applied and len(update.duplicate) == 3


def test_review_time_bounds_use_the_given_database_time():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    ok = protocol.ValidatedScore(uuid.uuid4(), 0.5, 1, now + timedelta(days=30))
    protocol.check_review_bounds((ok,), now)
    for when in (now - timedelta(days=2), now + timedelta(days=366)):
        with pytest.raises(protocol.ProtocolViolation):
            protocol.check_review_bounds((protocol.ValidatedScore(uuid.uuid4(), 0.5, 1, when),), now)
    assert protocol.parse_review_time("2026-12-01T12:00:00+03:00") == datetime(2026, 12, 1, 9, tzinfo=timezone.utc)


# --- client transport ---------------------------------------------------------------------------------------


def _client(handler, **overrides) -> AiMasteryClient:
    import httpx

    s = settings(ai_service_token=SecretStr(TOKEN), ai_service_url="http://ai.test:8000", **overrides)
    return AiMasteryClient(s, transport=httpx.MockTransport(handler))


def test_client_probe_is_authenticated_cached_and_refuses_redirects_and_mock():
    fake = FakeMastery()
    client = _client(fake.handler)
    assert client.probe_sync(pins()).status == "compatible"
    assert client.probe_sync(pins()).status == "compatible" and fake.probes == 1
    fake.mock = True
    assert _client(fake.handler).probe_sync(pins()).reason == "mock_service"
    redirect = _client(lambda r: raw_response(302, b"", location="http://elsewhere/"))
    assert redirect.probe_sync(pins()).status == "unavailable"
    assert TOKEN not in repr(client)


def test_client_update_is_sent_once_and_bounded():
    from app.mastery.client import MasteryUpstreamError

    calls = []

    def huge(request):
        calls.append(request)
        return raw_response(200, b"{" + b" " * 70_000 + b"}")

    with pytest.raises(MasteryUpstreamError) as exc:
        _client(huge).update_sync(b"{}")
    assert exc.value.code == "response_too_large" and len(calls) == 1

    def dup_keys(request):
        return raw_response(200, b'{"a": 1, "a": 2}')

    with pytest.raises(MasteryUpstreamError) as exc:
        _client(dup_keys).update_sync(b"{}")
    assert exc.value.code == "invalid_json"


def test_encoded_requests_refuse_nan():
    with pytest.raises(ValueError):
        encode_request({"x": math.nan})


# --- time zones -------------------------------------------------------------------------------------------------


def test_time_zone_names_are_validated():
    assert parse_timezone(None)[0] == "UTC"
    assert parse_timezone("Europe/Istanbul")[0] == "Europe/Istanbul"
    for bad in ("localtime", "Factory", "posix/Europe/Berlin", "../etc/passwd", "Mars/Base", "", "x" * 65, 5):
        with pytest.raises(InvalidTimezone):
            parse_timezone(bad)


def test_local_days_follow_dst_and_midnight():
    _, ny = parse_timezone("America/New_York")
    spring = days_ending_today(datetime(2026, 3, 9, 12, tzinfo=timezone.utc), ny, 2)
    assert spring[0].day == date(2026, 3, 8) and spring[0].end - spring[0].start == timedelta(hours=23)
    autumn = days_ending_today(datetime(2026, 11, 2, 12, tzinfo=timezone.utc), ny, 2)
    assert autumn[0].day == date(2026, 11, 1) and autumn[0].end - autumn[0].start == timedelta(hours=25)
    _, istanbul = parse_timezone("Europe/Istanbul")
    today = days_ending_today(datetime(2026, 11, 20, 21, 30, tzinfo=timezone.utc), istanbul, 1)[0]
    assert today.day == date(2026, 11, 21)  # 00:30 in Istanbul is already the next day
    assert today.start == datetime(2026, 11, 20, 21, tzinfo=timezone.utc)
    _, havana = parse_timezone("America/Havana")  # spring-forward gap at midnight: the day starts at 01:00
    gap = days_ending_today(datetime(2026, 3, 8, 12, tzinfo=timezone.utc), havana, 1)[0]
    assert gap.start.astimezone(havana).hour == 1 and gap.end - gap.start == timedelta(hours=23)
    days = days_ending_today(datetime(2026, 11, 20, tzinfo=timezone.utc), ny, 30)
    assert len(days) == 30 and all(a.end == b.start for a, b in zip(days, days[1:]))


# --- analytics cells ------------------------------------------------------------------------------------------------


def _students(*pairs):
    return {uuid.uuid4(): pair for pair in pairs}


def test_cells_need_five_distinct_students_and_spread():
    four = _students((4, 2), (4, 3), (4, 1), (4, 2))
    assert analytics.cell(four, 5)[0] == {"status": "withheld"}
    five = _students((4, 2), (4, 3), (4, 1), (4, 2), (4, 4))
    public = analytics.cell(five, 5)[0]
    assert public == {"status": "released", "accuracy": 0.6, "contributors": "5-9", "attempts": "5-24"}
    assert analytics.cell(five, 6)[0]["status"] == "withheld"


def test_dominance_homogeneity_and_extremes_are_withheld_without_a_reason():
    dominant = _students((30, 15), (2, 1), (2, 0), (2, 2), (2, 1))
    homogeneous = _students((2, 1), (2, 1), (2, 1), (2, 1), (2, 2))
    extreme = _students((10, 0), (10, 0), (10, 1), (10, 0), (10, 0))
    top_two = _students((10, 5), (10, 4), (1, 1), (1, 0), (1, 1))
    for case in (dominant, homogeneous, extreme, top_two):
        assert analytics.cell(case, 5)[0] == {"status": "withheld"}


def test_complementary_protection_and_no_exact_counts():
    topics = [uuid.uuid4() for _ in range(3)]
    rows = []
    for i in range(6):
        user = uuid.uuid4()
        rows.append((user, topics[0], 4, i % 4 + 0))
        rows.append((user, topics[1], 3, (i + 1) % 3))
    rows.append((uuid.uuid4(), topics[2], 3, 1))  # one student alone in topic 3
    payload = analytics.build_payload(rows, topics, 5, False)
    statuses = [t["cell"]["status"] for t in payload["topics"]]
    assert payload["overall"]["status"] == "released"
    assert statuses.count("withheld") >= 2  # the lone topic and one complement
    text_payload = json.dumps(payload)
    assert all(str(r[0]) not in text_payload for r in rows)
    assert '"students"' not in text_payload and '"correct"' not in text_payload


def test_withheld_overall_withholds_every_topic():
    topic = uuid.uuid4()
    rows = [(uuid.uuid4(), topic, 3, 3) for _ in range(6)]  # everyone perfect: extreme and homogeneous
    payload = analytics.build_payload(rows, [topic], 5, False)
    assert payload["overall"] == {"status": "withheld"} and payload["topics"][0]["cell"] == {"status": "withheld"}


def test_published_weeks_exclude_the_current_week_and_honour_the_delay():
    s = Settings(analytics_weeks=2)
    monday_early = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)  # within the 6 h delay after the week ended
    assert analytics.published_weeks(monday_early, s) == [date(2026, 11, 2), date(2026, 10, 26)]
    monday_late = datetime(2026, 11, 16, 7, tzinfo=timezone.utc)
    assert analytics.published_weeks(monday_late, s) == [date(2026, 11, 9), date(2026, 11, 2)]


# --- recommendations ---------------------------------------------------------------------------------------------------


def _topic(name, n=0, correct=0, last=None):
    ids = tuple(uuid.uuid4() for _ in range(n))
    return recommendations.TopicEvidence(uuid.uuid4(), name, n, n, correct, ids, last)


def test_weak_topics_need_minimum_evidence_and_rank_deterministically():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    weak, weaker, thin, fine = _topic("B", 5, 2, now), _topic("A", 4, 1, now), _topic("C", 2, 0, now), _topic("D", 5, 4, now)
    doc = Material(uuid.uuid4(), "Lecture.pdf", "class", 3)
    result = recommendations.build([fine, thin, weak, weaker], {weak.topic_id: doc}, {}, quiz_available=False)
    kinds = [(i["topic_name"], i["type"]) for i in result["items"]]
    assert kinds == [("A", "review_topic"), ("B", "review_topic"), ("B", "class_material")]
    assert result["items"][0]["evidence_strength"] == "limited" and result["items"][1]["evidence_strength"] == "moderate"
    again = recommendations.build([weaker, weak, thin, fine], {weak.topic_id: doc}, {}, quiz_available=False)
    assert [i["id"] for i in again["items"]] == [i["id"] for i in result["items"]]
    assert all("mastery" not in i["reason"]["text"].lower() for i in result["items"])


def test_quiz_recommendations_only_when_quizzes_can_be_started():
    weak = _topic("A", 3, 0)
    assert "practice_quiz" not in [i["type"] for i in recommendations.build([weak], {}, {}, False)["items"]]
    items = recommendations.build([weak], {}, {}, True)["items"]
    assert items[1]["type"] == "practice_quiz" and items[1]["target"]["difficulty"] == "easy"


def test_cold_start_uses_material_and_is_bounded():
    topics = [_topic(f"T{i}") for i in range(5)]
    docs = {t.topic_id: Material(uuid.uuid4(), f"Doc {i}.pdf", "class", i + 1) for i, t in enumerate(topics)}
    result = recommendations.build(topics, docs, {}, quiz_available=False)
    assert len(result["items"]) == 3 and [i["topic_name"] for i in result["items"]] == ["T4", "T3", "T2"]
    assert recommendations.build(topics, {}, {}, False)["empty_reason"] == "no_material"


# --- study-event parsing --------------------------------------------------------------------------------------------------


def _focus(**changes):
    event = {"client_event_id": str(uuid.uuid4()), "type": "focus_interval", "started_at": "2026-11-20T10:00:00Z",
             "ended_at": "2026-11-20T10:20:00+00:00", "surface": "document", "topic_id": None, "document_id": None}  # fmt: skip
    event.update(changes)
    return event


@pytest.mark.parametrize(
    "event",
    [
        _focus(type="browser_history"),
        _focus(url="https://example.com"),
        _focus(started_at="2026-11-20T10:00:00"),  # naive
        _focus(ended_at="2026-11-20T09:59:00Z"),  # before start
        _focus(ended_at="2026-11-20T11:00:01Z"),  # longer than 30 minutes
        _focus(surface="anywhere"),
        _focus(client_event_id="NOT-A-UUID"),
        _focus(topic_id="x"),
        {"client_event_id": str(uuid.uuid4()), "type": "recommendation_opened", "occurred_at": "2026-11-20T10:00:00Z",
         "recommendation_id": "XYZ", "recommendation_type": "review_topic", "topic_id": None, "target_document_id": None},
    ],
)
def test_invalid_events_reject_the_batch(event):
    with pytest.raises(ApiError) as exc:
        study_events.parse_batch({"events": [_focus(), event]}, Settings())
    assert exc.value.status_code == 422


def test_batch_limits_duplicate_ids_and_fingerprints():
    with pytest.raises(ApiError):
        study_events.parse_batch({"events": [_focus() for _ in range(26)]}, Settings())
    same = _focus()
    with pytest.raises(ApiError) as exc:
        study_events.parse_batch({"events": [same, dict(same)]}, Settings())
    assert exc.value.code == "duplicate_event_ids"
    course = uuid.uuid4()
    a = study_events.parse_batch({"events": [_focus(client_event_id=same["client_event_id"])]}, Settings())[0]
    b = study_events.parse_batch(
        {"events": [_focus(client_event_id=same["client_event_id"], started_at="2026-11-20T13:00:00+03:00")]}, Settings()
    )[0]
    assert a.fingerprint(course) == b.fingerprint(course)  # the same instant, another offset
    assert a.fingerprint(course) != a.fingerprint(uuid.uuid4())


# --- worker role, start-up checks, task safety ---------------------------------------------------------------------


def test_the_mastery_queue_needs_a_dedicated_worker_and_its_own_checks():
    from app.ingestion import runtime

    s = Settings()
    assert runtime.worker_role({s.mastery_queue}, s) == runtime.ROLE_MASTERY
    with pytest.raises(runtime.PreflightError, match="dedicated worker"):
        runtime.worker_role({s.mastery_queue, s.maintenance_queue}, s)
    base = {"DATABASE_URL": "postgresql+psycopg://u:p@db:5432/x", "REDIS_URL": "redis://redis:6379/0"}
    with pytest.raises(runtime.PreflightError, match="AI_SERVICE_URL"):
        runtime.mastery_worker_preflight(s, base)
    enabled = settings(mastery_delivery_enabled=True)
    with pytest.raises(runtime.PreflightError, match="AI_SERVICE_TOKEN"):
        runtime.mastery_worker_preflight(enabled, {**base, "AI_SERVICE_URL": "http://ai-service:8000"})
    assert runtime.REQUIRED_SCHEMA_REVISION == "0010"


def test_m8_task_failures_are_logged_without_parameters(caplog):
    from sqlalchemy.exc import StatementError

    from app import worker

    secret = "learner-" + uuid.uuid4().hex

    def boom():
        raise StatementError("failed", f"SELECT '{secret}'", {"p": secret}, Exception(secret))

    with caplog.at_level("ERROR"):
        worker._safely("mastery.deliver_stream", boom)
    assert "error_class=StatementError" in caplog.text and secret not in caplog.text


def test_mastery_messages_carry_identifiers_only():
    from app.ingestion import runtime

    u, c = uuid.uuid4(), uuid.uuid4()
    assert runtime.stream_of((), {"user_id": str(u), "course_id": str(c)}) == (u, c)
    for bad in ({"user_id": str(u)}, {"user_id": str(u), "course_id": str(c), "payload": "x"},
                {"user_id": 1, "course_id": str(c)}, {"user_id": str(u).upper(), "course_id": str(c)}):  # fmt: skip
        assert runtime.stream_of((), bad) is None
    assert runtime.stream_of((str(u),), {}) is None


def test_stored_error_codes_always_satisfy_the_check():
    from app.mastery.delivery import safe_code

    for raw in ("upstream_transport_ConnectError", "protocol_" + "x" * 60, "__", "5xx", "probe_http_503"):
        code = safe_code(raw)
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,39}", code), (raw, code)


# --- V3: malformed identity fields versus explicit identity changes -------------------------------------------------

_IDENTITY_CASES = [
    # malformed: no identity is established -> ordinary per-batch failure
    (lambda b: b.update(model=None), "model_malformed"),
    (lambda b: b.update(model={}), "model_malformed"),
    (lambda b: b.update(model={"name": "ai2-bkt"}), "model_malformed"),  # version missing
    (lambda b: b.update(model={"name": "ai2-bkt", "version": 140}), "model_malformed"),
    (lambda b: b.update(model={"name": ["ai2-bkt"], "version": "1.4.0"}), "model_malformed"),
    (lambda b: b.update(model={"name": "ai2 bkt!", "version": "1.4.0"}), "model_malformed"),
    (lambda b: b.update(model={"name": "ai2-bkt", "version": "1.4.0", "extra": 1}), "model_malformed"),
    (lambda b: b.update(model="ai2-bkt/1.4.0"), "model_malformed"),
    (lambda b: b.update(state_epoch=None), "state_epoch_malformed"),
    (lambda b: b.update(state_epoch="not-a-uuid"), "state_epoch_malformed"),
    (lambda b: b.update(state_epoch=b["state_epoch"].upper()), "state_epoch_malformed"),
    (lambda b: b.update(protocol=None), "protocol_malformed"),
    (lambda b: b.update(protocol="mastery"), "protocol_malformed"),
    (lambda b: b.update(protocol=1), "protocol_malformed"),
    (lambda b: b.update(mock=None), "mock_malformed"),
    (lambda b: b.update(mock="true"), "mock_malformed"),
    (lambda b: b.update(mock="false"), "mock_malformed"),
    (lambda b: b.update(mock=0), "mock_malformed"),
    (lambda b: b.update(mock=1), "mock_malformed"),
    (lambda b: b.update(protocol=b["protocol"] + "\n"), "protocol_malformed"),  # V4: terminal newline
    (lambda b: b.update(protocol="studygraph.mastery/2\n"), "protocol_malformed"),
    (lambda b: b.update(state_epoch=str(uuid.uuid4()) + "\n"), "state_epoch_malformed"),
    (lambda b: b.update(model={"name": "other-model\n", "version": "1.4.0"}), "model_malformed"),
    (lambda b: b.update(model={"name": "ai2-bkt", "version": "1.5.0\n"}), "model_malformed"),
    # explicit, well-formed identity change -> global suspension
    (lambda b: b.update(model={"name": "other-model", "version": "1.4.0"}), "model_mismatch"),
    (lambda b: b.update(model={"name": "ai2-bkt", "version": "1.5.0"}), "model_mismatch"),
    (lambda b: b.update(state_epoch=str(uuid.uuid4())), "state_epoch_mismatch"),
    (lambda b: b.update(protocol="studygraph.mastery/2"), "protocol_mismatch"),
    (lambda b: b.update(mock=True), "mock_response"),
]


@pytest.mark.parametrize("mutate, reason", _IDENTITY_CASES)
def test_identity_fields_are_classified_strictly(mutate, reason):
    from app.mastery.runner import IDENTITY_VIOLATIONS

    p, a, b = pins(), uuid.uuid4(), uuid.uuid4()
    events = _events(a, b)
    request = protocol.build_request(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), events, p)
    body = _response(request, events)
    mutate(body)
    with pytest.raises(protocol.ProtocolViolation) as exc:
        protocol.validate_update(body, request, events, p)
    assert exc.value.reason == reason
    assert (reason in IDENTITY_VIOLATIONS) == reason.endswith(("_mismatch", "mock_response"))


# --- final: UUIDs inside the response must match in full (terminal newline) ------------------------------------


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda b: b.update(applied_event_ids=[b["applied_event_ids"][0] + "\n", *b["applied_event_ids"][1:]]),
         "event_accounting"),
        (lambda b: b.update(duplicate_event_ids=[b["applied_event_ids"].pop() + "\n"]), "event_accounting"),
        (lambda b: b["scores"][0].update(topic_id=b["scores"][0]["topic_id"] + "\n"), "score_topics"),
    ],
)  # fmt: skip
def test_response_uuids_with_a_terminal_newline_are_protocol_violations(mutate, reason):
    p, a, b = pins(), uuid.uuid4(), uuid.uuid4()
    events = _events(a, b)
    request = protocol.build_request(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), events, p)
    body = _response(request, events)
    mutate(body)
    with pytest.raises(protocol.ProtocolViolation) as exc:  # never a bare ValueError
        protocol.validate_update(body, request, events, p)
    assert exc.value.reason == reason


# --- final review: recommendation ids must match in full -----------------------------------------------------------


def _rec_event(rec_id):
    return {"client_event_id": str(uuid.uuid4()), "type": "recommendation_opened", "occurred_at": "2026-11-20T10:00:00Z",
            "recommendation_id": rec_id, "recommendation_type": "review_topic", "topic_id": None,
            "target_document_id": None}  # fmt: skip


@pytest.mark.parametrize(
    "rec_id",
    ["a" * 16 + "\n", "0123456789abcdef\n", "a" * 17, "a" * 15, "", "A" * 16, "0123456789ABCDEF", "g" * 16,
     "aaaaaaaa-aaaaaaa", " " + "a" * 15, "a" * 16 + " ", None, 1234567890123456, ["a" * 16], {"id": "a" * 16}],
)  # fmt: skip
def test_recommendation_ids_must_be_exactly_16_lower_case_hex(rec_id):
    with pytest.raises(ApiError) as exc:
        study_events.parse_batch({"events": [_rec_event(rec_id)]}, Settings())
    assert (exc.value.status_code, exc.value.code) == (422, "invalid_study_event")


def test_a_valid_recommendation_id_is_accepted_unchanged():
    event = study_events.parse_batch({"events": [_rec_event("0123456789abcdef")]}, Settings())[0]
    assert event.recommendation_id == "0123456789abcdef" and event.kind == "recommendation_opened"
