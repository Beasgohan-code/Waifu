import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from summon.services.collection import CollectionService, GuessRateLimited


class FakeCatalog:
    async def choose(self):
        return {
            "_id": "mika",
            "name": "Mika 星",
            "category": "Sky",
            "rarity": "rare",
            "image_url": "/art/mika.webp",
        }


class FakeCollection:
    def __init__(self):
        self.group = {"settings": {"spawn_enabled": True, "spawn_every": 2}, "message_count": 0}
        self.spawn = None
        self.inventory = {}
        self.lock = asyncio.Lock()
        self.block_guess = False

    async def record_message(self, group_id, title, user_id, now):
        self.group["message_count"] += 1
        return self.group.copy()

    async def reserve_spawn(self, group_id, character, now):
        async with self.lock:
            if self.spawn or self.group["message_count"] < 2:
                return None
            self.spawn = {
                "id": "one",
                "character_id": character["_id"],
                "answer": "mika 星",
                "expires_at": now + timedelta(minutes=5),
            }
            self.group["message_count"] = 0
            return self.spawn

    async def allow_guess(self, group_id, user_id, now):
        return not self.block_guess

    async def claim_spawn(self, group_id, user, answer, now):
        async with self.lock:
            if not self.spawn or self.spawn["answer"] != answer or self.spawn["expires_at"] <= now:
                return None
            self.spawn = None
            self.inventory[user["id"]] = self.inventory.get(user["id"], 0) + 1
            return {"name": "Mika 星"}


@pytest.mark.asyncio
async def test_spawn_cadence_and_one_active_spawn():
    repo = FakeCollection()
    service = CollectionService(repo, FakeCatalog())
    now = datetime.now(UTC)
    assert await service.on_message(-100, "Club", 1, now) is None
    assert await service.on_message(-100, "Club", 2, now) is not None
    assert await service.on_message(-100, "Club", 3, now) is None


@pytest.mark.asyncio
async def test_only_one_concurrent_correct_guess_gets_character():
    repo = FakeCollection()
    service = CollectionService(repo, FakeCatalog())
    now = datetime.now(UTC)
    await service.on_message(-100, "Club", 1, now)
    await service.on_message(-100, "Club", 2, now)
    results = await asyncio.gather(
        *(
            service.guess(-100, {"id": i, "first_name": "User"}, " MIKA   星 ", now)
            for i in range(1, 11)
        )
    )
    assert sum(result is not None for result in results) == 1
    assert sum(repo.inventory.values()) == 1


@pytest.mark.asyncio
async def test_incorrect_and_expired_guesses_do_not_award():
    repo = FakeCollection()
    service = CollectionService(repo, FakeCatalog())
    now = datetime.now(UTC)
    await service.on_message(-100, "Club", 1, now)
    await service.on_message(-100, "Club", 2, now)
    assert await service.guess(-100, {"id": 1, "first_name": "A"}, "wrong", now) is None
    assert (
        await service.guess(
            -100, {"id": 1, "first_name": "A"}, "Mika 星", now + timedelta(minutes=6)
        )
        is None
    )
    assert repo.inventory == {}


@pytest.mark.asyncio
async def test_guess_cooldown_is_enforced_before_claim():
    repo = FakeCollection()
    repo.block_guess = True
    with pytest.raises(GuessRateLimited):
        await CollectionService(repo, FakeCatalog()).guess(
            -100, {"id": 1, "first_name": "A"}, "Mika", datetime.now(UTC)
        )


@pytest.mark.asyncio
async def test_manual_try_spawn_does_not_count_activity_and_only_one_can_reserve():
    repo = FakeCollection()
    service = CollectionService(repo, FakeCatalog())
    now = datetime.now(UTC)
    assert await service.try_spawn(-100, now) is None
    assert repo.group["message_count"] == 0
    repo.group["message_count"] = 2
    results = await asyncio.gather(*(service.try_spawn(-100, now) for _ in range(5)))
    assert sum(result is not None for result in results) == 1
    assert repo.group["message_count"] == 0


