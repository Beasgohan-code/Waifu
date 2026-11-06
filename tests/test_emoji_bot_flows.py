"""The supplied IDs are used in real bot responses, not only in a catalogue."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from summon.bot.handlers import build_router
from summon.bot.presentation import Emoji
from summon.config import Settings
from summon.repositories.users import DailyAlreadyClaimed


def make_store():
    return SimpleNamespace(
        collection=object(),
        catalog=SimpleNamespace(inventory=AsyncMock(return_value={"items": [], "total": 0})),
        users=SimpleNamespace(
            touch=AsyncMock(),
            profile=AsyncMock(
                return_value={
                    "level": 1,
                    "points": 2,
                    "unique_characters": 0,
                    "catalog_size": 3,
                    "catch_count": 0,
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
    )


def find_handler(router, name):
    return next(item.callback for item in router.message.handlers if item.callback.__name__ == name)


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
    router = build_router(make_store(), Settings(_env_file=None))
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
    router = build_router(make_store(), settings)
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
    router = build_router(make_store(), settings)
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
    router = build_router(make_store(), Settings(_env_file=None))
    with (
        patch("summon.bot.handlers.require_group_admin", new_callable=AsyncMock),
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as send,
    ):
        await find_handler(router, "spawns")(
            make_message("supergroup"), SimpleNamespace(args="bad")
        )
    assert send.await_args.args[3].emoji is Emoji.GEAR


async def test_already_claimed_daily_uses_hourglass():
    store = make_store()
    store.users.daily.side_effect = DailyAlreadyClaimed()
    router = build_router(store, Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, "daily")(make_message())
    assert send.await_args.args[2].emoji is Emoji.HOURGLASS


async def test_guess_guidance_uses_search_icon():
    router = build_router(make_store(), Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, "guess")(make_message(), SimpleNamespace(args=None))
    assert send.await_args.args[2].emoji is Emoji.SEARCH


async def test_start_uses_new_summon_brand_and_collection_button():
    router = build_router(
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
        ("profile", ("RARITY_STAR", "REWARD_COIN", "OPEN_COLLECTION", "SPARK")),
        ("daily", ("REWARD_COIN", "RARITY_STAR", "SPARK")),
    ],
)
async def test_profile_and_reward_fields_use_semantic_icons(handler_name, expected):
    router = build_router(make_store(), Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, handler_name)(make_message())
    card = send.await_args.args[2]
    assert card.field_icons == tuple(Emoji[name] for name in expected)
    assert len(card.field_icons) == len(card.fields)


async def test_group_rules_use_ios_document_icon():
    store = make_store()
    store.groups.get = AsyncMock(return_value={"settings": {"rules": "Be kind"}})
    router = build_router(store, Settings(_env_file=None))
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await find_handler(router, "rules")(make_message("supergroup"))
    assert send.await_args.args[2].emoji is Emoji.RULES
