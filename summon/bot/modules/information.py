"""Native Wzgram information feature handlers."""

from __future__ import annotations

import re

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.types import Message

from summon.bot.modules.context import CollectorContext
from summon.bot.presentation import Card, Emoji
from summon.bot.telegram_helpers import _user, command_input
from summon.rarities import rarity_label
from summon.repositories.character_numbers import CharacterNumberingUnavailable
from summon.repositories.rarity_weights import InvalidSpawnWeights


def register(context: CollectorContext) -> None:
    app = context.app
    store = context.store
    respond = context.respond
    collector_rate_allowed = context.rate_allowed
    collector_read_error = context.read_error

    @app.on_message(filters.incoming & filters.command(["ping"]))
    async def ping(_client: Client, message: Message) -> None:
        await respond(message, Card("Pong", "SUMMON is responding.", emoji=Emoji.SPARK))

    @app.on_message(filters.incoming & filters.command(["pinfo"]))
    async def premium_info(_client: Client, message: Message) -> None:
        if not _user(message) or message.from_user is None:
            return
        status = (
            "Telegram indicates that your account has Premium."
            if message.from_user.is_premium
            else "Telegram did not indicate a Premium subscription in this update."
        )
        await respond(message, Card("Telegram Premium", status, emoji=Emoji.STAR), private=True)

    @app.on_message(filters.incoming & filters.command(["check", "info"]))
    async def character_info(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        character_id = (command.args or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", character_id):
            await respond(
                message,
                Card("Character information", "Use /check followed by a character ID."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "character_info"):
            return
        try:
            character = await store.catalog.details(user.id, character_id)
        except CharacterNumberingUnavailable:
            await respond(
                message,
                Card(
                    "Numbering needs review",
                    "An operator must review conflicting or unusually large legacy catalogs. "
                    "Existing character IDs and ownership have not been changed.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        except PyMongoError:
            await collector_read_error(message)
            return
        if not character:
            await respond(
                message, Card("Not found", "That character is not in the catalog."), private=True
            )
            return
        display_id = str(character.get("number", character["id"]))
        if character.get("number") is not None and display_id != character["id"]:
            display_id += f" (catalog key: {character['id']})"
        await respond(
            message,
            Card(
                character["name"],
                character.get("description", ""),
                emoji=Emoji.SEARCH,
                fields=(
                    ("ID", display_id),
                    ("Series", character["category"]),
                    ("Rarity", rarity_label(character["rarity"])),
                    ("Your copies", str(character.get("quantity", 0))),
                ),
                field_icons=(None, Emoji.SERIES_TAG, Emoji.RARITY_STAR, Emoji.OPEN_COLLECTION),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["stats", "server"]))
    async def community_stats(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "community_stats"):
            return
        try:
            if not await store.limiter.allow("bot:community-stats", limit=30, seconds=60):
                await respond(message, Card("Please slow down", "Try again shortly."), private=True)
                return
            counts = await store.users.community_counts()
        except PyMongoError:
            await collector_read_error(message)
            return
        await respond(
            message,
            Card(
                "SUMMON community",
                "Aggregate database counts; no private player details are shown.",
                emoji=Emoji.STATS,
                fields=(
                    ("Collectors", str(counts["collectors"])),
                    ("Groups", str(counts["groups"])),
                    ("Active characters", str(counts["characters"])),
                ),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["chancelist", "clist", "claimlist"]))
    async def rarity_weights(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "rarity_weights"):
            return
        try:
            weights = await store.catalog.spawn_weights()
        except (PyMongoError, InvalidSpawnWeights, OSError, TimeoutError):
            await collector_read_error(message)
            return
        await respond(
            message,
            Card(
                "Summon rarity weights",
                "These weights apply to group summons and free /hclaim characters. "
                "Only active, nonempty buckets participate, so the resulting odds change "
                "when a rarity has no active characters. "
                "Legacy Epic shares the Special Edition bucket.",
                emoji=Emoji.RARITY_STAR,
                fields=tuple((rarity_label(name), str(weight)) for name, weight in weights.items()),
            ),
            private=True,
        )
