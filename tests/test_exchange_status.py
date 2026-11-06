from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from summon.repositories.gifts import GiftRepository, public_gift
from summon.repositories.trades import TradeRepository, public_trade
from summon.services.gifting import GiftNotFound
from summon.services.trading import TradeNotFound


def test_exchange_history_reports_elapsed_pending_offers_as_expired():
    now = datetime(2026, 11, 2, tzinfo=UTC)
    base = {
        "_id": "a" * 32,
        "sender_id": 1,
        "receiver_id": 2,
        "status": "pending",
        "created_at": now - timedelta(hours=1),
        "expires_at": now,
    }
    trade = {**base, "offer_id": "aiko", "request_id": "ren"}
    gift = {**base, "character_id": "aiko"}
    assert public_trade(trade, now=now)["status"] == "expired"
    assert public_gift(gift, now=now)["status"] == "expired"
    assert (
        trade["status"] == gift["status"] == "pending"
    )  # view only; no destructive history update
    assert public_gift({**gift, "status": "completed"}, now=now)["status"] == "completed"


@pytest.mark.parametrize(
    ("repository", "not_found", "collection"),
    [(GiftRepository, GiftNotFound, "gifts"), (TradeRepository, TradeNotFound, "trades")],
)
async def test_cancel_does_not_relabel_expired_offers(repository, not_found, collection):
    now = datetime.now(UTC)
    records = SimpleNamespace(find_one_and_update=AsyncMock(return_value=None))
    repo = repository(object(), SimpleNamespace(**{collection: records}))
    with pytest.raises(not_found):
        await repo.cancel("a" * 32, 1, now)
    assert records.find_one_and_update.await_args.args[0]["expires_at"] == {"$gt": now}
