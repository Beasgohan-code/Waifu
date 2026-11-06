"""MongoDB-backed fixed-window limits shared by all API instances."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from pymongo import ReturnDocument

from summon.repositories.base import utcnow


class RateLimiter:
    def __init__(self, database: Any) -> None:
        self.db = database

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
