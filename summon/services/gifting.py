"""Gift rules; only the recipient can confirm a transfer."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, cast


class GiftConflict(ValueError):
    pass


class GiftForbidden(PermissionError):
    pass


class GiftNotFound(LookupError):
    pass


class GiftService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def propose(
        self, sender: int, receiver: int, character: str, now: datetime
    ) -> dict[str, Any]:
        if sender <= 0 or receiver <= 0 or sender == receiver or receiver > 2**53 - 1:
            raise GiftConflict("Choose another player")
        character = character.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", character):
            raise GiftConflict("Choose a valid character ID")
        return cast(dict[str, Any], await self.repository.create(sender, receiver, character, now))

    async def confirm(self, gift_id: str, actor_id: int, now: datetime) -> dict[str, Any]:
        return cast(dict[str, Any], await self.repository.confirm(gift_id, actor_id, now))
