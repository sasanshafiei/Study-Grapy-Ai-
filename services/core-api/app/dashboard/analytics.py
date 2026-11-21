"""Professor analytics: immutable, privacy-filtered weekly releases (docs/analytics-privacy.md).

Policy analytics_release/1 (fixed; nothing is selectable by the caller):
- Unit: completed UTC ISO weeks [Monday 00:00 UTC, next Monday 00:00 UTC), non-overlapping.
  A week is released only once it ended at least ANALYTICS_PUBLICATION_DELAY_SECONDS ago
  (>= 1 hour); the current week is never released. The endpoint shows the latest
  ANALYTICS_WEEKS such weeks and takes no parameter: no date range, filter or subset.
- Population: historical participation, i.e. every student with a committed graded M7
  attempt (submitted_at) in that week, whether or not still enrolled. Legacy and exam
  attempts are excluded.
- Release: generated once per (course, week) by the maintenance tick (bounded per tick,
  database work only), stored in analytics_weekly_releases and never changed (UNIQUE key,
  INSERT .. ON CONFLICT DO NOTHING, an UPDATE trigger). Repeated requests and later commits
  cannot change a published week; an attempt that commits after its week was released is
  never counted in that release, and never in another week (it is bucketed by submitted_at).
  A missing release is shown as pending; it is never computed live by the endpoint.
- Cells: one overall cell per week and ONE topic breakdown (no difficulty breakdown, no
  cumulative course-to-date totals, no topic x difficulty). A cell is released only if ALL hold:
    * at least ANALYTICS_MIN_GROUP_SIZE (>= 5) distinct contributing students;
    * dominance: no student contributes more than 50 % of the cell's attempts, and no two
      together more than 75 %;
    * no homogeneity: at least two students' per-student accuracies differ from the most
      common value (otherwise everyone's result would be inferable);
    * not extreme: the student-weighted accuracy lies in [0.10, 0.90];
  and then complementary protection: when the overall cell is released and exactly one
  topic with contributors is withheld, the released topic with the fewest contributors is
  withheld as well; when the overall cell is withheld, every topic cell is.
- Released values: accuracy = mean of per-student accuracies (student-weighted, so a heavy
  user does not dominate), rounded to 0.05; contributors and attempts as coarse bands only.
  No exact count, no `correct` count, no per-student row, id, score, answer or question.
- Withheld cells say only {"status": "withheld"}: no count, no specific reason (a reason
  would itself leak, e.g. "extreme").
This is suppression, rounding and banding; it is NOT anonymisation and NOT differential
privacy. Residual risks are documented in docs/analytics-privacy.md.

Item analysis is unavailable: every M7 question belongs to exactly one student (UNIQUE
quiz_items.question_id), so difficulty or point-biserial statistics per item would be
fabricated; grouping questions by stem similarity is refused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..auth import CurrentUser
from ..config import Settings
from ..quiz.access import short_tx
from .access import require_owner

log = logging.getLogger(__name__)

POLICY = "analytics_release/1"
MAX_TOPICS = 200
DOMINANCE_TOP1 = Fraction(1, 2)
DOMINANCE_TOP2 = Fraction(3, 4)
EXTREME_LOW, EXTREME_HIGH = 0.10, 0.90
CONTRIBUTOR_BANDS = ((100, "100+"), (50, "50-99"), (20, "20-49"), (10, "10-19"), (5, "5-9"))
ATTEMPT_BANDS = ((500, "500+"), (100, "100-499"), (25, "25-99"), (5, "5-24"))
WITHHELD = {"status": "withheld"}
ITEM_ANALYSIS = {
    "available": False,
    "reason": "no_shared_items",
    "prerequisite": "A stable shared question identity answered independently by enough students "
    "(not available before a shared question bank, M9).",
}


def _band(value: int, bands) -> str:
    for low, label in bands:
        if value >= low:
            return label
    raise ValueError("below the smallest band")


def _round05(value: float) -> float:
    return round(round(value * 20) / 20, 2)


def cell(per_student: dict, k: int) -> tuple[dict, dict]:
    """(public cell, private facts used only for complementary protection). per_student: id -> (attempts, correct)."""
    students = len(per_student)
    attempts = sum(n for n, _ in per_student.values())
    facts = {"students": students, "attempts": attempts}
    if students < max(k, 5) or attempts == 0:
        return dict(WITHHELD), facts
    loads = sorted((n for n, _ in per_student.values()), reverse=True)
    if Fraction(loads[0], attempts) > DOMINANCE_TOP1 or Fraction(loads[0] + loads[1], attempts) > DOMINANCE_TOP2:
        return dict(WITHHELD), facts
    accuracies = [Fraction(c, n) for n, c in per_student.values()]
    modal = Counter(accuracies).most_common(1)[0][1]
    if students - modal < 2:
        return dict(WITHHELD), facts
    mean = float(sum(accuracies) / students)
    if not EXTREME_LOW <= mean <= EXTREME_HIGH:
        return dict(WITHHELD), facts
    return {
        "status": "released",
        "accuracy": _round05(mean),
        "contributors": _band(students, CONTRIBUTOR_BANDS),
        "attempts": _band(attempts, ATTEMPT_BANDS),
    }, facts


def build_payload(rows: list, topic_ids: list[uuid.UUID], k: int, truncated: bool) -> dict:
    """rows: (user_id, topic_id, attempts, correct) of one course-week."""
    overall: dict = {}
    by_topic: dict[uuid.UUID, dict] = {t: {} for t in topic_ids}
    for user_id, topic_id, n, c in rows:
        prev = overall.get(user_id, (0, 0))
        overall[user_id] = (prev[0] + int(n), prev[1] + int(c))
        if topic_id in by_topic:
            by_topic[topic_id][user_id] = (int(n), int(c))
    overall_cell, _ = cell(overall, k)
    topics = []
    for topic_id in topic_ids:
        public, facts = cell(by_topic[topic_id], k)
        topics.append([topic_id, public, facts])
    if overall_cell["status"] != "released":
        for entry in topics:
            entry[1] = dict(WITHHELD)
    else:
        withheld_with_data = [e for e in topics if e[1]["status"] == "withheld" and e[2]["students"] > 0]
        released = [e for e in topics if e[1]["status"] == "released"]
        if len(withheld_with_data) == 1 and released:
            victim = min(released, key=lambda e: (e[2]["students"], e[2]["attempts"], str(e[0])))
            victim[1] = dict(WITHHELD)
    return {
        "policy": POLICY,
        "overall": overall_cell,
        "topics": [{"topic_id": str(t), "cell": public} for t, public, _ in topics],
        "topics_truncated": truncated,
    }


def monday(value: date) -> date:
    return value - timedelta(days=value.isoweekday() - 1)


def published_weeks(as_of: datetime, settings: Settings) -> list[date]:
    """The latest ANALYTICS_WEEKS completed weeks whose publication time has passed, newest first."""
    delay = timedelta(seconds=settings.analytics_publication_delay_seconds)
    week = monday((as_of - delay).astimezone(timezone.utc).date()) - timedelta(days=7)
    return [week - timedelta(days=7 * i) for i in range(settings.analytics_weeks)]


def _week_bounds(week: date) -> tuple[datetime, datetime]:
    start = datetime(week.year, week.month, week.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=7)


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def generate_release(session: Session, course_id: uuid.UUID, week: date, settings: Settings) -> bool:
    """Compute and insert one release inside the caller's REPEATABLE READ transaction. True if inserted."""
    start, end = _week_bounds(week)
    topic_rows = session.execute(
        text("SELECT id FROM topics WHERE course_id = :c ORDER BY id LIMIT :limit"),
        {"c": course_id, "limit": MAX_TOPICS + 1},
    ).scalars().all()
    truncated = len(topic_rows) > MAX_TOPICS
    rows = session.execute(
        text(
            "SELECT user_id, topic_id, count(*) AS n, sum(CASE WHEN is_correct THEN 1 ELSE 0 END) AS k FROM attempts "
            "WHERE course_id = :c AND quiz_item_id IS NOT NULL AND submitted_at >= :s AND submitted_at < :e "
            "GROUP BY user_id, topic_id"
        ),
        {"c": course_id, "s": start, "e": end},
    ).all()
    payload = build_payload([(r.user_id, r.topic_id, r.n, r.k) for r in rows], list(topic_rows[:MAX_TOPICS]),
                            settings.analytics_min_group_size, truncated)  # fmt: skip
    payload["week_start"] = week.isoformat()
    canonical = _canonical(payload)
    inserted = session.execute(
        text(
            "INSERT INTO analytics_weekly_releases (course_id, week_start, policy, min_group_size, payload, "
            "payload_sha256) VALUES (:c, :w, :p, :k, CAST(:payload AS jsonb), :sha) "
            "ON CONFLICT (course_id, week_start) DO NOTHING"
        ),
        {"c": course_id, "w": week, "p": POLICY, "k": settings.analytics_min_group_size, "payload": canonical,
         "sha": hashlib.sha256(canonical.encode("ascii")).hexdigest()},
    ).rowcount  # fmt: skip
    return bool(inserted)


