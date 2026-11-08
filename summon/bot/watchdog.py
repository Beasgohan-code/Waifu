"""Supervise the sole aiogram poller without creating another update consumer.

A successful getUpdates response (including an empty long poll) is the progress
signal. This does not establish handler, MongoDB, or end-to-end delivery health.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from aiogram import Bot, Dispatcher
from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
from aiogram.methods import GetUpdates, Response, TelegramMethod
from aiogram.methods.base import TelegramType

logger = logging.getLogger(__name__)


class PollingStalled(RuntimeError):
    """No successful Telegram long poll was observed within the configured window."""


class PollingHeartbeat:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        on_success: Callable[[], None] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Polling watchdog timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.on_success = on_success
        self.last_success = clock()

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        response = await make_request(bot, method)
        if isinstance(method, GetUpdates):
            # An empty successful long poll is healthy in a quiet group.
            self.last_success = self.clock()
            if self.on_success is not None:
                try:
                    self.on_success()
                except Exception as exc:
                    # Monitoring must never turn a successful fetch into a retry.
                    logger.warning("Polling health observation failed (%s)", type(exc).__name__)
        return response

    async def wait_until_stale(self) -> None:
        while True:
            remaining = self.timeout_seconds - (self.clock() - self.last_success)
            if remaining <= 0:
                return
            # Re-evaluate at the deadline: another poll may have completed meanwhile.
            await asyncio.sleep(remaining)


async def _stop_polling(
    dispatcher: Dispatcher, polling: asyncio.Task[None], shutdown_timeout: float
) -> None:
    if polling.done():
        return
    try:
        # aiogram's normal stop cancels its own internal getUpdates task and
        # runs shutdown callbacks; cancelling start_polling directly can leak it.
        await asyncio.wait_for(dispatcher.stop_polling(), timeout=shutdown_timeout)
    except (TimeoutError, RuntimeError):
        logger.error("Could not stop polling gracefully; cancelling the polling task")
    if not polling.done():
        polling.cancel()
    await asyncio.gather(polling, return_exceptions=True)


async def run_supervised_polling(
    dispatcher: Dispatcher,
    bot: Bot,
    *,
    timeout_seconds: float,
    shutdown_timeout: float = 15,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Run exactly one poller; stop and fail on sustained getUpdates failure.

    A process manager is responsible for restarting the process after failure.
    This coroutine never starts a replacement or coordinates other processes.
    """
    if shutdown_timeout <= 0:
        raise ValueError("Polling shutdown timeout must be positive")
    heartbeat = PollingHeartbeat(timeout_seconds=timeout_seconds, on_success=on_success)
    bot.session.middleware.register(heartbeat)
    polling: asyncio.Task[None] | None = None
    monitor: asyncio.Task[None] | None = None
    try:
        polling = asyncio.create_task(
            dispatcher.start_polling(
                bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False
            )
        )
        monitor = asyncio.create_task(heartbeat.wait_until_stale())
        done, _pending = await asyncio.wait({polling, monitor}, return_when=asyncio.FIRST_COMPLETED)
        if polling in done:
            # Propagate a real polling error; normal SIGTERM remains a normal exit.
            await polling
            return
        await monitor
        logger.error(
            "No successful Telegram getUpdates for %.0f seconds; stopping the bot process",
            timeout_seconds,
        )
        await _stop_polling(dispatcher, polling, shutdown_timeout)
        raise PollingStalled("Telegram getUpdates made no progress within the watchdog window")
    finally:
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        try:
            if polling is not None and not polling.done():
                await _stop_polling(dispatcher, polling, shutdown_timeout)
        finally:
            bot.session.middleware.unregister(heartbeat)
