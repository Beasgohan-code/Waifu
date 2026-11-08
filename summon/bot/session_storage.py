"""Native Wzgram storage for isolated bot sessions and shared known peers.

The worker persists its Wzgram session and peer cache in a dedicated MongoDB
DATABASE. API clients use independent, in-memory MTProto sessions and consult
only the worker's peer cache on a cache miss; they never share its auth key.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from wzgram import Client, raw
from wzgram.errors import PeerIdInvalid
from wzgram.storage.mongo_storage import MongoStorage
from wzgram.storage.sqlite_storage import SQLiteStorage

logger = logging.getLogger(__name__)


def bot_id_from_token(token: str) -> int:
    prefix, separator, suffix = token.partition(":")
    if (
        not separator
        or not suffix
        or not prefix.isascii()
        or not prefix.isdecimal()
        or int(prefix) <= 0
    ):
        raise RuntimeError("SUMMON_BOT_TOKEN must be a valid bot token")
    return int(prefix)


class APIStorage(SQLiteStorage):
    """An in-memory Wzgram session which can reuse known bot-account peers."""

    def __init__(self, database: Any) -> None:
        super().__init__("summon-api", workdir=Path.cwd(), in_memory=True)
        self._shared_peers = database["peers"]

    async def get_peer_by_id(self, peer_id: int) -> Any:
        try:
            return await super().get_peer_by_id(peer_id)
        except KeyError:
            if not isinstance(peer_id, int) or isinstance(peer_id, bool):
                raise
            row = await self._shared_peers.find_one({"_id": peer_id}, {"access_hash": 1, "type": 1})
            if not row:
                # Do not let resolve_peer guess an access hash of zero for an
                # unknown numeric user/channel in a separate MTProto session.
                raise PeerIdInvalid from None
            peer_type = row.get("type")
            access_hash = row.get("access_hash")
            if peer_type not in {
                "user",
                "bot",
                "group",
                "direct",
                "channel",
                "forum",
                "supergroup",
            }:
                raise PeerIdInvalid from None
            if peer_type != "group" and not isinstance(access_hash, int):
                raise PeerIdInvalid from None
            # No phone number or auth key is copied from the persistent store.
            await self.update_peers([(peer_id, access_hash or 0, peer_type, None)])
            return await super().get_peer_by_id(peer_id)


def worker_storage(mongo_client: Any, database_name: str) -> MongoStorage:
    return MongoStorage("summon-bot", mongo_client, database=database_name)


async def start_verified_client(client: Client, token: str, expected_bot_id: int) -> None:
    """Start native Wzgram without dispatching updates under the wrong account."""
    try:
        authorized = await client.connect()
        if not authorized:
            await client.sign_in_bot(token)
        if not await client.storage.is_bot():
            raise RuntimeError("The stored Wzgram session is not a bot session")
        client.me = await client.get_me()
        if client.me is None or not client.me.is_bot or client.me.id != expected_bot_id:
            raise RuntimeError("The stored Wzgram session belongs to a different bot")
        await client.invoke(raw.functions.updates.GetState())
        await client.initialize()
    except BaseException:
        # Includes cancellation and a failure after storage opened but before
        # Wzgram marked the client connected.
        try:
            await client.stop()
        except Exception as exc:
            logger.warning("Could not stop Wzgram (%s)", type(exc).__name__)
        try:
            await client.storage.close()
        except Exception as exc:
            logger.warning("Could not close Wzgram storage (%s)", type(exc).__name__)
        raise
