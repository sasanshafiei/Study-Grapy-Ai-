"""Durable learner state for the mastery provider (SQLite on the ai-service volume).

This is the AI service's own store, not a core-api table: CE-1 owns every core table and the
AI side writes none of them (docs/ai-mastery-integration.md). It holds, per state epoch and
pseudonymous learner stream, every applied event (needed for exactly-once deduplication by
(state_epoch, learner_ref, event_id)) and one folded state per topic.

Events are kept with the features the model uses, so a parameter upgrade re-folds a topic
from its own history instead of mixing two parameter sets in one chain. Nothing here is a
user id, course id, answer or question; references are CE's HMAC pseudonyms.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import model
from .schedule import format_timestamp, parse_timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_state (
    epoch TEXT NOT NULL,
    learner_ref TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    course_ref TEXT NOT NULL,
    through_seq INTEGER NOT NULL,
    p_learned REAL NOT NULL,
    correct_streak INTEGER NOT NULL,
    last_practice TEXT NOT NULL,
    model_version TEXT NOT NULL,
    PRIMARY KEY (epoch, learner_ref, topic_id)
);
CREATE TABLE IF NOT EXISTS applied_events (
    epoch TEXT NOT NULL,
    learner_ref TEXT NOT NULL,
    event_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    topic_seq INTEGER NOT NULL,
    qtype TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    correct INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (epoch, learner_ref, event_id),
    UNIQUE (epoch, learner_ref, topic_id, topic_seq)
);
"""


@dataclass(frozen=True)
class TopicState:
    course_ref: str
    through_seq: int
    p_learned: float
    correct_streak: int
    last_practice: datetime | None


@dataclass(frozen=True)
class Event:
    event_id: str
    topic_id: str
    topic_seq: int
    qtype: str
    difficulty: str
    correct: bool
    submitted_at: datetime


def empty_state(params: model.BktParams, course_ref: str) -> TopicState:
    return TopicState(course_ref, 0, params.p_init, 0, None)


def step(params: model.BktParams, state: TopicState, event: Event) -> TopicState:
    practiced = event.submitted_at if state.last_practice is None else max(state.last_practice, event.submitted_at)
    return replace(
        state,
        through_seq=event.topic_seq,
        p_learned=model.update(params, state.p_learned, event.qtype, event.difficulty, event.correct),
        correct_streak=state.correct_streak + 1 if event.correct else 0,
        last_practice=practiced,
    )


def connect(path: str | Path) -> sqlite3.Connection:
    """One connection per request; autocommit mode so callers control BEGIN IMMEDIATE / COMMIT."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(SCHEMA)
    return conn


def _events_of_topic(conn: sqlite3.Connection, epoch: str, learner: str, topic: str) -> list[Event]:
    rows = conn.execute(
        "SELECT event_id, topic_id, topic_seq, qtype, difficulty, correct, submitted_at FROM applied_events "
        "WHERE epoch = ? AND learner_ref = ? AND topic_id = ? ORDER BY topic_seq",
        (epoch, learner, topic),
    ).fetchall()
    return [Event(r[0], r[1], r[2], r[3], r[4], bool(r[5]), parse_timestamp(r[6])) for r in rows]


def load_topic(conn, params: model.BktParams, epoch: str, learner: str, topic: str, course_ref: str) -> TopicState:
    row = conn.execute(
        "SELECT course_ref, through_seq, p_learned, correct_streak, last_practice, model_version FROM topic_state "
        "WHERE epoch = ? AND learner_ref = ? AND topic_id = ?",
        (epoch, learner, topic),
    ).fetchone()
    if row is None:
        return empty_state(params, course_ref)
    state = TopicState(row[0], row[1], row[2], row[3], parse_timestamp(row[4]))
    if row[5] != params.version:  # parameters changed: re-fold the topic from its stored history
        refolded = empty_state(params, row[0])
        for event in _events_of_topic(conn, epoch, learner, topic):
            refolded = step(params, refolded, event)
        state = refolded
    return state


def event_at(conn, epoch: str, learner: str, topic: str, seq: int) -> str | None:
    row = conn.execute(
        "SELECT event_id FROM applied_events WHERE epoch = ? AND learner_ref = ? AND topic_id = ? AND topic_seq = ?",
        (epoch, learner, topic, seq),
    ).fetchone()
    return row[0] if row else None


def event_known(conn, epoch: str, learner: str, event_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM applied_events WHERE epoch = ? AND learner_ref = ? AND event_id = ?", (epoch, learner, event_id)
    ).fetchone() is not None


def insert_event(conn, epoch: str, learner: str, event: Event) -> None:
    conn.execute(
        "INSERT INTO applied_events (epoch, learner_ref, event_id, topic_id, topic_seq, qtype, difficulty, correct, "
        "submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (epoch, learner, event.event_id, event.topic_id, event.topic_seq, event.qtype, event.difficulty,
         int(event.correct), format_timestamp(event.submitted_at)),
    )


def save_topic(conn, params: model.BktParams, epoch: str, learner: str, topic: str, state: TopicState) -> None:
    conn.execute(
        "INSERT INTO topic_state (epoch, learner_ref, topic_id, course_ref, through_seq, p_learned, correct_streak, "
        "last_practice, model_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (epoch, learner_ref, topic_id) DO UPDATE SET through_seq = excluded.through_seq, "
        "p_learned = excluded.p_learned, correct_streak = excluded.correct_streak, "
        "last_practice = excluded.last_practice, model_version = excluded.model_version",
        (epoch, learner, topic, state.course_ref, state.through_seq, state.p_learned, state.correct_streak,
         format_timestamp(state.last_practice), params.version),
    )
