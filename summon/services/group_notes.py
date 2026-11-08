"""Fresh Telegram authorization for public group notes and admin changes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from summon.group_notes import (
    validate_actor_id,
    validate_group_id,
    validate_note_content,
    validate_note_name,
    validate_revision,
)
from summon.services.moderation import PermissionDenied, _status, require_group_admin


async def _require_member(bot: Any, group_id: int, actor_id: int) -> None:
    validate_group_id(group_id)
    validate_actor_id(actor_id)
    member = await bot.get_chat_member(group_id, actor_id)
    status = _status(member)
    if status in {"owner", "creator", "administrator", "member"}:
        return
    if status == "restricted" and getattr(member, "is_member", False) is True:
        return
    raise PermissionDenied("Current group membership is required")


class GroupNotesService:
    def __init__(self, bot: Any, notes: Any) -> None:
        self.bot = bot
        self.notes = notes

    async def _require_admin(self, group_id: int, actor_id: int) -> None:
        validate_group_id(group_id)
        validate_actor_id(actor_id)
        await require_group_admin(self.bot, group_id, actor_id, "can_change_info")

    async def list_for_member(self, group_id: int, actor_id: int) -> list[str]:
        await _require_member(self.bot, group_id, actor_id)
        notes = await self.notes.list(group_id)
        return [str(note["name"]) for note in notes]

    async def get_for_member(self, group_id: int, actor_id: int, name: str) -> dict[str, str]:
        validate_note_name(name)
        await _require_member(self.bot, group_id, actor_id)
        note = await self.notes.get(group_id, name)
        # Revisions and administrator metadata aren't needed for the public reply.
        return {"name": note["name"], "content": note["content"]}

    async def list_for_admin(self, group_id: int, actor_id: int) -> list[dict[str, Any]]:
        await self._require_admin(group_id, actor_id)
        return cast(list[dict[str, Any]], await self.notes.list(group_id))

    async def get_for_admin(self, group_id: int, actor_id: int, name: str) -> dict[str, Any]:
        validate_note_name(name)
        await self._require_admin(group_id, actor_id)
        return cast(dict[str, Any], await self.notes.get(group_id, name))

    async def save(
        self,
        group_id: int,
        actor_id: int,
        name: str,
        content: str,
        expected_revision: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        validate_note_name(name)
        validate_note_content(content)
        validate_revision(expected_revision, allow_none=True)
        await self._require_admin(group_id, actor_id)
        return cast(
            dict[str, Any],
            await self.notes.save(group_id, actor_id, name, content, expected_revision, now),
        )

    async def delete(
        self, group_id: int, actor_id: int, name: str, expected_revision: str, now: datetime
    ) -> dict[str, Any]:
        validate_note_name(name)
        validate_revision(expected_revision)
        await self._require_admin(group_id, actor_id)
        return cast(
            dict[str, Any],
            await self.notes.delete(group_id, actor_id, name, expected_revision, now),
        )
