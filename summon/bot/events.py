"""Welcome, goodbye, group activity, and edited-message observers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.errors import BadRequest, Forbidden, RPCError
from wzgram.types import Message

from summon.auto_replies import AutoReplyStorageError, find_auto_reply
from summon.bot.presentation import Card, Emoji
from summon.bot.protection import is_group_user_content
from summon.bot.telegram_helpers import _group, _user
from summon.config import Settings
from summon.models import GroupSettings
from summon.rarities import rarity_label
from summon.services.collection import CollectionService

logger = logging.getLogger(__name__)


def register_group_events(
    app: Client,
    store: Any,
    settings: Settings,
    collection: CollectionService,
    respond: Any,
    send_photo_card: Any,
) -> None:
    @app.on_message(filters.incoming & filters.new_chat_members)
    async def welcome(_client: Client, message: Message) -> None:
        if not _group(message):
            return
        group = await store.groups.get(message.chat.id)
        if not group:
            return
        config = GroupSettings.model_validate(group["settings"])
        if config.welcome_enabled:
            names = ", ".join(
                user.first_name for user in (message.new_chat_members or []) if not user.is_bot
            )
            if names:
                await respond(
                    message, Card(f"Welcome, {names}", config.welcome_text, emoji=Emoji.SPARK)
                )

    @app.on_message(filters.incoming & filters.left_chat_member)
    async def goodbye(_client: Client, message: Message) -> None:
        departed = message.left_chat_member
        if not _group(message) or not departed or departed.is_bot:
            return
        group = await store.groups.get(message.chat.id)
        if not group:
            return
        config = GroupSettings.model_validate(group["settings"])
        if not config.goodbye_enabled:
            return
        # Bound service-event amplification per member and per group. These are
        # shared fixed windows; an event can fall across a bucket boundary.
        if not await store.limiter.allow(
            f"group:{message.chat.id}:goodbye:{departed.id}", limit=1, seconds=60
        ):
            return
        if not await store.limiter.allow(f"group:{message.chat.id}:goodbye", limit=10, seconds=60):
            return
        await respond(
            message,
            Card(f"Goodbye, {departed.first_name}", config.goodbye_text, emoji=Emoji.MOON),
        )

    @app.on_message(filters.incoming & filters.group)
    async def group_activity(_client: Client, message: Message) -> None:
        user = _user(message)
        summon_group_snapshot = getattr(message, "_summon_group_snapshot", None)
        if not user or not is_group_user_content(message):
            return
        # Ephemeral inputs and service events are not public group activity.
        if message.ephemeral_message_id is not None:
            return
        # Only genuinely new human-written text can trigger a public response.
        # Protection has already accepted the message; unknown slash commands,
        # forwards and inline/bot-authored content must never trigger replies.
        if (
            summon_group_snapshot
            and message.text
            and message.id > 0
            and not message.forward_origin
            and not message.automatic_forward
            and not message.via_bot
            and not message.sender_business_bot
        ):
            try:
                response = find_auto_reply(summon_group_snapshot, message.text)
                if (
                    response is not None
                    and await store.limiter.allow(
                        f"group:{message.chat.id}:auto-reply:user:{user.id}", limit=3, seconds=60
                    )
                    and await store.limiter.allow(
                        f"group:{message.chat.id}:auto-reply", limit=15, seconds=60
                    )
                    # Persist an opaque, 48-hour claim before sending. Replayed
                    # Telegram updates cannot retry an ambiguous external send.
                    and await store.limiter.claim_once(
                        f"group:{message.chat.id}:auto-reply:message:{message.id}",
                        seconds=48 * 60 * 60,
                    )
                ):
                    # Standard, escaped Wzgram output avoids rich-message retries;
                    # send_card retries only a definite custom-emoji rejection.
                    await respond(
                        message, Card("Group reply", response, emoji=Emoji.NOTE), standard=True
                    )
            except (AutoReplyStorageError, PyMongoError, RPCError, ConnectionError, OSError):
                # Never log public content or retry an ambiguous send. Collection
                # activity still runs if an optional auto-reply fails.
                logger.warning("Could not complete a group auto reply")
        now = datetime.now(UTC)
        await store.groups.ensure(message.chat.id, message.chat.title or "Group", now)
        event = await collection.on_message(
            message.chat.id, message.chat.title or "Group", user.id, now
        )
        if not event:
            return
        character = event["character"]
        card = Card(
            "A character appears",
            "A new companion is nearby. Guess their name with /summon name before they disappear!",
            emoji=Emoji.SPARK,
            fields=(
                ("Series", character["category"]),
                ("Rarity", rarity_label(character["rarity"])),
            ),
            field_icons=(Emoji.SERIES_TAG, Emoji.RARITY_STAR),
        )
        image = character.get("image_url", "")
        if image.startswith(("/art/", "/api/artwork/")):
            # Public art paths are rooted at the frontend origin even when the
            # Mini App URL itself contains a path.
            image = urljoin(settings.webapp_url, image) if settings.webapp_url else ""
        if not image:
            await store.collection.mark_delivery_failed(message.chat.id, event["spawn"]["id"])
            logger.warning("Spawn has no public artwork URL; spawn marked failed")
            return
        try:
            await send_photo_card(
                app,
                message.chat.id,
                image,
                card,
                premium=settings.premium_emoji_enabled,
                message_thread_id=message.message_thread_id,
            )
        except (BadRequest, Forbidden):
            # A text-only challenge provides no character artwork to guess.
            # Cancel only after a definite rejection; an ambiguous network
            # failure may have delivered, so leave that reservation to expire.
            await store.collection.mark_delivery_failed(message.chat.id, event["spawn"]["id"])
            logger.warning("Spawn artwork rejected; spawn marked failed")

    @app.on_edited_message(filters.incoming)
    async def ignore_edited_message(_client: Client, message: Message) -> None:
        # The outer middleware checks content; never execute edited commands or
        # count an edit as new group activity.
        return None
