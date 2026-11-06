"""Revocable Mini App sessions, with no bearer session IDs stored in MongoDB."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReadPreference
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class SessionRepository:
    def __init__(self, database: Any) -> None:
        # A successful logout must be durable and visible to later checks on
        # other workers, even after a primary change. These reads can be slower
        # and intentionally fail closed if a primary/majority is unavailable.
        self.sessions = database.web_sessions.with_options(
            read_preference=ReadPreference.PRIMARY,
            read_concern=ReadConcern("linearizable"),
            write_concern=WriteConcern(w="majority", wtimeout=5000),
        )

    async def create(self, session_id: str, user_id: int, now: datetime, max_age: int) -> None:
        await self.sessions.insert_one(
            {
                "_id": _key(session_id),
                "user_id": user_id,
                "expires_at": now + timedelta(seconds=max_age),
            }
        )

    async def is_active(self, session_id: str, user_id: int, now: datetime) -> bool:
        # The immutable unique _id selects exactly one document, as required
        # for linearizable reads. TTL cleanup is not relied on for expiry.
        return (
            await self.sessions.find_one(
                {"_id": _key(session_id), "user_id": user_id, "expires_at": {"$gt": now}},
                {"_id": 1},
                max_time_ms=5000,
            )
            is not None
        )

    async def revoke(self, session_id: str, user_id: int) -> None:
        await self.sessions.delete_one({"_id": _key(session_id), "user_id": user_id})
