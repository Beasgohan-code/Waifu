"""Last successful polling observations; not a lease or proof of delivery."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReadPreference
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

_RETENTION = timedelta(hours=1)
_MAX_FUTURE_SKEW_MS = 5_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def poller_health_key(bot_token: str, session_secret: str) -> str:
    if not bot_token or len(session_secret) < 32:
        raise ValueError("Polling health requires a bot token and a 32-character session secret")
    digest = hmac.new(
        session_secret.encode("utf-8"),
        b"summon:poller-health:v1\0" + bot_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"poller:{digest}"


class PollerHealthRepository:
    def __init__(self, database: Any, bot_token: str, session_secret: str) -> None:
        self._key = poller_health_key(bot_token, session_secret)
        self._collection = database["summon_poller_heartbeats_v1"].with_options(
            read_preference=ReadPreference.PRIMARY,
            read_concern=ReadConcern("majority"),
            write_concern=WriteConcern(w=1, wtimeout=3000),
        )

    async def record(self, observed_at: datetime) -> None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Polling observation must use a timezone-aware timestamp")
        observed_at = observed_at.astimezone(UTC)
        # Keep the original observation, never the write-completion time. Use
        # the server clock to discard a far-future event before it can poison
        # the monotonic maximum and later appear fresh when time catches up.
        event = {"$literal": observed_at}
        expires = {"$literal": observed_at + _RETENTION}
        acceptable = {"$lte": [event, {"$add": ["$$NOW", _MAX_FUTURE_SKEW_MS]}]}
        old_event = {"$ifNull": ["$observed_at", _EPOCH]}
        old_expiry = {"$ifNull": ["$expires_at", _EPOCH]}
        await self._collection.update_one(
            {"_id": self._key},
            [
                {
                    "$set": {
                        "observed_at": {
                            "$cond": [acceptable, {"$max": [old_event, event]}, old_event]
                        },
                        "expires_at": {
                            "$cond": [acceptable, {"$max": [old_expiry, expires]}, old_expiry]
                        },
                    }
                }
            ],
            upsert=True,
        )

    async def is_recent(self, max_age_seconds: int) -> bool:
        if not 1 <= max_age_seconds <= 900:
            raise ValueError("Invalid polling health window")
        # Compare to MongoDB's clock, not the API worker clock. Far-future
        # worker timestamps fail closed instead of extending apparent health.
        result = await self._collection.find_one(
            {
                "_id": self._key,
                "$expr": {
                    "$and": [
                        {
                            "$gte": [
                                "$observed_at",
                                {"$subtract": ["$$NOW", max_age_seconds * 1000]},
                            ]
                        },
                        {"$lte": ["$observed_at", {"$add": ["$$NOW", _MAX_FUTURE_SKEW_MS]}]},
                    ]
                },
            },
            {"_id": 1},
            max_time_ms=2000,
        )
        return result is not None
