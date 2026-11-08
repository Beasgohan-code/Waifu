from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request

from summon.api.session_cookies import EMBEDDED_SESSION_COOKIE, LEGACY_SESSION_COOKIE
from summon.auth import AuthenticationError, BoundSession, SessionCodec, TelegramUser
from summon.services.moderation import _has_right


def get_store(request: Request) -> Any:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Database is not ready; try again")
    return store


async def get_current_session(request: Request) -> BoundSession:
    settings = request.app.state.settings
    secret = settings.session_secret.get_secret_value()
    # A present v2 cookie is authoritative, even when invalid: do not fall
    # back to a different account's older cookie. Existing strict cookies can
    # continue to work when no v2 cookie has been issued yet.
    if EMBEDDED_SESSION_COOKIE in request.cookies:
        token = request.cookies[EMBEDDED_SESSION_COOKIE]
    else:
        token = request.cookies.get(LEGACY_SESSION_COOKIE, "")
    if len(secret) < 32 or not token:
        raise HTTPException(status_code=401, detail="Open SUMMON from Telegram to sign in")
    try:
        session = SessionCodec(secret, max_age=settings.session_max_age).verify(
            token, launch_digest=request.headers.get("X-Summon-Launch", "")
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401, detail="Your session has expired; reopen SUMMON in Telegram"
        ) from exc
    if not await get_store(request).sessions.is_active(
        session.session_id, session.user.id, datetime.now(UTC)
    ):
        raise HTTPException(401, "Your session has expired; reopen SUMMON in Telegram")
    return session


async def get_current_user(request: Request) -> TelegramUser:
    if request.app.state.settings.preview_mode:
        return TelegramUser(id=1, first_name="Mika", username="collector")
    return (await get_current_session(request)).user


async def limit_action(
    request: Request, user: TelegramUser, action: str, *, limit: int = 30
) -> None:
    if not await get_store(request).limiter.allow(
        f"user:{user.id}:{action}", limit=limit, seconds=60
    ):
        raise HTTPException(status_code=429, detail="Too many requests; please try again shortly")


def get_bot(request: Request) -> Any:
    bot = request.app.state.telegram_bot
    if bot is None:
        raise HTTPException(503, "Group management is not connected to Telegram")
    return bot


def permissions(member: Any) -> dict[str, bool]:
    return {
        "can_change_info": _has_right(member, "can_change_info"),
        "can_restrict_members": _has_right(member, "can_restrict_members"),
    }
