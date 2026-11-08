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


async def make_worker(
    monkeypatch, *, stop_mode="normal", embedded=False, owner_id=None, notify=None, start_error=None
):
    from importlib import import_module

    module = import_module("summon.bot.main")
    settings = Settings(
        bot_token="123:fake",
        session_secret="s" * 40,
        telegram_api_id=123,
        telegram_api_hash="fake",
        webhooks_disabled_confirmed=True,
        owner_id=owner_id,
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
    database = object()
    connect = AsyncMock(return_value=(mongo, database))
    started = MagicMock()
    idle_mock = AsyncMock(side_effect=idle)
    monkeypatch.setattr(module, "Settings", lambda: settings)
    monkeypatch.setattr(module, "validate_runtime", lambda: None)
    monkeypatch.setattr(module, "connect_database", connect)
    monkeypatch.setattr(module, "IngressLease", lambda *_args: lease)
    monkeypatch.setattr(module, "worker_storage", lambda *_args: object())
    monkeypatch.setattr(module, "Client", factory)
    monkeypatch.setattr(module, "Store", lambda *_args: object())
    monkeypatch.setattr(module, "register_handlers", lambda *_args: None)
    # This harness isolates worker supervision; the real manifest has dedicated tests.
    monkeypatch.setattr(module, "validate_handler_registration", lambda *_args: None)
    monkeypatch.setattr(module, "start_verified_client", AsyncMock(side_effect=start_error))
    monkeypatch.setattr(module, "register_command_menus", AsyncMock())
    monkeypatch.setattr(module, "monitor_connection", monitor)
    monkeypatch.setattr(module, "idle", idle_mock)
    if notify is not None:
        monkeypatch.setattr(module, "notify_owner_startup", notify)
    task = asyncio.create_task(
        module.run(
            shared_database=(mongo, database) if embedded else None,
            embedded=embedded,
            on_started=started,
        )
    )
    if start_error is None:
        await asyncio.wait_for(monitor_started.wait(), 1)
    return SimpleNamespace(
        task=task,
        bot=bot,
        lease=lease,
        mongo=mongo,
        factory=factory,
        connect=connect,
        started=started,
        idle=idle_mock,
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


async def test_embedded_worker_uses_shared_mongo_and_does_not_install_signal_handlers(monkeypatch):
    worker = await make_worker(monkeypatch, embedded=True)
    worker.started.assert_called_once()
    worker.connect.assert_not_awaited()
    worker.idle.assert_not_awaited()
    assert worker.factory.call_args.kwargs["no_updates"] is False
    worker.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker.task
    worker.bot.stop.assert_awaited_once()
    worker.lease.release.assert_awaited_once()
    worker.mongo.close.assert_not_awaited()


@pytest.mark.parametrize("stop_mode", ["error", "still-initialized"])
async def test_embedded_worker_never_restarts_after_uncertain_shutdown(monkeypatch, stop_mode):
    from summon.bot.main import EmbeddedBotShutdownUncertain

    worker = await make_worker(monkeypatch, stop_mode=stop_mode, embedded=True)
    worker.fail_monitor.set()
    with pytest.raises(EmbeddedBotShutdownUncertain):
        await asyncio.wait_for(worker.task, 1)
    worker.bot.stop.assert_awaited_once()
    worker.lease.release.assert_not_awaited()
    worker.mongo.close.assert_not_awaited()


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


def embedded_settings(**overrides):
    values = {
        "bot_token": "123:synthetic-worker-token",
        "telegram_api_id": 123,
        "telegram_api_hash": "synthetic-api-hash",
        "webhooks_disabled_confirmed": True,
        "_env_file": None,
    }
    return Settings(**{**values, **overrides})


@pytest.mark.parametrize("reason", ["webhook", "token", "api_id", "api_hash", "bad_token"])
async def test_embedded_supervisor_refuses_missing_confirmation_or_credentials(
    monkeypatch, reason, caplog
):
    from summon.bot import supervisor

    overrides = {
        "webhook": {"webhooks_disabled_confirmed": False},
        "token": {"bot_token": ""},
        "api_id": {"telegram_api_id": None},
        "api_hash": {"telegram_api_hash": ""},
        "bad_token": {"bot_token": "PRIVATE_BAD_TOKEN"},
    }[reason]
    worker = AsyncMock()
    monkeypatch.setattr(supervisor, "run", worker)
    status = supervisor.WorkerStatus()
    await supervisor.supervise_embedded_worker(
        embedded_settings(**overrides), object(), object(), status
    )
    assert status.state == "configuration_required"
    assert (
        status.reason
        == {
            "webhook": "webhook_confirmation_required",
            "token": "telegram_credentials_required",
            "api_id": "telegram_credentials_required",
            "api_hash": "telegram_credentials_required",
            "bad_token": "bot_token_invalid_format",
        }[reason]
    )
    worker.assert_not_awaited()
    assert "PRIVATE_BAD_TOKEN" not in caplog.text
    if reason == "webhook":
        assert "WEBHOOKS_DISABLED_CONFIRMED=true" in caplog.text


async def test_embedded_supervisor_retries_busy_lease_and_starts_one_native_worker(
    monkeypatch, caplog
):
    from summon.bot import supervisor

    settings = embedded_settings()
    mongo, database = object(), object()
    status = supervisor.WorkerStatus()
    entered = asyncio.Event()
    attempts = []

    async def worker(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise IngressLeaseLost("PRIVATE_LEASE_DETAIL")
        kwargs["on_started"]()
        entered.set()
        await asyncio.Event().wait()

    retry = AsyncMock()
    monkeypatch.setattr(supervisor, "run", worker)
    monkeypatch.setattr(supervisor, "_wait_before_retry", retry)
    task = asyncio.create_task(
        supervisor.supervise_embedded_worker(settings, mongo, database, status)
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert status.state == "running" and status.reason is None
    assert len(attempts) == 2
    assert all(a["embedded"] and a["shared_database"] == (mongo, database) for a in attempts)
    retry.assert_awaited_once_with(5)
    assert "PRIVATE_LEASE_DETAIL" not in caplog.text
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("failure", ["rejected", "session", "uncertain"])
async def test_embedded_supervisor_does_not_retry_permanent_or_uncertain_failure(
    monkeypatch, failure, caplog
):
    from wzgram.errors import AccessTokenInvalid

    from summon.bot import supervisor
    from summon.bot.main import EmbeddedBotShutdownUncertain

    error = {
        "rejected": AccessTokenInvalid("PRIVATE_REJECTED"),
        "session": RuntimeError("PRIVATE_SESSION_DETAIL"),
        "uncertain": EmbeddedBotShutdownUncertain("PRIVATE_STOP_DETAIL"),
    }[failure]
    worker = AsyncMock(side_effect=error)
    retry = AsyncMock()
    monkeypatch.setattr(supervisor, "run", worker)
    monkeypatch.setattr(supervisor, "_wait_before_retry", retry)
    status = supervisor.WorkerStatus()
    await supervisor.supervise_embedded_worker(embedded_settings(), object(), object(), status)
    assert status.state == (
        "restart_required" if failure == "uncertain" else "configuration_required"
    )
    assert (
        status.reason
        == {
            "rejected": "telegram_startup_rejected",
            "session": "worker_configuration_or_session",
            "uncertain": "shutdown_unconfirmed",
        }[failure]
    )
    worker.assert_awaited_once()
    retry.assert_not_awaited()
    assert "PRIVATE_" not in caplog.text


async def test_embedded_supervisor_caps_retries_and_respects_telegram_flood_wait(monkeypatch):
    from unittest.mock import call

    from wzgram.errors import FloodWait

    from summon.bot import supervisor

    entered = asyncio.Event()
    attempts = 0

    async def worker(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 9:
            entered.set()
            await asyncio.Event().wait()
        if attempts == 2:
            raise FloodWait(180)
        raise ConnectionError("PRIVATE_NETWORK_DETAIL")

    retry = AsyncMock()
    monkeypatch.setattr(supervisor, "run", worker)
    monkeypatch.setattr(supervisor, "_wait_before_retry", retry)
    status = supervisor.WorkerStatus()
    task = asyncio.create_task(
        supervisor.supervise_embedded_worker(embedded_settings(), object(), object(), status)
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert retry.await_args_list == [call(s) for s in (5, 180, 30, 60, 120, 300, 300, 300)]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("failure", ["lease", "flood", "network"])
async def test_embedded_supervisor_reports_fixed_retry_reason(monkeypatch, failure):
    from wzgram.errors import FloodWait

    from summon.bot import supervisor

    errors = {
        "lease": IngressLeaseLost("PRIVATE_LEASE_DETAIL"),
        "flood": FloodWait(180),
        "network": ConnectionError("PRIVATE_NETWORK_DETAIL"),
    }
    expected = {
        "lease": "lease_unavailable",
        "flood": "telegram_rate_limit",
        "network": "transient_failure",
    }
    status = supervisor.WorkerStatus()
    monkeypatch.setattr(supervisor, "run", AsyncMock(side_effect=errors[failure]))

    async def inspect_retry(_seconds):
        assert status.state == "retrying"
        assert status.reason == expected[failure]
        raise asyncio.CancelledError

    monkeypatch.setattr(supervisor, "_wait_before_retry", inspect_retry)
    with pytest.raises(asyncio.CancelledError):
        await supervisor.supervise_embedded_worker(embedded_settings(), object(), object(), status)


def owner_startup_settings(**overrides):
    values = {
        "owner_id": 42,
        "bot_token": "123:SYNTHETIC_PRIVATE_TOKEN",
        "telegram_api_hash": "SYNTHETIC_PRIVATE_HASH",
        "mongo_uri": "mongodb://SYNTHETIC_PRIVATE_HOST/db",
        "session_secret": "SYNTHETIC_PRIVATE_SESSION_SECRET",
        "_env_file": None,
    }
    return Settings(**{**values, **overrides})


async def test_owner_startup_report_contains_only_safe_details(monkeypatch):
    from summon.bot import startup_report

    bot = SimpleNamespace(me=SimpleNamespace(username="SummonTestBot"))
    lease = SimpleNamespace(valid=lambda: True)
    sender = AsyncMock(return_value=object())
    monkeypatch.setattr(startup_report, "send_card", sender)
    await startup_report.notify_owner_startup(bot, owner_startup_settings(), lease)
    sender.assert_awaited_once()
    assert sender.await_args.args[0] is bot and sender.await_args.args[1] == 42
    card = sender.await_args.args[2]
    text = card.telegram_html(premium=False)
    assert "SUMMON is online" in text and "@SummonTestBot" in text
    assert "Wzgram" in text and "Initialization completed" in text and "UTC" in text
    assert "verify update delivery" in text
    assert "SYNTHETIC_PRIVATE" not in text
    assert sender.await_args.kwargs == {"rich": True, "premium": True}


@pytest.mark.parametrize("reason", ["no_owner", "disabled", "lease_lost"])
async def test_owner_startup_notification_skips_missing_disabled_or_unleased(monkeypatch, reason):
    from summon.bot import startup_report

    overrides = {
        "no_owner": {"owner_id": None, "operator_ids": [7, 8]},
        "disabled": {"startup_dm_enabled": False},
        "lease_lost": {},
    }[reason]
    sender = AsyncMock()
    monkeypatch.setattr(startup_report, "send_card", sender)
    await startup_report.notify_owner_startup(
        SimpleNamespace(me=None),
        owner_startup_settings(**overrides),
        SimpleNamespace(valid=lambda: reason != "lease_lost"),
    )
    sender.assert_not_awaited()


async def test_owner_startup_report_respects_visual_settings_and_rejects_unsafe_username(
    monkeypatch,
):
    from summon.bot import startup_report

    sender = AsyncMock(return_value=object())
    monkeypatch.setattr(startup_report, "send_card", sender)
    await startup_report.notify_owner_startup(
        SimpleNamespace(me=SimpleNamespace(username='<script>alert("x")</script>')),
        owner_startup_settings(rich_enabled=False, premium_emoji_enabled=False),
        SimpleNamespace(valid=lambda: True),
    )
    sender.assert_awaited_once()
    assert sender.await_args.kwargs == {"rich": False, "premium": False}
    assert "<script>" not in sender.await_args.args[2].telegram_html(premium=False)


@pytest.mark.parametrize("failure", ["error", "flood", "unconfirmed", "timeout"])
async def test_owner_startup_notification_never_retries_ambiguous_failure(
    monkeypatch, failure, caplog
):
    from wzgram.errors import FloodWait

    from summon.bot import startup_report

    async def hung(*_args, **_kwargs):
        await asyncio.Event().wait()

    sender = {
        "error": AsyncMock(side_effect=OSError("PRIVATE_NOTIFICATION_DETAIL")),
        "flood": AsyncMock(side_effect=FloodWait(60)),
        "unconfirmed": AsyncMock(return_value=None),
        "timeout": AsyncMock(side_effect=hung),
    }[failure]
    monkeypatch.setattr(startup_report, "send_card", sender)
    monkeypatch.setattr(startup_report, "_STARTUP_DM_TIMEOUT_SECONDS", 0.01)
    await asyncio.wait_for(
        startup_report.notify_owner_startup(
            SimpleNamespace(me=None), owner_startup_settings(), SimpleNamespace(valid=lambda: True)
        ),
        timeout=1,
    )
    sender.assert_awaited_once()
    assert "PRIVATE_NOTIFICATION_DETAIL" not in caplog.text
    if failure in {"unconfirmed", "timeout"}:
        assert "unconfirmed" in caplog.text or "not confirmed" in caplog.text


async def test_owner_startup_report_uses_premium_fallback_only_on_definite_rejection(caplog):
    from wzgram.errors import BadRequest

    from summon.bot.startup_report import notify_owner_startup

    bot = SimpleNamespace(
        me=SimpleNamespace(username="SummonTestBot"),
        send_rich_message=AsyncMock(side_effect=BadRequest("CUSTOM_EMOJI_INVALID")),
        send_message=AsyncMock(return_value=object()),
    )
    await notify_owner_startup(bot, owner_startup_settings(), SimpleNamespace(valid=lambda: True))
    bot.send_rich_message.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 42
    assert "<tg-emoji" not in bot.send_message.await_args.kwargs["text"]
    assert "SUMMON is online" in bot.send_message.await_args.kwargs["text"]
    assert "CUSTOM_EMOJI_INVALID" not in caplog.text


async def test_owner_startup_report_does_not_fallback_after_unconfirmed_rich_send(caplog):
    from summon.bot.startup_report import notify_owner_startup

    bot = SimpleNamespace(
        me=SimpleNamespace(username="SummonTestBot"),
        send_rich_message=AsyncMock(return_value=None),
        send_message=AsyncMock(),
    )
    await notify_owner_startup(bot, owner_startup_settings(), SimpleNamespace(valid=lambda: True))
    bot.send_rich_message.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert "not confirmed" in caplog.text


@pytest.mark.parametrize("embedded", [False, True])
async def test_worker_sends_one_owner_notice_after_startup_and_cancels_it_before_stop(
    monkeypatch, embedded
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def notify(bot, settings, lease):
        assert bot.is_connected and settings.owner_id == 42 and lease.valid()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    notification = AsyncMock(side_effect=notify)
    worker = await make_worker(monkeypatch, embedded=embedded, owner_id=42, notify=notification)
    await asyncio.wait_for(entered.wait(), 1)
    worker.started.assert_called_once()
    worker.factory.assert_called_once()
    notification.assert_awaited_once()

    async def stop():
        assert cancelled.is_set()
        worker.bot.is_connected = False
        worker.bot.is_initialized = False

    worker.bot.stop.side_effect = stop
    worker.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker.task
    assert cancelled.is_set()
    worker.bot.stop.assert_awaited_once()
    worker.lease.release.assert_awaited_once()


async def test_failed_worker_startup_never_sends_owner_notification(monkeypatch):
    notification = AsyncMock()
    worker = await make_worker(
        monkeypatch,
        embedded=True,
        owner_id=42,
        notify=notification,
        start_error=OSError("PRIVATE_START_DETAIL"),
    )
    with pytest.raises(OSError):
        await asyncio.wait_for(worker.task, 1)
    notification.assert_not_awaited()
    worker.started.assert_not_called()
    worker.bot.stop.assert_awaited_once()
    worker.lease.release.assert_awaited_once()
    worker.mongo.close.assert_not_awaited()


async def test_owner_dm_failure_does_not_stop_started_worker(monkeypatch, caplog):
    from summon.bot import startup_report

    attempted = asyncio.Event()

    async def reject(*_args, **_kwargs):
        attempted.set()
        raise OSError("PRIVATE_NOTIFICATION_DETAIL")

    sender = AsyncMock(side_effect=reject)
    monkeypatch.setattr(startup_report, "send_card", sender)
    worker = await make_worker(monkeypatch, embedded=True, owner_id=42)
    await asyncio.wait_for(attempted.wait(), 1)
    await asyncio.sleep(0)
    assert not worker.task.done()
    sender.assert_awaited_once()
    assert "PRIVATE_NOTIFICATION_DETAIL" not in caplog.text
    worker.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker.task
    worker.bot.stop.assert_awaited_once()
