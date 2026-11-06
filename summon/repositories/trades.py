"""Trade proposals and transaction-backed two-sided exchanges."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from pymongo import DESCENDING, ReturnDocument

from summon.repositories.base import transaction, utcnow
from summon.services.trading import TradeConflict, TradeForbidden, TradeNotFound


def public_trade(doc: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    # Expiry is an effective read state; historical records are never TTL-deleted.
    current = now or utcnow()
    status = (
        "expired" if doc["status"] == "pending" and doc["expires_at"] <= current else doc["status"]
    )
    return {
        "id": doc["_id"],
        "sender_id": doc["sender_id"],
        "receiver_id": doc["receiver_id"],
        "offer_id": doc["offer_id"],
        "request_id": doc["request_id"],
        "status": status,
        "created_at": doc["created_at"],
        "expires_at": doc["expires_at"],
    }


class TradeRepository:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def create(
        self, sender: int, receiver: int, offer: str, request: str, now: datetime
    ) -> dict[str, Any]:
        owned = await self.db.inventory.find_one(
            {"user_id": sender, "character_id": offer, "quantity": {"$gt": 0}}
        )
        if not owned:
            raise TradeConflict("Your offered character is no longer in your collection")
        if not await self.db.characters.find_one({"_id": request, "active": True}, {"_id": 1}):
            raise TradeConflict("The requested character is not in the catalogue")
        # Do not probe the recipient's private inventory during proposal creation.
        # Their ownership is checked only when they voluntarily accept the offer.
        doc = {
            "_id": uuid4().hex,
            "sender_id": sender,
            "receiver_id": receiver,
            "offer_id": offer,
            "request_id": request,
            "status": "pending",
            "created_at": now,
            "expires_at": now + timedelta(minutes=15),
        }
        await self.db.trades.insert_one(doc)
        return public_trade(doc, now=now)

    async def confirm(self, trade_id: str, actor_id: int, now: datetime) -> dict[str, Any]:
        async def exchange(session: Any) -> dict[str, Any]:
            trade = await self.db.trades.find_one({"_id": trade_id}, session=session)
            if not trade:
                raise TradeNotFound("Trade not found")
            if actor_id != trade["receiver_id"]:
                raise TradeForbidden("Only the recipient can accept this trade")
            if trade["status"] != "pending" or trade["expires_at"] <= now:
                raise TradeConflict("This trade is no longer pending")
            sender, receiver = trade["sender_id"], trade["receiver_id"]
            offer, request = trade["offer_id"], trade["request_id"]
            for owner, char in ((sender, offer), (receiver, request)):
                remaining = await self.db.inventory.find_one_and_update(
                    {"user_id": owner, "character_id": char, "quantity": {"$gt": 0}},
                    {"$inc": {"quantity": -1}},
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if not remaining:
                    raise TradeConflict("One of the characters is no longer available for trade")
                if remaining["quantity"] == 0:
                    await self.db.inventory.update_one(
                        {"user_id": owner, "character_id": char},
                        {"$set": {"favorite": False}},
                        session=session,
                    )
            for owner, char in ((sender, request), (receiver, offer)):
                await self.db.inventory.update_one(
                    {"user_id": owner, "character_id": char},
                    {
                        "$inc": {"quantity": 1},
                        "$setOnInsert": {"favorite": False, "acquired_at": now},
                    },
                    upsert=True,
                    session=session,
                )
            updated = await self.db.trades.update_one(
                {"_id": trade_id, "status": "pending"},
                {"$set": {"status": "completed", "completed_at": now}},
                session=session,
            )
            if updated.modified_count != 1:
                raise TradeConflict("This trade is no longer pending")
            await self.db.activity.insert_one(
                {
                    "_id": f"trade:{trade_id}",
                    "user_id": sender,
                    "other_user_id": receiver,
                    "kind": "trade",
                    "created_at": now,
                },
                session=session,
            )
            trade["status"] = "completed"
            return public_trade(trade, now=now)

        return await transaction(self.client, exchange)

    async def cancel(self, trade_id: str, actor_id: int, now: datetime) -> dict[str, Any]:
        doc = await self.db.trades.find_one_and_update(
            {
                "_id": trade_id,
                "status": "pending",
                "expires_at": {"$gt": now},
                "$or": [{"sender_id": actor_id}, {"receiver_id": actor_id}],
            },
            {"$set": {"status": "cancelled", "cancelled_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            raise TradeNotFound("Pending trade not found")
        return public_trade(doc, now=now)

    async def list(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        cursor = (
            self.db.trades.find({"$or": [{"sender_id": user_id}, {"receiver_id": user_id}]})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )
        now = utcnow()
        return [public_trade(doc, now=now) async for doc in cursor]
