import asyncio
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pymongo.errors import AutoReconnect
from wzgram import Client

from summon.bot.handlers import COMMANDS, group_commands, register_handlers
from summon.config import Settings
from summon.rarities import RARITY_WEIGHTS


def make_store():
    return SimpleNamespace(
        collection=object(),
        catalog=SimpleNamespace(
            list=AsyncMock(return_value={"items": [], "total": 0}),
            inventory=AsyncMock(return_value={"items": [], "total": 0}),
            set_favorite=AsyncMock(return_value=True),
            spawn_weights=AsyncMock(return_value=dict(RARITY_WEIGHTS)),
        ),
        trades=object(),
        gifts=object(),
        groups=SimpleNamespace(db=object()),
        users=SimpleNamespace(
            progression=AsyncMock(return_value={"achievements": [], "daily": []}),
            collection_mode=AsyncMock(return_value="recent"),
            set_collection_mode=AsyncMock(),
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


async def test_hmode_shows_private_current_mode_without_writing():
    store = make_store()
    store.users.collection_mode.return_value = "copies"
    card = await invoke(store, "collection_display_mode")
    assert card.fields == (("Current mode", "copies"),)
    store.users.collection_mode.assert_awaited_once_with(42)
    store.users.set_collection_mode.assert_not_awaited()


@pytest.mark.parametrize(
    "argument,expected", [("recent", "recent"), ("copies", "copies"), ("fav", "favorites")]
)
async def test_hmode_saves_only_the_telegram_actors_preference(argument, expected):
    store = make_store()
    card = await invoke(store, "collection_display_mode", args=argument)
    actor, mode, now = store.users.set_collection_mode.await_args.args
    assert actor.id == 42 and mode == expected and now.tzinfo == UTC
    assert card.fields == (("Current mode", expected),)


@pytest.mark.parametrize("argument", ["other", "rarity", "recent extra"])
async def test_hmode_invalid_values_never_touch_storage(argument):
    store = make_store()
    card = await invoke(store, "collection_display_mode", args=argument)
    assert "/hmode" in card.body
    store.users.collection_mode.assert_not_awaited()
    store.users.set_collection_mode.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()


@pytest.mark.parametrize(
    "mode,expected_options",
    [("recent", {}), ("copies", {"sort": "copies"}), ("favorites", {"favorite": True})],
)
async def test_collection_uses_actor_scoped_display_preference(mode, expected_options):
    store = make_store()
    store.users.collection_mode.return_value = mode
    await invoke(store, "show_collection")
    store.users.collection_mode.assert_awaited_once_with(42)
    store.catalog.inventory.assert_awaited_once_with(42, limit=12, **expected_options)


async def test_hmode_database_error_is_safe_and_not_retried():
    store = make_store()
    store.users.set_collection_mode.side_effect = AutoReconnect("PRIVATE_MONGO_DETAIL")
    card = await invoke(store, "collection_display_mode", args="copies")
    assert store.users.set_collection_mode.await_count == 1
    assert "check /hmode" in card.body.lower()
    assert "PRIVATE_MONGO_DETAIL" not in card.body


async def test_hmode_rate_limit_prevents_storage_access():
    store = make_store()
    store.limiter.allow.return_value = False
    await invoke(store, "collection_display_mode", args="copies")
    store.users.set_collection_mode.assert_not_awaited()
    store.users.collection_mode.assert_not_awaited()


async def test_private_hmode_keyboard_has_native_emoji_and_actor_bound_actions():
    store = make_store()
    app = Client("mode-test", api_id=123, api_hash="test", bot_token="123:fake", in_memory=True)
    register_handlers(app, store, Settings(_env_file=None, premium_emoji_enabled=True))
    await asyncio.sleep(0)
    callback = next(
        handler.callback
        for handler in app.dispatcher.groups[0]
        if handler.callback.__name__ == "collection_display_mode"
    )
    message = group_message()
    message.chat = SimpleNamespace(id=42, type="private")
    message.text = "/hmode"
    message.command = ["hmode"]
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await callback(app, message)
    markup = send.await_args.kwargs["reply_markup"]
    buttons = markup.inline_keyboard[0]
    assert [button.callback_data for button in buttons] == [
        "hmode:42:recent",
        "hmode:42:copies",
        "hmode:42:favorites",
    ]
    assert all(button.icon_custom_emoji_id for button in buttons)
    assert "✓" in buttons[0].text


async def mode_callback(store, data, *, user_id=42, is_bot=False, edit=None):
    app = Client("mode-test", api_id=123, api_hash="test", bot_token="123:fake", in_memory=True)
    register_handlers(app, store, Settings(_env_file=None, premium_emoji_enabled=True))
    await asyncio.sleep(0)
    callback = next(
        handler.callback
        for handler in app.dispatcher.groups[0]
        if handler.callback.__name__ == "collection_mode_callback"
    )
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(
            id=user_id, first_name="Test", last_name=None, username=None, is_bot=is_bot
        ),
        message=SimpleNamespace(edit_reply_markup=edit or AsyncMock()),
        answer=AsyncMock(),
    )
    await callback(app, query)
    return query


async def test_hmode_callback_saves_actor_mode_and_updates_keyboard():
    store = make_store()
    query = await mode_callback(store, b"hmode:42:copies")
    actor, mode, now = store.users.set_collection_mode.await_args.args
    assert actor.id == 42 and mode == "copies" and now.tzinfo == UTC
    assert query.answer.await_count == 1
    assert "<tg-emoji" not in query.answer.await_args.args[0]
    buttons = query.message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert "✓" in buttons[1].text


@pytest.mark.parametrize(
    "data,user_id",
    [
        ("hmode:43:copies", 42),
        ("hmode:42:invalid", 42),
        ("hmode:x:copies", 42),
        ("hmode:42:copies:extra", 42),
    ],
)
async def test_hmode_callback_rejects_forged_or_invalid_actions(data, user_id):
    store = make_store()
    query = await mode_callback(store, data, user_id=user_id)
    store.limiter.allow.assert_not_awaited()
    store.users.set_collection_mode.assert_not_awaited()
    query.message.edit_reply_markup.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_hmode_callback_falls_back_after_definite_emoji_rejection():
    from wzgram.errors import BadRequest

    store = make_store()
    edit = AsyncMock(side_effect=[BadRequest("CUSTOM_EMOJI_INVALID"), True])
    await mode_callback(store, "hmode:42:favorites", edit=edit)
    assert store.users.set_collection_mode.await_count == 1
    assert edit.await_count == 2
    fallback = edit.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert all(button.icon_custom_emoji_id is None for button in fallback)
    assert [button.callback_data for button in fallback] == [
        "hmode:42:recent",
        "hmode:42:copies",
        "hmode:42:favorites",
    ]


async def test_hmode_callback_does_not_retry_ambiguous_telegram_edit():
    store = make_store()
    edit = AsyncMock(side_effect=TimeoutError("unknown result"))
    await mode_callback(store, "hmode:42:recent", edit=edit)
    assert store.users.set_collection_mode.await_count == 1
    assert edit.await_count == 1


async def test_chancelist_displays_current_weights_and_legacy_bucket():
    store = make_store()
    store.catalog.spawn_weights.return_value = dict(RARITY_WEIGHTS) | {"rare": 0}
    card = await invoke(store, "rarity_weights")
    store.catalog.spawn_weights.assert_awaited_once()
    assert ("Rare", "0") in card.fields
    assert "Legacy Epic shares the Special Edition bucket" in card.body


async def test_chancelist_fails_closed_when_the_weight_read_fails():
    store = make_store()
    store.catalog.spawn_weights.side_effect = AutoReconnect("PRIVATE_DATABASE_DETAIL")
    card = await invoke(store, "rarity_weights")
    assert card.title == "Collector tools unavailable"
    assert "PRIVATE_DATABASE_DETAIL" not in card.body
