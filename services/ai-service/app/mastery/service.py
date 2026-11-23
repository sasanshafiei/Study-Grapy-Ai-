"""Apply one studygraph.mastery/1 batch: exactly-once per (epoch, learner, event), per-topic order.

Outcomes for each event of a topic, given the stored `through_seq` of that topic:

- `topic_seq <= through_seq` and the stored event at that sequence has the same id: duplicate
  (a retried batch). The current score is reported again.
- `topic_seq <= through_seq` with another id, or an id already applied elsewhere: 409
  `sequence_conflict` / `event_conflict`. Two different events cannot share a slot; nothing is
  stored.
- `topic_seq == through_seq + 1`: applied, in order.
- `topic_seq > through_seq + 1`: this service has lost state (CE only ever sends the contiguous
  continuation of what it delivered). Nothing is stored; every topic is reported with its true
  `attempts_seen`, which is below `through_seq`. That is the protocol's state-mismatch signal:
  CE stores nothing, blocks delivery and keeps every event until a new epoch is agreed. The
  protocol has no third bucket, so the batch's non-duplicate ids are listed under
  `applied_event_ids` in that response even though they were not applied.

The whole batch commits in one SQLite transaction (all or nothing).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import model, store
from .schedule import format_timestamp, next_review, parse_timestamp

PROTOCOL = "studygraph.mastery/1"


class MasteryError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Outcome:
    applied: list[str]
    duplicate: list[str]
    states: dict[str, store.TopicState]  # topic -> state reported (after the batch, or before it if lost)
    last_seq: dict[str, int]  # topic -> last topic_seq in the batch


def _group(events: list[dict]) -> dict[str, list[store.Event]]:
    seen: set[str] = set()
    groups: dict[str, list[store.Event]] = {}
    for raw in events:
        if raw["event_id"] in seen:
            raise MasteryError(422, "invalid_request", "Event ids must be unique within a batch.")
        seen.add(raw["event_id"])
        event = store.Event(raw["event_id"], raw["topic_id"], raw["topic_seq"], raw["qtype"], raw["difficulty"],
                            raw["correct"], parse_timestamp(raw["submitted_at"]))
        groups.setdefault(event.topic_id, []).append(event)
    for topic_events in groups.values():
        topic_events.sort(key=lambda e: e.topic_seq)
        for before, after in zip(topic_events, topic_events[1:]):
            if after.topic_seq != before.topic_seq + 1:
                raise MasteryError(422, "invalid_request", "Each topic's events must have consecutive sequence numbers.")
    return groups


def apply_batch(conn, params: model.BktParams, epoch: str, request: dict) -> Outcome:
    if request["state_epoch"] != epoch:
        raise MasteryError(409, "epoch_mismatch", "The request names another state epoch.")
    learner, course = request["learner_ref"], request["course_ref"]
    groups = _group(request["events"])
    last_seq = {topic: evs[-1].topic_seq for topic, evs in groups.items()}
    applied: list[str] = []
    duplicate: list[str] = []
    states: dict[str, store.TopicState] = {}
    lost = False

    conn.execute("BEGIN IMMEDIATE")
    try:
        for topic, events in groups.items():
            state = store.load_topic(conn, params, epoch, learner, topic, course)
            if state.course_ref != course:
                raise MasteryError(409, "reference_conflict", "The learner stream belongs to another course.")
            changed = False
            for event in events:
                if event.topic_seq <= state.through_seq:
                    if store.event_at(conn, epoch, learner, topic, event.topic_seq) != event.event_id:
                        raise MasteryError(409, "sequence_conflict", "Another event already holds this sequence.")
                    duplicate.append(event.event_id)
                elif event.topic_seq == state.through_seq + 1 and not lost:
                    if store.event_known(conn, epoch, learner, event.event_id):
                        raise MasteryError(409, "event_conflict", "The event was already applied at another position.")
                    store.insert_event(conn, epoch, learner, event)
                    state = store.step(params, state, event)
                    applied.append(event.event_id)
                    changed = True
                else:
                    lost = True
                    applied.append(event.event_id)  # reported only; the transaction is rolled back below
            if changed and not lost:
                store.save_topic(conn, params, epoch, learner, topic, state)
            states[topic] = state
        if lost:
            conn.execute("ROLLBACK")
            states = {t: store.load_topic(conn, params, epoch, learner, t, course) for t in groups}
        else:
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return Outcome(applied, duplicate, states, last_seq)


def score(params: model.BktParams, topic: str, state: store.TopicState, last_seq: int, now: datetime) -> dict:
    review = None
    if state.last_practice is not None:
        review = format_timestamp(next_review(state.last_practice, state.p_learned, state.correct_streak, now))
    return {
        "topic_id": topic,
        "mastery": round(min(1.0, max(0.0, state.p_learned)), 6),
        "attempts_seen": state.through_seq,
        "through_seq": last_seq,
        "next_review_at": review,
    }


def build_result(params: model.BktParams, request: dict, outcome: Outcome, now: datetime) -> dict:
    return {
        "protocol": PROTOCOL,
        "request_id": request["request_id"],
        "state_epoch": request["state_epoch"],
        "course_ref": request["course_ref"],
        "learner_ref": request["learner_ref"],
        "mock": False,
        "model": {"name": params.name, "version": params.version},
        "applied_event_ids": outcome.applied,
        "duplicate_event_ids": outcome.duplicate,
        "scores": [score(params, t, outcome.states[t], outcome.last_seq[t], now) for t in outcome.last_seq],
    }
