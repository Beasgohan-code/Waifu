"""Consensual trade and gift commands and callbacks."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from wzgram import Client, filters
from wzgram.errors import BadRequest
from wzgram.types import CallbackQuery, Message

from summon.bot.keyboards import exchange_keyboard
from summon.bot.presentation import Card, Emoji, callback_text
from summon.bot.telegram_helpers import _group, _user, callback_data, command_input
from summon.config import Settings
from summon.services.gifting import GiftConflict, GiftForbidden, GiftNotFound, GiftService
from summon.services.trading import TradeConflict, TradeForbidden, TradeNotFound, TradingService

logger = logging.getLogger(__name__)


def register_exchange_commands(
    app: Client,
    store: Any,
    settings: Settings,
    trading: TradingService,
    gifting: GiftService,
    respond: Any,
) -> None:
    @app.on_message(filters.incoming & filters.command(["trade"]))
    async def propose_trade(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        reply = message.reply_to_message
        target = reply.from_user if reply and not getattr(reply, "sender_chat", None) else None
        args = (command.args or "").split()
        if not user or not _group(message) or not target or target.is_bot or len(args) != 2:
            await respond(
                message,
                Card(
                    "Make a trade",
                    "Reply to another player with /trade your-character-id their-character-id.",
                ),
            )
            return
        if not await store.limiter.allow(f"user:{user.id}:trade", limit=10, seconds=60):
            await respond(
                message,
                Card("Take a breath", "Too many trade actions; try again shortly."),
                private=True,
            )
            return
        try:
            trade = await trading.propose(user.id, target.id, args[0], args[1], datetime.now(UTC))
        except TradeConflict as exc:
            await respond(
                message, Card("Trade unavailable", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        markup = exchange_keyboard("trade", trade["id"], premium=settings.premium_emoji_enabled)
        await respond(
            message,
            Card(
                "Trade offer",
                (
                    f"{user.first_name} offered {args[0]} for {target.first_name}'s {args[1]}.\n"
                    "Only the recipient can accept; the offer expires in 15 minutes."
                ),
                emoji=Emoji.TRADE,
            ),
            markup=markup,
        )

    @app.on_callback_query(filters.regex("^trade:") | filters.regex(b"^trade:"))
    async def trade_callback(_client: Client, callback: CallbackQuery) -> None:
        parts = callback_data(callback).split(":")
        if (
            len(parts) != 3
            or parts[1] not in {"accept", "cancel"}
            or not re.fullmatch(r"[a-f0-9]{32}", parts[2])
        ):
            await callback.answer(
                callback_text("Invalid trade", emoji=Emoji.WARNING), show_alert=True
            )
            return
        if not await store.limiter.allow(
            f"user:{callback.from_user.id}:trade", limit=10, seconds=60
        ):
            await callback.answer(
                callback_text("Too many trade actions; try again shortly.", emoji=Emoji.HOURGLASS),
                show_alert=True,
            )
            return
        try:
            if parts[1] == "accept":
                await trading.confirm(parts[2], callback.from_user.id, datetime.now(UTC))
                result = "Trade completed. Your collections are updated."
            else:
                await store.trades.cancel(parts[2], callback.from_user.id, datetime.now(UTC))
                result = "Trade cancelled."
        except (TradeConflict, TradeForbidden, TradeNotFound) as exc:
            await callback.answer(callback_text(str(exc), emoji=Emoji.WARNING), show_alert=True)
            return
        icon = Emoji.DONE if parts[1] == "accept" else Emoji.CANCEL
        await callback.answer(callback_text(result, emoji=icon), show_alert=True)
        if callback.message and hasattr(callback.message, "edit_reply_markup"):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except BadRequest:
                logger.info("Trade completed but its old keyboard could not be removed")

    @app.on_message(filters.incoming & filters.command(["gift", "give"]))
    async def propose_gift(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        reply = message.reply_to_message
        target = reply.from_user if reply and not getattr(reply, "sender_chat", None) else None
        args = (command.args or "").split()
        if not user or not _group(message) or not target or target.is_bot or len(args) != 1:
            await respond(
                message,
                Card("Share a companion", "Reply to another player with /gift your-character-id."),
            )
            return
        if not await store.limiter.allow(f"user:{user.id}:gift", limit=10, seconds=60):
            await respond(
                message,
                Card("Take a breath", "Too many gift actions; try again shortly."),
                private=True,
            )
            return
        try:
            gift = await gifting.propose(user.id, target.id, args[0], datetime.now(UTC))
        except GiftConflict as exc:
            await respond(
                message, Card("Gift unavailable", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        markup = exchange_keyboard("gift", gift["id"], premium=settings.premium_emoji_enabled)
        await respond(
            message,
            Card(
                "A gift for you",
                f"{user.first_name} offered {args[0]} to {target.first_name}.\n"
                "Only the recipient can accept; the offer expires in 15 minutes.",
                emoji=Emoji.GIFT,
            ),
            markup=markup,
        )

    @app.on_callback_query(filters.regex("^gift:") | filters.regex(b"^gift:"))
    async def gift_callback(_client: Client, callback: CallbackQuery) -> None:
        parts = callback_data(callback).split(":")
        if (
            len(parts) != 3
            or parts[1] not in {"accept", "cancel"}
            or not re.fullmatch(r"[a-f0-9]{32}", parts[2])
        ):
            await callback.answer(
                callback_text("Invalid gift", emoji=Emoji.WARNING), show_alert=True
            )
            return
        if not await store.limiter.allow(
            f"user:{callback.from_user.id}:gift", limit=10, seconds=60
        ):
            await callback.answer(
                callback_text("Too many gift actions; try again shortly.", emoji=Emoji.HOURGLASS),
                show_alert=True,
            )
            return
        try:
            if parts[1] == "accept":
                await gifting.confirm(parts[2], callback.from_user.id, datetime.now(UTC))
                result = "Gift accepted. Your collection is updated."
            else:
                await store.gifts.cancel(parts[2], callback.from_user.id, datetime.now(UTC))
                result = "Gift cancelled."
        except (GiftConflict, GiftForbidden, GiftNotFound) as exc:
            await callback.answer(callback_text(str(exc), emoji=Emoji.WARNING), show_alert=True)
            return
        icon = Emoji.DONE if parts[1] == "accept" else Emoji.CANCEL
        await callback.answer(callback_text(result, emoji=icon), show_alert=True)
        if callback.message and hasattr(callback.message, "edit_reply_markup"):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except BadRequest:
                logger.info("Gift completed but its old keyboard could not be removed")
