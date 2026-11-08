"""Native connection monitoring and single-worker shutdown without external services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import DuplicateKeyError
from wzgram import Client, StopPropagation, raw

from summon.bot.ingress import IngressLease, IngressLeaseLost
from summon.bot.watchdog import TelegramConnectionStalled, monitor_connection
from summon.config import Settings


def connected(invoke=None):
    return SimpleNamespace(is_connected=True, is_initialized=True, invoke=invoke or AsyncMock())


async def test_successful_get_state_refreshes_deadline_after_failures():
    now = [0.0]
    calls = 0

    async def invoke(request):
        nonlocal calls
        assert isinstance(request, raw.functions.updates.GetState)
        calls += 1
        now[0] += 3
        if calls != 2:
            raise OSError("synthetic failure")

    with pytest.raises(TelegramConnectionStalled):
        await monitor_connection(
            connected(invoke),
            SimpleNamespace(valid=lambda: True),
            timeout_seconds=8,
            interval_seconds=0.001,
            clock=lambda: now[0],
        )
    # The success at t=6 refreshes the deadline: it expires at t=15, not t=9.
    assert calls == 5


async def test_regular_successful_checks_do_not_claim_update_delivery():
    checked = asyncio.Event()
    now = [0.0]
    calls = 0

    async def invoke(request):
        nonlocal calls
        assert isinstance(request, raw.functions.updates.GetState)
        calls += 1
        now[0] += 20
        if calls >= 5:
            checked.set()
        return object()

    task = asyncio.create_task(
        monitor_connection(
            connected(invoke),
            SimpleNamespace(valid=lambda: True),
            timeout_seconds=30,
            interval_seconds=0.001,
            clock=lambda: now[0],
        )
    )
    try:
        await asyncio.wait_for(checked.wait(), 1)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert calls >= 5  # RPC responsiveness, not proof of update delivery.


async def test_repeated_failed_checks_raise_without_restarting_a_client(caplog):
    client = connected(AsyncMock(side_effect=OSError("PRIVATE_CONNECTION_DETAIL")))
    with pytest.raises(TelegramConnectionStalled, match="stopped answering"):
        await asyncio.wait_for(
            monitor_connection(
                client,
                SimpleNamespace(valid=lambda: True),
                timeout_seconds=0.04,
                interval_seconds=0.01,
            ),
            1,
        )
    assert client.invoke.await_count >= 2
    assert "PRIVATE_CONNECTION_DETAIL" not in caplog.text


@pytest.mark.parametrize("connected_state,initialized", [(False, True), (True, False)])
async def test_disconnected_or_uninitialized_client_never_publishes_health(
    connected_state, initialized
):
    client = SimpleNamespace(
        is_connected=connected_state, is_initialized=initialized, invoke=AsyncMock()
    )
    health = SimpleNamespace(record=AsyncMock())
    with pytest.raises(TelegramConnectionStalled):
        await asyncio.wait_for(
            monitor_connection(
                client,
                SimpleNamespace(valid=lambda: True),
                timeout_seconds=0.03,
                interval_seconds=0.01,
                health_repository=health,
            ),
            1,
        )
    client.invoke.assert_not_awaited()
    health.record.assert_not_awaited()


async def test_lost_lease_stops_monitor_before_network_or_health_write():
    client = connected()
    health = SimpleNamespace(record=AsyncMock())
    with pytest.raises(TelegramConnectionStalled, match="coordination"):
        await monitor_connection(
            client,
            SimpleNamespace(valid=lambda: False),
            timeout_seconds=1,
            health_repository=health,
        )
    client.invoke.assert_not_awaited()
    health.record.assert_not_awaited()


async def test_hung_get_state_is_cancelled_and_eventually_declared_stalled():
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def hung(_request):
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(TelegramConnectionStalled):
        await asyncio.wait_for(
            monitor_connection(
                connected(hung),
                SimpleNamespace(valid=lambda: True),
                timeout_seconds=0.03,
                interval_seconds=0.001,
                rpc_timeout_seconds=0.02,
            ),
            1,
        )
    assert entered.is_set() and cancelled.is_set() and calls >= 1


async def test_external_cancellation_cancels_inflight_rpc():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hung(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(
        monitor_connection(connected(hung), SimpleNamespace(valid=lambda: True), timeout_seconds=10)
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 1, "interval_seconds": 0},
        {"timeout_seconds": 1, "rpc_timeout_seconds": 0},
    ],
)
async def test_monitor_rejects_nonpositive_intervals(kwargs):
    with pytest.raises(ValueError):
        await monitor_connection(connected(), SimpleNamespace(valid=lambda: True), **kwargs)


def test_watchdog_configuration_has_bounded_production_default():
    assert Settings(_env_file=None).poll_watchdog_seconds == 300
    for invalid in (0, 119, 3601):
        with pytest.raises(ValueError):
            Settings(poll_watchdog_seconds=invalid, _env_file=None)
    assert Settings(poll_watchdog_seconds=600, _env_file=None).poll_watchdog_seconds == 600


def fake_lease_database():
    collection = SimpleNamespace(update_one=AsyncMock(), delete_one=AsyncMock())
    db = MagicMock()
    db.__getitem__.return_value.with_options.return_value = collection
    return db, collection


async def test_lease_initializes_without_expr_upsert_then_acquires_and_renews():
    database, collection = fake_lease_database()
    collection.update_one.side_effect = [
        object(),
        SimpleNamespace(matched_count=1),
        SimpleNamespace(matched_count=1),
    ]
    lease = IngressLease(database, 123)
    await lease.acquire()
    assert lease.valid()
    first, second = collection.update_one.await_args_list[:2]
    assert first.args[0] == {"_id": "bot:123"} and first.kwargs["upsert"] is True
    assert "$expr" in second.args[0] and not second.kwargs.get("upsert", False)
    await lease.renew()
    renewal = collection.update_one.await_args_list[2]
    assert renewal.args[0]["owner"] == lease._owner and "$expr" in renewal.args[0]
    await lease.release()
    assert not lease.valid()
    collection.delete_one.assert_awaited_once_with({"_id": "bot:123", "owner": lease._owner})


async def test_concurrent_initial_slot_creation_is_not_treated_as_lease_ownership():
    database, collection = fake_lease_database()
    collection.update_one.side_effect = [
        DuplicateKeyError("race"),
        SimpleNamespace(matched_count=0),
    ]
    lease = IngressLease(database, 123)
    with pytest.raises(IngressLeaseLost):
        await lease.acquire()
    assert not lease.valid() and collection.update_one.await_count == 2


async def test_failed_renewal_invalidates_local_lease():
    database, collection = fake_lease_database()
    collection.update_one.side_effect = [
        object(),
        SimpleNamespace(matched_count=1),
        SimpleNamespace(matched_count=0),
    ]
    lease = IngressLease(database, 123)
    await lease.acquire()
    with pytest.raises(IngressLeaseLost):
        await lease.renew()
    assert not lease.valid()


async def test_registered_lease_guards_stop_messages_edits_and_callbacks():
    database, _ = fake_lease_database()
    lease = IngressLease(database, 123)
    client = Client(
        "offline-lease", api_id=123, api_hash="test", bot_token="123:fake", in_memory=True
    )
    lease.register(client)
    await asyncio.sleep(0)
    handlers = client.dispatcher.groups[-1000]
    assert len(handlers) == 3
    for handler in handlers:
        with pytest.raises(StopPropagation):
            await handler.callback(client, object())
    lease._deadline = asyncio.get_running_loop().time() + 10
    for handler in handlers:
        await handler.callback(client, object())
    lease.invalidate()
    assert not lease.valid()


async def make_worker(monkeypatch, *, stop_mode="normal"):
    from importlib import import_module

    module = import_module("summon.bot.main")
    settings = Settings(
        bot_token="123:fake",
        session_secret="s" * 40,
        telegram_api_id=123,
        telegram_api_hash="fake",
        webhooks_disabled_confirmed=True,
        _env_file=None,
    )
    monitor_started, fail_monitor, fail_lease = asyncio.Event(), asyncio.Event(), asyncio.Event()
    mongo = SimpleNamespace(close=AsyncMock())
    groups = {-1000: [object()] * 3, -100: [object()] * 2, 0: [object()] * 40}
    bot = SimpleNamespace(
        is_connected=True,
        is_initialized=True,
        dispatcher=SimpleNamespace(groups=groups),
        stop=AsyncMock(),
    )

    async def stop():
        if stop_mode == "error":
            raise OSError("PRIVATE_STOP_DETAIL")
        bot.is_connected = False
        bot.is_initialized = stop_mode == "still-initialized"

    bot.stop.side_effect = stop

    async def maintain():
        await fail_lease.wait()
        raise IngressLeaseLost("lease lost")

    lease = SimpleNamespace(
        acquire=AsyncMock(),
        maintain=AsyncMock(side_effect=maintain),
        register=MagicMock(),
        valid=MagicMock(return_value=True),
        invalidate=MagicMock(),
        release=AsyncMock(),
    )

    async def monitor(*_args, **_kwargs):
        monitor_started.set()
        await fail_monitor.wait()
        raise TelegramConnectionStalled("connection stalled")

    async def idle():
        await asyncio.Event().wait()

    factory = MagicMock(return_value=bot)
    monkeypatch.setattr(module, "Settings", lambda: settings)
    monkeypatch.setattr(module, "validate_runtime", lambda: None)
    monkeypatch.setattr(module, "connect_database", AsyncMock(return_value=(mongo, object())))
    monkeypatch.setattr(module, "IngressLease", lambda *_args: lease)
    monkeypatch.setattr(module, "worker_storage", lambda *_args: object())
    monkeypatch.setattr(module, "Client", factory)
    monkeypatch.setattr(module, "Store", lambda *_args: object())
    monkeypatch.setattr(module, "register_handlers", lambda *_args: None)
    # This harness isolates worker supervision; the real manifest has dedicated tests.
    monkeypatch.setattr(module, "validate_handler_registration", lambda *_args: None)
    monkeypatch.setattr(module, "start_verified_client", AsyncMock())
    monkeypatch.setattr(module, "register_command_menus", AsyncMock())
    monkeypatch.setattr(module, "monitor_connection", monitor)
    monkeypatch.setattr(module, "idle", idle)
    task = asyncio.create_task(module.run())
    await asyncio.wait_for(monitor_started.wait(), 1)
    return SimpleNamespace(
        task=task,
        bot=bot,
        lease=lease,
        mongo=mongo,
        factory=factory,
        fail_monitor=fail_monitor,
        fail_lease=fail_lease,
    )


@pytest.mark.parametrize("failure", ["monitor", "lease"])
async def test_worker_stops_once_when_supervisor_fails(monkeypatch, failure):
    worker = await make_worker(monkeypatch)
    getattr(worker, f"fail_{failure}").set()
    error = TelegramConnectionStalled if failure == "monitor" else IngressLeaseLost
    with pytest.raises(error):
        await asyncio.wait_for(worker.task, 1)
    worker.factory.assert_called_once()
    assert worker.factory.call_args.kwargs["no_updates"] is False
    assert worker.factory.call_args.kwargs["skip_updates"] is False
    worker.bot.stop.assert_awaited_once()
    worker.lease.release.assert_awaited_once()
    worker.mongo.close.assert_awaited_once()


@pytest.mark.parametrize("stop_mode", ["error", "still-initialized"])
async def test_uncertain_stop_never_releases_lease_early(monkeypatch, stop_mode, caplog):
    worker = await make_worker(monkeypatch, stop_mode=stop_mode)
    worker.fail_monitor.set()
    with pytest.raises(TelegramConnectionStalled):
        await asyncio.wait_for(worker.task, 1)
    worker.bot.stop.assert_awaited_once()
    worker.lease.release.assert_not_awaited()
    worker.mongo.close.assert_awaited_once()
    assert "PRIVATE_STOP_DETAIL" not in caplog.text


async def test_external_worker_cancellation_closes_client_before_releasing_lease(monkeypatch):
    worker = await make_worker(monkeypatch)
    worker.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker.task
    worker.factory.assert_called_once()
    worker.bot.stop.assert_awaited_once()
    worker.lease.release.assert_awaited_once()
    worker.mongo.close.assert_awaited_once()


async def test_missing_webhook_confirmation_fails_before_connecting_database(monkeypatch):
    from importlib import import_module

    module = import_module("summon.bot.main")
    settings = Settings(
        bot_token="123:fake",
        telegram_api_id=123,
        telegram_api_hash="fake",
        webhooks_disabled_confirmed=False,
        _env_file=None,
    )
    connect = AsyncMock()
    monkeypatch.setattr(module, "Settings", lambda: settings)
    monkeypatch.setattr(module, "validate_runtime", lambda: None)
    monkeypatch.setattr(module, "connect_database", connect)
    with pytest.raises(RuntimeError, match="webhook"):
        await module.run()
    connect.assert_not_awaited()
