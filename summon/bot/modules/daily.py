"""Native Wzgram daily feature handlers."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.types import Message

from summon.bot.modules.context import CollectorContext
from summon.bot.presentation import Card, Emoji
from summon.bot.telegram_helpers import _user, command_input
from summon.rarities import rarity_label
from summon.repositories.collection import DailyCharacterAlreadyClaimed, DailyCharacterUnavailable
from summon.repositories.rarity_weights import InvalidSpawnWeights
from summon.repositories.users import DailyAlreadyClaimed, DailyStatusUnavailable


def register(context: CollectorContext) -> None:
    app = context.app
    store = context.store
    collection = context.collection
    respond = context.respond
    collector_rate_allowed = context.rate_allowed
    collector_read_error = context.read_error

    @app.on_message(filters.incoming & filters.command(["hclaim", "claim"]))
    async def daily_character(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        if (command_input(message).args or "").strip():
            await respond(
                message,
                Card("Free daily character", "Use /hclaim without arguments."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "daily_character"):
            return
        try:
            allowed = await store.limiter.allow("bot:daily-character", limit=100, seconds=60)
        except (PyMongoError, OSError, TimeoutError):
            await collector_read_error(message)
            return
        if not allowed:
            await respond(message, Card("Please slow down", "Try again shortly."), private=True)
            return
        if app.me is None or not isinstance(message.id, int) or message.id <= 0:
            await collector_read_error(message)
            return
        material = f"{app.me.id}:{message.chat.id}:{message.id}".encode("ascii")
        message_id = "daily-character-message:" + hashlib.sha256(material).hexdigest()
        try:
            character = await collection.claim_daily_character(
                user, datetime.now(UTC), message_id=message_id
            )
        except DailyCharacterAlreadyClaimed:
            await respond(
                message,
                Card(
                    "Daily character already claimed",
                    "You can claim another after 00:00 UTC.",
                    emoji=Emoji.HOURGLASS,
                ),
                private=True,
            )
            return
        except (DailyCharacterUnavailable, InvalidSpawnWeights):
            await respond(
                message,
                Card(
                    "No character is available",
                    "There must be an active character in a nonzero rarity bucket. Try later.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        except (PyMongoError, OSError, TimeoutError):
            await respond(
                message,
                Card(
                    "Claim unconfirmed",
                    "Check /collection before trying again; a claim may have succeeded.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "Daily character claimed",
                "Added to your collection for free. One character is available per UTC day.",
                emoji=Emoji.GIFT_ACTION,
                fields=(
                    ("Name", character["name"]),
                    ("ID", character["id"]),
                    ("Series", character["category"]),
                    ("Rarity", rarity_label(character["rarity"])),
                ),
                field_icons=(None, None, Emoji.SERIES_TAG, Emoji.RARITY_STAR),
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
