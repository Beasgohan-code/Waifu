"""Native Wzgram collection feature handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.errors import BadRequest, RPCError
from wzgram.types import CallbackQuery, Message

from summon.bot.keyboards import collection_mode_keyboard
from summon.bot.modules.context import CollectorContext
from summon.bot.presentation import Card, Emoji, callback_text, edit_keyboard
from summon.bot.telegram_helpers import _group, _user, callback_data, command_input, telegram_user
from summon.models import CollectionMode
from summon.rarities import rarity_label
from summon.services.collection import GuessRateLimited


def register(context: CollectorContext) -> None:
    app = context.app
    store = context.store
    settings = context.settings
    collection = context.collection
    respond = context.respond
    collector_rate_allowed = context.rate_allowed
    collector_read_error = context.read_error

    @app.on_message(filters.incoming & filters.command(["summon", "guess", "collect", "grab"]))
    async def guess(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user or not _group(message):
            await respond(
                message,
                Card(
                    "Find a summon",
                    "Use this command in a group when a character appears.",
                    emoji=Emoji.SEARCH,
                ),
            )
            return
        if not command.args:
            await respond(
                message,
                Card(
                    "Make a guess",
                    "Use /summon followed by the character's name.",
                    emoji=Emoji.SEARCH,
                ),
            )
            return
        try:
            character = await collection.guess(
                message.chat.id, user.model_dump(), command.args, datetime.now(UTC)
            )
        except GuessRateLimited:
            await respond(
                message,
                Card(
                    "Take a breath",
                    "Wait a few seconds before guessing again.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        if not character:
            await respond(
                message,
                Card(
                    "Not quite",
                    "That name doesn't match an active summon, or it has already ended.",
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "A new companion!",
                f"{user.first_name} collected {character['name']}.",
                emoji=Emoji.STAR,
                fields=(
                    ("Series", character["category"]),
                    ("Rarity", rarity_label(character["rarity"])),
                    ("Reward", "+40 points · +25 XP"),
                ),
                field_icons=(Emoji.SERIES_TAG, Emoji.RARITY_STAR, Emoji.REWARD_COIN),
            ),
        )

    @app.on_message(filters.incoming & filters.command(["collection", "inventory", "harem", "inv"]))
    async def show_collection(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        if not await collector_rate_allowed(message, user.id, "collection"):
            return
        try:
            mode = await store.users.collection_mode(user.id)
            options: dict[str, Any] = {"limit": 12}
            if mode == "copies":
                options["sort"] = "copies"
            elif mode == "favorites":
                options["favorite"] = True
            inventory = await store.catalog.inventory(user.id, **options)
        except (PyMongoError, OSError, TimeoutError):
            await collector_read_error(message)
            return
        fields = tuple(
            (item["name"], f"{rarity_label(item['rarity'])} · ×{item['quantity']}")
            for item in inventory["items"]
        )
        empty = (
            "You have no favorites yet. Use /favorite character-id on."
            if mode == "favorites"
            else "Your collection is waiting for its first character. Guess a summon to begin."
        )
        await respond(
            message,
            Card(
                "Your collection",
                f"{inventory['total']} unique characters · {mode} mode." if fields else empty,
                emoji=Emoji.DIAMOND,
                fields=fields,
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["hmode"]))
    async def collection_display_mode(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        value = (command_input(message).args or "").strip().casefold()
        value = {"fav": "favorites", "favorite": "favorites"}.get(value, value)
        if value and value not in {"recent", "copies", "favorites"}:
            await respond(
                message,
                Card(
                    "Collection display",
                    "Use /hmode recent, /hmode copies or /hmode favorites.",
                    emoji=Emoji.GEAR,
                ),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "collection-mode"):
            return
        try:
            if value:
                mode = cast(CollectionMode, value)
                await store.users.set_collection_mode(user, mode, datetime.now(UTC))
            else:
                mode = await store.users.collection_mode(user.id)
        except (PyMongoError, OSError, TimeoutError):
            # A failed write may have committed; don't replay it automatically.
            await respond(
                message,
                Card(
                    "Display mode unavailable",
                    "Check /hmode before trying again.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "Collection display",
                "Choose how /collection shows your characters: recent, copies or favorites.",
                emoji=Emoji.GEAR,
                fields=(("Current mode", mode),),
            ),
            markup=(
                collection_mode_keyboard(user.id, mode, premium=settings.premium_emoji_enabled)
                if not _group(message)
                else None
            ),
            private=True,
        )

    @app.on_callback_query(filters.regex("^hmode:") | filters.regex(b"^hmode:"))
    async def collection_mode_callback(_client: Client, callback: CallbackQuery) -> None:
        actor = telegram_user(callback.from_user)
        parts = callback_data(callback).split(":")
        if (
            actor is None
            or len(parts) != 3
            or not parts[1].isdigit()
            or len(parts[1]) > 16
            or int(parts[1]) != actor.id
            or parts[2] not in {"recent", "copies", "favorites"}
        ):
            await callback.answer(
                callback_text(
                    "This menu belongs to another collector or has expired.", emoji=Emoji.WARNING
                ),
                show_alert=True,
            )
            return
        try:
            if not await store.limiter.allow(
                f"user:{actor.id}:collector:collection-mode", limit=30, seconds=60
            ):
                await callback.answer(
                    callback_text("Too many requests; try again shortly.", emoji=Emoji.HOURGLASS),
                    show_alert=True,
                )
                return
            mode = cast(CollectionMode, parts[2])
            await store.users.set_collection_mode(actor, mode, datetime.now(UTC))
        except (PyMongoError, OSError, TimeoutError):
            await callback.answer(
                callback_text("Check /hmode before trying again.", emoji=Emoji.WARNING),
                show_alert=True,
            )
            return
        await callback.answer(callback_text(f"Collection mode: {mode}", emoji=Emoji.DONE))
        if callback.message and hasattr(callback.message, "edit_reply_markup"):
            try:
                await edit_keyboard(
                    callback.message,
                    collection_mode_keyboard(
                        actor.id, mode, premium=settings.premium_emoji_enabled
                    ),
                    premium=settings.premium_emoji_enabled,
                )
            except (BadRequest, RPCError, OSError, TimeoutError):
                # The preference is already saved; never replay the mutation.
                pass
