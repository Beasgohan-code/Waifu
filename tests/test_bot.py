"""Native Wzgram handler registration and selected ownership-safety flows."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from wzgram import Client
from wzgram.errors import BadRequest

from summon.bot.handlers import COMMANDS, group_commands, register_handlers
from summon.bot.session_storage import APIStorage
from summon.config import Settings


def make_store(**overrides):
    values = {
        "collection": object(),
        "catalog": object(),
        "trades": object(),
        "gifts": object(),
        "groups": SimpleNamespace(db=object()),
        "limiter": SimpleNamespace(allow=AsyncMock(return_value=True)),
    }
    return SimpleNamespace(**{**values, **overrides})


async def registered(store):
    client = Client(
        "offline-test", api_id=123, api_hash="test", bot_token="123:fake", in_memory=True
    )
    register_handlers(client, store, Settings(_env_file=None))
    await asyncio.sleep(0)
    return client


def handler(client, name):
    return next(
        item.callback for item in client.dispatcher.groups[0] if item.callback.__name__ == name
    )


def group_message(text, *, reply=None):
    return SimpleNamespace(
        text=text,
        caption=None,
        command=[text.split()[0][1:]],
        from_user=SimpleNamespace(
            id=42, is_bot=False, first_name="Test", last_name=None, username=None
        ),
        reply_to_message=reply,
        sender_chat=None,
        chat=SimpleNamespace(id=-100, type="supergroup"),
        message_thread_id=None,
        ephemeral_message_id=987,
    )


def test_command_menu_has_no_duplicate_commands():
    names = [command.command for command in COMMANDS]
    assert len(names) == len(set(names))
    assert {"start", "summon", "collection", "manage", "daily", "gift"} <= set(names)


async def test_api_wzgram_client_is_outbound_only_and_in_memory():
    from summon.api.app import create_app

    settings = Settings(
        bot_token="123:fake", telegram_api_id=12345, telegram_api_hash="hash", _env_file=None
    )
    mongo = MagicMock(close=AsyncMock())
    bot = MagicMock(stop=AsyncMock())
    app = create_app(settings=settings)
    with (
        patch("summon.api.app.connect_database", AsyncMock(return_value=(mongo, object()))),
        patch("summon.api.app.Store", return_value=object()),
        patch("summon.api.app.Client", return_value=bot) as factory,
        patch("summon.api.app.start_verified_client", AsyncMock()),
    ):
        async with app.router.lifespan_context(app):
            assert app.state.telegram_bot is bot
    assert factory.call_args.kwargs["no_updates"] is True
    storage = factory.call_args.kwargs["storage_engine"]
    assert isinstance(storage, APIStorage) and storage.in_memory is True
    bot.stop.assert_awaited_once()
    mongo.close.assert_awaited_once()


async def test_native_handlers_are_registered_once_with_unique_callbacks():
    app = await registered(make_store())
    callbacks = [item.callback.__name__ for item in app.dispatcher.groups[0]]
    assert len(callbacks) == len(set(callbacks))
    assert len(app.dispatcher.groups[-100]) == 2


async def test_gift_callback_uses_telegram_actor_and_rejects_non_recipient():
    from summon.services.gifting import GiftForbidden

    gifts = SimpleNamespace(
        confirm=AsyncMock(side_effect=GiftForbidden("Only the recipient can accept")),
        cancel=AsyncMock(),
    )
    app = await registered(make_store(gifts=gifts))
    callback = SimpleNamespace(
        data=f"gift:accept:{'a' * 32}",
        from_user=SimpleNamespace(id=42),
        message=None,
        answer=AsyncMock(),
    )
    await handler(app, "gift_callback")(app, callback)
    assert gifts.confirm.await_args.args[:2] == ("a" * 32, 42)
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.args[0].endswith("Only the recipient can accept")
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "<tg-emoji" not in callback.answer.await_args.args[0]


def test_runtime_rejects_shared_pyrogram_distribution_conflict(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    import pytest

    from summon.bot.compat import validate_runtime

    versions = {"wzgram": "3.1.6", "kurigram": "2.2.26"}

    def version(name):
        if name not in versions:
            raise PackageNotFoundError(name)
        return versions[name]

    monkeypatch.setattr("summon.bot.compat.metadata.version", version)
    with pytest.raises(RuntimeError, match="Kurigram"):
        validate_runtime()


async def test_actual_api_wzgram_client_has_no_updates_and_no_handlers():
    from summon.bot.compat import validate_runtime

    validate_runtime()
    storage = APIStorage(MagicMock())
    client = Client(
        "offline-api",
        api_id=12345,
        api_hash="hash",
        bot_token="123:fake",
        storage_engine=storage,
        no_updates=True,
    )
    assert client.no_updates is True
    assert storage.in_memory is True
    assert not client.dispatcher.groups


def test_group_menu_does_not_use_misleading_ephemeral_command_flag():
    # Wzgram's flag removes menu entries; it does not protect a response.
    assert all(item.ephemeral is None for item in group_commands())
    assert all(item.ephemeral is None for item in COMMANDS)


async def test_menu_registration_handles_native_rejection_without_repeated_writes():
    from summon.bot.main import register_command_menus

    bot = AsyncMock()
    bot.set_bot_commands.side_effect = [True, BadRequest("MENU_REJECTED")]
    await register_command_menus(bot)
    assert bot.set_bot_commands.await_count == 2


async def test_collection_passes_ephemeral_trigger_to_private_response():
    store = make_store(
        catalog=SimpleNamespace(inventory=AsyncMock(return_value={"items": [], "total": 0})),
        users=SimpleNamespace(touch=AsyncMock(), collection_mode=AsyncMock(return_value="recent")),
    )
    app = await registered(store)
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as send:
        await handler(app, "show_collection")(app, group_message("/collection"))
    assert send.await_args.kwargs["incoming_ephemeral_id"] == 987


async def test_exchange_callbacks_are_rate_limited_before_ownership_operations():
    gifts = SimpleNamespace(confirm=AsyncMock(), cancel=AsyncMock())
    limiter = SimpleNamespace(allow=AsyncMock(return_value=False))
    store = make_store(gifts=gifts, limiter=limiter)
    app = await registered(store)
    callback = SimpleNamespace(
        data=f"gift:accept:{'a' * 32}",
        from_user=SimpleNamespace(id=42),
        message=None,
        answer=AsyncMock(),
    )
    await handler(app, "gift_callback")(app, callback)
    gifts.confirm.assert_not_awaited()
    assert "too many" in callback.answer.await_args.args[0].lower()
    assert limiter.allow.await_args.args[0] == "user:42:gift"


async def test_trade_proposal_is_rate_limited_before_creating_offer():
    trades = SimpleNamespace(create=AsyncMock())
    limiter = SimpleNamespace(allow=AsyncMock(return_value=False))
    app = await registered(make_store(trades=trades, limiter=limiter))
    target = SimpleNamespace(from_user=SimpleNamespace(id=43, is_bot=False), sender_chat=None)
    message = group_message("/trade aiko ren", reply=target)
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as response:
        await handler(app, "propose_trade")(app, message)
    trades.create.assert_not_awaited()
    assert "too many" in response.await_args.args[3].body.lower()
    assert limiter.allow.await_args.args[0] == "user:42:trade"


async def bot_with_real_registration():
    """Create a native Wzgram dispatcher without connecting to Telegram."""
    from summon.bot.ingress import IngressLease

    class StubDatabase:
        def __getitem__(self, _key):
            return self

        def with_options(self, **_kwargs):
            return self

    app = Client(
        "offline-manifest", api_id=123, api_hash="test", bot_token="123:fake", in_memory=True
    )
    IngressLease(StubDatabase(), 123).register(app)
    register_handlers(app, make_store(), Settings(_env_file=None))
    await asyncio.sleep(0)
    return app


async def test_real_feature_modules_match_the_ordered_native_manifest():
    from summon.bot.registration import validate_handler_registration

    app = await bot_with_real_registration()
    validate_handler_registration(app)
    origins = {
        handler.callback.__name__: handler.callback.__module__
        for handler in app.dispatcher.groups[0]
    }
    assert origins["start"] == "summon.bot.modules.start"
    assert origins["search_characters"] == "summon.bot.modules.search"
    assert origins["show_favorites"] == "summon.bot.modules.favorites"
    assert origins["create_character"] == "summon.bot.modules.catalog_admin"
    assert origins["propose_trade"] == "summon.bot.modules.exchange"
    assert origins["group_activity"] == "summon.bot.modules.group_events"


@pytest.mark.parametrize(
    "change",
    [
        "missing-guard",
        "duplicate-guard",
        "missing-command",
        "reordered",
        "filter",
        "extra-group",
        "wrong-kind",
    ],
)
async def test_registration_fails_closed_for_incomplete_or_unexpected_handlers(change):
    from wzgram.handlers import MessageHandler

    from summon.bot.registration import validate_handler_registration

    app = await bot_with_real_registration()
    groups = app.dispatcher.groups
    if change == "missing-guard":
        groups[-1000].pop()
    elif change == "duplicate-guard":
        groups[-1000][0] = groups[-1000][1]  # The old count-only check would pass.
    elif change == "missing-command":
        groups[0].pop(0)
    elif change == "reordered":
        observer = next(h for h in groups[0] if h.callback.__name__ == "group_activity")
        groups[0].remove(observer)
        groups[0].insert(0, observer)
    elif change == "filter":
        groups[0][0].filters = None
    elif change == "extra-group":
        groups[100] = []
    elif change == "wrong-kind":
        callback = groups[-1000][-1]
        groups[-1000][-1] = MessageHandler(callback.callback, callback.filters)
    with pytest.raises(RuntimeError, match="handler"):
        validate_handler_registration(app)


def test_module_entrypoint_calls_the_same_worker_main():
    import runpy

    with patch("summon.bot.main.main") as main:
        runpy.run_module("summon.bot", run_name="__main__")
    main.assert_called_once_with()


async def test_worker_refuses_incomplete_registration_before_connecting(monkeypatch):
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
    mongo = SimpleNamespace(close=AsyncMock())
    bot = SimpleNamespace(
        dispatcher=SimpleNamespace(groups={-1000: [object(), object()], -100: [], 0: []}),
        is_connected=False,
        is_initialized=False,
        stop=AsyncMock(),
    )

    async def maintain():
        await asyncio.Event().wait()

    lease = SimpleNamespace(
        acquire=AsyncMock(),
        maintain=maintain,
        register=MagicMock(),
        valid=MagicMock(return_value=True),
        invalidate=MagicMock(),
        release=AsyncMock(),
    )
    start = AsyncMock()
    monkeypatch.setattr(module, "Settings", lambda: settings)
    monkeypatch.setattr(module, "validate_runtime", lambda: None)
    monkeypatch.setattr(module, "connect_database", AsyncMock(return_value=(mongo, object())))
    monkeypatch.setattr(module, "IngressLease", lambda *_args: lease)
    monkeypatch.setattr(module, "worker_storage", lambda *_args: object())
    monkeypatch.setattr(module, "Client", lambda *_args, **_kwargs: bot)
    monkeypatch.setattr(module, "Store", lambda *_args: object())
    monkeypatch.setattr(module, "register_handlers", lambda *_args: None)
    monkeypatch.setattr(module, "start_verified_client", start)
    with pytest.raises(RuntimeError, match="registration"):
        await module.run()
    start.assert_not_awaited()
    bot.stop.assert_awaited_once()
    lease.release.assert_awaited_once()
    mongo.close.assert_awaited_once()


async def test_api_lifespan_closes_mongo_if_store_initialization_fails():
    from summon.api.app import create_app

    mongo = MagicMock(close=AsyncMock())
    app = create_app(settings=Settings(_env_file=None))
    with (
        patch("summon.api.app.connect_database", AsyncMock(return_value=(mongo, object()))),
        patch("summon.api.app.Store", side_effect=RuntimeError("synthetic setup failure")),
        pytest.raises(RuntimeError, match="synthetic setup failure"),
    ):
        async with app.router.lifespan_context(app):
            pytest.fail("startup must not finish")
    mongo.close.assert_awaited_once()


async def test_api_lifespan_closes_resources_if_startup_is_cancelled():
    from summon.api.app import create_app

    settings = Settings(
        bot_token="123:fake", telegram_api_id=12345, telegram_api_hash="hash", _env_file=None
    )
    mongo = MagicMock(close=AsyncMock())
    bot = MagicMock(stop=AsyncMock())
    started = asyncio.Event()

    async def interrupted(*_args):
        started.set()
        await asyncio.Event().wait()

    app = create_app(settings=settings)

    async def boot():
        async with app.router.lifespan_context(app):
            pytest.fail("startup must not finish")

    with (
        patch("summon.api.app.connect_database", AsyncMock(return_value=(mongo, object()))),
        patch("summon.api.app.Store", return_value=object()),
        patch("summon.api.app.Client", return_value=bot),
        patch("summon.api.app.start_verified_client", side_effect=interrupted),
    ):
        task = asyncio.create_task(boot())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    bot.stop.assert_awaited_once()
    mongo.close.assert_awaited_once()


async def test_api_lifespan_closes_mongo_even_if_telegram_shutdown_fails():
    from summon.api.app import create_app

    settings = Settings(
        bot_token="123:fake", telegram_api_id=12345, telegram_api_hash="hash", _env_file=None
    )
    mongo = MagicMock(close=AsyncMock())
    bot = MagicMock(stop=AsyncMock(side_effect=RuntimeError("synthetic shutdown failure")))
    app = create_app(settings=settings)
    with (
        patch("summon.api.app.connect_database", AsyncMock(return_value=(mongo, object()))),
        patch("summon.api.app.Store", return_value=object()),
        patch("summon.api.app.Client", return_value=bot),
        patch("summon.api.app.start_verified_client", AsyncMock()),
        pytest.raises(RuntimeError, match="synthetic shutdown failure"),
    ):
        async with app.router.lifespan_context(app):
            assert app.state.telegram_bot is bot
    mongo.close.assert_awaited_once()


async def test_api_reports_telegram_unavailable_without_echoing_startup_error(caplog):
    from summon.api.app import create_app

    settings = Settings(
        bot_token="123:fake", telegram_api_id=12345, telegram_api_hash="hash", _env_file=None
    )
    mongo = MagicMock(close=AsyncMock())
    bot = MagicMock(stop=AsyncMock())
    app = create_app(settings=settings)
    with (
        patch("summon.api.app.connect_database", AsyncMock(return_value=(mongo, object()))),
        patch("summon.api.app.Store", return_value=object()),
        patch("summon.api.app.Client", return_value=bot),
        patch(
            "summon.api.app.start_verified_client",
            AsyncMock(side_effect=ValueError("PRIVATE_SENTINEL")),
        ),
    ):
        async with app.router.lifespan_context(app):
            assert app.state.telegram_bot is None
    bot.stop.assert_awaited_once()
    mongo.close.assert_awaited_once()
    assert "PRIVATE_SENTINEL" not in caplog.text
