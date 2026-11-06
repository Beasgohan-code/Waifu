"""Group-message protection, before any command or activity handler runs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, TelegramObject

from summon.models import GroupSettings
from summon.services.antispam import SpamGuard
from summon.services.moderation import _status

logger = logging.getLogger(__name__)

# Service events (joins, pins, title changes, etc.) are not member chat activity.
# Unknown future content types are deliberately excluded until classified.
_USER_CONTENT_TYPES = frozenset(
    {
        ContentType.TEXT,
        ContentType.ANIMATION,
        ContentType.AUDIO,
        ContentType.DOCUMENT,
        ContentType.LIVE_PHOTO,
        ContentType.PAID_MEDIA,
        ContentType.PHOTO,
        ContentType.STICKER,
        ContentType.STORY,
        ContentType.VIDEO,
        ContentType.VIDEO_NOTE,
        ContentType.VOICE,
        ContentType.CHECKLIST,
        ContentType.CONTACT,
        ContentType.DICE,
        ContentType.GAME,
        ContentType.POLL,
        ContentType.VENUE,
        ContentType.LOCATION,
        ContentType.INVOICE,
        ContentType.RICH_MESSAGE,
    }
)


def is_group_user_content(message: Message) -> bool:
    """Only messages attributable to a human member have a checkable exemption."""
    return bool(
        message.chat.type in {"group", "supergroup"}
        and message.from_user
        and not message.from_user.is_bot
        and not message.sender_chat
        and message.content_type in _USER_CONTENT_TYPES
    )


class GroupProtectionMiddleware(BaseMiddleware):
    """Apply configured rules once to delivered group messages, including commands.

    Edited messages are checked against content rules only: an edit is not a new
    message and must not increment flood/duplicate counters or spawn activity.
    """

    def __init__(self, groups: Any, *, edited: bool = False) -> None:
        self.groups = groups
        self.guard = SpamGuard(groups.db)
        self.edited = edited

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not is_group_user_content(event):
            return await handler(event, data)
        user = event.from_user
        bot = event.bot
        if user is None or bot is None:
            logger.warning("Cannot check group protection without a sender and bot")
            return None

        group = await self.groups.get(event.chat.id)
        if not group:
            return await handler(event, data)
        # The catch-all can use the same protected group snapshot without a second
        # per-message database read. It must not run before this middleware.
        data["summon_group_snapshot"] = group
        config = GroupSettings.model_validate(group["settings"])
        now = datetime.now(UTC)
        reason = await self.guard.inspect(
            event.chat.id,
            user.id,
            event.text or event.caption or "",
            config,
            now,
            entities=event.entities or event.caption_entities or (),
            count_activity=not self.edited and event.ephemeral_message_id is None,
        )
        if not reason:
            return await handler(event, data)

        # Resolve the sender's current role on every possible enforcement action.
        # If Telegram cannot verify the exemption, do not delete or execute a
        # flagged command; a later update can be evaluated independently.
        try:
            member = await bot.get_chat_member(event.chat.id, user.id)
        except TelegramAPIError:
            logger.warning("Could not verify sender's role for group protection")
            return None
        if _status(member) in {"creator", "administrator"}:
            return await handler(event, data)

        # Ephemeral inputs are not ordinary chat messages; never attempt a
        # regular delete or claim one succeeded. They are still not processed.
        if event.ephemeral_message_id is not None:
            logger.info("Blocked a flagged ephemeral command")
            return None

        try:
            bot_member = await bot.get_chat_member(event.chat.id, bot.id)
            if _status(bot_member) != "administrator" or not getattr(
                bot_member, "can_delete_messages", False
            ):
                logger.warning("Spam detected but bot lacks delete permission")
                return None
            deleted = await event.delete()
        except TelegramAPIError:
            # A network failure may be ambiguous; never retry an uncertain delete
            # or record it as confirmed.
            logger.warning("Telegram did not confirm the spam message deletion")
            return None
        if not deleted:
            logger.warning("Telegram did not confirm the spam message deletion")
            return None
        await self.groups.record_spam(event.chat.id, user.id, reason, now)
        return None
