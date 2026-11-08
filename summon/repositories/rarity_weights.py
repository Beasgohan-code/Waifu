"""Audited operator overrides for the non-wagering group spawn distribution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypeGuard

from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern

from summon.rarities import MAX_SPAWN_WEIGHT, RARITY_WEIGHTS
from summon.repositories.base import transaction


class InvalidSpawnWeights(ValueError):
    """Stored spawn configuration is invalid; do not silently change odds."""


class SpawnWeightConflict(Exception):
    """An event was already applied or a concurrent initialization conflicted."""


def _weight(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SPAWN_WEIGHT


class SpawnWeightsRepository:
    def __init__(self, database: Any, *, client: Any | None = None) -> None:
        self.db = database
        self.client = client

    async def read(self) -> dict[str, int]:
        # Missing overrides retain reference initialization defaults. Explicit
        # zero disables one bucket; a corrupted row fails closed instead of
        # quietly changing the distribution.
        weights: dict[str, int] = {str(name): weight for name, weight in RARITY_WEIGHTS.items()}
        collection = self.db.spawn_weights.with_options(read_concern=ReadConcern("majority"))
        cursor = collection.find({"_id": {"$in": list(weights)}}, {"_id": 1, "weight": 1})
        rows = await cursor.to_list(length=len(weights))
        seen: set[str] = set()
        for row in rows:
            name, value = row.get("_id"), row.get("weight")
            if (
                not isinstance(name, str)
                or name not in weights
                or name in seen
                or not _weight(value)
            ):
                raise InvalidSpawnWeights("Stored spawn weights are invalid")
            seen.add(name)
            weights[name] = value
        return weights

    async def set_weight(
        self, rarity: str, weight: int, *, actor_id: int, event_id: str
    ) -> tuple[int, int]:
        if self.client is None:
            raise RuntimeError("Spawn-weight writes require a transaction client")
        if (
            rarity not in RARITY_WEIGHTS
            or not _weight(weight)
            or isinstance(actor_id, bool)
            or not isinstance(actor_id, int)
            or not 0 < actor_id < 2**53
            or not isinstance(event_id, str)
            or not 1 <= len(event_id) <= 256
        ):
            raise ValueError("Invalid spawn-weight change")
        now = datetime.now(UTC)

        async def save(session: Any) -> tuple[int, int]:
            existing = await self.db.spawn_weights.find_one({"_id": rarity}, session=session)
            previous = existing.get("weight") if existing else RARITY_WEIGHTS[rarity]
            if not _weight(previous):
                raise InvalidSpawnWeights("Stored spawn weight is invalid")
            await self.db.spawn_weights.update_one(
                {"_id": rarity},
                {"$set": {"weight": weight, "updated_by": actor_id, "updated_at": now}},
                upsert=True,
                session=session,
            )
            await self.db.activity.insert_one(
                {
                    "_id": event_id,
                    "user_id": actor_id,
                    "kind": "spawn_weight_changed",
                    "rarity": rarity,
                    "old_weight": previous,
                    "new_weight": weight,
                    "created_at": now,
                },
                session=session,
            )
            return previous, weight

        try:
            return await transaction(self.client, save)
        except DuplicateKeyError:
            # The surrounding transaction rolls back the weight write. Another
            # writer or a replay may have won; inspect the saved configuration.
            raise SpawnWeightConflict(
                "Spawn-weight update conflicts with an existing event"
            ) from None
