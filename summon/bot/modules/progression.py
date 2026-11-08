"""Native Wzgram progression feature handlers."""

from __future__ import annotations

from datetime import UTC, datetime

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.types import Message

from summon.bot.modules.context import CollectorContext
from summon.bot.presentation import Card, Emoji
from summon.bot.telegram_helpers import _user, command_input
from summon.rarities import rarity_label
from summon.repositories.collection_insights import InsightsDataError
from summon.repositories.users import DailyStatusUnavailable, daily_status


def register(context: CollectorContext) -> None:
    app = context.app
    store = context.store
    respond = context.respond
    collector_rate_allowed = context.rate_allowed
    collector_read_error = context.read_error

    @app.on_message(
        filters.incoming & filters.command(["achievements", "progress", "ach", "badges"])
    )
    async def show_achievements(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "achievements"):
            return
        try:
            progress = await store.users.progression(user.id, datetime.now(UTC))
        except (PyMongoError, DailyStatusUnavailable):
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
        if command.args:
            await respond(
                message,
                Card("Your collector profile", "This command shows only your own profile."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "profile"):
            return
        try:
            await store.users.touch(user, datetime.now(UTC))
            profile_data = await store.users.profile(user.id)
            insights = await store.users.collection_insights(user.id)
        except (PyMongoError, InsightsDataError, DailyStatusUnavailable, TimeoutError):
            await collector_read_error(message)
            return
        goal = next((item for item in insights["goals"] if not item["completed"]), None)
        rarity_lines = [
            f"{rarity_label(item['rarity'])}: {item['unique']} unique · {item['copies']} copies"
            for item in insights["rarity_breakdown"]
        ]
        await respond(
            message,
            Card(
                "Collector profile",
                user.first_name + " · Collection goals reflect what you own now.",
                emoji=Emoji.STATS,
                fields=(
                    ("Level", str(profile_data["level"])),
                    ("Points", str(profile_data["points"])),
                    (
                        "Active catalogue",
                        f"{insights['active_owned']} / {insights['catalog_size']}",
                    ),
                    ("Owned copies", str(insights["total_copies"])),
                    ("Duplicate copies", str(insights["duplicate_copies"])),
                    ("Favorites", str(insights["favorite_characters"])),
                    ("Group catches", str(profile_data["catch_count"])),
                    ("Best recorded streak", f"{profile_data['best_streak']} days"),
                    (
                        "Next goal",
                        f"{goal['title']} · {goal['current']}/{goal['target']}"
                        if goal
                        else "Current collection goals complete",
                    ),
                ),
                details=("Rarity distribution", "\n".join(rarity_lines) or "No characters yet."),
                field_icons=(
                    Emoji.RARITY_STAR,
                    Emoji.REWARD_COIN,
                    Emoji.OPEN_COLLECTION,
                    Emoji.OPEN_COLLECTION,
                    Emoji.OPEN_COLLECTION,
                    Emoji.HEART,
                    Emoji.SPARK,
                    Emoji.HOURGLASS,
                    Emoji.STAR,
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
        except (PyMongoError, DailyStatusUnavailable):
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
        except (PyMongoError, DailyStatusUnavailable):
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
                fields=(
                    ("Last recorded streak", f"{data.get('streak', 0)} days"),
                    (
                        "Best recorded streak",
                        f"{data.get('best_streak', data.get('streak', 0))} days",
                    ),
                ),
            ),
            private=True,
        )


def register_leaderboard(context: CollectorContext) -> None:
    app = context.app
    store = context.store
    respond = context.respond

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
