import asyncio
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pymongo.errors import AutoReconnect
from wzgram import Client

from summon.bot.handlers import COMMANDS, group_commands, register_handlers
from summon.config import Settings


def make_store():
    return SimpleNamespace(
        collection=object(),
        catalog=SimpleNamespace(
            list=AsyncMock(return_value={"items": [], "total": 0}),
            inventory=AsyncMock(return_value={"items": [], "total": 0}),
            set_favorite=AsyncMock(return_value=True),
        ),
        trades=object(),
        gifts=object(),
        groups=SimpleNamespace(db=object()),
        users=SimpleNamespace(
            progression=AsyncMock(return_value={"achievements": [], "daily": []})
        ),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )


def group_message():
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=42, is_bot=False, first_name="Test", last_name=None, username=None
        ),
        sender_chat=None,
        chat=SimpleNamespace(id=-100, type="supergroup"),
        message_thread_id=None,
        ephemeral_message_id=987,
        bot=AsyncMock(),
        text="/test",
        caption=None,
        command=["test"],
    )


async def invoke(store, name, *, args=None, message=None):
    app = Client("offline-test", api_id=123, api_hash="test", bot_token="123:fake", in_memory=True)
    register_handlers(app, store, Settings(_env_file=None))
    await asyncio.sleep(0)  # Wzgram queues handler registration on the event loop.
    callback = next(
        handler.callback
        for handler in app.dispatcher.groups[0]
        if handler.callback.__name__ == name
    )
    message = message or group_message()
    command_name = {"search_characters": "search", "update_favorite": "favorite"}.get(name, name)
    message.command = [command_name]
    message.text = "/" + command_name + (" " + args if args else "")
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as send:
        await callback(app, message)
    if not send.await_count:
        return None
    assert send.await_args.kwargs["incoming_ephemeral_id"] == 987
    return send.await_args.args[3]


def test_collector_commands_are_unique_and_private_in_groups():
    names = [command.command for command in COMMANDS]
    assert len(names) == len(set(names))
    menu = {command.command: command for command in group_commands()}
    for name in ("search", "favorites", "favorite", "achievements"):
        assert menu[name].ephemeral is None


@pytest.mark.parametrize("args", [None, "", "x" * 81, "\x00"])
async def test_invalid_search_never_queries_the_catalog(args):
    store = make_store()
    card = await invoke(store, "search_characters", args=args)
    assert "search" in card.body.lower()
    store.catalog.list.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()


async def test_search_empty_results_use_the_telegram_actor():
    store = make_store()
    card = await invoke(store, "search_characters", args="Aiko")
    store.catalog.list.assert_awaited_once_with(42, search="Aiko", limit=10)
    assert "no matching" in card.body.lower()


async def test_empty_favorites_are_private_and_scoped_to_the_actor():
    store = make_store()
    card = await invoke(store, "show_favorites")
    store.catalog.inventory.assert_awaited_once_with(42, favorite=True, limit=10)
    assert "no favorites" in card.body.lower()


@pytest.mark.parametrize("args", [None, "", "aiko", "bad! on", "aiko maybe", "aiko on extra"])
async def test_invalid_favorite_input_never_writes(args):
    store = make_store()
    await invoke(store, "update_favorite", args=args)
    store.catalog.set_favorite.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()


async def test_favorite_uses_actor_and_rejects_unowned_character():
    store = make_store()
    store.catalog.set_favorite.return_value = False
    card = await invoke(store, "update_favorite", args="aiko on")
    store.catalog.set_favorite.assert_awaited_once_with(42, "aiko", True)
    assert "owned" in card.body.lower()


async def test_favorite_off_is_explicit_and_idempotent():
    store = make_store()
    card = await invoke(store, "update_favorite", args="aiko off")
    store.catalog.set_favorite.assert_awaited_once_with(42, "aiko", False)
    assert "removed" in card.body.lower()


async def test_rate_limit_prevents_favorite_write():
    store = make_store()
    store.limiter.allow.return_value = False
    card = await invoke(store, "update_favorite", args="aiko on")
    store.catalog.set_favorite.assert_not_awaited()
    assert "too many" in card.body.lower()


@pytest.mark.parametrize("identity", ["bot", "anonymous"])
async def test_non_user_or_bot_cannot_change_favorites(identity):
    store = make_store()
    message = group_message()
    if identity == "bot":
        message.from_user.is_bot = True
    else:
        message.sender_chat = object()
    assert await invoke(store, "update_favorite", args="aiko on", message=message) is None
    store.catalog.set_favorite.assert_not_awaited()


async def test_ambiguous_favorite_write_is_not_retried_or_exposed():
    store = make_store()
    store.catalog.set_favorite.side_effect = AutoReconnect("internal database detail")
    card = await invoke(store, "update_favorite", args="aiko on")
    assert store.catalog.set_favorite.await_count == 1
    assert "check /favorites" in card.body.lower()
    assert "internal database detail" not in card.body


async def test_achievements_reuse_progression_with_utc_server_time():
    store = make_store()
    store.users.progression.return_value = {
        "achievements": [
            {"title": "First encounter", "current": 1, "target": 1, "completed": True}
        ],
        "daily": [],
    }
    card = await invoke(store, "show_achievements")
    actor, timestamp = store.users.progression.await_args.args
    assert actor == 42 and timestamp.tzinfo == UTC
    assert card.fields == (("First encounter", "1 / 1 · Complete"),)
