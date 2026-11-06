from unittest.mock import AsyncMock, MagicMock

import pytest

from summon.database import COLLECTION_SCHEMAS, INDEXES, initialize_database


def test_unique_inventory_and_ttl_indexes_are_declared():
    inventory = INDEXES["inventory"]
    assert any(
        keys == [("user_id", 1), ("character_id", 1)] and options.get("unique")
        for keys, options in inventory
    )
    assert any(options.get("expireAfterSeconds") == 0 for _, options in INDEXES["rate_limits"])
    assert set(COLLECTION_SCHEMAS) >= {"characters", "inventory", "groups", "trades"}


async def test_initialization_creates_collections_with_validation_and_indexes():
    database = MagicMock()
    database.list_collection_names = AsyncMock(return_value=[])
    database.create_collection = AsyncMock()
    collections = {}
    for name in INDEXES:
        collection = MagicMock()
        collection.create_index = AsyncMock()
        collections[name] = collection
    database.__getitem__.side_effect = lambda key: collections[key]
    await initialize_database(database)
    assert database.create_collection.await_count == len(COLLECTION_SCHEMAS)
    assert collections["inventory"].create_index.await_count == len(INDEXES["inventory"])


async def test_existing_collections_get_validators_updated():
    database = MagicMock()
    database.list_collection_names = AsyncMock(return_value=list(COLLECTION_SCHEMAS))
    database.command = AsyncMock()
    database.__getitem__.return_value.create_index = AsyncMock()
    await initialize_database(database)
    assert database.command.await_count == len(COLLECTION_SCHEMAS)


async def test_database_initialization_does_not_suppress_failures():
    database = MagicMock()
    database.list_collection_names = AsyncMock(side_effect=RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError, match="unavailable"):
        await initialize_database(database)
