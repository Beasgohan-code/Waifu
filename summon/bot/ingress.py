"""Coordinate one Wzgram update consumer with a short MongoDB lease.

This reduces overlapping workers; it is not an exactly-once processing or
split-brain fencing guarantee. Downstream ownership changes remain transactional.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Any

from pymongo import ReadPreference
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from wzgram import Client, StopPropagation, filters
from wzgram.types import CallbackQuery, Message


class IngressLeaseLost(RuntimeError):
    """The worker could not keep exclusive update-consumer coordination."""


class IngressLease:
    duration_seconds = 60
    renew_seconds = 15
    # The local deadline expires well before MongoDB's server-clock deadline.
    safety_seconds = 15

    def __init__(self, database: Any, bot_id: int) -> None:
        if bot_id <= 0:
            raise ValueError("A valid Telegram bot identity is required")
        self._key = f"bot:{bot_id}"
        self._owner = secrets.token_hex(32)
        self._collection = database["summon_ingress_lease_v1"].with_options(
            read_preference=ReadPreference.PRIMARY,
            read_concern=ReadConcern("majority"),
            write_concern=WriteConcern(w="majority", wtimeout=5000),
        )
        self._deadline = 0.0

    def valid(self) -> bool:
        return asyncio.get_running_loop().time() < self._deadline

    def invalidate(self) -> None:
        self._deadline = 0

    async def acquire(self) -> None:
        started = asyncio.get_running_loop().time()
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        # MongoDB does not permit $expr in an upsert predicate. Create an
        # expired slot first, then compete for it with a conditional atomic
        # update against MongoDB's own clock. Concurrent creators share _id.
        try:
            await self._collection.update_one(
                {"_id": self._key}, {"$setOnInsert": {"expires_at": epoch}}, upsert=True
            )
        except DuplicateKeyError:
            pass  # Another worker created the same slot first.
        result = await self._collection.update_one(
            {
                "_id": self._key,
                "$expr": {"$lte": [{"$ifNull": ["$expires_at", epoch]}, "$$NOW"]},
            },
            [
                {
                    "$set": {
                        "owner": {"$literal": self._owner},
                        "expires_at": {"$add": ["$$NOW", self.duration_seconds * 1000]},
                    }
                }
            ],
        )
        if not result.matched_count:
            raise IngressLeaseLost("Another SUMMON update worker holds the lease")
        self._deadline = started + self.duration_seconds - self.safety_seconds
        if not self.valid():
            raise IngressLeaseLost("The update-worker lease expired during acquisition")

    async def renew(self) -> None:
        if not self.valid():
            raise IngressLeaseLost("The update-worker lease is no longer valid")
        started = asyncio.get_running_loop().time()
        result = await self._collection.update_one(
            {
                "_id": self._key,
                "owner": self._owner,
                "$expr": {"$gt": ["$expires_at", "$$NOW"]},
            },
            [{"$set": {"expires_at": {"$add": ["$$NOW", self.duration_seconds * 1000]}}}],
        )
        if not result.matched_count:
            self._deadline = 0
            raise IngressLeaseLost("The update-worker lease was lost")
        self._deadline = started + self.duration_seconds - self.safety_seconds
        if not self.valid():
            raise IngressLeaseLost("The update-worker lease renewal took too long")

    async def maintain(self) -> None:
        while True:
            await asyncio.sleep(self.renew_seconds)
            try:
                async with asyncio.timeout(8):
                    await self.renew()
            except Exception as exc:
                self._deadline = 0
                raise IngressLeaseLost("Could not renew the update-worker lease") from exc

    async def release(self) -> None:
        self._deadline = 0
        await self._collection.delete_one({"_id": self._key, "owner": self._owner})

    def register(self, app: Client) -> None:
        @app.on_message(filters.incoming, group=-1000)
        async def guard_message(_client: Client, _message: Message) -> None:
            if not self.valid():
                raise StopPropagation

        @app.on_edited_message(filters.incoming, group=-1000)
        async def guard_edit(_client: Client, _message: Message) -> None:
            if not self.valid():
                raise StopPropagation

        @app.on_callback_query(group=-1000)
        async def guard_callback(_client: Client, _query: CallbackQuery) -> None:
            if not self.valid():
                raise StopPropagation