async def test_free_daily_character_selects_then_uses_authenticated_actor():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from summon.auth import TelegramUser

    user = TelegramUser(id=7, first_name="Collector")
    saved = {"id": "mika", "name": "Mika", "category": "Sky", "rarity": "rare"}
    repo = SimpleNamespace(claim_daily_character=AsyncMock(return_value=saved))
    now = datetime.now(UTC)
    result = await CollectionService(repo, FakeCatalog()).claim_daily_character(user, now)
    assert result == saved
    repo.claim_daily_character.assert_awaited_once_with(user, "mika", now)


async def test_free_daily_character_with_empty_catalog_never_writes():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from summon.auth import TelegramUser
    from summon.repositories.collection import DailyCharacterUnavailable

    repo = SimpleNamespace(claim_daily_character=AsyncMock())
    catalog = SimpleNamespace(choose=AsyncMock(return_value=None))
    with pytest.raises(DailyCharacterUnavailable):
        await CollectionService(repo, catalog).claim_daily_character(
            TelegramUser(id=7, first_name="Collector"), datetime.now(UTC)
        )
    repo.claim_daily_character.assert_not_awaited()


def daily_character_database():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    return SimpleNamespace(
        characters=SimpleNamespace(
            find_one=AsyncMock(
                return_value={"_id": "mika", "name": "Mika", "category": "Sky", "rarity": "rare"}
            )
        ),
        users=SimpleNamespace(update_one=AsyncMock()),
        inventory=SimpleNamespace(update_one=AsyncMock()),
        reward_history=SimpleNamespace(insert_one=AsyncMock()),
        activity=SimpleNamespace(insert_one=AsyncMock()),
    )


async def test_free_daily_character_uses_one_transaction_and_a_utc_ledger(monkeypatch):
    from datetime import timezone

    from summon.auth import TelegramUser
    from summon.repositories.collection import CollectionRepository

    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.collection.transaction", fake_transaction)
    db = daily_character_database()
    repo = CollectionRepository(object(), db)
    user = TelegramUser(id=7, first_name="Collector")
    local = datetime(2026, 1, 2, 0, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    result = await repo.claim_daily_character(user, "mika", local)
    assert result == {"id": "mika", "name": "Mika", "category": "Sky", "rarity": "rare"}
    ledger = db.reward_history.insert_one.await_args
    assert ledger.args[0]["_id"] == "daily-character:7:2026-01-01"
    assert ledger.kwargs["session"] == "session"
    assert db.activity.insert_one.await_args.kwargs["session"] == "session"
    assert db.inventory.update_one.await_args.kwargs["session"] == "session"
    assert db.users.update_one.await_args.kwargs["session"] == "session"
    assert db.inventory.update_one.await_args.args[1]["$inc"] == {"quantity": 1}
    assert "$inc" not in db.users.update_one.await_args.args[1]


async def test_free_daily_character_duplicate_ledger_aborts_before_inventory(monkeypatch):
    from pymongo.errors import DuplicateKeyError

    from summon.auth import TelegramUser
    from summon.repositories.collection import CollectionRepository, DailyCharacterAlreadyClaimed

    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.collection.transaction", fake_transaction)
    db = daily_character_database()
    db.reward_history.insert_one.side_effect = DuplicateKeyError("already claimed")
    with pytest.raises(DailyCharacterAlreadyClaimed):
        await CollectionRepository(object(), db).claim_daily_character(
            TelegramUser(id=7, first_name="Collector"), "mika", datetime.now(UTC)
        )
    db.inventory.update_one.assert_not_awaited()
    db.users.update_one.assert_not_awaited()
    db.activity.insert_one.assert_not_awaited()


async def test_unavailable_or_naive_daily_character_claim_never_touches_ledger(monkeypatch):
    from summon.auth import TelegramUser
    from summon.repositories.collection import CollectionRepository, DailyCharacterUnavailable

    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.collection.transaction", fake_transaction)
    db = daily_character_database()
    repo = CollectionRepository(object(), db)
    user = TelegramUser(id=7, first_name="Collector")
    with pytest.raises(ValueError):
        await repo.claim_daily_character(user, "mika", datetime(2026, 1, 1))
    db.characters.find_one.return_value = None
    with pytest.raises(DailyCharacterUnavailable):
        await repo.claim_daily_character(user, "mika", datetime.now(UTC))
    db.reward_history.insert_one.assert_not_awaited()
    db.inventory.update_one.assert_not_awaited()
