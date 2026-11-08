"""Shared MongoDB helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(UTC)


async def transaction(client: Any, callback: Callable[[Any], Awaitable[T]]) -> T:
    # PyMongo retries transient transaction failures. The callback may run more
    # than once; generate IDs outside it and never send Telegram messages here.
    async with client.start_session() as session:
        result = await session.with_transaction(
            callback,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
            max_commit_time_ms=5000,
        )
        return cast(T, result)


async def allow_after(database: Any, key: str, now: datetime, seconds: int) -> bool:
    """Atomic per-key cooldown across workers. TTL removes dormant keys."""
    try:
        doc = await database.rate_limits.find_one_and_update(
            {
                "_id": key,
                "$or": [{"not_before": {"$lte": now}}, {"not_before": {"$exists": False}}],
            },
            {
                "$set": {
                    "not_before": now + timedelta(seconds=seconds),
                    "expires_at": now + timedelta(hours=1),
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return doc is not None
    except DuplicateKeyError:
        # Existing key did not satisfy the cooldown filter; this is not an outage.
        return False
