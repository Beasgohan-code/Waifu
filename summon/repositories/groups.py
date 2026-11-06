"""Group settings, administrator links, and auditable moderation records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from pymongo import DESCENDING, ReturnDocument

from summon.models import GroupSettings
from summon.repositories.base import transaction


def public_group(doc: dict[str, Any]) -> dict[str, Any]:
    # Never include active_spawn: it contains the answer to the guessing challenge.
    return {
        "id": doc["_id"],
        "title": doc["title"],
        "settings": GroupSettings.model_validate(doc["settings"]).model_dump(),
        "total_messages": doc.get("total_messages", 0),
    }


class GroupRepository:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def ensure(self, group_id: int, title: str, now: datetime) -> dict[str, Any]:
        result = await self.db.groups.find_one_and_update(
            {"_id": group_id},
            {
                "$setOnInsert": {
                    "title": title[:255],
                    "settings": GroupSettings().model_dump(),
                    "message_count": 0,
                    "total_messages": 0,
                    "next_spawn_at": now,
                    "active_spawn": None,
                    "created_at": now,
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return cast(dict[str, Any], result)

    async def get(self, group_id: int) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, await self.db.groups.find_one({"_id": group_id}))

    async def link_admin(self, group_id: int, user_id: int, now: datetime) -> None:
        await self.db.group_admins.update_one(
            {"group_id": group_id, "user_id": user_id},
            {"$set": {"verified_at": now}},
            upsert=True,
        )

    async def linked_ids(self, user_id: int, limit: int = 100) -> list[int]:
        cursor = self.db.group_admins.find({"user_id": user_id}, {"group_id": 1}).limit(limit)
        return [row["group_id"] async for row in cursor]

    async def update_settings(
        self, group_id: int, actor_id: int, patch: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        event_id = uuid4().hex

        async def update(session: Any) -> dict[str, Any]:
            group = await self.db.groups.find_one({"_id": group_id}, session=session)
            if not group:
                raise LookupError("Group not found")
            settings = GroupSettings.model_validate({**group["settings"], **patch})
            normalized = settings.model_dump()
            await self.db.groups.update_one(
                {"_id": group_id},
                {"$set": {f"settings.{key}": normalized[key] for key in patch}},
                session=session,
            )
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "group_id": group_id,
                    "user_id": actor_id,
                    "kind": "settings_changed",
                    "fields": sorted(patch),
                    "created_at": now,
                },
                session=session,
            )
            group["settings"] = normalized
            return public_group(group)

        return await transaction(self.client, update)

    async def record_moderation(
        self, group_id: int, actor_id: int, target_id: int, action: str, reason: str, now: datetime
    ) -> dict[str, Any]:
        event_id = uuid4().hex

        async def record(session: Any) -> dict[str, Any]:
            count = None
            if action == "warn":
                await self.db.warnings.insert_one(
                    {
                        "_id": event_id,
                        "group_id": group_id,
                        "user_id": target_id,
                        "actor_id": actor_id,
                        "reason": reason,
                        "created_at": now,
                    },
                    session=session,
                )
                # Mongo equality to null also matches old records without this field.
                count = await self.db.warnings.count_documents(
                    {"group_id": group_id, "user_id": target_id, "revoked_at": None},
                    session=session,
                )
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "group_id": group_id,
                    "user_id": actor_id,
                    "target_id": target_id,
                    "kind": "moderation",
                    "action": action,
                    "reason": reason,
                    "created_at": now,
                    **({"warning_id": event_id} if action == "warn" else {}),
                },
                session=session,
            )
            return {"action": action, "target_id": target_id, "warning_count": count}

        return await transaction(self.client, record)

    async def list_warnings(
        self, group_id: int, target_id: int, *, before: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        if not 1 <= limit <= 25:
            raise ValueError("Page size must be between 1 and 25")
        active = {"group_id": group_id, "user_id": target_id, "revoked_at": None}
        query: dict[str, Any] = dict(active)
        if before is not None:
            # A cursor remains usable if its anchor was revoked after it was read.
            # It must still belong to the same group and target.
            anchor = await self.db.warnings.find_one(
                {"_id": before, "group_id": group_id, "user_id": target_id},
                {"created_at": 1},
                max_time_ms=5000,
            )
            if not anchor or not isinstance(anchor.get("created_at"), datetime):
                raise LookupError("Warning cursor not found")
            created_at = anchor["created_at"]
            query["$or"] = [
                {"created_at": {"$lt": created_at}},
                {"created_at": created_at, "_id": {"$lt": before}},
            ]
        cursor = (
            self.db.warnings.find(query)
            .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
            .limit(limit + 1)
            .max_time_ms(5000)
        )
        rows = await cursor.to_list(length=limit + 1)
        count = await self.db.warnings.count_documents(active, maxTimeMS=5000)
        page = rows[:limit]
        return {
            "items": [
                {
                    "id": row["_id"],
                    "actor_id": row["actor_id"],
                    "reason": row.get("reason", ""),
                    "created_at": row["created_at"],
                }
                for row in page
            ],
            "active_count": count,
            "next_cursor": page[-1]["_id"] if len(rows) > limit else None,
        }

    async def revoke_warning(
        self,
        group_id: int,
        actor_id: int,
        target_id: int,
        warning_id: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        event_id = uuid4().hex  # Stable if the driver's transaction callback is retried.

        async def revoke(session: Any) -> dict[str, Any]:
            warning = await self.db.warnings.find_one_and_update(
                {"_id": warning_id, "group_id": group_id, "user_id": target_id, "revoked_at": None},
                {"$set": {"revoked_at": now, "revoked_by": actor_id, "revocation_reason": reason}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if warning is None:
                # Do not distinguish absent, wrong group/target, or already revoked.
                raise LookupError("Warning not found or no longer active")
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "group_id": group_id,
                    "user_id": actor_id,
                    "target_id": target_id,
                    "kind": "moderation",
                    "action": "warning_revoked",
                    "warning_id": warning_id,
                    "reason": reason,
                    "created_at": now,
                },
                session=session,
            )
            return {"id": warning_id, "status": "revoked"}

        return await transaction(self.client, revoke)

    async def record_spam(self, group_id: int, user_id: int, reason: str, now: datetime) -> None:
        await self.db.activity.insert_one(
            {
                "_id": uuid4().hex,
                "group_id": group_id,
                "user_id": user_id,
                "kind": "spam_removed",
                "reason": reason,
                "created_at": now,
            }
        )

    async def recent_activity(self, group_id: int, limit: int = 20) -> list[dict[str, Any]]:
        cursor = (
            self.db.activity.find({"group_id": group_id})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )
        return [
            {
                "kind": row["kind"],
                "action": row.get("action"),
                "reason": row.get("reason"),
                "actor_id": row.get("user_id"),
                "target_id": row.get("target_id"),
                "warning_id": row.get("warning_id"),
                "note_name": row.get("note_name"),
                "reply_name": row.get("reply_name"),
                "created_at": row["created_at"],
            }
            async for row in cursor
        ]
