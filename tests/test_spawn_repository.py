"""Failed-delivery writes are conditioned and belong to one Mongo transaction."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from summon.repositories.collection import CollectionRepository


@pytest.fixture
def repo(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.collection.transaction", fake_transaction)
    groups = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
    spawns = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
    return CollectionRepository(object(), SimpleNamespace(groups=groups, spawns=spawns))


async def test_failed_delivery_clears_only_matching_active_spawn_and_records_history(repo):
    await repo.mark_delivery_failed(-100, "spawn-one")
    group_call = repo.db.groups.update_one.await_args
    history_call = repo.db.spawns.update_one.await_args
    assert group_call.args[0] == {"_id": -100, "active_spawn.id": "spawn-one"}
    assert group_call.args[1] == {"$set": {"active_spawn": None}}
    assert history_call.args[0] == {"_id": "spawn-one", "status": "active"}
    assert history_call.args[1] == {"$set": {"status": "delivery_failed"}}
    assert group_call.kwargs["session"] == history_call.kwargs["session"] == "session"


async def test_stale_delivery_failure_does_not_change_another_or_claimed_spawn(repo):
    repo.db.groups.update_one.return_value = SimpleNamespace(modified_count=0)
    await repo.mark_delivery_failed(-100, "spawn-one")
    repo.db.spawns.update_one.assert_not_awaited()


async def test_missing_active_history_raises_to_abort_transaction(repo):
    repo.db.spawns.update_one.return_value = SimpleNamespace(modified_count=0)
    with pytest.raises(RuntimeError, match="history"):
        await repo.mark_delivery_failed(-100, "spawn-one")
