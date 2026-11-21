"""Shared helpers of the M8 tests: real graded attempts, a deterministic in-process mastery service, pins.

graded_attempt() writes exactly what the M7 grading path writes (a ready one-question
session, its generated question and assignment, the topic statistics upsert, the graded
attempt with the returned counters), in one transaction, optionally with an explicit
submitted_at so that days and weeks can be tested. Every M7 and M8 database rule applies.

FakeMastery is an httpx.MockTransport handler implementing studygraph.mastery/1 with
per-(epoch, learner, topic) state and deduplication by event id; it never opens a socket.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from pydantic import SecretStr
from sqlalchemy import text

TOKEN = "t" * 40
PIN_KEY = "k" * 48
EPOCH = "0b9e8f5e-1f3a-4c55-9d0a-6f1d2c3b4a59"
PINS = {
    "mastery_model_name": "ai2-bkt", "mastery_model_version": "1.4.0", "mastery_state_epoch": EPOCH,
    "mastery_pseudonym_namespace": "studygraph-test", "mastery_pseudonym_key": SecretStr(PIN_KEY),
}  # fmt: skip
CHOICES = [{"id": "a", "text": "Right"}, {"id": "b", "text": "Wrong"}]
EVIDENCE = [{"chunk_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4()), "origin": "class",
             "locator": {"page": 1}, "quote": "x" * 25}]  # fmt: skip


def graded_attempt(engine, user, course, topic, *, correct: bool = True, submitted_at: datetime | None = None,
                   hint_count: int = 0, response_ms: int | None = 900, difficulty: str = "easy",
                   connection=None, forged: tuple[int, int] | None = None) -> uuid.UUID:  # fmt: skip
    """`forged=(n, k)` skips the statistics upsert and writes these counters (only possible below revision 0007);
    with `connection` the caller's open transaction is used (and not committed here)."""
    if connection is not None:
        return _graded(connection, user, course, topic, correct, submitted_at, hint_count, response_ms, difficulty, forged)
    with engine.begin() as c:
        return _graded(c, user, course, topic, correct, submitted_at, hint_count, response_ms, difficulty, forged)


def _graded(c, user, course, topic, correct, submitted_at, hint_count, response_ms, difficulty, forged) -> uuid.UUID:
    if True:
        session = c.execute(
            text(
                "INSERT INTO quiz_sessions (user_id, course_id, status, idempotency_key, request_fingerprint, "
                "requested_topic_ids, requested_count, difficulty, scope, attempt_token, lease_expires_at) VALUES "
                "(:u, :c, 'generating', gen_random_uuid(), repeat('a', 64), ARRAY[CAST(:t AS uuid)], 1, 'easy', "
                "'class_only', gen_random_uuid(), now() + interval '1 minute') RETURNING id"
            ),
            {"u": user, "c": course, "t": topic},
        ).scalar_one()
        question = c.execute(
            text(
                "INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, choices, explanation, "
                "citations, status, hint, answer_key, grading_method, generated_by_session_id, generated_for_user_id, "
                "generation, content_hash) VALUES (:c, :t, 'practice', 'mcq', :d, 'Stem?', CAST(:choices AS jsonb), "
                "'Because.', CAST(:ev AS jsonb), 'generated', 'Hint.', '{\"choice_id\": \"a\"}'::jsonb, 'choice_id/1', "
                ":s, :u, '{\"protocol\": \"studygraph.quiz/1\"}'::jsonb, repeat('b', 64)) RETURNING id"
            ),
            {"c": course, "t": topic, "d": difficulty, "choices": json.dumps(CHOICES), "ev": json.dumps(EVIDENCE),
             "s": session, "u": user},
        ).scalar_one()  # fmt: skip
        item = c.execute(
            text(
                "INSERT INTO quiz_items (session_id, user_id, course_id, question_id, topic_id, position) "
                "VALUES (:s, :u, :c, :q, :t, 1) RETURNING id"
            ),
            {"s": session, "u": user, "c": course, "q": question, "t": topic},
        ).scalar_one()
        c.execute(
            text(
                "UPDATE quiz_sessions SET status = 'ready', protocol = 'studygraph.quiz/1', service = 'ai-service', "
                "attempt_token = NULL, lease_expires_at = NULL, completed_at = now() WHERE id = :s"
            ),
            {"s": session},
        )
        if forged is None:
            stats = c.execute(
                text(
                    "INSERT INTO topic_practice_stats AS s (user_id, topic_id, course_id, attempts, correct) "
                    "VALUES (:u, :t, :c, 1, :k) ON CONFLICT (user_id, topic_id) DO UPDATE SET "
                    "attempts = s.attempts + 1, correct = s.correct + EXCLUDED.correct RETURNING attempts, correct"
                ),
                {"u": user, "t": topic, "c": course, "k": int(correct)},
            ).one()
        else:
            stats = type("Forged", (), {"attempts": forged[0], "correct": forged[1]})
        return c.execute(
            text(
                "INSERT INTO attempts (user_id, question_id, topic_id, course_id, is_correct, hint_count, response_ms, "
                "quiz_item_id, answer, grading_method, feedback, mastery_before, mastery_after, mastery_method, "
                "stats_attempts, stats_correct, submitted_at, attempted_at) VALUES (:u, :q, :t, :c, :ok, :h, :ms, :i, "
                ":ans, 'choice_id/1', '{}'::jsonb, NULL, :after, 'cumulative_accuracy/1', :n, :k, "
                "coalesce(CAST(:at AS timestamptz), now()), (now() AT TIME ZONE 'UTC')) RETURNING id"
            ),
            {"u": user, "q": question, "t": topic, "c": course, "ok": correct, "h": hint_count, "ms": response_ms,
             "i": item, "ans": "a" if correct else "b", "after": stats.correct / stats.attempts, "n": stats.attempts,
             "k": stats.correct, "at": submitted_at},
        ).scalar_one()  # fmt: skip


