"""Persisted group spawns and transaction-safe collection claims."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from summon.auth import TelegramUser
from summon.models import GroupSettings, normalize_name
from summon.repositories.base import allow_after, transaction
from summon.repositories.group_insights import GroupInsightsRepository, GroupSummonInsights


class DailyCharacterAlreadyClaimed(Exception):
    """A character has already been claimed for this actor's UTC day."""


class DailyCharacterUnavailable(Exception):
    """No active character can be awarded without changing ownership."""


class CollectionRepository:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def group_summon_insights(
        self, group_id: int, user_id: int, now: datetime
    ) -> GroupSummonInsights:
        return await GroupInsightsRepository(self.client, self.db).read(group_id, user_id, now)

    async def record_message(
        self, group_id: int, title: str, user_id: int, now: datetime
    ) -> dict[str, Any] | None:
        if not await allow_after(self.db, f"count:{group_id}:{user_id}", now, 5):
            return None
        result = await self.db.groups.find_one_and_update(
            {"_id": group_id},
            {
                "$set": {"title": title[:255]},
                "$setOnInsert": {
                    "settings": GroupSettings().model_dump(),
                    "next_spawn_at": now,
                    "active_spawn": None,
                    "created_at": now,
                },
                "$inc": {"message_count": 1, "total_messages": 1},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return cast(dict[str, Any] | None, result)

    async def allow_guess(self, group_id: int, user_id: int, now: datetime) -> bool:
        return await allow_after(self.db, f"guess:{group_id}:{user_id}", now, 3)

    async def reserve_spawn(
        self, group_id: int, character: dict[str, Any], now: datetime
    ) -> dict[str, Any] | None:
        spawn_id = uuid4().hex

        async def reserve(session: Any) -> dict[str, Any] | None:
            current = await self.db.groups.find_one({"_id": group_id}, session=session)
            if not current:
                return None
            settings = GroupSettings.model_validate(current["settings"])
            spawn = {
                "id": spawn_id,
                "character_id": character["_id"],
                "answer": normalize_name(character["name"]),
                "created_at": now,
                "expires_at": now + timedelta(seconds=settings.spawn_lifetime_seconds),
            }
            old = await self.db.groups.find_one_and_update(
                {
                    "_id": group_id,
                    "settings.spawn_enabled": True,
                    "next_spawn_at": {"$lte": now},
                    "$expr": {"$gte": ["$message_count", "$settings.spawn_every"]},
                    "$or": [{"active_spawn": None}, {"active_spawn.expires_at": {"$lte": now}}],
                },
                {
                    "$set": {
                        "active_spawn": spawn,
                        "message_count": 0,
                        "next_spawn_at": now + timedelta(seconds=settings.spawn_cooldown_seconds),
                    }
                },
                return_document=ReturnDocument.BEFORE,
                session=session,
            )
            if not old:
                return None
            if old.get("active_spawn"):
                await self.db.spawns.update_one(
                    {"_id": old["active_spawn"]["id"], "status": "active"},
                    {"$set": {"status": "expired"}},
                    session=session,
                )
            await self.db.spawns.insert_one(
                {
                    "_id": spawn_id,
                    "group_id": group_id,
                    "character_id": character["_id"],
                    "status": "active",
                    "created_at": now,
                    "expires_at": spawn["expires_at"],
                },
                session=session,
            )
            return spawn

        return await transaction(self.client, reserve)

    async def mark_delivery_failed(self, group_id: int, spawn_id: str) -> None:
        async def mark(session: Any) -> None:
            updated = await self.db.groups.update_one(
                {"_id": group_id, "active_spawn.id": spawn_id},
                {"$set": {"active_spawn": None}},
                session=session,
            )
            if updated.modified_count:
                history = await self.db.spawns.update_one(
                    {"_id": spawn_id, "status": "active"},
                    {"$set": {"status": "delivery_failed"}},
                    session=session,
                )
                if history.modified_count != 1:
                    raise RuntimeError(
                        "Spawn history was inconsistent; cancellation was rolled back"
                    )

        await transaction(self.client, mark)

    async def claim_daily_character(
        self, user: TelegramUser, character_id: str, now: datetime, *, message_id: str
    ) -> dict[str, Any]:
        """Award once per UTC day and once per Telegram message, atomically."""
        if now.utcoffset() is None:
            raise ValueError("A timezone-aware server timestamp is required")
        now = now.astimezone(UTC)
        if not isinstance(character_id, str) or not 1 <= len(character_id) <= 64:
            raise ValueError("Invalid character ID")
        if not re.fullmatch(r"daily-character-message:[0-9a-f]{64}", message_id):
            raise ValueError("A stable message identity is required")
        event_id = f"daily-character:{user.id}:{now.date().isoformat()}"

        async def claim(session: Any) -> dict[str, Any]:
            character = await self.db.characters.find_one(
                {"_id": character_id, "active": True}, session=session
            )
            if not character:
                raise DailyCharacterUnavailable
            try:
                # The daily ledger alone is insufficient: an old Telegram
                # update could otherwise be replayed on a different UTC day.
                await self.db.activity.insert_one(
                    {
                        "_id": message_id,
                        "user_id": user.id,
                        "kind": "daily_character_message",
                        "created_at": now,
                    },
                    session=session,
                )
                await self.db.reward_history.insert_one(
                    {
                        "_id": event_id,
                        "user_id": user.id,
                        "kind": "daily_character",
                        "character_id": character_id,
                        "created_at": now,
                    },
                    session=session,
                )
            except DuplicateKeyError:
                # Either the message or UTC-day guard already exists. Other
                # transaction conflicts are not misreported as daily claims.
                raise DailyCharacterAlreadyClaimed from None
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
                session=session,
            )
            await self.db.inventory.update_one(
                {"user_id": user.id, "character_id": character_id},
                {"$inc": {"quantity": 1}, "$setOnInsert": {"favorite": False, "acquired_at": now}},
                upsert=True,
                session=session,
            )
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "user_id": user.id,
                    "kind": "daily_character",
                    "character_id": character_id,
                    "created_at": now,
                },
                session=session,
            )
            return {
                "id": character_id,
                "name": character["name"],
                "category": character["category"],
                "rarity": character["rarity"],
            }

        return await transaction(self.client, claim)

    async def claim_spawn(
        self, group_id: int, user: dict[str, Any], answer: str, now: datetime
    ) -> dict[str, Any] | None:
        async def claim(session: Any) -> dict[str, Any] | None:
            group = await self.db.groups.find_one_and_update(
                {
                    "_id": group_id,
                    "active_spawn.answer": answer,
                    "active_spawn.expires_at": {"$gt": now},
                },
                {"$set": {"active_spawn": None}},
                return_document=ReturnDocument.BEFORE,
                session=session,
            )
            if not group:
                return None
            spawn = group["active_spawn"]
            character = await self.db.characters.find_one(
                {"_id": spawn["character_id"]}, session=session
            )
            if not character:
                raise RuntimeError("Spawn references a missing character; claim was rolled back")
            await self.db.inventory.update_one(
                {"user_id": user["id"], "character_id": spawn["character_id"]},
                {"$inc": {"quantity": 1}, "$setOnInsert": {"favorite": False, "acquired_at": now}},
                upsert=True,
                session=session,
            )
            await self.db.users.update_one(
                {"_id": user["id"]},
                {
                    "$set": {
                        "first_name": user["first_name"],
                        "username": user.get("username"),
                        "last_seen": now,
                    },
                    "$setOnInsert": {"created_at": now},
                    "$inc": {"points": 40, "xp": 25, "catch_count": 1},
                },
                upsert=True,
                session=session,
            )
            result = await self.db.spawns.update_one(
                {"_id": spawn["id"], "status": "active"},
                {"$set": {"status": "claimed", "claimed_by": user["id"], "claimed_at": now}},
                session=session,
            )
            if result.modified_count != 1:
                raise RuntimeError("Spawn history was inconsistent; claim was rolled back")
            await self.db.reward_history.insert_one(
                {
                    "_id": f"catch:{spawn['id']}",
                    "user_id": user["id"],
                    "kind": "catch",
                    "points": 40,
                    "xp": 25,
                    "created_at": now,
                },
                session=session,
            )
            await self.db.activity.insert_one(
                {
                    "_id": f"catch:{spawn['id']}",
                    "group_id": group_id,
                    "user_id": user["id"],
                    "kind": "catch",
                    "character_id": spawn["character_id"],
                    "created_at": now,
                },
                session=session,
            )
            return cast(dict[str, Any], character)

        return await transaction(self.client, claim)
