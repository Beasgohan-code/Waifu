from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon.repositories.gifts import GiftRepository
from summon.services.gifting import GiftConflict, GiftForbidden


@pytest.fixture
def repository(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.gifts.transaction", fake_transaction)
    db = MagicMock()
    db.gifts.find_one = AsyncMock()
    db.gifts.update_one = AsyncMock(return_value=SimpleNamespace(modified_count=1))
    db.inventory.find_one_and_update = AsyncMock()
    db.inventory.update_one = AsyncMock()
    db.activity.insert_one = AsyncMock()
    return GiftRepository(object(), db)


def pending(now):
    return {
        "_id": "a" * 32,
        "sender_id": 1,
        "receiver_id": 2,
        "character_id": "aiko",
        "status": "pending",
        "created_at": now,
        "expires_at": now + timedelta(minutes=15),
    }


async def test_confirm_decrements_and_increments_only_inside_transaction(repository):
    now = datetime.now(UTC)
    repository.db.gifts.find_one.return_value = pending(now)
    repository.db.inventory.find_one_and_update.return_value = {"quantity": 0}
    result = await repository.confirm("a" * 32, 2, now)
    assert result["status"] == "completed"
    assert repository.db.inventory.find_one_and_update.await_args.kwargs["session"] == "session"
    assert repository.db.inventory.update_one.await_count == 2  # clear favorite, credit recipient
    destination = repository.db.inventory.update_one.await_args_list[1]
    assert destination.args[0] == {"user_id": 2, "character_id": "aiko"}
    assert destination.kwargs["session"] == "session"
    assert repository.db.gifts.update_one.await_args.args[0]["status"] == "pending"
    assert repository.db.activity.insert_one.await_args.args[0]["kind"] == "gift"


async def test_confirm_rejects_wrong_recipient_and_expired_gift_before_writing(repository):
    now = datetime.now(UTC)
    repository.db.gifts.find_one.return_value = pending(now)
    with pytest.raises(GiftForbidden):
        await repository.confirm("a" * 32, 3, now)
    with pytest.raises(GiftConflict):
        await repository.confirm("a" * 32, 2, now + timedelta(minutes=16))
    repository.db.inventory.find_one_and_update.assert_not_awaited()
    repository.db.activity.insert_one.assert_not_awaited()


async def test_confirm_rechecks_current_ownership(repository):
    now = datetime.now(UTC)
    repository.db.gifts.find_one.return_value = pending(now)
    repository.db.inventory.find_one_and_update.return_value = None
    with pytest.raises(GiftConflict):
        await repository.confirm("a" * 32, 2, now)
    repository.db.inventory.update_one.assert_not_awaited()
    repository.db.gifts.update_one.assert_not_awaited()
