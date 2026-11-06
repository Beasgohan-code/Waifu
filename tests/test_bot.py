from types import SimpleNamespace
from unittest.mock import patch

from summon.bot.handlers import COMMANDS, build_router
from summon.bot.main import create_outbound_client
from summon.config import Settings


def test_command_menu_has_no_duplicate_commands():
    names = [command.command for command in COMMANDS]
    assert len(names) == len(set(names))
    assert {"start", "summon", "collection", "manage", "daily", "gift"} <= set(names)


def test_wzgram_is_outbound_only_and_in_memory():
    settings = Settings(
        bot_token="123:fake",
        telegram_api_id=12345,
        telegram_api_hash="hash",
        rich_enabled=True,
        _env_file=None,
    )
    with patch("summon.bot.main.Client") as client:
        create_outbound_client(settings)
    assert client.call_args.kwargs["no_updates"] is True
    assert client.call_args.kwargs["in_memory"] is True


def test_router_is_created_once_with_no_duplicate_message_registration():
    router = build_router(
        store=SimpleNamespace(
            collection=object(),
            catalog=object(),
            trades=object(),
            gifts=object(),
            groups=SimpleNamespace(db=object()),
        ),
        settings=Settings(_env_file=None),
    )
    callbacks = [handler.callback.__name__ for handler in router.message.handlers]
    assert len(callbacks) == len(set(callbacks))


async def test_gift_callback_uses_telegram_actor_and_rejects_non_recipient():
    from unittest.mock import AsyncMock

    from summon.services.gifting import GiftForbidden

    gifts = SimpleNamespace(
        confirm=AsyncMock(side_effect=GiftForbidden("Only the recipient can accept")),
        cancel=AsyncMock(),
    )
    store = SimpleNamespace(
        collection=object(),
        catalog=object(),
        trades=object(),
        gifts=gifts,
        groups=SimpleNamespace(db=object()),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    router = build_router(store=store, settings=Settings(_env_file=None))
    handler = next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "gift_callback"
    )
    callback = SimpleNamespace(
        data=f"gift:accept:{'a' * 32}",
        from_user=SimpleNamespace(id=42),
        message=None,
        answer=AsyncMock(),
    )
    await handler(callback)
    assert gifts.confirm.await_args.args[:2] == ("a" * 32, 42)
    callback.answer.assert_awaited_once_with("Only the recipient can accept", show_alert=True)


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


async def test_actual_wzgram_client_has_no_updates_and_no_handlers():
    from summon.bot.compat import validate_runtime

    validate_runtime()
    settings = Settings(
        bot_token="123:fake",
        telegram_api_id=12345,
        telegram_api_hash="hash",
        rich_enabled=True,
        _env_file=None,
    )
    client = create_outbound_client(settings)
    assert client.no_updates is True
    assert client.in_memory is True
    assert not client.dispatcher.groups


def test_group_menu_marks_only_private_flows_as_ephemeral():
    from summon.bot.handlers import group_commands

    commands = {item.command: item for item in group_commands()}
    for name in ("collection", "profile", "daily", "manage"):
        assert commands[name].is_ephemeral is True
    for name in ("summon", "trade", "gift"):
        assert commands[name].is_ephemeral is None
    assert all(command.is_ephemeral is None for command in COMMANDS)


async def test_command_registration_falls_back_for_older_bot_api():
    from unittest.mock import AsyncMock

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SetMyCommands

    from summon.bot.main import register_command_menus

    bot = AsyncMock()
    bot.set_my_commands.side_effect = [
        True,
        TelegramBadRequest(method=SetMyCommands(commands=COMMANDS), message="unknown field"),
        True,
    ]
    await register_command_menus(bot)
    calls = bot.set_my_commands.await_args_list
    assert len(calls) == 3
    assert calls[1].kwargs["scope"].type == "all_group_chats"
    assert any(item.is_ephemeral for item in calls[1].args[0])
    assert all(item.is_ephemeral is None for item in calls[2].args[0])


async def test_collection_passes_ephemeral_trigger_to_private_response():
    from unittest.mock import AsyncMock

    store = SimpleNamespace(
        collection=object(),
        catalog=SimpleNamespace(inventory=AsyncMock(return_value={"items": [], "total": 0})),
        trades=object(),
        gifts=object(),
        groups=SimpleNamespace(db=object()),
        users=SimpleNamespace(touch=AsyncMock()),
    )
    router = build_router(store=store, settings=Settings(_env_file=None))
    handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "show_collection"
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=42, is_bot=False, first_name="Test", last_name=None, username=None
        ),
        sender_chat=None,
        chat=SimpleNamespace(id=-100, type="supergroup"),
        message_thread_id=None,
        ephemeral_message_id=987,
        bot=AsyncMock(),
    )
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as send:
        await handler(message)
    assert send.await_args.kwargs["incoming_ephemeral_id"] == 987


async def test_exchange_callbacks_are_rate_limited_before_ownership_operations():
    from unittest.mock import AsyncMock

    gifts = SimpleNamespace(confirm=AsyncMock(), cancel=AsyncMock())
    store = SimpleNamespace(
        collection=object(),
        catalog=object(),
        trades=object(),
        gifts=gifts,
        groups=SimpleNamespace(db=object()),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=False)),
    )
    router = build_router(store=store, settings=Settings(_env_file=None))
    handler = next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "gift_callback"
    )
    callback = SimpleNamespace(
        data=f"gift:accept:{'a' * 32}",
        from_user=SimpleNamespace(id=42),
        message=None,
        answer=AsyncMock(),
    )
    await handler(callback)
    gifts.confirm.assert_not_awaited()
    assert "too many" in callback.answer.await_args.args[0].lower()
    assert store.limiter.allow.await_args.args[0] == "user:42:gift"


async def test_trade_proposal_is_rate_limited_before_creating_offer():
    from unittest.mock import AsyncMock

    trades = SimpleNamespace(create=AsyncMock())
    store = SimpleNamespace(
        collection=object(),
        catalog=object(),
        trades=trades,
        gifts=object(),
        groups=SimpleNamespace(db=object()),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=False)),
    )
    router = build_router(store=store, settings=Settings(_env_file=None))
    handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "propose_trade"
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=42, is_bot=False, first_name="Test", last_name=None, username=None
        ),
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=43, is_bot=False)),
        sender_chat=None,
        chat=SimpleNamespace(id=-100, type="supergroup"),
        message_thread_id=None,
        ephemeral_message_id=None,
        bot=AsyncMock(),
    )
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as response:
        await handler(message, SimpleNamespace(args="aiko ren"))
    trades.create.assert_not_awaited()
    assert "too many" in response.await_args.args[3].body.lower()
    assert store.limiter.allow.await_args.args[0] == "user:42:trade"
