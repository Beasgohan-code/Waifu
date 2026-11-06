"""Bounded, group-local auto replies with transactional, metadata-only audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from summon.auto_replies import (
    MAX_AUTO_REPLIES,
    AutoReplyConflict,
    AutoReplyLimitError,
    normalize_trigger,
    stored_rules,
    validate_enabled,
    validate_reply_text,
)
from summon.group_notes import (
    validate_actor_id,
    validate_group_id,
    validate_note_name,
    validate_revision,
)
from summon.repositories.base import transaction

_FIELD = "auto_replies_v1"  # Optional, separate from notes and all legacy fields.
_READ_MAX_MS = 5000


def _public(name: str, rule: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, **rule}


class AutoReplyRepository:
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
        return stored_rules(document)

    async def list(self, group_id: int) -> list[dict[str, Any]]:
        rules = await self._read(group_id)
        return [_public(name, rules[name]) for name in sorted(rules)]

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
        validate_group_id(group_id)
        validate_actor_id(actor_id)
        validate_note_name(name)
        trigger = normalize_trigger(trigger)
        validate_reply_text(response)
        validate_enabled(enabled)
        validate_revision(expected_revision, allow_none=True)
        # Allocate once outside the callback, including on driver retry.
        revision, event_id = uuid4().hex, uuid4().hex

        async def apply(session: Any) -> dict[str, Any]:
            rules = await self._read(group_id, session=session)
            old = rules.get(name)
            if (old is None and expected_revision is not None) or (
                old is not None and old["revision"] != expected_revision
            ):
                raise AutoReplyConflict(
                    "This rule changed or already exists; reload before retrying"
                )
            if old is None and len(rules) >= MAX_AUTO_REPLIES:
                raise AutoReplyLimitError("This group has reached the 20-rule limit")
            if any(other != name and rule["trigger"] == trigger for other, rule in rules.items()):
                raise AutoReplyConflict("Another rule already uses that normalized trigger")
            rule = {
                "trigger": trigger,
                "response": response,
                "enabled": enabled,
                "revision": revision,
                "created_at": old["created_at"] if old else now,
                "created_by": old["created_by"] if old else actor_id,
                "updated_at": now,
                "updated_by": actor_id,
            }
            result = await self.db.groups.update_one(
                {"_id": group_id}, {"$set": {f"{_FIELD}.{name}": rule}}, session=session
            )
            if result.matched_count != 1:
                raise LookupError("Group not found")
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "group_id": group_id,
                    "user_id": actor_id,
                    "kind": "auto_reply_changed",
                    "action": "updated" if old else "created",
                    "reply_name": name,
                    "reply_revision": revision,
                    "created_at": now,
                },
                session=session,
            )
            return _public(name, rule)

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
            rules = await self._read(group_id, session=session)
            old = rules.get(name)
            if old is None or old["revision"] != expected_revision:
                raise AutoReplyConflict("This rule changed or was removed; reload before retrying")
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
                    "kind": "auto_reply_changed",
                    "action": "deleted",
                    "reply_name": name,
                    "reply_revision": expected_revision,
                    "created_at": now,
                },
                session=session,
            )
            return {"name": name, "status": "deleted"}

        return await transaction(self.client, apply)
