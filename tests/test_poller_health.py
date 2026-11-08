"""External MTProto connection observation with synthetic credentials and storage."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pymongo import ReadPreference
from pymongo.errors import OperationFailure
from wzgram import raw

from summon.api.app import create_app
from summon.bot.watchdog import monitor_connection
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
    assert key.startswith("mtproto:") and len(key) == 72
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
    assert recent.status_code == 200 and recent.json() == {"status": "recent_connection"}
    assert stale.status_code == 503 and stale.json() == {"status": "no_recent_connection"}
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


async def test_connection_observation_follows_only_successful_native_rpc(caplog):
    observed = asyncio.Event()
    times = []

    async def record(value):
        times.append(value)
        observed.set()

    client = SimpleNamespace(
        is_connected=True,
        is_initialized=True,
        invoke=AsyncMock(side_effect=[OSError("PRIVATE_NETWORK_DETAIL"), object()]),
    )
    lease = SimpleNamespace(valid=MagicMock(return_value=True))
    repository = SimpleNamespace(record=record)
    task = asyncio.create_task(
        monitor_connection(
            client, lease, timeout_seconds=1, interval_seconds=0.01, health_repository=repository
        )
    )
    try:
        await asyncio.wait_for(observed.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert client.invoke.await_count == 2
    assert isinstance(client.invoke.await_args_list[0].args[0], raw.functions.updates.GetState)
    assert len(times) == 1 and times[0].tzinfo == UTC
    assert "PRIVATE_NETWORK_DETAIL" not in caplog.text


async def test_slow_publication_is_serialized_and_keeps_original_observation_time():
    started, release, next_check = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observations = []
    calls = 0

    async def invoke(_request):
        nonlocal calls
        calls += 1
        if calls == 2:
            next_check.set()

    async def record(value):
        observations.append(value)
        if len(observations) == 1:
            started.set()
            await release.wait()

    client = SimpleNamespace(is_connected=True, is_initialized=True, invoke=invoke)
    lease = SimpleNamespace(valid=lambda: True)
    task = asyncio.create_task(
        monitor_connection(
            client,
            lease,
            timeout_seconds=1,
            interval_seconds=0.01,
            health_repository=SimpleNamespace(record=record),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0.03)
        assert calls == 1  # Publication is bounded but sequential, not a second worker.
        release_at = datetime.now(UTC)
        release.set()
        await asyncio.wait_for(next_check.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert observations[0] <= release_at


async def test_failed_health_write_is_sanitized_and_does_not_stop_connection_monitor(caplog):
    second_check = asyncio.Event()
    calls = 0

    async def invoke(_request):
        nonlocal calls
        calls += 1
        if calls == 2:
            second_check.set()

    repository = SimpleNamespace(
        record=AsyncMock(side_effect=OperationFailure("PRIVATE_DATABASE_DETAIL"))
    )
    client = SimpleNamespace(is_connected=True, is_initialized=True, invoke=invoke)
    task = asyncio.create_task(
        monitor_connection(
            client,
            SimpleNamespace(valid=lambda: True),
            timeout_seconds=1,
            interval_seconds=0.01,
            health_repository=repository,
        )
    )
    try:
        await asyncio.wait_for(second_check.wait(), 1)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert repository.record.await_count >= 1
    assert "PRIVATE_DATABASE_DETAIL" not in caplog.text


async def test_hung_health_write_times_out_and_later_checks_resume():
    first_write, second_check = asyncio.Event(), asyncio.Event()
    calls = 0

    async def invoke(_request):
        nonlocal calls
        calls += 1
        if calls == 2:
            second_check.set()

    async def record(_value):
        first_write.set()
        await asyncio.Event().wait()

    client = SimpleNamespace(is_connected=True, is_initialized=True, invoke=invoke)
    task = asyncio.create_task(
        monitor_connection(
            client,
            SimpleNamespace(valid=lambda: True),
            timeout_seconds=10,
            interval_seconds=0.01,
            health_repository=SimpleNamespace(record=record),
        )
    )
    try:
        await asyncio.wait_for(first_write.wait(), 1)
        await asyncio.wait_for(second_check.wait(), 4)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_passes_optional_health_repository_and_cleans_up(enabled, monkeypatch):
    from importlib import import_module

    bot_main = import_module("summon.bot.main")
    settings = Settings(
        bot_token=FAKE_BOT_TOKEN,
        session_secret=FAKE_SESSION_SECRET,
        telegram_api_id=123,
        telegram_api_hash="fake",
        webhooks_disabled_confirmed=True,
        poller_heartbeat_enabled=enabled,
        _env_file=None,
    )
    assert not settings.poller_monitor_token.get_secret_value()
    mongo = SimpleNamespace(close=AsyncMock())
    groups = {-1000: [object()] * 3, -100: [object()] * 2, 0: [object()] * 40}
    bot = SimpleNamespace(
        is_connected=True,
        is_initialized=True,
        dispatcher=SimpleNamespace(groups=groups),
        stop=AsyncMock(),
    )

    async def stop():
        bot.is_connected = bot.is_initialized = False

    bot.stop.side_effect = stop
    lease = SimpleNamespace(
        acquire=AsyncMock(),
        maintain=AsyncMock(side_effect=lambda: asyncio.Event().wait()),
        register=MagicMock(),
        valid=MagicMock(return_value=True),
        invalidate=MagicMock(),
        release=AsyncMock(),
    )

    # AsyncMock's side-effect must itself be async to block until cancellation.
    async def maintain():
        await asyncio.Event().wait()

    lease.maintain = AsyncMock(side_effect=maintain)
    repository = SimpleNamespace(record=AsyncMock())
    started = asyncio.Event()

    async def monitor(client, coordination, *, timeout_seconds, health_repository):
        assert client is bot and coordination is lease and timeout_seconds == 300
        assert (health_repository is repository) is enabled
        started.set()
        await asyncio.Event().wait()

    async def idle():
        await started.wait()

    monkeypatch.setattr(bot_main, "Settings", lambda: settings)
    monkeypatch.setattr(bot_main, "validate_runtime", lambda: None)
    monkeypatch.setattr(bot_main, "connect_database", AsyncMock(return_value=(mongo, object())))
    monkeypatch.setattr(bot_main, "IngressLease", lambda *_args: lease)
    monkeypatch.setattr(bot_main, "worker_storage", lambda *_args: object())
    monkeypatch.setattr(bot_main, "Client", lambda *_args, **_kwargs: bot)
    monkeypatch.setattr(bot_main, "Store", lambda *_args: object())
    monkeypatch.setattr(bot_main, "register_handlers", lambda *_args: None)
    # This synthetic worker isolates heartbeat behavior, not handler registration.
    monkeypatch.setattr(bot_main, "validate_handler_registration", lambda *_args: None)
    monkeypatch.setattr(bot_main, "start_verified_client", AsyncMock())
    monkeypatch.setattr(bot_main, "register_command_menus", AsyncMock())
    monkeypatch.setattr(bot_main, "PollerHealthRepository", lambda *_args: repository)
    monkeypatch.setattr(bot_main, "monitor_connection", monitor)
    monkeypatch.setattr(bot_main, "idle", idle)
    await bot_main.run()
    bot.stop.assert_awaited_once()
    lease.release.assert_awaited_once()
    mongo.close.assert_awaited_once()
