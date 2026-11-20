"""Safe diagnostics for unexpected quiz failures.

Why this exists: a SQLAlchemy `StatementError` (and its subclasses) renders the
failing SQL *and its bound parameters* in `str(exc)` and in the traceback that
`logging.exception` or the ASGI server prints. For the quiz tables those
parameters are answer keys, explanations, hints and course passages. Nothing in
M7 logs `str(exc)`, a traceback or an upstream body; an unexpected exception is
described by two fixed-vocabulary facts only:

- the exception class name (never its message), and
- the PostgreSQL SQLSTATE of a database error (five characters), if any.

Endpoint code turns every unexpected exception into `internal_error()` raised
`from None`, so the server's generic "Exception in ASGI application" traceback
(which would print the original exception and its context) is never produced
for these routes. The exception object stays in memory only until the request
ends.
"""

from __future__ import annotations

import re

from ..errors import ApiError

_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")


def sqlstate_of(exc: BaseException) -> str | None:
    """The SQLSTATE of a database error, validated; None for anything else."""
    for candidate in (getattr(exc, "orig", None), exc):
        for attribute in ("sqlstate", "pgcode"):  # psycopg 3 / psycopg 2
            code = getattr(candidate, attribute, None)
            if isinstance(code, str) and _SQLSTATE.match(code):
                return code
    return None


def describe(exc: BaseException) -> str:
    """`error_class=<name> sqlstate=<code|->`: safe to log, carries no message, parameter or payload."""
    return f"error_class={type(exc).__name__} sqlstate={sqlstate_of(exc) or '-'}"


def internal_error() -> ApiError:
    """The same JSON 500 as the application's last-resort handler, with no-store."""
    return ApiError(
        500,
        "server_error",
        "Something went wrong on the server. Try again later.",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
