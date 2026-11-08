"""Collector commands and private progression views."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.types import Message

from summon.bot.keyboards import collection_keyboard
from summon.bot.presentation import Card, Emoji
from summon.bot.telegram_helpers import _group, _user, command_input
from summon.config import Settings
from summon.rarities import RARITY_WEIGHTS, rarity_label
from summon.repositories.users import DailyAlreadyClaimed, DailyStatusUnavailable, daily_status
from summon.services.collection import CollectionService, GuessRateLimited


def register_collector_commands(
    app: Client, store: Any, settings: Settings, collection: CollectionService, respond: Any
) -> None:
    @app.on_message(filters.incoming & filters.command(["start"]))
    async def start(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        await store.users.touch(user, datetime.now(UTC))
        keyboard = None
        if not _group(message) and settings.webapp_url:
            keyboard = collection_keyboard(
                settings.webapp_url, premium=settings.premium_emoji_enabled
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

    @app.on_message(filters.incoming & filters.command(["help", "commands", "menu"]))
    async def help_command(_client: Client, message: Message) -> None:
        user = _user(message)
        operator_fields = (
            (("Catalog operator", "/upload · /addchar · /updatechar (private chat)"),)
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
                    ("Collect", "/summon name · /collection · /search name · /check id"),
                    ("Favorites", "/favorites · /favorite id on|off · /fav id"),
                    ("Progress", "/profile · /balance · /daily · /streak · /achievements · /top"),
                    ("Trade & gift", "Reply with /trade your-id their-id or /gift your-id"),
                    ("Group", "/manage · /spawns · /checkspawn · /rules · /notes"),
                    ("Moderation", "/warn · /warnings · /unwarn · /ban"),
                    ("Other actions", "Use Telegram to mute, unmute, unban or kick."),
                )
                + operator_fields,
            ),
        )

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
        inventory = await store.catalog.inventory(user.id, limit=12)
        fields = tuple(
            (item["name"], f"{item['rarity'].title()} · ×{item['quantity']}")
            for item in inventory["items"]
        )
        await respond(
            message,
            Card(
                "Your collection",
                f"{inventory['total']} unique characters."
                if fields
                else (
                    "Your collection is waiting for its first character. "
                    "Guess a summon in a group to begin."
                ),
                emoji=Emoji.DIAMOND,
                fields=fields,
            ),
            private=True,
        )

    async def collector_rate_allowed(message: Message, actor_id: int, action: str) -> bool:
        try:
            allowed = await store.limiter.allow(
                f"user:{actor_id}:collector:{action}", limit=30, seconds=60
            )
        except PyMongoError:
            await respond(
                message,
                Card("Collector tools unavailable", "Please try again later."),
                private=True,
            )
            return False
        if not allowed:
            await respond(
                message,
                Card("Please slow down", "Too many requests; try again shortly."),
                private=True,
            )
        return bool(allowed)

    async def collector_read_error(message: Message) -> None:
        await respond(
            message,
            Card("Collector tools unavailable", "Could not read your results; try again later."),
            private=True,
        )

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
        except PyMongoError:
            await collector_read_error(message)
            return
        if not character:
            await respond(
                message, Card("Not found", "That character is not in the catalog."), private=True
            )
            return
        await respond(
            message,
            Card(
                character["name"],
                character.get("description", ""),
                emoji=Emoji.SEARCH,
                fields=(
                    ("ID", character["id"]),
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

    @app.on_message(filters.incoming & filters.command(["chancelist", "clist"]))
    async def rarity_weights(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "rarity_weights"):
            return
        await respond(
            message,
            Card(
                "Summon rarity weights",
                "Base weights apply only to rarities with active characters; "
                "the resulting odds change when a rarity has no active characters. "
                "Legacy Epic shares the Special Edition bucket.",
                emoji=Emoji.RARITY_STAR,
                fields=tuple(
                    (rarity_label(name), str(weight)) for name, weight in RARITY_WEIGHTS.items()
                ),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["search", "find"]))
    async def search_characters(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        term = (command.args or "").strip()
        if not term or len(term) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in term):
            await respond(
                message,
                Card("Search characters", "Use /search followed by 1–80 characters of a name."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "search"):
            return
        try:
            page = await store.catalog.list(user.id, search=term, limit=10)
        except PyMongoError:
            await collector_read_error(message)
            return
        fields = tuple(
            (item["name"], f"{item['id']} · {item['rarity'].title()}") for item in page["items"]
        )
        body = (
            f"Showing {len(fields)} of {page['total']} matches. Search matches name prefixes."
            if fields
            else "No matching characters found. Search matches the beginning of a name."
        )
        await respond(
            message, Card("Character search", body, emoji=Emoji.SEARCH, fields=fields), private=True
        )

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

    @app.on_message(
        filters.incoming & filters.command(["achievements", "progress", "ach", "badges"])
    )
    async def show_achievements(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "achievements"):
            return
        try:
            progress = await store.users.progression(user.id, datetime.now(UTC))
        except PyMongoError:
            await collector_read_error(message)
            return
        fields = tuple(
            (
                item["title"],
                f"{item['current']} / {item['target']} · "
                f"{'Complete' if item['completed'] else 'In progress'}",
            )
            for item in progress["achievements"]
        ) + tuple(
            (item["title"], "Complete" if item["completed"] else "Not complete")
            for item in progress["daily"]
        )
        body = "Your milestones and daily goals. Use /profile to see your level."
        await respond(
            message, Card("Your achievements", body, emoji=Emoji.STATS, fields=fields), private=True
        )

    @app.on_message(filters.incoming & filters.command(["profile", "me", "hstats"]))
    async def profile(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        if command and command.command in {"me", "hstats"} and command.args:
            await respond(
                message,
                Card("Your collector profile", "This command shows only your own profile."),
                private=True,
            )
            return
        await store.users.touch(user, datetime.now(UTC))
        profile_data = await store.users.profile(user.id)
        await respond(
            message,
            Card(
                "Collector profile",
                user.first_name,
                emoji=Emoji.STATS,
                fields=(
                    ("Level", str(profile_data["level"])),
                    ("Points", str(profile_data["points"])),
                    (
                        "Collection",
                        f"{profile_data['unique_characters']} / {profile_data['catalog_size']}",
                    ),
                    ("Total summons", str(profile_data["catch_count"])),
                ),
                field_icons=(
                    Emoji.RARITY_STAR,
                    Emoji.REWARD_COIN,
                    Emoji.OPEN_COLLECTION,
                    Emoji.SPARK,
                ),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["balance", "bal"]))
    async def balance(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "balance"):
            return
        try:
            data = await store.users.profile(user.id)
        except PyMongoError:
            await collector_read_error(message)
            return
        await respond(
            message,
            Card(
                "Collector balance",
                "Points are in-game progress, not money or a cash balance.",
                emoji=Emoji.REWARD_COIN,
                fields=(
                    ("Points", str(data["points"])),
                    ("Characters", str(data["unique_characters"])),
                ),
                field_icons=(Emoji.REWARD_COIN, Emoji.OPEN_COLLECTION),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["streak"]))
    async def streak(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "streak"):
            return
        try:
            data = await store.users.profile(user.id)
        except PyMongoError:
            await collector_read_error(message)
            return
        status = daily_status(data.get("last_daily"), datetime.now(UTC))["state"]
        daily_message = {
            "available": "Your daily reward is available with /daily.",
            "claimed": "Today's daily reward has already been claimed.",
            "unavailable": "Daily status needs review; please contact support.",
        }[status]
        await respond(
            message,
            Card(
                "Daily streak",
                f"{daily_message} The streak is recalculated on your next claim, using UTC.",
                emoji=Emoji.SPARK,
                fields=(("Last recorded streak", f"{data.get('streak', 0)} days"),),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["daily"]))
    async def daily(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        try:
            reward = await store.users.daily(user, datetime.now(UTC))
        except DailyAlreadyClaimed:
            await respond(
                message,
                Card(
                    "All caught up",
                    "Today's reward has already been claimed. Come back after 00:00 UTC.",
                    emoji=Emoji.HOURGLASS,
                ),
                private=True,
            )
            return
        except DailyStatusUnavailable:
            await respond(
                message,
                Card(
                    "Daily unavailable",
                    "Your daily status needs review. Please contact support.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "Daily reward",
                "A little something for your journey.",
                emoji=Emoji.COIN,
                fields=(
                    ("Points", f"+{reward['points']}"),
                    ("Experience", "+20 XP"),
                    ("Streak", f"{reward['streak']} days"),
                ),
                field_icons=(Emoji.REWARD_COIN, Emoji.RARITY_STAR, Emoji.SPARK),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["top", "leaderboard", "rank"]))
    async def leaderboard(_client: Client, message: Message) -> None:
        rows = await store.users.leaderboard(limit=10)
        fields = tuple(
            (f"{index}. {row['name']}", f"{row['catch_count']} summons")
            for index, row in enumerate(rows, 1)
        )
        await respond(
            message,
            Card(
                "Collectors leaderboard",
                "The community's most active collectors."
                if fields
                else "The leaderboard is waiting for its first collector.",
                emoji=Emoji.CROWN,
                fields=fields,
            ),
        )
