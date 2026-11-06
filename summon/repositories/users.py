"""Player profiles, collection progress, and non-wagering daily rewards."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pymongo import DESCENDING

from summon.auth import TelegramUser
from summon.models import player_level
from summon.repositories.base import transaction


class DailyAlreadyClaimed(ValueError):
    pass


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
        profile = await self.profile(user_id)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        caught_today = bool(
            await self.db.activity.find_one(
                {"user_id": user_id, "kind": "catch", "created_at": {"$gte": start}}, {"_id": 1}
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
            "achievements": achievements,
            "daily": [
                {
                    "id": "check-in",
                    "title": "Daily check-in",
                    "description": "Claim your daily points",
                    "completed": profile["last_daily"] == now.date().isoformat(),
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
        await self.touch(user, now)
        today = now.date().isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()

        async def grant(session: Any) -> dict[str, Any]:
            doc = await self.db.users.find_one({"_id": user.id}, session=session)
            if not doc:
                raise RuntimeError("Player missing during daily reward")
            if doc.get("last_daily") == today:
                raise DailyAlreadyClaimed("Today's reward has already been claimed")
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
