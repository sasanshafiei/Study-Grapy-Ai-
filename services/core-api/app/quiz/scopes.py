"""Quiz scopes (no imports: Settings validates QUIZ_ENABLED_SCOPES while app.config is loading).

Only class_only exists in M7. External material (M6) needs its activation
prerequisites first and live web search has no milestone yet; neither can be
enabled or requested, and a scope is never replaced by another one.
"""

from __future__ import annotations

SCOPES = ("class_only",)
SCOPE_ORIGINS: dict[str, tuple[str, ...]] = {"class_only": ("class",)}


def parse_quiz_scopes(raw: str) -> frozenset[str]:
    names = [part.strip() for part in raw.split(",")]
    if not names or any(not name for name in names):
        raise ValueError("QUIZ_ENABLED_SCOPES must be a comma-separated list of scopes")
    for name in names:
        if name not in SCOPES:
            raise ValueError(f"QUIZ_ENABLED_SCOPES: {name!r} is not available in M7 (only class_only)")
    return frozenset(names)
