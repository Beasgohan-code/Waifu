"""Native Wzgram protection handlers run before commands and group activity."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from wzgram import Client, StopPropagation, filters
from wzgram.enums import MessageMediaType
from wzgram.errors import RPCError
from wzgram.types import Message

from summon.bot.telegram_helpers import _group
from summon.models import GroupSettings
from summon.services.antispam import SpamGuard
from summon.services.moderation import _has_right, _status

logger = logging.getLogger(__name__)

# Deliberately exclude service messages and unclassified future media types.
_USER_MEDIA = frozenset(
    {
        MessageMediaType.ANIMATION,
        MessageMediaType.AUDIO,
        MessageMediaType.CHECKLIST,
        MessageMediaType.CONTACT,
        MessageMediaType.DICE,
        MessageMediaType.DOCUMENT,
        MessageMediaType.GAME,
        MessageMediaType.INVOICE,
        MessageMediaType.LIVE_PHOTO,
        MessageMediaType.LOCATION,
        MessageMediaType.PAID_MEDIA,
        MessageMediaType.PHOTO,
        MessageMediaType.POLL,
        MessageMediaType.STICKER,
        MessageMediaType.STORY,
        MessageMediaType.VENUE,
        MessageMediaType.VIDEO,
        MessageMediaType.VIDEO_NOTE,
        MessageMediaType.VOICE,
    }
)


def is_group_user_content(message: Message) -> bool:
    return bool(
        _group(message)
        and message.from_user
        and not message.from_user.is_bot
        and not message.sender_chat
        and not message.service
        and (
            message.text is not None
            or message.rich_message is not None
            or message.media in _USER_MEDIA
        )
    )


class GroupProtection:
    """Inspect new and edited member content once before any other handler."""

    def __init__(self, groups: Any) -> None:
        self.groups = groups
        self.guard = SpamGuard(groups.db)

    async def check(self, client: Client, message: Message, *, edited: bool) -> bool:
        if not is_group_user_content(message):
            return True
        user, chat = message.from_user, message.chat
        if user is None or chat is None:
            return False
        group = await self.groups.get(chat.id)
        if not group:
            return True
        # Reuse the same snapshot in the catch-all activity handler.
        message._summon_group_snapshot = group
        config = GroupSettings.model_validate(group["settings"])
        now = datetime.now(UTC)
        reason = await self.guard.inspect(
            chat.id,
            user.id,
            str(message.text or message.caption or ""),
            config,
            now,
            entities=message.entities or message.caption_entities or (),
            count_activity=not edited and message.ephemeral_message_id is None,
        )
        if not reason:
            return True

        # Fail closed if the sender's current admin exemption cannot be checked.
        try:
            member = await client.get_chat_member(chat.id, user.id)
        except (RPCError, ConnectionError, OSError, TimeoutError):
            logger.warning("Could not verify sender's role for group protection")
            return False
        if _status(member) in {"owner", "creator", "administrator"}:
            return True
        if message.ephemeral_message_id is not None:
            logger.info("Blocked a flagged ephemeral command")
            return False

        try:
            if client.me is None:
                return False
            bot_member = await client.get_chat_member(chat.id, client.me.id)
            if not _has_right(bot_member, "can_delete_messages"):
                logger.warning("Spam detected but bot lacks delete permission")
                return False
            deleted = await message.delete()
        except (RPCError, ConnectionError, OSError, TimeoutError):
            # Never retry an ambiguous delete or record it as confirmed.
            logger.warning("Telegram did not confirm the spam message deletion")
            return False
        if not deleted:
            logger.warning("Telegram did not confirm the spam message deletion")
            return False
        await self.groups.record_spam(chat.id, user.id, reason, now)
        return False

    def register(self, app: Client) -> None:
        @app.on_message(filters.incoming, group=-100)
        async def check_message(client: Client, message: Message) -> None:
            try:
                allowed = await self.check(client, message, edited=False)
            except Exception as exc:
                # Wzgram continues to later handler groups after ordinary errors.
                # Never let a failed protection check execute a command.
                logger.warning("Group protection failed (%s)", type(exc).__name__)
                raise StopPropagation from None
            if not allowed:
                raise StopPropagation

        @app.on_edited_message(filters.incoming, group=-100)
        async def check_edit(client: Client, message: Message) -> None:
            try:
                allowed = await self.check(client, message, edited=True)
            except Exception as exc:
                logger.warning("Edited-message protection failed (%s)", type(exc).__name__)
                raise StopPropagation from None
            if not allowed:
                raise StopPropagation
