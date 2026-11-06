"""MongoDB-backed fixed-window limits shared by all API instances."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from summon.repositories.base import utcnow


class RateLimiter:
    def __init__(self, database: Any) -> None:
        self.db = database

    async def claim_once(self, key: str, *, seconds: int) -> bool:
        """Claim an opaque event once until TTL cleanup, before external delivery.

        An uncertain database write must propagate, so the caller fails closed.
        Majority acknowledgement makes confirmed claims durable across normal
        elections, but cannot guarantee delivery or eliminate all failover risk.
        """
        if not 1 <= seconds <= 7 * 86400:
            raise ValueError("Invalid claim lifetime")
        digest = hashlib.sha256(key.encode()).hexdigest()
        collection = self.db.rate_limits.with_options(
            write_concern=WriteConcern(w="majority", wtimeout=5000)
        )
        try:
            await collection.insert_one(
                {"_id": f"once:{digest}", "expires_at": utcnow() + timedelta(seconds=seconds)}
            )
        except DuplicateKeyError:
            return False
        return True

    async def allow(self, key: str, *, limit: int, seconds: int) -> bool:
        now = utcnow()
        bucket = int(now.timestamp()) // seconds
        # Hash the principal rather than persist raw IP addresses.
        principal = hashlib.sha256(key.encode()).hexdigest()[:24]
        row = await self.db.rate_limits.find_one_and_update(
            {"_id": f"api:{principal}:{seconds}:{bucket}"},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {"expires_at": now + timedelta(seconds=seconds * 2)},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return bool(row and row["count"] <= limit)
