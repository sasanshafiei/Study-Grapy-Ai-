"""Public course shapes (snake_case of Course and Topic in CE-2's apps/web/src/lib/api/types.ts).

join_code is the course's join code only for the professor who owns the
course; everyone else receives the empty string (the web type is a plain
string, so the key is always present). code is the empty string for courses
created before revision 0004 (no code recorded).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel

COURSE_KEYS = frozenset({"id", "code", "name", "professor_name", "join_code", "student_count"})
TOPIC_KEYS = frozenset({"id", "name"})


class CourseItem(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    professor_name: str
    join_code: str
    student_count: int


class TopicItem(BaseModel):
    id: uuid.UUID
    name: str


class CourseCreateBody(BaseModel):
    """Documentation only: the handler reads and validates the raw body itself (after authorization)."""

    name: str
    code: str


class CourseJoinBody(BaseModel):
    """Documentation only, as CourseCreateBody."""

    join_code: str
