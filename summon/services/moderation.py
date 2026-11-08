"""Fresh Telegram permissions for group mutations; buttons confer no authority."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, cast


class PermissionDenied(PermissionError):
    pass


def _status(member: Any) -> str:
    value = getattr(member, "status", "")
    return str(getattr(value, "value", value))


def _has_right(member: Any, right: str | None) -> bool:
    status = _status(member)
    return status in {"owner", "creator"} or (
        status == "administrator"
        and (right is None or bool(getattr(getattr(member, "privileges", None), right, False)))
    )


async def require_group_admin(
    bot: Any, group_id: int, actor_id: int, right: str | None = None
) -> Any:
    if group_id >= 0 or actor_id <= 0:
        raise PermissionDenied("A Telegram group administrator is required")
    member = await bot.get_chat_member(group_id, actor_id)
    if not _has_right(member, right):
        raise PermissionDenied("You do not have the required group permission")
    return member


async def require_moderation_rights(bot: Any, group_id: int, actor_id: int, target_id: int) -> Any:
    identity = getattr(getattr(bot, "me", None), "id", None)
    if not identity:
        raise PermissionDenied("The bot identity is unavailable")
    if actor_id == target_id or target_id == identity or target_id <= 0:
        raise PermissionDenied("You cannot moderate yourself or the bot")
    await require_group_admin(bot, group_id, actor_id, "can_restrict_members")
    bot_member = await bot.get_chat_member(group_id, identity)
    if not _has_right(bot_member, "can_restrict_members"):
        raise PermissionDenied("The bot needs permission to restrict members")
    target = await bot.get_chat_member(group_id, target_id)
    if _status(target) in {"owner", "creator", "administrator"}:
        raise PermissionDenied("Group administrators cannot be moderated here")
    return target


def _warning_target(target_id: int) -> None:
    if isinstance(target_id, bool) or not isinstance(target_id, int) or not 0 < target_id < 2**53:
        raise ValueError("Enter a valid Telegram user ID")


def _warning_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid warning ID")


class ModerationService:
    def __init__(self, bot: Any, groups: Any) -> None:
        self.bot = bot
        self.groups = groups

    async def list_warnings(
        self,
        group_id: int,
        actor_id: int,
        target_id: int,
        *,
        before: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        _warning_target(target_id)
        if before is not None:
            _warning_id(before)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("Page size must be between 1 and 25")
        await require_group_admin(self.bot, group_id, actor_id, "can_restrict_members")
        return cast(
            dict[str, Any],
            await self.groups.list_warnings(group_id, target_id, before=before, limit=limit),
        )

    async def revoke_warning(
        self,
        group_id: int,
        actor_id: int,
        target_id: int,
        warning_id: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        _warning_target(target_id)
        _warning_id(warning_id)
        if not isinstance(reason, str) or len(reason) > 500:
            raise ValueError("Reason is too long")
        # A correction changes only SUMMON's record, not Telegram membership.
        # The target may since have become an admin, and bot restrict rights are
        # not needed; the actor's current restriction right is always required.
        await require_group_admin(self.bot, group_id, actor_id, "can_restrict_members")
        return cast(
            dict[str, Any],
            await self.groups.revoke_warning(
                group_id, actor_id, target_id, warning_id, reason, now
            ),
        )

    async def act(
        self,
        group_id: int,
        actor_id: int,
        target_id: int,
        action: str,
        reason: str,
        now: datetime,
        duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        if action not in {"ban", "unban", "mute", "unmute", "kick", "warn"}:
            raise ValueError("Unsupported moderation action")
        if len(reason) > 500:
            raise ValueError("Reason is too long")
        if duration_seconds is not None and not 60 <= duration_seconds <= 366 * 86400:
            raise ValueError("Duration must be between one minute and one year")
        await require_moderation_rights(self.bot, group_id, actor_id, target_id)
        # Wzgram's EditBanned-based restriction write clears view_messages.
        # Even a fresh membership check can race another administrator's ban,
        # so mute/unmute could accidentally unban the target. Unban and kick
        # likewise lack an atomic only-if-banned guard. Fail closed for all.
        if action in {"unban", "kick", "mute", "unmute"}:
            raise PermissionDenied(
                "This action is unavailable in Wzgram; use Telegram's group controls instead"
            )
        until = now + timedelta(seconds=duration_seconds) if duration_seconds else None
        if action == "ban":
            if until is None:
                await self.bot.ban_chat_member(group_id, target_id)
            else:
                await self.bot.ban_chat_member(group_id, target_id, until_date=until)
        result = await self.groups.record_moderation(
            group_id, actor_id, target_id, action, reason, now
        )
        return cast(dict[str, Any], result)
