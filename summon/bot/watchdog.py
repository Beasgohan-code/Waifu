"""Observe MTProto responsiveness; this is not proof of update delivery."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from wzgram import Client, raw

from summon.bot.ingress import IngressLease

logger = logging.getLogger(__name__)


class TelegramConnectionStalled(RuntimeError):
    """Telegram stopped answering the native Wzgram connection check."""


async def monitor_connection(
    client: Client,
    lease: IngressLease,
    *,
    timeout_seconds: float,
    health_repository: Any | None = None,
    interval_seconds: float = 30,
) -> None:
    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("Connection monitoring intervals must be positive")
    last_success = asyncio.get_running_loop().time()
    while True:
        if not lease.valid():
            raise TelegramConnectionStalled("Update-worker coordination is no longer valid")
        try:
            if not client.is_connected or not client.is_initialized:
                raise ConnectionError("Wzgram client is not connected")
            async with asyncio.timeout(15):
                # A successful GetState proves an RPC response, not that updates
                # were delivered, dispatched, or processed successfully.
                await client.invoke(raw.functions.updates.GetState())
        except Exception as exc:
            logger.warning("Telegram connection check failed (%s)", type(exc).__name__)
            if asyncio.get_running_loop().time() - last_success >= timeout_seconds:
                raise TelegramConnectionStalled(
                    "Telegram stopped answering connection checks"
                ) from exc
        else:
            last_success = asyncio.get_running_loop().time()
            if health_repository is not None and lease.valid():
                try:
                    async with asyncio.timeout(3):
                        await health_repository.record(datetime.now(UTC))
                except Exception as exc:
                    logger.warning("Connection health publication failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval_seconds)
