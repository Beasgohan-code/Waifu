from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request

from summon.auth import AuthenticationError, BoundSession, SessionCodec, TelegramUser
from summon.services.moderation import _has_right


def get_store(request: Request) -> Any:
    return request.app.state.store


async def get_current_session(request: Request) -> BoundSession:
    settings = request.app.state.settings
    secret = settings.session_secret.get_secret_value()
    token = request.cookies.get("summon_session", "")
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
    if not await request.app.state.store.sessions.is_active(
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
    if not await request.app.state.store.limiter.allow(
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