def legacy_attempt(engine, user, course, topic, *, correct: bool = True) -> None:
    """A pre-M7 style attempt (no assignment, no grading snapshot): never counted by M8."""
    with engine.begin() as c:
        question = c.execute(
            text("INSERT INTO questions (course_id, topic_id, purpose, qtype, difficulty, text, status) "
                 "VALUES (:c, :t, 'practice', 'mcq', 'easy', 'Legacy?', 'approved') RETURNING id"),  # fmt: skip
            {"c": course, "t": topic},
        ).scalar_one()
        c.execute(
            text("INSERT INTO attempts (user_id, question_id, topic_id, course_id, is_correct, hint_count) "
                 "VALUES (:u, :q, :t, :c, :ok, 0)"),  # fmt: skip
            {"u": user, "q": question, "t": topic, "c": course, "ok": correct},
        )


# --- deterministic in-process mastery service --------------------------------------------------------


def raw_response(status: int, content: bytes, content_type: str = "application/json", **headers) -> httpx.Response:
    """A streamed response with exactly one Content-Length, as a real server sends it."""
    return httpx.Response(
        status, stream=httpx.ByteStream(content),
        headers={"content-type": content_type, "content-length": str(len(content)), **headers},
    )  # fmt: skip


def _json(status: int, body) -> httpx.Response:
    return raw_response(status, json.dumps(body).encode())


@dataclass
class FakeMastery:
    model: dict = field(default_factory=lambda: {"name": "ai2-bkt", "version": "1.4.0"})
    epoch: str = EPOCH
    mock: object = False
    max_events: int = 100
    # "ok" | "timeout" | "http_500" | "lost" (applies, then the response is lost) | "forget" (state loss)
    mode: str = "ok"
    review: str | None = "2026-12-01T09:00:00Z"
    requests: list = field(default_factory=list)
    probes: int = 0
    applied: dict = field(default_factory=dict)  # (epoch, learner, topic) -> set of event ids
    tamper: object = None  # optional callable(body) -> body
    on_request: object = None  # optional callable(), run when an update request arrives (while it is in flight)

    def capabilities(self) -> dict:
        return {"protocol": "studygraph.mastery/1", "service": "ai-service", "mock": self.mock, "model": self.model,
                "state_epoch": self.epoch, "max_events": self.max_events, "ordering": "per_topic"}  # fmt: skip

    def _update(self, request: dict) -> dict:
        applied, duplicate, last = [], [], {}
        for event in request["events"]:
            key = (request["state_epoch"], request["learner_ref"], event["topic_id"])
            seen = self.applied.setdefault(key, set())
            (duplicate if event["event_id"] in seen else applied).append(event["event_id"])
            seen.add(event["event_id"])
            last[event["topic_id"]] = max(last.get(event["topic_id"], 0), event["topic_seq"])
        scores = []
        for topic, through in sorted(last.items()):
            seen = self.applied[(request["state_epoch"], request["learner_ref"], topic)]
            scores.append({"topic_id": topic, "mastery": round(min(1.0, 0.1 * len(seen)), 3),
                           "attempts_seen": len(seen), "through_seq": through, "next_review_at": self.review})  # fmt: skip
        return {
            "protocol": "studygraph.mastery/1", "request_id": request["request_id"], "state_epoch": request["state_epoch"],
            "course_ref": request["course_ref"], "learner_ref": request["learner_ref"], "mock": self.mock,
            "model": self.model, "applied_event_ids": applied, "duplicate_event_ids": duplicate, "scores": scores,
        }  # fmt: skip

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {TOKEN}"
        if request.url.path == "/ai/v1/mastery/capabilities":
            self.probes += 1
            return _json(200, self.capabilities())
        assert request.url.path == "/ai/v1/mastery/update"
        body = json.loads(request.content)
        self.requests.append(body)
        if self.on_request is not None:
            self.on_request()
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.mode == "connect":
            raise httpx.ConnectError("refused", request=request)
        if self.mode == "http_500":
            return _json(500, {"detail": "boom"})
        if self.mode == "forget":
            self.applied.clear()
        response = self._update(body)
        if self.mode == "lost":
            raise httpx.ReadTimeout("lost", request=request)
        if self.tamper is not None:
            response = self.tamper(response)
        return _json(200, response)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
