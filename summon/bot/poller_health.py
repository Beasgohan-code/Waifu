"""Publish only actual successful long-poll observations, without blocking polling."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class PollerHealthPublisher:
    def __init__(
        self,
        repository: Any,
        *,
        interval_seconds: float = 15,
        write_timeout: float = 3,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if interval_seconds <= 0 or write_timeout <= 0:
            raise ValueError("Polling health intervals must be positive")
        self._repository = repository
        self._interval = interval_seconds
        self._write_timeout = write_timeout
        self._now = now
        self._observed_at: datetime | None = None
        self._pending = asyncio.Event()

    def observe(self) -> None:
        # Called synchronously after getUpdates succeeds, including an empty
        # long poll. No Telegram payload, identity or update ID is retained.
        self._observed_at = self._now()
        self._pending.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_write = loop.time()
        while True:
            await self._pending.wait()
            remaining = next_write - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            # No await occurs between clearing and reading: any newer poll
            # observed during I/O will set the event again for the next write.
            self._pending.clear()
            observed_at = self._observed_at
            if observed_at is None:
                continue
            try:
                async with asyncio.timeout(self._write_timeout):
                    await self._repository.record(observed_at)
            except Exception as exc:
                # Never block or fail the update ingress on monitoring storage.
                logger.warning("Polling health publication failed (%s)", type(exc).__name__)
            next_write = loop.time() + self._interval
