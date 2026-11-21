"""Content-Disposition for exam files (contract §7.2).

`<course code snapshot>-<title>-v<version>-<student|answer-key>.<pdf|docx>` (draft preview files:
`...-draft-v<version>-...`). The ASCII `filename`: characters outside [A-Za-z0-9._-] become `-`, runs
collapsed, trimmed, at most 120 characters (the suffix is kept), never empty (fallback `exam`). The RFC 5987
`filename*`: the same parts with Unicode kept (NFC), whitespace runs as `-`, controls, quotes, `/`, `\\` and
`;` removed, percent-encoded. No path component can ever appear.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

MAX_ASCII = 120
_NOT_ASCII_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DASHES = re.compile(r"-{2,}")
_WS = re.compile(r"\s+")
_UNSAFE_UNICODE = re.compile("[\x00-\x1f\x7f-\x9f\"'/\\\\;]")


def _ascii(value: str) -> str:
    return _DASHES.sub("-", _NOT_ASCII_SAFE.sub("-", value)).strip("-.")


def _unicode(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = _UNSAFE_UNICODE.sub("", value)
    return _DASHES.sub("-", _WS.sub("-", value)).strip("-.")


def content_disposition(course_code: str, title: str, version: int, variant: str, fmt: str, *, draft: bool) -> str:
    kind = "answer-key" if variant == "answer_key" else "student"
    suffix = f"{'draft-' if draft else ''}v{version}-{kind}.{fmt}"
    stem = "-".join(p for p in (_ascii(course_code), _ascii(title)) if p)
    room = MAX_ASCII - len(suffix) - 1
    stem = stem[:room].rstrip("-.") if stem else ""
    ascii_name = f"{stem}-{suffix}" if stem else f"exam-{suffix}"
    unicode_stem = "-".join(p for p in (_unicode(course_code), _unicode(title)) if p) or "exam"
    unicode_name = f"{unicode_stem}-{suffix}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(unicode_name, safe='!#$&+-.^_`|~')}"
