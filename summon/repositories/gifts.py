"""Recipient-confirmed gifts with a transactional ownership transfer and history."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from pymongo import DESCENDING, ReturnDocument

from summon.repositories.base import transaction, utcnow
from summon.services.gifting import GiftConflict, GiftForbidden, GiftNotFound


def public_gift(doc: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    # Expiry is an effective read state; historical records are never TTL-deleted.
    current = now or utcnow()
    status = (
        "expired" if doc["status"] == "pending" and doc["expires_at"] <= current else doc["status"]
    )
    return {
        "id": doc["_id"],
        "sender_id": doc["sender_id"],
        "receiver_id": doc["receiver_id"],
        "character_id": doc["character_id"],
        "status": status,
        "created_at": doc["created_at"],
        "expires_at": doc["expires_at"],
    }


class GiftRepository:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def create(
        self, sender: int, receiver: int, character: str, now: datetime
    ) -> dict[str, Any]:
        owned = await self.db.inventory.find_one(
            {"user_id": sender, "character_id": character, "quantity": {"$gt": 0}}
        )
        if not owned:
            raise GiftConflict("This character is no longer in your collection")
        doc = {
            "_id": uuid4().hex,
            "sender_id": sender,
            "receiver_id": receiver,
            "character_id": character,
            "status": "pending",
            "created_at": now,
            "expires_at": now + timedelta(minutes=15),
        }
        await self.db.gifts.insert_one(doc)
        return public_gift(doc, now=now)

    async def confirm(self, gift_id: str, actor_id: int, now: datetime) -> dict[str, Any]:
        async def transfer(session: Any) -> dict[str, Any]:
            gift = await self.db.gifts.find_one({"_id": gift_id}, session=session)
            if not gift:
                raise GiftNotFound("Gift not found")
            if actor_id != gift["receiver_id"]:
                raise GiftForbidden("Only the recipient can accept this gift")
            if gift["status"] != "pending" or gift["expires_at"] <= now:
                raise GiftConflict("This gift is no longer pending")
            sender, receiver, character = (
                gift["sender_id"],
                gift["receiver_id"],
                gift["character_id"],
            )
            remaining = await self.db.inventory.find_one_and_update(
                {"user_id": sender, "character_id": character, "quantity": {"$gt": 0}},
                {"$inc": {"quantity": -1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if not remaining:
                raise GiftConflict("This character is no longer in the sender's collection")
            if remaining["quantity"] == 0:
                await self.db.inventory.update_one(
                    {"user_id": sender, "character_id": character},
                    {"$set": {"favorite": False}},
                    session=session,
                )
            await self.db.inventory.update_one(
                {"user_id": receiver, "character_id": character},
                {"$inc": {"quantity": 1}, "$setOnInsert": {"favorite": False, "acquired_at": now}},
                upsert=True,
                session=session,
            )
            updated = await self.db.gifts.update_one(
                {"_id": gift_id, "status": "pending"},
                {"$set": {"status": "completed", "completed_at": now}},
                session=session,
            )
            if updated.modified_count != 1:
                raise GiftConflict("This gift is no longer pending")
            await self.db.activity.insert_one(
                {
                    "_id": f"gift:{gift_id}",
                    "user_id": sender,
                    "other_user_id": receiver,
                    "character_id": character,
                    "kind": "gift",
                    "created_at": now,
                },
                session=session,
            )
            gift["status"] = "completed"
            return public_gift(gift, now=now)

        return await transaction(self.client, transfer)

    async def cancel(self, gift_id: str, actor_id: int, now: datetime) -> dict[str, Any]:
        doc = await self.db.gifts.find_one_and_update(
            {
                "_id": gift_id,
                "status": "pending",
                "expires_at": {"$gt": now},
                "$or": [{"sender_id": actor_id}, {"receiver_id": actor_id}],
            },
            {"$set": {"status": "cancelled", "cancelled_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            raise GiftNotFound("Pending gift not found")
        return public_gift(doc, now=now)

    async def list(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        cursor = self.db.gifts.find({"$or": [{"sender_id": user_id}, {"receiver_id": user_id}]})
        cursor = cursor.sort("created_at", DESCENDING).limit(limit)
        now = utcnow()
        return [public_gift(doc, now=now) async for doc in cursor]
