from datetime import UTC, datetime, timedelta

import pytest

from summon.services.gifting import GiftConflict, GiftForbidden, GiftService


class FakeGifts:
    def __init__(self):
        self.inventory = {(1, "aiko"): 1}
        self.gifts = {}

    async def create(self, sender, receiver, character, now):
        if self.inventory.get((sender, character), 0) < 1:
            raise GiftConflict("Character is no longer owned")
        gift = {
            "id": "gift",
            "sender_id": sender,
            "receiver_id": receiver,
            "character_id": character,
            "status": "pending",
            "expires_at": now + timedelta(minutes=15),
        }
        self.gifts["gift"] = gift
        return gift

    async def confirm(self, gift_id, actor, now):
        gift = self.gifts[gift_id]
        if actor != gift["receiver_id"]:
            raise GiftForbidden("Only the recipient can accept")
        if gift["status"] != "pending" or gift["expires_at"] <= now:
            raise GiftConflict("Gift is no longer pending")
        source = (gift["sender_id"], gift["character_id"])
        if self.inventory.get(source, 0) < 1:
            raise GiftConflict("Character is no longer owned")
        destination = (gift["receiver_id"], gift["character_id"])
        self.inventory[source] -= 1
        self.inventory[destination] = self.inventory.get(destination, 0) + 1
        gift["status"] = "completed"
        return gift


async def test_gift_requires_distinct_real_players_and_valid_character_id():
    service = GiftService(FakeGifts())
    now = datetime.now(UTC)
    for sender, receiver, character in [(1, 1, "aiko"), (1, 0, "aiko"), (1, 2, "$invalid")]:
        with pytest.raises(GiftConflict):
            await service.propose(sender, receiver, character, now)


async def test_gift_requires_recipient_confirmation_and_cannot_replay():
    repo = FakeGifts()
    service = GiftService(repo)
    now = datetime.now(UTC)
    await service.propose(1, 2, "aiko", now)
    assert repo.inventory[(1, "aiko")] == 1
    with pytest.raises(GiftForbidden):
        await service.confirm("gift", 1, now)
    await service.confirm("gift", 2, now)
    assert repo.inventory[(1, "aiko")] == 0
    assert repo.inventory[(2, "aiko")] == 1
    with pytest.raises(GiftConflict):
        await service.confirm("gift", 2, now)


async def test_gift_expiry_or_lost_ownership_never_transfers():
    repo = FakeGifts()
    service = GiftService(repo)
    now = datetime.now(UTC)
    await service.propose(1, 2, "aiko", now)
    with pytest.raises(GiftConflict):
        await service.confirm("gift", 2, now + timedelta(minutes=16))
    repo.inventory[(1, "aiko")] = 0
    with pytest.raises(GiftConflict):
        await service.confirm("gift", 2, now)
    assert (2, "aiko") not in repo.inventory
