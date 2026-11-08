"""Same-origin static assets and host-supplied API ports, without deployment."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from summon.api.app import create_app
from summon.api.run import main
from summon.config import Settings


def test_api_uses_platform_port_but_defaults_to_8000(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    with patch("summon.api.run.uvicorn.run") as run:
        main()
        assert run.call_args.kwargs["host"] == "0.0.0.0"  # noqa: S104 - deployment bind
        assert run.call_args.kwargs["port"] == 8000
        assert run.call_args.kwargs["access_log"] is False
        assert run.call_args.kwargs["workers"] == 1

    monkeypatch.setenv("PORT", "10000")
    with patch("summon.api.run.uvicorn.run") as run:
        main()
        assert run.call_args.kwargs["port"] == 10000


@pytest.mark.parametrize("port", ["", "SECRET_SENTINEL", "0", "65536", "-1"])
def test_invalid_platform_port_fails_without_echoing_its_value(monkeypatch, port):
    monkeypatch.setenv("PORT", port)
    with (
        patch("summon.api.run.uvicorn.run") as run,
        pytest.raises(ValueError, match="PORT") as error,
    ):
        main()
    assert "SECRET_SENTINEL" not in str(error.value)
    assert error.value.__cause__ is None
    run.assert_not_called()


def test_static_site_is_opt_in_and_keeps_api_routes_first(tmp_path):
    web = tmp_path / "site"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>SUMMON synthetic</title>")
    (web / "assets").mkdir()
    (web / "assets" / "bundle.js").write_text("console.log('synthetic');")
    (web / "art").mkdir()
    (web / "art" / "aiko.txt").write_text("sample art")
    (web / "api").mkdir()
    (web / "api" / "not-a-real-route").write_text("NOT_AN_API_RESPONSE")
    (tmp_path / "private.txt").write_text("NOT_PUBLIC_SENTINEL")
    (web / "link.txt").symlink_to(tmp_path / "private.txt")
    settings = Settings(preview_mode=True, web_dist=str(web), _env_file=None)
    with TestClient(create_app(settings=settings)) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "SUMMON synthetic" in root.text
        assert root.headers["x-content-type-options"] == "nosniff"
        assert client.get("/assets/bundle.js").status_code == 200
        assert client.get("/art/aiko.txt").text == "sample art"
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/bootstrap").json()["data"]["preview"] is True
        assert client.post("/api/auth/session", json={"init_data": "x"}).status_code == 403
        assert client.get("/api/not-a-real-route").status_code == 404
        assert client.get("/link.txt").status_code == 404
        assert "NOT_PUBLIC_SENTINEL" not in client.get("/%2e%2e/private.txt").text


def test_unconfigured_static_site_serves_backend_status_not_the_mini_app():
    settings = Settings(preview_mode=True, _env_file=None)
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["service"] == "SUMMON bot and API backend"
        assert response.json()["bot_status"] == "disabled"
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/health/bot").status_code == 503
        assert client.get("/api/bootstrap").status_code == 200


def test_backend_root_exposes_only_a_valid_public_deployment_revision(monkeypatch):
    app = create_app(settings=Settings(preview_mode=True, _env_file=None))
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a" * 40)
    with TestClient(app) as client:
        assert client.get("/").json()["revision"] == "a" * 40
        monkeypatch.setenv("RENDER_GIT_COMMIT", "PRIVATE_NOT_A_COMMIT")
        response = client.get("/")
        assert "revision" not in response.json() and "PRIVATE_NOT_A_COMMIT" not in response.text


def test_bad_static_directory_fails_early(tmp_path):
    settings = Settings(preview_mode=True, web_dist=str(tmp_path / "missing"), _env_file=None)
    with pytest.raises(RuntimeError):
        create_app(settings=settings)


async def test_combined_api_waits_for_database_then_starts_and_stops_one_bot_worker():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from httpx import ASGITransport, AsyncClient

    entered, release, bot_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def close_mongo():
        order.append("mongo_closed")

    mongo = SimpleNamespace(close=AsyncMock(side_effect=close_mongo))
    database = object()

    async def connect(_settings, *, wait_until_available):
        assert wait_until_available is True
        entered.set()
        await release.wait()
        return mongo, database

    async def bot_worker(_settings, client, db, status):
        assert client is mongo and db is database
        status.state = "running"
        bot_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append("bot_stopped")

    settings = Settings(run_bot_worker=True, _env_file=None)
    app = create_app(settings=settings, telegram_bot=object())
    with (
        patch("summon.api.app.connect_database", side_effect=connect),
        patch("summon.api.app.Store", return_value=object()),
        patch("summon.api.app.supervise_embedded_worker", side_effect=bot_worker) as supervise,
    ):
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(entered.wait(), 1)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as http:
                assert (await http.get("/health")).status_code == 200
                waiting = await http.get("/health/bot")
                assert waiting.status_code == 503
                assert waiting.json() == {"status": "waiting_for_database"}
                assert (await http.get("/")).json()["bot_status"] == "waiting_for_database"
                release.set()
                await asyncio.wait_for(bot_started.wait(), 1)
                started = await http.get("/health/bot")
                assert started.status_code == 200 and started.json() == {"status": "running"}
                assert (await http.get("/")).json()["bot_status"] == "running"
        supervise.assert_awaited_once()
    assert order == ["bot_stopped", "mongo_closed"]
    mongo.close.assert_awaited_once()


@pytest.mark.parametrize("injected", [False, True])
def test_preview_never_starts_embedded_bot_even_when_image_default_is_enabled(injected):
    from unittest.mock import AsyncMock

    settings = Settings(preview_mode=True, run_bot_worker=True, _env_file=None)
    app = create_app(settings=settings, store=object() if injected else None)
    with (
        patch("summon.api.app.supervise_embedded_worker", new_callable=AsyncMock) as supervise,
        TestClient(app) as client,
    ):
        response = client.get("/health/bot")
        assert response.status_code == 503 and response.json() == {"status": "disabled"}
    supervise.assert_not_awaited()


async def test_combined_api_reports_fatal_database_startup_without_starting_bot(caplog):
    from unittest.mock import AsyncMock

    from httpx import ASGITransport, AsyncClient
    from pymongo.errors import OperationFailure

    settings = Settings(run_bot_worker=True, _env_file=None)
    app = create_app(settings=settings)
    error = OperationFailure("PRIVATE_DATABASE_DETAIL", code=18)
    with (
        patch("summon.api.app.connect_database", AsyncMock(side_effect=error)),
        patch("summon.api.app.supervise_embedded_worker", new_callable=AsyncMock) as supervise,
    ):
        async with app.router.lifespan_context(app):
            await app.state.startup_task
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as http:
                assert (await http.get("/health")).status_code == 200
                assert (await http.get("/health/ready")).status_code == 503
                bot = await http.get("/health/bot")
                assert bot.status_code == 503 and bot.json() == {"status": "database_unavailable"}
    supervise.assert_not_awaited()
    assert "PRIVATE_DATABASE_DETAIL" not in caplog.text


def test_combined_server_sigterm_stops_native_worker_before_shared_mongo(tmp_path):
    """Real Uvicorn/socket/signals with synthetic Mongo and Telegram, not live services."""
    import json
    import os
    import signal
    import socket
    import subprocess
    import sys
    import textwrap
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    if os.name != "posix":
        pytest.skip("The Render shutdown test requires POSIX signals")
    script = textwrap.dedent(
        """
        import asyncio, os
        from contextlib import ExitStack
        from importlib import import_module
        from unittest.mock import AsyncMock, MagicMock, patch
        import uvicorn
        from summon.config import Settings

        api = import_module('summon.api.app')
        worker = import_module('summon.bot.main')
        settings = Settings(bot_token='123:synthetic', telegram_api_id=123,
                            telegram_api_hash='synthetic', webhooks_disabled_confirmed=True,
                            run_bot_worker=True, _env_file=None)

        async def close_mongo():
            print('SYNTHETIC mongo closed', flush=True)
        mongo = MagicMock(close=AsyncMock(side_effect=close_mongo))
        database = object()

        class Bot:
            is_connected = True
            is_initialized = True
            async def stop(self):
                self.is_connected = False
                self.is_initialized = False
                print('SYNTHETIC bot stopped', flush=True)

        class Lease:
            def __init__(self, *_args): pass
            async def acquire(self): pass
            async def maintain(self): await asyncio.Event().wait()
            def register(self, _bot): pass
            def valid(self): return True
            def invalidate(self): pass
            async def release(self): pass

        async def forever(*_args, **_kwargs):
            await asyncio.Event().wait()

        factory = MagicMock(return_value=Bot())
        app = api.create_app(settings=settings, telegram_bot=object())
        patches = [
            (api, 'connect_database', AsyncMock(return_value=(mongo, database))),
            (api, 'Store', MagicMock(return_value=object())),
            (worker, 'IngressLease', Lease), (worker, 'Client', factory),
            (worker, 'worker_storage', MagicMock(return_value=object())),
            (worker, 'Store', MagicMock(return_value=object())),
            (worker, 'validate_runtime', MagicMock()),
            (worker, 'register_handlers', MagicMock()),
            (worker, 'validate_handler_registration', MagicMock()),
            (worker, 'start_verified_client', AsyncMock()),
            (worker, 'register_command_menus', AsyncMock()),
            (worker, 'monitor_connection', forever),
        ]
        with ExitStack() as stack:
            for obj, name, replacement in patches:
                stack.enter_context(patch.object(obj, name, replacement))
            uvicorn.run(app, host='127.0.0.1', port=int(os.environ['TEST_PORT']),
                        access_log=False)
        """
    )
    sock = socket.socket()
    with sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONUNBUFFERED": "1",
        "TEST_PORT": str(port),
    }
    # The interpreter and script are fixed test code, not user-controlled input.
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        running = False
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/bot", timeout=0.5
                ) as response:
                    running = json.load(response)["status"] == "running"
                if running:
                    break
            except (OSError, urllib.error.HTTPError):
                pass
            time.sleep(0.05)
        assert running, "Synthetic worker did not complete startup"
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
        # Uvicorn can re-raise the received signal after graceful shutdown.
        assert process.returncode in (0, -signal.SIGTERM), output
        assert "SYNTHETIC bot stopped" in output and "SYNTHETIC mongo closed" in output
        assert output.index("SYNTHETIC bot stopped") < output.index("SYNTHETIC mongo closed")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
