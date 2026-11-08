"""Collection orchestration independent of Telegram and MongoDB transports."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from summon.models import normalize_name


class GuessRateLimited(Exception):
    """A player is guessing too rapidly."""


class CollectionService:
    def __init__(self, collection: Any, catalog: Any) -> None:
        self.collection = collection
        self.catalog = catalog

    async def on_message(
        self, group_id: int, title: str, user_id: int, now: datetime
    ) -> dict[str, Any] | None:
        group = await self.collection.record_message(group_id, title, user_id, now)
        if not group or not group["settings"].get("spawn_enabled"):
            return None
        if group["message_count"] < group["settings"]["spawn_every"]:
            return None
        return await self.try_spawn(group_id, now)

    async def try_spawn(self, group_id: int, now: datetime) -> dict[str, Any] | None:
        """Reserve only if MongoDB's atomic cadence, cooldown and active-spawn gates pass."""
        character = await self.catalog.choose()
        if not character:
            return None
        spawn = await self.collection.reserve_spawn(group_id, character, now)
        if not spawn:
            return None
        return {"spawn": spawn, "character": character}

    async def guess(
        self, group_id: int, user: dict[str, Any], text: str, now: datetime
    ) -> dict[str, Any] | None:
        if not await self.collection.allow_guess(group_id, user["id"], now):
            raise GuessRateLimited
        answer = normalize_name(text)
        if not answer or len(answer) > 120:
            return None
        return cast(
            dict[str, Any] | None, await self.collection.claim_spawn(group_id, user, answer, now)
        )
