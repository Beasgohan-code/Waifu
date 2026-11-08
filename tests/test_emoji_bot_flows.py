"""The supplied IDs are used in real bot responses, not only in a catalogue."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from wzgram import Client

from summon.bot.handlers import register_handlers
from summon.bot.presentation import Emoji
from summon.config import Settings
from summon.repositories.users import DailyAlreadyClaimed


def make_store():
    return SimpleNamespace(
        collection=object(),
        catalog=SimpleNamespace(inventory=AsyncMock(return_value={"items": [], "total": 0})),
        users=SimpleNamespace(
            touch=AsyncMock(),
            collection_mode=AsyncMock(return_value="recent"),
            profile=AsyncMock(
                return_value={
                    "level": 1,
                    "points": 2,
                    "unique_characters": 0,
                    "catalog_size": 3,
                    "catch_count": 0,
                    "best_streak": 3,
                }
            ),
            collection_insights=AsyncMock(
                return_value={
                    "active_owned": 0,
                    "catalog_size": 3,
                    "total_copies": 0,
                    "duplicate_copies": 0,
                    "favorite_characters": 0,
                    "rarity_breakdown": [],
                    "goals": [],
                }
            ),
            daily=AsyncMock(return_value={"points": 100, "streak": 1}),
            leaderboard=AsyncMock(return_value=[]),
        ),
        trades=SimpleNamespace(create=AsyncMock(return_value={"id": "a" * 32})),
        gifts=SimpleNamespace(create=AsyncMock(return_value={"id": "b" * 32})),
        groups=SimpleNamespace(db=object()),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )


def make_message(chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=42, is_bot=False, first_name="Mika", last_name=None, username=None
        ),
        sender_chat=None,
        chat=SimpleNamespace(
            id=42 if chat_type == "private" else -100, type=chat_type, title="Test"
        ),
        reply_to_message=SimpleNamespace(
            from_user=SimpleNamespace(id=43, is_bot=False, first_name="Ren")
        ),
        message_thread_id=None,
        ephemeral_message_id=None,
        bot=AsyncMock(),
        text="/test",
        caption=None,
        command=["test"],
    )


async def native_app(store, settings):
    app = Client("offline-test", api_id=123, api_hash="test", bot_token="123:fake", in_memory=True)
    register_handlers(app, store, settings)
    await asyncio.sleep(0)
    return app


def find_handler(app, name):
    callback = next(
        item.callback for item in app.dispatcher.groups[0] if item.callback.__name__ == name
    )

    async def invoke(message, args=None):
        command = {"propose_trade": "trade", "propose_gift": "gift", "guess": "summon"}.get(
            name, name
        )
        message.command = [command]
        argument = getattr(args, "args", None)
        message.text = "/" + command + (" " + argument if argument else "")
        return await callback(app, message)

    return invoke


@pytest.mark.parametrize(
    ("handler_name", "role"),
    [
        ("help_command", "QUESTION"),
        ("show_collection", "DIAMOND"),
        ("daily", "COIN"),
        ("leaderboard", "CROWN"),
        ("profile", "STATS"),
        ("manage", "SHIELD"),
    ],
)
async def test_real_command_cards_choose_meaningful_user_emoji(handler_name, role):
    router = await native_app(make_store(), Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, handler_name)(make_message())
    card = send.await_args.args[2]
    assert card.emoji is Emoji[role]
    assert card.emoji.value[1] in card.telegram_html()


@pytest.mark.parametrize("premium", [True, False])
async def test_start_button_has_user_icon_or_unicode_and_preserves_webapp(premium):
    settings = Settings(
        webapp_url="https://miniapp.example", premium_emoji_enabled=premium, _env_file=None
    )
    router = await native_app(make_store(), settings)
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, "start")(make_message())
    button = send.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.web_app.url == "https://miniapp.example"
    assert button.icon_custom_emoji_id == (Emoji.OPEN_COLLECTION.value[1] if premium else None)
    assert button.text == ("Open the collection" if premium else "💎 Open the collection")
    assert send.await_args.kwargs["premium"] is premium


@pytest.mark.parametrize("premium", [True, False])
@pytest.mark.parametrize(
    ("handler_name", "arguments", "role", "accept_role", "prefix"),
    [
        ("propose_trade", "aiko ren", "TRADE", "CHECK", "trade"),
        ("propose_gift", "aiko", "GIFT", "GIFT_ACTION", "gift"),
    ],
)
async def test_exchange_icons_keep_callbacks_and_have_unicode_when_disabled(
    premium, handler_name, arguments, role, accept_role, prefix
):
    settings = Settings(premium_emoji_enabled=premium, _env_file=None)
    router = await native_app(make_store(), settings)
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, handler_name)(
            make_message("supergroup"), SimpleNamespace(args=arguments)
        )
    assert send.await_args.args[2].emoji is Emoji[role]
    accept, cancel = send.await_args.kwargs["reply_markup"].inline_keyboard[0]
    accept_emoji = Emoji[accept_role]
    assert accept.callback_data.startswith(f"{prefix}:accept:")
    assert cancel.callback_data.startswith(f"{prefix}:cancel:")
    assert accept.icon_custom_emoji_id == (accept_emoji.value[1] if premium else None)
    assert cancel.icon_custom_emoji_id == (Emoji.CANCEL.value[1] if premium else None)
    if not premium:
        assert accept.text.startswith(accept_emoji.value[0] + " ")
        assert cancel.text == f"{Emoji.CANCEL.value[0]} Cancel"


async def test_group_spawn_configuration_guidance_uses_user_settings_emoji():
    with (
        patch("summon.bot.handlers.require_group_admin", new_callable=AsyncMock),
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as send,
    ):
        router = await native_app(make_store(), Settings(_env_file=None))
        await find_handler(router, "spawns")(
            make_message("supergroup"), SimpleNamespace(args="bad")
        )
    assert send.await_args.args[3].emoji is Emoji.GEAR


async def test_already_claimed_daily_uses_hourglass():
    store = make_store()
    store.users.daily.side_effect = DailyAlreadyClaimed()
    router = await native_app(store, Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, "daily")(make_message())
    assert send.await_args.args[2].emoji is Emoji.HOURGLASS


async def test_guess_guidance_uses_search_icon():
    router = await native_app(make_store(), Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, "guess")(make_message(), SimpleNamespace(args=None))
    assert send.await_args.args[2].emoji is Emoji.SEARCH


async def test_start_uses_new_summon_brand_and_collection_button():
    router = await native_app(
        make_store(), Settings(webapp_url="https://miniapp.example", _env_file=None)
    )
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, "start")(make_message())
    card = send.await_args.args[2]
    button = send.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert card.emoji is Emoji.SUMMON_BRAND
    assert button.icon_custom_emoji_id == Emoji.OPEN_COLLECTION.value[1]
    assert button.web_app.url == "https://miniapp.example"


@pytest.mark.parametrize(
    ("handler_name", "expected"),
    [
        (
            "profile",
            (
                "RARITY_STAR",
                "REWARD_COIN",
                "OPEN_COLLECTION",
                "OPEN_COLLECTION",
                "OPEN_COLLECTION",
                "HEART",
                "SPARK",
                "HOURGLASS",
                "STAR",
            ),
        ),
        ("daily", ("REWARD_COIN", "RARITY_STAR", "SPARK")),
    ],
)
async def test_profile_and_reward_fields_use_semantic_icons(handler_name, expected):
    router = await native_app(make_store(), Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, handler_name)(make_message())
    card = send.await_args.args[2]
    assert card.field_icons == tuple(Emoji[name] for name in expected)
    assert len(card.field_icons) == len(card.fields)


async def test_group_rules_use_ios_document_icon():
    store = make_store()
    store.groups.get = AsyncMock(return_value={"settings": {"rules": "Be kind"}})
    router = await native_app(store, Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, "rules")(make_message("supergroup"))
    assert send.await_args.args[2].emoji is Emoji.RULES


async def test_start_includes_group_invitation_and_actor_bound_guide():
    from wzgram.types import User

    app = await native_app(
        make_store(), Settings(webapp_url="https://miniapp.example", _env_file=None)
    )
    app.me = User(id=123, is_bot=True, username="summon_test", first_name="Bot")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(app, "start")(make_message())
    rows = send.await_args.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].web_app.url == "https://miniapp.example"
    assert rows[1][0].url == "https://t.me/summon_test?startgroup=true"
    assert rows[2][0].callback_data == "start-guide:42"
    assert rows[1][0].icon_custom_emoji_id == Emoji.SPARK.custom_id


async def test_start_guide_rejects_another_actor_before_sending():
    app = await native_app(make_store(), Settings(_env_file=None))
    callback = SimpleNamespace(
        from_user=SimpleNamespace(
            id=43, is_bot=False, first_name="Other", last_name=None, username=None
        ),
        data="start-guide:42",
        message=SimpleNamespace(chat=SimpleNamespace(id=43, type="private")),
        answer=AsyncMock(),
    )
    handler = next(
        item.callback
        for item in app.dispatcher.groups[0]
        if item.callback.__name__ == "start_guide"
    )
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
        await handler(app, callback)
    sent.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_profile_insights_are_private_and_always_scoped_to_the_sender():
    store = make_store()
    app = await native_app(store, Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(app, "profile")(make_message())
    store.users.collection_insights.assert_awaited_once_with(42)
    card = send.await_args.args[2]
    assert dict(card.fields)["Best recorded streak"] == "3 days"

    store.users.collection_insights.reset_mock()
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(app, "profile")(make_message(), SimpleNamespace(args="999"))
    store.users.collection_insights.assert_not_awaited()
    assert "only your own profile" in send.await_args.args[2].body


async def test_group_profile_uses_private_delivery_never_public_stats():
    store = make_store()
    app = await native_app(store, Settings(_env_file=None))
    with (
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private_send,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public_send,
    ):
        await find_handler(app, "profile")(make_message("supergroup"))
    private_send.assert_awaited_once()
    assert private_send.await_args.args[1:3] == (-100, 42)
    assert private_send.await_args.args[3].title == "Collector profile"
    public_send.assert_not_awaited()


async def test_profile_privacy_fallback_contains_no_collection_details():
    from wzgram.errors import BadRequest

    store = make_store()
    app = await native_app(store, Settings(_env_file=None))
    with (
        patch(
            "summon.bot.handlers.send_ephemeral_card",
            new_callable=AsyncMock,
            side_effect=BadRequest("unavailable"),
        ),
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public_send,
    ):
        await find_handler(app, "profile")(make_message("supergroup"))
    card = public_send.await_args.args[2]
    assert card.title == "Open your private chat" and not card.fields
    assert "Start SUMMON privately" in card.body


async def test_group_summon_insights_are_private_and_bound_to_group_and_sender():
    store = make_store()
    store.collection = SimpleNamespace(
        group_summon_insights=AsyncMock(
            return_value={
                "attempts": 8,
                "caught": 5,
                "expired": 1,
                "delivery_rejected": 1,
                "awaiting_resolution": 1,
                "my_catches": 2,
            }
        )
    )
    app = await native_app(store, Settings(_env_file=None, premium_emoji_enabled=False))
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as send:
        await find_handler(app, "group_stats")(make_message("supergroup"))
    assert store.collection.group_summon_insights.await_args.args[:2] == (-100, 42)
    card = send.await_args.args[3]
    assert card.emoji is Emoji.STATS
    assert dict(card.fields)["Your catches here"] == "2"
    assert "may not have been delivered" in card.body
    assert "999" not in card.telegram_html(premium=False)
    store.collection.group_summon_insights.reset_mock()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await find_handler(app, "group_stats")(
            make_message("supergroup"), SimpleNamespace(args="999")
        )
    store.collection.group_summon_insights.assert_not_awaited()


async def test_group_summon_insights_do_not_fall_back_to_public_private_content():
    from wzgram.errors import BadRequest

    store = make_store()
    store.collection = SimpleNamespace(
        group_summon_insights=AsyncMock(
            return_value={
                "attempts": 3,
                "caught": 2,
                "expired": 1,
                "delivery_rejected": 0,
                "awaiting_resolution": 0,
                "my_catches": 2,
            }
        )
    )
    app = await native_app(store, Settings(_env_file=None))
    with (
        patch(
            "summon.bot.handlers.send_ephemeral_card",
            new_callable=AsyncMock,
            side_effect=BadRequest("unavailable"),
        ),
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
    ):
        await find_handler(app, "group_stats")(make_message("supergroup"))
    card = public.await_args.args[2]
    assert card.title == "Open your private chat"
    assert "Your catches here" not in card.telegram_html()


async def test_group_summon_insights_rate_limit_and_sender_chat_fail_closed():
    store = make_store()
    store.collection = SimpleNamespace(group_summon_insights=AsyncMock())
    store.limiter.allow = AsyncMock(return_value=False)
    app = await native_app(store, Settings(_env_file=None))
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as send:
        await find_handler(app, "group_stats")(make_message("supergroup"))
    assert send.await_args.args[3].title == "Please slow down"
    store.collection.group_summon_insights.assert_not_awaited()
    store.limiter.allow.reset_mock()
    message = make_message("supergroup")
    message.sender_chat = SimpleNamespace(id=-200)
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as send:
        await find_handler(app, "group_stats")(message)
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
