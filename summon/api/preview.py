"""Explicit, read-only development preview. Never selected in production."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from summon.models import GroupSettings, InventorySort

_CHARACTERS: list[dict[str, Any]] = [
    {
        "id": "aiko",
        "name": "Aiko Tsukino",
        "category": "Moonlit Chronicles",
        "rarity": "legendary",
        "image_url": "/art/aiko.webp",
        "description": (
            "A quiet swordswoman who follows the falling leaves, "
            "protecting the stories hidden between worlds."
        ),
        "quantity": 1,
        "favorite": True,
        "wishlisted": False,
        "active": True,
    },
    {
        "id": "ren",
        "name": "Ren Aster",
        "category": "The Starlit Path",
        "rarity": "epic",
        "image_url": "/art/ren.webp",
        "description": (
            "A wandering sky mage, collecting lost constellations "
            "and the wishes that travel with them."
        ),
        "quantity": 2,
        "favorite": False,
        "wishlisted": False,
        "active": True,
    },
    {
        "id": "mira",
        "name": "Mira Evergreen",
        "category": "Whispers of the Grove",
        "rarity": "rare",
        "image_url": "/art/mira.webp",
        "description": (
            "An inventive alchemist with a soft spot for forgotten forests "
            "and impossible little miracles."
        ),
        "quantity": 0,
        "favorite": False,
        "wishlisted": True,
        "active": True,
    },
]


class PreviewCatalog:
    async def list(
        self,
        user_id: int,
        *,
        search: str = "",
        rarity: str | None = None,
        category: str | None = None,
        page: int = 1,
        limit: int = 24,
    ) -> dict[str, Any]:
        items = [
            item
            for item in _CHARACTERS
            if item["name"].casefold().startswith(search.casefold())
            and (not rarity or item["rarity"] == rarity)
            and (not category or item["category"] == category)
        ]
        return {
            "items": items[(page - 1) * limit : page * limit],
            "total": len(items),
            "page": page,
            "limit": limit,
        }

    async def inventory(
        self,
        user_id: int,
        *,
        page: int = 1,
        limit: int = 24,
        favorite: bool = False,
        duplicates: bool = False,
        sort: InventorySort = "recent",
    ) -> dict[str, Any]:
        if sort not in {"recent", "copies"}:
            raise ValueError("Unknown inventory ordering")
        items = [
            item
            for item in _CHARACTERS
            if item["quantity"] > (1 if duplicates else 0) and (not favorite or item["favorite"])
        ]
        if sort == "copies":
            items.sort(key=lambda item: (-item["quantity"], item["id"]))
        return {
            "items": items[(page - 1) * limit : page * limit],
            "total": len(items),
            "page": page,
            "limit": limit,
        }

    async def details(self, user_id: int, character_id: str) -> dict[str, Any] | None:
        return next((item for item in _CHARACTERS if item["id"] == character_id), None)


class PreviewUsers:
    async def profile(self, user_id: int) -> dict[str, Any]:
        return {
            "id": 1,
            "first_name": "Mika",
            "username": "collector",
            "points": 840,
            "xp": 425,
            "level": 3,
            "catch_count": 8,
            "unique_characters": 2,
            "catalog_size": 3,
            "streak": 3,
            "last_daily": None,
        }

    async def leaderboard(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {"name": "Akari", "catch_count": 32, "level": 5},
            {"name": "Mika", "catch_count": 8, "level": 3},
            {"name": "Haru", "catch_count": 6, "level": 2},
        ][:limit]

    async def progression(self, user_id: int, now: datetime) -> dict[str, Any]:
        return {
            "achievements": [
                {
                    "id": "first-summon",
                    "title": "First encounter",
                    "description": "Collect your first character",
                    "current": 1,
                    "target": 1,
                    "completed": True,
                },
                {
                    "id": "collector-five",
                    "title": "A growing universe",
                    "description": "Collect 5 unique characters",
                    "current": 2,
                    "target": 5,
                    "completed": False,
                },
                {
                    "id": "streak-three",
                    "title": "A little ritual",
                    "description": "Reach a 3-day check-in streak",
                    "current": 3,
                    "target": 3,
                    "completed": True,
                },
                {
                    "id": "level-five",
                    "title": "On your way",
                    "description": "Reach collector level 5",
                    "current": 3,
                    "target": 5,
                    "completed": False,
                },
            ],
            "daily": [
                {
                    "id": "check-in",
                    "title": "Daily check-in",
                    "description": "Claim your daily points",
                    "completed": False,
                    "reward": "100+ points",
                },
                {
                    "id": "summon-one",
                    "title": "Meet a companion",
                    "description": "Catch a character in a group",
                    "completed": True,
                    "reward": "+40 points on catch",
                },
            ],
        }


class PreviewTrades:
    async def list(self, user_id: int) -> list[dict[str, Any]]:
        return []


class PreviewGroups:
    @staticmethod
    def group() -> dict[str, Any]:
        settings = GroupSettings(
            spawn_enabled=True,
            flood_enabled=True,
            link_filter=True,
            welcome_enabled=True,
            welcome_text="Welcome to the club!",
            rules="Be kind, keep it fun, and look out for each other.",
        )
        return {
            "id": -100,
            "title": "The Collector's Club",
            "settings": settings.model_dump(),
            "total_messages": 1284,
            "permissions": {"can_change_info": True, "can_restrict_members": True},
            "recent_activity": [
                {
                    "kind": "moderation",
                    "action": "warn",
                    "reason": "Sample activity",
                    "created_at": datetime.now(UTC),
                },
                {
                    "kind": "settings_changed",
                    "action": None,
                    "reason": None,
                    "created_at": datetime.now(UTC),
                },
            ],
        }

    async def preview_list(self) -> list[dict[str, Any]]:
        return [self.group()]

    async def preview_get(self, group_id: int) -> dict[str, Any] | None:
        return self.group() if group_id == -100 else None

    async def preview_auto_replies(self, group_id: int) -> list[dict[str, Any]]:
        if group_id != -100:
            raise LookupError("Sample group not found")
        return [
            {
                "name": "greeting",
                "trigger": "hello everyone",
                "response": "Welcome to our little universe! Please be kind to each other.",
                "enabled": False,
                "revision": "c" * 32,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "created_by": 1,
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
                "updated_by": 1,
            }
        ]

    async def preview_notes(self, group_id: int) -> list[dict[str, Any]]:
        if group_id != -100:
            raise LookupError("Sample group not found")
        return [
            {
                "name": "getting-started",
                "content": "Say hello, introduce yourself, and share your favorite character!",
                "revision": "b" * 32,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "created_by": 1,
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
                "updated_by": 1,
            }
        ]

    async def preview_warnings(
        self, group_id: int, target_id: int, *, before: str | None, limit: int
    ) -> dict[str, Any]:
        if group_id != -100:
            raise LookupError("Sample group not found")
        sample_id = "a" * 32
        if before is not None and (target_id != 456 or before != sample_id):
            raise LookupError("Sample cursor not found")
        items = []
        if target_id == 456 and before is None:
            items = [
                {
                    "id": sample_id,
                    "actor_id": 1,
                    "reason": "Sample warning for review",
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ]
        return {"items": items[:limit], "active_count": int(target_id == 456), "next_cursor": None}


class PreviewStore:
    def __init__(self) -> None:
        self.catalog = PreviewCatalog()
        self.users = PreviewUsers()
        self.trades = PreviewTrades()
        self.gifts = PreviewTrades()
        self.groups = PreviewGroups()
