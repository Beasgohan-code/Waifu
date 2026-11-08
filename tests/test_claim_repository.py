"""Claim transaction shape; real MongoDB race/rollback remains an integration test."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon.repositories.collection import CollectionRepository


@pytest.fixture
def repository(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.collection.transaction", fake_transaction)
    db = MagicMock()
    db.groups.find_one_and_update = AsyncMock()
    db.characters.find_one = AsyncMock()
    db.inventory.update_one = AsyncMock()
    db.users.update_one = AsyncMock()
    db.spawns.update_one = AsyncMock(return_value=SimpleNamespace(modified_count=1))
    db.reward_history.insert_one = AsyncMock()
    db.activity.insert_one = AsyncMock()
    return CollectionRepository(object(), db)


async def test_claim_conditions_active_answer_and_records_all_writes_in_session(repository):
    now = datetime.now(UTC)
    repository.db.groups.find_one_and_update.return_value = {
        "active_spawn": {
            "id": "spawn-1",
            "character_id": "aiko",
            "expires_at": now + timedelta(minutes=1),
        }
    }
    repository.db.characters.find_one.return_value = {"_id": "aiko", "name": "Aiko"}
    result = await repository.claim_spawn(-100, {"id": 42, "first_name": "Mika"}, "aiko", now)
    assert result["_id"] == "aiko"
    condition = repository.db.groups.find_one_and_update.await_args.args[0]
    assert condition["active_spawn.answer"] == "aiko"
    assert condition["active_spawn.expires_at"] == {"$gt": now}
    assert repository.db.inventory.update_one.await_args.args[1]["$inc"] == {"quantity": 1}
    assert repository.db.reward_history.insert_one.await_args.args[0]["_id"] == "catch:spawn-1"
    assert repository.db.activity.insert_one.await_args.args[0]["_id"] == "catch:spawn-1"
    for call in [
        repository.db.groups.find_one_and_update,
        repository.db.characters.find_one,
        repository.db.inventory.update_one,
        repository.db.users.update_one,
        repository.db.spawns.update_one,
        repository.db.reward_history.insert_one,
        repository.db.activity.insert_one,
    ]:
        assert call.await_args.kwargs["session"] == "session"


async def test_claim_missing_or_wrong_answer_never_awards(repository):
    repository.db.groups.find_one_and_update.return_value = None
    assert (
        await repository.claim_spawn(
            -100, {"id": 42, "first_name": "Mika"}, "wrong", datetime.now(UTC)
        )
        is None
    )
    repository.db.inventory.update_one.assert_not_awaited()
    repository.db.reward_history.insert_one.assert_not_awaited()
