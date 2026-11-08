"""Indexed character catalog and inventory read models."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Any, cast
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from summon.models import CharacterChanges, CharacterInput, InventorySort, normalize_name
from summon.rarities import spawn_values
from summon.repositories.base import transaction, utcnow
from summon.repositories.character_numbers import CharacterNumbers, numeric_slug
from summon.repositories.rarity_weights import SpawnWeightsRepository
from summon.services.artwork import MAX_ARTWORK_BYTES, Artwork


def public_character(doc: dict[str, Any]) -> dict[str, Any]:
    character = {
        "id": doc["_id"],
        "name": doc["name"],
        "category": doc["category"],
        "rarity": doc["rarity"],
        "image_url": doc.get("image_url", ""),
        "description": doc.get("description", ""),
        "active": doc.get("active", True),
    }
    if isinstance(doc.get("number"), int) and not isinstance(doc["number"], bool):
        character["number"] = doc["number"]
    if doc.get("media_type") in {"video", "animation"}:
        character["media_type"] = doc["media_type"]
    return character


class CatalogConflict(Exception):
    """A catalog ID exists or a command was already processed."""


class CatalogNotFound(Exception):
    """A requested catalog record does not exist."""


class CatalogStaleRevision(CatalogConflict):
    """A reviewed catalog record changed before it could be saved."""


class CatalogRevisionUnavailable(Exception):
    """A catalog record cannot be safely compared for a reviewed save."""


# Include every field a catalog upsert can replace, including nonpublic media IDs.
# Presence is signed separately from null so legacy records compare correctly.
_REVISION_FIELDS = (
    "name",
    "name_key",
    "category",
    "rarity",
    "image_url",
    "description",
    "active",
    "media_type",
    "telegram_file_id",
)
_REVISION_TEXT_LIMITS = {
    "name": 120,
    "name_key": 4096,
    "category": 120,
    "rarity": 64,
    "image_url": 2048,
    "description": 1200,
    "media_type": 32,
    "telegram_file_id": 2048,
}


def catalog_revision(character_id: str, doc: dict[str, Any] | None, secret: str) -> str:
    """Authenticate the editable state without exposing internal media IDs."""
    if len(secret) < 32:
        raise CatalogRevisionUnavailable("Catalog revision key is unavailable")
    state: list[Any] = [character_id, doc is not None]
    if doc is not None:
        for field, maximum in _REVISION_TEXT_LIMITS.items():
            value = doc.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise CatalogRevisionUnavailable("Catalog record requires operator repair")
        if "active" in doc and not isinstance(doc["active"], bool):
            raise CatalogRevisionUnavailable("Catalog record requires operator repair")
        state.extend((field, field in doc, doc.get(field)) for field in _REVISION_FIELDS)
    try:
        encoded = json.dumps(state, ensure_ascii=True, sort_keys=True, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise CatalogRevisionUnavailable("Catalog record requires operator repair") from exc
    return hmac.new(
        secret.encode(), b"summon-catalog-revision-v1\0" + encoded, hashlib.sha256
    ).hexdigest()


def _check_artwork(artwork: Artwork | None, image_url: str | None) -> None:
    if artwork is not None and (
        image_url != artwork.path
        or not 0 < len(artwork.content) <= MAX_ARTWORK_BYTES
        or not artwork.content.startswith(b"\xff\xd8\xff")
        or hashlib.sha256(artwork.content).hexdigest() != artwork.digest
    ):
        raise ValueError("Invalid processed artwork")


class CatalogRepository:
    def __init__(self, database: Any, *, client: Any | None = None) -> None:
        self.db = database
        self.client = client
        self.spawn_weight_store = SpawnWeightsRepository(database, client=client)
        self.rng = secrets.SystemRandom()

    async def edit_state(self, character_id: str, *, secret: str) -> dict[str, Any]:
        """Read an exact internal ID, including inactive records, for an operator review."""
        projection = {field: 1 for field in (*_REVISION_FIELDS, "number")}
        doc = await self.db.characters.find_one({"_id": character_id}, projection)
        revision = catalog_revision(character_id, doc, secret)
        try:
            character = public_character(doc) if doc else None
        except (KeyError, TypeError) as exc:
            raise CatalogRevisionUnavailable("Catalog record requires operator repair") from exc
        return {"revision": revision, "character": character}

    async def upsert_character(
        self,
        data: CharacterInput,
        *,
        actor_id: int,
        expected_revision: str | None = None,
        revision_secret: str | None = None,
    ) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("Catalog writes require a transaction client")
        if (expected_revision is None) != (revision_secret is None):
            raise CatalogRevisionUnavailable("A revision and key are both required")
        if expected_revision is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            raise CatalogRevisionUnavailable("Invalid catalog revision")
        values = data.model_dump(exclude={"id"})
        values["name_key"] = normalize_name(data.name)
        now, random_key, event_id = utcnow(), self.rng.random(), uuid4().hex
        numbers = CharacterNumbers(self.client, self.db)
        await numbers.ensure_initialized()

        async def save(session: Any) -> dict[str, Any]:
            existing = await self.db.characters.find_one({"_id": data.id}, session=session)
            if expected_revision is not None and revision_secret is not None:
                current = catalog_revision(data.id, existing, revision_secret)
                if not hmac.compare_digest(expected_revision, current):
                    raise CatalogStaleRevision("The catalog record changed after review")
            number = (
                existing.get("number") if existing else await numbers.allocate(data.id, session)
            )
            if not isinstance(number, int) or isinstance(number, bool):
                raise RuntimeError("Existing character has no stable number")
            write_values = dict(values)
            if existing and data.image_url == existing.get("image_url"):
                # Editing unrelated metadata must not discard a Telegram backup
                # or silently change video artwork into a photo.
                for field in ("media_type", "telegram_file_id"):
                    if field not in data.model_fields_set:
                        write_values.pop(field, None)
                if (
                    "media_type" in data.model_fields_set
                    and data.media_type != existing.get("media_type", "photo")
                    and "telegram_file_id" not in data.model_fields_set
                ):
                    write_values["telegram_file_id"] = None
            await self.db.characters.update_one(
                {"_id": data.id},
                {
                    "$set": write_values,
                    "$setOnInsert": {"random_key": random_key, "created_at": now, "number": number},
                },
                upsert=True,
                session=session,
            )
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "user_id": actor_id,
                    "kind": "character_saved",
                    "character_id": data.id,
                    "fields": sorted(values),
                    "created_at": now,
                },
                session=session,
            )
            return public_character(
                {**(existing or {}), "_id": data.id, "number": number, **write_values}
            )

        return await transaction(self.client, save)

    async def _insert_artwork(self, artwork: Artwork | None, now: Any, session: Any) -> None:
        if artwork is not None:
            await self.db.character_artworks.update_one(
                {"_id": artwork.digest},
                {
                    "$setOnInsert": {
                        "content": artwork.content,
                        "created_at": now,
                        "content_type": "image/jpeg",
                    }
                },
                upsert=True,
                session=session,
            )

    async def prepare_numbering(self) -> None:
        """Initialize stable aliases before beginning an external media upload."""
        if self.client is None:
            raise RuntimeError("Catalog writes require a transaction client")
        await CharacterNumbers(self.client, self.db).ensure_initialized()

    async def catalog_event_exists(self, event_id: str) -> bool:
        """Avoid repeating optional external uploads for an already-audited command."""
        return bool(await self.db.activity.find_one({"_id": event_id}, {"_id": 1}))

    async def character_exists(self, character_id: str) -> bool:
        """Advisory preflight only; the write transaction remains authoritative."""
        return bool(await self.db.characters.find_one({"_id": character_id}, {"_id": 1}))

    async def create_character(
        self,
        data: CharacterInput,
        *,
        actor_id: int,
        event_id: str,
        artwork: Artwork | None = None,
    ) -> dict[str, Any]:
        """Create only; photo, catalog row and idempotent audit commit together."""
        if self.client is None:
            raise RuntimeError("Catalog writes require a transaction client")
        _check_artwork(artwork, data.image_url)
        values = data.model_dump(exclude={"id"})
        values["name_key"] = normalize_name(data.name)
        now, random_key = utcnow(), self.rng.random()
        numbers = CharacterNumbers(self.client, self.db)
        await numbers.ensure_initialized()

        async def create(session: Any) -> dict[str, Any]:
            number = await numbers.allocate(data.id, session)
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "user_id": actor_id,
                    "kind": "character_created",
                    "character_id": data.id,
                    "fields": sorted(values),
                    "created_at": now,
                },
                session=session,
            )
            await self.db.characters.insert_one(
                {
                    "_id": data.id,
                    **values,
                    "number": number,
                    "random_key": random_key,
                    "created_at": now,
                },
                session=session,
            )
            await self._insert_artwork(artwork, now, session)
            return public_character({"_id": data.id, "number": number, **values})

        try:
            return await transaction(self.client, create)
        except DuplicateKeyError as exc:
            raise CatalogConflict(
                "Character ID exists or this command was already processed"
            ) from exc

    async def update_character(
        self,
        character_id: str,
        changes: CharacterChanges,
        *,
        actor_id: int,
        event_id: str,
        artwork: Artwork | None = None,
        telegram_file_id: str | None = None,
    ) -> None:
        """Update only supplied metadata, without changing ID or ownership."""
        if self.client is None:
            raise RuntimeError("Catalog writes require a transaction client")
        values = changes.model_dump(exclude_none=True)
        if not values:
            raise ValueError("No character metadata was supplied")
        _check_artwork(artwork, values.get("image_url"))
        if telegram_file_id is not None and (
            "image_url" not in values
            or not isinstance(telegram_file_id, str)
            or not 1 <= len(telegram_file_id) <= 2048
            or not telegram_file_id.isascii()
            or not telegram_file_id.isprintable()
        ):
            raise ValueError("A valid Telegram file ID requires a new artwork URL")
        if "image_url" in values:
            # A new URL must never retain a file ID for unrelated old media.
            values.update(media_type="photo", telegram_file_id=telegram_file_id)
        if "name" in values:
            values["name_key"] = normalize_name(values["name"])
        now = utcnow()

        async def update(session: Any) -> None:
            # A deterministic audit ID makes replay of a Telegram command a
            # transaction conflict instead of overwriting a newer edit.
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "user_id": actor_id,
                    "kind": "character_updated",
                    "character_id": character_id,
                    "fields": sorted(values),
                    "created_at": now,
                },
                session=session,
            )
            result = await self.db.characters.update_one(
                {"_id": character_id}, {"$set": values}, session=session
            )
            if not result.matched_count:
                raise CatalogNotFound("Character not found")
            await self._insert_artwork(artwork, now, session)

        try:
            await transaction(self.client, update)
        except DuplicateKeyError as exc:
            raise CatalogConflict("This command was already processed") from exc

    async def artwork(self, digest: str) -> bytes | None:
        doc = await self.db.character_artworks.find_one({"_id": digest}, {"content": 1})
        data = doc.get("content") if doc else None
        if (
            not isinstance(data, bytes)
            or not 0 < len(data) <= MAX_ARTWORK_BYTES
            or not data.startswith(b"\xff\xd8\xff")
            or hashlib.sha256(data).hexdigest() != digest
        ):
            return None
        return data

    async def spawn_weights(self) -> dict[str, int]:
        return await self.spawn_weight_store.read()

    async def set_spawn_weight(
        self, rarity: str, weight: int, *, actor_id: int, event_id: str
    ) -> tuple[int, int]:
        return await self.spawn_weight_store.set_weight(
            rarity, weight, actor_id=actor_id, event_id=event_id
        )

    async def choose(self) -> dict[str, Any] | None:
        """Weight nonempty rarities, then uniformly choose one stable-catalog offset.

        Unlike a fixed random-key pivot, each character in a rarity has the same
        probability. Counts and index skips cost O(bucket size); a concurrent catalog
        edit can invalidate the offset, in which case selection is retried, not biased
        toward an arbitrary fallback record.
        """
        configured = await self.spawn_weights()
        for _ in range(3):
            remaining = {name: weight for name, weight in configured.items() if weight > 0}
            while remaining:
                rarity = self.rng.choices(list(remaining), weights=list(remaining.values()), k=1)[0]
                accepted = spawn_values(rarity)
                query: dict[str, Any] = {
                    "active": True,
                    "rarity": accepted[0] if len(accepted) == 1 else {"$in": list(accepted)},
                }
                count = await self.db.characters.count_documents(query)
                if not count:
                    del remaining[rarity]
                    continue
                char = await self.db.characters.find_one(
                    query, sort=[("_id", ASCENDING)], skip=self.rng.randrange(count)
                )
                if char:
                    return cast(dict[str, Any], char)
                break  # A catalog edit may have invalidated the count; start over.
            else:
                return None
        return None

    async def get(self, character_id: str) -> dict[str, Any] | None:
        number = numeric_slug(character_id)
        if number is not None:
            if self.client is None:
                raise RuntimeError("Numeric lookup requires a transaction client")
            await CharacterNumbers(self.client, self.db).ensure_initialized()
            doc = await self.db.characters.find_one({"number": number, "active": True})
        else:
            doc = await self.db.characters.find_one({"_id": character_id, "active": True})
        return public_character(doc) if doc else None

    async def details(self, user_id: int, character_id: str) -> dict[str, Any] | None:
        character = await self.get(character_id)
        if not character:
            return None
        # Inventory and wishlists always reference the immutable internal slug.
        canonical_id = character["id"]
        owned = (
            await self.db.inventory.find_one({"user_id": user_id, "character_id": canonical_id})
            or {}
        )
        wished = await self.db.wishlists.find_one(
            {"user_id": user_id, "character_id": canonical_id}
        )
        character.update(
            quantity=owned.get("quantity", 0),
            favorite=owned.get("favorite", False),
            wishlisted=bool(wished),
        )
        return character

    async def list(
        self,
        user_id: int,
        *,
        search: str = "",
        rarity: str | None = None,
        category: str | None = None,
        page: int = 1,
        limit: int = 24,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"active": True}
        if search:
            query["name_key"] = {"$regex": "^" + re.escape(normalize_name(search))}
        if rarity:
            query["rarity"] = rarity
        if category:
            query["category"] = category
        cursor = self.db.characters.find(query).sort([("name_key", ASCENDING), ("_id", ASCENDING)])
        docs = await cursor.skip((page - 1) * limit).limit(limit).to_list(length=limit)
        ids = [doc["_id"] for doc in docs]
        owned = await self.db.inventory.find(
            {"user_id": user_id, "character_id": {"$in": ids}, "quantity": {"$gt": 0}}
        ).to_list(length=limit)
        wished = await self.db.wishlists.find(
            {"user_id": user_id, "character_id": {"$in": ids}}
        ).to_list(length=limit)
        ownership = {item["character_id"]: item for item in owned}
        wishlist = {item["character_id"] for item in wished}
        items = []
        for doc in docs:
            entry = public_character(doc)
            record = ownership.get(doc["_id"], {})
            entry.update(
                quantity=record.get("quantity", 0),
                favorite=record.get("favorite", False),
                wishlisted=doc["_id"] in wishlist,
            )
            items.append(entry)
        total = await self.db.characters.count_documents(query)
        return {"items": items, "page": page, "limit": limit, "total": total}

    async def inventory(
        self,
        user_id: int,
        *,
        page: int = 1,
        limit: int = 24,
        favorite: bool = False,
        duplicates: bool = False,
        sort: InventorySort = "recent",
    ) -> dict[str, Any]:
        if sort not in {"recent", "copies"}:
            raise ValueError("Unknown inventory ordering")
        query: dict[str, Any] = {"user_id": user_id, "quantity": {"$gt": 1 if duplicates else 0}}
        if favorite:
            query["favorite"] = True
        sort_field = "quantity" if sort == "copies" else "acquired_at"
        cursor = self.db.inventory.find(query).sort(
            [(sort_field, DESCENDING), ("character_id", ASCENDING)]
        )
        rows = await cursor.skip((page - 1) * limit).limit(limit).to_list(length=limit)
        chars = await self.db.characters.find(
            {"_id": {"$in": [row["character_id"] for row in rows]}}
        ).to_list(length=limit)
        by_id = {doc["_id"]: doc for doc in chars}
        items = []
        for row in rows:
            char = by_id.get(row["character_id"])
            if char:
                entry = public_character(char)
                entry.update(quantity=row["quantity"], favorite=row.get("favorite", False))
                items.append(entry)
        return {
            "items": items,
            "page": page,
            "limit": limit,
            "total": await self.db.inventory.count_documents(query),
        }

    async def inventory_cursor(
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
        from summon.repositories.inventory_paging import SnapshotInventory

        return await SnapshotInventory(self.db, self.client).page(
            user_id,
            secret=secret,
            cursor=cursor,
            limit=limit,
            favorite=favorite,
            duplicates=duplicates,
            sort=sort,
        )

    async def set_favorite(self, user_id: int, character_id: str, favorite: bool) -> bool:
        result = await self.db.inventory.update_one(
            {"user_id": user_id, "character_id": character_id, "quantity": {"$gt": 0}},
            {"$set": {"favorite": favorite}},
        )
        return bool(result.matched_count)

    async def set_wishlist(self, user_id: int, character_id: str, wished: bool) -> bool:
        if not await self.db.characters.find_one({"_id": character_id, "active": True}, {"_id": 1}):
            return False
        if wished:
            await self.db.wishlists.update_one(
                {"user_id": user_id, "character_id": character_id},
                {"$setOnInsert": {"created_at": utcnow()}},
                upsert=True,
            )
        else:
            await self.db.wishlists.delete_one({"user_id": user_id, "character_id": character_id})
        return True
