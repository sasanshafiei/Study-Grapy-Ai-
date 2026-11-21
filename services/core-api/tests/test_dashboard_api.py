"""M8 endpoints on the disposable database: dashboard, analytics and study events (authorization, flags,
calculations, null and time semantics, privacy, idempotency, quotas). No AI service is involved."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.dashboard import analytics, student
from app.mastery import delivery
from app.mastery.pins import pins_of
from tests.m8_support import PINS, graded_attempt, legacy_attempt


@pytest.fixture
def course_setup(world):
    professor = world.user("professor")
    course = world.course(professor, code="CHEM 101")
    other_course = world.course(professor)
    topics = [world.topic(course, "Kinetics"), world.topic(course, "Stoichiometry")]
    other_topic = world.topic(other_course, "Elsewhere")
    learner = world.user("student")
    world.enroll(learner, course)
    world.enroll(learner, other_course)
    return {"world": world, "engine": world.engine, "professor": professor, "course": course, "other_course": other_course,
            "topics": topics, "other_topic": other_topic, "student": learner}  # fmt: skip


@pytest.fixture
def client(make_api, course_setup):
    def build(user=None, **settings):
        return make_api(settings_overrides=settings).as_user(user or course_setup["student"]).client

    return build


def dash(c, course, tz=None, **params):
    query = {"tz": tz, **params} if tz else params
    return c.get(f"/api/v1/courses/{course}/dashboard", params=query)


# --- authorization and flags ------------------------------------------------------------------------------------


def test_dashboard_access_matrix(client, course_setup, world):
    s = course_setup
    outsider, admin, other_prof = world.user("student"), world.user("admin"), world.user("professor")
    disabled = world.user("student", status="disabled")
    world.enroll(disabled, s["course"])
    assert dash(client(), s["course"]).status_code == 200
    for user, status, code in ((s["professor"], 403, "dashboard_not_allowed"), (outsider, 404, "course_not_found"),
                               (admin, 404, "course_not_found"), (other_prof, 404, "course_not_found"),
                               (disabled, 403, "account_disabled")):  # fmt: skip
        response = dash(client(user), s["course"])
        assert (response.status_code, response.json()["code"]) == (status, code)
        assert response.headers["cache-control"] == "no-store"
    assert dash(client(), uuid.uuid4()).status_code == 404
    off = dash(client(dashboard_enabled=False), s["course"])
    assert (off.status_code, off.json()["code"]) == (503, "dashboard_unavailable")
    assert dash(client(user=outsider, dashboard_enabled=False), s["course"]).status_code == 404  # flag after authorization
    assert dash(client(), s["course"], tz="Mars/Base").json()["code"] == "invalid_timezone"
    assert dash(client(), s["course"], user_id=str(outsider.id)).json()["code"] == "invalid_query"


def test_analytics_and_study_events_access_and_default_flags(client, course_setup, world):
    s = course_setup
    path = f"/api/v1/courses/{s['course']}"
    response = client(s["professor"]).get(f"{path}/analytics")
    assert (response.status_code, response.json()["code"]) == (503, "analytics_unavailable")  # off by default
    assert client(s["professor"], analytics_enabled=True).get(f"{path}/analytics").status_code == 200
    assert client(s["student"], analytics_enabled=True).get(f"{path}/analytics").json()["code"] == "analytics_not_allowed"
    for user in (world.user("admin"), world.user("professor"), world.user("student")):
        assert client(user, analytics_enabled=True).get(f"{path}/analytics").status_code == 404
    ranged = client(s["professor"], analytics_enabled=True).get(f"{path}/analytics", params={"from": "2026-01-01"})
    assert ranged.json()["code"] == "invalid_query"
    events = client().post(f"{path}/study-events", json={"events": []})
    assert (events.status_code, events.json()["code"]) == (503, "study_events_unavailable")  # off by default
    prof = client(s["professor"], study_events_enabled=True).post(f"{path}/study-events", json={"events": []})
    assert prof.json()["code"] == "study_events_not_allowed"


def test_access_is_checked_again_before_the_dashboard_is_sent(client, course_setup, monkeypatch):
    s = course_setup
    original = student.read_dashboard

    def revoke_during_read(*args, **kwargs):
        view = original(*args, **kwargs)
        with s["engine"].begin() as c:
            c.execute(text("DELETE FROM enrollments WHERE user_id = :u AND course_id = :c"),
                      {"u": s["student"].id, "c": s["course"]})  # fmt: skip
        return view

    monkeypatch.setattr(student, "read_dashboard", revoke_during_read)
    response = dash(client(), s["course"])
    assert response.status_code == 404 and "topics" not in response.text


# --- calculations ----------------------------------------------------------------------------------------------------


def test_empty_dashboard_uses_null_not_zero(client, course_setup):
    s = course_setup
    body = dash(client(), s["course"]).json()
    assert body["totals"] == {"attempts": 0, "correct": 0, "accuracy": None}
    assert len(body["daily"]) == 30 and all(d["accuracy"] is None and d["attempts"] == 0 for d in body["daily"])
    assert all(t["accuracy"] is None and t["recent"] is None and t["baseline"] is None and t["prediction"] is None
               for t in body["topics"])  # fmt: skip
    assert body["streak"] == {"days": 0, "through": None, "capped": False}
    assert body["activity"]["study_time"] == {"measurement": "client_reported_estimate", "available": False,
                                              "minutes_7d": None, "minutes_30d": None, "by_day": None}  # fmt: skip
    assert body["recommendations"]["items"] == [] and body["recommendations"]["empty_reason"] == "no_material"


def test_dashboard_counts_only_the_callers_graded_attempts_of_this_course(client, course_setup, world):
    s = course_setup
    e, u, (kinetics, stoich) = s["engine"], s["student"].id, s["topics"]
    for correct in (True, False, False, True):
        graded_attempt(e, u, s["course"], kinetics, correct=correct)
    graded_attempt(e, u, s["course"], stoich, correct=False)
    legacy_attempt(e, u, s["course"], kinetics)  # excluded
    graded_attempt(e, u, s["other_course"], s["other_topic"])  # another course
    peer = world.user("student")
    world.enroll(peer, s["course"])
    graded_attempt(e, peer.id, s["course"], kinetics)  # another student
    body = dash(client(), s["course"]).json()
    assert body["totals"] == {"attempts": 5, "correct": 2, "accuracy": 0.4}
    topics = {t["name"]: t for t in body["topics"]}
    assert (topics["Kinetics"]["attempts"], topics["Kinetics"]["accuracy"]) == (4, 0.5)
    assert topics["Kinetics"]["recent"] == {"window": 4, "correct": 2, "accuracy": 0.5}
    assert topics["Kinetics"]["baseline"] == {"kind": "baseline", "method": "cumulative_accuracy/1", "value": 0.5}
    with e.connect() as c:
        stats = c.execute(text("SELECT sum(attempts) FROM topic_practice_stats WHERE user_id = :u AND course_id = :c"),
                          {"u": u, "c": s["course"]}).scalar_one()  # fmt: skip
    assert stats == body["totals"]["attempts"]
    assert sum(d["attempts"] for d in body["daily"]) == 5 and body["daily"][-1]["attempts"] == 5
    assert body["streak"]["days"] == 1 and body["activity"]["active_days_7"] == 1
    assert len(body["recent_activity"]) == 5 and all("answer" not in a for a in body["recent_activity"])
    assert str(peer.id) not in json.dumps(body)


def test_days_follow_the_requested_time_zone_with_a_grace_day_streak(client, course_setup):
    s = course_setup
    e, u, topic = s["engine"], s["student"].id, s["topics"][0]
    now = datetime.now(timezone.utc)
    # 21:30 UTC is 00:30 the next day in Istanbul (UTC+3)
    late = (now - timedelta(days=3)).replace(hour=21, minute=30, second=0, microsecond=0)
    graded_attempt(e, u, s["course"], topic, submitted_at=late)
    graded_attempt(e, u, s["course"], topic, submitted_at=late - timedelta(days=1))
    utc = {d["date"]: d["attempts"] for d in dash(client(), s["course"]).json()["daily"]}
    ist = {d["date"]: d["attempts"] for d in dash(client(), s["course"], tz="Europe/Istanbul").json()["daily"]}
    assert utc[late.date().isoformat()] == 1
    assert ist.get((late.date() + timedelta(days=1)).isoformat()) == 1 and ist.get(late.date().isoformat()) == 1
    graded_attempt(e, u, s["course"], topic, submitted_at=now - timedelta(days=1))
    streak = dash(client(), s["course"]).json()["streak"]  # nothing today yet: counted from yesterday (grace)
    assert streak["days"] == 1 and streak["through"] == (now - timedelta(days=1)).date().isoformat()


def test_dashboard_reads_one_consistent_snapshot(client, course_setup, monkeypatch):
    s = course_setup
    e, u, topic = s["engine"], s["student"].id, s["topics"][0]
    graded_attempt(e, u, s["course"], topic)
    from app.dashboard import materials

    original = materials.class_material

    def concurrent_commit(*args, **kwargs):  # runs between the snapshot's statements
        graded_attempt(e, u, s["course"], topic)
        return original(*args, **kwargs)

    monkeypatch.setattr(materials, "class_material", concurrent_commit)
    body = dash(client(), s["course"]).json()
    assert body["totals"]["attempts"] == 1 and sum(d["attempts"] for d in body["daily"]) == 1
    assert len(body["recent_activity"]) == 1


# --- predictions --------------------------------------------------------------------------------------------------------


def _store_prediction(s, settings_pins, *, through: int, received=None, version="1.4.0", review=True):
    with s["engine"].begin() as c:
        c.execute(
            text("INSERT INTO mastery_scores (user_id, topic_id, course_id, mastery, attempts, next_review_at, protocol, "
                 "model_name, model_version, model_epoch, pseudonym_key_id, through_seq, attempts_seen, batch_id, "
                 "received_at) VALUES (:u, :t, :c, 0.7, :n, :review, 'studygraph.mastery/1', 'ai2-bkt', :v, :e, :k, :n, "
                 ":n, gen_random_uuid(), coalesce(CAST(:at AS timestamptz), now())) ON CONFLICT (user_id, topic_id) DO "
                 "UPDATE SET through_seq = EXCLUDED.through_seq, attempts_seen = EXCLUDED.attempts_seen, "
                 "attempts = EXCLUDED.attempts, received_at = EXCLUDED.received_at, model_version = EXCLUDED.model_version"),  # fmt: skip
            {"u": s["student"].id, "t": s["topics"][0], "c": s["course"], "n": through, "v": version,
             "e": settings_pins.state_epoch, "k": settings_pins.key_id, "at": received,
             "review": datetime.now(timezone.utc) + timedelta(days=2) if review else None},
        )  # fmt: skip


def _confirm_model(s, settings_pins):
    factory = sessionmaker(bind=s["engine"])
    delivery.record_probe(factory, delivery.probe_started_at(factory), "compatible", None, settings_pins, "ai-service")


def test_predictions_need_matching_provenance_a_confirmed_model_and_freshness(client, course_setup):
    s = course_setup
    from app.config import Settings

    p = pins_of(Settings(**PINS))
    for _ in range(2):
        graded_attempt(s["engine"], s["student"].id, s["course"], s["topics"][0])
    _store_prediction(s, p, through=2)

    def prediction(**extra):
        topics = dash(client(**{**PINS, **extra}), s["course"]).json()["topics"]
        return next(t for t in topics if t["topic_id"] == str(s["topics"][0]))["prediction"]

    assert prediction() is None  # the model was never confirmed by a probe
    _confirm_model(s, p)
    shown = prediction()
    assert shown["mastery"] == 0.7 and shown["stale"] is False and shown["next_review_at"] is not None
    assert dash(client(**PINS), s["course"]).json()["review_schedule"][0]["topic_id"] == str(s["topics"][0])
    assert dash(client(), s["course"]).json()["topics"][0]["prediction"] is None  # no pins configured: nothing shown
    assert prediction(mastery_model_version="1.5.0") is None  # pinned version changed
    graded_attempt(s["engine"], s["student"].id, s["course"], s["topics"][0])
    stale = prediction()
    assert stale["stale"] is True and stale["pending_events"] == 1 and stale["next_review_at"] is None
    _store_prediction(s, p, through=3, received=datetime.now(timezone.utc) - timedelta(days=40))
    assert prediction() is None  # too old
    with s["engine"].begin() as c:
        c.execute(text("UPDATE mastery_model_state SET status = 'incompatible', reason = 'ai_state_mismatch'"))
    _store_prediction(s, p, through=3)
    assert prediction() is None  # a blocked model hides every prediction


def test_pausing_delivery_keeps_validated_fresh_predictions_visible(client, course_setup):
    """V2 (M1): MASTERY_DELIVERY_ENABLED=false pauses delivery; it does not hide stored, valid predictions."""
    s = course_setup
    from app.config import Settings

    p = pins_of(Settings(**PINS))
    graded_attempt(s["engine"], s["student"].id, s["course"], s["topics"][0])
    _store_prediction(s, p, through=1)
    _confirm_model(s, p)
    paused = client(**PINS, mastery_delivery_enabled=False)

    def prediction():
        topics = dash(paused, s["course"]).json()["topics"]
        return next(t for t in topics if t["topic_id"] == str(s["topics"][0]))["prediction"]

    shown = prediction()
    assert shown is not None and shown["stale"] is False and shown["next_review_at"] is not None
    graded_attempt(s["engine"], s["student"].id, s["course"], s["topics"][0])  # committed while nothing is delivered
    stale = prediction()
    assert stale["stale"] is True and stale["pending_events"] == 1 and stale["next_review_at"] is None
    _store_prediction(s, p, through=1, received=datetime.now(timezone.utc) - timedelta(days=31))
    assert prediction() is None  # the age guard still applies


def test_legacy_scores_are_never_shown_as_model_output(client, course_setup):
    s = course_setup
    graded_attempt(s["engine"], s["student"].id, s["course"], s["topics"][0])
    with s["engine"].begin() as c:
        c.execute(text("INSERT INTO mastery_scores (user_id, topic_id, course_id, mastery, attempts, next_review_at) "
                       "VALUES (:u, :t, :c, 0.99, 1, now() + interval '1 day')"),
                  {"u": s["student"].id, "t": s["topics"][0], "c": s["course"]})  # fmt: skip
    body = dash(client(**PINS), s["course"]).json()
    assert all(t["prediction"] is None for t in body["topics"]) and body["review_schedule"] == []


# --- recommendations and visibility ----------------------------------------------------------------------------------------


def _link(world, engine, document, course, topic, *, origin="class"):
    chunk = world.chunk(document, course, f"Passage about the topic {uuid.uuid4().hex}", origin=origin)
    with engine.begin() as c:
        c.execute(text("INSERT INTO chunk_topics (chunk_id, topic_id) VALUES (:c, :t)"), {"c": chunk, "t": topic})


def _external(world, engine, course, topic, *, enabled=True, stale=False, status="indexed"):
    source = world._one(
        "INSERT INTO sources (kind, enabled, provider, language, licence_url) VALUES ('wikipedia', :e, 'wikipedia', "
        "'en', 'https://creativecommons.org/licenses/by-sa/4.0/') RETURNING id", e=enabled)
    world.sources.append(source)
    resource = world._one(
        "INSERT INTO external_resources (course_id, source_id, page_id, title, canonical_url, state) VALUES "
        "(:c, :s, :p, 'Reaction rate', 'https://en.wikipedia.org/wiki/Reaction_rate', 'accepted') RETURNING id",
        c=course, s=source, p=int(uuid.uuid4().int % 10**9) + 1)  # fmt: skip
    document = world.document(course, origin="external", source_id=source, status=status, title="Reaction rate",
                              url="https://en.wikipedia.org/wiki/Reaction_rate", external_resource_id=resource)  # fmt: skip
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO external_document_versions (document_id, resource_id, version, page_id, revision_id, "
                 "canonical_url, revision_url, licence, licence_url, attribution, content_sha256, language, fetched_at, "
                 "stale_reason, staled_at) VALUES (:d, :r, 1, 1, 1, 'https://en.wikipedia.org/wiki/Reaction_rate', "
                 "'https://en.wikipedia.org/w/index.php?oldid=1', 'CC BY-SA 4.0', "
                 "'https://creativecommons.org/licenses/by-sa/4.0/', 'Wikipedia contributors', repeat('c', 64), 'en', "
                 "now(), :reason, :staled)"),  # fmt: skip
            {"d": document, "r": resource, "reason": "superseded" if stale else None, "staled": datetime.now(timezone.utc) if stale else None},
        )
    _link(world, engine, document, course, topic, origin="external")
    return document, resource


def test_recommendations_use_real_visible_material_and_own_evidence(client, course_setup, world):
    s = course_setup
    e, u, kinetics = s["engine"], s["student"].id, s["topics"][0]
    for correct in (False, False, True):
        graded_attempt(e, u, s["course"], kinetics, correct=correct)
    lecture = world.document(s["course"], status="indexed", title="Lecture 4.pdf")
    _link(world, e, lecture, s["course"], kinetics)
    hidden = world.document(s["course"], status="stale", title="Old notes.pdf")
    _link(world, e, hidden, s["course"], kinetics)
    foreign = world.document(s["other_course"], status="indexed", title="Other course.pdf")
    _link(world, e, foreign, s["other_course"], s["other_topic"])
    external, resource = _external(world, e, s["course"], kinetics)
    # M6: a replacement version is pending; the older indexed version stays visible
    with e.begin() as c:
        pending = c.execute(text("INSERT INTO documents (course_id, origin, title, language, status, source_id, "
                                 "external_resource_id) SELECT course_id, 'external', title, 'en', 'pending', source_id, "
                                 "external_resource_id FROM documents WHERE id = :d RETURNING id"), {"d": external}).scalar_one()  # fmt: skip
        c.execute(text("UPDATE external_resources SET current_document_id = :p WHERE id = :r"), {"p": pending, "r": resource})
    world.documents.append(pending)
    body = dash(client(), s["course"]).json()["recommendations"]
    items = body["items"]
    assert [i["type"] for i in items] == ["review_topic", "class_material", "external_resource"]
    assert items[0]["reason"]["evidence"]["window"] == 3 and items[0]["evidence_strength"] == "limited"
    assert items[1]["target"]["document_id"] == str(lecture)
    assert items[2]["target"]["url"] == "https://en.wikipedia.org/wiki/Reaction_rate"
    assert items[2]["target"]["licence"] == "CC BY-SA 4.0" and items[2]["target"]["document_id"] == str(external)
    with_quiz = dash(client(quiz_enabled=True), s["course"]).json()["recommendations"]["items"]
    assert [i["type"] for i in with_quiz] == ["review_topic", "class_material", "practice_quiz"]
    assert [i["type"] for i in dash(client(quiz_enabled=True, quiz_generation_enabled=False), s["course"])
            .json()["recommendations"]["items"]] == ["review_topic", "class_material", "external_resource"]  # fmt: skip
    with e.begin() as c:  # the source is disabled: the external page disappears
        c.execute(text("UPDATE sources SET enabled = false WHERE id = (SELECT source_id FROM documents WHERE id = :d)"),
                  {"d": external})  # fmt: skip
    assert "external_resource" not in [i["type"] for i in dash(client(), s["course"]).json()["recommendations"]["items"]]


def test_cold_start_recommends_material_of_unpractised_topics(client, course_setup, world):
    s = course_setup
    lecture = world.document(s["course"], status="indexed", title="Lecture 1.pdf")
    _link(world, s["engine"], lecture, s["course"], s["topics"][1])
    items = dash(client(), s["course"]).json()["recommendations"]["items"]
    assert [(i["type"], i["reason"]["code"], i["evidence_strength"]) for i in items] == [
        ("class_material", "not_practised", "none")
    ]


def test_dashboard_works_without_any_ai_service(client, course_setup):
    s = course_setup
    graded_attempt(s["engine"], s["student"].id, s["course"], s["topics"][0])
    response = dash(client(ai_service_url="http://127.0.0.1:9", mastery_delivery_enabled=False), s["course"])
    assert response.status_code == 200 and response.json()["totals"]["attempts"] == 1


# --- analytics -------------------------------------------------------------------------------------------------------------


def _week_ago_monday() -> date:
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=today.isoweekday() - 1) - timedelta(days=14)


def _cohort(s, world, n: int, week: date, *, pattern=(True, False, True)):
    students = []
    for i in range(n):
        learner = world.user("student")
        world.enroll(learner, s["course"])
        students.append(learner)
        when = datetime(week.year, week.month, week.day, 10, tzinfo=timezone.utc) + timedelta(hours=i)
        for j, correct in enumerate(pattern[: 1 + i % len(pattern)] + (i % 2 == 0,)):
            graded_attempt(s["engine"], learner.id, s["course"], s["topics"][j % 2], correct=correct,
                           submitted_at=when + timedelta(minutes=j))  # fmt: skip
    return students


def _publish(s, **settings):
    from app.config import Settings

    factory = sessionmaker(bind=s["engine"])
    return analytics.publish_due_releases(factory, Settings(analytics_enabled=True, **settings))


def _week(body, week):
    return next(w for w in body["weeks"] if w["week_start"] == week.isoformat())


def test_weekly_releases_are_thresholded_immutable_and_never_per_student(client, course_setup, world):
    s = course_setup
    with s["engine"].begin() as c:  # the course existed for the whole window
        c.execute(text("UPDATE courses SET created_at = now() - interval '200 days' WHERE id = :c"), {"c": s["course"]})
    week = _week_ago_monday()
    small_week = week - timedelta(days=7)
    cohort = _cohort(s, world, 6, week)
    _cohort(s, world, 4, small_week)
    prof = client(s["professor"], analytics_enabled=True)
    pending = prof.get(f"/api/v1/courses/{s['course']}/analytics").json()
    assert _week(pending, week)["status"] == "pending_release" and _week(pending, week)["overall"] is None
    assert _publish(s) >= 2 and _publish(s) == 0  # bounded, once
    body = prof.get(f"/api/v1/courses/{s['course']}/analytics").json()
    released = _week(body, week)
    assert released["status"] == "released" and released["overall"]["status"] == "released"
    assert released["overall"]["contributors"] == "5-9" and released["overall"]["attempts"] in ("5-24", "25-99")
    assert _week(body, small_week)["overall"] == {"status": "withheld", "accuracy": None, "contributors": None,
                                                   "attempts": None}  # fmt: skip
    current_monday = datetime.now(timezone.utc).date() - timedelta(days=datetime.now(timezone.utc).date().isoweekday() - 1)
    assert all(w["week_start"] != current_monday.isoformat() for w in body["weeks"])
    raw = json.dumps(body)
    assert all(str(learner.id) not in raw for learner in cohort)
    assert '"correct"' not in raw and body["item_analysis"]["available"] is False
    # late commit into the released week, and a student leaving: the published week does not change
    graded_attempt(s["engine"], cohort[0].id, s["course"], s["topics"][0], correct=False,
                   submitted_at=datetime(week.year, week.month, week.day, 23, tzinfo=timezone.utc))  # fmt: skip
    with s["engine"].begin() as c:
        c.execute(text("DELETE FROM enrollments WHERE user_id = :u"), {"u": cohort[1].id})
    _publish(s)
    again = prof.get(f"/api/v1/courses/{s['course']}/analytics").json()
    assert _week(again, week) == released


def test_one_heavy_student_is_not_a_group(client, course_setup, world):
    s = course_setup
    with s["engine"].begin() as c:
        c.execute(text("UPDATE courses SET created_at = now() - interval '200 days' WHERE id = :c"), {"c": s["course"]})
    week = _week_ago_monday()
    when = datetime(week.year, week.month, week.day, 9, tzinfo=timezone.utc)
    for i in range(6):
        graded_attempt(s["engine"], s["student"].id, s["course"], s["topics"][0], correct=i % 2 == 0,
                       submitted_at=when + timedelta(minutes=i))  # fmt: skip
    _publish(s)
    body = client(s["professor"], analytics_enabled=True).get(f"/api/v1/courses/{s['course']}/analytics").json()
    assert _week(body, week)["overall"]["status"] == "withheld"


def test_analytics_off_generates_nothing(course_setup):
    from app.config import Settings

    assert analytics.publish_due_releases(sessionmaker(bind=course_setup["engine"]), Settings()) == 0


# --- study events ------------------------------------------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _focus(start: datetime, minutes: int, **extra):
    return {"client_event_id": str(uuid.uuid4()), "type": "focus_interval", "started_at": _iso(start),
            "ended_at": _iso(start + timedelta(minutes=minutes)), "surface": "document", "topic_id": None,
            "document_id": None, **extra}  # fmt: skip


def post(c, course, events):
    return c.post(f"/api/v1/courses/{course}/study-events", json={"events": events})


def test_study_events_are_idempotent_atomic_and_validated(client, course_setup, world):
    s = course_setup
    c = client(study_events_enabled=True)
    now = datetime.now(timezone.utc)
    event = _focus(now - timedelta(minutes=30), 10)
    first = post(c, s["course"], [event])
    assert first.status_code == 200 and first.json()["accepted"] == 1
    replay = post(c, s["course"], [event]).json()
    assert (replay["accepted"], replay["duplicates"], replay["results"][0]["status"]) == (0, 1, "duplicate")
    conflict = post(c, s["course"], [_focus(now - timedelta(minutes=5), 1), {**event, "surface": "quiz"}])
    assert (conflict.status_code, conflict.json()["code"]) == (409, "event_id_conflict")
    with s["engine"].connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM study_events WHERE user_id = :u"),
                            {"u": s["student"].id}).scalar_one() == 1  # nothing of the 409 batch was stored  # fmt: skip
    for bad in (_focus(now - timedelta(hours=25), 5),  # too old
                _focus(now + timedelta(minutes=10), 5),  # in the future
                _focus(now - timedelta(hours=24, minutes=10), 15),  # started_at too old, ended_at fine
                _focus(now - timedelta(minutes=50), 40),  # longer than 30 minutes
                _focus(now - timedelta(minutes=5), 1, topic_id=str(s["other_topic"])),  # another course's topic
                {"client_event_id": str(uuid.uuid4()), "type": "document_viewed", "occurred_at": _iso(now),
                 "document_id": str(uuid.uuid4())}):  # an unknown document  # fmt: skip
        response = post(c, s["course"], [bad])
        assert response.status_code == 422, bad
    hidden = world.document(s["course"], status="pending", title="Draft.pdf")
    visible = world.document(s["course"], status="indexed", title="Lecture.pdf")
    view = {"client_event_id": str(uuid.uuid4()), "type": "document_viewed", "occurred_at": _iso(now)}
    assert post(c, s["course"], [{**view, "document_id": str(hidden)}]).status_code == 422
    assert post(c, s["course"], [{**view, "document_id": str(visible)}]).status_code == 200


def test_the_daily_quota_holds_across_concurrent_requests_and_replays_are_free(make_api, course_setup):
    s = course_setup
    apis = [make_api(settings_overrides={"study_events_enabled": True, "study_events_daily_quota": 6})
            .as_user(s["student"]) for _ in range(3)]  # three "processes"  # fmt: skip
    now = datetime.now(timezone.utc)
    batches = [[_focus(now - timedelta(minutes=60 - 3 * (b * 3 + i)), 1) for i in range(3)] for b in range(3)]
    outcome: dict[int, int] = {}

    def send(index, api, batch):
        outcome[index] = post(api.client, s["course"], batch).status_code

    threads = [threading.Thread(target=send, args=(i, api, batch)) for i, (api, batch) in enumerate(zip(apis, batches))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcome.values()) == [200, 200, 429]
    with s["engine"].connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM study_events WHERE user_id = :u"),
                            {"u": s["student"].id}).scalar_one() == 6  # fmt: skip
    accepted = batches[next(i for i, status in outcome.items() if status == 200)]
    assert post(apis[0].client, s["course"], accepted).status_code == 200  # a replay consumes no quota


def test_study_time_is_the_union_of_intervals_split_by_local_day(client, course_setup):
    s = course_setup
    c = client(study_events_enabled=True)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    base = now - timedelta(hours=3)
    events = [_focus(base, 20), _focus(base + timedelta(minutes=10), 20),  # two devices overlapping: 30 minutes
              _focus(base + timedelta(hours=1), 5)]  # fmt: skip
    assert post(c, s["course"], events).status_code == 200
    study = dash(c, s["course"]).json()["activity"]["study_time"]
    assert study["available"] is True and study["minutes_30d"] == 35 and study["minutes_7d"] == 35
    assert sum(d["minutes"] or 0 for d in study["by_day"]) == 35
    assert dash(client(), s["course"]).json()["activity"]["study_time"]["minutes_30d"] is None  # flag off: not shown
    streak = dash(c, s["course"]).json()["streak"]
    assert streak["days"] == 0  # telemetry never counts as activity


def test_retention_removes_only_expired_events(course_setup):
    from app.config import Settings
    from app.dashboard import study_events

    s = course_setup
    with s["engine"].begin() as c:
        c.execute(text("INSERT INTO study_events (user_id, course_id, client_event_id, fingerprint, event_type, occurred_at, "
                       "started_at, ended_at, surface, received_at) VALUES (:u, :c, gen_random_uuid(), repeat('a', 64), "
                       "'focus_interval', now() - interval '200 days', now() - interval '200 days 5 minutes', "
                       "now() - interval '200 days', 'quiz', now() - interval '200 days'), (:u, :c, gen_random_uuid(), "
                       "repeat('b', 64), 'focus_interval', now(), now() - interval '5 minutes', now(), 'quiz', now())"),
                  {"u": s["student"].id, "c": s["course"]})  # fmt: skip
    assert study_events.cleanup(sessionmaker(bind=s["engine"]), Settings()) == 1


def test_unexpected_errors_are_generic_and_never_log_parameters(client, course_setup, monkeypatch, caplog):
    from sqlalchemy.exc import StatementError

    secret = "answer-key-" + uuid.uuid4().hex

    def boom(*args, **kwargs):
        raise StatementError("failed", f"SELECT '{secret}'", {"p": secret}, Exception(secret))

    monkeypatch.setattr(student, "read_dashboard", boom)
    with caplog.at_level("ERROR"):
        response = dash(client(), course_setup["course"])
    assert (response.status_code, response.json()["code"]) == (500, "server_error")
    assert response.headers["cache-control"] == "no-store"
    assert secret not in caplog.text and secret not in response.text



def test_recommendation_ids_are_validated_in_full_before_anything_is_stored(client, course_setup):
    """Final review: an id with a terminal newline (17 characters) is a 422, never a database error."""
    s = course_setup
    c = client(study_events_enabled=True, study_events_daily_quota=2)
    now = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))

    def rec(rec_id, **extra):
        return {"client_event_id": str(uuid.uuid4()), "type": "recommendation_shown", "occurred_at": now,
                "recommendation_id": rec_id, "recommendation_type": "practice_quiz", "topic_id": str(s["topics"][0]),
                "target_document_id": None, **extra}  # fmt: skip

    for bad in ("a" * 16 + "\n", "a" * 17, "a" * 15, "A" * 16, "z" * 16, None, 7):
        response = post(c, s["course"], [rec("0123456789abcdef"), rec(bad)])  # the valid event is not stored either
        assert (response.status_code, response.json()["code"]) == (422, "invalid_study_event"), bad
        assert response.json()["index"] == 1
    with s["engine"].connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM study_events WHERE user_id = :u"),
                            {"u": s["student"].id}).scalar_one() == 0  # fmt: skip
    good = [rec("0123456789abcdef"), rec("fedcba9876543210")]
    first = post(c, s["course"], good)  # the quota of 2 is still entirely available
    assert first.status_code == 200 and first.json()["accepted"] == 2
    replay = post(c, s["course"], good).json()
    assert (replay["accepted"], replay["duplicates"]) == (0, 2)
    with s["engine"].connect() as conn:
        stored = conn.execute(text("SELECT recommendation_id FROM study_events WHERE user_id = :u ORDER BY 1"),
                              {"u": s["student"].id}).scalars().all()  # fmt: skip
    assert stored == ["0123456789abcdef", "fedcba9876543210"]
