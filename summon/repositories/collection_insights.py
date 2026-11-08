"""Private, read-only collection statistics shared by Telegram and the Mini App.

The report is computed from current ownership in one MongoDB snapshot. It does
not grant rewards, expose another player's collection, or persist achievements.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from summon.rarities import RARITY_VALUES

MAX_SAFE_INTEGER = 2**53 - 1
UNKNOWN_RARITY = "unknown"


class InsightsDataError(RuntimeError):
    """Stored inventory data cannot safely be summarized."""


class RarityCount(TypedDict):
    rarity: str
    unique: int
    copies: int


class CollectionGoal(TypedDict):
    id: str
    title: str
    current: int
    target: int
    completed: bool


class CollectionInsights(TypedDict):
    unique_characters: int
    total_copies: int
    duplicate_copies: int
    favorite_characters: int
    active_owned: int
    catalog_size: int
    completion_percent: float | None
    rarity_breakdown: list[RarityCount]
    goals: list[CollectionGoal]


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SAFE_INTEGER:
        raise InsightsDataError("Collection counts need operator review")
    return value


def summarize_collection(rows: list[dict[str, Any]], catalog_size: int) -> CollectionInsights:
    """Validate bounded aggregates and derive non-permanent, current-collection goals."""
    catalog_size = _count(catalog_size)
    by_rarity: dict[str, RarityCount] = {}
    unique = copies = favorites = active = 0
    for row in rows:
        rarity = row.get("_id")
        if not isinstance(rarity, str) or rarity not in (*RARITY_VALUES, UNKNOWN_RARITY):
            raise InsightsDataError("Collection rarity needs operator review")
        if rarity in by_rarity:
            raise InsightsDataError("Collection rarity is duplicated")
        count, amount = _count(row.get("unique")), _count(row.get("copies"))
        liked, available = _count(row.get("favorites")), _count(row.get("active_owned"))
        if amount < count or liked > count or available > count:
            raise InsightsDataError("Collection counts are inconsistent")
        unique, copies = _count(unique + count), _count(copies + amount)
        favorites, active = _count(favorites + liked), _count(active + available)
        by_rarity[rarity] = {"rarity": rarity, "unique": count, "copies": amount}
    if active > catalog_size:
        raise InsightsDataError("Active collection count exceeds the catalog")
    goals: list[CollectionGoal] = []
    for key, title, current, target in (
        ("collection-1", "First companion", unique, 1),
        ("collection-5", "A growing collection", unique, 5),
        ("collection-25", "Story keeper", unique, 25),
        ("collection-100", "A universe of stories", unique, 100),
        ("favorites-1", "A favorite discovery", favorites, 1),
        ("favorites-5", "A circle of favorites", favorites, 5),
    ):
        goals.append(
            {
                "id": key,
                "title": title,
                "current": min(current, target),
                "target": target,
                "completed": current >= target,
            }
        )
    return {
        "unique_characters": unique,
        "total_copies": copies,
        "duplicate_copies": copies - unique,
        "favorite_characters": favorites,
        "active_owned": active,
        "catalog_size": catalog_size,
        "completion_percent": round(active * 100 / catalog_size, 1) if catalog_size else None,
        "rarity_breakdown": [
            by_rarity[name] for name in (*RARITY_VALUES, UNKNOWN_RARITY) if name in by_rarity
        ],
        "goals": goals,
    }


class CollectionInsightsRepository:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def read(self, user_id: int) -> CollectionInsights:
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or not 0 < user_id <= MAX_SAFE_INTEGER
        ):
            raise ValueError("A valid Telegram user identity is required")
        rarity = {"$ifNull": ["$character.rarity", UNKNOWN_RARITY]}
        pipeline = [
            {"$match": {"user_id": user_id, "quantity": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "characters",
                    "localField": "character_id",
                    "foreignField": "_id",
                    "as": "character",
                }
            },
            {"$unwind": {"path": "$character", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": {
                        "$cond": [{"$in": [rarity, list(RARITY_VALUES)]}, rarity, UNKNOWN_RARITY]
                    },
                    "unique": {"$sum": 1},
                    "copies": {"$sum": "$quantity"},
                    "favorites": {"$sum": {"$cond": [{"$eq": ["$favorite", True]}, 1, 0]}},
                    "active_owned": {
                        "$sum": {"$cond": [{"$eq": ["$character.active", True]}, 1, 0]}
                    },
                }
            },
        ]
        # The inventory and catalog must describe the same point in time. The
        # pipeline is actor-scoped and bounded by time, fixed rarity buckets,
        # indexed IDs, and no disk spill; expensive collections fail closed.
        async with asyncio.timeout(10):
            async with self.client.start_session(
                snapshot=True, causal_consistency=False
            ) as session:
                cursor = await self.db.inventory.aggregate(
                    pipeline,
                    session=session,
                    maxTimeMS=4000,
                    allowDiskUse=False,
                    comment="summon-collection-insights",
                )
                rows = await cursor.to_list(length=len(RARITY_VALUES) + 2)
                catalog_size = await self.db.characters.count_documents(
                    {"active": True}, session=session, maxTimeMS=4000
                )
        return summarize_collection(rows, catalog_size)
