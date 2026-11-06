"""Polling watchdog behavior without Telegram, MongoDB, or a second poller."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import GetMe, GetUpdates

from summon.bot.watchdog import PollingHeartbeat, PollingStalled, run_supervised_polling
from summon.config import Settings


async def test_heartbeat_counts_only_successful_get_updates_even_when_empty():
    now = [10.0]
    heartbeat = PollingHeartbeat(timeout_seconds=300, clock=lambda: now[0])
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    request = AsyncMock(return_value=[])

    now[0] = 30.0
    assert await heartbeat(request, bot, GetMe()) == []
    assert heartbeat.last_success == 10.0

    now[0] = 40.0
    assert await heartbeat(request, bot, GetUpdates(timeout=10)) == []
    assert heartbeat.last_success == 40.0

    now[0] = 60.0
    request.side_effect = OSError("synthetic failure")
    with pytest.raises(OSError):
        await heartbeat(request, bot, GetUpdates(timeout=10))
    assert heartbeat.last_success == 40.0
    await bot.session.close()


async def test_wait_until_stale_extends_deadline_after_successful_poll():
    heartbeat = PollingHeartbeat(timeout_seconds=0.2)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    request = AsyncMock(return_value=[])
    task = asyncio.create_task(heartbeat.wait_until_stale())
    await asyncio.sleep(0.1)
    await heartbeat(request, bot, GetUpdates(timeout=10))
    await asyncio.sleep(0.14)
    assert not task.done()
    await asyncio.wait_for(task, timeout=1)
    await bot.session.close()


class FakeDispatcher:
    def __init__(self, *, failure=None, block_stop=False):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.failure = failure
        self.block_stop = block_stop
        self.starts = 0
        self.stops = 0
        self.kwargs = None

    def resolve_used_update_types(self):
        return ["message"]

    async def start_polling(self, bot, **kwargs):
        self.starts += 1
        self.kwargs = kwargs
        self.started.set()
        if self.failure:
            raise self.failure
        await self.stopped.wait()

    async def stop_polling(self):
        self.stops += 1
        if self.block_stop:
            await asyncio.Event().wait()
        self.stopped.set()


async def test_stalled_polling_stops_once_then_exits_without_starting_another_poller():
    dispatcher = FakeDispatcher()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with pytest.raises(PollingStalled, match="getUpdates"):
        await asyncio.wait_for(
            run_supervised_polling(dispatcher, bot, timeout_seconds=0.05, shutdown_timeout=0.1),
            timeout=2,
        )
    assert dispatcher.starts == 1
    assert dispatcher.stops == 1
    assert dispatcher.kwargs == {"allowed_updates": ["message"], "close_bot_session": False}
    assert len(bot.session.middleware) == 0
    await bot.session.close()


async def test_graceful_polling_exit_cancels_watchdog_without_restarting():
    dispatcher = FakeDispatcher()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    task = asyncio.create_task(
        run_supervised_polling(dispatcher, bot, timeout_seconds=0.3, shutdown_timeout=0.1)
    )
    await dispatcher.started.wait()
    dispatcher.stopped.set()
    await asyncio.wait_for(task, timeout=1)
    assert dispatcher.starts == 1
    assert dispatcher.stops == 0
    assert len(bot.session.middleware) == 0
    await bot.session.close()


async def test_polling_failure_propagates_and_cleans_up():
    dispatcher = FakeDispatcher(failure=RuntimeError("synthetic polling error"))
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with pytest.raises(RuntimeError, match="synthetic polling error"):
        await run_supervised_polling(dispatcher, bot, timeout_seconds=1, shutdown_timeout=0.1)
    assert dispatcher.starts == 1
    assert len(bot.session.middleware) == 0
    await bot.session.close()


async def test_external_cancellation_stops_polling_and_unregisters_middleware():
    dispatcher = FakeDispatcher()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    task = asyncio.create_task(
        run_supervised_polling(dispatcher, bot, timeout_seconds=1, shutdown_timeout=0.1)
    )
    await dispatcher.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert dispatcher.starts == 1
    assert dispatcher.stops == 1
    assert len(bot.session.middleware) == 0
    await bot.session.close()


async def test_hung_stop_is_bounded_and_polling_task_is_cancelled():
    dispatcher = FakeDispatcher(block_stop=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with pytest.raises(PollingStalled):
        await asyncio.wait_for(
            run_supervised_polling(dispatcher, bot, timeout_seconds=0.05, shutdown_timeout=0.05),
            timeout=2,
        )
    assert dispatcher.starts == 1
    assert dispatcher.stops == 1
    assert len(bot.session.middleware) == 0
    await bot.session.close()


async def test_successful_empty_polls_prevent_false_stall_during_quiet_chats(monkeypatch):
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    stop = asyncio.Event()
    count = 0

    async def make_request(_bot, method, **_kwargs):
        nonlocal count
        assert isinstance(method, GetUpdates)
        await asyncio.sleep(0.03)
        count += 1
        return []

    monkeypatch.setattr(bot.session, "make_request", make_request)

    class QuietDispatcher:
        def resolve_used_update_types(self):
            return ["message"]

        async def start_polling(self, current_bot, **_kwargs):
            while not stop.is_set():
                await current_bot(GetUpdates(timeout=10))

        async def stop_polling(self):
            stop.set()

    task = asyncio.create_task(
        run_supervised_polling(QuietDispatcher(), bot, timeout_seconds=0.15, shutdown_timeout=0.1)
    )
    await asyncio.sleep(0.4)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert count >= 2
    assert len(bot.session.middleware) == 0
    await bot.session.close()


def test_watchdog_configuration_has_bounded_production_default():
    assert Settings(_env_file=None).poll_watchdog_seconds == 300
    for invalid in (0, 119, 3601):
        with pytest.raises(ValueError):
            Settings(poll_watchdog_seconds=invalid, _env_file=None)
    assert Settings(poll_watchdog_seconds=600, _env_file=None).poll_watchdog_seconds == 600


async def test_real_aiogram_dispatcher_stops_its_internal_long_poll_on_stall(monkeypatch):
    from aiogram import Dispatcher
    from aiogram.types import User

    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    in_request = asyncio.Event()
    request_cancelled = asyncio.Event()
    calls = []

    async def make_request(_bot, method, **_kwargs):
        calls.append(type(method).__name__)
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Synthetic", username="synthetic_bot")
        assert isinstance(method, GetUpdates)
        in_request.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_cancelled.set()

    monkeypatch.setattr(bot.session, "make_request", make_request)

    class SignalFreeDispatcher(Dispatcher):
        async def start_polling(self, *bots, **kwargs):
            await super().start_polling(*bots, handle_signals=False, **kwargs)

    dispatcher = SignalFreeDispatcher()
    with pytest.raises(PollingStalled):
        await asyncio.wait_for(
            run_supervised_polling(dispatcher, bot, timeout_seconds=0.1, shutdown_timeout=0.2),
            timeout=2,
        )
    assert in_request.is_set()
    assert request_cancelled.is_set()
    assert calls.count("GetUpdates") == 1
    assert len(bot.session.middleware) == 0
    await bot.session.close()


async def test_watchdog_internal_failure_still_stops_poller_and_unregisters(monkeypatch):
    async def broken_monitor(_self):
        raise RuntimeError("synthetic watchdog fault")

    monkeypatch.setattr(PollingHeartbeat, "wait_until_stale", broken_monitor)
    dispatcher = FakeDispatcher()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with pytest.raises(RuntimeError, match="synthetic watchdog fault"):
        await run_supervised_polling(dispatcher, bot, timeout_seconds=1, shutdown_timeout=0.1)
    assert dispatcher.starts == 1
    assert dispatcher.stops == 1
    assert len(bot.session.middleware) == 0
    await bot.session.close()
