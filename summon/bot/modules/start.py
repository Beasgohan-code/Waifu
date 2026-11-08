"""Native Wzgram start feature handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.types import CallbackQuery, Message

from summon.bot.keyboards import start_keyboard
from summon.bot.modules.context import CollectorContext
from summon.bot.presentation import Card, Emoji, callback_text
from summon.bot.telegram_helpers import _group, _user, callback_data, telegram_user


def register(context: CollectorContext) -> None:
    app = context.app
    store = context.store
    settings = context.settings
    respond = context.respond

    @app.on_message(filters.incoming & filters.command(["start"]))
    async def start(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        await store.users.touch(user, datetime.now(UTC))
        keyboard = None
        if not _group(message):
            bot_user = getattr(app, "me", None)
            keyboard = start_keyboard(
                settings.webapp_url,
                getattr(bot_user, "username", None),
                user.id,
                premium=settings.premium_emoji_enabled,
            )
        await respond(
            message,
            Card(
                "Welcome to SUMMON",
                (
                    "Collect. Discover. Summon.\n\nCharacters appear as your group chats. "
                    "Guess their names, grow your collection, and trade with friends."
                ),
                emoji=Emoji.SUMMON_BRAND,
                fields=(("Get started", "/help"), ("For group admins", "/manage")),
                field_icons=(Emoji.QUESTION, Emoji.ADMIN_SHIELD),
            ),
            markup=keyboard,
        )

    @app.on_callback_query(filters.regex("^start-guide:") | filters.regex(b"^start-guide:"))
    async def start_guide(_client: Client, callback: CallbackQuery) -> None:
        actor = telegram_user(callback.from_user)
        parts = callback_data(callback).split(":")
        chat = getattr(callback.message, "chat", None)
        kind: Any = getattr(chat, "type", None)
        if (
            actor is None
            or len(parts) != 2
            or parts[1] != str(actor.id)
            or chat is None
            or chat.id != actor.id
            or getattr(kind, "value", kind) != "private"
        ):
            await callback.answer(
                callback_text("This menu belongs to another collector.", emoji=Emoji.WARNING),
                show_alert=True,
            )
            return
        try:
            allowed = await store.limiter.allow(
                f"user:{actor.id}:start-guide", limit=20, seconds=60
            )
        except (PyMongoError, OSError, TimeoutError):
            allowed = False
        if not allowed:
            await callback.answer(
                callback_text("Try again shortly.", emoji=Emoji.HOURGLASS), show_alert=True
            )
            return
        await callback.answer(callback_text("Here is your guide.", emoji=Emoji.QUESTION))
        await respond(
            callback.message,
            Card(
                "Your first summon",
                "Add SUMMON to a group and ask an administrator to enable group spawns. "
                "When a character appears, guess their name with /summon. "
                "Collect a free character with /hclaim and explore your collection "
                "with /collection.",
                emoji=Emoji.SUMMON_BRAND,
                fields=(("Full guide", "/help"), ("Group setup", "/manage")),
                field_icons=(Emoji.QUESTION, Emoji.ADMIN_SHIELD),
            ),
        )

    @app.on_message(filters.incoming & filters.command(["help", "commands", "menu"]))
    async def help_command(_client: Client, message: Message) -> None:
        user = _user(message)
        operator_fields = (
            (
                ("Catalog operator", "/upload · /addchar · /updatechar (private chat)"),
                (
                    "Media upload",
                    'Reply with /upload "Name" "Anime" 1 or /updatechar exact-id media; '
                    "choose a host, review, then publish or cancel.",
                ),
            )
            if user and user.id in settings.operator_ids and not _group(message)
            else ()
        )
        await respond(
            message,
            Card(
                "Your SUMMON guide",
                "A little universe for collectors and their communities.",
                emoji=Emoji.QUESTION,
                fields=(
                    (
                        "Collect",
                        "/summon name · /collection · /hmode · /search name (/sips) · /check id",
                    ),
                    ("Favorites", "/favorites · /favorite id on|off · /fav id"),
                    (
                        "Progress",
                        "/profile · /balance · /daily · /hclaim · /streak · /achievements · /top",
                    ),
                    ("Trade & gift", "Reply with /trade your-id their-id or /gift your-id"),
                    (
                        "Group",
                        "/manage · /spawns · /spawn · /checkspawn · /groupstats · /rules · /notes",
                    ),
                    ("Moderation", "/warn · /warnings · /unwarn · /ban"),
                    ("Other actions", "Use Telegram to mute, unmute, unban or kick."),
                )
                + operator_fields,
            ),
        )
