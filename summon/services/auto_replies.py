"""Fresh Telegram authorization for all auto-reply management operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from summon.auto_replies import normalize_trigger, validate_enabled, validate_reply_text
from summon.group_notes import (
    validate_actor_id,
    validate_group_id,
    validate_note_name,
    validate_revision,
)
from summon.services.moderation import require_group_admin


class AutoReplyService:
    def __init__(self, bot: Any, replies: Any) -> None:
        self.bot = bot
        self.replies = replies

    async def _require_admin(self, group_id: int, actor_id: int) -> None:
        validate_group_id(group_id)
        validate_actor_id(actor_id)
        await require_group_admin(self.bot, group_id, actor_id, "can_change_info")

    async def list_for_admin(self, group_id: int, actor_id: int) -> list[dict[str, Any]]:
        await self._require_admin(group_id, actor_id)
        return cast(list[dict[str, Any]], await self.replies.list(group_id))

    async def save(
        self,
        group_id: int,
        actor_id: int,
        name: str,
        trigger: str,
        response: str,
        enabled: bool,
        expected_revision: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        validate_note_name(name)
        trigger = normalize_trigger(trigger)
        validate_reply_text(response)
        validate_enabled(enabled)
        validate_revision(expected_revision, allow_none=True)
        await self._require_admin(group_id, actor_id)
        return cast(
            dict[str, Any],
            await self.replies.save(
                group_id, actor_id, name, trigger, response, enabled, expected_revision, now
            ),
        )

    async def delete(
        self, group_id: int, actor_id: int, name: str, expected_revision: str, now: datetime
    ) -> dict[str, Any]:
        validate_note_name(name)
        validate_revision(expected_revision)
        await self._require_admin(group_id, actor_id)
        return cast(
            dict[str, Any],
            await self.replies.delete(group_id, actor_id, name, expected_revision, now),
        )
