"""Trade rules; the repository makes acceptance an all-or-nothing transaction."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, cast


class TradeConflict(ValueError):
    pass


class TradeForbidden(PermissionError):
    pass


class TradeNotFound(LookupError):
    pass


class TradingService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def propose(
        self, sender: int, receiver: int, offer: str, request: str, now: datetime
    ) -> dict[str, Any]:
        if sender <= 0 or receiver <= 0 or sender == receiver or receiver > 2**53 - 1:
            raise TradeConflict("Choose another player")
        offer, request = offer.strip().lower(), request.strip().lower()
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item) for item in (offer, request)):
            raise TradeConflict("Choose valid character IDs")
        if offer == request:
            raise TradeConflict("Choose two different characters")
        return cast(
            dict[str, Any], await self.repository.create(sender, receiver, offer, request, now)
        )

    async def confirm(self, trade_id: str, actor_id: int, now: datetime) -> dict[str, Any]:
        return cast(dict[str, Any], await self.repository.confirm(trade_id, actor_id, now))