def publish_due_releases(session_factory: sessionmaker[Session], settings: Settings) -> int:
    """Generate missing releases of publishable weeks, a bounded batch per tick. Nothing while analytics is off."""
    if not settings.analytics_enabled:
        return 0
    with session_factory() as session:
        as_of = session.execute(text("SELECT now()")).scalar_one()
        weeks = published_weeks(as_of, settings)
        due = session.execute(
            text(
                "SELECT c.id, w.week_start FROM courses c CROSS JOIN unnest(CAST(:weeks AS date[])) AS w(week_start) "
                "WHERE c.created_at < (w.week_start + 7)::timestamp AND NOT EXISTS (SELECT 1 FROM "
                "analytics_weekly_releases r WHERE r.course_id = c.id AND r.week_start = w.week_start) "
                "ORDER BY w.week_start DESC, c.id LIMIT :limit"
            ),
            {"weeks": weeks, "limit": settings.analytics_release_batch_size},
        ).all()
        session.rollback()
    published = 0
    for course_id, week in due:
        with session_factory() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            if generate_release(session, course_id, week, settings):
                published += 1
            session.commit()
    if published:
        log.info("analytics_releases_published count=%d", published)
    return published


def read_analytics(factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID, settings: Settings) -> dict:
    with short_tx(factory, read_only=True, snapshot=True) as session:
        require_owner(session, user, course_id)
        as_of = session.execute(text("SELECT now()")).scalar_one()
        weeks = published_weeks(as_of, settings)
        created = session.execute(text("SELECT created_at FROM courses WHERE id = :c"), {"c": course_id}).scalar_one()
        releases = {
            r.week_start: r
            for r in session.execute(
                text(
                    "SELECT week_start, payload, created_at, min_group_size FROM analytics_weekly_releases "
                    "WHERE course_id = :c AND week_start = ANY(CAST(:weeks AS date[]))"
                ),
                {"c": course_id, "weeks": weeks},
            ).all()
        }
        names = dict(
            session.execute(text("SELECT id, name FROM topics WHERE course_id = :c"), {"c": course_id}).all()
        )
    out_weeks = []
    for week in weeks:
        start, end = _week_bounds(week)
        if created is not None and created.replace(tzinfo=timezone.utc) >= end:
            continue  # the course did not exist yet
        release = releases.get(week)
        entry = {"week_start": week.isoformat(), "week_end": (week + timedelta(days=7)).isoformat()}
        if release is None:
            entry.update(status="pending_release", released_at=None, min_group_size=None, overall=None, topics=None,
                         topics_truncated=None)  # fmt: skip
        else:
            payload = release.payload
            entry.update(
                status="released",
                released_at=release.created_at.isoformat().replace("+00:00", "Z"),
                min_group_size=release.min_group_size,
                overall=payload["overall"],
                topics=[
                    {"topic_id": t["topic_id"], "name": names.get(uuid.UUID(t["topic_id"])), "cell": t["cell"]}
                    for t in payload["topics"]
                ],
                topics_truncated=payload["topics_truncated"],
            )
        out_weeks.append(entry)
    return {
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "course_id": str(course_id),
        "policy": {
            "version": POLICY,
            "min_group_size": settings.analytics_min_group_size,
            "week": "ISO week, Monday 00:00 UTC to the next Monday 00:00 UTC",
            "publication_delay_hours": round(settings.analytics_publication_delay_seconds / 3600, 2),
            "accuracy": "mean of per-student accuracy (student-weighted), rounded to 0.05",
            "population": "students with a graded practice attempt in the week, enrolled or not",
            "contributor_bands": [label for _, label in reversed(CONTRIBUTOR_BANDS)],
            "attempt_bands": [label for _, label in reversed(ATTEMPT_BANDS)],
        },
        "weeks": out_weeks,
        "item_analysis": dict(ITEM_ANALYSIS),
        "notice": "Aggregates of at least the minimum number of students, with suppression, rounding and bands. "
        "This is not anonymisation and not differential privacy; do not try to identify students.",
    }


def authorize_analytics(factory: sessionmaker[Session], user: CurrentUser, course_id: uuid.UUID) -> None:
    with short_tx(factory, read_only=True) as session:
        require_owner(session, user, course_id)
