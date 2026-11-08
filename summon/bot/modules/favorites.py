"""Native Wzgram favorites feature handlers."""

from __future__ import annotations

import re

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.types import Message

from summon.bot.modules.context import CollectorContext
from summon.bot.presentation import Card, Emoji
from summon.bot.telegram_helpers import _user, command_input


def register(context: CollectorContext) -> None:
    app = context.app
    store = context.store
    respond = context.respond
    collector_rate_allowed = context.rate_allowed
    collector_read_error = context.read_error

    @app.on_message(filters.incoming & filters.command(["favorites"]))
    async def show_favorites(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "favorites"):
            return
        try:
            page = await store.catalog.inventory(user.id, favorite=True, limit=10)
        except PyMongoError:
            await collector_read_error(message)
            return
        fields = tuple(
            (item["name"], f"{item['id']} · ×{item['quantity']}") for item in page["items"]
        )
        body = (
            f"Showing {len(fields)} of {page['total']} favorites. Open the Mini App for more."
            if fields
            else "You have no favorites yet. Use /favorite character-id on."
        )
        await respond(
            message, Card("Your favorites", body, emoji=Emoji.HEART, fields=fields), private=True
        )

    @app.on_message(filters.incoming & filters.command(["favorite", "fav"]))
    async def update_favorite(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        parts = (command.args or "").split()
        # The legacy shorthand adds one owned favorite; /favorite keeps its
        # existing explicit on/off contract. Never alter another user's data.
        if getattr(command, "command", "") == "fav" and len(parts) == 1:
            parts.append("on")
        if (
            len(parts) != 2
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", parts[0])
            or parts[1] not in {"on", "off"}
        ):
            await respond(
                message,
                Card("Update a favorite", "Use /favorite character-id on or off."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "favorite"):
            return
        try:
            changed = await store.catalog.set_favorite(user.id, parts[0], parts[1] == "on")
        except PyMongoError:
            await respond(
                message,
                Card(
                    "Result uncertain",
                    "Could not confirm the change. Check /favorites before retrying.",
                ),
                private=True,
            )
            return
        if not changed:
            await respond(
                message,
                Card("Not in your collection", "Only owned characters can be favorited."),
                private=True,
            )
            return
        text = "Added to favorites." if parts[1] == "on" else "Removed from favorites."
        await respond(message, Card("Favorite updated", text, emoji=Emoji.HEART), private=True)
