"""Repository-level trade assertions with a fake transaction (not a replica set)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon.repositories.trades import TradeRepository
from summon.services.trading import TradeConflict, TradeForbidden


@pytest.fixture
def repository(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.trades.transaction", fake_transaction)
    db = MagicMock()
    db.trades.find_one = AsyncMock()
    db.trades.update_one = AsyncMock(return_value=SimpleNamespace(modified_count=1))
    db.inventory.find_one_and_update = AsyncMock()
    db.inventory.update_one = AsyncMock()
    db.activity.insert_one = AsyncMock()
    return TradeRepository(object(), db)


def pending(now):
    return {
        "_id": "a" * 32,
        "sender_id": 1,
        "receiver_id": 2,
        "offer_id": "aiko",
        "request_id": "ren",
        "status": "pending",
        "created_at": now,
        "expires_at": now + timedelta(minutes=15),
    }


async def test_trade_confirmation_conditions_each_debit_and_records_history(repository):
    now = datetime.now(UTC)
    repository.db.trades.find_one.return_value = pending(now)
    repository.db.inventory.find_one_and_update.side_effect = [{"quantity": 0}, {"quantity": 2}]
    result = await repository.confirm("a" * 32, 2, now)
    assert result["status"] == "completed"
    debit_calls = repository.db.inventory.find_one_and_update.await_args_list
    assert [call.args[0] for call in debit_calls] == [
        {"user_id": 1, "character_id": "aiko", "quantity": {"$gt": 0}},
        {"user_id": 2, "character_id": "ren", "quantity": {"$gt": 0}},
    ]
    assert all(call.kwargs["session"] == "session" for call in debit_calls)
    credits = repository.db.inventory.update_one.await_args_list
    assert credits[1].args[0] == {"user_id": 1, "character_id": "ren"}
    assert credits[2].args[0] == {"user_id": 2, "character_id": "aiko"}
    assert all(call.kwargs["session"] == "session" for call in credits)
    assert repository.db.trades.update_one.await_args.args[0]["status"] == "pending"
    assert repository.db.activity.insert_one.await_args.args[0]["kind"] == "trade"


async def test_trade_confirmation_rejects_wrong_actor_and_expiry_before_mutation(repository):
    now = datetime.now(UTC)
    repository.db.trades.find_one.return_value = pending(now)
    with pytest.raises(TradeForbidden):
        await repository.confirm("a" * 32, 3, now)
    with pytest.raises(TradeConflict):
        await repository.confirm("a" * 32, 2, now + timedelta(minutes=16))
    repository.db.inventory.find_one_and_update.assert_not_awaited()
    repository.db.activity.insert_one.assert_not_awaited()


async def test_trade_aborts_if_second_character_is_no_longer_owned(repository):
    now = datetime.now(UTC)
    repository.db.trades.find_one.return_value = pending(now)
    repository.db.inventory.find_one_and_update.side_effect = [{"quantity": 1}, None]
    with pytest.raises(TradeConflict):
        await repository.confirm("a" * 32, 2, now)
    repository.db.trades.update_one.assert_not_awaited()
    repository.db.activity.insert_one.assert_not_awaited()
    # This fake does not prove rollback; the real transaction must be tested on MongoDB.


async def test_proposal_does_not_probe_recipient_private_inventory(repository):
    now = datetime.now(UTC)
    repository.db.inventory.find_one = AsyncMock(return_value={"quantity": 1})
    repository.db.characters.find_one = AsyncMock(return_value={"_id": "ren"})
    repository.db.trades.insert_one = AsyncMock()
    result = await repository.create(1, 2, "aiko", "ren", now)
    assert result["status"] == "pending"
    assert repository.db.inventory.find_one.await_count == 1
    assert repository.db.inventory.find_one.await_args.args[0]["user_id"] == 1
    assert repository.db.characters.find_one.await_args.args[0]["_id"] == "ren"
