from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, Mock

import pytest
from pymongo.errors import DuplicateKeyError

from summon.database import COLLECTION_SCHEMAS
from summon.models import CharacterInput, Rarity
from summon.rarities import (
    MAX_SPAWN_WEIGHT,
    RARITIES,
    RARITY_VALUES,
    RARITY_WEIGHTS,
    parse_rarity,
    rarity_label,
)
from summon.repositories.rarity_weights import (
    InvalidSpawnWeights,
    SpawnWeightConflict,
    SpawnWeightsRepository,
)


def test_reference_tiers_and_legacy_value_are_consistent_across_validation_and_schema():
    assert len(RARITIES) == 18
    assert [tier.id for tier in RARITIES] == list(range(1, 19))
    assert tuple(get_args(Rarity)) == RARITY_VALUES
    enum = COLLECTION_SCHEMAS["characters"]["properties"]["rarity"]["enum"]
    assert set(enum) == set(RARITY_VALUES)
    assert set(RARITY_VALUES) == {tier.value for tier in RARITIES} | {"epic"}
    for tier in RARITIES:
        assert parse_rarity(str(tier.id)) == tier.value
        assert parse_rarity(tier.label) == tier.value
        assert rarity_label(tier.value) == tier.label
        assert (
            CharacterInput(
                id="test",
                name="Test",
                category="Series",
                rarity=tier.value,
                image_url="https://example.com/test.jpg",
            ).rarity
            == tier.value
        )
    assert parse_rarity("Epic") == "epic"
    assert parse_rarity("19") is None


def test_amv_database_initialization_weight_is_recorded():
    assert RARITIES[13].value == "amv-edition"
    assert RARITIES[13].weight == 10


def fake_database(rows=()):
    cursor = SimpleNamespace(to_list=AsyncMock(return_value=list(rows)))
    collection = SimpleNamespace(
        find=lambda _query, _projection: cursor,
        find_one=AsyncMock(return_value=None),
        update_one=AsyncMock(),
    )
    collection.with_options = Mock(return_value=collection)
    return SimpleNamespace(
        spawn_weights=collection, activity=SimpleNamespace(insert_one=AsyncMock())
    )


def test_schema_bounds_every_reference_bucket_and_rejects_extra_fields():
    schema = COLLECTION_SCHEMAS["spawn_weights"]
    assert set(schema["properties"]["_id"]["enum"]) == set(RARITY_WEIGHTS)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["weight"]["maximum"] == MAX_SPAWN_WEIGHT


async def test_missing_overrides_use_defaults_and_valid_rows_replace_only_their_bucket():
    db = fake_database([{"_id": "rare", "weight": 17}])
    repo = SpawnWeightsRepository(db)
    weights = await repo.read()
    assert db.spawn_weights.with_options.call_args.kwargs["read_concern"].document == {
        "level": "majority"
    }
    assert weights["rare"] == 17
    assert weights["common"] == RARITY_WEIGHTS["common"]
    assert len(weights) == 18


@pytest.mark.parametrize("value", [-1, 1_000_001, True, 1.2, "7", None])
async def test_corrupt_stored_weight_never_silently_changes_odds(value):
    repo = SpawnWeightsRepository(fake_database([{"_id": "rare", "weight": value}]))
    with pytest.raises(InvalidSpawnWeights):
        await repo.read()


async def test_duplicate_or_unexpected_rows_fail_closed():
    for rows in (
        [{"_id": "rare", "weight": 1}, {"_id": "rare", "weight": 2}],
        [{"_id": "epic", "weight": 1}],
    ):
        with pytest.raises(InvalidSpawnWeights):
            await SpawnWeightsRepository(fake_database(rows)).read()


async def test_weight_and_actor_audit_share_one_transaction(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.rarity_weights.transaction", fake_transaction)
    db = fake_database()
    repo = SpawnWeightsRepository(db, client=object())
    assert await repo.set_weight("rare", 99, actor_id=42, event_id="event-1") == (2500, 99)
    update = db.spawn_weights.update_one.await_args
    audit = db.activity.insert_one.await_args
    assert update.kwargs["session"] == audit.kwargs["session"] == "session"
    assert update.args[0] == {"_id": "rare"}
    assert update.args[1]["$set"]["updated_by"] == 42
    assert update.args[1]["$set"]["weight"] == 99
    assert audit.args[0]["_id"] == "event-1"
    assert audit.args[0]["user_id"] == 42
    assert audit.args[0]["old_weight"] == 2500
    assert audit.args[0]["new_weight"] == 99


@pytest.mark.parametrize(
    "rarity,weight,actor,event",
    [
        ("epic", 1, 42, "event"),
        ("rare", -1, 42, "event"),
        ("rare", 1_000_001, 42, "event"),
        ("rare", True, 42, "event"),
        ("rare", 1, 0, "event"),
        ("rare", 1, True, "event"),
        ("rare", 1, 42, ""),
    ],
)
async def test_invalid_weight_changes_do_not_write(rarity, weight, actor, event):
    db = fake_database()
    repo = SpawnWeightsRepository(db, client=object())
    with pytest.raises(ValueError):
        await repo.set_weight(rarity, weight, actor_id=actor, event_id=event)
    db.spawn_weights.update_one.assert_not_awaited()
    db.activity.insert_one.assert_not_awaited()


async def test_replayed_event_surfaces_conflict_without_claiming_success(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.rarity_weights.transaction", fake_transaction)
    db = fake_database()
    db.activity.insert_one.side_effect = DuplicateKeyError("private database detail")
    repo = SpawnWeightsRepository(db, client=object())
    with pytest.raises(SpawnWeightConflict) as error:
        await repo.set_weight("rare", 1, actor_id=42, event_id="replay")
    assert "private database detail" not in str(error.value)


async def test_missing_transaction_client_fails_before_writes():
    db = fake_database()
    with pytest.raises(RuntimeError):
        await SpawnWeightsRepository(db).set_weight("rare", 1, actor_id=42, event_id="event")
    db.spawn_weights.update_one.assert_not_awaited()
