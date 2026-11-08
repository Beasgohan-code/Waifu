"""Short-lived, actor-bound keyset cursors over MongoDB snapshot reads.

A cursor carries a signed snapshot timestamp, not database credentials. MongoDB
5.0+ supports ``atClusterTime`` on reads outside a transaction; the server may
expire that history sooner than our token lifetime. We never silently continue
from a newer snapshot because mutable quantities and favorites change ordering
or membership. The original offset endpoint remains available on MongoDB 4.4.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Literal

from bson.timestamp import Timestamp
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pymongo.errors import OperationFailure, PyMongoError

from summon.repositories.catalog import public_character

InventorySort = Literal["recent", "copies"]
CURSOR_LIFETIME_SECONDS = 240


class CursorInvalid(ValueError):
    """The cursor is malformed, forged, or belongs to a different query."""


class CursorExpired(ValueError):
    """The original snapshot can no longer be continued."""


class CursorUnavailable(RuntimeError):
    """The server cannot safely serve snapshot pagination."""


class CursorDataError(RuntimeError):
    """Existing inventory contains data that cannot be ordered safely."""


class CursorState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    version: Literal[1] = 1
    user_id: int = Field(gt=0)
    favorite: bool
    duplicates: bool
    sort: InventorySort
    limit: int = Field(ge=1, le=50)
    snapshot_seconds: int = Field(ge=0, le=2**32 - 1)
    snapshot_increment: int = Field(ge=0, le=2**32 - 1)
    last_id: str | None = Field(min_length=1, max_length=512)
    last_value: str | int | None
    page: int = Field(ge=1)
    total: int = Field(ge=1)
    consumed: int = Field(ge=0)
    started_at: int = Field(ge=0)
    preview: bool


class CursorCodec:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise CursorUnavailable("Cursor signing is not configured")
        self.serializer = URLSafeTimedSerializer(secret, salt="summon-inventory-snapshot-v1")

    def encode(self, state: CursorState) -> str:
        return self.serializer.dumps(state.model_dump())

    def decode(
        self,
        token: str,
        *,
        user_id: int,
        favorite: bool,
        duplicates: bool,
        sort: InventorySort,
        limit: int,
        preview: bool,
    ) -> CursorState:
        if not token or len(token) > 2048:
            raise CursorInvalid("Invalid cursor")
        try:
            raw = self.serializer.loads(token, max_age=CURSOR_LIFETIME_SECONDS)
            state = CursorState.model_validate(raw)
        except SignatureExpired as exc:
            raise CursorExpired("Snapshot expired") from exc
        except (BadSignature, ValidationError, TypeError, ValueError) as exc:
            raise CursorInvalid("Invalid cursor") from exc
        if (
            state.user_id,
            state.favorite,
            state.duplicates,
            state.sort,
            state.limit,
            state.preview,
        ) != (
            user_id,
            favorite,
            duplicates,
            sort,
            limit,
            preview,
        ):
            raise CursorInvalid("Cursor does not belong to this collection view")
        now = int(time.time())
        if now - state.started_at > CURSOR_LIFETIME_SECONDS:
            raise CursorExpired("Snapshot expired")
        if state.started_at > now + 30 or state.consumed >= state.total:
            raise CursorInvalid("Invalid cursor")
        if state.page == 1:
            if state.consumed != 0 or state.last_id is not None or state.last_value is not None:
                raise CursorInvalid("Invalid cursor")
        elif state.consumed == 0 or state.last_id is None:
            raise CursorInvalid("Invalid cursor")
        return state


def seek_after(sort: InventorySort, value: str | int | None, character_id: str) -> dict[str, Any]:
    """Seek after a (descending value, ascending unique character ID) pair.

    Missing and explicit null acquisition dates sort together after BSON dates.
    Values of other BSON types are rejected by the initial snapshot validation.
    """
    if sort == "copies":
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise CursorInvalid("Invalid cursor position")
        return {
            "$or": [
                {"quantity": {"$lt": value}},
                {"quantity": value, "character_id": {"$gt": character_id}},
            ]
        }
    if value is None:
        return {"acquired_at": None, "character_id": {"$gt": character_id}}
    if not isinstance(value, str):
        raise CursorInvalid("Invalid cursor position")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("UTC offset required")
        parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise CursorInvalid("Invalid cursor position") from exc
    return {
        "$or": [
            {"acquired_at": {"$lt": parsed}},
            {"acquired_at": parsed, "character_id": {"$gt": character_id}},
            {"acquired_at": None},
        ]
    }


class SnapshotInventory:
    """Read bounded keyset pages and joins from one immutable Mongo snapshot."""

    def __init__(self, db: Any, client: Any) -> None:
        self.db = db
        self.client = client

    async def _check_server(self) -> None:
        try:
            try:
                hello = await self.client.admin.command("hello")
            except OperationFailure as exc:
                if exc.code != 59:  # MongoDB 4.4 before the hello alias
                    raise
                hello = await self.client.admin.command("isMaster")
        except PyMongoError as exc:
            raise CursorUnavailable("Could not check snapshot support") from exc
        if not isinstance(hello.get("maxWireVersion"), int) or hello["maxWireVersion"] < 13:
            raise CursorUnavailable("Snapshot pagination requires MongoDB 5.0 or later")

    async def _aggregate(
        self, session: Any, pipeline: list[dict[str, Any]], at: Timestamp | None, batch_size: int
    ) -> tuple[list[dict[str, Any]], Timestamp]:
        concern: dict[str, Any] = {"level": "snapshot"}
        if at is not None:
            concern["atClusterTime"] = at
        command = {
            "aggregate": "inventory",
            "pipeline": pipeline,
            "cursor": {"batchSize": batch_size},
            "readConcern": concern,
            "maxTimeMS": 5000,
        }
        try:
            result = await self.db.command(command, session=session)
        except OperationFailure as exc:
            if exc.code in {239, 246, 286}:
                raise CursorExpired("Snapshot history expired") from exc
            if exc.code in {20, 59, 72, 303}:
                raise CursorUnavailable("This database cannot serve snapshot pages") from exc
            raise
        cursor = result.get("cursor", {})
        actual_at = cursor.get("atClusterTime", at)
        if not isinstance(actual_at, Timestamp) or (at is not None and actual_at != at):
            raise CursorUnavailable("Database did not return the requested snapshot")
        rows = cursor.get("firstBatch")
        if not isinstance(rows, list):
            raise CursorUnavailable("Database returned an invalid snapshot page")
        cursor_id = cursor.get("id", 0)
        if cursor_id:
            # A batch can be shorter than requested due to MongoDB's 16 MiB limit.
            # Keep the returned rows and resume by key, not by a server cursor. Killing
            # it avoids tying a database cursor to a browser's lifetime.
            try:
                await self.db.command(
                    {"killCursors": "inventory", "cursors": [cursor_id]}, session=session
                )
            except PyMongoError:
                pass  # The server will eventually expire the unused cursor.
        return rows, actual_at

    async def page(
        self,
        user_id: int,
        *,
        secret: str,
        cursor: str | None = None,
        limit: int = 24,
        favorite: bool = False,
        duplicates: bool = False,
        sort: InventorySort = "recent",
    ) -> dict[str, Any]:
        codec = CursorCodec(secret)
        state = (
            codec.decode(
                cursor,
                user_id=user_id,
                favorite=favorite,
                duplicates=duplicates,
                sort=sort,
                limit=limit,
                preview=False,
            )
            if cursor
            else None
        )
        if state is None:
            await self._check_server()
        base: dict[str, Any] = {"user_id": user_id, "quantity": {"$gt": 1 if duplicates else 0}}
        if favorite:
            base["favorite"] = True
        position = (
            seek_after(sort, state.last_value, state.last_id)
            if state and state.last_id is not None
            else None
        )
        match = {"$and": [base, position]} if position else base
        order = {"acquired_at" if sort == "recent" else "quantity": -1, "character_id": 1}
        pipeline: list[dict[str, Any]] = [
            {"$match": match},
            {"$sort": order},
            {"$limit": limit},
            {
                "$lookup": {
                    "from": "characters",
                    "localField": "character_id",
                    "foreignField": "_id",
                    "as": "character",
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "character_id": 1,
                    "quantity": 1,
                    "favorite": 1,
                    "acquired_at": 1,
                    "character._id": 1,
                    "character.name": 1,
                    "character.category": 1,
                    "character.rarity": 1,
                    "character.image_url": 1,
                    "character.description": 1,
                    "character.active": 1,
                }
            },
        ]
        at = Timestamp(state.snapshot_seconds, state.snapshot_increment) if state else None
        started_at = state.started_at if state else int(time.time())
        consumed = state.consumed if state else 0
        page_number = state.page if state else 1
        async with self.client.start_session(causal_consistency=False) as session:
            rows, at = await self._aggregate(session, pipeline, at, limit + 1)
            if state:
                total = state.total
            else:
                valid_quantity: dict[str, Any] = {"$in": [{"$type": "$quantity"}, ["int", "long"]]}
                eligible: list[dict[str, Any]] = [
                    valid_quantity,
                    {"$gt": ["$quantity", 1 if duplicates else 0]},
                ]
                if favorite:
                    eligible.append({"$eq": ["$favorite", True]})
                selected = {"$and": eligible}
                valid_id = {
                    "$cond": [
                        {"$eq": [{"$type": "$character_id"}, "string"]},
                        {
                            "$and": [
                                {"$ne": ["$character_id", ""]},
                                {"$lte": [{"$strLenCP": "$character_id"}, 512]},
                            ]
                        },
                        False,
                    ]
                }
                count_pipeline: list[dict[str, Any]] = [
                    # Inspect all the actor's rows so bad quantities cannot disappear
                    # merely because the positive-quantity page query excludes them.
                    {"$match": {"user_id": user_id}},
                    {
                        "$group": {
                            "_id": None,
                            "total": {"$sum": {"$cond": [selected, 1, 0]}},
                            "invalid_quantity": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$or": [
                                                {"$not": [valid_quantity]},
                                                {"$lt": ["$quantity", 0]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "invalid_id": {
                                "$sum": {
                                    "$cond": [
                                        {"$and": [selected, {"$not": [valid_id]}]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "invalid_time": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                selected,
                                                {
                                                    "$not": [
                                                        {
                                                            "$in": [
                                                                {"$type": "$acquired_at"},
                                                                ["date", "null", "missing"],
                                                            ]
                                                        }
                                                    ]
                                                },
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ]
                counts, _ = await self._aggregate(session, count_pipeline, at, 2)
                total = counts[0]["total"] if counts else 0
                if counts and (
                    counts[0]["invalid_quantity"]
                    or counts[0]["invalid_id"]
                    or (sort == "recent" and counts[0]["invalid_time"])
                ):
                    raise CursorDataError("Inventory contains invalid sort data")
        items = []
        for row in rows:
            linked = row.get("character")
            if not isinstance(linked, list) or len(linked) != 1:
                raise CursorDataError("An inventory item has no matching character")
            char = public_character(linked[0])
            char["quantity"] = row["quantity"]
            char["favorite"] = row.get("favorite", False)
            items.append(char)
        consumed += len(items)
        if consumed > total or (consumed < total and not items):
            raise CursorDataError("Inventory snapshot could not be traversed safely")
        next_cursor = None
        if consumed < total:
            last = rows[-1]
            last_id = last.get("character_id")
            value = last.get("acquired_at") if sort == "recent" else last.get("quantity")
            if sort == "recent" and isinstance(value, datetime):
                # BSON datetimes are UTC, including when a test or client returns naive values.
                value = (
                    value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
                ).isoformat()
            elif sort == "copies" and isinstance(value, int) and not isinstance(value, bool):
                value = int(value)  # BSON Int64 subclasses int.
            try:
                if not isinstance(last_id, str) or not last_id:
                    raise ValueError("Invalid character ID")
                seek_after(sort, value, last_id)
                next_cursor = codec.encode(
                    CursorState(
                        user_id=user_id,
                        favorite=favorite,
                        duplicates=duplicates,
                        sort=sort,
                        limit=limit,
                        snapshot_seconds=at.time,
                        snapshot_increment=at.inc,
                        last_id=last_id,
                        last_value=value,
                        page=page_number + 1,
                        total=total,
                        consumed=consumed,
                        started_at=started_at,
                        preview=False,
                    )
                )
            except (ValidationError, ValueError, TypeError) as exc:
                raise CursorDataError("Inventory has an invalid sort position") from exc
        page_cursor = None
        if page_number == 1 and total:
            page_cursor = cursor or codec.encode(
                CursorState(
                    user_id=user_id,
                    favorite=favorite,
                    duplicates=duplicates,
                    sort=sort,
                    limit=limit,
                    snapshot_seconds=at.time,
                    snapshot_increment=at.inc,
                    last_id=None,
                    last_value=None,
                    page=1,
                    total=total,
                    consumed=0,
                    started_at=started_at,
                    preview=False,
                )
            )
        return {
            "items": items,
            "total": total,
            "page": page_number,
            "limit": limit,
            "page_cursor": page_cursor,
            "next_cursor": next_cursor,
        }
