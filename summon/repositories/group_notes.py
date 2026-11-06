"""Small group-local note map with optimistic, transactionally audited changes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from summon.group_notes import (
    MAX_NOTES,
    NoteConflict,
    NoteLimitError,
    NoteStorageError,
    validate_actor_id,
    validate_group_id,
    validate_note_content,
    validate_note_name,
    validate_revision,
)
from summon.repositories.base import transaction

_FIELD = "saved_notes_v1"  # Separate from legacy/other group fields; never migrate in place.
_READ_MAX_MS = 5000


def _notes(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = document.get(_FIELD, {})
    if not isinstance(value, dict) or len(value) > MAX_NOTES:
        raise NoteStorageError("Stored notes need operator review")
    try:
        for name, note in value.items():
            validate_note_name(name)
            if not isinstance(note, dict):
                raise ValueError("Invalid note")
            validate_note_content(note.get("content"))
            validate_revision(note.get("revision"))
            validate_actor_id(note.get("created_by"))
            validate_actor_id(note.get("updated_by"))
            if not isinstance(note.get("created_at"), datetime) or not isinstance(
                note.get("updated_at"), datetime
            ):
                raise ValueError("Invalid note timestamps")
    except (ValueError, TypeError) as exc:
        # Do not include note contents or keys from an untrusted, malformed document.
        raise NoteStorageError("Stored notes need operator review") from exc
    return value


def _public(name: str, note: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "content": note["content"],
        "revision": note["revision"],
        "created_at": note["created_at"],
        "created_by": note["created_by"],
        "updated_at": note["updated_at"],
        "updated_by": note["updated_by"],
    }


class GroupNoteRepository:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def _read(self, group_id: int, *, session: Any = None) -> dict[str, dict[str, Any]]:
        validate_group_id(group_id)
        kwargs: dict[str, Any] = {"max_time_ms": _READ_MAX_MS}
        if session is not None:
            kwargs["session"] = session
        document = await self.db.groups.find_one({"_id": group_id}, {_FIELD: 1}, **kwargs)
        if document is None:
            raise LookupError("Group not found")
        return _notes(document)

    async def list(self, group_id: int) -> list[dict[str, Any]]:
        notes = await self._read(group_id)
        return [_public(name, notes[name]) for name in sorted(notes)]

    async def get(self, group_id: int, name: str) -> dict[str, Any]:
        validate_note_name(name)
        notes = await self._read(group_id)
        if name not in notes:
            raise LookupError("Note not found")
        return _public(name, notes[name])

    async def save(
        self,
        group_id: int,
        actor_id: int,
        name: str,
        content: str,
        expected_revision: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        validate_group_id(group_id)
        validate_actor_id(actor_id)
        validate_note_name(name)
        validate_note_content(content)
        validate_revision(expected_revision, allow_none=True)
        # Stable across driver transaction retries. No messages are sent in callbacks.
        revision, event_id = uuid4().hex, uuid4().hex

        async def apply(session: Any) -> dict[str, Any]:
            notes = await self._read(group_id, session=session)
            old = notes.get(name)
            if (old is None and expected_revision is not None) or (
                old is not None and old["revision"] != expected_revision
            ):
                raise NoteConflict(
                    "This note changed or already exists; reload notes before retrying"
                )
            if old is None and len(notes) >= MAX_NOTES:
                raise NoteLimitError("This group has reached the 30-note limit")
            note = {
                "content": content,
                "revision": revision,
                "created_at": old["created_at"] if old else now,
                "created_by": old["created_by"] if old else actor_id,
                "updated_at": now,
                "updated_by": actor_id,
            }
            result = await self.db.groups.update_one(
                {"_id": group_id}, {"$set": {f"{_FIELD}.{name}": note}}, session=session
            )
            if result.matched_count != 1:
                raise LookupError("Group not found")
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "group_id": group_id,
                    "user_id": actor_id,
                    "kind": "note_changed",
                    "action": "updated" if old else "created",
                    "note_name": name,
                    "note_revision": revision,
                    "created_at": now,
                },
                session=session,
            )
            return _public(name, note)

        return await transaction(self.client, apply)

    async def delete(
        self, group_id: int, actor_id: int, name: str, expected_revision: str, now: datetime
    ) -> dict[str, Any]:
        validate_group_id(group_id)
        validate_actor_id(actor_id)
        validate_note_name(name)
        validate_revision(expected_revision)
        event_id = uuid4().hex

        async def apply(session: Any) -> dict[str, Any]:
            notes = await self._read(group_id, session=session)
            old = notes.get(name)
            if old is None or old["revision"] != expected_revision:
                raise NoteConflict("This note changed or was removed; reload notes before retrying")
            result = await self.db.groups.update_one(
                {"_id": group_id}, {"$unset": {f"{_FIELD}.{name}": ""}}, session=session
            )
            if result.matched_count != 1:
                raise LookupError("Group not found")
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "group_id": group_id,
                    "user_id": actor_id,
                    "kind": "note_changed",
                    "action": "deleted",
                    "note_name": name,
                    "note_revision": expected_revision,
                    "created_at": now,
                },
                session=session,
            )
            return {"name": name, "status": "deleted"}

        return await transaction(self.client, apply)
