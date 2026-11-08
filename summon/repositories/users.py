"""Player profiles, collection progress, and non-wagering daily rewards."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from pymongo import DESCENDING

from summon.auth import TelegramUser
from summon.models import CollectionMode, player_level
from summon.repositories.base import transaction


class DailyAlreadyClaimed(ValueError):
    pass


class DailyStatusUnavailable(RuntimeError):
    """The stored daily date is invalid or ahead of the current UTC day."""


def _as_utc(now: datetime) -> datetime:
    if now.utcoffset() is None:
        raise ValueError("A timezone-aware server timestamp is required")
    return now.astimezone(UTC)


def daily_status(last_daily: str | None, now: datetime) -> dict[str, str]:
    """Evaluate daily eligibility using server UTC time, never a device clock."""
    now = _as_utc(now)
    today = now.date()
    try:
        last = date.fromisoformat(last_daily) if last_daily is not None else None
        if last is not None and last.isoformat() != last_daily:
            raise ValueError("Noncanonical daily date")
        if last is None or last < today:
            state = "available"
        elif last == today:
            state = "claimed"
        else:
            state = "unavailable"
    except (TypeError, ValueError):
        state = "unavailable"
    reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"state": state, "server_time": now.isoformat(), "next_reset_at": reset.isoformat()}


class UserRepository:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def touch(self, user: TelegramUser, now: datetime) -> None:
        await self.db.users.update_one(
            {"_id": user.id},
            {
                "$set": {
                    "first_name": user.first_name,
                    "username": user.username,
                    "last_seen": now,
                },
                "$setOnInsert": {"created_at": now, "points": 0, "xp": 0, "catch_count": 0},
            },
            upsert=True,
        )

    async def collection_mode(self, user_id: int) -> CollectionMode:
        row = await self.db.users.find_one({"_id": user_id}, {"collection_mode": 1})
        value = row.get("collection_mode") if row else None
        if isinstance(value, str) and value in {"recent", "copies", "favorites"}:
            return cast(CollectionMode, value)
        return "recent"

    async def set_collection_mode(
        self, user: TelegramUser, mode: CollectionMode, now: datetime
    ) -> None:
        if mode not in {"recent", "copies", "favorites"}:
            raise ValueError("Unsupported collection mode")
        await self.touch(user, now)
        await self.db.users.update_one({"_id": user.id}, {"$set": {"collection_mode": mode}})

    async def profile(self, user_id: int) -> dict[str, Any]:
        doc = await self.db.users.find_one({"_id": user_id}) or {"_id": user_id}
        unique = await self.db.inventory.count_documents(
            {"user_id": user_id, "quantity": {"$gt": 0}}
        )
        total_characters = await self.db.characters.count_documents({"active": True})
        xp = doc.get("xp", 0)
        return {
            "id": user_id,
            "first_name": doc.get("first_name", "Collector"),
            "username": doc.get("username"),
            "points": doc.get("points", 0),
            "xp": xp,
            "level": player_level(xp),
            "catch_count": doc.get("catch_count", 0),
            "unique_characters": unique,
            "catalog_size": total_characters,
            "streak": doc.get("streak", 0),
            "last_daily": doc.get("last_daily"),
        }

    async def community_counts(self) -> dict[str, int]:
        """Bounded aggregate counts only; never return individual user or group data."""
        return {
            "collectors": await self.db.users.count_documents({}, maxTimeMS=2000),
            "groups": await self.db.groups.count_documents({}, maxTimeMS=2000),
            "characters": await self.db.characters.count_documents(
                {"active": True}, maxTimeMS=2000
            ),
        }

    async def leaderboard(self, limit: int = 20) -> list[dict[str, Any]]:
        cursor = (
            self.db.users.find({"catch_count": {"$gt": 0}})
            .sort([("catch_count", DESCENDING), ("_id", 1)])
            .limit(limit)
        )
        return [
            {
                "name": row.get("first_name", "Collector"),
                "catch_count": row["catch_count"],
                "level": player_level(row.get("xp", 0)),
            }
            async for row in cursor
        ]

    async def progression(self, user_id: int, now: datetime) -> dict[str, Any]:
        now = _as_utc(now)
        profile = await self.profile(user_id)
        status = daily_status(profile["last_daily"], now)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        caught_today = bool(
            await self.db.activity.find_one(
                {
                    "user_id": user_id,
                    "kind": "catch",
                    "created_at": {"$gte": start, "$lt": start + timedelta(days=1)},
                },
                {"_id": 1},
            )
        )
        achievements = [
            {
                "id": "first-summon",
                "title": "First encounter",
                "description": "Collect your first character",
                "current": min(profile["catch_count"], 1),
                "target": 1,
            },
            {
                "id": "collector-five",
                "title": "A growing universe",
                "description": "Collect 5 unique characters",
                "current": min(profile["unique_characters"], 5),
                "target": 5,
            },
            {
                "id": "streak-three",
                "title": "A little ritual",
                "description": "Reach a 3-day check-in streak",
                "current": min(profile["streak"], 3),
                "target": 3,
            },
            {
                "id": "level-five",
                "title": "On your way",
                "description": "Reach collector level 5",
                "current": min(profile["level"], 5),
                "target": 5,
            },
        ]
        for item in achievements:
            item["completed"] = item["current"] >= item["target"]
        return {
            "daily_status": status,
            "achievements": achievements,
            "daily": [
                {
                    "id": "check-in",
                    "title": "Daily check-in",
                    "description": "Claim your daily points",
                    "completed": status["state"] == "claimed",
                    "reward": "100+ points",
                },
                {
                    "id": "summon-one",
                    "title": "Meet a companion",
                    "description": "Catch a character in a group",
                    "completed": caught_today,
                    "reward": "+40 points on catch",
                },
            ],
        }

    async def daily(self, user: TelegramUser, now: datetime) -> dict[str, Any]:
        now = _as_utc(now)
        await self.touch(user, now)
        today = now.date().isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()

        async def grant(session: Any) -> dict[str, Any]:
            doc = await self.db.users.find_one({"_id": user.id}, session=session)
            if not doc:
                raise RuntimeError("Player missing during daily reward")
            state = daily_status(doc.get("last_daily"), now)["state"]
            if state == "claimed":
                raise DailyAlreadyClaimed("Today's reward has already been claimed")
            if state != "available":
                raise DailyStatusUnavailable("Daily status is unavailable; contact support")
            streak = doc.get("streak", 0) + 1 if doc.get("last_daily") == yesterday else 1
            points = 100 + min(streak - 1, 6) * 10
            await self.db.users.update_one(
                {"_id": user.id},
                {
                    "$set": {"last_daily": today, "streak": streak},
                    "$inc": {"points": points, "xp": 20},
                },
                session=session,
            )
            await self.db.reward_history.insert_one(
                {
                    "_id": f"daily:{user.id}:{today}",
                    "user_id": user.id,
                    "kind": "daily",
                    "points": points,
                    "xp": 20,
                    "created_at": now,
                },
                session=session,
            )
            return {"points": points, "xp": 20, "streak": streak, "date": today}

        return await transaction(self.client, grant)
