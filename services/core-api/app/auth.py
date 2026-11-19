"""The authentication interface every protected endpoint depends on.

Owner of the implementation: CE-3 (JWT). This module only fixes the seam so
feature endpoints can be built and tested before authentication exists:

- `CurrentUser` is the principal an endpoint receives.
- `get_current_user` is the single FastAPI dependency that produces it.

Contract for the real implementation (CE-3):
1. Verify the bearer token (signature, expiry); never trust its claims for
   role or status.
2. Load the `users` row named by the token on every request, so a role change
   or a disabled account takes effect immediately.
3. Raise 401 (with `WWW-Authenticate: Bearer`) for a missing, invalid or
   expired token or an unknown user; reject a disabled account (403).
4. Return `CurrentUser` built from that row.

Until then the dependency fails closed: every protected request gets 401.
There is no development token, no hard-coded user and no header that skips
it. Tests inject principals with `app.dependency_overrides` on their own
application instance (see tests/api_support.py), never on `app.main.app`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ApiError

_bearer = HTTPBearer(auto_error=False, description="Access token issued by the auth module (CE-3).")


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated caller, as stored in `users` when the request started.

    role is one of student | professor | admin and status one of active |
    disabled; they stay plain strings so an unexpected value is simply denied
    by the authorization rules instead of failing to parse.
    """

    id: uuid.UUID
    role: str
    status: str


def not_authenticated() -> ApiError:
    return ApiError(
        401,
        "not_authenticated",
        "Sign in to continue.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    """Fail closed until CE-3 replaces this body with token verification."""
    raise not_authenticated()
