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
