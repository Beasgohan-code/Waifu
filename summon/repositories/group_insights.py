"""Private group summon summaries derived from recorded reservations.

No message text, names of other players, rewards, or additional history are
stored. Counts are scoped to one group and the caller's own catches.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from summon.repositories.collection_insights import MAX_SAFE_INTEGER

WINDOW_DAYS = 30


class GroupInsightsDataError(RuntimeError):
    """Recorded summon states cannot be summarized safely."""


class GroupSummonInsights(TypedDict):
    window_days: int
    as_of: datetime
    attempts: int
    caught: int
    expired: int
    delivery_rejected: int
    awaiting_resolution: int
    my_catches: int


def summarize_group_summons(row: dict[str, Any] | None, now: datetime) -> GroupSummonInsights:
    keys = (
        "attempts",
        "caught",
        "expired",
        "delivery_rejected",
        "awaiting_resolution",
        "my_catches",
    )
    values: dict[str, int] = {}
    for key in keys:
        value = 0 if row is None else row.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_SAFE_INTEGER
        ):
            raise GroupInsightsDataError("Summon counts need operator review")
        values[key] = value
    if (
        values["attempts"]
        != values["caught"]
        + values["expired"]
        + values["delivery_rejected"]
        + values["awaiting_resolution"]
        or values["my_catches"] > values["caught"]
    ):
        raise GroupInsightsDataError("Summon counts are inconsistent")
    return {
        "window_days": WINDOW_DAYS,
        "as_of": now,
        "attempts": values["attempts"],
        "caught": values["caught"],
        "expired": values["expired"],
        "delivery_rejected": values["delivery_rejected"],
        "awaiting_resolution": values["awaiting_resolution"],
        "my_catches": values["my_catches"],
    }


def _sum_if(condition: dict[str, Any]) -> dict[str, Any]:
    return {"$sum": {"$cond": [condition, 1, 0]}}


class GroupInsightsRepository:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def read(self, group_id: int, user_id: int, now: datetime) -> GroupSummonInsights:
        if (
            isinstance(group_id, bool)
            or not isinstance(group_id, int)
            or not -MAX_SAFE_INTEGER <= group_id < 0
        ):
            raise ValueError("A valid group identity is required")
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or not 0 < user_id <= MAX_SAFE_INTEGER
        ):
            raise ValueError("A valid Telegram user identity is required")
        if now.utcoffset() is None:
            raise ValueError("A timezone-aware server timestamp is required")
        now = now.astimezone(UTC)
        active = {"$eq": ["$status", "active"]}
        valid_expiry = {"$eq": [{"$type": "$expires_at"}, "date"]}
        claimed = {
            "$and": [
                {"$eq": ["$status", "claimed"]},
                {"$in": [{"$type": "$claimed_by"}, ["int", "long"]]},
                {"$gt": ["$claimed_by", 0]},
                {"$lte": ["$claimed_by", MAX_SAFE_INTEGER]},
            ]
        }
        expired = {
            "$or": [
                {"$eq": ["$status", "expired"]},
                {"$and": [active, valid_expiry, {"$lte": ["$expires_at", now]}]},
            ]
        }
        awaiting = {"$and": [active, valid_expiry, {"$gt": ["$expires_at", now]}]}
        pipeline = [
            {
                "$match": {
                    "group_id": group_id,
                    "created_at": {"$gte": now - timedelta(days=WINDOW_DAYS), "$lte": now},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "attempts": {"$sum": 1},
                    "caught": _sum_if(claimed),
                    "expired": _sum_if(expired),
                    "delivery_rejected": _sum_if({"$eq": ["$status", "delivery_failed"]}),
                    "awaiting_resolution": _sum_if(awaiting),
                    "my_catches": _sum_if({"$and": [claimed, {"$eq": ["$claimed_by", user_id]}]}),
                }
            },
        ]
        # Group/time index, fixed-size aggregate and no disk spill. A snapshot
        # prevents mixed counts if a catch commits while this report is read.
        async with asyncio.timeout(8):
            async with self.client.start_session(
                snapshot=True, causal_consistency=False
            ) as session:
                cursor = await self.db.spawns.aggregate(
                    pipeline,
                    session=session,
                    maxTimeMS=4000,
                    allowDiskUse=False,
                    comment="summon-group-insights",
                )
                rows = await cursor.to_list(length=2)
        if len(rows) > 1:
            raise GroupInsightsDataError("Summon counts are inconsistent")
        return summarize_group_summons(rows[0] if rows else None, now)
