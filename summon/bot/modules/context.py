"""Dependencies and shared private-read safeguards for collector modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pymongo.errors import PyMongoError
from wzgram import Client
from wzgram.types import Message

from summon.api.context import Store
from summon.bot.presentation import Card
from summon.config import Settings
from summon.services.collection import CollectionService


class Responder(Protocol):
    async def __call__(
        self,
        message: Message,
        card: Card,
        *,
        markup: Any | None = None,
        private: bool = False,
        standard: bool = False,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class CollectorContext:
    """One client and shared services; modules must not create update consumers."""

    app: Client
    store: Store
    settings: Settings
    collection: CollectionService
    respond: Responder

    async def rate_allowed(self, message: Message, actor_id: int, action: str) -> bool:
        try:
            allowed = await self.store.limiter.allow(
                f"user:{actor_id}:collector:{action}", limit=30, seconds=60
            )
        except (PyMongoError, OSError, TimeoutError):
            await self.respond(
                message,
                Card("Collector tools unavailable", "Please try again later."),
                private=True,
            )
            return False
        if not allowed:
            await self.respond(
                message,
                Card("Please slow down", "Too many requests; try again shortly."),
                private=True,
            )
        return bool(allowed)

    async def read_error(self, message: Message) -> None:
        await self.respond(
            message,
            Card("Collector tools unavailable", "Could not read your results; try again later."),
            private=True,
        )
