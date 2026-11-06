from datetime import UTC, datetime, timedelta

import pytest

from summon.services.trading import TradeConflict, TradeForbidden, TradingService


class FakeTrades:
    def __init__(self):
        self.trades = {}
        self.inventory = {(1, "aiko"): 1, (2, "ren"): 1}

    async def create(self, sender, receiver, offer, request, now):
        if not self.inventory.get((sender, offer)):
            raise TradeConflict("Offered character is no longer owned")
        trade = {
            "id": "one",
            "sender_id": sender,
            "receiver_id": receiver,
            "offer_id": offer,
            "request_id": request,
            "status": "pending",
            "expires_at": now + timedelta(minutes=15),
        }
        self.trades["one"] = trade
        return trade

    async def confirm(self, trade_id, actor_id, now):
        trade = self.trades[trade_id]
        if actor_id != trade["receiver_id"]:
            raise TradeForbidden("Only the recipient can accept")
        if trade["status"] != "pending" or trade["expires_at"] <= now:
            raise TradeConflict("Trade expired or completed")
        a, b = (trade["sender_id"], trade["offer_id"]), (trade["receiver_id"], trade["request_id"])
        if not self.inventory.get(a) or not self.inventory.get(b):
            raise TradeConflict("Character is no longer owned")
        self.inventory[a] -= 1
        self.inventory[b] -= 1
        self.inventory[(a[0], b[1])] = self.inventory.get((a[0], b[1]), 0) + 1
        self.inventory[(b[0], a[1])] = self.inventory.get((b[0], a[1]), 0) + 1
        trade["status"] = "completed"
        return trade


async def test_trade_requires_two_people_and_two_different_characters():
    service = TradingService(FakeTrades())
    now = datetime.now(UTC)
    with pytest.raises(TradeConflict):
        await service.propose(1, 1, "aiko", "ren", now)
    with pytest.raises(TradeConflict):
        await service.propose(1, 2, "aiko", "aiko", now)


async def test_only_recipient_can_accept_and_trade_cannot_replay():
    repo = FakeTrades()
    service = TradingService(repo)
    now = datetime.now(UTC)
    await service.propose(1, 2, "aiko", "ren", now)
    with pytest.raises(TradeForbidden):
        await service.confirm("one", 3, now)
    await service.confirm("one", 2, now)
    assert repo.inventory[(1, "ren")] == 1
    assert repo.inventory[(2, "aiko")] == 1
    with pytest.raises(TradeConflict):
        await service.confirm("one", 2, now)


async def test_expired_or_missing_ownership_never_swaps():
    repo = FakeTrades()
    service = TradingService(repo)
    now = datetime.now(UTC)
    await service.propose(1, 2, "aiko", "ren", now)
    with pytest.raises(TradeConflict):
        await service.confirm("one", 2, now + timedelta(minutes=16))
    repo.inventory[(1, "aiko")] = 0
    with pytest.raises(TradeConflict):
        await service.confirm("one", 2, now)
    assert repo.inventory[(2, "ren")] == 1


async def test_trade_rejects_invalid_ids_and_normalizes_character_ids():
    now = datetime.now(UTC)
    repo = FakeTrades()
    service = TradingService(repo)
    for sender, receiver, offer, request in [
        (0, 2, "aiko", "ren"),
        (1, 2**53, "aiko", "ren"),
        (1, 2, "$bad", "ren"),
    ]:
        with pytest.raises(TradeConflict):
            await service.propose(sender, receiver, offer, request, now)
    trade = await service.propose(1, 2, " AIKO ", " REN ", now)
    assert trade["offer_id"] == "aiko"
    assert trade["request_id"] == "ren"
