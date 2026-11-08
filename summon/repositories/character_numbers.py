"""Stable additive character numbers without changing inventory's slug IDs."""

from __future__ import annotations

import re
from typing import Any

from pymongo import ASCENDING, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from summon.repositories.base import transaction

MAX_NUMBER = 2**53 - 1
MAX_BOOTSTRAP_CHARACTERS = 10_000
_COUNTER_ID = "characters"


class CharacterNumberingUnavailable(RuntimeError):
    """Existing records are ambiguous or too numerous for an automatic migration."""


def numeric_slug(value: object) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,64}", value):
        return None
    number = int(value)
    return number if 0 < number <= MAX_NUMBER else None


def plan_numbers(documents: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Preserve assigned numbers and numeric slugs, then fill gaps in stable order.

    The caller supplies documents sorted by creation time and ID. Conflicting
    numeric slugs such as '1' and '01' cannot be reconciled automatically.
    """
    used: dict[int, str] = {}
    assigned: dict[str, int] = {}
    for doc in documents:
        character_id = doc.get("_id")
        if not isinstance(character_id, str):
            raise CharacterNumberingUnavailable("Character IDs require operator review")
        existing = doc.get("number")
        if "number" in doc and (
            not isinstance(existing, int)
            or isinstance(existing, bool)
            or not 0 < existing <= MAX_NUMBER
        ):
            raise CharacterNumberingUnavailable("Invalid existing character number")
        slug_number = numeric_slug(character_id)
        if existing is not None and slug_number is not None and existing != slug_number:
            raise CharacterNumberingUnavailable("Numeric character ID conflicts with its number")
        number = existing if existing is not None else slug_number
        if number is not None:
            if number in used and used[number] != character_id:
                raise CharacterNumberingUnavailable("Numeric character IDs are ambiguous")
            used[number] = character_id
            assigned[character_id] = number
    next_free = 1
    for doc in documents:
        character_id = doc["_id"]
        if character_id in assigned:
            continue
        while next_free in used:
            next_free += 1
        if next_free > MAX_NUMBER:
            raise CharacterNumberingUnavailable("Character numbers are exhausted")
        assigned[character_id] = next_free
        used[next_free] = character_id
        next_free += 1
    return [(doc["_id"], assigned[doc["_id"]]) for doc in documents]


class CharacterNumbers:
    def __init__(self, client: Any, database: Any) -> None:
        self.client = client
        self.db = database

    async def ensure_initialized(self) -> None:
        if await self.db.catalog_counters.find_one({"_id": _COUNTER_ID}, {"_id": 1}):
            return

        async def initialize(session: Any) -> None:
            # Acquiring this row first serializes initializers and catalog writers.
            await self.db.catalog_counters.insert_one(
                {"_id": _COUNTER_ID, "last_number": 0}, session=session
            )
            cursor = self.db.characters.find(
                {}, {"_id": 1, "number": 1, "created_at": 1}, session=session
            ).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
            documents = await cursor.limit(MAX_BOOTSTRAP_CHARACTERS + 1).to_list(
                length=MAX_BOOTSTRAP_CHARACTERS + 1
            )
            if len(documents) > MAX_BOOTSTRAP_CHARACTERS:
                raise CharacterNumberingUnavailable("Catalog is too large for automatic numbering")
            planned = plan_numbers(documents)
            updates = [
                UpdateOne(
                    {"_id": doc["_id"], "number": {"$exists": False}},
                    {"$set": {"number": number}},
                )
                for doc, (_, number) in zip(documents, planned, strict=True)
                if "number" not in doc
            ]
            for offset in range(0, len(updates), 500):
                result = await self.db.characters.bulk_write(
                    updates[offset : offset + 500], ordered=True, session=session
                )
                if result.modified_count != len(updates[offset : offset + 500]):
                    raise CharacterNumberingUnavailable("Character numbering changed concurrently")
            await self.db.catalog_counters.update_one(
                {"_id": _COUNTER_ID},
                {"$set": {"last_number": max((number for _, number in planned), default=0)}},
                session=session,
            )

        try:
            await transaction(self.client, initialize)
        except DuplicateKeyError:
            # A competing initializer may have committed. Otherwise fail closed;
            # never repair or renumber an existing catalog automatically.
            if not await self.db.catalog_counters.find_one({"_id": _COUNTER_ID}, {"_id": 1}):
                raise CharacterNumberingUnavailable("Character numbering conflicts") from None

    async def allocate(self, character_id: str, session: Any) -> int:
        """Allocate within the same transaction that inserts the character."""
        preferred = numeric_slug(character_id)
        if preferred is not None:
            existing = await self.db.characters.find_one(
                {"number": preferred}, {"_id": 1}, session=session
            )
            if existing:
                raise CharacterNumberingUnavailable("This numeric character ID is already assigned")
            state = await self.db.catalog_counters.find_one_and_update(
                {"_id": _COUNTER_ID},
                {"$max": {"last_number": preferred}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if not state:
                raise CharacterNumberingUnavailable("Character numbering is not initialized")
            return preferred
        state = await self.db.catalog_counters.find_one_and_update(
            {"_id": _COUNTER_ID, "last_number": {"$lt": MAX_NUMBER}},
            {"$inc": {"last_number": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if not state:
            raise CharacterNumberingUnavailable("Character numbers are unavailable or exhausted")
        return int(state["last_number"])
