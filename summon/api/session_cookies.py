"""Session-cookie policy for first-party and embedded Telegram Mini Apps."""

from __future__ import annotations

from starlette.responses import Response

LEGACY_SESSION_COOKIE = "summon_session"
EMBEDDED_SESSION_COOKIE = "summon_session_v2"
SESSION_COOKIE_PATH = "/api"


def _partition_last_cookie(response: Response) -> None:
    """Add CHIPS on Python 3.11, whose cookie library cannot emit it yet."""
    name, value = response.raw_headers[-1]
    if name.lower() != b"set-cookie":
        raise RuntimeError("Session cookie was not appended")
    response.raw_headers[-1] = (name, value + b"; Partitioned")


def set_session_cookie(response: Response, value: str, *, max_age: int, production: bool) -> None:
    if production:
        # A new name prevents the browser from sending a legacy unpartitioned
        # and a new partitioned cookie with the same name and ambiguous order.
        response.set_cookie(
            EMBEDDED_SESSION_COOKIE,
            value,
            httponly=True,
            secure=True,
            samesite="none",
            max_age=max_age,
            path=SESSION_COOKIE_PATH,
        )
        _partition_last_cookie(response)
        response.delete_cookie(
            LEGACY_SESSION_COOKIE,
            path=SESSION_COOKIE_PATH,
            httponly=True,
            secure=True,
            samesite="strict",
        )
    else:
        # Development HTTP retains strict same-site cookies without Secure.
        response.set_cookie(
            LEGACY_SESSION_COOKIE,
            value,
            httponly=True,
            secure=False,
            samesite="strict",
            max_age=max_age,
            path=SESSION_COOKIE_PATH,
        )


def clear_session_cookies(response: Response, *, production: bool) -> None:
    if production:
        response.delete_cookie(
            EMBEDDED_SESSION_COOKIE,
            path=SESSION_COOKIE_PATH,
            httponly=True,
            secure=True,
            samesite="none",
        )
        _partition_last_cookie(response)
    response.delete_cookie(
        LEGACY_SESSION_COOKIE,
        path=SESSION_COOKIE_PATH,
        httponly=True,
        secure=production,
        samesite="strict",
    )
