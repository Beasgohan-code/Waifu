"""Dependency container shared by the API and bot runtimes."""

from __future__ import annotations

from typing import Any

from summon.repositories.auth import AuthRepository
from summon.repositories.catalog import CatalogRepository
from summon.repositories.collection import CollectionRepository
from summon.repositories.gifts import GiftRepository
from summon.repositories.group_notes import GroupNoteRepository
from summon.repositories.groups import GroupRepository
from summon.repositories.limits import RateLimiter
from summon.repositories.sessions import SessionRepository
from summon.repositories.trades import TradeRepository
from summon.repositories.users import UserRepository


class Store:
    def __init__(self, client: Any, database: Any) -> None:
        self.auth = AuthRepository(database)
        self.catalog = CatalogRepository(database, client=client)
        self.collection = CollectionRepository(client, database)
        self.groups = GroupRepository(client, database)
        self.notes = GroupNoteRepository(client, database)
        self.gifts = GiftRepository(client, database)
        self.limiter = RateLimiter(database)
        self.sessions = SessionRepository(database)
        self.trades = TradeRepository(client, database)
        self.users = UserRepository(client, database)
