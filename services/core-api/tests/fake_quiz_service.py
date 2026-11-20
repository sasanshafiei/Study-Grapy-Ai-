"""An isolated raw-socket fake of the *proposed* studygraph.quiz/1 endpoints, for tests only.

TEST DOUBLE. Not the AI service and not evidence that question generation
works: it generates nothing. It answers what each test scripts, over a real
TCP connection (real HTTP framing), reusing FakeChatService's server, `raw`,
`hold`/`release` and request recording. Listens on 127.0.0.1 only.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from tests.fake_chat_service import TOKEN, FakeChatService, Recorded, json_response

QUIZ_PROTOCOL = "studygraph.quiz/1"
__all__ = ["TOKEN", "FakeQuizService", "default_quiz_capabilities", "quiz_for", "Source"]


def default_quiz_capabilities() -> dict:
    return {
        "protocol": QUIZ_PROTOCOL,
        "service": "ai-service",
        "mock": False,
        "scopes": ["class_only"],
        "question_types": ["mcq", "true_false"],
        "max_questions": 10,
        "max_topics": 5,
    }


@dataclass(frozen=True)
class Source:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    quote: str
    locator: dict = field(default_factory=lambda: {"page": 1})


def question_for(request: dict, index: int, source: Source, qtype: str) -> dict:
    topics = request["topics"]
    difficulty = request["difficulty"] if request["difficulty"] != "mixed" else ("easy", "medium", "hard")[index % 3]
    question = {
        "topic_id": topics[index % len(topics)]["topic_id"],
        "type": qtype,
        "difficulty": difficulty,
        "stem": f"Question {index + 1}: which statement about the reaction is supported by the notes?",
        "choices": None,
        "answer": True,
        "hint": "Look at the limiting reagent.",
        "explanation": f"Explanation {index + 1}: the notes state it directly.",
        "citations": [
            {
                "chunk_id": str(source.chunk_id),
                "document_id": str(source.document_id),
                "origin": "class",
                "locator": source.locator,
                "quote": source.quote,
            }
        ],
    }
    if qtype == "mcq":
        question["choices"] = [f"Correct claim {index + 1}", f"Wrong claim {index + 1}a", f"Wrong claim {index + 1}b",
                               f"Wrong claim {index + 1}c"]  # fmt: skip
        question["answer"] = 0
    return question


def quiz_for(request: dict, sources: list[Source], *, types: tuple[str, ...] | None = None, **overrides) -> dict:
    """A valid response to `request`: alternating types, the correct MCQ choice always first (index 0)."""
    types = types or tuple(request["question_types"])
    body = {
        "protocol": request["protocol"],
        "request_id": request["request_id"],
        "course_id": request["course_id"],
        "scope": request["scope"],
        "mock": False,
        "grounded": True,
        "model": {"name": "test-generator", "version": "0"},
        "questions": [
            question_for(request, i, sources[i % len(sources)], types[i % len(types)]) for i in range(request["count"])
        ],
    }
    body.update(overrides)
    return body


@dataclass
class FakeQuizService(FakeChatService):
    capabilities: dict = field(default_factory=default_quiz_capabilities)
    generate: Callable[[dict], dict] | None = None
    on_generate: Callable[[dict], None] | None = None

    def _reply(self, request: Recorded) -> bytes:
        with self._lock:
            if self.raw:
                return self.raw.pop(0)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return json_response(401, {"error": {"code": "unauthorized", "message": "no", "retryable": False}})
        if request.method == "GET" and request.path == "/ai/v1/quiz/capabilities":
            return json_response(200, self.capabilities)
        if request.method == "POST" and request.path == "/ai/v1/quiz/generate":
            payload = request.json()
            if self.on_generate is not None:
                self.on_generate(payload)
            if self.generate is None:
                return json_response(503, {"error": {"code": "unavailable", "message": "no", "retryable": True}})
            return json_response(200, self.generate(payload))
        return json_response(404, {"error": {"code": "not_found", "message": "no", "retryable": False}})

    def generations(self) -> list[dict]:
        return [r.json() for r in self.requests if r.path == "/ai/v1/quiz/generate"]
