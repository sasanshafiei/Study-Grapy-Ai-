"""GET /courses/{id}/dashboard: one student's own learning data, computed in ONE consistent snapshot.

Source of truth: committed graded M7 attempts (quiz_item_id IS NOT NULL). Legacy attempts
(no assignment, no grading snapshot) and exam attempts (never assignable: quiz_items.purpose
is CHECKed to 'practice') are excluded. All reads happen in one REPEATABLE READ READ ONLY
transaction with statement and lock timeouts; as_of is that transaction's database time.
A fixed number of bounded statements runs (no N+1).

Null semantics: no measurement means null, never 0. accuracy is correct / attempts and
null when attempts = 0; counters may be 0. A topic without attempts has accuracy, recent,
baseline and last_attempt_at null.

Model predictions (`prediction`) are shown only when complete provenance matches the
pinned protocol, model, version, state epoch and pseudonym key, the last capability probe
confirmed that model, and the prediction is younger than MASTERY_PREDICTION_MAX_AGE_DAYS.
A legacy mastery_scores row is never shown. pending_events counts committed attempts the
prediction does not cover yet (stale when > 0); next_review_at (and review_schedule) only
for a fresh prediction that covers every committed attempt of the topic. Without AI (no
pins, or a model that was never confirmed) every prediction is null and the rest of the
dashboard is unchanged.

Delivery switched off (MASTERY_DELIVERY_ENABLED=false) is a PAUSE of event delivery, not a
withdrawal of the model: predictions that were already validated and stored stay visible
under every guard above (pins, confirmed model, provenance, age), and become `stale` with
`pending_events` as new attempts are committed while nothing is delivered; their review time
disappears as soon as one attempt is not covered, and they disappear entirely after
MASTERY_PREDICTION_MAX_AGE_DAYS. Hiding predictions is the job of the model state
(`incompatible` hides everything) or of changing the pins, not of the delivery flag.

Study time is a client-reported estimate (study_events, focus intervals): the length of
the union of the reported intervals per local day (overlaps across devices counted once,
split at local midnight, never beyond as_of). It is null when STUDY_EVENTS_ENABLED is off
or nothing was reported, and it never feeds grading, mastery, streaks or analytics.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser
from ..config import Settings
from ..mastery.pins import PROTOCOL, pins_of
from ..quiz.access import short_tx
from ..quiz.views import BASELINE_METHOD
from . import materials, recommendations
from .access import dashboard_not_allowed, require_student
from .timezones import Day, days_ending_today

HISTORY_DAYS = 30
STREAK_LOOKBACK = 366  # days before today
MAX_TOPICS = 200
RECENT_ACTIVITY = 10
RECENT_WINDOW = 10

_GRADED = "a.user_id = :u AND a.course_id = :c AND a.quiz_item_id IS NOT NULL"


def _ratio(correct: int, attempts: int) -> float | None:
    return None if attempts == 0 else round(correct / attempts, 4)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _day_arrays(days: list[Day]) -> dict:
    return {"days": [d.day for d in days], "starts": [d.start for d in days], "ends": [d.end for d in days]}


_BUCKETS = "unnest(CAST(:days AS date[]), CAST(:starts AS timestamptz[]), CAST(:ends AS timestamptz[])) AS b(day, s, e)"


def _daily(session: Session, params: dict, days: list[Day]) -> list[dict]:
    rows = session.execute(
        text(
            f"SELECT b.day, count(a.id) AS n, coalesce(sum(CASE WHEN a.is_correct THEN 1 ELSE 0 END), 0) AS k "
            f"FROM {_BUCKETS} LEFT JOIN attempts a ON {_GRADED} AND a.submitted_at >= b.s AND a.submitted_at < b.e "
            "GROUP BY b.day ORDER BY b.day"
        ),
        {**params, **_day_arrays(days)},
    ).all()
    return [{"date": r.day.isoformat(), "attempts": int(r.n), "correct": int(r.k), "accuracy": _ratio(int(r.k), int(r.n))}
            for r in rows]  # fmt: skip


def _active_days(session: Session, params: dict, days: list[Day]) -> set:
    return set(
        session.execute(
            text(
                f"SELECT b.day FROM {_BUCKETS} WHERE EXISTS (SELECT 1 FROM attempts a WHERE {_GRADED} "
                "AND a.submitted_at >= b.s AND a.submitted_at < b.e)"
            ),
            {**params, **_day_arrays(days)},
        ).scalars()
    )


def _streak(active: set, days: list[Day]) -> dict:
    today = days[-1].day
    index = len(days) - 1 if today in active else len(days) - 2  # grace: today may still come
    count = 0
    while index >= 0 and days[index].day in active:
        count += 1
        index -= 1
    through = None
    if count:
        through = (today if today in active else today - timedelta(days=1)).isoformat()
    return {"days": count, "through": through, "capped": count > 0 and index < 0}


def _study_time(session: Session, params: dict, days: list[Day], as_of: datetime) -> dict | None:
    window_start = days[0].start
    rows = session.execute(
        text(
            "WITH merged AS ("
            " SELECT unnest(range_agg(tstzrange(greatest(e.started_at, :ws), least(e.ended_at, :now), '[)'))) AS r"
            " FROM study_events e WHERE e.user_id = :u AND e.course_id = :c AND e.event_type = 'focus_interval'"
            " AND e.occurred_at > :ws AND e.started_at < :now AND greatest(e.started_at, :ws) < least(e.ended_at, :now))"
            f" SELECT b.day, sum(extract(epoch FROM upper(m.r * tstzrange(b.s, b.e)) - lower(m.r * tstzrange(b.s, b.e))))"
            f" AS seconds FROM {_BUCKETS} JOIN merged m ON m.r && tstzrange(b.s, b.e) GROUP BY b.day"
        ),
        {**params, **_day_arrays(days), "ws": window_start, "now": as_of},
    ).all()
    if not rows:
        return None
    seconds = {r.day: float(r.seconds or 0) for r in rows}
    by_day = [{"date": d.day.isoformat(), "minutes": None if d.day not in seconds else round(seconds[d.day] / 60)}
              for d in days]  # fmt: skip
    last7 = [d.day for d in days[-7:]]
    reported7 = [seconds[d] for d in last7 if d in seconds]
    return {
        "minutes_7d": round(sum(reported7) / 60) if reported7 else None,
        "minutes_30d": round(sum(seconds.values()) / 60),
        "by_day": by_day,
    }


def _predictions(session: Session, params: dict, settings: Settings, as_of: datetime, attempts: dict) -> dict:
    pins = pins_of(settings)
    if pins is None:
        return {}
    model = session.execute(
        text("SELECT status, model_name, model_version, state_epoch, pseudonym_key_id FROM mastery_model_state WHERE id = 1")
    ).one_or_none()
    expected = (pins.model_name, pins.model_version, pins.state_epoch, pins.key_id)
    if model is None or model.status != "compatible" or (
        model.model_name, model.model_version, model.state_epoch, model.pseudonym_key_id
    ) != expected:
        return {}
    oldest = as_of - timedelta(days=settings.mastery_prediction_max_age_days)
    rows = session.execute(
        text(
            "SELECT topic_id, mastery, through_seq, received_at, next_review_at, model_name, model_version, "
            "model_epoch, pseudonym_key_id FROM mastery_scores WHERE user_id = :u AND course_id = :c "
            "AND protocol = :p AND received_at IS NOT NULL"
        ),
        {**params, "p": PROTOCOL},
    ).all()
    shown = {}
    for r in rows:
        if (r.model_name, r.model_version, r.model_epoch, r.pseudonym_key_id) != expected or r.received_at < oldest:
            continue
        committed = attempts.get(r.topic_id, 0)
        if r.through_seq > committed:  # covers attempts that do not exist: never shown
            continue
        pending = committed - r.through_seq
        shown[r.topic_id] = {
            "kind": "model",
            "mastery": round(float(r.mastery), 4),
            "model": {"name": r.model_name, "version": r.model_version},
            "through_seq": r.through_seq,
            "pending_events": pending,
            "stale": pending > 0,
            "received_at": _iso(r.received_at),
            "next_review_at": _iso(r.next_review_at) if pending == 0 else None,
        }
    return shown


def compute(session: Session, user: CurrentUser, course_id: uuid.UUID, zone_name: str, zone, settings: Settings) -> dict:
    """The whole dashboard from the caller's open snapshot transaction (already authorized)."""
    params = {"u": user.id, "c": course_id}
    as_of: datetime = session.execute(text("SELECT now()")).scalar_one()
    history = days_ending_today(as_of, zone, HISTORY_DAYS)
    lookback = days_ending_today(as_of, zone, STREAK_LOOKBACK + 1)

    topic_rows = session.execute(
        text("SELECT id, name FROM topics WHERE course_id = :c ORDER BY name, id LIMIT :limit"),
        {"c": course_id, "limit": MAX_TOPICS + 1},
    ).all()
    truncated = len(topic_rows) > MAX_TOPICS
    topic_rows = topic_rows[:MAX_TOPICS]
    per_topic = {
        r.topic_id: r
        for r in session.execute(
            text(
                "SELECT a.topic_id, count(*) AS n, sum(CASE WHEN a.is_correct THEN 1 ELSE 0 END) AS k, "
                f"max(a.submitted_at) AS last_at FROM attempts a WHERE {_GRADED} GROUP BY a.topic_id"
            ),
            params,
        ).all()
    }
    recent: dict[uuid.UUID, list] = {}
    for r in session.execute(
        text(
            "SELECT topic_id, id, is_correct FROM (SELECT a.topic_id, a.id, a.is_correct, a.stats_attempts, "
            "row_number() OVER (PARTITION BY a.topic_id ORDER BY a.stats_attempts DESC) AS rn "
            f"FROM attempts a WHERE {_GRADED}) w WHERE rn <= :window ORDER BY topic_id, stats_attempts DESC"
        ),
        {**params, "window": RECENT_WINDOW},
    ).all():
        recent.setdefault(r.topic_id, []).append(r)

    total_n = sum(int(r.n) for r in per_topic.values())
    total_k = sum(int(r.k) for r in per_topic.values())
    attempts_by_topic = {t: int(r.n) for t, r in per_topic.items()}
    topic_ids = [r.id for r in topic_rows]
    class_docs = materials.class_material(session, course_id, topic_ids)
    external_docs = materials.external_material(session, course_id, topic_ids)
    predictions = _predictions(session, params, settings, as_of, attempts_by_topic)

    topics, evidence = [], []
    for row in topic_rows:
        stats = per_topic.get(row.id)
        n, k = (int(stats.n), int(stats.k)) if stats else (0, 0)
        window = recent.get(row.id, [])
        recent_k = sum(1 for w in window if w.is_correct)
        prediction = predictions.get(row.id)
        topics.append({
            "topic_id": str(row.id),
            "name": row.name,
            "practiced": n > 0,
            "attempts": n,
            "correct": k,
            "accuracy": _ratio(k, n),
            "last_attempt_at": _iso(stats.last_at) if stats else None,
            "recent": {"window": len(window), "correct": recent_k, "accuracy": _ratio(recent_k, len(window))}
            if window else None,
            "baseline": {"kind": "baseline", "method": BASELINE_METHOD, "value": _ratio(k, n)} if n else None,
            "prediction": prediction,
            "has_class_material": row.id in class_docs,
        })  # fmt: skip
        evidence.append(recommendations.TopicEvidence(
            row.id, row.name, n, len(window), recent_k, tuple(w.id for w in window), stats.last_at if stats else None
        ))  # fmt: skip

    activity_rows = session.execute(
        text(
            "SELECT a.id, qi.session_id AS quiz_id, a.topic_id, t.name, a.is_correct, a.submitted_at FROM attempts a "
            "JOIN quiz_items qi ON qi.id = a.quiz_item_id AND qi.user_id = a.user_id "
            f"JOIN topics t ON t.id = a.topic_id AND t.course_id = a.course_id WHERE {_GRADED} "
            "ORDER BY a.submitted_at DESC, a.id DESC LIMIT :limit"
        ),
        {**params, "limit": RECENT_ACTIVITY},
    ).all()
    active = _active_days(session, params, lookback)
    study_time = _study_time(session, params, history, as_of) if settings.study_events_enabled else None
    quiz_available = settings.quiz_enabled and settings.quiz_generation_enabled
    review = sorted(
        ({"topic_id": t["topic_id"], "name": t["name"], "next_review_at": t["prediction"]["next_review_at"]}
         for t in topics if t["prediction"] and t["prediction"]["next_review_at"]),
        key=lambda r: (r["next_review_at"], r["topic_id"]),
    )  # fmt: skip
    last7 = {d.day for d in lookback[-7:]}
    last30 = {d.day for d in lookback[-30:]}
    return {
        "as_of": _iso(as_of),
        "course_id": str(course_id),
        "window": {"tz": zone_name, "days": HISTORY_DAYS, "start": history[0].day.isoformat(),
                   "end": history[-1].day.isoformat()},  # fmt: skip
        "totals": {"attempts": total_n, "correct": total_k, "accuracy": _ratio(total_k, total_n)},
        "topics": topics,
        "topics_truncated": truncated,
        "daily": _daily(session, params, history),
        "recent_activity": [
            {"attempt_id": str(r.id), "quiz_id": str(r.quiz_id), "topic_id": str(r.topic_id), "topic_name": r.name,
             "correct": bool(r.is_correct), "submitted_at": _iso(r.submitted_at)}
            for r in activity_rows
        ],  # fmt: skip
        "streak": _streak(active, lookback),
        "activity": {
            "active_days_7": len(active & last7),
            "active_days_30": len(active & last30),
            "study_time": {
                "measurement": "client_reported_estimate",
                "available": settings.study_events_enabled,
                **(study_time or {"minutes_7d": None, "minutes_30d": None, "by_day": None}),
            },
        },
        "review_schedule": review,
        "recommendations": recommendations.build(evidence, class_docs, external_docs, quiz_available),
    }


def read_dashboard(
    factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID, zone_name: str, zone, settings: Settings
) -> dict:
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_student(session, user, course_id, dashboard_not_allowed)
        return compute(session, user, course_id, zone_name, zone, settings)


def authorize_dashboard(factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID) -> None:
    with short_tx(factory, read_only=True) as session:
        require_student(session, user, course_id, dashboard_not_allowed)
