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
    initialize.assert_awaited_once_with(db)
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
