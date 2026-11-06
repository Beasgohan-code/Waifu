"""Indexed character catalog and inventory read models."""

from __future__ import annotations

import re
import secrets
from typing import Any, cast
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING

from summon.models import CharacterInput, normalize_name
from summon.repositories.base import transaction, utcnow

RARITY_WEIGHTS = {"common": 55, "rare": 25, "epic": 15, "legendary": 5}


def public_character(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "name": doc["name"],
        "category": doc["category"],
        "rarity": doc["rarity"],
        "image_url": doc.get("image_url", ""),
        "description": doc.get("description", ""),
        "active": doc.get("active", True),
    }


class CatalogRepository:
    def __init__(self, database: Any, *, client: Any | None = None) -> None:
        self.db = database
        self.client = client
        self.rng = secrets.SystemRandom()

    async def upsert_character(self, data: CharacterInput, *, actor_id: int) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("Catalog writes require a transaction client")
        values = data.model_dump(exclude={"id"})
        values["name_key"] = normalize_name(data.name)
        now, random_key, event_id = utcnow(), self.rng.random(), uuid4().hex

        async def save(session: Any) -> dict[str, Any]:
            await self.db.characters.update_one(
                {"_id": data.id},
                {"$set": values, "$setOnInsert": {"random_key": random_key, "created_at": now}},
                upsert=True,
                session=session,
            )
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "user_id": actor_id,
                    "kind": "character_saved",
                    "character_id": data.id,
                    "fields": sorted(values),
                    "created_at": now,
                },
                session=session,
            )
            return public_character({"_id": data.id, **values})

        return await transaction(self.client, save)

    async def choose(self) -> dict[str, Any] | None:
        """Weight nonempty rarities, then uniformly choose one stable-catalog offset.

        Unlike a fixed random-key pivot, each character in a rarity has the same
        probability. Counts and index skips cost O(bucket size); a concurrent catalog
        edit can invalidate the offset, in which case selection is retried, not biased
        toward an arbitrary fallback record.
        """
        for _ in range(3):
            remaining = dict(RARITY_WEIGHTS)
            while remaining:
                rarity = self.rng.choices(list(remaining), weights=list(remaining.values()), k=1)[0]
                query = {"active": True, "rarity": rarity}
                count = await self.db.characters.count_documents(query)
                if not count:
                    del remaining[rarity]
                    continue
                char = await self.db.characters.find_one(
                    query, sort=[("_id", ASCENDING)], skip=self.rng.randrange(count)
                )
                if char:
                    return cast(dict[str, Any], char)
                break  # A catalog edit may have invalidated the count; start over.
            else:
                return None
        return None

    async def get(self, character_id: str) -> dict[str, Any] | None:
        doc = await self.db.characters.find_one({"_id": character_id, "active": True})
        return public_character(doc) if doc else None

    async def details(self, user_id: int, character_id: str) -> dict[str, Any] | None:
        character = await self.get(character_id)
        if not character:
            return None
        owned = (
            await self.db.inventory.find_one({"user_id": user_id, "character_id": character_id})
            or {}
        )
        wished = await self.db.wishlists.find_one(
            {"user_id": user_id, "character_id": character_id}
        )
        character.update(
            quantity=owned.get("quantity", 0),
            favorite=owned.get("favorite", False),
            wishlisted=bool(wished),
        )
        return character

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
        query: dict[str, Any] = {"active": True}
        if search:
            query["name_key"] = {"$regex": "^" + re.escape(normalize_name(search))}
        if rarity:
            query["rarity"] = rarity
        if category:
            query["category"] = category
        cursor = self.db.characters.find(query).sort([("name_key", ASCENDING), ("_id", ASCENDING)])
        docs = await cursor.skip((page - 1) * limit).limit(limit).to_list(length=limit)
        ids = [doc["_id"] for doc in docs]
        owned = await self.db.inventory.find(
            {"user_id": user_id, "character_id": {"$in": ids}, "quantity": {"$gt": 0}}
        ).to_list(length=limit)
        wished = await self.db.wishlists.find(
            {"user_id": user_id, "character_id": {"$in": ids}}
        ).to_list(length=limit)
        ownership = {item["character_id"]: item for item in owned}
        wishlist = {item["character_id"] for item in wished}
        items = []
        for doc in docs:
            entry = public_character(doc)
            record = ownership.get(doc["_id"], {})
            entry.update(
                quantity=record.get("quantity", 0),
                favorite=record.get("favorite", False),
                wishlisted=doc["_id"] in wishlist,
            )
            items.append(entry)
        total = await self.db.characters.count_documents(query)
        return {"items": items, "page": page, "limit": limit, "total": total}

    async def inventory(
        self, user_id: int, *, page: int = 1, limit: int = 24, favorite: bool = False
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"user_id": user_id, "quantity": {"$gt": 0}}
        if favorite:
            query["favorite"] = True
        cursor = self.db.inventory.find(query).sort(
            [("acquired_at", DESCENDING), ("character_id", ASCENDING)]
        )
        rows = await cursor.skip((page - 1) * limit).limit(limit).to_list(length=limit)
        chars = await self.db.characters.find(
            {"_id": {"$in": [row["character_id"] for row in rows]}}
        ).to_list(length=limit)
        by_id = {doc["_id"]: doc for doc in chars}
        items = []
        for row in rows:
            char = by_id.get(row["character_id"])
            if char:
                entry = public_character(char)
                entry.update(quantity=row["quantity"], favorite=row.get("favorite", False))
                items.append(entry)
        return {
            "items": items,
            "page": page,
            "limit": limit,
            "total": await self.db.inventory.count_documents(query),
        }

    async def set_favorite(self, user_id: int, character_id: str, favorite: bool) -> bool:
        result = await self.db.inventory.update_one(
            {"user_id": user_id, "character_id": character_id, "quantity": {"$gt": 0}},
            {"$set": {"favorite": favorite}},
        )
        return bool(result.matched_count)

    async def set_wishlist(self, user_id: int, character_id: str, wished: bool) -> bool:
        if not await self.db.characters.find_one({"_id": character_id, "active": True}, {"_id": 1}):
            return False
        if wished:
            await self.db.wishlists.update_one(
                {"user_id": user_id, "character_id": character_id},
                {"$setOnInsert": {"created_at": utcnow()}},
                upsert=True,
            )
        else:
            await self.db.wishlists.delete_one({"user_id": user_id, "character_id": character_id})
        return True
