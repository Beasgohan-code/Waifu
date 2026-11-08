"""Startup must not initialize schemas on a topology without transaction support."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymongo.errors import OperationFailure

from summon.config import Settings
from summon.database import connect_database


def client_for(hello, *, load_balanced=False):
    client = MagicMock()
    client.options = SimpleNamespace(load_balanced=load_balanced)
    client.admin.command = AsyncMock(side_effect=[{"ok": 1}, hello])
    client.close = AsyncMock()
    return client


def config():
    return Settings(mongo_uri="mongodb://example.invalid:27017", _env_file=None)


@pytest.mark.parametrize(
    "hello",
    [
        {
            "ok": 1,
            "isWritablePrimary": True,
            "maxWireVersion": 17,
            "logicalSessionTimeoutMinutes": 30,
        },
        {"ok": 1, "setName": "rs", "isWritablePrimary": True, "maxWireVersion": 17},
        {
            "ok": 1,
            "setName": "rs",
            "isWritablePrimary": False,
            "secondary": True,
            "maxWireVersion": 17,
            "logicalSessionTimeoutMinutes": 30,
        },
        {
            "ok": 1,
            "setName": "rs",
            "isWritablePrimary": True,
            "maxWireVersion": 8,
            "logicalSessionTimeoutMinutes": 30,
        },
        {"ok": 1, "msg": "isdbgrid", "maxWireVersion": 17, "logicalSessionTimeoutMinutes": None},
        {
            "ok": 1,
            "setName": "",
            "isWritablePrimary": True,
            "maxWireVersion": 17,
            "logicalSessionTimeoutMinutes": 30,
        },
    ],
    ids=[
        "standalone",
        "no-sessions",
        "secondary-only",
        "unsupported-wire",
        "mongos-no-sessions",
        "empty-set-name",
    ],
)
async def test_unsupported_topology_fails_before_any_schema_changes(hello):
    client = client_for(hello)
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
        pytest.raises(RuntimeError, match="transaction"),
    ):
        await connect_database(config())
    initialize.assert_not_awaited()
    client.close.assert_awaited_once()
    assert client.admin.command.await_args_list[0].args == ("ping",)
    assert client.admin.command.await_args_list[1].args == ("hello",)


@pytest.mark.parametrize(
    ("hello", "load_balanced"),
    [
        (
            {
                "ok": 1,
                "setName": "rs",
                "isWritablePrimary": True,
                "maxWireVersion": 17,
                "logicalSessionTimeoutMinutes": 30,
            },
            False,
        ),
        (
            {"ok": 1, "msg": "isdbgrid", "maxWireVersion": 17, "logicalSessionTimeoutMinutes": 30},
            False,
        ),
        # A post-handshake hello need not repeat the LB serviceId: PyMongo has
        # already required it in the initial loadBalanced handshake.
        ({"ok": 1, "maxWireVersion": 17, "logicalSessionTimeoutMinutes": 30}, True),
    ],
    ids=["replica-primary", "sharded-router", "load-balanced"],
)
async def test_supported_transaction_topologies_initialize_after_check(hello, load_balanced):
    client = client_for(hello, load_balanced=load_balanced)
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
    ):
        result, db = await connect_database(config())
    assert result is client
    initialize.assert_awaited_once_with(db, include_poller_health=False)
    client.close.assert_not_awaited()


async def test_legacy_hello_fallback_is_used_only_for_unknown_command():
    client = client_for(None)
    client.admin.command.side_effect = [
        {"ok": 1},
        OperationFailure("unknown command", code=59),
        {
            "ok": 1,
            "setName": "rs",
            "ismaster": True,
            "maxWireVersion": 9,
            "logicalSessionTimeoutMinutes": 30,
        },
    ]
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
    ):
        await connect_database(config())
    assert client.admin.command.await_args_list[2].args == ("isMaster",)
    initialize.assert_awaited_once()


async def test_hello_authentication_failure_never_falls_back_and_closes_client():
    client = client_for(None)
    client.admin.command.side_effect = [{"ok": 1}, OperationFailure("forbidden", code=13)]
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
        pytest.raises(OperationFailure),
    ):
        await connect_database(config())
    assert client.admin.command.await_count == 2
    initialize.assert_not_awaited()
    client.close.assert_awaited_once()


def test_readiness_rechecks_topology_and_does_not_claim_ready_for_standalone():
    from fastapi.testclient import TestClient

    from summon.api.app import create_app

    client = MagicMock()
    client.options = SimpleNamespace(load_balanced=False)
    client.admin.command = AsyncMock(
        return_value={
            "ok": 1,
            "isWritablePrimary": True,
            "maxWireVersion": 17,
            "logicalSessionTimeoutMinutes": 30,
        }
    )
    db = SimpleNamespace(client=client, command=AsyncMock(return_value={"ok": 1}))
    app = create_app(settings=Settings(_env_file=None), store=object())
    with TestClient(app) as http:
        app.state.database = db
        assert http.get("/health").status_code == 200
        response = http.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_topology_unsupported"
    assert "example.invalid" not in response.text
    client.admin.command.assert_awaited_once_with("hello")
    db.command.assert_awaited_once_with("ping")


def test_readiness_accepts_detected_primary_without_claiming_transaction_execution():
    from fastapi.testclient import TestClient

    from summon.api.app import create_app

    client = MagicMock()
    client.options = SimpleNamespace(load_balanced=False)
    client.admin.command = AsyncMock(
        return_value={
            "ok": 1,
            "setName": "rs",
            "isWritablePrimary": True,
            "maxWireVersion": 17,
            "logicalSessionTimeoutMinutes": 30,
        }
    )
    db = SimpleNamespace(client=client, command=AsyncMock(return_value={"ok": 1}))
    app = create_app(settings=Settings(_env_file=None), store=object())
    with TestClient(app) as http:
        app.state.database = db
        response = http.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    client.admin.command.assert_awaited_once_with("hello")


def test_readiness_redacts_hello_authorization_errors():
    from fastapi.testclient import TestClient

    from summon.api.app import create_app

    client = MagicMock()
    client.options = SimpleNamespace(load_balanced=False)
    client.admin.command = AsyncMock(side_effect=OperationFailure("internal detail", code=13))
    db = SimpleNamespace(client=client, command=AsyncMock(return_value={"ok": 1}))
    app = create_app(settings=Settings(_env_file=None), store=object())
    with TestClient(app) as http:
        app.state.database = db
        response = http.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "internal detail" not in response.text
    client.admin.command.assert_awaited_once_with("hello")


def test_preview_and_injected_readiness_remain_explicitly_non_database_results():
    from fastapi.testclient import TestClient

    from summon.api.app import create_app

    with TestClient(create_app(settings=Settings(preview_mode=True, _env_file=None))) as preview:
        assert preview.get("/health/ready").json() == {"status": "preview"}
    with TestClient(create_app(settings=Settings(_env_file=None), store=object())) as injected:
        assert injected.get("/health/ready").json() == {"status": "injected"}


async def test_enabled_polling_health_is_explicitly_forwarded_to_schema_initialization():
    client = client_for(
        {
            "ok": 1,
            "setName": "rs",
            "isWritablePrimary": True,
            "maxWireVersion": 17,
            "logicalSessionTimeoutMinutes": 30,
        }
    )
    settings = Settings(
        mongo_uri="mongodb://example.invalid:27017",
        bot_token="123456:TEST_TOKEN_NOT_REAL",
        session_secret="s" * 40,
        poller_heartbeat_enabled=True,
        _env_file=None,
    )
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
    ):
        _, db = await connect_database(settings)
    initialize.assert_awaited_once_with(db, include_poller_health=True)


@pytest.mark.parametrize("driver_error", ["selection", "authentication", "configuration"])
async def test_api_startup_redacts_mongo_driver_details(driver_error, caplog):
    from pymongo.errors import ConfigurationError, ServerSelectionTimeoutError

    from summon.api.app import create_app

    error_type = {
        "selection": ServerSelectionTimeoutError,
        "authentication": OperationFailure,
        "configuration": ConfigurationError,
    }[driver_error]
    error = error_type("PRIVATE_HOST PRIVATE_CONNECTION_STRING PRIVATE_PASSWORD")
    app = create_app(settings=Settings(_env_file=None))
    with patch("summon.api.app.connect_database", AsyncMock(side_effect=error)):
        async with app.router.lifespan_context(app):
            await app.state.startup_task
            assert app.state.store is None and app.state.database is None
    assert "MongoDB" in caplog.text and "PRIVATE_" not in caplog.text
    if driver_error == "selection":
        assert "outbound IP access list" in caplog.text
        assert "cannot be determined" in caplog.text


async def test_bot_startup_redacts_mongo_driver_details(monkeypatch):
    import traceback
    from importlib import import_module

    from pymongo.errors import ServerSelectionTimeoutError

    from summon.database import DatabaseStartupError

    module = import_module("summon.bot.main")
    settings = Settings(
        bot_token="123:synthetic",
        telegram_api_id=12345,
        telegram_api_hash="synthetic",
        webhooks_disabled_confirmed=True,
        _env_file=None,
    )
    monkeypatch.setattr(module, "Settings", lambda: settings)
    monkeypatch.setattr(module, "validate_runtime", lambda: None)
    monkeypatch.setattr(
        module,
        "connect_database",
        AsyncMock(side_effect=ServerSelectionTimeoutError("PRIVATE_HOST")),
    )
    with pytest.raises(DatabaseStartupError) as raised:
        await module.run()
    rendered = "".join(traceback.format_exception(raised.value))
    assert "outbound IP access list" in rendered
    assert "PRIVATE_HOST" not in rendered
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (
            "SSL handshake failed: PRIVATE_HOST TLSV1_ALERT_INTERNAL_ERROR PRIVATE_CREDENTIAL",
            "TLS handshake failed",
        ),
        (
            "SSL handshake failed: PRIVATE_HOST CERTIFICATE_VERIFY_FAILED PRIVATE_CREDENTIAL",
            "certificate verification failed",
        ),
        ("PRIVATE_HOST PRIVATE_CREDENTIAL", "server selection timed out"),
    ],
)
def test_mongo_startup_hints_are_specific_but_never_include_driver_details(error, hint):
    from pymongo.errors import ServerSelectionTimeoutError

    from summon.database import safe_startup_error

    message = str(safe_startup_error(ServerSelectionTimeoutError(error)))
    assert hint in message
    assert "PRIVATE_" not in message
    if "handshake failed" in hint:
        assert "outbound IP" in message and "cannot be determined" in message
        assert "do not disable TLS verification" in message


@pytest.mark.parametrize("code", [13, 18])
def test_mongo_startup_authentication_hint_never_reveals_server_message(code):
    from summon.database import safe_startup_error

    message = str(safe_startup_error(OperationFailure("PRIVATE_CREDENTIAL", code=code)))
    assert "authentication or authorization" in message
    assert "PRIVATE_CREDENTIAL" not in message


async def test_startup_retries_transient_selection_only_before_initialization(caplog):
    from unittest.mock import call

    from pymongo.errors import ServerSelectionTimeoutError

    hello = {
        "ok": 1,
        "setName": "rs",
        "isWritablePrimary": True,
        "maxWireVersion": 17,
        "logicalSessionTimeoutMinutes": 30,
    }
    error = ServerSelectionTimeoutError("SSL handshake failed PRIVATE_HOST PRIVATE_CREDENTIAL")
    client = client_for(hello)
    client.admin.command.side_effect = [error, error, {"ok": 1}, hello]
    with (
        patch("summon.database.AsyncMongoClient", return_value=client) as factory,
        patch("summon.database.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
    ):
        result, db = await connect_database(config())
    assert result is client
    assert client.admin.command.await_args_list == [
        call("ping"),
        call("ping"),
        call("ping"),
        call("hello"),
    ]
    assert sleep.await_args_list == [call(1), call(2)]
    initialize.assert_awaited_once_with(db, include_poller_health=False)
    client.close.assert_not_awaited()
    assert factory.call_args.kwargs["serverSelectionTimeoutMS"] == 5000
    assert "tlsAllowInvalidCertificates" not in factory.call_args.kwargs
    assert "tlsAllowInvalidHostnames" not in factory.call_args.kwargs
    assert "PRIVATE_" not in caplog.text
    assert "retrying the read-only ping" in caplog.text


async def test_api_ping_retries_with_capped_backoff_then_initializes_once(caplog):
    from unittest.mock import call

    from pymongo.errors import ServerSelectionTimeoutError

    hello = {
        "ok": 1,
        "setName": "rs",
        "isWritablePrimary": True,
        "maxWireVersion": 17,
        "logicalSessionTimeoutMinutes": 30,
    }
    error = ServerSelectionTimeoutError(
        "SSL handshake failed PRIVATE_HOST TLSV1_ALERT_INTERNAL_ERROR PRIVATE_CREDENTIAL"
    )
    client = client_for(hello)
    client.admin.command.side_effect = [error] * 11 + [{"ok": 1}, hello]
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
    ):
        result, db = await connect_database(config(), wait_until_available=True)
    assert result is client
    assert client.admin.command.await_args_list == [call("ping")] * 12 + [call("hello")]
    assert sleep.await_args_list == [
        call(delay) for delay in (1, 2, 4, 5, 5, 30, 60, 120, 300, 300, 300)
    ]
    initialize.assert_awaited_once_with(db, include_poller_health=False)
    client.close.assert_not_awaited()
    assert "TLS handshake failed" in caplog.text
    assert "PRIVATE_" not in caplog.text


async def test_startup_exhausts_bounded_selection_retries_and_closes(caplog):
    from unittest.mock import call

    from pymongo.errors import ServerSelectionTimeoutError

    client = client_for(None)
    client.admin.command.side_effect = ServerSelectionTimeoutError(
        "PRIVATE_HOST PRIVATE_CREDENTIAL"
    )
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
        pytest.raises(ServerSelectionTimeoutError),
    ):
        await connect_database(config())
    assert client.admin.command.await_args_list == [call("ping")] * 6
    assert sleep.await_args_list == [call(1), call(2), call(4), call(5), call(5)]
    initialize.assert_not_awaited()
    client.close.assert_awaited_once()
    assert "PRIVATE_" not in caplog.text


@pytest.mark.parametrize("wait_until_available", [False, True])
@pytest.mark.parametrize("failure", ["certificate", "authentication"])
async def test_startup_does_not_retry_definite_certificate_or_auth_failure(
    failure, wait_until_available
):
    from pymongo.errors import ServerSelectionTimeoutError

    client = client_for(None)
    client.admin.command.side_effect = (
        ServerSelectionTimeoutError("CERTIFICATE_VERIFY_FAILED PRIVATE_HOST")
        if failure == "certificate"
        else OperationFailure("PRIVATE_CREDENTIAL", code=18)
    )
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
        pytest.raises((ServerSelectionTimeoutError, OperationFailure)),
    ):
        await connect_database(config(), wait_until_available=wait_until_available)
    client.admin.command.assert_awaited_once_with("ping")
    sleep.assert_not_awaited()
    initialize.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.parametrize("wait_until_available", [False, True])
async def test_startup_never_retries_schema_initialization_after_selection_failure(
    wait_until_available,
):
    from pymongo.errors import ServerSelectionTimeoutError

    hello = {
        "ok": 1,
        "setName": "rs",
        "isWritablePrimary": True,
        "maxWireVersion": 17,
        "logicalSessionTimeoutMinutes": 30,
    }
    client = client_for(hello)
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch(
            "summon.database.initialize_database",
            new_callable=AsyncMock,
            side_effect=ServerSelectionTimeoutError("PRIVATE_HOST"),
        ) as initialize,
        pytest.raises(ServerSelectionTimeoutError),
    ):
        await connect_database(config(), wait_until_available=wait_until_available)
    assert client.admin.command.await_count == 2
    initialize.assert_awaited_once()
    sleep.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.parametrize("wait_until_available", [False, True])
async def test_cancelled_startup_retry_closes_mongo_client(wait_until_available):
    import asyncio

    from pymongo.errors import ServerSelectionTimeoutError

    entered_sleep = asyncio.Event()
    never = asyncio.Event()

    async def wait_for_cancellation(_delay):
        entered_sleep.set()
        await never.wait()

    client = client_for(None)
    client.admin.command.side_effect = ServerSelectionTimeoutError("PRIVATE_HOST")
    with (
        patch("summon.database.AsyncMongoClient", return_value=client),
        patch("summon.database.asyncio.sleep", side_effect=wait_for_cancellation),
        patch("summon.database.initialize_database", new_callable=AsyncMock) as initialize,
    ):
        task = asyncio.create_task(
            connect_database(config(), wait_until_available=wait_until_available)
        )
        await asyncio.wait_for(entered_sleep.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    client.close.assert_awaited_once()
    initialize.assert_not_awaited()


async def test_api_is_live_unready_then_recovers_without_restart():
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from summon.api.app import create_app

    entered = asyncio.Event()
    release = asyncio.Event()
    mongo = MagicMock(close=AsyncMock())
    mongo.options = SimpleNamespace(load_balanced=False)
    mongo.admin.command = AsyncMock(
        return_value={
            "ok": 1,
            "setName": "rs",
            "isWritablePrimary": True,
            "maxWireVersion": 17,
            "logicalSessionTimeoutMinutes": 30,
        }
    )
    database = SimpleNamespace(client=mongo, command=AsyncMock(return_value={"ok": 1}))
    fake_store = object()

    async def connect(_settings, *, wait_until_available):
        assert wait_until_available is True
        entered.set()
        await release.wait()
        return mongo, database

    app = create_app(settings=Settings(allowed_origins=["https://app.example"], _env_file=None))
    with (
        patch("summon.api.app.connect_database", side_effect=connect) as connection,
        patch("summon.api.app.Store", return_value=fake_store) as store_factory,
    ):
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(entered.wait(), timeout=1)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as http:
                live = await http.get("/health")
                assert live.status_code == 200 and live.json()["status"] == "ok"
                assert live.headers["cache-control"] == "no-store"
                ready = await http.get("/health/ready")
                assert ready.status_code == 503 and ready.headers["cache-control"] == "no-store"
                assert (await http.get("/api/bootstrap")).status_code == 200
                api = await http.get("/api/me", headers={"Origin": "https://app.example"})
                assert api.status_code == 503
                assert api.json()["error"]["code"] == "database_unavailable"
                assert api.headers["access-control-allow-origin"] == "https://app.example"
                assert api.headers["cache-control"] == "no-store"
                assert api.headers["x-content-type-options"] == "nosniff"
                assert api.headers["referrer-policy"] == "no-referrer"
                assert (await http.post("/api/auth/session")).status_code == 403
                protected = await http.post(
                    "/api/auth/session",
                    headers={"X-Summon-Request": "1", "Origin": "https://app.example"},
                    json={"init_data": "synthetic"},
                )
                assert protected.status_code == 503
                preflight = await http.options(
                    "/api/auth/session",
                    headers={
                        "Origin": "https://app.example",
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "X-Summon-Request",
                    },
                )
                assert preflight.status_code == 200
                assert app.state.store is None
                release.set()
                await asyncio.wait_for(app.state.startup_task, timeout=1)
                assert app.state.store is fake_store
                ready = await http.get("/health/ready")
                assert ready.status_code == 200 and ready.json() == {"status": "ready"}
                assert (await http.get("/api/me")).status_code == 401
    connection.assert_awaited_once()
    store_factory.assert_called_once_with(mongo, database)
    mongo.close.assert_awaited_once()


async def test_api_shutdown_cancels_pending_database_initialization():
    import asyncio

    from summon.api.app import create_app

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def connect(_settings, *, wait_until_available):
        assert wait_until_available is True
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    app = create_app(settings=Settings(_env_file=None))
    with patch("summon.api.app.connect_database", side_effect=connect):
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(entered.wait(), timeout=1)
    assert cancelled.is_set()
    assert app.state.startup_task.cancelled()


def test_readiness_timeout_is_unavailable_not_a_telegram_error():
    from fastapi.testclient import TestClient

    from summon.api.app import create_app

    db = SimpleNamespace(command=AsyncMock(side_effect=TimeoutError()))
    app = create_app(settings=Settings(_env_file=None), store=object())
    with TestClient(app) as http:
        app.state.database = db
        response = http.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Database readiness check timed out"
