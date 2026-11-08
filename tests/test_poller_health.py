"""External poll-progress monitoring with synthetic credentials and fake storage."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.methods import GetMe, GetUpdates
from fastapi.testclient import TestClient
from pymongo import ReadPreference
from pymongo.errors import OperationFailure
from summon.bot.poller_health import PollerHealthPublisher

from summon.api.app import create_app
from summon.bot.watchdog import PollingHeartbeat
from summon.config import Settings
from summon.database import initialize_database
from summon.repositories.poller_health import PollerHealthRepository, poller_health_key

FAKE_BOT_TOKEN = "123456:TEST_MONITOR_TOKEN_NOT_REAL"
FAKE_SESSION_SECRET = "s" * 40
FAKE_MONITOR_TOKEN = "t" * 40


def monitor_settings(**kwargs):
    return Settings(
        bot_token=FAKE_BOT_TOKEN,
        session_secret=FAKE_SESSION_SECRET,
        poller_heartbeat_enabled=True,
        poller_monitor_token=FAKE_MONITOR_TOKEN,
        _env_file=None,
        **kwargs,
    )


@contextmanager
def connected_monitor_client():
    # Exercise the real application's non-preview lifespan, but never connect to MongoDB.
    fake_client = MagicMock(close=AsyncMock())
    app = create_app(settings=monitor_settings(), telegram_bot=object())
    with (
        patch("summon.api.app.connect_database", AsyncMock(return_value=(fake_client, object()))),
        patch("summon.api.app.Store", return_value=object()),
        TestClient(app) as client,
    ):
        yield client
    fake_client.close.assert_awaited_once()


def test_monitor_is_opt_in_and_requires_separate_secure_api_token():
    settings = Settings(_env_file=None)
    assert settings.poller_heartbeat_enabled is False
    assert settings.poller_monitor_token.get_secret_value() == ""
    assert settings.poller_status_max_age_seconds == 120
    for age in (59, 901):
        with pytest.raises(ValueError):
            Settings(poller_status_max_age_seconds=age, _env_file=None)
    for bad in ("", "x" * 31, "x" * 129, "é" * 40, " " * 40):
        settings = Settings(
            bot_token=FAKE_BOT_TOKEN,
            session_secret=FAKE_SESSION_SECRET,
            poller_heartbeat_enabled=True,
            poller_monitor_token=bad,
            _env_file=None,
        )
        with pytest.raises(ValueError, match="SUMMON_POLLER_MONITOR_TOKEN") as error:
            create_app(settings=settings, store=object())
        assert bad not in str(error.value) if bad else True
    reused = Settings(
        bot_token=FAKE_BOT_TOKEN,
        session_secret=FAKE_SESSION_SECRET,
        poller_heartbeat_enabled=True,
        poller_monitor_token=FAKE_SESSION_SECRET,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="SUMMON_POLLER_MONITOR_TOKEN"):
        create_app(settings=reused, store=object())
    # The worker can publish without receiving the API-only monitoring token.
    worker = Settings(
        bot_token=FAKE_BOT_TOKEN,
        session_secret=FAKE_SESSION_SECRET,
        poller_heartbeat_enabled=True,
        _env_file=None,
    )
    assert worker.poller_heartbeat_enabled
    for token, secret in (("", FAKE_SESSION_SECRET), (FAKE_BOT_TOKEN, "short")):
        with pytest.raises(ValueError):
            Settings(
                bot_token=token,
                session_secret=secret,
                poller_heartbeat_enabled=True,
                _env_file=None,
            )


def test_key_is_domain_separated_opaque_and_changes_with_either_secret():
    key = poller_health_key(FAKE_BOT_TOKEN, FAKE_SESSION_SECRET)
    assert key.startswith("poller:") and len(key) == 71
    assert FAKE_BOT_TOKEN not in key and FAKE_SESSION_SECRET not in key
    assert key == poller_health_key(FAKE_BOT_TOKEN, FAKE_SESSION_SECRET)
    assert key != poller_health_key("another-fake-token", FAKE_SESSION_SECRET)
    assert key != poller_health_key(FAKE_BOT_TOKEN, "x" * 40)
    with pytest.raises(ValueError):
        poller_health_key("", FAKE_SESSION_SECRET)
    with pytest.raises(ValueError):
        poller_health_key(FAKE_BOT_TOKEN, "too-short")


def fake_database():
    database = MagicMock()
    collection = database.__getitem__.return_value.with_options.return_value
    collection.update_one = AsyncMock()
    collection.find_one = AsyncMock(return_value=None)
    return database, collection


async def test_repository_records_event_time_monotonically_not_write_completion_time():
    db, collection = fake_database()
    repo = PollerHealthRepository(db, FAKE_BOT_TOKEN, FAKE_SESSION_SECRET)
    event_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    await repo.record(event_at)
    args, kwargs = collection.update_one.await_args
    assert args[0] == {"_id": poller_health_key(FAKE_BOT_TOKEN, FAKE_SESSION_SECRET)}
    fields = args[1][0]["$set"]
    event = {"$literal": event_at}
    expires = {"$literal": event_at + timedelta(hours=1)}
    assert fields["observed_at"]["$cond"][0] == {"$lte": [event, {"$add": ["$$NOW", 5_000]}]}
    assert fields["observed_at"]["$cond"][1]["$max"][1] == event
    assert fields["expires_at"]["$cond"][1]["$max"][1] == expires
    assert kwargs == {"upsert": True}
    options = db.__getitem__.return_value.with_options.call_args.kwargs
    assert options["read_preference"] == ReadPreference.PRIMARY
    assert options["read_concern"].level == "majority"
    assert options["write_concern"].document["w"] == 1
    with pytest.raises(ValueError):
        await repo.record(datetime(2026, 1, 1))  # no ambiguous local clock


async def test_repository_checks_both_freshness_and_future_skew_using_mongo_clock():
    db, collection = fake_database()
    repo = PollerHealthRepository(db, FAKE_BOT_TOKEN, FAKE_SESSION_SECRET)
    assert await repo.is_recent(120) is False
    args, kwargs = collection.find_one.await_args
    assert args[0]["_id"] == poller_health_key(FAKE_BOT_TOKEN, FAKE_SESSION_SECRET)
    assert args[0]["$expr"]["$and"] == [
        {"$gte": ["$observed_at", {"$subtract": ["$$NOW", 120_000]}]},
        {"$lte": ["$observed_at", {"$add": ["$$NOW", 5_000]}]},
    ]
    assert kwargs["max_time_ms"] == 2_000
    collection.find_one.return_value = {"_id": "opaque"}
    assert await repo.is_recent(120) is True
    with pytest.raises(ValueError):
        await repo.is_recent(0)


async def test_optional_schema_is_untouched_by_default_and_initialized_when_enabled():
    database = MagicMock()
    database.list_collection_names = AsyncMock(return_value=[])
    database.create_collection = AsyncMock()
    database.__getitem__.return_value.create_index = AsyncMock()
    await initialize_database(database)
    assert "summon_poller_heartbeats_v1" not in [
        call.args[0] for call in database.create_collection.await_args_list
    ]
    database.create_collection.reset_mock()
    await initialize_database(database, include_poller_health=True)
    assert "summon_poller_heartbeats_v1" in [
        call.args[0] for call in database.create_collection.await_args_list
    ]
    assert any(
        call.args[0] == [("expires_at", 1)] and call.kwargs.get("expireAfterSeconds") == 0
        for call in database.__getitem__.return_value.create_index.await_args_list
    )


async def test_watchdog_callback_only_follows_successful_get_updates_and_never_breaks_polling():
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    success = MagicMock()
    heartbeat = PollingHeartbeat(timeout_seconds=300, on_success=success)
    request = AsyncMock(return_value=[])
    await heartbeat(request, bot, GetMe())
    success.assert_not_called()
    assert await heartbeat(request, bot, GetUpdates()) == []
    success.assert_called_once_with()
    request.side_effect = OSError("synthetic failure")
    with pytest.raises(OSError):
        await heartbeat(request, bot, GetUpdates())
    assert success.call_count == 1
    request.side_effect = None
    success.side_effect = RuntimeError("private internal detail")
    assert await heartbeat(request, bot, GetUpdates()) == []
    await bot.session.close()


async def test_publisher_coalesces_busy_polling_and_keeps_original_observation_time():
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    first, second = asyncio.Event(), asyncio.Event()
    recorded = []

    async def record(value):
        recorded.append(value)
        (first if len(recorded) == 1 else second).set()

    publisher = PollerHealthPublisher(
        MagicMock(record=record), interval_seconds=0.15, now=lambda: now[0]
    )
    task = asyncio.create_task(publisher.run())
    try:
        await asyncio.sleep(0.03)
        assert recorded == []  # running the process alone must never publish health
        publisher.observe()
        await asyncio.wait_for(first.wait(), timeout=2)
        original = now[0]
        now[0] += timedelta(seconds=10)
        for _ in range(100):
            publisher.observe()
        await asyncio.wait_for(second.wait(), timeout=2)
        assert recorded == [original, now[0]]
        await asyncio.sleep(0.03)
        assert len(recorded) == 2  # no periodic tick without another successful poll
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_publisher_storage_failures_are_bounded_and_do_not_log_details(caplog):
    calls = asyncio.Event()

    async def record(_value):
        calls.set()
        raise OperationFailure("PRIVATE_MONITOR_INTERNAL_DETAIL")

    publisher = PollerHealthPublisher(MagicMock(record=record), interval_seconds=0.1)
    task = asyncio.create_task(publisher.run())
    try:
        publisher.observe()
        await asyncio.wait_for(calls.wait(), timeout=2)
        await asyncio.sleep(0)
        assert not task.done()
        assert "PRIVATE_MONITOR_INTERNAL_DETAIL" not in caplog.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_disabled_or_bad_monitor_tokens_are_404_without_any_database_access():
    disabled = create_app(settings=Settings(_env_file=None), store=object())
    with TestClient(disabled) as client:
        assert client.get("/health/poller").status_code == 404
    with patch("summon.api.app.PollerHealthRepository") as repository:
        with connected_monitor_client() as client:
            for token in (b"", b"wrong", b"\x80" * 40, b"x" * 200):
                result = client.get("/health/poller", headers=[(b"X-Summon-Monitor-Token", token)])
                assert result.status_code == 404
                assert result.headers["cache-control"] == "no-store"
            duplicated = client.get(
                "/health/poller",
                headers=[
                    ("X-Summon-Monitor-Token", FAKE_MONITOR_TOKEN),
                    ("X-Summon-Monitor-Token", "wrong"),
                ],
            )
            assert duplicated.status_code == 404
        repository.assert_not_called()


def test_authenticated_monitor_reports_recent_or_stale_without_secrets(caplog):
    fake = MagicMock(is_recent=AsyncMock(side_effect=[True, False]))
    with patch("summon.api.app.PollerHealthRepository", return_value=fake):
        with connected_monitor_client() as client:
            headers = {"X-Summon-Monitor-Token": FAKE_MONITOR_TOKEN}
            recent = client.get("/health/poller", headers=headers)
            stale = client.get("/health/poller", headers=headers)
    assert recent.status_code == 200 and recent.json() == {"status": "recent_poll"}
    assert stale.status_code == 503 and stale.json() == {"status": "no_recent_poll"}
    assert recent.headers["cache-control"] == stale.headers["cache-control"] == "no-store"
    assert FAKE_MONITOR_TOKEN not in recent.text + stale.text + caplog.text
    assert FAKE_BOT_TOKEN not in recent.text + stale.text + caplog.text
    assert fake.is_recent.await_count == 2
    fake.is_recent.assert_awaited_with(120)


@pytest.mark.parametrize(
    "error",
    [OperationFailure("PRIVATE_DATABASE_DETAIL"), TimeoutError(), OSError(), RuntimeError()],
)
def test_authenticated_monitor_fails_closed_on_storage_errors(error, caplog):
    fake = MagicMock(is_recent=AsyncMock(side_effect=error))
    with patch("summon.api.app.PollerHealthRepository", return_value=fake):
        with connected_monitor_client() as client:
            result = client.get(
                "/health/poller", headers={"X-Summon-Monitor-Token": FAKE_MONITOR_TOKEN}
            )
    assert result.status_code == 503 and result.json() == {"status": "unavailable"}
    assert "PRIVATE_DATABASE_DETAIL" not in result.text + caplog.text
    assert FAKE_MONITOR_TOKEN not in result.text + caplog.text


def test_preview_and_injected_monitor_never_claim_polling_health():
    settings = monitor_settings(preview_mode=True)
    app = create_app(settings=settings)
    headers = {"X-Summon-Monitor-Token": FAKE_MONITOR_TOKEN}
    with TestClient(app) as client:
        assert client.get("/health/poller", headers=headers).status_code == 503
    app = create_app(settings=monitor_settings(), store=object())
    with patch("summon.api.app.PollerHealthRepository") as repository:
        with TestClient(app) as client:
            app.state.database = object()
            assert client.get("/health/poller", headers=headers).status_code == 503
        repository.assert_not_called()


async def test_slow_write_does_not_refresh_an_old_observation_or_block_new_polls():
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    first_started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recorded = []

    async def record(value):
        recorded.append(value)
        if len(recorded) == 1:
            first_started.set()
            await release.wait()
        else:
            finished.set()

    publisher = PollerHealthPublisher(
        MagicMock(record=record), interval_seconds=0.03, now=lambda: now[0]
    )
    task = asyncio.create_task(publisher.run())
    try:
        publisher.observe()
        await asyncio.wait_for(first_started.wait(), 1)
        now[0] += timedelta(seconds=10)
        publisher.observe()
        observed_at = now[0]
        now[0] += timedelta(hours=1)  # the delayed write must not use this time
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        assert recorded == [datetime(2026, 1, 1, tzinfo=UTC), observed_at]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_hung_publication_times_out_and_later_poll_can_be_published():
    first_started, next_started = asyncio.Event(), asyncio.Event()
    calls = 0

    async def record(_value):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await asyncio.Event().wait()
        next_started.set()

    publisher = PollerHealthPublisher(
        MagicMock(record=record), interval_seconds=0.02, write_timeout=0.05
    )
    task = asyncio.create_task(publisher.run())
    try:
        publisher.observe()
        await asyncio.wait_for(first_started.wait(), 1)
        publisher.observe()
        await asyncio.wait_for(next_started.wait(), 1)
        assert calls == 2 and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_only_publishes_observed_poll_and_cleans_up_publisher(enabled, monkeypatch):
    from importlib import import_module

    bot_main = import_module("summon.bot.main")
    settings = Settings(
        bot_token=FAKE_BOT_TOKEN,
        session_secret=FAKE_SESSION_SECRET,
        poller_heartbeat_enabled=enabled,
        _env_file=None,
    )
    assert settings.poller_monitor_token.get_secret_value() == ""  # not needed by worker
    fake_client, fake_bot, fake_dispatcher = MagicMock(), MagicMock(), MagicMock()
    fake_client.close = AsyncMock()
    fake_bot.session.close = AsyncMock()
    fake_bot.set_my_commands = AsyncMock()
    fake_bot.delete_webhook = AsyncMock()
    recorded, cleaned = asyncio.Event(), asyncio.Event()

    async def record(_value):
        recorded.set()

    repo = MagicMock(record=record)

    class TrackingPublisher(PollerHealthPublisher):
        async def run(self):
            try:
                await super().run()
            finally:
                cleaned.set()

    async def supervised(dispatcher, bot, *, timeout_seconds, on_success):
        assert dispatcher is fake_dispatcher and bot is fake_bot and timeout_seconds == 300
        assert not recorded.is_set()
        if enabled:
            assert on_success is not None
            on_success()
            await asyncio.wait_for(recorded.wait(), 1)
        else:
            assert on_success is None

    monkeypatch.setattr(bot_main, "Settings", lambda: settings)
    monkeypatch.setattr(bot_main, "validate_runtime", lambda: None)
    monkeypatch.setattr(
        bot_main, "connect_database", AsyncMock(return_value=(fake_client, object()))
    )
    monkeypatch.setattr(bot_main, "Bot", lambda _token: fake_bot)
    monkeypatch.setattr(bot_main, "Dispatcher", lambda: fake_dispatcher)
    monkeypatch.setattr(bot_main, "Store", lambda *_args: object())
    monkeypatch.setattr(bot_main, "build_router", lambda *_args: object())
    monkeypatch.setattr(bot_main, "PollerHealthRepository", lambda *_args: repo)
    monkeypatch.setattr(bot_main, "PollerHealthPublisher", TrackingPublisher)
    monkeypatch.setattr(bot_main, "run_supervised_polling", supervised)
    await bot_main.run()
    assert recorded.is_set() is enabled
    assert cleaned.is_set() is enabled
    fake_bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    fake_bot.session.close.assert_awaited_once()
    fake_client.close.assert_awaited_once()


def test_worker_import_does_not_require_api_monitor_token(monkeypatch):
    import subprocess
    import sys

    monkeypatch.setenv("SUMMON_POLLER_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("SUMMON_BOT_TOKEN", FAKE_BOT_TOKEN)
    monkeypatch.setenv("SUMMON_SESSION_SECRET", FAKE_SESSION_SECRET)
    monkeypatch.delenv("SUMMON_POLLER_MONITOR_TOKEN", raising=False)
    result = subprocess.run(
        [sys.executable, "-c", "import summon.bot.main"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0


def test_monitor_fails_closed_if_repository_setup_itself_fails(caplog):
    with patch(
        "summon.api.app.PollerHealthRepository",
        side_effect=OperationFailure("PRIVATE_SETUP_DETAIL"),
    ):
        with connected_monitor_client() as client:
            result = client.get(
                "/health/poller", headers={"X-Summon-Monitor-Token": FAKE_MONITOR_TOKEN}
            )
    assert result.status_code == 503 and result.json() == {"status": "unavailable"}
    assert "PRIVATE_SETUP_DETAIL" not in result.text + caplog.text
