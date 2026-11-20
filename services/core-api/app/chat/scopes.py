"""Answer scopes and the source origins each one permits.

class_plus_web stays unavailable until live web search has its own milestone:
it can neither be enabled (CHAT_ENABLED_SCOPES) nor requested. A scope that is
not enabled is refused; it is never replaced by a narrower or wider one.
"""

from __future__ import annotations

SCOPES = ("class_only", "class_plus_external", "class_plus_web")
UNAVAILABLE_SCOPES = frozenset({"class_plus_web"})

# Public origins (web app vocabulary) a citation may have under each scope.
SCOPE_ORIGINS: dict[str, frozenset[str]] = {
    "class_only": frozenset({"class"}),
    "class_plus_external": frozenset({"class", "external"}),
    "class_plus_web": frozenset({"class", "external", "web"}),
}


def parse_enabled_scopes(raw: str) -> frozenset[str]:
    names = [part.strip() for part in raw.split(",")]
    if not names or any(not name for name in names):
        raise ValueError("CHAT_ENABLED_SCOPES must be a comma-separated list of scopes")
    for name in names:
        if name not in SCOPES:
            raise ValueError(f"CHAT_ENABLED_SCOPES: unknown scope {name!r}")
        if name in UNAVAILABLE_SCOPES:
            raise ValueError(f"CHAT_ENABLED_SCOPES: {name} is not available (live web search is a later milestone)")
    return frozenset(names)
