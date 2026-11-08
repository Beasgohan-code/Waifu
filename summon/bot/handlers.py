"""Compose SUMMON's native Wzgram message and callback handlers."""

from __future__ import annotations

from typing import Any

from wzgram import Client
from wzgram.errors import BadRequest, Forbidden
from wzgram.types import Message

from summon.bot.commands_catalog import register_catalog_commands
from summon.bot.commands_collector import register_collector_commands
from summon.bot.commands_exchange import register_exchange_commands
from summon.bot.commands_groups import register_group_commands
from summon.bot.events import register_group_events
from summon.bot.menu import COMMANDS, group_commands
from summon.bot.presentation import Card, Emoji, send_card, send_ephemeral_card, send_photo_card
from summon.bot.protection import GroupProtection
from summon.bot.telegram_helpers import _group
from summon.config import Settings
from summon.services.collection import CollectionService
from summon.services.gifting import GiftService
from summon.services.moderation import require_group_admin
from summon.services.trading import TradingService

__all__ = ["COMMANDS", "group_commands", "register_handlers"]


def register_handlers(app: Client, store: Any, settings: Settings) -> None:
    """Register native handlers before starting the single update client."""
    collection = CollectionService(store.collection, store.catalog)
    trading = TradingService(store.trades)
    gifting = GiftService(store.gifts)
    GroupProtection(store.groups).register(app)

    async def respond(
        message: Message,
        card: Card,
        *,
        markup: Any | None = None,
        private: bool = False,
        standard: bool = False,
    ) -> Any:
        if message.chat is None:
            return None
        if private and _group(message) and message.from_user:
            try:
                return await send_ephemeral_card(
                    app,
                    message.chat.id,
                    message.from_user.id,
                    card,
                    premium=settings.premium_emoji_enabled,
                    message_thread_id=message.message_thread_id,
                    incoming_ephemeral_id=message.ephemeral_message_id,
                )
            except (Forbidden, BadRequest):
                # This notice contains no private card content.
                return await send_card(
                    app,
                    message.chat.id,
                    Card(
                        "Open your private chat",
                        "Start SUMMON privately to see this response.",
                        emoji=Emoji.PRIVATE,
                    ),
                    rich=False,
                    premium=settings.premium_emoji_enabled,
                    message_thread_id=message.message_thread_id,
                )
        return await send_card(
            app,
            message.chat.id,
            card,
            rich=not standard and settings.rich_enabled and settings.bot_api_rich_enabled,
            premium=settings.premium_emoji_enabled,
            reply_markup=markup,
            message_thread_id=message.message_thread_id,
        )

    register_catalog_commands(app, store, settings, respond)
    register_collector_commands(app, store, settings, collection, respond)
    register_exchange_commands(app, store, settings, trading, gifting, respond)
    register_group_commands(app, store, respond, require_group_admin)
    register_group_events(app, store, settings, collection, respond, send_photo_card)
