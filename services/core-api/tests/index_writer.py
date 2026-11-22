"""Deterministic provider test double. The same insert-once SQL operation is available to AI-1."""

import json
import re

from sqlalchemy import text

COMMIT = text("""
    SELECT chunk_count, replayed FROM public.commit_index_generation(
        :document, :course, :key, :digest, :sections, :model, :version, CAST(:chunks AS jsonb))
""")


def parameters(payload):
    doc = payload["document"]
    chunks = []
    for section in payload["sections"]:
        locator = section["locator"]
        if "page" not in locator:
            heading = locator["heading"] or "Introduction"
            locator = {"section": re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", heading).strip()[:40]}
        chunks.append({"section_ordinal": section["ordinal"], "text": section["text"], "locator": locator,
                       "embedding": [1] + [0] * 383})
    return {"document": doc["id"], "course": doc["course_id"], "key": payload["idempotency_key"],
            "digest": payload["content_digest"], "sections": len(payload["sections"]),
            "model": "test-only", "version": "1", "chunks": json.dumps(chunks)}


def persist(engine, payload):
    with engine.begin() as connection:
        return connection.execute(COMMIT, parameters(payload)).one()
